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
        # Приборная панель — отдельное окно, не встроенное внутрь MainWindow
        assert isinstance(mw.instrument_dock_window, InstrumentDockWindow)
        assert mw.instrument_dock_window.isWindow()
        assert mw.instrument_dock_window.parentWidget() is None
        assert mw.instrument_dock.tabs.count() == 2
        assert [mw.instrument_dock.tabs.tabText(i) for i in range(2)] == ["FAV", "ALL"]
        assert isinstance(mw.instrument_dock._cards_layout, QHBoxLayout)
        assert mw.instrument_dock.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded

        mw.instrument_dock.attach(w_vna, show=True, save=False)
        mw.instrument_dock.attach(w_pm, show=True, save=False)
        mw.set_dock_visible(True)
        qtbot.waitUntil(lambda: mw.instrument_dock_window.isVisible(), timeout=3000)

        # Вкладка 0 (FAV): у каждого прибора видно только избранное
        mw.instrument_dock.set_tab(0)
        assert mw.instrument_dock.view_mode == "fav"
        assert not w_vna.preset_panel.isHidden() and w_vna.parameter_panel.isHidden()
        assert not w_pm.preset_panel.isHidden() and w_pm.parameter_panel.isHidden()

        # Вкладка 1 (ALL): у каждого прибора видны вкладки настроек
        mw.instrument_dock.set_tab(1)
        assert mw.instrument_dock.view_mode == "all"
        assert w_vna.preset_panel.isHidden() and not w_vna.parameter_panel.isHidden()
        assert w_pm.preset_panel.isHidden() and not w_pm.parameter_panel.isHidden()

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
    assert e.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
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
