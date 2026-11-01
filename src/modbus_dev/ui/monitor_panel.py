"""展示会话控制、实际通信值、日志和统计，模式说明按逐点模型生成。"""
from .i18n import tr, bind, make_button, add_tab, table_item, retranslate_tree
import json
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit, QScrollArea, QTableWidget, QTabWidget, QVBoxLayout, QWidget
from ..domain.transactions import valid_reply_counts
from ..domain.usage import ParameterUsage
from ..runtime import default_output_directory
from .realtime_panel import RealtimePanel
from .theme import METRICS, ui_font
from .widgets import card, label, refresh_style
from .texts import CHOICE_LABELS, COUNTER_LABELS, METRIC_HELP, SCHEDULE_LATE_HELP


class MonitorPanel(QScrollArea):
    startRequested = Signal()
    stopRequested = Signal()
    openRequested = Signal()
    browseRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "tcp-slave"
        self.counter_mode = None
        self.last_counters = {}
        self.preview_dropped = 0
        self.result = None
        self._build_ui()


    def _build_ui(self):
        self.runtime_scroll = self
        self.runtime_scroll.setWidgetResizable(True)
        self.runtime_scroll.setMinimumWidth(430)
        self.runtime_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right = QWidget()
        right.setObjectName("runtimeContent")
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.page_spacing)
        control, controls = card()
        heading = QHBoxLayout()
        self.connection_title = label(tr('TCP slave'), "section")
        heading.addWidget(self.connection_title)
        heading.addStretch()
        self.status = label(tr('Ready · Not started'))
        self.status.setObjectName("sessionStatus")
        heading.addWidget(self.status)
        controls.addLayout(heading)
        self.connection_summary = label("", "caption")
        self.connection_summary.setWordWrap(True)
        controls.addWidget(self.connection_summary)
        self.hint = label("")
        self.hint.setObjectName("sessionHint")
        self.hint.setWordWrap(True)
        controls.addWidget(self.hint)
        actions = QHBoxLayout()
        actions.setSpacing(METRICS.spacing)
        self.start_button = make_button(tr('Start session'))
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self.startRequested.emit)
        self.stop_button = make_button(tr('Stop and save'))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopRequested.emit)
        self.open_button = make_button(tr('Open results'))
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.openRequested.emit)
        for button in (self.start_button, self.stop_button, self.open_button):
            actions.addWidget(button, 1)
        controls.addLayout(actions)
        location = QWidget()
        location_layout = QVBoxLayout(location)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(6)
        location_layout.addWidget(label(tr('Results parent directory'), "caption"))
        location_row = QHBoxLayout()
        location_row.setSpacing(METRICS.spacing)
        self.output = QLineEdit(str(default_output_directory()))
        self.output.setObjectName("output")
        bind(self.output, 'setAccessibleName', tr('Results parent directory'))
        location_row.addWidget(self.output, 1)
        browse = make_button(tr('Browse…'))
        browse.clicked.connect(self.browseRequested.emit)
        location_row.addWidget(browse)
        location_layout.addLayout(location_row)
        self.output_box = location
        controls.addWidget(location)
        layout.addWidget(control)
        metrics, metrics_layout = card()
        row = QHBoxLayout()
        row.setSpacing(METRICS.section_spacing)
        self.metric_labels, self.metric_values = [], []
        for _ in range(4):
            column = QVBoxLayout()
            column.setSpacing(3)
            value, caption = label("—", "metric"), label("", "caption")
            self.metric_values.append(value)
            self.metric_labels.append(caption)
            column.addWidget(value)
            column.addWidget(caption)
            row.addLayout(column, 1)
        metrics_layout.addLayout(row)
        layout.addWidget(metrics)
        monitor, monitor_layout = card()
        self.live_panel = RealtimePanel()
        monitor_layout.addWidget(self.live_panel)
        self.monitor_tabs = QTabWidget()
        self.stats = QTableWidget(0, 2)
        bind(self.stats, 'setHorizontalHeaderLabels', [tr('Metric'), tr('Value')])
        self.stats.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.stats.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.stats.verticalHeader().hide()
        self.stats.verticalHeader().setDefaultSectionSize(36)
        self.stats.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats.setSelectionBehavior(QTableWidget.SelectRows)
        self.stats.setShowGrid(False)
        self.logs = QPlainTextEdit()
        self.logs.setFont(ui_font(monospace=True))
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        self.logs.setMinimumHeight(140)
        bind(self.logs, 'setPlaceholderText', tr('Session logs appear here after starting.\nThe UI retains the latest 2000 lines; complete logs are saved to the results directory.'))
        add_tab(self.monitor_tabs, self.logs, tr('Session log'))
        add_tab(self.monitor_tabs, self.live_panel.curve_page, tr('Trends'))
        add_tab(self.monitor_tabs, self.stats, tr('Statistics'))
        monitor_layout.addWidget(self.monitor_tabs, 1)
        self.recording = label(tr('Log evidence: not started'), "caption")
        self.recording.setWordWrap(True)
        monitor_layout.addWidget(self.recording)
        self.path_label = label(tr('Each session creates a separate results directory.'), "caption")
        self.path_label.setWordWrap(True)
        monitor_layout.addWidget(self.path_label)
        layout.addWidget(monitor, 1)
        self.runtime_scroll.setWidget(right)


    def update_connection_summary(self, configuration):
        mode = configuration.text("mode")
        self.mode = mode
        bind(self.connection_title, 'setText', CHOICE_LABELS.get(mode, mode))
        endpoint = ("{}:{}".format(configuration.text("host"), configuration.text("port")) if ParameterUsage(mode).tcp else
                    "{}  ·  {} baud".format(configuration.text("serial.port") or tr("No serial port selected"),
                                             configuration.text("serial.baud")))
        bind(self.connection_summary, 'setText', tr('{p0}  ·  Unit {p1}').format(p0=endpoint, p1=configuration.text('unit')))


    def set_status(self, text, tone="idle"):
        bind(self.status, 'setText', text)
        self.status.setProperty("tone", tone)
        refresh_style(self.status)

    def retranslate_ui(self):
        retranslate_tree(self)
        self.show_counters(self.last_counters)
        self.live_panel.retranslate_ui()


    def display(self, packet):
        if packet.get("live"):
            self.live_panel.ingest(packet["live"])
        dropped = packet.get("preview_dropped", 0)
        if dropped > self.preview_dropped:
            self.logs.appendPlainText(tr('The UI log preview has dropped {p0} records; use the results directory for complete evidence.').format(p0=dropped))
            self.preview_dropped = dropped
        for event in packet.get("events", [])[-100:]:
            self.logs.appendPlainText(f"[{event['level']}] {event['code']} · {event['message']}")
        if packet["type"] == "result":
            self.result = packet["result"]
            self.show_counters(self.result.get("counters", {}))
            return
        states = {"idle": tr('Preparing'), "starting": tr('Starting'), "running": tr('Running'), "reconnecting": tr('Waiting to reconnect'),
                  "stopping": tr('Stopping'), "stopped": tr('Stopped'), "failed": tr('Failed')}
        state = packet["state"]
        tone = {"running": "active", "starting": "active", "reconnecting": "warning",
                "stopping": "warning", "failed": "error"}.get(state, "idle")
        self.set_status(states.get(state, state), tone)
        counters = packet.get("counters", {})
        self.show_counters(counters)
        recording = packet.get("recording", {})
        self.show_recording(recording)


    def show_recording(self, recording):
        """历史淘汰、队列丢失和界面预览丢失分别显示，不能把滚动历史当完整会话。"""
        segments = recording.get("segments", {})
        retained = segments.get("retained_files", 0)
        deleted = segments.get("deleted_files", 0)
        state = tr('Complete') if recording.get("evidence_complete") else tr('Not ready / incomplete')
        if recording.get("retention_truncated"):
            state = tr('Incomplete · {p0} older segments removed, {p1} newest retained').format(p0=deleted, p1=retained)
        bind(self.recording, 'setText', tr('Log evidence: {p0}  ·  Queue dropped {p1}  ·  UI preview dropped {p2}').format(p0=state, p1=recording.get('dropped', 0), p2=self.preview_dropped))


    def show_counters(self, counters):
        self.last_counters = dict(counters)
        self.stats.setRowCount(len(counters))
        for row, (key, value) in enumerate(sorted(counters.items())):
            item = table_item(COUNTER_LABELS.get(key, key))
            bind(item, 'setToolTip', f'{key}\n{SCHEDULE_LATE_HELP}' if key == 'schedule_late' else key)
            self.stats.setItem(row, 0, item)
            self.stats.setItem(row, 1, table_item(str(value)))
        self.update_metrics()


    def update_metrics(self):
        mode = self.counter_mode or self.mode
        keys = (("write_attempts", "valid_ack", "valid_read", "schedule_late") if ParameterUsage(mode).master else
                ("source_updates", "slave_request", "rx", "tx"))
        totals = valid_reply_counts(self.last_counters)
        for key, caption, value in zip(keys, self.metric_labels, self.metric_values):
            bind(caption, 'setText', COUNTER_LABELS[key])
            help_text = METRIC_HELP.get(key, "")
            bind(caption, 'setToolTip', f'{key}\n{help_text}' if help_text else key)
            bind(value, 'setText', str(totals.get(key, self.last_counters.get(key, 0))) if self.last_counters else '—')
            tooltip = f"{COUNTER_LABELS[key]} ({key})"
            bind(value, 'setToolTip', f'{tooltip}\n{help_text}' if help_text else tooltip)


    def set_running(self, running):
        self.output_box.setEnabled(not running)
        self.start_button.setEnabled(not running)
        bind(self.start_button, 'setText', tr('Session active') if running else tr('Start session'))
        self.stop_button.setEnabled(running)


    def update_hint(self, usage, readonly):
        mode = usage.mode
        if usage.master:
            text = tr('Each point runs its configured read or write operation; optional write readback uses FC03. All points share one cycle.')
        else:
            text = tr('Holding stores values for master reads and, when read/write access is enabled, retains master writes. Input values are generated locally for master reads only.')
            if readonly:
                text += tr('\nCurrently read-only: master writes are rejected.')
        bind(self.hint, 'setText', text)
        self.hint.setProperty("writing", usage.writing)
        refresh_style(self.hint)
