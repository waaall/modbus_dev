"""独立字节对端及双进程验证 TCP 主站，不使用生产编码器生成测试应答。"""
from contextlib import contextmanager, redirect_stdout
import errno
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from modbus_dev.application.control import Control
from modbus_dev.application.session import SessionRunner
from modbus_dev.cli import main
from modbus_dev.domain.config import TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.transactions import Request, StopRequested
from modbus_dev.engines.tcp_master import TcpMasterEngine
from modbus_dev.reporting.segments import event_paths


def adu(tid, payload, unit=1, protocol=0):
    return struct.pack(">HHHB", tid, protocol, len(payload) + 1, unit) + payload


def read_reply(tid, words=(0x4148, 0), function=4, unit=1):
    return adu(tid, bytes((function, 2 * len(words))) + struct.pack(">" + "H" * len(words), *words), unit)


def configuration(**options):
    return TaskConfig.from_mapping(dict(mode="tcp-master", **options))


class Clock(Control):
    def __init__(self, stop_at=float("inf")):
        super().__init__()
        self.time, self.stop_at = 0., stop_at

    def now(self):
        return self.time

    def advance(self, seconds):
        self.time += max(0, seconds)
        if self.time >= self.stop_at:
            self.stop.set()

    def wait_until(self, deadline):
        self.advance(deadline - self.time)
        self.check()


class BytePeer:
    """模拟非阻塞 socket；时钟、碎片和故障均由用例显式控制。"""
    def __init__(self, clock, response=None, *, send_limit=260, delay=.002,
                 connect_result=0, connect_delay=0):
        self.clock, self.response = clock, response
        self.send_limit, self.delay = send_limit, delay
        self.connect_result, self.connect_delay = connect_result, connect_delay
        self.connected_at = None
        self.closed = False
        self.sent = bytearray()
        self.frames, self.pending = [], []
        self.registers = {}

    def setblocking(self, blocking):
        assert not blocking

    def setsockopt(self, *args):
        pass

    def getsockopt(self, *args):
        return 0

    def connect_ex(self, address):
        self.address = address
        self.connected_at = self.clock.now() + self.connect_delay
        return self.connect_result

    def close(self):
        self.closed = True

    def send(self, data):
        self.clock.advance(.0001)
        size = min(len(data), self.send_limit)
        if size == 0:
            raise BlockingIOError()
        self.sent.extend(data[:size])
        if len(self.sent) >= 7 and len(self.sent) == 6 + int.from_bytes(self.sent[4:6], "big"):
            frame = bytes(self.sent)
            self.sent.clear()
            self.frames.append((self.clock.now(), frame))
            tid, protocol, length, unit = struct.unpack(">HHHB", frame[:7])
            assert protocol == 0 and length == len(frame) - 6
            function, address, argument = struct.unpack(">BHH", frame[7:12])
            if function == 16:
                count = frame[12]
                assert count == 2 * argument and len(frame[13:]) == count
                words = struct.unpack(">" + "H" * argument, frame[13:])
                self.registers.update({address + i: word for i, word in enumerate(words)})
            elif function == 6:
                self.registers[address] = argument
            if self.response is not None:
                deliveries = self.response(frame)
            elif function in (6, 16):
                deliveries = [(self.delay, adu(tid, frame[7:12], unit))]
            else:
                words = tuple(self.registers.get(address + i, 0) for i in range(argument))
                deliveries = [(self.delay, read_reply(tid, words, function, unit))]
            self.pending.extend((self.clock.now() + after, chunk) for after, chunk in deliveries)
        return size

    def recv(self, limit):
        at, data = self.pending.pop(0)
        assert at <= self.clock.now() + 1e-9
        if len(data) > limit:
            self.pending.insert(0, (at, data[limit:]))
        return data[:limit]

    def wait(self, reads, writes, errors, timeout):
        if writes and self.clock.now() >= self.connected_at:
            self.clock.advance(.0001)
            return [], writes, []
        if reads and self.pending and self.pending[0][0] <= self.clock.now() + timeout:
            self.clock.advance(self.pending[0][0] - self.clock.now())
            return reads, [], []
        self.clock.advance(timeout)
        return [], [], []


class TcpConfigurationTests(unittest.TestCase):
    def test_file_default_read_ignores_inactive_integer_source(self):
        data = dict(schema_version=4, mode="tcp-master", points=[
            dict(data_type="uint16", source=dict(value=.5))])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_config(path), TaskConfig.from_mapping(data))
            data["access_mode"] = "read-write"
            data["points"][0]["operation"] = "write-single"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_defaults_and_roundtrip_are_safe_without_changing_rtu_defaults(self):
        for config in (configuration(), TaskConfig(mode="tcp-master")):
            config.validate()
            self.assertEqual(config.access_mode, "read-only")
            self.assertEqual(config.points[0].operation, "read-input")
            self.assertEqual(TaskConfig.from_mapping(config.to_mapping()), config)
            self.assertEqual(config.connect_timeout_ms, 1000)
        self.assertEqual(TaskConfig().access_mode, "read-write")
        self.assertEqual(TaskConfig().points[0].operation, "write-multiple")

    def test_writes_require_explicit_permission_and_fc06_requires_16_bits(self):
        for operation in ("write-single", "write-multiple"):
            data = dict(points=[dict(operation=operation, data_type="uint16")])
            with self.assertRaises(ConfigurationError):
                configuration(**data)
            self.assertTrue(configuration(access_mode="read-write", **data).usage.writing)
        with self.assertRaises(ConfigurationError):
            configuration(access_mode="read-write", points=[dict(operation="write-single")])

    def test_tcp_budget_is_independent_of_serial_and_connection_timeout(self):
        config = configuration(period_ms=50, timeout_ms=49, connect_timeout_ms=60000,
                               serial=dict(port="", baud="unused", parity="unused"))
        self.assertEqual(config.period_ms, 50)
        self.assertFalse(config.usage.serial)
        self.assertTrue(config.usage.field_active("connect_timeout_ms"))
        self.assertFalse(config.usage.field_active("serial.baud"))
        for options in (dict(timeout_ms=100), dict(timeout_ms=101), dict(connect_timeout_ms=0),
                        dict(connect_timeout_ms=True), dict(connect_timeout_ms=60001), dict(period_ms=49),
                        dict(access_mode=None)):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                configuration(**options)

    def test_numeric_ips_and_endpoint_validation(self):
        for ip in ("127.0.0.1", "192.0.2.1", "::1"):
            self.assertEqual(configuration(host=ip).host, ip)
        for options in (dict(host="localhost"), dict(host="0.0.0.999"), dict(port=0), dict(unit=0), dict(unit=248)):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                configuration(**options)

    def test_read_sources_are_inactive_and_areas_follow_operations(self):
        config = configuration(points=[dict(operation="read-input", area="holding", source=dict(kind="unused")),
                                       dict(operation="read-holding", area="input")])
        self.assertEqual([point.area for point in config.enabled_points], ["input", "holding"])
        self.assertFalse(config.usage.source)
        with self.assertRaises(ConfigurationError):
            configuration(points=[dict(operation="read-input"), dict(operation="read-input")])

    def test_validate_reports_tcp_budget_without_serial_line_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(configuration().to_mapping()))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["validate", str(path)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["timeout_basis"], "whole_cycle")
            self.assertEqual(result["request_count"], 1)
            self.assertNotIn("line_ms", result)


class TcpEngineTests(unittest.TestCase):
    def make_engine(self, response=None, *, clock=None, config=None, **options):
        clock = clock or Clock()
        peer = BytePeer(clock, response, **options)
        events = []
        engine = TcpMasterEngine(config or configuration(), clock, events.append,
                                 socket_factory=lambda *_: peer, waiter=peer.wait)
        engine.connect()
        self.addCleanup(engine.close)
        return engine, clock, peer, events

    def test_four_functions_and_independent_fixed_request_bytes(self):
        engine, clock, peer, events = self.make_engine()
        cases = [(Request(6, 5, 1, (0x1234,)), b"\x06\x00\x05\x12\x34", "ack"),
                 (Request(16, 6, 2, (0x4148, 0)), b"\x10\x00\x06\x00\x02\x04\x41\x48\x00\x00", "ack"),
                 (Request(3, 5, 3), b"\x03\x00\x05\x00\x03", "read"),
                 (Request(4, 0, 2), b"\x04\x00\x00\x00\x02", "read")]
        for index, (request, payload, status) in enumerate(cases, 1):
            reply = engine.exchange(request, clock.now() + .08)
            self.assertEqual(reply.status, status, reply)
            self.assertEqual(peer.frames[-1][1], adu(index, payload))
            self.assertFalse(reply.ambiguous)
        self.assertEqual(engine.transaction_id, 4)

    def test_partial_sends_and_fragmented_response_publish_one_complete_tx(self):
        packet = read_reply(1)
        response = lambda _: [(.002, packet[:3]), (.004, packet[3:8]), (.006, packet[8:])]
        engine, clock, peer, events = self.make_engine(response, send_limit=2)
        reply = engine.exchange(Request(4, 0, 2), .08)
        self.assertEqual(reply.registers, (0x4148, 0))
        tx = [event for event in events if event.code == "tx"]
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0].details["bytes_written"], 12)
        self.assertGreater(len([e for e in events if e.code == "tx_chunk"]), 1)

    def test_other_transaction_id_is_discarded_even_when_coalesced(self):
        engine, clock, peer, events = self.make_engine(lambda _: [(.001, read_reply(99) + read_reply(1))])
        self.assertEqual(engine.exchange(Request(4, 0, 2), .08).status, "read")
        self.assertTrue(any(e.code == "transaction_id_mismatch" for e in events))
        self.assertFalse(engine.uncertain)

    def test_exception_response_preserves_connection_and_is_not_unknown_write(self):
        engine, clock, peer, events = self.make_engine(lambda _: [(.001, adu(1, b"\x86\x02"))])
        reply = engine.exchange(Request(6, 0, 1, (3,)), .08)
        self.assertEqual((reply.status, reply.exception_code), ("exception", 2))
        self.assertFalse(reply.outcome_unknown)
        self.assertTrue(engine.connected)

    def test_malformed_mbap_unit_function_length_and_echo_are_rejected(self):
        cases = [(Request(4, 0, 2), read_reply(1, unit=2)),
                 (Request(4, 0, 2), read_reply(1, function=3)),
                 (Request(4, 0, 2), read_reply(1, words=(3,))),
                 (Request(4, 0, 2), adu(1, b"\x04\x04\x00\x00\x00\x00\x00")),
                 (Request(4, 0, 2), adu(1, b"\x04\x00", protocol=1)),
                 (Request(4, 0, 2), struct.pack(">HHHB", 1, 0, 255, 1)),
                 (Request(4, 0, 2), struct.pack(">HHHB", 1, 0, 1, 1)),
                 (Request(6, 0, 1, (3,)), adu(1, b"\x06\x00\x01\x00\x03")),
                 (Request(16, 0, 2, (3, 4)), adu(1, b"\x10\x00\x00\x00\x01"))]
        for request, packet in cases:
            with self.subTest(packet=packet.hex()):
                engine, clock, peer, events = self.make_engine(lambda _, p=packet: [(.001, p)])
                reply = engine.exchange(request, .08)
                self.assertEqual(reply.status, "bad_reply", reply)
                self.assertFalse(engine.connected)
                self.assertEqual(reply.outcome_unknown, request.function in (6, 16))

    def test_fragments_and_wrong_ids_never_extend_deadline(self):
        for deliveries in ([ (.03, read_reply(1)[:7]), (.06, b"\x04"), (.09, b"\x04") ],
                           [(n * .015, read_reply(100 + n)) for n in range(1, 10)]):
            engine, clock, peer, events = self.make_engine(lambda _, d=deliveries: d)
            reply = engine.exchange(Request(4, 0, 2), .08)
            self.assertEqual(reply.status, "timeout")
            self.assertLessEqual(clock.now(), .080001)
            self.assertFalse(engine.connected)

    def test_write_timeout_is_unknown_not_a_sticky_ack_correlation_flag(self):
        engine, clock, peer, events = self.make_engine(lambda _: [])
        reply = engine.exchange(Request(6, 0, 1, (3,)), .08)
        self.assertTrue(reply.outcome_unknown)
        self.assertFalse(reply.ambiguous)
        self.assertFalse(engine.uncertain)
        self.assertEqual(sum(e.code == "write_outcome_unknown" for e in events), 1)
        self.assertEqual(len(peer.frames), 1)

    def test_partial_write_does_not_publish_full_tx_or_retry(self):
        engine, clock, peer, events = self.make_engine(send_limit=2)
        original = peer.send
        def block_after_first(data):
            if peer.sent:
                clock.advance(.003)
                raise BlockingIOError()
            return original(data)
        peer.send = block_after_first
        reply = engine.exchange(Request(6, 0, 1, (3,)), .02)
        self.assertTrue(reply.outcome_unknown)
        self.assertEqual([e for e in events if e.code == "tx"][0].details["bytes_written"], 2)
        self.assertFalse(peer.frames)

    def test_eof_is_connection_lost_and_write_outcome_remains_unknown(self):
        engine, clock, peer, events = self.make_engine(lambda _: [(.001, b"")])
        reply = engine.exchange(Request(16, 0, 2, (1, 2)), .08)
        self.assertEqual(reply.status, "connection_lost")
        self.assertTrue(reply.outcome_unknown)
        self.assertTrue(peer.closed)

    def test_pending_connect_has_fixed_timeout_and_stop_closes_socket(self):
        for stop_at, expected in ((float("inf"), TimeoutError), (.025, StopRequested)):
            clock = Clock(stop_at)
            peer = BytePeer(clock, connect_result=errno.EINPROGRESS, connect_delay=10)
            engine = TcpMasterEngine(configuration(connect_timeout_ms=50), clock, lambda _: None,
                                     socket_factory=lambda *_: peer, waiter=peer.wait)
            with self.assertRaises(expected):
                engine.connect()
            self.assertTrue(peer.closed)
            self.assertLessEqual(clock.now(), .050001)

    def test_stop_during_receive_is_prompt_and_records_unknown_write(self):
        engine, clock, peer, events = self.make_engine(lambda _: [], clock=Clock(.025))
        with self.assertRaises(StopRequested):
            engine.exchange(Request(6, 0, 1, (3,)), .08)
        self.assertLess(clock.now(), .04)
        self.assertTrue(peer.closed)
        self.assertTrue(any(e.code == "write_outcome_unknown" for e in events))

    def test_trailing_preemptive_response_is_not_carried_to_future_request(self):
        engine, clock, peer, events = self.make_engine(lambda _: [(.001, read_reply(1) + read_reply(2))])
        self.assertEqual(engine.exchange(Request(4, 0, 2), .08).status, "read")
        self.assertFalse(engine.connected)

    def test_transaction_id_reuse_requires_new_connection(self):
        engine, clock, peer, events = self.make_engine()
        engine.transaction_id = 0xfffe
        self.assertEqual(engine.exchange(Request(4, 0, 2), .08).status, "read")
        second = BytePeer(clock)
        engine.socket_factory = lambda *_: second
        engine.waiter = second.wait
        engine.prepare_cycle([Request(4, 0, 2)])
        self.assertFalse(engine.connected)
        engine.connect()
        self.assertEqual(engine.exchange(Request(4, 0, 2), .08).status, "read")
        self.assertTrue(peer.closed)
        self.assertEqual(int.from_bytes(peer.frames[0][1][:2], "big"), 0xffff)
        self.assertEqual(int.from_bytes(second.frames[0][1][:2], "big"), 1)

    def test_entire_cycle_reserves_identifiers_before_any_send(self):
        engine, clock, peer, events = self.make_engine()
        engine.transaction_id = 0xfffe
        engine.prepare_cycle([Request(4, 0, 2), Request(3, 0, 2)])
        self.assertFalse(engine.connected)
        self.assertFalse(peer.frames)


class TcpSessionTests(unittest.TestCase):
    def exercise(self, config, response=None, *, peer_options=None, stop_at=float("inf")):
        clock = Clock(stop_at)
        peers = []
        connect_times = []
        def factory(c, t, emit):
            def new_socket(*_):
                peer = BytePeer(clock, response, **(peer_options or {}))
                peers.append(peer)
                connect_times.append(clock.now())
                return peer
            def wait(*args):
                return peers[-1].wait(*args)
            return TcpMasterEngine(c, t, emit, socket_factory=new_socket, waiter=wait)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config, Path(root) / "run", control=clock, engine_factory=factory)
            result = runner.run()
            events = [json.loads(line) for path in event_paths(runner.directory) for line in path.read_text().splitlines()]
            live = runner.live.snapshot(clock.now(), runner.state)
        return result, peers, connect_times, events, live

    def test_all_requests_and_readbacks_use_one_cycle_deadline(self):
        config = configuration(duration_s=.1, timeout_ms=50, access_mode="read-write", points=[
            dict(operation="write-single", data_type="uint16", readback_every=1),
            dict(operation="read-input", address=4)])
        result, peers, _, events, _ = self.exercise(config, peer_options=dict(delay=.03))
        self.assertEqual(result["counters"]["timeout"], 1)
        self.assertEqual(len(peers[0].frames), 2)
        transactions = [e["details"] for e in events if e["code"] == "transaction"]
        self.assertEqual(len({item["deadline_s"] for item in transactions}), 1)
        self.assertLess(result["elapsed_s"], .16)

    def test_continuous_reconnect_is_rate_limited_and_failed_write_not_resent(self):
        config = configuration(duration_s=2.3, access_mode="read-write", points=[
            dict(operation="write-single", data_type="uint16", source=dict(kind="triangle", lower=0, upper=100))])
        result, peers, times, events, live = self.exercise(config, lambda _: [])
        self.assertEqual(result["exit_code"], 0)
        self.assertGreaterEqual(len(peers), 2)
        self.assertTrue(all(second - first >= 1 for first, second in zip(times, times[1:])))
        self.assertTrue(all(len(peer.frames) == 1 for peer in peers))
        written_values = [int.from_bytes(peer.frames[0][1][-2:], "big") for peer in peers]
        self.assertEqual(written_values[0], 0)
        self.assertGreater(written_values[1], 1)
        self.assertFalse(result["ack_correlation_uncertain"])
        self.assertEqual(result["counters"]["write_outcome_unknown"], len(peers))
        self.assertEqual(live["rows"][0]["confirmation"], "outcome-unknown")

    def test_strict_timeout_stops_after_one_request(self):
        result, peers, _, _, _ = self.exercise(configuration(policy="strict", duration_s=2), lambda _: [])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(peers), 1)
        self.assertEqual(len(peers[0].frames), 1)

    def test_connect_failure_policy_and_no_serial_metadata(self):
        for policy in ("continuous", "strict"):
            result, peers, times, events, _ = self.exercise(configuration(policy=policy, duration_s=2.2),
                                                         peer_options=dict(connect_result=errno.ECONNREFUSED))
            self.assertEqual(result["exit_code"], 1 if policy == "strict" else 0)
            self.assertTrue(all(peer.closed for peer in peers))
            self.assertTrue(all(b - a >= 1 for a, b in zip(times, times[1:])))
            failures = [e for e in events if e["code"] == "tcp_connect_failed"]
            self.assertEqual(failures[0]["details"]["host"], "127.0.0.1")
            self.assertEqual(failures[0]["details"]["port"], 1502)
            self.assertNotIn("port_open_failed", [e["code"] for e in events])

    def test_actual_tx_and_confirmed_readback_are_separate(self):
        config = configuration(duration_s=.2, access_mode="read-write", points=[
            dict(operation="write-multiple", readback_every=1, source=dict(value=12.5))])
        result, peers, _, events, live = self.exercise(config)
        self.assertEqual(result["reply_summary"], dict(valid_ack=2, valid_read=2))
        self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)
        self.assertEqual({row["value"] for row in live["rows"]}, {12.5})
        self.assertEqual(next(row for row in live["rows"] if row["direction"] == "tx")["confirmation"], "confirmed")
        self.assertTrue(all(peer.closed for peer in peers))

    def test_all_numeric_types_and_orders_share_one_write_readback_plan(self):
        values = (0x1234, -32768, -2147483648, 0x89abcdef, 12.5)
        points = [dict(address=address, data_type=kind, byte_order=order, operation="write-multiple",
                       source=dict(value=value), readback_every=1)
                  for address, kind, order, value in zip((0, 1, 2, 4, 6),
                      ("uint16", "int16", "int32", "uint32", "float32"),
                      ("B21", "B12", "B3412", "B4321", "B2143"), values)]
        result, peers, _, events, live = self.exercise(configuration(
            duration_s=.1, access_mode="read-write", points=points))
        self.assertEqual(result["reply_summary"], dict(valid_ack=1, valid_read=1))
        expected_words = (0x3412, 0x8000, 0, 0x8000, 0xefcd, 0xab89, 0x4841, 0)
        self.assertEqual(peers[0].frames[0][1], adu(1, b"\x10\x00\x00\x00\x08\x10" + struct.pack(">8H", *expected_words)))
        actual = {row["point"]: row["value"] for row in live["rows"] if row["direction"] == "rx"}
        self.assertEqual(actual, dict(enumerate(values)))

    def test_partial_write_and_stop_never_invent_a_submitted_value(self):
        result, peers, _, events, live = self.exercise(configuration(duration_s=.1, access_mode="read-write",
            points=[dict(operation="write-single", data_type="uint16")]), peer_options=dict(send_limit=0), stop_at=.125)
        self.assertEqual(result["exit_code"], 130)
        self.assertEqual(live["rows"], [])
        self.assertEqual(result["counters"].get("write_outcome_unknown", 0), 0)

    def test_strict_readback_mismatch_preserves_tx_and_stops(self):
        def mismatch(frame):
            tid = int.from_bytes(frame[:2], "big")
            return [(.001, adu(tid, frame[7:12]) if frame[7] == 16 else read_reply(tid, (0, 0), function=3))]
        result, peers, _, events, live = self.exercise(configuration(policy="strict", duration_s=1,
            access_mode="read-write", points=[dict(operation="write-multiple", source=dict(value=12.5), readback_every=1)]), mismatch)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["counters"]["readback_mismatch"], 1)
        self.assertEqual(len(peers[0].frames), 2)
        self.assertEqual(next(row for row in live["rows"] if row["direction"] == "tx")["value"], 12.5)


@contextmanager
def slave_process(directory, points):
    """真正独立的现有从站进程，临时端口不依赖机器上的固定服务。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = Path(directory)
    config = TaskConfig.from_mapping(dict(mode="tcp-slave", port=port, access_mode="read-write",
                                         duration_s=0, period_ms=100, points=points))
    path = root / "slave-config.json"
    path.write_text(json.dumps(config.to_mapping()), encoding="utf-8")
    with (root / "slave-stdout.log").open("w") as out, (root / "slave-stderr.log").open("w") as err:
        process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(path), "--output", str(root / "slave")],
                                   stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"Slave exited: {(root / 'slave-stderr.log').read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Slave did not listen within 5 seconds")
                    time.sleep(.02)
            yield port
        finally:
            # worker 的协议停止会等待日志收尾，Windows SIGTERM 则强制终止。
            try:
                process.communicate(input=b'{"schema_version":1,"command":"stop"}\n', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


class TcpProcessTests(unittest.TestCase):
    def test_independent_real_peer_fragmentation_and_transaction_matching(self):
        failures = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            def serve():
                try:
                    client, _ = listener.accept()
                    with client:
                        client.settimeout(2)
                        request = bytearray()
                        while len(request) < 12:
                            chunk = client.recv(12 - len(request))
                            if not chunk:
                                raise AssertionError("Master closed before sending complete request")
                            request.extend(chunk)
                        if bytes(request) != adu(1, b"\x04\x00\x00\x00\x02"):
                            raise AssertionError(request.hex())
                        response = read_reply(99) + read_reply(1)
                        for fragment in (response[:3], response[3:10], response[10:]):
                            client.sendall(fragment)
                            time.sleep(.005)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=serve)
            thread.start()
            engine = TcpMasterEngine(configuration(port=listener.getsockname()[1]), Control(), lambda _: None)
            try:
                engine.connect()
                reply = engine.exchange(Request(4, 0, 2), time.perf_counter() + .3)
                self.assertEqual((reply.status, reply.registers), ("read", (0x4148, 0)))
            finally:
                engine.close()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures, failures)

    def test_cli_master_and_existing_slave_four_functions_and_two_areas(self):
        slave_points = [dict(area="input", source=dict(value=12.5)),
                        dict(area="holding", address=4, data_type="uint16", source=dict(value=9)),
                        dict(area="holding", address=8, data_type="uint16"),
                        dict(area="holding", address=12, byte_order="B3412")]
        master_points = [dict(operation="read-input"),
                         dict(operation="read-holding", address=4, data_type="uint16"),
                         dict(operation="write-single", address=8, data_type="uint16", source=dict(value=11), readback_every=1),
                         dict(operation="write-multiple", address=12, byte_order="B3412", source=dict(value=3.25), readback_every=1)]
        with tempfile.TemporaryDirectory() as directory, slave_process(directory, slave_points) as port:
            root = Path(directory)
            config = configuration(port=port, duration_s=.4, period_ms=100, timeout_ms=90,
                                   access_mode="read-write", points=master_points)
            path = root / "master-config.json"
            path.write_text(json.dumps(config.to_mapping()), encoding="utf-8")
            process = subprocess.run([sys.executable, "-m", "modbus_dev", "run", str(path),
                                      "--output", str(root / "master")], capture_output=True, text=True, timeout=8)
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            result = json.loads(process.stdout)
            self.assertGreaterEqual(result["reply_summary"]["valid_ack"], 2)
            self.assertGreaterEqual(result["reply_summary"]["valid_read"], 4)
            self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)
            self.assertEqual(result["counters"].get("timeout", 0), 0)
            events = [json.loads(line) for p in event_paths(root / "master") for line in p.read_text().splitlines()]
            requests = [e["details"] for e in events if e["code"] == "transaction"]
            self.assertEqual({r["function"] for r in requests}, {3, 4, 6, 16})
            self.assertEqual(result["counters"]["connection_opens"], 1)
            values = [e["details"] for e in events if e["code"] == "register_values"]
            self.assertTrue(any(v["area"] == "input" and v["direction"] == "rx" for v in values))


if __name__ == "__main__":
    unittest.main()
