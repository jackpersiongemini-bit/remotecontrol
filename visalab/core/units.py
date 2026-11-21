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
_KNOWN_UNITS = ("VRMS", "VPP", "DBM", "OHM", "DEG", "RAD", "HZ", "DB", "V", "A", "W", "S", "%", "°", "PPM")

#: Единицы, к которым SI-префиксы (m, k, M...) не добавляются.
_NO_PREFIX_UNITS = frozenset(("%", "°", "deg", "dBm", "dB", "div"))

#: Верхнерегистровые SCPI-префиксы (когда прибор пишет всё КАПСОМ: 50MV, 100US, 10NS, 5MHZ).
_SCPI_UPPER_PREFIXES = {"P": 1e-12, "N": 1e-9, "U": 1e-6, "K": 1e3, "G": 1e9}
_SCPI_MEGA_UNITS = frozenset(("HZ", "OHM"))


def parse_number(text: str) -> float:
    """Преобразует ответ прибора в float.

    Понимает экспоненциальную запись и единицы/префиксы: ``"100HZ"``, ``"2.4e-07S"``,
    ``"30mA"``, ``"50MV"``, ``"100NS"``, ``"1.5 kHz"``, ``" 4V "``. Бросает ``ValueError`` при мусоре
    или переполнении (``NaN``/``Inf``).
    """
    if text is None:
        raise ValueError("пустое значение")
    m = _NUM_RE.match(str(text).strip().strip('"'))
    if not m:
        raise ValueError(f"не число: {text!r}")
    value = float(m.group(1))
    orig_suffix = m.group(2)
    if not orig_suffix:
        if not math.isfinite(value):
            raise ValueError(f"неконечное число: {text!r}")
        return value
    suffix = orig_suffix
    up = suffix.upper()
    matched_unit = ""
    for unit in _KNOWN_UNITS:
        if up.endswith(unit):
            matched_unit = unit
            suffix = suffix[: len(suffix) - len(unit)]
            break
    # SCPI-ответ в верхнем регистре: "50MV"/"30MA"/"10MS" - милли, "10MHZ"/"1MOHM" - мега, "100US"/"50NS" - микро/нано.
    if matched_unit and orig_suffix == up:
        if suffix == "":
            scale = 1.0
        elif suffix == "M":
            scale = 1e6 if matched_unit in _SCPI_MEGA_UNITS else 1e-3
        elif suffix in _SCPI_UPPER_PREFIXES:
            scale = _SCPI_UPPER_PREFIXES[suffix]
        else:
            raise ValueError(f"неизвестный суффикс {orig_suffix!r} в {text!r}")
    elif suffix in SI_PREFIXES:
        scale = SI_PREFIXES[suffix]
    else:
        raise ValueError(f"неизвестный суффикс {orig_suffix!r} в {text!r}")
    res = value * scale
    if not math.isfinite(res):
        raise ValueError(f"переполнение числа: {text!r}")
    return res


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
    if not math.isfinite(value):
        return f"{value}{(' ' + unit) if unit else ''}"
    if not unit or unit in _NO_PREFIX_UNITS or value == 0:
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
    res = value * SI_PREFIXES[suffix]
    if not math.isfinite(res):
        raise ValueError(f"неконечное число: {text!r}")
    return res
