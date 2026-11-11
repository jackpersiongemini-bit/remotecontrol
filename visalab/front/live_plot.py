"""График живых данных главного окна: несколько трасс, автошкала, легенда, курсор под мышью.

``set_traces([(подпись, Trace)])`` - линии (спектр, трейсы ВАЦ); ``points=True`` - ещё и точки
(показания ваттметра во времени). ``set_marker(x, y)`` - метка (пик спектра).
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from ..core.scpi import si_format
from ..core.trace import Trace
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_TEXT, UI_MONOSPACE_FONT_FAMILY, UI_TRACE_COLORS)

__all__ = ["LivePlot", "nice_range"]


def nice_range(lo: float, hi: float, divs: int) -> tuple[float, float, float]:
    """Шкала по данным: (низ, верх, цена деления) с шагом 1 / 2 / 5 × 10^n."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, float(divs), 1.0
    if hi - lo < 1e-12:
        lo, hi = lo - 0.5 * max(abs(lo), 1.0) * 0.1, hi + 0.5 * max(abs(hi), 1.0) * 0.1
    span = (hi - lo) * 1.1
    e = math.floor(math.log10(span / divs))
    for step in (st * 10.0 ** k for k in (e, e + 1, e + 2) for st in (1, 2, 5)):
        # данные посередине; не влезли из-за округления низа - низ по самому нижнему значению
        # (иначе -120..-5 dBm получали 50 dB/дел вместо 20)
        centered = math.floor((lo - (step * divs - (hi - lo)) / 2) / step + 1e-9) * step
        for bottom in (centered, math.floor(lo / step + 1e-9) * step):
            if bottom + step * divs >= hi and bottom <= lo:
                return bottom, bottom + step * divs, step
    return lo, hi, (hi - lo) / divs


class LivePlot(QWidget):
    DIVS_X, DIVS_Y = 10, 6

    def __init__(self, parent=None, min_height: int = 170):
        super().__init__(parent)
        self.traces: list[tuple[str, Trace]] = []
        self.points = False
        self.marker: tuple[float, float] | None = None
        self.x_range: tuple[float, float] | None = None      # фиксированная ось X (время ваттметра)
        self._scale: tuple[float, float, float] | None = None
        self._mouse: QPointF | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(min_height)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------ данные
    def set_traces(self, traces, points: bool = False) -> None:
        self.traces = [(n, t) for n, t in traces if isinstance(t, Trace) and len(t) > 0]
        self.points = points
        self._rescale()
        self.update()

    def set_marker(self, x: float | None, y: float | None = None) -> None:
        self.marker = (x, y) if x is not None and y is not None else None
        self.update()

    def _rescale(self) -> None:
        """Шкала держится, пока данные в ней и занимают больше четверти; иначе - новая."""
        ys = [v for _n, t in self.traces for v in t.y if math.isfinite(v)]
        if not ys:
            self._scale = None
            return
        lo, hi = min(ys), max(ys)
        s = self._scale
        if s and s[0] <= lo and hi <= s[1] and (hi - lo) >= (s[1] - s[0]) / 4:
            return
        self._scale = nice_range(lo, hi, self.DIVS_Y)

    # ------------------------------------------------------------ мышь
    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = e.position()
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self.update()

    # ------------------------------------------------------------ рисование
    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(6, 20, -6, -20)

    def paintEvent(self, e):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_BACKGROUND))
        p.drawRoundedRect(QRectF(self.rect()), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
        r = self._plot_rect()
        font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        font.setPointSizeF(8)
        p.setFont(font)
        p.setPen(QPen(QColor(UI_COLOR_BORDER), 1))
        for i in range(self.DIVS_X + 1):
            x = r.left() + r.width() * i / self.DIVS_X
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i in range(self.DIVS_Y + 1):
            y = r.top() + r.height() * i / self.DIVS_Y
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        muted = QColor(UI_COLOR_BORDER_LIGHT)
        if not self.traces or self._scale is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO DATA")
            return
        first = self.traces[0][1]
        if self.x_range:
            x0, x1 = self.x_range
        else:
            x0 = min(t.x[0] for _n, t in self.traces)
            x1 = max(t.x[-1] for _n, t in self.traces)
        lo, hi, step = self._scale

        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y):
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        p.save()
        p.setClipRect(r.adjusted(-3, -3, 3, 3))
        for i, (_name, t) in enumerate(self.traces):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            pts = [(px(x), py(y)) for x, y in zip(t.x, t.y) if math.isfinite(y)]
            if len(pts) > 1:
                path = QPainterPath(QPointF(*pts[0]))
                for xy in pts[1:]:
                    path.lineTo(*xy)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(color, 1.0 if self.points else 1.2))
                p.drawPath(path)
            if self.points or len(pts) == 1:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                for xy in pts:
                    p.drawEllipse(QPointF(*xy), 2.2, 2.2)
        if self.marker:                                   # пик: ромб и уровень
            mx, my = px(self.marker[0]), py(self.marker[1])
            p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(QPointF(mx, my - 5), QPointF(mx + 5, my), QPointF(mx, my + 5), QPointF(mx - 5, my))
        p.restore()

        # подписи: легенда сверху слева, шкала сверху справа, ось X снизу
        xu, yu = first.x_unit, first.y_unit
        x_legend = r.left() + 2
        for i, (name, _t) in enumerate(self.traces):
            if not name:
                continue
            p.setPen(QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)]))
            w = p.fontMetrics().horizontalAdvance(name) + 12
            p.drawText(QRectF(x_legend, 3, w, 14), Qt.AlignmentFlag.AlignLeft, name)
            x_legend += w
        p.setPen(muted)
        scale = f"{hi:g} {yu}  {step:g} {yu}/div"
        p.drawText(QRectF(r.left(), 3, r.width(), 14), Qt.AlignmentFlag.AlignRight, scale)
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 14)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, si_format(x0, xu, 4))
        if x1 != x0:
            p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter, f"{si_format((x1 - x0) / self.DIVS_X, xu, 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, si_format(x1, xu, 4))
        m = self._mouse
        if m is not None and r.contains(m) and len(first) > 0:        # курсор: точка первой трассы
            i = min(range(len(first)), key=lambda k: abs(px(first.x[k]) - m.x()))
            cx, cy = px(first.x[i]), py(first.y[i])
            p.setPen(QPen(muted, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(UI_COLOR_TEXT))
            p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
            p.setPen(QColor(UI_COLOR_TEXT))
            p.drawText(QRectF(r.left(), r.top() + 2, r.width() - 4, 14), Qt.AlignmentFlag.AlignRight,
                       f"{si_format(first.x[i], xu, 5)}  {first.y[i]:.2f} {yu}")
