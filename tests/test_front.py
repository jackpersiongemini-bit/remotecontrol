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
    assert not any(pg.isVisibleTo(win) for pg in win.pages)   # обычный вид: таблиц нет, вкладки - индикаторы
    assert win.tabs.isVisibleTo(win) and not win.tabs.show_selection
    assert win.channels[0]["output"].isVisibleTo(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(50)
    small = win.height()
    win.set_expanded(True)
    assert win.pages[win.tabs.currentIndex()].isVisibleTo(win)
    qtbot.waitUntil(lambda: win.height() > small)
    win.set_expanded(False)
    win._on_tab_clicked(2)                                   # клик по вкладке в свёрнутом - развернуть на ней
    assert win._expanded and win.pages[2].isVisibleTo(win)
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
    assert per.unit_button.text() == "ms" and per.alignment() & Qt.AlignmentFlag.AlignRight
    assert per.unit_button.x() < per.width() // 2
    per.set_prefix("µ")                                   # показ в µs, значение то же
    assert per.text() == "2000" and per.unit_button.text() == "µs"
    per.setText("1500")                                   # ввод - в выбранной единице
    per.setModified(True)
    per.editingFinished.emit()
    _wait(qtbot, proxy, "ch1.period", lambda v: v == pytest.approx(1.5e-3))


def test_mode_tabs_and_switch(qtbot, opened):
    from visalab.front.theme import ON
    sim, proxy, win = opened
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["MAIN", "MOD", "SWP", "BRST"]
    win.set_expanded(True)
    win.set_page(3)
    assert win.pages[3].isVisibleTo(win) and not win.pages[0].isVisibleTo(win)
    assert not win.tabs.is_on(3)
    win.channels[0]["burst_state"].click()
    _wait(qtbot, proxy, "ch1.burst_state", lambda v: v is True)
    qtbot.waitUntil(lambda: win.tabs.is_on(3))
    win.tabs.setCurrentIndex(1)                            # клик по вкладке
    assert win.pages[1].isVisibleTo(win)
    assert win.on_button.status() == ON and win.on_button.isChecked()


def test_clickable_widgets_have_pointer_cursor(qtbot, opened):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QAbstractButton, QTabBar
    _sim, _proxy, win = opened
    win.show()
    assert not hasattr(win, "status_indicator")          # ход записи - цветом рамки поля
    bad = [c for c in win.findChildren((QAbstractButton, QTabBar))
           if c.isVisibleTo(win) and c.isEnabled() and c.property("static") is not True
           and c.cursor().shape() != Qt.CursorShape.PointingHandCursor]
    assert not bad, [(type(c).__name__, c.objectName()) for c in bad]


def test_line_edit_placeholder_editing(qtbot, opened):
    from PyQt6.QtCore import Qt
    _sim, proxy, win = opened
    win.show()
    qtbot.waitExposed(win)
    field = win.channels[0]["high"]
    win.set_expanded(True)
    shown = field.text()
    assert shown
    field.setFocus()
    qtbot.waitUntil(field.hasFocus)
    assert field.text() == "" and field.placeholderText() == shown     # значение - в подсказке
    qtbot.keyClick(field, Qt.Key.Key_Escape)                           # отмена
    assert not field.hasFocus() and field.text() == shown and field.placeholderText() == ""
    field.setFocus()
    qtbot.waitUntil(field.hasFocus)
    qtbot.keyClicks(field, "1.25")
    qtbot.keyClick(field, Qt.Key.Key_Return)                           # запись и выход из поля
    _wait(qtbot, proxy, "ch1.high", lambda v: v == pytest.approx(1.25))
    qtbot.waitUntil(lambda: field.text() == "1.25" and not field.hasFocus())
    field.setFocus()
    qtbot.waitUntil(field.hasFocus)
    field.clearFocus()                                                 # пусто - значение не меняется
    assert field.text() == "1.25"
    assert proxy.value("ch1.high") == pytest.approx(1.25)


def test_error_log_opens_from_indicator(qtbot, opened):
    _sim, proxy, win = opened
    win.show()
    qtbot.waitExposed(win)
    assert not win._expanded
    proxy.set("ch1.high", "abc")                             # неверное значение -> ошибка записи
    qtbot.waitUntil(lambda: "CH1 HIGH" in win.error_log.toPlainText())
    win.error_indicator.clicked.emit()
    assert win._expanded and win.error_page.isVisible()
    assert not any(p.isVisible() for p in win.pages)
    win._on_tab_clicked(1)                                   # вкладка - выход из журнала
    assert not win.error_page.isVisible() and win.pages[1].isVisible()
    assert "CH1 HIGH" in win.error_log.toPlainText()         # журнал сохраняется
    win.show_errors()
    win.clear_button.click()                                 # CLEAR - очистить и выйти
    assert win.error_log.toPlainText() == ""
    assert not win.error_page.isVisible() and win.pages[1].isVisible()
    from visalab.front.window import WriteState
    assert win.error_indicator.state is not WriteState.ERROR


def test_connection_page_from_address(qtbot, opened, settings):
    _sim, _proxy, win = opened
    win.show()
    qtbot.waitExposed(win)
    win.address.clicked.emit()
    assert win._expanded and win.connection_page.isVisible()
    assert not win.error_page.isVisible() and not any(p.isVisible() for p in win.pages)
    assert win.driver_choice.value == "sdg"
    assert "SDG2042X" in win.model_choice._data
    win._on_tab_clicked(0)                                   # вкладка - выход без изменений
    assert not win.connection_page.isVisible() and win.pages[0].isVisible()
    win.show_connection()
    win.model_choice.set_value("SDG2042X")
    win.address_edit.setText("192.168.0.5")
    with qtbot.waitSignal(win.reconnect_requested) as sig:
        win.apply_button.click()
    assert sig.args == ["sdg", "192.168.0.5", "SDG2042X"]
    assert not win.connection_page.isVisible()
    from visalab.front.pulse_gen import saved_connection
    assert saved_connection(settings) == {"driver": "sdg", "address": "192.168.0.5", "model": "SDG2042X"}


def test_launcher_reconnect_builds_new_window(qtbot, tmp_path, monkeypatch):
    import run_pulse_gen
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    windows = []
    orig = run_pulse_gen.PulseGenWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        windows.append(self)
    monkeypatch.setattr(run_pulse_gen.PulseGenWindow, "__init__", track)
    ini = str(tmp_path / "l.ini")
    assert run_pulse_gen.main(["--sim", "--ini", ini, "--no-connect", "--lab", str(tmp_path / "none.json")]) == 0
    assert len(windows) == 1
    windows[0].reconnect_requested.emit("sdg", "sim", "SDG2042X")
    assert len(windows) == 2 and windows[1].isVisible() and not windows[0].isVisible()
    windows[1].proxy.shutdown()


def test_create_model_window(qtbot, opened, settings):
    from visalab.drivers.siglent_sdg import SDGModel
    from visalab.front.models import all_models, user_models
    _sim, _proxy, win = opened
    win.show_connection()
    win.model_choice.set_value("SDG2042X")
    win.new_model_button.click()
    mw = win.model_window
    qtbot.addWidget(mw)
    labels = [f.label for f in mw.fields]
    assert labels == ["NAME", "CH", "FMAX", "AMAX", "CMR"]
    assert mw.fields[0].widget.text() == "SDG2042X-custom"
    mw.fields[0].widget.setText("SDG-LAB")
    mw.fields[2].widget.setText("25 MHz")                   # с приставкой
    mw.fields[4].widget.click()                               # CMR -> YES
    assert mw.save()
    model = user_models(settings, "sdg")["SDG-LAB"]
    assert model == SDGModel("SDG-LAB", 2, 25e6, 20.0, True)
    assert "SDG-LAB" in all_models(settings, "sdg")
    assert win.model_choice.value == "SDG-LAB"                # сразу выбрана в MDL
    mw2 = __import__("visalab.front.models", fromlist=["ModelWindow"]).ModelWindow(settings, "sdg")
    qtbot.addWidget(mw2)
    mw2.fields[0].widget.setText("SDG2042X")                  # встроенное имя не перезаписать
    assert not mw2.save() and "built-in" in mw2.status.text()
