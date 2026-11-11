"""Координатор: сценарии над несколькими приборами (симуляторы в своих потоках, как в программе).

Стенд SimBench: выход генератора E8267D попадает на ваттметр U2021XA с потерями 10 dB + 0.5 dB/ГГц.
"""
import time

import pytest
from PyQt6.QtCore import QSettings, QTimer

from visalab.coord import (COORD, LINKS, AutoCalibration, Coordinator, FrequencySweep, ProxyLinks, Scenario,
                           ScenarioError, acal_file_name, frequency_points, load_csv)
from visalab.core import ConnectionState, InstrumentProxy, SimTransport
from visalab.drivers import DRIVERS
from visalab.sim.base import SimError
from visalab.sim.bench import SimBench


LOSS, SLOPE = 10.0, 0.5            # dB, dB/ГГц


def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return InstrumentProxy(drv, SimTransport(sim_cls()))


def _sim(proxy):
    return proxy._worker.transport.sim


def _open(qtbot, proxy):
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()


def _wait_value(qtbot, proxy, key, pred, timeout=5000):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=timeout)


@pytest.fixture
def lab(qtbot):
    """Генератор + ваттметр на стенде; свой координатор с отдельным реестром."""
    gen, pm = _proxy("e8267d"), _proxy("u2021xa")
    for p in (gen, pm):
        _open(qtbot, p)
    pm.set_poll_interval(50)
    bench = SimBench([_sim(gen)], [], [_sim(pm)], loss_db=LOSS, slope_db_per_ghz=SLOPE)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(bench.step)
    timer.start()
    gen.set("rf.power", -5.0)
    gen.set("rf.frequency", 5e9)
    _wait_value(qtbot, gen, "rf.power", lambda v: v == pytest.approx(-5.0))
    _wait_value(qtbot, gen, "rf.frequency", lambda v: v == pytest.approx(5e9))
    links = ProxyLinks()
    links.add(gen)
    links.add(pm)
    coord = Coordinator(links)
    coord.bench = bench                     # тестам: менять тракт
    yield gen, pm, coord
    coord.stop_all(wait_ms=3000)
    timer.stop()
    InstrumentProxy.shutdown_all([gen, pm])


def _finish(qtbot, run, timeout=30000):
    if run.running:
        with qtbot.waitSignal(run.ended, timeout=timeout):
            pass
    return run


def test_frequency_points_and_file_name():
    assert frequency_points(1e9, 2e9, 3) == [1e9, 1.5e9, 2e9]
    assert acal_file_name(1e9, 18e9, 101) == "ACAL_1G-18G_101.csv"
    assert acal_file_name(1.5e9, 2.45e9, 11) == "ACAL_1.5G-2.45G_11.csv"
    with pytest.raises(ValueError):
        frequency_points(2e9, 1e9, 3)


class Probe(Scenario):
    """Шаги движка: запись, чтение, пауза, параллельно; возврат в finally."""
    NAME = "PROBE"
    needs = ("signal_generator",)

    def steps(self, run):
        gen = run.device("signal_generator")
        old = gen.value("rf.frequency")
        try:
            yield gen.set("rf.frequency", 2e9)
            freq = yield gen.read("rf.frequency")
            yield run.sleep(0.01)
            both = yield (gen.set("rf.power", -7.0), gen.read("rf.output"))
            run.report(1, 1, "ok")
            return freq, both
        finally:
            yield gen.set("rf.frequency", old)


def test_engine_steps_and_restore(qtbot, lab):
    gen, pm, coord = lab
    run = _finish(qtbot, coord.start(Probe()))
    assert run.state == "done", run.error
    assert run.result == (pytest.approx(2e9), [pytest.approx(-7.0), False])
    assert gen.value("rf.frequency") == pytest.approx(5e9)          # вернул в finally
    assert coord.runs() == [] and coord.busy(gen) is None


def test_engine_instrument_error_fails_run(qtbot, lab):
    """Прибор отверг запись (-222): сценарий получает ошибку, возврат всё равно делается."""
    gen, pm, coord = lab
    sim = _sim(gen)
    pattern = next(p for p, _s, _g in sim._handlers if p.startswith("POWer"))
    getter = next(g for p, _s, g in sim._handlers if p == pattern)

    def reject(_a, _g):
        raise SimError(-222, "Data out of range")
    sim._handlers.insert(0, (pattern, reject, getter))
    run = _finish(qtbot, coord.start(Probe()))
    assert run.state == "failed" and "rf.power" in run.error and "out of range" in run.error
    assert gen.value("rf.frequency") == pytest.approx(5e9)


def test_start_checks_instruments(qtbot, lab):
    gen, pm, coord = lab
    with pytest.raises(ScenarioError, match="no power meter"):
        Coordinator(ProxyLinks()).start(AutoCalibration(1e9, 2e9, 2), bind={"signal_generator": gen})
    run = coord.start(AutoCalibration(1e9, 2e9, 5))
    with pytest.raises(ScenarioError, match="busy"):              # один прибор - один сценарий
        coord.start(AutoCalibration(1e9, 2e9, 5))
    assert coord.busy(pm) is run and coord.busy(gen) is run
    run.stop()
    _finish(qtbot, run)
    pm.close()
    qtbot.waitUntil(lambda: pm.state is ConnectionState.DISCONNECTED, timeout=5000)
    with pytest.raises(ScenarioError, match="not connected"):
        coord.start(AutoCalibration(1e9, 2e9, 5))


def test_acal_fits_gain_and_table_and_restores(qtbot, lab, tmp_path):
    gen, pm, coord = lab
    pm.set("offset", 3.0)
    pm.set("offset.state", True)
    _wait_value(qtbot, pm, "offset.state", lambda v: v is True)
    pm.set("trig.source", "INT")
    _wait_value(qtbot, pm, "trig.source", lambda v: v == "INT")
    assert gen.value("rf.output") is False
    pm_freq = pm.value("freq")
    path = tmp_path / "cal" / "acal.csv"
    run = coord.start(AutoCalibration(1e9, 3e9, 5, path=path, settle_s=0.02))
    points, messages = [], []
    run.point.connect(points.append)
    run.message.connect(messages.append)
    _finish(qtbot, run)
    assert run.state == "done", run.error
    assert any("GAIN -11.00 dB, flatness 1.00 dB p-p" in m for m in messages), messages
    res = run.result
    # R(f) = −(10 + 0.5·f[ГГц]): среднее на 1..3 ГГц = −11, таблица = 0.5·f − 1
    assert res.gain == pytest.approx(-(LOSS + 2 * SLOPE), abs=0.01)
    expect = [(f, SLOPE * f / 1e9 - 2 * SLOPE) for f in frequency_points(1e9, 3e9, 5)]
    table = load_csv(path)
    assert [f for f, _ in table] == pytest.approx([f for f, _ in expect])
    assert [d for _, d in table] == pytest.approx([d for _, d in expect], abs=0.01)
    assert len(points) == 5 and points[-1]["level"] == pytest.approx(-5.0)
    # что меняли - вернули: ВЧ выключен, частоты, поправка и запуск ваттметра - как были
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)
    assert gen.value("rf.frequency") == pytest.approx(5e9)
    assert pm.value("freq") == pytest.approx(pm_freq)
    assert pm.value("offset.state") is True and pm.value("trig.source") == "INT"


def test_acal_counts_duty_only_with_pulse_modulation(qtbot, lab, tmp_path):
    gen, pm, coord = lab
    run = _finish(qtbot, coord.start(AutoCalibration(1e9, 2e9, 2, duty_db=10.0, settle_s=0.02)))
    assert run.state == "done" and run.result.duty_db == 0.0           # CW: скважность не учитывается
    for key in ("pulse.state", "mod.output"):
        gen.set(key, True)
        _wait_value(qtbot, gen, key, lambda v: v is True)
    run = _finish(qtbot, coord.start(AutoCalibration(1e9, 2e9, 2, duty_db=10.0, settle_s=0.02)))
    assert run.state == "done" and run.result.duty_db == 10.0
    # стенд не моделирует импульсы: показание то же, поэтому GAIN выше на 10 dB
    assert run.result.gain == pytest.approx(-(LOSS + 1.5 * SLOPE) + 10.0, abs=0.01)


def test_stop_returns_instruments(qtbot, lab, tmp_path):
    gen, pm, coord = lab
    path = tmp_path / "x.csv"
    run = coord.start(AutoCalibration(1e9, 3e9, 50, path=path, settle_s=0.05))
    qtbot.waitUntil(lambda: run.done >= 2, timeout=10000)
    assert gen.value("rf.output") is True
    run.stop()
    _finish(qtbot, run)
    assert run.state == "stopped" and not path.exists()
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)
    assert gen.value("rf.frequency") == pytest.approx(5e9)


def test_lost_instrument_fails_run_and_generator_is_returned(qtbot, lab):
    gen, pm, coord = lab
    run = coord.start(AutoCalibration(1e9, 3e9, 50, settle_s=0.05))
    qtbot.waitUntil(lambda: run.done >= 1, timeout=10000)
    pm.close()                                                        # ваттметр пропал
    _finish(qtbot, run)
    assert run.state == "failed" and "PM" in run.error
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)        # ВЧ генератора выключен обратно


def test_out_of_range_fails_before_any_command(qtbot, lab):
    gen, pm, coord = lab
    n = len(_sim(gen).history)
    run = _finish(qtbot, coord.start(AutoCalibration(1e9, 900e9, 3)))
    assert run.state == "failed" and "rf.frequency" in run.error
    assert not [c for c in _sim(gen).history[n:] if not c.endswith("?")]     # ничего не записано


def test_stop_all_waits_for_restore(qtbot, lab):
    gen, pm, coord = lab
    run = coord.start(AutoCalibration(1e9, 3e9, 50, settle_s=0.05))
    qtbot.waitUntil(lambda: run.done >= 1, timeout=10000)
    assert coord.stop_all(wait_ms=5000) is True
    assert run.state == "stopped" and coord.runs() == []
    assert gen.value("rf.output") is False


def test_abort_without_restore(qtbot, lab):
    gen, pm, coord = lab
    run = coord.start(AutoCalibration(1e9, 3e9, 50, settle_s=0.05))
    qtbot.waitUntil(lambda: run.done >= 1, timeout=10000)
    run.stop()
    run.stop()                                                        # второй раз - прервать и возврат
    assert run.state == "stopped" and coord.runs() == []


def test_sweep_measures_each_frequency(qtbot, lab):
    gen, pm, coord = lab
    run = _finish(qtbot, coord.start(FrequencySweep([1e9, 2e9], settle_s=0.02)))
    assert run.state == "done"
    assert [(f, round(p, 2)) for f, p, _lvl in run.result] == [(1e9, -5.0 - LOSS - SLOPE), (2e9, -5.0 - LOSS - 2 * SLOPE)]


# ---------------------------------------------------------------- окно ваттметра + COORD
@pytest.fixture
def pm_window(qtbot, tmp_path, monkeypatch):
    from visalab.front import PowerMeterWindow
    monkeypatch.setattr(FrequencySweep, "SETTLE_S", 0.02)
    settings = QSettings(str(tmp_path / "pm.ini"), QSettings.Format.IniFormat)
    gen, pm = _proxy("e8267d"), _proxy("u2021xa")
    win = PowerMeterWindow(pm, settings)
    qtbot.addWidget(win)
    LINKS.add(gen)
    for p in (gen, pm):
        _open(qtbot, p)
    pm.set_poll_interval(50)
    bench = SimBench([_sim(gen)], [], [_sim(pm)], loss_db=LOSS, slope_db_per_ghz=SLOPE)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(bench.step)
    timer.start()
    gen.set("rf.power", -5.0)
    _wait_value(qtbot, gen, "rf.power", lambda v: v == pytest.approx(-5.0))
    yield gen, pm, win, settings
    COORD.stop_all(wait_ms=3000)
    timer.stop()
    LINKS.remove(gen)
    InstrumentProxy.shutdown_all([gen, pm])


def _acal_fields(win, start, stop, pts):
    for w, text in ((win.acal_start, start), (win.acal_stop, stop), (win.acal_points, pts)):
        w.setText(text)
        w.setModified(True)
        w._commit()


def test_acal_button_runs_coordinator_and_applies_result(qtbot, pm_window):
    gen, pm, win, settings = pm_window
    _acal_fields(win, "1", "3", "5")
    win.acal_run.click()
    assert win.acal_run.text() == "■" and COORD.busy(pm) is not None
    assert not win.acal_start.isEnabled()
    qtbot.waitUntil(lambda: win.readout.cal_button.text().startswith("ACAL "), timeout=5000)
    qtbot.waitUntil(lambda: win._acal is None, timeout=30000)
    assert win.acal_run.text() == "▶" and COORD.busy(pm) is None
    name = acal_file_name(1e9, 3e9, 5)
    assert (win.cal_dir / name).is_file() and settings.value(f"{win._prefix}/CAL_FILE") == name
    assert win.cal_choice.value == name and len(win.cal_points) == 5
    assert win.gain_on.value is True and win.gain.value == pytest.approx(-(LOSS + 2 * SLOPE), abs=0.01)
    assert "GAIN" in win.error_log.toPlainText()
    # поправка в приборе: показание ваттметра = уровень генератора на любой частоте полосы
    gen.set("rf.output", True)
    for f in (1.25e9, 2.6e9):
        gen.set("rf.frequency", f)
        pm.set("freq", f)
        _wait_value(qtbot, pm, "meas.power", lambda v: v is not None and v == pytest.approx(-5.0, abs=0.02),
                    timeout=10000)


def test_acal_button_stops(qtbot, pm_window):
    gen, pm, win, settings = pm_window
    _acal_fields(win, "1", "3", "200")
    win.acal_run.click()
    qtbot.waitUntil(lambda: win._acal is not None and win._acal.done >= 2, timeout=10000)
    win.acal_run.click()                                              # ■ - стоп
    qtbot.waitUntil(lambda: win._acal is None, timeout=10000)
    assert "stopped" in win.error_log.toPlainText()
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)
    assert settings.value(f"{win._prefix}/CAL_FILE") in (None, "")


def test_acal_without_generator_logs(qtbot, pm_window):
    gen, pm, win, settings = pm_window
    LINKS.remove(gen)
    _acal_fields(win, "1", "3", "5")
    win.acal_run.click()
    assert win._acal is None and "no signal generator" in win.error_log.toPlainText()
    LINKS.add(gen)


def test_window_does_not_write_correction_during_scenario(qtbot, pm_window):
    gen, pm, win, settings = pm_window
    win.gain_on.set_value(True)
    win.gain.set_value(10.0)
    _wait_value(qtbot, pm, "offset", lambda v: v == pytest.approx(-10.0, abs=1e-3))
    _acal_fields(win, "1", "3", "200")
    win.acal_run.click()
    qtbot.waitUntil(lambda: win._acal is not None and win._acal.done >= 2, timeout=10000)
    assert pm.value("offset.state") is False                         # ACAL меряет без поправки
    win.gain.set_value(5.0)                                          # окно не вмешивается
    qtbot.wait(200)
    assert pm.value("offset.state") is False and pm.value("offset") == pytest.approx(-10.0, abs=1e-3)
    win.acal_run.click()
    qtbot.waitUntil(lambda: win._acal is None, timeout=10000)
    _wait_value(qtbot, pm, "offset", lambda v: v == pytest.approx(-5.0, abs=1e-3))   # после - поправка окна
    _wait_value(qtbot, pm, "offset.state", lambda v: v is True)


def test_main_window_shows_running_scenario(qtbot, pm_window, tmp_path):
    from visalab.coord import ActivityMonitor
    from visalab.front.main_window import MainWindow
    gen, pm, win, _settings = pm_window
    main = MainWindow(QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat), ActivityMonitor())
    qtbot.addWidget(main)
    assert main.scenario_chip.isHidden()
    _acal_fields(win, "1", "3", "200")
    win.acal_run.click()
    qtbot.waitUntil(lambda: main.scenario_chip.text().startswith("ACAL\n") and "/200" in main.scenario_chip.text(),
                    timeout=10000)
    assert not main.scenario_chip.isHidden() and main.scenario_chip.indicated()
    win.acal_run.click()
    qtbot.waitUntil(lambda: main.scenario_chip.isHidden(), timeout=10000)
    main.monitor.stop()


# ------------------------------------------------------------------ ACAL на стенде: что бывает на железе
def _lagging(sim, window_s=0.15):
    """Датчик усредняет за window_s: после смены уровня показание ещё window_s «тянет» старое значение."""
    real, hist = sim._compute, []

    def compute(triggered):
        r = real(triggered)
        now = time.monotonic()
        if not hist or hist[-1][1] != r["AVER"]:
            hist.append((now, r["AVER"]))
        start = now - window_s
        cur_t, cur_v, acc = start, hist[0][1], 0.0
        for t_i, v_i in hist:
            if t_i <= start:
                cur_v = v_i
                continue
            acc += cur_v * (t_i - cur_t)
            cur_t, cur_v = t_i, v_i
        acc += cur_v * (now - cur_t)
        return {**r, "AVER": acc / window_s}
    sim._compute = compute


def _response_error(run, loss, slope):
    """Наибольшее отличие измеренного R(f) от тракта стенда, dB."""
    return max(abs(r + loss + slope * f / 1e9) for f, r in run.result.response)


def test_acal_waits_for_settled_reading(qtbot, lab):
    """Ваттметр усредняет: сразу после смены частоты показание ещё «тянет» старое значение. ACAL берёт
    показание, когда два подряд совпали; с одним показанием на точку таблица была бы с ошибкой."""
    gen, pm, coord = lab
    coord.bench.slope_db_per_ghz = 4.0                             # 2 dB между точками
    _lagging(_sim(pm))
    hasty = _finish(qtbot, coord.start(AutoCalibration(1e9, 3e9, 5, settle_s=0.02, max_readings=1)))
    assert hasty.state == "done", hasty.error
    careful = _finish(qtbot, coord.start(AutoCalibration(1e9, 3e9, 5, settle_s=0.02)))
    assert careful.state == "done", careful.error
    assert _response_error(hasty, LOSS, 4.0) > 0.3
    assert _response_error(careful, LOSS, 4.0) < AutoCalibration.STABLE_DB      # в пределах «совпали»


def test_acal_without_signal_fails_with_reason(qtbot, lab):
    gen, pm, coord = lab
    coord.bench.loss_db = 80.0                                     # кабель не подключён
    run = _finish(qtbot, coord.start(AutoCalibration(1e9, 3e9, 5, settle_s=0.02)))
    assert run.state == "failed" and "no signal at PM" in run.error and "check the cable" in run.error
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)        # генератор вернули


def test_acal_stops_when_generator_is_changed_by_hand(qtbot, lab):
    gen, pm, coord = lab
    run = coord.start(AutoCalibration(1e9, 3e9, 5, settle_s=0.5))
    fiddled = []

    def by_hand(done, _total, _text):
        if done == 1 and not fiddled:                               # точка 2: частота поставлена, идёт пауза
            fiddled.append(1)
            QTimer.singleShot(250, lambda: gen.set("rf.frequency", 7e9))
    run.progress.connect(by_hand)
    _finish(qtbot, run)
    assert run.state == "failed" and "GEN rf.frequency changed during ACAL" in run.error, run.error
    _wait_value(qtbot, gen, "rf.output", lambda v: v is False)


def test_generator_lost_during_acal_rf_off_when_it_reconnects(qtbot, lab):
    """Связь с генератором пропала посреди ACAL (сбой LAN): ВЧ у него остался включённым. Сценарий не
    может его выключить - это сделает координатор, когда генератор переподключится сам."""
    gen, pm, coord = lab
    gen._worker.RECONNECT_DELAYS_S = (0.05,)
    tr, sim = gen._worker.transport, _sim(gen)
    run = coord.start(AutoCalibration(1e9, 3e9, 50, settle_s=0.05))
    messages = []
    run.message.connect(messages.append)
    qtbot.waitUntil(lambda: run.done >= 1, timeout=10000)
    sim.powered, tr.fail_after = False, tr._ops                       # обмен с генератором оборвался
    _finish(qtbot, run)
    assert run.state == "failed" and "GEN" in run.error
    assert any("GEN: no connection" in m and "rf.output OFF when it reconnects" in m for m in messages), messages
    assert ("rf.output", False) in coord.pending_restore(gen)
    assert sim.s["rf.output"] is True                                 # на самом приборе ВЧ ещё включён
    sim.powered, tr.fail_after = True, None                           # связь вернулась
    qtbot.waitUntil(lambda: gen.is_connected, timeout=5000)
    qtbot.waitUntil(lambda: sim.s["rf.output"] is False, timeout=5000)
    assert coord.pending_restore(gen) == []
