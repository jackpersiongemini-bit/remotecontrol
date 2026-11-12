"""Правила окна источников: строка вида

    IF CH2.I < 5m OR CH2 OFF THEN CH1 OFF AND BLOCK ELSE UNBLOCK

Условие - термы через AND / OR (AND сильнее, скобок нет):
    CHn.V | CHn.I | CHn.P  <  <=  >  >=  число (с префиксом: 5m, 1.5k)
    CHn ON | CHn OFF | CHn IS ON | CHn IS OFF          состояние выхода
Действия (через AND), THEN выполняется при истинном условии, ELSE (необязательно) - при ложном:
    CHn ON | CHn OFF | CHn BLOCK | CHn UNBLOCK
    BLOCK / UNBLOCK без канала - канал последнего упомянутого действия (в ELSE - канал из THEN).
BLOCK запрещает включать выход; OFF выключает (повторяется, пока условие держится).
Значение, которого ещё нет (нет связи, не пришло измерение), делает терм истинным: блокировка
по умолчанию сработает, а не пропустит.

Модуль не знает о Qt и приборах: ``Rule.outcome(get)`` получает функцию ``get(канал, величина)``
(величины ``V I P OUT``) и возвращает действия выбранной ветки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..core.units import si_parse

UNITS = {"V": "V", "I": "A", "P": "W"}
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
VERBS = ("ON", "OFF", "BLOCK", "UNBLOCK")

_TOKEN = re.compile(r"\s*(<=|>=|<|>|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z]+)?|[-+]?\d[\w.,µ]*)")


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Term:
    ch: int
    quantity: str                     # V / I / P / OUT
    op: str = ""                      # для V I P
    value: float = 0.0                # для V I P
    want_on: bool = True              # для OUT

    def check(self, get: Callable[[int, str], object]) -> bool:
        v = get(self.ch, self.quantity)
        if v is None:
            return True               # неизвестно - считаем условие выполненным (см. модуль)
        if self.quantity == "OUT":
            return bool(v) == self.want_on
        return OPS[self.op](v, self.value)


@dataclass(frozen=True)
class Action:
    ch: int
    verb: str                         # ON / OFF / BLOCK / UNBLOCK


@dataclass
class Rule:
    text: str
    cond: list[list[Term]]            # OR из AND-групп
    then: list[Action]
    other: list[Action] = field(default_factory=list)

    def holds(self, get) -> bool:
        return any(all(t.check(get) for t in group) for group in self.cond)

    def outcome(self, get) -> list[Action]:
        return self.then if self.holds(get) else self.other

    def channels(self) -> set[int]:
        return {t.ch for g in self.cond for t in g} | {a.ch for a in self.then + self.other}


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise RuleError(f"unexpected {text[pos:pos + 8]!r}")
        out.append(m.group(1).strip())
        pos = m.end()
    return out


def _channel(word: str, channels) -> int:
    m = re.fullmatch(r"CH(\d+)", word.upper())
    if not m:
        raise RuleError(f"expected CHn, got {word!r}")
    n = int(m.group(1))
    if channels is not None and n not in channels:
        raise RuleError(f"no CH{n}")
    return n


def parse_rule(text: str, channels=None) -> Rule:
    """``channels`` - допустимые номера каналов (None - любые). RuleError с понятным текстом."""
    toks = _tokens(text)
    up = [t.upper() for t in toks]
    if not toks or up[0] != "IF":
        raise RuleError("rule starts with IF")
    if "THEN" not in up:
        raise RuleError("missing THEN")
    i_then = up.index("THEN")
    i_else = up.index("ELSE") if "ELSE" in up else len(toks)
    if i_else < i_then:
        raise RuleError("ELSE before THEN")
    cond = _parse_cond(toks[1:i_then], channels)
    last: list[int | None] = [None]
    then = _parse_actions(toks[i_then + 1:i_else], channels, last)
    if i_else < len(toks) and i_else + 1 == len(toks):
        raise RuleError("empty ELSE")
    other = _parse_actions(toks[i_else + 1:], channels, last) if i_else < len(toks) else []
    return Rule(" ".join(text.split()), cond, then, other)


def _split(tokens: list[str], word: str) -> list[list[str]]:
    parts, cur = [], []
    for t in tokens:
        if t.upper() == word:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    parts.append(cur)
    return parts


def _parse_cond(tokens: list[str], channels) -> list[list[Term]]:
    if not tokens:
        raise RuleError("empty condition")
    return [[_parse_term(t, channels) for t in _split(group, "AND")] for group in _split(tokens, "OR")]


def _parse_term(t: list[str], channels) -> Term:
    if not t:
        raise RuleError("empty term")
    head = t[0].upper()
    if "." in head:
        ch_word, q = head.split(".", 1)
        if q not in UNITS:
            raise RuleError(f"unknown quantity {q!r} (V, I, P)")
        if len(t) != 3 or t[1] not in OPS:
            raise RuleError(f"expected {head} <op> value")
        try:
            value = si_parse(t[2], UNITS[q])
        except ValueError as e:
            raise RuleError(str(e)) from None
        return Term(_channel(ch_word, channels), q, t[1], value)
    rest = [w.upper() for w in t[1:]]
    if rest and rest[0] == "IS":
        rest = rest[1:]
    if len(rest) != 1 or rest[0] not in ("ON", "OFF"):
        raise RuleError(f"expected {t[0]} ON/OFF or {t[0]}.V/I/P <op> value")
    return Term(_channel(head, channels), "OUT", want_on=rest[0] == "ON")


def _parse_actions(tokens: list[str], channels, last: list) -> list[Action]:
    out = []
    for a in _split(tokens, "AND"):
        words = [w.upper() for w in a]
        if len(words) == 1 and words[0] in ("BLOCK", "UNBLOCK"):
            if last[0] is None:
                raise RuleError(f"{words[0]} needs a channel (CHn {words[0]})")
            out.append(Action(last[0], words[0]))
        elif len(words) == 2 and words[1] in VERBS:
            last[0] = _channel(words[0], channels)
            out.append(Action(last[0], words[1]))
        else:
            raise RuleError("expected CHn ON/OFF/BLOCK/UNBLOCK")
    return out


def resolve(rules: list[Rule], get) -> tuple[set[int], set[int], set[int], dict[int, str]]:
    """(выключить, включить, заблокировать, причины блокировки) по всем правилам."""
    off: set[int] = set()
    on: set[int] = set()
    blocked: set[int] = set()
    why: dict[int, str] = {}
    for r in rules:
        for a in r.outcome(get):
            if a.verb == "OFF":
                off.add(a.ch)
            elif a.verb == "ON":
                on.add(a.ch)
            elif a.verb == "BLOCK":
                blocked.add(a.ch)
                why.setdefault(a.ch, r.text)
    return off, on - blocked - off, blocked, why
