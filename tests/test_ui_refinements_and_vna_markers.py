"""Регрессионные тесты оформления, тем, кнопок панелей, компактности PM/SA, спинбоксов, SincEmblem и PSU."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QRectF, QSettings
from PyQt6.QtGui import QColor, QEnterEvent, QImage, QPainter
from PyQt6.QtWidgets import QAbstractSpinBox, QSizePolicy

from conftest import attach
from visalab.coord import ActivityMonitor, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front import (GraphsWindow, InstrumentsWindow, PowerMeterWindow, PSUWindow, RFGenWindow, SincEmblem,
                           SpectrumWindow)
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


def test_panel_line_input_priority_and_no_arrow_on_pm_follow(qtbot, tmp_path):
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
    s = QSettings(str(tmp_path / "psu_pwr.ini"), QSettings.Format.IniFormat)
    p = _proxy("it6300")
    w = PSUWindow(p, s)
    qtbot.addWidget(w)
    try:
        with qtbot.waitSignal(p.sync_done, timeout=5000):
            p.open()

        p.set("ch2.voltage", 0.5)
        p.set("ch2.current", 1.0)
        p.set("ch2.output", True)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "500 mV", timeout=5000)
        qtbot.waitUntil(lambda: w.tiles[2].fields["P"].text() == "25 mW", timeout=5000)

        p.set("ch2.output", False)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "0 mV", timeout=5000)

        v2_edit = w.setpoint_fields[2]["V"]
        v2_edit.set_prefix("m")
        v2_edit.setText("10000")
        v2_edit.setModified(True)
        v2_edit.returnPressed.emit()
        qtbot.waitUntil(lambda: p.value("ch2.voltage") == pytest.approx(10.0), timeout=5000)
        p.set("ch2.current", 2.0)
        p.set("ch2.output", True)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "10 V", timeout=5000)
        qtbot.waitUntil(lambda: w.tiles[2].fields["P"].text() == "10 W", timeout=5000)

        p.set("ch2.output", False)
        qtbot.waitUntil(lambda: w.tiles[2].fields["V"].text() == "0 V", timeout=5000)

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


def test_light_and_dark_theme_live_switch(qtbot, tmp_path):
    """По умолчанию активна светлая тема; кнопка DARK/LIGHT переключает тему на лету по всем окнам
    и сохраняет выбор в QSettings."""
    from visalab.front import theme

    s = QSettings(str(tmp_path / "theme.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_gen = _proxy("e8267d")
    links.add(p_gen)
    w_gen = RFGenWindow(p_gen, s)
    iw = InstrumentsWindow(s, instruments=("GEN",))
    iw.attach_instrument("GEN", w_gen)
    gw = GraphsWindow(s, ActivityMonitor(links), instruments_window=iw)
    qtbot.addWidget(iw)
    qtbot.addWidget(gw)
    try:
        assert theme.current_theme() == "light"
        assert QColor(theme.UI_COLOR_BACKGROUND).lightness() > 220
        assert gw.theme_button.text() == "DARK"
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in gw.styleSheet()

        gw.theme_button.click()
        assert theme.current_theme() == "dark"
        assert QColor(theme.UI_COLOR_BACKGROUND).lightness() < 60
        assert gw.theme_button.text() == "LIGHT"
        assert s.value("MAIN/THEME") == "dark"
        assert theme.DARK_COLORS["UI_COLOR_BACKGROUND"] in gw.styleSheet()
        assert theme.DARK_COLORS["UI_COLOR_BACKGROUND"] in iw.styleSheet()

        gw.theme_button.click()
        assert theme.current_theme() == "light"
        assert s.value("MAIN/THEME") == "light"
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in gw.styleSheet()
        assert theme.LIGHT_COLORS["UI_COLOR_BACKGROUND"] in iw.styleSheet()
    finally:
        theme.apply_theme("light")
        gw.cleanup()
        gw.close()
        iw.cleanup()
        iw.close()
        p_gen.shutdown()


def test_dark_theme_indicators_panel_icons_hover_and_uniform_fonts(qtbot, tmp_path):
    from visalab.front import params as params_mod
    from visalab.front import theme
    from visalab.front.favorites import FavoritesPopup
    from visalab.front.plot_tools import _SETTINGS_STYLE
    from visalab.front.psu import SourceTile
    from visalab.front.window import IconButtonBase, PanelClearButton, PanelCloseButton, PanelSettingsButton

    for sheet in (FavoritesPopup.STYLE_SHEET, SourceTile.STYLE, _SETTINGS_STYLE):
        assert "9pt" not in sheet
        assert "13pt" not in sheet
        assert "border-radius: 4px" not in sheet
        assert "min-height: 22px" not in sheet

    s = QSettings(str(tmp_path / "dark_icons.ini"), QSettings.Format.IniFormat)
    links = ProxyLinks()
    p_sa = _proxy("e4446a")
    links.add(p_sa)
    w_sa = SpectrumWindow(p_sa, s)
    gw = GraphsWindow(s, ActivityMonitor(links))
    qtbot.addWidget(w_sa)
    qtbot.addWidget(gw)
    try:
        card = gw.card("SA")
        assert isinstance(card.clear_button, IconButtonBase) and isinstance(card.clear_button, PanelClearButton)
        assert isinstance(card.settings_button, IconButtonBase) and isinstance(card.settings_button, PanelSettingsButton)

        gw.set_theme("dark")
        assert theme.current_theme() == "dark"
        assert params_mod.UI_COLOR_INDICATOR == theme.DARK_COLORS["UI_COLOR_INDICATOR"]

        img = QImage(40, 28, QImage.Format.Format_ARGB32)
        img.fill(QColor(theme.DARK_COLORS["UI_COLOR_BACKGROUND"]))
        painter = QPainter(img)
        params_mod.paint_indicator(painter, QRectF(0.5, 0.5, 39.0, 27.0))
        painter.end()
        px_col = img.pixelColor(20, 26)
        assert px_col.name().lower() == theme.DARK_COLORS["UI_COLOR_INDICATOR"].lower()

        assert theme.DARK_COLORS["UI_COLOR_BORDER_LIGHT"] in w_sa.favorites._empty_label.styleSheet()

        dark_inv = QColor(theme.DARK_COLORS["UI_COLOR_TEXT_INVERSE"])
        dark_muted = QColor(theme.DARK_COLORS["UI_COLOR_TEXT_MUTED"])
        extra_set_btn = PanelSettingsButton()
        extra_cls_btn = PanelCloseButton()
        qtbot.addWidget(extra_set_btn)
        qtbot.addWidget(extra_cls_btn)
        for btn in (extra_set_btn, card.clear_button, extra_cls_btn):
            assert btn._icon_color == dark_muted
            btn.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            assert btn._icon_color == dark_inv
            btn.leaveEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            assert btn._icon_color == dark_muted

        extra_set_btn.setChecked(True)
        assert extra_set_btn._icon_color == dark_inv
        extra_set_btn.setChecked(False)
        assert extra_set_btn._icon_color == dark_muted
    finally:
        theme.apply_theme("light")
        w_sa.cleanup()
        w_sa.close()
        gw.cleanup()
        gw.close()
        p_sa.shutdown()
