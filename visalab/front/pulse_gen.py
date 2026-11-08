"""Окно генератора импульсов (Siglent SDG) - по PulseGen.py Алексея.

Общее (заголовок, журнал ошибок, подключение, сворачивание) - в instrument_window.py.
Здесь: выходы, пресеты PERI/WID (общие для обоих каналов, «+» берёт текущие значения CH1, «−» удаляет
выбранный) и таблица параметров по каналам. Каждая запись проверяется драйвером чтением обратно,
EQPHASE после изменений, влияющих на фазу, драйвер отправляет сам.

Окно знает только InstrumentProxy и ключи профиля function_generator.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings

from ..core.params import Kind
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import PRESET_GAP, PRESET_HEIGHT, HScrollArea, InstrumentWindow  # noqa: F401
from .params import PLabel, PLineEdit, PNLabel, PSButton, PanelLine, PanelTable
from .theme import UI_COLUMN_WIDTH_PX

__all__ = ["PulseGenWindow", "PULSE_PRESETS", "ROWS", "saved_connection"]

PULSE_PRESETS = ((400e-3, 30e-6, "PULSE"),)       # (PERI, WID, форма)

def _auto(proxy: InstrumentProxy, key: str):
    """Виджет по типу параметра: BOOL - OFF/ON, ENUM - список, число/строка - поле."""
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, [("OFF", False), ("ON", True)])
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


#: строка: (подпись, ключ без "chN.", фабрика виджета, сохранять в ini)
OUTPUT_ROW = ("OUT", "output",
              lambda p, k: PSButton(p, k, {f"{k[:3].upper()} OFF": False, f"{k[:3].upper()} ON": True}), True)

#: вкладки развёрнутой таблицы: (название, ключ включения режима или None, строки)
PAGES = (
    ("MAIN", None, (
        ("TYPE", "wave", _auto, True),
        ("DUTY", "duty", lambda p, k: PNLabel(p, k, "%"), False),
        ("PERI", "period", _auto, True),
        ("WID", "width", _auto, True),
        ("HLVL", "high", _auto, True),
        ("LLVL", "low", _auto, True),
        ("DLY", "delay", _auto, True),
        ("LOAD", "load", lambda p, k: PSButton(p, k, {"50 Ω": "50", "HiZ": "HIZ"}), True),
    )),
    ("MOD", "mod_state", (
        ("STAT", "mod_state", _auto, True),
        ("TYPE", "mod_type", _auto, True),
        ("SRC", "mod_source", _auto, True),
        ("SHP", "mod_shape", _auto, True),
        ("FRQ", "mod_freq", _auto, True),
        ("DEPT", "mod_depth", _auto, True),
        ("DEVI", "mod_dev", _auto, True),
    )),
    ("SWP", "sweep_state", (
        ("STAT", "sweep_state", _auto, True),
        ("TIME", "sweep_time", _auto, True),
        ("STRT", "sweep_start", _auto, True),
        ("STOP", "sweep_stop", _auto, True),
        ("MODE", "sweep_mode", _auto, True),
        ("DIR", "sweep_dir", _auto, True),
        ("TRIG", "sweep_trig", _auto, True),
    )),
    ("BRST", "burst_state", (
        ("STAT", "burst_state", _auto, True),
        ("MODE", "burst_mode", _auto, True),
        ("PER", "burst_period", _auto, True),
        ("CYC", "burst_cycles", _auto, True),
        ("TRIG", "burst_trig", _auto, True),
        ("DLY", "burst_delay", _auto, True),
    )),
)
ROWS = (OUTPUT_ROW,) + tuple(row for _t, _s, rows in PAGES for row in rows)
#: порядок восстановления: форма -> нагрузка (до уровней) -> остальное -> включение режимов -> выходы
RESTORE_RANK = {"wave": 0, "load": 1, "mod_state": 3, "sweep_state": 3, "burst_state": 3, "output": 4}


def _fmt(value: float) -> str:
    return si_format(value, "s", 4)


def _preset(period, width, wave="PULSE") -> tuple[float, float, str]:
    return float(period), float(width), str(wave).upper()


def _describe(preset: tuple) -> tuple[str, str]:
    period, width, wave = preset
    return (f"{wave}\n{_fmt(period)}\n{_fmt(width)}", f"TYPE {wave}\nPERI {_fmt(period)}\nWID {_fmt(width)}")


class PresetGroup(_iw.PresetGroup):
    """Пресеты (PERI, WID, форма). Чип в три строки: форма / период / ширина."""

    def __init__(self, settings: QSettings, key: str, defaults=PULSE_PRESETS, parent=None):
        super().__init__(settings, key, defaults, _preset, _describe, parent)


#: группа настроек подключения окна в ini (драйвер, адрес, модель)
CONNECTION_GROUP = "CONNECTION/PULSE_GEN"


def saved_connection(settings: QSettings) -> dict[str, str]:
    """Сохранённые в окне драйвер / адрес / модель (пустые строки - не задано)."""
    return _iw.saved_connection(settings, CONNECTION_GROUP)


class PulseGenWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "sdg"
    PRESET_KEY = "PULSE_PRESETS"
    PRESET_DEFAULTS = PULSE_PRESETS
    PRESET_ADD_TIP = "Add preset from CH1 TYPE / PERI / WID"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Pulse generator", parent=None):
        super().__init__(proxy, settings, title, parent)

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy = self.proxy
        nch = min(2, proxy.channels)
        self.channels: list[dict[str, Any]] = []
        for n in range(1, nch + 1):
            self.channels.append({name: make(proxy, f"ch{n}.{name}") for _t, name, make, _s in ROWS
                                  if proxy.supports(f"ch{n}.{name}")})
        saved = {name for _t, name, _m, s in ROWS if s}
        self.saved_widgets = [w for ch in self.channels for name, w in ch.items() if name in saved]

        outputs = [ch["output"] for ch in self.channels if "output" in ch]
        for w in outputs:
            w.setMinimumWidth(UI_COLUMN_WIDTH_PX)
        self.top_lines = [PanelLine("OUT", *outputs), self.preset_line()]

        for title_, state, rows in PAGES:
            present = [r for r in rows if all(r[1] in ch for ch in self.channels)]
            if not present:
                continue
            heads = []
            for n in range(1, nch + 1):
                h = PLabel(text=f"CH{n}")
                h.setObjectName("heading")
                heads.append(h)
            lines = [PanelLine("", *heads)]
            lines += [PanelLine(t, *(ch[name] for ch in self.channels)) for t, name, _m, _s in present]
            states = tuple(f"ch{n}.{state}" for n in range(1, nch + 1)) if state else ()
            self.page_specs.append((title_, states, PanelTable(*lines)))

    def restore_rank(self, key: str) -> int:
        # форма (от неё зависят PERI/WID/DLY) -> нагрузка (пересчитывает уровни, поэтому до HLVL/LLVL)
        # -> остальное -> включение режимов -> выходы последними
        return RESTORE_RANK.get(key.split(".", 1)[-1], 2)

    # ------------------------------------------------------------ пресеты
    def create_presets(self, key: str) -> PresetGroup:
        return PresetGroup(self.settings, key)

    def preset_from_current(self):
        ch1 = self.channels[0]
        period, width = ch1["period"].value, ch1["width"].value
        wave = ch1["wave"].value if "wave" in ch1 else None
        if period is None or width is None:
            return None
        return period, width, wave or "PULSE"

    def apply_preset_values(self, preset: tuple) -> None:
        period, width, wave = preset
        for ch in self.channels:
            if "wave" in ch:
                ch["wave"].set_value(wave)    # форма - первой: от неё зависят PERI/WID
            cur = ch["period"].value
            pairs = [(ch["period"], period)]
            if wave == "PULSE":               # ширина есть только у импульса
                pairs.append((ch["width"], width))
            if cur is not None and period < cur:
                pairs.reverse()              # уменьшаем: сначала WID, чтобы WID < PERI
            for w, v in pairs:
                w.set_value(v)
