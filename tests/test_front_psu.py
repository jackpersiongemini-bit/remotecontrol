"""Окно источников питания на симуляторе IT6300."""
import pytest
from PyQt6.QtCore import QPoint, QSettings, Qt

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import DRIVERS
from visalab.front import PSUWindow
from visalab.front.psu import SourceGrid


def _proxy():
    drv, sim_cls = DRIVERS["it6300"]
    return InstrumentProxy(drv, SimTransport(sim_cls()))


@pytest.fixture
def psu(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "psu.ini"), QSettings.Format.IniFormat)
    proxy = _proxy()
    win = PSUWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    yield proxy, win, settings
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def _click_tile(qtbot, tile):
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(tile.width() // 2, 4))


def test_grid_rows(qtbot):
    proxies = [_proxy(), _proxy()]
    try:
        chans = [(p, n, f"PSU{i + 1} CH{n}") for i, p in enumerate(proxies) for n in (1, 2, 3)][:5]
        grid = SourceGrid(chans)                       # 5 каналов с двух приборов: 3 + 2
        qtbot.addWidget(grid)
        assert [r.count() for r in grid.rows] == [3, 2]
        grid.resize(600, 200)
        grid.show()
        qtbot.waitExposed(grid)
        a, b = grid.tiles[0], grid.tiles[3]
        assert b.width() > a.width() * 1.3             # неполный ряд растянут
        assert grid.tiles[3].name.text() == "PSU2 CH1"
    finally:
        for p in proxies:
            p.shutdown()


def test_field_shows_measurement_and_setpoint(qtbot, psu):
    proxy, win, _s = psu
    proxy.set("ch2.voltage", 5.0)
    proxy.set("ch2.current", 1.0)
    proxy.set("ch2.output", True)
    _wait(qtbot, proxy, "ch2.meas_v", lambda v: v == pytest.approx(5.0, abs=1e-3))
    f = win.tiles[2].fields["V"]
    qtbot.waitUntil(lambda: f.text() == "5", timeout=5000)        # покой: измерение в поле
    assert f.side.text() == "5"                                  # мелко снизу слева - уставка
    proxy.set("ch2.output", False)
    qtbot.waitUntil(lambda: f.text() == "0", timeout=5000)        # выход выключен: измерено 0
    assert f.side.text().startswith("5")
    win.show()
    qtbot.waitExposed(win)
    assert not f.hasFocus()                                       # фокус при показе не захватывается
    f.setFocus()
    qtbot.waitUntil(f.hasFocus, timeout=2000)
    assert f.text() == "" and f.placeholderText() == "5" and f.side.text().startswith("0")
    qtbot.keyClicks(f, "7.5")
    qtbot.keyClick(f, Qt.Key.Key_Return)
    _wait(qtbot, proxy, "ch2.voltage", lambda v: v == pytest.approx(7.5))


def test_tile_click_toggles_output(qtbot, psu):
    proxy, win, _s = psu
    win.show()
    qtbot.waitExposed(win)
    t = win.tiles[3]
    _wait(qtbot, proxy, "ch3.output", lambda v: v is False)
    _click_tile(qtbot, t)
    _wait(qtbot, proxy, "ch3.output", lambda v: v is True)
    qtbot.waitUntil(lambda: t.is_on() and t.mode.text() == "CV", timeout=5000)
    qtbot.mouseClick(t.fields["V"], Qt.MouseButton.LeftButton)   # клик по полю - не переключает
    assert proxy.value("ch3.output") is True
    _click_tile(qtbot, t)
    _wait(qtbot, proxy, "ch3.output", lambda v: v is False)
    qtbot.waitUntil(lambda: t.mode.text() == "OFF", timeout=5000)


def test_default_rule_blocks_ch1(qtbot, psu):
    proxy, win, settings = psu
    t1 = win.tiles[1]
    assert len(win.rules_table.rows) == 1 and t1.locked and "CH2" in t1.lock_reason
    assert not t1.toggle()
    for n in (2, 3):
        proxy.set(f"ch{n}.voltage", 2.0)                          # нагрузка 10 Ом -> 0.2 А
        proxy.set(f"ch{n}.output", True)
    qtbot.waitUntil(lambda: not t1.locked, timeout=5000)
    assert t1.toggle()
    _wait(qtbot, proxy, "ch1.output", lambda v: v is True)
    proxy.set("ch3.output", False)                                # THEN CH1 OFF AND BLOCK
    _wait(qtbot, proxy, "ch1.output", lambda v: v is False)
    qtbot.waitUntil(lambda: t1.locked, timeout=5000)
    assert not t1.toggle()


def test_rules_edit_save_and_errors(qtbot, psu):
    proxy, win, settings = psu
    table = win.rules_table
    row = table.rows[0]
    row.toggle.set_value(False)                                   # правило выключено - блокировки нет
    qtbot.waitUntil(lambda: not win.tiles[1].locked, timeout=5000)
    bad = table.add_rule("IF CH9 ON THEN CH1 OFF", True)
    bad.commit()
    assert bad.rule is None and "CH9" in bad.error and bad.edit.property("wstate") == "error"
    bad.edit.setText("IF CH3 ON THEN CH2 BLOCK ELSE CH2 UNBLOCK")
    bad.edit.setModified(True)
    bad.commit()
    assert bad.rule is not None and not bad.error
    qtbot.waitUntil(lambda: win.tiles[2].locked is False, timeout=5000)
    proxy.set("ch3.output", True)
    qtbot.waitUntil(lambda: win.tiles[2].locked, timeout=5000)
    assert "CH3 ON" in win.tiles[2].lock_reason
    import json
    saved = json.loads(settings.value(f"{win._prefix}/RULES"))
    assert [r["on"] for r in saved] == [False, True] and saved[1]["text"].startswith("IF CH3 ON")
    table.remove_rule(bad)                                        # удалили - блокировка снята
    qtbot.waitUntil(lambda: not win.tiles[2].locked, timeout=5000)
    assert len(json.loads(settings.value(f"{win._prefix}/RULES"))) == 1


def test_off_all_and_tabs(qtbot, psu):
    proxy, win, _s = psu
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["PROT", "OUT", "RULES"]
    for n in (2, 3):
        proxy.set(f"ch{n}.output", True)
    _wait(qtbot, proxy, "ch3.output", lambda v: v is True)
    win.all_off.click()
    for n in (1, 2, 3):
        _wait(qtbot, proxy, f"ch{n}.output", lambda v: v is False)


def test_psu_launcher(qtbot, tmp_path, monkeypatch):
    import run_psu
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    orig = run_psu.PSUWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(run_psu.PSUWindow, "__init__", track)
    assert run_psu.main(["--sim", "--ini", str(tmp_path / "x.ini"), "--no-connect"]) == 0
    assert len(made) == 1 and len(made[0].grid.tiles) == 3
    made[0].proxy.shutdown()
