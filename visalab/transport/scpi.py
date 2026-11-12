"""Формат сообщений SCPI: то, что драйвер пишет в прибор и читает из него через транспорт.

Часть слоя 1 (транспорт): им пользуются драйверы (слой 2, сосед сверху) и симуляторы приборов
(слой 1). Окнам и координатору разбор SCPI не нужен - они работают ключами профилей.
"""
from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["strip_response_header", "split_units", "split_header", "compile_pattern", "match_header"]

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
