"""Интеграция: прокси в GUI-потоке, прибор в своём QThread, только сигналы."""
import time

from PyQt6.QtCore import QThread

from visalab.core import ConnectionState, InstrumentProxy, PStatus, SimTransport
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


def test_sdg_has_default_delay():
    from visalab.drivers import ItechIT6300 as P, SiglentSDG as D
    assert D.COMMAND_DELAY_S == 0.05 and P.COMMAND_DELAY_S == 0.0
