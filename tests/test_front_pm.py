"""Окно ваттметра на симуляторе U2021XA."""
import json
import math

import pytest
from PyQt6.QtCore import QSettings

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import DRIVERS
from visalab.front import PowerMeterWindow, PulseGenWindow
from visalab.front.instrument_window import LINKS
from visalab.front.power_meter import interpolate, load_csv, save_csv


def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return InstrumentProxy(drv, SimTransport(sim_cls()))


@pytest.fixture
def pm(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "pm.ini"), QSettings.Format.IniFormat)
    proxy = _proxy("u2021xa")
    win = PowerMeterWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    yield proxy, win, settings
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def test_interpolate():
    t = [(1e9, 1.0), (3e9, 3.0)]
    assert interpolate(t, 2e9) == pytest.approx(2.0)
    assert interpolate(t, 0.5e9) == 1.0 and interpolate(t, 9e9) == 3.0
    assert interpolate([], 2e9) == 0.0


def test_layout_and_readout(qtbot, pm):
    proxy, win, _s = pm
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["MAIN", "CAL", "TIME"]
    avg = next(ln for ln in win.pages[0].lines if ln.name == "AVG")       # усреднение - одной строкой
    assert [w.key for w in avg.widgets] == ["avg.state", "avg.count", "avg.auto"]
    assert not any(ln.name in ("ACNT", "AAUT") for ln in win.pages[0].lines)
    assert len(win.preset_panel.table.lines) == 1 and win.preset_panel.table.lines[0].widgets == [win.readout, win.plot]
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None, timeout=5000)
    qtbot.waitUntil(lambda: win.readout.main.text() == f"{proxy.value('meas.power'):.2f}", timeout=5000)
    assert win.readout.sec_button.text().endswith("W")
    win.readout.sec_button.click()
    assert win.readout.sec_button.text().startswith("PEAK")
    assert win.readout.trig_button.text().startswith("FREE")


def test_trigger_toggle_from_readout(qtbot, pm):
    proxy, win, _s = pm
    _wait(qtbot, proxy, "trig.source", lambda v: v == "IMM")
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "INT")
    qtbot.waitUntil(lambda: win.readout.trig_button.text().startswith("LEVEL"), timeout=5000)
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "EXT")       # внешний запуск: ждём импульс на TRIG IN
    qtbot.waitUntil(lambda: win.readout.trig_button.text() == "EXT · WAIT", timeout=5000)
    sim = proxy._worker.transport.sim
    before = proxy.value("meas.power")
    sim.set_signal(on_dbm=-3.0, width=None)
    sim.fire_external()
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None
                    and proxy.value("meas.power") == pytest.approx(-3.0, abs=0.05), timeout=5000)
    assert before is None or before != pytest.approx(-3.0, abs=0.05)
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "IMM")


def test_correction_written_as_offset(qtbot, pm):
    proxy, win, settings = pm
    proxy.set("freq", 2e9)
    _wait(qtbot, proxy, "freq", lambda v: v == pytest.approx(2e9))
    win.cal_dir.mkdir(exist_ok=True)
    save_csv(win.cal_dir / "cable.csv", [(1e9, 1.0), (3e9, 3.0)])
    win.cal_choice.click()                            # список перечитывает папку
    win.cal_choice.set_value("cable.csv")
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(2.0, abs=1e-3))
    _wait(qtbot, proxy, "offset.state", lambda v: v is True)
    win.gain_on.set_value(True)
    win.gain.set_value(10.0)
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(-8.0, abs=1e-3))
    win.duty_src.set_value("MAN")
    win.duty.set_value(10.0)                          # 10 % -> +10 dB
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(2.0, abs=1e-3))
    assert win.readout.cal_button.text() == "CAL +2.00 dB"
    assert settings.value(f"{win._prefix}/CAL_FILE") == "cable.csv"
    assert win.cal_plot.trace is not None and win.cal_plot.marker() == pytest.approx(2e9)
    proxy.set("freq", 3e9)                            # частота датчика -> новая точка таблицы
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(3.0, abs=1e-3))
    win.cal_on.set_value(False)
    win.gain_on.set_value(False)
    win.duty_src.set_value("OFF")
    _wait(qtbot, proxy, "offset.state", lambda v: v is False)


def test_csv_and_acal_ui(qtbot, pm, tmp_path):
    proxy, win, _s = pm
    path = tmp_path / "t.csv"
    path.write_text("freq;dB\n2e9;1.5\n1e9;0.5\nbad line\n", encoding="utf-8")
    assert load_csv(path) == [(1e9, 0.5), (2e9, 1.5)]
    win.cal_plot.set_points(load_csv(path))
    lo, hi = win.cal_plot.y_range()
    assert lo <= 0.5 and hi >= 1.5 and hi - lo <= 3
    win.cal_plot.resize(300, 150)
    win.cal_plot.grab()
    got = []
    win.acal_requested.connect(lambda *a: got.append(a))
    win.acal_run.click()                                 # поля пустые - только запись в журнал
    assert got == [] and "ACAL" in win.error_log.toPlainText()
    for w, text in ((win.acal_start, "1"), (win.acal_stop, "18"), (win.acal_points, "101")):
        w.setText(text)
        w.setModified(True)
        w._commit()
    win.acal_run.click()
    assert got == [(pytest.approx(1e9), pytest.approx(18e9), 101)]

def test_duty_from_pulse_generator(qtbot, pm, tmp_path):
    proxy, win, _s = pm
    win.duty_src.set_value("CH1")
    assert win.duty_value() is None and win.duty.placeholderText() == "no SDG"
    sdg = _proxy("sdg")
    gen = PulseGenWindow(sdg, QSettings(str(tmp_path / "sdg.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(gen)
    try:
        assert LINKS.find("function_generator") is sdg
        with qtbot.waitSignal(sdg.sync_done, timeout=5000):
            sdg.open()
        sdg.set("ch1.wave", "PULSE")
        sdg.set("ch1.duty", 10.0)
        _wait(qtbot, sdg, "ch1.duty", lambda v: v is not None and v == pytest.approx(10.0, abs=0.5))
        duty = sdg.value("ch1.duty")
        assert duty and win.duty_value() == pytest.approx(duty)
        _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(10 * math.log10(100 / duty), abs=1e-3))
        assert win.duty.isReadOnly()
    finally:
        sdg.shutdown()


def test_zero_needs_confirmation(qtbot, pm):
    proxy, win, _s = pm
    calls = []
    proxy.action_done.connect(lambda name, _r: calls.append(name))
    win.zero_button.click()
    assert win.zero_button.text() == "SURE?" and calls == []
    win.zero_button.click()
    qtbot.waitUntil(lambda: "zero" in calls, timeout=10000)
    assert win.zero_button.text() == "ZERO"


def test_time_domain_trace(qtbot, pm):
    proxy, win, _s = pm
    proxy.set("detector", "AVERAGE")
    _wait(qtbot, proxy, "detector", lambda v: v == "AVERAGE")
    proxy.set("trace.state", True)                      # сам переводит датчик в NORMAL
    _wait(qtbot, proxy, "trace.state", lambda v: v is True)
    _wait(qtbot, proxy, "detector", lambda v: v == "NORMAL")
    proxy.set("trace.time", 2e-3)
    _wait(qtbot, proxy, "trace.time", lambda v: v == pytest.approx(2e-3))
    qtbot.waitUntil(lambda: win.plot.trace is not None and win.plot.trace.x[-1] == pytest.approx(2e-3),
                    timeout=5000)
    t = win.plot.trace
    assert len(t) == 1000 and t.x_unit == "s"
    assert max(t.y) == pytest.approx(-10.0, abs=0.01)    # импульс симулятора -10 dBm
    qtbot.waitUntil(lambda: win.tabs.is_on(2), timeout=5000)
    assert not win.plot.isHidden() and win.readout.isHidden()          # график вместо индикатора
    win.plot.resize(400, 200)
    win.plot.grab()                                     # отрисовка без ошибок
    proxy.set("detector", "AVERAGE")                    # трасса только в NORMAL - выключается
    _wait(qtbot, proxy, "trace.state", lambda v: v is False)
    qtbot.waitUntil(lambda: win.plot.trace is None and not win.tabs.is_on(2), timeout=5000)
    assert win.plot.isHidden() and not win.readout.isHidden()

def test_pm_launcher_with_sdg(qtbot, tmp_path, monkeypatch):
    import run_power_meter
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    for cls in (run_power_meter.PowerMeterWindow, run_power_meter.PulseGenWindow):
        orig = cls.__init__

        def track(self, *a, _orig=orig, **k):
            _orig(self, *a, **k)
            made.append(self)
        monkeypatch.setattr(cls, "__init__", track)
    assert run_power_meter.main(["--sim", "--with-sdg", "--ini", str(tmp_path / "x.ini"), "--no-connect"]) == 0
    assert [type(w).__name__ for w in made] == ["PowerMeterWindow", "PulseGenWindow"]
    for w in made:
        w.proxy.shutdown()
