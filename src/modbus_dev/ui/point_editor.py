"""可复用的单测点编辑区；独立管理启用、地址、数据源和参数切换。"""
from .i18n import tr, bind, add_item, retranslate_tree
from dataclasses import asdict

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget,
)

from ..domain.config import LINEAR_TRANSFORM_FIELDS, PointConfig, SOURCE_FIELDS
from ..domain.usage import ParameterUsage
from ..domain.operations import OPERATIONS
from .texts import CHOICE_LABELS, HOLDING_AREA_HELP, INPUT_AREA_HELP, SLAVE_AREAS_HELP, HOLDING_INITIAL_HELP
from ..domain.numeric import NUMERIC_TYPES, ORDERS, canonical_order, numeric_type, parse_integer
from .context_choices import ContextComboBox
from .errors import format_error
from .theme import METRICS
from .widgets import CheckBox, ComboBox, SpinBox, label, refresh_style

TRANSFORM_LABELS = {
    "raw_start": tr('Raw range start'), "raw_end": tr('Raw range end'),
    "mapped_start": tr('Mapped range start'), "mapped_end": tr('Mapped range end'),
}


class PointEditor(QWidget):
    changed = Signal()

    def __init__(self, index, parent=None):
        super().__init__(parent)
        bind(self, "setAccessibleName", tr("Point {p0}").format(p0=index + 1))
        self.index = index
        self._loading = False
        self._source_used = True
        self._source_tip = ""
        self._usage = ParameterUsage("tcp-slave")
        self._last_type = "float32"
        self._point = PointConfig(enabled=False)
        self.setObjectName(f"point-{index + 1}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, METRICS.spacing, 0, 0)
        layout.setSpacing(METRICS.spacing)
        layout.setAlignment(Qt.AlignTop)
        header = QHBoxLayout()
        self.enabled = CheckBox(tr('Enable'))
        bind(self.enabled, 'setAccessibleName', tr('Enable point {p0}').format(p0=index + 1))
        self.range_label = label("", "pointRange")
        self.range_label.setWordWrap(True)
        self.range_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.enabled)
        header.addWidget(self.range_label, 1)
        layout.addLayout(header)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(METRICS.section_spacing)
        self.form.setVerticalSpacing(METRICS.spacing)
        self.form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        # 配置栏已有最小宽度，所有输入始终留在右列，不按单行切换上下布局。
        self.form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.fields = {"name": QLineEdit(), "address": SpinBox(),
                       "data_type": ComboBox(), "byte_order": ContextComboBox(normalize=canonical_order),
                       "operation": ContextComboBox(), "area": ComboBox(), "readback_every": SpinBox(),
                       "source.kind": ContextComboBox(allow_unknown=True),
                       "source.value": QLineEdit(), "source.amplitude": QLineEdit(),
                       "source.frequency_hz": QLineEdit(), "source.lower": QLineEdit(), "source.upper": QLineEdit()}
        self.fields["linear_transform.enabled"] = CheckBox(tr('Enable'))
        self.fields["linear_transform.enabled"].setFixedHeight(METRICS.control_height)
        self.fields.update({"linear_transform." + key: QLineEdit() for key in LINEAR_TRANSFORM_FIELDS})
        bind(self.fields['linear_transform.enabled'], 'setToolTip',
             tr('Display conversion only; register reads, writes and source values remain unchanged. Stop the session before editing.'))
        bind(self.fields['name'], 'setPlaceholderText', tr('Optional, e.g. temperature or pressure'))
        self.fields["address"].setRange(0, 65535)
        bind(self.fields['address'], 'setToolTip', tr('Zero-based register address; the selected data type determines register width.'))
        for data_type in NUMERIC_TYPES:
            self.fields["data_type"].addItem(data_type, data_type)
        for order in ORDERS:
            self.fields["byte_order"].addItem(order.removeprefix("B"), order)
        bind(self.fields['byte_order'], 'setToolTip', tr('Only byte orders matching this type are shown. B12 is the default for 16-bit points.'))
        for operation in OPERATIONS:
            add_item(self.fields['operation'], CHOICE_LABELS[operation], operation)
        add_item(self.fields['area'], tr('Holding registers'), 'holding')
        add_item(self.fields['area'], tr('Input registers'), 'input')
        bind(self.fields['area'], 'setToolTip', SLAVE_AREAS_HELP)
        self.fields['readback_every'].setRange(0, 1000000)
        bind(self.fields['readback_every'], 'setToolTip', tr('FC03 readback every N cycles; 0 disables it for this point.'))
        add_item(self.fields['source.kind'], tr('Constant'), 'constant')
        add_item(self.fields['source.kind'], tr('Sine wave'), 'sine')
        add_item(self.fields['source.kind'], tr('Triangle wave'), 'triangle')
        for key, caption in (("name", tr('Name')), ("area", tr('Register area')), ("operation", tr('Operation')),
                             ("readback_every", tr('Readback cycles')), ("address", tr('Start address')),
                             ("data_type", tr('Data type')), ("byte_order", tr('Byte order')), ("source.kind", tr('Data source')),
                             ("source.value", tr('Value')), ("source.amplitude", tr('Amplitude')),
                             ("source.frequency_hz", tr('Frequency (Hz)')), ("source.lower", tr('Lower bound')),
                             ("source.upper", tr('Upper bound')),
                             ("linear_transform.enabled", tr('Linear transform')),
                             *(("linear_transform." + key, caption) for key, caption in TRANSFORM_LABELS.items())):
            widget = self.fields[key]
            widget.setObjectName(f"points.{index}.{key}")
            bind(widget, 'setAccessibleName', tr('Point {p0} · {p1}').format(p0=index + 1, p1=caption))
            caption_widget = label(caption)
            # 长译文可在左列内部折行，不撑宽滚动内容，也不移动右列输入框。
            caption_widget.setWordWrap(True)
            self.form.addRow(caption_widget, widget)
        layout.addWidget(self.body)
        self.source_note = label("", "caption")
        self.source_note.setWordWrap(True)
        layout.addWidget(self.source_note)
        self.selection_note = label("", "caption")
        self.selection_note.setWordWrap(True)
        self.selection_note.setProperty("tone", "error")
        layout.addWidget(self.selection_note)
        self.enabled.toggled.connect(self._changed)
        self.fields["linear_transform.enabled"].toggled.connect(self._changed)
        self.fields["operation"].currentIndexChanged.connect(self._changed)
        self.fields["area"].currentIndexChanged.connect(self._changed)
        self.fields["readback_every"].valueChanged.connect(self._changed)
        self.fields["address"].valueChanged.connect(self._changed)
        self.fields["source.kind"].currentIndexChanged.connect(self._changed)
        self.fields["data_type"].currentIndexChanged.connect(self._changed)
        self.fields["byte_order"].currentIndexChanged.connect(self._changed)
        for key in ("name", "source.value", "source.amplitude", "source.frequency_hz", "source.lower", "source.upper"):
            self.fields[key].textChanged.connect(self._changed)
        for key in LINEAR_TRANSFORM_FIELDS:
            self.fields["linear_transform." + key].textChanged.connect(self._changed)

    def _changed(self, *_):
        if self._loading:
            return
        active = self.enabled.isChecked()
        specification = numeric_type(self.current_data_type())
        orders = self.fields["byte_order"]
        supported_type = self.fields['data_type'].currentIndex() >= 0
        if supported_type and (orders.draft_value() is None or (self._last_type != self.current_data_type()
                                          and orders.draft_value() not in specification.orders)):
            orders.set_draft_value(specification.orders[0])
        self._last_type = self.current_data_type()
        orders.filter_values(specification.orders)
        self._usage = ParameterUsage(self._usage.mode,
            self.fields['operation'].draft_value() or 'write-multiple', self.fields['readback_every'].value())
        self._source_used = self._usage.source
        allowed = tuple(operation for operation, function in OPERATIONS.items()
                        if (not getattr(self, '_readonly', False) or function in (3, 4))
                        and (specification.registers == 1 or function != 6))
        self.fields['operation'].filter_values(allowed)
        self.form.setRowVisible(self.fields['operation'], self._usage.master)
        self.form.setRowVisible(self.fields['area'], not self._usage.master)
        self.form.setRowVisible(self.fields['readback_every'], self._usage.writing)
        # 不兼容的旧选择保留在草稿中，界面留空并即时解释，避免静默替换。
        source_kind = self.fields["source.kind"]
        source_kind.filter_values(tuple(key for key in SOURCE_FIELDS if key != "sine" or not specification.integer))
        # 禁用仅锁定参数，不隐藏内容；页签切换由外层容器负责，启用开关始终可操作。
        self.body.setEnabled(active)
        transform_enabled = self.fields["linear_transform.enabled"].isChecked()
        for key in LINEAR_TRANSFORM_FIELDS:
            field = self.fields["linear_transform." + key]
            field.setEnabled(transform_enabled)
            self.form.labelForField(field).setEnabled(transform_enabled)
        kind = source_kind.currentData()
        sine = kind == "sine"
        holding = not self._usage.master and self.fields['area'].currentData() == 'holding'
        effective_kind = 'constant' if holding else kind
        self._source_row("source.kind", self._source_used and not holding)
        area_help = HOLDING_AREA_HELP if holding else INPUT_AREA_HELP
        bind(source_kind, 'setToolTip', self._source_tip if self._usage.master else area_help)
        bind(self.fields['source.value'], 'setToolTip', HOLDING_INITIAL_HELP if holding else '')
        bind(self.form.labelForField(self.fields['source.value']), 'setText', tr('Initial value') if holding else tr('Offset') if sine else tr('Value'))
        for key in ("value", "amplitude", "frequency_hz", "lower", "upper"):
            self._source_row("source." + key, self._source_used and key in SOURCE_FIELDS.get(effective_kind, ()))
        note = (HOLDING_INITIAL_HELP if holding else INPUT_AREA_HELP if not self._usage.master
                else tr('Values come from the remote slave; no local source is used.') if not self._source_used else '')
        if self._source_used and not holding and kind == 'triangle':
            note += ('\n' if note else '') + tr('Fixed step 1 per data update; starts at the lower bound and reverses after reaching each bound.')
        bind(self.source_note, 'setText', note)
        self.source_note.setVisible(bool(note))
        self.source_note.setEnabled(active)
        issues = self.selection_issues()
        message = issues[0] if issues else ""
        for issue in issues[1:]:
            message = message + " · " + issue
        bind(self.selection_note, 'setText', message)
        self.selection_note.setVisible(active and bool(issues))
        self.set_conflict(False)
        self.changed.emit()

    def _source_row(self, key, visible):
        widget = self.fields[key]
        self.form.setRowVisible(widget, visible)
        widget.setEnabled(visible)
        self.form.labelForField(widget).setEnabled(visible)

    def selection_issues(self):
        if not self.enabled.isChecked():
            return []
        issues = []
        if self.fields['data_type'].currentIndex() < 0:
            issues.append(tr('Choose one of: {choices}.').format(choices=', '.join(NUMERIC_TYPES)))
        if not self.fields["byte_order"].compatible():
            issues.append(tr('Previous byte order is incompatible; select a byte order for this type.'))
        if self._usage.master and not self.fields['operation'].compatible():
            issues.append(tr('Select an operation compatible with access and data width.'))
        holding = not self._usage.master and self.fields['area'].currentData() == 'holding'
        if self._source_used and not holding and not self.fields["source.kind"].compatible():
            issues.append(tr('Previous data source is incompatible; select a data source for this type.'))
        return issues

    def validation_error(self, period_ms):
        """复用逐点领域校验，为隐藏页签提供错误提示，不修改草稿。"""
        if not self.enabled.isChecked():
            return ''
        try:
            path = f'points[{self.index}]'
            point = PointConfig.from_mapping(self.configuration(), path, source_used=self._source_used)
            point.validate(period_ms, path, source_used=self._source_used)
        except (ValueError, TypeError) as issue:
            return str(format_error(issue))
        return ''

    def retranslate_ui(self):
        retranslate_tree(self)
        self.set_conflict(False)

    def set_usage(self, usage, readonly=False):
        self._usage = usage
        self._readonly = readonly
        self._source_tip = (tr('Generate values for the master to write to remote holding registers.') if usage.master else
                            tr('Input values follow the source; holding values initialize once.'))
        caption = tr('Start address') if usage.master else tr('Local start address')
        bind(self.form.labelForField(self.fields['address']), 'setText', caption)
        self._changed()

    def set_conflict(self, conflict):
        address = self.fields["address"].value()
        data_type = self.current_data_type()
        overflow = self.enabled.isChecked() and address + numeric_type(data_type).registers > 65536
        text = (tr('Registers {p0}–{p1} · {data_type}').format(p0=address, p1=address + numeric_type(data_type).registers - 1, data_type=data_type)
                if self.enabled.isChecked() else tr('Disabled · No registers occupied'))
        if self.enabled.isChecked():
            context = (CHOICE_LABELS.get(self.fields['operation'].draft_value(), '') if self._usage.master
                       else tr('Holding registers') if self.fields['area'].currentData() == 'holding' else tr('Input registers'))
            text = context + ' · ' + text
        bind(self.range_label, 'setText', text + (tr(' · Address overlap') if conflict else '')
             + (tr(' · Address range exceeds 65535') if overflow else ''))
        self.range_label.setProperty("tone", "error" if conflict or overflow or self.selection_issues() else "idle")
        refresh_style(self.range_label)

    def apply_point(self, point):
        self._loading = True
        self._point = point
        try:
            self.enabled.setChecked(point.enabled)
            bind(self.fields['name'], 'setText', str(point.name))
            valid_address = isinstance(point.address, int) and not isinstance(point.address, bool)
            self.fields["address"].setValue(point.address if valid_address else 0)
            self.fields["data_type"].setCurrentIndex(self.fields["data_type"].findData(point.data_type))
            self._last_type = point.data_type
            self.fields["byte_order"].set_draft_value(point.byte_order or (point.encoding_order() if point.enabled else "B1234"))
            self.fields['operation'].set_draft_value(point.operation or 'write-multiple')
            self.fields['area'].setCurrentIndex(self.fields['area'].findData(point.area))
            self.fields['readback_every'].setValue(point.readback_every or 0)
            kind = self.fields["source.kind"]
            kind.set_draft_value(point.source.kind)
            for key in ("value", "amplitude", "frequency_hz", "lower", "upper"):
                bind(self.fields['source.' + key], 'setText', str(getattr(point.source, key)))
            self.fields["linear_transform.enabled"].setChecked(point.linear_transform.enabled)
            for key in LINEAR_TRANSFORM_FIELDS:
                bind(self.fields['linear_transform.' + key], 'setText', str(getattr(point.linear_transform, key)))
        finally:
            self._loading = False
        self._changed()

    def configuration(self):
        """有效参数转换为数值；无关草稿保留，切换操作后再校验。"""
        active = self.enabled.isChecked()
        issues = self.selection_issues()
        if issues:
            raise ValueError(tr('Point {p0}: {message}').format(p0=self.index + 1, message=issues[0]))
        kind = self.fields["source.kind"].draft_value()
        if kind is None:
            kind = self._point.source.kind
        data = asdict(self._point)
        data.update(enabled=active, name=self.fields["name"].text().strip(),
                    address=self.fields["address"].value())
        data["byte_order"] = self.fields["byte_order"].draft_value()
        if self.fields["data_type"].currentIndex() >= 0:
            data["data_type"] = self.fields["data_type"].currentData()
        if not active and not (isinstance(self._point.address, int) and not isinstance(self._point.address, bool)
                               and 0 <= self._point.address <= 65535):
            data["address"] = self._point.address
        data['operation'] = self.fields['operation'].draft_value()
        data['area'] = ('input' if data['operation'] == 'read-input' else 'holding') if self._usage.master else self.fields['area'].currentData()
        data['readback_every'] = self.fields['readback_every'].value()
        transform = data["linear_transform"]
        transform["enabled"] = self.fields["linear_transform.enabled"].isChecked()
        for key, caption in TRANSFORM_LABELS.items():
            draft = self.fields["linear_transform." + key].text().strip()
            if not active or not transform["enabled"]:
                if draft != str(transform[key]):
                    transform[key] = draft
                continue
            try:
                transform[key] = float(draft)
            except ValueError:
                raise ValueError(tr('Point {p0} · {p1}: enter a valid number').format(
                    p0=self.index + 1, p1=caption)) from None
        if active and not self._usage.master and data['area'] == 'holding':
            kind = 'constant'
        data["source"]["kind"] = kind
        for key, caption in (("value", tr('Value / offset')), ("amplitude", tr('Amplitude')), ("frequency_hz", tr('Frequency (Hz)')),
                             ("lower", tr('Lower bound')), ("upper", tr('Upper bound'))):
            draft = self.fields["source." + key].text().strip()
            used = active and self._source_used and key in SOURCE_FIELDS.get(kind, ())
            if not used:
                if draft != str(data["source"][key]):
                    data["source"][key] = draft
                continue
            try:
                data["source"][key] = (parse_integer(draft, data["data_type"])
                                       if numeric_type(data["data_type"]).integer else float(draft))
            except ValueError:
                raise ValueError(tr('Point {p0} · {p1}: enter a valid number').format(p0=self.index + 1, p1=caption)) from None
        return data

    def current_data_type(self):
        """无效的禁用草稿仅按默认宽度预览，不修改其保存内容。"""
        return self.fields["data_type"].currentData() or "float32"
