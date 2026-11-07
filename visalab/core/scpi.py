"""Утилиты SCPI: разбор чисел, булевых значений, шаблонов команд, SI-префиксы.

Модуль не зависит от Qt и pyvisa, поэтому используется и драйверами, и симуляторами,
и GUI.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

__all__ = [
    "parse_number", "format_number", "parse_bool", "format_bool",
    "split_units", "split_header", "compile_pattern", "match_header",
    "si_format", "si_parse", "SI_PREFIXES", "strip_response_header",
]

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


# ---------------------------------------------------------- разбор сообщений

# Заголовок, который часть приборов (Siglent) повторяет в ответе:
# "*ESR 1", "C1:SYNC OFF", "CMR 0", "C1:BSWV WVTP,SINE". Только верхний регистр,
# поэтому "Siglent Technologies,..." не задевается.
_ECHO_HEADER_RE = re.compile(r"^\s*(?:C\d+\s*:\s*)?\*?[A-Z][A-Z0-9_]*(?::[A-Z][A-Z0-9_]*)*\s+(?=\S)")


def strip_response_header(answer: str) -> str:
    """``"*ESR 1"`` -> ``"1"``, ``"C1:OUTP ON,LOAD,HZ"`` -> ``"ON,LOAD,HZ"``, ``"1"`` -> ``"1"``."""
    return _ECHO_HEADER_RE.sub("", answer, count=1).strip()


def split_units(message: str) -> list[str]:
    """Делит SCPI-сообщение на единицы по ``;`` с учётом кавычек."""
    units, buf, quote = [], [], None
    for ch in message:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ";":
            units.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    units.append("".join(buf))
    return [u.strip() for u in units if u.strip()]


_HEADER_RE = re.compile(r"^\s*(:?[*A-Za-z0-9_]+(?:\s*:\s*[A-Za-z0-9_]+)*\??)\s*(.*)$", re.S)


def split_header(unit: str) -> tuple[str, str, bool]:
    """``"C1: BSWV FRQ,10"`` -> ``("C1:BSWV", "FRQ,10", False)``."""
    m = _HEADER_RE.match(unit)
    if not m:
        raise ValueError(f"нет заголовка: {unit!r}")
    header = re.sub(r"\s*:\s*", ":", m.group(1))
    args = m.group(2).strip()
    # Запрос с аргументом после пробела: "CURR? MAX" или "STL? BUILDIN"
    is_query = header.endswith("?")
    if is_query:
        header = header[:-1]
    return header.lstrip(":"), args, is_query


# ----------------------------------------------------- шаблоны SCPI-команд

@lru_cache(maxsize=None)
def compile_pattern(pattern: str) -> re.Pattern:
    """Компилирует шаблон из мануала в регулярное выражение.

    ``[SOURce:]VOLTage[:LEVel]`` принимает VOLT, VOLTAGE, SOUR:VOLT:LEV и т.д.
    ``#`` в конце слова захватывает номер: ``C#:BSWV``, ``ISUMmary#``.
    """
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            out.append("(?:")
            i += 1
        elif ch == "]":
            out.append(")?")
            i += 1
        elif ch == ":":
            out.append(":")
            i += 1
        else:
            j = i
            while j < len(pattern) and pattern[j] not in "[]:":
                j += 1
            word = pattern[i:j]
            capture = word.endswith("#")
            word = word.rstrip("#")
            k = 0
            while k < len(word) and (word[k].isupper() or word[k] in "*_" or word[k].isdigit()):
                k += 1
            short, full = word[:k].upper(), word.upper()
            out.append(re.escape(full) if short == full else f"(?:{re.escape(full)}|{re.escape(short)})")
            if capture:
                out.append(r"(\d+)")
            i = j
    return re.compile("^" + "".join(out) + "$", re.I)


def match_header(pattern: str, header: str):
    """Возвращает ``re.Match`` или ``None``."""
    return compile_pattern(pattern).match(header.lstrip(":"))


# ------------------------------------------------------------ SI для GUI

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
