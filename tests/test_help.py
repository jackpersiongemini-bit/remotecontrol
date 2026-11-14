"""Подсказки (Алексей 2026-11-14: «навожу на параметр, а подсказка не высвечивается, что это и как работает»).

* у каждого ключа каждого профиля есть текст (что это и как работает);
* у каждого поля, кнопки и подписи строки во всех окнах есть подсказка;
* поле прибора: описание, диапазон, как менять, ключ; ошибка записи добавляется, шаг спинбокса остаётся;
* подпись строки показывает подсказку своего поля.
"""
import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QAbstractButton, QAbstractSpinBox, QLabel, QLineEdit, QToolButton

from conftest import attach
from visalab.coord import ActivityMonitor
from visalab.core import PStatus
from visalab.drivers import DRIVERS
from visalab.front import PowerMeterWindow, PSUWindow, PulseGenWindow, RFGenWindow, SpectrumWindow, VNAWindow
from visalab.front.help import HELP, help_text, pattern_key
from visalab.front.main_window import MainWindow
from visalab.front.params import PanelLine, PSpinBox
from visalab.instruments import InstrumentProxy
from visalab.profiles import PROFILES
from visalab.transport import SimTransport

WINDOWS = [("pna", VNAWindow), ("e4446a", SpectrumWindow), ("u2021xa", PowerMeterWindow), ("e8267d", RFGenWindow),
           ("sdg", PulseGenWindow), ("it6300", PSUWindow)]
#: стандартные кнопки qframelesswindow (скрыты, вместо них свои)
_HIDDEN_STANDARD = ("MinimizeButton", "MaximizeButton", "CloseButton")


def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return attach(InstrumentProxy(drv, SimTransport(sim_cls())))


def test_every_profile_key_has_help():
    missing = [(name, s.key) for name, p in PROFILES.items() for s in p.slots if not help_text(name, s.key)]
    assert not missing
    assert set(HELP) == set(PROFILES)
    assert pattern_key("ch2.voltage") == "ch{n}.voltage" and pattern_key("m12.format") == "m{n}.format"
    assert help_text("power_meter", "detector") != help_text("spectrum_analyzer", "detector")


def _without_tip(win):
    missing = []
    for w in win.findChildren((QAbstractButton, QLineEdit, QAbstractSpinBox, QLabel)):
        if w.isHidden() or type(w).__name__ in _HIDDEN_STANDARD or isinstance(w, QToolButton) \
                or w.objectName() == "qt_tableview_cornerbutton":
            continue
        if isinstance(w, QLineEdit) and isinstance(w.parent(), QAbstractSpinBox):
            continue                                          # поле внутри спинбокса - подсказка у спинбокса
        if isinstance(w, QLabel) and not (w.objectName() == "lineName" and w.text()):
            continue                                          # подписи строк; прочие QLabel - текст
        tip = w.tip() if hasattr(w, "tip") else w.toolTip()
        if not tip:
            missing.append(f"{type(w).__name__} {w.objectName()} {getattr(w, 'text', lambda: '')()!r}")
    return missing


@pytest.mark.parametrize("kind,cls", WINDOWS, ids=[k for k, _c in WINDOWS])
def test_every_control_has_a_tooltip(qtbot, tmp_path, kind, cls):
    proxy = _proxy(kind)
    win = cls(proxy, QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    try:
        with qtbot.waitSignal(proxy.sync_done, timeout=10000):
            proxy.open()
        win.set_expanded(True)
        assert not _without_tip(win)
        bound = [w for w in win.bound_widgets() if hasattr(w, "help_text")]
        assert bound
        for w in bound:                                        # что это - не только ключ
            tip = w.toolTip()
            assert f"Key: {w.key}" in tip and tip.splitlines()[0] != f"Key: {w.key}", (w.key, tip)
    finally:
        proxy.shutdown()


def test_main_window_controls_have_tooltips(qtbot, tmp_path):
    win = MainWindow(QSettings(str(tmp_path / "m.ini"), QSettings.Format.IniFormat), ActivityMonitor())
    qtbot.addWidget(win)
    assert not _without_tip(win)


def test_field_tooltip_and_line_name(qtbot, tmp_path):
    proxy = _proxy("e8267d")
    win = RFGenWindow(proxy, QSettings(str(tmp_path / "rf.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    try:
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        spin = next(w for w in win.bound_widgets() if isinstance(w, PSpinBox) and w.key == "rf.frequency")
        tip = spin.toolTip()
        assert tip.startswith("CW frequency of the RF output")
        assert "Range 250 kHz .. 44 GHz" in tip and "Wheel" in tip and "STEP AUTO" in tip
        assert tip.rstrip().endswith("Key: rf.frequency")
        line = next(ln for ln in win.findChildren(PanelLine) if spin in ln.widgets)
        assert line.help() == spin.help_text()                # навели на имя строки - та же подсказка
        spin._on_status(PStatus.ERROR, "write failed: -222 Data out of range")
        assert spin.toolTip().endswith("Data out of range") and "STEP AUTO" in spin.toolTip()
        assert "Data out of range" not in line.help()         # подпись: что это, а не последняя ошибка
        spin.set_step(1e6)
        assert "STEP 1 MHz" in spin.toolTip() and "Data out of range" in spin.toolTip()
        spin._on_status(PStatus.WRITTEN, "")
        assert "Data out of range" not in spin.toolTip()
        spin.setToolTip("Knob of the generator")             # текст окна + общее описание
        assert spin.toolTip().startswith("Knob of the generator\nCW frequency")
    finally:
        proxy.shutdown()
