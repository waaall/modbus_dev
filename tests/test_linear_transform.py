"""验证逐点显示换算、配置兼容、原始证据和通信隔离，不连接真实设备。"""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

from modbus_dev.application.live import LiveValues
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import LinearTransformConfig, TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError, Event
from modbus_dev.domain.linear_transform import transform_value
from modbus_dev.domain.numeric import NUMERIC_TYPES, encode_value
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.trend import TrendHistory
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.reporting.segments import event_paths
from modbus_dev.sources.synthetic import SyntheticSource
from test_rtu_session import FakeControl, Peer


def mapping(**changes):
    return {**dict(enabled=True, raw_start=4., raw_end=20., mapped_start=0., mapped_end=255.), **changes}


def settings(mode="tcp-master", **point):
    return TaskConfig.from_mapping(dict(mode=mode, serial=dict(port="TEST"),
        access_mode="read-write", points=[{"linear_transform": mapping(), **point}]))


def value_event(config, value, *, direction="rx", at=0., words=None, quality=None):
    point = config.points[0]
    area = config.point_area(point)
    return Event("register_values", "实际值", details=dict(area=area, direction=direction,
        address=point.address, registers=encode_value(value, point.data_type, point.encoding_order())
        if words is None else words, function=4 if area == "input" else 3, sampled_s=at,
        quality=quality or ("received" if direction == "rx" else "submitted")))


class LinearTransformTests(unittest.TestCase):
    def test_endpoints_midpoint_and_extrapolation(self):
        config = LinearTransformConfig(**mapping())
        for raw, expected, outside in ((4, 0., False), (12, 127.5, False), (20, 255., False),
                                       (0, -63.75, True), (24, 318.75, True)):
            with self.subTest(raw=raw):
                result = transform_value(raw, config)
                self.assertEqual((result.value, result.status, result.out_of_range), (expected, "ok", outside))

    def test_negative_reverse_constant_and_device_independent_ranges(self):
        for config, raw, expected in (
            (LinearTransformConfig(True, -10., 10., 100., 0.), 5, 25.),
            (LinearTransformConfig(True, 0., 65536., 0., 255.), 32768, 127.5),
            (LinearTransformConfig(True, 4., 20., -3., -3.), 24, -3.),
        ):
            with self.subTest(config=config):
                self.assertEqual(transform_value(raw, config).value, expected)

    def test_invalid_values_and_disabled_drafts_are_not_converted(self):
        for raw in (None, float("nan"), float("inf"), -float("inf"), True, "12"):
            with self.subTest(raw=raw):
                result = transform_value(raw, LinearTransformConfig(**mapping()))
                self.assertIsNone(result.value)
                self.assertEqual(result.status, "invalid")
        self.assertEqual(transform_value(12, LinearTransformConfig(raw_end="unfinished")).status, "disabled")

    def test_large_finite_intermediates_and_real_result_overflow(self):
        wide = LinearTransformConfig(True, -1e308, 1e308, -1e308, 1e308)
        self.assertEqual(transform_value(0., wide).value, 0.)
        self.assertEqual(transform_value(1e308, wide).value, 1e308)
        overflowing = LinearTransformConfig(True, 0., 1., 0., sys.float_info.max)
        result = transform_value(2., overflowing)
        self.assertIsNone(result.value)
        self.assertEqual(result.status, "overflow")
        self.assertTrue(result.out_of_range)

    def test_tiny_input_range_does_not_require_a_fixed_minimum_span(self):
        config = LinearTransformConfig(True, 0., 1e-300, 0., 1.)
        self.assertEqual(transform_value(5e-301, config).value, .5)


class LinearTransformConfigTests(unittest.TestCase):
    def test_old_current_version_configuration_defaults_to_disabled(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[{}]))
        self.assertFalse(config.points[0].linear_transform.enabled)

    def test_save_load_and_all_roles_preserve_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "task.json"
            for mode in ("tcp-master", "rtu-master", "tcp-slave", "rtu-slave"):
                with self.subTest(mode=mode):
                    config = settings(mode)
                    path.write_text(json.dumps(config.to_mapping(), allow_nan=False), encoding="utf-8")
                    self.assertEqual(load_config(path), config)

    def test_endpoints_are_not_constrained_by_register_type(self):
        config = settings(data_type="uint16", linear_transform=dict(
            enabled=True, raw_start=-100., raw_end=65536., mapped_start=1e100, mapped_end=-1e100))
        self.assertEqual(config.points[0].linear_transform.raw_end, 65536.)

    def test_invalid_enabled_endpoints_are_rejected_with_field_paths(self):
        for key in ("raw_start", "raw_end", "mapped_start", "mapped_end"):
            for value in (True, "1", None, float("nan"), float("inf"), 10 ** 1000):
                transform = mapping()
                transform[key] = value
                with self.subTest(key=key, value=type(value)), self.assertRaises(ConfigurationError) as failure:
                    settings(linear_transform=transform)
                self.assertEqual(failure.exception.reason, "transform_finite")
                self.assertTrue(failure.exception.path.endswith("linear_transform." + key))
        for end in (4., -1.):
            with self.assertRaises(ConfigurationError) as failure:
                settings(linear_transform={**mapping(), "raw_end": end})
            self.assertEqual(failure.exception.reason, "transform_bounds")

    def test_wrong_objects_unknown_fields_and_switch_are_rejected(self):
        for transform in (None, [], {"unknown": 1}, {"enabled": "true"}):
            with self.subTest(transform=transform), self.assertRaises(ConfigurationError):
                settings(linear_transform=transform)

    def test_disabled_mapping_and_disabled_point_keep_unfinished_endpoints(self):
        for point in (dict(address=2, linear_transform=dict(enabled=False, raw_end="unfinished")),
                      dict(enabled=False, address=2, linear_transform=dict(enabled=True, raw_end="unfinished"))):
            config = TaskConfig.from_mapping(dict(mode="tcp-slave", points=[{}, point]))
            self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))), config)
            self.assertEqual(config.points[1].linear_transform.raw_end, "unfinished")


class LinearTransformLiveTests(unittest.TestCase):
    def test_all_roles_directions_and_numeric_types_keep_raw_and_map_trends(self):
        for mode in ("tcp-master", "rtu-master", "tcp-slave", "rtu-slave"):
            for data_type in NUMERIC_TYPES:
                config = settings(mode, data_type=data_type)
                for direction in ("tx", "rx"):
                    with self.subTest(mode=mode, data_type=data_type, direction=direction):
                        live = LiveValues(config)
                        event = value_event(config, 12, direction=direction)
                        original = dict(event.details)
                        live.consume(event)
                        snapshot = live.snapshot(0., "running", drain=True)
                        row = snapshot["rows"][0]
                        self.assertEqual(row["value"], 12)
                        self.assertEqual(row["mapped_value"], 127.5)
                        self.assertEqual(row["registers"], list(event.details["registers"]))
                        self.assertEqual(event.details, original)
                        self.assertEqual(snapshot["samples"][0][3], 127.5)
                        history = TrendHistory()
                        history.sample(snapshot, 0.)
                        self.assertEqual(list(history.series[row["key"]]), [(0., 127.5)])

    def test_points_at_same_address_in_different_areas_map_independently(self):
        config = TaskConfig.from_mapping(dict(mode="tcp-slave", points=[
            dict(area="holding", data_type="uint16", linear_transform=mapping()),
            dict(area="input", data_type="uint16", linear_transform=dict(
                enabled=True, raw_start=0, raw_end=24, mapped_start=0, mapped_end=100))]))
        live = LiveValues(config)
        for area, function in (("holding", 3), ("input", 4)):
            live.consume(Event("register_values", "实际值", details=dict(area=area, direction="tx",
                address=0, registers=[12], function=function, sampled_s=0., quality="submitted")))
        rows = live.snapshot(0., "running")["rows"]
        self.assertEqual([(row["area"], row["mapped_value"]) for row in rows], [("holding", 127.5), ("input", 50.)])

    def test_disabled_mapping_preserves_original_curve_and_display_precision_is_not_used(self):
        base = settings(data_type="uint32")
        config = replace(base, points=(replace(base.points[0], linear_transform=LinearTransformConfig()),))
        live = LiveValues(config)
        live.consume(value_event(config, 4294967295))
        snapshot = live.snapshot(0., "running")
        self.assertEqual(snapshot["rows"][0]["mapping_status"], "disabled")
        self.assertIsNone(snapshot["rows"][0]["mapped_value"])
        self.assertEqual(snapshot["samples"][0][3], 4294967295)
        config = settings(data_type="uint16", linear_transform=dict(
            enabled=True, raw_start=0, raw_end=3, mapped_start=0, mapped_end=1))
        live = LiveValues(config)
        live.consume(value_event(config, 1))
        sample = live.snapshot(0., "running")["samples"][0][3]
        self.assertEqual(sample, 1 / 3)
        self.assertNotEqual(sample, float(f"{sample:.7g}"))

    def test_partial_nonfinite_and_overflow_samples_break_curve_without_zero_fill(self):
        config = settings()
        live, history = LiveValues(config), TrendHistory()
        live.consume(value_event(config, 12, at=0.))
        live.consume(value_event(config, 0, words=(0x7f80, 0), at=.1))
        live.consume(value_event(config, 0, words=(0,), at=.2))
        live.consume(value_event(config, 20, at=.3))
        snapshot = live.snapshot(.3, "running", drain=True)
        self.assertEqual([sample[3] for sample in snapshot["samples"]], [127.5, None, None, 255.])
        history.sample(snapshot, .3)
        self.assertEqual([value for _, value in history.series["input:0:rx"]], [127.5, None, None, 255.])
        overflow = settings(data_type="uint16", linear_transform=dict(
            enabled=True, raw_start=0., raw_end=1., mapped_start=0., mapped_end=sys.float_info.max))
        live = LiveValues(overflow)
        live.consume(value_event(overflow, 2))
        row = live.snapshot(0., "running")["rows"][0]
        self.assertEqual((row["value"], row["quality"], row["mapping_status"]), (2, "received", "overflow"))
        self.assertIsNone(row["trend_value"])
        json.dumps(live.snapshot(0., "running"), allow_nan=False)

    def test_rows_only_fallback_and_confirmation_do_not_recompute_or_duplicate_samples(self):
        config = settings("rtu-master")
        live = LiveValues(config)
        live.consume(value_event(config, 12, direction="tx"))
        snapshot = live.snapshot(0., "running", drain=True)
        live.consume(Event("register_confirmation", "ACK", details=dict(area="holding", address=0,
            count=2, direction="tx", sampled_s=0., quality="timeout")))
        confirmation = live.snapshot(.1, "running", drain=True)
        self.assertEqual(confirmation["samples"], [])
        row = confirmation["rows"][0]
        self.assertEqual((row["value"], row["mapped_value"], row["sampled_s"]), (12., 127.5, 0.))
        history = TrendHistory()
        snapshot.pop("samples")
        history.sample(snapshot, 0.)
        self.assertEqual(list(history.series[row["key"]]), [(0., 127.5)])

    def test_sources_request_plans_readback_and_journal_remain_raw_when_mapping_overflows(self):
        # 使用真实 RTU 引擎和独立字节对端，验证换算溢出也不会终止写入及读回。
        config = settings("rtu-master", source=dict(value=12), readback_every=1,
            linear_transform=dict(enabled=True, raw_start=0., raw_end=1., mapped_start=0., mapped_end=sys.float_info.max))
        config = replace(config, duration_s=.2)
        raw_config = replace(config, points=tuple(replace(point, linear_transform=LinearTransformConfig())
                                                  for point in config.points))
        values = SyntheticSource(config).sample(0)
        self.assertEqual(values, SyntheticSource(raw_config).sample(0))
        self.assertEqual(cycle_requests(config, values, slot=0), cycle_requests(raw_config, values, slot=0))
        clock = FakeControl()
        peer = Peer(clock, delay=.001)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config, Path(root) / "run", control=clock,
                engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
            result = runner.run()
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)
            self.assertEqual(result["counters"]["ack"], 2)
            self.assertEqual(result["counters"]["read"], 2)
            self.assertEqual(peer.registers, {0: 0x4140, 1: 0})
            self.assertEqual(load_config(runner.directory / "config.json"), config)
            events = [json.loads(line) for path in event_paths(runner.directory) for line in path.read_text().splitlines()]
            recorded = [event["details"] for event in events if event["code"] == "register_values"]
            self.assertTrue(recorded)
            self.assertTrue(all(row["registers"] == [0x4140, 0] and "mapped_value" not in row for row in recorded))
            rows = runner.live.snapshot(clock.now(), "stopped")["rows"]
            self.assertEqual({row["direction"] for row in rows}, {"tx", "rx"})
            self.assertTrue(all(row["value"] == 12. and row["mapping_status"] == "overflow" for row in rows))


if __name__ == "__main__":
    unittest.main()
