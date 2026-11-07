"""Новый фронтенд (visalab.ui): окна без бэкенда, на DemoModel."""
from PyQt6.QtCore import QSettings

from visalab.ui import InstrumentWindow, MacroWindow, ModeSwitch
from visalab.ui.demo import PSU, demo_model
from visalab.ui.editors import NumberEditor, ToggleEditor


def _win(qtbot, name="psu", **kw):
    model = demo_model(name)
    win = InstrumentWindow(model, **kw)
    qtbot.addWidget(win)
    model.connect_requested.emit(True)
    return model, win


def test_compact_and_full_modes(qtbot):
    model, win = _win(qtbot)
    win.show()
    assert win.mode == ModeSwitch.FULL and win.settings_panel.isVisible()
    win.set_mode(ModeSwitch.COMPACT)
    assert win.mode == ModeSwitch.COMPACT and not win.settings_panel.isVisible()
    assert win.table.isVisible() and win.action_bar.isVisible()
    assert win.table.keys == list(PSU.default_selected)


def test_pin_adds_field_to_table(qtbot):
    model, win = _win(qtbot)
    win.settings_panel.pins["ch3.voltage"].click()
    assert win.table.keys[-1] == "ch3.voltage"
    win.settings_panel.pins["ch1.voltage"].click()
    assert "ch1.voltage" not in win.table.keys
    model.move("ch3.voltage", -10)
    assert win.table.keys[0] == "ch3.voltage"


def test_edit_goes_through_model_only(qtbot):
    model, win = _win(qtbot)
    requests = []
    model.edit_requested.connect(lambda k, v: requests.append((k, v)))
    ed = next(w for w in win.table.findChildren(NumberEditor) if w.spec.key == "ch1.voltage")
    ed.setText("12.5")
    ed.setModified(True)
    ed.returnPressed.emit()
    assert requests == [("ch1.voltage", 12.5)]
    # тот же ключ в полном экране получил значение
    full = next(w for w in win.settings_panel.findChildren(NumberEditor) if w.spec.key == "ch1.voltage")
    assert full.text() == "12.5 V"
    ed.setText("99")              # больше максимума 30 В - не отправляется
    ed.returnPressed.emit()
    assert ed.property("invalid") is True and len(requests) == 1


def test_output_toggle_waits_for_confirmation(qtbot):
    model, win = _win(qtbot)
    model.edit_requested.disconnect()          # «прибор» молчит
    t = win.action_bar.toggles[0]
    t.click()
    assert not t.isChecked()
    model.set_value("ch1.output", True)
    assert t.isChecked() and "ON" in t.text()


def test_disconnected_disables_editors(qtbot):
    model, win = _win(qtbot)
    model.connect_requested.emit(False)
    assert not any(w.isEnabled() for w in win.findChildren(ToggleEditor))
    assert win.settings_panel.pins["ch1.voltage"].isEnabled()      # выбирать можно и без прибора


def test_selection_and_mode_are_remembered(qtbot, tmp_path):
    store = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    model, win = _win(qtbot, settings=store)
    model.set_selected(["ch2.current", "ch1.meas_v"])
    win.set_mode(ModeSwitch.COMPACT)
    model2, win2 = _win(qtbot, settings=store)
    assert model2.selected == ("ch2.current", "ch1.meas_v") and win2.mode == ModeSwitch.COMPACT


def test_all_demo_windows_render(qtbot):
    for name in ("psu", "generator", "rf"):
        _, win = _win(qtbot, name)
        win.grab()
        win.set_mode(ModeSwitch.COMPACT)
        win.grab()
    m = MacroWindow()
    qtbot.addWidget(m)
    m.grab()
