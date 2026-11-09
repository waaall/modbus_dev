"""验证配置生命周期，以及实际 Qt/worker 边界上的语言切换。"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from config_factory import task_mapping
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.ui.preferences import PreferenceStore

HAS_QT = importlib.util.find_spec("PySide6") is not None


class PreferenceTests(unittest.TestCase):
    def test_atomic_language_preferences_are_separate_from_task_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferenceStore(path)
            self.assertEqual(store.load().get("language", "system"), "system")
            store.save({"language": "en"})
            self.assertEqual(PreferenceStore(path).load().get("language", "system"), "en")
            self.assertEqual(json.loads(path.read_text())["language"], "en")
            with patch("modbus_dev.ui.preferences.os.replace", side_effect=OSError("read-only preferences")):
                with self.assertRaises(OSError):
                    store.save({"language": "zh_CN"})
            self.assertEqual(store.load().get("language", "system"), "en")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertNotIn("language", TaskConfig().to_mapping())

    def test_malformed_and_unknown_preferences_use_system_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            for content in ("broken", "[]", '{"schema_version":99}'):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(PreferenceStore(path).load().get("language", "system"), "system")


@unittest.skipUnless(HAS_QT, "Requires the optional GUI dependency")
class TranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="zh_CN")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.root = tempfile.TemporaryDirectory()
        self.manager = self.app.language_manager
        self.previous_store = self.app.preference_store
        self.app.preference_store = PreferenceStore(Path(self.root.name) / "preferences.json")
        self.manager._install("zh_CN")
        self.window = Workbench()
        self.window.output.setText(self.root.name)
        self.window.show()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Qt/worker lifecycle timed out")

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.preference_store = self.previous_store
        self.manager._install("zh_CN")
        self.root.cleanup()

    def test_system_language_follows_first_supported_preference(self):
        from modbus_dev.ui.i18n import resolve_locale
        self.assertEqual(resolve_locale("system", ["en-US", "zh-CN"]), "en")
        self.assertEqual(resolve_locale("system", ["fr-FR", "zh-Hans-CN", "en"]), "zh_CN")
        self.assertEqual(resolve_locale("system", ["zh_CN.UTF-8"]), "zh_CN")
        self.assertEqual(resolve_locale("system", ["fr-FR"]), "en")

    def test_compiled_catalog_matches_source_and_english_fallback(self):
        from PySide6.QtCore import QCoreApplication
        catalog = Path(__file__).resolve().parents[1] / "src/modbus_dev/ui/translations/modbus_dev_zh_CN.ts"
        messages = [(message.findtext("source"), message.findtext("translation"))
                    for message in ET.parse(catalog).getroot().findall("context/message")]
        for source, translation in messages:
            self.assertEqual(QCoreApplication.translate("ModbusDev", source), translation, source)
        self.assertTrue(self.window.set_language("en"))
        for source, _ in messages:
            self.assertEqual(QCoreApplication.translate("ModbusDev", source), source)

    def test_switch_updates_text_without_rebuilding_or_validating_drafts(self):
        from PySide6.QtCore import QSignalBlocker
        from PySide6.QtTest import QSignalSpy
        w = self.window
        w.apply_config(TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="COM77", baud=31250))))
        w.fields["period_ms"].setText("unfinished")
        w.point_editors[0].fields["source.value"].setText("1.")
        w.point_editors[0].fields["name"].setText("温度")
        w.config_tabs.setCurrentIndex(2)
        w.monitor_tabs.setCurrentIndex(1)
        w.counter_mode = "rtu-master"
        w.show_counters(dict(write_attempts=9, ack=2, ack_ambiguous=3))
        w.logs.appendPlainText("Historical log 原始内容")
        checkbox = next(iter(w.live_panel.selectors.values()))
        checkbox.setChecked(False)
        history = w.live_panel.history
        before = (w.fields["serial.port"], w.fields["serial.baud"], w.point_editors[0])
        spy = QSignalSpy(w.editor.changed)
        self.assertTrue(w.set_language("en"))
        self.assertEqual(w.start_button.text(), "Start session")
        self.assertEqual(w.config_tabs.tabText(0), "Connection and timing")
        self.assertEqual(w.point_editors[0].accessibleName(), "Point 1")
        self.assertEqual(w.point_tabs.tab_bar.tabText(0), "1")
        self.assertEqual(w.fields["mode"].currentData(), "rtu-slave")
        self.assertEqual(w.fields["mode"].currentText(), "RTU slave")
        self.assertEqual(w.fields["serial.port"].currentText(), "COM77")
        self.assertEqual(w.fields["serial.baud"].currentText(), "31250")
        self.assertEqual(w.fields["period_ms"].text(), "unfinished")
        self.assertEqual(w.point_editors[0].fields["source.value"].text(), "1.")
        self.assertEqual(w.point_editors[0].fields["name"].text(), "温度")
        self.assertEqual((w.config_tabs.currentIndex(), w.monitor_tabs.currentIndex()), (2, 1))
        self.assertEqual([label.text() for label in w.metric_values], ["9", "5", "0", "0"])
        self.assertEqual(w.metric_labels[1].text(), "Valid ACKs")
        self.assertEqual(w.logs.toPlainText(), "Historical log 原始内容")
        self.assertIs(w.live_panel.history, history)
        self.assertFalse(checkbox.isChecked())
        self.assertEqual(before, (w.fields["serial.port"], w.fields["serial.baud"], w.point_editors[0]))
        self.assertEqual(spy.count(), 0)
        self.assertEqual(self.app.preference_store.load().get("language", "system"), "en")
        self.assertTrue(w.set_language("zh_CN"))
        self.assertEqual(w.start_button.text(), "启动会话")
        self.assertEqual(w.metric_labels[1].text(), "有效 ACK")

    def test_failed_preference_write_keeps_current_language_and_selection(self):
        with patch.object(self.app.preference_store, "save", side_effect=OSError("disk unavailable")), \
                patch.object(self.window, "error") as error:
            self.assertFalse(self.window.set_language("en"))
        error.assert_called_once()
        self.assertEqual(self.manager.mode, "zh_CN")
        self.assertEqual(self.window.editor.language.currentData(), "zh_CN")
        self.assertEqual(self.window.start_button.text(), "启动会话")

    def test_translated_validation_preserves_headless_error_diagnostics(self):
        from modbus_dev.ui.errors import format_error
        try:
            TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST", baud=1199)))
        except ConfigurationError as error:
            diagnostic = str(error)
            self.assertIn("波特率", str(format_error(error)))
            self.window.set_language("en")
            self.assertIn("Baud rate", str(format_error(error)))
            self.assertIn("1200", str(format_error(error)))
            self.assertEqual(str(error), diagnostic)
        else:
            self.fail("Invalid baud rate accepted")

    def test_all_configuration_entries_stay_locked_until_worker_cleanup(self):
        from PySide6.QtWidgets import QFileDialog
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        w = self.window
        w.apply_config(TaskConfig.from_mapping(dict(mode="tcp-slave", port=port, duration_s=0)))
        w.start()
        self.assertTrue(w.active())
        self.assertFalse(w.editor.isEnabled())
        self.assertFalse(w.output_box.isEnabled())
        self.assertFalse(w.editor.language.isEnabled())
        self.assertFalse(w.set_language("en"))
        self.assertFalse(w.settings.change_language("en"))
        self.assertFalse(w.apply_config(TaskConfig(mode="tcp-slave", port=port + 1)))
        with patch.object(QFileDialog, "getOpenFileName") as load, \
                patch.object(QFileDialog, "getSaveFileName") as save, \
                patch.object(QFileDialog, "getExistingDirectory") as browse, \
                patch.object(w, "configuration") as validate:
            w.load(); w.save(); w.choose_output(); w.validate()
            for mock in (load, save, browse, validate):
                mock.assert_not_called()
        self.wait_for(lambda: w.status.text() == "运行中")
        completion = []
        w.controller.completed.connect(lambda _: completion.append((w.active(), w.editor.isEnabled(), w.temporary)))
        first_directory = w.last_output
        w.stop()
        self.assertTrue(w.active())
        self.assertFalse(w.set_language("en"))
        self.wait_for(lambda: not w.active())
        self.assertEqual(completion, [(True, False, None)])
        self.assertTrue((first_directory / "summary.json").is_file())
        self.assertTrue(w.editor.isEnabled())
        self.assertTrue(w.set_language("en"))
        first_config = json.loads((first_directory / "config.json").read_text())
        w.fields["period_ms"].setText("200")
        w.fields["duration_s"].setText("0.25")
        w.point_editors[0].fields["source.value"].setText("7.5")
        w.start()
        self.wait_for(lambda: not w.active())
        self.assertEqual(w.result["exit_code"], 0)
        next_config = json.loads((w.last_output / "config.json").read_text())
        self.assertEqual(next_config["period_ms"], 200)
        self.assertEqual(next_config["points"][0]["source"]["value"], 7.5)
        self.assertEqual(json.loads((first_directory / "config.json").read_text()), first_config)
        self.assertNotIn("language", next_config)

    def test_english_and_chinese_show_live_default_names_in_current_language(self):
        w = self.window
        w.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode="rtu-slave", serial=dict(port="TEST"),
            points=[dict(area="holding"), dict(area="input")])))
        now = time.perf_counter()
        rows = [dict(key=f"{area}:{identity-1}:tx", point=identity-1, name="测点 1", area=area, address=0, direction="tx",
                     sampled_s=now, quality="submitted", value=2.5, registers=[0x4020, 0],
                     function=function, sample_id=identity)
                for identity, (area, function) in enumerate((("holding", 3), ("input", 4)), 1)]
        w.live_panel.ingest(dict(state="stopped", rows=rows, stale_after_s=1.))
        w.live_panel.refresh_values()
        for language, name, areas in (("en", "Point 1", ("Holding (FC03)", "Input (FC04)")),
                                      ("zh_CN", "测点 1", ("保持区（FC03）", "输入区（FC04）"))):
            with self.subTest(language=language):
                self.assertTrue(w.set_language(language))
                for index, area in enumerate(("holding", "input")):
                    text = f"{name[:-1]}{index+1} · {areas[index]}"
                    self.assertEqual(w.live_panel.table.item(index, 0).text(), text)
                    self.assertEqual(w.live_panel.selectors[f"{area}:{index}:tx"].text(), f"{text} · TX")

    def test_register_hints_follow_language_without_changing_configuration(self):
        w = self.window
        cases = ((dict(mode="rtu-slave", serial=dict(port="TEST")), "data_note",
                  "Each point belongs to one register area.", "每个测点只配置一个寄存器区",
                  "Currently read-only", "当前为只读"),
                 (task_mapping(mode="rtu-master", serial=dict(port="TEST"), operation="write-multiple"),
                  "readback_note", "FC04 reads a separate register area", "FC04 读取的是独立输入区", "FC03", "FC03"))
        for settings, field, english, chinese, english_detail, chinese_detail in cases:
            with self.subTest(mode=settings["mode"]):
                w.apply_config(TaskConfig.from_mapping(settings))
                before = w.editor.capture_draft()
                for language, text, detail in (("en", english, english_detail),
                                               ("zh_CN", chinese, chinese_detail)):
                    self.assertTrue(w.set_language(language))
                    self.assertIn(text, getattr(w.editor, field).text())
                    self.assertIn(detail, getattr(w.editor, field).text())
                    self.assertEqual(w.editor.capture_draft(), before)


if __name__ == "__main__":
    unittest.main()
