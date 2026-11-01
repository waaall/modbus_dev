"""有界 TCP 传输；单事件循环维护客户端，并复用独立寄存器区。"""
from dataclasses import dataclass, field
from collections import deque
import errno
import os
import selectors
import socket

from pymodbus.framer.socket import FramerSocket
from pymodbus.pdu.decoders import DecodePDU

from .register_banks import RegisterBanks


# POSIX 与 Winsock 的暂态接受错误只影响本次连接，不应终止监听会话。
_ACCEPT_RETRY_ERRORS = frozenset(getattr(errno, name) for name in (
    "EAGAIN", "EWOULDBLOCK", "EINTR", "ECONNABORTED", "ECONNRESET",
    "WSAEWOULDBLOCK", "WSAEINTR", "WSAECONNABORTED", "WSAECONNRESET",
) if hasattr(errno, name))


@dataclass
class Client:
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    pending_since: float | None = None
    submitted_bytes: int = 0
    queued_bytes: int = 0
    values_pending: deque = field(default_factory=deque)
    read_eof: bool = False
    drain_since: float | None = None


class TcpSlaveEngine(RegisterBanks):
    MAX_CLIENTS = 16
    MAX_BUFFER = 4096

    def __init__(self, config, control, emit):
        super().__init__(config, control, emit)
        self.selector = selectors.DefaultSelector()
        self.listener = None
        self.clients = {}
        self.framer = FramerSocket(DecodePDU(True))
        self.bound_address = None
        self.client_timeout_s = max(1, config.timeout_ms / 1000)

    def connect(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener = listener
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name == "posix":
            # 允许正常关闭后的同端口重启；不启用 SO_REUSEPORT，不共享活动监听端口。
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.config.host, self.config.port))
        listener.listen(self.MAX_CLIENTS)
        listener.setblocking(False)
        self.bound_address = listener.getsockname()
        self.selector.register(listener, selectors.EVENT_READ)

    def _drop(self, sock):
        if self.clients.pop(sock, None) is None:
            return
        try:
            self.selector.unregister(sock)
        finally:
            sock.close()

    def _accept_client(self):
        try:
            client, _ = self.listener.accept()
        except OSError as exc:
            if exc.errno not in _ACCEPT_RETRY_ERRORS:
                raise
            self._event("accept_retry", reason=str(exc), errno=exc.errno)
            return
        if len(self.clients) >= self.MAX_CLIENTS:
            client.close()
            self._event("client_limit")
            return
        try:
            client.setblocking(False)
            self.selector.register(client, selectors.EVENT_READ)
        except (OSError, ValueError) as exc:
            client.close()
            self._event("client_closed", reason=str(exc))
            return
        self.clients[client] = Client()

    def _receive_client(self, sock, state):
        try:
            data = sock.recv(self.MAX_BUFFER)
        except BlockingIOError:
            return
        received = self.control.now()
        if not data:
            # 读 EOF 仅结束请求流：完整请求的响应仍须发送，不等待无法补全的尾帧。
            state.read_eof = True
            state.drain_since = received
            state.incoming.clear()
            state.pending_since = None
            return
        self._event("rx", peer=sock.getpeername(), data=data.hex())
        if not state.incoming:
            state.pending_since = received
        state.incoming.extend(data)
        if len(state.incoming) > self.MAX_BUFFER:
            raise ValueError("TCP input buffer limit")
        while len(state.incoming) >= 7:
            length = int.from_bytes(state.incoming[4:6], "big")
            if state.incoming[2:4] != b"\x00\x00" or not 2 <= length <= 254:
                raise ValueError("Invalid MBAP protocol or length")
            size = 6 + length
            if len(state.incoming) < size:
                break
            frame = bytes(state.incoming[:size])
            del state.incoming[:size]
            # 新残余帧从本次交付开始计时；同一帧的后续碎片不重置期限。
            state.pending_since = received if state.incoming else None
            _, unit, tid, pdu = self.framer.decode(frame)
            response = self.response(unit, pdu)
            if response is not None:
                encoded = self.framer.encode(response, unit, tid)
                state.outgoing.extend(encoded)
                # 将数值快照绑定到响应结束的字节边界，排队本身不算实际 TX。
                state.queued_bytes += len(encoded)
                if self._pending_values is not None:
                    state.values_pending.append((state.queued_bytes, self._pending_values))
                self._pending_values = None
                if len(state.outgoing) > self.MAX_BUFFER:
                    raise ValueError("TCP output buffer limit")

    def _send_client(self, sock, state):
        try:
            sent = sock.send(state.outgoing)
        except BlockingIOError:
            return
        if not sent:
            raise OSError("TCP connection closed during write")
        self._event("tx", peer=sock.getpeername(), data=bytes(state.outgoing[:sent]).hex())
        del state.outgoing[:sent]
        state.submitted_bytes += sent
        # 完整响应交给 socket 后才发布数值，部分发送不能提前报成功。
        while state.values_pending and state.values_pending[0][0] <= state.submitted_bytes:
            _, values = state.values_pending.popleft()
            self._event('register_values', direction='tx', quality='submitted',
                        sampled_s=self.control.now(), **values)

    def _expire_client(self, sock, state):
        now = self.control.now()
        reason = None
        if state.pending_since is not None and now - state.pending_since > self.client_timeout_s:
            reason = "Incomplete request deadline"
        elif state.drain_since is not None and now - state.drain_since > self.client_timeout_s:
            reason = "Response drain deadline"
        if reason is None:
            return False
        self._event("client_closed", reason=reason)
        self._drop(sock)
        return True

    def poll(self, deadline):
        self.control.check()
        for key, mask in self.selector.select(max(0, min(.02, deadline - self.control.now()))):
            self.control.check()
            sock = key.fileobj
            if sock is self.listener:
                self._accept_client()
                continue
            # kqueue 同批可分别返回读写事件，前一项已注销的连接必须跳过。
            state = self.clients.get(sock)
            if state is None or self._expire_client(sock, state):
                continue
            try:
                if mask & selectors.EVENT_READ and not state.read_eof:
                    self._receive_client(sock, state)
                if mask & selectors.EVENT_WRITE and state.outgoing:
                    self._send_client(sock, state)
                if state.read_eof and not state.outgoing:
                    self._drop(sock)
                    continue
                # 半关闭后只等待可写，避免 EOF 持续就绪造成忙轮询。
                self.selector.modify(sock, (0 if state.read_eof else selectors.EVENT_READ) |
                                     (selectors.EVENT_WRITE if state.outgoing else 0))
            except (OSError, ValueError) as exc:
                self._event("client_closed", reason=str(exc))
                self._drop(sock)
        # 无就绪事件的慢连接也要清理；半关闭响应发送采用同一固定超时下限。
        for sock, state in list(self.clients.items()):
            self._expire_client(sock, state)

    def close(self):
        for sock in list(self.clients):
            self._drop(sock)
        if self.listener is not None:
            self.listener.close()
        self.selector.close()
