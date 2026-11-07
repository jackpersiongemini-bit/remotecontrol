"""Базовые блоки окна прибора.

* ``ActionBar``     - верх окна: подключение, выходы, действия и крупный вывод измерений;
* ``ParamTable``    - таблица выбранных параметров (её же показывает компактное окно);
* ``SettingsPanel`` - полный экран настроек, где отмечается, что попадёт в таблицу;
* ``ModeSwitch``    - переключатель «Компакт | Полный»;
* ``StatusLed``     - индикатор связи.
"""
from __future__ import annotations

from collections import OrderedDict

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QBoxLayout, QButtonGroup, QFormLayout, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
                             QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTabWidget, QToolButton,
                             QVBoxLayout, QWidget)

from .editors import ToggleEditor, format_value, make_editor
from .model import FieldSpec, InstrumentModel

__all__ = ["StatusLed", "ModeSwitch", "ReadoutTile", "ActionBar", "ParamTable", "SettingsPanel"]

STATES = {
    "disconnected": ("Disconnected", "#7a828e"),
    "connecting": ("Connecting…", "#d0a630"),
    "connected": ("Connected", "#3fb950"),
    "error": ("Connection error", "#f85149"),
}


class StatusLed(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("led")
        self.set_state("disconnected")

    def set_state(self, state: str) -> None:
        text, color = STATES.get(state, (state, "#7a828e"))
        self.setToolTip(text)
        self.setStyleSheet(f"background: {color};")


class ModeSwitch(QFrame):
    """Сегментный переключатель режимов окна."""

    COMPACT, FULL = "compact", "full"
    changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modeSwitch")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        self.buttons = {}
        for mode, text, tip in ((self.COMPACT, "Compact", "Top bar and selected parameters only"),
                                (self.FULL, "Full", "All instrument settings")):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setProperty("mode", mode)
            self._group.addButton(b)
            lay.addWidget(b)
            self.buttons[mode] = b
        self.buttons[self.FULL].setChecked(True)
        self._group.buttonClicked.connect(lambda b: self.changed.emit(b.property("mode")))

    @property
    def mode(self) -> str:
        return next(m for m, b in self.buttons.items() if b.isChecked())

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.buttons[mode].setChecked(True)
            self.changed.emit(mode)


class ReadoutTile(QFrame):
    """Крупный вывод одного значения: подпись сверху, число снизу."""

    def __init__(self, model: InstrumentModel, spec: FieldSpec, parent=None):
        super().__init__(parent)
        self.setObjectName("readout")
        self.spec = spec
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 6)
        lay.setSpacing(0)
        cap = QLabel(spec.caption)
        cap.setObjectName("readoutCaption")
        self.value = QLabel("—")
        self.value.setObjectName("readoutValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(cap)
        lay.addWidget(self.value)
        self.setToolTip(spec.key)
        model.value_changed.connect(self._on_value)
        self._on_value(spec.key, model.value(spec.key))

    def _on_value(self, key, value):
        if key == self.spec.key:
            self.value.setText(format_value(self.spec, value, digits=5))


class ActionBar(QFrame):
    """Верхняя панель: основные действия слева, вывод справа."""

    READOUT_COLUMNS = 3

    def __init__(self, model: InstrumentModel, parent=None):
        super().__init__(parent)
        self.setObjectName("actionBar")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.model = model
        spec = model.spec
        self.root = root = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(12)

        left = QGridLayout()
        left.setSpacing(6)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("role", "primary")
        self.connect_btn.clicked.connect(self._toggle_connection)
        left.addWidget(self.connect_btn, 0, 0)
        self.toggles = []
        for key in spec.toggles:
            f = spec.field(key)
            t = ToggleEditor(model, f, text=f.caption)
            t.setProperty("role", "output")
            self.toggles.append(t)
        self.action_buttons = []
        for a in spec.actions:
            b = QPushButton(a.label)
            b.setToolTip(a.name)
            b.clicked.connect(lambda _=False, a=a: self._run(a))
            self.action_buttons.append(b)
        # ряд 0: подключение + выходы, ряд 1: действия
        for i, w in enumerate(self.toggles, start=1):
            left.addWidget(w, 0, i)
        for i, w in enumerate(self.action_buttons):
            left.addWidget(w, 1, i)
        root.addLayout(left)
        root.addStretch(1)

        right = QGridLayout()
        right.setSpacing(6)
        self.tiles = [ReadoutTile(model, spec.field(k)) for k in spec.readouts]
        n = len(self.tiles)
        rows = -(-n // self.READOUT_COLUMNS) or 1
        cols = -(-n // rows) or 1           # 4 -> 2x2, 6 -> 3x2
        for i, t in enumerate(self.tiles):
            right.addWidget(t, i // cols, i % cols)
        root.addLayout(right)

        model.state_changed.connect(self._on_state)
        self._on_state(model.state)

    def set_compact(self, on: bool) -> None:
        """Компактно - вывод под кнопками, а не справа от них (окно уже)."""
        self.root.setDirection(QBoxLayout.Direction.TopToBottom if on else QBoxLayout.Direction.LeftToRight)

    def _toggle_connection(self):
        self.model.connect_requested.emit(self.model.state not in ("connected", "connecting"))

    def _run(self, a):
        if a.confirm and QMessageBox.question(self, a.label, a.confirm) != QMessageBox.StandardButton.Yes:
            return
        self.model.run(a.name)

    def _on_state(self, state):
        on = state == "connected"
        self.connect_btn.setText("Disconnect" if state in ("connected", "connecting") else "Connect")
        for w in self.action_buttons:
            w.setEnabled(on)


class ParamTable(QTableWidget):
    """Таблица выбранных параметров: Параметр | Значение (единица - в значении).

    Порядок строк = порядок выбора; ПКМ - поднять/опустить/убрать.
    """

    COLS = ("Parameter", "Value")

    def __init__(self, model: InstrumentModel, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setObjectName("paramTable")
        self.model = model
        self.setHorizontalHeaderLabels(self.COLS)
        h = self.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(30)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.placeholder = QLabel("No parameters selected.\nMark them with ☆ in the full view.", self.viewport())
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        model.selection_changed.connect(self.rebuild)
        self.rebuild()

    @property
    def keys(self) -> list[str]:
        return [self.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(self.rowCount())]

    def rebuild(self, *_):
        from PyQt6.QtWidgets import QTableWidgetItem
        self.setRowCount(0)
        for key in self.model.selected:
            f = self.model.spec.field(key)
            r = self.rowCount()
            self.insertRow(r)
            item = QTableWidgetItem(f.caption)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(key)
            self.setItem(r, 0, item)
            self.setCellWidget(r, 1, make_editor(self.model, f))
        self.placeholder.setVisible(self.rowCount() == 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.placeholder.resize(self.viewport().size())

    def _menu(self, pos):
        row = self.rowAt(pos.y())
        if row < 0:
            return
        key = self.keys[row]
        m = QMenu(self)
        m.addAction("Move up", lambda: self.model.move(key, -1))
        m.addAction("Move down", lambda: self.model.move(key, +1))
        m.addSeparator()
        m.addAction("Remove from table", lambda: self.model.select(key, False))
        m.exec(self.viewport().mapToGlobal(pos))

    def sizeHint(self):
        s = super().sizeHint()
        s.setWidth(360)
        return s


class _PinButton(QToolButton):
    """☆ / ★ - поле в таблице компактного окна."""

    def __init__(self, model: InstrumentModel, key: str, parent=None):
        super().__init__(parent)
        self.model, self.key = model, key
        self.setObjectName("pin")
        self.setCheckable(True)
        self.setToolTip("Show in the table (compact view)")
        self.toggled.connect(lambda on: (self.setText("★" if on else "☆"), model.select(key, on)))
        model.selection_changed.connect(self._sync)
        self._sync(model.selected)
        self.setText("★" if self.isChecked() else "☆")

    def _sync(self, keys):
        self.blockSignals(True)
        self.setChecked(self.key in keys)
        self.setText("★" if self.isChecked() else "☆")
        self.blockSignals(False)


class SettingsPanel(QTabWidget):
    """Полный экран настроек: ``group = "Вкладка/Группа"``, группы до трёх в ряд."""

    GROUP_COLUMNS = 3

    def __init__(self, model: InstrumentModel, parent=None):
        super().__init__(parent)
        self.setObjectName("settings")
        self.model = model
        layout: "OrderedDict[str, OrderedDict[str, list[FieldSpec]]]" = OrderedDict()
        for f in model.spec.fields:
            tab, _, group = f.group.partition("/")
            layout.setdefault(tab, OrderedDict()).setdefault(group or tab, []).append(f)
        self.pins: dict[str, _PinButton] = {}
        for tab, groups in layout.items():
            page = QWidget()
            grid = QGridLayout(page)
            grid.setSpacing(10)
            for i, (gname, fields) in enumerate(groups.items()):
                box = QGroupBox(gname)
                form = QFormLayout(box)
                form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                for f in fields:
                    row = QHBoxLayout()
                    row.setSpacing(4)
                    ed = make_editor(model, f)
                    ed.setMinimumWidth(100)
                    pin = _PinButton(model, f.key)
                    self.pins[f.key] = pin
                    row.addWidget(ed, 1)
                    row.addWidget(pin)
                    form.addRow(f.label, row)
                grid.addWidget(box, i // self.GROUP_COLUMNS, i % self.GROUP_COLUMNS,
                               alignment=Qt.AlignmentFlag.AlignTop)
            grid.setRowStretch(grid.rowCount(), 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.addTab(scroll, tab)
