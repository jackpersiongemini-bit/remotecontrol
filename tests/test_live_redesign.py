"""Тесты единого ``PlotWidget``, математики ``plot_tools`` и двухпанельного ``GraphsWindow``."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QSettings

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.core.trace import Trace
from visalab.drivers import DRIVERS
from visalab.front import GraphsWindow, PlotSeries, PlotWidget
from visalab.front.live_plot import LivePlot
from visalab.front.plot_tools import (
    PlotSettingsWidget,
    eval_math_expr,
    parse_x_range_expr,
    trace_math,
    validate_math_expr,
)
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


def _proxy(kind: str) -> Instrument:
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


def test_x_range_expr_parser():
    rs = parse_x_range_expr("[1G,3G], [4G,5G]", "Hz")
    assert rs.contains(2e9) and rs.contains(4.5e9)
    assert not rs.contains(3.5e9)
    empty = parse_x_range_expr("", "Hz")
    assert empty.is_empty and empty.contains(12345.0)


def test_trace_math_operations_and_live_plot_math(qtbot, tmp_path):
    a = Trace((1e9, 2e9, 3e9), (10.0, 20.0, 30.0), "Hz", "dB", name="S21")
    b = Trace((1e9, 2e9, 3e9), (2.0, 4.0, 5.0), "Hz", "dB", name="S21")

    assert trace_math(a, b, "SUB").y == pytest.approx((8.0, 16.0, 25.0))
    assert trace_math(a, b, "ADD").y == pytest.approx((12.0, 24.0, 35.0))
    assert trace_math(a, b, "DIV").y == pytest.approx((5.0, 5.0, 6.0))
    assert trace_math(a, b, "MUL").y == pytest.approx((20.0, 80.0, 150.0))
    assert trace_math(a, b, "EXPR", "y * 2 - mem").y == pytest.approx((18.0, 36.0, 55.0))

    assert validate_math_expr("y + 3")[0]
    assert validate_math_expr("10**(y/10)")[0]
    assert not validate_math_expr("__import__('os').system('echo hi')")[0]
    assert eval_math_expr("y - mem + 1", 1e9, 10.0, 4.0) == pytest.approx(7.0)

    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.resize(540, 300)
    plot.show()
    assert plot._font.pointSizeF() >= 10.0

    plot.set_traces([("1·S21", a)])
    plot.store_memory()
    plot.set_traces([("1·S21", Trace((1e9, 2e9, 3e9), (12.0, 24.0, 35.0), "Hz", "dB", name="S21"))])
    plot.set_math("ADD")
    eff, _yt = plot._effective_traces()
    assert eff[0][1].y == pytest.approx((22.0, 44.0, 65.0))

    w = PlotSettingsWidget(plot)
    qtbot.addWidget(w)
    assert set(w.math_buttons) == {"OFF", "SUB", "ADD", "DIV", "MUL", "EXPR"}
    w.math_expr_edit.setText("y + 100")
    w._apply_math_expr()
    assert plot.math_mode == "EXPR" and plot.math_expr == "y + 100"
    eff2, _yt = plot._effective_traces()
    assert eff2[0][1].y == pytest.approx((112.0, 124.0, 135.0))

    plot._mouse = QPointF(420, 120)
    plot.grab()
    plot._mouse = QPointF(100, 120)
    plot.grab()


def test_plot_widget_markers_limits_math_and_multi_unit(qtbot, tmp_path):
    pw = PlotWidget(empty_text="TEST")
    qtbot.addWidget(pw)
    pw.resize(620, 320)
    pw.show()

    xs = (1e9, 2e9, 3e9, 4e9, 5e9)
    ys = (-10.0, -3.0, 0.0, -3.0, -10.0)
    pw.set_series([PlotSeries("S21", xs, ys)], x_unit="Hz", y_unit="dB")

    # Локальные маркеры (Peak и обычный)
    m_pk = pw.add_extremum_marker(maximum=True)
    assert m_pk is not None
    assert pw.marker_y(m_pk) == pytest.approx(0.0)

    # Локальные пределы + PASS/FAIL
    pw.add_limit("MAX", 1.0)
    assert pw.evaluate_limits() is True
    pw.add_limit("MIN", -5.0)
    assert pw.evaluate_limits() is False

    # Память и математика
    pw.store_memory()
    pw.set_math_mode("SUB")
    pw.grab()


def test_graphs_window_two_pane_promote_and_live_feeds(qtbot, tmp_path):
    links = ProxyLinks()
    proxies = {k: _proxy(k) for k in ("pna", "e4446a", "u2021xa", "e8267d")}
    sa_sim = sim_of(proxies["e4446a"])
    sa_sim.c.update(center=1e9, span=100e6)
    sa_sim._couple()
    bench = SimBench([sim_of(proxies["e8267d"])], [sa_sim], [sim_of(proxies["u2021xa"])], loss_db=10.0)
    for p in proxies.values():
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    try:
        ini = tmp_path / "two_pane.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        mon = ActivityMonitor(links)
        win = GraphsWindow(settings, mon)
        qtbot.addWidget(win)
        win.resize(1100, 720)
        win.show()

        assert win.main_card.graph_name == "VNA"
        assert set(win.side_cards) == {"SA", "PM"}

        gen = proxies["e8267d"]
        gen.set("rf.frequency", 1e9)
        gen.set("rf.power", 0.0)
        gen.set("rf.output", True)
        qtbot.waitUntil(lambda: gen.value("rf.output") is True, timeout=5000)
        bench.step()

        qtbot.waitUntil(lambda: len(win.card("SA").plot.series) == 1, timeout=10000)
        qtbot.waitUntil(lambda: len(win.card("PM").plot.series) >= 1, timeout=10000)

        # Переводим SA в главный монолитный график слева; VNA уходит в правую ленту
        win.set_main_graph("SA")
        assert win.main_card.graph_name == "SA"
        assert "VNA" in win.side_cards

        win.cleanup()
        win.close()
        mon.stop()
    finally:
        for p in proxies.values():
            p.shutdown()
