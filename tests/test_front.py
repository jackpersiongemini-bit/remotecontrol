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
    assert ch1["output"].text() == "CH1 OFF"
    ch1["output"].click()
    _wait(qtbot, proxy, "ch1.output", lambda v: v is True)
    qtbot.waitUntil(lambda: ch1["output"].text() == "CH1 ON")
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
    # на экране порядок не меняется, в ini - по последнему использованию
    from visalab.front.pulse_gen import PresetGroup
    screen = list(win.presets.data)
    win._apply_preset(0)
    assert win.presets.data == screen
    assert win.presets.saved_order()[:2] == [screen[0], (2e-3, 50e-6, "PULSE")]
    restarted = PresetGroup(win.presets.settings, win.presets.key)
    assert restarted.data[:2] == [screen[0], (2e-3, 50e-6, "PULSE")]
    idx = win.presets.find(2e-3, 50e-6)
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


def test_collapsible_table_and_short_address(qtbot, opened, settings):
    from visalab.front.window import short_address
    sim, proxy, win = opened
    assert not win.parameter_panel.isVisibleTo(win)          # обычный вид: только выходы и пресеты
    assert win.channels[0]["output"].isVisibleTo(win)
    small = win.height()
    win.set_expanded(True)
    assert win.parameter_panel.isVisibleTo(win) and win.height() > small
    assert settings.value(f"{win._prefix}/EXPANDED", type=bool) is True
    assert short_address("TCPIP0::192.168.0.5::INSTR") == "192.168.0.5"
    assert short_address("USB0::0xF4EC::0x1102::SDG2XAAQ::INSTR") == "SDG2XAAQ"


def test_restore_order_load_before_levels(qtbot, opened, settings):
    sim, proxy, win = opened
    sent = []
    orig = proxy.set
    proxy.set = lambda k, v: (sent.append(k), orig(k, v))
    pre = win._prefix
    settings.setValue(f"{pre}/CH1.HIGH", 3.0)
    settings.setValue(f"{pre}/CH1.LOAD", "50" if proxy.value("ch1.load") != "50" else "HIZ")
    settings.setValue(f"{pre}/CH1.OUTPUT", True)
    win.restore()
    assert sent.index("ch1.load") < sent.index("ch1.high") < sent.index("ch1.output")


def test_presets_switch_with_arrow_keys(qtbot, opened):
    from PyQt6.QtCore import Qt
    sim, proxy, win = opened
    win.show()
    for i in range(12):
        win.presets.add(1e-3 * (i + 1), 1e-6)       # первый - последний добавленный (12 ms)
    order = list(win.presets.data)
    assert not win.preset_scroll.horizontalScrollBar().isVisible()
    win.presets.set_checked(0)
    qtbot.keyClick(win, Qt.Key.Key_Right)
    assert win.presets.checked_index() == 1 and win.presets.data == order    # без перестановки
    _wait(qtbot, proxy, "ch1.period", lambda v: v == pytest.approx(order[1][0]))
    qtbot.keyClick(win, Qt.Key.Key_Left)
    assert win.presets.checked_index() == 0
    _wait(qtbot, proxy, "ch2.period", lambda v: v == pytest.approx(order[0][0]))
    assert win.add_button.height() + win.remove_button.height() + 4 == win.preset_scroll.height()


def test_wave_type_menu_and_preset_wave(qtbot, opened):
    from PyQt6.QtWidgets import QMenu
    sim, proxy, win = opened
    wave = win.channels[0]["wave"]
    wave.click()
    menu = wave.findChild(QMenu)
    assert menu is not None and "SQUARE" in [a.text() for a in menu.actions()]
    next(a for a in menu.actions() if a.text() == "SQUARE").trigger()
    _wait(qtbot, proxy, "ch1.wave", lambda v: v == "SQUARE")
    menu.close()
    win.presets.add(1e-3, 10e-6, "PULSE")
    win._apply_preset(win.presets.find(1e-3, 10e-6, "PULSE"))
    _wait(qtbot, proxy, "ch1.wave", lambda v: v == "PULSE")
    _wait(qtbot, proxy, "ch1.width", lambda v: v == pytest.approx(10e-6))
    assert win.presets.buttons()[win.presets.find(1e-3, 10e-6)].text().startswith("PULSE\n")


def test_unit_dropdown_value_left(qtbot, opened):
    from PyQt6.QtCore import Qt
    sim, proxy, win = opened
    per = win.channels[0]["period"]
    per.set_value(2e-3)
    _wait(qtbot, proxy, "ch1.period", lambda v: v == pytest.approx(2e-3))
    qtbot.waitUntil(lambda: per.text() == "2")
    assert per.unit_button.text() == "ms" and per.alignment() & Qt.AlignmentFlag.AlignLeft
    per.set_prefix("µ")                                   # показ в µs, значение то же
    assert per.text() == "2000" and per.unit_button.text() == "µs"
    per.setText("1500")                                   # ввод - в выбранной единице
    per.setModified(True)
    per.editingFinished.emit()
    _wait(qtbot, proxy, "ch1.period", lambda v: v == pytest.approx(1.5e-3))


def test_mode_tabs_and_switch(qtbot, opened):
    from visalab.front.theme import ON
    sim, proxy, win = opened
    assert [b.text() for b in win.tabs.buttons()] == ["MAIN", "MOD", "SWP", "BRST"]
    win.set_expanded(True)
    win.set_page(3)
    assert win.pages[3].isVisibleTo(win) and not win.pages[0].isVisibleTo(win)
    assert not win.tabs.buttons()[3].property("on")
    win.channels[0]["burst_state"].click()
    _wait(qtbot, proxy, "ch1.burst_state", lambda v: v is True)
    qtbot.waitUntil(lambda: win.tabs.buttons()[3].property("on") is True)
    assert win.on_button.status() == ON and win.on_button.isChecked()
