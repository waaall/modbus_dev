"""离屏验证线性变换编辑、双值表格、曲线及实际工作进程边界。"""
import importlib.util
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest

from modbus_dev.application.live import LiveValues
from modbus_dev.domain.config import TaskConfig, load_config
from test_linear_transform import mapping, settings, value_event
from test_tcp_slave import frame, receive


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class LinearTransformGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from gui_support import create_test_application
        cls.app = create_test_application(language="en")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.app.language_manager._install("en")
        self.window = Workbench(remember_settings=False)
        self.window.apply_config(settings())
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.language_manager._install("zh_CN")

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("工作进程等待超时")

    def test_point_editor_defaults_enable_and_independent_roundtrip(self):
        config = TaskConfig.from_mapping(dict(mode="tcp-slave", points=[{}, dict(address=2)]))
        self.window.apply_config(config)
        editor = self.window.point_editors[0]
        switch = editor.fields["linear_transform.enabled"]
        self.assertFalse(switch.isChecked())
        for key in ("raw_start", "raw_end", "mapped_start", "mapped_end"):
            self.assertFalse(editor.fields["linear_transform." + key].isEnabled())
        switch.setChecked(True)
        for key, value in mapping().items():
            if key != "enabled":
                field = editor.fields["linear_transform." + key]
                self.assertTrue(field.isEnabled())
                field.setText(str(value))
        result = self.window.configuration()
        self.assertEqual(result.points[0].linear_transform.raw_end, 20.)
        self.assertFalse(result.points[1].linear_transform.enabled)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "export.json"
            self.assertTrue(self.window.settings.export_configuration(path, result))
            self.window.apply_config(load_config(path))
            self.assertEqual(self.window.configuration(), result)

    def test_disabled_and_unfinished_mapping_drafts_survive_restore_and_languages(self):
        editor = self.window.point_editors[0]
        editor.fields["linear_transform.raw_start"].setText("-4.25")
        editor.fields["linear_transform.enabled"].setChecked(False)
        editor.fields["linear_transform.raw_end"].setText("unfinished")
        draft = self.window.editor.capture_draft()
        self.window.apply_config(TaskConfig(mode="tcp-slave"))
        self.window.editor.restore_draft(json.loads(json.dumps(draft)))
        self.assertEqual(self.window.editor.capture_draft(), draft)
        self.assertEqual(self.window.configuration().points[0].linear_transform.raw_end, "unfinished")
        for language, caption in (("zh_CN", "原始量程终点"), ("en", "Raw range end")):
            self.app.language_manager._install(language)
            self.window.retranslate_ui()
            self.assertEqual(self.window.editor.capture_draft(), draft)
            field = editor.fields["linear_transform.raw_end"]
            self.assertEqual(editor.form.labelForField(field).text(), caption)
        editor.fields["linear_transform.enabled"].setChecked(True)
        self.assertFalse(self.window.editor.configuration_valid)
        editor.fields["linear_transform.raw_end"].setText("20")
        self.assertTrue(self.window.editor.configuration_valid)

    def test_invalid_range_blocks_start_and_busy_locks_all_mapping_fields(self):
        editor = self.window.point_editors[0]
        editor.fields["linear_transform.raw_end"].setText("4")
        self.assertFalse(self.window.editor.configuration_valid)
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIn("raw range end", editor.validation_error(100))
        editor.fields["linear_transform.raw_end"].setText("20")
        self.assertTrue(self.window.editor.configuration_valid)
        before = self.window.configuration()
        self.window.editor.set_busy(True)
        for key, field in editor.fields.items():
            if key.startswith("linear_transform."):
                self.assertFalse(field.isEnabled())
        self.assertFalse(self.window.editor.apply_config(TaskConfig(mode="tcp-slave")))
        self.window.editor.set_busy(False)
        self.assertEqual(self.window.configuration(), before)

    def test_table_shows_raw_and_mapped_values_and_keeps_directional_warnings(self):
        config = settings("rtu-master", data_type="uint16", readback_every=1)
        self.window.apply_config(config)
        panel = self.window.live_panel
        now = panel.clock()
        live = LiveValues(config)
        live.consume(value_event(config, 24, direction="tx", at=now))
        live.consume(value_event(config, 12, direction="rx", at=now + .001))
        panel.ingest(live.snapshot(now + .001, "running", drain=True))
        panel.refresh_values()
        self.assertEqual([panel.table.item(0, column).text() for column in (2, 3, 4, 5)],
                         ["24", "318.75", "12", "127.5"])
        self.assertIn("TX: Outside raw range", panel.table.item(0, 7).text())
        self.assertIn("Received", panel.table.item(0, 7).text())
        self.assertIn("Mapped", panel.selectors["holding:0:rx"].text())
        self.assertTrue(all(not panel.table.isColumnHidden(column) for column in (2, 3, 4, 5)))
        self.assertEqual(list(panel.history.series["holding:0:tx"]), [(now, 318.75)])
        panel.set_state("stopped")
        self.assertIn("TX: Outside raw range", panel.table.item(0, 7).text())
        self.app.language_manager._install("zh_CN")
        panel.retranslate_ui()
        self.assertIn("超出原始量程", panel.table.item(0, 7).text())
        self.assertEqual(panel.table.horizontalHeaderItem(4).text(), "RX 原始值")
        self.assertEqual(panel.table.horizontalHeaderItem(5).text(), "RX 映射值")

    def test_overflow_keeps_raw_while_invalid_mapping_breaks_curve_and_disabled_shows_dash(self):
        config = settings(data_type="uint16", linear_transform=dict(
            enabled=True, raw_start=0, raw_end=1, mapped_start=0, mapped_end=sys.float_info.max))
        self.window.apply_config(config)
        panel = self.window.live_panel
        now = panel.clock()
        live = LiveValues(config)
        live.consume(value_event(config, 2, at=now))
        panel.ingest(live.snapshot(now, "running", drain=True))
        panel.refresh_values()
        self.assertEqual(panel.table.item(0, 4).text(), "2")
        self.assertEqual(panel.table.item(0, 5).text(), "—")
        self.assertIn("Linear transform overflow", panel.table.item(0, 7).text())
        self.assertEqual(list(panel.history.series["input:0:rx"]), [(now, None)])
        config = settings(data_type="uint16", linear_transform={"enabled": False})
        panel.configure(config)
        live = LiveValues(config)
        live.consume(value_event(config, 12, at=now))
        panel.ingest(live.snapshot(now, "running", drain=True))
        panel.refresh_values()
        self.assertEqual(panel.table.item(0, 4).text(), "12")
        self.assertEqual(panel.table.item(0, 5).text(), "—")
        self.assertTrue(panel.table.isColumnHidden(2))
        self.assertFalse(panel.table.isColumnHidden(4))
        self.assertEqual(list(panel.history.series["input:0:rx"]), [(now, 12)])

    def test_finite_extreme_mapped_values_render_with_finite_axis_and_coordinates(self):
        from unittest.mock import patch
        from PySide6.QtGui import QImage, QPainter
        from modbus_dev.ui import realtime_panel
        config = settings(data_type="uint16", linear_transform=dict(
            enabled=True, raw_start=0, raw_end=1, mapped_start=-sys.float_info.max, mapped_end=sys.float_info.max))
        self.window.apply_config(config)
        panel = self.window.live_panel
        now = panel.clock()
        live = LiveValues(config)
        for index in (0, 1):
            live.consume(value_event(config, index, at=now - .1 + .1 * index))
        panel.ingest(live.snapshot(now, "running", drain=True))
        labels, dots = [], []

        class RecordingPainter(QPainter):
            def drawText(self, rect, flags, text):
                labels.append(text)
                return super().drawText(rect, flags, text)

            def drawEllipse(self, rect):
                dots.append(rect.center())
                return super().drawEllipse(rect)

        panel.plot.setFixedSize(600, 220)
        image = QImage(panel.plot.size(), QImage.Format_ARGB32)
        with patch.object(realtime_panel, "QPainter", RecordingPainter):
            panel.plot.render(image)
        self.assertTrue(dots)
        # 十位有效数字的刻度文本可略大于 double 上限，仍应为有限十进制标签。
        self.assertTrue(all(Decimal(label).is_finite() for label in labels[:5]))
        self.assertTrue(all(math.isfinite(dot.x()) and math.isfinite(dot.y()) for dot in dots))

    def test_worker_returns_raw_registers_and_next_session_uses_changed_mapping(self):
        # 仅访问本机临时 TCP 从站，验证 GUI → worker → 数值表的完整链路。
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        first_config = settings("tcp-slave", data_type="uint16", source=dict(value=12))
        data = first_config.to_mapping()
        data.update(port=port, duration_s=0)
        with tempfile.TemporaryDirectory() as root:
            self.window.output.setText(root)
            first_path = first_bytes = None
            for mapped_end, expected in ((255., "127.5"), (100., "50")):
                data["points"][0]["linear_transform"]["mapped_end"] = mapped_end
                self.window.apply_config(TaskConfig.from_mapping(data))
                self.window.start()
                self.wait_for(lambda: self.window.status.text() == "Running")
                self.assertFalse(self.window.point_editors[0].fields["linear_transform.enabled"].isEnabled())
                with socket.create_connection(("127.0.0.1", port), timeout=1) as peer:
                    peer.sendall(frame(bytes.fromhex("0400000001")))
                    self.assertEqual(receive(peer), frame(bytes.fromhex("0402000c")))
                panel = self.window.live_panel
                self.wait_for(lambda: panel.table.rowCount() == 1 and panel.table.item(0, 3) is not None
                              and panel.table.item(0, 3).text() == expected)
                self.assertEqual(panel.table.item(0, 2).text(), "12")
                self.window.stop()
                self.wait_for(lambda: not self.window.active())
                saved = self.window.last_output / "config.json"
                self.assertEqual(load_config(saved).points[0].linear_transform.mapped_end, mapped_end)
                if first_path is None:
                    first_path, first_bytes = saved, saved.read_bytes()
                else:
                    self.assertNotEqual(saved, first_path)
                    self.assertEqual(first_path.read_bytes(), first_bytes)

    def test_axis_padding_near_double_limit_never_displays_nonfinite_ticks(self):
        from unittest.mock import patch
        from PySide6.QtGui import QImage, QPainter
        from modbus_dev.ui import realtime_panel
        panel = self.window.live_panel
        panel.plot.setFixedSize(600, 220)
        labels = []

        class RecordingPainter(QPainter):
            def drawText(self, rect, flags, text):
                labels.append(text)
                return super().drawText(rect, flags, text)

        # 上限除以归一化因子再乘回来可能舍入溢出，不能只覆盖恰好等于上限的值。
        for magnitude in (sys.float_info.max, sys.float_info.max * .98041, sys.float_info.max * .99):
            with self.subTest(magnitude=magnitude):
                panel.history.clear()
                now = panel.clock()
                panel.ingest(dict(state="running", samples=[
                    [1, "input:0:rx", now - .1, -magnitude, "received"],
                    [2, "input:0:rx", now, magnitude, "received"]]))
                labels.clear()
                image = QImage(panel.plot.size(), QImage.Format_ARGB32)
                with patch.object(realtime_panel, "QPainter", RecordingPainter):
                    panel.plot.render(image)
                self.assertTrue(all(Decimal(label).is_finite() for label in labels[:5]), labels)


if __name__ == "__main__":
    unittest.main()
