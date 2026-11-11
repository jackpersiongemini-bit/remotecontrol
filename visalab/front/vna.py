"""Окно векторного анализатора цепей (N5231A и любой прибор профиля network_analyzer): только настройки.

Трассы здесь не выводятся (для этого будет отдельный виджет). Избранное:

* MEM  - DATA→MEM: данные -> память у всех читаемых измерений;
* MATH - математика с памятью у всех читаемых измерений: DATA, D/M, D−M, D+M, D·M;
* POW  - мощность источника (колесо / стрелки - разряд под курсором), RF ON/OFF и PRST: в режиме
  пресетов колесо и стрелки переключают мощность только по списку из вкладки PWR;
* CW   - одна частота (ВАЦ как генератор): CW включает тип развёртки CW, повторный клик
  возвращает прежний тип; рядом частота CW;
* CORR - коррекция (калибровка) ON / OFF.

Вкладки: TRAC (сверху чипы всех трейсов прибора, как пресеты: клик выбирает, + создаёт трейс S21 на
канале выбранного, − удаляет выбранный с прибора; под ними выбранный трейс: CH - список каналов (другой
канал переносит трейс, пересоздавая там), READ, SPAR, FMT, MATH; ALL / NONE; слоты не видны),
STIM (стимул и развёртка), PWR (мощность, RF, список пресетов мощности), SAVE (состояния в памяти прибора: чип -
вызвать, + сохранить, − удалить; в ini только порядок чипов).
Окно знает только InstrumentProxy и ключи профиля network_analyzer.
"""
from __future__ import annotations

import re
from typing import Any

from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtWidgets import QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..core.params import Kind
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import PRESET_GAP, PRESET_HEIGHT, HScrollArea, InstrumentWindow, PresetGroup
from .params import IndicatorButton, PBGroup, PLineEdit, PSButton, PSpinBox, PanelLine, PanelTable
from .power_meter import ActionButton
from .spectrum import StatePresets
from .theme import UI_COLUMN_WIDTH_PX

__all__ = ["VNAWindow", "PowerSpin", "MathButton", "CWButton", "TraceEditor", "MenuButton", "parse_power_list",
           "saved_connection"]

CONNECTION_GROUP = "CONNECTION/VNA"
ONOFF = [("OFF", False), ("ON", True)]
AUTO = [("MAN", False), ("AUTO", True)]
SLOTS = 16
MATH = (("DATA", "NORM"), ("D/M", "DIV"), ("D−M", "SUBT"), ("D+M", "ADD"), ("D·M", "MULT"))
DEFAULT_POWER_LIST = (-20.0, -10.0, -5.0, 0.0)

#: вкладка STIM: (подпись, ключ, второй ключ или None)
STIM_ROWS = (("STAR", "freq.start", None), ("STOP", "freq.stop", None), ("CENT", "freq.center", None),
             ("SPAN", "freq.span", None), ("PTS", "sweep.points", None), ("IFBW", "bw.if", None),
             ("TIME", "sweep.time", "sweep.time_auto"), ("TYPE", "sweep.type", None),
             ("AVG", "avg.state", "avg.count"))
NOT_SAVED = ("freq.center", "freq.span", "cal.state")       # центр/обзор следуют из старта/стопа; калибровку не вернуть
RESTORE_RANK = {"freq.start": 0, "freq.stop": 0, "sweep.type": 1, "freq.cw": 1, "rf.output": 5}


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def parse_power_list(text: str) -> list[float]:
    """``"-20, -10 0;5"`` -> [-20, -10, 0, 5] (по возрастанию, без повторов). ValueError при мусоре."""
    out = set()
    for t in re.split(r"[\s,;]+", text.strip()):
        if t:
            out.add(float(t))
    return sorted(out)


def _auto(proxy: InstrumentProxy, key: str):
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, AUTO if key.endswith("_auto") else ONOFF)
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


class PowerSpin(PSpinBox):
    """Мощность: колесо / стрелки - разряд под курсором, а в режиме пресетов - соседнее значение списка."""

    def __init__(self, proxy: InstrumentProxy, key: str, parent=None):
        super().__init__(proxy, key, parent=parent)
        self.preset_mode = False
        self.levels: list[float] = list(DEFAULT_POWER_LIST)

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt
        if not self.preset_mode or not self.levels:
            super().stepBy(steps)
            return
        cur = self._value if isinstance(self._value, (int, float)) else self.value()
        if steps > 0:
            above = [v for v in self.levels if v > cur + 1e-9]
            target = above[min(steps, len(above)) - 1] if above else self.levels[-1]
        else:
            below = [v for v in self.levels if v < cur - 1e-9]
            target = below[-min(-steps, len(below))] if below else self.levels[0]
        self.show_value(target, force=True)
        self.set_value(target)


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
    """CW (одна частота): включена, когда тип развёртки CW; повторный клик - прежний тип (ini)."""

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, prefix: str, parent=None):
        super().__init__("CW", parent)
        self.proxy, self.settings, self._key = proxy, settings, f"{prefix}/LAST_SWEEP_TYPE"
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("CW: one frequency, the analyzer works as a generator. Click again to restore the sweep")
        self._type: str | None = None
        self.last_type = str(settings.value(self._key, "LIN"))
        self.clicked.connect(self.toggle)
        proxy.subscribe("sweep.type", self._on_type)

    def indicated(self) -> bool:
        return self._type == "CW"

    def _on_type(self, value) -> None:
        self._type = value
        if value and value != "CW":
            self.last_type = str(value)
            self.settings.setValue(self._key, self.last_type)
        self.update()

    def toggle(self) -> None:
        if not self.proxy.is_connected or self._type is None:
            return
        self.proxy.set("sweep.type", self.last_type if self._type == "CW" else "CW")


SPARAMS = ("S11", "S21", "S12", "S22")
#: формат профиля -> короткая подпись
FORMATS = (("MLOG", "MLOG"), ("PHAS", "PHASE"), ("UPH", "UPHASE"), ("GDEL", "GDELAY"), ("SWR", "SWR"),
           ("MLIN", "LIN_MAG"), ("REAL", "REAL"), ("IMAG", "IMAG"))


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
    не рисуется: выбор и включение не смешиваются). Высота - как у «+»/«−», ширина - вся строка."""

    def __init__(self, owner: "TraceEditor", key: tuple[int, str]):
        super().__init__()
        self.owner, self.trace_key = owner, key
        self.setObjectName("chip")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):  # noqa: N802 - Qt; QSS чипа (min/max-height) иначе перебивает setFixedHeight
        return QSize(super().sizeHint().width(), PRESET_HEIGHT)

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
    """Вкладка TRAC: сверху чипы всех трейсов прибора (по всем каналам), под ними - выбранный трейс:
    канал (список: перенести в другой канал), READ, S-параметр, формат, математика.
    Слоты не видны: READ сам занимает свободный (до 16 читаемых)."""

    def __init__(self, proxy: InstrumentProxy, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.table: list[tuple[int, str, str, str, str]] = []
        self.current: tuple[int, str] | None = None
        self.chips = TraceChips(self)
        self.chips.selected.connect(self._chip_clicked)
        self.scroll = HScrollArea(self.chips)
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
                self.current = keys[0] if keys else None
            self.chips.updateGeometry()
        self._show_current()

    def _show_current(self) -> None:
        for b, (c, n, sp, fmt, math) in zip(self.chips.buttons(), self.table):
            b.setText(f"C{c} {sp}\n{next((n for n, d in FORMATS if d == fmt), fmt)}")
            b.setToolTip(f"Channel {c}, measurement {n!r}\n{sp} {fmt} {math}")
            b.update()
        keys = [(c, n) for c, n, *_ in self.table]
        self.chips.set_checked(keys.index(self.current) if self.current in keys else -1)
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


class VNAWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "pna"
    PRESET_KEY = "VNA_STATES"
    PRESET_DEFAULTS = ()
    PRESET_ADD_TIP = "Save the current state in the analyzer memory"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Network analyzer", parent=None):
        self._select_after_list: str | None = None
        super().__init__(proxy, settings, title, parent)
        proxy.action_done.connect(self._on_action)
        proxy.sync_done.connect(self.refresh_lists)
        if proxy.is_connected:
            self.refresh_lists()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, sup, pre = self.proxy, self.proxy.supports, self._prefix
        self.widgets: dict[str, Any] = {}
        top = []
        if "mem_store" in proxy.actions:
            top.append(PanelLine("MEM", ActionButton(proxy, "mem_store", "DATA→MEM",
                                                     "Copy data to memory for every measurement being read")))
        if sup("m1.math"):
            self.math_button = MathButton(proxy)
            top.append(PanelLine("MATH", self.math_button))
        self.power_spin = PowerSpin(proxy, "source.power")
        self.power_spin.levels = self._load_levels()
        pow_ws: list[QWidget] = [self.power_spin]
        if sup("rf.output"):
            rf = PSButton(proxy, "rf.output", [("RF OFF", False), ("RF ON", True)])
            self.widgets["rf.output"] = rf
            pow_ws.append(rf)
        self.prst_button = IndicatorButton("PRST")
        self.prst_button.setObjectName("choice")
        self.prst_button.setCheckable(True)
        self.prst_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.prst_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prst_button.setToolTip("Preset mode: the wheel and arrows step the power through the PWR list")
        self.prst_button.toggled.connect(self._set_preset_mode)
        self.prst_button.setChecked(self.settings.value(f"{pre}/POWER_PRESET_MODE", False, type=bool))
        pow_ws.append(self.prst_button)
        top.append(PanelLine("POW", *pow_ws))
        if sup("sweep.type") and sup("freq.cw"):
            self.cw_button = CWButton(proxy, self.settings, pre)
            self.cw_spin = PSpinBox(proxy, "freq.cw", ("k", "M", "G"))
            self.widgets["freq.cw"] = self.cw_spin
            top.append(PanelLine("CW", self.cw_button, self.cw_spin))
        if sup("cal.state"):
            corr = PSButton(proxy, "cal.state", [("CORR OFF", False), ("CORR ON", True)])
            self.widgets["cal.state"] = corr
            top.append(PanelLine("CORR", corr))
        self.top_lines = top
        self.saved_widgets += [self.power_spin, *(w for k, w in self.widgets.items() if k not in NOT_SAVED)]

        # STIM
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
        if sup("meas.continuous"):
            cont = PSButton(proxy, "meas.continuous", [("CONT", True), ("SINGLE", False)])
            self.widgets["meas.continuous"] = cont
            mode = [cont] + ([ActionButton(proxy, "single", "SWEEP", "Single sweep")] if "single" in proxy.actions
                             else [])
            lines.append(PanelLine("MODE", *mode))
        self.page_specs.append(("STIM", (), PanelTable(*lines)))

        # PWR
        self.levels_edit = QLineEdit(", ".join(f"{v:g}" for v in self.power_spin.levels))
        self.levels_edit.setToolTip("Power presets, dBm, separated by commas: the PRST mode steps through them")
        self.levels_edit.editingFinished.connect(self._levels_edited)
        pwr = [PanelLine("POW", _auto(proxy, "source.power"))]
        if sup("rf.output"):
            pwr.append(PanelLine("RF", PSButton(proxy, "rf.output", ONOFF)))
        pwr.append(PanelLine("LIST", self.levels_edit))
        self.page_specs.append(("PWR", ("rf.output",) if sup("rf.output") else (), PanelTable(*pwr)))

        # TRAC (первая вкладка): чипы всех трейсов прибора, под ними - выбранный трейс
        if "meas_table" in proxy.actions:
            te = self.trace_editor = TraceEditor(proxy)
            half = (PRESET_HEIGHT - PRESET_GAP) // 2
            tools = QWidget()
            tl = QVBoxLayout(tools)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(PRESET_GAP)
            tl.addWidget(self._tool("+", "New S21 trace on the channel of the selected one", te.create, half))
            tl.addWidget(self._tool("−", "Delete the selected trace from the analyzer", te.delete, half))
            chips = PanelLine("TRC", te.scroll, tools)
            chips.line_layout.setAlignment(chips._name, Qt.AlignmentFlag.AlignTop)
            trac = [chips, PanelLine("CH", te.channel, te.read), PanelLine("SPAR", te.sparam),
                    PanelLine("FMT", te.format), PanelLine("MATH", te.math)]
            quick = [ActionButton(proxy, a, t, tip) for a, t, tip in
                     (("meas_all", "ALL", "Read every trace (first 16)"), ("meas_none", "NONE", "Read nothing"))
                     if a in proxy.actions]
            if quick:
                trac.append(PanelLine("SET", *quick))
            if sup("meas.complex"):
                trac.append(PanelLine("CPLX", PSButton(proxy, "meas.complex", ONOFF)))
            self.page_specs.insert(0, ("TRAC", (), PanelTable(*trac)))

        # SAVE: чипы состояний в памяти прибора
        if "state_list" in proxy.actions:
            self.page_specs.append(("SAVE", (), PanelTable(self.preset_line("STAT"))))

    def restore_rank(self, key: str) -> int:
        if key.endswith(("_auto", "_state")) or key == "avg.count":
            return 3
        return RESTORE_RANK.get(key, 2)

    # ------------------------------------------------------------ мощность: пресеты
    def _load_levels(self) -> list[float]:
        raw = self.settings.value(f"{self._prefix}/POWER_LIST", "")
        try:
            return parse_power_list(str(raw)) or list(DEFAULT_POWER_LIST)
        except ValueError:
            return list(DEFAULT_POWER_LIST)

    def _levels_edited(self) -> None:
        try:
            levels = parse_power_list(self.levels_edit.text())
        except ValueError:
            levels = []
        if levels:
            self.power_spin.levels = levels
            self.settings.setValue(f"{self._prefix}/POWER_LIST", ", ".join(f"{v:g}" for v in levels))
        self.levels_edit.setText(", ".join(f"{v:g}" for v in self.power_spin.levels))

    def _set_preset_mode(self, on: bool) -> None:
        self.power_spin.preset_mode = bool(on)
        self.settings.setValue(f"{self._prefix}/POWER_PRESET_MODE", bool(on))

    # ------------------------------------------------------------ состояния в приборе (вкладка SAVE)
    def create_presets(self, key: str) -> PresetGroup:
        return StatePresets(self.settings, key, self._delete_state)

    def refresh_lists(self) -> None:
        for name in ("state_list", "meas_table"):
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
        if name == "state_list" and isinstance(result, (list, tuple)):
            self.presets.set_names([str(n) for n in result], self._select_after_list)
            self._select_after_list = None
        elif name == "state_recall" and "meas_table" in self.proxy.actions:
            self.proxy.action("meas_table")
