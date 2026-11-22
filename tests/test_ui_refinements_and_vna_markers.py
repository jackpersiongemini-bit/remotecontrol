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
