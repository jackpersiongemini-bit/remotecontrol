"""Окно СВЧ генератора на симуляторе E8267D."""
import pytest
from PyQt6.QtCore import QSettings

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import DRIVERS
from visalab.front import RFGenWindow


@pytest.fixture
def rf(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "rf.ini"), QSettings.Format.IniFormat)
    drv, sim_cls = DRIVERS["e8267d"]
    proxy = InstrumentProxy(drv, SimTransport(sim_cls()))
    win = RFGenWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    yield proxy, win, settings
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def test_layout_tabs_and_favourites(qtbot, rf):
    _proxy, win, _s = rf
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["RF", "AM", "FM", "PM", "PULS"]
    assert [ln.name for ln in win.preset_panel.table.lines][:3] == ["OUT", "PRST", "FREQ"]
    assert win.freq_spin.key == "rf.frequency"
    assert set(win.mod_spins) == {"PULSE", "AM", "FM", "PM"}


def test_preset_add_and_apply(qtbot, rf):
    proxy, win, _s = rf
    proxy.set("rf.frequency", 2.45e9)
    proxy.set("rf.power", -5.0)
    _wait(qtbot, proxy, "rf.power", lambda v: v == pytest.approx(-5.0))
    win._add_preset()
    assert win.presets.data[win.presets.checked_index()][:2] == (pytest.approx(2.45e9), pytest.approx(-5.0))
    win.presets.add(10e9, 0.0, "AM")
    win._apply_preset(win.presets.find(10e9, 0.0, "AM"))
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(10e9))
    _wait(qtbot, proxy, "am.state", lambda v: v is True)
    _wait(qtbot, proxy, "mod.output", lambda v: v is True)
    assert win.mod_line.name == "AM" and win.mod_spins["AM"].isVisibleTo(win)
    assert win.current_mod() == "AM"
    qtbot.waitUntil(lambda: win.tabs.is_on(1))                  # вкладка AM - индикатор
    win.presets.add(5e9, -20.0, "PULSE")
    win._apply_preset(win.presets.find(5e9, -20.0, "PULSE"))
    _wait(qtbot, proxy, "pulse.state", lambda v: v is True)
    _wait(qtbot, proxy, "am.state", lambda v: v is False)
    assert win.mod_line.name == "PULS"
    win.presets.add(1e9, -10.0, "CW")
    win._apply_preset(win.presets.find(1e9, -10.0, "CW"))
    _wait(qtbot, proxy, "pulse.state", lambda v: v is False)
    _wait(qtbot, proxy, "mod.output", lambda v: v is False)
    assert win.current_mod() == "CW"


def test_spinbox_steps_digit_under_cursor(qtbot, rf):
    proxy, win, _s = rf
    win.show()
    qtbot.waitExposed(win)
    proxy.set("rf.frequency", 2.45e9)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(2.45e9))
    spin = win.freq_spin
    qtbot.waitUntil(lambda: spin.lineEdit().text() == "2.45" and spin.prefix == "G")
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus)
    spin.lineEdit().setCursorPosition(3)                          # после «4» -> шаг 0.1 GHz
    spin.stepBy(1)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(2.55e9))
    spin.lineEdit().setCursorPosition(1)                          # после «2» -> шаг 1 GHz
    spin.stepBy(-1)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.55e9))


def test_restore_order_rf_last(qtbot, rf):
    _proxy, win, _s = rf
    order = [w.key for w in sorted(win.saved_widgets, key=lambda w: win.restore_rank(w.key))]
    assert order[0] == "rf.frequency" and order[-1] == "rf.output"
    assert order.index("mod.output") > order.index("am.state")


def test_rf_launcher(qtbot, tmp_path, monkeypatch):
    import run_rf_gen
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    orig = run_rf_gen.RFGenWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(run_rf_gen.RFGenWindow, "__init__", track)
    assert run_rf_gen.main(["--sim", "--ini", str(tmp_path / "x.ini"), "--no-connect"]) == 0
    made[0].reconnect_requested.emit("smf100a", "sim", "")
    assert len(made) == 2 and made[1].driver_choice.value == "smf100a"
    made[1].proxy.shutdown()
