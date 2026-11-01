"""会话配置与逐点编码编辑，共用领域校验并保留无关参数草稿。"""
from .i18n import tr, bind, make_button, group_box, add_item, add_tab, retranslate_tree
import json
from PySide6.QtCore import QSignalBlocker, Signal, Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget
from ..application.session import check_capabilities
from ..domain.config import MAX_POINTS, MIN_PERIOD_MS, MAX_PERIOD_MS, PointConfig, TaskConfig, point_conflicts
from ..domain.wire import cycle_budget
from .errors import format_error
from ..domain.usage import ParameterUsage
from .point_editor import PointEditor
from .point_tabs import PointTabs
from .theme import METRICS
from .widgets import CheckBox, ComboBox, SerialPortComboBox, label, refresh_style
from .texts import CHOICE_LABELS, SCHEDULE_LATE_HELP, SLAVE_AREAS_HELP, INPUT_PERIOD_HELP, MASTER_PERIOD_HELP, TCP_MASTER_PERIOD_HELP
from .form_state import FormState, programmatic_changes
from .defaults import default_task_config


class ConfigurationPanel(QFrame):
    changed = Signal()
    edited = Signal()
    validityChanged = Signal(bool)
    loadRequested = Signal()
    saveRequested = Signal()
    validateRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fields = {}
        self._busy = False
        self._applying = False
        self._last_mode = None
        self.configuration_valid = True
        self.base_config = default_task_config().to_mapping()
        self._build_ui()
        self.fields["mode"].currentIndexChanged.connect(self.mode_changed)
        self.fields["access_mode"].currentIndexChanged.connect(self.operation_changed)
        for field in self.fields.values():
            signal = field.currentTextChanged if isinstance(field, QComboBox) else field.textChanged
            signal.connect(lambda *_: self.changed.emit())
        draft_fields = dict(self.fields, **{"serial.rts_toggle": self.rts})
        for index, editor in enumerate(self.point_editors):
            draft_fields[f"points.{index}.enabled"] = editor.enabled
            draft_fields.update({f"points.{index}.{key}": field for key, field in editor.fields.items()})
        self.form_state = FormState(draft_fields, self.edited.emit)
        self.changed.connect(self.update_points_summary)
        self.changed.connect(self.update_budget)

    def capture_draft(self):
        return dict(schema_version=1, base_config=self.base_config, values=self.form_state.capture())

    @programmatic_changes
    def restore_draft(self, draft):
        if not isinstance(draft, dict) or draft.get("schema_version") != 1:
            raise ValueError("Unsupported saved configuration draft")
        config = TaskConfig.from_mapping(draft["base_config"])
        check_capabilities(config)
        values = draft["values"]
        self.form_state.validate(values)
        self.apply_config(config)
        self.form_state.restore(values)
        self.mode_changed()
        self.update_points_summary()

    def set_busy(self, busy):
        self._busy = busy
        self.setEnabled(not busy)


    def _build_ui(self):
        self.editor = self
        self.setProperty("card", True)
        form_layout = QVBoxLayout(self)
        form_layout.setContentsMargins(*([METRICS.card_padding] * 4))
        form_layout.setSpacing(METRICS.section_spacing)
        self.editor.setMinimumWidth(374)
        form_layout.addWidget(label(tr('Session configuration'), "section"))
        description = label(tr('Configure the connection, shared timing and up to eight independent points.'), "caption")
        description.setWordWrap(True)
        form_layout.addWidget(description)
        self.config_buttons = QWidget()
        config_actions = QHBoxLayout(self.config_buttons)
        config_actions.setContentsMargins(0, 0, 0, 0)
        config_actions.setSpacing(METRICS.spacing)
        for caption, callback in ((tr('Import Config'), self.loadRequested.emit), (tr('Export Config'), self.saveRequested.emit), (tr('Validate'), self.validateRequested.emit)):
            button = make_button(caption)
            button.clicked.connect(callback)
            config_actions.addWidget(button)
        form_layout.addWidget(self.config_buttons)
        self.config_tabs = QTabWidget()
        form_layout.addWidget(self.config_tabs, 1)

        def page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            body.setObjectName("configPage")
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, METRICS.section_spacing, METRICS.spacing, METRICS.spacing)
            layout.setSpacing(METRICS.page_spacing)
            scroll.setWidget(body)
            add_tab(self.config_tabs, scroll, title)
            return layout

        def group(layout, title, rows):
            box = group_box(title)
            form = QFormLayout(box)
            form.setContentsMargins(0, METRICS.section_spacing, 0, 0)
            form.setHorizontalSpacing(METRICS.section_spacing)
            form.setVerticalSpacing(METRICS.spacing)
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            for key, caption, choices in rows:
                widget = (SerialPortComboBox() if key == "serial.port" else
                          ComboBox() if choices else QLineEdit())
                if choices:
                    for value in choices:
                        add_item(widget, CHOICE_LABELS.get(value, value), value)
                if key == "serial.baud":
                    widget.setEditable(True)
                    widget.setInsertPolicy(QComboBox.NoInsert)
                    widget.setCompleter(None)
                    bind(widget.lineEdit(), 'setPlaceholderText', tr('Select or enter a baud rate'))
                    bind(widget, 'setToolTip', tr('Select a common baud rate or enter a custom value. Both the serial adapter and peer must support the same rate.'))
                widget.setObjectName(key)
                bind(widget, 'setAccessibleName', caption)
                self.fields[key] = widget
                form.addRow(label(caption), widget)
            layout.addWidget(box)
            return box

        connection = page(tr('Connection and timing'))
        group(connection, tr('Communication mode'), [("mode", tr('Mode'), ["tcp-slave", "rtu-slave", "rtu-master", "tcp-master"]),
                                      ("policy", tr('Run policy'), ["continuous", "strict"])])
        self.access_box = group(connection, tr('Access permission'), [("access_mode", tr('Access'), ["read-only", "read-write"])])
        self.tcp_box = group(connection, tr('TCP listener'), [("host", tr('Bind address'), None), ("port", tr('Port'), None),
                                                            ("connect_timeout_ms", tr('Connection timeout (ms)'), None)])
        bind(self.fields['connect_timeout_ms'], 'setToolTip', tr('1–60000 ms. Limits one connection attempt, separately from the whole-cycle transaction budget.'))
        self.serial_box = group(connection, tr('Serial connection'), [("serial.port", tr('Serial port'), None),
                                                       ("serial.baud", tr('Baud rate'), ["1200", "2400", "4800", "9600", "19200", "38400",
                                                                              "57600", "115200"]),
                                                       ("serial.parity", tr('Parity'), ["N", "E", "O"]),
                                                       ("serial.stop_bits", tr('Stop bits'), ["1", "2"])])
        self.rts = CheckBox(tr('Driver RTS toggle (Windows only)'))
        self.serial_box.layout().addRow(self.rts)
        group(connection, tr('Device'), [("unit", tr('Unit ID'), None)])
        self.timing_box = group(connection, tr('Session timing'), [("period_ms", tr('Data period (ms)'), None), ("timeout_ms", tr('I/O timeout (ms)'), None),
                                ("duration_s", tr('Duration (s)'), None)])
        bind(self.fields['timeout_ms'], 'setToolTip', tr('Master: whole-cycle transaction budget. Slave: maximum wait for one write, not the polling period.'))
        bind(self.fields['duration_s'], 'setToolTip', tr('0 keeps the session running until stopped manually.'))
        bind(self.fields['duration_s'], 'setPlaceholderText', tr('0 = run continuously'))
        self.master_box = group(connection, tr('Master scheduling'), [
            ("schedule_tolerance_percent", tr('Scheduling tolerance (%)'), None)])
        bind(self.fields['schedule_tolerance_percent'], 'setToolTip', SCHEDULE_LATE_HELP)
        self.readback_note = label(tr('Each point selects its own operation. Writes may enable FC03 readback; FC04 reads a separate register area.'), "caption")
        self.readback_note.setWordWrap(True)
        self.master_box.layout().addRow(self.readback_note)
        self.budget_note = label("", "caption")
        self.budget_note.setObjectName('cycleBudget')
        self.budget_note.setWordWrap(True)
        self.timing_box.layout().addRow(self.budget_note)
        connection.addStretch()

        data = page(tr('Data'))
        self.data_note = label("", "caption")
        self.data_note.setWordWrap(True)
        data.addWidget(self.data_note)
        self.points_status = label("", "caption")
        self.points_status.setWordWrap(True)
        data.addWidget(self.points_status)
        self.data_budget_note = label("", "caption")
        self.data_budget_note.setWordWrap(True)
        data.addWidget(self.data_budget_note)
        # 页签复用全部测点编辑器，槽位数量沿用领域上限，不复制或重置表单数据。
        self.point_editors = [PointEditor(i) for i in range(MAX_POINTS)]
        self.point_tabs = PointTabs(self.point_editors)
        data.addWidget(self.point_tabs)
        for editor in self.point_editors:
            editor.changed.connect(self.changed)
        data.addStretch()

        other = page(tr('Other settings'))
        interface = group(other, tr("Interface"), [])
        self.language = ComboBox()
        self.language.setObjectName("language")
        bind(self.language, "setAccessibleName", tr("Display language"))
        add_item(self.language, tr("Follow system"), "system")
        add_item(self.language, "简体中文", "zh_CN")
        add_item(self.language, "English", "en")
        bind(self.language, "setToolTip", tr("Stop communication before changing settings."))
        interface.layout().addRow(label(tr("Display language")), self.language)
        retention = group(other, tr('Log retention'), [("logging.max_files", tr('Maximum log segments'), None)])
        bind(self.fields['logging.max_files'], 'setToolTip', tr('1 to 10000 files, including the current file. Each is at most 32 MiB; the oldest is removed when the limit is exceeded.'))
        retention_note = label(tr('Each log segment is at most 32 MiB. Only the newest segments are retained; removed history marks the evidence as incomplete.'), "caption")
        retention_note.setWordWrap(True)
        retention.layout().addRow(retention_note)
        other.addStretch()


    def text(self, key):
        field = self.fields[key]
        if isinstance(field, QComboBox) and field.isEditable():
            return field.currentText().strip()
        return str(field.currentData()) if isinstance(field, QComboBox) else field.text().strip()

    def retranslate_ui(self):
        retranslate_tree(self)
        for editor in self.point_editors:
            editor.retranslate_ui()
        self.update_points_summary()
        self.update_budget()


    @programmatic_changes
    def apply_config(self, config):
        if self._busy:
            return False
        config = TaskConfig.from_mapping(config.to_mapping())
        check_capabilities(config)
        self._applying = True
        self.base_config = config.to_mapping()
        for key, field in self.fields.items():
            value = self.base_config
            for part in key.split("."):
                value = value[part]
            text = str(value)
            if isinstance(field, QComboBox) and field.isEditable():
                field.setCurrentIndex(field.findText(text, Qt.MatchExactly))
                field.setEditText(text)
            elif isinstance(field, QComboBox):
                field.setCurrentIndex(field.findData(text))
            else:
                bind(field, 'setText', text)
        self.rts.setChecked(config.serial.rts_toggle is True)
        for editor, point in zip(self.point_editors, config.points):
            editor.apply_point(point)
        self._applying = False
        self.mode_changed()


    def configuration(self, *, validate=True):
        data = json.loads(json.dumps(self.base_config))
        usage = self.parameter_usage()
        integers = {"unit", "port", "period_ms", "timeout_ms", "connect_timeout_ms",
                    "serial.baud", "serial.stop_bits", "logging.max_files"}
        floats = {"duration_s", "schedule_tolerance_percent"}
        for key in self.fields:
            value = self.text(key)
            if not usage.field_active(key):
                original = self.base_config
                for part in key.split("."):
                    original = original[part]
                field = self.fields[key]
                if value == str(original) or (isinstance(field, QComboBox) and not field.isEditable() and field.currentIndex() < 0):
                    value = original
                if "." in key:
                    parent, name = key.split(".")
                    data[parent][name] = value
                else:
                    data[key] = value
                continue
            try:
                if key in integers:
                    value = int(value)
                elif key in floats:
                    value = float(value)
            except ValueError:
                raise ValueError(tr('{p0}: enter a valid number').format(p0=self.fields[key].accessibleName())) from None
            if "." in key:
                parent, name = key.split(".")
                data[parent][name] = value
            else:
                data[key] = value
        if usage.field_active("serial.rts_toggle"):
            data["serial"]["rts_toggle"] = self.rts.isChecked()
        data["points"] = [editor.configuration() for editor in self.point_editors]
        config = TaskConfig.from_mapping(data, validate=validate)
        if validate:
            check_capabilities(config)
        return config


    def parameter_usage(self):
        from ..domain.usage import ParameterUsage
        usages = [ParameterUsage(self.text('mode'), editor.fields['operation'].draft_value() or 'write-multiple',
                                  editor.fields['readback_every'].value())
                  for editor in self.point_editors if editor.enabled.isChecked()]
        return ParameterUsage(self.text('mode'),
                              'write-multiple' if any(usage.writing for usage in usages) else 'read-input',
                              int(any(usage.readback for usage in usages)))

    def mode_changed(self, *_):
        mode = self.text("mode")
        usage = ParameterUsage(mode)
        previous, self._last_mode = self._last_mode, mode
        # 用户切入新主站角色时先关闭写权限；导入和恢复时尊重明确保存的权限及操作。
        if (mode == 'tcp-master' and previous != mode and not self._applying
                and not self.form_state.programmatic):
            with QSignalBlocker(self.fields['access_mode']):
                self.fields['access_mode'].setCurrentIndex(self.fields['access_mode'].findData('read-only'))
            if previous is not None and not ParameterUsage(previous).master:
                for editor in self.point_editors:
                    editor.fields['operation'].set_draft_value(
                        'read-input' if editor.fields['area'].currentData() == 'input' else 'read-holding')
        self.tcp_box.setVisible(usage.tcp)
        self.tcp_box.setEnabled(usage.tcp)
        bind(self.tcp_box, 'setTitle', tr('TCP target') if usage.master else tr('TCP listener'))
        host_caption = tr('Target IP address') if usage.master else tr('Bind address')
        bind(self.tcp_box.layout().labelForField(self.fields['host']), 'setText', host_caption)
        bind(self.fields['host'], 'setAccessibleName', host_caption)
        self.tcp_box.layout().setRowVisible(self.fields['connect_timeout_ms'], mode == 'tcp-master')
        self.serial_box.setVisible(usage.serial)
        self.serial_box.setEnabled(usage.serial)
        self.access_box.setVisible(True)
        self.access_box.setEnabled(True)
        self.master_box.setVisible(usage.master)
        self.master_box.setEnabled(usage.master)
        self.operation_changed()


    @programmatic_changes
    def operation_changed(self, *_):
        mode = self.text("mode")
        readonly = self.text("access_mode") == "read-only"
        usage = self.parameter_usage()
        for key, widget in self.fields.items():
            widget.setEnabled(usage.field_active(key))
        self.rts.setEnabled(usage.field_active("serial.rts_toggle"))
        self.rts.setVisible(usage.rts_supported)
        bind(self.rts, 'setToolTip', tr('Supported by Windows serial drivers only; ignored on other platforms.'))
        for editor in self.point_editors:
            editor.set_usage(usage, readonly)
        timing = self.timing_box.layout()
        # 同一参数按角色展示不同含义，不把本机数据更新解释为通信发送频率。
        period_caption = tr('Communication polling period (ms)') if usage.master else tr('Input register update period (ms)')
        bind(timing.labelForField(self.fields['period_ms']), 'setText', period_caption)
        bind(self.fields['period_ms'], 'setAccessibleName', period_caption)
        timeout_caption = tr('Cycle budget (ms)') if usage.master else tr('Reply write timeout (ms)') if usage.serial else tr('Partial request timeout (ms)')
        bind(timing.labelForField(self.fields['timeout_ms']), 'setText', timeout_caption)
        bind(self.fields['timeout_ms'], 'setAccessibleName', timeout_caption)
        bind(self.fields['period_ms'], 'setToolTip', (MASTER_PERIOD_HELP if usage.serial else TCP_MASTER_PERIOD_HELP) if usage.master else INPUT_PERIOD_HELP)
        bind(self.fields['timeout_ms'], 'setToolTip', tr('All requests and readbacks share one deadline from the planned cycle time. The budget must be smaller than the polling period.') if mode == 'tcp-master' else tr('Budget for the entire cycle, including the silent interval and all requests.') if usage.master else tr('Wait for one reply write, capped at 50 ms.') if usage.serial else tr('Fixed deadline for receiving a partial request, at least 1000 ms.'))
        if usage.master:
            bind(self.data_note, 'setText', tr('Choose an operation, address, type and byte order for each point. Read values come from the slave; only write points use a local source. Contiguous points with the same operation may share one request.'))
        else:
            note = SLAVE_AREAS_HELP
            if readonly:
                note += tr('\nCurrently read-only: master writes are rejected.')
            bind(self.data_note, 'setText', note)
        self.changed.emit()

    def update_budget(self, *_):
        if self._applying:
            return
        tone = 'idle'
        try:
            config = self.configuration(validate=False)
            if config.mode == 'rtu-master':
                config.serial.validate()
                budget = cycle_budget(config)
                tone = budget.level(config.period_ms)
                message = tr('{points} points → {requests} requests (busiest cycle). Line {line:.3f} ms + workflow 10 ms; minimum {minimum} ms, yellow below {warning} ms. Current {period} ms.').format(
                    points=len(config.enabled_points), requests=budget.transactions, line=budget.line_ms,
                    minimum=budget.minimum_period_ms, warning=budget.warning_period_ms, period=config.period_ms)
            elif config.mode == 'tcp-master':
                message = TCP_MASTER_PERIOD_HELP
            else:
                message = INPUT_PERIOD_HELP
            try:
                check_capabilities(config)
                valid = True
            except (ValueError, TypeError) as error:
                valid, tone = False, 'error'
                message += ' ' + format_error(error)
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
            valid, tone = False, 'error'
            message = tr('Complete a valid configuration: {detail}').format(detail=str(format_error(error)))
        for widget in (self.budget_note, self.data_budget_note):
            bind(widget, 'setText', message)
            widget.setProperty('tone', tone)
            refresh_style(widget)
        self.configuration_valid = valid
        self.validityChanged.emit(valid)

    def update_points_summary(self):
        # 地址冲突以区域为边界，主站区域由每点操作推导。
        master = ParameterUsage(self.text('mode')).master
        points = [PointConfig(enabled=editor.enabled.isChecked(), address=editor.fields['address'].value(),
                              data_type=editor.current_data_type(), area=(
                                  'input' if editor.fields['operation'].draft_value() == 'read-input' else 'holding')
                              if master else editor.fields['area'].currentData())
                  for editor in self.point_editors]
        conflicts = point_conflicts(points)
        overflow = any(point.enabled and point.address + point.register_count > 65536 for point in points)
        overlapping = {index for pair in conflicts for index in pair}
        try:
            period = int(self.text('period_ms'))
        except ValueError:
            period = self.base_config['period_ms']
        # 时序草稿无效时仍校验测点自身，避免除零或把公共参数错误标到全部页签。
        if not MIN_PERIOD_MS <= period <= MAX_PERIOD_MS:
            period = self.base_config['period_ms']
        for i, editor in enumerate(self.point_editors):
            editor.set_conflict(i in overlapping)
            error = editor.validation_error(period)
            title = tr('Point {p0}').format(p0=i + 1)
            name = editor.fields['name'].text().strip()
            tooltip = title + (' · ' + name if name else '') + '\n' + editor.range_label.text()
            if error:
                tooltip += '\n' + error
            self.point_tabs.update_point(i, enabled=points[i].enabled,
                                         error=i in overlapping or bool(error), tooltip=tooltip)
        count = sum(point.enabled for point in points)
        if conflicts:
            text = "；".join(tr('Point {p0} overlaps point {p1}').format(p0=a + 1, p1=b + 1) for a, b in conflicts)
        else:
            text = tr('{p0} / {p1} points enabled').format(p0=count, p1=MAX_POINTS) if count else tr('Enable at least one point')
        incompatible = any(editor.selection_issues() for editor in self.point_editors)
        if incompatible:
            text += tr(' · Resolve incompatible point selections below')
        if overflow:
            text += tr(' · Address range exceeds 65535')
        bind(self.points_status, 'setText', text)
        self.points_status.setProperty("tone", "error" if conflicts or overflow or not count or incompatible else "idle")
        refresh_style(self.points_status)
