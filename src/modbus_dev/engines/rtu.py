"""RTU FC03/04/06/16 适配器：复用 PyModbus 编码，使用绝对截止时间，不隐藏重试。"""
import struct

from pymodbus.framer.rtu import FramerRTU
from pymodbus.pdu import DecodePDU
from .master_pdu import request_pdu

from ..domain.events import Event
from ..domain.transactions import Reply


def open_serial(config):
    import os
    import serial
    if os.name == "nt":
        from .serial_windows import TimeoutSerial
        driver = TimeoutSerial
    else:
        driver = serial.Serial
    port = driver(port=None, baudrate=config.baud, bytesize=8,
                         parity=config.parity, stopbits=config.stop_bits,
                         timeout=0, write_timeout=.05, xonxoff=False, rtscts=False, dsrdtr=False)
    try:
        port.dtr = True
        port.rts = False
        if os.name == "nt" and config.rts_toggle:
            from serial.rs485 import RS485Settings
            port.rs485_mode = RS485Settings()
        # Ask POSIX for an exclusive port; Windows already uses exclusive opens.
        if os.name == "posix":
            port.exclusive = True
        port.port = config.port
        port.open()
        return port
    except BaseException:
        port.close()
        raise


class RtuEngine:
    def __init__(self, config, control, emit, port_factory=open_serial):
        self.config, self.control, self.emit = config, control, emit
        self.port_factory = port_factory
        self.port = None
        self.decoder = DecodePDU(is_server=False)
        self.framer = FramerRTU(self.decoder)
        self.quiet_until = 0.0
        self.uncertain = False  # Persists through reconnection.
        self.inflight = False

    @property
    def connected(self):
        return self.port is not None

    @property
    def request_gap_s(self):
        return self.config.serial.gap_ms / 1000

    def prepare_cycle(self, requests):
        """RTU 没有事务号容量限制，周期准备不改变串口状态。"""

    def connect(self):
        self.control.check()
        self.port = self.port_factory(self.config.serial)

    def close(self):
        port, self.port = self.port, None
        if self.inflight:
            self.mark_uncertain("Port closed with an unresolved transaction")
            self.inflight = False
        if port is not None:
            port.close()

    def mark_uncertain(self, reason):
        if not self.uncertain:
            self.uncertain = True
            self.emit(Event("ack_correlation_uncertain", reason, "WARNING", "communication"))

    def _event(self, code, message, level="DEBUG", **details):
        self.emit(Event(code, message, level, "communication", details))

    def packet(self, request):
        return self.framer.buildFrame(request_pdu(request, self.config.unit))

    def minimum_s(self, request):
        response_bytes = 8 if request.function in (6, 16) else 5 + 2 * request.count
        return ((len(self.packet(request)) + response_bytes) *
                self.config.serial.bits_per_character / self.config.serial.baud +
                self.config.serial.gap_ms / 1000)

    def _read(self, deadline, size=1):
        self.control.check()
        self.port.timeout = max(0, min(.01, deadline - self.control.now()))
        data = self.port.read(size)
        received = self.control.now()
        if data:
            self.quiet_until = received + self.config.serial.gap_ms / 1000
        self.control.check()
        return data, received

    def _idle(self, deadline):
        while self.control.now() < deadline:
            self.control.check()
            waiting = self.port.in_waiting
            if not waiting and self.control.now() >= self.quiet_until:
                return
            end = deadline if waiting else min(deadline, self.quiet_until)
            data, received = self._read(end, min(256, max(1, waiting)))
            if data:
                self.mark_uncertain("Bytes delivered outside the active transaction")
                self._event("idle_rx", "Late or unsolicited serial data", "WARNING",
                            hex=data.hex(), received_s=received)

    def exchange(self, request, deadline):
        self._idle(deadline)
        self.control.check()
        if self.control.now() + self.minimum_s(request) >= deadline:
            return Reply("schedule_missed", detail="Insufficient budget after RTU silence")
        packet = self.packet(request)
        self.port.write_timeout = max(.001, min(.05, deadline - self.control.now()))
        sent = self.control.now()
        size = None
        self.inflight = True
        try:
            size = self.port.write(packet)
        finally:
            self._event("tx", "Serial write attempted", hex=packet.hex(), sent_s=sent,
                        returned_s=self.control.now(), bytes_written=size)
        if size != len(packet):
            raise OSError(f"Short serial write: {size}/{len(packet)}")
        buffer = bytearray()
        total = 0
        while self.control.now() < deadline:
            data, received = self._read(deadline, min(256, max(1, self.port.in_waiting)))
            if not data:
                continue
            self._event("rx", "Serial data delivered", hex=data.hex(), received_s=received)
            total += len(data)
            buffer.extend(data)
            for offset in range(max(0, len(buffer) - 4)):
                if buffer[offset] != self.config.unit or buffer[offset + 1] not in (
                        request.function, request.function | 0x80):
                    continue
                part = bytes(buffer[offset:])
                cls = self.decoder.lookupPduClass(part)
                try:
                    length = cls.calculateRtuFrameSize(part)
                except (IndexError, struct.error):
                    continue
                if not 5 <= length <= 256 or len(part) < length:
                    continue
                candidate = part[:length]
                if not FramerRTU.check_CRC(candidate[:-2], int.from_bytes(candidate[-2:], "big")):
                    self.mark_uncertain("CRC rejected response candidate")
                    self._event("crc_rejected", "Response candidate failed CRC", "WARNING", hex=candidate.hex())
                    continue
                # 与 TCP 一致：零异常码不是有效拒绝，不能据此放行后续请求。
                if candidate[1] == request.function | 0x80 and candidate[2] == 0:
                    self.mark_uncertain("Exception response has a zero exception code")
                    self._event("reply_mismatch", "Exception response has a zero exception code",
                                "WARNING", exception_code=0, hex=candidate.hex())
                    continue
                # 先校验线上长度，防止库忽略奇数载荷末尾字节后误报有效读取。
                # 异常应答不含字节数字段，仍交由下方的异常 PDU 解码处理。
                if candidate[1] in (3, 4):
                    expected_bytes = request.count * 2
                    expected_length = 5 + expected_bytes
                    if candidate[2] != expected_bytes or len(candidate) != expected_length:
                        self.mark_uncertain("Read response byte count or frame length does not match request")
                        self._event("reply_mismatch", "Read response byte count or frame length does not match request",
                                    "WARNING", expected_count=request.count, expected_byte_count=expected_bytes,
                                    actual_byte_count=candidate[2], expected_frame_size=expected_length,
                                    actual_frame_size=len(candidate), hex=candidate.hex())
                        continue
                try:
                    pdu = self.decoder.decode(candidate[1:-2])
                except struct.error:
                    pdu = None
                if pdu is None:
                    self.mark_uncertain("Malformed response PDU")
                    continue
                if not pdu.isError():
                    if request.function == 6:
                        matches = pdu.address == request.address and tuple(pdu.registers) == request.registers
                    else:
                        matches = ((pdu.address, pdu.count) == (request.address, request.count)
                                   if request.function == 16 else len(pdu.registers) == request.count)
                    if not matches:
                        self.mark_uncertain("Response address or register quantity does not match")
                        self._event("reply_mismatch", "Response does not match requested range", "WARNING",
                                    expected_address=request.address, expected_count=request.count,
                                    actual_address=pdu.address, actual_count=pdu.count, hex=candidate.hex())
                        continue
                if total != length or offset:
                    self.mark_uncertain("Noise or extra bytes accompany the response")
                if received > deadline:
                    self.mark_uncertain("Reply delivered after deadline")
                    self.inflight = False
                    return Reply("late_reply", sent_s=sent, received_s=received, ambiguous=True)
                status = "exception" if pdu.isError() else "ack" if request.function in (6, 16) else "read"
                self.inflight = False
                return Reply(status, tuple(pdu.registers), sent, received, self.uncertain,
                             exception_code=pdu.exception_code if pdu.isError() else None)
            if len(buffer) > 256:
                del buffer[:-8]
                self.mark_uncertain("Response buffer overflow/noise")
        self.mark_uncertain("No matching response before transaction deadline")
        self.inflight = False
        return Reply("bad_reply" if total else "timeout", sent_s=sent, ambiguous=True,
                     detail=f"No matching response within budget; received {total} bytes")
