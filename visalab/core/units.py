"""Общий словарь: числа и логические значения в тексте, SI-префиксы.

Не слой: модуль не знает ни о приборах, ни о Qt, ни о pyvisa, поэтому им пользуются все слои -
драйверы (ответы приборов «100HZ»), симуляторы, координатор и окна (``si_format`` / ``si_parse``).
Разбор SCPI-сообщений (заголовки, шаблоны команд) - в :mod:`visalab.transport.scpi`.
"""
from __future__ import annotations

import math
import re

__all__ = ["parse_number", "format_number", "parse_bool", "format_bool", "si_format", "si_parse", "SI_PREFIXES"]

# --------------------------------------------------------------------- числа

_NUM_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$")

#: Префиксы, учитывающие регистр (m - милли, M - мега).
SI_PREFIXES = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}

#: Единицы, которые приборы дописывают к числам в ответах (сравнение без учёта регистра).
_KNOWN_UNITS = ("VRMS", "VPP", "DBM", "OHM", "HZ", "V", "A", "W", "S", "%", "°", "PPM")


def parse_number(text: str) -> float:
    """Преобразует ответ прибора в float.

    Понимает экспоненциальную запись и единицы/префиксы: ``"100HZ"``, ``"2.4e-07S"``,
    ``"30mA"``, ``"1.5 kHz"``, ``" 4V "``. Бросает ``ValueError`` при мусоре.
    """
    if text is None:
        raise ValueError("пустое значение")
    m = _NUM_RE.match(str(text).strip().strip('"'))
    if not m:
        raise ValueError(f"не число: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2)
    if not suffix:
        return value
    up = suffix.upper()
    for unit in _KNOWN_UNITS:
        if up.endswith(unit):
            suffix = suffix[: len(suffix) - len(unit)]
            break
    if suffix in SI_PREFIXES:
        return value * SI_PREFIXES[suffix]
    # SCPI-форма "MA"/"MV" (верхний регистр) означает милли, "MHZ" - мега.
    if suffix == "M" and up.endswith("HZ"):
        return value * 1e6
    raise ValueError(f"неизвестный суффикс {m.group(2)!r} в {text!r}")


def format_number(value: float) -> str:
    """Число для отправки в прибор: без потери точности и без лишних нулей."""
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"недопустимое число {value}")
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.12g}"


def parse_bool(text: str) -> bool:
    s = str(text).strip().strip('"').upper()
    if s in ("1", "ON", "TRUE"):
        return True
    if s in ("0", "OFF", "FALSE"):
        return False
    raise ValueError(f"не логическое значение: {text!r}")


def format_bool(value: bool, on: str = "1", off: str = "0") -> str:
    return on if bool(value) else off


# ------------------------------------------------------------ SI для окон

_PREFIX_BY_POWER = {-4: "p", -3: "n", -2: "µ", -1: "m", 0: "", 1: "k", 2: "M", 3: "G"}


def si_format(value: float | None, unit: str = "", digits: int = 6) -> str:
    """``1500, "Hz"`` -> ``"1.5 kHz"``. Без единицы - без префикса."""
    if value is None:
        return ""
    value = float(value)
    if not unit or unit in ("%", "°") or value == 0:
        return f"{value:.{digits}g}{(' ' + unit) if unit else ''}"
    power = int(math.floor(math.log10(abs(value)) / 3))
    power = max(-4, min(3, power))
    scaled = value / 1000 ** power
    return f"{scaled:.{digits}g} {_PREFIX_BY_POWER[power]}{unit}"


def si_parse(text: str, unit: str = "") -> float:
    """Ввод пользователя: ``"1.5k"``, ``"1,5 kHz"``, ``"20 mV"``, ``"3"``."""
    s = text.strip().replace(",", ".")
    if unit and s.lower().endswith(unit.lower()):
        s = s[: -len(unit)].rstrip()
    m = _NUM_RE.match(s)
    if not m:
        raise ValueError(f"не число: {text!r}")
    value, suffix = float(m.group(1)), m.group(2)
    if suffix == "u":
        suffix = "µ"
    if suffix not in SI_PREFIXES:
        raise ValueError(f"неизвестный префикс {suffix!r}")
    return value * SI_PREFIXES[suffix]
