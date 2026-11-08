"""Модели приборов, заданные пользователем (пределы и функции), и окно их создания.

Модель - frozen dataclass драйвера (SDGModel, RFGenModel, ...). Пользовательские модели
хранятся в ini окна (``MODELS/<driver>/<name>`` = JSON) и дополняют встроенные ``MODELS``.
Форма строится по полям dataclass, поэтому окно годится для любого драйвера.
"""
from __future__ import annotations

import dataclasses
import json
import typing
from typing import Any

from PyQt6.QtCore import QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..core.scpi import si_format, si_parse
from ..drivers import MODELS
from .params import Panel, PanelLine, PanelTable
from .theme import UI_COLUMN_WIDTH_PX, WINDOW_BAR_HEIGHT
from .window import Window

__all__ = ["FIELD_META", "model_class", "user_models", "all_models", "save_user_model", "ModelWindow"]

#: короткие подписи и единицы полей моделей
FIELD_META: dict[str, tuple[str, str]] = {
    "name": ("NAME", ""), "channels": ("CH", ""), "max_freq": ("FMAX", "Hz"), "max_amp_hiz": ("AMAX", "Vpp"),
    "has_cmr": ("CMR", ""), "f_min": ("FMIN", "Hz"), "f_max": ("FMAX", "Hz"), "p_min": ("PMIN", "dBm"),
    "p_max": ("PMAX", "dBm"), "max_points": ("PTS", ""), "ratings": ("U,I", ""),
}


def model_class(kind: str):
    """Класс модели драйвера (по встроенным моделям); None - у драйвера моделей нет."""
    models = MODELS.get(kind) or {}
    return type(next(iter(models.values()))) if models else None


def _hints(cls) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _to_tuple(v):
    return tuple(_to_tuple(x) for x in v) if isinstance(v, (list, tuple)) else v


def _from_dict(cls, data: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: _to_tuple(v) for k, v in data.items() if k in names})


def user_models(settings: QSettings, kind: str) -> dict[str, Any]:
    cls = model_class(kind)
    if cls is None:
        return {}
    out = {}
    settings.beginGroup(f"MODELS/{kind}")
    try:
        for name in settings.childKeys():
            try:
                out[name] = _from_dict(cls, json.loads(str(settings.value(name))))
            except (ValueError, TypeError):
                continue                      # испорченная запись не мешает остальным
    finally:
        settings.endGroup()
    return out


def all_models(settings: QSettings, kind: str) -> dict[str, Any]:
    """Встроенные + пользовательские (пользовательские не перекрывают встроенные)."""
    return {**user_models(settings, kind), **(MODELS.get(kind) or {})}


def save_user_model(settings: QSettings, kind: str, model) -> None:
    settings.setValue(f"MODELS/{kind}/{model.name}", json.dumps(dataclasses.asdict(model)))
    settings.sync()


# ------------------------------------------------------------------ окно
class _Field:
    """Поле формы: QLineEdit (число / текст) или кнопка-флажок (bool)."""

    def __init__(self, name: str, typ, value):
        self.name, self.typ = name, typ
        self.label, self.unit = FIELD_META.get(name, (name.upper()[:5], ""))
        if typ is bool:
            self.widget = QPushButton()
            self.widget.setObjectName("choice")          # как выпадающие кнопки: высота поля
            self.widget.setCheckable(True)
            self.widget.setCursor(Qt.CursorShape.PointingHandCursor)
            self.widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.widget.toggled.connect(lambda on: self.widget.setText("YES" if on else "NO"))
            self.widget.setChecked(bool(value))
            self.widget.setText("YES" if value else "NO")
        else:
            self.widget = QLineEdit(self._fmt(value))
            self.widget.setObjectName("cfg")
            if self.unit:
                self.widget.setPlaceholderText(self.unit)
        self.widget.setToolTip(f"{name}" + (f", {self.unit}" if self.unit else ""))

    def _fmt(self, value) -> str:
        if value is None:
            return ""
        if self.typ is float:
            return si_format(value, self.unit) if self.unit else f"{value:g}"
        if isinstance(value, tuple):
            return json.dumps(value)
        return str(value)

    def value(self):
        if self.typ is bool:
            return self.widget.isChecked()
        text = self.widget.text().strip()
        if self.typ is str:
            if not text:
                raise ValueError(f"{self.label}: empty")
            return text
        if self.typ is int:
            return int(float(text))
        if self.typ is float:
            try:
                return float(text.replace(",", "."))
            except ValueError:
                return si_parse(text, self.unit)
        return _to_tuple(json.loads(text))     # кортежи (например, номиналы по каналам)


class ModelWindow(Window):
    """Создание модели: пределы и функции по полям класса модели драйвера. SAVE - в ini окна."""

    saved = pyqtSignal(str)

    def __init__(self, settings: QSettings, kind: str, template=None, parent=None):
        super().__init__(settings, parent)
        self.kind = kind
        self.cls = model_class(kind)
        if self.cls is None:
            raise ValueError(f"{kind}: driver has no models")
        self.setWindowTitle(f"New {kind.upper()} model")
        hints = _hints(self.cls)
        self.fields: list[_Field] = []
        for f in dataclasses.fields(self.cls):
            value = getattr(template, f.name) if template is not None else (
                None if f.default is dataclasses.MISSING else f.default)
            if f.name == "name" and template is not None:
                value = f"{value}-custom"
            self.fields.append(_Field(f.name, hints.get(f.name, str), value))
        self.status = QLabel("")
        self.status.setObjectName("lineName")
        self.save_button = self._button("SAVE", "Save the model to the settings file", self.save)
        self.cancel_button = self._button("CANCEL", "Close without saving", self.close)
        table = PanelTable(*(PanelLine(fl.label, fl.widget) for fl in self.fields))
        buttons = QWidget()
        bl = QHBoxLayout(buttons)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        bl.addWidget(self.status, 1)
        bl.addWidget(self.cancel_button)
        bl.addWidget(self.save_button)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(table)
        lay.addWidget(buttons)
        panel = Panel(body)
        table.build(UI_COLUMN_WIDTH_PX * 2)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        outer.addWidget(panel)
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), outer.minimumSize().width()))
        self.recall_size(QSize(self.minimumWidth(), 0))
        self.setFixedHeight(outer.sizeHint().height())

    @staticmethod
    def _button(text, tip, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        b.setProperty("wide", True)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(slot)
        return b

    def model(self):
        return self.cls(**{fl.name: fl.value() for fl in self.fields})

    def save(self) -> bool:
        try:
            model = self.model()
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc).splitlines()[0][:40])
            self.status.setToolTip(str(exc))
            return False
        if model.name in (MODELS.get(self.kind) or {}):
            self.status.setText("NAME is built-in")
            return False
        save_user_model(self.settings, self.kind, model)
        self.saved.emit(model.name)
        self.close()
        return True
