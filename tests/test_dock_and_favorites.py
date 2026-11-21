"""Тесты дока приборов (прикрепление/открепление), настраиваемого избранного и круглых чекбоксов."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QCheckBox

from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front.favorites import FavoritesPopup
from visalab.front.instrument_dock import InstrumentDock
from visalab.front.instrument_window import window_for
from visalab.front.main_window import MainWindow
from visalab.front.params import PLineEdit, PSButton, Panel
from visalab.front.power_meter import PowerMeterWindow
from visalab.front.psu import PSUWindow
from visalab.front.pulse_gen import PulseGenWindow
from visalab.front.rf_gen import RFGenWindow
from visalab.front.spectrum import SpectrumWindow
from visalab.front.theme import checkbox_qss
from visalab.front.vna import VNAWindow
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

    for sheet in (Window.STYLE_SHEET, Panel.STYLE_SHEET, InstrumentDock.STYLE_SHEET, FavoritesPopup.STYLE_SHEET):
        _assert_circle_checkbox_qss(sheet)

    # quick.py stylesheet
    import visalab.quick as quick_mod
    _assert_circle_checkbox_qss(quick_mod.STYLE)

    # MainWindow SET panel
    links = ProxyLinks()
    sa = _proxy("e4446a")
    links.add(sa)
    mon = ActivityMonitor(links)
    mw = MainWindow(settings, mon)
    qtbot.addWidget(mw)
    try:
        _assert_circle_checkbox_qss(mw.settings_panel.styleSheet())
    finally:
        mw.cleanup()
        mw.close()
        sa.shutdown()

    # visalab/gui/window.py poll_log checkbox
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


# ------------------------------------------------------------------ 2. Док приборов
def test_dock_attach_detach_and_tiles(qtbot, settings):
    links = ProxyLinks()
    rf_p = _proxy("e8267d")
    sa_p = _proxy("e4446a")
    links.add(rf_p)
    links.add(sa_p)
    rf_win = RFGenWindow(rf_p, settings)
    sa_win = SpectrumWindow(sa_p, settings)
    qtbot.addWidget(rf_win)
    qtbot.addWidget(sa_win)

    mon = ActivityMonitor(links)
    mw = MainWindow(settings, mon)
    qtbot.addWidget(mw)
    mw.show()
    qtbot.waitExposed(mw)

    try:
        assert not mw.instrument_dock.isVisible()
        assert not rf_win.is_docked and rf_win.isWindow()

        # Прикрепить RFGenWindow в док
        rf_win.toggle_dock()
        qtbot.waitUntil(lambda: rf_win.is_docked and mw.instrument_dock.isVisible(), timeout=3000)
        assert not rf_win.isWindow()
        assert rf_win.dock_button.docked
        assert rf_win.titleBar.minimized_button.isHidden()
        assert window_for(rf_p) is rf_win
        assert mw.tiles[rf_p].window_open
        assert "DOCK (1)" == mw.dock_button.text()

        # Закрытие прикреплённого окна скрывает ТОЛЬКО его, MainWindow остаётся открытым
        rf_win.titleBar.close_button.click()
        qtbot.waitUntil(lambda: rf_win.isHidden(), timeout=3000)
        assert mw.isVisible()
        assert not mw.tiles[rf_p].window_open

        # Клик по плитке снова показывает прикреплённое окно в доке
        mw.open_instrument(rf_p)
        qtbot.waitUntil(lambda: rf_win.isVisible(), timeout=3000)
        assert mw.tiles[rf_p].window_open

        # DOCK ALL прикрепляет и второй прибор
        mw.instrument_dock.dock_all()
        qtbot.waitUntil(lambda: sa_win.is_docked and mw.dock_button.text() == "DOCK (2)", timeout=3000)
        assert window_for(sa_p) is sa_win

        # Открепить RFGenWindow обратно в плавающее окно
        rf_win.toggle_dock()
        qtbot.waitUntil(lambda: not rf_win.is_docked and rf_win.isWindow() and rf_win.isVisible(), timeout=3000)
        assert not rf_win.dock_button.docked
        assert not rf_win.titleBar.minimized_button.isHidden()
        assert mw.dock_button.text() == "DOCK (1)"

        # UNDOCK ALL открепляет оставшийся SA
        mw.instrument_dock.undock_all()
        qtbot.waitUntil(lambda: not sa_win.is_docked and sa_win.isWindow(), timeout=3000)
        assert mw.dock_button.text() == "DOCK"
        assert not mw.instrument_dock.undock_all_button.isEnabled()
    finally:
        rf_win.cleanup()
        sa_win.cleanup()
        rf_win.close()
        sa_win.close()
        mw.cleanup()
        mw.close()
        rf_p.shutdown()
        sa_p.shutdown()


def test_dock_state_persisted_in_settings(qtbot, settings):
    links = ProxyLinks()
    rf_p = _proxy("e8267d")
    links.add(rf_p)
    rf_win = RFGenWindow(rf_p, settings)
    qtbot.addWidget(rf_win)
    mw1 = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(mw1)
    mw1.show()
    qtbot.waitExposed(mw1)

    try:
        rf_win.dock_into(mw1.instrument_dock, show=True, save=True)
        assert rf_win.want_docked()
        # Отцепляем без сохранения, имитируя перезапуск с теми же QSettings
        rf_win.undock(show=True, save=False)
        assert rf_win.want_docked()
        mw1.cleanup()
        mw1.close()

        mw2 = MainWindow(settings, ActivityMonitor(links))
        qtbot.addWidget(mw2)
        mw2.show()
        qtbot.waitExposed(mw2)
        try:
            assert rf_win.is_docked
            assert rf_win._dock is mw2.instrument_dock
            assert mw2.instrument_dock.isVisible()
        finally:
            mw2.cleanup()
            mw2.close()
    finally:
        rf_win.cleanup()
        rf_win.close()
        rf_p.shutdown()


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

        # В списке есть стандартная строка PRST и параметры вкладок (CENT, RBW...)
        ids = {it.item_id: it for it in win.favorites.items}
        top_id = next(i for i, it in ids.items() if it.is_default_top)
        cent_id = next(i for i, it in ids.items() if "freq.center" in it.keys)
        rbw_id = next(i for i, it in ids.items() if "bw.res" in it.keys)

        # Открыть всплывающую панель настройки избранного
        popup = win.show_favorites_editor()
        assert popup.isVisible()
        assert all(isinstance(cb, QCheckBox) for cb in popup.checkboxes.values())
        assert popup.checkboxes[top_id].isChecked()
        assert not popup.checkboxes[cent_id].isChecked()

        # Включить CENT и RBW, выключить стандартный PRST
        popup.checkboxes[cent_id].setChecked(True)
        popup.checkboxes[rbw_id].setChecked(True)
        popup.checkboxes[top_id].setChecked(False)

        assert win.favorites.is_customized()
        assert win.fav_button.customized
        assert win.top_lines[0].isHidden()
        pinned = win.favorites.pinned_lines()
        assert len(pinned) == 2

        # Проверяем живую синхронизацию: изменение на приборе обновляет и закреплённое поле, и вкладку
        cent_pinned = next(w for ln in pinned for w in ln.widgets if getattr(w, "key", None) == "freq.center")
        assert isinstance(cent_pinned, PLineEdit)
        p.set("freq.center", 2.5e9)
        qtbot.waitUntil(lambda: p.value("freq.center") == pytest.approx(2.5e9), timeout=5000)
        qtbot.waitUntil(lambda: "2.5" in cent_pinned.text(), timeout=5000)

        # Проверка восстановления из QSettings при новом открытии окна
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

        # Сброс к умолчаниям и отписка виджетов без ошибок
        popup2 = win.show_favorites_editor()
        popup2.reset_button.click()
        assert not win.favorites.is_customized()
        assert not win.top_lines[0].isHidden()
        assert len(win.favorites.pinned_lines()) == 0
        popup2.close()
        # Сигнал после удаления закреплённых виджетов не падает
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
        # Можно закрепить первый параметр со вкладки и затем сбросить
        win.set_favorite_enabled(params[0].item_id, True)
        assert len(win.favorites.pinned_lines()) == 1
        win.reset_favorites()
        assert len(win.favorites.pinned_lines()) == 0
    finally:
        win.cleanup()
        win.close()
        p.shutdown()
