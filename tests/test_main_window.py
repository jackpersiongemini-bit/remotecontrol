"""Главное окно и координатор «кто что измеряет» на симуляторах."""
import math

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QKeySequence

from conftest import sim_of, worker
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks, Thresholds
from visalab.coord.activity import (Source, magnitude_db, match_source, short_name, slot_magnitude_db,
                                    spectrum_peak, vna_signal)
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport
from visalab.core.trace import Trace
from visalab.drivers import DRIVERS
from visalab.front.live_plot import nice_range
from visalab.front.main_window import MainWindow
from visalab.sim.bench import SimBench


# ------------------------------------------------------------------ расчёты
def test_spectrum_peak_against_noise_floor():
    y = [-90.0] * 50 + [-20.0] + [-90.0] * 50
    present, x, peak, floor = spectrum_peak(Trace.linear(0.9e9, 1.1e9, y), 10.0)
    assert present and x == pytest.approx(1e9) and peak == -20 and floor == -90
    assert not spectrum_peak(Trace.linear(0, 1, [-90.0, -85.0, -90.0]), 10.0)[0]


def test_match_source_nearest_within_tolerance():
    a, b = Source(None, "GEN", 1e9, -10.0), Source(None, "GEN2", 2e9, 0.0)
    assert match_source(1.00005e9, [a, b], 100e3) is a
    assert match_source(1.5e9, [a, b], 100e3) is None
    assert match_source(None, [a], 1e9) is None


def test_vna_signal_by_level():
    assert vna_signal("S21", [-80.0, -1.0, -80.0], -40, -3)          # фильтр: передача есть
    assert not vna_signal("S21", [-100.0] * 3, -40, -3)              # порты открыты
    assert vna_signal("S11", [-0.1, -25.0, -0.1], -40, -3)           # порт нагружен
    assert not vna_signal("S11", [-0.01] * 3, -40, -3)               # полное отражение
    cplx = Trace.linear(1, 2, [0.5, 0.5], im=[0.0, 0.0], meta={"format": "SMITH"})
    assert slot_magnitude_db(cplx)[0] == pytest.approx(20 * math.log10(0.5))
    assert slot_magnitude_db(Trace.linear(1, 2, [10.0, 20.0], meta={"format": "PHASE"})) is None
    assert slot_magnitude_db(Trace.linear(1, 2, [1.0, 3.0], meta={"format": "SWR"}))[1] == pytest.approx(-6.02, abs=0.01)
    assert magnitude_db(cplx).y_unit == "dB"


def test_nice_range_contains_data():
    lo, hi, step = nice_range(-23.4, -11.2, 6)
    assert lo <= -23.4 and hi >= -11.2 and step in (2.0, 5.0)
    assert nice_range(5.0, 5.0, 6)[0] < 5.0 < nice_range(5.0, 5.0, 6)[1]
    assert nice_range(-120.0, -5.0, 6) == (-120.0, 0.0, 20.0)       # ваттметр: ВЧ выкл и вкл - не 50 dB/дел


def test_sim_bench_feeds_analyzer_and_meter():
    gen_cls, sa_cls, pm_cls = DRIVERS["e8267d"][1], DRIVERS["e4446a"][1], DRIVERS["u2021xa"][1]
    gen, sa, pm = gen_cls(), sa_cls(), pm_cls()
    bench = SimBench([gen], [sa], [pm], loss_db=10.0)
    bench.step()
    assert sa.signals == [] and pm.signal["on_dbm"] == -120.0
    gen.s.update({"rf.output": True, "rf.frequency": 2e9, "rf.power": 0.0})
    bench.step()
    assert sa.signals == [(2e9, -10.0)] and pm.signal["on_dbm"] == pytest.approx(-10.0) and pm.signal["width"] is None


# ------------------------------------------------------------------ монитор и окно на симуляторах
def _proxy(kind):
    """Прибор вне координатора процесса (свой реестр у каждого теста)."""
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


def _sim(proxy):
    return sim_of(proxy)


@pytest.fixture
def lab(qtbot):
    """VNA + SA + PM + GEN на симуляторах, свой реестр, стенд GEN -> SA/PM (10 dB потерь)."""
    links = ProxyLinks()
    proxies = {k: _proxy(k) for k in ("pna", "e4446a", "u2021xa", "e8267d")}
    sa_sim = _sim(proxies["e4446a"])
    sa_sim.c.update(center=1e9, span=100e6)
    sa_sim._couple()
    bench = SimBench([_sim(proxies["e8267d"])], [sa_sim], [_sim(proxies["u2021xa"])])
    bench.step()
    for p in proxies.values():
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    yield links, proxies, bench
    for p in proxies.values():
        p.shutdown()


def _measuring(mon, proxy):
    return mon.activity(proxy).measuring


def test_monitor_finds_who_measures_and_the_source(qtbot, lab):
    links, px, bench = lab
    mon = ActivityMonitor(links)
    assert [mon.name(p) for p in mon.receivers()] == ["VNA", "SA", "PM"]
    vna, sa, pm, gen = px["pna"], px["e4446a"], px["u2021xa"], px["e8267d"]
    # Приборы подключены и выдают данные -> все три отображаются даже без сигнала от генератора
    qtbot.waitUntil(lambda: _measuring(mon, vna) and _measuring(mon, sa) and _measuring(mon, pm), timeout=10000)
    assert not mon.activity(sa).signal and not mon.activity(pm).signal
    assert mon.activity(sa).source is None and mon.activity(pm).source is None
    gen.set("rf.frequency", 1e9)
    gen.set("rf.power", -5.0)
    gen.set("rf.output", True)
    qtbot.waitUntil(lambda: gen.value("rf.output") is True and gen.value("rf.power") == -5.0, timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: mon.activity(sa).signal and mon.activity(pm).signal, timeout=10000)
    a = mon.activity(sa)
    assert a.source is not None and a.source.name == "GEN" and a.freq == pytest.approx(1e9, rel=1e-3)
    assert a.path_db == pytest.approx(-10.0, abs=1.0)
    b = mon.activity(pm)
    assert b.source is not None and b.source.name == "GEN" and b.path_db == pytest.approx(-10.0, abs=0.5)
    assert len(mon.history[pm]) > 0
    # таблица ваттметра: частота датчика -> последнее показание
    rows = mon.pm_table(pm)
    assert len(rows) == 1 and rows[0][0] == pytest.approx(pm.value("freq")) and rows[0][1] == pytest.approx(b.level, abs=0.5)
    mon.clear_pm_table(pm)
    assert mon.pm_table(pm) == []


def test_monitor_shows_data_even_with_open_ports_and_hides_when_stopped(qtbot, lab):
    """Любой приёмник, выдающий данные (даже без сигнала / открытые порты), отображается; без данных - пропадает."""
    links, px, _bench = lab
    vna = px["pna"]
    _sim(vna).dut = None                               # порты открыты, но трасса идёт
    mon = ActivityMonitor(links, Thresholds(hold_s=0.5))
    qtbot.waitUntil(lambda: _measuring(mon, vna), timeout=10000)
    assert not mon.activity(vna).signal
    vna.action("meas_none")                            # все слоты выключены -> данных нет
    qtbot.waitUntil(lambda: not _measuring(mon, vna), timeout=5000)


def test_main_window_shows_only_measuring(qtbot, lab, tmp_path):
    links, px, bench = lab
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    assert [t.text() for t in win.tiles.values()] == ["VNA", "SA", "PM", "GEN"]     # только что это, без IDLE
    from visalab.front.main_window import TILE_MIN_HEIGHT
    assert all(t.height() >= TILE_MIN_HEIGHT for t in win.tiles.values())
    qtbot.waitUntil(
        lambda: len(win.visible_panels()) == 3
        and all(win.monitor.activity(px[k]).measuring for k in ("pna", "e4446a", "u2021xa")),
        timeout=10000,
    )
    vna_panel = win.panels[px["pna"]]
    assert vna_panel.plots[0].traces and vna_panel.plots[0].traces[0][0].startswith("1·S11")
    assert vna_panel.plots[0].minimumHeight() >= 240
    # генератор выключен: SA показывает шум (NOISE ...), без фальшивого источника
    assert win.panels[px["e4446a"]].level.text().startswith("NOISE")
    gen = px["e8267d"]
    gen.set("rf.power", -5.0)
    gen.set("rf.output", True)
    qtbot.waitUntil(lambda: gen.value("rf.output") is True and gen.value("rf.power") == -5.0, timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: "← GEN" in win.panels[px["e4446a"]].source.text(), timeout=10000)
    assert [t.text() for t in win.tiles.values()] == ["VNA", "SA", "PM", "GEN"]
    assert not any(t.indicated() or t.error for t in win.tiles.values())   # окон приборов нет, ошибок нет
    pm = px["u2021xa"]
    panel = win.panels[pm]                                # ваттметр: таблица частота / мощность, не график
    assert panel.power_table.minimumHeight() >= 240
    qtbot.waitUntil(lambda: bool(panel.power_table.rows), timeout=5000)
    assert panel.power_table.isVisibleTo(panel) and not any(p.isVisibleTo(panel) for p in panel.plots)
    assert panel.power_table.rows[0][0] == pytest.approx(pm.value("freq")) and panel.power_table.item(0, 1).text().endswith("dBm")
    win.all_button.setChecked(False)
    assert settings.value("MAIN/SHOW_ALL", type=bool) is False


def test_main_window_all_shows_idle_receivers(qtbot, lab, tmp_path):
    links, _px, _bench = lab
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    win.all_button.setChecked(True)
    assert len(win.visible_panels()) == 3 and not win.idle.isVisible()


def test_short_names_number_same_class():
    class P:
        def __init__(self, prof):
            self.profile = type("Prof", (), {"name": prof})()
    a, b, c = P("signal_generator"), P("signal_generator"), P("power_meter")
    assert [short_name(p, [a, b, c]) for p in (a, b, c)] == ["GEN", "GEN2", "PM"]


def test_run_main_launcher(qtbot, tmp_path, monkeypatch):
    import run_main
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    orig = run_main.MainWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(run_main.MainWindow, "__init__", track)
    from visalab.coord import LINKS
    before = set(LINKS.proxies())                      # окна других тестов могут ещё жить
    assert run_main.main(["--sim", "--ini", str(tmp_path / "x.ini"), "--no-connect", "--only", "pna,e8267d"]) == 0
    new = [p for p in LINKS.proxies() if p not in before]
    assert len(made) == 1 and len(new) == 2 and all(p in made[0].tiles for p in new)
    assert {made[0].monitor.name(p)[:3] for p in new} == {"VNA", "GEN"}
    for p in new:
        p.shutdown()
    made[0].close()


def test_thresholds_in_main_window_settings(qtbot, lab, tmp_path):
    links, px, _bench = lab
    pm = px["u2021xa"]
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links, Thresholds(hold_s=0.5)))
    qtbot.addWidget(win)
    win.show()
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["LIVE", "SET"]
    assert win.live_page.isVisible() and not win.settings_panel.isVisible()
    win.tabs.setCurrentIndex(1)                         # SET - настройки вместо живых данных
    assert win.settings_panel.isVisible() and not win.live_page.isVisible()
    assert settings.value("MAIN/TAB", type=int) == 1 and set(win.fields) >= {"sa_margin_db", "pm_floor_dbm", "hold_s"}
    qtbot.waitUntil(lambda: win.monitor.activity(pm).measuring, timeout=10000)
    f = win.fields["pm_floor_dbm"]
    f.setText("-40")
    f.editingFinished.emit()
    assert win.monitor.th.pm_floor_dbm == -40.0 and settings.value("MAIN/THRESHOLDS/pm_floor_dbm", type=float) == -40.0
    assert win.monitor.activity(pm).measuring           # порог не скрывает выдаваемые данные
    f.setText("junk")
    f.editingFinished.emit()
    assert f.text() == "-40 dBm"
    win.fields["freq_tol_rel"].setText("0.5")           # в процентах
    win.fields["freq_tol_rel"].editingFinished.emit()
    assert win.monitor.th.freq_tol_rel == pytest.approx(0.005)
    win2 = MainWindow(settings, ActivityMonitor(links))   # из ini
    qtbot.addWidget(win2)
    assert win2.tabs.currentIndex() == 1 and win2.monitor.th.pm_floor_dbm == -40.0 and win2.fields["pm_floor_dbm"].text() == "-40 dBm"


def test_open_instrument_picks_the_instrument_window_not_helpers(qtbot, tmp_path):
    from PyQt6.QtWidgets import QWidget
    from visalab.front import VNAWindow
    settings = QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    proxy = _proxy("pna")
    links.add(proxy)
    helper = QWidget()                                  # вспомогательное окно с тем же прибором (как TraceEditor)
    helper.proxy = proxy
    qtbot.addWidget(helper)
    vna = VNAWindow(proxy, settings)
    qtbot.addWidget(vna)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.open_instrument(proxy)
    assert vna.isVisible() and not helper.isVisible()
    win.monitor.stop()
    proxy.shutdown()


def test_tiles_show_open_window_and_error(qtbot, tmp_path):
    """Плитка: имя прибора; индикатор - окно открыто; красная - ошибка в окне прибора (до CLEAR)."""
    from visalab.front import PulseGenWindow
    settings = QSettings(str(tmp_path / "t.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    proxy = _proxy("sdg")
    links.add(proxy)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    tile = win.tiles[proxy]
    assert tile.text() == "PULSE" and not tile.window_open and not tile.error
    sdg = PulseGenWindow(proxy, settings)
    qtbot.addWidget(sdg)
    assert not tile.window_open                         # окно есть, но скрыто
    sdg.show()
    qtbot.waitUntil(lambda: tile.window_open, timeout=2000)
    assert tile.indicated()
    sdg.tracker.note_error("instrument", "-113 Undefined header [EQPHASE]")
    assert tile.error and "Undefined header" in tile.toolTip() and tile.property("wstate") == "error"
    sdg.clear_button.click()                            # CLEAR в журнале - ошибка снята
    assert not tile.error
    sdg.close()
    assert not tile.window_open
    tile.click()                                        # клик - показать окно
    assert sdg.isVisible() and tile.window_open
    win.monitor.stop()
    proxy.shutdown()


def test_main_window_full_screen(qtbot, tmp_path):
    from PyQt6.QtCore import Qt
    settings = QSettings(str(tmp_path / "f.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(ProxyLinks()))
    qtbot.addWidget(win)
    win.show_window()
    assert win.isVisible() and not win.isFullScreen()
    assert win.titleBar.hBoxLayout.indexOf(win.full_button) == win.titleBar.hBoxLayout.indexOf(win.titleBar.minimized_button) + 1
    assert win.titleBar.hBoxLayout.indexOf(win.full_button) < win.titleBar.hBoxLayout.indexOf(win.titleBar.close_button)
    win.full_button.click()
    assert win.isFullScreen() and win.full_button.full and settings.value("MAIN/FULL_SCREEN", type=bool)
    win.toggle_full_screen()                            # то же, что F11
    assert not win.isFullScreen() and not win.full_button.full
    assert win._full_key.key().toString() == QKeySequence(Qt.Key.Key_F11).toString()
    win.set_full_screen(True)
    win.close()
    win2 = MainWindow(settings, ActivityMonitor(ProxyLinks()))   # как при выходе - во весь экран
    qtbot.addWidget(win2)
    win2.show_window()
    assert win2.isFullScreen() and win2.full_button.full


def test_delay_on_address_page(qtbot, tmp_path):
    from visalab.front import PulseGenWindow
    from visalab.front.instrument_window import saved_delay
    settings = QSettings(str(tmp_path / "d.ini"), QSettings.Format.IniFormat)
    proxy = _proxy("sdg")
    win = PulseGenWindow(proxy, settings)
    qtbot.addWidget(win)
    names = [ln.name for ln in win.connection_page.lines]
    assert names[:4] == ["DRV", "MDL", "ADDR", "DLY"]
    from visalab.core.units import si_format
    from visalab.drivers import default_delay
    assert win.delay_edit.text() == ""                                  # пустое - пауза драйвера, и видно какая
    assert win.delay_edit.placeholderText() == f"default {si_format(default_delay('sdg'), 's', 4)}"
    win.delay_edit.setText("120m")
    assert win.apply_delay()
    assert saved_delay(settings, win.CONNECTION_GROUP) == pytest.approx(0.12) and win.delay_edit.text() == "120 ms"
    qtbot.waitUntil(lambda: worker(proxy).command_delay_s == pytest.approx(0.12), timeout=3000)
    win.delay_edit.setText("junk")
    assert not win.apply_delay() and win.delay_edit.text() == "120 ms"
    win.delay_edit.setText("")
    assert win.apply_delay() and saved_delay(settings, win.CONNECTION_GROUP) is None
    proxy.shutdown()


def test_windows_restored_as_at_exit(qtbot, tmp_path, monkeypatch):
    import run_main
    from PyQt6.QtWidgets import QApplication
    from visalab.coord import LINKS
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    ini = str(tmp_path / "v.ini")
    QSettings(ini, QSettings.Format.IniFormat).setValue("WINDOWS/pna/VISIBLE", True)
    made = []
    orig = run_main.MainWindow.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(run_main.MainWindow, "__init__", track)
    monkeypatch.setattr(QApplication, "quit", lambda *a: None)
    before = set(LINKS.proxies())
    assert run_main.main(["--sim", "--ini", ini, "--no-connect", "--only", "pna,e8267d"]) == 0
    from visalab.front.instrument_window import InstrumentWindow
    new = [p for p in LINKS.proxies() if p not in before]          # приборы, открытые этим запуском
    wins = {w.proxy: w for w in QApplication.topLevelWidgets() if isinstance(w, InstrumentWindow)
            and w.proxy in new}
    shown = sorted(made[0].monitor.name(p)[:3] for p, w in wins.items() if w.isVisible())
    assert shown == ["VNA"]                              # GEN был скрыт (по умолчанию)
    gen = next(w for p, w in wins.items() if made[0].monitor.name(p).startswith("GEN"))
    gen.show()
    made[0].close()                                      # запомнить перед выходом
    s = QSettings(ini, QSettings.Format.IniFormat)
    assert s.value("WINDOWS/e8267d/VISIBLE", type=bool) is True and s.value("WINDOWS/pna/VISIBLE", type=bool) is True
    for p in wins:
        p.shutdown()


def test_main_window_widgets_grow_with_window(qtbot, lab, tmp_path):
    """Алексей 2026-11-18: растянули главное окно - графики, таблица ваттметра и поля SET растут вместе с ним."""
    links, _px, _bench = lab
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    win.all_button.setChecked(True)
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)
    win.resize(720, 900)
    qtbot.wait(100)
    plots = [p for panel in win.visible_panels() for p in panel.plots if p.isVisible()]
    table = next(panel.power_table for panel in win.visible_panels() if panel.power_table is not None)
    before = [p.height() for p in plots], table.height(), [p.width() for p in plots]
    win.resize(1120, 1300)
    qtbot.waitUntil(lambda: all(p.height() > h + 60 for p, h in zip(plots, before[0])), timeout=2000)
    assert table.height() > before[1] + 60
    assert all(p.width() > w + 300 for p, w in zip(plots, before[2]))
    win.tabs.setCurrentIndex(1)
    qtbot.wait(100)
    field = win.fields["hold_s"] if "hold_s" in win.fields else next(iter(win.fields.values()))
    assert field.width() > 600


def test_main_window_panel_close_reorder_and_layout(qtbot, lab, tmp_path):
    """Панели измерений можно закрывать (и возвращать чипом), переставлять и раскладывать по сетке."""
    links, px, _bench = lab
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    win.all_button.setChecked(True)
    assert [p.title.text() for p in win.visible_panels()] == ["VNA", "SA", "PM"]

    # перестановка: VNA -> вправо
    win.panels[px["pna"]].move_next_button.click()
    assert [p.title.text() for p in win.visible_panels()] == ["SA", "VNA", "PM"]

    # смена раскладки: 1 COL -> 2 COL -> ROW
    assert win.layout_button.text() == "1 COL"
    win.layout_button.click()
    assert win.layout_mode == "2 COL" and win.layout_button.text() == "2 COL"
    win.layout_button.click()
    assert win.layout_mode == "ROW" and win.layout_button.text() == "ROW"

    # закрыть SA -> появляется чип возврата "+ SA"
    win.panels[px["e4446a"]].close_button.click()
    assert [p.title.text() for p in win.visible_panels()] == ["VNA", "PM"]
    restore_sa = [b for b in win.restore_chips.values() if b.text() == "+ SA"]
    assert len(restore_sa) == 1

    # перезапуск: порядок, раскладка и скрытая панель восстановились из ini
    win2 = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win2)
    win2.show()
    assert win2.layout_mode == "ROW"
    assert [p.title.text() for p in win2.visible_panels()] == ["VNA", "PM"]
    win2.restore_chips[win2._panel_key(px["e4446a"])].click()
    assert [p.title.text() for p in win2.visible_panels()] == ["SA", "VNA", "PM"]

    # В CW режиме с портом P2 панель VNA показывает CW P2 и частоту
    vna = px["pna"]
    vna.set("freq.cw", 2.4e9)
    vna.set("gen.port", 2)
    vna.set("gen.state", True)
    qtbot.waitUntil(lambda: "CW P2" in win2.panels[vna].source.text(), timeout=5000)


def test_live_plot_decimation_cache_and_cursor(qtbot):
    from PyQt6.QtCore import QPointF, QRectF
    from visalab.front.live_plot import LivePlot, decimate_trace_polygon, nearest_index

    y = [-80.0] * 10000 + [-5.0] + [-80.0] * 10000 + [-95.0] + [-80.0] * 11999
    tr = Trace.linear(1e9, 10e9, y)
    rect = QRectF(6, 20, 400, 200)
    poly = decimate_trace_polygon(tr, 1e9, 10e9, -100.0, 0.0, rect)
    assert 0 < len(poly) <= 2 * 200
    ys_px = [pt.y() for pt in poly]
    # пик -5 dBm и провал -95 dBm сохранились в огибающей
    assert min(ys_px) == pytest.approx(rect.bottom() - (-5.0 - (-100.0)) / 100.0 * rect.height())
    assert max(ys_px) == pytest.approx(rect.bottom() - (-95.0 - (-100.0)) / 100.0 * rect.height())

    # нелинейная ось X с NaN
    non_uni = Trace((0.0, 1.0, 2.0, 4.0, 8.0, 10.0), (-50.0, float("nan"), -10.0, -60.0, -20.0, -40.0))
    assert nearest_index(tr, 5.5e9) == 16000
    assert nearest_index(non_uni, 3.9) == 3

    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.resize(500, 260)
    plot.set_traces([("S21", tr)])
    plot.grab()
    cache_obj = plot._cache
    assert cache_obj is not None
    # движение мыши не перестраивает базовый кэш
    plot._mouse = QPointF(200, 100)
    plot.grab()
    assert plot._cache is cache_obj
    # повторный set_traces с тем же объектом Trace ничего не сбрасывает
    plot.set_traces([("S21", tr)])
    assert plot._cache is cache_obj
    # смена маркера перестраивает кэш
    plot.set_marker(5.5e9, -5.0)
    plot.grab()
    assert plot._cache is not cache_obj


def test_activity_fast_vna_signal_matches_reference():
    from visalab.coord.activity import _trace_has_signal
    cplx_thru = Trace.linear(1e9, 2e9, [0.01, 0.5, 0.01], im=[0.0, 0.0, 0.0], meta={"format": "SMITH"})
    assert _trace_has_signal(cplx_thru, "S21", -40.0, -3.0) is True
    assert _trace_has_signal(cplx_thru, "S21", -3.0, -3.0) is False
    cplx_zero = Trace.linear(1e9, 2e9, [0.0, 0.0], im=[0.0, 0.0], meta={"format": "SDATA"})
    assert _trace_has_signal(cplx_zero, "S11", -40.0, -300.0) is True
    assert _trace_has_signal(cplx_zero, "S11", -40.0, -301.0) is False
    lin = Trace.linear(1e9, 2e9, [0.001, 0.1], meta={"format": "LIN_MAG"})
    assert _trace_has_signal(lin, "S21", -25.0, -3.0) is True
    assert _trace_has_signal(lin, "S21", -15.0, -3.0) is False
    assert _trace_has_signal(lin, "S11", -40.0, -50.0) is True


def test_main_window_sa_plot_matches_device_scale(qtbot, lab, tmp_path):
    """График SA в Main Window имеет такой же масштаб Y (RLEV, SDIV, 10 делений), как прибор."""
    links, px, _bench = lab
    sa = px["e4446a"]
    settings = QSettings(str(tmp_path / "sa_scale.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    panel = win.panels[sa]
    qtbot.waitUntil(lambda: bool(panel.plots) and panel.plots[0]._scale == pytest.approx((-100.0, 0.0, 10.0)), timeout=10000)
    assert panel.plots[0]._divs_y == 10

    sa.set("ref_level", -20.0)
    sa.set("scale_div", 5.0)
    qtbot.waitUntil(lambda: panel.plots[0]._scale == pytest.approx((-70.0, -20.0, 5.0)), timeout=5000)
    assert panel.plots[0]._divs_y == 10

    # Изменили масштаб на передней панели прибора -> обновится при следующей развёртке
    _sim(sa).c.update(rlev=-10.0, pdiv=2.0)
    qtbot.waitUntil(lambda: panel.plots[0]._scale == pytest.approx((-30.0, -10.0, 2.0)), timeout=5000)
    panel.plots[0].grab()


def test_main_window_vna_plot_matches_device_scale(qtbot, lab, tmp_path):
    """Графики VNA в Main Window имеют такой же масштаб Y (PDIV, RLEV, RPOS, 10 делений), как на приборе."""
    links, px, _bench = lab
    vna = px["pna"]
    settings = QSettings(str(tmp_path / "vna_scale.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    panel = win.panels[vna]
    # По умолчанию на PNA: pdiv=10, rlev=0, rpos=5, 10 делений -> [-50, +50]
    qtbot.waitUntil(lambda: bool(panel.plots) and panel.plots[0]._scale == pytest.approx((-50.0, 50.0, 10.0)), timeout=10000)
    assert panel.plots[0]._divs_y == 10

    # Изменяем масштаб трейса через драйвер
    vna.set("scale.div", 5.0)
    vna.set("scale.ref", -10.0)
    vna.set("scale.rpos", 8.0)
    qtbot.waitUntil(lambda: panel.plots[0]._scale == pytest.approx((-50.0, 0.0, 5.0)), timeout=5000)
    assert panel.plots[0]._divs_y == 10

    # Даже в HOLD (без новых развёрток) autoscale и смена scale.* сразу обновляют график
    vna.action("abort")
    qtbot.waitUntil(lambda: vna.value("meas.continuous") is False, timeout=5000)
    vna.action("autoscale")
    qtbot.waitUntil(lambda: vna.value("scale.rpos") == pytest.approx(5.0), timeout=5000)
    pdiv, rlev, rpos = vna.value("scale.div"), vna.value("scale.ref"), vna.value("scale.rpos")
    expected = (rlev - rpos * pdiv, rlev + (10 - rpos) * pdiv, pdiv)
    qtbot.waitUntil(lambda: panel.plots[0]._scale == pytest.approx(expected), timeout=5000)

    # Второй трейс с другим масштабом -> отдельный график со своим масштабом прибора
    sim = _sim(vna)
    sim.meas["CH1_S21_2"] = {"s": "S21", "fmt": "MLOG", "ch": 1, "pdiv": 2.0, "rlev": 0.0, "rpos": 5.0}
    sim.windows[2] = {1: "CH1_S21_2"}
    vna.set("m2.channel", 1)
    vna.set("m2.name", "CH1_S21_2")
    vna.set("m2.enabled", True)
    vna.set("meas.continuous", True)
    qtbot.waitUntil(lambda: len([p for p in panel.plots if p.isVisibleTo(panel)]) == 2, timeout=10000)
    scales = [p._scale for p in panel.plots if p.isVisibleTo(panel)]
    assert any(s == pytest.approx((-10.0, 10.0, 2.0)) for s in scales)
    assert any(s == pytest.approx(expected) for s in scales)
    for p in panel.plots:
        if p.isVisibleTo(panel):
            p.grab()


def test_main_window_pm_time_plot_matches_power_meter_scale(qtbot, lab, tmp_path):
    """Во временном режиме (TRAC ON) график PM в Main Window повторяет шкалу окна PowerMeterWindow (AUTO/MAN, dBm/W)."""
    from visalab.front import PowerMeterWindow
    links, px, _bench = lab
    pm = px["u2021xa"]
    settings = QSettings(str(tmp_path / "pm_scale.ini"), QSettings.Format.IniFormat)
    pm_win = PowerMeterWindow(pm, settings)
    qtbot.addWidget(pm_win)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    pm.set("trace.state", True)
    panel = win.panels[pm]
    qtbot.waitUntil(lambda: bool(panel.plots) and panel.plots[0].isVisibleTo(panel) and panel.plots[0]._scale is not None,
                    timeout=10000)
    qtbot.waitUntil(lambda: pm_win.plot.trace is not None, timeout=5000)
    lo, hi = pm_win.plot.y_range()
    assert panel.plots[0]._divs_y == 6
    assert panel.plots[0]._scale == pytest.approx((lo, hi, (hi - lo) / 6))

    # Ручная шкала в PowerMeterWindow (REF=10 dBm, DIV=5 dB) -> Main Window сразу обновляется
    pm_win.scale_ref.set_value(10.0)
    pm_win.scale_div.set_value(5.0)
    qtbot.waitUntil(lambda: panel.plots[0]._scale == pytest.approx((-20.0, 10.0, 5.0)), timeout=5000)
    assert panel.plots[0]._y_unit == "dBm"

    # Переключение в W -> Main Window переходит на линейную шкалу в ваттах
    pm_win.scale_auto.set_value(True)
    pm_win.scale_unit.set_value("W")
    lo_w, hi_w = pm_win.plot.y_range()
    qtbot.waitUntil(lambda: panel.plots[0]._y_unit == "W"
                    and panel.plots[0]._scale == pytest.approx((lo_w, hi_w, (hi_w - lo_w) / 6)), timeout=5000)
    panel.plots[0].grab()


def test_pm_table_matrix_and_unit_switch(qtbot, lab, tmp_path):
    """Таблица ваттметра: строки - частота, колонки - подаваемая мощность источника;
    кнопка единицы переключает dBm / W / dBW / V / dB и сохраняется в ini."""
    links, px, bench = lab
    gen, pm = px["e8267d"], px["u2021xa"]
    ini = tmp_path / "pm_matrix.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    mon = ActivityMonitor(links)
    win = MainWindow(settings, mon)
    qtbot.addWidget(win)
    win.show()
    panel = win.panels[pm]
    tbl = panel.power_table

    def measure_at(freq_hz: float, power_dbm: float, expected_meas: float):
        gen.set("rf.frequency", freq_hz)
        gen.set("rf.power", power_dbm)
        gen.set("rf.output", True)
        pm.set("freq", freq_hz)
        qtbot.waitUntil(lambda: gen.value("rf.output") is True
                        and gen.value("rf.frequency") == pytest.approx(freq_hz)
                        and gen.value("rf.power") == pytest.approx(power_dbm)
                        and pm.value("freq") == pytest.approx(freq_hz), timeout=5000)
        bench.step()
        qtbot.waitUntil(lambda: pm.value("meas.power") is not None
                        and abs(pm.value("meas.power") - expected_meas) < 0.3, timeout=5000)
        qtbot.waitUntil(lambda: any(abs(f - freq_hz) < 1.0 and power_dbm in vals
                                    and abs(vals[power_dbm] - expected_meas) < 0.3
                                    for f, vals in tbl.rows), timeout=5000)

    # Стенд: потери 10 дБ
    measure_at(1e9, -10.0, -20.0)
    measure_at(1e9, -5.0, -15.0)
    measure_at(2e9, -5.0, -15.0)
    measure_at(2e9, -10.0, -20.0)

    assert tbl.columnCount() == 3
    assert [tbl.horizontalHeaderItem(c).text() for c in range(3)] == ["FREQ", "-10 dBm", "-5 dBm"]
    assert tbl.rowCount() == 2
    assert tbl.item(0, 0).text() == "1 GHz" and tbl.item(1, 0).text() == "2 GHz"
    assert tbl.item(0, 1).text() == "-20.00 dBm" and tbl.item(0, 2).text() == "-15.00 dBm"
    assert tbl.item(1, 1).text() == "-20.00 dBm" and tbl.item(1, 2).text() == "-15.00 dBm"
    # Текущая ячейка (2 GHz, -10 dBm) выделена жирным
    assert tbl.item(1, 0).font().bold() and tbl.item(1, 1).font().bold() and not tbl.item(0, 2).font().bold()

    # Переключение единиц: dBm -> W -> dBW -> V -> dB -> dBm
    assert panel.unit_button.isVisibleTo(panel) and panel.unit_button.text() == "dBm"
    panel.unit_button.click()                                   # W
    assert tbl.unit == "W" and panel.unit_button.text() == "W"
    assert tbl.horizontalHeaderItem(1).text().endswith("W")
    assert tbl.item(0, 1).text().endswith("W") and panel.level.text().endswith("W")
    panel.unit_button.click()                                   # dBW
    assert tbl.unit == "dBW" and tbl.horizontalHeaderItem(1).text() == "-40 dBW"
    assert tbl.item(0, 1).text() == "-50.00 dBW"
    panel.unit_button.click()                                   # V (50 Ω)
    assert tbl.unit == "V" and tbl.horizontalHeaderItem(1).text().endswith("V")
    assert tbl.item(0, 1).text().endswith("V")
    panel.unit_button.click()                                   # dB (Pout - Pin = -10.00 dB)
    assert tbl.unit == "dB" and tbl.horizontalHeaderItem(1).text() == "-10 dBm"
    assert tbl.item(0, 1).text() == "-10.00 dB" and tbl.item(1, 2).text() == "-10.00 dB"

    # Выбранная единица сохраняется в ini и восстанавливается в новом окне
    settings.sync()
    win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), mon)
    qtbot.addWidget(win2)
    assert win2.panels[pm].power_table.unit == "dB" and win2.panels[pm].unit_button.text() == "dB"
    panel.unit_button.click()                                   # обратно в dBm
    assert tbl.unit == "dBm"
    gen.set("rf.output", False)
    qtbot.waitUntil(lambda: gen.value("rf.output") is False, timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: pm.value("meas.power") is not None and pm.value("meas.power") < -60.0, timeout=5000)
    mon.clear_pm_table(pm)
    qtbot.waitUntil(lambda: tbl.rowCount() == 0, timeout=5000)


def test_settings_displayed_graphs_and_level_trigger(qtbot, lab, tmp_path):
    """Настройки отображаемых графиков во вкладке SET: галочки Show / Above level и порог видеоуровня."""
    links, px, bench = lab
    gen, sa, pm = px["e8267d"], px["e4446a"], px["u2021xa"]
    ini = tmp_path / "display_rules.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    mon = ActivityMonitor(links)
    win = MainWindow(settings, mon)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)

    sa_key, pm_key = win._panel_key(sa), win._panel_key(pm)
    assert {sa_key, pm_key, win._panel_key(px["pna"])} <= set(win.display_controls)
    sa_show, sa_by_lvl, sa_edit = win.display_controls[sa_key]
    pm_show, pm_by_lvl, pm_edit = win.display_controls[pm_key]

    # 1) Сняли галочку Show у PM -> панель скрылась, Above level / порог недоступны
    pm_show.setChecked(False)
    assert win.panels[pm] not in win.visible_panels()
    assert not pm_by_lvl.isEnabled() and not pm_edit.isEnabled()
    pm_show.setChecked(True)
    assert win.panels[pm] in win.visible_panels() and pm_by_lvl.isEnabled()

    # 2) Включили Above level у SA с порогом -30 dBm: при шуме (-90 dBm) SA скрыт
    sa_by_lvl.setChecked(True)
    sa_edit.setText("-30")
    sa_edit.editingFinished.emit()
    assert win.display_rules[sa_key]["level"] == pytest.approx(-30.0)
    assert win.panels[sa] not in win.visible_panels()

    # Подали сигнал -10 dBm (на SA пик -20 dBm >= -30 dBm) -> SA появился
    gen.set("rf.frequency", 1e9)
    gen.set("rf.power", -10.0)
    gen.set("rf.output", True)
    qtbot.waitUntil(lambda: gen.value("rf.output") is True and gen.value("rf.power") == pytest.approx(-10.0), timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: win.panels[sa] in win.visible_panels(), timeout=10000)

    # Подняли порог до -15 dBm (-20 < -15) -> SA снова скрылся
    sa_edit.setText("-15")
    sa_edit.editingFinished.emit()
    assert win.panels[sa] not in win.visible_panels()

    # 3) Проверим сохранение в ini и восстановление в новом MainWindow
    pm_by_lvl.setChecked(True)
    pm_edit.setText("-25")
    pm_edit.editingFinished.emit()
    pm_show.setChecked(False)
    settings.sync()

    win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), mon)
    qtbot.addWidget(win2)
    sa_show2, sa_by_lvl2, sa_edit2 = win2.display_controls[sa_key]
    pm_show2, pm_by_lvl2, pm_edit2 = win2.display_controls[pm_key]
    assert sa_show2.isChecked() is True and sa_by_lvl2.isChecked() is True
    assert win2.display_rules[sa_key]["level"] == pytest.approx(-15.0) and sa_edit2.text() == "-15 dBm"
    assert pm_show2.isChecked() is False and pm_by_lvl2.isChecked() is True
    assert win2.display_rules[pm_key]["level"] == pytest.approx(-25.0) and pm_edit2.text() == "-25 dBm"
    assert win2.panels[pm] not in win2.visible_panels() and win2.panels[sa] not in win2.visible_panels()


def test_vna_panel_stays_visible_in_hold_and_with_non_m1_slot(qtbot, lab, tmp_path):
    """График ВАЦ не пропадает через hold_s в режиме HOLD / SINGLE или когда включён только слот m2 (m1 выкл)."""
    links, px, _bench = lab
    vna = px["pna"]
    sim = _sim(vna)
    sim.meas["CH1_S21_2"] = {"s": "S21", "fmt": "MLOG", "ch": 1}
    sim.windows[1][2] = "CH1_S21_2"
    mon = ActivityMonitor(links, Thresholds(hold_s=0.2))
    settings = QSettings(str(tmp_path / "vna_hold.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, mon)
    qtbot.addWidget(win)
    win.show()
    panel = win.panels[vna]
    qtbot.waitUntil(lambda: panel in win.visible_panels() and bool(panel.plots[0].traces), timeout=5000)

    # Переключаемся только на m2 (m1 выключен) и останавливаем развёртку (HOLD)
    vna.set("m2.channel", 1)
    vna.set("m2.name", "CH1_S21_2")
    vna.set("m2.enabled", True)
    vna.set("m1.enabled", False)
    qtbot.waitUntil(lambda: vna.value("m2.trace") is not None and vna.value("m1.enabled") is False, timeout=5000)
    vna.action("abort")
    qtbot.waitUntil(lambda: vna.value("meas.continuous") is False, timeout=5000)

    # Ждём дольше hold_s (0.2 с): график ВАЦ остаётся на экране и показывает трассу m2
    qtbot.wait(450)
    mon.tick()
    assert mon.activity(vna).measuring is True
    assert panel in win.visible_panels()
    assert panel.plots[0].traces and "S21" in panel.plots[0].traces[0][0]

    # Даже когда данных нет (meas_none) и ALL выключен, панель с галочкой только на Show всё равно показывается
    win.all_button.setChecked(False)
    vna.action("meas_none")
    qtbot.waitUntil(lambda: mon.activity(vna).measuring is False, timeout=5000)
    assert panel in win.visible_panels()

    # А если поставить ещё и галочку Above level - без данных панель скрывается
    vna_key = win._panel_key(vna)
    win.set_panel_by_level(vna_key, True)
    assert panel not in win.visible_panels()
    win.set_panel_by_level(vna_key, False)
    assert panel in win.visible_panels()


def test_titlebar_sinc_emblem_and_instrument_tiles(qtbot, lab, tmp_path):
    """Заголовок главного окна: без надписи LAB, слева пульсирующая sinc-эмблема и кнопки приборов."""
    from visalab.front.main_window import SincEmblem
    links, px, _bench = lab
    settings = QSettings(str(tmp_path / "tb.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    try:
        assert win.titleBar.title_label.isHidden()
        assert isinstance(win.emblem, SincEmblem)
        assert win.titleBar.hBoxLayout.indexOf(win.emblem) == 0
        assert win.titleBar.hBoxLayout.indexOf(win._tiles_host) == 1
        assert win.emblem._timer.isActive()

        # Фаза пульсации движется на тиках анимации
        p0 = win.emblem.pulse
        qtbot.wait(100)
        assert win.emblem.pulse != p0

        # Все плитки приборов находятся в заголовке и не перехватываются перетаскиванием окна
        for p in px.values():
            tile = win.tiles[p]
            assert tile.parentWidget() is win._tiles_host
            assert win.titleBar.isAncestorOf(tile)
            pos = tile.mapTo(win.titleBar, tile.rect().center())
            assert not win.titleBar.canDrag(pos)

        # Пустое место заголовка остаётся перетаскиваемым
        empty_x = win._tiles_host.geometry().right() + 30
        if empty_x < win.titleBar.minimized_button.x() - 10:
            from PyQt6.QtCore import QPoint
            assert win.titleBar.canDrag(QPoint(empty_x, win.titleBar.height() // 2))

        win.hide()
        assert not win.emblem._timer.isActive()
    finally:
        win.cleanup()
        win.close()
