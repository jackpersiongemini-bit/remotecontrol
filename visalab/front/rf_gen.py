"""Окно СВЧ генератора (E8267D, SMF100A - профиль signal_generator), по образцу окна SDG.

Избранное: выходы RF / MOD, пресеты (частота, мощность, тип модуляции), частота RF и частота
модулирующего сигнала (у импульсной - период) спинбоксами: шаг - разряд под курсором.
Развёрнутый вид - вкладки RF / AM / FM / PM / PULS (индикатор - режим включён).

Окно знает только InstrumentProxy и ключи профиля signal_generator.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings

from ..core.params import Kind
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow, PresetGroup
from .params import PLineEdit, PSButton, PSpinBox, PanelLine, PanelTable
from .theme import UI_COLUMN_WIDTH_PX

__all__ = ["RFGenWindow", "RF_PRESETS", "MOD_TYPES", "saved_connection"]

#: тип модуляции пресета -> префикс ключей профиля (CW - без модуляции)
MOD_TYPES = {"CW": None, "PULSE": "pulse", "AM": "am", "FM": "fm", "PM": "pm"}
#: подпись строки «частота модуляции» в избранном и её ключ
#: (AM в избранное не выводится - по просьбе Алексея; настраивается на вкладке AM)
MOD_RATE = {"PULSE": ("PULS", "pulse.period"), "FM": ("FM", "fm.freq"), "PM": ("PM", "pm.freq")}
RF_PRESETS = ((1e9, -10.0, "CW"),)                 # (FREQ, POW, MOD)

CONNECTION_GROUP = "CONNECTION/RF_GEN"


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _preset(freq, power, mod="CW") -> tuple[float, float, str]:
    mod = str(mod).upper()
    return float(freq), float(power), mod if mod in MOD_TYPES else "CW"


def _describe(preset: tuple) -> tuple[str, str]:
    freq, power, mod = preset
    f, p = si_format(freq, "Hz", 6), f"{power:g} dBm"
    return f"{mod}\n{f}\n{p}", f"MOD {mod}\nFREQ {f}\nPOW {p}"


def _auto(proxy: InstrumentProxy, key: str):
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, [("OFF", False), ("ON", True)])
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


#: вкладки: (название, ключ «режим включён», строки (подпись, ключ))
PAGES = (
    ("RF", "rf.output", (("OUT", "rf.output"), ("FREQ", "rf.frequency"), ("POW", "rf.power"),
                         ("ALC", "rf.alc"), ("REF", "ref.source"))),
    ("AM", "am.state", (("STAT", "am.state"), ("DEPT", "am.depth"), ("SRC", "am.source"), ("FRQ", "am.freq"))),
    ("FM", "fm.state", (("STAT", "fm.state"), ("DEVI", "fm.deviation"), ("SRC", "fm.source"), ("FRQ", "fm.freq"))),
    ("PM", "pm.state", (("STAT", "pm.state"), ("DEVI", "pm.deviation"), ("SRC", "pm.source"), ("FRQ", "pm.freq"))),
    ("PULS", "pulse.state", (("STAT", "pulse.state"), ("SRC", "pulse.source"), ("PERI", "pulse.period"),
                             ("WID", "pulse.width"))),
)
#: порядок восстановления: частота -> мощность -> настройки -> включение модуляций -> общий MOD -> RF
RESTORE_RANK = {"rf.frequency": 0, "rf.power": 1, "am.state": 3, "fm.state": 3, "pm.state": 3,
                "pulse.state": 3, "mod.output": 4, "rf.output": 5}


class RFGenWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "e8267d"
    PRESET_KEY = "RF_PRESETS"
    PRESET_DEFAULTS = RF_PRESETS
    PRESET_ADD_TIP = "Add preset from current FREQ / POW / MOD"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "RF generator", parent=None):
        super().__init__(proxy, settings, title, parent)

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy = self.proxy
        self.widgets: dict[str, Any] = {}
        sup = proxy.supports

        # избранное: выходы, пресеты, частота RF и частота модуляции
        outs = []
        for key, name in (("rf.output", "RF"), ("mod.output", "MOD")):
            if sup(key):
                w = PSButton(proxy, key, {f"{name} OFF": False, f"{name} ON": True})
                w.setMinimumWidth(UI_COLUMN_WIDTH_PX)
                self.widgets[key] = w
                outs.append(w)
        self.top_lines = [PanelLine("OUT", *outs), self.preset_line()]
        self.freq_spin = PSpinBox(proxy, "rf.frequency", ("k", "M", "G"))
        self.top_lines.append(PanelLine("FREQ", self.freq_spin))
        self.mod_spins: dict[str, PSpinBox] = {}
        for mod, (_label, key) in MOD_RATE.items():
            if sup(key):
                prefixes = ("n", "µ", "m", "") if key.endswith("period") else ("", "k", "M")
                self.mod_spins[mod] = PSpinBox(proxy, key, prefixes)
        self.mod_line = PanelLine("PULS", *self.mod_spins.values())
        self.top_lines.append(self.mod_line)
        saved_mod = str(self.settings.value(f"{self._prefix}/FAV_MOD", "PULSE"))
        self._fav_mod = saved_mod if saved_mod in self.mod_spins else next(iter(self.mod_spins), "")
        self._show_mod_rate(self._fav_mod)

        # вкладки: один столбец
        for title_, state, rows in PAGES:
            present = [(t, k) for t, k in rows if sup(k)]
            if not present:
                continue
            lines = []
            for t, k in present:
                w = _auto(proxy, k) if k != "rf.output" else PSButton(proxy, k, [("OFF", False), ("ON", True)])
                self.widgets.setdefault(k, w)
                self.saved_widgets.append(w)
                lines.append(PanelLine(t, w))
            self.page_specs.append((title_, (state,) if sup(state) else (), PanelTable(*lines)))
        if "mod.output" in self.widgets:
            self.saved_widgets.append(self.widgets["mod.output"])

    def restore_rank(self, key: str) -> int:
        return RESTORE_RANK.get(key, 2)

    # ------------------------------------------------------------ частота модуляции в избранном
    def _show_mod_rate(self, mod: str) -> None:
        """Показать в избранном частоту (у импульса - период) модуляции ``mod``."""
        if mod not in self.mod_spins:
            return
        self._fav_mod = mod
        for m, w in self.mod_spins.items():
            w.setVisible(m == mod)
        label, key = MOD_RATE[mod]
        self.mod_line._name.setText(label)
        self.mod_line._name.setToolTip(key)
        self.settings.setValue(f"{self._prefix}/FAV_MOD", mod)

    def on_param(self, key: str, value: Any) -> None:
        if value is True and key.endswith(".state"):
            mod = next((m for m, p in MOD_TYPES.items() if p and key == f"{p}.state"), None)
            if mod:
                self._show_mod_rate(mod)

    def current_mod(self) -> str:
        if self.proxy.supports("mod.output") and self.proxy.value("mod.output") is False:
            return "CW"
        for mod in ("PULSE", "AM", "FM", "PM"):
            if self.proxy.value(f"{MOD_TYPES[mod]}.state") is True:
                return mod
        return "CW"

    # ------------------------------------------------------------ пресеты
    def create_presets(self, key: str) -> PresetGroup:
        return PresetGroup(self.settings, key, RF_PRESETS, _preset, _describe)

    def preset_from_current(self):
        freq, power = self.proxy.value("rf.frequency"), self.proxy.value("rf.power")
        if freq is None or power is None:
            return None
        return freq, power, self.current_mod()

    def apply_preset_values(self, preset: tuple) -> None:
        freq, power, mod = preset
        w = self.widgets
        w["rf.frequency"].set_value(freq)
        w["rf.power"].set_value(power)
        target = MOD_TYPES.get(mod)
        # сначала выключить лишние модуляции (FM и PM на многих приборах несовместимы), потом нужную
        for m, prefix in MOD_TYPES.items():
            key = f"{prefix}.state" if prefix else None
            if key and key in w and prefix != target and self.proxy.value(key) is not False:
                w[key].set_value(False, force=True)
        if target and f"{target}.state" in w:
            w[f"{target}.state"].set_value(True)
            self._show_mod_rate(mod)
        if "mod.output" in w:
            w["mod.output"].set_value(target is not None)
