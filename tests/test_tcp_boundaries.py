"""TCP 连接边界回归：分片期限、半关闭、残留事件和接受连接异常隔离。"""
from contextlib import contextmanager
from dataclasses import replace
import errno
import selectors
import socket
import time
import unittest
from unittest.mock import Mock, patch

from modbus_dev.application.control import Control
from modbus_dev.domain.config import TaskConfig
from modbus_dev.engines.tcp_slave import TcpSlaveEngine
from test_tcp_slave import frame, receive


class ManualControl(Control):
    """只替换单调时间，保留真实回环 socket 与选择器，避免等待超时测试。"""
    def __init__(self):
        super().__init__()
        self.time = 0.

    def now(self):
        return self.time

    def advance(self, seconds):
        self.time += seconds


@contextmanager
def connected_engine(*, selector_factory=None, control=None):
    config = TaskConfig.from_mapping(dict(mode="tcp-slave", timeout_ms=1000,
        access_mode="read-write", points=[dict(area="holding", data_type="uint16", source=dict(value=7))]))
    events = []
    engine = TcpSlaveEngine(replace(config, port=0), control or Control(), events.append)
    if selector_factory is not None:
        engine.selector.close()
        engine.selector = selector_factory()
    client = None
    try:
        engine.connect()
        engine.publish({"holding": {0: 7}, "input": {}})
        client = socket.create_connection(engine.bound_address, timeout=1)
        pump_until(engine, lambda: bool(engine.clients))
        yield engine, client, events
    finally:
        if client is not None:
            client.close()
        engine.close()


def pump(engine):
    engine.poll(engine.control.now() + .03)


def pump_until(engine, predicate, timeout=1):
    end = time.perf_counter() + timeout
    while not predicate():
        if time.perf_counter() >= end:
            raise AssertionError("TCP 轮询未在期限内达到预期状态")
        pump(engine)


def exchange(engine, client, packet, events):
    previous = sum(event.code == "tx" for event in events)
    client.sendall(packet)
    pump_until(engine, lambda: sum(event.code == "tx" for event in events) > previous)
    return receive(client)


class TcpBoundaryTests(unittest.TestCase):
    def test_new_partial_request_gets_an_independent_deadline(self):
        clock = ManualControl()
        with connected_engine(control=clock) as (engine, client, events):
            request = bytes.fromhex("0300000001")
            prefix = 4
            client.sendall(frame(request, tid=0)[:prefix])
            pump(engine)
            for tid in range(9):
                clock.advance(.2)
                # 每笔仅等待 200ms，交付时同时结束前一帧并开始下一帧。
                packet = frame(request, tid=tid)[prefix:] + frame(request, tid=tid + 1)[:prefix]
                self.assertEqual(exchange(engine, client, packet, events), frame(bytes.fromhex("03020007"), tid=tid))
                self.assertEqual(next(iter(engine.clients.values())).pending_since, clock.now())

    def test_trickle_bytes_do_not_extend_the_same_request_deadline(self):
        clock = ManualControl()
        with connected_engine(control=clock) as (engine, client, events):
            packet = frame(bytes.fromhex("0300000001"))
            client.sendall(packet[:2])
            pump(engine)
            clock.advance(.6)
            client.sendall(packet[2:4])
            pump(engine)
            self.assertEqual(next(iter(engine.clients.values())).pending_since, 0.)
            clock.advance(.41)
            pump(engine)
            self.assertFalse(engine.clients)
            self.assertEqual(client.recv(1), b"")
            self.assertTrue(any(event.details.get("reason") == "Incomplete request deadline" for event in events))

    def test_expired_partial_request_cannot_be_completed_after_its_deadline(self):
        clock = ManualControl()
        with connected_engine(control=clock) as (engine, client, events):
            packet = frame(bytes.fromhex("0600000009"))
            client.sendall(packet[:4])
            pump(engine)
            clock.advance(1.01)
            client.sendall(packet[4:])
            pump(engine)
            self.assertFalse(engine.clients)
            self.assertEqual(engine.holding[0], 7)
            self.assertFalse(any(event.code == "slave_request" for event in events))

    def test_half_close_drains_read_write_responses_and_ignores_incomplete_tail(self):
        for selector_factory in (None, selectors.SelectSelector):
            with self.subTest(selector=selector_factory), connected_engine(
                    selector_factory=selector_factory) as (engine, client, events):
                write = bytes.fromhex("0600000009")
                read = bytes.fromhex("0300000001")
                client.sendall(frame(write, tid=1) + frame(read, tid=2) + frame(read, tid=3)[:4])
                client.shutdown(socket.SHUT_WR)
                pump_until(engine, lambda: not engine.clients)
                self.assertEqual(receive(client), frame(write, tid=1))
                self.assertEqual(receive(client), frame(bytes.fromhex("03020009"), tid=2))
                self.assertEqual(client.recv(1), b"")
                self.assertEqual(engine.holding[0], 9)
                # TX 数值仍只在完整响应交给 socket 后发布一次。
                values = [event for event in events if event.code == "register_values"]
                self.assertEqual([event.details["direction"] for event in values], ["rx", "tx"])

    def test_stale_ready_event_for_a_dropped_client_is_ignored(self):
        with connected_engine() as (engine, client, _):
            server = next(iter(engine.clients))
            key = engine.selector.get_key(server)
            client.shutdown(socket.SHUT_WR)
            # kqueue 的读写事件可分开返回；第一项关闭后，第二项不得再索引旧客户端。
            with patch.object(engine.selector, "select", return_value=[
                    (key, selectors.EVENT_READ), (key, selectors.EVENT_WRITE)]):
                pump(engine)
            self.assertFalse(engine.clients)
            # 同一个监听器继续接受并处理新客户端，不把单连接结束升级成会话故障。
            with socket.create_connection(engine.bound_address, timeout=1) as healthy:
                healthy.sendall(frame(bytes.fromhex("0300000001")))
                pump_until(engine, lambda: bool(engine.clients))
                pump(engine)
                pump(engine)
                self.assertEqual(receive(healthy), frame(bytes.fromhex("03020007")))

    def test_transient_accept_errors_are_isolated(self):
        errors = (BlockingIOError(errno.EAGAIN, "not ready"),
                  InterruptedError(errno.EINTR, "interrupted"),
                  ConnectionAbortedError(errno.ECONNABORTED, "aborted"),
                  ConnectionResetError(errno.ECONNRESET, "reset"))
        with connected_engine() as (engine, client, events):
            key = engine.selector.get_key(engine.listener)
            for error in errors:
                with self.subTest(error=type(error).__name__), \
                     patch.object(engine.selector, "select", return_value=[(key, selectors.EVENT_READ)]), \
                     patch.object(socket.socket, "accept", side_effect=error):
                    pump(engine)
            self.assertEqual(exchange(engine, client, frame(bytes.fromhex("0300000001")), events),
                             frame(bytes.fromhex("03020007")))

    def test_fatal_listener_errors_are_not_silently_ignored(self):
        with connected_engine() as (engine, _, _):
            key = engine.selector.get_key(engine.listener)
            with patch.object(engine.selector, "select", return_value=[(key, selectors.EVENT_READ)]), \
                 patch.object(socket.socket, "accept", side_effect=OSError(errno.EBADF, "invalid listener")):
                with self.assertRaises(OSError):
                    pump(engine)

    def test_accepted_socket_is_closed_when_registration_fails(self):
        with connected_engine() as (engine, _, _):
            original = set(engine.clients)
            child = Mock()
            key = engine.selector.get_key(engine.listener)
            with patch.object(engine.selector, "select", return_value=[(key, selectors.EVENT_READ)]), \
                 patch.object(socket.socket, "accept", return_value=(child, ("127.0.0.1", 1))), \
                 patch.object(engine.selector, "register", side_effect=OSError("registration failed")):
                pump(engine)
            child.close.assert_called_once_with()
            self.assertEqual(set(engine.clients), original)

    def test_half_closed_slow_reader_has_a_fixed_drain_deadline(self):
        clock = ManualControl()
        with connected_engine(control=clock) as (engine, client, events):
            server = next(iter(engine.clients))
            client.sendall(frame(bytes.fromhex("0300000001")))
            pump(engine)
            client.shutdown(socket.SHUT_WR)
            key = engine.selector.get_key(server)
            # 只交付 EOF 就绪，不交付写就绪，模拟对端不读导致发送长期无法推进。
            with patch.object(engine.selector, "select", return_value=[(key, selectors.EVENT_READ)]):
                pump(engine)
            self.assertIn(server, engine.clients)
            self.assertEqual(engine.selector.get_key(server).events, selectors.EVENT_WRITE)
            clock.advance(1.01)
            with patch.object(engine.selector, "select", return_value=[]):
                pump(engine)
            self.assertFalse(engine.clients)
            self.assertTrue(any(event.details.get("reason") == "Response drain deadline" for event in events))

    def test_partial_and_blocked_writes_preserve_response_value_boundaries(self):
        with connected_engine() as (engine, client, events):
            server = next(iter(engine.clients))
            client.sendall(frame(bytes.fromhex("0300000001")))
            pump(engine)
            client.shutdown(socket.SHUT_WR)
            send = socket.socket.send
            attempts = iter((3, None))
            def limited(sock, data):
                if sock is not server:
                    return send(sock, data)
                size = next(attempts, len(data))
                if size is None:
                    raise BlockingIOError(errno.EAGAIN, "write would block")
                return send(sock, data[:size])
            with patch.object(socket.socket, "send", limited):
                for _ in range(2):
                    pump(engine)
                    self.assertFalse(any(event.code == "register_values" for event in events))
                pump_until(engine, lambda: not engine.clients)
            self.assertEqual(receive(client), frame(bytes.fromhex("03020007")))
            self.assertEqual(sum(event.code == "register_values" for event in events), 1)


if __name__ == "__main__":
    unittest.main()
