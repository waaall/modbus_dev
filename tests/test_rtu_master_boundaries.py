"""使用独立字节对端检查 RTU 主站读取应答的原始长度及严格会话边界。"""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.transactions import Request
from modbus_dev.engines.rtu import RtuEngine
from test_rtu_session import FakeControl, Peer, frame


READ_OPERATIONS = {3: "read-holding", 4: "read-input"}


def read_frame(unit, function, payload, *, byte_count=None):
    """声明长度和实际载荷可独立设置，CRC 始终按真实字节计算。"""
    declared = len(payload) if byte_count is None else byte_count
    return frame(bytes((unit, function, declared)) + payload)


class ResponsePeer(Peer):
    """沿用可控时钟和串口读缓冲，仅由用例决定每次请求的应答与分片。"""
    def __init__(self, clock, responses):
        super().__init__(clock)
        self.responses = responses

    def write(self, data):
        if frame(data[:-2]) != data:
            raise AssertionError("Invalid request CRC")
        self.writes.append((self.clock.now(), data))
        self.pending.extend((self.clock.now() + delay, response)
                            for delay, response in self.responses(data))
        return len(data)


class RtuMasterBoundaryTests(unittest.TestCase):
    def make_engine(self, function, responses, count=1):
        config = TaskConfig.from_mapping(dict(mode="rtu-master", access_mode="read-only",
            serial=dict(port="TEST", baud=115200), points=[
                dict(operation=READ_OPERATIONS[function], data_type="uint16", address=index)
                for index in range(count)]))
        clock, events = FakeControl(), []
        peer = ResponsePeer(clock, responses)
        engine = RtuEngine(config, clock, events.append, lambda _: peer)
        engine.connect()
        self.addCleanup(engine.close)
        return engine, clock, peer, events

    def test_byte_count_must_match_requested_register_width(self):
        for function in READ_OPERATIONS:
            for count in (1, 2):
                expected_bytes = count * 2
                for byte_count in (0, expected_bytes - 1, expected_bytes + 1, expected_bytes + 2):
                    with self.subTest(function=function, count=count, byte_count=byte_count):
                        response = read_frame(1, function, b"\x12" * byte_count)
                        engine, clock, peer, _ = self.make_engine(function,
                            lambda _, response=response: [(.005, response)], count)
                        reply = engine.exchange(Request(function, 0, count), .08)
                        self.assertEqual(reply.status, "bad_reply", reply)
                        self.assertEqual(reply.registers, ())
                        self.assertTrue(reply.ambiguous)
                        self.assertAlmostEqual(clock.now(), .08)
                        self.assertEqual(len(peer.writes), 1)

    def test_payload_length_must_match_declared_byte_count(self):
        for function in READ_OPERATIONS:
            for count in (1, 2):
                expected_bytes = count * 2
                for difference in (-1, 1, 2):
                    with self.subTest(function=function, count=count, difference=difference):
                        response = read_frame(1, function, b"\x12" * (expected_bytes + difference),
                                              byte_count=expected_bytes)
                        engine, _, _, _ = self.make_engine(function,
                            lambda _, response=response: [(.005, response)], count)
                        reply = engine.exchange(Request(function, 0, count), .08)
                        self.assertEqual(reply.status, "bad_reply", reply)
                        self.assertEqual(reply.registers, ())
                        self.assertTrue(reply.ambiguous)

    def test_valid_reads_support_single_multi_register_and_fragmentation(self):
        for function in READ_OPERATIONS:
            for words in ((0x1234,), (0x1234, 0xABCD)):
                response = read_frame(1, function, struct.pack(">" + "H" * len(words), *words))
                for split in (False, True):
                    with self.subTest(function=function, count=len(words), split=split):
                        deliveries = [(.005, response[:3]), (.01, response[3:])] if split else [(.005, response)]
                        engine, _, _, events = self.make_engine(function,
                            lambda _, deliveries=deliveries: deliveries, len(words))
                        reply = engine.exchange(Request(function, 0, len(words)), .08)
                        self.assertEqual(reply.status, "read", reply)
                        self.assertEqual(reply.registers, words)
                        self.assertFalse(reply.ambiguous)
                        self.assertFalse(any(event.code == "reply_mismatch" for event in events))

    def test_exception_reply_keeps_modbus_exception_status(self):
        for function in READ_OPERATIONS:
            with self.subTest(function=function):
                response = frame(bytes((1, function | 0x80, 2)))
                engine, _, _, _ = self.make_engine(function, lambda _: [(.005, response)])
                reply = engine.exchange(Request(function, 0, 1), .08)
                self.assertEqual(reply.status, "exception", reply)
                self.assertEqual(reply.exception_code, 2)
                self.assertFalse(reply.ambiguous)

    def test_malformed_then_valid_reply_remains_ambiguous(self):
        for function in READ_OPERATIONS:
            with self.subTest(function=function):
                malformed = read_frame(1, function, bytes.fromhex("123456"))
                valid = read_frame(1, function, bytes.fromhex("ABCD"))
                engine, clock, peer, events = self.make_engine(function,
                    lambda _: [(.005, malformed), (.025, valid)])
                reply = engine.exchange(Request(function, 0, 1), .08)
                self.assertEqual(reply.status, "read", reply)
                self.assertEqual(reply.registers, (0xABCD,))
                self.assertTrue(reply.ambiguous)
                self.assertAlmostEqual(clock.now(), .025)
                self.assertEqual(len(peer.writes), 1)
                self.assertTrue(any(event.code == "reply_mismatch" for event in events))

    def test_strict_sessions_reject_malformed_reads_and_readbacks(self):
        operations = (*READ_OPERATIONS.values(), "write-single", "write-multiple")
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                value = 0x1234
                writing = operation.startswith("write")

                def responses(request):
                    if request[1] in READ_OPERATIONS:
                        # 多余字节不能被解码器忽略后伪装成与写入值一致的成功读回。
                        payload = struct.pack(">H", value) + b"\x56"
                        response = read_frame(request[0], request[1], payload)
                    else:
                        response = frame(request[:6])
                    return [(.005, response)]

                clock = FakeControl()
                peer = ResponsePeer(clock, responses)
                config = TaskConfig.from_mapping(dict(mode="rtu-master", policy="strict", duration_s=.3,
                    serial=dict(port="TEST", baud=115200), points=[dict(operation=operation,
                        data_type="uint16", readback_every=int(writing), source=dict(value=value))]))
                runner = SessionRunner(config, Path(root) / "run", control=clock,
                    engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
                result = runner.run()
                self.assertEqual(result["exit_code"], 1, result)
                self.assertEqual(result["counters"].get("bad_reply"), 1)
                self.assertEqual(result["counters"].get("read", 0), 0)
                self.assertEqual(result["counters"].get("ack", 0), int(writing))
                self.assertEqual(len(peer.writes), 2 if writing else 1)
                self.assertTrue(peer.closed)
                rows = runner.live.snapshot(clock.now(), runner.state)["rows"]
                self.assertFalse(any(row["direction"] == "rx" for row in rows))
                summary = json.loads((runner.directory / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["run"]["exit_code"], result["exit_code"])


if __name__ == "__main__":
    unittest.main()
