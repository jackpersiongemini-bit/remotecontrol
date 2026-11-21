"""График живых данных главного окна: несколько трасс, автошкала, легенда, курсор под мышью.

``set_traces([(подпись, Trace)])`` - линии (спектр, трейсы ВАЦ); ``points=True`` - ещё и точки
(показания ваттметра во времени). ``set_marker(x, y)`` - метка (пик спектра).
"""
from __future__ import annotations

from bisect import bisect_left
import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QWidget

from ..core.units import si_format
from ..core.trace import Trace
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_TEXT, UI_MONOSPACE_FONT_FAMILY, UI_TRACE_COLORS)

__all__ = ["LivePlot", "nice_range", "decimate_trace_polygon", "nearest_index"]


def nice_range(lo: float, hi: float, divs: int) -> tuple[float, float, float]:
    """Шкала по данным: (низ, верх, цена деления) с шагом 1 / 2 / 5 × 10^n."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or divs <= 0:
        return 0.0, float(max(divs, 1)), 1.0
    if lo > hi:
        lo, hi = hi, lo
    if hi - lo < 1e-12:
        delta = 0.5 * max(abs(lo), 1.0) * 0.1
        lo, hi = lo - delta, hi + delta
    span = (hi - lo) * 1.1
    if not math.isfinite(span) or span <= 0:
        return 0.0, float(divs), 1.0
    e = math.floor(math.log10(span / divs))
    for step in (st * 10.0 ** k for k in (e, e + 1, e + 2) for st in (1, 2, 5)):
        if not math.isfinite(step) or step <= 0:
            continue
        # данные посередине; не влезли из-за округления низа - низ по самому нижнему значению
        # (иначе -120..-5 dBm получали 50 dB/дел вместо 20)
        centered = math.floor((lo - (step * divs - (hi - lo)) / 2) / step + 1e-9) * step
        for bottom in (centered, math.floor(lo / step + 1e-9) * step):
            if math.isfinite(bottom) and bottom + step * divs >= hi and bottom <= lo:
                return bottom, bottom + step * divs, step
    return lo, hi, (hi - lo) / divs


def nearest_index(trace: Trace, x_target: float) -> int:
    """Индекс ближайшей точки трассы к ``x_target``: O(1) для равномерной оси, O(log N) для отсортированной."""
    xs = trace.x
    n = len(xs)
    if n <= 1:
        return 0
    if trace.uniform and xs[-1] > xs[0]:
        i = round((x_target - xs[0]) * (n - 1) / (xs[-1] - xs[0]))
        return 0 if i < 0 else (n - 1 if i >= n else i)
    if xs[-1] > xs[0]:
        i = bisect_left(xs, x_target)
        if i <= 0:
            return 0
        if i >= n:
            return n - 1
        return i - 1 if abs(xs[i - 1] - x_target) <= abs(xs[i] - x_target) else i
    return min(range(n), key=lambda k: abs(xs[k] - x_target))


def _append_bucket(poly: QPolygonF, px_val: float, mn: float, mx: float, min_first: bool,
                   lo: float, hi: float, bottom: float, sy: float, y_transform) -> None:
    if not math.isfinite(px_val):
        return
    if y_transform is not None:
        mn = y_transform(mn)
        mx = y_transform(mx)
        if not (math.isfinite(mn) and math.isfinite(mx)):
            return
        if mn > mx:
            mn, mx = mx, mn
    elif not (math.isfinite(mn) and math.isfinite(mx)):
        return
    mn_c = lo if mn < lo else (hi if mn > hi else mn)
    mx_c = lo if mx < lo else (hi if mx > hi else mx)
    py_mn = bottom - (mn_c - lo) * sy
    if mn == mx:
        poly.append(QPointF(px_val, py_mn))
    else:
        py_mx = bottom - (mx_c - lo) * sy
        if min_first:
            poly.append(QPointF(px_val, py_mn))
            poly.append(QPointF(px_val, py_mx))
        else:
            poly.append(QPointF(px_val, py_mx))
            poly.append(QPointF(px_val, py_mn))


def decimate_trace_polygon(trace: Trace, x0: float, x1: float, lo: float, hi: float,
                           rect: QRectF, step_px: float = 2.0, y_transform=None) -> QPolygonF:
    """Построить QPolygonF из трассы с min/max-огибающей по пиксельным столбцам до расчёта координат."""
    w, h = rect.width(), rect.height()
    xs, ys = trace.x, trace.y
    n = len(ys)
    poly = QPolygonF()
    isfinite = math.isfinite
    if w <= 0 or h <= 0 or n == 0 or hi <= lo or not (isfinite(x0) and isfinite(x1) and isfinite(lo) and isfinite(hi)):
        return poly
    left, bottom = rect.left(), rect.bottom()
    sx = w / (x1 - x0) if x1 != x0 else 0.0
    sy = h / (hi - lo)
    center_x = rect.center().x()
    cols = max(1, int(w / step_px))

    if cols < 16 or n <= 2 * cols or sx == 0.0:
        for x, y in zip(xs, ys):
            if not (isfinite(x) and isfinite(y)):
                continue
            yv = y_transform(y) if y_transform is not None else y
            if not isfinite(yv):
                continue
            yc = lo if yv < lo else (hi if yv > hi else yv)
            px_val = left + (x - x0) * sx if sx else center_x
            poly.append(QPointF(px_val, bottom - (yc - lo) * sy))
        return poly

    tx0, tx1 = xs[0], xs[-1]
    if trace.uniform and n > 1 and tx1 > tx0:
        inv_step = (n - 1) / (tx1 - tx0)
        col_span = (x1 - x0) / cols
        prev_i = max(0, min(n, math.ceil((x0 - tx0) * inv_step - 1e-12)))
        end_lim = tx1 - 1e-12 * (tx1 - tx0)
        for c in range(cols):
            if c == cols - 1 and x1 >= end_lim:
                next_i = n
            else:
                next_i = max(prev_i, min(n, math.ceil((x0 + (c + 1) * col_span - tx0) * inv_step - 1e-12)))
            if next_i <= prev_i:
                continue
            chunk = ys[prev_i:next_i]
            px_val = left + (xs[prev_i] - x0) * sx
            mn = min(chunk)
            mx = max(chunk)
            if isfinite(mn) and isfinite(mx):
                min_first = True if mn == mx else (chunk.index(mn) <= chunk.index(mx))
                _append_bucket(poly, px_val, mn, mx, min_first, lo, hi, bottom, sy, y_transform)
            else:
                finite = [v for v in chunk if isfinite(v)]
                if finite:
                    mn = min(finite)
                    mx = max(finite)
                    min_first = True if mn == mx else (finite.index(mn) <= finite.index(mx))
                    _append_bucket(poly, px_val, mn, mx, min_first, lo, hi, bottom, sy, y_transform)
            prev_i = next_i
        return poly

    scale_col = (w / step_px) / (x1 - x0)
    cur_col: int | None = None
    first_x = min_y = max_y = 0.0
    min_first = True
    for x, y in zip(xs, ys):
        if not (isfinite(x) and isfinite(y)):
            continue
        col = int((x - x0) * scale_col)
        if col != cur_col:
            if cur_col is not None:
                _append_bucket(poly, left + (first_x - x0) * sx, min_y, max_y, min_first,
                               lo, hi, bottom, sy, y_transform)
            cur_col = col
            first_x = x
            min_y = max_y = y
            min_first = True
        else:
            if y < min_y:
                min_y = y
                min_first = False
            if y > max_y:
                max_y = y
                min_first = True
    if cur_col is not None:
        _append_bucket(poly, left + (first_x - x0) * sx, min_y, max_y, min_first,
                       lo, hi, bottom, sy, y_transform)
    return poly


class LivePlot(QWidget):
    DIVS_X, DIVS_Y = 10, 6
    _SI_UNITS = ("W", "s", "Hz", "V", "A")

    def __init__(self, parent=None, min_height: int = 240):
        super().__init__(parent)
        self.traces: list[tuple[str, Trace]] = []
        self.points = False
        self.marker: tuple[float, float] | None = None
        self.x_range: tuple[float, float] | None = None      # фиксированная ось X (время ваттметра)
        self._scale: tuple[float, float, float] | None = None
        self._fixed_scale: tuple[float, float, float] | None = None
        self._divs_y: int = self.DIVS_Y
        self._y_unit: str | None = None
        self._y_transform = None
        self._mouse: QPointF | None = None
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self._font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        self._font.setPointSizeF(8)
        self.setMouseTracking(True)
        self.setMinimumHeight(min_height)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------ данные
    def set_scale(self, scale: tuple[float, float, float] | None, *,
                  divs_y: int | None = None, y_unit: str | None = None,
                  y_transform=None) -> None:
        """Задать шкалу Y ``(lo, hi, step)`` от прибора; ``None`` - автошкала по данным."""
        new_divs = self.DIVS_Y if divs_y is None else max(1, int(divs_y))
        if scale is not None:
            lo, hi, step = float(scale[0]), float(scale[1]), float(scale[2])
            if math.isfinite(lo) and math.isfinite(hi) and math.isfinite(step) and hi > lo and step > 0:
                scale = (lo, hi, step)
            else:
                scale = None
        if (scale == self._fixed_scale and new_divs == self._divs_y
                and y_unit == self._y_unit and y_transform is self._y_transform):
            return
        self._fixed_scale = scale
        self._divs_y = new_divs
        self._y_unit = y_unit
        self._y_transform = y_transform
        if scale is not None:
            self._scale = scale
        else:
            self._scale = None
            self._rescale()
        self.update()

    def set_traces(self, traces, points: bool = False) -> None:
        cleaned = [(n, t) for n, t in traces if isinstance(t, Trace) and len(t) > 0]
        points = bool(points)
        if (points == self.points and len(cleaned) == len(self.traces)
                and all(n1 == n2 and t1 is t2 for (n1, t1), (n2, t2) in zip(cleaned, self.traces))):
            return
        self.traces = cleaned
        self.points = points
        self._rescale()
        self.update()

    def set_marker(self, x: float | None, y: float | None = None) -> None:
        marker = (x, y) if x is not None and y is not None else None
        if marker == self.marker:
            return
        self.marker = marker
        self.update()

    def _rescale(self) -> None:
        """Если задана шкала прибора - держим её; иначе автошкала (держится, пока данные > четверти)."""
        if self._fixed_scale is not None:
            self._scale = self._fixed_scale
            return
        lo = math.inf
        hi = -math.inf
        isfinite = math.isfinite
        yt = self._y_transform
        for _n, t in self.traces:
            y = t.y
            if yt is None:
                t_lo, t_hi = min(y), max(y)
                if isfinite(t_lo) and isfinite(t_hi):
                    if t_lo < lo:
                        lo = t_lo
                    if t_hi > hi:
                        hi = t_hi
                    continue
            for v in y:
                if not isfinite(v):
                    continue
                if yt is not None:
                    v = yt(v)
                    if not isfinite(v):
                        continue
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
        if lo > hi:
            self._scale = None
            return
        s = self._scale
        if s and s[0] <= lo and hi <= s[1] and (hi - lo) >= (s[1] - s[0]) / 4:
            return
        self._scale = nice_range(lo, hi, self._divs_y)

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

    def _x_bounds(self) -> tuple[float, float]:
        if self.x_range and math.isfinite(self.x_range[0]) and math.isfinite(self.x_range[1]):
            return self.x_range
        xs0 = [t.x[0] for _n, t in self.traces if t.x and math.isfinite(t.x[0])]
        xs1 = [t.x[-1] for _n, t in self.traces if t.x and math.isfinite(t.x[-1])]
        return (min(xs0), max(xs1)) if xs0 and xs1 else (0.0, 1.0)

    def _render_base(self) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        if not p.isActive():
            return pm
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_BACKGROUND))
        p.drawRoundedRect(QRectF(self.rect()), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
        r = self._plot_rect()
        p.setFont(self._font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        grid_pen = QPen(QColor(UI_COLOR_BORDER), 1)
        grid_pen.setCosmetic(True)
        p.setPen(grid_pen)
        for i in range(self.DIVS_X + 1):
            x = r.left() + r.width() * i / self.DIVS_X
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i in range(self._divs_y + 1):
            y = r.top() + r.height() * i / self._divs_y
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        muted = QColor(UI_COLOR_BORDER_LIGHT)
        if not self.traces or self._scale is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO DATA")
            p.end()
            return pm
        first = self.traces[0][1]
        x0, x1 = self._x_bounds()
        lo, hi, step = self._scale
        yt = self._y_transform

        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y):
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        p.save()
        p.setClipRect(r.adjusted(-3, -3, 3, 3))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for i, (_name, t) in enumerate(self.traces):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            if self.points or len(t) == 1:
                pts = []
                for x, y in zip(t.x, t.y):
                    if not math.isfinite(y):
                        continue
                    yv = yt(y) if yt is not None else y
                    if math.isfinite(yv):
                        pts.append(QPointF(px(x), py(yv)))
                if len(pts) > 1:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(color, 1.0))
                    p.drawPolyline(QPolygonF(pts))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                for pt in pts:
                    p.drawEllipse(pt, 2.2, 2.2)
            else:
                poly = decimate_trace_polygon(t, x0, x1, lo, hi, r, y_transform=yt)
                if len(poly) > 1:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    pen = QPen(color, 1)
                    pen.setCosmetic(True)
                    p.setPen(pen)
                    p.drawPolyline(poly)
        if self.marker and math.isfinite(self.marker[0]) and math.isfinite(self.marker[1]):
            my_val = yt(self.marker[1]) if yt is not None else self.marker[1]
            if math.isfinite(my_val):
                mx, my = px(self.marker[0]), py(my_val)
                p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolygon(QPointF(mx, my - 5), QPointF(mx + 5, my), QPointF(mx, my + 5), QPointF(mx - 5, my))
        p.restore()

        # подписи: легенда сверху слева, шкала сверху справа, ось X снизу
        xu = first.x_unit
        yu = self._y_unit if self._y_unit is not None else first.y_unit
        x_legend = r.left() + 2
        for i, (name, _t) in enumerate(self.traces):
            if not name:
                continue
            p.setPen(QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)]))
            w = p.fontMetrics().horizontalAdvance(name) + 12
            p.drawText(QRectF(x_legend, 3, w, 14), Qt.AlignmentFlag.AlignLeft, name)
            x_legend += w
        p.setPen(muted)
        if yu in self._SI_UNITS:
            scale = f"{si_format(hi, yu, 4)}  {si_format(step, yu, 3)}/div"
        else:
            div_u = "dB" if yu == "dBm" else yu
            top_s = f"{hi:g} {yu}" if yu else f"{hi:g}"
            div_s = f"{step:g} {div_u}/div" if div_u else f"{step:g}/div"
            scale = f"{top_s}  {div_s}"
        p.drawText(QRectF(r.left(), 3, r.width(), 14), Qt.AlignmentFlag.AlignRight, scale)
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 14)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, si_format(x0, xu, 4))
        if x1 != x0:
            p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter, f"{si_format((x1 - x0) / self.DIVS_X, xu, 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, si_format(x1, xu, 4))
        p.end()
        return pm

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.width() <= 0 or self.height() <= 0:
            return
        key = (self.width(), self.height(), self.devicePixelRatioF(),
               tuple((n, id(t)) for n, t in self.traces),
               self.points, self.marker, self.x_range, self._scale,
               self._divs_y, self._y_unit, self._y_transform)
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_base()
            self._cache_key = key
        p = QPainter(self)
        if not p.isActive():
            return
        p.drawPixmap(0, 0, self._cache)
        m = self._mouse
        if m is None or not self.traces or self._scale is None:
            return
        r = self._plot_rect()
        first = self.traces[0][1]
        if r.contains(m) and len(first) > 0:              # курсор: точка первой трассы
            x0, x1 = self._x_bounds()
            lo, hi, _step = self._scale
            x_target = x0 + (m.x() - r.left()) / r.width() * (x1 - x0) if x1 != x0 and r.width() > 0 else x0
            i = nearest_index(first, x_target)
            yi_raw = first.y[i]
            if not math.isfinite(yi_raw):
                return
            yi = self._y_transform(yi_raw) if self._y_transform is not None else yi_raw
            if not math.isfinite(yi):
                return
            cx = r.left() + (first.x[i] - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()
            cy = r.bottom() - (min(max(yi, lo), hi) - lo) / (hi - lo) * r.height()
            muted = QColor(UI_COLOR_BORDER_LIGHT)
            p.setFont(self._font)
            p.setPen(QPen(muted, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(UI_COLOR_TEXT))
            p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
            p.setPen(QColor(UI_COLOR_TEXT))
            yu = self._y_unit if self._y_unit is not None else first.y_unit
            y_txt = si_format(yi, yu, 4) if yu in self._SI_UNITS else (f"{yi:.2f} {yu}" if yu else f"{yi:.3g}")
            p.drawText(QRectF(r.left(), r.top() + 2, r.width() - 4, 14), Qt.AlignmentFlag.AlignRight,
                       f"{si_format(first.x[i], first.x_unit, 5)}  {y_txt}")
