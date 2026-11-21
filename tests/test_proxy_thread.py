"""Интеграция: прокси в GUI-потоке, прибор в своём QThread, только сигналы."""
import time

import pytest

from PyQt6.QtCore import QThread

from visalab.core import ConnectionState, PStatus
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport
from visalab.drivers import ItechIT6300, SiglentSDG
from visalab.sim import ItechIT6300Sim, SiglentSDGSim

from conftest import Recorder


def test_worker_runs_in_own_thread_and_gui_is_not_blocked(qtbot):
    sim = SiglentSDGSim()
    proxy = InstrumentProxy(SiglentSDG, SimTransport(sim, latency_s=0.02))  # 20 мс на операцию
    rec = Recorder(proxy)
    assert proxy._worker.thread() is not QThread.currentThread()
    t0 = time.perf_counter()
    with qtbot.waitSignal(proxy.sync_done, timeout=10000):
        proxy.open()
        assert time.perf_counter() - t0 < 0.05      # open() не ждёт прибор
    assert proxy.state is ConnectionState.CONNECTED
    assert proxy.value("ch1.frequency") == 1000.0

    got = []
    proxy.subscribe("ch2.frequency", got.append)
    with qtbot.waitSignal(proxy.param_status, timeout=5000,
                          check_params_cb=lambda k, s, m: k == "ch2.frequency" and s is PStatus.WRITTEN):
        proxy.set("ch2.frequency", 12345.0)
    assert got[-1] == 12345.0 and sim.ch[2]["BSWV"]["FRQ"] == 12345.0
    proxy.shutdown()
    assert not proxy._thread.isRunning() and proxy.state is ConnectionState.DISCONNECTED
    qtbot.wait(20)                      # доставить события, поставленные в очередь до остановки
    assert ConnectionState.DISCONNECTED in rec.states


def test_two_instruments_independent(qtbot):
    a = InstrumentProxy(ItechIT6300, SimTransport(ItechIT6300Sim(), address="SIM::A"))
    b = InstrumentProxy(ItechIT6300, SimTransport(ItechIT6300Sim(), address="SIM::B"))
    with qtbot.waitSignals([a.sync_done, b.sync_done], timeout=5000):
        a.open(); b.open()
    with qtbot.waitSignal(a.param_status, timeout=5000,
                          check_params_cb=lambda k, s, m: s is PStatus.WRITTEN):
        a.set("ch1.voltage", 7.0)
    assert a.value("ch1.voltage") == 7.0 and b.value("ch1.voltage") == 0.0   # сигналы не пересекаются
    a.shutdown(); b.shutdown()


def test_polling_updates_measurements(qtbot):
    sim = ItechIT6300Sim()
    proxy = InstrumentProxy(ItechIT6300, SimTransport(sim))
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    proxy.set_poll_interval(50)
    proxy.set("ch1.voltage", 5.0)
    proxy.set("ch1.output", True)
    qtbot.waitUntil(lambda: proxy.value("ch1.meas_v") == 5.0 and proxy.value("ch1.mode") == "CV", timeout=3000)
    sim.set_load(1, 1.0)            # 5 А > 3 А -> CC
    qtbot.waitUntil(lambda: proxy.value("ch1.mode") == "CC", timeout=3000)
    proxy.shutdown()


def test_command_delay_spaces_out_commands(make):
    from visalab.sim import SiglentSDGSim as S
    from visalab.drivers import SiglentSDG as D
    import time as _t
    drv, sim, rec = make(D, S, driver_kw={"command_delay_s": 0.03})
    stamps = []
    orig = sim.write
    sim.write = lambda m: (stamps.append(_t.monotonic()), orig(m))
    for _ in range(5):
        drv.query("*OPC?")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.029


@pytest.mark.real_pacing
def test_sdg_has_default_delay():
    from visalab.drivers import ItechIT6300 as P, SiglentSDG as D
    from visalab.drivers import default_delay
    assert D.COMMAND_DELAY_S == 0.15 and P.COMMAND_DELAY_S == 0.0      # как COMMAND_DELAY_SECONDS в PulseGen.py
    assert default_delay("sdg") == 0.15


def test_proxy_skips_unchanged_writes_and_respects_also_read_in_flight(qtbot):
    """Прокси хранит текущие характеристики прибора и не шлёт драйверу неизменённые параметры,
    но не пропускает зависимые ключи (also_read), если в той же пачке ушла запись, влияющая на них."""
    sim = SiglentSDGSim()
    proxy = InstrumentProxy(SiglentSDG, SimTransport(sim), command_delay_s=0.0)
    rec = Recorder(proxy)
    try:
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        for k, v in (("ch1.wave", "PULSE"), ("ch1.period", 2e-3), ("ch1.width", 50e-6),
                     ("ch1.high", 3.0), ("ch1.low", 0.0), ("ch1.delay", 1e-6)):
            proxy.set(k, v)
        qtbot.waitUntil(lambda: proxy.value("ch1.delay") == pytest.approx(1e-6), timeout=5000)
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.sync()
        del sim.history[:]
        eq0 = sim.eqphase_count
        # Повторная отправка тех же значений отфильтровывается в прокси без команд в драйвер/прибор
        rec.statuses.clear()
        keys = ("ch1.wave", "ch1.period", "ch1.width", "ch1.high", "ch1.low", "ch1.delay")
        for k, v in zip(keys, ("PULSE", 2e-3, 50e-6, 3.0, 0.0, 1e-6)):
            proxy.set(k, v)
        qtbot.waitUntil(lambda: len(rec.statuses) == len(keys), timeout=2000)
        assert sim.history == [] and sim.eqphase_count == eq0
        assert all(rec.last_status(k)[0] is PStatus.WRITTEN for k in keys)

        # Изменили только ch1.high среди всей пачки - прокси пропускает остальные и отправляет только ch1.high
        for k, v in zip(keys, ("PULSE", 2e-3, 50e-6, 4.0, 0.0, 1e-6)):
            proxy.set(k, v)
        qtbot.waitUntil(lambda: proxy.value("ch1.high") == pytest.approx(4.0), timeout=5000)
        writes = [h for h in sim.history if "?" not in h]
        assert writes == ["C1:BSWV HLEV,4"] and sim.eqphase_count == eq0

        # Если в пачке меняется ch1.load (влияет на ch1.high через also_read), запись ch1.high не пропускается
        proxy.set("ch1.high", 6.0)
        proxy.set("ch1.low", -6.0)
        qtbot.waitUntil(lambda: proxy.value("ch1.high") == pytest.approx(6.0) and proxy.value("ch1.low") == pytest.approx(-6.0),
                        timeout=5000)
        del sim.history[:]
        proxy.set("ch1.load", "50")                  # HIZ -> 50 Ом меняет пределы уровней
        proxy.set("ch1.high", 6.0)                   # не должен быть пропущен прокси до завершения ch1.load
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.sync()
        assert any(h.startswith("C1:BSWV HLEV") for h in sim.history)
    finally:
        proxy.shutdown()
