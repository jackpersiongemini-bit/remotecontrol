"""Окно анализатора спектра на симуляторе E4446A: настройки и пресеты в памяти прибора."""
import json

import pytest
from PyQt6.QtCore import QSettings, Qt

from conftest import attach, sim_of
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport
from visalab.drivers import DRIVERS
from visalab.front import SpectrumWindow


def _proxy():
    drv, sim_cls = DRIVERS["e4446a"]
    return attach(InstrumentProxy(drv, SimTransport(sim_cls())))


def _open(qtbot, tmp_path, proxy=None, name="sa.ini"):
    settings = QSettings(str(tmp_path / name), QSettings.Format.IniFormat)
    proxy = proxy or _proxy()
    win = SpectrumWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    return proxy, win, settings


@pytest.fixture
def sa(qtbot, tmp_path):
    proxy, win, settings = _open(qtbot, tmp_path)
    yield proxy, win, settings
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def test_layout_settings_only(qtbot, sa):
    proxy, win, _s = sa
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["FREQ", "BW", "AMPL", "SWP", "TRAC", "TRIG"]
    assert len(win.preset_panel.table.lines) == 1 and win.preset_panel.table.lines[0].name == "PRST"
    names = {ln.name: [w.key for w in ln.widgets if hasattr(w, "key")] for p in win.pages for ln in p.lines}
    assert names["RBW"] == ["bw.res", "bw.res_auto"] and names["CENT"] == ["freq.center"]
    assert not any(k == "trace" or k.startswith("marker") for ks in names.values() for k in ks)   # вывода информации нет


def test_edit_settings_and_auto_pair(qtbot, sa):
    proxy, win, _s = sa
    win.widgets["freq.center"].set_value(2.4e9)
    win.widgets["freq.span"].set_value(100e6)
    _wait(qtbot, proxy, "freq.center", lambda v: v == pytest.approx(2.4e9))
    _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(100e6))
    win.widgets["bw.res"].set_value(100e3)                 # ручной RBW выключает авто
    _wait(qtbot, proxy, "bw.res_auto", lambda v: v is False)
    win.widgets["bw.res_auto"].set_value(True)
    _wait(qtbot, proxy, "bw.res_auto", lambda v: v is True)


def test_presets_live_in_analyzer_memory(qtbot, tmp_path):
    proxy, win, settings = _open(qtbot, tmp_path)
    try:
        sim = sim_of(proxy)
        assert win.presets.data == []
        proxy.set("freq.center", 2.4e9)
        proxy.set("freq.span", 100e6)
        _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(100e6))
        win.add_button.click()
        qtbot.waitUntil(lambda: win.presets.names() == ["2G4S100M"], timeout=5000)
        assert set(sim.files) == {"2G4S100M.STA"} and win.presets.checked_index() == 0
        proxy.set("freq.center", 1e9)
        proxy.set("freq.span", 10e6)
        _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(10e6))
        win.add_button.click()
        qtbot.waitUntil(lambda: len(win.presets.data) == 2, timeout=5000)
        assert win.presets.selected_name() == "1GS10M"
        # чип вызывает состояние из прибора
        qtbot.mouseClick(win.presets.buttons()[0], Qt.MouseButton.LeftButton)
        _wait(qtbot, proxy, "freq.center", lambda v: v == pytest.approx(2.4e9))
        _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(100e6))
        # в ini - только порядок (последний использованный первым)
        assert json.loads(settings.value(f"{win._prefix}/SA_PRESETS")) == [["2G4S100M"], ["1GS10M"]]
        files = dict(sim.files)
    finally:
        proxy.shutdown()

    # новый сеанс: пресеты читаются из прибора, порядок - из ini; лишние имена из ini исчезают
    settings.setValue(f"{win._prefix}/SA_PRESETS", json.dumps([["1GS10M"], ["GONE"], ["2G4S100M"]]))
    proxy2 = _proxy()
    proxy2_sim_files = files
    win2 = SpectrumWindow(proxy2, settings)
    qtbot.addWidget(win2)
    assert win2.presets.names() == ["1GS10M", "GONE", "2G4S100M"]      # до подключения - как в ini
    with qtbot.waitSignal(proxy2.sync_done, timeout=5000):
        proxy2.open()
    sim_of(proxy2).files.update(proxy2_sim_files)                 # «прибор» с теми же файлами
    proxy2.action("state_list")
    try:
        qtbot.waitUntil(lambda: win2.presets.names() == ["1GS10M", "2G4S100M"], timeout=5000)
        win2.presets.set_checked(1)
        win2.remove_button.click()                                              # удаление - из памяти прибора
        qtbot.waitUntil(lambda: win2.presets.names() == ["1GS10M"], timeout=5000)
        assert set(sim_of(proxy2).files) == {"1GS10M.STA"}
    finally:
        proxy2.shutdown()


def test_sa_launcher(qtbot, tmp_path, monkeypatch):
    import run_spectrum
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    orig = run_spectrum.SpectrumWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(run_spectrum.SpectrumWindow, "__init__", track)
    assert run_spectrum.main(["--sim", "--ini", str(tmp_path / "x.ini"), "--no-connect"]) == 0
    assert len(made) == 1
    made[0].proxy.shutdown()


def test_zero_span_button_and_trigger_tabs(qtbot, sa):
    proxy, win, settings = sa
    names = {ln.name: [w.key for w in ln.widgets if hasattr(w, "key")] for p in win.pages for ln in p.lines}
    assert names["MODE"][:1] == ["trace.mode"] and names["DLY"] == ["trig.delay", "trig.delay_state"]
    assert names["SRC"] == ["trig.source"] and names["TYPE"] == ["sweep.type"] and names["PRE"] == ["preamp"]
    win.widgets["freq.center"].set_value(1e9)
    win.widgets["freq.span"].set_value(50e6)
    _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(50e6))
    zero = next(w for p in win.pages for ln in p.lines for w in ln.widgets if type(w).__name__ == "ZeroSpan")
    assert not zero.indicated()
    zero.click()
    _wait(qtbot, proxy, "freq.span", lambda v: v == 0)
    qtbot.waitUntil(zero.indicated, timeout=5000)
    proxy.set("sweep.time", 2e-3)
    qtbot.waitUntil(lambda: proxy.value("trace") is not None and proxy.value("trace").x_unit == "s", timeout=5000)
    zero.click()                                                     # возврат к прежнему обзору
    _wait(qtbot, proxy, "freq.span", lambda v: v == pytest.approx(50e6))
    qtbot.waitUntil(lambda: not zero.indicated(), timeout=5000)
    assert float(settings.value(f"{win._prefix}/LAST_SPAN")) == pytest.approx(50e6)
    win.widgets["trig.source"].set_value("VIDEO")
    win.widgets["trace.mode"].set_value("MAXHOLD")
    _wait(qtbot, proxy, "trig.source", lambda v: v == "VIDEO")
    _wait(qtbot, proxy, "trace.mode", lambda v: v == "MAXHOLD")
