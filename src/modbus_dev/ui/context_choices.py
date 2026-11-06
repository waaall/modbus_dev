"""按上下文隐藏不适用的候选项，同时保留原始草稿供用户修正或切回。"""
from PySide6.QtCore import QEvent, QSignalBlocker, Qt

from .i18n import bind, tr
from .widgets import ComboBox


class ContextComboBox(ComboBox):
    def __init__(self, parent=None, *, normalize=lambda value: value, allow_unknown=False):
        super().__init__(parent)
        # 共用短提示，避免未显示的长占位文案撑大最小宽度；完整原因由表单说明。
        bind(self, 'setPlaceholderText', tr('Select'))
        self._normalize = normalize
        self._allow_unknown = allow_unknown
        self._draft_value = None
        self._allowed = None
        self._aliases = {}
        self.currentIndexChanged.connect(self._remember_selection)
        self.view().installEventFilter(self)

    def eventFilter(self, watched, event):
        if (watched is self.view() and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Home, Qt.Key_End)):
            # QListView 的首尾导航不会自动回退到可见行，显式跳过过滤掉的候选。
            rows = [index for index in range(self.count()) if not self.view().isRowHidden(index)
                    and self.model().item(index).isEnabled()]
            if rows:
                index = self.model().index(rows[0] if event.key() == Qt.Key_Home else rows[-1], 0)
                self.view().setCurrentIndex(index)
                self.view().scrollTo(index)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _remember_selection(self, index):
        if index >= 0:
            self._draft_value = self.itemData(index)

    def draft_value(self):
        return self._draft_value

    def accepts_draft_value(self, value):
        # 旧草稿用空值保存不可显示的选择；无关数据源还可保留暂不支持的来源名。
        return (value is None or self.findData(self._normalize(value)) >= 0
                or self._allow_unknown and isinstance(value, str))

    def set_draft_value(self, value):
        self._draft_value = self._normalize(value)
        self.filter_values(self._allowed, aliases=self._aliases)

    def filter_values(self, allowed, *, aliases=None):
        """只改变可见候选和当前显示，不静默转换不兼容的源或排列。"""
        self._allowed = allowed
        self._aliases = aliases or {}
        with QSignalBlocker(self):
            for index in range(self.count()):
                applicable = allowed is None or self.itemData(index) in allowed
                self.model().item(index).setEnabled(applicable)
                self.view().setRowHidden(index, not applicable)
            visible_value = self._aliases.get(self._draft_value, self._draft_value)
            index = self.findData(visible_value)
            self.setCurrentIndex(index if index >= 0 and not self.view().isRowHidden(index) else -1)

    def compatible(self):
        return self.currentIndex() >= 0
