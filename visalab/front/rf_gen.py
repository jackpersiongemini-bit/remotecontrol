"""Окно СВЧ генератора (E8267D, SMF100A - профиль signal_generator), по образцу окна SDG.

Избранное, как в векторнике: выходы RF / MOD, мощность спинбоксом с PRST (режим пресетов: колесо /
стрелки ходят по списку уровней) и частота RF спинбоксом (шаг - разряд под курсором). Список уровней -
чипы PRST на вкладке RF (клик ставит мощность, «+» - текущая, «−» - выбранная). Модуляция - на вкладках.
Развёрнутый вид - вкладки RF / AM / FM / PM / PULS (индикатор - режим включён).

Окно знает только InstrumentProxy и ключи профиля signal_generator.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QPushButton

from ..core.params import Kind
from ..core.proxy import InstrumentProxy
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow
from .params import PLineEdit, PSButton, PSpinBox, PanelLine, PanelTable
from .theme import UI_COLUMN_WIDTH_PX

__all__ = ["RFGenWindow", "MOD_TYPES", "saved_connection"]

#: тип модуляции -> префикс ключей профиля (CW - без модуляции)
MOD_TYPES = {"CW": None, "PULSE": "pulse", "AM": "am", "FM": "fm", "PM": "pm"}

CONNECTION_GROUP = "CONNECTION/RF_GEN"


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


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
    PRESET_KEY = None                                 # вместо пресетов FREQ/POW/MOD - пресеты мощности

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "RF generator", parent=None):
        super().__init__(proxy, settings, title, parent)
        tools = getattr(self, "_pwr_tools", None)     # «+»/«−» - по ширине кнопок, у правого края
        if tools is not None:
            tools.setMinimumWidth(0)
            tools.setFixedWidth(max(b.sizeHint().width() for b in tools.findChildren(QPushButton)))

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy = self.proxy
        self.widgets: dict[str, Any] = {}
        sup = proxy.supports

        # избранное: выходы, мощность с режимом пресетов, частота RF
        outs = []
        for key, name in (("rf.output", "RF"), ("mod.output", "MOD")):
            if sup(key):
                w = PSButton(proxy, key, {f"{name} OFF": False, f"{name} ON": True})
                w.setMinimumWidth(UI_COLUMN_WIDTH_PX)
                self.widgets[key] = w
                outs.append(w)
        self.top_lines = [PanelLine("OUT", *outs)]
        if sup("rf.power"):
            spin, prst = self.power_widgets("rf.power", "Preset mode: the wheel and arrows step the power "
                                                        "through the PRST list (RF tab)")
            self.top_lines.append(PanelLine("POW", spin, prst))
        self.freq_spin = PSpinBox(proxy, "rf.frequency", ("k", "M", "G"))
        self.top_lines.append(PanelLine("FREQ", self.freq_spin))

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
                if k == "rf.power":                   # пресеты мощности - под POW
                    lines.append(self.power_levels_line("rf.power"))
            self.page_specs.append((title_, (state,) if sup(state) else (), PanelTable(*lines)))
        if "mod.output" in self.widgets:
            self.saved_widgets.append(self.widgets["mod.output"])

    def restore_rank(self, key: str) -> int:
        return RESTORE_RANK.get(key, 2)
