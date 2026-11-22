"""Тесты переработанного экрана LIVE:
1) Двухпанельный экран: слева (50%, без скролла) активный прибор по видеоуровню или VNA по умолчанию;
   справа (50%, вертикальный скролл) — остальные выбранные графики и таблицы;
2) Облегчённый заголовок панели: имя, процесс над графиком, иконки настроек / очистки / закрытия;
   настройки открываются вместо графика;
3) MATH (SUB/ADD/DIV/MUL/EXPR) и ANALYSIS доступны на всех графиках (VNA, SA, PM, PSU);
4) Увеличенный шрифт подписей и маркерная прямая не перекрывает числа;
5) Диапазоны ваттметра строкой ``{f1}+{[f2,f3],[f4,5]}-{[f6]}`` и подписи только для min (снизу) и max (сверху)
   мощностей без чисел на промежуточных мощностях (например 12, 16, 17 dBm).
"""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QSettings, Qt
from PyQt6.QtWidgets import QScrollArea

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.core.trace import Trace
from visalab.drivers import DRIVERS
from visalab.front.live_plot import LivePlot
from visalab.front.main_window import MainWindow
from visalab.front.plot_tools import (
    ANALYSIS_KEYS,
    PlotSettingsWidget,
    eval_math_expr,
    make_panel_icon,
    trace_math,
    validate_math_expr,
)
from visalab.front.pm_view import (
    PowerCurvePlot,
    filter_pm_matrix_by_ranges,
    parse_range_expr,
)
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


def _proxy(kind: str) -> Instrument:
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


# ------------------------------------------------------------------ 1. Множества диапазонов {f1}+{[f2,f3],[f4,5]}-{[f6]}
def test_range_set_parser_freq_and_power():
    # Частоты: точка + два интервала - исключённая точка
    rs_f = parse_range_expr("{1G}+{[2G,3G],[4G,5G]}-{[2.5G]}", "freq")
    assert rs_f.contains(1e9)
    assert rs_f.contains(2e9) and rs_f.contains(2.2e9) and rs_f.contains(3e9)
    assert rs_f.contains(4.5e9) and rs_f.contains(5e9)
    assert not rs_f.contains(2.5e9)  # вычтено
    assert not rs_f.contains(1.5e9)
    assert not rs_f.contains(3.5e9)
    assert not rs_f.contains(6e9)

    # Вычитание подинтервала и повторное добавление точки слева направо
    rs_f2 = parse_range_expr("{[1G,5G]}-{[2G,3G]}+{2.5G}", "freq")
    assert rs_f2.contains(1.5e9)
    assert not rs_f2.contains(2.2e9)
    assert rs_f2.contains(2.5e9)
    assert rs_f2.contains(4e9)

    # Мощности (включая отрицательные dBm)
    rs_p = parse_range_expr("{-20}+{[-15,-5],[12,17]}-{[-10],[16]}", "power")
    assert rs_p.contains(-20.0)
    assert rs_p.contains(-15.0) and rs_p.contains(-8.0) and rs_p.contains(-5.0)
    assert not rs_p.contains(-10.0)
    assert rs_p.contains(12.0) and rs_p.contains(17.0)
    assert not rs_p.contains(16.0)
    assert not rs_p.contains(0.0)

    # Пустое выражение пропускает всё
    empty = parse_range_expr("", "freq")
    assert empty.is_empty and empty.contains(12345.0)

    # Ошибки синтаксиса
    for bad in ("{1G", "[1G,2G", "{[1G,2G,3G]}", "{abc}", "{1G}+"):
        with pytest.raises(ValueError):
            parse_range_expr(bad, "freq")

    # Фильтрация матрицы
    cols = [12.0, 16.0, 17.0]
    rows = [
        (1e9, {12.0: 2.0, 16.0: 6.0, 17.0: 7.0}),
        (2.5e9, {12.0: 1.5, 16.0: 5.5, 17.0: 6.5}),
        (4.5e9, {12.0: 1.0, 16.0: 5.0, 17.0: 6.0}),
    ]
    out_c, out_r = filter_pm_matrix_by_ranges(cols, rows, rs_f, parse_range_expr("{12}+{[15,17]}-{[16]}", "power"))
    assert out_c == [12.0, 17.0]
    assert [f for f, _v in out_r] == [1e9, 4.5e9]
    assert list(out_r[0][1].keys()) == [12.0, 17.0]


# ------------------------------------------------------------------ 2. Ваттметр: 12 снизу, 17 сверху, 16 без чисел + анализ/math
def test_pm_curve_min_below_max_above_no_intermediate_numbers_and_analysis(qtbot, tmp_path):
    ini = tmp_path / "pm_plot.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    plot = PowerCurvePlot(unit="dBm")
    plot.bind_settings(settings, "TEST/PM0")
    qtbot.addWidget(plot)
    plot.resize(620, 320)
    plot.show()

    # Увеличенные шрифты подписей
    assert plot._font.pointSizeF() >= 10.0
    assert plot._lbl_font.pointSizeF() >= 9.5

    # Мощности 12, 16, 17 dBm: на графике 12 -> снизу, 17 -> сверху, 16 (промежуточная) -> БЕЗ чисел!
    cols = [12.0, 16.0, 17.0]
    rows = [
        (1e9, {12.0: 2.0, 16.0: 6.0, 17.0: 7.0}),
        (2e9, {12.0: 1.5, 16.0: 5.5, 17.0: 6.5}),
        (3e9, {12.0: 1.0, 16.0: 5.0, 17.0: 6.0}),
    ]
    plot.set_matrix(cols, rows, unit="dBm")
    plot.grab()

    above = [lbl for lbl in plot.point_labels if lbl[0] == "above"]
    below = [lbl for lbl in plot.point_labels if lbl[0] == "below"]
    assert len(above) == 3 and len(below) == 3
    # Сверху только значения от 17 dBm (7.0, 6.5, 6.0); снизу только от 12 dBm (2.0, 1.5, 1.0)
    assert all(any(v in lbl[1] for v in ("7.0", "6.5", "6.0")) for lbl in above)
    assert all(any(v in lbl[1] for v in ("2.0", "1.5", "1.0")) for lbl in below)
    # Промежуточная мощность 16 dBm (6.0? нет, проверим чётко отличным значением 4.3!)
    rows2 = [
        (1e9, {12.0: 1.1, 16.0: 4.3, 17.0: 8.9}),
        (2e9, {12.0: 1.2, 16.0: 4.4, 17.0: 8.8}),
    ]
    plot.set_matrix(cols, rows2, unit="dBm")
    plot.grab()
    all_texts = [lbl[1] for lbl in plot.point_labels]
    assert any("8.9" in t for t in all_texts) and any("1.1" in t for t in all_texts)
    assert not any("4.3" in t or "4.4" in t for t in all_texts)
    for place, _txt, _px, pt_y, lbl_y in plot.point_labels:
        if place == "above":
            assert lbl_y < pt_y
        else:
            assert lbl_y > pt_y

    # Поддержка ANALYSIS и MATH на графике ваттметра!
    plot.set_analysis_metrics(["mean", "min", "max", "pp"])
    joined = " ".join(plot.analysis_lines())
    for tok in ("MEAN", "MIN", "MAX", "P-P"):
        assert tok in joined
    plot.grab()

    assert plot.store_memory()
    plot.set_math("EXPR", "y + 10")
    assert plot.math_mode == "EXPR"
    res = plot.analysis_results()
    assert res["max"] == pytest.approx(18.9, abs=0.05)

    # Hover в правой и левой половинах не падает и рисует бейдж на противоположной от прямой стороне
    plot._mouse = QPointF(500, 140)
    plot.grab()
    plot._mouse = QPointF(100, 140)
    plot.grab()


# ------------------------------------------------------------------ 3. MATH (SUB/ADD/DIV/MUL/EXPR) и LivePlot
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

    # Проверка hover на обеих сторонах (прямая не перекрывает числа)
    plot._mouse = QPointF(420, 120)
    plot.grab()
    plot._mouse = QPointF(100, 120)
    plot.grab()

    # Иконки кнопок заголовка непустые
    for kind in ("settings", "clear", "close"):
        assert not make_panel_icon(kind, 15).isNull()


# ------------------------------------------------------------------ 4. Двухпанельный MainWindow + активный по видеоуровню + VNA fallback
def test_main_window_two_pane_active_by_video_level_and_inline_settings(qtbot, tmp_path):
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
        win = MainWindow(settings, mon)
        qtbot.addWidget(win)
        win.resize(1100, 720)
        win.show()

        vna_p = win.panels[proxies["pna"]]
        sa_p = win.panels[proxies["e4446a"]]
        pm_p = win.panels[proxies["u2021xa"]]

        qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)

        # 1) Генератор выключен (никто не превышает видеоуровень) -> СЛЕВА показывается VNA!
        assert win.active_panel() is vna_p
        assert win.right_panels() == [sa_p, pm_p]
        # Слева нет QScrollArea, справа — вертикальный скролл, без горизонтального
        assert not isinstance(win._active_host, QScrollArea)
        assert win.right_scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert win.right_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

        # 2) Облегчённый заголовок: имя + процесс + только 3 иконки (настройки, clear, close), без нижней строки source
        for panel in (vna_p, sa_p, pm_p):
            assert not hasattr(panel, "source")
            assert not panel.settings_button.icon().isNull()
            assert not panel.clear_button.icon().isNull()
            assert not panel.close_button.icon().isNull()
            # Открытие настроек заменяет сам график внутри панели
            assert panel.stack.currentIndex() == 0
            panel.settings_button.click()
            assert panel.stack.currentIndex() == 1
            panel.settings_button.click()
            assert panel.stack.currentIndex() == 0

        # 3) Включаем генератор на частоте SA (-10 dBm -> на входе SA -20 dBm >= -50 dBm)
        #    Чтобы однозначно проверить выбор SA по видеоуровню, ставим PM порог выше (-5 dBm)
        win.set_panel_level(win._panel_key(proxies["u2021xa"]), -5.0)
        gen = proxies["e8267d"]
        gen.set("rf.frequency", 1e9)
        gen.set("rf.power", -10.0)
        gen.set("rf.output", True)
        qtbot.waitUntil(lambda: gen.value("rf.output") is True, timeout=5000)
        bench.step()

        # SA превышает видеоуровень -> SA автоматически переходит в ЛЕВУЮ половину, а VNA уходит направо!
        qtbot.waitUntil(lambda: win.active_panel() is sa_p, timeout=10000)
        assert vna_p in win.right_panels() and pm_p in win.right_panels()
        assert sa_p not in win.right_panels()

        # 4) Теперь поднимаем порог SA до +10 dBm и опускаем порог PM до -40 dBm -> слева становится PM!
        win.set_panel_level(win._panel_key(proxies["e4446a"]), 10.0)
        win.set_panel_level(win._panel_key(proxies["u2021xa"]), -40.0)
        qtbot.waitUntil(lambda: win.active_panel() is pm_p, timeout=5000)
        assert vna_p in win.right_panels() and sa_p in win.right_panels()

        # 5) Поднимаем порог PM до +10 dBm (никто не превышает видеоуровень) -> снова слева VNA!
        win.set_panel_level(win._panel_key(proxies["u2021xa"]), 10.0)
        assert win.active_panel() is vna_p
        assert sa_p in win.right_panels() and pm_p in win.right_panels()
    finally:
        for p in proxies.values():
            p.shutdown()
