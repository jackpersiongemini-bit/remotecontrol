"""Окно генератора импульсов (Siglent SDG) - по PulseGen.py Алексея.

Общее (заголовок, журнал ошибок, подключение, сворачивание) - в instrument_window.py.
Здесь: выходы, пресеты (сохраняют все настройки каналов со всех вкладок, кроме выходов; при применении
окно передаёт все параметры пресета, а драйвер отправляет прибору только отличающиеся) и таблица
параметров по каналам. Каждая запись проверяется драйвером чтением обратно, EQPHASE после изменений,
влияющих на фазу, драйвер отправляет сам.

Окно знает только прибор координатора (Instrument) и ключи профиля function_generator.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings

from ..core.params import Kind
from ..coord import Instrument
from ..core.units import si_format
from . import instrument_window as _iw
from .instrument_window import PRESET_GAP, PRESET_HEIGHT, HScrollArea, InstrumentWindow, _same  # noqa: F401
from .params import PLabel, PLineEdit, PNLabel, PSButton, PanelLine, PanelTable
from .theme import UI_COLUMN_WIDTH_PX

__all__ = ["PulseGenWindow", "PULSE_PRESETS", "ROWS", "saved_connection"]

PULSE_PRESETS = ((400e-3, 30e-6, "PULSE"),)       # (PERI, WID, форма[, {ключ: значение}])

def _auto(proxy: Instrument, key: str):
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
_PAGE_KEYS = {title: tuple(name for _t, name, _m, save in rows if save) for title, _s, rows in PAGES}
_UNIT_OF = {"period": "s", "width": "s", "delay": "s", "high": "V", "low": "V",
            "mod_freq": "Hz", "mod_depth": "%", "sweep_time": "s", "sweep_start": "Hz", "sweep_stop": "Hz",
            "burst_period": "s", "burst_delay": "s"}
#: порядок восстановления: форма -> нагрузка (до уровней) -> остальное -> включение режимов -> выходы
RESTORE_RANK = {"wave": 0, "load": 1, "mod_state": 3, "sweep_state": 3, "burst_state": 3, "output": 4}


def _fmt(value: float) -> str:
    return si_format(value, "s", 4)


def _norm_param_val(val: Any) -> Any:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        return val.strip().upper()
    return val


def _norm_params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    return {str(k): _norm_param_val(v) for k, v in sorted(params.items()) if v is not None}


def _preset(period, width=None, wave="PULSE", params=None) -> tuple[float | None, float | None, str, dict[str, Any]]:
    if isinstance(period, dict):
        p = _norm_params(period)
        return (p.get("ch1.period"), p.get("ch1.width"), str(p.get("ch1.wave") or "PULSE").upper(), p)
    per = None if period is None else float(period)
    wid = None if width is None else float(width)
    wv = str(wave or "PULSE").upper()
    return per, wid, wv, _norm_params(params)


def _fmt_param(name: str, val: Any) -> str:
    if isinstance(val, bool):
        return "ON" if val else "OFF"
    if isinstance(val, (int, float)):
        unit = _UNIT_OF.get(name, "")
        return si_format(val, unit, 4) if unit in ("s", "Hz", "V") else f"{val:g}{(' ' + unit) if unit else ''}"
    return str(val)


def _describe(preset: tuple) -> tuple[str, str]:
    period, width, wave, params = preset
    lines = [wave]
    if period is not None:
        lines.append(_fmt(period))
    if wave == "PULSE" and width is not None:
        lines.append(_fmt(width))
    elif "ch1.high" in params and "ch1.low" in params:
        lines.append(f"{params['ch1.high']:g}/{params['ch1.low']:g} V")
    text = "\n".join(lines)
    if not params:
        tip_lines = [f"TYPE {wave}"]
        if period is not None:
            tip_lines.append(f"PERI {_fmt(period)}")
        if width is not None:
            tip_lines.append(f"WID {_fmt(width)}")
        return text, "\n".join(tip_lines)
    tip_lines = []
    channels = sorted({k.split(".", 1)[0] for k in params if "." in k})
    for ch in channels:
        parts = []
        prefix = f"{ch}."
        for _title, _state, rows in PAGES:
            for label, name, _m, save in rows:
                if not save:
                    continue
                k = prefix + name
                if k not in params:
                    continue
                v = params[k]
                if name.endswith("_state") and v is False:
                    continue
                parts.append(f"{label} {_fmt_param(name, v)}")
        if parts:
            tip_lines.append(f"{ch.upper()}: " + ", ".join(parts))
    return text, "\n".join(tip_lines) or text


class PresetGroup(_iw.PresetGroup):
    """Пресеты генератора (форма, период, ширина и полные настройки каналов). Чип в три строки."""

    def __init__(self, settings: QSettings, key: str, defaults=PULSE_PRESETS, parent=None):
        super().__init__(settings, key, defaults, _preset, _describe, parent)

    def find(self, *values) -> int:
        target = self.normalize(*values)
        idx = self._find_exact(target)
        if idx >= 0 or len(values) >= 4 or (len(values) == 1 and isinstance(values[0], dict)):
            return idx
        for i, preset in enumerate(self.data):
            if _same(preset[:3], target[:3]):
                return i
        return -1

    def match(self, *values) -> int:
        """Сначала ищем точное совпадение полного пресета; если нет - старый пресет (PERI/WID/TYPE)."""
        target = self.normalize(*values)
        idx = self._find_exact(target)
        if idx >= 0:
            return idx
        for i, preset in enumerate(self.data):
            if not preset[3] and _same(preset[:3], target[:3]):
                return i
        return -1


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
    PRESET_ADD_TIP = "Add preset from current CH1 / CH2 settings"
    PRESET_TRACKS_VALUES = True             # горит пресет, совпадающий с текущими настройками прибора

    def __init__(self, proxy: Instrument, settings: QSettings, title: str = "Pulse generator", parent=None):
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
        if not self.channels:
            return None
        ch1 = self.channels[0]
        wave = ch1["wave"].value if "wave" in ch1 else None
        if wave is None:
            return None
        params: dict[str, Any] = {}
        for n, ch in enumerate(self.channels, start=1):
            for _title, state_key, rows in PAGES:
                if state_key is not None:
                    if state_key not in ch or ch[state_key].value is None:
                        continue
                    enabled = bool(ch[state_key].value)
                    params[f"ch{n}.{state_key}"] = enabled
                    if not enabled:
                        continue
                for _lbl, name, _make, save in rows:
                    if not save or name == state_key or name not in ch:
                        continue
                    val = ch[name].value
                    if val is not None:
                        params[f"ch{n}.{name}"] = val
        if not params:
            return None
        period = ch1["period"].value if "period" in ch1 else None
        width = ch1["width"].value if "width" in ch1 else None
        return period, width, wave, params

    def apply_preset_values(self, preset: tuple) -> None:
        period, width, wave, params = preset
        if not params:
            # старый формат пресета (PERI, WID, TYPE): применяем на оба канала; отличия проверит драйвер
            for ch in self.channels:
                if "wave" in ch:
                    ch["wave"].set_value(wave)    # форма - первой: от неё зависят PERI/WID
                cur = ch["period"].value if "period" in ch else None
                pairs = []
                if "period" in ch and period is not None:
                    pairs.append((ch["period"], period))
                if wave == "PULSE" and "width" in ch and width is not None:
                    pairs.append((ch["width"], width))
                if cur is not None and period is not None and period < cur:
                    pairs.reverse()              # уменьшаем: сначала WID, чтобы WID < PERI
                for w, v in pairs:
                    w.set_value(v)
            return
        # полный пресет: интерфейс передаёт все параметры в безопасном порядке, драйвер меняет только отличающиеся
        for n, ch in enumerate(self.channels, start=1):
            prefix = f"ch{n}."
            ch_vals = {k[len(prefix):]: v for k, v in params.items() if k.startswith(prefix) and v is not None}
            if not ch_vals:
                continue

            def send(name: str, _ch=ch, _vals=ch_vals) -> None:
                if name in _vals and name in _ch:
                    _ch[name].set_value(_vals[name])

            def send_pair(a: str, b: str, reverse: bool) -> None:
                for k in ((b, a) if reverse else (a, b)):
                    send(k)

            # 1) сначала выключаем неактивные режимы (иначе смена формы может дать конфликт)
            for state_key in ("mod_state", "sweep_state", "burst_state"):
                if ch_vals.get(state_key) is False:
                    send(state_key)
            # 2) форма и нагрузка (нагрузка пересчитывает уровни)
            send("wave")
            send("load")
            # 3) период / ширина (уменьшаем период - сначала ширина, чтобы WID < PERI)
            cur_per = ch["period"].value if "period" in ch else None
            shrink_per = ("period" in ch_vals and "width" in ch_vals
                          and cur_per is not None and ch_vals["period"] < cur_per)
            send_pair("period", "width", shrink_per)
            # 4) верхний / нижний уровни (чтобы в промежутке всегда было HLEV > LLEV)
            cur_lo = ch["low"].value if "low" in ch else None
            low_first = ("high" in ch_vals and "low" in ch_vals
                         and cur_lo is not None and ch_vals["high"] <= cur_lo)
            send_pair("high", "low", low_first)
            # 5) остальные параметры MAIN (DLY и т.п.)
            for name in _PAGE_KEYS["MAIN"]:
                if name not in ("wave", "load", "period", "width", "high", "low"):
                    send(name)
            # 6) включаем активный режим и задаём его параметры (STATE ON - первым)
            if ch_vals.get("mod_state") is True:
                send("mod_state")
                send("mod_type")
                for name in _PAGE_KEYS["MOD"]:
                    if name not in ("mod_state", "mod_type"):
                        send(name)
            if ch_vals.get("sweep_state") is True:
                send("sweep_state")
                cur_stop = ch["sweep_stop"].value if "sweep_stop" in ch else None
                stop_first = ("sweep_start" in ch_vals and "sweep_stop" in ch_vals
                              and cur_stop is not None and ch_vals["sweep_start"] > cur_stop)
                send_pair("sweep_start", "sweep_stop", stop_first)
                for name in _PAGE_KEYS["SWP"]:
                    if name not in ("sweep_state", "sweep_start", "sweep_stop"):
                        send(name)
            if ch_vals.get("burst_state") is True:
                send("burst_state")
                for name in _PAGE_KEYS["BRST"]:
                    if name != "burst_state":
                        send(name)
