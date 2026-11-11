"""Проверка стенда (run_check.py): приборы подключаются как в окнах, всё читается, итог - отчёт.

На симуляторах проверяется сама проверка: что попадает в отчёт, когда прибор не отвечает на запрос
(нет у модели), отвечает непонятно, держит ошибку в очереди или не подключается вовсе.
"""
import logging

from visalab.applog import Throttle, close_logging, setup_logging
from visalab.check import format_report, plural, run_check
from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import KeysightE8267D, KeysightU2021XA
from visalab.sim import KeysightE8267DSim, KeysightU2021XASim

import run_check as launcher


def test_check_all_on_simulators(qtbot, tmp_path):
    out = tmp_path / "report.txt"
    rc = launcher.main(["--sim", "--poll", "0.5", "--ini", str(tmp_path / "lab.ini"), "--out", str(out),
                        "e8267d", "u2021xa"])
    text = out.read_text(encoding="utf-8")
    assert rc == 0, text
    assert "Итог: 2 прибора - без замечаний 2" in text
    assert "[OK] e8267d  Keysight E8267D" in text and "[OK] u2021xa  Keysight U2021XA" in text
    assert text.count("*IDN?: ") == 2 and "meas.power ×" in text          # ваттметр мерил при опросе
    assert (tmp_path / "logs" / "visalab.log").is_file()                   # журнал программы рядом с ini


def _handler(sim, prefix):
    return next(i for i, (p, _s, _g) in enumerate(sim._handlers) if p.startswith(prefix))


def test_check_reports_what_the_instrument_does_not_answer(qtbot):
    gen_sim = KeysightE8267DSim()
    i = _handler(gen_sim, "AM[1]:DEPTh")
    pattern, setter, _getter = gen_sim._handlers[i]
    gen_sim._handlers[i] = (pattern, setter, None)                         # запроса нет у модели: -113, молчит
    j = _handler(gen_sim, "FM[1]:DEViation")
    gen_sim._handlers[j] = (gen_sim._handlers[j][0], gen_sim._handlers[j][1], lambda a, g: "WHAT")  # не число
    off_sim = KeysightU2021XASim()
    off_sim.powered = False                                                # прибор выключен
    targets = [("e8267d", InstrumentProxy(KeysightE8267D, SimTransport(gen_sim, address="SIM::GEN::INSTR"))),
               ("u2021xa", InstrumentProxy(KeysightU2021XA, SimTransport(off_sim, address="SIM::PM::INSTR")))]
    try:
        results = run_check(targets, poll_s=0.3, timeout_s=20)
    finally:
        InstrumentProxy.shutdown_all(p for _k, p in targets)
    gen, pm = results
    assert gen.status == "WARN" and pm.status == "FAIL"
    assert gen.unsupported == ["am.depth"]
    assert "fm.deviation" in gen.key_errors and "не распознан" in gen.key_errors["fm.deviation"]
    assert any(m.startswith("-113") for m in gen.instrument_errors)
    assert pm.connected_s is None and pm.comm
    text = format_report(results, ["Проверка"])
    assert "[WARN] e8267d" in text and "[FAIL] u2021xa" in text
    assert "не поддерживает (нет у модели или опции - драйвер спрашивает зря): am.depth" in text
    assert "fm.deviation: ответ не распознан" in text and "не подключился: SIM::PM::INSTR" in text


def test_plural():
    assert [plural(n, ("прибор", "прибора", "приборов")) for n in (1, 2, 5, 11, 21, 22, 112)] == [
        "1 прибор", "2 прибора", "5 приборов", "11 приборов", "21 прибор", "22 прибора", "112 приборов"]


# ------------------------------------------------------------------ журнал программы
def test_log_file_gets_instrument_events(qtbot, tmp_path):
    path = tmp_path / "logs" / "visalab.log"
    handler = setup_logging(path)
    try:
        assert setup_logging(path) is handler                               # второй вызов не дублирует
        sim = KeysightE8267DSim()
        proxy = InstrumentProxy(KeysightE8267D, SimTransport(sim, address="SIM::GEN::INSTR"))
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        proxy.set("rf.power", 1e6)                                          # вне пределов модели
        qtbot.waitUntil(lambda: "rf.power" in path.read_text(encoding="utf-8"), timeout=5000)
        proxy.shutdown()
    finally:
        close_logging(handler)
    text = path.read_text(encoding="utf-8")
    assert "старт:" in text and "*IDN? Agilent Technologies, E8267D" in text
    assert "SIM::GEN::INSTR: connected" in text and "rf.power invalid" in text
    assert handler not in logging.getLogger().handlers


def test_same_message_is_written_once_a_minute():
    now = [0.0]
    throttle = Throttle(60.0, clock=lambda: now[0])
    assert throttle("key a", "нет ответа") and not throttle("key a", "нет ответа")
    assert throttle("key b", "нет ответа") and throttle("key a", "другое")
    now[0] = 61.0
    assert throttle("key a", "другое")
