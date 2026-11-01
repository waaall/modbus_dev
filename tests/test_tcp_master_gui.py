"""验证 TCP 主站的界面角色、权限、草稿和真实工作进程，不改变全局用户设置。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest

from modbus_dev.domain.config import TaskConfig
from test_tcp_master import configuration, slave_process

HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "Requires the optional GUI dependency")
class TcpMasterGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="en")

    def setUp(self):
        from modbus_dev.gui import Workbench
        from modbus_dev.ui.preferences import PreferenceStore
        self.directory = tempfile.TemporaryDirectory()
        self.previous_store = self.app.preference_store
        self.app.preference_store = PreferenceStore(Path(self.directory.name) / "preferences.json")
        self.app.language_manager._install("en")
        self.window = Workbench(remember_settings=False)
        self.window.output.setText(self.directory.name)
        self.window.show()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("TCP master GUI worker timed out")

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.preference_store = self.previous_store
        self.app.language_manager._install("zh_CN")
        self.directory.cleanup()

    def test_selecting_tcp_master_is_readonly_and_uses_target_not_serial_fields(self):
        editor = self.window.editor
        editor.apply_config(TaskConfig(mode="tcp-slave", access_mode="read-write"))
        self.window.fields["mode"].setCurrentIndex(self.window.fields["mode"].findData("tcp-master"))
        self.assertEqual(editor.configuration().access_mode, "read-only")
        self.assertEqual(editor.configuration().points[0].operation, "read-input")
        self.assertTrue(editor.fields["host"].isEnabled())
        self.assertTrue(editor.fields["connect_timeout_ms"].isEnabled())
        self.assertFalse(editor.fields["serial.port"].isEnabled())
        self.assertEqual(editor.tcp_box.title(), "TCP target")
        self.assertEqual(editor.fields["host"].accessibleName(), "Target IP address")
        self.assertIn("Unit 1", self.window.connection_summary.text())
        self.assertNotIn("baud", self.window.connection_summary.text())
        self.assertNotIn("Line ", editor.budget_note.text())

    def test_imported_explicit_write_permission_and_inactive_serial_draft_roundtrip(self):
        config = configuration(access_mode="read-write", connect_timeout_ms=321, points=[
            dict(data_type="uint16", operation="write-single", readback_every=2)],
            serial=dict(port="", baud="unfinished"))
        editor = self.window.editor
        editor.apply_config(config)
        self.assertEqual(editor.configuration(), config)
        draft = editor.capture_draft()
        editor.apply_config(TaskConfig(mode="tcp-slave"))
        editor.restore_draft(draft)
        self.assertEqual(editor.configuration(), config)
        self.assertEqual(editor.fields["serial.baud"].currentText(), "unfinished")

    def test_readonly_hides_write_options_and_budget_errors_disable_start(self):
        editor = self.window.editor
        editor.apply_config(configuration())
        operation = editor.point_editors[0].fields["operation"]
        for key in ("write-single", "write-multiple"):
            self.assertTrue(operation.view().isRowHidden(operation.findData(key)))
        editor.fields["timeout_ms"].setText(editor.fields["period_ms"].text())
        self.assertFalse(editor.configuration_valid)
        self.assertFalse(self.window.start_button.isEnabled())

    def test_translations_and_master_counters_preserve_configuration(self):
        from PySide6.QtCore import QCoreApplication
        editor = self.window.editor
        editor.apply_config(configuration())
        before = editor.capture_draft()
        self.window.counter_mode = "tcp-master"
        self.window.show_counters(dict(write_attempts=8, ack=3, read=5))
        self.assertEqual([label.text() for label in self.window.metric_values], ["8", "3", "5", "0"])
        self.assertTrue(self.window.set_language("zh_CN"))
        self.assertEqual(editor.fields["mode"].currentText(), "TCP 主站")
        self.assertEqual(editor.tcp_box.title(), "TCP 目标")
        self.assertEqual(editor.fields["connect_timeout_ms"].accessibleName(), "连接超时（ms）")
        self.assertEqual(QCoreApplication.translate("ModbusDev", "Write outcome unknown"), "写入结果未知")
        self.assertEqual(editor.capture_draft(), before)

    def test_worker_reads_existing_slave_and_stops_cleanly(self):
        root = Path(self.directory.name)
        with slave_process(root, [dict(area="input", source=dict(value=12.5))]) as port:
            self.window.apply_config(configuration(port=port, duration_s=0))
            self.window.start()
            # 状态包先到，数值表最多 5Hz 刷新；等待两条链路都完成，不依赖定时器先后顺序。
            self.wait_for(lambda: self.window.monitor.last_counters.get("read", 0) >= 2
                          and bool(self.window.live_panel.snapshot.get("rows")))
            self.assertFalse(self.window.editor.isEnabled())
            self.assertTrue(self.window.live_panel.snapshot["rows"])
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
            self.assertEqual(self.window.result["exit_code"], 130)
            self.assertTrue((self.window.last_output / "summary.json").is_file())
            self.assertTrue(self.window.editor.isEnabled())
            self.assertFalse(self.window.result["ack_correlation_uncertain"])

    def test_unknown_write_remains_visible_after_stop(self):
        self.window.apply_config(configuration(access_mode="read-write", points=[
            dict(operation="write-single", data_type="uint16")]), persist=False)
        now = time.monotonic()
        row = dict(key="holding:0:tx", point=0, name="", area="holding", address=0, direction="tx",
                   value=3, registers=[3], function=6, quality="submitted", confirmation="outcome-unknown",
                   sampled_s=now, sample_id=1)
        panel = self.window.live_panel
        panel.ingest(dict(state="stopped", rows=[row], stale_after_s=1))
        panel.refresh_values()
        self.assertIn("Write outcome unknown", panel.table.item(0, 5).text())

    def test_target_and_connection_timeout_remain_reachable_on_compact_screen(self):
        editor = self.window.editor
        editor.apply_config(configuration())
        editor.config_tabs.setCurrentIndex(0)
        scroll = editor.config_tabs.widget(0)
        for key in ("host", "port", "connect_timeout_ms", "timeout_ms", "unit"):
            field = editor.fields[key]
            scroll.ensureWidgetVisible(field)
            self.app.processEvents()
            center = field.mapTo(scroll.viewport(), field.rect().center())
            self.assertTrue(field.isVisible(), key)
            self.assertTrue(scroll.viewport().rect().contains(center), key)


if __name__ == "__main__":
    unittest.main()
