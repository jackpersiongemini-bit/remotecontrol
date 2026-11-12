"""Редакторы значений, привязанные к InstrumentModel (не к прибору).

Каждый редактор знает свой ``FieldSpec`` и модель: просит запись через
``model.request``, а показывает только то, что пришло через ``value_changed``.
Кнопка-переключатель сама не переключается: ждёт подтверждения значения.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QWidget

from ..core.units import si_format, si_parse
from .model import FieldSpec, InstrumentModel, Kind

__all__ = ["make_editor", "format_value"]


def format_value(spec: FieldSpec, value: Any, digits: int = 6) -> str:
    if value is None:
        return "—"
    if spec.kind is Kind.FLOAT:
        return si_format(value, spec.unit, digits=digits)
    if spec.kind is Kind.BOOL:
        return "ON" if value else "OFF"
    if spec.kind is Kind.ENUM and spec.labels:
        for token, label in zip(spec.choices, spec.labels):
            if token == str(value).upper():
                return label
    return f"{value}{' ' + spec.unit if spec.unit and spec.kind is Kind.INT else ''}"


class _Bound:
    def bind(self, model: InstrumentModel, spec: FieldSpec) -> None:
        self.model, self.spec = model, spec
        self._value: Any = None
        model.value_changed.connect(self._on_value)
        tip = spec.key + (f"  [{spec.unit}]" if spec.unit else "")
        if spec.minimum is not None or spec.maximum is not None:
            tip += f"\nrange: {spec.minimum} … {spec.maximum}"
        self.setToolTip(tip)
        self._on_value(spec.key, model.value(spec.key))
        model.state_changed.connect(self._on_state)
        self._on_state(model.state)

    def _on_state(self, state: str) -> None:
        self.setEnabled(state == "connected")

    def _on_value(self, key: str, value: Any) -> None:
        if key != self.spec.key:
            return
        self._value = value
        self.setProperty("invalid", False)
        self._repolish()
        self.show_value(value)

    def _repolish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    def mark_invalid(self, message: str) -> None:
        self.setProperty("invalid", True)
        self._repolish()
        self.setToolTip(self.toolTip().split("\n⚠")[0] + f"\n⚠ {message}")

    def show_value(self, value):  # pragma: no cover - переопределяется
        raise NotImplementedError


class NumberEditor(QLineEdit, _Bound):
    """Число с SI-префиксами: ``1.5k``, ``2 MHz``, ``20m``. Ввод - Enter."""

    def __init__(self, model, spec, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bind(model, spec)
        self.returnPressed.connect(self._commit)

    def show_value(self, value):
        if self.hasFocus() and self.isModified():
            return
        self.setText("" if value is None else format_value(self.spec, value))
        self.setPlaceholderText("—")
        self.setModified(False)

    def _commit(self):
        try:
            v = si_parse(self.text(), self.spec.unit)
            if self.spec.kind is Kind.INT:
                v = int(round(v))
            if self.spec.minimum is not None and v < self.spec.minimum:
                raise ValueError(f"below minimum {self.spec.minimum}")
            if self.spec.maximum is not None and v > self.spec.maximum:
                raise ValueError(f"above maximum {self.spec.maximum}")
        except ValueError as exc:
            self.mark_invalid(str(exc))
            return
        self.setModified(False)
        self.model.request(self.spec.key, v)


class TextEditor(QLineEdit, _Bound):
    def __init__(self, model, spec, parent=None):
        super().__init__(parent)
        self.bind(model, spec)
        self.returnPressed.connect(lambda: self.model.request(self.spec.key, self.text().strip()))

    def show_value(self, value):
        if not (self.hasFocus() and self.isModified()):
            self.setText("" if value is None else str(value))


class EnumEditor(QComboBox, _Bound):
    def __init__(self, model, spec, parent=None):
        super().__init__(parent)
        for token, label in zip(spec.choices, spec.labels or spec.choices):
            self.addItem(label, token)
        self.bind(model, spec)
        self.activated.connect(lambda i: self.model.request(self.spec.key, self.itemData(i)))

    def show_value(self, value):
        self.blockSignals(True)
        i = -1 if value is None else self.findData(str(value).upper())
        if value is not None and i < 0:
            self.addItem(str(value), str(value))
            i = self.count() - 1
        self.setCurrentIndex(i)
        self.blockSignals(False)


class ToggleEditor(QPushButton, _Bound):
    def __init__(self, model, spec, parent=None, text: str | None = None):
        super().__init__(parent)
        self._caption = text
        self.setCheckable(True)
        self.setProperty("role", "toggle")
        self.bind(model, spec)
        self.clicked.connect(self._click)

    def _click(self):
        self.setChecked(bool(self._value))          # ждём подтверждения
        self.model.request(self.spec.key, not bool(self._value))

    def show_value(self, value):
        self.setChecked(bool(value))
        state = "—" if value is None else ("ON" if value else "OFF")
        self.setText(f"{self._caption}  {state}" if self._caption else state)


class ValueLabel(QLabel, _Bound):
    def __init__(self, model, spec, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setProperty("role", "value")
        self.bind(model, spec)

    def show_value(self, value):
        self.setText(format_value(self.spec, value))


def make_editor(model: InstrumentModel, spec: FieldSpec, parent: QWidget | None = None) -> QWidget:
    if spec.readonly:
        return ValueLabel(model, spec, parent)
    if spec.kind is Kind.BOOL:
        return ToggleEditor(model, spec, parent)
    if spec.kind is Kind.ENUM:
        return EnumEditor(model, spec, parent)
    if spec.kind in (Kind.FLOAT, Kind.INT):
        return NumberEditor(model, spec, parent)
    return TextEditor(model, spec, parent)
