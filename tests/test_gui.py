"""Окно строится из описания параметров и отражает значения прибора."""
from visalab.core import InstrumentProxy, PStatus, SimTransport
from visalab.drivers import ItechIT6300, SiglentSDG
from visalab.gui.widgets import NumberField, ToggleField
from visalab.gui.window import InstrumentWindow
from visalab.sim import ItechIT6300Sim, SiglentSDGSim


def _field(win, cls, key):
    return next(w for w in win.findChildren(cls) if w.spec.key == key)


def test_psu_window_roundtrip(qtbot):
    sim = ItechIT6300Sim()
    proxy = InstrumentProxy(ItechIT6300, SimTransport(sim))
    win = InstrumentWindow(proxy)
    qtbot.addWidget(win)
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["Каналы", "Защита", "Общие"]
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    assert win.tabs.isEnabled()
    field = _field(win, NumberField, "ch2.voltage")
    field.setText("12.5")
    field.setModified(True)
    with qtbot.waitSignal(proxy.param_status, timeout=5000,
                          check_params_cb=lambda k, s, m: k == "ch2.voltage" and s is PStatus.WRITTEN):
        field.returnPressed.emit()
    assert sim.chs[1].volt == 12.5 and field.text() == "12.5 V"
    toggle = _field(win, ToggleField, "ch2.output")
    with qtbot.waitSignal(proxy.param_status, timeout=5000,
                          check_params_cb=lambda k, s, m: k == "ch2.output" and s is PStatus.WRITTEN):
        toggle.click()
    assert sim.chs[1].output and toggle.isChecked()
    win.close()
    assert not proxy._thread.isRunning()


def test_bad_input_marks_invalid_without_sending(qtbot):
    sim = SiglentSDGSim()
    proxy = InstrumentProxy(SiglentSDG, SimTransport(sim))
    win = InstrumentWindow(proxy)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    field = _field(win, NumberField, "C1.frequency")
    n = len(sim.history)
    field.setText("abc")
    field.returnPressed.emit()
    assert field.property("pstatus") == "invalid"
    qtbot.wait(50)
    assert not any("FRQ" in h for h in sim.history[n:])
    win.close()
