"""Единый универсальный виджет графика (``PlotWidget``) и окно его настроек (``PlotSettingsWindow``).

Принимает только сырые наборы точек ``PlotSeries(name, x, y)`` и сам считает всё локально:
* HiDPI ``QPixmap``-кэш сетки, осей и кривых + мгновенное перекрестие курсора поверх кэша;
* min/max децимация по пиксельным колонкам (сохраняет узкие пики и провалы при любом числе точек);
* двойной клик ЛКМ: ``DATA → MEM`` (сохранить снимок в память / очистить память);
* локальные маркеры (``M1…Mn``, ``Δ``, ``Peak`` / ``Min``, перетаскивание мышью по кривой);
* локальная математика (``OFF / D−M / D+M / D/M / D×M / f(x,y)``);
* локальные линии пределов (``MAX / MIN`` + индикатор ``PASS / FAIL``);
* локальный анализ (``MEAN / MIN / MAX / STD / P-P / FLAT / NONLIN / SLOPE / RMS / BW-3dB``);
* самостоятельное сохранение и восстановление параметров графика в ``QSettings`` при выходе.
"""
from __future__ import annotations

from bisect import bisect_left
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QPointF, QRectF, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFontMetrics, QFontMetricsF, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from ..core.trace import Trace
from ..core.units import si_format, si_parse
from . import theme as _theme
from .plot_tools import (ANALYSIS_METRICS, MATH_LABELS, MATH_MODES, XRangeSet, _interp_y, analyze_trace,
                         format_analysis, parse_x_range_expr, trace_math, validate_math_expr)
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_CONTROL, UI_COLOR_ERROR, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER,
                    UI_COLOR_PANEL, UI_COLOR_SUCCESS, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_COMPACT_HEIGHT_PX, UI_FONT_SIZE_PT, UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY,
                    WINDOW_BAR_HEIGHT, button_qss, current_theme, input_qss, mono_font, render_qss, scrollbar_qss)
from .widgets import CollapsibleSection, CommitLineEdit
from .window import CollapseAllButton, ExpandAllButton, PanelCloseButton, Window

__all__ = ["PlotSeries", "PlotMarker", "PlotLimit", "PlotWidget", "PlotSettingsWindow"]

_SI_UNITS = frozenset(("Hz", "s", "W", "V", "A"))


def _c(name: str) -> QColor:
    return QColor(str(_theme.TOKENS[name]))


def _series_color(idx: int, explicit: str | None) -> QColor:
    if explicit:
        return QColor(explicit)
    palette = _theme.UI_TRACE_COLORS
    return QColor(palette[idx % len(palette)])


def _swatch_icon(col: QColor, *, dashed: bool = False) -> QIcon:
    """Маленькая иконка-линия цвета кривой для выпадающего меню выбора кривой."""
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(col, 2.4, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(1.5, 7.0), QPointF(12.5, 7.0))
    p.end()
    return QIcon(pix)


_POPUP_MENU_QSS = render_qss("""
    QMenu {
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
        padding: 3px;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
    }
    QMenu::item {
        padding: 4px 12px;
        border-radius: 4px;
    }
    QMenu::item:selected {
        background-color: $UI_COLOR_INVERT;
        color: $UI_COLOR_TEXT_INVERSE;
    }
    QMenu::separator {
        height: 1px;
        background: $UI_COLOR_BORDER;
        margin: 3px 6px;
    }
""")


@dataclass(frozen=True)
class PlotSeries:
    """Одна кривая на графике: имя, массивы ``x``/``y``, стиль линии и опциональная единица измерения по Y."""

    name: str
    x: tuple[float, ...]
    y: tuple[float, ...]
    color: str | None = None
    show_points: bool = False
    line_width: float = 1.6
    dashed: bool = False
    y_unit: str | None = None


@dataclass
class PlotMarker:
    """Локальный маркер на графике (можно двигать мышью по кривой, закреплять на MAX/MIN или делать Δ)."""

    name: str
    x: float
    series_index: int = 0
    delta_ref: str | None = None
    track: str | None = None  # None | "MAX" | "MIN"


def _marker_sort_key(m: PlotMarker) -> tuple[int, str]:
    s = m.name.lstrip("Mm")
    if s.isdigit():
        return (int(s), m.name)
    return (10**9, m.name)


def _fmt_signed_value(v: float, unit: str) -> str:
    txt = _fmt_value(v, unit)
    if v > 0 and not txt.startswith("+"):
        return "+" + txt
    return txt


@dataclass
class PlotLimit:
    """Локальная линия предела (``MAX`` или ``MIN``; если ``x1/x2 is None`` — на весь диапазон X)."""

    kind: str  # "MAX" | "MIN"
    y1: float
    y2: float | None = None
    x1: float | None = None
    x2: float | None = None

    def y_at(self, x: float, x_lo: float, x_hi: float) -> float | None:
        a = x_lo if self.x1 is None else self.x1
        b = x_hi if self.x2 is None else self.x2
        if a > b:
            a, b = b, a
        if x < a - 1e-12 or x > b + 1e-12:
            return None
        y_end = self.y1 if self.y2 is None else self.y2
        if b == a or y_end == self.y1:
            return self.y1
        return self.y1 + (y_end - self.y1) * (x - a) / (b - a)


def _nearest_point(s: PlotSeries, target_x: float) -> tuple[float, float] | None:
    """Найти ближайшую измеренную точку ``(x, y)`` кривой ``s`` по координате ``target_x``."""
    xs, ys = s.x, s.y
    n = min(len(xs), len(ys))
    if n == 0:
        return None
    # Быстрый путь O(log N) для отсортированного по возрастанию X
    if n == 1 or xs[0] <= xs[-1]:
        i = bisect_left(xs, target_x, 0, n)
        best: tuple[float, float] | None = None
        best_dx = math.inf
        for cand in (i - 1, i):
            if 0 <= cand < n:
                xv, yv = xs[cand], ys[cand]
                if math.isfinite(xv) and math.isfinite(yv):
                    dx = abs(xv - target_x)
                    if dx < best_dx:
                        best_dx = dx
                        best = (float(xv), float(yv))
        if best is not None:
            return best
    best = None
    best_dx = math.inf
    for idx in range(n):
        xv, yv = xs[idx], ys[idx]
        if math.isfinite(xv) and math.isfinite(yv):
            dx = abs(xv - target_x)
            if dx < best_dx:
                best_dx = dx
                best = (float(xv), float(yv))
    return best


def _nice_step(span: float, divs: int) -> float:
    if not math.isfinite(span) or span <= 0 or divs <= 0:
        return 1.0
    raw = span / divs
    exp = math.floor(math.log10(raw))
    base = 10.0 ** exp
    frac = raw / base
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if frac <= mult + 1e-12:
            return mult * base
    return 10.0 * base


def _auto_bounds(lo: float, hi: float, divs: int, *, pad_ratio: float = 0.08) -> tuple[float, float]:
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, float(divs)
    span = hi - lo
    if span <= 0:
        margin = max(abs(lo) * 0.1, 1.0)
        lo, hi = lo - margin, hi + margin
        span = hi - lo
    else:
        lo -= span * pad_ratio
        hi += span * pad_ratio
        span = hi - lo
    step = _nice_step(span, divs)
    nice_lo = math.floor(lo / step) * step
    nice_hi = nice_lo + step * divs
    while nice_hi < hi - 1e-12 * step:
        step = _nice_step(span * 1.15, divs)
        nice_lo = math.floor(lo / step) * step
        nice_hi = nice_lo + step * divs
    return nice_lo, nice_hi


def _fmt_value(value: float, unit: str, step: float = 0.0, *, include_unit: bool = True) -> str:
    if not math.isfinite(value):
        return "—"
    if abs(value) < 1e-15:
        value = 0.0
    if unit in _SI_UNITS:
        return si_format(value, unit, 5)
    if step > 0 and math.isfinite(step):
        dec = max(0, min(6, -math.floor(math.log10(step)) if step < 1 else 0))
        num = f"{value:.{dec}f}"
    else:
        num = f"{value:.5g}"
    if num == "-0":
        num = "0"
    return f"{num} {unit}".strip() if (include_unit and unit) else num


def _fmt_point_label(value: float, unit: str) -> str:
    if not math.isfinite(value):
        return "—"
    if abs(value) < 1e-15:
        value = 0.0
    if unit in _SI_UNITS:
        return si_format(value, unit, 3)
    num = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if num == "-0" else num


def _parse_axis_input(text: str, unit: str) -> float:
    raw = text.strip().replace(",", ".")
    if not raw:
        raise ValueError("empty")
    try:
        return float(raw)
    except ValueError:
        return float(si_parse(raw, unit))


def _decimate_paths(xs: Sequence[float], ys: Sequence[float], x0: float, x1: float,
                    y0: float, y1: float, rect: QRectF) -> list[list[QPointF]]:
    n = min(len(xs), len(ys))
    if n == 0 or x1 == x0 or y1 == y0 or rect.width() <= 1 or rect.height() <= 1:
        return []

    left, bottom = rect.left(), rect.bottom()
    sx = rect.width() / (x1 - x0)
    sy = rect.height() / (y1 - y0)

    def _to_pt(xv: float, yv: float) -> QPointF:
        return QPointF(left + (xv - x0) * sx, bottom - (yv - y0) * sy)

    if n <= int(rect.width() * 2):
        segments: list[list[QPointF]] = []
        cur: list[QPointF] = []
        for i in range(n):
            xv, yv = xs[i], ys[i]
            if not (math.isfinite(xv) and math.isfinite(yv)):
                if cur:
                    segments.append(cur)
                    cur = []
                continue
            cur.append(_to_pt(xv, yv))
        if cur:
            segments.append(cur)
        return segments

    segments = []
    cur = []
    cur_col: int | None = None
    first_x = 0.0
    min_y = max_y = 0.0
    min_first = True

    def _flush() -> None:
        nonlocal cur_col
        if cur_col is None:
            return
        if min_y == max_y:
            cur.append(_to_pt(first_x, min_y))
        elif min_first:
            cur.append(_to_pt(first_x, min_y))
            cur.append(_to_pt(first_x, max_y))
        else:
            cur.append(_to_pt(first_x, max_y))
            cur.append(_to_pt(first_x, min_y))
        cur_col = None

    for i in range(n):
        xv, yv = xs[i], ys[i]
        if not (math.isfinite(xv) and math.isfinite(yv)):
            _flush()
            if cur:
                segments.append(cur)
                cur = []
            continue
        col = int(left + (xv - x0) * sx)
        if col != cur_col:
            _flush()
            cur_col = col
            first_x = xv
            min_y = max_y = yv
            min_first = True
        else:
            if yv < min_y:
                min_y = yv
                min_first = False
            if yv > max_y:
                max_y = yv
                min_first = True
    _flush()
    if cur:
        segments.append(cur)
    return segments


class PlotWidget(QWidget):
    """Единый виджет графика (``QPainter`` + HiDPI ``QPixmap``-кэш + маркеры + MEM/MATH + LIMITS + ANALYSIS)."""

    DIVS_X = 10
    DIVS_Y = 6

    view_changed = pyqtSignal()

    def __init__(self, *, x_unit: str = "", y_unit: str = "", empty_text: str = "No data", parent=None):
        super().__init__(parent)
        self.series: list[PlotSeries] = []
        self.x_unit: str = x_unit
        self.y_unit: str = y_unit
        self.empty_text: str = empty_text
        self.show_grid: bool = True
        self.show_points: bool = False
        self._show_points_explicit: bool = False
        self.show_point_values: bool = False
        self.show_peaks: bool = False
        self.show_axis_labels: bool = True
        self.top_pad: float = 0.0
        self.divs_y: int = self.DIVS_Y
        self.manual_x_range: tuple[float, float] | None = None
        self.manual_y_range: tuple[float, float] | None = None
        self.series_y_ranges: dict[int, tuple[float, float]] = {}

        # Локальная память (MEM), математика (MATH), маркеры, лимиты и анализ
        self.memory: list[PlotSeries] = []
        self.memory_visible: bool = True
        self.math_mode: str = "OFF"
        self.math_expr: str = ""
        self.markers: list[PlotMarker] = []
        self.limits: list[PlotLimit] = []
        self.analysis_metrics: list[str] = []
        self.analysis_x_expr: str = ""
        self._analysis_x_set: XRangeSet | None = None
        self.active_series_index: int | None = None

        self._settings: QSettings | None = None
        self._settings_key: str = ""
        self._quit_connected: bool = False

        self._data_version: int = 0
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self._plot_rect = QRectF()
        self._legend_box = QRectF()
        self._markers_box = QRectF()
        self._analysis_box = QRectF()
        self._overlay_pos: dict[str, tuple[float, float]] = {}
        self._overlay_press: tuple[str, QPointF, QPointF, int | None, bool] | None = None
        self._dragging_overlay: tuple[str, QPointF, QRectF] | None = None
        self._legend_hits: list[tuple[QRectF, int]] = []
        self._legend_meta: dict[int, tuple[QRectF, str, QColor, bool]] = {}
        self._legend_hover: int | None = None
        self._legend_scroll_idx: int = 0
        self._legend_wheel_acc: float = 0.0
        self._legend_more_hit: QRectF | None = None
        self._legend_more_text: str = ""
        self._legend_more_hover: bool = False
        self._top_right_pad: float = 0.0
        self._chip_hits: list[tuple[QRectF, str, str, Callable[[], None], QRectF]] = []
        self._chip_hover: str | None = None
        self._chips_strip_rect = QRectF()
        self._chips_scroll_x: float = 0.0
        self._chips_max_scroll: float = 0.0
        self._cur_x_range: tuple[float, float] = (0.0, 1.0)
        self._cur_y_range: tuple[float, float] = (0.0, 1.0)
        self._cur_y_ranges: list[tuple[float, float]] = []
        self._cur_y_units: list[str] = []
        self._mouse: QPointF | None = None
        self._pan_origin: tuple[QPointF, tuple[float, float], tuple[float, float], list[tuple[float, float]], int | None] | None = None
        self._dragging_marker: PlotMarker | None = None
        self._font = mono_font()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Legend wheel / click: select curve · Double-click: DATA → MEM · Wheel (focused / Cmd): zoom Y · Shift+drag: pan · R: reset")

    # ------------------------------------------------------------ сохранение состояния
    def attach_settings(self, settings: QSettings, key: str) -> None:
        self._settings = settings
        self._settings_key = key
        self.load_state()
        if not self._quit_connected:
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.save_state)
                self._quit_connected = True

    def detach_settings(self) -> None:
        if self._quit_connected:
            app = QApplication.instance()
            if app is not None:
                try:
                    app.aboutToQuit.disconnect(self.save_state)
                except (TypeError, RuntimeError):
                    pass
            self._quit_connected = False

    def save_state(self) -> None:
        if sip.isdeleted(self) or self._settings is None or not self._settings_key:
            return
        payload = {
            "show_grid": self.show_grid,
            "show_points": self.show_points,
            "show_point_values": self.show_point_values,
            "show_peaks": self.show_peaks,
            "show_axis_labels": self.show_axis_labels,
            "divs_y": self.divs_y,
            "manual_x_range": list(self.manual_x_range) if self.manual_x_range else None,
            "manual_y_range": list(self.manual_y_range) if self.manual_y_range else None,
            "series_y_ranges": {str(k): [v[0], v[1]] for k, v in self.series_y_ranges.items()},
            "memory_visible": self.memory_visible,
            "math_mode": self.math_mode,
            "math_expr": self.math_expr,
            "markers": [
                {"name": m.name, "x": m.x, "series_index": m.series_index,
                 "delta_ref": m.delta_ref, "track": m.track}
                for m in self.markers
            ],
            "limits": [
                {"kind": lim.kind, "y1": lim.y1, "y2": lim.y2, "x1": lim.x1, "x2": lim.x2}
                for lim in self.limits
            ],
            "analysis_metrics": list(self.analysis_metrics),
            "analysis_x_expr": self.analysis_x_expr,
            "active_series_index": self.active_series_index,
            "overlay_pos": {k: [round(v[0], 4), round(v[1], 4)] for k, v in self._overlay_pos.items()},
        }
        self._settings.setValue(self._settings_key, json.dumps(payload))

    def load_state(self) -> None:
        if sip.isdeleted(self) or self._settings is None or not self._settings_key:
            return
        raw = self._settings.value(self._settings_key, "", type=str)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if "show_grid" in data:
            self.show_grid = bool(data["show_grid"])
        if "show_points" in data:
            self.show_points = bool(data["show_points"])
            self._show_points_explicit = True
        if "show_point_values" in data:
            self.show_point_values = bool(data["show_point_values"])
        if "show_peaks" in data:
            self.show_peaks = bool(data["show_peaks"])
        if "show_axis_labels" in data:
            self.show_axis_labels = bool(data["show_axis_labels"])
        if "divs_y" in data:
            try:
                self.divs_y = max(2, min(12, int(data["divs_y"])))
            except (TypeError, ValueError):
                pass
        mx = data.get("manual_x_range")
        if isinstance(mx, list) and len(mx) == 2 and mx[1] > mx[0]:
            self.manual_x_range = (float(mx[0]), float(mx[1]))
        my = data.get("manual_y_range")
        if isinstance(my, list) and len(my) == 2 and my[1] > my[0]:
            self.manual_y_range = (float(my[0]), float(my[1]))
        raw_syr = data.get("series_y_ranges")
        self.series_y_ranges = {}
        if isinstance(raw_syr, dict):
            for k, v in raw_syr.items():
                if isinstance(v, list) and len(v) == 2 and v[1] > v[0]:
                    try:
                        idx = int(k)
                        if idx >= 0:
                            self.series_y_ranges[idx] = (float(v[0]), float(v[1]))
                    except (TypeError, ValueError):
                        pass
        if "memory_visible" in data:
            self.memory_visible = bool(data["memory_visible"])
        mm = str(data.get("math_mode", "OFF"))
        if mm in MATH_MODES:
            self.math_mode = mm
        self.math_expr = str(data.get("math_expr", "") or "")
        if isinstance(data.get("markers"), list):
            loaded_markers: list[PlotMarker] = []
            for i, item in enumerate(data["markers"]):
                if not (isinstance(item, dict) and "x" in item):
                    continue
                trk_raw = item.get("track")
                trk = str(trk_raw).upper() if trk_raw else None
                if trk not in ("MAX", "MIN"):
                    trk = None
                loaded_markers.append(PlotMarker(
                    name=str(item.get("name", f"M{i + 1}")),
                    x=float(item["x"]),
                    series_index=int(item.get("series_index", 0)),
                    delta_ref=str(item["delta_ref"]) if item.get("delta_ref") else None,
                    track=trk,
                ))
            loaded_markers.sort(key=_marker_sort_key)
            self.markers = loaded_markers
        if isinstance(data.get("limits"), list):
            self.limits = [
                PlotLimit(
                    kind="MIN" if str(item.get("kind", "MAX")).upper() == "MIN" else "MAX",
                    y1=float(item["y1"]),
                    y2=float(item["y2"]) if item.get("y2") is not None else None,
                    x1=float(item["x1"]) if item.get("x1") is not None else None,
                    x2=float(item["x2"]) if item.get("x2") is not None else None,
                )
                for item in data["limits"]
                if isinstance(item, dict) and "y1" in item
            ]
        valid_metrics = {k for k, _, _ in ANALYSIS_METRICS}
        if isinstance(data.get("analysis_metrics"), list):
            self.analysis_metrics = [str(k) for k in data["analysis_metrics"] if str(k) in valid_metrics]
        ax_expr = str(data.get("analysis_x_expr", "") or "").strip()
        self.analysis_x_expr = ax_expr
        self._analysis_x_set = None
        if ax_expr:
            try:
                self._analysis_x_set = parse_x_range_expr(ax_expr, self.x_unit)
            except ValueError:
                self._analysis_x_set = None
        asi = data.get("active_series_index")
        self.active_series_index = int(asi) if isinstance(asi, int) and asi >= 0 else None
        raw_ov = data.get("overlay_pos")
        self._overlay_pos = {}
        if isinstance(raw_ov, dict):
            for k, v in raw_ov.items():
                if k in ("legend", "markers", "analysis") and isinstance(v, list) and len(v) == 2:
                    try:
                        fx = max(0.0, min(1.0, float(v[0])))
                        fy = max(0.0, min(1.0, float(v[1])))
                        self._overlay_pos[k] = (fx, fy)
                    except (TypeError, ValueError):
                        pass
        self._bump()

    # ------------------------------------------------------------ публичный API: данные и масштаб
    def set_series(self, series: Sequence[PlotSeries], *, x_unit: str | None = None,
                   y_unit: str | None = None) -> None:
        self.series = list(series)
        if not self._show_points_explicit and any(s.show_points for s in self.series):
            self.show_points = True
        if x_unit is not None:
            self.x_unit = x_unit
            if self.analysis_x_expr:
                try:
                    self._analysis_x_set = parse_x_range_expr(self.analysis_x_expr, self.x_unit)
                except ValueError:
                    self._analysis_x_set = None
        if y_unit is not None:
            self.y_unit = y_unit
        self._bump()
        self.view_changed.emit()

    def set_axes(self, x_unit: str = "", y_unit: str = "") -> None:
        self.x_unit = x_unit
        self.y_unit = y_unit
        if self.analysis_x_expr:
            try:
                self._analysis_x_set = parse_x_range_expr(self.analysis_x_expr, self.x_unit)
            except ValueError:
                self._analysis_x_set = None
        self._bump()

    def clear(self) -> None:
        self.series.clear()
        self._bump()
        self.view_changed.emit()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = bool(on)
        self._bump()
        self.view_changed.emit()

    def set_show_points(self, on: bool) -> None:
        self.show_points = bool(on)
        self._show_points_explicit = True
        self._bump()
        self.view_changed.emit()

    def set_show_point_values(self, on: bool) -> None:
        self.show_point_values = bool(on)
        self._bump()
        self.view_changed.emit()

    def set_show_peaks(self, on: bool) -> None:
        """Включить/выключить постоянный режим автопоиска всех пиков (отображает точку и (x, y) без создания маркеров)."""
        self.show_peaks = bool(on)
        self._bump()
        self.view_changed.emit()

    def set_show_axis_labels(self, on: bool) -> None:
        self.show_axis_labels = bool(on)
        self._bump()
        self.view_changed.emit()

    def set_top_pad(self, pad: float) -> None:
        val = max(0.0, float(pad))
        if val != self.top_pad:
            self.top_pad = val
            self._bump()

    def set_top_right_pad(self, pad: float) -> None:
        """Зарезервировать место в правом верхнем углу внутри графика (например под кнопку настроек на главном графике)."""
        val = max(0.0, float(pad))
        if val != self._top_right_pad:
            self._top_right_pad = val
            self._bump()

    def set_active_series(self, index: int | None) -> None:
        """Выбрать активную кривую для привязки перекрестья (``None`` = свободный курсор)."""
        if index is None:
            nxt: int | None = None
        else:
            idx = int(index)
            nxt = idx if (0 <= idx < len(self.series)) else None
        if nxt != self.active_series_index:
            self.active_series_index = nxt
            self._bump()
            self.view_changed.emit()

    def _step_active_series(self, step: int) -> None:
        """Переключить активную кривую колёсиком мыши (``None → 0 → 1 → ... → N-1``)."""
        if not step:
            return
        eff, _ = self._effective_series_and_unit()
        named = [i for i, s in enumerate(eff) if s.name]
        if not named:
            return
        active = self._active_series_idx(eff)
        order: list[int | None] = [None, *named]
        cur_pos = order.index(active) if active in order else 0
        nxt_pos = max(0, min(len(order) - 1, cur_pos + (1 if step > 0 else -1)))
        self.set_active_series(order[nxt_pos])

    def _active_series_idx(self, eff: Sequence[PlotSeries]) -> int | None:
        if self.active_series_index is not None and 0 <= self.active_series_index < len(eff):
            return self.active_series_index
        return None

    def _axis_series_idx(self, eff: Sequence[PlotSeries]) -> int | None:
        active = self._active_series_idx(eff)
        if active is not None:
            return active
        return 0 if eff else None

    def _series_y_unit(self, s: PlotSeries) -> str:
        return s.y_unit if s.y_unit is not None else self.y_unit

    def active_y_unit(self) -> str:
        """Единица измерения по Y для текущей шкалы (выбранной кривой или первой/общей)."""
        eff, eff_units = self._effective_series_and_units()
        axis_si = self._axis_series_idx(eff)
        return eff_units[axis_si] if (axis_si is not None and axis_si < len(eff_units)) else self.y_unit

    def is_y_auto(self) -> bool:
        """``True``, если масштаб Y текущей шкалы (выбранной кривой или общий) в режиме AUTO."""
        eff, eff_units = self._effective_series_and_units()
        active_si = self._active_series_idx(eff)
        same_unit = len(set(eff_units)) <= 1
        if active_si is not None:
            if active_si in self.series_y_ranges:
                return False
            return (not same_unit) or (self.manual_y_range is None)
        return self.manual_y_range is None and not self.series_y_ranges

    def set_divs_y(self, divs: int) -> None:
        n = max(2, min(12, int(divs)))
        if n != self.divs_y:
            self.divs_y = n
            self._bump()
            self.view_changed.emit()

    def set_x_range(self, lo: float | None, hi: float | None) -> None:
        self.manual_x_range = (float(lo), float(hi)) if (lo is not None and hi is not None and hi > lo) else None
        self._bump()
        self.view_changed.emit()

    def set_y_range(self, lo: float | None, hi: float | None, *, series_index: int | None = None) -> None:
        """Задать ручной диапазон Y. Если выбрана активная кривая (или задан ``series_index``) —
        меняется масштаб только этой кривой; в режиме ``Free`` — общий масштаб всех кривых."""
        eff, _ = self._effective_series_and_units()
        target_si = series_index if series_index is not None else self._active_series_idx(eff)
        valid = lo is not None and hi is not None and hi > lo
        if target_si is not None and 0 <= target_si < len(eff):
            if valid:
                self.series_y_ranges[target_si] = (float(lo), float(hi))
            else:
                self.series_y_ranges.pop(target_si, None)
        else:
            self.series_y_ranges.clear()
            self.manual_y_range = (float(lo), float(hi)) if valid else None
        self._bump()
        self.view_changed.emit()

    def reset_view(self) -> None:
        if self.manual_x_range is None and self.manual_y_range is None and not self.series_y_ranges:
            return
        self.manual_x_range = None
        self.manual_y_range = None
        self.series_y_ranges.clear()
        self._bump()
        self.view_changed.emit()

    # ------------------------------------------------------------ MEM и MATH
    def store_memory(self) -> bool:
        """Сохранить текущие кривые в память (``DATA → MEM``)."""
        if not self.series:
            return False
        self.memory = [
            PlotSeries(s.name, tuple(s.x), tuple(s.y), s.color, s.show_points, s.line_width,
                       dashed=True, y_unit=s.y_unit)
            for s in self.series
        ]
        self.memory_visible = True
        self._bump()
        self.view_changed.emit()
        return True

    def clear_memory(self) -> None:
        if not self.memory and self.math_mode in ("SUB", "ADD", "DIV", "MUL"):
            return
        self.memory.clear()
        if self.math_mode in ("SUB", "ADD", "DIV", "MUL"):
            self.math_mode = "OFF"
        self._bump()
        self.view_changed.emit()

    def set_memory_visible(self, on: bool) -> None:
        self.memory_visible = bool(on)
        self._bump()
        self.view_changed.emit()

    def set_math_mode(self, mode: str) -> None:
        if mode not in MATH_MODES:
            return
        if mode in ("SUB", "ADD", "DIV", "MUL") and not self.memory and self.series:
            self.store_memory()
        self.math_mode = mode
        self._bump()
        self.view_changed.emit()

    def set_math_expr(self, expr: str) -> bool:
        expr = expr.strip()
        if expr:
            ok, _ = validate_math_expr(expr)
            if not ok:
                return False
        self.math_expr = expr
        if self.math_mode == "EXPR":
            self._bump()
            self.view_changed.emit()
        return True

    def _effective_series_and_units(self) -> tuple[list[PlotSeries], list[str]]:
        if self.math_mode == "OFF" or not self.series:
            return list(self.series), [self._series_y_unit(s) for s in self.series]
        mem_by_name = {m.name: m for m in self.memory}
        out: list[PlotSeries] = []
        units: list[str] = []
        for idx, s in enumerate(self.series):
            s_yu = self._series_y_unit(s)
            mem_s = mem_by_name.get(s.name) or (self.memory[idx] if idx < len(self.memory) else None)
            t_live = Trace(list(s.x), list(s.y), self.x_unit, s_yu)
            t_mem = (
                Trace(list(mem_s.x), list(mem_s.y), self.x_unit, self._series_y_unit(mem_s))
                if mem_s is not None else None
            )
            res = trace_math(t_live, t_mem, self.math_mode, expr=self.math_expr)
            if res is not None:
                units.append(res.y_unit)
                out.append(PlotSeries(s.name, tuple(res.x), tuple(res.y), s.color, s.show_points,
                                      s.line_width, y_unit=res.y_unit))
            else:
                units.append(s_yu)
                out.append(s)
        return out, units

    def _effective_series_and_unit(self) -> tuple[list[PlotSeries], str]:
        eff, units = self._effective_series_and_units()
        axis_si = self._axis_series_idx(eff)
        return eff, (units[axis_si] if (axis_si is not None and axis_si < len(units)) else self.y_unit)

    # ------------------------------------------------------------ локальные маркеры
    @staticmethod
    def _find_extremum(s: PlotSeries, maximum: bool,
                       x_range: tuple[float, float] | None = None) -> tuple[float, float] | None:
        best_x: float | None = None
        best_y: float | None = None
        x_lo = x_hi = None
        if x_range is not None and math.isfinite(x_range[0]) and math.isfinite(x_range[1]):
            x_lo, x_hi = (x_range[0], x_range[1]) if x_range[0] <= x_range[1] else (x_range[1], x_range[0])
        for xv, yv in zip(s.x, s.y):
            if not (math.isfinite(xv) and math.isfinite(yv)):
                continue
            if x_lo is not None and (xv < x_lo or xv > x_hi):
                continue
            if best_y is None or (yv > best_y if maximum else yv < best_y):
                best_x, best_y = float(xv), float(yv)
        if best_x is None and x_lo is not None:
            return PlotWidget._find_extremum(s, maximum, None)
        return (best_x, best_y) if (best_x is not None and best_y is not None) else None

    def _next_marker_name(self) -> str:
        used = {m.name for m in self.markers}
        i = 1
        while f"M{i}" in used:
            i += 1
        return f"M{i}"

    def add_marker(self, x: float | None = None, *, series_index: int | None = None,
                   delta_ref: str | None = None, track: str | None = None) -> PlotMarker | None:
        eff, _ = self._effective_series_and_unit()
        if not eff:
            return None
        base_si = self.active_series_index if (series_index is None and self.active_series_index is not None) else (series_index or 0)
        si = max(0, min(base_si, len(eff) - 1))
        trk = str(track).upper() if track else None
        if trk not in ("MAX", "MIN"):
            trk = None
        if trk is not None:
            ext = self._find_extremum(eff[si], trk == "MAX", self.manual_x_range)
            if ext is not None:
                x = ext[0]
        if x is None:
            xr, _ = self.current_ranges()
            x = 0.5 * (xr[0] + xr[1])
        m = PlotMarker(name=self._next_marker_name(), x=float(x), series_index=si,
                       delta_ref=delta_ref, track=trk)
        self.markers.append(m)
        self.markers.sort(key=_marker_sort_key)
        self._bump()
        self.view_changed.emit()
        return m

    @staticmethod
    def _find_peaks(s: PlotSeries, x_range: tuple[float, float] | None = None,
                    *, max_peaks: int = 8) -> list[tuple[float, float]]:
        """Автопоиск всех локальных пиков по топографической выраженности (prominence)."""
        pts: list[tuple[float, float]] = []
        x_lo = x_hi = None
        if x_range is not None and math.isfinite(x_range[0]) and math.isfinite(x_range[1]):
            x_lo, x_hi = (x_range[0], x_range[1]) if x_range[0] <= x_range[1] else (x_range[1], x_range[0])
        for xv, yv in zip(s.x, s.y):
            if not (math.isfinite(xv) and math.isfinite(yv)):
                continue
            if x_lo is not None and (xv < x_lo or xv > x_hi):
                continue
            pts.append((float(xv), float(yv)))
        if len(pts) < 3:
            if not pts:
                return []
            best = max(pts, key=lambda p: p[1])
            return [best]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        n = len(ys)
        y_min, y_max = min(ys), max(ys)
        span_y = y_max - y_min
        if span_y <= 0.0:
            return []

        # Оценка шумовой «ряби» по медиане разностей соседних точек
        diffs = sorted(abs(ys[i + 1] - ys[i]) for i in range(n - 1))
        mad_dy = diffs[len(diffs) // 2]
        prom_thresh = max(0.08 * span_y, 6.0 * mad_dy)

        # Кандидаты — локальные максимумы (с обработкой плоских вершин)
        cand_indices: list[int] = []
        i = 1
        while i < n - 1:
            if ys[i] > ys[i - 1]:
                j = i
                while j + 1 < n - 1 and ys[j + 1] == ys[i]:
                    j += 1
                if ys[j] > ys[j + 1]:
                    cand_indices.append((i + j) // 2)
                i = j + 1
            else:
                i += 1

        # Вычисляем prominence каждого пика
        peaks: list[tuple[float, float, float]] = []  # (y, x, prominence)
        for idx in cand_indices:
            yv = ys[idx]
            left_min = yv
            for k in range(idx - 1, -1, -1):
                if ys[k] > yv:
                    break
                if ys[k] < left_min:
                    left_min = ys[k]
            right_min = yv
            for k in range(idx + 1, n):
                if ys[k] > yv:
                    break
                if ys[k] < right_min:
                    right_min = ys[k]
            ref_base = max(left_min, right_min) if (left_min < yv and right_min < yv) else min(left_min, right_min)
            prom = yv - ref_base
            if prom >= prom_thresh:
                peaks.append((yv, xs[idx], prom))

        if not peaks:
            best_i = max(range(n), key=lambda k: ys[k])
            return [(xs[best_i], ys[best_i])]

        # Сортируем от самого высокого пика к более низким (M1 = главный пик)
        peaks.sort(key=lambda item: (-item[0], item[1]))
        return [(px, py) for py, px, _prom in peaks[:max_peaks]]

    def add_extremum_marker(self, *, maximum: bool = True, series_index: int | None = None) -> PlotMarker | None:
        return self.add_marker(series_index=series_index, track="MAX" if maximum else "MIN")

    def add_all_peaks(self, *, series_index: int | None = None, max_peaks: int = 8) -> list[PlotMarker]:
        """Найти все значимые локальные пики активной кривой и расставить маркеры."""
        eff, _ = self._effective_series_and_unit()
        if not eff:
            return []
        base_si = self.active_series_index if (series_index is None and self.active_series_index is not None) else (series_index or 0)
        si = max(0, min(base_si, len(eff) - 1))
        xr, _ = self.current_ranges()
        found = self._find_peaks(eff[si], xr, max_peaks=max_peaks)
        if not found:
            return []
        x_span = max(1e-12, abs(xr[1] - xr[0]))
        dup_tol = 0.01 * x_span
        added: list[PlotMarker] = []
        for px, _py in found:
            if any(m.series_index == si and abs(m.x - px) <= dup_tol for m in self.markers):
                continue
            m = PlotMarker(name=self._next_marker_name(), x=float(px), series_index=si)
            self.markers.append(m)
            added.append(m)
        if added:
            self.markers.sort(key=_marker_sort_key)
            self._bump()
            self.view_changed.emit()
        return added

    def update_marker(self, name: str, *, x: float | None = None, series_index: int | None = None,
                      delta_ref: str | None | object = ..., track: str | None | object = ...) -> None:
        eff, _ = self._effective_series_and_unit()
        for m in self.markers:
            if m.name == name:
                if x is not None and math.isfinite(x):
                    m.x = float(x)
                    if track is ...:
                        m.track = None
                if series_index is not None and self.series:
                    m.series_index = series_index % len(self.series)
                if delta_ref is not ...:
                    m.delta_ref = delta_ref if (isinstance(delta_ref, str) and delta_ref != m.name) else None
                if track is not ...:
                    trk = str(track).upper() if isinstance(track, str) else None
                    m.track = trk if trk in ("MAX", "MIN") else None
                if m.track in ("MAX", "MIN") and eff:
                    si = max(0, min(m.series_index, len(eff) - 1))
                    ext = self._find_extremum(eff[si], m.track == "MAX", self.manual_x_range)
                    if ext is not None:
                        m.x = ext[0]
                self._bump()
                self.view_changed.emit()
                return

    def remove_marker(self, name: str) -> None:
        self.markers = [m for m in self.markers if m.name != name]
        for m in self.markers:
            if m.delta_ref == name:
                m.delta_ref = None
        self._bump()
        self.view_changed.emit()

    def clear_markers(self) -> None:
        if not self.markers:
            return
        self.markers.clear()
        self._bump()
        self.view_changed.emit()

    def resolve_marker(self, m: PlotMarker, eff_series: Sequence[PlotSeries] | None = None,
                       x_range: tuple[float, float] | None = None) -> tuple[float, float] | None:
        """Вернуть актуальные ``(x, y)`` маркера с учётом автотрекинга ``MAX`` / ``MIN``."""
        eff = eff_series if eff_series is not None else self._effective_series_and_unit()[0]
        if not eff:
            return None
        si = max(0, min(m.series_index, len(eff) - 1))
        s = eff[si]
        if not s.x or not s.y:
            return None
        if m.track in ("MAX", "MIN"):
            ext = self._find_extremum(s, m.track == "MAX", x_range if x_range is not None else self.manual_x_range)
            if ext is not None:
                m.x = ext[0]
                return ext
        val = _interp_y(s.x, s.y, m.x)
        return (m.x, val) if math.isfinite(val) else None

    def marker_y(self, m: PlotMarker, eff_series: Sequence[PlotSeries] | None = None) -> float | None:
        res = self.resolve_marker(m, eff_series)
        return res[1] if res is not None else None

    # ------------------------------------------------------------ локальные лимиты
    def add_limit(self, kind: str, y1: float, *, y2: float | None = None,
                  x1: float | None = None, x2: float | None = None) -> PlotLimit:
        lim = PlotLimit(
            kind="MIN" if str(kind).upper() == "MIN" else "MAX",
            y1=float(y1),
            y2=float(y2) if y2 is not None else None,
            x1=float(x1) if x1 is not None else None,
            x2=float(x2) if x2 is not None else None,
        )
        self.limits.append(lim)
        self._bump()
        self.view_changed.emit()
        return lim

    def remove_limit(self, index: int) -> None:
        if 0 <= index < len(self.limits):
            self.limits.pop(index)
            self._bump()
            self.view_changed.emit()

    def clear_limits(self) -> None:
        if not self.limits:
            return
        self.limits.clear()
        self._bump()
        self.view_changed.emit()

    def evaluate_limits(self, eff_series: Sequence[PlotSeries] | None = None,
                        x_range: tuple[float, float] | None = None,
                        eff_y_units: Sequence[str] | None = None) -> bool | None:
        """Проверить кривые (совпадающие по единице Y с активной шкалой) против ``self.limits``.
        ``True`` = PASS, ``False`` = FAIL, ``None`` = нет лимитов."""
        if not self.limits:
            return None
        if eff_series is None or eff_y_units is None:
            eff_calc, units_calc = self._effective_series_and_units()
            eff = eff_series if eff_series is not None else eff_calc
            units = eff_y_units if eff_y_units is not None else units_calc
        else:
            eff, units = eff_series, eff_y_units
        if not eff:
            return None
        x0, x1 = x_range if x_range is not None else self.current_ranges()[0]
        axis_si = self._axis_series_idx(eff)
        ref_unit = units[axis_si] if (axis_si is not None and axis_si < len(units)) else self.y_unit
        active_si = self._active_series_idx(eff)
        for idx, s in enumerate(eff):
            if active_si is not None and len(set(units)) > 1 and idx != active_si:
                continue
            if idx < len(units) and units[idx] != ref_unit:
                continue
            for xv, yv in zip(s.x, s.y):
                if not (math.isfinite(xv) and math.isfinite(yv)):
                    continue
                for lim in self.limits:
                    ly = lim.y_at(xv, x0, x1)
                    if ly is None:
                        continue
                    if lim.kind == "MAX" and yv > ly:
                        return False
                    if lim.kind == "MIN" and yv < ly:
                        return False
        return True

    # ------------------------------------------------------------ анализ
    def set_analysis_metrics(self, metrics: Sequence[str]) -> None:
        valid = {k for k, _, _ in ANALYSIS_METRICS}
        self.analysis_metrics = [m for m in metrics if m in valid]
        self._bump()
        self.view_changed.emit()

    def toggle_analysis_metric(self, key: str, enabled: bool) -> None:
        if enabled and key not in self.analysis_metrics:
            self.analysis_metrics.append(key)
        elif not enabled and key in self.analysis_metrics:
            self.analysis_metrics.remove(key)
        self._bump()
        self.view_changed.emit()

    def set_analysis_x_expr(self, expr: str) -> tuple[bool, str]:
        """Задать диапазон X для анализа в формате ``[x,y]+5-[a,b]`` (пусто = видимый диапазон)."""
        raw = str(expr or "").strip()
        if not raw:
            self.analysis_x_expr = ""
            self._analysis_x_set = None
            self._bump()
            self.view_changed.emit()
            return True, ""
        try:
            rset = parse_x_range_expr(raw, self.x_unit)
        except ValueError as exc:
            return False, str(exc)
        self.analysis_x_expr = raw
        self._analysis_x_set = rset
        self._bump()
        self.view_changed.emit()
        return True, ""

    def current_ranges(self, series_index: int | None = None) -> tuple[tuple[float, float], tuple[float, float]]:
        xr, yr, _, _, y_ranges, _ = self._compute_ranges()
        if series_index is not None and 0 <= series_index < len(y_ranges):
            return xr, y_ranges[series_index]
        return xr, yr

    def _bump(self) -> None:
        self._data_version += 1
        self._cache_key = None
        self.update()

    # ------------------------------------------------------------ диапазоны и координаты
    def _compute_ranges(self) -> tuple[tuple[float, float], tuple[float, float], bool,
                                       list[PlotSeries], list[tuple[float, float]], list[str]]:
        eff, eff_y_units = self._effective_series_and_units()
        xs: list[float] = []
        ys_by_unit: dict[str, list[float]] = {}
        isfinite = math.isfinite

        for idx, s in enumerate(eff):
            u = eff_y_units[idx]
            bucket = ys_by_unit.setdefault(u, [])
            n = min(len(s.x), len(s.y))
            for i in range(n):
                xv, yv = s.x[i], s.y[i]
                if isfinite(xv) and isfinite(yv):
                    xs.append(xv)
                    bucket.append(yv)

        if self.memory and self.memory_visible and self.math_mode == "OFF":
            for idx, ms in enumerate(self.memory):
                u = eff_y_units[idx] if idx < len(eff_y_units) else self._series_y_unit(ms)
                bucket = ys_by_unit.setdefault(u, [])
                n = min(len(ms.x), len(ms.y))
                for i in range(n):
                    xv, yv = ms.x[i], ms.y[i]
                    if isfinite(xv) and isfinite(yv):
                        xs.append(xv)
                        bucket.append(yv)

        if not xs:
            xr = self.manual_x_range or (0.0, 1.0)
            default_yr = self.manual_y_range or (0.0, float(self.divs_y))
            y_ranges = [self.series_y_ranges.get(i, default_yr) for i in range(len(eff))]
            axis_si = self._axis_series_idx(eff)
            yr = y_ranges[axis_si] if (axis_si is not None and axis_si < len(y_ranges)) else default_yr
            return xr, yr, False, eff, y_ranges, eff_y_units

        if self.manual_x_range is not None:
            xr = self.manual_x_range
        else:
            xlo, xhi = min(xs), max(xs)
            xr = (xlo - 0.5, xhi + 0.5) if xhi <= xlo else (xlo, xhi)

        same_unit = len(set(eff_y_units)) <= 1
        unit_auto: dict[str, tuple[float, float]] = {}
        for u, vals in ys_by_unit.items():
            unit_auto[u] = _auto_bounds(min(vals), max(vals), self.divs_y) if vals else (0.0, float(self.divs_y))

        y_ranges = []
        for idx, u in enumerate(eff_y_units):
            if idx in self.series_y_ranges:
                y_ranges.append(self.series_y_ranges[idx])
            elif same_unit and self.manual_y_range is not None:
                y_ranges.append(self.manual_y_range)
            else:
                y_ranges.append(unit_auto.get(u, (0.0, float(self.divs_y))))

        axis_si = self._axis_series_idx(eff)
        yr = y_ranges[axis_si] if (axis_si is not None and axis_si < len(y_ranges)) else (
            self.manual_y_range or (0.0, float(self.divs_y))
        )
        return xr, yr, True, eff, y_ranges, eff_y_units

    def _chip_font(self, *, bold: bool = False):
        return mono_font(max(9.0, float(UI_FONT_SIZE_PT) - 1.0), bold=bold)

    def _chip_bar_height(self) -> float:
        cfm = QFontMetricsF(self._chip_font())
        return max(20.0, math.ceil(cfm.height()) + 5.0)

    def _calc_plot_rect(self, fm: QFontMetricsF, y_labels: Sequence[str]) -> QRectF:
        chip_h = self._chip_bar_height()
        if self.show_axis_labels:
            y_w = max((fm.horizontalAdvance(lbl) for lbl in y_labels), default=36.0)
            left = max(52.0, math.ceil(y_w) + 14.0)
            bottom = math.ceil(fm.height()) + 10.0 + chip_h + 6.0
        else:
            left = 10.0
            bottom = 6.0 + chip_h + 6.0
        top = 12.0 + self.top_pad
        right = 14.0
        w = max(20.0, self.width() - left - right)
        h = max(20.0, self.height() - top - bottom)
        return QRectF(left, top, w, h)

    def _y_range_for(self, series_index: int | None) -> tuple[float, float]:
        if series_index is not None and 0 <= series_index < len(self._cur_y_ranges):
            return self._cur_y_ranges[series_index]
        return self._cur_y_range

    def _data_to_screen(self, xv: float, yv: float, *, series_index: int | None = None) -> QPointF:
        r = self._plot_rect
        x0, x1 = self._cur_x_range
        y0, y1 = self._y_range_for(series_index)
        px = r.left() + (xv - x0) / (x1 - x0) * r.width() if x1 != x0 else r.left()
        py = r.bottom() - (yv - y0) / (y1 - y0) * r.height() if y1 != y0 else r.bottom()
        return QPointF(px, py)

    def _screen_to_data(self, pos: QPointF, *, series_index: int | None = None) -> tuple[float, float] | None:
        r = self._plot_rect
        if r.width() <= 1 or r.height() <= 1 or not r.contains(pos):
            return None
        x0, x1 = self._cur_x_range
        y0, y1 = self._y_range_for(series_index)
        xv = x0 + (pos.x() - r.left()) / r.width() * (x1 - x0)
        yv = y0 + (r.bottom() - pos.y()) / r.height() * (y1 - y0)
        return xv, yv

    def _hit_marker(self, pos: QPointF) -> PlotMarker | None:
        if not self.markers or not self._plot_rect.contains(pos):
            return None
        eff, _ = self._effective_series_and_units()
        best: PlotMarker | None = None
        best_dist = 12.0
        for m in self.markers:
            yv = self.marker_y(m, eff)
            if yv is None:
                continue
            si = max(0, min(m.series_index, len(eff) - 1)) if eff else None
            pt = self._data_to_screen(m.x, yv, series_index=si)
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d <= best_dist:
                best_dist = d
                best = m
        return best

    @staticmethod
    def _point_to_segment_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        dx = bx - ax
        dy = by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 <= 1e-12:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _hit_series(self, pos: QPointF, max_dist_px: float = 8.0) -> int | None:
        """Найти ближайшую кривую к экранной точке ``pos`` в пределах ``max_dist_px`` пикселей."""
        r = self._plot_rect
        if not self.series or r.width() <= 1 or r.height() <= 1 or not r.contains(pos):
            return None
        eff, _ = self._effective_series_and_units()
        if not eff:
            return None
        x0, x1 = self._cur_x_range
        if x1 == x0:
            return None

        px, py = pos.x(), pos.y()
        sx = r.width() / (x1 - x0)
        left, bottom = r.left(), r.bottom()
        x_lo = x0 + (px - max_dist_px - left) / sx
        x_hi = x0 + (px + max_dist_px - left) / sx
        if x_lo > x_hi:
            x_lo, x_hi = x_hi, x_lo

        best_idx: int | None = None
        best_dist = max_dist_px
        isfinite = math.isfinite

        for idx, s in enumerate(eff):
            y0_i, y1_i = self._y_range_for(idx)
            if y1_i == y0_i:
                continue
            sy = r.height() / (y1_i - y0_i)
            xs, ys = s.x, s.y
            n = min(len(xs), len(ys))
            if n == 0:
                continue
            if n == 1:
                xv, yv = xs[0], ys[0]
                if isfinite(xv) and isfinite(yv):
                    d = math.hypot(px - (left + (xv - x0) * sx), py - (bottom - (yv - y0_i) * sy))
                    if d <= best_dist:
                        best_dist = d
                        best_idx = idx
                continue
            if xs[0] <= xs[-1]:
                i0 = max(0, bisect_left(xs, x_lo, 0, n) - 1)
                i1 = min(n - 1, bisect_left(xs, x_hi, 0, n) + 1)
            else:
                i0, i1 = 0, n - 1

            prev_pt: tuple[float, float] | None = None
            for i in range(i0, i1 + 1):
                xv, yv = xs[i], ys[i]
                if not (isfinite(xv) and isfinite(yv)):
                    prev_pt = None
                    continue
                cur_x = left + (xv - x0) * sx
                cur_y = bottom - (yv - y0_i) * sy
                if prev_pt is not None:
                    d = self._point_to_segment_dist(px, py, prev_pt[0], prev_pt[1], cur_x, cur_y)
                else:
                    d = math.hypot(px - cur_x, py - cur_y)
                if d <= best_dist:
                    best_dist = d
                    best_idx = idx
                prev_pt = (cur_x, cur_y)
        return best_idx

    # ------------------------------------------------------------ отрисовка
    def _draw_legend_row(self, p: QPainter, rect: QRectF, name: str, col: QColor, *,
                         dashed: bool = False, active: bool = False, hovered: bool = False) -> None:
        """Строка кривой в полупрозрачной легенде: без рамки, с цветным подчёркиванием выбранной кривой."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if hovered:
            hcol = _c("UI_COLOR_INVERT_HOVER")
            hcol.setAlpha(42 if current_theme() == "dark" else 28)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hcol)
            p.drawRoundedRect(rect, 4.0, 4.0)

        font_use = mono_font(bold=True) if active else self._font
        p.setFont(font_use)
        cur_fm = QFontMetricsF(font_use)
        pad_x = 5.0
        sw_len = 12.0
        gap = 6.0
        sw_x = rect.left() + pad_x
        y_shift = 1.0 if active else 0.0
        cy = rect.center().y() - y_shift

        sw_pen = QPen(col, 2.5 if active else 2.0, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
        sw_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(sw_pen)
        p.drawLine(QPointF(sw_x, cy), QPointF(sw_x + sw_len, cy))

        p.setPen(_c("UI_COLOR_TEXT") if (active or hovered) else _c("UI_COLOR_TEXT_MUTED"))
        text_x = sw_x + sw_len + gap
        text_w = max(1.0, rect.right() - text_x - pad_x + 1.0)
        elided = cur_fm.elidedText(name, Qt.TextElideMode.ElideRight, text_w)
        p.drawText(QRectF(text_x, rect.top() - y_shift, text_w, rect.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        if active:
            ul_pen = QPen(col, 2.2)
            ul_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(ul_pen)
            p.drawLine(QPointF(rect.left() + pad_x, rect.bottom() - 1.5),
                       QPointF(rect.right() - pad_x, rect.bottom() - 1.5))
        p.restore()

    def _draw_legend_action_row(self, p: QPainter, rect: QRectF, text: str, *,
                                hovered: bool = False, muted: bool = False) -> None:
        """Компактная строка-кнопка внутри полупрозрачной легенды (``MEM ×`` или счётчик ``↕ k/N``)."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c("UI_COLOR_INVERT_HOVER"))
            p.drawRoundedRect(rect, 4.0, 4.0)
            p.setFont(mono_font(bold=True))
            p.setPen(_c("UI_COLOR_TEXT_INVERSE"))
        else:
            p.setFont(self._font)
            p.setPen(_c("UI_COLOR_BORDER_LIGHT") if muted else _c("UI_COLOR_TEXT_MUTED"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    def _place_overlay(self, key: str, rect: QRectF, box_w: float, box_h: float,
                       default_x: float, default_y: float) -> QRectF:
        """Разместить плавающую табличку (``legend`` / ``markers`` / ``analysis``) внутри ``rect``
        с учётом перенесённой пользователем позиции (в нормированных координатах ``0..1``)."""
        pad = 8.0
        min_x = rect.left() + pad
        min_y = rect.top() + pad
        max_x = max(min_x, rect.right() - pad - box_w)
        max_y = max(min_y, rect.bottom() - pad - box_h)
        pos = self._overlay_pos.get(key)
        if pos is not None:
            x = min_x + max(0.0, min(1.0, pos[0])) * (max_x - min_x)
            y = min_y + max(0.0, min(1.0, pos[1])) * (max_y - min_y)
        else:
            x = max(min_x, min(default_x, max_x))
            y = max(min_y, min(default_y, max_y))
        return QRectF(x, y, box_w, box_h)

    def _overlay_box_for(self, key: str) -> QRectF:
        if key == "legend":
            return self._legend_box
        if key == "markers":
            return self._markers_box
        if key == "analysis":
            return self._analysis_box
        return QRectF()

    def _hit_overlay(self, pos: QPointF) -> tuple[str, QRectF] | None:
        """Проверить попадание в плавающую табличку (сверху вниз по порядку отрисовки)."""
        for key in ("analysis", "markers", "legend"):
            box = self._overlay_box_for(key)
            if box.isValid() and box.contains(pos):
                return key, box
        return None

    def _draw_in_plot_legend(self, p: QPainter, rect: QRectF, fm: QFontMetricsF, bold_fm: QFontMetricsF,
                             eff: Sequence[PlotSeries], active_si: int | None) -> None:
        """Полупрозрачная легенда в левом верхнем углу внутри области графика (выбор кривой колёсиком или кликом)."""
        self._legend_box = QRectF()
        self._legend_hits = []
        self._legend_meta = {}
        self._legend_more_hit = None
        self._legend_more_text = ""

        curve_items: list[tuple[int, str, QColor, bool]] = [
            (idx, s.name, _series_color(idx, s.color), bool(s.dashed))
            for idx, s in enumerate(eff)
            if s.name
        ]
        if not curve_items:
            return

        row_h = max(21.0, math.ceil(fm.height()) + 5.0)
        foot_h = max(18.0, math.ceil(fm.height()) + 3.0)
        pad_x = 5.0
        pad_y = 5.0
        row_gap = 2.0

        # Максимум видимых строк кривых, чтобы легенда оставалась компактной
        max_vis = max(2, min(6, int((rect.height() * 0.62 - foot_h - 3.0 - pad_y * 2.0) // (row_h + row_gap))))
        total_curves = len(curve_items)
        overflow = total_curves > max_vis

        if overflow:
            if active_si is not None:
                pos = next((i for i, item in enumerate(curve_items) if item[0] == active_si), None)
                if pos is not None:
                    if pos < self._legend_scroll_idx:
                        self._legend_scroll_idx = pos
                    elif pos >= self._legend_scroll_idx + max_vis:
                        self._legend_scroll_idx = pos - max_vis + 1
            self._legend_scroll_idx = max(0, min(self._legend_scroll_idx, total_curves - max_vis))
            vis_items = curve_items[self._legend_scroll_idx:self._legend_scroll_idx + max_vis]
            if active_si is not None:
                pos = next((i for i, item in enumerate(curve_items) if item[0] == active_si), -1)
                more_txt = f"↕ {pos + 1}/{total_curves}" if pos >= 0 else f"↕ {total_curves}"
            else:
                more_txt = f"↕ {total_curves}"
        else:
            self._legend_scroll_idx = 0
            vis_items = curve_items
            more_txt = ""

        widths: list[float] = []
        for _idx, name, _col, _dashed in curve_items:
            tw = max(fm.horizontalAdvance(name), bold_fm.horizontalAdvance(name))
            widths.append(5.0 + 12.0 + 6.0 + tw + 6.0)
        if more_txt:
            widths.append(bold_fm.horizontalAdvance(f"↕ {total_curves}/{total_curves}") + 14.0)

        inner_w = max(widths, default=64.0)
        max_box_w = min(176.0, max(78.0, rect.width() * 0.38))
        box_w = min(max_box_w, inner_w + pad_x * 2.0)
        row_w = box_w - pad_x * 2.0

        content_h = len(vis_items) * row_h + row_gap * max(0, len(vis_items) - 1)
        if more_txt:
            content_h += 3.0 + foot_h
        box_h = pad_y * 2.0 + content_h

        box = self._place_overlay("legend", rect, box_w, box_h, rect.left() + 8.0, rect.top() + 8.0)
        self._legend_box = box

        bg = _c("UI_COLOR_BACKGROUND")
        bg.setAlpha(196)
        bdr = _c("UI_COLOR_BORDER")
        bdr.setAlpha(200)
        p.save()
        p.setPen(QPen(bdr, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(box, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)

        cy = box.top() + pad_y
        rx = box.left() + pad_x
        for idx, name, col, dashed in vis_items:
            r_row = QRectF(rx, cy, row_w, row_h)
            self._legend_hits.append((r_row, idx))
            self._legend_meta[idx] = (r_row, name, col, dashed)
            self._draw_legend_row(p, r_row, name, col, dashed=dashed,
                                  active=(active_si == idx), hovered=False)
            cy += row_h + row_gap
        cy -= row_gap

        if more_txt:
            cy += 3.0
            more_rect = QRectF(rx, cy, row_w, foot_h)
            self._legend_more_hit = more_rect
            self._legend_more_text = more_txt
            self._draw_legend_action_row(p, more_rect, more_txt, hovered=False, muted=True)
        p.restore()

    def _draw_analysis_box(self, p: QPainter, rect: QRectF, rows: Sequence[str], *,
                           header_name: str = "", header_col: QColor | None = None,
                           top_obstacle: float = 0.0) -> None:
        """Полупрозрачная карточка аналитики (в стиле легенды) в левом нижнем углу графика:
        показывает все включённые метрики целиком, при нехватке высоты раскладывая их в 2–3 столбца."""
        if not rows:
            return
        pairs: list[tuple[str, str]] = []
        for r in rows:
            k, _, v = r.partition(" ")
            pairs.append((k, v))

        an_font = self._chip_font()
        an_bold = self._chip_font(bold=True)
        afm = QFontMetricsF(an_font)
        abfm = QFontMetricsF(an_bold)
        row_h = math.ceil(afm.height()) + 3.0
        pad_x = 8.0
        pad_y = 5.0
        kv_gap = 8.0
        col_gap = 14.0
        header_h = (row_h + 4.0) if header_name else 0.0

        avail_h = (
            max(48.0, rect.height() - 16.0)
            if "analysis" in self._overlay_pos
            else max(48.0, rect.bottom() - max(rect.top() + 8.0, top_obstacle) - 14.0)
        )
        avail_w = max(120.0, rect.width() - 16.0)

        def _measure_cols(ncols: int) -> tuple[int, list[tuple[float, float]], float, float]:
            per_col = max(1, math.ceil(len(pairs) / ncols))
            cols_dims: list[tuple[float, float]] = []
            for ci in range(ncols):
                chunk = pairs[ci * per_col:(ci + 1) * per_col]
                if not chunk:
                    continue
                kw = max(afm.horizontalAdvance(k) for k, _ in chunk)
                vw = max(afm.horizontalAdvance(v) for _, v in chunk)
                cols_dims.append((kw, vw))
            actual_cols = len(cols_dims)
            w = pad_x * 2.0 + sum(kw + kv_gap + vw for kw, vw in cols_dims) + col_gap * max(0, actual_cols - 1)
            if header_name:
                w = max(w, pad_x * 2.0 + 18.0 + abfm.horizontalAdvance(header_name) + 4.0)
            h = pad_y * 2.0 + header_h + per_col * row_h
            return per_col, cols_dims, w, h

        chosen = _measure_cols(1)
        if chosen[3] > avail_h and len(pairs) > 2:
            for cand_n in (2, 3):
                cand = _measure_cols(cand_n)
                if cand[2] <= avail_w:
                    chosen = cand
                    if cand[3] <= avail_h:
                        break
                elif cand_n == 2 and chosen[3] > rect.height() - 20.0:
                    chosen = cand
                    break

        per_col, cols_dims, box_w, box_h = chosen
        box_w = min(box_w, avail_w)
        box_h = min(box_h, max(40.0, rect.height() - 16.0))
        abox = self._place_overlay("analysis", rect, box_w, box_h,
                                   rect.right() - box_w - 8.0, rect.bottom() - box_h - 8.0)
        self._analysis_box = abox

        bg = _c("UI_COLOR_BACKGROUND")
        bg.setAlpha(196)
        bdr = _c("UI_COLOR_BORDER")
        bdr.setAlpha(200)
        p.save()
        p.setPen(QPen(bdr, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(abox, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)

        p.setClipRect(abox.adjusted(1.0, 1.0, -1.0, -1.0))
        y0_cur = abox.top() + pad_y
        if header_name:
            hx = abox.left() + pad_x
            if header_col is not None:
                sw_pen = QPen(header_col, 2.2)
                sw_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(sw_pen)
                hcy = y0_cur + row_h / 2.0
                p.drawLine(QPointF(hx, hcy), QPointF(hx + 11.0, hcy))
                hx += 17.0
            p.setFont(an_bold)
            p.setPen(_c("UI_COLOR_TEXT"))
            p.drawText(QRectF(hx, y0_cur, max(10.0, abox.right() - hx - pad_x), row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, header_name)
            sep_y = y0_cur + row_h + 1.5
            sep_c = _c("UI_COLOR_BORDER")
            sep_c.setAlpha(130)
            p.setPen(QPen(sep_c, 1.0))
            p.drawLine(QPointF(abox.left() + pad_x, sep_y), QPointF(abox.right() - pad_x, sep_y))
            y0_cur += header_h

        p.setFont(an_font)
        key_col = _c("UI_COLOR_BORDER_LIGHT")
        val_col = _c("UI_COLOR_TEXT")
        cx = abox.left() + pad_x
        actual_cols = len(cols_dims)
        avail_inner_w = max(20.0, abox.width() - pad_x * 2.0 - col_gap * max(0, actual_cols - 1))
        raw_sum = sum(kw + kv_gap + vw for kw, vw in cols_dims) or 1.0
        scale_w = min(1.0, avail_inner_w / raw_sum)

        for ci, (kw, vw) in enumerate(cols_dims):
            col_w = (kw + kv_gap + vw) * scale_w
            kw_s = kw * scale_w
            vw_s = max(10.0, col_w - kw_s - kv_gap * scale_w)
            if ci > 0:
                div_x = cx - col_gap / 2.0
                div_c = _c("UI_COLOR_BORDER")
                div_c.setAlpha(95)
                p.setPen(QPen(div_c, 1.0))
                p.drawLine(QPointF(div_x, y0_cur + 1.0), QPointF(div_x, abox.bottom() - pad_y - 1.0))
            chunk = pairs[ci * per_col:(ci + 1) * per_col]
            for ri, (k_lbl, v_lbl) in enumerate(chunk):
                ry = y0_cur + ri * row_h
                p.setPen(key_col)
                p.drawText(QRectF(cx, ry, kw_s + 2.0, row_h),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, k_lbl)
                p.setPen(val_col)
                v_elided = afm.elidedText(v_lbl, Qt.TextElideMode.ElideRight, vw_s + 2.0)
                p.drawText(QRectF(cx + col_w - vw_s - 2.0, ry, vw_s + 2.0, row_h),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, v_elided)
            cx += col_w + col_gap
        p.restore()

    def _active_setting_chips(self) -> list[tuple[str, str, Callable[[], None]]]:
        """Компактные чипы всех активных настроек/оверлеев графика (сброс по клику) для нижней полосы."""
        chips: list[tuple[str, str, Callable[[], None]]] = []
        active_si = self._active_series_idx(self.series)
        if active_si is not None and active_si in self.series_y_ranges:
            sname = self.series[active_si].name or f"#{active_si + 1}"
            chips.append((
                f"scale_{active_si}",
                f"{sname} Y ×",
                lambda si=active_si: self.set_y_range(None, None, series_index=si),
            ))
        other_y = (
            self.manual_y_range is not None
            or any(k != active_si for k in self.series_y_ranges)
        )
        if self.manual_x_range is not None or other_y:
            if self.manual_x_range is not None and (other_y or (active_si in self.series_y_ranges)):
                lbl = "SCALE ×"
            elif self.manual_x_range is not None:
                lbl = "X:MAN ×"
            else:
                lbl = "Y:MAN ×"
            chips.append(("scale", lbl, self.reset_view))
        if self.memory:
            chips.append(("mem", "MEM ×" if self.memory_visible else "MEM(off) ×", self.clear_memory))
        if self.math_mode != "OFF":
            tag = self.math_expr if (self.math_mode == "EXPR" and self.math_expr) else MATH_LABELS.get(self.math_mode, self.math_mode)
            if len(tag) > 14:
                tag = tag[:13] + "…"
            chips.append(("math", f"{tag} ×", lambda: self.set_math_mode("OFF")))
        if self.markers:
            lbl = f"MK:{len(self.markers)} ×" if len(self.markers) > 1 else f"{self.markers[0].name} ×"
            chips.append(("markers", lbl, self.clear_markers))
        if self.show_peaks:
            chips.append(("peaks", "PEAKS ×", lambda: self.set_show_peaks(False)))
        if self.show_point_values:
            chips.append(("values", "VALS ×", lambda: self.set_show_point_values(False)))
        if self.limits:
            lbl = f"LIM:{len(self.limits)} ×" if len(self.limits) > 1 else "LIM ×"
            chips.append(("limits", lbl, self.clear_limits))
        if self.analysis_metrics:
            lbl = f"AN:{len(self.analysis_metrics)} ×" if len(self.analysis_metrics) > 1 else "AN ×"
            chips.append(("analysis", lbl, lambda: self.set_analysis_metrics([])))
        if self.analysis_x_expr:
            tag = self.analysis_x_expr if len(self.analysis_x_expr) <= 14 else (self.analysis_x_expr[:13] + "…")
            chips.append(("an_x", f"X:{tag} ×", lambda: self.set_analysis_x_expr("")))
        return chips

    def _draw_setting_chip(self, p: QPainter, rect: QRectF, text: str, *, hovered: bool = False) -> None:
        """Компактный чип активной настройки в нижней полосе графика (единый стиль кнопок)."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = 5.0
        if hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c("UI_COLOR_INVERT_HOVER"))
            p.drawRoundedRect(rect, radius, radius)
            p.setFont(self._chip_font(bold=True))
            p.setPen(_c("UI_COLOR_TEXT_INVERSE"))
        else:
            p.setPen(QPen(_c("UI_COLOR_BORDER"), float(UI_BORDER_WIDTH_PX)))
            p.setBrush(_c("UI_COLOR_BACKGROUND"))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            p.setFont(self._chip_font())
            p.setPen(_c("UI_COLOR_TEXT_MUTED"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    def _draw_bottom_chips(self, p: QPainter, rect: QRectF, fm: QFontMetricsF) -> None:
        """Компактная нижняя полоса активных настроек (``SCALE ×``, ``MEM ×``, ``D−M ×``, ``MK ×``, ``PEAKS ×`` и т.д.)."""
        self._chip_hits = []
        self._chips_max_scroll = 0.0
        chip_h = self._chip_bar_height()
        chip_top = rect.bottom() + (math.ceil(fm.height()) + 10.0 if self.show_axis_labels else 6.0)
        strip_rect = QRectF(rect.left(), chip_top - 1.0, rect.width(), chip_h + 2.0)
        self._chips_strip_rect = strip_rect

        chips = self._active_setting_chips()
        if not chips:
            self._chips_scroll_x = 0.0
            return

        cfm = QFontMetricsF(self._chip_font())
        cb_fm = QFontMetricsF(self._chip_font(bold=True))
        gap = 5.0
        measured: list[tuple[str, str, Callable[[], None], float]] = []
        for cid, lbl, cb in chips:
            tw = max(cfm.horizontalAdvance(lbl), cb_fm.horizontalAdvance(lbl))
            measured.append((cid, lbl, cb, tw + 14.0))

        total_w = sum(w for *_, w in measured) + gap * max(0, len(measured) - 1)
        self._chips_max_scroll = max(0.0, total_w - rect.width())
        self._chips_scroll_x = max(0.0, min(self._chips_scroll_x, self._chips_max_scroll))

        p.save()
        if self._chips_max_scroll > 0.0:
            p.setClipRect(strip_rect)
        lx = rect.left() - self._chips_scroll_x
        for cid, lbl, cb, cw in measured:
            full_r = QRectF(lx, chip_top, cw, chip_h)
            if full_r.right() > strip_rect.left() and full_r.left() < strip_rect.right():
                self._draw_setting_chip(p, full_r, lbl, hovered=False)
                hit_r = full_r.intersected(strip_rect) if self._chips_max_scroll > 0.0 else full_r
                if hit_r.width() >= 4.0:
                    self._chip_hits.append((hit_r, cid, lbl, cb, full_r))
            lx += cw + gap

        if self._chips_max_scroll > 0.0:
            fade_w = 14.0
            bg_opaque = _c("UI_COLOR_BACKGROUND")
            bg_clear = QColor(bg_opaque)
            bg_clear.setAlpha(0)
            if self._chips_scroll_x > 1.0:
                g_l = QLinearGradient(strip_rect.left(), 0.0, strip_rect.left() + fade_w, 0.0)
                g_l.setColorAt(0.0, bg_opaque)
                g_l.setColorAt(1.0, bg_clear)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(g_l)
                p.drawRect(QRectF(strip_rect.left(), strip_rect.top(), fade_w, strip_rect.height()))
            if self._chips_scroll_x < self._chips_max_scroll - 1.0:
                g_r = QLinearGradient(strip_rect.right() - fade_w, 0.0, strip_rect.right(), 0.0)
                g_r.setColorAt(0.0, bg_clear)
                g_r.setColorAt(1.0, bg_opaque)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(g_r)
                p.drawRect(QRectF(strip_rect.right() - fade_w, strip_rect.top(), fade_w, strip_rect.height()))
        p.restore()

    def _ensure_cache(self) -> QPixmap:
        dpr = max(1.0, float(self.devicePixelRatioF()))
        ov_key = tuple(sorted((k, round(v[0], 4), round(v[1], 4)) for k, v in self._overlay_pos.items()))
        syr_key = tuple(sorted((k, round(v[0], 9), round(v[1], 9)) for k, v in self.series_y_ranges.items()))
        key = (self.width(), self.height(), dpr, current_theme(), self._data_version,
               self.show_grid, self.show_points, self.show_point_values, self.show_peaks,
               self.show_axis_labels, self.divs_y, self.manual_x_range, self.manual_y_range,
               syr_key, self.active_series_index,
               self._legend_scroll_idx, round(self._chips_scroll_x, 1),
               round(self._top_right_pad, 1), round(self.top_pad, 1), ov_key)
        if self._cache is not None and self._cache_key == key:
            return self._cache

        pix = QPixmap(max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr))))
        pix.setDevicePixelRatio(dpr)
        pix.fill(_c("UI_COLOR_BACKGROUND"))

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._font = mono_font()
        p.setFont(self._font)
        fm = QFontMetricsF(self._font)
        bold_fm = QFontMetricsF(mono_font(bold=True))

        xr, yr, has_data, eff, y_ranges, eff_y_units = self._compute_ranges()
        self._cur_x_range, self._cur_y_range = xr, yr
        self._cur_y_ranges = list(y_ranges)
        self._cur_y_units = list(eff_y_units)
        x0, x1 = xr
        y0, y1 = yr
        divs_y = self.divs_y
        y_step = (y1 - y0) / divs_y if divs_y else 1.0
        x_step = (x1 - x0) / self.DIVS_X if self.DIVS_X else 1.0

        active_si = self._active_series_idx(eff)
        axis_si = self._axis_series_idx(eff)
        axis_y_unit = eff_y_units[axis_si] if (axis_si is not None and axis_si < len(eff_y_units)) else self.y_unit
        is_multi_scale = len(set(eff_y_units)) > 1 or bool(self.series_y_ranges)

        y_ticks = [y0 + i * y_step for i in range(divs_y + 1)]
        y_labels = [_fmt_value(v, axis_y_unit, y_step, include_unit=(i == divs_y)) for i, v in enumerate(y_ticks)]

        rect = self._calc_plot_rect(fm, y_labels)
        self._plot_rect = rect

        c_border = _c("UI_COLOR_BORDER")
        c_muted = _c("UI_COLOR_BORDER_LIGHT")
        c_text = _c("UI_COLOR_TEXT_MUTED")
        c_panel = _c("UI_COLOR_PANEL")

        # Фон области графика
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c_panel)
        p.drawRoundedRect(rect, 3.0, 3.0)

        # Сетка
        if self.show_grid:
            grid_pen = QPen(c_border, 1.0, Qt.PenStyle.DotLine)
            p.setPen(grid_pen)
            for i in range(1, self.DIVS_X):
                gx = rect.left() + rect.width() * i / self.DIVS_X
                p.drawLine(QPointF(gx, rect.top()), QPointF(gx, rect.bottom()))
            for i in range(1, divs_y):
                gy = rect.bottom() - rect.height() * i / divs_y
                p.drawLine(QPointF(rect.left(), gy), QPointF(rect.right(), gy))

        # Подписи осей X и Y (при разных единицах/масштабах ось Y окрашивается в цвет кривой-владельца шкалы)
        if self.show_axis_labels:
            y_lbl_col = (
                _series_color(axis_si, eff[axis_si].color)
                if (is_multi_scale and axis_si is not None and axis_si < len(eff))
                else c_muted
            )
            p.setPen(y_lbl_col)
            for i, lbl in enumerate(y_labels):
                gy = rect.bottom() - rect.height() * i / divs_y
                p.drawText(QRectF(2.0, gy - fm.height() / 2.0, rect.left() - 8.0, fm.height()),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

            p.setPen(c_muted)
            x_tick_count = 5 if rect.width() >= 320 else 3
            for i in range(x_tick_count):
                frac = i / (x_tick_count - 1) if x_tick_count > 1 else 0.0
                xv = x0 + (x1 - x0) * frac
                gx = rect.left() + rect.width() * frac
                lbl = _fmt_value(xv, self.x_unit, x_step, include_unit=True)
                lw = fm.horizontalAdvance(lbl) + 4.0
                if i == 0:
                    tx, align = rect.left(), Qt.AlignmentFlag.AlignLeft
                elif i == x_tick_count - 1:
                    tx, align = rect.right() - lw, Qt.AlignmentFlag.AlignRight
                else:
                    tx, align = gx - lw / 2.0, Qt.AlignmentFlag.AlignHCenter
                p.drawText(QRectF(tx, rect.bottom() + 4.0, lw, fm.height() + 2.0),
                           align | Qt.AlignmentFlag.AlignVCenter, lbl)

        self._legend_box = QRectF()
        self._markers_box = QRectF()
        self._analysis_box = QRectF()
        self._legend_hits = []
        self._legend_meta = {}
        self._legend_more_hit = None
        self._legend_more_text = ""

        # Содержимое области графика (внутри clip)
        if has_data:
            p.save()
            p.setClipRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

            # 1. Память (MEM) пунктиром на заднем плане (в масштабе своей кривой)
            if self.memory and self.memory_visible and self.math_mode == "OFF":
                for idx, ms in enumerate(self.memory):
                    mcol = _series_color(idx, ms.color)
                    mcol.setAlpha(115)
                    mpen = QPen(mcol, 1.3, Qt.PenStyle.DashLine)
                    p.setPen(mpen)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    my0, my1 = y_ranges[idx] if idx < len(y_ranges) else (y0, y1)
                    for pts in _decimate_paths(ms.x, ms.y, x0, x1, my0, my1, rect):
                        if len(pts) >= 2:
                            path = QPainterPath(pts[0])
                            for pt in pts[1:]:
                                path.lineTo(pt)
                            p.drawPath(path)

            # 2. Линии пределов (LIMIT LINES)
            if self.limits:
                lim_col = _c("UI_COLOR_ERROR")
                lim_pen = QPen(lim_col, 1.5, Qt.PenStyle.DashLine)
                for lim in self.limits:
                    lx1 = x0 if lim.x1 is None else lim.x1
                    lx2 = x1 if lim.x2 is None else lim.x2
                    ly2 = lim.y1 if lim.y2 is None else lim.y2
                    pt1 = self._data_to_screen(lx1, lim.y1)
                    pt2 = self._data_to_screen(lx2, ly2)
                    p.setPen(lim_pen)
                    p.drawLine(pt1, pt2)
                    if lim.x1 is not None or lim.x2 is not None:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(lim_col)
                        p.drawEllipse(pt1, 2.8, 2.8)
                        p.drawEllipse(pt2, 2.8, 2.8)
                    tick_dir = -5.0 if lim.kind == "MAX" else 5.0
                    p.setPen(QPen(lim_col, 1.1))
                    for frac in (0.15, 0.5, 0.85):
                        tx = pt1.x() + (pt2.x() - pt1.x()) * frac
                        ty = pt1.y() + (pt2.y() - pt1.y()) * frac
                        p.drawLine(QPointF(tx, ty), QPointF(tx, ty + tick_dir))

            # 3. Основные кривые (каждая в своём диапазоне Y; выбранная — поверх и толще)
            has_multi = len(eff) > 1
            draw_order = [i for i in range(len(eff)) if i != active_si]
            if active_si is not None:
                draw_order.append(active_si)
            for idx in draw_order:
                s = eff[idx]
                sy0, sy1 = y_ranges[idx] if idx < len(y_ranges) else (y0, y1)
                col = _series_color(idx, s.color)
                is_active = (active_si == idx)
                if is_active:
                    lw = max(s.line_width * 1.65, s.line_width + 1.0)
                    pt_r_big, pt_r_small = 3.4, 3.0
                elif active_si is not None and has_multi:
                    lw = max(1.0, s.line_width * 0.85)
                    col = QColor(col)
                    col.setAlpha(195)
                    pt_r_big, pt_r_small = 2.4, 2.1
                else:
                    lw = s.line_width
                    pt_r_big, pt_r_small = 2.7, 2.4
                pen = QPen(col, lw)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                if s.dashed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                segments = _decimate_paths(s.x, s.y, x0, x1, sy0, sy1, rect)
                for pts in segments:
                    if len(pts) >= 2:
                        path = QPainterPath(pts[0])
                        for pt in pts[1:]:
                            path.lineTo(pt)
                        p.drawPath(path)
                if self.show_points:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(col)
                    vis_pts: list[QPointF] = []
                    x_span = x1 - x0
                    y_span = sy1 - sy0
                    if x_span != 0.0 and y_span != 0.0:
                        rx, rw = rect.left(), rect.width()
                        rb, rh = rect.bottom(), rect.height()
                        for xv, yv in zip(s.x, s.y):
                            if not (math.isfinite(xv) and math.isfinite(yv)):
                                continue
                            if xv < x0 or xv > x1:
                                continue
                            px = rx + (xv - x0) / x_span * rw
                            py = rb - (yv - sy0) / y_span * rh
                            vis_pts.append(QPointF(px, py))
                    if len(vis_pts) <= 90:
                        for pt in vis_pts:
                            p.drawEllipse(pt, pt_r_big, pt_r_big)
                    else:
                        last_x = -1e9
                        min_gap = 7.0
                        for pt in vis_pts:
                            if pt.x() - last_x >= min_gap:
                                p.drawEllipse(pt, pt_r_small, pt_r_small)
                                last_x = pt.x()

            # 3b. Подписи значений точек (в единице и масштабе своей кривой)
            if self.show_point_values and eff and (x1 != x0):
                val_font = self._font
                p.setFont(val_font)
                vfm = fm
                lbl_h = vfm.height() + 2.0
                placed_rects: list[QRectF] = []
                rx, rw = rect.left(), rect.width()
                rb, rh = rect.bottom(), rect.height()
                x_span = x1 - x0
                pill_bg = _c("UI_COLOR_BACKGROUND")
                pill_bg.setAlpha(205)

                for idx, s in enumerate(eff):
                    sy0, sy1 = y_ranges[idx] if idx < len(y_ranges) else (y0, y1)
                    y_span = sy1 - sy0
                    if y_span == 0.0:
                        continue
                    s_yu = eff_y_units[idx] if idx < len(eff_y_units) else axis_y_unit
                    col = _series_color(idx, s.color)
                    vis_raw: list[tuple[float, float, float]] = []
                    for xv, yv in zip(s.x, s.y):
                        if not (math.isfinite(xv) and math.isfinite(yv)):
                            continue
                        if xv < x0 or xv > x1:
                            continue
                        px = rx + (xv - x0) / x_span * rw
                        py = rb - (yv - sy0) / y_span * rh
                        if rect.top() <= py <= rect.bottom():
                            vis_raw.append((px, py, yv))
                    if not vis_raw:
                        continue

                    step_sample = max(1, len(vis_raw) // 60)
                    max_lbl_w = max(
                        (vfm.horizontalAdvance(_fmt_point_label(vis_raw[i][2], s_yu)) + 6.0
                         for i in range(0, len(vis_raw), step_sample)),
                        default=32.0,
                    )
                    min_dist_px = max(max_lbl_w + 8.0, lbl_h + 8.0)

                    last_px = -1e9
                    last_py = -1e9
                    for px, py, yv in vis_raw:
                        if math.hypot(px - last_px, py - last_py) < min_dist_px:
                            continue
                        txt = _fmt_point_label(yv, s_yu)
                        lw = vfm.horizontalAdvance(txt) + 6.0
                        lx = max(rect.left() + 2.0, min(px - lw / 2.0, rect.right() - lw - 2.0))
                        above_y = py - lbl_h - 5.0
                        below_y = py + 5.0
                        cand_ys = (above_y, below_y) if above_y >= rect.top() + 2.0 else (below_y, above_y)
                        chosen: QRectF | None = None
                        for cy in cand_ys:
                            if cy < rect.top() + 1.0 or cy + lbl_h > rect.bottom() - 1.0:
                                continue
                            cand = QRectF(lx, cy, lw, lbl_h)
                            if not any(cand.intersects(pr) for pr in placed_rects):
                                chosen = cand
                                break
                        if chosen is None:
                            continue
                        if not self.show_points:
                            p.setPen(Qt.PenStyle.NoPen)
                            p.setBrush(col)
                            p.drawEllipse(QPointF(px, py), 2.4, 2.4)
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(pill_bg)
                        p.drawRoundedRect(chosen, 3.0, 3.0)
                        p.setPen(col)
                        p.drawText(chosen, Qt.AlignmentFlag.AlignCenter, txt)
                        placed_rects.append(chosen.adjusted(-4.0, -2.0, 4.0, 2.0))
                        last_px, last_py = px, py
                p.setFont(self._font)

            # 3c. Режим автопоиска всех пиков (в масштабе и единице активной кривой)
            if self.show_peaks and eff and (x1 != x0):
                pk_si = active_si if active_si is not None else 0
                pk_series = eff[pk_si]
                pk_yu = eff_y_units[pk_si] if pk_si < len(eff_y_units) else axis_y_unit
                pk_col = _series_color(pk_si, pk_series.color)
                found_peaks = self._find_peaks(pk_series, xr)
                pk_font = self._font
                p.setFont(pk_font)
                pfm = fm
                pk_h = pfm.height() + 4.0
                pk_bg = _c("UI_COLOR_BACKGROUND")
                pk_bg.setAlpha(225)
                pk_placed: list[QRectF] = []
                for pk_x, pk_y in found_peaks:
                    pt = self._data_to_screen(pk_x, pk_y, series_index=pk_si)
                    if not rect.contains(pt):
                        continue
                    p.setPen(QPen(_c("UI_COLOR_BACKGROUND"), 1.4))
                    p.setBrush(pk_col)
                    p.drawEllipse(pt, 3.6, 3.6)

                    y_part = _fmt_point_label(pk_y, pk_yu)
                    if pk_yu and pk_yu not in _SI_UNITS:
                        y_part = f"{y_part} {pk_yu}"
                    pk_txt = f"{_fmt_value(pk_x, self.x_unit)}, {y_part}"
                    lw = pfm.horizontalAdvance(pk_txt) + 10.0
                    lx = max(rect.left() + 3.0, min(pt.x() - lw / 2.0, rect.right() - lw - 3.0))
                    above_y = pt.y() - pk_h - 7.0
                    below_y = pt.y() + 7.0
                    cand_ys = (above_y, below_y) if above_y >= rect.top() + 2.0 else (below_y, above_y)
                    chosen_pk: QRectF | None = None
                    for cy in cand_ys:
                        if cy < rect.top() + 1.0 or cy + pk_h > rect.bottom() - 1.0:
                            continue
                        cand = QRectF(lx, cy, lw, pk_h)
                        if not any(cand.intersects(pr) for pr in pk_placed):
                            chosen_pk = cand
                            break
                    if chosen_pk is None:
                        continue
                    p.setPen(QPen(pk_col, 0.9))
                    p.setBrush(pk_bg)
                    p.drawRoundedRect(chosen_pk, 4.0, 4.0)
                    p.setPen(_c("UI_COLOR_TEXT"))
                    p.drawText(chosen_pk, Qt.AlignmentFlag.AlignCenter, pk_txt)
                    pk_placed.append(chosen_pk.adjusted(-3.0, -2.0, 3.0, 2.0))
                p.setFont(self._font)

            # 4. Локальные маркеры на кривых (в масштабе и единице своей кривой)
            resolved: dict[str, tuple[float, float]] = {}
            for m in self.markers:
                pos_xy = self.resolve_marker(m, eff, xr)
                if pos_xy is not None:
                    resolved[m.name] = pos_xy

            marker_lines: list[tuple[QColor, str]] = []
            for m in self.markers:
                pos_xy = resolved.get(m.name)
                if pos_xy is None:
                    continue
                mx, yv = pos_xy
                si = max(0, min(m.series_index, len(eff) - 1))
                m_yu = eff_y_units[si] if si < len(eff_y_units) else axis_y_unit
                mcol = _series_color(si, eff[si].color)
                pt = self._data_to_screen(mx, yv, series_index=si)
                tri = QPolygonF([
                    QPointF(pt.x(), pt.y() - 1.0),
                    QPointF(pt.x() - 5.0, pt.y() - 9.0),
                    QPointF(pt.x() + 5.0, pt.y() - 9.0),
                ])
                p.setPen(QPen(_c("UI_COLOR_BACKGROUND"), 1.0))
                p.setBrush(mcol)
                p.drawPolygon(tri)
                p.setPen(mcol)
                badge_lbl = f"{m.name}{'▲' if m.track == 'MAX' else ('▼' if m.track == 'MIN' else '')}"
                lbl_w = fm.horizontalAdvance(badge_lbl) + 4.0
                lbl_x = max(rect.left() + 2.0, min(pt.x() - lbl_w / 2.0, rect.right() - lbl_w - 2.0))
                lbl_y = max(rect.top() + 2.0, pt.y() - 11.0 - fm.height())
                p.drawText(QRectF(lbl_x, lbl_y, lbl_w, fm.height()),
                           Qt.AlignmentFlag.AlignCenter, badge_lbl)
                trk_tag = f" [{m.track}]" if m.track else ""
                if m.delta_ref and m.delta_ref in resolved:
                    ref_x, ref_y = resolved[m.delta_ref]
                    dx_txt = _fmt_signed_value(mx - ref_x, self.x_unit)
                    dy_unit = "dB" if m_yu == "dBm" else m_yu
                    dy_txt = _fmt_signed_value(yv - ref_y, dy_unit)
                    marker_lines.append((mcol, f"Δ{m.name}-{m.delta_ref}{trk_tag}: {dx_txt}  {dy_txt}"))
                    continue
                marker_lines.append((mcol, f"{m.name}{trk_tag}: {_fmt_value(mx, self.x_unit)}  {_fmt_value(yv, m_yu)}"))

            # Полупрозрачная легенда выбора кривых (сверху слева внутри графика)
            self._draw_in_plot_legend(p, rect, fm, bold_fm, eff, active_si)
            top_left_bottom = self._legend_box.bottom() if self._legend_box.isValid() else (rect.top() + 8.0)

            # Плашка сводки маркеров (сверху слева внутри графика, ниже легенды, переносимая)
            if marker_lines:
                line_h = fm.height() + 2.0
                box_w = max(fm.horizontalAdvance(t) for _, t in marker_lines) + 14.0
                box_h = len(marker_lines) * line_h + 6.0
                leg_near_tl = (
                    self._legend_box.isValid()
                    and self._legend_box.left() < rect.left() + 48.0
                    and self._legend_box.top() < rect.top() + 48.0
                )
                m_top = (self._legend_box.bottom() + 6.0) if leg_near_tl else (rect.top() + 8.0)
                box = self._place_overlay("markers", rect, box_w, box_h, rect.left() + 8.0, m_top)
                self._markers_box = box
                top_left_bottom = box.bottom()
                bg = _c("UI_COLOR_BACKGROUND")
                bg.setAlpha(225)
                p.setPen(QPen(c_border, 1.0))
                p.setBrush(bg)
                p.drawRoundedRect(box, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
                for row_i, (mcol, txt) in enumerate(marker_lines):
                    p.setPen(mcol)
                    p.drawText(QRectF(box.left() + 7.0, box.top() + 3.0 + row_i * line_h, box_w - 14.0, line_h),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)

            # Плашка PASS / FAIL для лимитов (сверху справа)
            lim_ok = self.evaluate_limits(eff, xr, eff_y_units)
            top_right_bottom = rect.top() + 8.0
            if lim_ok is not None:
                badge = "PASS" if lim_ok else "FAIL"
                bcol = _c("UI_COLOR_SUCCESS") if lim_ok else _c("UI_COLOR_ERROR")
                bw = fm.horizontalAdvance(badge) + 16.0
                bh = fm.height() + 6.0
                pill = QRectF(rect.right() - bw - 6.0 - self._top_right_pad, rect.top() + 6.0, bw, bh)
                top_right_bottom = pill.bottom()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bcol)
                p.drawRoundedRect(pill, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
                p.setPen(QColor("#ffffff"))
                p.drawText(pill, Qt.AlignmentFlag.AlignCenter, badge)

            # Плашка анализа (в единице активной кривой) + мягкая подсветка диапазона X
            if self.analysis_metrics and eff:
                an_idx = active_si if active_si is not None else 0
                s0 = eff[an_idx]
                an_yu = eff_y_units[an_idx] if an_idx < len(eff_y_units) else axis_y_unit
                t0 = Trace(list(s0.x), list(s0.y), self.x_unit, an_yu)
                use_xf = self._analysis_x_set is not None and not self._analysis_x_set.is_empty
                if use_xf:
                    tint = _c("UI_COLOR_INVERT")
                    tint.setAlpha(18)
                    edge = _c("UI_COLOR_BORDER_LIGHT")
                    edge.setAlpha(120)
                    for iv_lo, iv_hi in self._analysis_x_set.intervals(x0, x1):
                        px1 = self._data_to_screen(iv_lo, y0).x()
                        px2 = self._data_to_screen(iv_hi, y0).x()
                        if px2 - px1 >= 1.0:
                            p.setPen(Qt.PenStyle.NoPen)
                            p.setBrush(tint)
                            p.drawRect(QRectF(px1, rect.top(), px2 - px1, rect.height()))
                            p.setPen(QPen(edge, 1.0, Qt.PenStyle.DotLine))
                            p.drawLine(QPointF(px1, rect.top()), QPointF(px1, rect.bottom()))
                            p.drawLine(QPointF(px2, rect.top()), QPointF(px2, rect.bottom()))
                        else:
                            p.setPen(QPen(edge, 1.2, Qt.PenStyle.DashLine))
                            p.drawLine(QPointF(px1, rect.top()), QPointF(px1, rect.bottom()))
                    stats = analyze_trace(t0, x_filter=self._analysis_x_set)
                else:
                    stats = analyze_trace(t0, x_range=xr)
                rows = format_analysis(stats, self.analysis_metrics, self.x_unit, an_yu)
                if rows:
                    show_hdr = len(eff) > 1 and (active_si is not None or is_multi_scale) and bool(s0.name)
                    self._draw_analysis_box(
                        p, rect, rows,
                        header_name=s0.name if show_hdr else "",
                        header_col=_series_color(an_idx, s0.color) if show_hdr else None,
                        top_obstacle=top_right_bottom,
                    )

            p.restore()
        else:
            p.setPen(c_muted)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.empty_text)

        # Рамка области графика
        p.setPen(QPen(c_border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 3.0, 3.0)

        # Компактная нижняя полоса активных настроек/оверлеев (SCALE ×, MEM ×, D−M ×, MK ×, PEAKS ×, …)
        self._draw_bottom_chips(p, rect, fm)

        p.end()
        self._cache = pix
        self._cache_key = (
            self.width(), self.height(), dpr, current_theme(), self._data_version,
            self.show_grid, self.show_points, self.show_point_values, self.show_peaks,
            self.show_axis_labels, self.divs_y, self.manual_x_range, self.manual_y_range,
            syr_key, self.active_series_index,
            self._legend_scroll_idx, round(self._chips_scroll_x, 1),
            round(self._top_right_pad, 1), round(self.top_pad, 1), ov_key,
        )
        return pix

    def paintEvent(self, e):  # noqa: N802 - Qt
        pix = self._ensure_cache()
        p = QPainter(self)
        p.drawPixmap(0, 0, pix)

        if self.hasFocus() and self._plot_rect.isValid():
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(_c("UI_COLOR_BORDER_LIGHT"), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self._plot_rect, 3.0, 3.0)

        if self._legend_hover is not None and self._legend_hover in self._legend_meta:
            l_rect, l_name, l_col, l_dashed = self._legend_meta[self._legend_hover]
            self._draw_legend_row(
                p, l_rect, l_name, l_col,
                dashed=l_dashed,
                active=(self.active_series_index == self._legend_hover),
                hovered=True,
            )

        if self._legend_more_hover and self._legend_more_hit is not None:
            self._draw_legend_action_row(p, self._legend_more_hit, self._legend_more_text, hovered=True)

        if self._chip_hover is not None:
            for _hit_r, cid, lbl, _cb, full_r in self._chip_hits:
                if cid == self._chip_hover:
                    p.save()
                    if self._chips_max_scroll > 0.0 and self._chips_strip_rect.isValid():
                        p.setClipRect(self._chips_strip_rect)
                    self._draw_setting_chip(p, full_r, lbl, hovered=True)
                    p.restore()
                    break

        if self._mouse is not None and self._plot_rect.contains(self._mouse):
            data_pt = self._screen_to_data(self._mouse)
            if data_pt is not None:
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                r = self._plot_rect
                eff, eff_y_units = self._effective_series_and_units()
                axis_si = self._axis_series_idx(eff)
                readout_y_unit = eff_y_units[axis_si] if (axis_si is not None and axis_si < len(eff_y_units)) else self.y_unit
                xv, yv = data_pt
                mx, my = self._mouse.x(), self._mouse.y()
                snap_col: QColor | None = None
                snap_name = ""

                active_si = self._active_series_idx(eff)
                if active_si is not None:
                    s_act = eff[active_si]
                    nearest = _nearest_point(s_act, xv)
                    if nearest is not None:
                        xv, yv = nearest
                        sp = self._data_to_screen(xv, yv, series_index=active_si)
                        mx, my = sp.x(), sp.y()
                        snap_col = _series_color(active_si, s_act.color)
                        snap_name = s_act.name
                        readout_y_unit = eff_y_units[active_si] if active_si < len(eff_y_units) else readout_y_unit

                p.save()
                p.setClipRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
                cross_col = snap_col if snap_col is not None else _c("UI_COLOR_BORDER_LIGHT")
                if snap_col is not None:
                    cross_col = QColor(snap_col)
                    cross_col.setAlpha(155)
                p.setPen(QPen(cross_col, 1.0, Qt.PenStyle.DashLine))
                if r.left() <= mx <= r.right():
                    p.drawLine(QPointF(mx, r.top()), QPointF(mx, r.bottom()))
                if r.top() <= my <= r.bottom():
                    p.drawLine(QPointF(r.left(), my), QPointF(r.right(), my))
                if snap_col is not None and r.contains(QPointF(mx, my)):
                    p.setPen(QPen(_c("UI_COLOR_BACKGROUND"), 1.6))
                    p.setBrush(snap_col)
                    p.drawEllipse(QPointF(mx, my), 4.2, 4.2)
                p.restore()

                prefix = f"{snap_name}: " if snap_name else ""
                badge = f"{prefix}{_fmt_value(xv, self.x_unit)}   {_fmt_value(yv, readout_y_unit)}"
                p.setFont(self._font)
                fm = QFontMetricsF(self._font)
                bw = fm.horizontalAdvance(badge) + 14.0
                bh = fm.height() + 6.0
                # Если справа сверху есть плашка PASS/FAIL — сдвигаем координаты чуть ниже неё
                top_offset = (fm.height() + 16.0) if self.limits else 6.0
                pill = QRectF(r.right() - bw - 6.0 - self._top_right_pad, r.top() + top_offset, bw, bh)
                p.setPen(QPen(snap_col if snap_col is not None else _c("UI_COLOR_BORDER"), 1.0))
                p.setBrush(_c("UI_COLOR_BACKGROUND"))
                p.drawRoundedRect(pill, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
                p.setPen(_c("UI_COLOR_TEXT"))
                p.drawText(pill, Qt.AlignmentFlag.AlignCenter, badge)
        p.end()

    # ------------------------------------------------------------ мышь и клавиатура
    def mouseDoubleClickEvent(self, e):  # noqa: N802 - Qt
        pos = e.position()
        if e.button() == Qt.MouseButton.LeftButton and self._plot_rect.contains(pos):
            if self._hit_overlay(pos) is not None:
                e.accept()
                return
            self.store_memory()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def _hit_legend(self, pos: QPointF) -> int | None:
        for rect, idx in self._legend_hits:
            if rect.contains(pos):
                return idx
        return None

    def _hit_chip(self, pos: QPointF) -> tuple[str, Callable[[], None]] | None:
        for hit_r, cid, _lbl, cb, _full_r in self._chip_hits:
            if hit_r.contains(pos):
                return cid, cb
        return None

    def _open_legend_menu(self) -> None:
        """Открыть выпадающий список всех кривых (при переполнении шапки легенды)."""
        eff, _ = self._effective_series_and_unit()
        if not eff:
            return
        active_si = self._active_series_idx(eff)
        menu = QMenu(self)
        menu.setStyleSheet(_POPUP_MENU_QSS)
        free_prefix = "✓ " if active_si is None else "   "
        free_act = menu.addAction(f"{free_prefix}Free (no snap)")
        free_act.triggered.connect(lambda _c=False: self.set_active_series(None))
        menu.addSeparator()
        for idx, s in enumerate(eff):
            if not s.name:
                continue
            col = _series_color(idx, s.color)
            prefix = "✓ " if active_si == idx else "   "
            act = menu.addAction(_swatch_icon(col, dashed=bool(s.dashed)), f"{prefix}{s.name}")
            act.triggered.connect(
                lambda _c=False, i=idx: self.set_active_series(None if self.active_series_index == i else i)
            )
        anchor = self._legend_more_hit.bottomLeft() if self._legend_more_hit is not None else QPointF(self._plot_rect.left(), self._plot_rect.top())
        self._legend_more_hover = False
        self.update()
        menu.exec(self.mapToGlobal(QPoint(int(round(anchor.x())), int(round(anchor.y())) + 2)))

    def _move_overlay_to(self, key: str, top_left: QPointF, box: QRectF) -> None:
        r = self._plot_rect
        pad = 8.0
        min_x = r.left() + pad
        min_y = r.top() + pad
        max_x = max(min_x, r.right() - pad - box.width())
        max_y = max(min_y, r.bottom() - pad - box.height())
        nx = max(min_x, min(top_left.x(), max_x))
        ny = max(min_y, min(top_left.y(), max_y))
        fx = (nx - min_x) / (max_x - min_x) if (max_x > min_x) else 0.0
        fy = (ny - min_y) / (max_y - min_y) if (max_y > min_y) else 0.0
        self._overlay_pos[key] = (fx, fy)
        self._cache_key = None
        self.update()

    def mousePressEvent(self, e):  # noqa: N802 - Qt
        pos = e.position()
        if e.button() == Qt.MouseButton.LeftButton and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            chip = self._hit_chip(pos)
            if chip is not None:
                self._chip_hover = None
                chip[1]()
                e.accept()
                return
            ov = self._hit_overlay(pos)
            if ov is not None:
                key, box = ov
                pending_more = (
                    key == "legend"
                    and self._legend_more_hit is not None
                    and self._legend_more_hit.contains(pos)
                )
                pending_leg = None if (key != "legend" or pending_more) else self._hit_legend(pos)
                self._overlay_press = (key, pos, pos - box.topLeft(), pending_leg, pending_more)
                e.accept()
                return
            hit = self._hit_marker(pos)
            if hit is not None:
                self._dragging_marker = hit
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                e.accept()
                return
            if self._plot_rect.contains(pos):
                self.setFocus()
                hit_si = self._hit_series(pos)
                if hit_si is not None:
                    self.set_active_series(hit_si)
                elif self.active_series_index is not None:
                    self.set_active_series(None)
                e.accept()
                return
        if (e.button() == Qt.MouseButton.MiddleButton
                or (e.button() == Qt.MouseButton.LeftButton and e.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            if self._plot_rect.contains(pos):
                active_si = self._active_series_idx(self.series)
                self._pan_origin = (pos, self._cur_x_range, self._cur_y_range, list(self._cur_y_ranges), active_si)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        pos = e.position()
        if self._dragging_overlay is not None:
            key, grab_off, box = self._dragging_overlay
            self._mouse = None
            self._legend_hover = None
            self._legend_more_hover = False
            self._move_overlay_to(key, pos - grab_off, box)
            e.accept()
            return
        if self._overlay_press is not None:
            key, start_pos, grab_off, _leg, _more = self._overlay_press
            if (e.buttons() & Qt.MouseButton.LeftButton):
                if math.hypot(pos.x() - start_pos.x(), pos.y() - start_pos.y()) >= max(4.0, float(QApplication.startDragDistance()) * 0.6):
                    box = self._overlay_box_for(key)
                    self._overlay_press = None
                    self._dragging_overlay = (key, grab_off, QRectF(box))
                    self._mouse = None
                    self._legend_hover = None
                    self._legend_more_hover = False
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self._move_overlay_to(key, pos - grab_off, box)
                e.accept()
                return
            self._overlay_press = None
        if self._dragging_marker is not None:
            r = self._plot_rect
            if r.width() > 1:
                x0, x1 = self._cur_x_range
                clamped_x = max(r.left(), min(pos.x(), r.right()))
                xv = x0 + (clamped_x - r.left()) / r.width() * (x1 - x0)
                self._dragging_marker.x = xv
                self._dragging_marker.track = None
                self._mouse = pos if r.contains(pos) else None
                self._bump()
                self.view_changed.emit()
            e.accept()
            return
        if self._pan_origin is not None:
            start_pos, (x0, x1), (y0, y1), orig_y_ranges, pan_si = self._pan_origin
            r = self._plot_rect
            if r.width() > 1 and r.height() > 1:
                dx = (pos.x() - start_pos.x()) / r.width() * (x1 - x0)
                dy_frac = (pos.y() - start_pos.y()) / r.height()
                self.manual_x_range = (x0 - dx, x1 - dx)
                if pan_si is not None and 0 <= pan_si < len(orig_y_ranges):
                    sy0, sy1 = orig_y_ranges[pan_si]
                    dy = dy_frac * (sy1 - sy0)
                    self.series_y_ranges[pan_si] = (sy0 + dy, sy1 + dy)
                elif len(set(self._cur_y_units)) <= 1 and not self.series_y_ranges:
                    dy = dy_frac * (y1 - y0)
                    self.manual_y_range = (y0 + dy, y1 + dy)
                else:
                    for idx, (sy0, sy1) in enumerate(orig_y_ranges):
                        dy = dy_frac * (sy1 - sy0)
                        self.series_y_ranges[idx] = (sy0 + dy, sy1 + dy)
                self._bump()
            e.accept()
            return
        ov = self._hit_overlay(pos)
        self._mouse = pos if (self._plot_rect.contains(pos) and ov is None) else None
        chip = self._hit_chip(pos)
        self._chip_hover = chip[0] if chip is not None else None
        in_leg = ov is not None and ov[0] == "legend"
        on_more = in_leg and (chip is None) and self._legend_more_hit is not None and self._legend_more_hit.contains(pos)
        self._legend_more_hover = on_more
        leg_hover = None if (not in_leg or chip is not None or on_more) else self._hit_legend(pos)
        self._legend_hover = leg_hover
        if chip is not None or on_more or leg_hover is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif ov is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif self._hit_marker(pos) is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        if self._dragging_overlay is not None:
            self._dragging_overlay = None
            ov = self._hit_overlay(e.position())
            self.setCursor(Qt.CursorShape.OpenHandCursor if ov is not None else Qt.CursorShape.CrossCursor)
            e.accept()
            return
        if self._overlay_press is not None:
            _key, _start, _grab, pending_leg, pending_more = self._overlay_press
            self._overlay_press = None
            if pending_more:
                self._open_legend_menu()
            elif pending_leg is not None:
                self.set_active_series(None if self.active_series_index == pending_leg else pending_leg)
            e.accept()
            return
        if self._dragging_marker is not None:
            self._dragging_marker = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.view_changed.emit()
            e.accept()
            return
        if self._pan_origin is not None:
            self._pan_origin = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.view_changed.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self._chip_hover = None
        self._legend_more_hover = False
        self._legend_hover = None
        self._legend_wheel_acc = 0.0
        self._overlay_press = None
        self.update()
        super().leaveEvent(e)

    def wheelEvent(self, e):  # noqa: N802 - Qt
        pos = e.position()
        if self._chips_max_scroll > 0.0 and self._chips_strip_rect.isValid() and pos.y() >= self._chips_strip_rect.top():
            pd = e.pixelDelta()
            ad = e.angleDelta()
            if pd.x() or pd.y():
                delta = -(pd.x() if abs(pd.x()) >= abs(pd.y()) else pd.y())
            else:
                raw = ad.x() if abs(ad.x()) >= abs(ad.y()) else ad.y()
                delta = -(raw / 120.0) * 60.0
            if delta:
                nxt = max(0.0, min(self._chips_scroll_x + delta, self._chips_max_scroll))
                if abs(nxt - self._chips_scroll_x) > 0.1:
                    self._chips_scroll_x = nxt
                    self._cache_key = None
                    self._ensure_cache()
                    chip = self._hit_chip(pos)
                    self._chip_hover = chip[0] if chip is not None else None
                    self.update()
                e.accept()
                return
        if self._legend_box.isValid() and self._legend_box.contains(pos):
            pd = e.pixelDelta()
            ad = e.angleDelta()
            if pd.y():
                self._legend_wheel_acc += float(-pd.y())
                thresh = 28.0
            else:
                self._legend_wheel_acc += float(-ad.y())
                thresh = 120.0
            steps = int(self._legend_wheel_acc / thresh)
            if steps:
                self._legend_wheel_acc -= steps * thresh
                for _ in range(abs(steps)):
                    self._step_active_series(1 if steps > 0 else -1)
            e.accept()
            return
        mods = e.modifiers()
        ctrl_zoom = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        if not self.hasFocus() and not ctrl_zoom:
            e.ignore()
            return
        shift_held = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        dy = e.angleDelta().y() or (e.angleDelta().x() if shift_held else 0)
        if not dy or not self._plot_rect.contains(pos):
            e.ignore()
            return
        data_pt = self._screen_to_data(pos)
        if data_pt is None:
            e.ignore()
            return
        factor = 0.85 if dy > 0 else 1.0 / 0.85
        cx, cy = data_pt
        if shift_held:
            x0, x1 = self._cur_x_range
            self.manual_x_range = (cx - (cx - x0) * factor, cx + (x1 - cx) * factor)
        else:
            active_si = self._active_series_idx(self.series)
            if active_si is not None and active_si < len(self._cur_y_ranges):
                sy0, sy1 = self._cur_y_ranges[active_si]
                scy = sy0 + (self._plot_rect.bottom() - pos.y()) / self._plot_rect.height() * (sy1 - sy0)
                self.series_y_ranges[active_si] = (scy - (scy - sy0) * factor, scy + (sy1 - scy) * factor)
            elif len(set(self._cur_y_units)) <= 1 and not self.series_y_ranges:
                y0, y1 = self._cur_y_range
                self.manual_y_range = (cy - (cy - y0) * factor, cy + (y1 - cy) * factor)
            else:
                frac_y = (self._plot_rect.bottom() - pos.y()) / self._plot_rect.height()
                for idx, (sy0, sy1) in enumerate(self._cur_y_ranges):
                    scy = sy0 + frac_y * (sy1 - sy0)
                    self.series_y_ranges[idx] = (scy - (scy - sy0) * factor, scy + (sy1 - scy) * factor)
        self._bump()
        self.view_changed.emit()
        e.accept()

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() in (Qt.Key.Key_R, Qt.Key.Key_0):
            self.reset_view()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):  # noqa: N802 - Qt
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        self.update()


class PlotSettingsWindow(Window):
    """Отдельное плавающее окно-инспектор настроек графика (в стиле ``InstrumentsWindow``):
    ``Scale``, ``Display``, ``Markers``, ``Memory / Math``, ``Limits``, ``Analysis``."""

    closed = pyqtSignal()

    _MATH_HINT_TEXT = (
        "Vars: x, y, mem (m) · Const: pi, e\n"
        "Funcs: abs, sqrt, log10, log, exp, sin, cos, db, db20, lin\n"
        "Ex: y - mem  ·  2*y + 3  ·  db(lin(y) - lin(mem))"
    )

    STYLE_SHEET_EXTRA = (
        render_qss("""
            QScrollArea#plotSetScroll, QWidget#plotSetHost {
                background-color: $UI_COLOR_BACKGROUND;
                border: none;
            }
            QWidget#plotSecBody {
                background: transparent;
                border: none;
            }
            QLabel#rowLbl {
                color: $UI_COLOR_TEXT_MUTED;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                background: transparent;
                min-width: 62px;
            }
            QLabel#inlineLbl {
                color: $UI_COLOR_TEXT_MUTED;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                background: transparent;
                min-width: 0px;
            }
            QLabel#valLbl {
                color: $UI_COLOR_TEXT;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                background: transparent;
            }
            QLabel#markerSub {
                color: $UI_COLOR_TEXT_MUTED;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: 9pt;
                background: transparent;
                padding-left: 28px;
            }
            QLabel#mathHint {
                color: $UI_COLOR_TEXT_MUTED;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: 9pt;
                background-color: $UI_COLOR_PANEL;
                border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
                border-radius: ${UI_BORDER_RADIUS_PX}px;
                padding: 5px 7px;
            }
            QLabel#mathHint[error="true"] {
                color: $UI_COLOR_ERROR;
                border-color: $UI_COLOR_ERROR;
            }
            QLabel#secStub {
                color: $UI_COLOR_BORDER_LIGHT;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                background: transparent;
                padding: 4px 2px;
            }
        """)
        + "\n" + button_qss("QWidget#plotSetHost")
        + "\n" + input_qss("QWidget#plotSetHost")
        + "\n" + scrollbar_qss("QScrollArea#plotSetScroll ")
        + "\n" + render_qss("""
            QWidget#plotSetHost QPushButton[compact="true"] {
                padding: 0 4px;
            }
            QWidget#plotSetHost QPushButton[square="true"] {
                min-width: ${UI_COMPACT_HEIGHT_PX}px; max-width: ${UI_COMPACT_HEIGHT_PX}px;
                min-height: ${UI_COMPACT_HEIGHT_PX}px; max-height: ${UI_COMPACT_HEIGHT_PX}px;
                padding: 0;
            }
            QWidget#plotSetHost PanelCloseButton {
                min-width: ${UI_COMPACT_HEIGHT_PX}px; max-width: ${UI_COMPACT_HEIGHT_PX}px;
                min-height: ${UI_COMPACT_HEIGHT_PX}px; max-height: ${UI_COMPACT_HEIGHT_PX}px;
                background-color: $UI_COLOR_BACKGROUND;
                border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
                border-radius: ${UI_BORDER_RADIUS_PX}px;
                padding: 0;
            }
            QWidget#plotSetHost PanelCloseButton:hover {
                background-color: $UI_COLOR_INVERT_HOVER;
                border-color: $UI_COLOR_INVERT_HOVER;
            }
        """)
    )

    _MENU_QSS = render_qss("""
        QMenu {
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 3px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
        }
        QMenu::item {
            padding: 4px 12px;
            border-radius: 4px;
        }
        QMenu::item:selected {
            background-color: $UI_COLOR_INVERT;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        QMenu::separator {
            height: 1px;
            background: $UI_COLOR_BORDER;
            margin: 3px 6px;
        }
    """)

    def __init__(self, settings: QSettings | None = None, parent=None):
        self._settings = settings or QSettings()
        super().__init__(self._settings, parent)
        self._plot: PlotWidget | None = None
        self._plot_name: str = ""
        self._closing_for_shutdown = False
        self._marker_sig: tuple | None = None
        self._limit_sig: tuple | None = None
        self._trace_sig: tuple | None = None
        self._size_key = "WINDOW_SIZE/PlotSettingsWindow"
        self._pos_key = "WINDOW_POS/PlotSettingsWindow"
        self._heights_key = "WINDOW_HEIGHTS/PlotSettingsWindow"

        self.setWindowTitle("PLOT SETTINGS")
        self.titleBar.minimized_button.hide()
        self.titleBar.title_label.setStyleSheet(render_qss("""
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT;
            padding: 0 6px;
        """))

        self.expand_all_button = ExpandAllButton(self.titleBar)
        self.expand_all_button.setToolTip("Expand all sections")
        self.expand_all_button.clicked.connect(self.expand_all)
        self.collapse_all_button = CollapseAllButton(self.titleBar)
        self.collapse_all_button.setToolTip("Collapse all sections")
        self.collapse_all_button.clicked.connect(self.collapse_all)
        self.titleBar.insert_control_widget(self.expand_all_button)
        self.titleBar.insert_control_widget(self.collapse_all_button)

        host = QWidget()
        host.setObjectName("plotSetHost")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(8)

        scroll = QScrollArea(self)
        scroll.setObjectName("plotSetScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(host)
        scroll.setStyleSheet(self.STYLE_SHEET_EXTRA)

        self.sections: dict[str, CollapsibleSection] = {}

        # ---------------- Scale ----------------
        scale_body = self._sec_body()
        sl = QVBoxLayout(scale_body)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)

        self.y_auto_btn = self._toggle_btn("AUTO", self._on_y_auto)
        self.y_min_edit = CommitLineEdit("Y axis minimum", self._on_y_edit)
        self.y_max_edit = CommitLineEdit("Y axis maximum", self._on_y_edit)
        y_row = self._row("Y Axis", self.y_min_edit, self.y_max_edit, self.y_auto_btn)
        self._y_axis_lbl: QLabel = y_row.itemAt(0).widget()
        sl.addLayout(y_row)

        self.x_auto_btn = self._toggle_btn("AUTO", self._on_x_auto)
        self.x_min_edit = CommitLineEdit("X axis minimum (supports SI: 1G, 500M)", self._on_x_edit)
        self.x_max_edit = CommitLineEdit("X axis maximum (supports SI: 6G, 2.4G)", self._on_x_edit)
        sl.addLayout(self._row("X Axis", self.x_min_edit, self.x_max_edit, self.x_auto_btn))

        self.reset_btn = self._action_btn("Reset View (R)", self._on_reset_view)
        sl.addWidget(self.reset_btn)

        sec_scale = CollapsibleSection("Scale", scale_body, expanded=True, settings=self._settings,
                                       settings_key="PLOT_SETTINGS/SECTIONS/SCALE", parent=host)
        self.sections["SCALE"] = sec_scale
        hl.addWidget(sec_scale)

        # ---------------- Display ----------------
        disp_body = self._sec_body()
        dl = QVBoxLayout(disp_body)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)

        self.grid_btn = self._toggle_btn("Grid", self._on_grid_toggled)
        self.grid_btn.setProperty("compact", True)
        self.points_btn = self._toggle_btn("Points", self._on_points_toggled)
        self.points_btn.setProperty("compact", True)
        self.values_btn = self._toggle_btn("Values", self._on_values_toggled)
        self.values_btn.setProperty("compact", True)
        self.values_btn.setToolTip("Show point Y values on curves with automatic anti-overlap spacing")
        self.labels_btn = self._toggle_btn("Labels", self._on_labels_toggled)
        self.labels_btn.setProperty("compact", True)
        self.labels_btn.setToolTip("Show or hide X and Y axis tick labels")
        dl.addLayout(self._row("Show", self.grid_btn, self.points_btn, self.values_btn, self.labels_btn))

        self._trace_host = self._sec_body()
        self._trace_lay = QGridLayout(self._trace_host)
        self._trace_lay.setContentsMargins(0, 0, 0, 0)
        self._trace_lay.setSpacing(4)
        self._trace_buttons: dict[int | None, QPushButton] = {}
        dl.addLayout(self._row("Cursor", self._trace_host))

        self.div_buttons: dict[int, QPushButton] = {}
        div_widgets: list[QWidget] = []
        for n in (4, 6, 8, 10):
            b = self._toggle_btn(str(n), lambda _c=False, divs=n: self._on_divs_y(divs))
            self.div_buttons[n] = b
            div_widgets.append(b)
        dl.addLayout(self._row("Y Divs", *div_widgets))

        sec_disp = CollapsibleSection("Display", disp_body, expanded=True, settings=self._settings,
                                      settings_key="PLOT_SETTINGS/SECTIONS/DISPLAY", parent=host)
        self.sections["DISPLAY"] = sec_disp
        hl.addWidget(sec_disp)

        # ---------------- Markers ----------------
        markers_body = self._sec_body()
        ml = QVBoxLayout(markers_body)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)

        m_act = QHBoxLayout()
        m_act.setContentsMargins(0, 0, 0, 0)
        m_act.setSpacing(4)
        self.marker_add_btn = self._action_btn("+ Add", self._on_marker_add)
        self.marker_max_btn = self._action_btn("MAX", lambda: self._on_marker_ext(True))
        self.marker_max_btn.setToolTip("Add MAX tracking marker")
        self.marker_min_btn = self._action_btn("MIN", lambda: self._on_marker_ext(False))
        self.marker_min_btn.setToolTip("Add MIN tracking marker")
        self.marker_peaks_btn = self._toggle_btn("Peaks", self._on_marker_peaks)
        self.marker_peaks_btn.setToolTip("Live auto-peak overlay: shows all significant peaks (x, y) without adding markers")
        self.marker_clear_btn = self._action_btn("Clear", self._on_marker_clear)
        for b in (self.marker_add_btn, self.marker_max_btn, self.marker_min_btn,
                  self.marker_peaks_btn, self.marker_clear_btn):
            b.setProperty("compact", True)
            m_act.addWidget(b, 1)
        ml.addLayout(m_act)

        self._markers_list_host = self._sec_body()
        self._markers_list_lay = QVBoxLayout(self._markers_list_host)
        self._markers_list_lay.setContentsMargins(0, 0, 0, 0)
        self._markers_list_lay.setSpacing(4)
        ml.addWidget(self._markers_list_host)

        sec_markers = CollapsibleSection("Markers", markers_body, expanded=True, settings=self._settings,
                                         settings_key="PLOT_SETTINGS/SECTIONS/MARKERS", parent=host)
        self.sections["MARKERS"] = sec_markers
        hl.addWidget(sec_markers)

        # ---------------- Memory / Math ----------------
        mem_body = self._sec_body()
        meml = QVBoxLayout(mem_body)
        meml.setContentsMargins(0, 0, 0, 0)
        meml.setSpacing(6)

        mem_btns = QHBoxLayout()
        mem_btns.setContentsMargins(0, 0, 0, 0)
        mem_btns.setSpacing(4)
        self.mem_store_btn = self._action_btn("DATA → MEM", self._on_mem_store)
        self.mem_store_btn.setToolTip("Store current trace in memory (same as double-click on plot)")
        self.mem_show_btn = self._toggle_btn("MEM", self._on_mem_visible)
        self.mem_clear_btn = self._action_btn("Clear", self._on_mem_clear)
        mem_btns.addWidget(self.mem_store_btn, 2)
        mem_btns.addWidget(self.mem_show_btn, 1)
        mem_btns.addWidget(self.mem_clear_btn, 1)
        meml.addLayout(mem_btns)

        self.math_buttons: dict[str, QPushButton] = {}
        math_grid = QGridLayout()
        math_grid.setContentsMargins(0, 0, 0, 0)
        math_grid.setSpacing(4)
        for idx, mode in enumerate(MATH_MODES):
            b = self._toggle_btn(MATH_LABELS[mode], lambda _c=False, m=mode: self._on_math_mode(m))
            if mode == "EXPR":
                b.setToolTip("Custom formula — " + self._MATH_HINT_TEXT.replace("\n", " | "))
            self.math_buttons[mode] = b
            math_grid.addWidget(b, idx // 3, idx % 3)
        meml.addLayout(math_grid)

        self.math_expr_edit = CommitLineEdit("Math expression with x, y, mem (e.g. y - mem, 2*y)",
                                             self._on_math_expr, placeholder="y - mem",
                                             align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.math_expr_edit.hide()
        meml.addWidget(self.math_expr_edit)

        self.math_hint_lbl = QLabel(self._MATH_HINT_TEXT)
        self.math_hint_lbl.setObjectName("mathHint")
        self.math_hint_lbl.setWordWrap(True)
        self.math_hint_lbl.hide()
        meml.addWidget(self.math_hint_lbl)

        sec_mem = CollapsibleSection("Memory / Math", mem_body, expanded=True, settings=self._settings,
                                     settings_key="PLOT_SETTINGS/SECTIONS/MEMORY", parent=host)
        self.sections["MEMORY"] = sec_mem
        hl.addWidget(sec_mem)

        # ---------------- Limits ----------------
        lim_body = self._sec_body()
        liml = QVBoxLayout(lim_body)
        liml.setContentsMargins(0, 0, 0, 0)
        liml.setSpacing(6)

        lim_grid = QGridLayout()
        lim_grid.setContentsMargins(0, 0, 0, 0)
        lim_grid.setHorizontalSpacing(4)
        lim_grid.setVerticalSpacing(4)
        self.lim_kind_btn = self._action_btn("MAX", self._on_toggle_new_lim_kind)
        self.lim_kind_btn.setProperty("compact", True)
        self.lim_kind_btn.setFixedWidth(54)
        self.lim_kind_btn.setToolTip("Limit type: MAX (upper limit) or MIN (lower limit)")
        self.lim_x1_edit = CommitLineEdit("Start X (optional, e.g. 2.5G; blank = full span)",
                                          self._on_limit_add, placeholder="X1 (opt)")
        self.lim_y1_edit = CommitLineEdit("Start Y limit (e.g. -3)",
                                          self._on_limit_add, placeholder="Y1")
        self.lim_add_btn = self._action_btn("+ Add", self._on_limit_add)
        self.lim_add_btn.setProperty("compact", True)
        self.lim_add_btn.setFixedWidth(56)

        lim_to_lbl = QLabel("to")
        lim_to_lbl.setObjectName("inlineLbl")
        lim_to_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lim_to_lbl.setFixedWidth(54)
        self.lim_x2_edit = CommitLineEdit("End X (optional, e.g. 4G; blank = full span)",
                                          self._on_limit_add, placeholder="X2 (opt)")
        self.lim_y2_edit = CommitLineEdit("End Y limit (optional; blank = same as Y1)",
                                          self._on_limit_add, placeholder="Y2 (=Y1)")
        self.lim_clear_btn = self._action_btn("Clear", self._on_limit_clear)
        self.lim_clear_btn.setProperty("compact", True)
        self.lim_clear_btn.setFixedWidth(56)

        lim_grid.addWidget(self.lim_kind_btn, 0, 0)
        lim_grid.addWidget(self.lim_x1_edit, 0, 1)
        lim_grid.addWidget(self.lim_y1_edit, 0, 2)
        lim_grid.addWidget(self.lim_add_btn, 0, 3)
        lim_grid.addWidget(lim_to_lbl, 1, 0)
        lim_grid.addWidget(self.lim_x2_edit, 1, 1)
        lim_grid.addWidget(self.lim_y2_edit, 1, 2)
        lim_grid.addWidget(self.lim_clear_btn, 1, 3)
        lim_grid.setColumnStretch(1, 1)
        lim_grid.setColumnStretch(2, 1)
        liml.addLayout(lim_grid)

        self._limits_list_host = self._sec_body()
        self._limits_list_lay = QVBoxLayout(self._limits_list_host)
        self._limits_list_lay.setContentsMargins(0, 0, 0, 0)
        self._limits_list_lay.setSpacing(4)
        liml.addWidget(self._limits_list_host)

        sec_lim = CollapsibleSection("Limits", lim_body, expanded=False, settings=self._settings,
                                     settings_key="PLOT_SETTINGS/SECTIONS/LIMITS", parent=host)
        self.sections["LIMITS"] = sec_lim
        hl.addWidget(sec_lim)

        # ---------------- Analysis ----------------
        an_body = self._sec_body()
        an_root = QVBoxLayout(an_body)
        an_root.setContentsMargins(0, 0, 0, 0)
        an_root.setSpacing(6)

        self.analysis_x_edit = CommitLineEdit(
            "Analysis X range: [x1,x2]+p-[a,b] (e.g. [2.5G,3.9G], [1G,6G]-[2.8G,3.4G]; blank = visible)",
            self._on_analysis_x_edit,
            placeholder="[x,y]+5-[a,b]",
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        an_root.addLayout(self._row("X Range", self.analysis_x_edit))

        an_grid = QGridLayout()
        an_grid.setContentsMargins(0, 0, 0, 0)
        an_grid.setSpacing(4)
        self.analysis_buttons: dict[str, QPushButton] = {}
        for idx, (key, lbl, tip) in enumerate(ANALYSIS_METRICS):
            b = self._toggle_btn(lbl, lambda checked, k=key: self._on_analysis_toggled(k, checked))
            b.setToolTip(tip)
            self.analysis_buttons[key] = b
            an_grid.addWidget(b, idx // 2, idx % 2)
        an_root.addLayout(an_grid)

        sec_an = CollapsibleSection("Analysis", an_body, expanded=False, settings=self._settings,
                                    settings_key="PLOT_SETTINGS/SECTIONS/ANALYSIS", parent=host)
        self.sections["ANALYSIS"] = sec_an
        hl.addWidget(sec_an)

        hl.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        root.setSpacing(0)
        root.addWidget(scroll, 1)

        self.setMinimumWidth(360)
        self.setMinimumHeight(340)
        self.recall_size(QSize(400, 620))

    @staticmethod
    def _sec_body() -> QWidget:
        w = QWidget()
        w.setObjectName("plotSecBody")
        return w

    @staticmethod
    def _row(label: str, *widgets: QWidget) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lbl = QLabel(label)
        lbl.setObjectName("rowLbl")
        lay.addWidget(lbl)
        for w in widgets:
            if isinstance(w, QLabel):
                w.setObjectName("rowLbl")
                w.setMinimumWidth(0)
                lay.addWidget(w)
            else:
                lay.addWidget(w, 1)
        return lay

    @staticmethod
    def _toggle_btn(text: str, handler) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(handler)
        return b

    @staticmethod
    def _action_btn(text: str, handler) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(handler)
        return b

    @property
    def current_plot(self) -> PlotWidget | None:
        return self._plot

    @property
    def current_name(self) -> str:
        return self._plot_name

    def bind_plot(self, name: str, plot: PlotWidget) -> None:
        if self._plot is not None and not sip.isdeleted(self._plot):
            try:
                self._plot.view_changed.disconnect(self.sync_from_plot)
            except (TypeError, RuntimeError):
                pass
        self._plot = plot
        self._plot_name = name
        self._marker_sig = None
        self._limit_sig = None
        self._trace_sig = None
        self.setWindowTitle(f"{name} · SETTINGS")
        plot.view_changed.connect(self.sync_from_plot)
        self.sync_from_plot(force=True)

    def sync_from_plot(self, *, force: bool = False) -> None:
        plot = self._plot
        if plot is None or sip.isdeleted(self) or sip.isdeleted(plot):
            return
        (x0, x1), (y0, y1) = plot.current_ranges()
        eff, eff_y_unit = plot._effective_series_and_unit()
        y_auto = plot.is_y_auto()
        x_auto = plot.manual_x_range is None
        active_si = plot._active_series_idx(eff)

        # Scale
        if active_si is not None and len(eff) > 1 and eff[active_si].name:
            self._y_axis_lbl.setText(f"Y {eff[active_si].name}")
        else:
            self._y_axis_lbl.setText("Y Axis")
        self.y_auto_btn.setChecked(y_auto)
        self.y_auto_btn.setText("AUTO" if y_auto else "MAN")
        self.y_min_edit.set_committed(_fmt_value(y0, eff_y_unit, include_unit=False), force=force)
        self.y_max_edit.set_committed(_fmt_value(y1, eff_y_unit, include_unit=False), force=force)

        self.x_auto_btn.setChecked(x_auto)
        self.x_auto_btn.setText("AUTO" if x_auto else "MAN")
        self.x_min_edit.set_committed(_fmt_value(x0, plot.x_unit, include_unit=True), force=force)
        self.x_max_edit.set_committed(_fmt_value(x1, plot.x_unit, include_unit=True), force=force)

        # Display
        self.grid_btn.setChecked(plot.show_grid)
        self.points_btn.setChecked(plot.show_points)
        self.values_btn.setChecked(plot.show_point_values)
        self.labels_btn.setChecked(plot.show_axis_labels)
        self._sync_trace_selector(plot, eff)
        for n, b in self.div_buttons.items():
            b.setChecked(n == plot.divs_y)

        # Memory / Math
        has_mem = bool(plot.memory)
        self.mem_show_btn.setEnabled(has_mem)
        self.mem_show_btn.setChecked(has_mem and plot.memory_visible)
        self.mem_clear_btn.setEnabled(has_mem)
        for mode, b in self.math_buttons.items():
            b.setChecked(mode == plot.math_mode)
        is_expr = plot.math_mode == "EXPR"
        self.math_expr_edit.setVisible(is_expr)
        self.math_hint_lbl.setVisible(is_expr)
        self.math_expr_edit.set_committed(plot.math_expr, force=force)
        if not is_expr:
            self._set_math_hint_error(None)
        self.sections["MEMORY"].set_active(has_mem or plot.math_mode != "OFF")

        # Markers
        self.marker_peaks_btn.setChecked(plot.show_peaks)
        self._sync_markers_list(plot, eff, eff_y_unit, force=force)
        self.sections["MARKERS"].set_active(bool(plot.markers) or plot.show_peaks)

        # Limits
        self._sync_limits_list(plot, eff_y_unit)
        self.sections["LIMITS"].set_active(bool(plot.limits))

        # Analysis
        self.analysis_x_edit.set_committed(plot.analysis_x_expr, force=force)
        active_metrics = set(plot.analysis_metrics)
        for k, b in self.analysis_buttons.items():
            b.setChecked(k in active_metrics)
        self.sections["ANALYSIS"].set_active(bool(active_metrics))

    def _sync_trace_selector(self, plot: PlotWidget, eff: Sequence[PlotSeries]) -> None:
        names = tuple((s.name or f"#{i + 1}") for i, s in enumerate(eff))
        if names != self._trace_sig:
            self._trace_sig = names
            while self._trace_lay.count():
                item = self._trace_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()
            self._trace_buttons = {}
            all_btns: list[QPushButton] = []
            free_btn = self._toggle_btn("Free", lambda _c=False: self._on_active_series(None))
            free_btn.setProperty("compact", True)
            free_btn.setToolTip("Free crosshair cursor (click a trace to snap to its points)")
            self._trace_buttons[None] = free_btn
            all_btns.append(free_btn)
            for idx, nm in enumerate(names):
                b = self._toggle_btn(nm, lambda _c=False, i=idx: self._on_active_series(i))
                b.setProperty("compact", True)
                b.setToolTip(f"Snap crosshair to {nm} points (also clickable in plot legend)")
                self._trace_buttons[idx] = b
                all_btns.append(b)
            cols = len(all_btns) if len(all_btns) <= 4 else 3
            for pos, btn in enumerate(all_btns):
                self._trace_lay.addWidget(btn, pos // cols, pos % cols)
            for c_idx in range(cols):
                self._trace_lay.setColumnStretch(c_idx, 1)
        active = plot._active_series_idx(eff)
        for key, b in self._trace_buttons.items():
            b.setChecked(key == active)

    @staticmethod
    def _clear_layout(lay: QVBoxLayout) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _sync_markers_list(self, plot: PlotWidget, eff: Sequence[PlotSeries], _eff_y_unit: str,
                           *, force: bool = False) -> None:
        xr, _ = plot.current_ranges()
        for m in plot.markers:
            plot.resolve_marker(m, eff, xr)

        series_names = tuple((s.name or f"#{i + 1}") for i, s in enumerate(eff))
        sig = (series_names, tuple((m.name, m.series_index, m.delta_ref, m.track) for m in plot.markers))
        if sig != self._marker_sig or force:
            self._marker_sig = sig
            self._clear_layout(self._markers_list_lay)
            self._marker_rows: dict[str, CommitLineEdit] = {}
            if not plot.markers:
                lbl = QLabel("No markers — click + Add, MAX, MIN or Peaks")
                lbl.setObjectName("secStub")
                self._markers_list_lay.addWidget(lbl)
            else:
                fm = QFontMetrics(mono_font())
                name_w = max(24, max(fm.horizontalAdvance(m.name) for m in plot.markers) + 6)
                has_multi_series = len(eff) > 1
                series_w = (max(40, max(fm.horizontalAdvance(n) for n in series_names) + 16)
                            if has_multi_series else 0)
                trk_w = max(40, max(fm.horizontalAdvance(t) for t in ("—", "MAX", "MIN")) + 14)
                can_delta = len(plot.markers) > 1
                for m in plot.markers:
                    row_w = self._sec_body()
                    rl = QHBoxLayout(row_w)
                    rl.setContentsMargins(0, 0, 0, 0)
                    rl.setSpacing(4)

                    name_lbl = QLabel(m.name, row_w)
                    name_lbl.setObjectName("valLbl")
                    name_lbl.setFixedWidth(name_w)
                    rl.addWidget(name_lbl)

                    x_edit = CommitLineEdit("Marker X position (supports SI: 3.2G)",
                                            lambda mn=m.name: self._on_marker_x_edit(mn), parent=row_w)
                    x_edit.set_committed(_fmt_value(m.x, plot.x_unit), force=True)
                    rl.addWidget(x_edit, 1)

                    if has_multi_series:
                        si = max(0, min(m.series_index, len(eff) - 1))
                        s_btn = self._action_btn(series_names[si],
                                                 lambda _c=False, mn=m.name, nxt=si + 1: self._on_marker_series(mn, nxt))
                        s_btn.setProperty("compact", True)
                        s_btn.setFixedWidth(series_w)
                        s_btn.setToolTip("Switch marker trace")
                        rl.addWidget(s_btn)

                    trk_text = m.track if m.track in ("MAX", "MIN") else "—"
                    trk_btn = self._toggle_btn(trk_text, lambda _c=False, mn=m.name: self._on_marker_track_cycle(mn))
                    trk_btn.setProperty("compact", True)
                    trk_btn.setFixedWidth(trk_w)
                    trk_btn.setChecked(m.track in ("MAX", "MIN"))
                    trk_btn.setToolTip("Cycle tracking: Free (—) → MAX → MIN")
                    rl.addWidget(trk_btn)

                    d_label = f"Δ{m.delta_ref[1:]}" if (m.delta_ref and m.delta_ref.startswith("M")) else (
                        "Δ" if not m.delta_ref else "Δ*"
                    )
                    d_btn = QPushButton(d_label, row_w)
                    d_btn.setCheckable(True)
                    d_btn.setChecked(bool(m.delta_ref))
                    d_btn.setEnabled(can_delta)
                    d_btn.setProperty("square", True)
                    d_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    d_btn.setToolTip(
                        f"Delta vs {m.delta_ref} (click to choose reference)" if m.delta_ref
                        else "Delta marker: click to choose reference marker"
                    )
                    d_btn.clicked.connect(lambda _c=False, mn=m.name, btn=d_btn: self._on_marker_delta_menu(mn, btn))
                    rl.addWidget(d_btn)

                    rm_btn = PanelCloseButton(row_w)
                    rm_btn.setToolTip(f"Remove {m.name}")
                    rm_btn.clicked.connect(lambda _c=False, mn=m.name: self._on_marker_remove(mn))
                    rl.addWidget(rm_btn)

                    self._marker_rows[m.name] = x_edit
                    self._markers_list_lay.addWidget(row_w)
        else:
            for m in plot.markers:
                x_edit = getattr(self, "_marker_rows", {}).get(m.name)
                if x_edit is not None and not sip.isdeleted(x_edit):
                    x_edit.set_committed(_fmt_value(m.x, plot.x_unit))
        self.marker_clear_btn.setEnabled(bool(plot.markers))

    def _sync_limits_list(self, plot: PlotWidget, eff_y_unit: str) -> None:
        sig = tuple((lim.kind, lim.y1, lim.y2, lim.x1, lim.x2) for lim in plot.limits)
        if sig == self._limit_sig:
            return
        self._limit_sig = sig
        self._clear_layout(self._limits_list_lay)
        if not plot.limits:
            lbl = QLabel("No limit lines")
            lbl.setObjectName("secStub")
            self._limits_list_lay.addWidget(lbl)
        else:
            for idx, lim in enumerate(plot.limits):
                row_w = self._sec_body()
                rl = QHBoxLayout(row_w)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(4)

                kind_btn = self._action_btn(lim.kind, lambda _c=False, i=idx: self._on_limit_flip(i))
                kind_btn.setProperty("compact", True)
                kind_btn.setFixedWidth(54)
                rl.addWidget(kind_btn)

                y1_txt = _fmt_value(lim.y1, eff_y_unit)
                y2_val = lim.y1 if lim.y2 is None else lim.y2
                y2_txt = _fmt_value(y2_val, eff_y_unit)
                if lim.x1 is None and lim.x2 is None:
                    if lim.y2 is None or lim.y2 == lim.y1:
                        line_txt = f"Full · {y1_txt}"
                    else:
                        line_txt = f"Full · {y1_txt} → {y2_txt}"
                else:
                    x1_txt = _fmt_value(lim.x1, plot.x_unit) if lim.x1 is not None else "start"
                    x2_txt = _fmt_value(lim.x2, plot.x_unit) if lim.x2 is not None else "end"
                    if y2_val == lim.y1:
                        line_txt = f"{x1_txt}…{x2_txt} · {y1_txt}"
                    else:
                        line_txt = f"({x1_txt}, {y1_txt}) → ({x2_txt}, {y2_txt})"
                desc = QLabel(line_txt, row_w)
                desc.setObjectName("valLbl")
                rl.addWidget(desc, 1)

                rm_btn = PanelCloseButton(row_w)
                rm_btn.setToolTip("Remove limit line")
                rm_btn.clicked.connect(lambda _c=False, i=idx: self._on_limit_remove(i))
                rl.addWidget(rm_btn)
                self._limits_list_lay.addWidget(row_w)
        self.lim_clear_btn.setEnabled(bool(plot.limits))

    # ------------------------------------------------------------ обработчики
    def _on_y_auto(self, checked: bool) -> None:
        if self._plot is None:
            return
        if checked:
            self._plot.set_y_range(None, None)
        else:
            _, (y0, y1) = self._plot.current_ranges()
            self._plot.set_y_range(y0, y1)

    def _on_x_auto(self, checked: bool) -> None:
        if self._plot is None:
            return
        if checked:
            self._plot.set_x_range(None, None)
        else:
            (x0, x1), _ = self._plot.current_ranges()
            self._plot.set_x_range(x0, x1)

    def _on_y_edit(self) -> None:
        if self._plot is None:
            return
        yu = self._plot.active_y_unit()
        try:
            lo = _parse_axis_input(self.y_min_edit.text(), yu)
            hi = _parse_axis_input(self.y_max_edit.text(), yu)
            if hi <= lo:
                raise ValueError("max <= min")
        except ValueError:
            self.y_min_edit.set_invalid(True)
            self.y_max_edit.set_invalid(True)
            return
        self.y_min_edit.set_invalid(False)
        self.y_max_edit.set_invalid(False)
        self._plot.set_y_range(lo, hi)

    def _on_x_edit(self) -> None:
        if self._plot is None:
            return
        try:
            lo = _parse_axis_input(self.x_min_edit.text(), self._plot.x_unit)
            hi = _parse_axis_input(self.x_max_edit.text(), self._plot.x_unit)
            if hi <= lo:
                raise ValueError("max <= min")
        except ValueError:
            self.x_min_edit.set_invalid(True)
            self.x_max_edit.set_invalid(True)
            return
        self.x_min_edit.set_invalid(False)
        self.x_max_edit.set_invalid(False)
        self._plot.set_x_range(lo, hi)

    def _on_reset_view(self) -> None:
        if self._plot is not None:
            self._plot.reset_view()

    def _on_grid_toggled(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_show_grid(checked)

    def _on_points_toggled(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_show_points(checked)

    def _on_values_toggled(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_show_point_values(checked)

    def _on_labels_toggled(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_show_axis_labels(checked)

    def _on_active_series(self, index: int | None) -> None:
        if self._plot is not None:
            self._plot.set_active_series(index)

    def _on_divs_y(self, divs: int) -> None:
        if self._plot is not None:
            self._plot.set_divs_y(divs)

    def _on_marker_add(self) -> None:
        if self._plot is not None:
            self._plot.add_marker()

    def _on_marker_ext(self, maximum: bool) -> None:
        if self._plot is not None:
            self._plot.add_extremum_marker(maximum=maximum)

    def _on_marker_peaks(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_show_peaks(checked)

    def _on_marker_clear(self) -> None:
        if self._plot is not None:
            self._plot.clear_markers()

    def _on_marker_x_edit(self, name: str) -> None:
        if self._plot is None:
            return
        x_edit = getattr(self, "_marker_rows", {}).get(name)
        if x_edit is None:
            return
        try:
            val = _parse_axis_input(x_edit.text(), self._plot.x_unit)
        except ValueError:
            x_edit.set_invalid(True)
            return
        x_edit.set_invalid(False)
        self._plot.update_marker(name, x=val)

    def _on_marker_series(self, name: str, nxt: int) -> None:
        if self._plot is not None:
            self._plot.update_marker(name, series_index=nxt)

    def _on_marker_track_cycle(self, name: str) -> None:
        if self._plot is None:
            return
        cur = next((m.track for m in self._plot.markers if m.name == name), None)
        nxt = "MAX" if cur is None else ("MIN" if cur == "MAX" else None)
        self._plot.update_marker(name, track=nxt)

    def _on_marker_delta(self, name: str, ref: str | None) -> None:
        if self._plot is not None:
            self._plot.update_marker(name, delta_ref=ref)

    def _on_marker_delta_menu(self, name: str, btn: QPushButton) -> None:
        if self._plot is None:
            return
        cur_m = next((m for m in self._plot.markers if m.name == name), None)
        if cur_m is None:
            return
        others = [m.name for m in self._plot.markers if m.name != name]
        if not others:
            btn.setChecked(False)
            return
        menu = QMenu(self)
        menu.setStyleSheet(_POPUP_MENU_QSS)
        if cur_m.delta_ref is not None:
            off_act = menu.addAction("Off (absolute)")
            off_act.triggered.connect(lambda _c=False, mn=name: self._on_marker_delta(mn, None))
            menu.addSeparator()
        for ref_name in others:
            prefix = "✓ " if cur_m.delta_ref == ref_name else "   "
            act = menu.addAction(f"{prefix}Δ vs {ref_name}")
            act.triggered.connect(lambda _c=False, mn=name, r=ref_name: self._on_marker_delta(mn, r))
        btn.setChecked(bool(cur_m.delta_ref))
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if not sip.isdeleted(btn):
            latest = next((m for m in self._plot.markers if m.name == name), None)
            btn.setChecked(bool(latest and latest.delta_ref))

    def _on_marker_remove(self, name: str) -> None:
        if self._plot is not None:
            self._plot.remove_marker(name)

    def _on_mem_store(self) -> None:
        if self._plot is not None:
            self._plot.store_memory()

    def _on_mem_visible(self, checked: bool) -> None:
        if self._plot is not None:
            self._plot.set_memory_visible(checked)

    def _on_mem_clear(self) -> None:
        if self._plot is not None:
            self._plot.clear_memory()

    def _on_math_mode(self, mode: str) -> None:
        if self._plot is not None:
            self._plot.set_math_mode(mode)

    def _set_math_hint_error(self, err: str | None) -> None:
        lbl = self.math_hint_lbl
        if err:
            lbl.setText(f"Error: {err}\n{self._MATH_HINT_TEXT}")
            lbl.setProperty("error", True)
        else:
            lbl.setText(self._MATH_HINT_TEXT)
            lbl.setProperty("error", False)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)
        lbl.update()

    def _on_math_expr(self) -> None:
        if self._plot is None:
            return
        expr = self.math_expr_edit.text().strip()
        if expr:
            ok, err = validate_math_expr(expr)
            if not ok:
                self.math_expr_edit.set_invalid(True)
                self._set_math_hint_error(err)
                return
        self.math_expr_edit.set_invalid(False)
        self._set_math_hint_error(None)
        self._plot.set_math_expr(expr)

    def _on_toggle_new_lim_kind(self) -> None:
        self.lim_kind_btn.setText("MIN" if self.lim_kind_btn.text() == "MAX" else "MAX")

    def _on_limit_add(self) -> None:
        if self._plot is None:
            return
        for ed in (self.lim_x1_edit, self.lim_y1_edit, self.lim_x2_edit, self.lim_y2_edit):
            ed.set_invalid(False)
        yu = self._plot.active_y_unit()
        try:
            y1 = _parse_axis_input(self.lim_y1_edit.text(), yu)
        except ValueError:
            self.lim_y1_edit.set_invalid(True)
            return
        y2_txt = self.lim_y2_edit.text().strip()
        y2: float | None = None
        if y2_txt:
            try:
                y2 = _parse_axis_input(y2_txt, yu)
            except ValueError:
                self.lim_y2_edit.set_invalid(True)
                return
        x1_txt = self.lim_x1_edit.text().strip()
        x1: float | None = None
        if x1_txt:
            try:
                x1 = _parse_axis_input(x1_txt, self._plot.x_unit)
            except ValueError:
                self.lim_x1_edit.set_invalid(True)
                return
        x2_txt = self.lim_x2_edit.text().strip()
        x2: float | None = None
        if x2_txt:
            try:
                x2 = _parse_axis_input(x2_txt, self._plot.x_unit)
            except ValueError:
                self.lim_x2_edit.set_invalid(True)
                return
        self._plot.add_limit(self.lim_kind_btn.text(), y1, y2=y2, x1=x1, x2=x2)
        for ed in (self.lim_x1_edit, self.lim_y1_edit, self.lim_x2_edit, self.lim_y2_edit):
            ed.set_committed("", force=True)

    def _on_limit_flip(self, index: int) -> None:
        if self._plot is None or not (0 <= index < len(self._plot.limits)):
            return
        lim = self._plot.limits[index]
        lim.kind = "MIN" if lim.kind == "MAX" else "MAX"
        self._plot._bump()
        self._plot.view_changed.emit()

    def _on_limit_remove(self, index: int) -> None:
        if self._plot is not None:
            self._plot.remove_limit(index)

    def _on_limit_clear(self) -> None:
        if self._plot is not None:
            self._plot.clear_limits()

    def _on_analysis_toggled(self, key: str, checked: bool) -> None:
        if self._plot is not None:
            self._plot.toggle_analysis_metric(key, checked)

    def _on_analysis_x_edit(self) -> None:
        if self._plot is None:
            return
        ok, _err = self._plot.set_analysis_x_expr(self.analysis_x_edit.text())
        self.analysis_x_edit.set_invalid(not ok)

    def expand_all(self) -> None:
        for sec in self.sections.values():
            sec.set_expanded(True)

    def collapse_all(self) -> None:
        for sec in self.sections.values():
            sec.set_expanded(False)

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            e.accept()
            return
        super().keyPressEvent(e)

    def closeEvent(self, event):  # noqa: N802 - Qt
        if not self._closing_for_shutdown:
            self.save_size()
            self.hide()
            self.closed.emit()
            event.accept()
            return
        super().closeEvent(event)
