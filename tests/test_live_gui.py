"""离屏验证实时表、权限操作以及曲线隐藏后的定时器停绘。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class LiveGuiTests(unittest.TestCase):
    def test_backend_samples_render_and_expire_when_clock_epochs_differ(self):
        script = '''
from unittest.mock import patch
from PySide6.QtGui import QImage, QPainter
from modbus_dev.ui.app import create_application
from modbus_dev.application.control import Control
from modbus_dev.application.live import LiveValues
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event
app = create_application(language="zh_CN")
# Windows Python can give monotonic and perf_counter different epochs.
with patch('time.monotonic') as uptime, patch('time.perf_counter') as communication_clock:
    from modbus_dev.ui import realtime_panel
    dots = []
    class RecordingPainter(QPainter):
        def drawEllipse(self, rect):
            dots.append(rect)
            return super().drawEllipse(rect)
    realtime_panel.QPainter = RecordingPainter
    for offset in (-12.5, 12.5):
        uptime.return_value = 100. + offset
        communication_clock.return_value = 100.
        panel = realtime_panel.RealtimePanel()
        config = TaskConfig.from_mapping(dict(mode='rtu-master', access_mode='read-only',
            serial=dict(port='TEST'), duration_s=0,
            points=[dict(operation='read-holding', data_type='uint16')]))
        panel.configure(config)
        live = LiveValues(config)
        sampled = Control().now()
        live.consume(Event('register_values', 'RX', 'DEBUG', details=dict(area='holding', direction='rx',
            address=0, registers=[65535], function=3, sampled_s=sampled, quality='received')))
        panel.ingest(live.snapshot(sampled, 'running', drain=True))
        panel.refresh_values()
        assert panel.table.item(0, 4).text() == '65535'
        assert panel.table.item(0, 7).text() == '已接收', (offset, panel.table.item(0, 7).text())
        panel.plot.setFixedSize(600, 220)
        image = QImage(panel.plot.size(), QImage.Format_ARGB32)
        dots.clear()
        panel.plot.render(image)
        assert any(image.rect().contains(rect.center().toPoint()) for rect in dots), (
            'Fresh received sample was not visible', offset, panel.clock(), sampled, dots)
        assert abs(panel.clock() - sampled) < .001
        # Age and stale detection must use the same epoch as the plotted sample.
        communication_clock.return_value = 101.5
        uptime.return_value = 101.5 + offset
        panel.refresh_values()
        assert '过期' in panel.table.item(0, 7).text()
        panel.history.advance(panel.clock(), 'running', 1.)
        assert panel.history.series['holding:0:rx'][-1][1] is None
        panel.set_state('stopped')
        panel.close()
'''
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trend_axis_labels_fit_and_preserve_integer_values_at_common_scales(self):
        script = '''
import os
from pathlib import Path
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter
from modbus_dev.gui import create_application
from modbus_dev.domain.config import TaskConfig
from modbus_dev.ui import realtime_panel
app = create_application(language="zh_CN")
fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
if (fonts / 'segoeui.ttf').is_file():
    QFontDatabase.addApplicationFont(str(fonts / 'segoeui.ttf'))
drawn = []
class RecordingPainter(QPainter):
    def drawText(self, rect, flags, text):
        drawn.append((text, self.boundingRect(rect, flags, text)))
        return super().drawText(rect, flags, text)
realtime_panel.QPainter = RecordingPainter
panel = realtime_panel.RealtimePanel(clock=lambda: 100.)
panel.configure(TaskConfig.from_mapping(dict(mode='rtu-master', access_mode='read-only',
    serial=dict(port='TEST'), points=[dict(operation='read-holding', data_type='uint16')], duration_s=0)))
plot = panel.plot
for pixels in (14, 22):
    font = QFont('Segoe UI')
    font.setPixelSize(pixels)
    plot.setFont(font)
    plot.setStyleSheet(f'font-size: {pixels}px;')
    for width in (430, 900):
        plot.setFixedSize(width, 220)
        for value in (65535, 4294967295, -2147483648, 3.4e38, 1.2e-20):
            panel.history.clear()
            panel.ingest(dict(state='running', stale_after_s=1., samples=[
                [1, 'holding:0:rx', 100., value, 'received']]))
            drawn.clear()
            image = QImage(plot.size(), QImage.Format_ARGB32)
            image.fill(0)
            plot.render(image)
            assert len(drawn) == 11, drawn
            viewport = QRectF(plot.rect())
            for text, ink in drawn:
                assert viewport.contains(ink), (pixels, width, value, text, ink, viewport)
            if value == 65535:
                assert drawn[2][0] == '65535', drawn[:5]
                assert all('e' not in text.lower() for text, _ in drawn[:5]), drawn[:5]
panel.set_state('stopped')
panel.close()
'''
        for scale in ("1", "1.5", "2"):
            with self.subTest(scale=scale):
                environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_valid_ack_metrics_and_tx_history_are_independent_of_correlation(self):
        script = '''
import time
from modbus_dev.application.live import LiveValues
from modbus_dev.domain.events import Event
from modbus_dev.gui import Workbench, create_application
from modbus_dev.domain.config import TaskConfig
app = create_application(language="zh_CN")
window = Workbench(remember_settings=False)
config = TaskConfig.from_mapping(dict(mode='rtu-master', serial=dict(port='TEST', baud=115200), duration_s=0))
window.apply_config(config)
window.show()
app.processEvents()
window.counter_mode = 'rtu-master'
window.show_counters(dict(write_attempts=100, ack=12, ack_ambiguous=87, timeout=1, read=1, read_ambiguous=2))
assert [x.text() for x in window.metric_values] == ['100', '99', '3', '0']
assert window.metric_labels[1].text() == '有效 ACK'
assert '包含关联不确定' in window.metric_values[1].toolTip()
assert window.last_counters['ack'] == 12
assert window.last_counters['ack_ambiguous'] == 87
panel = window.live_panel
now = time.perf_counter()
live = LiveValues(config)
live.consume(Event('register_values', 'TX', 'DEBUG', details=dict(area='holding', direction='tx',
    address=0, registers=[0x3fa0, 0], function=16, sampled_s=now, quality='submitted')))
live.consume(Event('register_confirmation', 'ACK', 'DEBUG', details=dict(area='holding', direction='tx',
    address=0, count=2, sampled_s=now, quality='ambiguous')))
snapshot = live.snapshot(now, 'running', drain=True)
snapshot['ack_correlation_uncertain'] = True
panel.ingest(snapshot)
panel.refresh_values()
assert panel.table.item(0, 2).text() == '1.25'
assert '已提交发送' in panel.table.item(0, 7).text()
assert '关联不确定' in panel.table.item(0, 2).toolTip()
assert list(panel.history.series['holding:0:tx']) == [(now, 1.25)]
assert panel.correlation_note.isVisible()
# A later timeout changes the confirmation text without erasing or refreshing the sent value.
live.consume(Event('register_confirmation', 'timeout', 'DEBUG', details=dict(area='holding', direction='tx',
    address=0, count=2, sampled_s=now, quality='timeout')))
panel.ingest(live.snapshot(now + .1, 'running', drain=True))
panel.refresh_values()
assert '应答超时' in panel.table.item(0, 7).text()
assert list(panel.history.series['holding:0:tx']) == [(now, 1.25)]
assert panel.correlation_note.isVisible()
panel.set_state('stopped')
assert not panel.value_timer.isActive()
assert '停止' in panel.table.item(0, 7).text()
panel.configure(config)
assert not panel.correlation_note.isVisible()
assert not panel.correlation_uncertain
window.close()
'''
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_continuous_status_updates_do_not_starve_curve_timer(self):
        script = '''
import time
from PySide6.QtCore import QEventLoop, QTimer
from modbus_dev.gui import Workbench, create_application
from modbus_dev.domain.config import TaskConfig
app = create_application(language="zh_CN")
window = Workbench(remember_settings=False)
window.apply_config(TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-slave',
    serial=dict(port='TEST', baud=115200), duration_s=0)))
window.show()
window.monitor_tabs.setCurrentIndex(1)
app.processEvents()
panel = window.live_panel
sequence = 0
draws = []
panel.plot.timer.timeout.connect(lambda: draws.append(time.monotonic()))
def publish():
    global sequence
    now = time.perf_counter()
    samples = []
    for at in (now - .1, now):
        sequence += 1
        samples.append([sequence, 'input:0:tx', at, at % 1, 'submitted'])
    row = dict(key='input:0:tx', point=0, name='压力', area='input', address=0,
        direction='tx', value=now % 1, registers=[0, 0], function=4,
        quality='submitted', sampled_s=now)
    panel.ingest(dict(state='running', sampled_s=now, stale_after_s=1., rows=[row], samples=samples))
publish()
updates = QTimer()
updates.setInterval(200)
updates.timeout.connect(publish)
updates.start()
loop = QEventLoop()
QTimer.singleShot(2200, loop.quit)
loop.exec()
updates.stop()
assert panel.table.rowCount() == 1
assert panel.selected('input:0:tx')
points = panel.history.series.get('input:0:tx', ())
assert sum(value is not None for _, value in points) == sequence, list(points)
assert 3 <= len(draws) <= 6, draws
assert sequence >= 18, sequence
# 隐藏页只停止绘制，状态批次仍完整入缓冲，数值快照覆盖也不能丢点。
window.monitor_tabs.setCurrentIndex(0)
app.processEvents()
assert not panel.plot.timer.isActive()
publish()
publish()
assert sum(value is not None for _, value in panel.history.series['input:0:tx']) == sequence
window.close()
'''
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_values_permissions_visibility_and_stop_state(self):
        script = '''
import time
from modbus_dev.gui import Workbench, create_application
from modbus_dev.domain.config import TaskConfig
app = create_application(language="zh_CN")
window = Workbench(remember_settings=False)
window.resize(1280, 900)
window.apply_config(TaskConfig.from_mapping(dict(mode='rtu-master', access_mode='read-only',
    points=[dict(operation='read-input')], serial=dict(port='TEST', baud=115200), duration_s=0)))
window.show()
app.processEvents()
assert window.configuration().point_usage(window.configuration().points[0]).operation == 'read-input'
operation = window.point_editors[0].fields['operation']
assert not operation.model().item(operation.findData('write-single')).isEnabled()
panel = window.live_panel
now = time.perf_counter()
row = dict(key='input:0:rx', point=0, name='压力', area='input', address=0,
    direction='rx', value=1.25, registers=[0x3fa0, 0], function=4,
    quality='received', sampled_s=now)
panel.ingest(dict(state='running', sampled_s=now, stale_after_s=1., rows=[row]))
assert panel.value_timer.interval() == 200
assert panel.plot.timer.interval() == 500
panel.refresh_values()
assert panel.table.item(0, 2).text() == '—'
assert panel.table.item(0, 4).text() == '1.25'
assert not panel.plot.timer.isActive()
window.monitor_tabs.setCurrentIndex(1)
app.processEvents()
assert panel.plot.timer.isActive()
panel.plot.tick()
assert len(panel.history.series['input:0:rx']) == 1
panel.selectors['input:0:rx'].setChecked(False)
assert not panel.selected('input:0:rx')
row['quality'] = 'ambiguous'
panel.ingest(dict(state='running', sampled_s=now, stale_after_s=1., rows=[row]))
panel.refresh_values()
assert panel.table.item(0, 4).text() == '1.25'
assert panel.table.item(0, 7).text() == '应答关联不确定'
assert '应答关联不确定' in panel.table.item(0, 4).toolTip()
panel.plot.tick()
assert panel.history.series['input:0:rx'][-1][1] is None
window.monitor_tabs.setCurrentIndex(0)
app.processEvents()
assert not panel.plot.timer.isActive()
panel.set_state('stopped')
assert not panel.value_timer.isActive()
assert '停止' in panel.table.item(0, 7).text()
assert panel.table.item(0, 4).text() == '1.25'
window.close()
'''
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
