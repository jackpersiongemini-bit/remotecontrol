"""Временный фронтенд: простые окна для ежедневной работы, по одному на прибор.

* ``RFGenWindow``      - СВЧ-генератор (E8267D / SMF100A): частота, мощность, выход, кнопки модуляции;
* ``PSUWindow``        - источник IT6300: 3 канала, уставки U/I, измерения, выходы,
  CH1 включается только при потреблении CH2 и CH3 (с флажком обхода);
* ``FuncGenWindow``    - генератор SDG: форма/частота/амплитуда/смещение/фаза, выходы всегда 50 Ом,
  пресеты (при применении отправляется только то, что отличается);
* ``PowerMeterWindow`` - датчик U2021XA: режим Free run / Level, уровень запуска, частота, результат.

С прибором окна говорят только через InstrumentProxy (сигналы Qt) и ключи профилей,
поэтому RFGenWindow одинаково работает с E8267D и SMF100A. Настройки окон и пресеты -
в QSettings (ini-файл). Интерфейс на английском.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QRadioButton, QScrollArea,
                             QVBoxLayout, QWidget)

from .core.params import ConnectionState, PStatus
from .core.proxy import InstrumentProxy
from .core.scpi import si_format
from .ui.frame import FramelessFrame

__all__ = ["RFGenWindow", "PSUWindow", "FuncGenWindow", "PowerMeterWindow", "LabWindow", "WINDOWS"]

ACCENT = "#e0a020"
STYLE = f"""
QWidget {{ color: #d6dbe1; font-size: 10pt; }}
QWidget#frameRoot, QWidget#labBody, QScrollArea, QScrollArea > QWidget > QWidget {{ background: #11151b; }}
QWidget#frameRoot {{ border: 1px solid #2a313b; }}
QToolTip {{ background: #222a33; color: #d6dbe1; border: 1px solid #30363d; }}

/* заголовок окна и верхняя панель */
QWidget#titleBar {{ background: #0b0e12; }}
QLabel#titleText {{ background: transparent; font-weight: 600; padding-left: 6px; }}
QLabel#titleSub {{ background: transparent; color: #7a828e; padding-left: 8px; }}
QFrame#labBar {{ background: #161b22; border-bottom: 1px solid #2a313b; }}
QStatusBar, QLabel#statusLine {{ background: #0b0e12; color: #7a828e; padding: 2px 8px; }}

/* карточки приборов */
QFrame#card {{ background: #1a2029; border: 1px solid #2a313b; border-radius: 10px; }}
QFrame#card QLabel, QFrame#card QCheckBox, QFrame#card QRadioButton {{ background: transparent; }}
QLabel#cardTitle {{ font-size: 12pt; font-weight: 600; }}
QLabel#cardState {{ color: #7a828e; padding-left: 6px; }}
QLabel#cardMessage {{ color: #7a828e; font-size: 9pt; }}
QLabel#cardMessage[error="true"] {{ color: #f85149; }}
QLabel#led {{ border-radius: 6px; min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px; }}
QLabel#colHeader {{ color: #7a828e; font-size: 8.5pt; }}
QLabel[warn="true"] {{ color: #d9822b; }}
QLabel[dim="true"] {{ color: #7a828e; }}

QGroupBox {{ background: #151a21; border: 1px solid #2a313b; border-radius: 8px; margin-top: 14px; padding: 8px 6px 6px 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT}; font-weight: 600; }}

/* кнопки */
QPushButton {{ background: #252d38; border: 1px solid #343d4a; border-radius: 6px; padding: 5px 14px; }}
QPushButton:hover {{ border-color: #4b5562; background: #2b3440; }}
QPushButton:pressed {{ background: #1f262f; }}
QPushButton:disabled {{ color: #565e69; background: #1d232b; border-color: #2a313b; }}
QPushButton#connectBtn {{ padding: 4px 12px; }}
QPushButton[role="toggle"]:checked {{ background: #238636; border-color: #2ea043; color: white; font-weight: 600; }}
QPushButton[role="off"]:checked {{ background: #4a515c; border-color: #5d6571; color: white; font-weight: 600; }}
QPushButton[role="danger"] {{ background: #5a1f1d; border-color: #7d2a26; color: #ffd7d5; font-weight: 600; }}
QPushButton[role="danger"]:hover {{ background: #6e2522; }}

/* поля */
QDoubleSpinBox, QComboBox {{ background: #0f1318; border: 1px solid #343d4a; border-radius: 5px; padding: 3px 6px; min-height: 22px; }}
QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QDoubleSpinBox:disabled, QComboBox:disabled {{ color: #565e69; border-color: #252c35; }}
QDoubleSpinBox[pstatus="writing"] {{ border-color: #c9a227; }}
QDoubleSpinBox[pstatus="coerced"] {{ border-color: #d9822b; }}
QDoubleSpinBox[pstatus="invalid"], QDoubleSpinBox[pstatus="error"] {{ border-color: #f85149; }}
QComboBox QAbstractItemView {{ background: #1a2029; border: 1px solid #343d4a; selection-background-color: #2b3440; }}

QRadioButton::indicator, QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #4b5562; background: #0f1318; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator:checked, QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton:disabled, QCheckBox:disabled {{ color: #565e69; }}
QScrollBar:vertical {{ background: #11151b; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2b3440; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

/* вывод */
QLabel[role="readout"] {{ font-family: "Consolas", "DejaVu Sans Mono", monospace; font-size: 13pt; color: {ACCENT}; }}
QLabel[role="big"] {{ font-family: "Consolas", "DejaVu Sans Mono", monospace; font-size: 30pt; color: {ACCENT};
                     background: #0f1318; border: 1px solid #2a313b; border-radius: 8px; padding: 6px 14px; }}
QLabel[on="true"] {{ color: #3fb950; font-weight: 600; }}
"""

_STATE_TEXT = {
    ConnectionState.DISCONNECTED: "Disconnected",
    ConnectionState.CONNECTING: "Connecting…",
    ConnectionState.CONNECTED: "Connected",
    ConnectionState.ERROR: "Connection error",
}


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


# ======================================================================= поля
class SpinField(QDoubleSpinBox):
    """Число, привязанное к ключу прибора. Отправка по Enter / уходу фокуса, только если изменилось.

    ``scale`` - во сколько раз отображаемая единица больше единицы СИ (MHz -> 1e6).
    """

    def __init__(self, proxy: InstrumentProxy, key: str, suffix: str = "", scale: float = 1.0,
                 decimals: int = 3, step: float = 1.0, parent=None):
        super().__init__(parent)
        self.proxy, self.key, self.scale = proxy, key, scale
        spec = proxy.params[key]
        lo = spec.minimum if spec.minimum is not None else -1e12
        hi = spec.maximum if spec.maximum is not None else 1e12
        self.setDecimals(decimals)
        self.setRange(lo / scale, hi / scale)
        self.setSingleStep(step)
        self.setSuffix(f" {suffix}" if suffix else "")
        self.setKeyboardTracking(False)
        self.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)   # шаг - колесом и стрелками
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMinimumWidth(120)
        self.setToolTip(key)
        self.setEnabled(False)
        self._shown: float | None = None
        proxy.subscribe(key, self._on_value, self._on_status)
        self.editingFinished.connect(self._commit)

    def _on_value(self, value: Any) -> None:
        if value is None:
            self._shown = None
            self.setEnabled(False)
            return
        self.setEnabled(True)
        if self.hasFocus():
            return                                  # не мешать вводу
        self.blockSignals(True)
        self.setValue(float(value) / self.scale)
        self.blockSignals(False)
        self._shown = self.value()

    def _on_status(self, status: PStatus, message: str) -> None:
        self.setProperty("pstatus", status.value)
        _repolish(self)
        self.setToolTip(self.key + (f"\n{message}" if message else ""))

    def _commit(self) -> None:
        if self._shown is None or abs(self.value() - self._shown) < 10 ** -self.decimals() / 2:
            return
        self._shown = self.value()
        self.proxy.set(self.key, self.value() * self.scale)

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        v = self.proxy.value(self.key)              # показать актуальное значение прибора
        if v is not None and self._shown is not None and abs(self.value() - self._shown) < 1e-12:
            self._on_value(v)


class ToggleButton(QPushButton):
    """Кнопка-переключатель BOOL-ключа. Сама не переключается: ждёт подтверждения прибора."""

    def __init__(self, proxy: InstrumentProxy, key: str, text: str, parent=None):
        super().__init__(text, parent)
        self.proxy, self.key, self._text = proxy, key, text
        self.setCheckable(True)
        self.setProperty("role", "toggle")
        self.setEnabled(False)
        self.setMinimumWidth(90)
        self.setToolTip(key)
        self._value: bool | None = None
        proxy.subscribe(key, self._on_value)
        self.clicked.connect(self._click)

    def _click(self) -> None:
        self.setChecked(bool(self._value))
        if self._value is not None:
            self.proxy.set(self.key, not self._value)

    def _on_value(self, value: Any) -> None:
        self._value = None if value is None else bool(value)
        self.setChecked(bool(value))
        self.setEnabled(value is not None)
        self.setText(self._text + ("" if value is None else ("  ON" if value else "  OFF")))


class OnOffButtons(QWidget):
    """Отдельные кнопки ON и OFF + состояние выхода (подсвечена та, что сейчас на приборе)."""

    def __init__(self, proxy: InstrumentProxy, key: str, text: str, parent=None):
        super().__init__(parent)
        self.proxy, self.key = proxy, key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text)
        self.state = QLabel("—")
        self.state.setMinimumWidth(40)
        self.on_btn = QPushButton("ON")
        self.off_btn = QPushButton("OFF")
        for b, v in ((self.on_btn, True), (self.off_btn, False)):
            b.setCheckable(True)
            b.setProperty("role", "toggle" if v else "off")
            b.setMinimumHeight(34)
            b.clicked.connect(lambda _=False, v=v: self._send(v))
        self.off_btn.setProperty("role", "off")
        lay.addWidget(self.label)
        lay.addWidget(self.state)
        lay.addWidget(self.on_btn, 1)
        lay.addWidget(self.off_btn, 1)
        self.setToolTip(key)
        proxy.subscribe(key, self._on_value)
        self._on_value(proxy.value(key))

    def _send(self, value: bool) -> None:
        self._on_value(self.proxy.value(self.key))   # подсветка - только по ответу прибора
        self.proxy.set(self.key, value)

    def _on_value(self, value) -> None:
        known = value is not None
        self.on_btn.setEnabled(known)
        self.off_btn.setEnabled(known)
        self.on_btn.setChecked(value is True)
        self.off_btn.setChecked(value is False)
        self.state.setText("—" if not known else ("ON" if value else "OFF"))
        self.state.setProperty("on", bool(value))
        _repolish(self.state)


class Readout(QLabel):
    def __init__(self, proxy: InstrumentProxy, key: str, digits: int = 5, role: str = "readout", parent=None):
        super().__init__("—", parent)
        spec = proxy.params[key]
        self.unit = spec.unit
        self.labels = {"HIZ": "High-Z", "50": "50 Ω", **{t: l for t, l in WAVES}}
        self.digits = digits
        self.setProperty("role", role)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setMinimumWidth(90)
        self.setToolTip(key)
        proxy.subscribe(key, self._on_value)

    def _on_value(self, value):
        if value is None:
            self.setText("—")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            self.setText(self.labels.get(str(value), str(value)))
        elif self.unit in ("dBm", "dB"):
            self.setText(f"{value:.{max(1, self.digits - 3)}f} {self.unit}")
        else:
            self.setText(si_format(value, self.unit, digits=self.digits))


class EnumBox(QComboBox):
    def __init__(self, proxy: InstrumentProxy, key: str, items: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.proxy, self.key = proxy, key
        allowed = set(proxy.params[key].choices)
        for token, label in items:
            if token in allowed:
                self.addItem(label, token)
        self.setEnabled(False)
        self.setToolTip(key)
        proxy.subscribe(key, self._on_value)
        self.activated.connect(lambda i: self.proxy.set(self.key, self.itemData(i)))

    def _on_value(self, value):
        self.setEnabled(value is not None)
        self.blockSignals(True)
        self.setCurrentIndex(-1 if value is None else self.findData(str(value).upper()))
        self.blockSignals(False)


# ================================================================= панель
_LED = {
    ConnectionState.DISCONNECTED: "#7a828e",
    ConnectionState.CONNECTING: "#d0a630",
    ConnectionState.CONNECTED: "#3fb950",
    ConnectionState.ERROR: "#f85149",
}


class QuickWindow(QFrame):
    """Карточка одного прибора: заголовок (индикатор, название, Connect), тело, строка сообщений.

    Карточки собираются в одно окно ``LabWindow``. ``shutdown()`` сохраняет настройки
    и закрывает прибор.
    """

    KIND = "base"

    def __init__(self, proxy: InstrumentProxy, title: str, settings: QSettings, parent=None):
        super().__init__(parent)
        self.proxy, self.settings = proxy, settings
        self.title = title
        self.setObjectName("card")
        self.setWindowTitle(f"{title} — {proxy.address}")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(14, 10, 14, 10)
        self.root.setSpacing(8)
        bar = QHBoxLayout()
        self.led = QLabel()
        self.led.setObjectName("led")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.state_label = QLabel()
        self.state_label.setObjectName("cardState")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.clicked.connect(self._toggle_connection)
        bar.addWidget(self.led)
        bar.addWidget(self.title_label)
        bar.addWidget(self.state_label)
        bar.addStretch(1)
        bar.addWidget(self.connect_btn)
        self.root.addLayout(bar)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.root.addLayout(self.body, 1)
        self.message = QLabel("")
        self.message.setObjectName("cardMessage")
        self.message.setWordWrap(True)
        self.root.addWidget(self.message)
        self.idn_label = self.title_label            # IDN - в подсказке заголовка

        proxy.state_changed.connect(self._on_state)
        proxy.reconnect_pending.connect(lambda _s: self._on_state(self.proxy.state))
        proxy.identified.connect(lambda idn: self.title_label.setToolTip(f"{idn}\n{proxy.address}"))
        proxy.instrument_error.connect(lambda c, m: self.show_message(f"Instrument error {c}: {m}", True))
        proxy.comm_error.connect(lambda m: self.show_message(m, True))
        proxy.param_status.connect(self._on_param_status)
        self.title_label.setToolTip(proxy.address)
        self._on_state(proxy.state)

    def show_message(self, text: str, error: bool = False) -> None:
        self.message.setProperty("error", error)
        _repolish(self.message)
        self.message.setText(text)

    def _on_param_status(self, key: str, status: PStatus, message: str) -> None:
        if status in (PStatus.ERROR, PStatus.INVALID, PStatus.COERCED) and message:
            self.show_message(f"{key}: {message}", status is not PStatus.COERCED)

    def _toggle_connection(self) -> None:
        if self.proxy.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING) or self.proxy.reconnecting:
            self.proxy.close()
        else:
            self.proxy.open()

    def _on_state(self, state: ConnectionState) -> None:
        retry = state is ConnectionState.ERROR and self.proxy.reconnecting
        self.state_label.setText("No connection, reconnecting…" if retry else _STATE_TEXT[state])
        self.led.setStyleSheet(f"background: {_LED[state]};")
        busy = state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING) or retry
        self.connect_btn.setText("Disconnect" if busy else "Connect")

    def shutdown(self) -> None:
        self.save_settings()
        self.settings.sync()
        self.proxy.shutdown()

    def save_settings(self) -> None:
        """Переопределяется: сохранить своё в self.settings."""


# =========================================================== СВЧ-генератор
class RFGenWindow(QuickWindow):
    KIND = "signal_generator"
    MODULATION = (("mod.output", "Mod"), ("am.state", "AM"), ("fm.state", "FM"),
                  ("pm.state", "PM"), ("pulse.state", "Pulse"))

    def __init__(self, proxy, title="RF Signal Generator", settings=None, parent=None):
        super().__init__(proxy, title, settings, parent)
        grid = QGridLayout()
        self.freq = SpinField(proxy, "rf.frequency", "MHz", scale=1e6, decimals=6, step=1.0)
        self.power = SpinField(proxy, "rf.power", "dBm", decimals=2, step=0.5)
        self.output = ToggleButton(proxy, "rf.output", "RF")
        grid.addWidget(QLabel("Frequency"), 0, 0)
        grid.addWidget(self.freq, 0, 1)
        grid.addWidget(QLabel("Power"), 1, 0)
        grid.addWidget(self.power, 1, 1)
        grid.addWidget(self.output, 0, 2, 2, 1)
        self.output.setMinimumHeight(56)
        self.body.addLayout(grid)
        box = QGroupBox("Modulation")
        row = QHBoxLayout(box)
        self.mod_buttons = {}
        for key, text in self.MODULATION:
            if proxy.supports(key):
                b = ToggleButton(proxy, key, text)
                row.addWidget(b)
                self.mod_buttons[key] = b
        self.body.addWidget(box)
        self.body.addStretch(1)


# ================================================================= источник
class PSUWindow(QuickWindow):
    """3 канала. CH1 можно включить, только если CH2 и CH3 потребляют ток (или включён обход)."""

    KIND = "power_supply"
    GATED = 1                 # канал с блокировкой
    GATE_BY = (2, 3)          # чьё потребление нужно

    def __init__(self, proxy, title="Power Supply", settings=None, parent=None):
        super().__init__(proxy, title, settings, parent)
        row = QHBoxLayout()
        self.outputs: dict[int, ToggleButton] = {}
        n_ch = min(proxy.channels, 3)
        for n in range(1, n_ch + 1):
            box = QGroupBox(f"CH{n}")
            g = QGridLayout(box)
            g.addWidget(QLabel("Voltage"), 0, 0)
            g.addWidget(SpinField(proxy, f"ch{n}.voltage", "V", decimals=3, step=0.1), 0, 1)
            g.addWidget(QLabel("Current"), 1, 0)
            g.addWidget(SpinField(proxy, f"ch{n}.current", "A", decimals=3, step=0.01), 1, 1)
            g.addWidget(QLabel("V meas"), 2, 0)
            g.addWidget(Readout(proxy, f"ch{n}.meas_v"), 2, 1)
            g.addWidget(QLabel("I meas"), 3, 0)
            g.addWidget(Readout(proxy, f"ch{n}.meas_i"), 3, 1)
            if proxy.supports(f"ch{n}.meas_p"):
                g.addWidget(QLabel("P meas"), 4, 0)
                g.addWidget(Readout(proxy, f"ch{n}.meas_p"), 4, 1)
            if proxy.supports(f"ch{n}.mode"):
                g.addWidget(QLabel("Mode"), 5, 0)
                g.addWidget(Readout(proxy, f"ch{n}.mode"), 5, 1)
            out = ToggleButton(proxy, f"ch{n}.output", "Output")
            out.setMinimumHeight(40)
            g.addWidget(out, 6, 0, 1, 2)
            self.outputs[n] = out
            row.addWidget(box)
        self.body.addLayout(row)

        gate = QHBoxLayout()
        self.bypass = QCheckBox(f"Bypass CH{self.GATED} interlock")
        self.bypass.setToolTip(f"Without bypass CH{self.GATED} can be switched on only when "
                               + " and ".join(f"CH{n}" for n in self.GATE_BY) + " draw current")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.0, 3000.0)
        self.threshold.setDecimals(1)
        self.threshold.setSuffix(" mA")
        self.threshold.setToolTip("Minimum current that counts as consumption")
        self.gate_label = QLabel()
        self.all_off = QPushButton("All off")
        self.all_off.setProperty("role", "danger")
        self.all_off.clicked.connect(lambda: self.proxy.action("all_off"))
        gate.addWidget(self.bypass)
        gate.addSpacing(12)
        gate.addWidget(QLabel("min current"))
        gate.addWidget(self.threshold)
        gate.addStretch(1)
        gate.addWidget(self.all_off)
        self.body.addLayout(gate)
        self.body.addWidget(self.gate_label)
        self.body.addStretch(1)

        s = self.settings
        self.bypass.setChecked(str(s.value(f"{self.KIND}/bypass_ch{self.GATED}_interlock", "false")).lower() == "true")
        self.threshold.setValue(float(s.value(f"{self.KIND}/interlock_min_mA", 1.0)))
        self.bypass.toggled.connect(self._update_gate)
        self.threshold.valueChanged.connect(self._update_gate)
        if n_ch >= max(self.GATED, *self.GATE_BY):
            for n in (*self.GATE_BY, self.GATED):
                proxy.subscribe(f"ch{n}.meas_i", lambda _v: self._update_gate())
                proxy.subscribe(f"ch{n}.output", lambda _v: self._update_gate())
        proxy.state_changed.connect(lambda _s: (self._update_gate(), self.all_off.setEnabled(proxy.is_connected)))
        self.all_off.setEnabled(proxy.is_connected)
        self._update_gate()

    def gate_open(self) -> bool:
        if self.bypass.isChecked():
            return True
        thr = self.threshold.value() / 1000.0
        return all((self.proxy.value(f"ch{n}.meas_i") or 0.0) >= thr and self.proxy.value(f"ch{n}.output")
                   for n in self.GATE_BY)

    def _update_gate(self) -> None:
        btn = self.outputs.get(self.GATED)
        if btn is None:
            return
        known = self.proxy.value(f"ch{self.GATED}.output") is not None
        is_on = bool(self.proxy.value(f"ch{self.GATED}.output"))
        ok = self.gate_open()
        btn.setEnabled(known and (ok or is_on))           # выключить можно всегда
        if self.bypass.isChecked():
            self.gate_label.setText(f"CH{self.GATED} interlock bypassed")
            self.gate_label.setProperty("warn", True)
            _repolish(self.gate_label)
        else:
            self.gate_label.setText(f"CH{self.GATED} " + ("allowed" if ok else "locked until "
                                    + " and ".join(f"CH{n}" for n in self.GATE_BY) + " draw current"))
            self.gate_label.setProperty("warn", False)
            _repolish(self.gate_label)

    def save_settings(self) -> None:
        self.settings.setValue(f"{self.KIND}/bypass_ch{self.GATED}_interlock", self.bypass.isChecked())
        self.settings.setValue(f"{self.KIND}/interlock_min_mA", self.threshold.value())


# ======================================================== генератор SDG
WAVES = [("SINE", "Sine"), ("SQUARE", "Square"), ("RAMP", "Ramp"), ("PULSE", "Pulse"),
         ("NOISE", "Noise"), ("ARB", "Arb"), ("DC", "DC")]


class FuncGenWindow(QuickWindow):
    """Генератор SDG. Нагрузка выходов всегда 50 Ом (выставляется после подключения).

    Пресет - значения ``PRESET_KEYS`` всех каналов. При применении отправляются только
    отличающиеся от текущих значения, форма сигнала первой.
    """

    KIND = "function_generator"
    LOAD = "50"
    PRESET_KEYS = ("wave", "frequency", "amplitude", "offset", "phase", "duty")

    def __init__(self, proxy, title="Function Generator", settings=None, parent=None):
        super().__init__(proxy, title, settings, parent)
        row = QHBoxLayout()
        for n in range(1, proxy.channels + 1):
            box = QGroupBox(f"CH{n}  (50 Ω)")
            g = QGridLayout(box)
            hdr_set, hdr_act = QLabel("Set"), QLabel("Actual")
            for h in (hdr_set, hdr_act):
                h.setObjectName("colHeader")
            g.addWidget(hdr_set, 0, 1)
            g.addWidget(hdr_act, 0, 2, Qt.AlignmentFlag.AlignRight)
            rows = [("Waveform", EnumBox(proxy, f"ch{n}.wave", WAVES), "wave"),
                    ("Frequency", SpinField(proxy, f"ch{n}.frequency", "Hz", decimals=3, step=100.0), "frequency"),
                    ("Amplitude", SpinField(proxy, f"ch{n}.amplitude", "Vpp", decimals=3, step=0.1), "amplitude"),
                    ("Offset", SpinField(proxy, f"ch{n}.offset", "V", decimals=3, step=0.1), "offset"),
                    ("Phase", SpinField(proxy, f"ch{n}.phase", "deg", decimals=1, step=10.0), "phase"),
                    ("Duty", SpinField(proxy, f"ch{n}.duty", "%", decimals=1, step=5.0), "duty"),
                    ("Load", None, "load")]
            for r, (label, editor, suffix) in enumerate(rows, start=1):
                g.addWidget(QLabel(label), r, 0)
                if editor is not None:
                    g.addWidget(editor, r, 1)
                g.addWidget(Readout(proxy, f"ch{n}.{suffix}", digits=7), r, 2)
            g.addWidget(OnOffButtons(proxy, f"ch{n}.output", "Output"), len(rows) + 1, 0, 1, 3)
            row.addWidget(box)
        self.body.addLayout(row)

        pbox = QGroupBox("Presets")
        p = QHBoxLayout(pbox)
        self.preset_box = QComboBox()
        self.preset_box.setMinimumWidth(180)
        self.apply_btn = QPushButton("Apply")
        self.save_btn = QPushButton("Save as…")
        self.delete_btn = QPushButton("Delete")
        self.apply_btn.clicked.connect(lambda: self.apply_preset(self.preset_box.currentText()))
        self.save_btn.clicked.connect(self._save_dialog)
        self.delete_btn.clicked.connect(self._delete_current)
        for w in (self.preset_box, self.apply_btn, self.save_btn, self.delete_btn):
            p.addWidget(w)
        p.addStretch(1)
        self.body.addWidget(pbox)
        self.body.addStretch(1)
        self._reload_presets()
        proxy.sync_done.connect(self._force_load)
        proxy.state_changed.connect(lambda _s: self._update_buttons())
        self._update_buttons()

    # ------------------------------------------------------------- 50 Ом
    def _force_load(self) -> None:
        for n in range(1, self.proxy.channels + 1):
            key = f"ch{n}.load"
            if self.proxy.value(key) not in (None, self.LOAD):
                self.proxy.set(key, self.LOAD)
                self.show_message(f"CH{n} load set to 50 Ω")

    # ------------------------------------------------------------ пресеты
    def presets(self) -> dict[str, dict]:
        raw = self.settings.value(f"{self.KIND}/presets", "{}")
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _store(self, data: dict) -> None:
        self.settings.setValue(f"{self.KIND}/presets", json.dumps(data, ensure_ascii=False))
        self.settings.sync()
        self._reload_presets()

    def _reload_presets(self) -> None:
        current = self.preset_box.currentText()
        self.preset_box.clear()
        self.preset_box.addItems(sorted(self.presets()))
        i = self.preset_box.findText(current)
        if i >= 0:
            self.preset_box.setCurrentIndex(i)
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.preset_box.count() > 0
        self.apply_btn.setEnabled(has and self.proxy.is_connected)
        self.delete_btn.setEnabled(has)
        self.save_btn.setEnabled(self.proxy.is_connected)

    def current_values(self) -> dict[str, Any]:
        out = {}
        for n in range(1, self.proxy.channels + 1):
            for suffix in self.PRESET_KEYS:
                key = f"ch{n}.{suffix}"
                if self.proxy.supports(key) and self.proxy.value(key) is not None:
                    out[key] = self.proxy.value(key)
        return out

    def save_preset(self, name: str) -> None:
        data = self.presets()
        data[name] = self.current_values()
        self._store(data)
        self.preset_box.setCurrentText(name)
        self.show_message(f"Preset '{name}' saved")

    def _save_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:", text=self.preset_box.currentText())
        name = name.strip()
        if ok and name:
            if name in self.presets() and QMessageBox.question(
                    self, "Save preset", f"Overwrite '{name}'?") != QMessageBox.StandardButton.Yes:
                return
            self.save_preset(name)

    def _delete_current(self) -> None:
        name = self.preset_box.currentText()
        if name and QMessageBox.question(self, "Delete preset", f"Delete '{name}'?") \
                == QMessageBox.StandardButton.Yes:
            data = self.presets()
            data.pop(name, None)
            self._store(data)

    def preset_changes(self, name: str) -> list[tuple[str, Any]]:
        """Что нужно отправить, чтобы прийти к пресету: только отличающиеся значения, форма первой."""
        target = self.presets().get(name, {})
        changes = []
        for key, value in target.items():
            spec = self.proxy.params.get(key)
            if spec is None or spec.readonly:
                continue
            wave = target.get(f"{key.split('.')[0]}.wave")
            if key.endswith(".duty") and wave not in ("SQUARE", "PULSE"):
                continue                                # скважность есть только у меандра/импульса
            if not spec.same(spec.validate(value), self.proxy.value(key)):
                changes.append((key, value))
        changes.sort(key=lambda kv: 0 if kv[0].endswith(".wave") else 1)
        return changes

    def apply_preset(self, name: str) -> int:
        if not name:
            return 0
        self._force_load()
        changes = self.preset_changes(name)
        for key, value in changes:
            self.proxy.set(key, value)
        self.show_message(f"Preset '{name}': " + (", ".join(k for k, _ in changes) if changes
                                                  else "nothing to change"))
        return len(changes)


# ================================================================== ваттметр
class PowerMeterWindow(QuickWindow):
    """Режимы: Free run (свободный счёт) и Level (запуск по уровню входного сигнала)."""

    KIND = "power_meter"
    FREE, LEVEL = "IMM", "INT"

    def __init__(self, proxy, title="Power Meter", settings=None, parent=None):
        super().__init__(proxy, title, settings, parent)
        self.reading = Readout(proxy, "meas.power", digits=6, role="big")
        self.body.addWidget(self.reading)
        grid = QGridLayout()
        self.free = QRadioButton("Free run")
        self.level = QRadioButton("Level")
        self._modes = QButtonGroup(self)
        self._modes.addButton(self.free)
        self._modes.addButton(self.level)
        self.level_spin = SpinField(proxy, "trig.level", "dBm", decimals=2, step=0.5)
        self.freq = SpinField(proxy, "freq", "GHz", scale=1e9, decimals=6, step=0.1)
        self.armed = QLabel("")
        grid.addWidget(QLabel("Mode"), 0, 0)
        grid.addWidget(self.free, 0, 1)
        grid.addWidget(self.level, 0, 2)
        grid.addWidget(QLabel("Trigger level"), 1, 0)
        grid.addWidget(self.level_spin, 1, 1, 1, 2)
        grid.addWidget(QLabel("Frequency"), 2, 0)
        grid.addWidget(self.freq, 2, 1, 1, 2)
        grid.addWidget(self.armed, 3, 1, 1, 2)
        self.body.addLayout(grid)
        self.zero_btn = QPushButton("Zero")
        self.zero_btn.clicked.connect(self._zero)
        self.body.addWidget(self.zero_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addStretch(1)
        self.free.clicked.connect(lambda: self.set_mode(self.FREE))
        self.level.clicked.connect(lambda: self.set_mode(self.LEVEL))
        proxy.subscribe("trig.source", self._on_source)
        if proxy.supports("meas.armed"):
            proxy.subscribe("meas.armed", lambda v: self.armed.setText("waiting for trigger…" if v else ""))
        proxy.state_changed.connect(lambda _s: self._enable())
        self._enable()

    def set_mode(self, source: str) -> None:
        self.proxy.set("trig.source", source)
        if self.proxy.value("meas.continuous") is not True:
            self.proxy.set("meas.continuous", True)

    def _on_source(self, value) -> None:
        for b in (self.free, self.level):
            b.blockSignals(True)
        self.free.setChecked(value == self.FREE)
        self.level.setChecked(value == self.LEVEL)
        if value not in (self.FREE, self.LEVEL, None):
            self._modes.setExclusive(False)
            self.free.setChecked(False)
            self.level.setChecked(False)
            self._modes.setExclusive(True)
            self.show_message(f"Trigger source on the instrument: {value}")
        for b in (self.free, self.level):
            b.blockSignals(False)
        self.level_spin.setVisible(True)
        self._enable()

    def _enable(self) -> None:
        on = self.proxy.is_connected and self.proxy.value("trig.source") is not None
        self.free.setEnabled(on)
        self.level.setEnabled(on)
        self.zero_btn.setEnabled(self.proxy.is_connected)

    def _zero(self) -> None:
        if QMessageBox.question(self, "Zero", "Remove the signal from the sensor input. Zero now?") \
                == QMessageBox.StandardButton.Yes:
            self.proxy.action("zero")


#: тип драйвера (как в run_gui.DEVICES) -> окно
WINDOWS = {
    "e8267d": RFGenWindow,
    "smf100a": RFGenWindow,
    "it6300": PSUWindow,
    "sdg": FuncGenWindow,
    "u2021xa": PowerMeterWindow,
}


# ============================================================ общее окно
class LabWindow(FramelessFrame):
    """Все приборы в одном окне (qframelesswindow): карточки 2x2 и общие кнопки сверху."""

    #: где стоит карточка: (ряд, столбец)
    PLACES = {"signal_generator": (0, 0), "power_meter": (0, 1),
              "function_generator": (1, 0), "power_supply": (1, 1)}

    def __init__(self, panels: list[QuickWindow], settings: QSettings, title: str = "Lab Control", parent=None):
        super().__init__(title, "remote instrument control", parent, style=STYLE)
        self.panels, self.settings = panels, settings

        bar = QFrame()
        bar.setObjectName("labBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        self.connect_all = QPushButton("Connect all")
        self.disconnect_all = QPushButton("Disconnect all")
        self.outputs_off = QPushButton("ALL OUTPUTS OFF")
        self.outputs_off.setProperty("role", "danger")
        self.outputs_off.setToolTip("Switch off the outputs of every connected instrument")
        self.connect_all.clicked.connect(lambda: [p.proxy.open() for p in panels if not p.proxy.is_connected])
        self.disconnect_all.clicked.connect(lambda: [p.proxy.close() for p in panels])
        self.outputs_off.clicked.connect(self.all_outputs_off)
        row.addWidget(self.connect_all)
        row.addWidget(self.disconnect_all)
        row.addStretch(1)
        row.addWidget(self.outputs_off)
        self.body.addWidget(bar)

        content = QWidget()
        content.setObjectName("labBody")
        grid = QGridLayout(content)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(12)
        extra = 2
        for p in panels:
            r, c = self.PLACES.get(p.KIND, (extra, 0))
            if p.KIND not in self.PLACES:
                extra += 1
            grid.addWidget(p, r, c)
        grid.setRowStretch(grid.rowCount(), 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.body.addWidget(scroll, 1)

        for p in panels:
            p.proxy.state_changed.connect(lambda s, p=p: self.show_message(f"{p.title}: {_STATE_TEXT[s]}",
                                                                          s is ConnectionState.ERROR))
        geo = settings.value("lab/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1600, 940)

    def all_outputs_off(self) -> None:
        n = 0
        for p in self.panels:
            if p.proxy.is_connected and p.proxy.supports("all_off"):
                p.proxy.action("all_off")
                n += 1
        self.show_message(f"All outputs off: {n} instrument(s)")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt
        self.settings.setValue("lab/geometry", self.saveGeometry())
        for p in self.panels:
            p.shutdown()
        self.settings.sync()
        super().closeEvent(event)
