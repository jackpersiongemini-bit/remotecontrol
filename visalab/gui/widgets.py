"""Виджеты параметров, привязанные к прибору координатора (Instrument).

Каждый виджет знает только свой ``Param`` и прокси: пишет через ``proxy.set``,
получает значения/статусы через ``proxy.subscribe`` (сигналы Qt из потока прибора).
Цвет рамки показывает PStatus: писать - жёлтый, округлено - оранжевый, ошибка - красный.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QSizePolicy, QWidget

from ..core.params import Kind, Param, PStatus
from ..coord import Instrument
from ..core.units import si_format, si_parse

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

    def bind(self, proxy: Instrument, spec: Param) -> None:
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


class TracePlot(QWidget, ParamWidget):
    """График трассы (visalab.core.trace.Trace) без внешних библиотек.

    Шкала Y - от опорного уровня вниз на 10 делений (как на экране анализатора), если у
    прибора есть ref_level/scale_div; иначе - по данным.
    """

    def __init__(self, proxy, spec, parent=None):
        super().__init__(parent)
        self.bind(proxy, spec)
        self.trace = None
        for key in ("marker.level", "marker.value", "marker.state", "ref_level", "scale_div"):
            if proxy.supports(key):
                proxy.subscribe(key, lambda _v: self.update())
        self.setMinimumSize(520, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def show_value(self, value):
        self.trace = value
        self.update()

    def _y_range(self) -> tuple[float, float]:
        ref, div = self.proxy.value("ref_level"), self.proxy.value("scale_div")
        if ref is not None and div:
            return ref - 10 * div, ref
        ys = self.trace.y
        lo, hi = min(ys), max(ys)
        return (lo - 1, hi + 1) if hi - lo < 1e-9 else (lo, hi)

    def paintEvent(self, event):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.base())
        left, top, right, bottom = 64, 10, self.width() - 12, self.height() - 26
        w, h = max(1, right - left), max(1, bottom - top)
        grid = QPen(QColor(128, 128, 128, 70))
        p.setPen(grid)
        for i in range(11):
            y = top + h * i / 10
            p.drawLine(QPointF(left, y), QPointF(right, y))
            x = left + w * i / 10
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
        p.setPen(pal.text().color())
        if not self.trace or len(self.trace) < 2:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "нет трассы")
            return
        t = self.trace
        y0, y1 = self._y_range()
        x0, x1 = t.x[0], t.x[-1]
        sx = w / (x1 - x0) if x1 != x0 else 0.0
        path = QPainterPath()
        for i, (xv, yv) in enumerate(zip(t.x, t.y)):
            px = left + (xv - x0) * sx if sx else left + w * i / (len(t) - 1)
            py = top + h * (y1 - min(max(yv, y0), y1)) / (y1 - y0)
            path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
        p.drawText(QPointF(4, top + 10), f"{y1:.1f}")
        p.drawText(QPointF(4, bottom), f"{y0:.1f}")
        p.drawText(QPointF(left, self.height() - 6), si_format(x0, t.x_unit))
        txt = si_format(x1, t.x_unit)
        p.drawText(QPointF(right - p.fontMetrics().horizontalAdvance(txt), self.height() - 6), txt)
        p.drawText(QPointF(4, top + h / 2), t.y_unit)
        p.setPen(QPen(QColor("#e0a020"), 1.4))
        p.drawPath(path)
        mx = self.proxy.value("marker.freq")
        my = self.proxy.value("marker.level", self.proxy.value("marker.value"))
        if self.proxy.value("marker.state") and mx is not None and my is not None and sx:
            px, py = left + (mx - x0) * sx, top + h * (y1 - min(max(my, y0), y1)) / (y1 - y0)
            p.setPen(QPen(QColor("#d64545"), 2))
            p.drawLine(QPointF(px - 6, py - 10), QPointF(px, py))
            p.drawLine(QPointF(px + 6, py - 10), QPointF(px, py))
            p.drawText(QPointF(min(px + 8, right - 150), max(top + 12, py - 12)),
                       f"{si_format(mx, 'Hz')}  {my:.3g} {t.y_unit}")


def make_widget(proxy: Instrument, spec: Param) -> QWidget:
    if spec.kind is Kind.TRACE:
        return TracePlot(proxy, spec)
    if spec.readonly:
        return Readout(proxy, spec)
    if spec.kind is Kind.BOOL:
        return ToggleField(proxy, spec)
    if spec.kind is Kind.ENUM:
        return EnumField(proxy, spec)
    if spec.kind in (Kind.FLOAT, Kind.INT):
        return NumberField(proxy, spec)
    return TextField(proxy, spec)
