"""Тесты инструментов графиков: перенос источника под график, настройки шкалы, анализ галочками и MEM."""
from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QPoint, QSettings, Qt
from PyQt6.QtGui import QWheelEvent

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.core.trace import Trace
from visalab.drivers import DRIVERS
from visalab.front.live_plot import LivePlot
from visalab.front.main_window import MainWindow
from visalab.front.plot_tools import (
    ANALYSIS_KEYS,
    PlotSettingsPopup,
    analyze_trace,
    format_analysis,
    trace_diff,
)
from visalab.front.power_meter import CalPlot, TracePlot
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


# ------------------------------------------------------------------ расчёты анализа и DATA-MEM
def test_analyze_trace_all_metrics_and_window():
    # Линейная зависимость y = -10 - 2 * (f_GHz - 1) с известным отклонением нелинейности +0.5 dB в центре
    xs = [1e9, 1.5e9, 2e9, 2.5e9, 3e9]
    ys_lin = [-10.0, -11.0, -12.0, -13.0, -14.0]
    tr_lin = Trace(tuple(xs), tuple(ys_lin), x_unit="Hz", y_unit="dBm")
    st = analyze_trace(tr_lin)
    assert st["mean"] == pytest.approx(-12.0)
    assert st["min"] == pytest.approx(-14.0) and st["min_x"] == pytest.approx(3e9)
    assert st["max"] == pytest.approx(-10.0) and st["max_x"] == pytest.approx(1e9)
    assert st["pp"] == pytest.approx(4.0)
    assert st["flatness"] == pytest.approx(2.0)
    assert st["std"] == pytest.approx(math.sqrt(2.0))
    assert st["rms"] == pytest.approx(math.sqrt((100 + 121 + 144 + 169 + 196) / 5))
    assert st["slope"] == pytest.approx(-2e-9)
    assert st["nonlin"] == pytest.approx(0.0, abs=1e-9)

    # С нелинейностью +0.5 dB на средней точке
    tr_nl = Trace(tuple(xs), (-10.0, -11.0, -11.5, -13.0, -14.0), x_unit="Hz", y_unit="dBm")
    st_nl = analyze_trace(tr_nl)
    assert st_nl["nonlin"] == pytest.approx(0.4, abs=1e-6)

    # Окно по X: только [1e9, 2e9] -> -10, -11, -12
    st_win = analyze_trace(tr_lin, x_range=(1e9, 2e9))
    assert st_win["count"] == 3 and st_win["mean"] == pytest.approx(-11.0) and st_win["pp"] == pytest.approx(2.0)

    # Полоса по -3 дБ: симметричный резонанс с пиком 0 dB на 2 GHz и -3 dB на 1.95 / 2.05 GHz -> BW = 100 MHz
    tr_pk = Trace((1.9e9, 1.95e9, 2.0e9, 2.05e9, 2.1e9), (-6.0, -3.0, 0.0, -3.0, -6.0), x_unit="Hz", y_unit="dB")
    st_pk = analyze_trace(tr_pk)
    assert st_pk["bw3"] == pytest.approx(100e6)

    # Форматирование единиц: для dBm разности (STD, P-P, FLAT, NONLIN) в dB, SLOPE в dB/GHz
    formatted = format_analysis(st, ["mean", "std", "pp", "flatness", "nonlin", "slope"], "Hz", "dBm")
    assert formatted[0] == "MEAN -12.00 dBm"
    assert formatted[1].endswith(" dB") and formatted[2] == "P-P 4.00 dB"
    assert formatted[3] == "FLAT ±2.00 dB" and formatted[4] == "NONLIN 0.00 dB"
    assert formatted[5] == "SLOPE -2.00 dB/GHz"


def test_trace_diff_same_and_interpolated_grids():
    a = Trace((1e9, 2e9, 3e9), (-10.0, -12.0, -14.0), x_unit="Hz", y_unit="dBm", name="S21")
    b = Trace((1e9, 2e9, 3e9), (-10.0, -10.0, -10.0), x_unit="Hz", y_unit="dBm", name="S21")
    d = trace_diff(a, b)
    assert d.y == pytest.approx((0.0, -2.0, -4.0)) and d.y_unit == "dB"

    # Несовпадающая сетка X -> интерполяция b на сетку a
    b_coarse = Trace((1e9, 3e9), (-10.0, -14.0), x_unit="Hz", y_unit="dBm")
    d2 = trace_diff(a, b_coarse)
    assert d2.y == pytest.approx((0.0, 0.0, 0.0))


# ------------------------------------------------------------------ LivePlot: MEM, SCALE, ANALYSIS, persistence
def test_live_plot_double_click_mem_diff_scale_and_popup(qtbot, tmp_path):
    ini = tmp_path / "plot.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    plot = LivePlot()
    plot.bind_settings(settings, "TEST/PLOT0")
    qtbot.addWidget(plot)
    plot.resize(520, 280)
    plot.show()

    tr1 = Trace.linear(1e9, 3e9, [-10.0, -12.0, -11.0, -13.0, -14.0], x_unit="Hz", y_unit="dB")
    tr2 = Trace.linear(1e9, 3e9, [-9.0, -10.0, -11.0, -12.0, -13.0], x_unit="Hz", y_unit="dB")
    plot.set_scale((-50.0, 50.0, 10.0), divs_y=10)
    plot.set_traces([("1·S21", tr1)])

    # 1) Двойной клик сохраняет MEM; Shift+2× обновляет; повторный 2× очищает
    assert not plot.memory
    qtbot.mouseDClick(plot, Qt.MouseButton.LeftButton, pos=QPoint(200, 120))
    assert len(plot.memory) == 1 and plot.memory[0][1] is tr1
    plot.grab()

    plot.set_traces([("1·S21", tr2)])
    # Включаем DATA-MEM -> значения разности tr2 - tr1 = [+1, +2, 0, +1, +1]
    plot.set_memory_diff(True)
    assert plot.memory_diff
    eff, _yt = plot._effective_traces()
    assert eff[0][1].y == pytest.approx((1.0, 2.0, 0.0, 1.0, 1.0))
    plot.grab()

    # Shift + двойной клик обновляет MEM на tr2 -> разность становится 0
    qtbot.mouseDClick(plot, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=QPoint(200, 120))
    assert plot.memory[0][1] is tr2
    eff2, _yt = plot._effective_traces()
    assert eff2[0][1].y == pytest.approx((0.0, 0.0, 0.0, 0.0, 0.0))

    # Повторный двойной клик очищает MEM и выходит из diff
    qtbot.mouseDClick(plot, Qt.MouseButton.LeftButton, pos=QPoint(200, 120))
    assert not plot.memory and not plot.memory_diff

    # 2) Всплывающая панель настроек: галочки анализа и ручная шкала
    popup = plot.open_settings()
    assert isinstance(popup, PlotSettingsPopup)
    for k in ("mean", "min", "max", "std", "pp", "flatness", "nonlin"):
        popup.analysis_checks[k].setChecked(True)
    assert set(plot.analysis_metrics) == {"mean", "min", "max", "std", "pp", "flatness", "nonlin"}
    lines = plot.analysis_lines()
    joined = " ".join(lines)
    for token in ("MEAN", "MIN", "MAX", "STD", "P-P", "FLAT", "NONLIN"):
        assert token in joined
    plot.grab()

    # Ручная шкала, X-зум, скрытие сетки, точки
    popup.ref_edit.setText("0")
    popup.div_edit.setText("2")
    popup.divs_edit.setText("8")
    popup._apply_scale()
    assert plot.manual_scale and plot._scale == pytest.approx((-16.0, 0.0, 2.0)) and plot._divs_y == 8
    plot.set_x_zoom(1.5e9, 2.5e9)
    assert plot._x_bounds() == pytest.approx((1.5e9, 2.5e9))
    popup.grid_cb.setChecked(False)
    popup.points_cb.setChecked(True)
    plot.grab()
    popup.close()

    # 3) Сохранение и восстановление из QSettings
    settings.sync()
    plot2 = LivePlot()
    plot2.bind_settings(QSettings(str(ini), QSettings.Format.IniFormat), "TEST/PLOT0")
    qtbot.addWidget(plot2)
    assert plot2.manual_scale and plot2._scale == pytest.approx((-16.0, 0.0, 2.0)) and plot2._divs_y == 8
    assert plot2.manual_x_range == pytest.approx((1.5e9, 2.5e9))
    assert not plot2.show_grid and plot2.show_points
    assert set(plot2.analysis_metrics) == {"mean", "min", "max", "std", "pp", "flatness", "nonlin"}

    # Reset возвращает AUTO и полный X
    plot2.reset_view()
    assert not plot2.manual_scale and plot2.manual_x_range is None


def test_trace_plot_and_cal_plot_support_mem_scale_and_analysis(qtbot):
    tp = TracePlot()
    qtbot.addWidget(tp)
    tp.resize(420, 220)
    tp.show()
    tr = Trace.linear(0.0, 1e-3, [-20.0, -10.0, -10.0, -20.0], x_unit="s", y_unit="dBm")
    tp.set_trace(tr)

    # Двойной клик -> MEM
    qtbot.mouseDClick(tp, Qt.MouseButton.LeftButton, pos=QPoint(180, 100))
    assert tp.memory is tr
    tp.set_analysis_metrics(["mean", "std", "pp", "flatness"])
    assert any("MEAN" in ln for ln in tp.analysis_lines())
    tp.grab()

    popup = tp.open_settings()
    popup.mem_diff_btn.click()
    assert tp.memory_diff
    tp.grab()
    popup.close()

    # CalPlot также поддерживает ручную шкалу и MEM
    cp = CalPlot()
    qtbot.addWidget(cp)
    cp.resize(360, 180)
    cp.set_points([(1e9, 0.5), (2e9, 1.5), (3e9, 2.5)])
    cp.set_manual_scale(5.0, 1.0)
    assert cp.y_range() == pytest.approx((-1.0, 5.0))
    cp.toggle_memory()
    assert cp.memory is not None
    cp.grab()


# ------------------------------------------------------------------ MainWindow: источник под графиком + кнопки
def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


def test_main_window_source_below_graph_and_mem_scale_controls(qtbot, tmp_path):
    links = ProxyLinks()
    proxies = {k: _proxy(k) for k in ("pna", "e4446a", "u2021xa", "e8267d")}
    sa_sim = sim_of(proxies["e4446a"])
    sa_sim.c.update(center=1e9, span=100e6)
    sa_sim._couple()
    bench = SimBench([sim_of(proxies["e8267d"])], [sa_sim], [sim_of(proxies["u2021xa"])])
    for p in proxies.values():
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    try:
        gen = proxies["e8267d"]
        gen.set("rf.frequency", 1e9)
        gen.set("rf.power", -10.0)
        gen.set("rf.output", True)
        qtbot.waitUntil(lambda: gen.value("rf.output") is True, timeout=5000)
        bench.step()

        ini = tmp_path / "mw_plot.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        win = MainWindow(settings, ActivityMonitor(links))
        qtbot.addWidget(win)
        win.resize(900, 760)
        win.show()
        sa_panel = win.panels[proxies["e4446a"]]
        qtbot.waitUntil(lambda: "dBm" in sa_panel.process_label.text() and not sa_panel.process_label.text().startswith("NOISE"),
                        timeout=10000)

        # Заголовок облегчён: иконки настроек, очистки и закрытия; настройки открываются вместо графика
        assert not sa_panel.settings_button.icon().isNull()
        assert not sa_panel.clear_button.icon().isNull()
        assert not sa_panel.close_button.icon().isNull()
        assert sa_panel.stack.currentIndex() == 0
        sa_panel.settings_button.click()
        assert sa_panel.stack.currentIndex() == 1
        assert not sa_panel.plots[0].memory
        sa_panel.plot_settings_widget.mem_store_btn.click()
        assert bool(sa_panel.plots[0].memory)
        sa_panel.clear_button.click()
        assert not sa_panel.plots[0].memory
        sa_panel.settings_button.click()
        assert sa_panel.stack.currentIndex() == 0

        # Анализ и ручная шкала на панели SA сохраняются и восстанавливаются при перезапуске
        sa_panel.plots[0].set_analysis_metrics(["mean", "min", "max", "flatness", "nonlin"])
        sa_panel.plots[0].set_manual_scale(10.0, 5.0, 10)
        settings.sync()

        win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
        qtbot.addWidget(win2)
        p2 = win2.panels[proxies["e4446a"]].plots[0]
        assert p2.manual_scale and p2._scale == pytest.approx((-40.0, 10.0, 5.0))
        assert set(p2.analysis_metrics) == {"mean", "min", "max", "flatness", "nonlin"}
    finally:
        for p in proxies.values():
            p.shutdown()
