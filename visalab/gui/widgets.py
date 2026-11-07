"""Виджеты параметров, привязанные к InstrumentProxy.

Каждый виджет знает только свой ``Param`` и прокси: пишет через ``proxy.set``,
получает значения/статусы через ``proxy.subscribe`` (сигналы Qt из потока прибора).
Цвет рамки показывает PStatus: писать - жёлтый, округлено - оранжевый, ошибка - красный.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QWidget

from ..core.params import Kind, Param, PStatus
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format, si_parse

STYLE = """
QLineEdit[pstatus="writing"], QComboBox[pstatus="writing"], QPushButton[pstatus="writing"] { border: 1px solid #c9a227; }
QLineEdit[pstatus="coerced"], QComboBox[pstatus="coerced"], QPushButton[pstatus="coerced"] { border: 1px solid #d9822b; }
QLineEdit[pstatus="invalid"], QComboBox[pstatus="invalid"], QPushButton[pstatus="invalid"],
QLineEdit[pstatus="error"], QComboBox[pstatus="error"], QPushButton[pstatus="error"] { border: 1px solid #d64545; }
QPushButton[class="toggle"]:checked { background: #2e7d32; color: white; font-weight: bold; }
QLabel[class="readout"] { font-family: monospace; font-size: 15pt; }
QLabel#led { border-radius: 7px; min-width: 14px; max-width: 14px; min-height: 14px; max-height: 14px; }
"""


class ParamWidget:
    """Общая логика: подписка, статус, подсказка."""

    def bind(self, proxy: InstrumentProxy, spec: Param) -> None:
        self.proxy, self.spec = proxy, spec
        self._value: Any = None
        proxy.subscribe(spec.key, self._on_value, self._on_status)
        tip = spec.key + (f"  [{spec.unit}]" if spec.unit else "")
        if spec.minimum is not None or spec.maximum is not None:
            tip += f"\nдиапазон: {spec.minimum} … {spec.maximum}"
        self.setToolTip(tip)

    def _on_value(self, value: Any) -> None:
        self._value = value
        self.setEnabled(value is not None or self.spec.readonly)
        self.show_value(value)

    def _on_status(self, status: PStatus, message: str) -> None:
        self.setProperty("pstatus", status.value)
        self.style().unpolish(self)
        self.style().polish(self)
        base = self.toolTip().split("\n⚠")[0]
        self.setToolTip(base + (f"\n⚠ {message}" if message else ""))
        if status is PStatus.UNKNOWN:
            self._value = None
            self.show_value(None)

    def show_value(self, value: Any) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def mark_invalid(self, message: str) -> None:
        self._on_status(PStatus.INVALID, message)


class NumberField(QLineEdit, ParamWidget):
    """Число с SI-префиксами: ``1.5k``, ``2 MHz``, ``20m``."""

    def __init__(self, proxy, spec, parent: QWidget | None = None):
        super().__init__(parent)
        self.bind(proxy, spec)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.returnPressed.connect(self._commit)

    def show_value(self, value):
        if self.hasFocus() and self.isModified():
            return  # не перетирать то, что пользователь печатает
        self.setText("—" if value is None else
                     si_format(value, self.spec.unit) if self.spec.kind is Kind.FLOAT else str(value))
        self.setModified(False)

    def _commit(self):
        try:
            value = si_parse(self.text(), self.spec.unit)
        except ValueError as exc:
            self.mark_invalid(str(exc))
            return
        self.setModified(False)
        self.proxy.set(self.spec.key, value)


class TextField(QLineEdit, ParamWidget):
    def __init__(self, proxy, spec, parent=None):
        super().__init__(parent)
        self.bind(proxy, spec)
        self.returnPressed.connect(lambda: self.proxy.set(self.spec.key, self.text().strip()))

    def show_value(self, value):
        if not (self.hasFocus() and self.isModified()):
            self.setText("" if value is None else str(value))


class EnumField(QComboBox, ParamWidget):
    def __init__(self, proxy, spec, parent=None):
        super().__init__(parent)
        labels = spec.labels or spec.choices
        for token, label in zip(spec.choices, labels):
            self.addItem(label, token)
        self.bind(proxy, spec)
        self.setCurrentIndex(-1)
        self.activated.connect(lambda i: self.proxy.set(self.spec.key, self.itemData(i)))

    def show_value(self, value):
        self.blockSignals(True)
        if value is None:
            self.setCurrentIndex(-1)
        else:
            i = self.findData(str(value).upper(), flags=Qt.MatchFlag.MatchFixedString)
            if i < 0:                      # прибор вернул значение вне списка
                self.addItem(str(value), str(value))
                i = self.count() - 1
            self.setCurrentIndex(i)
        self.blockSignals(False)


class ToggleField(QPushButton, ParamWidget):
    def __init__(self, proxy, spec, parent=None):
        super().__init__("—", parent)
        self.setCheckable(True)
        self.setProperty("class", "toggle")
        self.bind(proxy, spec)
        self.clicked.connect(self._click)

    def _click(self):
        # Кнопка не переключается сама: ждём подтверждения прибора.
        target = not bool(self._value)
        self.setChecked(bool(self._value))
        self.proxy.set(self.spec.key, target)

    def show_value(self, value):
        self.setChecked(bool(value))
        self.setText("—" if value is None else ("ВКЛ" if value else "ВЫКЛ"))


class Readout(QLabel, ParamWidget):
    """Только чтение: измерения, режим CV/CC, флаги."""

    def __init__(self, proxy, spec, parent=None):
        super().__init__("—", parent)
        self.bind(proxy, spec)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if spec.poll and spec.kind is Kind.FLOAT:
            self.setProperty("class", "readout")
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def show_value(self, value):
        if value is None:
            self.setText("—")
        elif self.spec.kind is Kind.FLOAT:
            self.setText(si_format(value, self.spec.unit, digits=5))
        elif self.spec.kind is Kind.BOOL:
            self.setText("ДА" if value else "нет")
            self.setStyleSheet("color: #d64545; font-weight: bold;" if value and "ovp" in self.spec.key else "")
        else:
            self.setText(str(value))


def make_widget(proxy: InstrumentProxy, spec: Param) -> QWidget:
    if spec.readonly:
        return Readout(proxy, spec)
    if spec.kind is Kind.BOOL:
        return ToggleField(proxy, spec)
    if spec.kind is Kind.ENUM:
        return EnumField(proxy, spec)
    if spec.kind in (Kind.FLOAT, Kind.INT):
        return NumberField(proxy, spec)
    return TextField(proxy, spec)
