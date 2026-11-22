"""График живых данных главного окна: несколько трасс, автошкала, легенда, курсор под мышью.

``set_traces([(подпись, Trace)])`` - линии (спектр, трейсы ВАЦ); ``points=True`` - ещё и точки
(показания ваттметра во времени). ``set_marker(x, y)`` - метка (пик спектра).
"""
from __future__ import annotations

from bisect import bisect_left
import json
import math

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QWidget

from ..core.units import si_format
from ..core.trace import Trace
from .plot_tools import ANALYSIS_KEYS, MATH_MODES, analyze_trace, format_analysis, trace_math, validate_math_expr
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_ERROR, UI_COLOR_SUCCESS, UI_COLOR_TEXT, UI_MONOSPACE_FONT_FAMILY, UI_TRACE_COLORS,
                    current_theme, mono_font)

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
    settings_changed = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self, parent=None, min_height: int = 240):
        super().__init__(parent)
        self.traces: list[tuple[str, Trace]] = []
        self.points = False
        self.marker: tuple[float, float] | None = None
        self.markers: list[tuple[str, float, float, int]] = []
        self.limit_lines: list[tuple[str, float, float, float, float]] = []
        self.limit_fail: bool | None = None
        self.x_range: tuple[float, float] | None = None      # фиксированная ось X (время ваттметра)
        self._scale: tuple[float, float, float] | None = None
        self._fixed_scale: tuple[float, float, float] | None = None
        self._device_divs_y: int = self.DIVS_Y
        self._divs_y: int = self.DIVS_Y
        self._y_unit: str | None = None
        self._y_transform = None

        # Пользовательские настройки шкалы, памяти (MEM), математики (MATH) и анализа
        self.manual_scale: bool = False
        self._manual_scale: tuple[float, float, float] | None = None
        self._manual_divs_y: int = self.DIVS_Y
        self.manual_x_range: tuple[float, float] | None = None
        self.show_grid: bool = True
        self.show_points: bool = False
        self.memory: list[tuple[str, Trace]] = []
        self.memory_visible: bool = True
        self.math_mode: str = "OFF"
        self.math_expr: str = ""
        self.analysis_metrics: list[str] = []
        self._settings: QSettings | None = None
        self._settings_key: str = ""
        self._math_cache: dict[tuple, Trace] = {}

        self._mouse: QPointF | None = None
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self._font = mono_font()
        self.setMouseTracking(True)
        self.setMinimumHeight(min_height)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Double-click: store / clear MEM · Wheel: zoom Y")

    @property
    def memory_diff(self) -> bool:
        return self.math_mode == "SUB" and bool(self.memory)

    @memory_diff.setter
    def memory_diff(self, on: bool) -> None:
        if on and self.memory:
            self.math_mode = "SUB"
        elif self.math_mode == "SUB":
            self.math_mode = "OFF"

    def _math_active(self) -> bool:
        if self.math_mode == "OFF":
            return False
        if self.math_mode == "EXPR":
            return bool(self.math_expr.strip()) and validate_math_expr(self.math_expr)[0]
        return bool(self.memory)

    # ------------------------------------------------------------ сохранение настроек графика
    def bind_settings(self, settings: QSettings | None, key: str) -> None:
        self._settings = settings
        self._settings_key = key
        if settings is None or not key:
            return
        base = key
        self.show_grid = bool(settings.value(f"{base}/GRID", True, type=bool))
        self.show_points = bool(settings.value(f"{base}/POINTS", False, type=bool))
        raw_an = settings.value(f"{base}/ANALYSIS", "", type=str)
        try:
            items = json.loads(raw_an) if raw_an else []
            if isinstance(items, list):
                self.analysis_metrics = [k for k in items if k in ANALYSIS_KEYS]
        except (TypeError, ValueError):
            self.analysis_metrics = []
        man = bool(settings.value(f"{base}/MANUAL_SCALE", False, type=bool))
        ref_raw = settings.value(f"{base}/MAN_REF", None)
        div_raw = settings.value(f"{base}/MAN_DIV", None)
        divs_raw = settings.value(f"{base}/MAN_DIVS", None)
        if man and ref_raw not in (None, "") and div_raw not in (None, ""):
            try:
                ref = float(ref_raw)
                div = float(div_raw)
                divs = max(2, min(20, int(divs_raw))) if divs_raw not in (None, "") else self.DIVS_Y
                if math.isfinite(ref) and math.isfinite(div) and div > 0:
                    self.manual_scale = True
                    self._manual_divs_y = divs
                    self._manual_scale = (ref - divs * div, ref, div)
                    self._divs_y = divs
            except (TypeError, ValueError):
                pass
        x0_raw = settings.value(f"{base}/X_MIN", None)
        x1_raw = settings.value(f"{base}/X_MAX", None)
        if x0_raw not in (None, "") and x1_raw not in (None, ""):
            try:
                x0, x1 = float(x0_raw), float(x1_raw)
                if math.isfinite(x0) and math.isfinite(x1) and x1 > x0:
                    self.manual_x_range = (x0, x1)
            except (TypeError, ValueError):
                pass
        saved_expr = str(settings.value(f"{base}/MATH_EXPR", "", type=str) or "").strip()
        saved_mode = str(settings.value(f"{base}/MATH_MODE", "OFF", type=str) or "OFF").upper()
        self.math_expr = saved_expr if (saved_expr and validate_math_expr(saved_expr)[0]) else ""
        if saved_mode == "EXPR" and self.math_expr:
            self.math_mode = "EXPR"
        else:
            self.math_mode = "OFF"
        self._rescale()
        self.update()

    def _save_settings(self) -> None:
        if self._settings is None or not self._settings_key:
            return
        base = self._settings_key
        s = self._settings
        s.setValue(f"{base}/GRID", self.show_grid)
        s.setValue(f"{base}/POINTS", self.show_points)
        s.setValue(f"{base}/ANALYSIS", json.dumps(self.analysis_metrics))
        s.setValue(f"{base}/MANUAL_SCALE", self.manual_scale)
        s.setValue(f"{base}/MATH_MODE", self.math_mode if self.math_mode == "EXPR" else "OFF")
        s.setValue(f"{base}/MATH_EXPR", self.math_expr)
        if self.manual_scale and self._manual_scale is not None:
            _lo, hi, step = self._manual_scale
            s.setValue(f"{base}/MAN_REF", hi)
            s.setValue(f"{base}/MAN_DIV", step)
            s.setValue(f"{base}/MAN_DIVS", self._manual_divs_y)
        if self.manual_x_range is not None:
            s.setValue(f"{base}/X_MIN", self.manual_x_range[0])
            s.setValue(f"{base}/X_MAX", self.manual_x_range[1])
        else:
            s.remove(f"{base}/X_MIN")
            s.remove(f"{base}/X_MAX")

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
        if (scale == self._fixed_scale and new_divs == self._device_divs_y
                and y_unit == self._y_unit and y_transform is self._y_transform):
            return
        self._fixed_scale = scale
        self._device_divs_y = new_divs
        self._y_unit = y_unit
        self._y_transform = y_transform
        if not self.manual_scale:
            self._divs_y = new_divs
            if scale is not None and not self._math_active():
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
        self._math_cache.clear()
        self._rescale()
        self.update()

    def set_marker(self, x: float | None, y: float | None = None) -> None:
        marker = (x, y) if x is not None and y is not None else None
        if marker == self.marker:
            return
        self.marker = marker
        self.update()

    def set_markers(self, markers) -> None:
        """Задать список именованных маркеров ``[(label, x, y[, color_idx]), ...]`` (например, маркеры ВАЦ)."""
        cleaned: list[tuple[str, float, float, int]] = []
        for item in (markers or ()):
            if len(item) == 3:
                label, x, y = item
                cidx = 0
            elif len(item) >= 4:
                label, x, y, cidx = item[0], item[1], item[2], int(item[3])
            else:
                continue
            if x is None or y is None:
                continue
            xf, yf = float(x), float(y)
            if math.isfinite(xf) and math.isfinite(yf):
                cleaned.append((str(label), xf, yf, cidx))
        if cleaned == self.markers:
            return
        self.markers = cleaned
        self.update()

    def set_limit_lines(self, segments, fail: bool | None = None) -> None:
        """Задать сегменты limit line ``[(kind, x1, x2, y1, y2), ...]`` (``kind`` = ``'MAX'`` | ``'MIN'``)
        и результат limit test (``True`` = FAIL, ``False`` = PASS, ``None`` = только отображение)."""
        cleaned: list[tuple[str, float, float, float, float]] = []
        for seg in (segments or ()):
            if len(seg) < 5:
                continue
            kind = str(seg[0]).upper()
            if kind not in ("MAX", "MIN"):
                continue
            vals = tuple(float(v) for v in seg[1:5])
            if all(math.isfinite(v) for v in vals):
                cleaned.append((kind, vals[0], vals[1], vals[2], vals[3]))
        fail_val = None if fail is None else bool(fail)
        if cleaned == self.limit_lines and fail_val == self.limit_fail:
            return
        self.limit_lines = cleaned
        self.limit_fail = fail_val
        self.update()

    # ------------------------------------------------------------ настройки шкалы, MEM и анализа
    def set_manual_scale(self, ref: float, per_div: float, divs_y: int | None = None) -> None:
        ref, per_div = float(ref), float(per_div)
        if not (math.isfinite(ref) and math.isfinite(per_div) and per_div > 0):
            return
        divs = self._manual_divs_y if divs_y is None else max(2, min(20, int(divs_y)))
        self.manual_scale = True
        self._manual_divs_y = divs
        self._manual_scale = (ref - divs * per_div, ref, per_div)
        self._divs_y = divs
        self._scale = self._manual_scale
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def set_auto_scale(self) -> None:
        self.manual_scale = False
        self._manual_scale = None
        self._divs_y = self._device_divs_y
        self._scale = None
        self._rescale()
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def autoscale_once(self) -> None:
        """Подогнать шкалу Y плотно по текущим данным и зафиксировать в MAN."""
        eff, yt = self._effective_traces()
        lo, hi = self._data_y_bounds(eff, yt)
        if lo is None or hi is None:
            return
        divs = self._divs_y
        lo_n, hi_n, step_n = nice_range(lo, hi, divs)
        self.set_manual_scale(hi_n, step_n, divs)

    def set_x_zoom(self, x_min: float | None, x_max: float | None) -> None:
        if x_min is None or x_max is None:
            self.manual_x_range = None
        else:
            x0, x1 = float(x_min), float(x_max)
            if math.isfinite(x0) and math.isfinite(x1) and x1 > x0:
                self.manual_x_range = (x0, x1)
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def reset_view(self) -> None:
        self.manual_x_range = None
        self.set_auto_scale()

    def set_show_grid(self, on: bool) -> None:
        on = bool(on)
        if on == self.show_grid:
            return
        self.show_grid = on
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def set_show_points(self, on: bool) -> None:
        on = bool(on)
        if on == self.show_points:
            return
        self.show_points = on
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def store_memory(self) -> bool:
        """Запомнить текущую(ие) трассу(ы) в память MEM."""
        if not self.traces:
            return False
        self.memory = [(n, t) for n, t in self.traces]
        self.memory_visible = True
        self._math_cache.clear()
        self._rescale()
        self.settings_changed.emit()
        self.update()
        return True

    def clear_memory(self) -> None:
        if not self.memory and self.math_mode not in ("SUB", "ADD", "DIV", "MUL"):
            return
        self.memory = []
        if self.math_mode in ("SUB", "ADD", "DIV", "MUL"):
            self.math_mode = "OFF"
        self._math_cache.clear()
        if not self.manual_scale:
            self._scale = None
            self._rescale()
        self.settings_changed.emit()
        self.update()

    def toggle_memory(self) -> bool:
        """Двойной клик / кнопка MEM: если памяти нет - сохранить, если есть - очистить."""
        if self.memory:
            self.clear_memory()
            return False
        return self.store_memory()

    def set_memory_visible(self, on: bool) -> None:
        self.memory_visible = bool(on)
        self.settings_changed.emit()
        self.update()

    def set_memory_diff(self, on: bool) -> None:
        self.set_math("SUB" if (bool(on) and bool(self.memory)) else "OFF")

    def set_math(self, mode: str, expr: str | None = None) -> None:
        """Задать режим математики графика: ``OFF / SUB / ADD / DIV / MUL / EXPR``."""
        mode = str(mode or "OFF").upper()
        if mode not in MATH_MODES:
            mode = "OFF"
        if expr is not None:
            self.math_expr = str(expr).strip()
        if mode in ("SUB", "ADD", "DIV", "MUL") and not self.memory:
            mode = "OFF"
        if mode == "EXPR" and (not self.math_expr or not validate_math_expr(self.math_expr)[0]):
            mode = "OFF"
        self.math_mode = mode
        self._math_cache.clear()
        if not self.manual_scale:
            self._scale = None
            self._rescale()
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def set_analysis_metrics(self, metrics: list[str] | tuple[str, ...]) -> None:
        cleaned = [k for k in ANALYSIS_KEYS if k in set(metrics)]
        if cleaned == self.analysis_metrics:
            return
        self.analysis_metrics = cleaned
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def toggle_analysis_metric(self, key: str, on: bool) -> None:
        if key not in ANALYSIS_KEYS:
            return
        cur = set(self.analysis_metrics)
        if on:
            cur.add(key)
        else:
            cur.discard(key)
        self.set_analysis_metrics(list(cur))

    def plot_settings_state(self) -> dict:
        eff, _yt = self._effective_traces()
        first = eff[0][1] if eff else (self.traces[0][1] if self.traces else None)
        yu = self._effective_y_unit(first)
        xu = first.x_unit if first is not None else ""
        ref = self._scale[1] if self._scale is not None else None
        div = self._scale[2] if self._scale is not None else None
        return {
            "manual_scale": self.manual_scale,
            "ref": ref,
            "div": div,
            "divs_y": self._divs_y,
            "y_unit": yu,
            "x_unit": xu,
            "x_bounds": self._x_bounds(),
            "manual_x_range": self.manual_x_range,
            "show_grid": self.show_grid,
            "show_points": self.show_points,
            "has_memory": bool(self.memory),
            "memory_count": len(self.memory),
            "memory_visible": self.memory_visible,
            "memory_diff": self.memory_diff,
            "math_mode": self.math_mode,
            "math_expr": self.math_expr,
            "analysis_metrics": list(self.analysis_metrics),
            "supports_divs": True,
            "supports_x_zoom": True,
            "supports_points": True,
        }

    # ------------------------------------------------------------ трассы, шкала и анализ
    def _effective_traces(self) -> tuple[list[tuple[str, Trace]], object]:
        if self._math_active() and self.traces:
            out: list[tuple[str, Trace]] = []
            yt_id = id(self._y_transform)
            for i, (name, tr) in enumerate(self.traces):
                mem_tr = (self.memory[i][1] if i < len(self.memory) else self.memory[0][1]) if self.memory else None
                ck = (id(tr), id(mem_tr), yt_id, self.math_mode, self.math_expr)
                math_tr = self._math_cache.get(ck)
                if math_tr is None:
                    math_tr = trace_math(tr, mem_tr, self.math_mode, self.math_expr, y_transform=self._y_transform)
                    self._math_cache[ck] = math_tr
                prefix = {"SUB": "Δ", "ADD": "+", "DIV": "÷", "MUL": "×", "EXPR": "f:"}.get(self.math_mode, "")
                out.append((f"{prefix}{name}" if name else math_tr.name, math_tr))
            return out, None
        return self.traces, self._y_transform

    def _effective_y_unit(self, first: Trace | None) -> str:
        if self._math_active() and first is not None:
            return first.y_unit
        if self._y_unit is not None:
            return self._y_unit
        return first.y_unit if first is not None else ""

    def _data_y_bounds(self, traces: list[tuple[str, Trace]], yt) -> tuple[float | None, float | None]:
        lo = math.inf
        hi = -math.inf
        isfinite = math.isfinite
        x_lim = self.manual_x_range
        for _n, t in traces:
            xs, ys = t.x, t.y
            if yt is None and x_lim is None:
                t_lo, t_hi = min(ys), max(ys)
                if isfinite(t_lo) and isfinite(t_hi):
                    if t_lo < lo:
                        lo = t_lo
                    if t_hi > hi:
                        hi = t_hi
                    continue
            for x, v in zip(xs, ys):
                if not (isfinite(x) and isfinite(v)):
                    continue
                if x_lim is not None and (x < x_lim[0] or x > x_lim[1]):
                    continue
                if yt is not None:
                    v = yt(v)
                    if not isfinite(v):
                        continue
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
        return (None, None) if lo > hi else (lo, hi)

    def _rescale(self) -> None:
        """Ручная шкала > шкала прибора > автошкала по данным."""
        if self.manual_scale and self._manual_scale is not None:
            self._divs_y = self._manual_divs_y
            self._scale = self._manual_scale
            return
        self._divs_y = self._device_divs_y
        if self._fixed_scale is not None and not self._math_active():
            self._scale = self._fixed_scale
            return
        eff, yt = self._effective_traces()
        lo, hi = self._data_y_bounds(eff, yt)
        if lo is None or hi is None:
            self._scale = None
            return
        s = self._scale
        if s and s[0] <= lo and hi <= s[1] and (hi - lo) >= (s[1] - s[0]) / 4:
            return
        self._scale = nice_range(lo, hi, self._divs_y)

    def analysis_results(self) -> dict:
        eff, yt = self._effective_traces()
        if not eff:
            return {}
        return analyze_trace(eff[0][1], x_range=self.manual_x_range, y_transform=yt)

    def analysis_lines(self) -> list[str]:
        if not self.analysis_metrics:
            return []
        eff, yt = self._effective_traces()
        if not eff:
            return []
        first = eff[0][1]
        stats = analyze_trace(first, x_range=self.manual_x_range, y_transform=yt)
        tokens = format_analysis(stats, self.analysis_metrics, first.x_unit, self._effective_y_unit(first))
        if not tokens:
            return []
        fm = QFontMetrics(self._font)
        max_w = max(120, self.width() - 16)
        lines: list[str] = []
        cur = ""
        for tok in tokens:
            cand = f"{cur}  ·  {tok}" if cur else tok
            if cur and fm.horizontalAdvance(cand) > max_w:
                lines.append(cur)
                cur = tok
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    # ------------------------------------------------------------ мышь
    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = e.position()
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self.update()

    def mouseDoubleClickEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton:
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.store_memory()
            else:
                self.toggle_memory()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def wheelEvent(self, e):  # noqa: N802 - Qt
        delta = e.angleDelta().y()
        if delta == 0 or self._scale is None:
            super().wheelEvent(e)
            return
        lo, hi, step = self._scale
        divs = self._divs_y
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            shift = step * 0.5 * (1 if delta > 0 else -1)
            self.set_manual_scale(hi + shift, step, divs)
        else:
            factor = 0.8 if delta > 0 else 1.25
            new_step = max(1e-12, step * factor)
            center = (lo + hi) * 0.5
            new_hi = center + new_step * divs * 0.5
            self.set_manual_scale(new_hi, new_step, divs)
        e.accept()

    # ------------------------------------------------------------ рисование
    def _analysis_height(self, lines: list[str] | None = None) -> int:
        if lines is None:
            lines = self.analysis_lines()
        return (len(lines) * 18 + 6) if lines else 0

    def _plot_rect(self, analysis_h: int | None = None) -> QRectF:
        ah = self._analysis_height() if analysis_h is None else analysis_h
        return QRectF(self.rect()).adjusted(8, 25, -8, -(25 + ah))

    def _x_bounds(self) -> tuple[float, float]:
        if self.manual_x_range is not None:
            return self.manual_x_range
        if self.x_range and math.isfinite(self.x_range[0]) and math.isfinite(self.x_range[1]):
            return self.x_range
        eff, _yt = self._effective_traces()
        src = eff if eff else self.traces
        xs0 = [t.x[0] for _n, t in src if t.x and math.isfinite(t.x[0])]
        xs1 = [t.x[-1] for _n, t in src if t.x and math.isfinite(t.x[-1])]
        return (min(xs0), max(xs1)) if xs0 and xs1 else (0.0, 1.0)

    def _draw_trace(self, p: QPainter, t: Trace, x0: float, x1: float, lo: float, hi: float,
                    r: QRectF, yt, color: QColor, *, dashed: bool = False, draw_pts: bool = False) -> None:
        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y):
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        if (draw_pts or len(t) == 1) and not dashed:
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
                pen = QPen(color, 1, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawPolyline(poly)

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

        an_lines = self.analysis_lines()
        ah = self._analysis_height(an_lines)
        r = self._plot_rect(ah)
        p.setFont(self._font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        grid_pen = QPen(QColor(UI_COLOR_BORDER), 1)
        grid_pen.setCosmetic(True)
        p.setPen(grid_pen)
        if self.show_grid:
            for i in range(self.DIVS_X + 1):
                x = r.left() + r.width() * i / self.DIVS_X
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            for i in range(self._divs_y + 1):
                y = r.top() + r.height() * i / self._divs_y
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        else:
            p.drawRect(r)

        muted = QColor(UI_COLOR_BORDER_LIGHT)
        eff, yt = self._effective_traces()
        if not eff or self._scale is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO DATA")
            p.end()
            return pm

        first = eff[0][1]
        x0, x1 = self._x_bounds()
        lo, hi, step = self._scale

        def px(x):
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y):
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        p.save()
        p.setClipRect(r.adjusted(-3, -3, 3, 3))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Память (MEM) рисуется пунктиром под живыми трассами (в режиме overlay)
        if self.memory and self.memory_visible and not self._math_active():
            for i, (_mname, mtr) in enumerate(self.memory):
                mc = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
                mc.setAlpha(115)
                self._draw_trace(p, mtr, x0, x1, lo, hi, r, self._y_transform, mc, dashed=True)
        # Линии пределов (limit lines ВАЦ): верхний (MAX) и нижний (MIN) пределы
        if not self._math_active() and self.limit_lines and hi > lo:
            lim_col = QColor(UI_COLOR_ERROR)
            shade_col = QColor(UI_COLOR_ERROR)
            shade_col.setAlpha(42)
            lim_pen = QPen(lim_col, 1.6)
            lim_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            for kind, lx1, lx2, ly1_raw, ly2_raw in self.limit_lines:
                ly1 = yt(ly1_raw) if yt is not None else ly1_raw
                ly2 = yt(ly2_raw) if yt is not None else ly2_raw
                if not (math.isfinite(ly1) and math.isfinite(ly2)):
                    continue
                p1 = QPointF(px(lx1), r.bottom() - (ly1 - lo) / (hi - lo) * r.height())
                p2 = QPointF(px(lx2), r.bottom() - (ly2 - lo) / (hi - lo) * r.height())
                dy = -6.0 if kind == "MAX" else 6.0
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(shade_col)
                p.drawPolygon(QPolygonF([p1, p2, QPointF(p2.x(), p2.y() + dy), QPointF(p1.x(), p1.y() + dy)]))
                p.setPen(lim_pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawLine(p1, p2)
        draw_pts = self.points or self.show_points
        for i, (_name, t) in enumerate(eff):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            self._draw_trace(p, t, x0, x1, lo, hi, r, yt, color, draw_pts=draw_pts)
        if (not self._math_active() and self.marker
                and math.isfinite(self.marker[0]) and math.isfinite(self.marker[1])):
            my_val = yt(self.marker[1]) if yt is not None else self.marker[1]
            if math.isfinite(my_val):
                mx, my = px(self.marker[0]), py(my_val)
                p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolygon(QPointF(mx, my - 5), QPointF(mx + 5, my), QPointF(mx, my + 5), QPointF(mx - 5, my))
        # Именованные маркеры (например, маркеры ВАЦ M1..M10)
        drawn_markers: list[tuple[str, float, float, int]] = []
        if not self._math_active() and self.markers:
            fm_m = QFontMetrics(self._font)
            for label, mx_raw, my_raw, cidx in self.markers:
                my_val = yt(my_raw) if yt is not None else my_raw
                if not (math.isfinite(mx_raw) and math.isfinite(my_val)):
                    continue
                drawn_markers.append((label, mx_raw, my_val, cidx))
                if mx_raw < min(x0, x1) - 1e-12 * max(1.0, abs(x1 - x0)) or mx_raw > max(x0, x1) + 1e-12 * max(1.0, abs(x1 - x0)):
                    continue
                mx, my = px(mx_raw), py(my_val)
                mc = QColor(UI_TRACE_COLORS[cidx % len(UI_TRACE_COLORS)])
                # Треугольник-указатель прямо над точкой
                p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.2))
                p.setBrush(mc)
                p.drawPolygon(QPointF(mx, my), QPointF(mx - 5.0, my - 8.5), QPointF(mx + 5.0, my - 8.5))
                short_lbl = label.rsplit(" ", 1)[-1]
                lw = fm_m.horizontalAdvance(short_lbl) + 6
                lh = fm_m.height() + 1
                lx = max(r.left() + 2, min(mx - lw / 2.0, r.right() - lw - 2))
                ly = my - 10.0 - lh
                if ly < r.top() + 2:
                    ly = min(my + 4.0, r.bottom() - lh - 2)
                lbg = QColor(UI_COLOR_BACKGROUND)
                lbg.setAlpha(210)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(lbg)
                p.drawRoundedRect(QRectF(lx, ly, lw, lh), 2.5, 2.5)
                p.setPen(mc)
                p.drawText(QRectF(lx, ly, lw, lh), Qt.AlignmentFlag.AlignCenter, short_lbl)
        p.restore()

        # Табличка значений активных маркеров в правом верхнем углу графика
        if drawn_markers:
            xu_m = first.x_unit
            yu_m = self._effective_y_unit(first)
            fm_m = QFontMetrics(self._font)
            rows_m: list[tuple[str, int]] = []
            for label, mx_raw, my_val, cidx in drawn_markers[:6]:
                x_s = si_format(mx_raw, xu_m, 4) if xu_m in self._SI_UNITS else (f"{mx_raw:g} {xu_m}" if xu_m else f"{mx_raw:g}")
                y_s = si_format(my_val, yu_m, 4) if yu_m in self._SI_UNITS else (f"{my_val:.2f} {yu_m}" if yu_m else f"{my_val:.3g}")
                rows_m.append((f"{label}: {x_s}  {y_s}", cidx))
            line_h = fm_m.height() + 3
            box_w = max(fm_m.horizontalAdvance(txt) for txt, _c in rows_m) + 14
            box_h = len(rows_m) * line_h + 6
            if box_w < r.width() - 12 and box_h < r.height() - 12:
                box = QRectF(r.right() - box_w - 6, r.top() + 5, box_w, box_h)
                bg = QColor(UI_COLOR_BACKGROUND)
                bg.setAlpha(224)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                p.setPen(QPen(QColor(UI_COLOR_BORDER), 1.0))
                p.setBrush(bg)
                p.drawRoundedRect(box, 4, 4)
                for idx_m, (txt, cidx) in enumerate(rows_m):
                    p.setPen(QColor(UI_TRACE_COLORS[cidx % len(UI_TRACE_COLORS)]))
                    p.drawText(QRectF(box.left() + 7, box.top() + 3 + idx_m * line_h, box_w - 14, line_h),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)

        # подписи: легенда сверху слева, шкала сверху справа, ось X снизу
        xu = first.x_unit
        yu = self._effective_y_unit(first)
        x_legend = r.left() + 2
        for i, (name, _t) in enumerate(eff):
            if not name:
                continue
            p.setPen(QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)]))
            w = p.fontMetrics().horizontalAdvance(name) + 14
            p.drawText(QRectF(x_legend, 3, w, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            x_legend += w
        if self._math_active():
            tag_map = {"SUB": "[DATA−MEM]", "ADD": "[DATA+MEM]", "DIV": "[DATA/MEM]", "MUL": "[DATA×MEM]"}
            tag = f"[f: {self.math_expr}]" if self.math_mode == "EXPR" else tag_map.get(self.math_mode, "[MATH]")
            p.setPen(muted)
            tw = p.fontMetrics().horizontalAdvance(tag) + 10
            p.drawText(QRectF(x_legend, 3, tw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tag)
        elif self.memory:
            tag = "[MEM]" if self.memory_visible else "[MEM OFF]"
            p.setPen(muted)
            tw = p.fontMetrics().horizontalAdvance(tag) + 10
            p.drawText(QRectF(x_legend, 3, tw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tag)

        p.setPen(muted)
        if yu in self._SI_UNITS:
            scale = f"{si_format(hi, yu, 4)}  {si_format(step, yu, 3)}/div"
        else:
            div_u = "dB" if yu == "dBm" else yu
            top_s = f"{hi:g} {yu}" if yu else f"{hi:g}"
            div_s = f"{step:g} {div_u}/div" if div_u else f"{step:g}/div"
            scale = f"{top_s}  {div_s}"
        if self.manual_scale:
            scale += "  MAN"
        p.drawText(QRectF(r.left(), 3, r.width(), 19), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, scale)
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 19)
        if xu in self._SI_UNITS:
            x0_s = si_format(x0, xu, 4)
            x1_s = si_format(x1, xu, 4)
            dx_s = f"{si_format((x1 - x0) / self.DIVS_X, xu, 3)}/div"
        else:
            dx_u = "dB" if xu == "dBm" else xu
            x0_s = f"{x0:g} {xu}" if xu else f"{x0:g}"
            x1_s = f"{x1:g} {xu}" if xu else f"{x1:g}"
            dx_s = f"{(x1 - x0) / self.DIVS_X:.3g} {dx_u}/div" if dx_u else f"{(x1 - x0) / self.DIVS_X:.3g}/div"
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, x0_s)
        if x1 != x0:
            p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, dx_s)
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, x1_s)

        # Плашка статуса Limit Test (PASS / FAIL) в левом верхнем углу графика
        if not self._math_active() and self.limit_lines and self.limit_fail is not None:
            fm_l = QFontMetrics(self._font)
            badge_txt = "FAIL" if self.limit_fail else "PASS"
            badge_col = QColor(UI_COLOR_ERROR if self.limit_fail else UI_COLOR_SUCCESS)
            bw_l = fm_l.horizontalAdvance(badge_txt) + 14
            bh_l = fm_l.height() + 4
            lbox = QRectF(r.left() + 6, r.top() + 5, bw_l, bh_l)
            lbg = QColor(UI_COLOR_BACKGROUND)
            lbg.setAlpha(224)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(badge_col, 1.1))
            p.setBrush(lbg)
            p.drawRoundedRect(lbox, 4, 4)
            p.setPen(badge_col)
            p.drawText(lbox, Qt.AlignmentFlag.AlignCenter, badge_txt)

        # полоса выбранных метрик анализа под осью X
        if an_lines:
            p.setPen(QColor(UI_COLOR_TEXT))
            y_an = r.bottom() + 24
            for ln in an_lines:
                p.drawText(QRectF(r.left() + 2, y_an, r.width() - 4, 18),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ln)
                y_an += 18
        p.end()
        return pm

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.width() <= 0 or self.height() <= 0:
            return
        key = (self.width(), self.height(), self.devicePixelRatioF(),
               tuple((n, id(t)) for n, t in self.traces),
               tuple((n, id(t)) for n, t in self.memory),
               self.memory_visible, self.math_mode, self.math_expr,
               self.points, self.show_points, self.show_grid,
               self.marker, tuple(self.markers), tuple(self.limit_lines), self.limit_fail,
               self.x_range, self.manual_x_range,
               self.manual_scale, self._scale, self._divs_y, self._y_unit, self._y_transform,
               tuple(self.analysis_metrics), current_theme())
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_base()
            self._cache_key = key
        p = QPainter(self)
        if not p.isActive():
            return
        p.drawPixmap(0, 0, self._cache)
        m = self._mouse
        eff, yt = self._effective_traces()
        if m is None or not eff or self._scale is None:
            return
        r = self._plot_rect()
        first = eff[0][1]
        if r.contains(m) and len(first) > 0:              # курсор: точка первой трассы
            x0, x1 = self._x_bounds()
            lo, hi, _step = self._scale
            x_target = x0 + (m.x() - r.left()) / r.width() * (x1 - x0) if x1 != x0 and r.width() > 0 else x0
            i = nearest_index(first, x_target)
            yi_raw = first.y[i]
            if not math.isfinite(yi_raw):
                return
            yi = yt(yi_raw) if yt is not None else yi_raw
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
            p.drawEllipse(QPointF(cx, cy), 3.0, 3.0)
            yu = self._effective_y_unit(first)
            xu = first.x_unit
            x_txt = si_format(first.x[i], xu, 5) if xu in self._SI_UNITS else (f"{first.x[i]:.2f} {xu}" if xu else f"{first.x[i]:.4g}")
            y_txt = si_format(yi, yu, 4) if yu in self._SI_UNITS else (f"{yi:.2f} {yu}" if yu else f"{yi:.3g}")
            readout = f"{x_txt}  {y_txt}"
            fm = QFontMetrics(self._font)
            bw = fm.horizontalAdvance(readout) + 14
            bh = fm.height() + 6
            # Ставим табличку на противоположной от вертикальной прямой стороне, чтобы прямая не перекрывала числа
            if cx >= r.center().x():
                bx = r.left() + 6
            else:
                bx = r.right() - bw - 6
            bx = max(r.left() + 4, min(bx, r.right() - bw - 4))
            badge = QRectF(bx, r.top() + 5, bw, bh)
            bg = QColor(UI_COLOR_BACKGROUND)
            bg.setAlpha(238)
            p.setPen(QPen(QColor(UI_COLOR_BORDER_LIGHT), 1.0))
            p.setBrush(bg)
            p.drawRoundedRect(badge, 4, 4)
            p.setPen(QColor(UI_COLOR_TEXT))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, readout)
