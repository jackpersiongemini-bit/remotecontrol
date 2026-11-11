"""Главное окно и координатор «кто что измеряет» на симуляторах."""
import math

import pytest
from PyQt6.QtCore import QSettings

from visalab.coord import ActivityMonitor, ProxyLinks, Thresholds
from visalab.coord.activity import (Source, magnitude_db, match_source, short_name, slot_magnitude_db,
                                    spectrum_peak, vna_signal)
from visalab.core import InstrumentProxy, SimTransport
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
    drv, sim_cls = DRIVERS[kind]
    return InstrumentProxy(drv, SimTransport(sim_cls()))


def _sim(proxy):
    return proxy._worker.transport.sim


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
    # ВАЦ: DUT (фильтр) на портах -> по уровню S11 измеряет
    qtbot.waitUntil(lambda: _measuring(mon, vna), timeout=10000)
    # генератор выключен: анализатор видит только шум, ваттметр ниже порога
    qtbot.waitUntil(lambda: mon.activity(sa).level is not None, timeout=10000)
    assert not _measuring(mon, sa) and not _measuring(mon, pm)
    gen.set("rf.frequency", 1e9)
    gen.set("rf.power", -5.0)
    gen.set("rf.output", True)
    qtbot.waitUntil(lambda: gen.value("rf.output") is True and gen.value("rf.power") == -5.0, timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: _measuring(mon, sa) and _measuring(mon, pm), timeout=10000)
    a = mon.activity(sa)
    assert a.source is not None and a.source.name == "GEN" and a.freq == pytest.approx(1e9, rel=1e-3)
    assert a.path_db == pytest.approx(-10.0, abs=1.0)
    b = mon.activity(pm)
    assert b.source is not None and b.source.name == "GEN" and b.path_db == pytest.approx(-10.0, abs=0.5)
    assert len(mon.history[pm]) > 0


def test_monitor_vna_idle_with_open_ports(qtbot, lab):
    links, px, _bench = lab
    vna = px["pna"]
    _sim(vna).dut = None                               # развёртки идут, но на портах ничего
    mon = ActivityMonitor(links, Thresholds(hold_s=0.5))
    qtbot.waitUntil(lambda: mon._updated.get(vna) is not None, timeout=10000)
    qtbot.wait(800)
    mon.tick()
    assert not _measuring(mon, vna)


def test_main_window_shows_only_measuring(qtbot, lab, tmp_path):
    links, px, bench = lab
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    assert [t.text().split("\n")[0] for t in win.tiles.values()] == ["VNA", "SA", "PM", "GEN"]
    qtbot.waitUntil(lambda: [p.title.text() for p in win.visible_panels()] == ["VNA"], timeout=10000)
    vna_panel = win.panels[px["pna"]]
    assert vna_panel.plots[0].traces and vna_panel.plots[0].traces[0][0].startswith("1·S11")
    gen = px["e8267d"]
    gen.set("rf.power", -5.0)
    gen.set("rf.output", True)
    qtbot.waitUntil(lambda: gen.value("rf.output") is True and gen.value("rf.power") == -5.0, timeout=5000)
    bench.step()
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)
    assert win.tiles[gen].indicated() and win.tiles[px["e4446a"]].indicated()
    assert "← GEN" in win.panels[px["e4446a"]].source.text()
    assert win.panels[px["u2021xa"]].plots[0].points
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
    vna = px["pna"]
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links, Thresholds(hold_s=0.5)))
    qtbot.addWidget(win)
    win.show()
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["LIVE", "SET"]
    assert win.live_page.isVisible() and not win.settings_panel.isVisible()
    win.tabs.setCurrentIndex(1)                         # SET - настройки вместо живых данных
    assert win.settings_panel.isVisible() and not win.live_page.isVisible()
    assert settings.value("MAIN/TAB", type=int) == 1 and set(win.fields) >= {"vna_thru_db", "vna_refl_db", "pm_floor_dbm"}
    qtbot.waitUntil(lambda: win.monitor.activity(vna).measuring, timeout=10000)
    f = win.fields["vna_refl_db"]                       # фильтр в S11 ~ -34 dB: порог ниже - не измеряет
    f.setText("-60")
    f.editingFinished.emit()
    assert win.monitor.th.vna_refl_db == -60.0 and settings.value("MAIN/THRESHOLDS/vna_refl_db", type=float) == -60.0
    qtbot.waitUntil(lambda: not win.monitor.activity(vna).measuring, timeout=5000)
    f.setText("junk")
    f.editingFinished.emit()
    assert f.text() == "-60 dB"
    win.fields["freq_tol_rel"].setText("0.5")           # в процентах
    win.fields["freq_tol_rel"].editingFinished.emit()
    assert win.monitor.th.freq_tol_rel == pytest.approx(0.005)
    win2 = MainWindow(settings, ActivityMonitor(links))   # из ini
    qtbot.addWidget(win2)
    assert win2.tabs.currentIndex() == 1 and win2.monitor.th.vna_refl_db == -60.0 and win2.fields["vna_refl_db"].text() == "-60 dB"
