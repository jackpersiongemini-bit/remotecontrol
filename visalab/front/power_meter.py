"""Окно ваттметра (U2021XA - профиль power_meter), по образцу окон SDG и СВЧ генератора.

Избранное - один виджет-индикатор: основная мощность крупно справа, дополнительная (W / PEAK /
PTAV, клик - следующая) слева сверху, под ней запуск (FREE RUN / LEVEL / EXT, клик - следующий), ниже -
сколько добавляет поправка (CAL, клик - вкладка CAL).

Вкладки: MAIN (частота, усреднение, запуск), CAL (таблица калибровки по частоте, GAIN, DUTY,
обнуление и калибровка датчика), TIME - режим временной области: TRAC ON - в избранном вместо
индикатора график мощности от времени (трасса датчика после каждого измерения, строб на графике).

Поправка считается в окне и пишется в прибор как offset (CORR:GAIN2), поэтому все показания
прибора уже исправлены:  P = Pизм + CAL(f) − GAIN + 10·lg(100 / DUTY).
CAL - потери тракта на частоте датчика (линейная интерполяция по таблице), GAIN - усиление в
тракте, DUTY - скважность импульсов (вручную или с ГСС, открытого в этом же процессе).

ACAL (вкладка CAL) - автокалибровка через координатор (``visalab.coord``, сценарий AutoCalibration):
СВЧ генератор перебирает START..STOP по PTS точкам, ваттметр меряет без поправки, GAIN = средний
коэффициент передачи тракта, таблица ``calibrations/ACAL_<start>-<stop>_<pts>.csv`` - неравномерность;
после неё исправленное показание равно уровню генератора. Пока идёт ACAL, окно не пишет поправку в
прибор, кнопка ▶ - стоп (■), в индикаторе вместо CAL - «ACAL n/N».

Окно знает только координатор (прибор - Instrument) и ключи профилей power_meter / function_generator.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from ..coord import COORD, AutoCalibration, Instrument, ScenarioError, acal_file_name, interpolate, load_csv, save_csv
from ..core.params import ConnectionState, Kind
from ..core.units import SI_PREFIXES, si_format, si_parse
from ..core.trace import Trace
from . import instrument_window as _iw
from .instrument_window import WINDOW_EVENTS, InstrumentWindow
from .live_plot import decimate_trace_polygon, nearest_index, nice_range
from .params import IndicatorButton, PChoice, PLineEdit, PNLabel, PSButton, PSpinBox, PanelLine, PanelTable, _repolish
from .plot_tools import ANALYSIS_KEYS, PlotSettingsPopup, analyze_trace, format_analysis, trace_diff
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT,
                    UI_COLOR_TEXT, UI_COLUMN_WIDTH_PX, UI_CONTROL_HEIGHT_PX, UI_MONOSPACE_FONT_FAMILY, render_qss)
from .window import mark_growing

__all__ = ["PowerMeterWindow", "PowerReadout", "TracePlot", "CalPlot", "load_csv", "save_csv", "saved_connection",
           "interpolate", "pm_time_scale"]

CONNECTION_GROUP = "CONNECTION/POWER_METER"
OFFSET_LIMIT = 100.0                    # CORR:GAIN2: ±100 dB
SECONDARY = ("W", "PEAK", "PTAV")       # что показывает малое поле индикатора
DUTY_SOURCES = (("OFF", "OFF"), ("MAN", "MAN"), ("SDG1", "CH1"), ("SDG2", "CH2"))
CONFIRM_MS = 3000
ACAL_TIP = ("Run auto calibration: the generator steps the frequency, the meter measures, GAIN and the table "
            "are fitted (the generator level is the reference)")


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _dbm_to_w(dbm: float) -> float:
    if not math.isfinite(dbm) or abs(dbm) > 300.0:
        return math.nan
    return 10.0 ** ((dbm - 30.0) / 10.0)


def _parse_finite(v, positive: bool = False) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if (math.isfinite(f) and (not positive or f > 0)) else None
    except (TypeError, ValueError):
        return None


def _trace_auto_range(trace: Trace | None, unit: str, divs_y: int, db_per_div: float) -> tuple[float, float]:
    """AUTO-шкала TIME: dBm - сверху максимум, округлённый вверх до 10 dB; W - от 0 с шагом 1/2/5."""
    finite = [y for y in trace.y if math.isfinite(y) and abs(y) <= 300.0] if (trace is not None and len(trace.y)) else []
    top_raw = max(finite) if finite else None
    y_unit = "W" if unit == "W" and trace is not None and trace.y_unit == "dBm" else (
        trace.y_unit if trace is not None else "")
    if y_unit == "W":
        top = (_dbm_to_w(top_raw) if unit == "W" and trace is not None and trace.y_unit == "dBm" else top_raw) \
            if top_raw is not None else 1e-3
        if not math.isfinite(top) or top <= 0:
            top = 1e-3
        raw = top / divs_y
        mag = 10.0 ** math.floor(math.log10(raw))
        step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw * (1 - 1e-9))
        return 0.0, step * divs_y
    top = math.ceil(top_raw / 10.0) * 10.0 if top_raw is not None else 0.0
    return top - divs_y * db_per_div, top


def pm_time_scale(proxy, trace: Trace, settings: QSettings | None = None) -> tuple[tuple[float, float, float], int, str, Any]:
    """Шкала Y графика TIME ваттметра: ``((lo, hi, step), divs_y, y_unit, y_transform)`` - как в окне прибора."""
    win = _iw.window_for(proxy)
    plot = getattr(win, "plot", None) if win is not None else None
    if isinstance(plot, TracePlot):
        unit, auto, ref, per_div = plot.unit, plot.auto, plot.ref, plot.per_div
    else:
        unit, auto, ref, per_div = "dBm", True, None, None
        if settings is not None:
            pre = re.sub(r"\W+", "_", getattr(proxy, "address", "") or "").strip("_").upper() or PowerMeterWindow.DEFAULT_KIND.upper()
            auto = settings.value(f"{pre}/TIME_AUTO", True, type=bool)
            unit = "W" if settings.value(f"{pre}/TIME_UNIT", "dBm", type=str) == "W" else "dBm"
            tag = "W" if unit == "W" else "DBM"
            ref = _parse_finite(settings.value(f"{pre}/TIME_REF_{tag}", None))
            per_div = _parse_finite(settings.value(f"{pre}/TIME_DIV_{tag}", None), positive=True)
    divs = TracePlot.DIVS_Y
    to_w = unit == "W" and trace.y_unit == "dBm"
    y_unit = "W" if to_w else trace.y_unit
    y_transform = _dbm_to_w if to_w else None
    if not auto and ref is not None and per_div:
        lo, hi = ref - divs * per_div, ref
    else:
        lo, hi = _trace_auto_range(trace, unit, divs, TracePlot.DB_PER_DIV)
    return (lo, hi, (hi - lo) / divs), divs, y_unit, y_transform


def _auto(proxy: Instrument, key: str):
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

    def __init__(self, value: float | None, unit: str, minimum: float, maximum: float, digits: int = 4,
                 scale: float = 1.0, si_unit: str = "", hint: str = "", parent=None):
        """``scale`` - показ в единицах unit (GHz: 1e9, значение в Hz); ``si_unit`` - можно ввести
        с префиксом («2.4G»); ``hint`` - подсказка в пустом поле."""
        super().__init__(parent)
        self.setObjectName("cfg")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.unit, self.minimum, self.maximum, self.digits = unit, minimum, maximum, digits
        self.scale, self.si_unit, self.hint = scale, si_unit, hint
        self._value = None if value is None else float(value)
        self.unit_button = QPushButton(unit, self)
        self.unit_button.setObjectName("unit")
        self.unit_button.setProperty("static", True)
        self.unit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.unit_button.setToolTip(f"Unit: {unit}" if unit else "")
        self.editingFinished.connect(self._commit)
        self.show_value(self._value)

    @property
    def value(self) -> float | None:
        return self._value

    def set_unit(self, unit: str, minimum: float, maximum: float, scale: float = 1.0, si_unit: str = "") -> None:
        """Другая единица поля (шкала TIME: dBm / mW)."""
        self.unit, self.minimum, self.maximum, self.scale, self.si_unit = unit, minimum, maximum, scale, si_unit
        self.unit_button.setText(unit)
        self.unit_button.setToolTip(f"Unit: {unit}" if unit else "")
        self.resizeEvent(None)
        self.show_value(self._value)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        if e is not None:
            super().resizeEvent(e)
        b = self.unit_button
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(2, 2, w, max(self.height() - 4, 0))
        self.setTextMargins(w if not b.isHidden() else 0, 0, 0, 0)

    def _update_unit(self, value) -> None:
        """Пустое поле с подсказкой - без единицы (подсказке нужно место); при вводе единица видна."""
        show = not (self.hint and value is None and not self.hasFocus())
        # подсказка пустого поля - по центру, число - справа
        self.setAlignment((Qt.AlignmentFlag.AlignRight if show else Qt.AlignmentFlag.AlignHCenter)
                          | Qt.AlignmentFlag.AlignVCenter)
        if self.unit_button.isVisibleTo(self) != show or self.unit_button.isHidden() == show:
            self.unit_button.setVisible(show)
            w = max(self.unit_button.sizeHint().width(), 26) if show else 0
            self.setTextMargins(w, 0, 0, 0)

    def show_value(self, value: float | None) -> None:
        self._update_unit(value)
        text = "" if value is None else f"{value / self.scale:.{self.digits}g}"
        if self.hasFocus() and not self.isReadOnly():
            self.setPlaceholderText(text or self.hint)
            return
        self.setText(text)
        self.setPlaceholderText(self.hint if value is None else "")
        self.setModified(False)

    def clear_value(self) -> None:
        self._value = None
        self.show_value(None)

    def set_value(self, value: float, emit: bool = True) -> None:
        v = float(value)
        if not math.isfinite(v):
            self.show_value(self._value)
            return
        value = min(max(v, self.minimum), self.maximum)
        changed = value != self._value
        self._value = value
        self.show_value(value)
        if changed and emit:
            self.changed.emit(value)

    def focusInEvent(self, e):  # noqa: N802 - Qt
        super().focusInEvent(e)
        self._update_unit(self._value)
        if not self.isReadOnly():
            self.setPlaceholderText(self.text())
            self.clear()
            self.setModified(False)

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
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
        value = self.parse(self.text())
        if value is None:
            self.show_value(self._value)
            return
        self.set_value(value)

    def parse(self, text: str) -> float | None:
        text = text.strip().replace(",", ".").removesuffix(self.unit).strip()
        try:
            v = float(text) * self.scale
            if math.isfinite(v):
                return v
        except ValueError:
            pass
        if self.si_unit:
            try:
                v = si_parse(text, self.si_unit)
                if math.isfinite(v):
                    return v
            except (TypeError, ValueError):
                pass
        return None


class ActionButton(QPushButton):
    """Действие прибора. ``confirm`` - первый клик спрашивает SURE?, второй (за 3 с) выполняет."""

    def __init__(self, proxy: Instrument, name: str, text: str, tip: str, confirm: bool = False, parent=None):
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
        proxy.comm_error.connect(self._clear_state)
        proxy.refused.connect(self._clear_state)          # прибор ведёт сценарий: не отправлено
        proxy.state_changed.connect(self._on_state)
        self.setEnabled(proxy.is_connected)

    def _clear_state(self, *_a) -> None:
        if not sip.isdeleted(self):
            self._set_state("")

    def _on_state(self, _s) -> None:
        if sip.isdeleted(self):
            return
        if not self.proxy.is_connected:
            self._timer.stop()
            self._disarm()
            self._set_state("")
        self.setEnabled(self.proxy.is_connected)

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
        if not sip.isdeleted(self) and name == self.name:
            self._set_state("")


# ---------------------------------------------------------------- индикатор избранного
class PowerReadout(QFrame):
    """Один виджет: дополнительная величина слева сверху, запуск, поправка; основная - справа крупно."""

    trigger_clicked = pyqtSignal()
    cal_clicked = pyqtSignal()
    secondary_changed = pyqtSignal(str)
    main_unit_changed = pyqtSignal(str)          # основная величина: "dBm" / "W"

    STYLE = render_qss("""
        PowerReadout { background-color: $UI_COLOR_BACKGROUND; border: none; border-radius: ${UI_BORDER_RADIUS_PX}px; }
        PowerReadout QLabel, PowerReadout QPushButton { background: transparent; border: none; padding: 0 4px;
            border-radius: 4px; color: $UI_COLOR_TEXT_MUTED; text-align: left; min-height: 0; max-height: 1000px; }
        PowerReadout QPushButton:hover { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        PowerReadout QPushButton:disabled, PowerReadout QLabel:disabled { color: $UI_COLOR_BORDER_LIGHT; }
        PowerReadout QLabel#main { font-size: 24pt; font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT;
            padding: 0; }
        PowerReadout QLabel#mainUnit { color: $UI_COLOR_BORDER_LIGHT; padding: 0 2px 5px 2px; }
    """)

    def __init__(self, secondary: str = "W", main_unit: str = "dBm", parent=None):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE)
        self.secondary = secondary if secondary in SECONDARY else "W"
        self.main_unit = main_unit if main_unit in ("dBm", "W") else "dBm"
        self.sec_button = self._button("sec", "W / PEAK / PTAV - click to switch", self.next_secondary)
        self.trig_button = self._button("trig", "Trigger: click to switch FREE RUN / LEVEL / EXT", self.trigger_clicked)
        self.cal_button = self._button("cal", "Correction added to every reading: CAL − GAIN + DUTY",
                                       self.cal_clicked)
        self.main = QLabel("---")
        self.main.setObjectName("main")
        self.main.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.unit = QLabel("dBm")
        self.unit.setObjectName("mainUnit")
        self.unit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        for w in (self.main, self.unit):                 # клик по мощности - dBm <-> W
            w.setCursor(Qt.CursorShape.PointingHandCursor)
            w.setToolTip("Click to switch dBm / W")
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

    def mousePressEvent(self, e):  # noqa: N802 - Qt
        if any(w.geometry().contains(e.position().toPoint()) for w in (self.main, self.unit)):
            self.toggle_main_unit()
            e.accept()
            return
        super().mousePressEvent(e)

    def toggle_main_unit(self) -> None:
        self.main_unit = "W" if self.main_unit == "dBm" else "dBm"
        self.main_unit_changed.emit(self.main_unit)
        self.refresh()

    def next_secondary(self) -> None:
        self.secondary = SECONDARY[(SECONDARY.index(self.secondary) + 1) % len(SECONDARY)]
        self.secondary_changed.emit(self.secondary)
        self.refresh()

    def update_values(self, **values) -> None:
        self.values.update(values)
        self.refresh()

    def refresh(self) -> None:
        v = self.values
        raw_power = v.get("power")
        power = raw_power if (isinstance(raw_power, (int, float)) and not isinstance(raw_power, bool)
                              and math.isfinite(raw_power) and abs(raw_power) <= 300.0) else None
        if self.main_unit == "W":                      # «13.74 µW»: число крупно, единица с префиксом
            num, unit = "---", "W"
            if power is not None:
                w = _dbm_to_w(power)
                if math.isfinite(w):
                    unit = si_format(w, "W", 4).partition(" ")[2] or "W"        # префикс: µW, mW...
                    num = f"{w / SI_PREFIXES.get(unit[:-1], 1.0) if unit[:-1] else w:#.4g}".rstrip(".")
            self.main.setText(num)
            self.unit.setText(unit or "W")
        else:
            self.main.setText("---" if power is None else f"{power:.2f}")
            self.unit.setText("dBm")
        if self.secondary == "W":                      # малое поле - другая единица основной величины
            if self.main_unit == "W":
                sec = "--- dBm" if power is None else f"{power:.2f} dBm"
            else:
                w = _dbm_to_w(power) if power is not None else math.nan
                sec = "--- W" if not math.isfinite(w) else si_format(w, "W", 4)
        elif self.secondary == "PEAK":
            peak = v.get("peak")
            sec = "PEAK " + ("---" if peak is None else f"{peak:.2f} dBm")
        else:
            ptav = v.get("ptav")
            sec = "PTAV " + ("---" if ptav is None else f"{ptav:.2f} dB")
        self.sec_button.setText(sec)
        src, level, auto = v.get("source"), v.get("level"), v.get("level_auto")
        if src == "IMM":
            trig = "FREE RUN"
        elif src == "INT":
            trig = "LEVEL AUTO" if auto else ("LEVEL" if level is None else f"LEVEL {level:g} dBm")
        else:
            trig = src or "TRIG ---"
        self.trig_button.setText(trig)
        cal, acal = v.get("cal"), v.get("acal")
        self.cal_button.setText(acal or "CAL " + ("---" if cal is None else f"{cal:+.2f} dB"))



# ---------------------------------------------------------------- график временной области
class TracePlot(QWidget):
    """Мощность от времени: сетка, трасса, строб, курсор, память MEM (2× клик), анализ и настройки шкалы (ПКМ)."""

    DIVS_X, DIVS_Y = 10, 6
    DB_PER_DIV = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trace: Trace | None = None
        self.gate: tuple[float, float] | None = None
        #: шкала Y (вкладка TIME, Алексей 2026-11-14): единица dBm / W; AUTO или верх (REF) и цена деления
        self.unit = "dBm"
        self.auto = True
        self.ref: float | None = None
        self.per_div: float | None = None
        self.show_grid = True
        self.memory: Trace | None = None
        self.memory_visible = True
        self.memory_diff = False
        self.analysis_metrics: list[str] = []
        self._popup: PlotSettingsPopup | None = None
        self._mouse: QPointF | None = None
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self._font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        self._font.setPointSizeF(8)
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        mark_growing(self)                        # растянули окно - график выше
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_trace(self, trace: Trace | None) -> None:
        nxt = trace if isinstance(trace, Trace) and len(trace) > 1 else None
        if nxt is self.trace:
            return
        self.trace = nxt
        self.update()

    def set_gate(self, start, length) -> None:
        gate = (float(start), float(length)) if isinstance(start, (int, float)) and \
            isinstance(length, (int, float)) and length > 0 else None
        if gate == self.gate:
            return
        self.gate = gate
        self.update()

    def marker(self) -> float | None:
        """X вертикальной метки (у графика калибровки - частота датчика)."""
        return None

    def set_scale(self, unit: str = "dBm", auto: bool = True, ref: float | None = None,
                  per_div: float | None = None) -> None:
        """Шкала Y: ``unit`` dBm (лог.) или W (линейная); вручную - верх ``ref`` и ``per_div`` в этой единице."""
        self.unit = "W" if unit == "W" else "dBm"
        self.auto = bool(auto) or ref is None or not per_div
        self.ref, self.per_div = ref, per_div
        self.update()

    # ------------------------------------------------------------ единый интерфейс PlotSettingsPopup
    def set_manual_scale(self, ref: float, per_div: float, _divs_y: int | None = None) -> None:
        if math.isfinite(ref) and math.isfinite(per_div) and per_div > 0:
            self.set_scale(self.unit, False, float(ref), float(per_div))

    def set_auto_scale(self) -> None:
        self.set_scale(self.unit, True, self.ref, self.per_div)

    def autoscale_once(self) -> None:
        t, yt = self._effective_trace()
        if t is None:
            return
        vals = [yt(y) if yt is not None else y for y in t.y if math.isfinite(y)]
        vals = [v for v in vals if math.isfinite(v)]
        if not vals:
            return
        _lo, hi, step = nice_range(min(vals), max(vals), self.DIVS_Y)
        self.set_manual_scale(hi, step)

    def set_x_zoom(self, _x0, _x1) -> None:
        pass

    def reset_view(self) -> None:
        self.set_auto_scale()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = bool(on)
        self.update()

    def set_show_points(self, _on: bool) -> None:
        pass

    def store_memory(self) -> bool:
        if self.trace is None:
            return False
        self.memory = self.trace
        self.memory_visible = True
        self.update()
        return True

    def clear_memory(self) -> None:
        self.memory = None
        self.memory_diff = False
        self.update()

    def toggle_memory(self) -> bool:
        if self.memory is not None:
            self.clear_memory()
            return False
        return self.store_memory()

    def set_memory_visible(self, on: bool) -> None:
        self.memory_visible = bool(on)
        self.update()

    def set_memory_diff(self, on: bool) -> None:
        self.memory_diff = bool(on) and self.memory is not None
        self.update()

    def set_analysis_metrics(self, metrics: list[str] | tuple[str, ...]) -> None:
        self.analysis_metrics = [k for k in ANALYSIS_KEYS if k in set(metrics)]
        self.update()

    def toggle_analysis_metric(self, key: str, on: bool) -> None:
        cur = set(self.analysis_metrics)
        if on:
            cur.add(key)
        else:
            cur.discard(key)
        self.set_analysis_metrics(list(cur))

    def open_settings(self, global_pos: QPoint | None = None) -> PlotSettingsPopup:
        if self._popup is not None:
            self._popup.close()
        self._popup = PlotSettingsPopup(self, self)
        pos = global_pos if global_pos is not None else self.mapToGlobal(QPoint(self.width() - 280, 20))
        self._popup.show_at(pos)
        return self._popup

    def plot_settings_state(self) -> dict:
        lo, hi = self.y_range()
        div = (hi - lo) / self.DIVS_Y if hi > lo else self.DB_PER_DIV
        t, _yt = self._effective_trace()
        return {
            "manual_scale": not self.auto,
            "ref": hi,
            "div": div,
            "divs_y": self.DIVS_Y,
            "y_unit": self.y_unit(),
            "x_unit": t.x_unit if t is not None else "",
            "x_bounds": (t.x[0], t.x[-1]) if t is not None and len(t.x) else (0.0, 1.0),
            "manual_x_range": None,
            "show_grid": self.show_grid,
            "show_points": False,
            "has_memory": self.memory is not None,
            "memory_count": 1 if self.memory is not None else 0,
            "memory_visible": self.memory_visible,
            "memory_diff": self.memory_diff,
            "analysis_metrics": list(self.analysis_metrics),
            "supports_divs": False,
            "supports_x_zoom": False,
            "supports_points": False,
        }

    def _effective_trace(self) -> tuple[Trace | None, Any]:
        if self.trace is None:
            return None, None
        yt = _dbm_to_w if (self.unit == "W" and self.trace.y_unit == "dBm") else None
        if self.memory_diff and self.memory is not None:
            return trace_diff(self.trace, self.memory, y_transform=yt), None
        return self.trace, yt

    def ys(self) -> list[float]:
        """Значения трассы в единице шкалы (W - из dBm)."""
        if self.unit == "W" and self.trace is not None and self.trace.y_unit == "dBm":
            return [_dbm_to_w(y) for y in self.trace.y]
        return list(self.trace.y) if self.trace is not None else []

    def y_unit(self) -> str:
        if self.memory_diff and self.memory is not None and self.trace is not None:
            return "W" if (self.unit == "W" and self.trace.y_unit == "dBm") else (
                "dB" if self.trace.y_unit in ("dBm", "dB") else self.trace.y_unit
            )
        return "W" if self.unit == "W" and self.trace is not None and self.trace.y_unit == "dBm" else \
            (self.trace.y_unit if self.trace is not None else "")

    def auto_range(self) -> tuple[float, float]:
        """AUTO: dBm - сверху максимум, округлённый вверх до 10 dB, 10 dB / дел; W - от 0 до максимума
        с круглой ценой деления (1, 2, 5)."""
        t, _yt = self._effective_trace()
        if self.memory_diff and self.memory is not None and t is not None:
            vals = [y for y in t.y if math.isfinite(y)]
            if vals:
                lo, hi, _step = nice_range(min(vals), max(vals), self.DIVS_Y)
                return lo, hi
        return _trace_auto_range(self.trace, self.unit, self.DIVS_Y, self.DB_PER_DIV)

    def y_range(self) -> tuple[float, float]:
        if not self.auto and self.ref is not None and self.per_div:
            return self.ref - self.DIVS_Y * self.per_div, self.ref
        return self.auto_range()

    def _y_text(self, v: float, digits: int = 4) -> str:
        return si_format(v, "W", digits) if self.y_unit() == "W" else f"{v:g} {self.y_unit()}"

    def analysis_lines(self) -> list[str]:
        if not self.analysis_metrics:
            return []
        t, yt = self._effective_trace()
        if t is None:
            return []
        stats = analyze_trace(t, y_transform=yt)
        tokens = format_analysis(stats, self.analysis_metrics, t.x_unit, self.y_unit())
        if not tokens:
            return []
        fm = QFontMetrics(self._font)
        max_w = max(120, self.width() - 16)
        lines: list[str] = []
        cur = ""
        for tok in tokens:
            cand = f"{cur}  ·  {tok}" if cur else tok
            if cur and fm.horizontalAdvance(cand) > max_w:
                lines.append(cur)
                cur = tok
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    def _analysis_height(self, lines: list[str] | None = None) -> int:
        if lines is None:
            lines = self.analysis_lines()
        return (len(lines) * 14 + 4) if lines else 0

    def _plot_rect(self, analysis_h: int | None = None) -> QRectF:
        ah = self._analysis_height() if analysis_h is None else analysis_h
        return QRectF(self.rect()).adjusted(6, 6, -6, -(20 + ah))

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = e.position()
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self.update()

    def mouseDoubleClickEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton:
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.store_memory()
            else:
                self.toggle_memory()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def wheelEvent(self, e):  # noqa: N802 - Qt
        delta = e.angleDelta().y()
        if delta == 0 or self.trace is None:
            super().wheelEvent(e)
            return
        lo, hi = self.y_range()
        step = (hi - lo) / self.DIVS_Y
        if step <= 0:
            return
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            shift = step * 0.5 * (1 if delta > 0 else -1)
            self.set_manual_scale(hi + shift, step)
        else:
            factor = 0.8 if delta > 0 else 1.25
            new_step = max(1e-12, step * factor)
            center = (lo + hi) * 0.5
            self.set_manual_scale(center + new_step * self.DIVS_Y * 0.5, new_step)
        e.accept()

    def contextMenuEvent(self, e):  # noqa: N802 - Qt
        self.open_settings(e.globalPos())
        e.accept()

    def _render_base(self, mk: float | None) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        if not p.isActive():
            return pm
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_BACKGROUND))
        p.drawRoundedRect(QRectF(self.rect()), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
        an_lines = self.analysis_lines()
        r = self._plot_rect(self._analysis_height(an_lines))
        p.setFont(self._font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        grid_pen = QPen(QColor(UI_COLOR_BORDER), 1)
        grid_pen.setCosmetic(True)
        p.setPen(grid_pen)
        if self.show_grid:
            for i in range(self.DIVS_X + 1):
                x = r.left() + r.width() * i / self.DIVS_X
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            for i in range(self.DIVS_Y + 1):
                y = r.top() + r.height() * i / self.DIVS_Y
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        else:
            p.drawRect(r)
        muted = QColor(UI_COLOR_BORDER_LIGHT)
        t, y_transform = self._effective_trace()
        if t is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO TRACE")
            p.end()
            return pm
        x0, x1 = t.x[0], t.x[-1]
        lo, hi = self.y_range()

        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.left()

        if self.gate:                                  # строб
            g0, g1 = max(px(self.gate[0]), r.left()), min(px(self.gate[0] + self.gate[1]), r.right())
            if g1 > g0:
                band = QColor(UI_COLOR_INVERT)
                band.setAlpha(40)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(band)
                p.drawRect(QRectF(g0, r.top(), g1 - g0, r.height()))
                p.setBrush(Qt.BrushStyle.NoBrush)
        p.save()
        p.setClipRect(r.adjusted(-2, -2, 2, 2))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.memory is not None and self.memory_visible and not self.memory_diff:
            mem_yt = _dbm_to_w if (self.unit == "W" and self.memory.y_unit == "dBm") else None
            mpoly = decimate_trace_polygon(self.memory, x0, x1, lo, hi, r, y_transform=mem_yt)
            if len(mpoly) > 1:
                mc = QColor(UI_COLOR_TEXT)
                mc.setAlpha(115)
                mpen = QPen(mc, 1, Qt.PenStyle.DashLine)
                mpen.setCosmetic(True)
                p.setPen(mpen)
                p.drawPolyline(mpoly)
        poly = decimate_trace_polygon(t, x0, x1, lo, hi, r, y_transform=y_transform)
        if len(poly) > 1:
            pen = QPen(QColor(UI_COLOR_TEXT), 1)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawPolyline(poly)
        p.restore()
        # подписи: верх шкалы и дБ/дел слева сверху, X по краям снизу
        xu, yu = t.x_unit, self.y_unit()
        p.setPen(muted)
        div = (hi - lo) / self.DIVS_Y
        div_text = si_format(div, "W", 3) if yu == "W" else f"{div:g} {'dB' if yu == 'dBm' else yu}"
        mem_tag = "  [DATA−MEM]" if (self.memory is not None and self.memory_diff) else (
            "  [MEM]" if (self.memory is not None and self.memory_visible) else ""
        )
        p.drawText(QRectF(r.left() + 3, r.top() + 1, r.width(), 14), Qt.AlignmentFlag.AlignLeft,
                   f"{self._y_text(hi)}  {div_text}/div{'' if self.auto else '  MAN'}{mem_tag}")
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 14)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, si_format(x0, xu, 4))
        p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter, f"{si_format((x1 - x0) / self.DIVS_X, xu, 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, si_format(x1, xu, 4))
        if an_lines:
            p.setPen(QColor(UI_COLOR_TEXT))
            y_an = r.bottom() + 19
            for ln in an_lines:
                p.drawText(QRectF(r.left() + 2, y_an, r.width() - 4, 14), Qt.AlignmentFlag.AlignLeft, ln)
                y_an += 14
        if mk is not None and x0 <= mk <= x1:            # метка: частота датчика
            p.setPen(QPen(QColor(UI_COLOR_INVERT), 1))
            p.drawLine(QPointF(px(mk), r.top()), QPointF(px(mk), r.bottom()))
        p.end()
        return pm

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.width() <= 0 or self.height() <= 0:
            return
        mk = self.marker()
        key = (self.width(), self.height(), self.devicePixelRatioF(), id(self.trace),
               id(self.memory), self.memory_visible, self.memory_diff, self.show_grid,
               tuple(self.analysis_metrics),
               self.gate, self.unit, self.auto, self.ref, self.per_div, mk)
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_base(mk)
            self._cache_key = key
        p = QPainter(self)
        if not p.isActive():
            return
        p.drawPixmap(0, 0, self._cache)
        t, y_transform = self._effective_trace()
        m = self._mouse
        if t is None or m is None:
            return
        r = self._plot_rect()
        if r.contains(m):                              # курсор: точка трассы под мышью
            x0, x1 = t.x[0], t.x[-1]
            lo, hi = self.y_range()
            x_target = x0 + (m.x() - r.left()) / r.width() * (x1 - x0) if x1 != x0 and r.width() > 0 else x0
            i = nearest_index(t, x_target)
            yv = y_transform(t.y[i]) if y_transform is not None else t.y[i]
            if not math.isfinite(yv):
                return
            cx = r.left() + (t.x[i] - x0) / (x1 - x0) * r.width() if x1 != x0 else r.left()
            cy = r.bottom() - (min(max(yv, lo), hi) - lo) / (hi - lo) * r.height()
            muted = QColor(UI_COLOR_BORDER_LIGHT)
            p.setFont(self._font)
            p.setPen(QPen(muted, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(UI_COLOR_TEXT))
            p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
            p.setPen(QColor(UI_COLOR_TEXT))
            p.drawText(QRectF(r.left(), r.top() + 1, r.width() - 3, 14), Qt.AlignmentFlag.AlignRight,
                       f"{si_format(t.x[i], t.x_unit, 4)}  " + (si_format(yv, "W", 4) if self.y_unit() == "W"
                                                                else f"{yv:.2f} {self.y_unit()}"))


class CalPlot(TracePlot):
    """Поправка от частоты (таблица калибровки): шкала по данным, метка - частота датчика."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.freq: float | None = None

    def set_points(self, points) -> None:
        pts = sorted(points)
        if len(pts) == 1:                              # одна точка - горизонтальная линия
            pts = [(pts[0][0] * 0.999, pts[0][1]), (pts[0][0] * 1.001, pts[0][1])]
        self.set_trace(Trace(tuple(f for f, _ in pts), tuple(d for _, d in pts), "Hz", "dB", "cal")
                       if len(pts) > 1 else None)

    def set_freq(self, freq) -> None:
        self.freq = float(freq) if isinstance(freq, (int, float)) else None
        self.update()

    def marker(self) -> float | None:
        return self.freq

    def y_range(self) -> tuple[float, float]:
        """Шкала по данным (или ручная, если пользователь задал MAN через ПКМ)."""
        if not self.auto and self.ref is not None and self.per_div:
            return self.ref - self.DIVS_Y * self.per_div, self.ref
        t, _yt = self._effective_trace()
        if t is None or not len(t.y):
            return 0.0, float(self.DIVS_Y)
        finite = [y for y in t.y if math.isfinite(y)]
        if not finite:
            return 0.0, float(self.DIVS_Y)
        lo, hi = min(finite), max(finite)
        for step in (st * 10 ** e for e in range(-1, 4) for st in (1, 2, 5)):
            full = step * self.DIVS_Y
            if full < (hi - lo) * 1.1:
                continue
            bottom = math.floor((lo - (full - (hi - lo)) / 2) / step + 1e-9) * step
            if bottom + full >= hi:
                return bottom, bottom + full
        return lo, hi + 1.0


# ---------------------------------------------------------------- окно
#: порядок восстановления: детектор (меняет запуск) -> запуск -> остальное -> непрерывный счёт
RESTORE_RANK = {"detector": 0, "trig.source": 1, "trace.state": 3, "meas.continuous": 4}


class PowerMeterWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "u2021xa"
    PRESET_KEY = "PM_PRESETS"            # пресетов у ваттметра нет

    #: ACAL ▶ нажата: (start Hz, stop Hz, точек); саму автокалибровку ведёт координатор (COORD)
    acal_requested = pyqtSignal(float, float, int)

    def __init__(self, proxy: Instrument, settings: QSettings, title: str = "Power meter", parent=None):
        self._sdg: Instrument | None = None
        self._acal = None                      # идущая автокалибровка (Run координатора)
        self._acal_curve: list[tuple[float, float]] = []
        super().__init__(proxy, settings, title, parent)
        proxy.sync_done.connect(self.apply_correction)
        proxy.action_done.connect(lambda name, _r: name == "reset" and self.apply_correction())
        proxy.param_updated.connect(self._on_meas)
        COORD.links.changed.connect(self._relink)
        COORD.run_ended.connect(self._on_run_ended)
        COORD.follow.changed.connect(self._show_follow)
        COORD.follow.set_enabled(proxy, self.follow_button.isChecked())
        self._relink()
        self._refresh_readout()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, s, pre = self.proxy, self.settings, self._prefix
        sup = proxy.supports
        self.widgets: dict[str, Any] = {}

        # избранное: один индикатор
        self.readout = PowerReadout(s.value(f"{pre}/SECONDARY", "W", type=str),
                                    s.value(f"{pre}/MAIN_UNIT", "dBm", type=str))
        self.readout.main_unit_changed.connect(lambda v: s.setValue(f"{pre}/MAIN_UNIT", v))
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
            """Строка: (подпись, ключ, фабрика[, (ключ, фабрика)...]) - несколько виджетов в одной строке."""
            lines = []
            for t, k, make, *more in rows:
                ws = []
                for key, mk in ((k, make), *more):
                    if not sup(key):
                        continue
                    w = mk(proxy, key) if mk else _auto(proxy, key)
                    self.widgets.setdefault(key, w)
                    if not isinstance(w, PNLabel):
                        self.saved_widgets.append(w)
                    ws.append(w)
                if ws:
                    lines.append(PanelLine(t, *ws))
            return lines

        # MAIN: частота, усреднение; режим детектора, видеополоса и непрерывный счёт - в три столбца
        main_top = add((("FREQ", "freq", lambda p, k: PSpinBox(p, k, ("k", "M", "G"))),
                        ("AVG", "avg.state", None, ("avg.count", None),         # усреднение: вкл, число, авто
                         ("avg.auto", lambda p, k: PSButton(p, k, [("MAN", False), ("AUTO", True)])))))
        main_flags = add((("DET", "detector", lambda p, k: PSButton(p, k, [("AVG", "AVERAGE"), ("NORM", "NORMAL")])),
                          ("VBW", "video_bw", None), ("CONT", "meas.continuous", None)))
        # FREQ следует за генератором / ВАЦ в CW (координатор); кнопка - вкл / выкл, видно за кем
        self.follow_button = IndicatorButton("FOLW")
        self.follow_button.setObjectName("choice")
        self.follow_button.setCheckable(True)
        self.follow_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.follow_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.follow_button.setChecked(s.value(f"{pre}/FREQ_FOLLOW", True, type=bool))
        self.follow_button.toggled.connect(self._set_follow)
        for ln in main_top:
            if ln.name == "FREQ":
                ln.add_widget(self.follow_button)
        main = [*main_top, *([tuple(main_flags)] if main_flags else [])]
        self.page_specs.append(("MAIN", (), PanelTable(*main)))

        # TRIG: настройка запуска (источник + фронт, уровень, задержка + блокировка) и строба (Gate)
        trig_src = lambda p, k: PSButton(p, k, [("FREE", "IMM"), ("LEVEL", "INT"), ("EXT", "EXT"), ("BUS", "BUS")])  # noqa: E731
        trig_auto = lambda p, k: PSButton(p, k, [("MAN", False), ("AUTO", True)])  # noqa: E731
        trig_row1 = add((("SRC", "trig.source", trig_src), ("SLOP", "trig.slope", None)))
        if "trigger" in proxy.actions:
            self.trig_bus_button = ActionButton(proxy, "trigger", "TRIG", "Software trigger (*TRG for BUS mode)")
            for ln in trig_row1:
                if ln.name == "SRC":
                    ln.add_widget(self.trig_bus_button)
                    break
        trig_lvl = add((("LVL", "trig.level", lambda p, k: PSpinBox(p, k, ("",)), ("trig.level_auto", trig_auto)),))
        trig_time = add((("DLY", "trig.delay", lambda p, k: PSpinBox(p, k, ("n", "µ", "m", ""))),
                         ("HOLD", "trig.holdoff", lambda p, k: PSpinBox(p, k, ("µ", "m", "")))))
        trig_gate = add((("GSTR", "gate.start", lambda p, k: PSpinBox(p, k, ("n", "µ", "m", ""))),
                         ("GLEN", "gate.length", lambda p, k: PSpinBox(p, k, ("n", "µ", "m", "")))))
        trig_lines: list = []
        if trig_row1:
            trig_lines.append(tuple(trig_row1))
        trig_lines.extend(trig_lvl)
        if trig_time:
            trig_lines.append(tuple(trig_time))
        if trig_gate:
            trig_lines.append(tuple(trig_gate))
        trig_acts = []
        if "single" in proxy.actions:
            self.single_button = ActionButton(proxy, "single", "SING", "Arm / run a single triggered measurement")
            trig_acts.append(self.single_button)
        if "abort" in proxy.actions:
            self.abort_button = ActionButton(proxy, "abort", "STOP", "Abort waiting for trigger")
            trig_acts.append(self.abort_button)
        if trig_acts:
            trig_lines.append(PanelLine("RUN", *trig_acts))
        if trig_lines:
            self.page_specs.append(("TRIG", (), PanelTable(*trig_lines)))

        # CAL: график поправки, выбор таблицы (CSV), автокалибровка, GAIN, DUTY, итог, датчик
        self.cal_dir = Path(s.fileName()).resolve().parent / "calibrations"
        self.cal_points: list[tuple[float, float]] = []
        self.cal_plot = CalPlot()
        self.cal_plot.setToolTip("Calibration table: correction vs frequency; line - sensor frequency")
        self.cal_choice = PChoice([("NONE", "")])
        self.cal_choice.setToolTip(f"Calibration table (CSV: freq_hz, dB) from {self.cal_dir}")
        self.cal_choice.pressed.connect(self.refresh_tables)
        self.refresh_tables(s.value(f"{pre}/CAL_FILE", "", type=str))
        self.cal_choice.changed.connect(self._table_chosen)
        # автокалибровка: ГСС перебирает частоты, ваттметр меряет -> GAIN и таблица (пока только интерфейс)
        self.acal_start = LNumber(None, "GHz", 1e3, 1000e9, 6, scale=1e9, si_unit="Hz", hint="START")
        self.acal_stop = LNumber(None, "GHz", 1e3, 1000e9, 6, scale=1e9, si_unit="Hz", hint="STOP")
        self.acal_points = LNumber(None, "", 2, 100000, 6, hint="PTS")
        for w, name in ((self.acal_start, "ACAL_START"), (self.acal_stop, "ACAL_STOP"), (self.acal_points, "ACAL_PTS")):
            v = s.value(f"{pre}/{name}")
            if v not in (None, ""):
                try:
                    w.set_value(float(v), emit=False)
                except (TypeError, ValueError):
                    pass
            w.changed.connect(lambda v, n=name: s.setValue(f"{pre}/{n}", v))
            w.setMinimumWidth(0)
        self.acal_start.setToolTip("ACAL start frequency (the generator starts here)")
        self.acal_stop.setToolTip("ACAL stop frequency")
        self.acal_points.setToolTip("ACAL number of frequency points from START to STOP")
        self.acal_run = InstrumentWindow._tool("▶", ACAL_TIP, self.run_acal)
        self.gain_on = LToggle([("OFF", False), ("ON", True)], s.value(f"{pre}/GAIN_ON", False, type=bool))
        self.gain_on.setToolTip("GAIN on / off: subtract the path gain from the reading")
        self.gain = LNumber(s.value(f"{pre}/GAIN", 0.0, type=float), "dB", -100.0, 100.0)
        self.gain.setToolTip("Gain in the path (subtracted)")
        self.duty_src = LToggle(DUTY_SOURCES, s.value(f"{pre}/DUTY_SRC", "OFF", type=str))
        self.duty_src.setToolTip("DUTY: OFF / manual / from the pulse generator (SDG CH1 / CH2) in this app")
        self.duty = LNumber(s.value(f"{pre}/DUTY", 100.0, type=float), "%", 0.001, 100.0)
        self.duty.setToolTip("Pulse duty cycle: adds 10·lg(100 / DUTY)")
        for w, name in ((self.gain_on, "GAIN_ON"), (self.duty_src, "DUTY_SRC")):
            w.changed.connect(lambda v, n=name: self._local_changed(n, v))
        for w, name in ((self.gain, "GAIN"), (self.duty, "DUTY")):
            w.changed.connect(lambda v, n=name: self._local_changed(n, v))
        plot_line = PanelLine("", self.cal_plot)
        plot_line._name.hide()
        # выбранная таблица и есть «поправка включена»; NONE - без таблицы
        tabl_row = [PanelLine("TABL", self.cal_choice)]
        if sup("offset"):
            off = PNLabel(proxy, "offset")
            off.setToolTip("Total correction written to the meter (CORR:GAIN2)")
            self.widgets["offset"] = off
            tabl_row.append(PanelLine("OFFS", off))
        cal: list = [plot_line, tuple(tabl_row),
                     PanelLine("ACAL", self.acal_start, self.acal_stop, self.acal_points, self.acal_run),
                     PanelLine("GAIN", self.gain_on, self.gain), PanelLine("DUTY", self.duty_src, self.duty)]
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

        # TIME: режим временной области (график - в избранном, пока TRAC ON) и его настройки в два столбца
        # Запуск и строб вынесены на отдельную вкладку TRIG.
        time_len = add((("TLEN", "trace.time", None), ("TOFS", "trace.offset", None)))
        time_meas = add((("PEAK", "meas.peak", PNLabel), ("PTAV", "meas.ptav", PNLabel)))
        time_trac = add((("TRAC", "trace.state", None),))
        scal_line, ref_line, div_line = self._time_scale_lines()
        time_: list = []
        if time_len:
            time_.append(tuple(time_len))
        if time_meas:
            time_.append(tuple(time_meas))
        time_.append((*time_trac, scal_line) if time_trac else scal_line)
        time_.append((ref_line, div_line))
        self.page_specs.append(("TIME", ("trace.state",) if sup("trace.state") else (),
                                PanelTable(*time_)))

    # ------------------------------------------------------------ шкала графика TIME (настройка окна)
    def _time_scale_lines(self) -> list[PanelLine]:
        """SCAL: AUTO / MAN и единица dBm / W; REF - верх шкалы, DIV - цена деления (ini TIME_*)."""
        s, pre = self.settings, self._prefix
        self.scale_auto = LToggle([("AUTO", True), ("MAN", False)], s.value(f"{pre}/TIME_AUTO", True, type=bool))
        self.scale_auto.setToolTip("Y scale of the TIME graph: AUTO from the trace, MAN - REF and DIV below")
        self.scale_unit = LToggle([("dBm", "dBm"), ("W", "W")], s.value(f"{pre}/TIME_UNIT", "dBm", type=str))
        self.scale_unit.setToolTip("Y unit of the TIME graph: dBm (log) or W (linear)")
        self.scale_ref = LNumber(None, "dBm", -200.0, 100.0)
        self.scale_ref.setToolTip("Top of the TIME graph (REF); typing a value switches the scale to MAN")
        self.scale_div = LNumber(None, "dB", 0.001, 100.0)
        self.scale_div.setToolTip("Value of one of the 6 divisions; typing a value switches the scale to MAN")
        self.scale_auto.changed.connect(lambda v: self._scale_changed("TIME_AUTO", bool(v)))
        self.scale_unit.changed.connect(lambda v: self._scale_changed("TIME_UNIT", v))
        self.scale_ref.changed.connect(lambda v: self._scale_typed("REF", v))
        self.scale_div.changed.connect(lambda v: self._scale_typed("DIV", v))
        self._apply_scale()
        return [PanelLine("SCAL", self.scale_auto, self.scale_unit), PanelLine("REF", self.scale_ref),
                PanelLine("DIV", self.scale_div)]

    def _scale_values(self) -> tuple[str, float | None, float | None]:
        unit = self.scale_unit.value
        tag = "W" if unit == "W" else "DBM"
        ref = self.settings.value(f"{self._prefix}/TIME_REF_{tag}", None)
        div = self.settings.value(f"{self._prefix}/TIME_DIV_{tag}", None)
        return unit, _parse_finite(ref), _parse_finite(div, positive=True)

    def _apply_scale(self) -> None:
        """Шкала - в график, поля REF / DIV - в единице шкалы (в AUTO показывают текущую шкалу)."""
        unit, ref, div = self._scale_values()
        if unit == "W":
            self.scale_ref.set_unit("mW", 0.0, 1e3, scale=1e-3, si_unit="W")
            self.scale_div.set_unit("mW", 1e-12, 1e3, scale=1e-3, si_unit="W")
        else:
            self.scale_ref.set_unit("dBm", -200.0, 100.0)
            self.scale_div.set_unit("dB", 0.001, 100.0)
        auto = self.scale_auto.value is not False
        self.plot.set_scale(unit, auto, ref, div)
        lo, hi = self.plot.y_range() if self.plot.trace is not None else (None, None)
        shown_ref = ref if not auto and ref is not None else hi
        shown_div = div if not auto and div else (None if hi is None else (hi - lo) / self.plot.DIVS_Y)
        for w, v in ((self.scale_ref, shown_ref), (self.scale_div, shown_div)):
            if v is None:
                w.clear_value()
            else:
                w.set_value(v, emit=False)

    def _scale_changed(self, name: str, value) -> None:
        if self.settings.value(f"{self._prefix}/{name}") != value:
            self.settings.setValue(f"{self._prefix}/{name}", value)
        self._apply_scale()
        WINDOW_EVENTS.changed.emit(self.proxy)

    def _scale_typed(self, what: str, value: float) -> None:
        """Ввели REF или DIV - шкала ручная; второе значение берётся с текущей шкалы."""
        tag = "W" if self.scale_unit.value == "W" else "DBM"
        lo, hi = self.plot.y_range()
        other = {"REF": ("DIV", (hi - lo) / self.plot.DIVS_Y), "DIV": ("REF", hi)}[what]
        self.settings.setValue(f"{self._prefix}/TIME_{what}_{tag}", float(value))
        if self.settings.value(f"{self._prefix}/TIME_{other[0]}_{tag}", None) in (None, ""):
            self.settings.setValue(f"{self._prefix}/TIME_{other[0]}_{tag}", float(other[1]))
        if self.scale_auto.value is not False:
            self.scale_auto.set_value(False)      # -> _scale_changed -> _apply_scale + WINDOW_EVENTS
        else:
            self._apply_scale()
            WINDOW_EVENTS.changed.emit(self.proxy)

    def restore_rank(self, key: str) -> int:
        return RESTORE_RANK.get(key, 2)

    def restore(self) -> None:
        # поправка (CORR:GAIN2) должна восстановиться до trig.level, чтобы его диапазон уже учитывал калибровку
        self.apply_correction()
        super().restore()

    def _tab_index(self, title: str) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == title:
                return i
        return -1

    def _update_tabs(self) -> None:
        super()._update_tabs()
        idx = self._tab_index("TRIG")
        if idx >= 0:
            src = self.proxy.value("trig.source")
            self.tabs.set_on(idx, src is not None and src != "IMM")

    def _update_trig_enabled(self) -> None:
        slope = self.widgets.get("trig.slope")
        if slope is not None:
            src = self.proxy.value("trig.source")
            slope.setEnabled(self.proxy.is_connected and src in ("INT", "EXT")
                             and self.proxy.value("trig.slope") is not None)

    # ------------------------------------------------------------ вкладки
    def on_param(self, key: str, value: Any) -> None:
        if key == "trace":
            self.plot.set_trace(value)
            if self.scale_auto.value is not False:
                self._apply_scale()               # в AUTO поля REF / DIV показывают текущую шкалу
        elif key == "trace.state":
            self.show_plot(value is True)
        elif key in ("gate.start", "gate.length"):
            self.plot.set_gate(self.proxy.value("gate.start"), self.proxy.value("gate.length"))
        elif key == "freq":
            self.cal_plot.set_freq(value)
            self.apply_correction()
        elif key in ("trig.source", "trig.slope"):
            self._update_tabs()
            self._update_trig_enabled()

    # ------------------------------------------------------------ таблицы калибровки (CSV)
    def table_files(self) -> list[str]:
        return sorted(p.name for p in self.cal_dir.glob("*.csv")) if self.cal_dir.is_dir() else []

    def refresh_tables(self, select: str | None = None) -> None:
        """Перечитать папку calibrations (перед открытием списка)."""
        current = self.cal_choice.value if select is None else select
        self.cal_choice.blockSignals(True)
        self.cal_choice.set_choices([("NONE", "")] + [(Path(n).stem, n) for n in self.table_files()], current)
        self.cal_choice.blockSignals(False)
        if select is not None:
            self._load_table(self.cal_choice.value)

    def _load_table(self, name: str) -> None:
        try:
            self.cal_points = load_csv(self.cal_dir / name) if name else []
        except OSError as exc:
            if hasattr(self, "error_log"):
                self._log("CAL TABLE", f"{name}: {exc}")
            self.cal_points = []
        self.cal_plot.set_points(self.cal_points)

    def _table_chosen(self, name) -> None:
        self._load_table(name or "")
        self._local_changed("CAL_FILE", name or "")

    def run_acal(self) -> None:
        """▶: автокалибровка - сценарий координатора ACAL (ГСС перебирает частоты START..STOP, ваттметр
        меряет, подбираются GAIN и таблица CSV). Во время неё та же кнопка (■) останавливает."""
        if self._acal is not None:
            self._acal.stop()
            return
        start, stop, pts = self.acal_start.value, self.acal_stop.value, self.acal_points.value
        if None in (start, stop, pts) or stop <= start or pts < 2:
            self._log("ACAL", "set START < STOP and PTS >= 2")
            return
        self.acal_requested.emit(float(start), float(stop), int(pts))
        scenario = AutoCalibration(start, stop, int(pts), path=self.cal_dir / acal_file_name(start, stop, pts),
                                   duty_db=self.correction_parts()["duty"])
        try:
            run = COORD.start(scenario, bind={"power_meter": self.proxy})
        except ScenarioError as exc:
            self._log("ACAL", str(exc))
            return
        self._acal = run
        self._acal_curve = []
        run.progress.connect(self._acal_progress)
        run.point.connect(self._acal_point)
        run.message.connect(self._acal_message)
        run.ended.connect(self._acal_ended)
        self._acal_ui(True)

    def _acal_message(self, text: str) -> None:
        if not sip.isdeleted(self):
            self._log("ACAL", text)

    def _acal_ui(self, running: bool) -> None:
        self.acal_run.setText("■" if running else "▶")
        self.acal_run.setToolTip("Stop auto calibration (the instruments are returned as they were)"
                                 if running else ACAL_TIP)
        for w in (self.acal_start, self.acal_stop, self.acal_points, self.cal_choice):
            w.setEnabled(not running)
        self._refresh_readout()

    def _acal_progress(self, done: int, total: int, _text: str) -> None:
        self._refresh_readout()

    def _acal_point(self, point: dict) -> None:
        """Пока идёт ACAL, график показывает измеренную поправку тракта (уровень ГСС − показание)."""
        self._acal_curve.append((point["freq"], point["level"] - point["power"]))
        self.cal_plot.set_points(self._acal_curve)

    def _acal_ended(self, run) -> None:
        self._acal = None
        if sip.isdeleted(self):
            return
        self._acal_ui(False)
        result = run.result if run.state == run.DONE else None
        if result is not None and result.path is not None:
            name = result.path.name
            self.refresh_tables(select=name)            # новая таблица выбрана и на графике
            self._local_changed("CAL_FILE", name)
            self.gain.set_value(round(result.gain, 3))
            self.gain_on.set_value(True)                # итог - строкой сценария в журнале (run.message)
        else:
            self.cal_plot.set_points(self.cal_points)  # график - снова выбранная таблица
            self._log("ACAL", run.error or run.state)
        self.apply_correction()

    def _on_run_ended(self, run) -> None:
        """Сценарий с этим ваттметром закончился - вернуть прибору поправку окна."""
        if sip.isdeleted(self):
            return
        if self.proxy in run.instruments():
            self.apply_correction()

    def show_plot(self, on: bool) -> None:
        """Избранное: график (временная область) или индикатор."""
        if self.plot.isHidden() != on:
            return
        self.readout.setVisible(not on)
        self.plot.setVisible(on)
        self._fit_height()

    def show_cal(self) -> None:
        idx = self._tab_index("CAL")
        self.set_page(idx if idx >= 0 else 0, fit=False)
        self.set_expanded(True)

    # ------------------------------------------------------------ индикатор
    def _on_meas(self, key: str, _value: Any) -> None:
        if sip.isdeleted(self):
            return
        if key.startswith("meas.") or key.startswith("trig."):
            self._refresh_readout()

    def _refresh_readout(self) -> None:
        if sip.isdeleted(self):
            return
        v = self.proxy.value
        run = self._acal
        self.readout.update_values(power=v("meas.power"), peak=v("meas.peak"), ptav=v("meas.ptav"),
                                   source=v("trig.source"), level=v("trig.level"),
                                   level_auto=v("trig.level_auto"),
                                   cal=self.total_correction(),
                                   acal=f"ACAL {run.done}/{run.total}" if run is not None else None)
        self.readout.setEnabled(self.proxy.is_connected)

    def _on_state(self, state: ConnectionState) -> None:
        super()._on_state(state)
        if not sip.isdeleted(self) and hasattr(self, "readout"):
            self._refresh_readout()
            self._update_trig_enabled()

    def toggle_trigger(self) -> None:
        """Клик по запуску в индикаторе: FREE RUN -> LEVEL -> EXT -> FREE RUN."""
        if self.proxy.is_connected and self.proxy.supports("trig.source"):
            order = ("IMM", "INT", "EXT")
            cur = self.proxy.value("trig.source")
            self.proxy.set("trig.source", order[(order.index(cur) + 1) % 3] if cur in order else "IMM")

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
        cal = interpolate(self.cal_points, self.proxy.value("freq"))
        gain = -self.gain.value if self.gain_on.value else 0.0
        duty = self.duty_value()
        return {"cal": cal, "gain": gain, "duty": 10 * math.log10(100.0 / duty) if duty else 0.0}

    def total_correction(self) -> float:
        total = sum(self.correction_parts().values())
        return min(max(total, -OFFSET_LIMIT), OFFSET_LIMIT)

    def apply_correction(self) -> None:
        """Записать итоговую поправку в прибор (offset), если она изменилась."""
        if sip.isdeleted(self):
            return
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
        if not p.is_connected or self._resetting or COORD.busy(p) is not None:
            return                               # сценарий координатора ведёт прибор (ACAL: без поправки)
        if p.supports("offset"):
            cur = p.value("offset")
            if cur is None or abs(cur - total) > 5e-4:
                p.set("offset", total)
        if p.supports("offset.state"):
            on = abs(total) > 5e-4
            if p.value("offset.state") is not on:
                p.set("offset.state", on)

    # ------------------------------------------------------------ связь с ГСС (скважность)
    # ------------------------------------------------------------ частота за генератором
    def _set_follow(self, on: bool) -> None:
        if self.settings.value(f"{self._prefix}/FREQ_FOLLOW", True, type=bool) != bool(on):
            self.settings.setValue(f"{self._prefix}/FREQ_FOLLOW", bool(on))
        COORD.follow.set_enabled(self.proxy, on)

    def _show_follow(self, pm=None) -> None:
        if sip.isdeleted(self) or (pm is not None and pm is not self.proxy):
            return
        src = COORD.follow.source(self.proxy)
        on = self.follow_button.isChecked()
        name = COORD.display_name(src) if src is not None else ""
        self.follow_button.setText(f"← {name}" if on and name else "FOLW")
        self.follow_button.setToolTip(
            ("FREQ follows " + (f"{name}: " if name else "the generator: ") if on else "FREQ does not follow: ")
            + "a new generator frequency (GEN, or VNA in CW) is set on the sensor.\nClick - on / off")

    def _relink(self) -> None:
        if sip.isdeleted(self):
            return
        sdg = COORD.find("function_generator") if self.duty_src.value in ("CH1", "CH2") else None
        if sdg is self._sdg:
            return
        if self._sdg is not None and not sip.isdeleted(self._sdg):
            try:
                self._sdg.param_updated.disconnect(self._on_sdg_param)
            except (TypeError, RuntimeError):
                pass
        self._sdg = sdg
        if sdg is not None:
            sdg.param_updated.connect(self._on_sdg_param)
        self.apply_correction()

    def _on_sdg_param(self, key: str, _value: Any) -> None:
        if not sip.isdeleted(self) and key.endswith(".duty"):
            self.apply_correction()

    def cleanup(self) -> None:
        if self._acal is not None:
            for sig, slot in ((self._acal.progress, self._acal_progress), (self._acal.point, self._acal_point),
                              (self._acal.message, self._acal_message), (self._acal.ended, self._acal_ended)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._acal = None
        for signal, slot in ((COORD.links.changed, self._relink), (COORD.run_ended, self._on_run_ended),
                             (COORD.follow.changed, self._show_follow)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if self._sdg is not None and not sip.isdeleted(self._sdg):
            try:
                self._sdg.param_updated.disconnect(self._on_sdg_param)
            except (TypeError, RuntimeError):
                pass
        self._sdg = None
        super().cleanup()

    def closeEvent(self, event):  # noqa: N802 - Qt
        # Не вызывать cleanup() при скрытии окна из MainWindow — окно переиспользуется при повторном открытии.
        super().closeEvent(event)
