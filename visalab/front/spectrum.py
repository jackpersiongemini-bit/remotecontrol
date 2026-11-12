"""Окно анализатора спектра (E4446A и любой прибор профиля spectrum_analyzer): только настройки.

Трасса и вся измерительная информация выводятся отдельным виджетом (его ещё нет), здесь -
избранное и настройки. В избранном одни пресеты, и это состояния в памяти САМОГО анализатора:
``+`` сохраняет текущее состояние (имя - из центра и обзора, например 2p4GHz_100MHz), чип
вызывает его, ``-`` удаляет из прибора. Список читается из прибора при каждой синхронизации,
поэтому пресеты, сохранённые на приборе раньше или с другого компьютера, появляются сами.
В ini лежит только порядок чипов (последние использованные - первыми).

Вкладки: FREQ (центр, обзор, старт, стоп, ZERO - нулевая полоса одной кнопкой), BW (RBW, VBW),
AMPL (опорный уровень и его смещение, масштаб, аттенюатор, детектор, предусилитель, связь входа),
SWP (время, точки, тип развёртки, опора, непрерывно / одна развёртка), TRAC (режим трассы:
clear-write / max hold / min hold / view / blank, усреднение), TRIG (источник, уровень, фронт, задержка).
Окно знает только прибор координатора (Instrument) и ключи профиля spectrum_analyzer.
"""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import QSettings, Qt

from ..coord import Instrument
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow, PresetGroup
from .params import IndicatorButton, PLineEdit, PSButton, PanelLine, PanelTable
from .power_meter import ActionButton

__all__ = ["SpectrumWindow", "ZeroSpan", "StatePresets", "saved_connection", "compact_si"]

CONNECTION_GROUP = "CONNECTION/SPECTRUM"
AUTO = [("MAN", False), ("AUTO", True)]
ONOFF = [("OFF", False), ("ON", True)]

#: вкладки: (название, строки); строка - (подпись, ключ значения, ключ второго виджета строки или None);
#: второй виджет: ``*_auto`` - MAN / AUTO, другое булево - OFF / ON, не булево - поле
PAGES = (
    ("FREQ", (("CENT", "freq.center", None), ("SPAN", "freq.span", None),
              ("STAR", "freq.start", None), ("STOP", "freq.stop", None))),
    ("BW", (("RBW", "bw.res", "bw.res_auto"), ("VBW", "bw.video", "bw.video_auto"))),
    ("AMPL", (("RLEV", "ref_level", None), ("ROFF", "ref_offset", None), ("SDIV", "scale_div", None),
              ("ATT", "atten", "atten_auto"), ("DET", "detector", None), ("PRE", "preamp", None),
              ("CPL", "input.coupling", None))),
    ("SWP", (("TIME", "sweep.time", "sweep.time_auto"), ("PTS", "sweep.points", None),
             ("TYPE", "sweep.type", None), ("REF", "ref.source", None))),
    ("TRAC", (("MODE", "trace.mode", None), ("AVG", "avg.state", "avg.count"))),
    ("TRIG", (("SRC", "trig.source", None), ("LVL", "trig.level", None), ("SLOP", "trig.slope", None),
              ("DLY", "trig.delay", "trig.delay_state"))),
)
NOT_SAVED = ("freq.start", "freq.stop")          # связаны с центром и обзором - после Init не восстанавливаем


def compact_si(value: float, digits: int = 3) -> str:
    """Число с SI-префиксом вместо точки: 2.4e9 -> ``2G4``, 1e9 -> ``1G``, 1.5e6 -> ``1M5``, 0 -> ``0``."""
    if not value:
        return "0"
    power = max(-4, min(4, math.floor(math.log10(abs(value)) / 3)))
    scaled = float(f"{abs(value) / 1000 ** power:.{digits}g}")
    if scaled >= 1000 and power < 4:                       # 999.9M -> 1G
        power, scaled = power + 1, scaled / 1000
    whole, _, frac = f"{scaled:.{digits}g}".partition(".")
    prefix = {-4: "P", -3: "N", -2: "U", -1: "M", 0: "", 1: "K", 2: "M", 3: "G", 4: "T"}[power]
    if power == -1:
        prefix = "m"                                       # милли - строчная (не путать с M)
    return ("-" if value < 0 else "") + whole + ((prefix or "R") + frac if frac else prefix)


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


class ZeroSpan(IndicatorButton):
    """Нулевая полоса одной кнопкой: включена, когда SPAN = 0. Повторный клик возвращает прежний обзор
    (последний ненулевой, хранится в ini)."""

    DEFAULT_SPAN = 1e6

    def __init__(self, proxy: Instrument, settings: QSettings, prefix: str, parent=None):
        super().__init__("ZERO", parent)
        self.proxy, self.settings, self._key = proxy, settings, f"{prefix}/LAST_SPAN"
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Zero span: SPAN = 0, the trace becomes power versus time. Click again to restore the span")
        self._span: float | None = None
        self.last_span = float(settings.value(self._key, self.DEFAULT_SPAN, type=float)) or self.DEFAULT_SPAN
        self.clicked.connect(self.toggle)
        proxy.subscribe("freq.span", self._on_span)

    def indicated(self) -> bool:
        return self._span == 0

    def _on_span(self, value) -> None:
        self._span = value
        if isinstance(value, (int, float)) and value > 0:
            self.last_span = float(value)
            self.settings.setValue(self._key, self.last_span)
        self.update()

    def toggle(self) -> None:
        if not self.proxy.is_connected or self._span is None:
            return
        self.proxy.set("freq.span", self.last_span if self._span == 0 else 0.0)


def _preset(name) -> tuple[str]:
    return (str(name),)


def _describe(preset: tuple) -> tuple[str, str]:
    return preset[0], f"{preset[0]}\nstate in the analyzer memory"


class StatePresets(PresetGroup):
    """Пресеты - имена состояний в памяти прибора. Удаление идёт в прибор (``on_remove``),
    а из списка пропадает, когда прибор перечислит файлы заново."""

    def __init__(self, settings, key, on_remove, parent=None):
        super().__init__(settings, key, (), _preset, _describe, parent)
        self.on_remove = on_remove

    def names(self) -> list[str]:
        return [p[0] for p in self.data]

    def selected_name(self) -> str | None:
        i = self.checked_index()
        return self.data[i][0] if 0 <= i < len(self.data) else None

    def remove_selected(self) -> bool:
        name = self.selected_name()
        if name is None:
            return False
        self.on_remove(name)
        return True

    def set_names(self, names: list[str], select: str | None = None) -> None:
        """Список из прибора: порядок - как в ini (последние использованные первыми), новые - по алфавиту."""
        keep = select or self.selected_name()
        known = self.names()
        order = [n for n in known if n in names] + sorted(n for n in names if n not in known)
        if order != known:
            while self.data:
                self.remove_button(0)
                del self.data[0]
            for n in order:
                self._append((n,))
        self.set_checked(order.index(keep) if keep in order else -1)
        self.save()


class SpectrumWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "e4446a"
    PRESET_KEY = "SA_PRESETS"
    PRESET_DEFAULTS = ()
    PRESET_ADD_TIP = "Save the current state in the analyzer memory"

    def __init__(self, proxy: Instrument, settings: QSettings, title: str = "Spectrum analyzer", parent=None):
        self._select_after_list: str | None = None
        super().__init__(proxy, settings, title, parent)
        proxy.action_done.connect(self._on_action)
        proxy.sync_done.connect(self.refresh_states)
        if proxy.is_connected:
            self.refresh_states()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, sup = self.proxy, self.proxy.supports
        self.top_lines = [self.preset_line()]
        self.widgets: dict[str, Any] = {}

        def make(key):
            spec = proxy.params[key]
            from ..core.params import Kind
            if spec.kind is Kind.BOOL:
                return PSButton(proxy, key, ONOFF)
            if spec.kind is Kind.ENUM:
                return PSButton(proxy, key, [(c, c) for c in spec.choices])
            return PLineEdit(proxy, key)

        for title_, rows in PAGES:
            lines = []
            for name, key, extra in rows:
                if not sup(key):
                    continue
                w = make(key)
                ws = [w]
                if extra and sup(extra):
                    if proxy.params[extra].kind.name == "BOOL":
                        ws.append(PSButton(proxy, extra, AUTO if extra.endswith("_auto") else ONOFF))
                    else:
                        ws.append(make(extra))
                for x, k in zip(ws, (key, extra)):
                    self.widgets[k] = x
                    if k not in NOT_SAVED:
                        self.saved_widgets.append(x)
                lines.append(PanelLine(name, *ws))
            if title_ == "FREQ" and sup("freq.span"):
                lines.append(PanelLine("ZERO", ZeroSpan(proxy, self.settings, self._prefix)))
            if title_ == "SWP":
                if sup("meas.continuous"):
                    cont = PSButton(proxy, "meas.continuous", [("CONT", True), ("SINGLE", False)])
                    self.widgets["meas.continuous"] = cont
                    extra_ws = [cont]
                    if "single" in proxy.actions:
                        extra_ws.append(ActionButton(proxy, "single", "SWEEP", "Single sweep"))
                    lines.append(PanelLine("MODE", *extra_ws))
            if lines:
                self.page_specs.append((title_, (), PanelTable(*lines)))

    def restore_rank(self, key: str) -> int:
        return 1 if key.endswith(("_auto", "_state")) else 0

    # ------------------------------------------------------------ пресеты = состояния в приборе
    def create_presets(self, key: str) -> PresetGroup:
        return StatePresets(self.settings, key, self._delete_state)

    def refresh_states(self) -> None:
        if "state_list" in self.proxy.actions:
            self.proxy.action("state_list")

    def state_name(self) -> str | None:
        """Имя пресета из центра и полосы: ``2G4S100M`` = центр 2.4 GHz, полоса (S) 100 MHz; ``1GS0`` - нулевая
        полоса. Только буквы и цифры, до 8 знаков: такие имена файлов принимает и PSA (DOS 8.3). Пресет с тем же
        центром и полосой записывается поверх."""
        c, s = self.proxy.value("freq.center"), self.proxy.value("freq.span")
        if c is None or s is None:
            return None
        return f"{compact_si(c)}S{compact_si(s)}"

    def _add_preset(self) -> None:
        name = self.state_name()
        if name is not None:
            self._select_after_list = name
            self.proxy.action("state_save", name)
            self.refresh_states()

    def _delete_state(self, name: str) -> None:
        self.proxy.action("state_delete", name)
        self.refresh_states()

    def apply_preset_values(self, preset: tuple) -> None:
        self.proxy.action("state_recall", preset[0])

    def _on_action(self, name: str, result) -> None:
        if name == "state_save" and isinstance(result, str) and result:
            self._select_after_list = result            # имя, под которым прибор записал состояние
        if name == "state_list" and isinstance(result, (list, tuple)):
            self.presets.set_names([str(n) for n in result], self._select_after_list)
            self._select_after_list = None
            if self.presets.data:
                i = self.presets.checked_index()
                if i >= 0:
                    self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[i], 0, 0)
