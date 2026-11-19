"""通信点批量传输与绘图频率分离，覆盖丢包边界和确认状态更新。"""
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from modbus_dev.application.live import LiveValues
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event
from modbus_dev.domain.transactions import Reply, Request
from modbus_dev.domain.live_policy import CURVE_MAX_POINTS, LIVE_BATCH_MAX_SAMPLES
from modbus_dev.domain.trend import TrendHistory
from modbus_dev.worker import live_packets


def settings():
    return TaskConfig.from_mapping(dict(schema_version=4, mode="rtu-slave", serial=dict(port="TEST"), points=[dict(area="input")]))


def value_event(at, value, *, direction="tx"):
    return Event("register_values", "value", "DEBUG", details=dict(
        area="input", direction=direction, address=0, function=4,
        registers=struct.unpack(">HH", struct.pack(">f", value)),
        sampled_s=at, quality="submitted" if direction == "tx" else "received"))


class BufferedLiveTests(unittest.TestCase):
    def test_merged_readback_keeps_independent_point_quality_and_trend(self):
        config = TaskConfig.from_mapping(dict(mode="tcp-master", access_mode="read-write", points=[
            dict(operation="write-multiple", address=10, data_type="uint16"),
            dict(operation="write-multiple", address=11, data_type="uint32")]))
        request = Request(3, 10, 3, (7, 0, 8))
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                runner = SessionRunner(config, ".", control=SimpleNamespace(now=lambda: 1.))
                runner.engine, runner.journal = Mock(), Mock()
                # 先给两个点建立有效趋势，再只改变第二个点的低字。
                runner.engine.exchange.side_effect = [
                    Reply("read", request.registers, received_s=0.),
                    Reply("read", (7, 0, 9), received_s=.1, ambiguous=ambiguous)]
                for slot in range(2):
                    runner._transaction(request, 1., slot, slot / 10)
                snapshot = runner.live.snapshot(.1, "running", drain=True)
                self.assertEqual([r["quality"] for r in snapshot["rows"]],
                                 ["ambiguous" if ambiguous else "received", "readback-mismatch"])
                history = TrendHistory()
                history.sample(snapshot, .1)
                self.assertEqual(list(history.series["holding:0:rx"]),
                                 [(0., 7), (.1, None if ambiguous else 7)])
                self.assertEqual(list(history.series["holding:1:rx"]), [(0., 8), (.1, None)])

    def test_ten_hz_points_keep_original_times_and_survive_latest_value_replacement(self):
        live, history = LiveValues(settings()), TrendHistory()
        for batch in range(50):
            for index in range(2):
                number = batch * 2 + index
                live.consume(value_event(number / 10, number))
            snapshot = live.snapshot((batch * 2 + 1) / 10, "running", drain=True)
            history.sample(snapshot, snapshot["sampled_s"])
        points = list(history.series["input:0:tx"])
        self.assertEqual(points, [(i / 10, float(i)) for i in range(100)])
        self.assertEqual(snapshot["rows"][0]["value"], 99.)
        history.sample(live.snapshot(9.95, "running", drain=True), 9.95)
        self.assertEqual(list(history.series["input:0:tx"]), points)
        self.assertEqual(history.dropped, 0)

    def test_worker_takes_points_at_send_time_and_flushes_final_batches(self):
        live = LiveValues(settings())
        total = 2 * LIVE_BATCH_MAX_SAMPLES + 7
        for i in range(total):
            live.consume(value_event(i / 1000, i))
        runner = SimpleNamespace(live=live, state="running", counters={},
                                 control=SimpleNamespace(now=lambda: 3.))
        # 多个旧状态可以被替换；构造状态不能提前取走通信点。
        statuses = [dict(schema_version=1, type="status", state="running", events=[]) for _ in range(4)]
        first = list(live_packets(statuses[-1], runner))
        runner.state = "stopped"
        final = list(live_packets(dict(schema_version=1, type="result", result=dict(state="stopped")), runner))
        packets = first + final
        self.assertEqual([p["type"] for p in packets], ["status", "status", "result"])
        self.assertEqual([s[0] for p in packets for s in p["live"]["samples"]], list(range(1, total + 1)))
        self.assertEqual(packets[-1]["live"]["samples_pending"], 0)
        for packet in packets:
            self.assertLess(len(json.dumps(packet, ensure_ascii=True).encode("ascii")), 262144)

    def test_overflow_is_counted_and_a_line_cannot_bridge_the_missing_points(self):
        live, history = LiveValues(settings(), capacity=3), TrendHistory()
        live.consume(value_event(0., 0.))
        history.sample(live.snapshot(0., "running", drain=True), 0.)
        for i in range(1, 6):
            live.consume(value_event(i / 10, i))
        snapshot = live.snapshot(.5, "running", drain=True)
        self.assertEqual(snapshot["samples_dropped"], 2)
        self.assertEqual([s[3] for s in snapshot["samples"]], [3., 4., 5.])
        history.sample(snapshot, .5)
        self.assertEqual(list(history.series["input:0:tx"]),
                         [(0., 0.), (.3, None), (.3, 3.), (.4, 4.), (.5, 5.)])
        self.assertEqual(history.dropped, 2)

    def test_confirmation_changes_existing_point_without_inventing_new_samples(self):
        live, history = LiveValues(settings()), TrendHistory()
        live.consume(value_event(0., 1.25))
        history.sample(live.snapshot(0., "running", drain=True), 0.)
        live.consume(Event("register_confirmation", "ack", "DEBUG", details=dict(
            area="input", direction="tx", address=0, count=2, quality="ambiguous")))
        snapshot = live.snapshot(.1, "running", drain=True)
        self.assertEqual(snapshot["rows"][0]["quality"], "submitted")
        self.assertEqual(snapshot["rows"][0]["confirmation"], "ambiguous")
        self.assertEqual(snapshot["samples"], [])
        history.sample(snapshot, .1)
        self.assertEqual(list(history.series["input:0:tx"]), [(0., 1.25)])
        live.consume(value_event(.2, 2.5))
        snapshot = live.snapshot(.2, "running", drain=True)
        self.assertIsNone(snapshot["rows"][0]["confirmation"])
        history.sample(snapshot, .2)
        self.assertEqual(list(history.series["input:0:tx"]), [(0., 1.25), (.2, 2.5)])

    def test_confirmation_storm_does_not_fill_buffer_or_refresh_value_age(self):
        live = LiveValues(settings(), capacity=2)
        live.consume(value_event(0., 1.25))
        for index in range(10000):
            live.consume(Event("register_confirmation", "ACK", "DEBUG", details=dict(
                area="input", direction="tx", address=0, count=2, sampled_s=0.,
                quality="ambiguous" if index % 2 else "timeout")))
        snapshot = live.snapshot(.5, "running", drain=True)
        self.assertEqual(len(snapshot["samples"]), 1)
        self.assertEqual(snapshot["samples_dropped"], 0)
        row = snapshot["rows"][0]
        self.assertEqual(row["sample_id"], 1)
        self.assertEqual(row["sampled_s"], 0.)
        self.assertEqual(row["quality"], "submitted")
        self.assertEqual(row["confirmation"], "ambiguous")
        history = TrendHistory()
        history.sample(snapshot, .5)
        history.advance(1.5, "running")
        self.assertIsNone(history.series["input:0:tx"][-1][1])

    def test_old_confirmation_cannot_be_attached_to_a_newer_value(self):
        live = LiveValues(settings())
        live.consume(value_event(0., 1.25))
        live.consume(value_event(.1, 2.5))
        live.consume(Event("register_confirmation", "old ACK", "DEBUG", details=dict(
            area="input", direction="tx", address=0, count=2, sampled_s=0., quality="confirmed")))
        row = live.snapshot(.1, "running")["rows"][0]
        self.assertIsNone(row["confirmation"])
        self.assertEqual(row["sampled_s"], .1)
        self.assertEqual(row["value"], 2.5)

    def test_worker_publishes_session_uncertainty_after_stop(self):
        runner = SimpleNamespace(live=LiveValues(settings()), state="stopped", counters={},
                                 engine=SimpleNamespace(uncertain=True),
                                 control=SimpleNamespace(now=lambda: 1.))
        packet = next(live_packets(dict(type="result", result={}), runner))
        self.assertTrue(packet["live"]["ack_correlation_uncertain"])

    def test_time_window_capacity_and_stopped_final_batch(self):
        live = LiveValues(settings())
        live.consume(value_event(0., 1.))
        live.consume(value_event(12., 2.))
        snapshot = live.snapshot(12., "stopped", drain=True)
        self.assertEqual(snapshot["samples_dropped"], 1)
        history = TrendHistory()
        history.sample(snapshot, 12.)
        self.assertEqual(list(history.series["input:0:tx"]), [(12., 2.), (12., None)])
        samples = [[i + 1, "input:0:tx", i / 10000, 1., "submitted"]
                   for i in range(CURVE_MAX_POINTS + 5)]
        history.clear()
        history.sample(dict(state="running", samples=samples), 1.1)
        self.assertEqual(len(history.series["input:0:tx"]), CURVE_MAX_POINTS)
        self.assertEqual(history.dropped, 5)
        history.advance(13., "running")
        self.assertFalse(history.series["input:0:tx"])

    def test_planned_source_values_never_enter_the_communication_buffer(self):
        live = LiveValues(settings())
        live.consume(Event("source_sample", "plan", "DEBUG", details=dict(values=[1.25])))
        snapshot = live.snapshot(0., "running", drain=True)
        self.assertEqual(snapshot["rows"], [])
        self.assertEqual(snapshot["samples"], [])
