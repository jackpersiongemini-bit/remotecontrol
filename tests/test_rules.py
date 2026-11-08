"""Разбор и вычисление правил окна источников (без Qt)."""
import pytest

from visalab.front.rules import RuleError, parse_rule, resolve

TEXT = "IF CH2.I < 5m OR CH2 is OFF THEN CH1 OFF AND BLOCK ELSE UNBLOCK"


def _get(i=0.1, out=True):
    return lambda n, q: {(2, "I"): i, (2, "OUT"): out}.get((n, q))


def test_user_example():
    r = parse_rule(TEXT, {1, 2, 3})
    assert [(a.ch, a.verb) for a in r.then] == [(1, "OFF"), (1, "BLOCK")]
    assert [(a.ch, a.verb) for a in r.other] == [(1, "UNBLOCK")]
    assert r.holds(_get(0.001)) and r.holds(_get(0.1, out=False)) and not r.holds(_get(0.1))
    off, on, blocked, why = resolve([r], _get(0.001))
    assert off == {1} and blocked == {1} and why[1] == TEXT
    off, on, blocked, _ = resolve([r], _get(0.1))
    assert not off and not blocked


def test_precedence_and_unknown_is_true():
    r = parse_rule("IF CH1.V > 10 OR CH2 ON AND CH3.P >= 1.5 THEN CH1 OFF", {1, 2, 3})
    get = lambda n, q: {(1, "V"): 5.0, (2, "OUT"): True, (3, "P"): 2.0}.get((n, q))
    assert r.holds(get)                                           # AND сильнее OR
    assert not r.holds(lambda n, q: {(1, "V"): 5.0, (2, "OUT"): True, (3, "P"): 1.0}.get((n, q)))
    assert r.holds(lambda n, q: None)                             # неизвестно - блокируем


def test_errors():
    for bad, part in (("", "IF"), ("IF CH2 ON", "THEN"), ("IF CH9 ON THEN CH1 OFF", "CH9"),
                      ("IF CH2.X < 1 THEN CH1 OFF", "quantity"), ("IF CH2.I < x THEN CH1 OFF", "число"),
                      ("IF CH2 ON THEN BLOCK", "channel"), ("IF CH2 ON THEN CH1 OFF ELSE", "ELSE"),
                      ("IF CH2.I 5 THEN CH1 OFF", "<op>")):
        with pytest.raises(RuleError, match=part):
            parse_rule(bad, {1, 2, 3})


def test_on_never_overrides_block():
    a = parse_rule("IF CH2 ON THEN CH1 ON", {1, 2})
    b = parse_rule("IF CH2 ON THEN CH1 BLOCK", {1, 2})
    off, on, blocked, _ = resolve([a, b], lambda n, q: True)
    assert on == set() and blocked == {1}
