"""Регрессионные тесты: отдельная 2-вкладочная приборная панель (FAV/ALL, горизонтальный скролл),
минимальная ширина окон PM/SA, спинбоксы без стрелок, SincEmblem без рамки и маркеры ВАЦ на графике."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QAbstractSpinBox, QHBoxLayout

from conftest import attach
from visalab.coord import ActivityMonitor, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front import PowerMeterWindow, RFGenWindow, SpectrumWindow, VNAWindow
from visalab.front.instrument_dock import InstrumentDockWindow
from visalab.front.main_window import MainWindow, SincEmblem
from visalab.front.params import PSpinBox
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport


def _proxy(kind: str):
    drv, sim_cls = DRIVERS[kind]
    return attach(InstrumentProxy(drv, SimTransport(sim_cls(), latency_s=0.001)))


def test_pm_and_sa_minimum_width_compact(qtbot, tmp_path):
    s = QSettings(str(tmp_path / "minw.ini"), QSettings.Format.IniFormat)
    for kind, cls in (("u2021xa", PowerMeterWindow), ("e4446a", SpectrumWindow)):
        p = _proxy(kind)
        w = cls(p, s)
        qtbot.addWidget(w)
        try:
            assert w.minimumWidth() <= 400, f"{cls.__name__} minimumWidth={w.minimumWidth()} > 400"
            w.show()
            qtbot.waitExposed(w)
            w.set_expanded(True)
            for i in range(w.tabs.count()):
                w.set_page(i)
                w._fit_height(False)
                assert w.minimumWidth() <= 400
        finally:
            w.cleanup()
            w.close()
            p.shutdown()


def test_separate_dock_window_two_tabs_horizontal(qtbot, tmp_path):
    s = QSettings(str(tmp_path / "dock2.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_vna = _proxy("pna")
    p_pm = _proxy("u2021xa")
    links.add(p_vna)
    links.add(p_pm)
    w_vna = VNAWindow(p_vna, s)
    w_pm = PowerMeterWindow(p_pm, s)
    qtbot.addWidget(w_vna)
    qtbot.addWidget(w_pm)
    mw = MainWindow(s, ActivityMonitor(links))
    qtbot.addWidget(mw)
    try:
        # Приборная панель — отдельное независимое окно с плитками приборов в заголовке и вкладками FAV / SET
        assert isinstance(mw.instrument_dock_window, InstrumentDockWindow)
        assert mw.instrument_dock_window.isWindow()
        assert mw.instrument_dock_window.parentWidget() is None
        assert mw.instrument_dock.tabs.count() == 2
        assert [mw.instrument_dock.tabs.tabText(i) for i in range(2)] == ["FAV", "SET"]
        assert isinstance(mw.instrument_dock._columns_layout, QHBoxLayout)
        assert mw.instrument_dock.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        for pr in (p_vna, p_pm):
            assert mw.instrument_dock_window.titleBar.isAncestorOf(mw.tiles[pr])
            assert not mw.titleBar.isAncestorOf(mw.tiles[pr])

        mw.instrument_dock.attach(w_vna, show=True, save=False)
        mw.instrument_dock.attach(w_pm, show=True, save=False)
        mw.instrument_dock_window.resize(960, 640)
        mw.set_dock_visible(True)
        qtbot.waitUntil(lambda: mw.instrument_dock_window.isVisible(), timeout=3000)
        qtbot.waitUntil(lambda: mw.instrument_dock.column_count == 2, timeout=3000)

        # Вкладка 0 (FAV): у каждого прибора видно ТОЛЬКО избранное (без настроек)
        mw.instrument_dock.set_tab(0)
        assert mw.instrument_dock.view_mode == "fav"
        assert not w_vna.preset_panel.isHidden() and w_vna.parameter_panel.isHidden()
        assert not w_pm.preset_panel.isHidden() and w_pm.parameter_panel.isHidden()

        # Вкладка 1 (SET): все настройки и выбор избранного (★), без дублирования панели избранного
        mw.instrument_dock.set_tab(1)
        assert mw.instrument_dock.view_mode == "all"
        assert w_vna.preset_panel.isHidden() and not w_vna.parameter_panel.isHidden()
        assert w_pm.preset_panel.isHidden() and not w_pm.parameter_panel.isHidden()
        assert not w_vna.fav_button.isHidden() and not w_pm.fav_button.isHidden()

        # Сужаем окно -> адаптивно перестраивается в 1 колонку
        mw.instrument_dock_window.resize(440, 640)
        qtbot.waitUntil(lambda: mw.instrument_dock.column_count == 1, timeout=3000)

        # При откреплении обратно восстанавливаются обе панели
        w_vna.undock(show=False, save=False)
        assert not w_vna.preset_panel.isHidden() and not w_vna.parameter_panel.isHidden()
    finally:
        w_vna.cleanup()
        w_pm.cleanup()
        mw.cleanup()
        mw.close()
        p_vna.shutdown()
        p_pm.shutdown()


def test_sinc_emblem_borderless_and_spinboxes_no_arrows(qtbot, tmp_path):
    s = QSettings(str(tmp_path / "emblem.ini"), QSettings.Format.IniFormat)
    e = SincEmblem()
    qtbot.addWidget(e)
    assert not e.autoFillBackground()
    assert "transparent" in e.styleSheet()
    e.show()
    qtbot.waitExposed(e)
    e.repaint()
    e.stop()

    p = _proxy("e8267d")
    w = RFGenWindow(p, s)
    qtbot.addWidget(w)
    try:
        spins = w.findChildren(PSpinBox)
        assert spins
        for sp in spins:
            assert sp.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    finally:
        w.cleanup()
        w.close()
        p.shutdown()


def test_vna_markers_shown_on_live_plot(qtbot, tmp_path):
    s = QSettings(str(tmp_path / "vna_mk.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_vna = _proxy("pna")
    links.add(p_vna)
    mon = ActivityMonitor(links)
    mw = MainWindow(s, mon)
    qtbot.addWidget(mw)
    try:
        with qtbot.waitSignal(p_vna.sync_done, timeout=5000):
            p_vna.open()
        # Ждём первую трассу
        qtbot.waitUntil(lambda: p_vna.value("trace") is not None, timeout=5000)
        panel = mw.panels[p_vna]
        qtbot.waitUntil(lambda: len(panel.plots) >= 1 and len(panel.plots[0].traces) >= 1, timeout=5000)

        # Включаем маркер 1 на максимум (mark_peak) и маркер 2 на заданной частоте
        p_vna.set("mark.n", 1)
        p_vna.action("mark_peak")
        qtbot.waitUntil(lambda: len(panel.plots[0].markers) >= 1, timeout=5000)
        labels = [m[0] for m in panel.plots[0].markers]
        assert any("M1" in lbl for lbl in labels)

        p_vna.set("mark.n", 2)
        p_vna.set("mark.state", True)
        p_vna.set("mark.x", 1.5e9)
        qtbot.waitUntil(lambda: len(panel.plots[0].markers) >= 2, timeout=5000)
        labels = [m[0] for m in panel.plots[0].markers]
        assert any("M1" in lbl for lbl in labels) and any("M2" in lbl for lbl in labels)

        # Проверяем, что отрисовка графика с маркерами не падает
        mw.show()
        qtbot.waitExposed(mw)
        panel.plots[0].repaint()

        # Выключаем все маркеры -> маркеры исчезают с графика
        p_vna.action("mark_aoff")
        qtbot.waitUntil(lambda: len(panel.plots[0].markers) == 0, timeout=5000)
    finally:
        mw.cleanup()
        mw.close()
        p_vna.shutdown()


def test_panel_line_input_priority_and_no_arrow_on_pm_follow(qtbot, tmp_path):
    """В строках «поле + кнопка» (например FREQ + GEN у PM, POW + RF + PRST у GEN) кнопка имеет
    нулевой приоритет (stretch=0, Fixed), а весь приоритет по ширине у поля ввода; на GEN нет стрелки ←."""
    from PyQt6.QtWidgets import QSizePolicy
    s = QSettings(str(tmp_path / "prio.ini"), QSettings.Format.IniFormat)
    p_pm = _proxy("u2021xa")
    p_gen = _proxy("e8267d")
    w_pm = PowerMeterWindow(p_pm, s)
    w_gen = RFGenWindow(p_gen, s)
    qtbot.addWidget(w_pm)
    qtbot.addWidget(w_gen)
    try:
        freq_line = next(ln for ln in w_pm.pages[0].lines if ln.name == "FREQ")
        freq_spin = w_pm.widgets["freq"]
        folw = w_pm.follow_button
        assert freq_line.line_layout.stretch(freq_line.line_layout.indexOf(freq_spin)) == 1
        assert freq_line.line_layout.stretch(freq_line.line_layout.indexOf(folw)) == 0
        assert folw.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
        assert "←" not in folw.text()

        w_pm.show()
        w_pm.set_expanded(True)
        qtbot.waitExposed(w_pm)
        assert freq_spin.width() > folw.width() * 2

        # И в строке POW у генератора: спинбокс stretch=1, кнопки RF/PRST stretch=0
        pow_line = next(ln for ln in w_gen.top_lines if ln.name == "POW")
        assert pow_line.line_layout.stretch(pow_line.line_layout.indexOf(w_gen.power_spin)) == 1
        assert pow_line.line_layout.stretch(pow_line.line_layout.indexOf(w_gen.prst_button)) == 0
    finally:
        w_pm.cleanup()
        w_gen.cleanup()
        w_pm.close()
        w_gen.close()
        p_pm.shutdown()
        p_gen.shutdown()


def test_psu_voltage_units_channel_power_and_total_power(qtbot, tmp_path):
    """PSU: 500 mV -> '500 mV', 10 000 mV -> '10 V', при уставке в вольтах - вольты;
    отображается мощность каждого канала (P) и суммарная мощность (TOTAL)."""
    from visalab.front import PSUWindow
    s = QSettings(str(tmp_path / "psu_pwr.ini"), QSettings.Format.IniFormat)
    p = _proxy("it6300")
    w = PSUWindow(p, s)
    qtbot.addWidget(w)
    try:
        with qtbot.waitSignal(p.sync_done, timeout=5000):
            p.open()

        # 500 mV на CH2 (10 Ом нагрузка -> 50 mA, 25 mW)
        p.set("ch2.voltage", 0.5)
        p.set("ch2.current", 1.0)
        p.set("ch2.output", True)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "500 mV", timeout=5000)
        qtbot.waitUntil(lambda: w.tiles[2].fields["P"].text() == "25 mW", timeout=5000)

        # Выключили выход при уставке 500 mV -> остаётся в mV ('0 mV')
        p.set("ch2.output", False)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "0 mV", timeout=5000)

        # Ввод 10000 в единицах mV в настройках CH -> должно показывать 10 V!
        v2_edit = w.setpoint_fields[2]["V"]
        v2_edit.set_prefix("m")
        v2_edit.setText("10000")
        v2_edit.setModified(True)
        v2_edit.returnPressed.emit()
        qtbot.waitUntil(lambda: p.value("ch2.voltage") == pytest.approx(10.0), timeout=5000)
        qtbot.waitUntil(lambda: v2_edit.unit_button.text() == "V" and v2_edit.text() == "10", timeout=5000)
        p.set("ch2.current", 2.0)
        p.set("ch2.output", True)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "10 V", timeout=5000)
        # 10 V на 10 Ом -> 1 A, 10 W
        qtbot.waitUntil(lambda: w.tiles[2].fields["P"].text() == "10 W", timeout=5000)

        # При выключении канала с уставкой 10 V -> показывает '0 V' (в вольтах!)
        p.set("ch2.output", False)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "0 V", timeout=5000)

        # Включаем два канала: CH2 = 5 V (2.5 W) и CH3 = 5 V (2.5 W) -> TOTAL = 5 W
        p.set("ch2.voltage", 5.0)
        p.set("ch2.output", True)
        p.set("ch3.voltage", 5.0)
        p.set("ch3.current", 2.0)
        p.set("ch3.output", True)
        qtbot.waitUntil(lambda: w.total_power == pytest.approx(5.0, abs=0.05), timeout=5000)
        qtbot.waitUntil(lambda: w.total_power_label.text() == "TOTAL 5 W", timeout=5000)
    finally:
        w.cleanup()
        w.close()
        p.shutdown()


def test_vna_limit_lines_read_and_displayed(qtbot, tmp_path):
    """ВАЦ: limit lines (MAX/MIN) считываются с прибора в Trace.meta['limits'] и рисуются на LivePlot
    вместе со статусом PASS/FAIL."""
    from visalab.drivers import KeysightPNA
    from visalab.sim import KeysightPNASim

    s = QSettings(str(tmp_path / "vna_lim.ini"), QSettings.Format.IniFormat)
    sim = KeysightPNASim()
    # Настраиваем limit line прямо на приборе (как оператор с передней панели PNA):
    # тип 1 = MAX (-10 dB на 1..3 ГГц), тип 2 = MIN (-40 dB), тип 0 = OFF (должен игнорироваться)
    sim.meas["CH1_S11_1"]["limit"] = {
        "disp": True,
        "stat": True,
        "segs": [
            (1, 1.0e9, 3.0e9, -10.0, -10.0),
            (2, 1.5e9, 2.5e9, -40.0, -40.0),
            (0, 1.0e9, 2.0e9, 0.0, 0.0),
        ],
    }
    p_vna = attach(InstrumentProxy(KeysightPNA, SimTransport(sim, latency_s=0.001)))
    links = ProxyLinks()
    links.add(p_vna)
    mw = MainWindow(s, ActivityMonitor(links))
    qtbot.addWidget(mw)
    try:
        with qtbot.waitSignal(p_vna.sync_done, timeout=5000):
            p_vna.open()
        qtbot.waitUntil(
            lambda: p_vna.value("trace") is not None and len((p_vna.value("trace").meta or {}).get("limits", ())) == 2,
            timeout=5000,
        )
        tr = p_vna.value("trace")
        assert tr.meta["limits"] == (
            ("MAX", 1.0e9, 3.0e9, -10.0, -10.0),
            ("MIN", 1.5e9, 2.5e9, -40.0, -40.0),
        )
        # S11 вне резонанса ~0 dB > -10 dB -> limit test FAIL
        assert tr.meta["limit_test"] is True
        assert tr.meta["limit_fail"] is True

        panel = mw.panels[p_vna]
        qtbot.waitUntil(lambda: len(panel.plots) >= 1 and len(panel.plots[0].limit_lines) == 2, timeout=5000)
        assert panel.plots[0].limit_fail is True

        mw.show()
        qtbot.waitExposed(mw)
        panel.plots[0].repaint()

        # Ставим мягкий предел (MAX +5 dB, MIN -80 dB) -> PASS
        sim.meas["CH1_S11_1"]["limit"]["segs"] = [
            (1, 1.0e9, 3.0e9, 5.0, 5.0),
            (2, 1.0e9, 3.0e9, -80.0, -80.0),
        ]
        p_vna.sync()
        qtbot.waitUntil(lambda: panel.plots[0].limit_fail is False, timeout=5000)
        panel.plots[0].repaint()

        # Выключаем limit lines -> пропадают с графика
        sim.meas["CH1_S11_1"]["limit"]["disp"] = False
        sim.meas["CH1_S11_1"]["limit"]["stat"] = False
        p_vna.sync()
        qtbot.waitUntil(lambda: len(panel.plots[0].limit_lines) == 0 and panel.plots[0].limit_fail is None, timeout=5000)
    finally:
        mw.cleanup()
        mw.close()
        p_vna.shutdown()


def test_light_and_dark_theme_live_switch(qtbot, tmp_path):
    """По умолчанию активна светлая тема; кнопка DARK/LIGHT переключает тему на лету по всем окнам
    и сохраняет выбор в QSettings."""
    from PyQt6.QtGui import QColor
    from visalab.front import theme

    s = QSettings(str(tmp_path / "theme.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_gen = _proxy("e8267d")
    links.add(p_gen)
    w_gen = RFGenWindow(p_gen, s)
    mw = MainWindow(s, ActivityMonitor(links))
    qtbot.addWidget(w_gen)
    qtbot.addWidget(mw)
    try:
        # По умолчанию - светлая тема
        assert theme.current_theme() == "light"
        assert QColor(theme.UI_COLOR_BACKGROUND).lightness() > 220
        assert mw.theme_button.text() == "DARK"
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in mw.styleSheet()

        # Переключаем на тёмную тему на лету
        mw.theme_button.click()
        assert theme.current_theme() == "dark"
        assert QColor(theme.UI_COLOR_BACKGROUND).lightness() < 60
        assert mw.theme_button.text() == "LIGHT"
        assert s.value("MAIN/THEME") == "dark"
        assert theme.DARK_COLORS["UI_COLOR_BACKGROUND"] in mw.styleSheet()
        assert theme.DARK_COLORS["UI_COLOR_BACKGROUND"] in w_gen.styleSheet()

        # Переключаем обратно на светлую тему
        mw.theme_button.click()
        assert theme.current_theme() == "light"
        assert s.value("MAIN/THEME") == "light"
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in mw.styleSheet()
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in w_gen.styleSheet()
    finally:
        theme.apply_theme("light")
        w_gen.cleanup()
        w_gen.close()
        mw.cleanup()
        mw.close()
        p_gen.shutdown()


def test_vna_corr_and_math_apply_per_trace_preserving_limit_lines(qtbot, tmp_path):
    """CORR OFF/ON, CAPTURE и MATH применяются строго к выбранному треку, не ломая остальные
    треки канала и их limit lines; _parse_limit_data поддерживает оба формата (5*N и 1+5*N)."""
    from visalab.drivers import KeysightPNA
    from visalab.sim import KeysightPNASim

    # Проверка обоих форматов CALC:LIM:DATA? (PNA 5*N и ENA/PNA 1+5*N)
    assert KeysightPNA._parse_limit_data("1,1e9,3e9,-10,-10,2,1e9,3e9,-40,-40") == (
        ("MAX", 1e9, 3e9, -10.0, -10.0),
        ("MIN", 1e9, 3e9, -40.0, -40.0),
    )
    assert KeysightPNA._parse_limit_data("2,1,1e9,3e9,-10,-10,2,1e9,3e9,-40,-40") == (
        ("MAX", 1e9, 3e9, -10.0, -10.0),
        ("MIN", 1e9, 3e9, -40.0, -40.0),
    )

    s = QSettings(str(tmp_path / "vna_per_trace.ini"), QSettings.Format.IniFormat)
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = {"s": "S21", "fmt": "MLOG", "ch": 1}
    sim.meas["CH1_S11_1"]["limit"] = {
        "disp": True,
        "stat": True,
        "segs": [(1, 1.0e9, 3.0e9, -10.0, -10.0)],
    }
    p = attach(InstrumentProxy(KeysightPNA, SimTransport(sim, latency_s=0.001)))
    win = VNAWindow(p, s)
    qtbot.addWidget(win)
    try:
        with qtbot.waitSignal(p.sync_done, timeout=5000):
            p.open()
        te = win.trace_editor
        qtbot.waitUntil(lambda: len(te.chips.buttons()) == 2, timeout=5000)

        # Включаем чтение обоих треков и применяем cal set к каналу 1 -> CORR ON на обоих треках
        p.set("m2.enabled", True)
        p.set("cal.set", "CAL_1-3G")
        qtbot.waitUntil(lambda: p.value("cal.state") is True, timeout=5000)

        # Выбираем второй трек (USER_S21) и выключаем CORR -> OFF только у USER_S21!
        te.chips.buttons()[1].click()
        qtbot.waitUntil(lambda: p.value("trace.sel") == "1:USER_S21", timeout=5000)
        p.set("cal.state", False)
        qtbot.waitUntil(lambda: p.value("cal.state") is False, timeout=5000)
        assert sim.meas["USER_S21"].get("corr") is False
        assert sim.meas["CH1_S11_1"].get("corr", sim.c["corr"]) is True

        # Возвращаемся на первый трек (CH1_S11_1) -> у него CORR остался ON, и limit line на месте!
        te.chips.buttons()[0].click()
        qtbot.waitUntil(lambda: p.value("trace.sel") == "1:CH1_S11_1" and p.value("cal.state") is True, timeout=5000)
        qtbot.waitUntil(
            lambda: p.value("m1.trace") is not None and len((p.value("m1.trace").meta or {}).get("limits", ())) == 1,
            timeout=5000,
        )

        # Переходим на USER_S21, нажимаем CAPTURE и ставим MATH = DIV -> применяется ТОЛЬКО к USER_S21!
        te.chips.buttons()[1].click()
        qtbot.waitUntil(lambda: p.value("trace.sel") == "1:USER_S21", timeout=5000)
        win.capture_button.click()
        qtbot.waitUntil(lambda: sim.meas["USER_S21"].get("mem") is not None, timeout=5000)
        assert sim.meas["CH1_S11_1"].get("mem") is None

        win.math_button.set_value("DIV")
        qtbot.waitUntil(lambda: win.math_button.text() == "D/M" and sim.meas["USER_S21"].get("math") == "DIV", timeout=5000)
        assert sim.meas["CH1_S11_1"].get("math", "NORM") == "NORM"

        # Переключаемся обратно на CH1_S11_1 -> MathButton показывает DATA (NORM) этого трека!
        te.chips.buttons()[0].click()
        qtbot.waitUntil(lambda: win.math_button.text() == "DATA", timeout=5000)
    finally:
        win.cleanup()
        win.close()
        p.shutdown()


def test_dark_theme_indicators_panel_icons_hover_and_uniform_fonts(qtbot, tmp_path):
    """Проверка:
    1) В тёмной теме paint_indicator берёт тёмный UI_COLOR_INDICATOR (не светлый),
       а виджеты/шаблоны (FavoritesManager._empty_label, PlotSettingsWidget, InstrumentDock)
       полностью перекрашиваются без остатков светлой темы.
    2) Кнопки панелей (settings / clear / close) — простые IconButtonBase, инвертирующие цвет
       иконки в UI_COLOR_TEXT_INVERSE (чёрный в тёмной теме, белый в светлой) при наведении и выборе.
    3) Шрифт стандартизирован (${UI_FONT_SIZE_PT}pt = 10pt, без 9pt / 13pt).
    """
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QColor, QEnterEvent, QImage, QPainter
    from visalab.front import params as params_mod
    from visalab.front import theme
    from visalab.front.favorites import FavoritesPopup
    from visalab.front.instrument_dock import InstrumentDock
    from visalab.front.psu import SourceTile
    from visalab.front.window import IconButtonBase, PanelClearButton, PanelCloseButton, PanelSettingsButton

    # 1. Стандартизация шрифта: нигде в InstrumentDock / FavoritesPopup / SourceTile нет 9pt или 13pt
    for sheet in (InstrumentDock.STYLE_SHEET, FavoritesPopup.STYLE_SHEET, SourceTile.STYLE):
        assert "9pt" not in sheet
        assert "13pt" not in sheet

    s = QSettings(str(tmp_path / "dark_icons.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_sa = _proxy("e4446a")
    links.add(p_sa)
    w_sa = SpectrumWindow(p_sa, s)
    mw = MainWindow(s, ActivityMonitor(links))
    qtbot.addWidget(w_sa)
    qtbot.addWidget(mw)
    try:
        panel = mw.panels[p_sa]
        for btn, cls in (
            (panel.settings_button, PanelSettingsButton),
            (panel.clear_button, PanelClearButton),
            (panel.close_button, PanelCloseButton),
        ):
            assert isinstance(btn, IconButtonBase) and isinstance(btn, cls)

        # Переключаем в тёмную тему
        mw.set_theme("dark")
        assert theme.current_theme() == "dark"
        assert params_mod.UI_COLOR_INDICATOR == theme.DARK_COLORS["UI_COLOR_INDICATOR"]

        # Проверяем реальную отрисовку paint_indicator в тёмной теме: нижняя полоса = DARK UI_COLOR_INDICATOR
        img = QImage(40, 28, QImage.Format.Format_ARGB32)
        img.fill(QColor(theme.DARK_COLORS["UI_COLOR_BACKGROUND"]))
        painter = QPainter(img)
        params_mod.paint_indicator(painter, QRectF(0.5, 0.5, 39.0, 27.0))
        painter.end()
        px_col = img.pixelColor(20, 26)
        assert px_col.name().lower() == theme.DARK_COLORS["UI_COLOR_INDICATOR"].lower()

        # FavoritesManager._empty_label тоже перекрашен в тёмную тему
        assert theme.DARK_COLORS["UI_COLOR_BORDER_LIGHT"] in w_sa.favorites._empty_label.styleSheet()

        # Наведение на кнопки панелей в тёмной теме инвертирует цвет иконки на чёрный (#222222)
        dark_inv = QColor(theme.DARK_COLORS["UI_COLOR_TEXT_INVERSE"])
        dark_muted = QColor(theme.DARK_COLORS["UI_COLOR_TEXT_MUTED"])
        for btn in (panel.settings_button, panel.clear_button, panel.close_button):
            assert btn._icon_color == dark_muted
            btn.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            assert btn._icon_color == dark_inv
            btn.leaveEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            assert btn._icon_color == dark_muted

        # Выбор (checked) кнопки настроек тоже инвертирует цвет иконки
        panel.settings_button.setChecked(True)
        assert panel.settings_button._icon_color == dark_inv
        panel.settings_button.setChecked(False)
        assert panel.settings_button._icon_color == dark_muted
    finally:
        theme.apply_theme("light")
        w_sa.cleanup()
        w_sa.close()
        mw.cleanup()
        mw.close()
        p_sa.shutdown()
