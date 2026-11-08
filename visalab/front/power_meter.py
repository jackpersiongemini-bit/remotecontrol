"""Окно ваттметра (U2021XA - профиль power_meter), по образцу окон SDG и СВЧ генератора.

Избранное - один виджет-индикатор: основная мощность крупно справа, дополнительная (W / PEAK /
PTAV, клик - следующая) слева сверху, под ней запуск (FREE RUN / LEVEL, клик - переключить), ниже -
сколько добавляет поправка (CAL, клик - вкладка CAL).

Вкладки: MAIN (частота, усреднение, запуск), CAL (таблица калибровки по частоте, GAIN, DUTY,
обнуление и калибровка датчика), TIME - режим временной области: TRAC ON - в избранном вместо
индикатора график мощности от времени (трасса датчика после каждого измерения, строб на графике).

Поправка считается в окне и пишется в прибор как offset (CORR:GAIN2), поэтому все показания
прибора уже исправлены:  P = Pизм + CAL(f) − GAIN + 10·lg(100 / DUTY).
CAL - потери тракта на частоте датчика (линейная интерполяция по таблице), GAIN - усиление в
тракте, DUTY - скважность импульсов (вручную или с ГСС, открытого в этом же процессе).

Окно знает только InstrumentProxy и ключи профилей power_meter / function_generator.
"""
from __future__ import annotations

import bisect
import json
import math
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.params import ConnectionState, Kind
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format, si_parse
from ..core.trace import Trace
from . import instrument_window as _iw
from .instrument_window import LINKS, InstrumentWindow
from .params import IndicatorButton, PLineEdit, PNLabel, PSButton, PSpinBox, PanelLine, PanelTable, _repolish
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT,
                    UI_COLOR_TEXT, UI_COLUMN_WIDTH_PX, UI_CONTROL_HEIGHT_PX, UI_MONOSPACE_FONT_FAMILY, render_qss)

__all__ = ["PowerMeterWindow", "PowerReadout", "TracePlot", "CalTable", "saved_connection", "interpolate"]

CONNECTION_GROUP = "CONNECTION/POWER_METER"
OFFSET_LIMIT = 100.0                    # CORR:GAIN2: ±100 dB
SECONDARY = ("W", "PEAK", "PTAV")       # что показывает малое поле индикатора
DUTY_SOURCES = (("OFF", "OFF"), ("MAN", "MAN"), ("SDG1", "CH1"), ("SDG2", "CH2"))
CONFIRM_MS = 3000


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def interpolate(table: list[tuple[float, float]], freq: float | None) -> float:
    """Поправка на частоте: линейно между точками, за краями - крайняя точка, пустая таблица - 0."""
    if not table or freq is None:
        return 0.0
    pts = sorted(table)
    xs = [f for f, _ in pts]
    if freq <= xs[0]:
        return pts[0][1]
    if freq >= xs[-1]:
        return pts[-1][1]
    i = bisect.bisect_right(xs, freq)
    (f0, d0), (f1, d1) = pts[i - 1], pts[i]
    return d0 + (d1 - d0) * (freq - f0) / (f1 - f0) if f1 != f0 else d1


def _dbm_to_w(dbm: float) -> float:
    return 10 ** ((dbm - 30.0) / 10.0)


def _auto(proxy: InstrumentProxy, key: str):
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, [("OFF", False), ("ON", True)])
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


# ---------------------------------------------------------------- виджеты без прибора (настройки окна)
class LToggle(PSButton):
    """Переключатель настройки окна - вид как у PSButton, но значение хранит сам (не прибор)."""

    changed = pyqtSignal(object)

    def __init__(self, states, value=None, parent=None):
        IndicatorButton.__init__(self, self.UNKNOWN, parent)
        pairs = list(states.items()) if isinstance(states, dict) else list(states)
        self._names = tuple(n for n, _v in pairs)
        self._data = tuple(v for _n, v in pairs)
        self._value = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self.next_value)
        self.set_value(value if self._index(value) >= 0 else self._data[0], force=True)

    def set_value(self, value, force: bool = False) -> bool:
        if not force and self._index(self._value) == self._index(value) >= 0:
            return False
        self._value = value
        self.show_value(value)
        self.changed.emit(value)
        return True


class LNumber(QLineEdit):
    """Число настройки окна (dB, %): ввод как у PLineEdit - значение уходит в подсказку,
    Enter - записать, Esc - отменить, пусто - без изменений."""

    changed = pyqtSignal(float)

    def __init__(self, value: float, unit: str, minimum: float, maximum: float, digits: int = 4, parent=None):
        super().__init__(parent)
        self.setObjectName("cfg")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.unit, self.minimum, self.maximum, self.digits = unit, minimum, maximum, digits
        self._value = float(value)
        self.unit_button = QPushButton(unit, self)
        self.unit_button.setObjectName("unit")
        self.unit_button.setProperty("static", True)
        self.unit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.editingFinished.connect(self._commit)
        self.show_value(self._value)

    @property
    def value(self) -> float:
        return self._value

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        b = self.unit_button
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(2, 2, w, max(self.height() - 4, 0))
        self.setTextMargins(w, 0, 0, 0)

    def show_value(self, value: float | None) -> None:
        text = "" if value is None else f"{value:.{self.digits}g}"
        if self.hasFocus() and not self.isReadOnly():
            self.setPlaceholderText(text)
            return
        self.setText(text)
        self.setModified(False)

    def set_value(self, value: float, emit: bool = True) -> None:
        value = min(max(float(value), self.minimum), self.maximum)
        changed = value != self._value
        self._value = value
        self.show_value(value)
        if changed and emit:
            self.changed.emit(value)

    def focusInEvent(self, e):  # noqa: N802 - Qt
        super().focusInEvent(e)
        if not self.isReadOnly():
            self.setPlaceholderText(self.text())
            self.clear()
            self.setModified(False)

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        self.setPlaceholderText("")
        self.show_value(self._value)

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self.clear()
            self.setModified(False)
            self.clearFocus()
            return
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()

    def _commit(self) -> None:
        if self.isReadOnly() or not self.isModified() or not self.text().strip():
            return
        self.setModified(False)
        try:
            value = float(self.text().strip().replace(",", ".").removesuffix(self.unit).strip())
        except ValueError:
            self.show_value(self._value)
            return
        self.set_value(value)


class ActionButton(QPushButton):
    """Действие прибора. ``confirm`` - первый клик спрашивает SURE?, второй (за 3 с) выполняет."""

    def __init__(self, proxy: InstrumentProxy, name: str, text: str, tip: str, confirm: bool = False, parent=None):
        super().__init__(text, parent)
        self.setObjectName("choice")
        self.proxy, self.name, self._text, self.confirm = proxy, name, text, confirm
        self._armed = False
        self.setToolTip(tip)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._disarm)
        self.clicked.connect(self._click)
        proxy.action_done.connect(self._done)
        proxy.comm_error.connect(lambda _m: self._set_state(""))
        proxy.state_changed.connect(lambda _s: self.setEnabled(proxy.is_connected))
        self.setEnabled(proxy.is_connected)

    def _set_state(self, state: str) -> None:
        self.setProperty("wstate", state)
        _repolish(self)

    def _disarm(self) -> None:
        self._armed = False
        self.setText(self._text)

    def _click(self) -> None:
        if self.confirm and not self._armed:
            self._armed = True
            self.setText("SURE?")
            self._timer.start(CONFIRM_MS)
            return
        self._timer.stop()
        self._disarm()
        self._set_state("process")
        self.proxy.action(self.name)

    def _done(self, name: str, _result) -> None:
        if name == self.name:
            self._set_state("")


class CalTable(QWidget):
    """Таблица калибровки FREQ -> dB (правка в ячейках, «+» - строка на частоте датчика, «−» - удалить)."""

    changed = pyqtSignal()
    ROWS_VISIBLE = 5
    STYLE = render_qss("""
        QTableWidget#calTable { background-color: $UI_COLOR_BACKGROUND; color: $UI_COLOR_TEXT_MUTED; border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px; gridline-color: $UI_COLOR_BORDER; outline: none; }
        QTableWidget#calTable::item { padding: 0 4px; }
        QTableWidget#calTable::item:selected { background-color: $UI_COLOR_INVERT; color: $UI_COLOR_TEXT_INVERSE; }
        QTableWidget#calTable QHeaderView::section { background-color: $UI_COLOR_PANEL; color: $UI_COLOR_BORDER_LIGHT;
            border: none; padding: 0 4px; }
        QTableWidget#calTable QLineEdit { background-color: $UI_COLOR_BACKGROUND; color: $UI_COLOR_TEXT;
            border: 1px solid $UI_COLOR_TEXT_MUTED; padding: 0 2px; }
        QTableWidget#calTable QScrollBar:vertical { width: 6px; background: transparent; }
        QTableWidget#calTable QScrollBar::handle:vertical { background: $UI_COLOR_BORDER; border-radius: 3px; }
        QTableWidget#calTable QScrollBar::add-line, QTableWidget#calTable QScrollBar::sub-line { height: 0; }
    """)

    def __init__(self, points=(), parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("calTable")
        self.table.setStyleSheet(self.STYLE)
        self.table.setHorizontalHeaderLabels(["FREQ", "dB"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setFixedHeight(22)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setFixedHeight(22 * (self.ROWS_VISIBLE + 1) + 4)
        self.table.itemChanged.connect(self._edited)
        self._points: list[tuple[float, float]] = []
        self.add_button = InstrumentWindow._tool("+", "Add a point at the sensor frequency", lambda: None,
                                                 (UI_CONTROL_HEIGHT_PX * 2 - 4) // 2)
        self.remove_button = InstrumentWindow._tool("−", "Remove the selected point", self.remove_selected,
                                                    (UI_CONTROL_HEIGHT_PX * 2 - 4) // 2)
        tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        tl.addWidget(self.add_button)
        tl.addWidget(self.remove_button)
        tl.addStretch()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.table, 1)
        lay.addWidget(tools)
        self.set_points(points)

    @property
    def points(self) -> list[tuple[float, float]]:
        return list(self._points)

    def set_points(self, points) -> None:
        self._points = sorted((float(f), float(d)) for f, d in points)
        self._fill()

    def _fill(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._points))
        for r, (f, d) in enumerate(self._points):
            for c, text in enumerate((si_format(f, "Hz", 6), f"{d:+.3g}")):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)

    def add_point(self, freq: float, db: float | None = None) -> None:
        if db is None:
            db = interpolate(self._points, freq)
        pts = [p for p in self._points if not math.isclose(p[0], freq, rel_tol=1e-9)]
        self.set_points(pts + [(freq, db)])
        row = next(i for i, p in enumerate(self._points) if math.isclose(p[0], freq, rel_tol=1e-9))
        self.table.selectRow(row)
        self.changed.emit()

    def remove_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        self.set_points([p for i, p in enumerate(self._points) if i not in rows])
        self.changed.emit()

    def _edited(self, item: QTableWidgetItem) -> None:
        r, c = item.row(), item.column()
        f, d = self._points[r]
        text = item.text().strip().replace(",", ".")
        try:
            if c == 0:
                try:
                    f = float(text)
                except ValueError:
                    f = si_parse(text, "Hz")
                if f <= 0:
                    raise ValueError
            else:
                d = float(text.removesuffix("dB").strip())
        except (TypeError, ValueError):
            self._fill()                              # не число - вернуть как было
            return
        pts = list(self._points)
        pts[r] = (f, d)
        self.set_points(pts)
        self.changed.emit()


# ---------------------------------------------------------------- индикатор избранного
class PowerReadout(QFrame):
    """Один виджет: дополнительная величина слева сверху, запуск, поправка; основная - справа крупно."""

    trigger_clicked = pyqtSignal()
    cal_clicked = pyqtSignal()
    secondary_changed = pyqtSignal(str)

    STYLE = render_qss("""
        PowerReadout { background-color: $UI_COLOR_BACKGROUND; border: none; border-radius: ${UI_BORDER_RADIUS_PX}px; }
        PowerReadout QLabel, PowerReadout QPushButton { background: transparent; border: none; padding: 0 4px;
            border-radius: 4px; color: $UI_COLOR_TEXT_MUTED; text-align: left; min-height: 0; max-height: 1000px; }
        PowerReadout QPushButton:hover { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        PowerReadout QPushButton:disabled, PowerReadout QLabel:disabled { color: $UI_COLOR_BORDER_LIGHT; }
        PowerReadout QLabel#main { font-size: 24pt; font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT;
            padding: 0; }
        PowerReadout QLabel#mainUnit { color: $UI_COLOR_BORDER_LIGHT; padding: 0 2px 5px 2px; }
        PowerReadout QPushButton#trig[armed="true"] { color: $UI_COLOR_PROCESS; }
    """)

    def __init__(self, secondary: str = "W", parent=None):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE)
        self.secondary = secondary if secondary in SECONDARY else "W"
        self.sec_button = self._button("sec", "W / PEAK / PTAV - click to switch", self.next_secondary)
        self.trig_button = self._button("trig", "Trigger: click to switch FREE RUN / LEVEL", self.trigger_clicked)
        self.cal_button = self._button("cal", "Correction added to every reading: CAL − GAIN + DUTY",
                                       self.cal_clicked)
        self.main = QLabel("---")
        self.main.setObjectName("main")
        self.main.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.unit = QLabel("dBm")
        self.unit.setObjectName("mainUnit")
        self.unit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        lay = QGridLayout(self)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.setHorizontalSpacing(0)
        lay.setVerticalSpacing(0)
        lay.addWidget(self.sec_button, 0, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.trig_button, 1, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.cal_button, 2, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.main, 0, 1, 3, 1)
        lay.addWidget(self.unit, 0, 2, 3, 1)
        lay.setColumnStretch(1, 1)
        self.values: dict[str, Any] = {}
        self.refresh()

    def _button(self, name: str, tip: str, slot) -> QPushButton:
        b = QPushButton()
        b.setObjectName(name)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def next_secondary(self) -> None:
        self.secondary = SECONDARY[(SECONDARY.index(self.secondary) + 1) % len(SECONDARY)]
        self.secondary_changed.emit(self.secondary)
        self.refresh()

    def update_values(self, **values) -> None:
        self.values.update(values)
        self.refresh()

    def refresh(self) -> None:
        v = self.values
        power = v.get("power")
        self.main.setText("---" if power is None else f"{power:.2f}")
        if self.secondary == "W":
            sec = "--- W" if power is None else si_format(_dbm_to_w(power), "W", 4)
        elif self.secondary == "PEAK":
            peak = v.get("peak")
            sec = "PEAK " + ("---" if peak is None else f"{peak:.2f} dBm")
        else:
            ptav = v.get("ptav")
            sec = "PTAV " + ("---" if ptav is None else f"{ptav:.2f} dB")
        self.sec_button.setText(sec)
        src, level, auto, armed = v.get("source"), v.get("level"), v.get("level_auto"), v.get("armed")
        if src == "IMM":
            trig = "FREE RUN"
        elif src == "INT":
            trig = "LEVEL AUTO" if auto else ("LEVEL" if level is None else f"LEVEL {level:g} dBm")
        else:
            trig = src or "TRIG ---"
        waiting = bool(armed) and src != "IMM"
        self.trig_button.setText(trig + (" · WAIT" if waiting else ""))
        if self.trig_button.property("armed") != waiting:
            self.trig_button.setProperty("armed", waiting)
            _repolish(self.trig_button)
        cal = v.get("cal")
        self.cal_button.setText("CAL " + ("---" if cal is None else f"{cal:+.2f} dB"))



# ---------------------------------------------------------------- график временной области
class TracePlot(QWidget):
    """Мощность от времени: сетка, трасса, строб (полупрозрачная полоса), курсор под мышью."""

    DIVS_X, DIVS_Y = 10, 6
    DB_PER_DIV = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trace: Trace | None = None
        self.gate: tuple[float, float] | None = None
        self._mouse: QPointF | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_trace(self, trace: Trace | None) -> None:
        self.trace = trace if isinstance(trace, Trace) and len(trace) > 1 else None
        self.update()

    def set_gate(self, start, length) -> None:
        self.gate = (float(start), float(length)) if isinstance(start, (int, float)) and \
            isinstance(length, (int, float)) and length > 0 else None
        self.update()

    def y_range(self) -> tuple[float, float]:
        """Сверху - максимум трассы, округлённый вверх до 10 dB; шкала 10 dB / дел."""
        top = math.ceil(max(self.trace.y) / 10.0) * 10.0 if self.trace else 0.0
        return top - self.DIVS_Y * self.DB_PER_DIV, top

    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(6, 6, -6, -20)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = e.position()
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_BACKGROUND))
        p.drawRoundedRect(QRectF(self.rect()), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
        r = self._plot_rect()
        font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        font.setPointSizeF(8)
        p.setFont(font)
        grid = QPen(QColor(UI_COLOR_BORDER), 1)
        p.setPen(grid)
        for i in range(self.DIVS_X + 1):
            x = r.left() + r.width() * i / self.DIVS_X
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i in range(self.DIVS_Y + 1):
            y = r.top() + r.height() * i / self.DIVS_Y
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        muted = QColor(UI_COLOR_BORDER_LIGHT)
        t = self.trace
        if t is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO TRACE")
            return
        x0, x1 = t.x[0], t.x[-1]
        lo, hi = self.y_range()

        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.left()

        def py(y):
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        if self.gate:                                  # строб
            g0, g1 = max(px(self.gate[0]), r.left()), min(px(self.gate[0] + self.gate[1]), r.right())
            if g1 > g0:
                band = QColor(UI_COLOR_INVERT)
                band.setAlpha(40)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(band)
                p.drawRect(QRectF(g0, r.top(), g1 - g0, r.height()))
                p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(px(t.x[0]), py(t.y[0])))
        for x, y in zip(t.x[1:], t.y[1:]):
            path.lineTo(px(x), py(y))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.2))
        p.drawPath(path)
        # подписи: верх шкалы и дБ/дел слева сверху, время по краям снизу
        p.setPen(muted)
        p.drawText(QRectF(r.left() + 3, r.top() + 1, r.width(), 14), Qt.AlignmentFlag.AlignLeft,
                   f"{hi:g} dBm  {self.DB_PER_DIV:g} dB/div")
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 14)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, si_format(x0, "s", 4))
        p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter, f"{si_format((x1 - x0) / self.DIVS_X, 's', 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, si_format(x1, "s", 4))
        m = self._mouse
        if m is not None and r.contains(m):              # курсор: точка трассы под мышью
            i = min(range(len(t)), key=lambda k: abs(px(t.x[k]) - m.x()))
            cx, cy = px(t.x[i]), py(t.y[i])
            p.setPen(QPen(muted, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(UI_COLOR_TEXT))
            p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
            p.setPen(QColor(UI_COLOR_TEXT))
            p.drawText(QRectF(r.left(), r.top() + 1, r.width() - 3, 14), Qt.AlignmentFlag.AlignRight,
                       f"{si_format(t.x[i], 's', 4)}  {t.y[i]:.2f} dBm")


# ---------------------------------------------------------------- окно
#: порядок восстановления: детектор (меняет запуск) -> запуск -> остальное -> непрерывный счёт
RESTORE_RANK = {"detector": 0, "trig.source": 1, "trace.state": 3, "meas.continuous": 4}


class PowerMeterWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "u2021xa"
    PRESET_KEY = "PM_PRESETS"            # пресетов у ваттметра нет

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Power meter", parent=None):
        self._sdg: InstrumentProxy | None = None
        super().__init__(proxy, settings, title, parent)
        proxy.sync_done.connect(self.apply_correction)
        proxy.action_done.connect(lambda name, _r: name == "reset" and self.apply_correction())
        proxy.param_updated.connect(self._on_meas)
        LINKS.changed.connect(self._relink)
        self._relink()
        self._refresh_readout()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, s, pre = self.proxy, self.settings, self._prefix
        sup = proxy.supports
        self.widgets: dict[str, Any] = {}

        # избранное: один индикатор
        self.readout = PowerReadout(s.value(f"{pre}/SECONDARY", "W", type=str))
        self.readout.setMinimumWidth(2 * UI_COLUMN_WIDTH_PX + 40)
        self.readout.secondary_changed.connect(lambda v: s.setValue(f"{pre}/SECONDARY", v))
        self.readout.trigger_clicked.connect(self.toggle_trigger)
        self.readout.cal_clicked.connect(self.show_cal)
        # режим временной области (TRAC ON) - вместо индикатора график
        self.plot = TracePlot()
        self.plot.setToolTip("Power vs time after each measurement; shaded band - gate")
        self.plot.hide()
        line = PanelLine("", self.readout, self.plot)
        line._name.hide()                     # один виджет на всю ширину, без подписи
        self.top_lines = [line]

        def add(rows):
            lines = []
            for t, k, make in rows:
                if not sup(k):
                    continue
                w = make(proxy, k) if make else _auto(proxy, k)
                self.widgets.setdefault(k, w)
                if not isinstance(w, PNLabel):
                    self.saved_widgets.append(w)
                lines.append(PanelLine(t, w))
            return lines

        # MAIN
        trig = lambda p, k: PSButton(p, k, [("FREE", "IMM"), ("LEVEL", "INT")])   # noqa: E731
        main = add((("FREQ", "freq", lambda p, k: PSpinBox(p, k, ("k", "M", "G"))),
                    ("TRIG", "trig.source", trig), ("TLVL", "trig.level", None), ("TAUT", "trig.level_auto", None),
                    ("SLOP", "trig.slope", None), ("AVG", "avg.state", None), ("ACNT", "avg.count", None),
                    ("AAUT", "avg.auto", None), ("VBW", "video_bw", None), ("CONT", "meas.continuous", None)))
        self.page_specs.append(("MAIN", (), PanelTable(*main)))

        # CAL: таблица, GAIN, DUTY, итог в приборе, обнуление / калибровка датчика
        try:
            points = json.loads(s.value(f"{pre}/CAL_TABLE", "[]", type=str))
        except (TypeError, ValueError):
            points = []
        self.cal_on = LToggle([("OFF", False), ("ON", True)], s.value(f"{pre}/CAL_ON", True, type=bool))
        self.cal_value = QLabel("")
        self.cal_value.setObjectName("calValue")
        self.cal_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cal_value.setToolTip("Table value at the sensor frequency (dB)")
        self.cal_table = CalTable(points)
        self.cal_table.add_button.clicked.connect(self._add_cal_point)
        self.gain_on = LToggle([("OFF", False), ("ON", True)], s.value(f"{pre}/GAIN_ON", False, type=bool))
        self.gain = LNumber(s.value(f"{pre}/GAIN", 0.0, type=float), "dB", -100.0, 100.0)
        self.gain.setToolTip("Gain in the path (subtracted)")
        self.duty_src = LToggle(DUTY_SOURCES, s.value(f"{pre}/DUTY_SRC", "OFF", type=str))
        self.duty_src.setToolTip("DUTY: OFF / manual / from the pulse generator (SDG CH1 / CH2) in this app")
        self.duty = LNumber(s.value(f"{pre}/DUTY", 100.0, type=float), "%", 0.001, 100.0)
        self.duty.setToolTip("Pulse duty cycle: adds 10·lg(100 / DUTY)")
        for w, name in ((self.cal_on, "CAL_ON"), (self.gain_on, "GAIN_ON"), (self.duty_src, "DUTY_SRC")):
            w.changed.connect(lambda v, n=name: self._local_changed(n, v))
        for w, name in ((self.gain, "GAIN"), (self.duty, "DUTY")):
            w.changed.connect(lambda v, n=name: self._local_changed(n, v))
        self.cal_table.changed.connect(lambda: self._local_changed("CAL_TABLE", json.dumps(self.cal_table.points)))
        cal = [PanelLine("CAL", self.cal_on, self.cal_value), PanelLine("TABL", self.cal_table),
               PanelLine("GAIN", self.gain_on, self.gain), PanelLine("DUTY", self.duty_src, self.duty)]
        if sup("offset"):
            off = PNLabel(proxy, "offset")
            off.setToolTip("Total correction written to the meter (CORR:GAIN2)")
            self.widgets["offset"] = off
            cal.append(PanelLine("OFFS", off))
        acts = []
        if "zero" in proxy.actions:
            self.zero_button = ActionButton(proxy, "zero", "ZERO", "Zero the sensor: remove the signal first!",
                                            confirm=True)
            acts.append(self.zero_button)
        if "calibrate" in proxy.actions:
            acts.append(ActionButton(proxy, "calibrate", "ICAL", "Internal calibration of the sensor"))
        if acts:
            cal.append(PanelLine("SENS", *acts))
        self.page_specs.append(("CAL", ("offset.state",) if sup("offset.state") else (), PanelTable(*cal)))

        # TIME: режим временной области (график - в избранном, пока TRAC ON) и его настройки
        time_ = add((("TRAC", "trace.state", None), ("TLEN", "trace.time", None), ("TOFS", "trace.offset", None),
                     ("DET", "detector", lambda p, k: PSButton(p, k, [("AVG", "AVERAGE"), ("NORM", "NORMAL")])),
                     ("DLY", "trig.delay", None), ("GSTR", "gate.start", None), ("GLEN", "gate.length", None),
                     ("HOLD", "trig.holdoff", None), ("PEAK", "meas.peak", PNLabel), ("PTAV", "meas.ptav", PNLabel)))
        self.page_specs.append(("TIME", ("trace.state",) if sup("trace.state") else (),
                                PanelTable(*time_)))

    def restore_rank(self, key: str) -> int:
        return RESTORE_RANK.get(key, 2)

    # ------------------------------------------------------------ вкладки
    def on_param(self, key: str, value: Any) -> None:
        if key == "trace":
            self.plot.set_trace(value)
        elif key == "trace.state":
            self.show_plot(value is True)
        elif key in ("gate.start", "gate.length"):
            self.plot.set_gate(self.proxy.value("gate.start"), self.proxy.value("gate.length"))
        elif key == "freq":
            self.apply_correction()

    def show_plot(self, on: bool) -> None:
        """Избранное: график (временная область) или индикатор."""
        if self.plot.isHidden() != on:
            return
        self.readout.setVisible(not on)
        self.plot.setVisible(on)
        self._fit_height()

    def show_cal(self) -> None:
        self.set_page(1, fit=False)
        self.set_expanded(True)

    # ------------------------------------------------------------ индикатор
    def _on_meas(self, key: str, _value: Any) -> None:
        if key.startswith("meas.") or key.startswith("trig."):
            self._refresh_readout()

    def _refresh_readout(self) -> None:
        v = self.proxy.value
        self.readout.update_values(power=v("meas.power"), peak=v("meas.peak"), ptav=v("meas.ptav"),
                                   source=v("trig.source"), level=v("trig.level"),
                                   level_auto=v("trig.level_auto"), armed=v("meas.armed"),
                                   cal=self.total_correction())
        self.readout.setEnabled(self.proxy.is_connected)

    def _on_state(self, state: ConnectionState) -> None:
        super()._on_state(state)
        if hasattr(self, "readout"):
            self._refresh_readout()

    def toggle_trigger(self) -> None:
        """Клик по запуску в индикаторе: FREE RUN <-> LEVEL."""
        if self.proxy.is_connected and self.proxy.supports("trig.source"):
            self.proxy.set("trig.source", "INT" if self.proxy.value("trig.source") == "IMM" else "IMM")

    # ------------------------------------------------------------ поправка
    def _local_changed(self, name: str, value) -> None:
        self.settings.setValue(f"{self._prefix}/{name}", value)
        if name == "DUTY_SRC":
            self._relink()
        self.apply_correction()

    def duty_value(self) -> float | None:
        """Скважность, % (None - не учитывать)."""
        src = self.duty_src.value
        if src == "OFF":
            return None
        if src == "MAN":
            return self.duty.value
        if self._sdg is None:
            return None
        duty = self._sdg.value(f"{src.lower()}.duty")
        return float(duty) if isinstance(duty, (int, float)) and duty > 0 else None

    def correction_parts(self) -> dict[str, float]:
        """Слагаемые поправки, dB: CAL(f), −GAIN, 10·lg(100 / DUTY)."""
        cal = interpolate(self.cal_table.points, self.proxy.value("freq")) if self.cal_on.value else 0.0
        gain = -self.gain.value if self.gain_on.value else 0.0
        duty = self.duty_value()
        return {"cal": cal, "gain": gain, "duty": 10 * math.log10(100.0 / duty) if duty else 0.0}

    def total_correction(self) -> float:
        total = sum(self.correction_parts().values())
        return min(max(total, -OFFSET_LIMIT), OFFSET_LIMIT)

    def apply_correction(self) -> None:
        """Записать итоговую поправку в прибор (offset), если она изменилась."""
        parts = self.correction_parts()
        freq = self.proxy.value("freq")
        self.cal_value.setText(f"{parts['cal']:+.3g} dB" if self.cal_on.value else "")
        self.cal_value.setToolTip(f"Table value at {si_format(freq, 'Hz', 6)}" if freq else "")
        if self.duty_src.value in ("CH1", "CH2"):
            self.duty.setReadOnly(True)
            duty = self.duty_value()
            self.duty.show_value(duty)
            self.duty.setPlaceholderText("" if duty is not None else "no SDG")
        else:
            self.duty.setReadOnly(False)
            self.duty.setPlaceholderText("")
            self.duty.show_value(self.duty.value)
        total = round(self.total_correction(), 3)
        self._refresh_readout()
        p = self.proxy
        if not p.is_connected or self._resetting:
            return
        if p.supports("offset"):
            cur = p.value("offset")
            if cur is None or abs(cur - total) > 5e-4:
                p.set("offset", total)
        if p.supports("offset.state"):
            on = abs(total) > 5e-4
            if p.value("offset.state") is not on:
                p.set("offset.state", on)

    # ------------------------------------------------------------ связь с ГСС (скважность)
    def _relink(self) -> None:
        sdg = LINKS.find("function_generator") if self.duty_src.value in ("CH1", "CH2") else None
        if sdg is self._sdg:
            return
        if self._sdg is not None:
            try:
                self._sdg.param_updated.disconnect(self._on_sdg_param)
            except TypeError:
                pass
        self._sdg = sdg
        if sdg is not None:
            sdg.param_updated.connect(self._on_sdg_param)
        self.apply_correction()

    def _on_sdg_param(self, key: str, _value: Any) -> None:
        if key.endswith(".duty"):
            self.apply_correction()

    def _add_cal_point(self) -> None:
        freq = self.proxy.value("freq")
        self.cal_table.add_point(float(freq) if freq else 1e9)

    def closeEvent(self, event):  # noqa: N802 - Qt
        try:
            LINKS.changed.disconnect(self._relink)
        except TypeError:
            pass
        if self._sdg is not None:
            try:
                self._sdg.param_updated.disconnect(self._on_sdg_param)
            except TypeError:
                pass
            self._sdg = None
        super().closeEvent(event)
