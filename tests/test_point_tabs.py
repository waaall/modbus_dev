"""验证微型测点页签的导航、灰显、草稿隔离、错误提示与窄栏布局。"""
import importlib.util
import os
import unittest

from modbus_dev.domain.config import MAX_POINTS, TaskConfig


@unittest.skipUnless(importlib.util.find_spec('PySide6'), '需要可选 Qt 依赖')
class PointTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from gui_support import create_test_application
        cls.app = create_test_application(language='zh_CN')

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.app.language_manager._install('zh_CN')
        self.window = Workbench(remember_settings=False)
        self.window.show()
        self.window.config_tabs.setCurrentIndex(1)
        self.tabs = self.window.point_tabs
        self.bar = self.tabs.tab_bar
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def select(self, index):
        self.tabs.setCurrentIndex(index)
        self.app.processEvents()

    def state(self, index):
        return self.bar.tabData(index)

    def test_numeric_tabs_click_through_exactly_one_page_without_editing(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QSignalSpy, QTest
        self.assertEqual(self.bar.count(), MAX_POINTS)
        self.assertEqual([self.bar.tabText(i) for i in range(MAX_POINTS)],
                         [str(i + 1) for i in range(MAX_POINTS)])
        self.assertFalse(self.bar.usesScrollButtons())
        self.assertFalse(self.bar.tabsClosable())
        self.assertTrue(self.bar.expanding())
        self.assertFalse(self.bar.drawBase())
        before = self.window.editor.capture_draft()
        edited = QSignalSpy(self.window.editor.edited)
        changed = QSignalSpy(self.window.editor.changed)
        for index in range(MAX_POINTS):
            self.window.config_tabs.widget(1).ensureWidgetVisible(self.bar)
            QTest.mouseClick(self.bar, Qt.LeftButton, pos=self.bar.tabRect(index).center())
            self.app.processEvents()
            self.assertEqual(self.tabs.currentIndex(), index)
            for number, editor in enumerate(self.window.point_editors):
                self.assertEqual(editor.isVisible(), number == index)
                self.assertFalse(hasattr(editor, 'expand'))
        self.assertEqual(self.window.editor.capture_draft(), before)
        self.assertEqual(edited.count(), 0)
        self.assertEqual(changed.count(), 0)

    def test_tab_bar_keeps_native_keyboard_navigation(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.window.config_tabs.widget(1).ensureWidgetVisible(self.bar)
        self.bar.setFocus()
        QTest.keyClick(self.bar, Qt.Key_Right)
        self.assertEqual(self.tabs.currentIndex(), 1)
        QTest.keyClick(self.bar, Qt.Key_Left)
        self.assertEqual(self.tabs.currentIndex(), 0)

    def test_disabled_point_shows_saved_parameters_and_only_enable_is_editable(self):
        points = [dict(address=0), dict(enabled=False, address=12, name='备用',
                  source=dict(kind='sine', value=12.5, amplitude=2, frequency_hz=.5))]
        config = TaskConfig.from_mapping(dict(schema_version=4, mode='tcp-slave', points=points))
        self.window.apply_config(config)
        self.select(1)
        editor = self.window.point_editors[1]
        self.assertTrue(editor.body.isVisible())
        self.assertFalse(editor.body.isEnabled())
        self.assertTrue(editor.enabled.isEnabled())
        self.assertTrue(self.bar.isTabEnabled(1))
        for key in ('name', 'address', 'source.kind', 'source.value', 'source.amplitude', 'source.frequency_hz'):
            self.assertTrue(editor.fields[key].isVisible(), key)
            self.assertFalse(editor.fields[key].isEnabled(), key)
            self.assertFalse(editor.form.labelForField(editor.fields[key]).isEnabled(), key)
        self.assertEqual(editor.fields['source.value'].text(), '12.5')
        self.assertEqual(self.window.configuration(), config)
        height = editor.sizeHint().height()
        editor.enabled.setChecked(True)
        self.assertTrue(editor.body.isEnabled())
        self.assertEqual(editor.sizeHint().height(), height)
        self.assertEqual(self.window.configuration().points[1].source.value, 12.5)
        editor.enabled.setChecked(False)
        self.assertEqual(self.window.configuration(), config)

    def test_switching_and_draft_restore_preserve_unfinished_input_and_selection(self):
        self.select(1)
        editor = self.window.point_editors[1]
        editor.enabled.setChecked(True)
        editor.fields['name'].setText('压力草稿')
        editor.fields['source.value'].setText('unfinished')
        before = self.window.editor.capture_draft()
        self.select(MAX_POINTS - 1)
        self.select(0)
        self.select(1)
        self.assertEqual(self.window.editor.capture_draft(), before)
        self.window.editor.restore_draft(before)
        self.assertEqual(self.tabs.currentIndex(), 1)
        self.assertEqual(self.window.editor.capture_draft(), before)
        self.assertEqual(editor.fields['source.value'].text(), 'unfinished')
        self.assertFalse(self.window.start_button.isEnabled())

    def test_hidden_conflicts_overflow_and_incompatible_choices_mark_their_tabs(self):
        from PySide6.QtGui import QColor
        from modbus_dev.ui.theme import COLORS
        first, second = self.window.point_editors[:2]
        second.enabled.setChecked(True)
        second.fields['address'].setValue(first.fields['address'].value())
        self.assertTrue(self.state(0)['error'])
        self.assertTrue(self.state(1)['error'])
        self.assertEqual(self.bar.tabTextColor(1), QColor(COLORS['error']))
        self.assertIn('重叠', self.bar.tabToolTip(1))
        second.fields['address'].setValue(65535)
        self.assertTrue(self.state(1)['error'])
        self.assertIn('65535', self.bar.tabToolTip(1))
        second.fields['address'].setValue(10)
        self.assertFalse(self.state(0)['error'])
        self.assertFalse(self.state(1)['error'])
        second.fields['source.kind'].setCurrentIndex(second.fields['source.kind'].findData('sine'))
        second.fields['data_type'].setCurrentIndex(second.fields['data_type'].findData('int16'))
        self.assertTrue(self.state(1)['error'])
        second.enabled.setChecked(False)
        self.assertFalse(self.state(1)['error'])
        self.assertTrue(self.bar.isTabEnabled(1))
        self.assertEqual(self.bar.tabTextColor(1), QColor(COLORS['disabled_ink']))

    def test_source_validation_and_period_changes_update_hidden_tab_errors(self):
        editor = self.window.point_editors[-1]
        editor.enabled.setChecked(True)
        editor.fields['source.value'].setText('unfinished')
        self.assertTrue(self.state(MAX_POINTS - 1)['error'])
        self.assertIn('有效数字', self.bar.tabToolTip(MAX_POINTS - 1))
        editor.fields['source.value'].setText('2')
        source = editor.fields['source.kind']
        source.setCurrentIndex(source.findData('sine'))
        editor.fields['source.frequency_hz'].setText('1')
        self.assertFalse(self.state(MAX_POINTS - 1)['error'])
        self.window.fields['period_ms'].setText('1000')
        self.assertTrue(self.state(MAX_POINTS - 1)['error'])
        self.window.fields['period_ms'].setText('0')
        self.assertFalse(self.state(MAX_POINTS - 1)['error'])
        self.window.fields['period_ms'].setText('100')
        source.setCurrentIndex(source.findData('triangle'))
        editor.fields['source.lower'].setText('10')
        editor.fields['source.upper'].setText('2')
        self.assertTrue(self.state(MAX_POINTS - 1)['error'])
        editor.fields['source.upper'].setText('20')
        self.assertFalse(self.state(MAX_POINTS - 1)['error'])

    def test_disabled_and_error_tab_text_colors_are_actually_rendered(self):
        from PySide6.QtGui import QColor
        from modbus_dev.ui.theme import COLORS
        editor = self.window.point_editors[1]
        editor.enabled.setChecked(True)
        editor.fields['source.value'].setText('unfinished')
        self.window.config_tabs.widget(1).ensureWidgetVisible(self.bar)
        self.app.processEvents()
        rendered = self.bar.grab().toImage()
        dpr = rendered.devicePixelRatio()
        for index, color in ((1, 'error'), (MAX_POINTS - 1, 'disabled_ink')):
            with self.subTest(color=color):
                target = QColor(COLORS[color])
                bounds = self.bar.tabRect(index).adjusted(5, 5, -5, -5)
                found = any(all(abs(actual - expected) <= 12 for actual, expected in
                                zip(rendered.pixelColor(round(x * dpr), round(y * dpr)).getRgb()[:3],
                                    target.getRgb()[:3]))
                            for x in range(bounds.left(), bounds.right() + 1)
                            for y in range(bounds.top(), bounds.bottom() + 1))
                self.assertTrue(found, f'{color} text was overridden by a global style')

    def test_language_changes_keep_numbers_selection_drafts_and_update_tooltips(self):
        self.select(MAX_POINTS - 1)
        editor = self.window.point_editors[-1]
        editor.fields['name'].setText('备用温度')
        before = self.window.editor.capture_draft()
        for language, title in (('en', 'Point'), ('zh_CN', '测点')):
            self.app.language_manager._install(language)
            self.window.retranslate_ui()
            self.assertEqual(self.tabs.currentIndex(), MAX_POINTS - 1)
            self.assertEqual(self.bar.tabText(MAX_POINTS - 1), str(MAX_POINTS))
            self.assertIn(f'{title} {MAX_POINTS}', self.bar.tabToolTip(MAX_POINTS - 1))
            self.assertIn('备用温度', self.bar.tabToolTip(MAX_POINTS - 1))
            self.assertEqual(self.window.editor.capture_draft(), before)

    def test_all_tabs_fit_minimum_configuration_width_in_both_languages_and_all_modes(self):
        from modbus_dev.ui.configuration_panel import ConfigurationPanel
        panel = ConfigurationPanel()
        try:
            panel.setFixedWidth(panel.minimumWidth())
            panel.resize(panel.width(), 840)
            panel.config_tabs.setCurrentIndex(1)
            panel.show()
            for language in ('zh_CN', 'en'):
                self.app.language_manager._install(language)
                for mode in ('tcp-slave', 'rtu-slave', 'rtu-master'):
                    with self.subTest(language=language, mode=mode):
                        panel.apply_config(TaskConfig.from_mapping(dict(mode=mode, serial=dict(port='TEST'))))
                        panel.retranslate_ui()
                        self.app.processEvents()
                        bar = panel.point_tabs.tab_bar
                        viewport = panel.config_tabs.widget(1).viewport()
                        last = bar.tabRect(MAX_POINTS - 1)
                        self.assertTrue(bar.rect().contains(last))
                        self.assertLessEqual(bar.mapTo(viewport, last.bottomRight()).x(), viewport.width())
                        self.assertLessEqual(bar.height(), 30)
                        self.assertFalse(bar.usesScrollButtons())
                        widths = [bar.tabRect(i).width() for i in range(bar.count())]
                        self.assertLessEqual(max(widths) - min(widths), 1)
                        self.assertEqual(sum(widths), bar.width())
                        self.assertEqual(last.right(), bar.width() - 1)
                        self.assertEqual(panel.width(), 374)
                        self.assertEqual(panel.config_tabs.widget(1).horizontalScrollBar().maximum(), 0)
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()

    def test_tabs_share_the_full_width_when_the_left_splitter_changes_size(self):
        from PySide6.QtTest import QSignalSpy
        self.window.resize(1280, 900)
        self.app.processEvents()
        before = self.window.editor.capture_draft()
        edited = QSignalSpy(self.window.editor.edited)
        previous_width = None
        for left_width in (374, 430, 570, 374):
            with self.subTest(left_width=left_width):
                workspace = self.window.workspace
                workspace.setSizes([left_width, workspace.width() - left_width - workspace.handleWidth()])
                self.app.processEvents()
                actual_width = self.bar.width()
                if previous_width is not None:
                    self.assertNotEqual(actual_width, previous_width)
                previous_width = actual_width
                for selected in (0, MAX_POINTS // 2, MAX_POINTS - 1):
                    self.select(selected)
                    bounds = [self.bar.tabRect(index) for index in range(MAX_POINTS)]
                    widths = [rect.width() for rect in bounds]
                    self.assertLessEqual(max(widths) - min(widths), 1)
                    self.assertEqual(sum(widths), actual_width)
                    self.assertEqual(bounds[0].left(), 0)
                    self.assertEqual(bounds[-1].right(), actual_width - 1)
                    self.assertEqual(self.tabs.pages.width(), actual_width)
                    self.assertEqual(self.bar.height(), 28)
                    for first, second in zip(bounds, bounds[1:]):
                        self.assertEqual(first.right() + 1, second.left())
        self.assertEqual(self.window.editor.capture_draft(), before)
        self.assertEqual(edited.count(), 0)

    def test_tab_strip_and_content_have_no_baseline_border_or_selected_underline(self):
        from PySide6.QtGui import QColor
        from modbus_dev.ui.theme import COLORS
        self.assertFalse(self.bar.drawBase())
        self.assertEqual(self.tabs.pages.frameWidth(), 0)
        self.window.config_tabs.widget(1).ensureWidgetVisible(self.bar)
        self.app.processEvents()
        rendered = self.bar.grab().toImage()
        dpr = rendered.devicePixelRatio()
        selected = self.bar.tabRect(self.tabs.currentIndex())
        bottom = rendered.pixelColor(round(selected.center().x() * dpr),
                                     round((selected.bottom() - 1) * dpr))
        self.assertEqual(bottom, QColor(COLORS['accent_soft']))

    def test_only_current_page_controls_scroll_height(self):
        editor = self.window.point_editors[1]
        editor.enabled.setChecked(True)
        source = editor.fields['source.kind']
        source.setCurrentIndex(source.findData('sine'))
        self.select(1)
        long_height = self.tabs.pages.sizeHint().height()
        long_actual_height = self.tabs.pages.height()
        self.select(0)
        self.assertLess(self.tabs.pages.sizeHint().height(), long_height)
        self.assertLess(self.tabs.pages.height(), long_actual_height)
        margins = self.tabs.pages.contentsMargins()
        current = self.tabs.pages.currentWidget()
        inner_width = self.tabs.pages.width() - margins.left() - margins.right()
        self.assertEqual(self.tabs.pages.heightForWidth(self.tabs.pages.width()),
                         current.heightForWidth(inner_width) + margins.top() + margins.bottom())

    def test_busy_state_locks_tabs_and_enable_switch_until_stopped(self):
        self.window.editor.set_busy(True)
        self.assertFalse(self.bar.isEnabled())
        self.assertTrue(all(not editor.enabled.isEnabled() for editor in self.window.point_editors))
        self.window.editor.set_busy(False)
        self.assertTrue(self.bar.isEnabled())
        self.assertTrue(all(editor.enabled.isEnabled() for editor in self.window.point_editors))


if __name__ == '__main__':
    unittest.main()
