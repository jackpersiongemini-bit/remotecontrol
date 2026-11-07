"""Временный фронтенд (visalab.quick) на симуляторах."""
import pytest
from PyQt6.QtCore import QSettings

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import ItechIT6300, KeysightE8267D, KeysightU2021XA, RSSMF100A, SiglentSDG
from visalab.quick import FuncGenWindow, PowerMeterWindow, PSUWindow, RFGenWindow, SpinField
from visalab.sim import (ItechIT6300Sim, KeysightE8267DSim, KeysightU2021XASim, RSSMF100ASim,
                         SiglentSDGSim)


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "quick.ini"), QSettings.Format.IniFormat)


def _open(qtbot, win_cls, drv, sim, settings):
    proxy = InstrumentProxy(drv, SimTransport(sim))
    win = win_cls(proxy, settings=settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    return proxy, win


def _wait_value(qtbot, proxy, key, pred, timeout=5000):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=timeout)


def _spin(win, key):
    return next(w for w in win.findChildren(SpinField) if w.key == key)


@pytest.mark.parametrize("drv,sim_cls", [(KeysightE8267D, KeysightE8267DSim), (RSSMF100A, RSSMF100ASim)])
def test_rf_window(qtbot, settings, drv, sim_cls):
    proxy, win = _open(qtbot, RFGenWindow, drv, sim_cls(), settings)
    assert win.freq.value() == pytest.approx(1000.0)            # 1 GHz в MHz
    win.freq.setValue(2500.5)
    win.freq.editingFinished.emit()
    _wait_value(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(2500.5e6))
    win.mod_buttons["am.state"].click()
    _wait_value(qtbot, proxy, "am.state", lambda v: v is True)
    assert {"mod.output", "am.state", "fm.state", "pm.state", "pulse.state"} <= set(win.mod_buttons)
    win.shutdown()


def test_psu_ch1_interlock(qtbot, settings):
    sim = ItechIT6300Sim()
    proxy, win = _open(qtbot, PSUWindow, ItechIT6300, sim, settings)
    win.threshold.setValue(1.0)
    win.bypass.setChecked(False)
    assert not win.outputs[1].isEnabled()                       # CH2/CH3 ничего не потребляют
    assert win.outputs[2].isEnabled() and win.outputs[3].isEnabled()
    win.bypass.setChecked(True)
    assert win.outputs[1].isEnabled()
    win.bypass.setChecked(False)
    # нагрузка на CH2 и CH3 -> потребление есть -> CH1 разрешён
    for n in (2, 3):
        sim.set_load(n, 100.0)
        proxy.set(f"ch{n}.voltage", 5.0)
        proxy.set(f"ch{n}.output", True)
    _wait_value(qtbot, proxy, "ch2.meas_i", lambda v: (v or 0) > 1e-3)
    _wait_value(qtbot, proxy, "ch3.meas_i", lambda v: (v or 0) > 1e-3)
    qtbot.waitUntil(win.outputs[1].isEnabled, timeout=3000)
    win.shutdown()
    s = QSettings(settings.fileName(), QSettings.Format.IniFormat)
    assert s.value("power_supply/interlock_min_mA") is not None


def test_sdg_load_forced_and_presets(qtbot, settings):
    sim = SiglentSDGSim()
    proxy, win = _open(qtbot, FuncGenWindow, SiglentSDG, sim, settings)
    _wait_value(qtbot, proxy, "ch1.load", lambda v: v == "50")
    _wait_value(qtbot, proxy, "ch2.load", lambda v: v == "50")
    win.save_preset("base")
    proxy.set("ch1.frequency", 5e3)
    proxy.set("ch2.amplitude", 2.0)
    _wait_value(qtbot, proxy, "ch2.amplitude", lambda v: v == pytest.approx(2.0))
    _wait_value(qtbot, proxy, "ch1.frequency", lambda v: v == pytest.approx(5e3))
    changes = dict(win.preset_changes("base"))
    assert set(changes) == {"ch1.frequency", "ch2.amplitude"}    # только отличия
    n = len(sim.history)
    assert win.apply_preset("base") == 2
    _wait_value(qtbot, proxy, "ch1.frequency", lambda v: v == pytest.approx(1e3))
    _wait_value(qtbot, proxy, "ch2.amplitude", lambda v: v == pytest.approx(win.presets()["base"]["ch2.amplitude"]))
    writes = [h for h in sim.history[n:] if "?" not in h]
    assert len([w for w in writes if "BSWV" in w]) == 2, writes
    assert win.preset_changes("base") == []
    win.shutdown()
    # пресет сохранился в ini
    s = QSettings(settings.fileName(), QSettings.Format.IniFormat)
    assert "base" in s.value("function_generator/presets")


def test_power_meter_modes(qtbot, settings):
    sim = KeysightU2021XASim()
    proxy, win = _open(qtbot, PowerMeterWindow, KeysightU2021XA, sim, settings)
    win.level.click()
    _wait_value(qtbot, proxy, "trig.source", lambda v: v == "INT")
    assert win.level.isChecked() and not win.free.isChecked()
    win.free.click()
    _wait_value(qtbot, proxy, "trig.source", lambda v: v == "IMM")
    assert proxy.value("meas.continuous") is True
    win.shutdown()


def test_sdg_on_off_buttons_and_readouts(qtbot, settings):
    from visalab.quick import OnOffButtons, Readout
    proxy, win = _open(qtbot, FuncGenWindow, SiglentSDG, SiglentSDGSim(), settings)
    onoff = next(w for w in win.findChildren(OnOffButtons) if w.key == "ch1.output")
    assert onoff.off_btn.isChecked() and not onoff.on_btn.isChecked()
    onoff.on_btn.click()
    _wait_value(qtbot, proxy, "ch1.output", lambda v: v is True)
    assert onoff.on_btn.isChecked() and onoff.state.text() == "ON"
    onoff.off_btn.click()
    _wait_value(qtbot, proxy, "ch1.output", lambda v: v is False)
    assert onoff.off_btn.isChecked()
    texts = {w.toolTip(): w.text() for w in win.findChildren(Readout)}
    assert texts["ch1.wave"] == "Sine" and texts["ch1.frequency"] == "1 kHz"
    qtbot.waitUntil(lambda: {w.toolTip(): w.text() for w in win.findChildren(Readout)}["ch1.load"] == "50 Ω")
    win.shutdown()


def test_lab_window_holds_all_panels(qtbot, settings):
    from visalab.quick import LabWindow
    panels = []
    for cls, drv, sim in ((RFGenWindow, KeysightE8267D, KeysightE8267DSim()), (PSUWindow, ItechIT6300, ItechIT6300Sim()),
                          (FuncGenWindow, SiglentSDG, SiglentSDGSim()), (PowerMeterWindow, KeysightU2021XA, KeysightU2021XASim())):
        panels.append(cls(InstrumentProxy(drv, SimTransport(sim)), settings=settings))
    win = LabWindow(panels, settings)
    qtbot.addWidget(win)
    win.connect_all.click()
    for p in panels:
        qtbot.waitUntil(lambda p=p: p.proxy.is_connected, timeout=5000)
    rf = panels[0].proxy
    rf.set("rf.output", True)
    _wait_value(qtbot, rf, "rf.output", lambda v: v is True)
    win.outputs_off.click()
    _wait_value(qtbot, rf, "rf.output", lambda v: v is False)
    win.close()
    assert not any(p.proxy._thread.isRunning() for p in panels)
