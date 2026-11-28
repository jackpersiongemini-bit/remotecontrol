"""Тесты ``InstrumentsWindow`` (карточки, настройки, открепление, поиск, звёздочки), настраиваемого избранного и круглых чекбоксов."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QCheckBox

from visalab.coord import Instrument
from visalab.drivers import DRIVERS
from visalab.front import (InstrumentsWindow, PowerMeterWindow, PSUWindow, PulseGenWindow, RFGenWindow, SpectrumWindow,
                           VNAWindow)
from visalab.front.favorites import FavoritesPopup
from visalab.front.params import PLineEdit, Panel
from visalab.front.theme import checkbox_qss
from visalab.front.window import Window
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport


def _proxy(kind: str) -> Instrument:
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


# ------------------------------------------------------------------ 1. Круглые чекбоксы
def _assert_circle_checkbox_qss(sheet: str) -> None:
    assert "QCheckBox::indicator" in sheet
    assert "border-radius: 8px" in sheet
    assert "qradialgradient" in sheet
    for line in sheet.splitlines():
        if "QCheckBox::indicator" in line:
            assert "border-radius: 3px" not in line


def test_circle_in_circle_checkbox_qss_everywhere(qtbot, settings):
    _assert_circle_checkbox_qss(checkbox_qss())

    for sheet in (Window.STYLE_SHEET, Panel.STYLE_SHEET, FavoritesPopup.STYLE_SHEET):
        _assert_circle_checkbox_qss(sheet)

    import visalab.quick as quick_mod
    _assert_circle_checkbox_qss(quick_mod.STYLE)

    from visalab.gui.window import InstrumentWindow as GuiWindow
    p = _proxy("e8267d")
    gw = None
    try:
        gw = GuiWindow(p)
        qtbot.addWidget(gw)
        _assert_circle_checkbox_qss(gw.poll_log.styleSheet())
    finally:
        if gw is not None:
            gw.close()
        p.shutdown()


# ------------------------------------------------------------------ 2. InstrumentsWindow: карточки, настройки, открепление, поиск
def test_instruments_window_attach_settings_detach_and_search(qtbot, settings):
    rf_p = _proxy("e8267d")
    sa_p = _proxy("e4446a")
    rf_win = RFGenWindow(rf_p, settings)
    sa_win = SpectrumWindow(sa_p, settings)

    iw = InstrumentsWindow(settings, instruments=("SA", "GEN"))
    qtbot.addWidget(iw)
    iw.attach_instrument("SA", sa_win)
    iw.attach_instrument("GEN", rf_win)
    iw.show()
    qtbot.waitExposed(iw)

    try:
        assert iw.instrument_order() == ["SA", "GEN"]
        assert iw.selected_instrument is None

        # Открытие настроек прибора в правой половине
        iw.open_settings("SA")
        assert iw.selected_instrument == "SA"
        assert iw.settings_views["SA"].isVisible()
        assert not iw.settings_views["GEN"].isVisible()

        # Закрепление параметра в избранном звёздочкой
        sa_view = iw.settings_views["SA"]
        assert sa_view.fav_stars
        first_iid, star = next(iter(sa_view.fav_stars.items()))
        star.click()
        assert sa_win.favorites.is_enabled(first_iid)
        star.click()
        assert not sa_win.favorites.is_enabled(first_iid)

        # Открепление карточки в отдельное окно и возврат обратно
        iw.detach_card("GEN")
        assert iw.is_detached("GEN")
        assert iw.instrument_order() == ["SA"]
        iw.dock_card("GEN", slot=0)
        assert not iw.is_detached("GEN")
        assert iw.instrument_order() == ["GEN", "SA"]

        # Глобальный поиск настроек
        iw.search_edit.setText("Center")
        assert iw.search_view.isVisible()
        iw.clear_search()
        assert not iw.search_view.isVisible()
        assert iw.settings_views["SA"].isVisible()

        # open_instrument_by_proxy открывает настройки нужного прибора
        assert iw.open_instrument_by_proxy(rf_p)
        assert iw.selected_instrument == "GEN"

        # Переподключение (re-attach) нового окна прибора в тот же слот
        rf_p2 = _proxy("e8267d")
        rf_win2 = RFGenWindow(rf_p2, settings)
        try:
            iw.attach_instrument("GEN", rf_win2)
            assert iw.cards["GEN"].instrument_window is rf_win2
            assert iw.open_instrument_by_proxy(rf_p2)
        finally:
            rf_p2.shutdown()
    finally:
        iw.cleanup()
        iw.close()
        rf_p.shutdown()
        sa_p.shutdown()


# ------------------------------------------------------------------ 3. Настраиваемое избранное
def test_favorites_customize_sync_and_persist(qtbot, settings):
    p = _proxy("e4446a")
    with qtbot.waitSignal(p.sync_done, timeout=10000):
        p.open()
    win = SpectrumWindow(p, settings)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)

    try:
        assert not win.favorites.is_customized()
        assert not win.fav_button.customized
        assert len(win.favorites.pinned_lines()) == 0

        ids = {it.item_id: it for it in win.favorites.items}
        top_id = next(i for i, it in ids.items() if it.is_default_top)
        cent_id = next(i for i, it in ids.items() if "freq.center" in it.keys)
        rbw_id = next(i for i, it in ids.items() if "bw.res" in it.keys)

        popup = win.show_favorites_editor()
        assert popup.isVisible()
        assert all(isinstance(cb, QCheckBox) for cb in popup.checkboxes.values())
        assert popup.checkboxes[top_id].isChecked()
        assert not popup.checkboxes[cent_id].isChecked()

        popup.checkboxes[cent_id].setChecked(True)
        popup.checkboxes[rbw_id].setChecked(True)
        popup.checkboxes[top_id].setChecked(False)

        assert win.favorites.is_customized()
        assert win.fav_button.customized
        assert win.top_lines[0].isHidden()
        pinned = win.favorites.pinned_lines()
        assert len(pinned) == 2

        from visalab.front.params import PSpinBox
        cent_pinned = next(w for ln in pinned for w in ln.widgets if getattr(w, "key", None) == "freq.center")
        assert isinstance(cent_pinned, PSpinBox)
        p.set("freq.center", 2.5e9)
        qtbot.waitUntil(lambda: p.value("freq.center") == pytest.approx(2.5e9), timeout=5000)
        qtbot.waitUntil(lambda: "2.5" in cent_pinned.text(), timeout=5000)

        popup.close()
        win2 = SpectrumWindow(p, settings)
        qtbot.addWidget(win2)
        try:
            assert win2.favorites.is_customized()
            assert win2.top_lines[0].isHidden()
            assert len(win2.favorites.pinned_lines()) == 2
        finally:
            win2.cleanup()
            win2.close()

        popup2 = win.show_favorites_editor()
        popup2.reset_button.click()
        assert not win.favorites.is_customized()
        assert not win.top_lines[0].isHidden()
        assert len(win.favorites.pinned_lines()) == 0
        popup2.close()
        p.set("freq.center", 1.8e9)
        qtbot.waitUntil(lambda: p.value("freq.center") == pytest.approx(1.8e9), timeout=5000)
    finally:
        win.cleanup()
        win.close()
        p.shutdown()


@pytest.mark.parametrize("kind,win_cls", [
    ("e8267d", RFGenWindow),
    ("sdg", PulseGenWindow),
    ("u2021xa", PowerMeterWindow),
    ("pna", VNAWindow),
    ("it6300", PSUWindow),
])
def test_favorites_discover_works_on_every_instrument(qtbot, settings, kind, win_cls):
    p = _proxy(kind)
    win = win_cls(p, settings)
    qtbot.addWidget(win)
    try:
        defaults = [it for it in win.favorites.items if it.is_default_top]
        params = [it for it in win.favorites.items if not it.is_default_top]
        assert len(defaults) >= 1
        assert len(params) >= 1
        win.set_favorite_enabled(params[0].item_id, True)
        assert len(win.favorites.pinned_lines()) == 1
        win.reset_favorites()
        assert len(win.favorites.pinned_lines()) == 0
    finally:
        win.cleanup()
        win.close()
        p.shutdown()
