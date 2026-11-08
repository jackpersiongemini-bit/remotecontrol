"""Виджеты параметров в духе Parameter.py Алексея, но поверх InstrumentProxy.

* ``PLineEdit`` - число с единицей (SI-префиксы при вводе и показе), запись по Enter/уходу фокуса;
* ``PNLabel``   - то же, только показ;
* ``PSButton``  - кнопка, клик переключает на следующий вариант ({"OFF": False, "ON": True});
* ``PLabel``    - текст / значение прибора;
* ``PBGroup``   - взаимоисключающие кнопки (пресеты);
* ``PanelLine`` / ``PanelTable`` / ``Panel`` - строки «подпись | виджеты» в панели.

Виджет знает только прокси и ключ профиля. Состояние записи (``wstate``: process / error /
verified) окрашивает рамку и собирается ``WriteTracker`` для индикаторов в заголовке окна.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QTabBar, QVBoxLayout,
                             QWidget)

from ..core.params import ConnectionState, Kind, PStatus
from ..core.proxy import InstrumentProxy
from ..core.scpi import SI_PREFIXES, si_format, si_parse
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_INDICATOR_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER_LIGHT, UI_COLOR_INDICATOR_FRAME, UI_COLOR_INVERT,
                    UI_COLOR_INVERT_HOVER, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_CONTROL_HEIGHT_PX, render_qss)
from .window import WriteState

__all__ = ["PLineEdit", "PNLabel", "PSButton", "PLabel", "PBGroup", "PTabBar", "PanelLine", "PanelTable", "Panel",
           "WriteTracker"]

_WSTATE = {PStatus.WRITING: "process", PStatus.ERROR: "error", PStatus.INVALID: "error",
           PStatus.WRITTEN: "verified", PStatus.COERCED: "verified", PStatus.UNKNOWN: ""}


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    for child in w.findChildren(QWidget):
        child.style().unpolish(child)
        child.style().polish(child)


class _Bound:
    """Привязка к ключу: значение, статус записи, доступность по состоянию связи."""

    def bind(self, proxy: InstrumentProxy, key: str) -> None:
        self.proxy, self.key = proxy, key
        self.spec = proxy.params[key]
        self._value: Any = None
        proxy.subscribe(key, self._on_value, self._on_status)
        proxy.state_changed.connect(lambda _s: self._update_enabled())
        self.setToolTip(key)
        self._update_enabled()

    def _update_enabled(self) -> None:
        self.setEnabled(self.proxy.is_connected and self._value is not None)

    def _on_value(self, value: Any) -> None:
        self._value = value
        self._update_enabled()
        self.show_value(value)

    def _on_status(self, status: PStatus, message: str) -> None:
        self.setProperty("wstate", _WSTATE.get(status, ""))
        _repolish(self)
        self.setToolTip(self.key + (f"\n{message}" if message else ""))

    def show_value(self, value: Any) -> None:  # pragma: no cover
        raise NotImplementedError


#: префиксы, которые предлагает список единиц (порядок - по возрастанию)
UNIT_PREFIXES = {"s": ("n", "µ", "m", ""), "Hz": ("", "k", "M"), "V": ("m", ""), "Vpp": ("m", ""),
                 "A": ("m", ""), "W": ("m", "")}


class PLineEdit(QLineEdit, _Bound):
    """Число слева, единица справа - кнопка со списком префиксов (ns / µs / ms / s).

    Введённое число берётся в выбранной единице; можно ввести и с префиксом («30u», «1.5 kHz»).
    Пока единицу не выбрали вручную, префикс подбирается под значение.
    """

    def __init__(self, proxy: InstrumentProxy, key: str, unit: str | None = None, digits: int = 6, parent=None):
        super().__init__(parent)
        self.unit = unit if unit is not None else proxy.params[key].unit
        self.digits = digits
        self.prefixes = UNIT_PREFIXES.get(self.unit, ("",))
        self.prefix = "" if "" in self.prefixes else self.prefixes[-1]
        self.pinned = False                       # единица выбрана вручную
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.unit_button: QPushButton | None = None
        if self.unit:
            b = QPushButton(self.unit, self)
            b.setObjectName("unit")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if len(self.prefixes) > 1:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(self._unit_menu)
            else:
                b.setProperty("static", True)
            self.unit_button = b
        self.bind(proxy, key)
        self.editingFinished.connect(self.change_value)
        self._update_unit()

    @property
    def value(self):
        return self._value

    # -------------------------------------------------------- единица
    def _factor(self) -> float:
        return SI_PREFIXES[self.prefix] if self.prefix else 1.0

    def _auto_prefix(self, value) -> None:
        if self.pinned or not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
            return
        chosen = self.prefixes[0]
        for p in self.prefixes:
            if abs(value) >= (SI_PREFIXES[p] if p else 1.0) * 0.9999999:
                chosen = p
        self.prefix = chosen

    def _update_unit(self) -> None:
        if self.unit_button is None:
            return
        self.unit_button.setText(f"{self.prefix}{self.unit}")
        self._place_unit()

    def _place_unit(self) -> None:
        b = self.unit_button
        if b is None:
            return
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(self.width() - w - 2, 2, w, max(self.height() - 4, 0))
        self.setTextMargins(0, 0, w, 0)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        self._place_unit()

    def _unit_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for p in self.prefixes:
            act = menu.addAction(f"{p}{self.unit}")
            act.setCheckable(True)
            act.setChecked(p == self.prefix)
            act.triggered.connect(lambda _=False, p=p: self.set_prefix(p))
        menu.popup(self.unit_button.mapToGlobal(self.unit_button.rect().bottomLeft()))

    def set_prefix(self, prefix: str) -> None:
        """Выбрать единицу вручную: значение не меняется, меняется только показ."""
        self.prefix, self.pinned = prefix, True
        self._update_unit()
        self.show_value(self._value, force=True)

    # -------------------------------------------------------- значение
    def show_value(self, value, force: bool = False):
        if not force and self.hasFocus() and self.isModified():
            return
        if value is None:
            self.setText("")
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and self.unit:
            self._auto_prefix(value)
            self._update_unit()
            self.setText(f"{value / self._factor():.{self.digits}g}")
        else:
            self.setText(str(value))
        self.setModified(False)

    def _parse(self, text: str):
        if self.spec.kind not in (Kind.FLOAT, Kind.INT):
            return text.strip()
        try:
            return float(text.strip().replace(",", ".")) * self._factor()
        except ValueError:
            return si_parse(text, self.unit)        # с префиксом: «30u», «1.5 kHz»

    def change_value(self) -> None:
        if self.isReadOnly() or not self.isModified():
            return
        self.setModified(False)
        try:
            value = self.spec.validate(self._parse(self.text()))
        except ValueError as exc:
            self._on_status(PStatus.INVALID, str(exc))
            self.show_value(self._value)
            return
        self.set_value(value)
        self.show_value(self._value)

    def set_value(self, value: Any) -> bool:
        """Отправить значение, если оно отличается от текущего. True - если отправлено."""
        if self._value is not None and self.spec.same(value, self._value):
            return False
        self.proxy.set(self.key, value)
        return True


class PNLabel(PLineEdit):
    """Число только для показа (DUTY)."""

    def __init__(self, proxy: InstrumentProxy, key: str, unit: str | None = None, digits: int = 6, parent=None):
        super().__init__(proxy, key, unit, digits, parent)
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


def paint_indicator(p: QPainter, rect: QRectF, bar: str = UI_COLOR_TEXT_MUTED) -> None:
    """Индикатор «включено/выбрано»: тонкая рамка и толстая нижняя граница по скруглению.

    Рисуется вручную: QSS с разной толщиной границ даёт кривые углы на скруглениях.
    """
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(UI_COLOR_INDICATOR_FRAME), UI_BORDER_WIDTH_PX))
    p.drawRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
    clip = QPainterPath()
    clip.addRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
    p.setClipPath(clip)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(bar))
    p.drawRect(QRectF(rect.left(), rect.bottom() - UI_INDICATOR_WIDTH_PX, rect.width(), UI_INDICATOR_WIDTH_PX))
    p.restore()


class IndicatorButton(QPushButton):
    """QPushButton с индикатором, когда он «включён» (``indicated()``; по умолчанию - isChecked)."""

    def indicated(self) -> bool:
        return self.isChecked()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if self.indicated():
            p = QPainter(self)
            paint_indicator(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class PSButton(IndicatorButton, _Bound):
    """Кнопка-переключатель: клик - следующий вариант. ``states`` - {подпись: значение}.

    Подпись показывает только значение прибора (``NKNW`` - пока не известно).
    """

    UNKNOWN = "NKNW"

    def indicated(self) -> bool:
        return self.property("state") in ("true", "on")

    def __init__(self, proxy: InstrumentProxy, key: str, states, parent=None):
        super().__init__(self.UNKNOWN, parent)
        pairs = list(states.items()) if isinstance(states, dict) else list(states)   # или [(подпись, значение)]
        if not pairs:
            raise ValueError("PSButton requires at least one state")
        self._names = tuple(n for n, _v in pairs)
        self._data = tuple(v for _n, v in pairs)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bind(proxy, key)
        self.clicked.connect(self.next_value)

    @property
    def value(self):
        return self._value

    @staticmethod
    def _norm(value):
        return str(value).upper() if isinstance(value, str) else value

    def _index(self, value) -> int:
        for i, data in enumerate(self._data):
            if value is not None and self._norm(data) == self._norm(value):
                return i
        return -1

    def show_value(self, value):
        i = self._index(value)
        self.setText(self._names[i] if i >= 0 else self.UNKNOWN)
        self.setProperty("state", str(self._data[i]).lower() if i >= 0 else "")
        _repolish(self)

    def next_value(self) -> None:
        """2 варианта - переключение по клику; больше - выпадающий список под кнопкой."""
        if len(self._data) > 2:
            menu = QMenu(self)
            menu.setObjectName("psmenu")
            current = self._index(self._value)
            for i, name in enumerate(self._names):
                act = menu.addAction(name)
                act.setCheckable(True)
                act.setChecked(i == current)
                act.triggered.connect(lambda _=False, v=self._data[i]: self.set_value(v))
            menu.setMinimumWidth(self.width())
            menu.popup(self.mapToGlobal(self.rect().bottomLeft()))
            return
        i = self._index(self._value)
        self.set_value(self._data[(i + 1) % len(self._data)], force=len(self._data) == 1)

    def set_value(self, value, force: bool = False) -> bool:
        if not force and self._index(self._value) == self._index(value) >= 0:
            return False
        self.proxy.set(self.key, value)
        return True


class PLabel(QLabel, _Bound):
    """Показ значения прибора (или просто текст, если ``proxy`` не задан)."""

    def __init__(self, proxy: InstrumentProxy | None = None, key: str = "", unit: str | None = None,
                 digits: int = 6, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if proxy is not None:
            self.unit = unit if unit is not None else proxy.params[key].unit
            self.digits = digits
            self.bind(proxy, key)

    def _update_enabled(self) -> None:
        pass

    def show_value(self, value):
        if value is None:
            self.setText("")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self.setText(si_format(value, self.unit, self.digits))
        else:
            self.setText(str(value))


class PBGroup(QWidget):
    """Группа взаимоисключающих кнопок (без привязки к прибору - для пресетов и т.п.)."""

    selected = pyqtSignal(int)

    def __init__(self, names: list[str] | tuple[str, ...] = (), columns: int = 0, parent=None):
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.columns = columns                   # 0 - в одну строку, иначе сетка
        self.group_layout = QGridLayout(self)
        self.group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_layout.setSpacing(4)
        self._buttons: list[QPushButton] = []
        for name in names:
            self.add_button(name)
        self._group.idClicked.connect(self.selected)

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def _relayout(self) -> None:
        for b in self._buttons:
            self.group_layout.removeWidget(b)
        for i, b in enumerate(self._buttons):
            r, c = divmod(i, self.columns) if self.columns else (0, i)
            self.group_layout.addWidget(b, r, c)

    def add_button(self, name: str) -> QPushButton:
        b = IndicatorButton(name)
        b.setObjectName("chip")
        b.setCheckable(True)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._group.addButton(b, len(self._buttons))
        self._buttons.append(b)
        self._relayout()
        return b

    def remove_button(self, index: int) -> None:
        if not 0 <= index < len(self._buttons):
            return
        b = self._buttons.pop(index)
        self._group.removeButton(b)
        for i, other in enumerate(self._buttons):
            self._group.setId(other, i)
        self.group_layout.removeWidget(b)
        b.deleteLater()
        self._relayout()

    def checked_index(self) -> int:
        return self._group.checkedId()

    def set_checked(self, index: int) -> None:
        if index < 0:
            self.clear()
        elif self._group.button(index):
            self._group.button(index).setChecked(True)

    def clear(self) -> None:
        self._group.setExclusive(False)
        for b in self._group.buttons():
            b.setChecked(False)
        self._group.setExclusive(True)


class PTabBar(QTabBar):
    """Вкладки (QTabBar) в стиле панели: выбранная - фон поля, отмеченная ``set_on`` - инверсия.

    QSS не умеет красить отдельные вкладки по своему признаку, поэтому вкладки рисуются здесь.
    Колесо мыши и клавиши переключают вкладки (поведение QTabBar).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(True)
        self.setDrawBase(False)
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on: set[int] = set()
        self._hover = -1

    def set_on(self, index: int, on: bool) -> None:
        if on != (index in self._on):
            (self._on.add if on else self._on.discard)(index)
            self.update()

    def is_on(self, index: int) -> bool:
        return index in self._on

    def tabSizeHint(self, index):  # noqa: N802 - Qt
        hint = super().tabSizeHint(index)
        return QSize(max(hint.width(), 56), UI_CONTROL_HEIGHT_PX)

    def minimumTabSizeHint(self, index):  # noqa: N802 - Qt
        return self.tabSizeHint(index)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        super().mouseMoveEvent(e)
        hover = self.tabAt(e.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        super().leaveEvent(e)
        self._hover = -1
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(self.count()):
            rect = QRectF(self.tabRect(i)).adjusted(2.5, 0.5, -2.5, -0.5)
            selected, on, hover = i == self.currentIndex(), i in self._on, i == self._hover
            # фон: наведение и выбранная вкладка - инверсия
            bg = UI_COLOR_INVERT_HOVER if hover else UI_COLOR_INVERT if selected else None
            if bg:
                p.setBrush(QColor(bg))
                p.drawRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
            if on:                        # режим включён: тонкая рамка + толстая нижняя граница
                paint_indicator(p, rect, UI_COLOR_TEXT_INVERSE if (selected or hover) else UI_COLOR_TEXT_MUTED)
            fg = UI_COLOR_TEXT_INVERSE if (hover or selected) else \
                UI_COLOR_TEXT_MUTED if on else UI_COLOR_BORDER_LIGHT
            font = QFont(self.font())
            font.setBold(selected)
            p.setFont(font)
            p.setPen(QColor(fg))
            p.drawText(rect.adjusted(0, 0, 0, -1 if on else 0), Qt.AlignmentFlag.AlignCenter, self.tabText(i))
            p.setPen(Qt.PenStyle.NoPen)


class PanelLine(QWidget):
    """Строка панели: подпись слева, дальше виджеты (по одному на столбец)."""

    def __init__(self, name: str, *widgets: QWidget, parent=None):
        super().__init__(parent)
        self._name = QLabel(name)
        self._name.setObjectName("lineName")
        self._name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.widgets: list[QWidget] = []
        self.line_layout = QHBoxLayout(self)
        self.line_layout.setContentsMargins(0, 0, 0, 0)
        self.line_layout.setSpacing(6)
        self.line_layout.addWidget(self._name)
        self.add_widget(*widgets)

    def add_widget(self, *widgets: QWidget) -> None:
        for w in widgets:
            self.widgets.append(w)
            self.line_layout.addWidget(w)

    @property
    def name(self) -> str:
        return self._name.text()

    def set_label_width(self, width: int) -> None:
        self._name.setFixedWidth(width)

    def label_hint(self) -> int:
        self._name.ensurePolished()
        return self._name.sizeHint().width()


class PanelTable(QWidget):
    """Строки панели одна под другой; подписи выравниваются по самой длинной."""

    def __init__(self, *lines: PanelLine, parent=None):
        super().__init__(parent)
        self.lines: list[PanelLine] = []
        self.table_layout = QVBoxLayout(self)
        self.table_layout.setContentsMargins(0, 0, 0, 0)
        self.table_layout.setSpacing(4)
        self.table_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.add_line(*lines)

    def add_line(self, *lines: PanelLine) -> None:
        for line in lines:
            self.lines.append(line)
            self.table_layout.addWidget(line)

    def build(self, column_width: int | None = None) -> None:
        """Вызывать после применения стиля (размеры зависят от шрифта)."""
        if not self.lines:
            return
        width = max(line.label_hint() for line in self.lines)
        for line in self.lines:
            line.set_label_width(width)
            if column_width:
                for w in line.widgets:
                    w.setMinimumWidth(column_width)


class Panel(QFrame):
    STYLE_SHEET = render_qss("""
        Panel, Panel QWidget {
            border: none;
            background-color: $UI_COLOR_PANEL;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            color: $UI_COLOR_TEXT_MUTED;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        Panel QLabel#lineName { padding-right: 6px; }
        Panel QLabel#heading { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT; }
        Panel PLineEdit, Panel PSButton, Panel QPushButton#chip, Panel QPushButton#tool {
            background-color: $UI_COLOR_BACKGROUND;
            min-height: ${UI_CONTROL_HEIGHT_PX}px; max-height: ${UI_CONTROL_HEIGHT_PX}px;
            padding: 0 6px;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND;
        }
        Panel PLineEdit:focus { border-color: $UI_COLOR_TEXT_MUTED; }
        Panel PLineEdit[readOnly="true"], Panel PLabel {
            background-color: transparent; border-color: transparent;
        }
        Panel PSButton[state="true"], Panel PSButton[state="on"] {
            background-color: $UI_COLOR_BACKGROUND;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT;
        }
        Panel PSButton:hover, Panel QPushButton#chip:hover, Panel QPushButton#tool:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel PSButton:disabled, Panel PLineEdit:disabled { color: $UI_COLOR_BORDER; }
        Panel QPushButton#chip { padding: 2px 8px; min-height: 0; max-height: 1000px; }
        Panel QPushButton#chip:checked {
            background-color: $UI_COLOR_BACKGROUND;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT;
        }
        Panel QPushButton#chip:checked:hover, Panel PSButton[state="true"]:hover, Panel PSButton[state="on"]:hover {
            background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel QPushButton#tool {
            min-width: ${UI_CONTROL_HEIGHT_PX}px; max-width: ${UI_CONTROL_HEIGHT_PX}px; padding: 0;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        Panel QPushButton#tool[half="true"] { min-height: 0; max-height: 1000px; }
        QMenu#psmenu { border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER; padding: 4px; }
        QMenu#psmenu::item { padding: 4px 12px; border-radius: ${UI_BORDER_RADIUS_PX}px; }
        QMenu#psmenu::item:selected { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        QMenu#psmenu::item:checked { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; }
        QMenu#psmenu::indicator { width: 0; }
        Panel PTabBar { background: transparent; }
        Panel PTabBar QToolButton { background-color: $UI_COLOR_BACKGROUND; border: none; }
        Panel QPushButton#unit {
            background-color: transparent; border: none; padding: 0 4px; min-height: 0;
            color: $UI_COLOR_BORDER_LIGHT; font-weight: normal;
        }
        Panel QPushButton#unit:hover { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        Panel QPushButton#unit[static="true"]:hover { background-color: transparent; color: $UI_COLOR_BORDER_LIGHT; }
        Panel [wstate="process"] { border-color: $UI_COLOR_PROCESS; }
        Panel [wstate="error"] { border-color: $UI_COLOR_ERROR; }
    """)

    def __init__(self, table: QWidget, column_width: int | None = None, parent=None):
        """``table`` - PanelTable (будет выровнена) или любой виджет со своими таблицами."""
        super().__init__(parent)
        self.setStyleSheet(self.STYLE_SHEET)
        self.table = table
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(0)
        lay.addWidget(table)
        if isinstance(table, PanelTable):
            table.build(column_width)


class WriteTracker(QObject):
    """Собирает статусы записи выбранных ключей для индикаторов в заголовке."""

    changed = pyqtSignal(list, bool)          # [(name, WriteState, detail)], active

    def __init__(self, proxy: InstrumentProxy, keys: list[str], names: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.keys = list(keys)
        self.names = names or {}
        self._state: dict[str, tuple[WriteState, str]] = {}
        proxy.param_status.connect(self._on_status)
        proxy.state_changed.connect(self._on_conn)
        proxy.comm_error.connect(lambda m: self._set("connection", WriteState.ERROR, m))

    def _set(self, key, state, detail=""):
        self._state[key] = (state, detail)
        self.emit()

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if key not in self.keys:
            return
        state = {PStatus.WRITING: WriteState.PROCESS, PStatus.ERROR: WriteState.ERROR,
                 PStatus.INVALID: WriteState.ERROR}.get(status, WriteState.VERIFIED)
        if status is PStatus.UNKNOWN:
            self._state.pop(key, None)
            self.emit()
            return
        self._set(key, state, message)

    def _on_conn(self, state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            self._state.pop("connection", None)
        elif state is ConnectionState.DISCONNECTED:
            self._state.clear()
        self.emit()

    def emit(self) -> None:
        items = [(self.names.get(k, k), s, d) for k, (s, d) in self._state.items()]
        self.changed.emit(items, self.proxy.is_connected)
