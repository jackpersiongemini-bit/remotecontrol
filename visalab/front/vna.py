"""Окно векторного анализатора цепей (N5231A и любой прибор профиля network_analyzer): только настройки.

Трассы здесь не выводятся (для этого будет отдельный виджет). Избранное:

* MEM  - CAPTURE: данные -> память у всех читаемых измерений;
* MATH - математика с памятью у всех читаемых измерений: DATA, D/M, D−M, D+M, D·M;
* POW  - мощность источника (колесо / стрелки - разряд под курсором), RF ON/OFF и PRST: в режиме
  пресетов колесо и стрелки переключают мощность только по списку из вкладки PWR;
* CW   - одна частота (ВАЦ как генератор): CW включает тип развёртки CW, повторный клик
  возвращает прежний тип; рядом частота CW;

Первая строка избранного CH - чипы каналов (как пресеты, без «+»/«−»): текущий канал окна, к нему
относятся POW, CW и вкладки STIM, CAL.
Вкладки: TRAC (сверху чипы всех трейсов прибора, как пресеты: клик выбирает, + создаёт трейс S21 на
канале выбранного, − удаляет выбранный с прибора; под ними выбранный трейс: CH - список каналов (другой
канал переносит трейс, пересоздавая там), READ, SPAR, FMT, MATH; ALL / NONE; слоты не видны),
SCAL (масштаб выбранного трейса), MARK (маркеры выбранного трейса: чипы 1..10, STAT, X, Y, MAX / MIN,
OFF всех), STIM (стимул и развёртка), TRIG (запуск), PWR (мощность, RF, пресеты
мощности чипами: клик ставит, «+» текущая, «−» выбранная), CAL (cal set'ы прибора чипами: клик применяет к каналу выбранного трейса, NONE снимает;
стимул из cal set или свой, CORR, интерполяция; CALIBRATE открывает отдельное окно калибровки), SAVE (состояния в памяти прибора: чип -
вызвать, + сохранить, − удалить; в ini только порядок чипов).
Окно знает только InstrumentProxy и ключи профиля network_analyzer.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, QSize, Qt, QTimer
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..core.export import write_touchstone, write_traces_csv
from ..core.params import Kind
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import (PRESET_GAP, HScrollArea, InstrumentWindow, PowerSpin, PresetGroup,
                                parse_power_list)
from .params import IndicatorButton, PBGroup, PLineEdit, PSButton, PSpinBox, Panel, PanelLine, PanelTable
from .power_meter import CONFIRM_MS, ActionButton
from .spectrum import StatePresets
from .theme import UI_COLUMN_WIDTH_PX, WINDOW_BAR_HEIGHT
from .window import Window

__all__ = ["VNAWindow", "PowerSpin", "MathButton", "CWButton", "TraceEditor", "MenuButton", "parse_power_list",
           "saved_connection"]

CONNECTION_GROUP = "CONNECTION/VNA"
ONOFF = [("OFF", False), ("ON", True)]
AUTO = [("MAN", False), ("AUTO", True)]
SLOTS = 16
MATH = (("DATA", "NORM"), ("D/M", "DIV"), ("D−M", "SUBT"), ("D+M", "ADD"), ("D·M", "MULT"))
TRACE_CHIP_HEIGHT = 44            # чипы трейсов в избранном: две строки (формат / канал·S-параметр)
CAL_WINDOW_WIDTH = 440            # окно калибровки: полные названия наборов и шагов

#: вкладка STIM: (подпись, ключ, второй ключ или None)
STIM_ROWS = (("STAR", "freq.start", None), ("STOP", "freq.stop", None), ("CENT", "freq.center", None),
             ("SPAN", "freq.span", None), ("PTS", "sweep.points", None), ("IFBW", "bw.if", None),
             ("TIME", "sweep.time", "sweep.time_auto"), ("TYPE", "sweep.type", None),
             ("AVG", "avg.state", "avg.count"))
#: вкладка TRIG: (подпись, ключ, кнопки или None - по профилю, подсказка)
TRIG_ROWS = (("SRC", "trig.source", [("INT", "IMM"), ("EXT", "EXT"), ("MAN", "MAN")],
              "Trigger source: internal, external input (rear BNC), manual (TRIG button)"),
             ("SCOP", "trig.scope", [("ALL", "ALL"), ("CH", "CURRENT")],
              "One trigger sweeps all channels in turn, or only the current one"),
             ("MODE", "trig.mode", [("CHAN", "CHANNEL"), ("SWP", "SWEEP"), ("PNT", "POINT"), ("TRAC", "TRACE")],
              "What one trigger does on the CH channel: the whole channel, one sweep, one point, one trace"),
             ("SLOP", "trig.slope", [("POS", "POS"), ("NEG", "NEG")], "External trigger edge / level polarity"),
             ("TYPE", "trig.type", [("EDGE", "EDGE"), ("LEVEL", "LEVEL")], "External trigger by edge or by level"),
             ("DLY", "trig.delay", None, "Delay after the external trigger, s"))
NOT_SAVED = ("freq.center", "freq.span", "cal.state")       # центр/обзор следуют из старта/стопа; калибровку не вернуть
RESTORE_RANK = {"freq.start": 0, "freq.stop": 0, "sweep.type": 1, "freq.cw": 1, "rf.output": 5, "trig.source": 6}


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _auto(proxy: InstrumentProxy, key: str):
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, AUTO if key.endswith("_auto") else ONOFF)
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


class MathButton(IndicatorButton):
    """Математика с памятью у всех читаемых измерений; показывает первое читаемое. Индикатор - не DATA."""

    def __init__(self, proxy: InstrumentProxy, parent=None):
        super().__init__("DATA", parent)
        self.proxy = proxy
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Math with the memory trace for every measurement being read")
        self.clicked.connect(self._menu)
        for n in range(1, SLOTS + 1):
            for what in ("math", "enabled", "name"):
                if proxy.supports(f"m{n}.{what}"):
                    proxy.subscribe(f"m{n}.{what}", lambda _v: self.refresh())
        self.refresh()

    def slots(self) -> list[int]:
        p = self.proxy
        return [n for n in range(1, SLOTS + 1) if p.value(f"m{n}.enabled") is True and p.value(f"m{n}.name")]

    def value(self) -> str | None:
        s = self.slots()
        return self.proxy.value(f"m{s[0]}.math") if s else None

    def indicated(self) -> bool:
        return self.value() not in (None, "NORM")

    def refresh(self) -> None:
        v = self.value()
        self.setText(next((n for n, d in MATH if d == v), "DATA" if v is None else str(v)))
        self.setEnabled(bool(self.slots()))
        self.update()

    def set_value(self, value: str) -> None:
        for n in self.slots():
            self.proxy.set(f"m{n}.math", value)

    def _menu(self) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        cur = self.value()
        for name, data in MATH:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(data == cur)
            act.triggered.connect(lambda _=False, d=data: self.set_value(d))
        menu.setMinimumWidth(self.width())
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))


class CWButton(IndicatorButton):
    """CW - режим генератора (``gen.state``): канал CH в CW, остальные каналы стоят; повторный клик
    возвращает настройки канала, как были до включения (это делает драйвер)."""

    def __init__(self, proxy: InstrumentProxy, parent=None):
        super().__init__("CW", parent)
        self.proxy = proxy
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Generator mode: the CH channel goes to CW, other channels stop.\n"
                        "Click again to restore everything as it was")
        self._on: bool | None = None
        self.clicked.connect(self.toggle)
        proxy.subscribe("gen.state", self._on_state)

    def indicated(self) -> bool:
        return bool(self._on)

    def _on_state(self, value) -> None:
        self._on = value
        self.update()

    def toggle(self) -> None:
        if not self.proxy.is_connected or self._on is None:
            return
        self.proxy.set("gen.state", not self._on)


SPARAMS = ("S11", "S21", "S12", "S22")
#: формат профиля -> короткая подпись
FORMATS = (("MLOG", "MLOG"), ("PHAS", "PHASE"), ("UPH", "UPHASE"), ("GDEL", "GDELAY"), ("SWR", "SWR"),
           ("MLIN", "LIN_MAG"), ("REAL", "REAL"), ("IMAG", "IMAG"), ("POL", "POLAR"), ("SMIT", "SMITH"))


class MenuButton(IndicatorButton):
    """Выбор из списка без привязки к ключу: клик - меню; ``on_pick(значение)`` - только выбор пользователя."""

    def __init__(self, choices, on_pick, tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tip)
        self.choices, self.on_pick, self.value = tuple(choices), on_pick, None
        self.clicked.connect(self._menu)

    def show_value(self, value) -> None:
        self.value = value
        self.setText(next((n for n, d in self.choices if d == value), "" if value is None else str(value)))

    def _menu(self) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for name, data in self.choices:
            if name is None:
                menu.addSeparator()
                continue
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(data == self.value)
            act.triggered.connect(lambda _=False, d=data: self.on_pick(d))
        menu.setMinimumWidth(self.width())
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))


class _TraceChip(IndicatorButton):
    """Чип трейса: выбор - инверсия (как у пресета), индикатор снизу - трейс читается (у выбранного
    не рисуется: выбор и включение не смешиваются). Высота - строка избранного, ширина - вся строка."""

    def __init__(self, owner: "TraceEditor", key: tuple[int, str]):
        super().__init__()
        self.owner, self.trace_key = owner, key
        self.setObjectName("chip")
        self.setProperty("tight", True)                    # узкие поля: больше трейсов в строке без прокрутки
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):  # noqa: N802 - Qt; QSS чипа (min/max-height) иначе перебивает setFixedHeight
        return QSize(super().sizeHint().width(), TRACE_CHIP_HEIGHT)

    def minimumSizeHint(self):  # noqa: N802 - Qt
        return self.sizeHint()

    def indicated(self) -> bool:
        return not self.isChecked() and self.owner.is_read(*self.trace_key)


class TraceChips(PBGroup):
    """Строка чипов трейсов (как пресеты): клик выбирает трейс для правки."""

    def __init__(self, owner: "TraceEditor", parent=None):
        self.owner = owner
        super().__init__(parent=parent)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignTop)          # чипы делят всю ширину

    def add_button(self, name: str) -> QPushButton:  # чипы создаёт set_keys
        raise NotImplementedError

    def set_keys(self, keys: list[tuple[int, str]]) -> None:
        while self._buttons:
            self.remove_button(0)
        for key in keys:
            b = _TraceChip(self.owner, key)
            self._group.addButton(b, len(self._buttons))
            self._buttons.append(b)
        self._relayout()


class TraceEditor(QWidget):
    """Трейсы: чипы всех трейсов прибора (по всем каналам) - строка TRC избранного; выбранный трейс
    задаёт и текущий канал окна (``stim.channel``: POW / CW, STIM, CAL, режим TRIG). Вкладка TRAC -
    выбранный трейс: канал (список: перенести в другой канал), READ, S-параметр, формат, математика.
    Слоты не видны: READ сам занимает свободный (до 16 читаемых). В режиме генератора трейсы
    других каналов недоступны."""

    def __init__(self, proxy: InstrumentProxy, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.table: list[tuple[int, str, str, str, str]] = []
        self.current: tuple[int, str] | None = None
        self.chips = TraceChips(self)
        self.chips.selected.connect(self._chip_clicked)
        self.scroll = HScrollArea(self.chips, TRACE_CHIP_HEIGHT)
        cfg = lambda what: (lambda v: self.configure(what, v))
        self.channel = MenuButton([], self._channel_picked, "Channel: pick another to move the trace there")
        self.read = _ReadButton(self)
        self.read.setText("READ")
        self.read.setToolTip("Read this trace after every sweep")
        self.read.clicked.connect(self.toggle_read)
        self.sparam = MenuButton([(x, x) for x in SPARAMS], cfg("sparam"), "S-parameter")
        self.format = MenuButton(FORMATS, cfg("format"), "Format")
        self.math = MenuButton(MATH, cfg("math"), "Math with the memory trace")
        self.math.indicated = lambda: self.math.value not in (None, "NORM")
        self.editors = (self.channel, self.read, self.sparam, self.format, self.math)
        proxy.action_done.connect(self._on_action)
        proxy.subscribe("stim.channel", self._on_stim_channel)
        proxy.subscribe("gen.state", lambda _v: self._show_current())
        for n in range(1, SLOTS + 1):
            for what in ("channel", "name", "enabled", "sparam", "format", "math"):
                if proxy.supports(f"m{n}.{what}"):
                    proxy.subscribe(f"m{n}.{what}", lambda _v, n=n: self._from_slot(n))
        self._show_current()

    # -------------------------------------------------------- слоты
    def slot_of(self, channel: int, name: str) -> int | None:
        p = self.proxy
        for n in range(1, SLOTS + 1):
            if p.value(f"m{n}.name") == name and p.value(f"m{n}.channel") == channel:
                return n
        return None

    def is_read(self, channel: int, name: str) -> bool:
        n = self.slot_of(channel, name)
        return n is not None and self.proxy.value(f"m{n}.enabled") is True

    def toggle_read(self) -> None:
        if self.current is None:
            return
        channel, name = self.current
        p, n = self.proxy, self.slot_of(channel, name)
        if n is not None:
            p.set(f"m{n}.enabled", not self.is_read(channel, name))
            return
        free = next((k for k in range(1, SLOTS + 1) if p.value(f"m{k}.enabled") is not True), None)
        if free is None:
            return                                         # все 16 слотов читаются
        p.set(f"m{free}.channel", channel)
        p.set(f"m{free}.name", name)
        p.set(f"m{free}.enabled", True)

    def _from_slot(self, n: int) -> None:
        """Слот прочитан (или изменён из избранного): обновить его трейс без запроса таблицы."""
        p = self.proxy
        for i, (c, name, sp, fmt, math) in enumerate(self.table):
            if name == p.value(f"m{n}.name") and c == p.value(f"m{n}.channel"):
                self.table[i] = (c, name, p.value(f"m{n}.sparam") or sp, p.value(f"m{n}.format") or fmt,
                                 p.value(f"m{n}.math") or math)
        self._show_current()

    # -------------------------------------------------------- выбранный трейс
    def row(self) -> tuple[int, str, str, str, str] | None:
        return next((r for r in self.table if (r[0], r[1]) == self.current), None)

    def channels(self) -> list[int]:
        return sorted({c for c, *_ in self.table}) or [1]

    def _chip_clicked(self, index: int) -> None:
        if 0 <= index < len(self.table):
            self.current = self.table[index][:2]
            self._show_current()
            self._follow_channel()

    def _follow_channel(self) -> None:
        """Канал окна - канал выбранного трейса; вкладка SCAL - сам выбранный трейс (``trace.sel``)."""
        p = self.proxy
        if self.current is None or not p.is_connected:
            return
        if p.supports("stim.channel") and p.value("stim.channel") != self.current[0]:
            p.set("stim.channel", self.current[0])
        sel = f"{self.current[0]}:{self.current[1]}"
        if p.supports("trace.sel") and p.value("trace.sel") != sel:
            p.set("trace.sel", sel)

    def _on_stim_channel(self, value) -> None:
        """Канал сменился не кликом (вызов состояния, отказ в генераторе): выбрать трейс этого канала."""
        if isinstance(value, int) and self.current is not None and self.current[0] != value:
            first = next(((c, n) for c, n, *_ in self.table if c == value), None)
            if first is not None:
                self.current = first
        self._show_current()

    def _channel_picked(self, value) -> None:
        if self.current is not None and value != self.current[0]:
            self.proxy.action("meas_move", self.current[0], self.current[1], int(value))

    def configure(self, what: str, value) -> None:
        if self.current is not None:
            self.proxy.action("meas_config", self.current[0], self.current[1], what, value)

    def create(self) -> None:
        """+ : новый трейс S21 на канале выбранного."""
        self.proxy.action("meas_create", self.current[0] if self.current else 1, "S21")

    def delete(self) -> bool:
        """− : удалить выбранный трейс с прибора."""
        if self.current is None:
            return False
        self.proxy.action("meas_delete", *self.current)
        return True

    def _on_action(self, name: str, result) -> None:
        if name in ("meas_table", "meas_config", "meas_create", "meas_delete", "meas_move") and \
                isinstance(result, (list, tuple)):
            self.set_table([(int(c), str(n), str(s), str(f), str(m)) for c, n, s, f, m in result])

    def set_table(self, table: list[tuple[int, str, str, str, str]]) -> None:
        old = [(c, n) for c, n, *_ in self.table]
        keys = [(c, n) for c, n, *_ in table]
        self.table = list(table)
        if keys != old:
            self.chips.set_keys(keys)
            added = [k for k in keys if k not in old]
            if old and len(added) == 1:                    # новый трейс (+ или перенос) - выбрать его
                self.current = added[0]
            elif self.current not in keys:
                cur = self.proxy.value("stim.channel")
                self.current = next((k for k in keys if k[0] == cur), keys[0] if keys else None)
            self.chips.updateGeometry()
        self._show_current()
        self._follow_channel()

    def _show_current(self) -> None:
        gen = self.proxy.value("gen.state") is True
        cur_ch = self.proxy.value("stim.channel")
        for b, (c, n, sp, fmt, math) in zip(self.chips.buttons(), self.table):
            b.setText(f"{next((n for n, d in FORMATS if d == fmt), fmt)}\n{c}·{sp}")
            b.setToolTip(f"Channel {c}, measurement {n!r}\n{sp} {fmt} {math}")
            b.setEnabled(not gen or c == cur_ch)
            b.update()
        keys = [(c, n) for c, n, *_ in self.table]
        self.chips.set_checked(keys.index(self.current) if self.current in keys else -1)
        if self.current in keys:                           # выбранный трейс - в видимой части строки
            self.scroll.ensureWidgetVisible(self.chips.buttons()[keys.index(self.current)], 0, 0)
        row = self.row()
        for w in self.editors:
            w.setEnabled(row is not None)
        if row is None:
            for w in (self.channel, self.sparam, self.format, self.math):
                w.show_value(None)
            return
        c, _n, sp, fmt, math = row
        new = max(self.channels()) + 1
        self.channel.choices = tuple((f"C{x}", x) for x in self.channels()) + ((f"C{new} NEW", new),)
        self.channel.show_value(c)
        self.sparam.show_value(sp)
        self.format.show_value(fmt)
        self.math.show_value(math)
        self.read.update()
        self.math.update()


class _ReadButton(IndicatorButton):
    def __init__(self, owner: TraceEditor):
        super().__init__()
        self.owner = owner
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def indicated(self) -> bool:
        return self.owner.current is not None and self.owner.is_read(*self.owner.current)


#: единица значения трейса по формату
FORMAT_UNITS = {"MLOG": "dB", "PHASE": "°", "UPHASE": "°", "GDELAY": "s"}


class _MarkChip(IndicatorButton):
    """Чип номера маркера: выбор - инверсия, индикатор - маркер включён (у выбранного не рисуется)."""

    def __init__(self, owner: "MarkerPanel", n: int):
        super().__init__(str(n))
        self.owner, self.n = owner, n
        self.setObjectName("chip")
        self.setProperty("tight", True)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def indicated(self) -> bool:
        return not self.isChecked() and self.n in self.owner.table


class MarkerPanel(QWidget):
    """Маркеры выбранного трейса (``trace.sel``): чипы 1..10 (выбор ``mark.n``, индикатор - включён) и
    значение выбранного (``mark.y`` перечитывается после каждой развёртки)."""

    MARKS = 10

    def __init__(self, proxy: InstrumentProxy, trace_row=None, parent=None):
        super().__init__(parent)
        self.proxy, self.trace_row = proxy, trace_row
        self.table: dict[int, tuple[float, float | None]] = {}
        self.chips = PBGroup(columns=5)
        for n in range(1, self.MARKS + 1):
            b = _MarkChip(self, n)
            self.chips._group.addButton(b, n - 1)
            self.chips._buttons.append(b)
        self.chips._relayout()
        self.chips.selected.connect(lambda i: proxy.set("mark.n", i + 1))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chips)
        self.value = QLabel("")
        self.value.setObjectName("lineName")
        self.value.setToolTip("Value of the selected marker on the selected trace")
        proxy.subscribe("mark.n", lambda _v: self._show())
        proxy.subscribe("mark.y", lambda _v: self._show())
        for key in ("trace.sel", "mark.state", "mark.x"):
            proxy.subscribe(key, lambda _v: self.refresh())
        proxy.action_done.connect(self._on_action)
        self._show()

    def refresh(self) -> None:
        if self.proxy.is_connected and "mark_table" in self.proxy.actions:
            self.proxy.action("mark_table")

    def unit(self) -> str:
        row = self.trace_row() if self.trace_row is not None else None
        return FORMAT_UNITS.get(row[3], "") if row is not None else ""

    def _on_action(self, name: str, result) -> None:
        if name in ("mark_table", "mark_peak", "mark_min", "mark_aoff") and isinstance(result, (list, tuple)):
            self.table = {int(n): (x, y) for n, x, y in result}
            self._show()

    def _show(self) -> None:
        n = self.proxy.value("mark.n")
        self.chips.set_checked(n - 1 if isinstance(n, int) and 1 <= n <= self.MARKS else -1)
        for b in self.chips.buttons():
            b.update()
        on = self.proxy.value("mark.state") is True
        y = self.proxy.value("mark.y")
        self.value.setText(("-" if y is None else f"{y:.2f} {self.unit()}".rstrip()) if on else "OFF")



class CalSets(QWidget):
    """Cal set'ы в памяти анализатора - чипы (NONE + имена, по два в ряд). Клик применяет cal set к
    каналу выбранного трейса (``cal.set``), NONE снимает; выбран чип cal set'а этого канала.
    ↻ перечитывает список, − удаляет cal set канала из памяти прибора (второй клик за 3 с)."""

    NONE = "NONE"

    def __init__(self, proxy: InstrumentProxy, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.names: list[str] = []
        self.chips = PBGroup(columns=2)
        self.chips.selected.connect(self._clicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chips)
        self._armed = QTimer(self)                         # − : первый клик взводит, второй удаляет
        self._armed.setSingleShot(True)
        self._armed.timeout.connect(lambda: self.delete_button and self.delete_button.setText("−"))
        self.delete_button: QPushButton | None = None
        self.relayout = None                               # окно: подогнать высоту под новые чипы
        proxy.action_done.connect(self._on_action)
        proxy.subscribe("cal.set", lambda _v: self._show())
        proxy.subscribe("gen.state", lambda _v: self._show())
        self.set_names([])

    def refresh(self) -> None:
        if self.proxy.is_connected and "cal_list" in self.proxy.actions:
            self.proxy.action("cal_list")

    def set_names(self, names: list[str]) -> None:
        self.names = list(names)
        while self.chips.buttons():
            self.chips.remove_button(0)
        for text in (self.NONE, *self.names):
            b = self.chips.add_button(text)
            b.setToolTip("Remove the cal set from the channel (correction off)" if text == self.NONE
                         else f"Apply cal set {text!r} to the channel of the selected trace")
        self._show()
        if self.relayout is not None:
            self.relayout()

    def _show(self) -> None:
        cur = self.proxy.value("cal.set")
        keys = ["", *self.names]
        self.chips.set_checked(keys.index(cur) if cur in keys else -1)
        gen = self.proxy.value("gen.state") is True              # CW: калибровку канала не трогаем
        for b in self.chips.buttons():
            b.setEnabled(not gen)

    def _clicked(self, index: int) -> None:
        name = "" if index == 0 else self.names[index - 1]
        if self.proxy.value("cal.set") != name:
            self.proxy.set("cal.set", name)
        self._show()                                       # чип выбирается, когда прибор подтвердит

    def delete(self) -> bool:
        """− : cal set канала из памяти прибора; первый клик только взводит (кнопка - «?»)."""
        name = self.proxy.value("cal.set")
        b = self.delete_button
        if not name or name not in self.names:
            return False
        if b is not None and not self._armed.isActive():
            b.setText("?")
            self._armed.start(CONFIRM_MS)
            return False
        self._armed.stop()
        if b is not None:
            b.setText("−")
        self.proxy.action("cal_delete", name)
        return True

    def _on_action(self, name: str, result) -> None:
        if name in ("cal_list", "cal_delete", "cal_save") and isinstance(result, (list, tuple)):
            self.set_names([str(n) for n in result])


def short_conn(name: str) -> str:
    """Короткая подпись разъёма: «Type N (50) female» -> «N F», «APC 3.5 male» -> «3.5 M»."""
    words = [w for w in re.sub(r"\(\d+\)", "", name).split() if w.lower() not in ("type", "apc")]
    return " ".join({"female": "F", "male": "M"}.get(w.lower(), w) for w in words) or name


def short_step(text: str, conns=()) -> str:
    """Короткий шаг: «Connect APC 3.5 female OPEN to port 1» -> «3.5 F OPEN to P1»."""
    for c in sorted(conns, key=len, reverse=True):
        text = text.replace(c, short_conn(c))
    text = re.sub(r"^\s*connect\s+", "", text, flags=re.I)
    return re.sub(r"\bport\s*(\d+)", r"P\1", text, flags=re.I)


class _StepButton(IndicatorButton):
    """Шаг калибровки: клик - измерить стандарт, индикатор - измерен."""

    def __init__(self, text: str, step: int):
        super().__init__(text)
        self.step, self.done = step, False
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def indicated(self) -> bool:
        return self.done


class CalWizard(QWidget):
    """Калибровка канала выбранного трейса (guided): разъём и набор порта 1 и 2 (P2 OFF - однопортовая),
    START - прибор выдаёт шаги; клик по шагу меряет стандарт (индикатор - измерен); SAVE - в cal set с
    именем NAME и сразу на канал. Набор по умолчанию - первый в списке прибора (идеальные первыми).
    TYPE THRU - калибровка только по перемычке (response) выбранного трейса S21 / S12: один шаг, без наборов."""

    TYPES = ("FULL", "THRU")

    OFF = "OFF"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, prefix: str, parent=None):
        super().__init__(parent)
        self.proxy, self.settings, self.prefix = proxy, settings, prefix
        self.conns: list[str] = []
        self.kits: dict[str, list[str]] = {}
        self._kits_pending: list[str] = []
        self.port_conn = {p: str(settings.value(f"{prefix}/CAL_P{p}_CONN", "") or "") for p in (1, 2)}
        self.port_kit = {p: str(settings.value(f"{prefix}/CAL_P{p}_KIT", "") or "") for p in (1, 2)}
        self.steps: list[str] = []
        self.done: set[int] = set()
        kind = str(settings.value(f"{prefix}/CAL_TYPE", "FULL") or "FULL")
        self.kind = kind if kind in self.TYPES else "FULL"
        self.type_button = self._button(self.kind, "Calibration type: FULL - guided with the cal kits (P1 / P2),\n"
                                                   "THRU - transmission response of the selected S21 / S12 trace",
                                        self.next_type)
        self.trace_label = QLabel("")
        self.trace_label.setObjectName("lineName")
        self.trace_row = None                              # окно: () -> строка выбранного трейса (канал, имя, S, ...)
        self.port_lines: list[QWidget] = []                # P1 / P2 - только для FULL
        self.conn_buttons = {p: MenuButton([], lambda v, p=p: self.set_conn(p, v), f"Port {p} connector")
                             for p in (1, 2)}
        self.kit_buttons = {p: MenuButton([], lambda v, p=p: self.set_kit(p, v), f"Port {p} cal kit")
                            for p in (1, 2)}
        self.start_button = self._button("START", "Start the calibration of the selected trace channel:\n"
                                                  "P1 and P2 - 2-port, P2 OFF - 1-port", self.start)
        self.abort_button = self._button("ABORT", "Stop the calibration, sweeps go on", self.abort)
        self.steps_box = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_box)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(4)
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Name of the new cal set (letters, digits, _ and -)")
        self.save_button = self._button("SAVE", "Save the calibration to the cal set NAME and apply it", self.save)
        proxy.action_done.connect(self._on_action)
        proxy.subscribe("cal.busy", self._on_busy)
        proxy.subscribe("gen.state", lambda _v: self._show())
        proxy.subscribe("trace.sel", lambda _v: self._show())
        self.run_lines: list[QWidget] = []                 # строки шагов и имени - только во время калибровки
        self.relayout = None                               # окно: подогнать высоту (шаги появились / исчезли)
        proxy.state_changed.connect(lambda _s: self._show())
        self._show()

    @staticmethod
    def _button(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("choice")
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # -------------------------------------------------------- разъёмы и наборы
    def refresh(self) -> None:
        if self.proxy.is_connected and "cal_conns" in self.proxy.actions:
            self.proxy.action("cal_conns")

    def _ask_kits(self, conn: str) -> None:
        if conn and conn != self.OFF and conn not in self.kits and conn not in self._kits_pending:
            self._kits_pending.append(conn)
            self.proxy.action("cal_kits", conn)

    def set_conn(self, port: int, conn: str) -> None:
        self.port_conn[port] = conn
        self.settings.setValue(f"{self.prefix}/CAL_P{port}_CONN", conn)
        self._ask_kits(conn)
        self._show()

    def set_kit(self, port: int, kit: str) -> None:
        self.port_kit[port] = kit
        self.settings.setValue(f"{self.prefix}/CAL_P{port}_KIT", kit)
        self._show()

    def conn(self, port: int) -> str:
        """Разъём порта: сохранённый, иначе первый (порт 2 - как порт 1)."""
        c = self.port_conn[port]
        if c == self.OFF or c in self.conns:
            return c
        return self.conn(1) if port == 2 and self.conns else (self.conns[0] if self.conns else "")

    def kit(self, port: int) -> str:
        kits = self.kits.get(self.conn(port), [])
        return self.port_kit[port] if self.port_kit[port] in kits else (kits[0] if kits else "")

    # -------------------------------------------------------- калибровка
    def next_type(self) -> None:
        self.kind = self.TYPES[(self.TYPES.index(self.kind) + 1) % len(self.TYPES)]
        self.settings.setValue(f"{self.prefix}/CAL_TYPE", self.kind)
        self._show()
        if self.relayout is not None:
            self.relayout()

    def thru_trace(self) -> tuple | None:
        """Выбранный трейс, если он годится для калибровки по перемычке (S21 / S12)."""
        row = self.trace_row() if self.trace_row is not None else None
        return row if row is not None and row[2] in ("S21", "S12") else None

    def start(self) -> None:
        if self.kind == "THRU":
            self.proxy.action("cal_start_thru")
            return
        args = []
        for p in (1, 2):
            c = self.conn(p)
            args += ["", ""] if c in ("", self.OFF) else [c, self.kit(p)]
        self.proxy.action("cal_start", *args)

    def measure(self, step: int) -> None:
        self.proxy.action("cal_measure", step)

    def abort(self) -> None:
        self.proxy.action("cal_abort")

    def save(self) -> None:
        name = self.name_edit.text().strip() or self.default_name()
        self.name_edit.setText(name)
        self.proxy.action("cal_save", name)

    def default_name(self) -> str:
        return f"CAL_CH{self.proxy.value('stim.channel') or 1}_{time.strftime('%Y%m%d_%H%M')}"

    def _on_busy(self, busy) -> None:
        if busy is False and self.steps:
            self.proxy.action("cal_status")
        self._show()

    def _on_action(self, name: str, result) -> None:
        if name == "cal_conns" and isinstance(result, (list, tuple)):
            self.conns = [str(c) for c in result]
            for p in (1, 2):
                self._ask_kits(self.conn(p))
        elif name == "cal_kits" and isinstance(result, (list, tuple)) and self._kits_pending:
            self.kits[self._kits_pending.pop(0)] = [str(k) for k in result]
        elif name in ("cal_start", "cal_start_thru", "cal_status") and isinstance(result, (list, tuple)) \
                and len(result) == 2:
            steps, done = result
            if name != "cal_status" or list(steps) != self.steps:
                self.steps = [str(t) for t in steps]
                self._make_steps()
                self.name_edit.setText(self.default_name())
            self.done = {int(k) for k in done}
        elif name in ("cal_save", "cal_abort"):
            self.steps, self.done = [], set()
            self._make_steps()
        else:
            return
        self._show()
        if name != "cal_status" and self.relayout is not None:
            self.relayout()

    def _make_steps(self) -> None:
        while self.steps_layout.count():
            w = self.steps_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for k, text in enumerate(self.steps, 1):
            b = _StepButton(f"{k}. {short_step(text, self.conns)}", k)
            b.setToolTip(f"{text}\nConnect the standard, then click to measure it")
            b.clicked.connect(lambda _=False, k=k: self.measure(k))
            self.steps_layout.addWidget(b)

    def step_buttons(self) -> list[_StepButton]:
        return [self.steps_layout.itemAt(i).widget() for i in range(self.steps_layout.count())]

    def _show(self) -> None:
        on = self.proxy.is_connected
        running = bool(self.steps)
        busy = self.proxy.value("cal.busy") is True
        gen = self.proxy.value("gen.state") is True
        for p in (1, 2):
            choices = [(short_conn(c), c) for c in self.conns] + ([(self.OFF, self.OFF)] if p == 2 else [])
            self.conn_buttons[p].choices = tuple(choices)
            self.conn_buttons[p].show_value(self.conn(p))
            self.conn_buttons[p].setToolTip(f"Port {p} connector: {self.conn(p) or '?'}")
            kits = self.kits.get(self.conn(p), [])
            self.kit_buttons[p].choices = tuple((k, k) for k in kits)
            self.kit_buttons[p].show_value(self.kit(p) or None)
            self.kit_buttons[p].setVisible(self.conn(p) != self.OFF)
            for b in (self.conn_buttons[p], self.kit_buttons[p]):
                b.setEnabled(on and not running)
        thru = self.kind == "THRU"
        self.type_button.setText(self.kind)
        self.type_button.setEnabled(on and not running)
        for line in self.port_lines:
            line.setVisible(not thru)
        row = self.trace_row() if self.trace_row is not None else None
        self.trace_label.setVisible(thru)
        self.trace_label.setText("" if row is None else f"{row[0]}·{row[2]}" + ("" if self.thru_trace() else " ✕"))
        self.trace_label.setToolTip("Selected trace; THRU needs S21 or S12")
        ready = bool(self.thru_trace()) if thru else bool(self.conn(1)) and bool(self.kit(1))
        self.start_button.setEnabled(on and not running and not gen and ready)
        self.abort_button.setEnabled(on and running and not busy)
        for b in self.step_buttons():
            b.done = b.step in self.done
            b.setEnabled(on and not busy)
            b.update()
        complete = running and len(self.done) == len(self.steps)
        self.save_button.setEnabled(on and complete and not busy)
        for line in self.run_lines:
            line.setVisible(running)


class _CalOpenButton(IndicatorButton):
    """CALIBRATE: открыть окно калибровки; индикатор - калибровка идёт."""

    def __init__(self, wizard: "CalWizard"):
        super().__init__("CALIBRATE")
        self.wizard = wizard
        self.setObjectName("choice")
        self.setToolTip("Open the calibration window for the channel of the selected trace")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        wizard.proxy.action_done.connect(lambda *_a: self.update())

    def indicated(self) -> bool:
        return bool(self.wizard.steps)


class CalWindow(Window):
    """Отдельное окно калибровки (guided) канала выбранного трейса: P1 / P2, START / ABORT, шаги, NAME + SAVE.
    Закрытие окна посреди калибровки её прерывает (развёртки продолжаются)."""

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, prefix: str, parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        wz = self.wizard = CalWizard(proxy, settings, prefix)
        wz.run_lines = [PanelLine("STEP", wz.steps_box), PanelLine("NAME", wz.name_edit, wz.save_button)]
        wz.port_lines = [PanelLine("P1", wz.conn_buttons[1], wz.kit_buttons[1]),
                         PanelLine("P2", wz.conn_buttons[2], wz.kit_buttons[2])]
        self.table = PanelTable(PanelLine("TYPE", wz.type_button, wz.trace_label), *wz.port_lines,
                                PanelLine("RUN", wz.start_button, wz.abort_button), *wz.run_lines)
        panel = Panel(self.table)
        self.table.build(UI_COLUMN_WIDTH_PX)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        self._outer.addWidget(panel)
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), self._outer.minimumSize().width()))
        self.recall_size(QSize(max(self.minimumWidth(), CAL_WINDOW_WIDTH), 0))
        wz.relayout = self.fit_height
        proxy.subscribe("stim.channel", lambda _v: self._title())
        self._title()
        wz._show()
        self.fit_height()

    def _title(self) -> None:
        self.setWindowTitle(f"CAL CH{self.proxy.value('stim.channel') or 1}")

    def fit_height(self, again: bool = True) -> None:
        """Высота - по содержимому (шаги появились / исчезли); раскладки пересчитываются не сразу."""
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest.value)
        for w in self.findChildren(QWidget):
            if w.layout() is not None:
                w.layout().invalidate()
        self._outer.invalidate()
        self._outer.activate()
        self.setFixedHeight(self._outer.sizeHint().height())
        if again:
            QTimer.singleShot(0, self._fit_again)

    def _fit_again(self) -> None:
        self.fit_height(False)

    def open(self) -> None:
        self.wizard.refresh()
        self.wizard._show()
        self._title()
        self.show()
        self.raise_()
        self.activateWindow()
        self.fit_height()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if self.wizard.steps and self.proxy.is_connected:
            self.proxy.action("cal_abort")
        super().closeEvent(event)


class VNAWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "pna"
    PRESET_KEY = "VNA_STATES"
    PRESET_DEFAULTS = ()
    PRESET_ADD_TIP = "Save the current state in the analyzer memory"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Network analyzer", parent=None):
        self._select_after_list: str | None = None
        super().__init__(proxy, settings, title, parent)
        # столбики «+»/«−» (TRC, SAVE) - по ширине кнопок, у правого края (вкладки задают ширину столбца)
        for tools in (getattr(self, "_trc_tools", None), getattr(self, "_cal_tools", None),
                      getattr(self, "_pwr_tools", None),
                      getattr(self, "add_button", None) and self.add_button.parentWidget()):
            if tools is not None:
                tools.setMinimumWidth(0)
                tools.setFixedWidth(max(b.sizeHint().width() for b in tools.findChildren(QPushButton)))
        proxy.action_done.connect(self._on_action)
        proxy.sync_done.connect(self.refresh_lists)
        if proxy.is_connected:
            self.refresh_lists()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, sup, pre = self.proxy, self.proxy.supports, self._prefix
        self.widgets: dict[str, Any] = {}
        top = []
        if "meas_table" in proxy.actions:            # трейсы; выбранный задаёт канал POW / CW, STIM, TRIG, CAL
            te = self.trace_editor = TraceEditor(proxy)
            tools = self._trc_tools = QWidget()
            tl = QVBoxLayout(tools)                   # «+» над «−», как у пресетов
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(PRESET_GAP)
            half = (TRACE_CHIP_HEIGHT - PRESET_GAP) // 2
            for text, tip, slot in (("+", "New S21 trace on the channel of the selected one", te.create),
                                    ("−", "Delete the selected trace from the analyzer", te.delete)):
                tl.addWidget(self._tool(text, tip, slot, half))
            top.append(PanelLine("TRC", te.scroll, tools))
        if "mem_store" in proxy.actions:
            mem = [ActionButton(proxy, "mem_store", "CAPTURE", "Copy data to memory for every measurement being read")]
        else:
            mem = []
        if sup("m1.math"):                            # MEM и MATH - одной строкой
            self.math_button = MathButton(proxy)
            mem.append(self.math_button)
        if mem:
            top.append(PanelLine("MEM", *mem))
        if sup("m1.trace"):                           # читаемые трейсы последней развёртки -> CSV на ПК (вкладка SAVE)
            b = self.data_button = QPushButton("SAVE")
            b.setObjectName("choice")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Save every trace being read (last sweep) to a CSV file; folder and name - SAVE tab")
            b.clicked.connect(self.save_data)
        spin, prst = self.power_widgets("source.power", "Preset mode: the wheel and arrows step the power "
                                                        "through the PWR list")
        pow_ws: list[QWidget] = [spin]
        if sup("rf.output"):
            rf = PSButton(proxy, "rf.output", [("RF OFF", False), ("RF ON", True)])
            self.widgets["rf.output"] = rf
            pow_ws.append(rf)
        pow_ws.append(prst)
        top.append(PanelLine("POW", *pow_ws))
        if sup("gen.state") and sup("freq.cw"):
            self.cw_button = CWButton(proxy)
            self.cw_spin = PSpinBox(proxy, "freq.cw", ("k", "M", "G"))
            self.widgets["freq.cw"] = self.cw_spin
            top.append(PanelLine("CW", self.cw_button, self.cw_spin))
        self.top_lines = top
        self.saved_widgets += [self.power_spin, *(w for k, w in self.widgets.items() if k not in NOT_SAVED)]

        # STIM: настройки канала, выбранного в избранном (CH)
        lines = []
        for name, key, extra in STIM_ROWS:
            if not sup(key):
                continue
            ws = [_auto(proxy, key)] + ([_auto(proxy, extra)] if extra and sup(extra) else [])
            for w in ws:
                self.widgets[w.key] = w
                if w.key not in NOT_SAVED:
                    self.saved_widgets.append(w)
            lines.append(PanelLine(name, *ws))
        self.page_specs.append(("STIM", (), PanelTable(*lines)))

        # TRIG: запуск развёрток (источник и вход - общие, режим - у канала CH)
        trig = []
        for name, key, choices, tip in TRIG_ROWS:
            if not sup(key):
                continue
            w = PSButton(proxy, key, choices) if choices else _auto(proxy, key)
            w.setToolTip(tip)
            self.widgets[key] = w
            self.saved_widgets.append(w)
            ws = [w]
            if key == "trig.source" and "trigger" in proxy.actions:
                ws.append(ActionButton(proxy, "trigger", "TRIG", "Manual trigger (SRC = MAN)"))
            trig.append(PanelLine(name, *ws))
        if sup("meas.continuous"):
            cont = PSButton(proxy, "meas.continuous", [("CONT", True), ("SINGLE", False)])
            self.widgets["meas.continuous"] = cont
            mode = [cont] + ([ActionButton(proxy, "single", "SWEEP", "Single sweep")] if "single" in proxy.actions
                             else [])
            trig.append(PanelLine("RUN", *mode))
        if trig:
            self.page_specs.append(("TRIG", (), PanelTable(*trig)))

        # PWR
        prst = self.power_levels_line("source.power")
        pwr = [PanelLine("POW", _auto(proxy, "source.power"))]
        if sup("rf.output"):
            pwr.append(PanelLine("RF", PSButton(proxy, "rf.output", ONOFF)))
        pwr.append(prst)
        self.page_specs.append(("PWR", ("rf.output",) if sup("rf.output") else (), PanelTable(*pwr)))

        # CAL: калибровка канала выбранного трейса - cal set из памяти прибора
        if sup("cal.set") and "cal_list" in proxy.actions:
            cs = self.cal_sets = CalSets(proxy)
            tools = QWidget()
            tl = QVBoxLayout(tools)                   # «↻» над «−», как у пресетов
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(PRESET_GAP)
            half = (_iw.PRESET_HEIGHT - PRESET_GAP) // 2
            tl.addWidget(self._tool("↻", "Re-read the cal sets of the analyzer", cs.refresh, half))
            if "cal_delete" in proxy.actions:
                cs.delete_button = self._tool("−", "Delete the cal set of this channel from the analyzer "
                                                   "(click twice)", cs.delete, half)
                tl.addWidget(cs.delete_button)
            tl.addStretch(1)
            self._cal_tools = tools
            cal = [PanelLine("CSET", cs, tools)]
            for name, key, choices, tip in (
                    ("STIM", "cal.stim", [("CAL", True), ("KEEP", False)],
                     "Applying a cal set: CAL - take its stimulus too (start, stop, points, IFBW, power),\n"
                     "KEEP - keep the channel stimulus (interpolated if INT is on)"),
                    ("CORR", "cal.state", [("OFF", False), ("ON", True)], "Correction on the channel"),
                    ("INT", "cal.interp", ONOFF, "Interpolate the calibration when the stimulus differs")):
                if sup(key):
                    w = PSButton(proxy, key, choices)
                    w.setToolTip(tip)
                    self.widgets.setdefault(key, w)
                    cal.append(PanelLine(name, w))
            if "cal_start" in proxy.actions:          # сама калибровка - в отдельном окне
                self.cal_window = CalWindow(proxy, self.settings, pre)
                self.cal_wizard = self.cal_window.wizard
                if hasattr(self, "trace_editor"):
                    self.cal_wizard.trace_row = self.trace_editor.row
                    self.trace_editor.chips.selected.connect(lambda _i: self.cal_wizard._show())
                b = self.cal_button = _CalOpenButton(self.cal_wizard)
                b.clicked.connect(self.open_cal_window)
                cal.append(PanelLine("NEW", b))
            cs.relayout = self._fit_height
            self.page_specs.append(("CAL", ("cal.state",), PanelTable(*cal)))

        # TRAC (первая вкладка): трейс, выбранный в избранном (TRC)
        if "meas_table" in proxy.actions:
            te = self.trace_editor
            trac = [PanelLine("CH", te.channel, te.read), PanelLine("SPAR", te.sparam),
                    PanelLine("FMT", te.format), PanelLine("MATH", te.math)]
            quick = [ActionButton(proxy, a, t, tip) for a, t, tip in
                     (("meas_all", "ALL", "Read every trace (first 16)"), ("meas_none", "NONE", "Read nothing"))
                     if a in proxy.actions]
            if quick:
                trac.append(PanelLine("SET", *quick))
            self.page_specs.insert(0, ("TRAC", (), PanelTable(*trac)))

        # SCAL: масштаб выбранного трейса на экране прибора, задержка и сглаживание (меняют данные)
        if sup("trace.sel"):
            scal = []
            if "autoscale" in proxy.actions:
                scal.append(PanelLine("AUTO", ActionButton(proxy, "autoscale", "AUTOSCALE",
                                                           "Autoscale the selected trace on the analyzer screen")))
            for name, keys, tip in (("DIV", ("scale.div",), "Scale per division, units of the trace format"),
                                    ("REF", ("scale.ref",), "Reference level"),
                                    ("RPOS", ("scale.rpos",), "Reference position, divisions from the bottom"),
                                    ("EDEL", ("scale.edel",), "Electrical delay: removes the phase slope of a "
                                                              "cable (changes the data read)"),
                                    ("SMO", ("smooth.state", "smooth.aper"), "Smoothing on/off and aperture, % "
                                                                             "of span (changes the data read)")):
                ws = [_auto(proxy, k) for k in keys if sup(k)]
                for w in ws:
                    w.setToolTip(tip)
                    self.widgets[w.key] = w
                if ws:
                    scal.append(PanelLine(name, *ws))
            self.page_specs.insert(1 if "meas_table" in proxy.actions else 0, ("SCAL", (), PanelTable(*scal)))

        # MARK: маркеры выбранного трейса (после SCAL)
        if sup("mark.n") and "mark_table" in proxy.actions:
            mp = self.marker_panel = MarkerPanel(proxy, self.trace_editor.row if hasattr(self, "trace_editor")
                                                 else None)
            state = PSButton(proxy, "mark.state", ONOFF)
            state.setToolTip("Selected marker on / off")
            mx = PSpinBox(proxy, "mark.x", ("k", "M", "G"))
            mx.setToolTip("Marker position")
            self.widgets["mark.state"], self.widgets["mark.x"] = state, mx
            row = QWidget()                           # STAT · X · Y одной строкой (один столбец таблицы)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            for w in (state, mx, mp.value):
                w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            rl.addWidget(state, 2)
            rl.addWidget(mx, 4)
            rl.addWidget(mp.value, 3)
            mp.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            mark = [PanelLine("MKR", mp), PanelLine("STAT", row)]
            search = [ActionButton(proxy, a, t, tip) for a, t, tip in
                      (("mark_peak", "MAX", "Selected marker to the maximum"),
                       ("mark_min", "MIN", "Selected marker to the minimum")) if a in proxy.actions]
            if search:
                mark.append(PanelLine("SRCH", *search))
            if "mark_aoff" in proxy.actions:
                mark.append(PanelLine("ALL", ActionButton(proxy, "mark_aoff", "OFF", "All markers of the trace off")))
            pos = next((i + 1 for i, (n, *_r) in enumerate(self.page_specs) if n == "SCAL"), len(self.page_specs))
            self.page_specs.insert(pos, ("MARK", ("mark.state",), PanelTable(*mark)))

        # SAVE: чипы состояний в памяти прибора
        save = []
        if "state_list" in proxy.actions:             # состояния в памяти анализатора: папка на его диске и чипы
            if sup("state.dir"):
                vdir = PLineEdit(proxy, "state.dir")
                vdir.setToolTip("Folder on the analyzer disk with the state files (*.csa), e.g.\n"
                                "C:\\Users\\Public\\Documents\\Network Analyzer")
                self.widgets["state.dir"] = vdir
                ws = [vdir]
                if "state_dirs" in proxy.actions:     # … - выбрать папку из подпапок анализатора
                    self.vdir_button = self._tool("…", "Choose the folder on the analyzer", self._ask_dirs)
                    self.vdir_button.setFixedWidth(TRACE_CHIP_HEIGHT)
                    ws.append(self.vdir_button)
                save.append(PanelLine("VNA", *ws))
                proxy.subscribe("state.dir", lambda _v: self.proxy.is_connected and self.proxy.action("state_list"))
            save.append(self.preset_line("STAT"))
        if sup("m1.trace"):                           # куда и под каким именем DATA SAVE пишет CSV
            self.dir_edit = QLineEdit(str(self.data_dir()))
            self.dir_edit.setToolTip("Folder on this PC for DATA SAVE files")
            self.dir_edit.editingFinished.connect(
                lambda: self.settings.setValue(f"{pre}/DATA_DIR", self.dir_edit.text().strip()))
            browse = self._tool("…", "Choose the folder", self._browse_dir)
            browse.setFixedWidth(TRACE_CHIP_HEIGHT)
            self.name_edit = QLineEdit(str(self.settings.value(f"{pre}/DATA_NAME", "")))
            self.name_edit.setPlaceholderText("file name prefix, e.g. DUT")
            self.name_edit.setToolTip("Prefix of the file name; date and time are added: <prefix>_YYYYMMDD_HHMMSS.csv")
            self.name_edit.editingFinished.connect(
                lambda: self.settings.setValue(f"{pre}/DATA_NAME", self.name_edit.text().strip()))
            save += [PanelLine("PC", self.dir_edit, browse), PanelLine("NAME", self.name_edit),
                     PanelLine("DATA", self.data_button)]
            if "snp_read" in proxy.actions:           # S-параметры канала выбранного трейса -> .s2p
                b = self.s2p_button = QPushButton("SAVE")
                b.setObjectName("choice")
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip("Save S11 S21 S12 S22 of the selected trace channel to a Touchstone .s2p file\n"
                             "(one fresh sweep; folder and name - PC / NAME)")
                b.clicked.connect(self.save_s2p)
                for sig in (proxy.comm_error, proxy.instrument_error):     # snp_read не прошёл
                    sig.connect(lambda *_a: self.s2p_button.text() == "…" and self._flash("ERROR", self.s2p_button))
                save.append(PanelLine("S2P", b))
        if save:
            self.page_specs.append(("SAVE", (), PanelTable(*save)))

    def open_cal_window(self) -> None:
        w = self.cal_window
        if not w.isVisible():
            w.move(self.pos() + QPoint(self.width() + 8, 0))
        w.open()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if getattr(self, "cal_window", None) is not None:
            self.cal_window.close()
        super().closeEvent(event)

    # ------------------------------------------------------------ папка состояний на анализаторе
    _dirs_asked = False

    def _ask_dirs(self) -> None:
        if self.proxy.is_connected:
            self._dirs_asked = True
            self.proxy.action("state_dirs")

    @staticmethod
    def dir_choices(path: str, subdirs: list[str]) -> list[tuple[str, str]]:
        """Пункты меню папок: «..» (на уровень выше, если есть) и подпапки -> (подпись, полный путь)."""
        path = path.rstrip("\\")
        out = []
        if "\\" in path:
            parent = path.rsplit("\\", 1)[0]
            out.append(("..", parent + "\\" if parent.endswith(":") else parent))
        base = path + "\\"
        return out + [(d, base + d) for d in subdirs]

    def _dirs_menu(self, path: str, subdirs: list[str]) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        title = menu.addAction(path)
        title.setEnabled(False)
        menu.addSeparator()
        choices = self.dir_choices(path, subdirs)
        for text, full in choices:
            menu.addAction(text).triggered.connect(lambda _=False, f=full: self.proxy.set("state.dir", f))
        if not choices:
            menu.addAction("(no folders)").setEnabled(False)
        b = self.vdir_button                          # правым краем к кнопке: «…» стоит у правого края окна
        menu.popup(b.mapToGlobal(QPoint(b.width() - menu.sizeHint().width(), b.height())))
        self._dirs_popup = menu

    # ------------------------------------------------------------ данные в файл (DATA SAVE)
    def data_dir(self) -> Path:
        saved = str(self.settings.value(f"{self._prefix}/DATA_DIR", "") or "").strip()
        return Path(saved) if saved else Path(self.settings.fileName()).resolve().parent / "vna_data"

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for VNA data", str(self.data_dir()))
        if d:
            self.dir_edit.setText(d)
            self.settings.setValue(f"{self._prefix}/DATA_DIR", d)

    def read_traces(self) -> list[tuple[str, Any]]:
        """Трейсы последней развёртки (читаемые слоты): [(подпись, Trace)]."""
        out = []
        for n in range(1, SLOTS + 1):
            t = self.proxy.value(f"m{n}.trace") if self.proxy.supports(f"m{n}.trace") else None
            if t is None:
                continue
            m = t.meta
            fmt = next((name for name, d in FORMATS if d == m.get("format")), m.get("format") or "")
            label = f"{fmt} {m.get('channel', '?')}·{m.get('sparam', t.name)}"
            if m.get("math") not in (None, "NORM"):
                label += " " + next((name for name, d in MATH if d == m["math"]), m["math"])
            out.append((label, t))
        return out

    def save_data(self) -> Path | None:
        """DATA SAVE: все читаемые трейсы в один CSV (папка и префикс имени - вкладка SAVE)."""
        traces = self.read_traces()
        if not traces:
            self._flash("NO DATA")
            return None
        path = self._data_path("", "csv")
        comments = [f"{self.windowTitle() or 'VNA'}, saved {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for label, t in traces:
            m = t.meta
            extra = ", ".join(f"{k} {m[k]}" for k in ("bw.if", "source.power", "avg.state", "avg.count", "cal.state")
                              if m.get(k) is not None)
            comments.append(f"{label}: channel {m.get('channel')}, measurement '{m.get('name')}', {len(t)} points"
                            + (f", {extra}" if extra else ""))
        try:
            write_traces_csv(path, [t for _l, t in traces], [lbl for lbl, _t in traces], comments)
        except OSError as e:
            self._flash("ERROR")
            self.data_button.setToolTip(f"Not saved: {e}")
            return None
        self._flash("SAVED")
        self.data_button.setToolTip(f"Last file: {path}")
        return path

    def _flash(self, text: str, b: QPushButton | None = None) -> None:
        """Итог на кнопке на 1.5 с (таймер - дочерний кнопки: умирает вместе с окном)."""
        b = b or self.data_button
        timer = getattr(b, "_flash_timer", None)
        if timer is None:
            timer = b._flash_timer = QTimer(b)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: b.setText("SAVE"))
        b.setText(text)
        timer.start(1500)

    def _data_path(self, tag: str, ext: str) -> Path:
        """Новый файл в папке PC: <NAME>_<tag>_<дата_время>.<ext> (без перезаписи)."""
        name = (getattr(self, "name_edit", None) and self.name_edit.text().strip()) or ""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = re.sub(r"[^\w.-]+", "_", "_".join(p for p in (name, tag, stamp) if p))
        folder = Path(self.dir_edit.text().strip()) if getattr(self, "dir_edit", None) and \
            self.dir_edit.text().strip() else self.data_dir()
        path, k = folder / f"{base}.{ext}", 2
        while path.exists():
            path, k = folder / f"{base}-{k}.{ext}", k + 1
        return path

    def save_s2p(self) -> None:
        """S2P SAVE: прибор читает S-параметры канала (snp_read), файл пишется по ответу."""
        if self.proxy.is_connected:
            self.s2p_button.setText("…")
            self.proxy.action("snp_read", "1,2")

    def _write_s2p(self, sparams: dict) -> Path | None:
        t = next(iter(sparams.values()))
        m = t.meta
        path = self._data_path(f"CH{m.get('channel', '')}", "s2p")
        comments = [f"{self.windowTitle() or 'VNA'}, saved {time.strftime('%Y-%m-%d %H:%M:%S')}",
                    f"channel {m.get('channel')}, {len(t)} points, cal set {m.get('cal.set') or '-'}, "
                    f"correction {'ON' if m.get('cal.state') else 'OFF'}, IFBW {m.get('bw.if')}, "
                    f"power {m.get('source.power')} dBm"]
        try:
            write_touchstone(path, sparams, comments)
        except (OSError, ValueError) as e:
            self._flash("ERROR", self.s2p_button)
            self.s2p_button.setToolTip(f"Not saved: {e}")
            return None
        self._flash("SAVED", self.s2p_button)
        self.s2p_button.setToolTip(f"Last file: {path}")
        return path

    def restore_rank(self, key: str) -> int:
        if key.endswith(("_auto", "_state")) or key == "avg.count":
            return 3
        return RESTORE_RANK.get(key, 2)

    # ------------------------------------------------------------ мощность: пресеты
    # ------------------------------------------------------------ состояния в приборе (вкладка SAVE)
    def create_presets(self, key: str) -> PresetGroup:
        return StatePresets(self.settings, key, self._delete_state)

    def refresh_lists(self) -> None:
        if hasattr(self, "cal_wizard"):
            self.cal_wizard.refresh()
        for name in ("state_list", "meas_table", "cal_list", "mark_table"):
            if name in self.proxy.actions:
                self.proxy.action(name)

    def state_name(self) -> str | None:
        if self.proxy.value("sweep.type") == "CW":
            f = self.proxy.value("freq.cw")
            parts = ["CW", f]
        else:
            parts = [self.proxy.value("freq.start"), self.proxy.value("freq.stop")]
        if any(v is None for v in parts):
            return None
        text = lambda v: v if isinstance(v, str) else si_format(v, "Hz", 4).replace(" ", "").replace(".", "p")
        base, name, n = "_".join(text(v) for v in parts), None, 1
        while name is None or name in self.presets.names():
            name = base if n == 1 else f"{base}-{n}"
            n += 1
        return name

    def _add_preset(self) -> None:
        name = self.state_name()
        if name is not None:
            self._select_after_list = name
            self.proxy.action("state_save", name)
            self.refresh_lists()

    def _delete_state(self, name: str) -> None:
        self.proxy.action("state_delete", name)
        self.refresh_lists()

    def apply_preset_values(self, preset: tuple) -> None:
        self.proxy.action("state_recall", preset[0])

    def _on_action(self, name: str, result) -> None:
        if name == "snp_read" and isinstance(result, dict) and result and hasattr(self, "s2p_button"):
            self._write_s2p(result)
        elif name == "state_dirs" and self._dirs_asked and isinstance(result, (list, tuple)) and len(result) == 2:
            self._dirs_asked = False
            self._dirs_menu(str(result[0]), [str(d) for d in result[1]])
        elif name == "state_list" and isinstance(result, (list, tuple)):
            self.presets.set_names([str(n) for n in result], self._select_after_list)
            self._select_after_list = None
        elif name == "state_recall" and "meas_table" in self.proxy.actions:
            self.proxy.action("meas_table")
