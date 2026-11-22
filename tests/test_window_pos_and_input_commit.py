"""Тесты сохранения положения неприкреплённых окон и отправки полей только по Enter / stepBy."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPoint, QSettings, QSize, Qt
from PyQt6.QtGui import QFocusEvent, QGuiApplication

from conftest import attach
from visalab.coord import ActivityMonitor, ProxyLinks
from visalab.drivers import DRIVERS, KeysightE8267D, SiglentSDG
from visalab.front import PowerMeterWindow, PulseGenWindow, RFGenWindow
from visalab.front.launch import remember_windows
from visalab.front.main_window import MainWindow
from visalab.front.window import Window
from visalab.instruments import InstrumentProxy
from visalab.sim import KeysightE8267DSim, KeysightU2021XASim, SiglentSDGSim
from visalab.transport import SimTransport


def _focus_out(widget) -> None:
    widget.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason))


def _proxy(drv, sim):
    return attach(InstrumentProxy(drv, SimTransport(sim, latency_s=0.001)))


def test_window_position_persists_across_restart(qtbot, tmp_path):
    ini = str(tmp_path / "pos.ini")
    s1 = QSettings(ini, QSettings.Format.IniFormat)
    avail = QGuiApplication.primaryScreen().availableGeometry()
    target = QPoint(avail.left() + 140, avail.top() + 110)

    w1 = Window(s1)
    qtbot.addWidget(w1)
    w1.recall_size(QSize(320, 220))
    w1.move(target)
    w1.show()
    qtbot.waitExposed(w1)
    moved = w1.pos()
    w1.close()
    s1.sync()

    s2 = QSettings(ini, QSettings.Format.IniFormat)
    w2 = Window(s2)
    qtbot.addWidget(w2)
    w2.recall_size(QSize(320, 220))
    assert w2._pos_restored is True
    assert w2.pos() == moved


def test_window_position_clamped_when_offscreen(qtbot, tmp_path):
    ini = str(tmp_path / "offscreen.ini")
    s = QSettings(ini, QSettings.Format.IniFormat)
    w = Window(s)
    qtbot.addWidget(w)
    s.setValue(w._pos_key, QPoint(99999, 99999))
    w.recall_size(QSize(300, 200))
    avail = QGuiApplication.primaryScreen().availableGeometry()
    assert avail.left() <= w.pos().x() <= avail.right()
    assert avail.top() <= w.pos().y() <= avail.bottom()


def test_instrument_window_position_remembered_on_quit(qtbot, tmp_path):
    ini = str(tmp_path / "inst.ini")
    s1 = QSettings(ini, QSettings.Format.IniFormat)
    avail = QGuiApplication.primaryScreen().availableGeometry()
    target = QPoint(avail.left() + 180, avail.top() + 130)

    p1 = _proxy(KeysightE8267D, KeysightE8267DSim())
    try:
        w1 = RFGenWindow(p1, s1)
        qtbot.addWidget(w1)
        w1.move(target)
        w1.show()
        qtbot.waitExposed(w1)
        saved_pos = w1.pos()
        remember_windows([{"lab_key": "rf", "win": w1, "proxy": p1}], s1)
    finally:
        p1.shutdown()

    s2 = QSettings(ini, QSettings.Format.IniFormat)
    p2 = _proxy(KeysightE8267D, KeysightE8267DSim())
    try:
        w2 = RFGenWindow(p2, s2)
        qtbot.addWidget(w2)
        assert w2._pos_restored is True
        assert w2.pos() == saved_pos
    finally:
        p2.shutdown()


def test_plineedit_commits_on_enter_and_focus_out(qtbot, tmp_path):
    s = QSettings(str(tmp_path / "sdg.ini"), QSettings.Format.IniFormat)
    proxy = _proxy(SiglentSDG, SiglentSDGSim())
    try:
        win = PulseGenWindow(proxy, s)
        qtbot.addWidget(win)
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        proxy.set("ch1.wave", "PULSE")
        qtbot.waitUntil(lambda: proxy.value("ch1.wave") == "PULSE", timeout=5000)
        high = win.channels[0]["high"]

        # Ввод и уход фокуса -> отправляется
        high.setText("2.25 V")
        high.setModified(True)
        _focus_out(high)
        qtbot.waitUntil(lambda: proxy.value("ch1.high") == pytest.approx(2.25), timeout=5000)

        # Esc откатывает без отправки
        high.setText("3.5 V")
        high.setModified(True)
        qtbot.keyClick(high, Qt.Key.Key_Escape)
        qtbot.wait(60)
        assert proxy.value("ch1.high") == pytest.approx(2.25)

        # Ввод и Enter -> отправляется
        high.setText("2.75 V")
        high.setModified(True)
        high.returnPressed.emit()
        qtbot.waitUntil(lambda: proxy.value("ch1.high") == pytest.approx(2.75), timeout=5000)
        win.cleanup()
    finally:
        proxy.shutdown()


def test_pspinbox_commits_on_enter_stepby_and_focus_out_no_arrows(qtbot, tmp_path):
    from PyQt6.QtWidgets import QAbstractSpinBox
    s = QSettings(str(tmp_path / "rf.ini"), QSettings.Format.IniFormat)
    proxy = _proxy(KeysightE8267D, KeysightE8267DSim())
    try:
        win = RFGenWindow(proxy, s)
        qtbot.addWidget(win)
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        qtbot.waitUntil(lambda: proxy.value("rf.frequency") is not None, timeout=5000)
        from visalab.front.params import PSpinBox
        freq = next(w for w in win.findChildren(PSpinBox) if w.key == "rf.frequency")
        assert freq.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons

        # Уход фокуса -> отправляется
        freq.lineEdit().setText("2.5")
        freq.lineEdit().setModified(True)
        freq._dirty = True
        _focus_out(freq)
        qtbot.waitUntil(lambda: proxy.value("rf.frequency") == pytest.approx(2.5e9), timeout=5000)

        # Esc -> откатывает без отправки
        freq.lineEdit().setText("3.1")
        freq.lineEdit().setModified(True)
        freq._dirty = True
        qtbot.keyClick(freq, Qt.Key.Key_Escape)
        qtbot.wait(60)
        assert proxy.value("rf.frequency") == pytest.approx(2.5e9)

        # Enter -> отправляется
        freq.lineEdit().setText("2.8")
        freq.lineEdit().setModified(True)
        freq._dirty = True
        qtbot.keyClick(freq, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: proxy.value("rf.frequency") == pytest.approx(2.8e9), timeout=5000)

        # stepBy -> отправляется сразу
        before_step = proxy.value("rf.frequency")
        freq.stepBy(1)
        qtbot.waitUntil(lambda: proxy.value("rf.frequency") > before_step, timeout=5000)
        win.cleanup()
    finally:
        proxy.shutdown()


def test_lnumber_and_setting_field_commit_on_enter_and_focus_out(qtbot, tmp_path):
    # PowerMeter LNumber (GAIN)
    s = QSettings(str(tmp_path / "pm.ini"), QSettings.Format.IniFormat)
    proxy = _proxy(DRIVERS["u2021xa"][0], KeysightU2021XASim())
    try:
        pm = PowerMeterWindow(proxy, s)
        qtbot.addWidget(pm)
        from visalab.front.power_meter import LNumber
        gain = next(w for w in pm.findChildren(LNumber) if w.unit == "dB")
        gain.setText("12.5")
        gain.setModified(True)
        _focus_out(gain)
        assert gain.value == pytest.approx(12.5)
        gain.setText("15.0")
        gain.setModified(True)
        gain.returnPressed.emit()
        assert gain.value == pytest.approx(15.0)
        pm.cleanup()
    finally:
        proxy.shutdown()

    # MainWindow SettingField
    ms = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    mw = MainWindow(ms, ActivityMonitor(ProxyLinks()))
    qtbot.addWidget(mw)
    f = mw.fields["pm_floor_dbm"]
    f.setText("-42")
    f.setModified(True)
    _focus_out(f)
    assert mw.monitor.th.pm_floor_dbm == pytest.approx(-42.0)
    f.setText("-35")
    f.setModified(True)
    f.returnPressed.emit()
    assert mw.monitor.th.pm_floor_dbm == pytest.approx(-35.0)
    mw.cleanup()
