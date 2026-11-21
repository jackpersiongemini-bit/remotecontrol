"""Окно СВЧ генератора на симуляторе E8267D."""
import pytest
from PyQt6.QtCore import QSettings

from conftest import attach
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport
from visalab.drivers import DRIVERS
from visalab.front import RFGenWindow


@pytest.fixture
def rf(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "rf.ini"), QSettings.Format.IniFormat)
    drv, sim_cls = DRIVERS["e8267d"]
    proxy = attach(InstrumentProxy(drv, SimTransport(sim_cls())))
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
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["RF", "AM", "FM", "PM", "PULS", "SET"]
    assert [ln.name for ln in win.preset_panel.table.lines][:3] == ["OUT", "POW", "FREQ"]
    assert [ln.name for ln in win.pages[0].lines][:4] == ["OUT", "FREQ", "POW", "PRST"]      # чипы - на вкладке RF
    assert win.freq_spin.key == "rf.frequency"
    assert len(win.preset_panel.table.lines) == 3                # модуляция - только на вкладках


def test_power_presets_as_in_vna(qtbot, rf):
    proxy, win, settings = rf
    pl, spin = win.power_levels, win.power_spin
    assert [b.text() for b in pl.chips.buttons()] == ["-20", "-10", "-5", "0"]
    pl.chips.buttons()[1].click()                                  # чип -10 -> мощность
    _wait(qtbot, proxy, "rf.power", lambda v: v == pytest.approx(-10.0))
    qtbot.waitUntil(lambda: pl.chips.checked_index() == 1)
    assert pl.remove_selected() and pl.levels == spin.levels == [-20.0, -5.0, 0.0]     # «−» - выбранный
    proxy.set("rf.power", 3.0)
    _wait(qtbot, proxy, "rf.power", lambda v: v == pytest.approx(3.0))
    assert pl.add_current() and not pl.add_current()               # «+» - текущая, без повторов
    assert spin.levels == [-20.0, -5.0, 0.0, 3.0] and pl.chips.checked_index() == 3
    assert settings.value(f"{win._prefix}/POWER_LIST") == "-20, -5, 0, 3"
    win.prst_button.setChecked(True)                               # PRST в избранном: шаг по списку
    assert settings.value(f"{win._prefix}/POWER_PRESET_MODE", type=bool) is True
    spin.stepBy(-1)
    _wait(qtbot, proxy, "rf.power", lambda v: v == pytest.approx(0.0))
    spin.stepBy(-2)
    _wait(qtbot, proxy, "rf.power", lambda v: v == pytest.approx(-20.0))
    win.prst_button.setChecked(False)
    spin.stepBy(1)                                                 # обычный режим: разряд под курсором
    _wait(qtbot, proxy, "rf.power", lambda v: v > -20.0 and v not in (-5.0, 0.0, 3.0))
    assert win.presets.data == [] and not hasattr(win, "freq_hold")      # пресетов FREQ/POW/MOD и замка нет
    win.show()
    win.set_expanded(True)
    win.set_page(0)
    qtbot.wait(50)
    line = win._pwr_tools.parentWidget()                           # «+»/«−» у правого края
    assert line.width() - win._pwr_tools.geometry().right() <= 2 and win._pwr_tools.width() <= 40


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


def test_spinbox_auto_step_and_step_setting(qtbot, rf):
    """AUTO: последняя показанная цифра, пока крутят - тот же разряд; правый клик - постоянный шаг (ini)."""
    proxy, win, settings = rf
    win.show()
    qtbot.waitExposed(win)
    proxy.set("rf.frequency", 1.28e9)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.28e9))
    spin = win.freq_spin
    qtbot.waitUntil(lambda: spin.lineEdit().text() == "1.28" and spin.prefix == "G")
    assert not spin.hasFocus() and spin.step_index() == 3        # подчёркнута «8»: шаг 0.01 GHz
    spin.stepBy(1)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.29e9))
    spin.stepBy(1)                                                # 1.29 + 0.01 = «1.30», а не шаг 0.1
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.30e9))
    qtbot.waitUntil(lambda: spin.lineEdit().text() == "1.30")
    spin.stepBy(1)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.31e9))
    spin._release_step()                                          # мышь ушла: снова по последней цифре
    assert spin.step_setting is None
    assert spin.step_choices()[:3] == [1e3, 1e4, 1e5] and spin.step_label(1e6) == "1 MHz"
    spin.set_step(1e6)                                            # меню: постоянный шаг 1 MHz
    assert settings.value(f"{win._prefix}/STEP/RF.FREQUENCY", type=float) == 1e6
    qtbot.waitUntil(lambda: spin.lineEdit().text() == "1.310")   # знаки - до шага
    assert spin.step_index() == 4 and "STEP 1 MHz" in spin.toolTip()
    spin.stepBy(-2)
    _wait(qtbot, proxy, "rf.frequency", lambda v: v == pytest.approx(1.308e9))
    from visalab.front import RFGenWindow
    win2 = RFGenWindow(proxy, settings)                           # из ini
    qtbot.addWidget(win2)
    assert win2.freq_spin.step_setting == 1e6
    win2.freq_spin.set_step(None)                                 # AUTO - ключ убран
    assert settings.value(f"{win2._prefix}/STEP/RF.FREQUENCY") is None
