"""离屏验证八点表单、逐点操作、独立区域与红黄周期校验，不打开真实串口。"""
import importlib.util
import json
import os
import unittest

from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import ConfigurationError


@unittest.skipUnless(importlib.util.find_spec('PySide6'), '需要可选 Qt 依赖')
class IndependentGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from modbus_dev.gui import create_application
        cls.app = create_application(language='zh_CN')

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.app.language_manager._install('zh_CN')
        self.window = Workbench(remember_settings=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def configure_master(self, **options):
        self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-master',
            serial=dict(port='TEST', baud=115200), points=[dict(operation='read-input')], **options)))

    def test_eight_editors_and_no_global_order_or_operation(self):
        self.assertEqual(len(self.window.point_editors), 8)
        for field in ('byte_order', 'master_operation', 'read_function', 'readback_every'):
            self.assertNotIn(field, self.window.fields)
        self.assertTrue(all('operation' in editor.fields and 'area' in editor.fields
                            for editor in self.window.point_editors))

    def test_type_changes_choose_a_valid_local_default_and_keep_compatible_orders(self):
        editor = self.window.point_editors[0]
        types, orders = (editor.fields[key] for key in ('data_type', 'byte_order'))
        self.assertEqual(orders.currentText(), '1234')
        orders.setCurrentIndex(orders.findData('B3412'))
        types.setCurrentIndex(types.findData('int32'))
        self.assertEqual(orders.currentData(), 'B3412')
        types.setCurrentIndex(types.findData('uint16'))
        self.assertEqual((orders.currentData(), orders.currentText()), ('B12', '12'))
        types.setCurrentIndex(types.findData('float32'))
        self.assertEqual((orders.currentData(), orders.currentText()), ('B1234', '1234'))

    def test_native_slave_only_exposes_the_selected_area_and_initial_value(self):
        editor = self.window.point_editors[0]
        editor.fields['area'].setCurrentIndex(editor.fields['area'].findData('holding'))
        self.assertTrue(editor.fields['source.kind'].isHidden())
        self.assertEqual(editor.form.labelForField(editor.fields['source.value']).text(), '初始值')
        settings = self.window.configuration()
        self.assertEqual(settings.enabled_points[0].area, 'holding')
        self.assertEqual(settings.enabled_points[0].source.kind, 'constant')
        editor.fields['area'].setCurrentIndex(editor.fields['area'].findData('input'))
        self.assertFalse(editor.fields['source.kind'].isHidden())

    def test_two_areas_can_share_address_without_conflict(self):
        first, second = self.window.point_editors[:2]
        first.fields['area'].setCurrentIndex(first.fields['area'].findData('holding'))
        second.enabled.setChecked(True)
        second.fields['address'].setValue(0)
        second.fields['area'].setCurrentIndex(second.fields['area'].findData('input'))
        self.window.configuration()
        self.assertNotIn('重叠', self.window.points_status.text())
        second.fields['area'].setCurrentIndex(second.fields['area'].findData('holding'))
        self.assertIn('重叠', self.window.points_status.text())
        self.assertFalse(self.window.start_button.isEnabled())

    def test_mixed_operations_and_per_point_readback(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-master',
            serial=dict(port='TEST', baud=115200), points=[dict(operation='read-input'),
                dict(operation='write-multiple', address=10, readback_every=2)])))
        first, second = self.window.point_editors[:2]
        self.window.point_tabs.setCurrentIndex(1)
        self.assertTrue(first.fields['source.kind'].isHidden())
        self.assertFalse(second.fields['source.kind'].isHidden())
        self.assertTrue(first.fields['readback_every'].isHidden())
        self.assertFalse(second.fields['readback_every'].isHidden())
        self.assertEqual(self.window.configuration().point_readback(self.window.configuration().points[1]), 2)
        self.assertEqual(set(self.window.live_panel.selectors),
                         {'input:0:rx', 'holding:1:tx', 'holding:1:rx'})

    def test_red_and_yellow_are_distinct_and_start_remains_enabled_for_yellow(self):
        self.configure_master()
        fields = self.window.fields
        fields['timeout_ms'].setText('10')
        for period, tone, enabled in ((49, 'error', False), (50, 'warning', True), (59, 'warning', True), (60, 'idle', True)):
            with self.subTest(period=period):
                fields['period_ms'].setText(str(period))
                self.assertEqual(self.window.editor.budget_note.property('tone'), tone)
                self.assertEqual(self.window.start_button.isEnabled(), enabled)
        fields['serial.baud'].setEditText('1200')
        self.assertIn('210ms', self.window.editor.budget_note.text())
        self.assertFalse(self.window.start_button.isEnabled())

    def test_all_roles_enforce_50ms_and_preserve_invalid_saved_drafts(self):
        for mode in ('tcp-master', 'tcp-slave', 'rtu-master', 'rtu-slave'):
            with self.subTest(mode=mode):
                self.window.apply_config(TaskConfig.from_mapping(dict(mode=mode,
                    period_ms=50, timeout_ms=40, serial=dict(port='TEST', baud=115200))))
                field = self.window.fields['period_ms']
                self.assertTrue(self.window.start_button.isEnabled())
                self.assertIn('50', field.toolTip())
                # 旧草稿可以展示，但不得静默改成 50ms 或在低周期下启动。
                field.setText('40')
                draft = self.window.editor.capture_draft()
                self.window.editor.restore_draft(draft)
                self.assertEqual(field.text(), '40')
                self.assertFalse(self.window.start_button.isEnabled())
                with self.assertRaises(ConfigurationError):
                    self.window.configuration()
                field.setText('50')
                self.assertTrue(self.window.start_button.isEnabled())

    def test_request_count_tracks_contiguous_and_disjoint_points(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-master',
            serial=dict(port='TEST', baud=115200), points=[dict(operation='read-input', address=2*i) for i in range(8)])))
        self.assertIn('1 笔请求', self.window.editor.budget_note.text())
        self.window.point_editors[-1].fields['address'].setValue(30)
        self.assertIn('2 笔请求', self.window.editor.budget_note.text())

    def test_byte_orders_and_operations_survive_draft_restore(self):
        self.configure_master()
        point = self.window.point_editors[0]
        point.fields['operation'].setCurrentIndex(point.fields['operation'].findData('write-multiple'))
        point.fields['source.value'].setText('unfinished')
        draft = self.window.editor.capture_draft()
        self.window.editor.restore_draft(json.loads(json.dumps(draft)))
        self.assertEqual(self.window.editor.capture_draft(), draft)
        self.assertFalse(self.window.start_button.isEnabled())

    def test_bilingual_budget_and_area_names_follow_language(self):
        self.configure_master()
        for language, word in (('en', 'workflow 10 ms'), ('zh_CN', '流程预留 10ms')):
            self.app.language_manager._install(language)
            self.window.retranslate_ui()
            self.assertIn(word, self.window.editor.budget_note.text())

    def test_disabled_future_type_is_preserved_until_the_point_is_enabled(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode='tcp-slave', points=[dict(area='input'),
            dict(enabled=False, data_type='future', byte_order='unused', source=dict(kind='future'))]))
        self.window.apply_config(config)
        self.assertEqual(self.window.configuration(), config)
        self.window.point_editors[1].enabled.setChecked(True)
        self.assertFalse(self.window.start_button.isEnabled())
        with self.assertRaises(ValueError):
            self.window.configuration()

    def test_period_names_explain_different_master_and_slave_roles(self):
        cases = (('rtu-master', '通信轮询周期（ms）', '一轮已配置测点的读写'),
                 ('rtu-slave', '输入区数据更新周期（ms）', '所有启用的输入区测点'),
                 ('tcp-slave', '输入区数据更新周期（ms）', '所有启用的输入区测点'))
        for mode, caption, explanation in cases:
            with self.subTest(mode=mode):
                config = TaskConfig.from_mapping(dict(schema_version=4, mode=mode,
                    serial=dict(port='TEST', baud=115200), points=[dict(area='input', operation='read-input')]))
                self.window.apply_config(config)
                field = self.window.fields['period_ms']
                label = self.window.timing_box.layout().labelForField(field)
                self.assertEqual(label.text(), caption)
                self.assertEqual(field.accessibleName(), caption)
                self.assertIn(explanation, field.toolTip())
                if mode != 'rtu-master':
                    for text in ('不更新保持区', '不决定主站读取频率'):
                        self.assertIn(text, self.window.editor.budget_note.text())
                self.assertEqual(self.window.configuration(), config)

    def test_slave_help_explains_initial_values_writes_and_locally_generated_inputs(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-slave',
            access_mode='read-write', serial=dict(port='TEST'), points=[dict(area='holding')])) )
        note = self.window.editor.data_note.text()
        for text in ('保存数值供主站读取', '接收并保存主站写入', 'FC06/16', '本机按数据源生成', '主站不能写入'):
            self.assertIn(text, note)
        editor = self.window.point_editors[0]
        for text in ('会话启动时', '保留主站写入', '不随输入区数据更新周期刷新'):
            self.assertIn(text, editor.source_note.text())
            self.assertIn(text, editor.fields['source.value'].toolTip())
        area = editor.fields['area']
        self.assertIn('接收并保存主站写入', area.toolTip())
        area.setCurrentIndex(area.findData('input'))
        self.assertIn('本机按数据源生成', editor.source_note.text())
        self.assertIn('主站不能写入', editor.source_note.text())
        self.assertEqual(editor.fields['source.value'].toolTip(), '')
        access = self.window.fields['access_mode']
        access.setCurrentIndex(access.findData('read-only'))
        self.assertIn('拒绝主站写入', self.window.editor.data_note.text())
        self.assertIn('拒绝主站写入', self.window.monitor.hint.text())

    def test_role_period_labels_and_help_retranslate_without_changing_values(self):
        for mode, english, chinese in (('rtu-master', 'Communication polling period (ms)', '通信轮询周期（ms）'),
                                        ('rtu-slave', 'Input register update period (ms)', '输入区数据更新周期（ms）')):
            self.window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode=mode,
                serial=dict(port='TEST', baud=1200), period_ms=300 if mode == 'rtu-master' else 50,
                timeout_ms=200 if mode == 'rtu-master' else 80, points=[dict(area='input', operation='read-input')])) )
            before = self.window.editor.capture_draft()
            for language, caption in (('en', english), ('zh_CN', chinese)):
                self.app.language_manager._install(language)
                self.window.retranslate_ui()
                field = self.window.fields['period_ms']
                self.assertEqual(field.accessibleName(), caption)
                self.assertEqual(self.window.timing_box.layout().labelForField(field).text(), caption)
                self.assertEqual(self.window.editor.capture_draft(), before)
                if mode != 'rtu-master':
                    self.assertIn('holding registers' if language == 'en' else '保持区', field.toolTip())
