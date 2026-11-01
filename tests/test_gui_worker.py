"""验证工作进程协议、日志保留配置和可选 Qt 界面的完整生命周期。"""
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from config_factory import task_mapping
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event
from modbus_dev.worker import EventPreview


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class WorkerTests(unittest.TestCase):
    def test_preview_is_bounded_and_counts_loss(self):
        preview = EventPreview()
        for i in range(120):
            preview.add(Event("failure", str(i), "WARNING"))
        events, dropped = preview.drain()
        self.assertEqual(len(events), 100)
        self.assertEqual(dropped, 20)
        self.assertEqual(events[0]["message"], "20")

    def exercise(self, duration, command=None, close_input=False, occupied=False):
        with tempfile.TemporaryDirectory() as root, socket.socket() as reserved:
            if occupied:
                reserved.bind(("127.0.0.1", 0))
                reserved.listen()
                port = reserved.getsockname()[1]
            else:
                port = available_port()
            config = Path(root) / "input.json"
            config.write_text(json.dumps(dict(schema_version=4, mode="tcp-slave", port=port, duration_s=duration)))
            process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(config),
                                        "--output", str(Path(root) / "run")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            records = queue.Queue()
            def read():
                for line in process.stdout:
                    records.put(json.loads(line))
            reader = threading.Thread(target=read)
            reader.start()
            try:
                first = records.get(timeout=5)
                self.assertEqual(first["schema_version"], 1)
                if command:
                    process.stdin.write(json.dumps(command) + "\n")
                    process.stdin.flush()
                if close_input:
                    process.stdin.close()
                process.wait(6)
                reader.join(2)
                packets = [first]
                while not records.empty():
                    packets.append(records.get_nowait())
                terminal = packets[-1]
                self.assertEqual(terminal["type"], "result")
                self.assertEqual(process.returncode, terminal["result"]["exit_code"])
                return terminal["result"]
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(3)
                reader.join(2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_natural_completion(self):
        result = self.exercise(.25)
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(result["counters"]["source_updates"], 0)

    def test_stop_command_and_parent_pipe_closure(self):
        self.assertEqual(self.exercise(0, command=dict(schema_version=1, command="stop"))["exit_code"], 130)
        self.assertEqual(self.exercise(0, close_input=True)["exit_code"], 130)

    def test_start_failure_delivers_terminal_result(self):
        self.assertEqual(self.exercise(.25, occupied=True)["exit_code"], 1)

    def test_unread_status_pipe_does_not_block_session_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps(dict(schema_version=4, mode="tcp-slave", port=available_port(), duration_s=2.5)))
            process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(path),
                                        "--output", str(Path(root) / "run")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                # Deliberately do not read stdout until after the process exits.
                process.wait(7)
                summary = json.loads((Path(root) / "run/summary.json").read_text())
                self.assertEqual(summary["run"]["exit_code"], 0)
                self.assertGreaterEqual(summary["run"]["counters"]["source_updates"], 20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "Install the optional GUI dependency to run Qt tests")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="zh_CN")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.root = tempfile.TemporaryDirectory()
        self.window = Workbench(remember_settings=False)
        self.window.output.setText(self.root.name)
        self.window.fields["port"].setText(str(available_port()))
        self.window.show()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=6):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Qt lifecycle timed out")

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: self.window.temporary is None)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.root.cleanup()

    def test_configuration_roundtrip_all_modes(self):
        for mode in ("rtu-master", "rtu-slave", "tcp-slave"):
            config = TaskConfig.from_mapping(task_mapping(mode=mode, serial=dict(port="TEST"),
                                                 source=dict(kind="sine", value=2), addresses=[0, 4],
                                                 period_ms=200, timeout_ms=150,
                                                 logging=dict(max_files=7),
                                                 readback_every=2 if mode == "rtu-master" else 0))
            self.window.apply_config(config)
            self.assertEqual(self.window.configuration(), config)

    def test_gui_bridge_archives_a_completed_session(self):
        root = Path(self.root.name)
        path = root / "bridge-config.json"
        path.write_text(json.dumps(dict(schema_version=4, mode="tcp-slave", host="127.0.0.1", port=available_port(), duration_s=.25)),
                        encoding="utf-8")
        output = root / "archived"
        bridge = Path(__file__).resolve().parents[1] / "tools/verify_gui_session.py"
        process = subprocess.run([sys.executable, str(bridge), str(path), "--output", str(output)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["exit_code"], 0)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["evidence_complete"])
        self.assertEqual(summary["run"]["counters"], result["counters"])
        self.assertTrue((output / "config.json").is_file())
        self.assertTrue(list(output.glob("events*.jsonl")))
        self.assertTrue((root / "gui-preview.log").is_file())

    def test_retention_and_queue_loss_are_displayed_separately(self):
        self.window.show_recording(dict(evidence_complete=False, retention_truncated=True,
            dropped=0, segments=dict(deleted_files=3, retained_files=2)))
        self.assertIn("不完整", self.window.recording.text())
        self.assertIn("历史已淘汰 3 片", self.window.recording.text())
        self.assertIn("保留最新 2 片", self.window.recording.text())
        self.assertIn("队列丢失 0", self.window.recording.text())

    def test_retention_configuration_reaches_worker(self):
        self.window.fields["logging.max_files"].setText("2")
        self.window.fields["duration_s"].setText("0.25")
        self.window.start()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 0)
        folder = self.window.last_output
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(config["logging"]["max_files"], 2)
        self.assertEqual(summary["segments"]["max_files"], 2)

    def test_mode_and_source_choices_keep_protocol_values(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        mode = self.window.fields["mode"]
        mode.setFocus()
        QTest.keyClick(mode, Qt.Key_Down)
        self.assertEqual(self.window.text("mode"), "rtu-slave")
        self.assertFalse(self.window.serial_box.isHidden())
        self.assertTrue(self.window.tcp_box.isHidden())
        before = mode.currentIndex()
        mode.showPopup()
        self.app.processEvents()
        QTest.keyClick(mode.view(), Qt.Key_Down)
        QTest.keyClick(mode.view(), Qt.Key_Escape)
        self.assertEqual(mode.currentIndex(), before)
        mode.showPopup()
        self.app.processEvents()
        QTest.keyClick(mode.view(), Qt.Key_End)
        QTest.keyClick(mode.view(), Qt.Key_Return)
        self.assertEqual(self.window.text("mode"), mode.itemData(mode.count() - 1))
        # 新增角色后，末项不再固定为 RTU；以下数据源断言仍明确验证 RTU 主站。
        mode.setCurrentIndex(mode.findData("rtu-master"))
        self.assertFalse(self.window.master_box.isHidden())
        point = self.window.point_editors[0]
        # TCP 新角色会安全地切到只读；测试写入数据源前显式恢复读写与写操作。
        access = self.window.fields["access_mode"]
        access.setCurrentIndex(access.findData("read-write"))
        point.fields["operation"].setCurrentIndex(point.fields["operation"].findData("write-multiple"))
        source = point.fields["source.kind"]
        source.setCurrentIndex(source.findData("sine"))
        self.assertTrue(point.fields["source.amplitude"].isEnabled())
        self.assertEqual(point.form.labelForField(point.fields["source.value"]).text(), "偏置")
        source.setCurrentIndex(source.findData("constant"))
        self.assertFalse(point.fields["source.amplitude"].isEnabled())
        self.assertEqual(point.form.labelForField(point.fields["source.value"]).text(), "数值")

    def test_summary_uses_counters_from_the_completed_session(self):
        self.window.counter_mode = "tcp-slave"
        self.window.show_counters(dict(source_updates=17, slave_request=3, rx=4, tx=3))
        self.assertEqual([label.text() for label in self.window.metric_values], ["17", "3", "4", "3"])
        self.window.apply_config(TaskConfig.from_mapping(dict(mode="rtu-master", serial=dict(port="TEST"))))
        self.assertEqual([label.text() for label in self.window.metric_values], ["17", "3", "4", "3"])
        self.assertEqual(self.window.metric_labels[0].text(), "数据更新")

    def test_visible_controls_share_the_same_height(self):
        from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QSpinBox
        from modbus_dev.gui_theme import METRICS
        for mode in ("tcp-slave", "rtu-slave", "rtu-master", "tcp-master"):
            self.window.apply_config(TaskConfig.from_mapping(dict(mode=mode, serial=dict(port="TEST"))))
            for tab in range(self.window.config_tabs.count()):
                self.window.config_tabs.setCurrentIndex(tab)
                self.app.processEvents()
                widgets = [widget for kind in (QComboBox, QLineEdit, QPushButton, QSpinBox)
                           for widget in self.window.findChildren(kind)
                           if not (isinstance(widget, QLineEdit) and isinstance(widget.parent(), (QSpinBox, QComboBox)))]
                for widget in widgets:
                    if widget.isVisible():
                        self.assertEqual(widget.height(), METRICS.control_height, widget.objectName() or widget.text())

    def test_invalid_configuration_never_starts_worker(self):
        self.window.point_editors[1].enabled.setChecked(True)
        self.window.point_editors[1].fields["address"].setValue(1)
        with patch.object(self.window, "error") as error:
            self.window.start()
        error.assert_called_once()
        self.assertIsNone(self.window.process)

    def test_pages_separate_connection_data_and_other_settings(self):
        self.assertEqual([self.window.config_tabs.tabText(i) for i in range(3)],
                         ["连接与时序", "数据设置", "其他设置"])
        expected = {"period_ms": 0, "duration_s": 0, "timeout_ms": 0, "logging.max_files": 2}
        for key, tab in expected.items():
            self.assertTrue(self.window.config_tabs.widget(tab).isAncestorOf(self.window.fields[key]))
        self.assertNotIn("addresses", self.window.fields)
        self.assertEqual(len(self.window.point_editors), 8)
        for editor in self.window.point_editors:
            self.assertTrue(self.window.config_tabs.widget(1).isAncestorOf(editor))

    def test_point_settings_roundtrip_disabled_slots_and_conflict_feedback(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[
            dict(name="温度", address=8, source=dict(value=12.5)),
            dict(enabled=False, name="预留", address=8, source=dict(kind="sine", frequency_hz=1)),
            dict(name="压力", address=0, source=dict(kind="sine", value=5, amplitude=2, frequency_hz=.5)),
            dict(enabled=False, address=20, source=dict(value=-1))]))
        self.window.apply_config(config)
        self.assertEqual(self.window.configuration(), config)
        second = self.window.point_editors[1]
        self.assertFalse(second.body.isHidden())
        self.assertFalse(second.body.isEnabled())
        second.enabled.setChecked(True)
        self.assertFalse(second.body.isHidden())
        self.assertTrue(second.body.isEnabled())
        self.assertIn("测点 1 与测点 2", self.window.points_status.text())
        self.assertEqual(second.range_label.property("tone"), "error")
        second.fields["address"].setValue(4)
        self.assertNotIn("重叠", self.window.points_status.text())
        self.assertEqual(second.range_label.property("tone"), "idle")
        self.assertEqual(self.window.configuration().addresses, (8, 4, 0))
        for editor in self.window.point_editors:
            editor.enabled.setChecked(False)
        self.assertIn("至少启用", self.window.points_status.text())
        with self.assertRaisesRegex(ValueError, "至少启用"):
            self.window.configuration()

    def test_independent_point_values_reach_actual_worker(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", duration_s=0,
            port=available_port(), points=[dict(address=2, source=dict(value=-3.5)),
                                          dict(address=0, source=dict(value=12.5))])))
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        with socket.create_connection(("127.0.0.1", int(self.window.text("port"))), timeout=1) as peer:
            peer.sendall(bytes.fromhex("000700000006010400000004"))
            response = b""
            while len(response) < 17:
                chunk = peer.recv(17 - len(response))
                self.assertTrue(chunk)
                response += chunk
        self.assertEqual(response.hex(), "00070000000b01040841480000c0600000")
        self.window.stop()
        self.wait_for(lambda: self.window.temporary is None)
        snapshot = json.loads((self.window.last_output / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["schema_version"], 4)
        self.assertEqual([p["source"]["value"] for p in snapshot["points"][:2]], [-3.5, 12.5])

    def test_address_spinbox_and_disabled_point_parameters(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        editor = self.window.point_editors[0]
        self.window.config_tabs.setCurrentIndex(1)
        address = editor.fields["address"]
        address.setFocus()
        QTest.keyClick(address, Qt.Key_Up)
        self.assertEqual(address.value(), 1)
        QTest.keyClick(address, Qt.Key_Down)
        self.assertEqual(address.value(), 0)
        address.setValue(65534)
        QTest.keyClick(address, Qt.Key_Up)
        self.assertEqual(address.value(), 65535)
        self.assertIn("65535～65536", editor.range_label.text())
        self.assertEqual(editor.range_label.property("tone"), "error")
        address.setValue(65534)
        editor.fields["source.value"].setText("23.75")
        editor.enabled.setChecked(False)
        editor.enabled.setChecked(True)
        self.assertEqual(self.window.configuration().enabled_points[0].source.value, 23.75)

    def test_disabled_unsupported_source_is_preserved_until_enabled(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[dict(address=0),
            dict(enabled=False, address=2, source=dict(kind="dyn", path="reserved.dyn"))]))
        self.window.apply_config(config)
        self.assertEqual(self.window.configuration(), config)
        self.window.point_editors[1].enabled.setChecked(True)
        with self.assertRaisesRegex(ValueError, "测点 2"):
            self.window.configuration()

    def test_start_stop_and_restart(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.assertFalse(self.window.editor.isEnabled())
        with socket.create_connection(("127.0.0.1", int(self.window.text("port"))), timeout=1) as peer:
            peer.sendall(bytes.fromhex("000700000006010400000002"))
            response = b""
            while len(response) < 13:
                response += peer.recv(13 - len(response))
            self.assertEqual(response.hex(), "00070000000701040400000000")
        self.window.stop()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 130)
        self.assertTrue(self.window.editor.isEnabled())
        first = self.window.last_output
        self.window.fields["duration_s"].setText("0.25")
        self.window.start()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 0)
        self.assertNotEqual(first, self.window.last_output)

    def test_worker_crash_is_visible_and_releases_ui(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.window.monitor_tabs.setCurrentIndex(1)
        self.window.process.kill()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertIn("异常结束", self.window.status.text())
        self.assertEqual(self.window.status.property("tone"), "error")
        self.assertEqual(self.window.monitor_tabs.currentIndex(), 0)
        self.assertTrue(self.window.start_button.isEnabled())
        marker = json.loads((self.window.last_output / "gui-worker-error.json").read_text(encoding="utf-8"))
        self.assertFalse(marker["evidence_complete"])

    def test_close_waits_for_worker_cleanup(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.window.close()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 130)
        self.assertFalse(self.window.isVisible())

    def test_failed_process_launch_restores_controls(self):
        with patch("modbus_dev.gui.sys.executable", str(Path(self.root.name) / "missing-python.exe")):
            self.window.start()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertIn("异常结束", self.window.status.text())

    def test_log_preview_is_bounded(self):
        for _ in range(25):
            self.window.display(dict(type="status", state="running", counters={},
                                     events=[dict(level="WARNING", code="test", message="message")] * 100))
        self.assertEqual(self.window.logs.document().blockCount(), 2000)
