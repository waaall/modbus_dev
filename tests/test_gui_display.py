"""验证小屏滚动配置可达性，并将 Windows 原生 DPI 检查与离屏生命周期分开。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
HAS_QT = importlib.util.find_spec("PySide6") is not None


def clean_qt_environment():
    environment = dict(os.environ)
    for key in ("QT_QPA_PLATFORM", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_FONT_DPI",
                "QT_ENABLE_HIGHDPI_SCALING", "QT_SCALE_FACTOR_ROUNDING_POLICY", "QT_AUTO_SCREEN_SCALE_FACTOR"):
        environment.pop(key, None)
    return environment


@unittest.skipUnless(HAS_QT, "Install the optional GUI dependency")
class DisplayTests(unittest.TestCase):
    def test_compact_screen_keeps_configuration_and_actions_accessible(self):
        environment = clean_qt_environment()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        script = '''
from PySide6.QtCore import Qt
from modbus_dev.gui import Workbench, create_application
app = create_application(language="zh_CN")
window = Workbench(remember_settings=False)
window.show()
app.processEvents()
assert app.platformName() == 'offscreen'
assert window.width() <= window.screen().availableGeometry().width()
assert window.height() <= window.screen().availableGeometry().height()
assert window.workspace.orientation() == Qt.Vertical
for index, editor in enumerate(window.point_editors):
    editor.enabled.setChecked(True)
    if index % 2:
        editor.fields['source.kind'].setCurrentIndex(1)
for tab in range(window.config_tabs.count()):
    window.config_tabs.setCurrentIndex(tab)
    app.processEvents()
    scroll = window.config_tabs.widget(tab)
    if tab == 0:
        groups = [(None, [window.fields['unit'], window.fields['duration_s']])]
    elif tab == 1:
        groups = [(editor.index, [editor.enabled, *[widget for widget in editor.fields.values()
                                                   if not widget.isHidden()]])
                  for editor in window.point_editors]
    else:
        groups = [(None, [window.fields['logging.max_files']])]
    for point_index, fields in groups:
        if point_index is not None:
            window.point_tabs.setCurrentIndex(point_index)
        app.processEvents()
        for field in fields:
            scroll.ensureWidgetVisible(field)
            app.processEvents()
            position = field.mapTo(scroll.viewport(), field.rect().center())
            assert field.isVisible()
            assert scroll.viewport().rect().contains(position)
window.runtime_scroll.ensureWidgetVisible(window.start_button)
app.processEvents()
position = window.start_button.mapTo(window.runtime_scroll.viewport(), window.start_button.rect().center())
assert window.runtime_scroll.viewport().rect().contains(position)
window.close()
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=PROJECT, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Requires the Windows native Qt platform")
    def test_windows_native_font_and_per_monitor_dpi_at_common_scales(self):
        environment = clean_qt_environment()
        probe = subprocess.run(
            [sys.executable, "-c", "from modbus_dev.gui import create_application; "
             "app=create_application(language='zh_CN'); print(app.primaryScreen().devicePixelRatio())"],
            cwd=PROJECT, env=environment, capture_output=True, text=True, timeout=30, check=True)
        native_dpr = float(probe.stdout.strip())
        for scale in sorted({1, 1.25, 1.5, 1.75, 2, native_dpr}):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory(prefix="modbus-dpi-") as root:
                result = subprocess.run(
                    [sys.executable, str(PROJECT / "tools/preview_gui.py"), "--native",
                     "--scale", str(scale / native_dpr), "--output-dir", root],
                    cwd=PROJECT, env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["platform"], "windows")
                self.assertEqual(report["platform_setting"], "windows:fontengine=gdi")
                self.assertTrue(report["per_monitor_v2"])
                self.assertEqual(report["hinting"], "PreferFullHinting")
                self.assertEqual(report["log_hinting"], report["hinting"])
                self.assertAlmostEqual(report["dpr"], scale)
                self.assertTrue(any("YaHei" in family for family in report["fonts"]))
                self.assertTrue(all(height == 36 for height in report["control_heights"].values()))
                self.assertEqual(len(report["screenshots"]), 12)
                self.assertTrue(all(size <= available for size, available in
                                    zip(report["logical_size"], report["available_size"])))
                for screenshot in report["screenshots"]:
                    self.assertEqual(screenshot["dpr"], report["dpr"])
                    self.assertEqual(screenshot["pixel_size"],
                                     [round(size * scale) for size in report["logical_size"]])


if __name__ == "__main__":
    unittest.main()
