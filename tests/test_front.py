"""Окно генератора импульсов (visalab.front) на симуляторе SDG."""
import pytest
from PyQt6.QtCore import QSettings

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import SiglentSDG
from visalab.front import PulseGenWindow
from visalab.front.params import PSButton
from visalab.sim import SiglentSDGSim


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "front.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def opened(qtbot, settings):
    sim = SiglentSDGSim()
    proxy = InstrumentProxy(SiglentSDG, SimTransport(sim))
    win = PulseGenWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    for n in (1, 2):
        proxy.set(f"ch{n}.wave", "PULSE")
    qtbot.waitUntil(lambda: proxy.value("ch2.wave") == "PULSE", timeout=5000)
    yield sim, proxy, win
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def test_line_edit_and_cycle_button(qtbot, opened):
    sim, proxy, win = opened
    ch1 = win.channels[0]
    ch1["high"].setText("1.5 V")
    ch1["high"].setModified(True)
    ch1["high"].editingFinished.emit()
    _wait(qtbot, proxy, "ch1.high", lambda v: v == pytest.approx(1.5))
    assert isinstance(ch1["output"], PSButton)
    assert ch1["output"].text() == "OFF"
    ch1["output"].click()
    _wait(qtbot, proxy, "ch1.output", lambda v: v is True)
    qtbot.waitUntil(lambda: ch1["output"].text() == "ON")
    before = proxy.value("ch1.load")
    ch1["load"].click()                                  # HiZ <-> 50 Ω
    _wait(qtbot, proxy, "ch1.load", lambda v: v != before)


def test_preset_add_apply_and_eqphase(qtbot, opened):
    sim, proxy, win = opened
    win.channels[0]["period"].set_value(2e-3)
    _wait(qtbot, proxy, "ch1.period", lambda v: v == pytest.approx(2e-3))
    win.channels[0]["width"].set_value(50e-6)
    _wait(qtbot, proxy, "ch1.width", lambda v: v == pytest.approx(50e-6))
    win._add_preset()
    idx = win.presets.find(2e-3, 50e-6)
    assert idx >= 0
    eq0 = sim.eqphase_count
    win._apply_preset(idx)
    for n in (1, 2):
        _wait(qtbot, proxy, f"ch{n}.period", lambda v: v == pytest.approx(2e-3))
        _wait(qtbot, proxy, f"ch{n}.width", lambda v: v == pytest.approx(50e-6))
    assert sim.eqphase_count > eq0
    # пресеты сохраняются в ini и удаляются
    win.presets.set_checked(idx)
    assert win.presets.remove_selected()
    assert win.presets.find(2e-3, 50e-6) < 0


def test_init_restores_saved_values(qtbot, opened, settings):
    sim, proxy, win = opened
    win.channels[1]["delay"].set_value(1e-6)
    _wait(qtbot, proxy, "ch2.delay", lambda v: v == pytest.approx(1e-6))
    win.channels[0]["low"].set_value(-1.0)
    _wait(qtbot, proxy, "ch1.low", lambda v: v == pytest.approx(-1.0))
    win._init()
    with qtbot.waitSignal(proxy.action_done, timeout=5000):
        pass
    _wait(qtbot, proxy, "ch2.delay", lambda v: v == pytest.approx(1e-6))
    _wait(qtbot, proxy, "ch1.low", lambda v: v == pytest.approx(-1.0))
    _wait(qtbot, proxy, "ch1.wave", lambda v: v == "PULSE")
