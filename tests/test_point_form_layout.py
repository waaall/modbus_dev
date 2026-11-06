"""验证测点参数在最小栏宽下始终保持两列、无横向裁切且不改变配置草稿。"""
import importlib.util
import os
import unittest

from modbus_dev.domain.config import TaskConfig


@unittest.skipUnless(importlib.util.find_spec('PySide6'), '需要可选 Qt 依赖')
class PointFormLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from modbus_dev.ui.app import create_application
        cls.app = create_application(language='zh_CN')

    def setUp(self):
        from PySide6.QtCore import Qt
        from modbus_dev.ui.configuration_panel import ConfigurationPanel
        self.app.language_manager._install('zh_CN')
        self.panel = ConfigurationPanel()
        # 原生平台也只创建隐藏窗口，不打扰桌面上正在使用的应用。
        self.panel.setAttribute(Qt.WA_DontShowOnScreen)
        self.panel.config_tabs.setCurrentIndex(1)
        self.minimum_width = self.panel.minimumWidth()
        self.resize_panel(self.minimum_width)
        self.panel.show()
        self.settle()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.settle()
        self.app.language_manager._install('zh_CN')

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def resize_panel(self, width):
        self.panel.setMaximumWidth(width)
        self.panel.resize(width, 720)
        self.settle()

    def configure(self, mode='rtu-master', **point):
        config = TaskConfig.from_mapping(dict(mode=mode, access_mode='read-write',
            serial=dict(port='LAYOUT-ONLY'), points=[dict(area='input', **point)]))
        self.panel.apply_config(config)
        self.settle()
        return self.panel.point_editors[0]

    def assert_two_columns(self, editor):
        from modbus_dev.ui.theme import METRICS
        scroll = self.panel.config_tabs.widget(1)
        viewport = scroll.viewport()
        # 水平滚动被禁用时，仅检查滚动条最大值不能识别被裁切的内容。
        self.assertLessEqual(scroll.widget().width(), viewport.width())
        self.assertLessEqual(editor.form.minimumSize().width(), editor.body.width())
        column_positions = set()
        for key, field in editor.fields.items():
            if field.isHidden():
                continue
            with self.subTest(field=key):
                caption = editor.form.labelForField(field)
                column_positions.add(field.x())
                self.assertLess(caption.geometry().right(), field.x())
                self.assertTrue(field.geometry().top() <= caption.geometry().center().y()
                                <= field.geometry().bottom())
                self.assertEqual(field.height(), METRICS.control_height)
                # 检查完整控件而非中心点，防止箭头或右边框已被视口遮掉。
                for widget in (caption, field):
                    position = widget.mapTo(viewport, widget.rect().topLeft())
                    self.assertGreaterEqual(position.x(), 0)
                    self.assertLessEqual(position.x() + widget.width(), viewport.width())
                scroll.ensureWidgetVisible(field)
                self.settle()
                self.assertTrue(viewport.rect().contains(field.mapTo(viewport, field.rect().center())))
        self.assertEqual(len(column_positions), 1)

    def test_bilingual_roles_types_and_sources_fit_minimum_and_resized_widths(self):
        from PySide6.QtTest import QSignalSpy
        # 覆盖最长读回标签、纯读取隐藏源、从站独立区域及全部数值类型。
        cases = (
            dict(data_type='float32', source=dict(kind='sine'), readback_every=1),
            dict(data_type='int16', source=dict(kind='constant')),
            dict(data_type='uint16', source=dict(kind='triangle')),
            dict(data_type='int32', source=dict(kind='triangle')),
            dict(data_type='uint32', source=dict(kind='constant')),
            dict(data_type='float32', operation='read-input'),
            dict(data_type='int16', operation='write-single'),
        )
        for language in ('zh_CN', 'en'):
            self.app.language_manager._install(language)
            self.panel.retranslate_ui()
            for mode in ('tcp-slave', 'rtu-slave', 'rtu-master', 'tcp-master'):
                for point in cases:
                    editor = self.configure(mode, **point)
                    before = self.panel.capture_draft()
                    edited = QSignalSpy(self.panel.edited)
                    for width in (self.minimum_width, 430, 570, self.minimum_width):
                        with self.subTest(language=language, mode=mode, point=point, width=width):
                            self.resize_panel(width)
                            self.assertEqual(self.panel.minimumWidth(), self.minimum_width)
                            self.assert_two_columns(editor)
                    self.assertEqual(self.panel.capture_draft(), before)
                    self.assertEqual(edited.count(), 0)

    def test_disabled_points_and_busy_configuration_keep_the_same_columns(self):
        from PySide6.QtTest import QSignalSpy
        editor = self.configure(source=dict(kind='sine'))
        for enabled, busy in ((True, False), (False, False), (True, True)):
            editor.enabled.setChecked(enabled)
            self.panel.set_busy(busy)
            before = self.panel.capture_draft()
            edited = QSignalSpy(self.panel.edited)
            for width in (self.minimum_width, 570, self.minimum_width):
                with self.subTest(enabled=enabled, busy=busy, width=width):
                    self.resize_panel(width)
                    self.assert_two_columns(editor)
                    self.assertEqual(editor.body.isEnabled(), enabled and not busy)
            self.assertEqual(self.panel.capture_draft(), before)
            self.assertEqual(edited.count(), 0)
            self.panel.set_busy(False)

    def test_short_master_captions_keep_readback_values_and_help(self):
        from PySide6.QtTest import QSignalSpy
        for mode in ('rtu-master', 'tcp-master'):
            editor = self.configure(mode, readback_every=7)
            before = self.panel.capture_draft()
            edited = QSignalSpy(self.panel.edited)
            for language, readback_text, address_text in (
                ('zh_CN', '写后读回间隔', '起始地址'),
                ('en', 'Readback cycles', 'Start address'),
            ):
                with self.subTest(mode=mode, language=language):
                    self.app.language_manager._install(language)
                    self.panel.retranslate_ui()
                    self.settle()
                    readback = editor.fields['readback_every']
                    self.assertEqual(editor.form.labelForField(readback).text(), readback_text)
                    self.assertEqual(editor.form.labelForField(editor.fields['address']).text(), address_text)
                    self.assertEqual(readback.value(), 7)
                    # 简短标签不删除完整用法说明，也不改变读回周期和草稿。
                    self.assertIn('FC03', readback.toolTip())
                    self.assertIn('0', readback.toolTip())
                    self.assert_two_columns(editor)
            self.assertEqual(self.panel.capture_draft(), before)
            self.assertEqual(edited.count(), 0)

    def test_incompatible_choices_keep_short_prompts_help_and_original_drafts(self):
        from PySide6.QtTest import QSignalSpy
        from modbus_dev.ui.i18n import tr
        editor = self.configure(source=dict(kind='sine'))
        editor.fields['data_type'].setCurrentIndex(editor.fields['data_type'].findData('int16'))
        order, source = editor.fields['byte_order'], editor.fields['source.kind']
        order.set_draft_value('B3412')
        editor._changed()
        before = self.panel.capture_draft()
        edited = QSignalSpy(self.panel.edited)
        for language in ('en', 'zh_CN', 'en'):
            self.app.language_manager._install(language)
            self.panel.retranslate_ui()
            for width in (570, self.minimum_width):
                with self.subTest(language=language, width=width):
                    self.resize_panel(width)
                    self.assert_two_columns(editor)
                    for field in (order, source):
                        self.assertEqual(field.currentIndex(), -1)
                        self.assertEqual(field.placeholderText(), str(tr('Select')))
                        self.assertTrue(field.toolTip())
                        self.assertTrue(field.accessibleName())
                    self.assertTrue(editor.selection_note.isVisible())
        self.assertEqual(order.draft_value(), 'B3412')
        self.assertEqual(source.draft_value(), 'sine')
        self.assertEqual(self.panel.capture_draft(), before)
        self.assertEqual(edited.count(), 0)


if __name__ == '__main__':
    unittest.main()
