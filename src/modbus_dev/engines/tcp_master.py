"""有界 TCP 主站：复用库编解码，独立控制连接、整轮截止时间和逐笔应答关联。"""
import errno
import ipaddress
import select
import socket
import struct

from pymodbus.framer.socket import FramerSocket
from pymodbus.pdu import DecodePDU

from ..domain.events import Event
from ..domain.transactions import Reply, StopRequested
from .master_pdu import request_pdu

_CONNECT_PENDING = frozenset(getattr(errno, name) for name in (
    "EINPROGRESS", "EWOULDBLOCK", "EALREADY", "EINTR",
    "WSAEINPROGRESS", "WSAEWOULDBLOCK", "WSAEALREADY",
) if hasattr(errno, name))


class TcpMasterEngine:
    MAX_BUFFER = 4096
    WAIT_SLICE_S = .01
    request_gap_s = 0.0
    # TCP 的逐笔结果未知不等于 RTU 的会话级应答关联风险。
    uncertain = False

    def __init__(self, config, control, emit, *, socket_factory=socket.socket, waiter=select.select):
        self.config, self.control, self.emit = config, control, emit
        self.socket_factory, self.waiter = socket_factory, waiter
        self.socket = None
        self.decoder = DecodePDU(is_server=False)
        self.framer = FramerSocket(self.decoder)
        self.transaction_id = 0

    @property
    def connected(self):
        return self.socket is not None

    def _event(self, code, message, level="DEBUG", **details):
        self.emit(Event(code, message, level, "communication", details))

    def _wait(self, deadline, *, writing=False):
        # 所有短等待都使用同一绝对期限；分片、EINTR 和反复就绪不延长预算。
        while self.control.now() < deadline:
            self.control.check()
            sock = self.socket
            try:
                readable, writable, errors = self.waiter(
                    [] if writing else [sock], [sock] if writing else [], [sock],
                    min(self.WAIT_SLICE_S, max(0, deadline - self.control.now())))
            except InterruptedError:
                continue
            self.control.check()
            if self.control.now() >= deadline:
                return False
            if errors:
                raise OSError("TCP socket exception")
            if (writable if writing else readable):
                return True
        self.control.check()
        return False

    def connect(self):
        self.close()
        self.control.check()
        ip = ipaddress.ip_address(self.config.host)
        deadline = self.control.now() + self.config.connect_timeout_ms / 1000
        sock = self.socket_factory(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        self.socket = sock
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            result = sock.connect_ex((str(ip), self.config.port))
            if result not in (0, errno.EISCONN):
                if result not in _CONNECT_PENDING:
                    raise OSError(result, "TCP connect failed")
                if not self._wait(deadline, writing=True):
                    raise TimeoutError("TCP connection deadline expired")
                result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result:
                    raise OSError(result, "TCP connect failed")
            self.control.check()
            if self.control.now() >= deadline:
                raise TimeoutError("TCP connection delivered after deadline")
            self.transaction_id = 0
        except BaseException:
            self.close()
            raise

    def close(self):
        sock, self.socket = self.socket, None
        if sock is not None:
            sock.close()

    def minimum_s(self, request):
        # 网络和对端耗时不可由配置推导，不套用串口线速公式。
        return 0.0

    def prepare_cycle(self, requests):
        """整轮发送前预留事务号，轮换后的连接仍由应用层统一建立和统计。"""
        if self.connected and self.transaction_id + len(requests) > 0xffff:
            self._event("transaction_id_exhausted", "Rotate connection before reusing transaction identifiers")
            self.close()

    def packet(self, request, tid):
        pdu = request_pdu(request, self.config.unit)
        return self.framer.encode(bytes((request.function,)) + pdu.encode(), self.config.unit, tid)

    def _decode_reply(self, request, payload):
        function = payload[0]
        if function == request.function | 0x80:
            valid = len(payload) == 2 and payload[1] != 0
        elif function != request.function:
            valid = False
        elif function in (3, 4):
            valid = len(payload) == 2 + 2 * request.count and payload[1] == 2 * request.count
        else:
            expected = request.registers[0] if function == 6 else request.count
            valid = len(payload) == 5 and payload[1:] == struct.pack(">HH", request.address, expected)
        if not valid:
            raise ValueError("TCP response function, length or request fields do not match")
        pdu = self.decoder.decode(payload)
        if pdu is None:
            raise ValueError("Malformed TCP response PDU")
        return pdu

    def _receive(self, request, tid, deadline):
        buffer = bytearray()
        total = 0
        while self._wait(deadline):
            try:
                data = self.socket.recv(self.MAX_BUFFER)
            except BlockingIOError:
                continue
            received = self.control.now()
            self.control.check()
            if not data:
                raise ConnectionError("TCP peer closed before a matching response")
            self._event("rx", "TCP data delivered", hex=data.hex(), received_s=received)
            if received >= deadline:
                raise TimeoutError("TCP response delivered after deadline")
            total += len(data)
            buffer.extend(data)
            if len(buffer) > self.MAX_BUFFER:
                raise ValueError("TCP response buffer limit")
            while len(buffer) >= 7:
                length = int.from_bytes(buffer[4:6], "big")
                if buffer[2:4] != b"\x00\x00" or not 2 <= length <= 254:
                    raise ValueError("Invalid MBAP protocol or length")
                size = 6 + length
                if len(buffer) < size:
                    break
                frame = bytes(buffer[:size])
                del buffer[:size]
                _, unit, reply_tid, payload = self.framer.decode(frame)
                if reply_tid != tid:
                    self._event("transaction_id_mismatch", "Discarded response for another transaction", "WARNING",
                                transaction_id=tid, received_transaction_id=reply_tid, hex=frame.hex())
                    continue
                if unit != self.config.unit:
                    raise ValueError("TCP response Unit ID does not match")
                pdu = self._decode_reply(request, payload)
                if buffer:
                    # 不保留尚未发出请求的预先应答，防止后续事务被残余字节冒认。
                    self._event("unsolicited_tcp_data", "Closed connection with trailing response bytes", "WARNING",
                                hex=bytes(buffer).hex())
                    self.close()
                return pdu, received
        raise TimeoutError(f"No matching TCP response within cycle budget; received {total} bytes")

    def _unknown_write(self, request, tid, sent, written, packet_size):
        unknown = request.function in (6, 16) and written > 0
        if unknown:
            self._event("write_outcome_unknown", "Write may have executed; this transaction is not retried", "WARNING",
                        transaction_id=tid, function=request.function, address=request.address, count=request.count,
                        sent_s=sent, bytes_written=written, packet_size=packet_size)
        return unknown

    def exchange(self, request, deadline):
        self.control.check()
        if self.control.now() >= deadline:
            return Reply("schedule_missed", detail="Cycle deadline expired before TCP send")
        if not self.connected or self.transaction_id == 0xffff:
            raise OSError("TCP socket is not connected")
        sent, written, tid, packet = None, 0, self.transaction_id, b""
        try:
            self.transaction_id += 1
            tid = self.transaction_id
            packet = self.packet(request, tid)
            try:
                while written < len(packet):
                    if not self._wait(deadline, writing=True):
                        raise TimeoutError("TCP send deadline expired")
                    if sent is None:
                        sent = self.control.now()
                    try:
                        size = self.socket.send(packet[written:])
                    except BlockingIOError:
                        continue
                    if not size:
                        raise ConnectionError("TCP peer closed during send")
                    self._event("tx_chunk", "TCP bytes submitted", hex=packet[written:written + size].hex(),
                                sent_s=self.control.now(), transaction_id=tid)
                    written += size
            finally:
                if sent is not None:
                    self._event("tx", "TCP request send attempted", hex=packet.hex(), sent_s=sent,
                                returned_s=self.control.now(), bytes_written=written, transaction_id=tid)
            pdu, received = self._receive(request, tid, deadline)
            status = "exception" if pdu.isError() else "ack" if request.function in (6, 16) else "read"
            reply = Reply(status, tuple(pdu.registers), sent, received,
                          exception_code=pdu.exception_code if pdu.isError() else None)
            return reply
        except StopRequested:
            self._unknown_write(request, tid, sent, written, len(packet))
            self.close()
            raise
        except (OSError, ValueError) as exc:
            unknown = self._unknown_write(request, tid, sent, written, len(packet))
            self.close()
            status = "timeout" if isinstance(exc, TimeoutError) else "bad_reply" if isinstance(exc, ValueError) else "connection_lost"
            return Reply(status, sent_s=sent, detail=str(exc), outcome_unknown=unknown)
