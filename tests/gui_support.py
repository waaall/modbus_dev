"""GUI 测试的环境适配，不改变应用行为或布局验收条件。"""
import os
from pathlib import Path
import sys


def create_test_application(**options):
    from modbus_dev.gui import create_application

    app = create_application(**options)
    if (sys.platform == "win32" and app.platformName() == "offscreen"
            and not getattr(app, "_test_windows_fonts_loaded", False)):
        # Windows 离屏插件不枚举系统字体，与原生界面和预览工具使用同一组字体。
        from PySide6.QtGui import QFontDatabase
        from modbus_dev.ui.theme import ui_font

        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "msyh.ttc"):
            path = fonts / filename
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        font = ui_font()
        font.setHintingPreference(app.font().hintingPreference())
        app.setFont(font)
        app._test_windows_fonts_loaded = True
    return app
