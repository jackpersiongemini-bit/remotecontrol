"""Тесты графиков тока ИП (PSU) от частоты и мощности генератора в ``GraphsWindow``."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QAbstractButton

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front import GraphsWindow
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport


def _proxy(kind: str, addr: str | None = None) -> Instrument:
    drv, sim_cls = DRIVERS[kind]
    tr = SimTransport(sim_cls(), address=addr or f"SIM::{kind.upper()}::INSTR")
    return Instrument(InstrumentProxy(drv, tr))


@pytest.fixture
def bench_psu(qtbot):
    links = ProxyLinks()
    psu = _proxy("it6300", "SIM::PSU1::INSTR")
    gen1 = _proxy("e8267d", "SIM::GEN1::INSTR")
    gen2 = _proxy("e8267d", "SIM::GEN2::INSTR")
    pm = _proxy("u2021xa", "SIM::PM1::INSTR")
    proxies = (psu, gen1, gen2, pm)
    try:
        psu_sim = sim_of(psu)
        psu_sim.chs[0].load_ohm = 10.0   # 5 В / 10 Ом = 0.5 A
        psu_sim.chs[1].load_ohm = 20.0   # 5 В / 20 Ом = 0.25 A
        for p in proxies:
            links.add(p)
            p.set_poll_interval(50)
            with qtbot.waitSignal(p.sync_done, timeout=10000):
                p.open()
    except Exception:
        for p in proxies:
            p.shutdown()
        raise
    yield links, psu, gen1, gen2, pm
    for p in proxies:
        p.shutdown()


def test_psu_current_vs_frequency_graphs(qtbot, bench_psu, tmp_path):
    links, psu, gen1, gen2, pm = bench_psu
    psu_sim = sim_of(psu)
    mon = ActivityMonitor(links)
    ini = tmp_path / "main.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    gw = GraphsWindow(settings, mon)
    qtbot.addWidget(gw)
    gw.show()
    qtbot.waitExposed(gw)

    try:
        # Все 3 канала IT6300 в GraphsWindow строят только график тока от частоты (Hz / A), без режима TIME
        for ch_name in ("PSU CH1", "PSU CH2", "PSU CH3"):
            card = gw.card(ch_name)
            assert card is not None
            assert card.mode_button is None
            assert card.plot.x_unit == "Hz"
            assert card.plot.y_unit == "A"

        # Включаем выходы CH1 и CH2 на ИП (CH3 выключен) и генератор GEN1
        psu.set("ch1.voltage", 5.0)
        psu.set("ch1.current", 2.0)
        psu.set("ch1.output", True)
        psu.set("ch2.voltage", 5.0)
        psu.set("ch2.current", 2.0)
        psu.set("ch2.output", True)
        gen1.set("rf.frequency", 1e9)
        gen1.set("rf.power", -10.0)
        gen1.set("rf.output", True)

        qtbot.waitUntil(
            lambda: mon.psu_matrix(psu, 1)[0] == [-10.0] and len(mon.psu_matrix(psu, 1)[1]) == 1,
            timeout=5000,
        )
        psu_sim.chs[0].load_ohm = 5.0  # 1.0 A
        gen1.set("rf.power", 0.0)
        qtbot.waitUntil(lambda: mon.psu_matrix(psu, 1)[0] == [-10.0, 0.0], timeout=5000)

        gen1.set("rf.frequency", 2e9)
        qtbot.waitUntil(lambda: len(mon.psu_matrix(psu, 1)[1]) == 2, timeout=5000)

        cols1, rows1 = mon.psu_matrix(psu, 1)
        cols2, rows2 = mon.psu_matrix(psu, 2)
        assert cols1 == [-10.0, 0.0] and [f for f, _ in rows1] == [1e9, 2e9]
        assert rows1[0][1][-10.0] == pytest.approx(0.5, abs=0.02)
        assert rows1[0][1][0.0] == pytest.approx(1.0, abs=0.02)
        assert rows2[0][1][-10.0] == pytest.approx(0.25, abs=0.02)
        assert mon.psu_matrix(psu, 3) == ([], [])

        # На графике PSU CH1 отображаются кривые тока (A) по частоте (Hz) для каждого уровня генератора
        ch1 = gw.card("PSU CH1")
        qtbot.waitUntil(lambda: ch1.plot.x_unit == "Hz" and len(ch1.plot.series) == 2, timeout=5000)
        assert ch1.plot.y_unit == "A"
        assert [s.name for s in ch1.plot.series] == ["-10 dBm", "0 dBm"]
        assert all(s.show_points for s in ch1.plot.series)

        # Выбор конкретного RF-генератора GEN2 для CH2 в мониторе
        gen2.set("rf.frequency", 3.5e9)
        gen2.set("rf.power", -5.0)
        gen2.set("rf.output", True)
        qtbot.waitUntil(lambda: gen2.value("rf.output") is True, timeout=5000)
        mon.set_psu_rf_source(psu, 2, gen2)
        qtbot.waitUntil(
            lambda: any(abs(f - 3.5e9) < 1.0 for f, _ in mon.psu_matrix(psu, 2)[1]),
            timeout=5000,
        )

        for btn in gw.findChildren(QAbstractButton):
            if btn.isVisible():
                assert btn.toolTip(), f"Missing tooltip on {btn.text()!r} ({type(btn).__name__})"
    finally:
        gw.cleanup()
        gw.close()
        mon.stop()
