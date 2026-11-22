"""Тесты sweep-режимов ваттметра (PM) и анализатора спектра (SA): сценарии координатора и UI."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings, QTimer

from conftest import sim_of
from visalab.coord import (
    COORD,
    Coordinator,
    Instrument,
    PowerMeterSweep,
    ProxyLinks,
    SpectrumSweep,
    SweepPoint,
    SweepResult,
    sweep_points,
)
from visalab.drivers import DRIVERS
from visalab.front import PowerMeterWindow, SpectrumWindow
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import BENCH, SimBench
from visalab.transport import SimTransport


# ------------------------------------------------------------------ сетки и SweepResult
def test_sweep_points_linear_log_and_validation():
    assert sweep_points(1e9, 3e9, 5) == pytest.approx([1e9, 1.5e9, 2e9, 2.5e9, 3e9])
    assert sweep_points(0.0, -20.0, 5) == pytest.approx([0.0, -5.0, -10.0, -15.0, -20.0])
    assert sweep_points(1e9, 100e9, 3, log=True) == pytest.approx([1e9, 10e9, 100e9])
    assert sweep_points(2e9, 2e9, 1) == [2e9]

    with pytest.raises(ValueError):
        sweep_points(1e9, 2e9, 1)
    with pytest.raises(ValueError):
        sweep_points(1e9, 1e9, 3)
    with pytest.raises(ValueError):
        sweep_points(0.0, 1e9, 5, log=True)
    with pytest.raises(ValueError):
        sweep_points(-1.0, 1e9, 5, log=True)


def test_sweep_result_traces_summary_and_p1db():
    # Убывающая развёртка по частоте -> трасса сортируется по возрастанию X
    res_f = SweepResult("FREQ", "power_meter", [
        SweepPoint(3e9, -10.0, -22.0, -12.0),
        SweepPoint(1e9, -10.0, -20.0, -10.0),
        SweepPoint(2e9, -10.0, -21.0, -11.0),
    ])
    tr_pout = res_f.traces("POUT")
    assert len(tr_pout) == 1 and tr_pout[0][1].x == (1e9, 2e9, 3e9) and tr_pout[0][1].y == (-20.0, -21.0, -22.0)
    assert tr_pout[0][1].x_unit == "Hz" and tr_pout[0][1].y_unit == "dBm"
    tr_gain = res_f.traces("GAIN")
    assert tr_gain[0][1].y == (-10.0, -11.0, -12.0) and tr_gain[0][1].y_unit == "dB"
    assert "Δ 2.00 dB" in res_f.summary("POUT")

    # POW режим с компрессией на входе +5 dBm (G0 = +10 dB, при +5 dBm усиление +9 dB -> P1dB in = +5, out = +14)
    res_p = SweepResult("POW", "spectrum_analyzer", [
        SweepPoint(2e9, -20.0, -10.0, 10.0),
        SweepPoint(2e9, -10.0, 0.0, 10.0),
        SweepPoint(2e9, 0.0, 10.0, 10.0),
        SweepPoint(2e9, 10.0, 18.0, 8.0),
    ])
    comp = res_p.p1db()
    assert comp is not None
    assert comp[0] == pytest.approx(5.0) and comp[1] == pytest.approx(14.0) and comp[2] == pytest.approx(10.0)
    assert "P1dB +5.0 in / +14.0 out dBm" in res_p.summary("POUT")
    assert res_p.traces("POUT")[0][1].x_unit == "dBm"

    # 2D режим -> по одной трассе на каждый уровень мощности
    res_2d = SweepResult("2D", "power_meter", [
        SweepPoint(1e9, -10.0, -20.0, -10.0),
        SweepPoint(2e9, -10.0, -21.0, -11.0),
        SweepPoint(1e9, 0.0, -10.0, -10.0),
        SweepPoint(2e9, 0.0, -11.0, -11.0),
    ])
    t2d = res_2d.traces("POUT")
    assert [n for n, _t in t2d] == ["-10 dBm", "0 dBm"]
    assert "2 lvl · 4 pts" in res_2d.summary("POUT")


# ------------------------------------------------------------------ сценарии на изолированном стенде
def _inst(kind):
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


@pytest.fixture
def isolated_bench(qtbot):
    links = ProxyLinks()
    coord = Coordinator(links)
    gen, pm, sa = _inst("e8267d"), _inst("u2021xa"), _inst("e4446a")
    sim_of(sa).c.update(center=1e9, span=20e6, rbw=100e3, swt=0.001)
    sim_of(sa)._couple()
    bench = SimBench([sim_of(gen)], [sim_of(sa)], [sim_of(pm)], loss_db=10.0, slope_db_per_ghz=0.5)
    timer = QTimer()
    timer.setInterval(15)
    timer.timeout.connect(bench.step)
    timer.start()
    for p in (gen, pm, sa):
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    bench.step()
    yield coord, gen, pm, sa, bench
    timer.stop()
    coord.stop_all(2000)
    coord.follow.stop()
    for p in (gen, pm, sa):
        p.shutdown()


def test_power_meter_sweep_freq_pow_2d_and_offset(qtbot, isolated_bench):
    coord, gen, pm, _sa, _bench = isolated_bench
    gen.set("rf.frequency", 1.5e9)
    gen.set("rf.power", -5.0)
    gen.set("rf.output", False)
    qtbot.waitUntil(lambda: gen.value("rf.frequency") == pytest.approx(1.5e9)
                    and gen.value("rf.power") == pytest.approx(-5.0)
                    and gen.value("rf.output") is False, timeout=5000)

    # 1) FREQ sweep с поправкой offset_fn (+10 dB на каждой частоте)
    sc_f = PowerMeterSweep("FREQ", freqs=sweep_points(1e9, 3e9, 3), settle_s=0.03,
                           skip=1, max_readings=2, offset_fn=lambda f: 10.0)
    run = coord.start(sc_f)
    with qtbot.waitSignal(run.ended, timeout=15000):
        pass
    assert run.state == run.DONE and isinstance(run.result, SweepResult)
    assert len(run.result.points) == 3
    # Тракт: -(10 + 0.5*f_GHz), поправка +10 -> ожидаем Pout ≈ -5 - 0.5*f_GHz
    for pt in run.result.points:
        expected = -5.0 - 0.5 * (pt.freq / 1e9)
        assert pt.power == pytest.approx(expected, abs=0.25)
    # Исходные состояния возвращены
    assert gen.value("rf.output") is False
    assert gen.value("rf.frequency") == pytest.approx(1.5e9)
    assert pm.value("offset.state") is False

    # 2) POW sweep
    sc_p = PowerMeterSweep("POW", powers=sweep_points(-20.0, 0.0, 3), settle_s=0.03, skip=1, max_readings=2)
    run_p = coord.start(sc_p)
    with qtbot.waitSignal(run_p.ended, timeout=15000):
        pass
    assert run_p.state == run_p.DONE and len(run_p.result.points) == 3
    for pt in run_p.result.points:
        # loss на 1.5 GHz = 10.75 dB
        assert pt.gain == pytest.approx(-10.75, abs=0.25)
    assert gen.value("rf.power") == pytest.approx(-5.0)
    assert gen.value("rf.output") is False

    # 3) 2D sweep (2 частоты × 2 мощности = 4 точки)
    sc_2d = PowerMeterSweep("2D", freqs=[1e9, 2e9], powers=[-10.0, 0.0], settle_s=0.03, skip=1, max_readings=2)
    run_2d = coord.start(sc_2d)
    with qtbot.waitSignal(run_2d.ended, timeout=15000):
        pass
    assert run_2d.state == run_2d.DONE and len(run_2d.result.points) == 4
    assert len(run_2d.result.traces("POUT")) == 2


def test_spectrum_sweep_freq_zero_span_pow_and_2d(qtbot, isolated_bench):
    coord, gen, _pm, sa, _bench = isolated_bench
    gen.set("rf.frequency", 1.5e9)
    gen.set("rf.power", -10.0)
    gen.set("rf.output", False)
    sa.set("freq.center", 2.0e9)
    sa.set("freq.span", 50e6)
    qtbot.waitUntil(lambda: gen.value("rf.frequency") == pytest.approx(1.5e9)
                    and gen.value("rf.power") == pytest.approx(-10.0)
                    and sa.value("freq.center") == pytest.approx(2.0e9)
                    and sa.value("freq.span") == pytest.approx(50e6), timeout=5000)

    # 1) Tracking FREQ sweep с полосой 10 MHz
    sc_f = SpectrumSweep("FREQ", freqs=[1e9, 2e9, 3e9], settle_s=0.03, track_center=True, track_span=10e6)
    run = coord.start(sc_f)
    with qtbot.waitSignal(run.ended, timeout=15000):
        pass
    assert run.state == run.DONE and isinstance(run.result, SweepResult)
    assert len(run.result.points) == 3
    for pt in run.result.points:
        expected = -10.0 - (10.0 + 0.5 * (pt.freq / 1e9))
        assert pt.power == pytest.approx(expected, abs=1.0)
        assert pt.floor is not None and pt.floor < pt.power - 20.0
    # Всё восстановлено
    assert gen.value("rf.output") is False
    assert gen.value("rf.frequency") == pytest.approx(1.5e9)
    assert sa.value("freq.center") == pytest.approx(2.0e9)
    assert sa.value("freq.span") == pytest.approx(50e6)
    assert sa.value("meas.continuous") is True

    # 2) Zero-span FREQ sweep (track_span = 0)
    sc_z = SpectrumSweep("FREQ", freqs=[1e9, 2e9], settle_s=0.03, track_center=True, track_span=0.0)
    run_z = coord.start(sc_z)
    with qtbot.waitSignal(run_z.ended, timeout=15000):
        pass
    assert run_z.state == run_z.DONE and len(run_z.result.points) == 2
    assert run_z.result.points[0].power == pytest.approx(-20.5, abs=1.0)
    assert run_z.result.points[0].floor is None

    # 3) POW sweep на SA
    sc_p = SpectrumSweep("POW", powers=[-20.0, -10.0, 0.0], settle_s=0.03, track_center=True, track_span=10e6)
    run_p = coord.start(sc_p)
    with qtbot.waitSignal(run_p.ended, timeout=15000):
        pass
    assert run_p.state == run_p.DONE and len(run_p.result.points) == 3
    for pt in run_p.result.points:
        assert pt.gain == pytest.approx(-10.75, abs=1.0)

    # 4) 2D sweep на SA
    sc_2d = SpectrumSweep("2D", freqs=[1e9, 2e9], powers=[-15.0, -5.0], settle_s=0.03, track_span=10e6)
    run_2d = coord.start(sc_2d)
    with qtbot.waitSignal(run_2d.ended, timeout=15000):
        pass
    assert run_2d.state == run_2d.DONE and len(run_2d.result.points) == 4
    assert len(run_2d.result.traces("GAIN")) == 2


def test_continuous_sweep_repeats_and_restores_on_stop(qtbot, isolated_bench):
    coord, gen, _pm, _sa, _bench = isolated_bench
    gen.set("rf.output", False)
    qtbot.waitUntil(lambda: gen.value("rf.output") is False, timeout=5000)

    cycles = []
    sc = PowerMeterSweep("FREQ", freqs=[1e9, 2e9], settle_s=0.02, skip=0, max_readings=1, repeat=True)
    run = coord.start(sc)
    run.point.connect(lambda pt: cycles.append(pt["cycle"]))
    qtbot.waitUntil(lambda: any(c >= 1 for c in cycles), timeout=10000)
    run.stop()
    with qtbot.waitSignal(run.ended, timeout=5000):
        pass
    assert run.state == run.STOPPED
    assert gen.value("rf.output") is False


# ------------------------------------------------------------------ UI-тесты окон PM и SA
def test_power_meter_window_sweep_tab_ui_and_csv(qtbot, tmp_path):
    ini = tmp_path / "pm_swp.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    gen = COORD.open_instrument("e8267d", "sim")
    pm = COORD.open_instrument("u2021xa", "sim")
    win = PowerMeterWindow(pm, settings)
    qtbot.addWidget(win)
    try:
        for p in (gen, pm):
            with qtbot.waitSignal(p.sync_done, timeout=10000):
                p.open()
        gen.set("rf.power", -10.0)
        qtbot.waitUntil(lambda: gen.value("rf.power") == pytest.approx(-10.0), timeout=5000)
        BENCH.step()

        win.show()
        win.set_expanded(True)
        swp_idx = win._tab_index("SWP")
        assert swp_idx >= 0
        win.set_page(swp_idx)

        sw = win.sweep
        sw.mode.set_value("FREQ")
        sw.f_start.set_value(1e9)
        sw.f_stop.set_value(2e9)
        sw.f_points.set_value(3)
        sw.dwell.set_value(0.25)                    # BENCH.INTERVAL_MS = 200 ms

        assert not sw.p_start.isEnabled() and sw.f_start.isEnabled()
        sw.run_button.click()
        assert sw.running and win.tabs.is_on(swp_idx)
        qtbot.waitUntil(lambda: not sw.running, timeout=20000)
        assert not win.tabs.is_on(swp_idx)
        assert len(sw.points) == 3 and len(sw.plot.traces) == 1
        assert sw.save_button.isEnabled() and sw.clear_button.isEnabled()

        # Переключение POUT -> GAIN обновляет трассу на dB
        sw.view.set_value("GAIN")
        assert sw.plot.traces[0][1].y_unit == "dB"

        # Сохранение CSV
        saved = sw.save_csv()
        assert saved is not None and saved.is_file()
        text = saved.read_text(encoding="utf-8")
        assert "Freq, Hz" in text and "Pout, dBm" in text and "Gain, dB" in text

        # Переключение режимов включает/выключает соответствующие поля
        sw.mode.set_value("POW")
        assert sw.p_start.isEnabled() and not sw.f_start.isEnabled() and not sw.law.isEnabled()
        sw.mode.set_value("2D")
        assert sw.p_start.isEnabled() and sw.f_start.isEnabled() and sw.law.isEnabled()

        # Очистка и восстановление настроек из ini
        sw.clear_button.click()
        assert sw.points == [] and sw.plot.traces == []
        settings.sync()
        win2 = PowerMeterWindow(pm, QSettings(str(ini), QSettings.Format.IniFormat))
        qtbot.addWidget(win2)
        assert win2.sweep.mode.value == "2D" and win2.sweep.view.value == "GAIN"
        assert win2.sweep.f_stop.value == pytest.approx(2e9) and win2.sweep.f_points.value == pytest.approx(3)
    finally:
        win.cleanup()
        COORD.close_all([gen, pm])


def test_spectrum_window_sweep_ui_csv_and_vna_cw_source(qtbot, tmp_path):
    ini = tmp_path / "sa_swp.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    vna = COORD.open_instrument("pna", "sim")
    sa = COORD.open_instrument("e4446a", "sim")
    win = SpectrumWindow(sa, settings)
    qtbot.addWidget(win)
    try:
        for p in (vna, sa):
            with qtbot.waitSignal(p.sync_done, timeout=10000):
                p.open()
        # Включаем ВАЦ в режим CW-генератора (проверка работы SA sweep с ВАЦ в качестве источника)
        vna.set("gen.state", True)
        vna.set("source.power", -10.0)
        qtbot.waitUntil(lambda: vna.value("gen.state") is True
                        and vna.value("source.power") == pytest.approx(-10.0), timeout=5000)
        BENCH.step()

        win.show()
        win.set_expanded(True)
        swp_idx = [win.tabs.tabText(i) for i in range(win.tabs.count())].index("SWP")
        win.set_page(swp_idx)

        sw = win.sweep
        assert sw.sa_track is not None and sw.sa_span is not None
        sw.mode.set_value("FREQ")
        sw.f_start.set_value(1e9)
        sw.f_stop.set_value(2e9)
        sw.f_points.set_value(3)
        sw.dwell.set_value(0.25)
        sw.sa_span.set_value(20e6)

        sw.run_button.click()
        assert sw.running and win.tabs.is_on(swp_idx)
        qtbot.waitUntil(lambda: not sw.running, timeout=20000)
        assert not win.tabs.is_on(swp_idx)
        assert len(sw.points) == 3 and len(sw.plot.traces) == 1

        saved = sw.save_csv()
        assert saved is not None and saved.is_file()
        assert "Pout, dBm" in saved.read_text(encoding="utf-8")

        # Очистка sa_span пустой строкой возвращает KEEP (None)
        sw.sa_span.setText("")
        sw.sa_span.setModified(True)
        sw.sa_span._commit()
        assert sw.sa_span.value is None and settings.value(f"{win._prefix}/SWP_SA_SPAN", None) is None
    finally:
        win.cleanup()
        COORD.close_all([vna, sa])
