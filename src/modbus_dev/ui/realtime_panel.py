"""轻量实时数值与趋势组件：数值 5Hz，曲线 2Hz，隐藏时停止曲线定时器。"""
from .i18n import tr, bind, table_item, retranslate_tree
from datetime import datetime, timedelta
import time

from PySide6.QtCore import QTimer, Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QHeaderView, QTableWidget, QVBoxLayout, QWidget

from ..domain.live_policy import CURVE_REFRESH_HZ, CURVE_WINDOW_S, VALUE_REFRESH_HZ
from ..domain.trend import TrendHistory
from .theme import COLORS
from .widgets import CheckBox, label

SERIES_COLORS = ("#4269C7", "#338164", "#A87426", "#8455A6", "#248797", "#BD586B", "#787430", "#666CB0")
# 数值表与曲线图例共用区域名称，避免同地址的两区数值被误认为同一份数据。
AREA_NAMES = {"holding": tr('Holding (FC03)'), "input": tr('Input (FC04)')}
QUALITY_NAMES = {"submitted": tr('Submitted for transmission'), "received": tr('Received'), "confirmed": tr('Write confirmed'),
                 "partial": tr('Single register / raw value'), "ambiguous": tr('Reply correlation uncertain'),
                 "timeout": tr('Reply timeout'), "exception": tr('Exception reply'), "bad_reply": tr('Invalid reply'),
                 "readback-mismatch": tr('Readback mismatch'), "nonfinite": tr('Non-finite float32 / raw value')}
CONFIRMATION_NAMES = {"confirmed": tr('Valid ACK received'), "ambiguous": tr('Valid ACK received; correlation uncertain'),
                      "timeout": tr('Reply timeout'), "exception": tr('Exception reply'), "bad_reply": tr('Invalid / incomplete reply'),
                      "late_reply": tr('Late reply'), "outcome-unknown": tr('Write outcome unknown'),
                      "connection_lost": tr('TCP connection lost')}


class TrendPlot(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMinimumHeight(180)
        bind(self, 'setAccessibleName', tr('Live trends for the last ten seconds; TX solid, RX dashed'))
        self.timer = QTimer(self)
        self.timer.setInterval(round(1000 / CURVE_REFRESH_HZ))
        self.timer.timeout.connect(self.tick)

    def sync_timer(self):
        if self.isVisible() and self.owner.running:
            # 状态快照比绘图更频繁；重复启动会一直推迟 timeout。
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_timer()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def tick(self):
        # 数据到达时已入缓冲；此处只清理时间窗口并按 2Hz 重绘。
        if self.isVisible() and self.owner.running:
            snapshot = self.owner.snapshot
            self.owner.history.advance(self.owner.clock(), snapshot.get("state", "idle"),
                                       snapshot.get("stale_after_s", 1.))
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["panel"]))
        painter.setPen(QPen(QColor(COLORS["line"]), 1))
        now = self.owner.clock()
        visible = {key: points for key, points in self.owner.history.series.items()
                   if self.owner.selected(key)}
        values = [value for points in visible.values() for at, value in points
                  if now - CURVE_WINDOW_S <= at <= now and value is not None]
        low, high = (min(values), max(values)) if values else (-1., 1.)
        padding = max((high - low) * .1, abs(high) * .02, .001)
        low, high = low - padding, high + padding
        # 保留常见 16/32 位整数的十进制刻度；极大/极小值仍可使用科学计数法。
        labels = [f"{high - (high - low) * index / 4:.10g}" for index in range(5)]
        metrics = painter.fontMetrics()
        label_width = max(48, max(max(metrics.horizontalAdvance(text), metrics.boundingRect(text).width())
                                  for text in labels) + 2)
        label_height = metrics.height() + 4
        # 按实际字体与标签宽度预留空间，避免负号、指数或高 DPI 字体被左边界裁切。
        left = 6 + label_width + 8
        top = max(16, label_height / 2 + 2)
        bottom = label_height + 12
        bounds = QRectF(left, top, max(10, self.width() - left - 20),
                        max(10, self.height() - top - bottom))
        for index, text in enumerate(labels):
            y = bounds.top() + bounds.height() * index / 4
            painter.setPen(QPen(QColor(COLORS["line"]), 1))
            painter.drawLine(QPointF(bounds.left(), y), QPointF(bounds.right(), y))
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(6, y - label_height / 2, label_width, label_height),
                             Qt.AlignRight | Qt.AlignVCenter, text)
        for index in range(6):
            x = bounds.left() + bounds.width() * index / 5
            text = f"{-(CURVE_WINDOW_S - index * 2):g}s"
            width = metrics.horizontalAdvance(text) + 4
            painter.drawText(QRectF(x - width / 2, bounds.bottom() + 8, width, label_height),
                             Qt.AlignCenter, text)
        painter.save()
        painter.setClipRect(bounds)
        for key, points in visible.items():
            color, style = self.owner.series_style(key)
            painter.setPen(QPen(QColor(color), 2, style))
            path, connected = QPainterPath(), False
            for at, value in points:
                if at < now - CURVE_WINDOW_S or value is None:
                    connected = False
                    continue
                x = bounds.right() - (now - at) / CURVE_WINDOW_S * bounds.width()
                y = bounds.bottom() - (value - low) / (high - low) * bounds.height()
                if connected:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                connected = True
                painter.drawEllipse(QRectF(x - 1.5, y - 1.5, 3, 3))
            painter.drawPath(path)
        painter.restore()
        if not values:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(bounds, Qt.AlignCenter, tr('Waiting for valid communication values · Planned source values do not replace actual TX/RX'))


class RealtimePanel(QWidget):
    def __init__(self, clock=None):
        super().__init__()
        # 必须与通信层 Control.now() 使用同一时间基准；Windows 上两种单调时钟可能有偏差。
        self.clock = time.perf_counter if clock is None else clock
        self.config = None
        self.snapshot = {}
        self.pending = None
        self.running = False
        self.correlation_uncertain = False
        self.history = TrendHistory()
        self.selectors = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(label(tr('Live values'), "section"))
        heading.addStretch()
        refresh_note = label(tr('Display: values {p0} Hz · Curves {p1} Hz · Last {p2} s').format(p0=VALUE_REFRESH_HZ, p1=CURVE_REFRESH_HZ, p2=CURVE_WINDOW_S), "caption")
        refresh_note.setWordWrap(True)
        refresh_note.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bind(refresh_note, 'setToolTip', tr('The table shows the latest values. Curves retain actual communication samples independently of the display refresh rate.'))
        heading.addWidget(refresh_note, 1)
        layout.addLayout(heading)
        self.correlation_note = label(tr('Reply correlation uncertainty occurred in this session. Later valid replies still count; see the logs for details.'), "caption")
        self.correlation_note.setObjectName("correlationHistory")
        self.correlation_note.setWordWrap(True)
        self.correlation_note.hide()
        layout.addWidget(self.correlation_note)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("liveValues")
        bind(self.table, 'setAccessibleName', tr('Actual transmitted and received values'))
        bind(self.table, 'setHorizontalHeaderLabels', [tr('Point / area'), tr('Address'), tr('TX value'), tr('RX value'), tr('Last update'), tr('Status')])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(84)
        self.table.setMaximumHeight(168)
        layout.addWidget(self.table)
        self.value_timer = QTimer(self)
        self.value_timer.setInterval(round(1000 / VALUE_REFRESH_HZ))
        self.value_timer.timeout.connect(self.refresh_values)
        self.curve_page = QWidget()
        curve_layout = QVBoxLayout(self.curve_page)
        curve_layout.setContentsMargins(0, 8, 0, 0)
        self.legend = QGridLayout()
        curve_layout.addLayout(self.legend)
        self.plot = TrendPlot(self)
        curve_layout.addWidget(self.plot, 1)
        self.curve_note = label("", "caption")
        self.curve_note.setWordWrap(True)
        curve_layout.addWidget(self.curve_note)
        self.update_curve_note()

    def update_curve_note(self):
        text = tr('TX solid: submitted to driver · RX dashed: decoded values · Actual communication samples, redrawn at {p0} Hz').format(p0=CURVE_REFRESH_HZ)
        if self.history.dropped:
            text += tr(' · Buffer dropped {p0} records').format(p0=self.history.dropped)
        bind(self.curve_note, 'setText', text)

    def configure(self, config):
        self.config = config
        self.snapshot, self.pending = {}, None
        self.correlation_uncertain = False
        self.correlation_note.hide()
        self.history.clear()
        self.update_curve_note()
        self.plot.setVisible(True)
        self.table.setRowCount(0)
        for checkbox in self.selectors.values():
            self.legend.removeWidget(checkbox)
            checkbox.hide()
            checkbox.deleteLater()
        self.selectors.clear()
        for index, point in enumerate(config.points):
            if not point.enabled:
                continue
            area = config.point_area(point)
            usage = config.point_usage(point)
            directions = (("tx", "rx") if usage.readback else ("tx",)) if usage.writing else ("rx",)
            if not usage.master:
                directions = ("tx", "rx") if area == "holding" and config.access_mode == "read-write" else ("tx",)
            for direction in directions:
                key = f"{area}:{index}:{direction}"
                checkbox = CheckBox(tr("{name} · {area} · {direction}").format(
                    name=point.name or tr("Point {p0}").format(p0=index + 1),
                    area=AREA_NAMES[area], direction=direction.upper()))
                checkbox.setChecked(True)
                color, _ = self.series_style(key)
                checkbox.setStyleSheet(f"color: {color};")
                checkbox.toggled.connect(lambda _: self.plot.update())
                self.legend.addWidget(checkbox, len(self.selectors) // 2, len(self.selectors) % 2)
                self.selectors[key] = checkbox
        self.set_state("idle")

    def retranslate_ui(self):
        retranslate_tree(self)
        retranslate_tree(self.curve_page)
        self.update_curve_note()
        self.refresh_values()
        self.plot.update()

    def series_style(self, key):
        area, index, direction = key.split(":")
        color = SERIES_COLORS[int(index) % len(SERIES_COLORS)]
        return color, Qt.SolidLine if direction == "tx" else Qt.DashLine

    def selected(self, key):
        return key in self.selectors and self.selectors[key].isEnabled() and self.selectors[key].isChecked()

    def ingest(self, snapshot):
        # 数值表可以覆盖最新快照；曲线必须先接收其中的所有通信点。
        self.history.sample(snapshot, self.clock())
        self.update_curve_note()
        self.pending = snapshot
        if snapshot.get("ack_correlation_uncertain") and not self.correlation_uncertain:
            self.correlation_uncertain = True
            self.correlation_note.show()
        self.set_state(snapshot.get("state", "idle"))

    def set_state(self, state):
        self.running = state in ("starting", "running", "reconnecting")
        if self.snapshot:
            self.snapshot["state"] = state
        if self.running:
            if not self.value_timer.isActive():
                self.value_timer.start()
        else:
            self.value_timer.stop()
            self.refresh_values()
            self.history.advance(self.clock(), state, self.snapshot.get("stale_after_s", 1.))
            self.plot.update()
        self.plot.sync_timer()

    def refresh_values(self):
        if self.pending is not None:
            self.snapshot, self.pending = self.pending, None
        grouped = {}
        for row in self.snapshot.get("rows", []):
            grouped.setdefault((row["point"], row["area"]), {})[row["direction"]] = row
        self.table.setRowCount(len(grouped))
        self.table.setFixedHeight(max(84, self.table.horizontalHeader().height() + min(4, len(grouped)) * 30 + 4))
        now = self.clock()
        for index, (_, directions) in enumerate(sorted(grouped.items())):
            last = max(directions.values(), key=lambda row: row["sampled_s"])
            age = max(0, now - last["sampled_s"])
            quality = self.describe_row(last)
            stale = self.snapshot.get("state") != "running" or age > self.snapshot.get("stale_after_s", 1.)
            if stale:
                quality = tr('Stopped / stale') if not self.running else tr('Stale / waiting for data')
                # 失效只说明数值不再新鲜，不能抹去可能已执行的写入风险。
                if last.get("confirmation") == "outcome-unknown":
                    quality += " · " + tr('Write outcome unknown')
            name = last["name"]
            if self.config is not None:
                name = self.config.points[last["point"]].name or tr("Point {p0}").format(p0=last["point"] + 1)
            values = [tr("{name} · {area}").format(name=name, area=AREA_NAMES[last["area"]]), str(last["address"]),
                      self.format_value(directions.get("tx")), self.format_value(directions.get("rx")),
                      (datetime.now() - timedelta(seconds=age)).strftime("%H:%M:%S.%f")[:-3], quality]
            for column, value in enumerate(values):
                item = table_item(value)
                if column in (2, 3):
                    row = directions.get("tx" if column == 2 else "rx")
                    if row:
                        bind(item, 'setToolTip', tr('FC{p0:02d} · {p1}\nRaw registers: {p2}\nAge: {p3:.3f} s').format(p0=row['function'], p1=self.describe_row(row), p2=' '.join((f'{word:04X}' for word in row['registers'])), p3=max(0, now - row['sampled_s']))
                             + "\n" + row.get("data_type", "float32") + " · " + row.get("byte_order", ""))
                        if now - row["sampled_s"] > self.snapshot.get("stale_after_s", 1.) or stale:
                            item.setForeground(QColor(COLORS["muted"]))
                self.table.setItem(index, column, item)

    @staticmethod
    def describe_row(row):
        text = QUALITY_NAMES.get(row["quality"], row["quality"])
        confirmation = row.get("confirmation")
        if confirmation is not None:
            text += " · " + CONFIRMATION_NAMES.get(confirmation, confirmation)
        return text

    @staticmethod
    def format_value(row):
        if row is None:
            return "—"
        if row["value"] is None:
            return " ".join(f"0x{word:04X}" for word in row["registers"])
        # 整数按十进制完整显示，避免 uint32 被七位有效数字格式舍入。
        if isinstance(row["value"], int):
            return str(row["value"])
        return f"{row['value']:.7g}"
