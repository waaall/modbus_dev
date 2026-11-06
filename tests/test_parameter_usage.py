"""按角色和操作验证参数适用性，覆盖无关草稿、实际请求和界面切换。"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from modbus_dev.application.session import check_capabilities
from config_factory import task_mapping
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.wire import minimum_cycle_ms
from modbus_dev.sources.synthetic import SyntheticSource
import test_rtu_session


def master(operation="read-input", **options):
    if operation == 'write-single':
        options.setdefault('points', [dict(data_type='uint16')])
    return TaskConfig.from_mapping(task_mapping(mode="rtu-master", operation=operation,
                                       serial=dict(port="TEST", baud=115200), **options))


class ParameterTests(unittest.TestCase):
    def test_reads_ignore_unused_point_source_values(self):
        source = dict(kind="dyn", value="unused", amplitude=-1, frequency_hz="unused",
                      path="", channels="unused")
        for operation in ("read-input", "read-holding"):
            for data in (dict(source=source), dict(points=[dict(source=source)])):
                with self.subTest(operation=operation, data=data):
                    config = master(operation, **data)
                    check_capabilities(config)
                    self.assertEqual(config.points[0].source.value, "unused")
                    self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))), config)

    def test_read_plan_and_budget_do_not_encode_generated_values(self):
        config = master(points=[dict(address=6), dict(address=0), dict(address=2),
                                dict(enabled=False, address=4)])
        with patch("modbus_dev.domain.operations.write_requests", side_effect=AssertionError("source encoding")):
            requests = cycle_requests(config, ())
            self.assertGreater(minimum_cycle_ms(config), 0)
        self.assertEqual([(r.function, r.address, r.count, r.registers) for r in requests],
                         [(4, 0, 4, ()), (4, 6, 2, ())])

    def test_read_session_never_constructs_or_samples_a_source(self):
        helper = test_rtu_session.SessionTests()
        for operation in ("read-input", "read-holding"):
            with self.subTest(operation=operation), patch("modbus_dev.application.session.SyntheticSource",
                    side_effect=AssertionError("source construction")):
                result, peer, events = helper.exercise(duration=.3, delay=.005,
                    operation=operation, source=dict(kind="dyn", value="unused", channels="unused"),
                    readback_every="unused")
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(result["counters"]["read"], 3)
                self.assertNotIn("source_sample", {event["code"] for event in events})
                self.assertTrue(all(packet[1] == (4 if operation == "read-input" else 3)
                                    for _, packet in peer.writes))

    def test_inactive_connection_and_master_fields_do_not_block_validation(self):
        check_capabilities(master(host="", port="unused", readback_every="unused"))
        config = TaskConfig.from_mapping(task_mapping(mode="tcp-slave", serial=dict(port="", baud="unused"),
            operation="unused", access_mode="read-only", readback_every="unused",
            schedule_tolerance_percent="unused"))
        check_capabilities(config)
        config = TaskConfig.from_mapping(task_mapping(mode="rtu-slave", serial=dict(port="TEST"), host="", port="unused",
            operation="unused", readback_every="unused",
            schedule_tolerance_percent="unused"))
        check_capabilities(config)
        check_capabilities(master("write-multiple", readback_every=0))

    def test_constant_and_disabled_sources_skip_unused_parameters(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode="rtu-slave", serial=dict(port="TEST"), points=[
            dict(source=dict(kind="constant", value=1.25, amplitude="unused", frequency_hz=-10,
                             channels="unused")),
            dict(enabled=False, address="unused", source=dict(kind="unused", value="unused"))]))
        check_capabilities(config)
        self.assertEqual(SyntheticSource(config).sample(1), (1.25,))

    def test_active_source_connection_and_readback_errors_still_fail(self):
        cases = [dict(operation="write-multiple", source=dict(value="invalid")),
                 dict(operation="write-multiple", readback_every=1, read_function=4),
                 dict(operation="write-multiple", readback_every="invalid"),
                 dict(operation="read-input", unit=0),
                 dict(operation="read-input", points=[dict(address=-1)])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(task_mapping(mode="rtu-master", serial=dict(port="TEST", baud=115200), **data))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(task_mapping(mode="rtu-slave", serial=dict(port="TEST"), access_mode="read-only",
                source=dict(kind="sine", frequency_hz=100)))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(task_mapping(mode="tcp-slave", port="invalid"))


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class ParameterGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="zh_CN")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.window = Workbench(remember_settings=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            until = time.monotonic() + 5
            while self.window.active() and time.monotonic() < until:
                self.app.processEvents()
                time.sleep(.01)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_read_source_is_disabled_but_slave_readonly_source_remains_active(self):
        self.window.apply_config(master())
        point = self.window.point_editors[0]
        self.assertTrue(point.fields["address"].isEnabled())
        for key in ("source.kind", "source.value", "source.amplitude", "source.frequency_hz"):
            self.assertFalse(point.fields[key].isEnabled(), key)
        mode = self.window.fields["mode"]
        mode.setCurrentIndex(mode.findData("rtu-slave"))
        self.window.fields["access_mode"].setCurrentIndex(self.window.fields["access_mode"].findData("read-only"))
        self.assertTrue(point.fields["source.kind"].isEnabled())
        self.assertTrue(point.fields["source.value"].isEnabled())

    def test_read_mode_hides_all_source_rows_and_retains_triangle_draft(self):
        self.window.apply_config(master("write-multiple", points=[dict(data_type="int16",
            source=dict(kind="triangle", lower=-2, upper=3))]))
        point = self.window.point_editors[0]
        operation = self.window.point_editors[0].fields["operation"]
        operation.setCurrentIndex(operation.findData("read-input"))
        for key in ("source.kind", "source.value", "source.amplitude", "source.frequency_hz", "source.lower", "source.upper"):
            self.assertTrue(point.fields[key].isHidden(), key)
        self.assertIn("对端从站", point.source_note.text())
        self.assertTrue(self.window.point_editors[0].fields["readback_every"].isHidden())
        operation.setCurrentIndex(operation.findData("write-multiple"))
        self.assertFalse(point.fields["source.lower"].isHidden())
        self.assertEqual(self.window.configuration().points[0].source.lower, -2)

    def test_unused_unsupported_source_names_survive_draft_restore(self):
        for kind in ("dyn", "future-source"):
            with self.subTest(kind=kind):
                self.window.apply_config(master(points=[dict(source=dict(kind=kind, value="unused"))]))
                draft = self.window.editor.capture_draft()
                self.window.editor.restore_draft(json.loads(json.dumps(draft)))
                self.assertEqual(self.window.editor.capture_draft(), draft)
                self.assertEqual(self.window.configuration().points[0].source.kind, kind)

    def test_old_empty_source_draft_keeps_the_inactive_base_source(self):
        self.window.apply_config(master(points=[dict(source=dict(kind="dyn", value="unused"))]))
        draft = self.window.editor.capture_draft()
        draft["values"]["points.0.source.kind"] = None
        self.window.editor.restore_draft(draft)
        self.assertEqual(self.window.configuration().points[0].source.kind, "dyn")

    def test_readonly_operations_filter_writes_without_losing_original_selection(self):
        self.window.apply_config(master("write-multiple"))
        access, operation = (self.window.fields["access_mode"], self.window.point_editors[0].fields["operation"])
        access.setCurrentIndex(access.findData("read-only"))
        self.assertEqual([operation.itemData(i) for i in range(operation.count()) if not operation.view().isRowHidden(i)],
                         ["read-holding", "read-input"])
        self.assertEqual(operation.currentIndex(), -1)
        self.assertFalse(self.window.point_editors[0].selection_note.isHidden())
        with self.assertRaises(ValueError):
            self.window.configuration()
        draft = self.window.editor.capture_draft()
        self.window.editor.restore_draft(draft)
        self.assertEqual(self.window.editor.capture_draft(), draft)
        access.setCurrentIndex(access.findData("read-write"))
        self.assertEqual(operation.currentData(), "write-multiple")
        self.window.configuration()

    def test_fixed_readback_is_text_and_unsupported_rts_is_hidden(self):
        self.window.apply_config(master("write-multiple", readback_every=1))
        self.assertNotIn("read_function", self.window.fields)
        self.assertFalse(self.window.editor.readback_note.isHidden())
        self.assertIn("FC03", self.window.editor.readback_note.text())
        self.assertEqual(cycle_requests(self.window.configuration(), slot=0)[-1].function, 3)
        if not self.window.editor.parameter_usage().rts_supported:
            self.assertTrue(self.window.editor.rts.isHidden())

    def test_tcp_curve_has_no_unusable_selectors(self):
        self.window.apply_config(TaskConfig(mode="tcp-slave"))
        self.assertEqual(set(self.window.live_panel.selectors), {'input:0:tx'})
        self.assertFalse(self.window.live_panel.plot.isHidden())
        self.assertIn("实际通信点", self.window.live_panel.curve_note.text())

    def test_slave_register_hint_tracks_permissions_and_does_not_leak_into_tcp(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(
            mode="rtu-slave", serial=dict(port="TEST"), access_mode="read-write")))
        note = self.window.editor.data_note
        self.assertFalse(note.isHidden())
        self.assertTrue(note.wordWrap())
        for text in ("每个测点只配置一个寄存器区", "保持区：保存数值", "FC06/16", "接收并保存主站写入",
                     "本机按数据源生成并更新数值", "FC04", "主站不能写入", "两区相互独立"):
            self.assertIn(text, note.text())
        self.assertNotIn("拒绝主站写入", note.text())
        # 权限和模式切换复用现有说明，不留下上一种模式的提示。
        access = self.window.fields["access_mode"]
        access.setCurrentIndex(access.findData("read-only"))
        self.assertIn("当前为只读，拒绝主站写入", note.text())
        access.setCurrentIndex(access.findData("read-write"))
        self.assertNotIn("拒绝主站写入", note.text())
        self.window.apply_config(TaskConfig(mode="tcp-slave"))
        self.assertIn("每个测点只配置一个寄存器区", note.text())
        self.assertIn("FC06/16", note.text())

    def test_write_hint_explains_why_fc04_cannot_verify_a_write(self):
        for operation in ("write-single", "write-multiple"):
            for readback in (0, 1):
                with self.subTest(operation=operation, readback=readback):
                    self.window.apply_config(master(operation, readback_every=readback))
                    note = self.window.editor.readback_note
                    self.assertFalse(note.isHidden())
                    self.assertTrue(note.wordWrap())
                    for text in ("每点独立选择操作", "FC03", "FC04", "独立输入区"):
                        self.assertIn(text, note.text())
        for operation in ("read-holding", "read-input"):
            self.window.apply_config(master(operation))
            self.assertFalse(self.window.editor.readback_note.isHidden())

    def test_invalid_source_draft_is_ignored_for_read_and_restored_for_write(self):
        self.window.apply_config(master("write-multiple"))
        point = self.window.point_editors[0]
        point.fields["source.value"].setText("unfinished")
        self.window.fields["host"].setText("")
        self.window.fields["port"].setText("unused")
        self.window.point_editors[0].fields["readback_every"].setValue(0)
        operation = self.window.point_editors[0].fields["operation"]
        operation.setCurrentIndex(operation.findData("read-input"))
        config = self.window.configuration()
        self.assertEqual(config.point_usage(config.points[0]).operation, "read-input")
        self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))).points[0].source.value,
                         "unfinished")
        self.assertEqual(point.fields["source.value"].text(), "unfinished")
        self.window.point_editors[0].fields["readback_every"].setValue(0)
        operation.setCurrentIndex(operation.findData("write-multiple"))
        self.assertTrue(point.fields["source.value"].isEnabled())
        with self.assertRaisesRegex(ValueError, "数值"):
            self.window.configuration()

    def test_tcp_ignores_serial_drafts_but_checks_its_own_port(self):
        self.window.apply_config(TaskConfig.from_mapping(task_mapping(mode="tcp-slave",
            serial=dict(baud="unfinished", rts_toggle="unused"))))
        self.window.fields["schedule_tolerance_percent"].setText("unfinished")
        self.window.configuration()
        self.window.fields["port"].setText("unfinished")
        with self.assertRaises(ValueError):
            self.window.configuration()

    def test_inactive_serial_draft_reaches_actual_tcp_worker(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            self.window.output.setText(root)
            self.window.fields["port"].setText(str(port))
            self.window.fields["serial.baud"].setEditText("unfinished")
            self.window.fields["schedule_tolerance_percent"].setText("unfinished")
            self.window.fields["duration_s"].setText(".25")
            with patch.object(self.window, "error") as error:
                self.window.start()
                error.assert_not_called()
            until = time.monotonic() + 6
            while self.window.temporary is not None and time.monotonic() < until:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNone(self.window.temporary)
            self.assertEqual(self.window.result["exit_code"], 0, self.window.result)
            saved = json.loads((self.window.last_output / "config.json").read_text(encoding="utf-8"))
            summary = json.loads((self.window.last_output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["serial"]["baud"], "unfinished")
            self.assertTrue(summary["evidence_complete"])

    def test_constant_ignores_sine_drafts_until_sine_is_selected(self):
        point = self.window.point_editors[0]
        point.fields["source.amplitude"].setText("unfinished")
        point.fields["source.frequency_hz"].setText("unfinished")
        self.window.configuration()
        point.fields["source.kind"].setCurrentIndex(point.fields["source.kind"].findData("sine"))
        with self.assertRaisesRegex(ValueError, "振幅"):
            self.window.configuration()

    def test_curve_choices_follow_actual_numeric_directions(self):
        cases = [(master(), {"input:0:rx"}),
                 (master("write-multiple"), {"holding:0:tx"}),
                 (master("write-multiple", readback_every=1), {"holding:0:tx", "holding:0:rx"}),
                 (TaskConfig.from_mapping(task_mapping(mode="rtu-slave", serial=dict(port="TEST"))),
                  {"input:0:tx"}),
                 (TaskConfig.from_mapping(task_mapping(mode="rtu-slave", serial=dict(port="TEST"), access_mode="read-write")),
                  {"input:0:tx"})]
        for config, expected in cases:
            with self.subTest(config=config):
                self.window.apply_config(config)
                actual = {key for key, checkbox in self.window.live_panel.selectors.items() if checkbox.isEnabled()}
                self.assertEqual(actual, expected)
                self.assertEqual(set(self.window.live_panel.selectors), expected)
