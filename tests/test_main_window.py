"""Главное окно графиков (``GraphsWindow``), окно инструментов (``InstrumentsWindow``), ``run_main`` и ``ActivityMonitor``."""
import math

import pytest
from PyQt6.QtCore import QPointF, QSettings
from PyQt6.QtGui import QMouseEvent

from conftest import sim_of, worker
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks, Thresholds
from visalab.coord.activity import (Source, _trace_has_signal, magnitude_db, match_source, short_name,
                                    slot_magnitude_db, spectrum_peak, vna_signal)
from visalab.core.trace import Trace
from visalab.drivers import DRIVERS
from visalab.front import GraphsWindow, InstrumentsWindow, RFGenWindow
from visalab.front.live_plot import LivePlot, nice_range
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


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
    assert vna_signal("S21", [-80.0, -1.0, -80.0], -40, -3)
    assert not vna_signal("S21", [-100.0] * 3, -40, -3)
    assert vna_signal("S11", [-0.1, -25.0, -0.1], -40, -3)
    assert not vna_signal("S11", [-0.01] * 3, -40, -3)
    cplx = Trace.linear(1, 2, [0.5, 0.5], im=[0.0, 0.0], meta={"format": "SMITH"})
    assert slot_magnitude_db(cplx)[0] == pytest.approx(20 * math.log10(0.5))
    assert slot_magnitude_db(Trace.linear(1, 2, [10.0, 20.0], meta={"format": "PHASE"})) is None
    assert slot_magnitude_db(Trace.linear(1, 2, [1.0, 3.0], meta={"format": "SWR"}))[1] == pytest.approx(-6.02, abs=0.01)
    assert magnitude_db(cplx).y_unit == "dB"


def test_nice_range_contains_data():
    lo, hi, step = nice_range(-23.4, -11.2, 6)
    assert lo <= -23.4 and hi >= -11.2 and step in (2.0, 5.0)
    assert nice_range(5.0, 5.0, 6)[0] < 5.0 < nice_range(5.0, 5.0, 6)[1]
    assert nice_range(-120.0, -5.0, 6) == (-120.0, 0.0, 20.0)


def test_sim_bench_feeds_analyzer_and_meter():
    gen_cls, sa_cls, pm_cls = DRIVERS["e8267d"][1], DRIVERS["e4446a"][1], DRIVERS["u2021xa"][1]
    gen, sa, pm = gen_cls(), sa_cls(), pm_cls()
    bench = SimBench([gen], [sa], [pm], loss_db=10.0)
    bench.step()
    assert sa.signals == [] and pm.signal["on_dbm"] == -120.0
    gen.s.update({"rf.output": True, "rf.frequency": 2e9, "rf.power": 0.0})
    bench.step()
    assert sa.signals == [(2e9, -10.0)] and pm.signal["on_dbm"] == pytest.approx(-10.0) and pm.signal["width"] is None


def test_short_names_number_same_class():
    class P:
        def __init__(self, prof):
            self.profile = type("Prof", (), {"name": prof})()
    a, b, c = P("signal_generator"), P("signal_generator"), P("power_meter")
    assert [short_name(p, [a, b, c]) for p in (a, b, c)] == ["GEN", "GEN2", "PM"]


def test_activity_fast_vna_signal_matches_reference():
    cases = [
        ("S21", Trace.linear(1, 2, [-80.0, -10.0, -80.0], meta={"format": "MLOG"})),
        ("S21", Trace.linear(1, 2, [-80.0, -60.0], meta={"format": "MLOG"})),
        ("S11", Trace.linear(1, 2, [0.99, 0.1, 0.99], meta={"format": "LIN_MAG"})),
        ("S11", Trace.linear(1, 2, [1.05, 2.0, 10.0], meta={"format": "SWR"})),
        ("S21", Trace.linear(1, 2, [0.001, 0.5], im=[0.0, 0.0], meta={"format": "POLAR"})),
        ("S11", Trace.linear(1, 2, [0.95, 0.2], im=[0.0, 0.0], meta={"format": "SMITH"})),
    ]
    for sp, tr in cases:
        mag = slot_magnitude_db(tr)
        assert _trace_has_signal(tr, sp, -40.0, -3.0) == vna_signal(sp, mag, -40.0, -3.0)


def test_live_plot_decimation_cache_and_cursor(qtbot):
    from PyQt6.QtCore import QEvent, Qt
    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.resize(400, 220)
    plot.show()
    xs = tuple(1e9 + i * 1e6 for i in range(4000))
    ys = tuple(-60.0 + (40.0 if i == 1234 else 0.0) for i in range(4000))
    plot.set_traces([("S21", Trace(xs, ys, "Hz", "dB"))])
    plot.grab()
    assert plot._cache is not None
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(200, 100), QPointF(200, 100),
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    plot.mouseMoveEvent(ev)
    plot.grab()


# ------------------------------------------------------------------ монитор и новые окна на симуляторах
def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


@pytest.fixture
def lab(qtbot):
    """VNA + SA + PM + GEN на симуляторах, свой реестр, стенд GEN -> SA/PM (10 dB потерь)."""
    links = ProxyLinks()
    proxies = {k: _proxy(k) for k in ("pna", "e4446a", "u2021xa", "e8267d")}
    sa_sim = sim_of(proxies["e4446a"])
    sa_sim.c.update(center=1e9, span=100e6)
    sa_sim._couple()
    bench = SimBench([sim_of(proxies["e8267d"])], [sa_sim], [sim_of(proxies["u2021xa"])])
    bench.step()
    for p in proxies.values():
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    yield links, proxies, bench
    for p in proxies.values():
        p.shutdown()


def test_monitor_finds_who_measures_and_the_source(qtbot, lab):
    links, px, bench = lab
    mon = ActivityMonitor(links)
    try:
        assert [mon.name(p) for p in mon.receivers()] == ["VNA", "SA", "PM"]
        vna, sa, pm, gen = px["pna"], px["e4446a"], px["u2021xa"], px["e8267d"]
        qtbot.waitUntil(
            lambda: mon.activity(vna).measuring and mon.activity(sa).measuring and mon.activity(pm).measuring,
            timeout=10000,
        )
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
        rows = mon.pm_table(pm)
        assert len(rows) == 1 and rows[0][0] == pytest.approx(pm.value("freq")) and rows[0][1] == pytest.approx(b.level, abs=0.5)
        mon.clear_pm_table(pm)
        assert mon.pm_table(pm) == []
    finally:
        mon.stop()


def test_graphs_window_live_receivers_promote_detach_and_persist(qtbot, lab, tmp_path):
    links, px, bench = lab
    ini = tmp_path / "main.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    mon = ActivityMonitor(links)
    gw = GraphsWindow(settings, mon)
    qtbot.addWidget(gw)
    gw.show()
    qtbot.waitExposed(gw)
    try:
        assert gw.main_card.graph_name == "VNA"
        assert set(gw.side_cards) == {"SA", "PM"}
        assert gw.emblem.isVisible()
        assert gw.card("SA").controls_bar.isHidden()

        # Ждём живые трассы VNA и SA
        qtbot.waitUntil(lambda: len(gw.card("VNA").plot.series) >= 1, timeout=10000)
        qtbot.waitUntil(lambda: len(gw.card("SA").plot.series) == 1, timeout=10000)

        # Включаем генератор -> на одном графике PM одновременно и мощность от частоты (Hz), и от времени (s)
        gen = px["e8267d"]
        gen.set("rf.frequency", 1e9)
        gen.set("rf.power", -5.0)
        gen.set("rf.output", True)
        qtbot.waitUntil(lambda: gen.value("rf.output") is True, timeout=5000)
        bench.step()
        qtbot.waitUntil(
            lambda: {s.x_unit for s in gw.card("PM").plot.series} >= {"Hz", "s"},
            timeout=10000,
        )
        assert any(s.x_unit == "Hz" and "-5 dBm" in s.name for s in gw.card("PM").plot.series)
        assert any(s.x_unit == "s" for s in gw.card("PM").plot.series)

        # Открепление карточки графика и возврат обратно
        gw.detach_card("PM")
        assert gw.is_detached("PM")
        gw.dock_card("PM", slot=0)
        assert not gw.is_detached("PM")
        assert gw.graph_order()[0] == "PM"

        # Перевод SA в главный график + сохранение в QSettings
        gw.set_main_graph("SA")
        assert gw.main_card.graph_name == "SA"
        assert "VNA" in gw.side_cards
        gw.cleanup()
        gw.close()

        gw2 = GraphsWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
        qtbot.addWidget(gw2)
        try:
            assert gw2.main_card.graph_name == "SA"
            assert gw2.graph_order()[0] == "PM"
        finally:
            gw2.cleanup()
            gw2.close()
    finally:
        if not gw.isHidden():
            gw.cleanup()
            gw.close()
        mon.stop()


def test_run_main_launcher_opens_graphs_and_instruments_windows(qtbot, tmp_path, monkeypatch):
    import run_main
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    built = {}
    orig_setup = run_main.setup

    def track_setup(opened, args, settings):
        res = orig_setup(opened, args, settings)
        built.update(res)
        return res

    monkeypatch.setattr(run_main, "setup", track_setup)
    from visalab.coord import LINKS
    before = set(LINKS.proxies())
    assert run_main.main(["--sim", "--ini", str(tmp_path / "x.ini"), "--no-connect", "--only", "pna,e8267d,psu"]) == 0
    new = [p for p in LINKS.proxies() if p not in before]
    try:
        gw = built["graphs"]
        iw = built["instruments"]
        assert isinstance(gw, GraphsWindow)
        assert isinstance(iw, InstrumentsWindow)
        assert set(iw.cards) == {"VNA", "GEN", "PSU"}
        assert gw.main_card.graph_name == "VNA"
        assert "PSU CH1" in gw.all_cards
        assert gw.emblem.isVisible()
        # Управление видимостью InstrumentsWindow
        gw.set_instruments_visible(False)
        assert not iw.isVisible()
        gw.set_instruments_visible(True)
        assert iw.isVisible()
        # Закрытие InstrumentsWindow по × скрывает его (не убивая и не мешая Cmd+Q)
        iw.close()
        assert not iw.isVisible()
        gw.set_instruments_visible(True)
        assert iw.isVisible()
        # Системное закрытие всех окон (Cmd+Q / Close all) не блокируется event.ignore()
        gw.quit_on_close = False
        app = QApplication.instance()
        app.closeAllWindows()
        assert not gw.isVisible()
        assert not iw.isVisible()
    finally:
        built["graphs"].quit_on_close = False
        built["graphs"].cleanup()
        built["graphs"].close()
        built["instruments"].hide_on_close = False
        built["instruments"].cleanup()
        built["instruments"].close()
        built["monitor"].stop()
        for p in new:
            p.shutdown()


def test_delay_on_address_page(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "d.ini"), QSettings.Format.IniFormat)
    proxy = _proxy("e8267d")
    try:
        win = RFGenWindow(proxy, settings)
        qtbot.addWidget(win)
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        win.delay_edit.setText("25m")
        win.delay_edit.returnPressed.emit()
        qtbot.waitUntil(lambda: worker(proxy).command_delay_s == pytest.approx(0.025), timeout=5000)
        assert settings.value(f"{win.CONNECTION_GROUP}/DELAY", type=float) == pytest.approx(0.025)
        win.cleanup()
    finally:
        proxy.shutdown()


def test_right_settings_no_spinboxes_favorites_spinboxes_and_button_line_order(qtbot, tmp_path):
    """Правое меню настроек и поиск не содержат спинбоксов (только PLineEdit);
    в избранном числовые поля становятся PSpinBox; пары (edit, button) идут как (button, edit)."""
    from PyQt6.QtWidgets import QAbstractButton, QLineEdit
    from visalab.front import InstrumentsWindow, PowerMeterWindow, SpectrumWindow
    from visalab.front.params import PLineEdit, PSpinBox, PSpinEdit

    settings = QSettings(str(tmp_path / "inst_ui.ini"), QSettings.Format.IniFormat)
    iw = InstrumentsWindow(settings=settings)
    qtbot.addWidget(iw)
    p_pm = _proxy("u2021xa")
    p_sa = _proxy("e4446a")
    p_gen = _proxy("e8267d")
    try:
        pm_win = PowerMeterWindow(p_pm, settings)
        sa_win = SpectrumWindow(p_sa, settings)
        gen_win = RFGenWindow(p_gen, settings)
        iw.attach_instrument("PM", pm_win)
        iw.attach_instrument("SA", sa_win)
        iw.attach_instrument("GEN", gen_win)

        # 1. В правом меню настроек нет ни одного PSpinBox / PSpinEdit
        for name in ("PM", "SA", "GEN"):
            view = iw.settings_views[name]
            assert not view.findChildren(PSpinBox), f"{name}: found PSpinBox in right settings"
            assert not view.findChildren(PSpinEdit), f"{name}: found PSpinEdit in right settings"

        # 2. В результатах поиска справа также нет спинбоксов
        iw.search_edit.setText("FREQ")
        assert not iw.search_view.findChildren(PSpinBox)
        assert not iw.search_view.findChildren(PSpinEdit)
        iw.search_edit.clear()

        # 3. Закрепление числового параметра из правого меню в избранное превращает его в PSpinBox
        freq_item = next(it for it in pm_win.favorites.items if not it.is_default_top and "freq" in it.keys)
        pm_win.favorites.set_enabled(freq_item.item_id, True)
        pinned = pm_win.favorites.pinned_lines()
        assert len(pinned) == 1
        assert any(isinstance(w, PSpinBox) for w in pinned[0].widgets)
        assert not any(isinstance(w, PLineEdit) for w in pinned[0].widgets)

        # 4. Порядок button -> line в строках с кнопкой и полем ввода
        freq_ln = next(ln for ln in pm_win.pages[0].lines if ln.name == "FREQ" or "freq" in [getattr(w, "key", "") for w in ln.widgets])
        assert isinstance(freq_ln.widgets[0], QAbstractButton) and isinstance(freq_ln.widgets[-1], QLineEdit)
        lvl_ln = next(ln for ln in pm_win.pages[1].lines if "trig.level" in [getattr(w, "key", "") for w in ln.widgets])
        assert isinstance(lvl_ln.widgets[0], QAbstractButton) and isinstance(lvl_ln.widgets[-1], QLineEdit)
        pow_top = next(ln for ln in gen_win.top_lines if ln.name == "POW")
        assert isinstance(pow_top.widgets[0], QAbstractButton) and isinstance(pow_top.widgets[-1], PSpinBox)
        rbw_ln = next(ln for ln in sa_win.pages[1].lines if "bw.res" in [getattr(w, "key", "") for w in ln.widgets])
        assert isinstance(rbw_ln.widgets[0], QAbstractButton) and isinstance(rbw_ln.widgets[-1], QLineEdit)
    finally:
        iw.hide_on_close = False
        iw.cleanup()
        iw.close()
        for p in (p_pm, p_sa, p_gen):
            p.shutdown()
