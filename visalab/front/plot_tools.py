"""Инструменты графиков: анализ трасс (среднее, мин/макс, СКО, P-P, flatness, нелинейность, наклон,
RMS, полоса −3 дБ), разность DATA − MEM и всплывающая панель настроек шкалы, памяти и анализа.
"""
from __future__ import annotations

from bisect import bisect_left
import math
from typing import Any

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout, QWidget)

from ..core.trace import Trace
from ..core.units import si_format, si_parse
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL,
                    UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT,
                    UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, render_qss)

__all__ = [
    "ANALYSIS_METRICS",
    "ANALYSIS_KEYS",
    "analyze_trace",
    "format_analysis",
    "trace_diff",
    "PlotSettingsPopup",
]

#: (ключ, короткая подпись, подсказка)
ANALYSIS_METRICS: tuple[tuple[str, str, str], ...] = (
    ("mean", "MEAN", "Arithmetic mean of displayed trace values"),
    ("min", "MIN", "Minimum value and its X coordinate"),
    ("max", "MAX", "Maximum value and its X coordinate"),
    ("std", "STD", "Standard deviation (RMS spread around the mean)"),
    ("pp", "P-P", "Peak-to-peak span (Max − Min)"),
    ("flatness", "FLAT", "Flatness: ±(Max − Min) / 2"),
    ("nonlin", "NONLIN", "Nonlinearity: max deviation from the linear best-fit line"),
    ("slope", "SLOPE", "Linear regression slope (dY / dX)"),
    ("rms", "RMS", "Root mean square value"),
    ("bw3", "BW-3dB", "-3 dB bandwidth around the peak"),
)
ANALYSIS_KEYS: tuple[str, ...] = tuple(k for k, _lbl, _tip in ANALYSIS_METRICS)
_SI_UNITS = ("W", "s", "Hz", "V", "A")


def _fmt_y(value: float, unit: str, *, diff: bool = False) -> str:
    if not math.isfinite(value):
        return "---"
    u = "dB" if (diff and unit == "dBm") else unit
    if u in _SI_UNITS:
        return si_format(value, u, 4)
    if u:
        return f"{value:.2f} {u}"
    return f"{value:.3g}"


def _fmt_x(value: float, unit: str) -> str:
    if not math.isfinite(value):
        return "---"
    if unit in _SI_UNITS:
        return si_format(value, unit, 4)
    return f"{value:g} {unit}".strip()


def _fmt_slope(slope: float, x_unit: str, y_unit: str, span_x: float) -> str:
    if not math.isfinite(slope):
        return "---"
    yu = "dB" if y_unit == "dBm" else (y_unit or "1")
    if x_unit == "Hz":
        if span_x >= 1e8:
            return f"{slope * 1e9:+.2f} {yu}/GHz"
        if span_x >= 1e5:
            return f"{slope * 1e6:+.2f} {yu}/MHz"
        if span_x >= 1e2:
            return f"{slope * 1e3:+.2f} {yu}/kHz"
        return f"{slope:+.3g} {yu}/Hz"
    if x_unit == "s":
        if span_x <= 1e-4:
            return f"{slope * 1e-6:+.2f} {yu}/µs"
        if span_x <= 0.1:
            return f"{slope * 1e-3:+.2f} {yu}/ms"
        return f"{slope:+.2f} {yu}/s"
    if x_unit:
        return f"{slope:+.3f} {yu}/{x_unit}"
    return f"{slope:+.3g}"


def _bw3(xs: list[float], ys: list[float]) -> float | None:
    """Ширина полосы по уровню −3 дБ относительно максимума (с линейной интерполяцией границ)."""
    n = len(ys)
    if n < 3:
        return None
    pk_i = max(range(n), key=lambda i: ys[i])
    thr = ys[pk_i] - 3.0
    left_x: float | None = None
    for i in range(pk_i - 1, -1, -1):
        if ys[i] <= thr:
            dy = ys[i + 1] - ys[i]
            t = (thr - ys[i]) / dy if dy != 0 else 0.0
            left_x = xs[i] + t * (xs[i + 1] - xs[i])
            break
    right_x: float | None = None
    for i in range(pk_i + 1, n):
        if ys[i] <= thr:
            dy = ys[i - 1] - ys[i]
            t = (thr - ys[i]) / dy if dy != 0 else 0.0
            right_x = xs[i] + t * (xs[i - 1] - xs[i])
            break
    if left_x is None or right_x is None or right_x <= left_x:
        return None
    return right_x - left_x


def analyze_trace(trace: Trace | None, *, x_range: tuple[float, float] | None = None,
                  y_transform=None) -> dict[str, Any]:
    """Посчитать набор статистических и ВЧ-метрик трассы в пределах ``x_range``."""
    if trace is None or len(trace) == 0:
        return {}
    xs: list[float] = []
    ys: list[float] = []
    x_lo = x_hi = None
    if x_range is not None and math.isfinite(x_range[0]) and math.isfinite(x_range[1]):
        x_lo, x_hi = (x_range[0], x_range[1]) if x_range[0] <= x_range[1] else (x_range[1], x_range[0])
    isfinite = math.isfinite
    for x, y in zip(trace.x, trace.y):
        if not (isfinite(x) and isfinite(y)):
            continue
        if x_lo is not None and (x < x_lo or x > x_hi):
            continue
        yv = y_transform(y) if y_transform is not None else y
        if not isfinite(yv):
            continue
        xs.append(float(x))
        ys.append(float(yv))
    n = len(ys)
    if n == 0:
        return {}
    mean = sum(ys) / n
    min_i = min(range(n), key=lambda i: ys[i])
    max_i = max(range(n), key=lambda i: ys[i])
    y_min, x_min = ys[min_i], xs[min_i]
    y_max, x_max = ys[max_i], xs[max_i]
    pp = y_max - y_min
    flatness = 0.5 * pp
    var = sum((y - mean) ** 2 for y in ys) / n
    std = math.sqrt(var)
    rms = math.sqrt(sum(y * y for y in ys) / n)

    slope: float | None = None
    nonlin: float | None = None
    span_x = abs(xs[-1] - xs[0]) if n >= 2 else 0.0
    if n >= 2:
        mx = sum(xs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0.0:
            sxy = sum((x - mx) * (y - mean) for x, y in zip(xs, ys))
            a = sxy / sxx
            b = mean - a * mx
            slope = a
            nonlin = max(abs(y - (a * x + b)) for x, y in zip(xs, ys))
        else:
            slope = 0.0
            nonlin = max(abs(y - mean) for y in ys)

    bw3 = _bw3(xs, ys) if trace.y_unit in ("dB", "dBm") and y_transform is None else None
    return {
        "count": n,
        "mean": mean,
        "min": y_min,
        "min_x": x_min,
        "max": y_max,
        "max_x": x_max,
        "std": std,
        "rms": rms,
        "pp": pp,
        "flatness": flatness,
        "slope": slope,
        "nonlin": nonlin,
        "bw3": bw3,
        "span_x": span_x,
    }


def format_analysis(stats: dict[str, Any], metrics: list[str] | tuple[str, ...],
                    x_unit: str, y_unit: str) -> list[str]:
    """Отформатировать выбранные метрики в список коротких элементов ``["MEAN ...", ...]``."""
    if not stats or not metrics:
        return []
    out: list[str] = []
    span_x = float(stats.get("span_x", 0.0) or 0.0)
    for key in metrics:
        if key == "mean" and "mean" in stats:
            out.append(f"MEAN {_fmt_y(stats['mean'], y_unit)}")
        elif key == "min" and "min" in stats:
            out.append(f"MIN {_fmt_y(stats['min'], y_unit)}@{_fmt_x(stats['min_x'], x_unit)}")
        elif key == "max" and "max" in stats:
            out.append(f"MAX {_fmt_y(stats['max'], y_unit)}@{_fmt_x(stats['max_x'], x_unit)}")
        elif key == "std" and "std" in stats:
            out.append(f"STD {_fmt_y(stats['std'], y_unit, diff=True)}")
        elif key == "pp" and "pp" in stats:
            out.append(f"P-P {_fmt_y(stats['pp'], y_unit, diff=True)}")
        elif key == "flatness" and "flatness" in stats:
            out.append(f"FLAT ±{_fmt_y(stats['flatness'], y_unit, diff=True)}")
        elif key == "nonlin":
            v = stats.get("nonlin")
            out.append(f"NONLIN {_fmt_y(v, y_unit, diff=True)}" if v is not None else "NONLIN ---")
        elif key == "slope":
            v = stats.get("slope")
            out.append(f"SLOPE {_fmt_slope(v, x_unit, y_unit, span_x)}" if v is not None else "SLOPE ---")
        elif key == "rms" and "rms" in stats:
            out.append(f"RMS {_fmt_y(stats['rms'], y_unit)}")
        elif key == "bw3":
            v = stats.get("bw3")
            out.append(f"BW-3dB {_fmt_x(v, x_unit)}" if v is not None else "BW-3dB ---")
    return out


def _interp_y(xs, ys, x: float) -> float:
    n = len(xs)
    if n == 0:
        return math.nan
    if n == 1:
        return float(ys[0])
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    i = bisect_left(xs, x)
    if i <= 0:
        return float(ys[0])
    if i >= n:
        return float(ys[-1])
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return float(y0)
    t = (x - x0) / (x1 - x0)
    return float(y0 + t * (y1 - y0))


def trace_diff(live: Trace, mem: Trace, y_transform=None) -> Trace:
    """Вычислить разность ``live − mem`` (при несовпадении сетки X - линейная интерполяция ``mem``)."""
    same_grid = (len(live.x) == len(mem.x) and len(live.x) > 0
                 and live.x[0] == mem.x[0] and live.x[-1] == mem.x[-1])
    ys: list[float] = []
    if same_grid:
        for yl, ym in zip(live.y, mem.y):
            if math.isfinite(yl) and math.isfinite(ym):
                a = y_transform(yl) if y_transform is not None else yl
                b = y_transform(ym) if y_transform is not None else ym
                ys.append(a - b if (math.isfinite(a) and math.isfinite(b)) else math.nan)
            else:
                ys.append(math.nan)
    else:
        mem_xs = list(mem.x)
        mem_ys = [y_transform(v) if (y_transform is not None and math.isfinite(v)) else v for v in mem.y]
        for xl, yl in zip(live.x, live.y):
            if math.isfinite(xl) and math.isfinite(yl):
                a = y_transform(yl) if y_transform is not None else yl
                b = _interp_y(mem_xs, mem_ys, xl)
                ys.append(a - b if (math.isfinite(a) and math.isfinite(b)) else math.nan)
            else:
                ys.append(math.nan)
    y_unit = "dB" if live.y_unit in ("dBm", "dB") else live.y_unit
    name = f"Δ{live.name}" if live.name else "ΔMEM"
    return Trace(live.x, tuple(ys), live.x_unit, y_unit, name=name,
                 timestamp=live.timestamp, meta=dict(live.meta or {}), uniform=live.uniform)


# ------------------------------------------------------------ всплывающая панель настроек графика
_POPUP_STYLE = render_qss("""
    PlotSettingsPopup {
        background-color: $UI_COLOR_PANEL;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER_LIGHT;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
    }
    PlotSettingsPopup QWidget {
        background: transparent;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        color: $UI_COLOR_TEXT_MUTED;
    }
    PlotSettingsPopup QLabel#sec {
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        color: $UI_COLOR_TEXT;
    }
    PlotSettingsPopup QLabel#muted {
        color: $UI_COLOR_BORDER_LIGHT;
    }
    PlotSettingsPopup QLineEdit {
        background-color: $UI_COLOR_BACKGROUND;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: 4px;
        padding: 2px 6px;
        min-height: 22px;
        color: $UI_COLOR_TEXT;
    }
    PlotSettingsPopup QLineEdit:focus { border-color: $UI_COLOR_TEXT_MUTED; }
    PlotSettingsPopup QLineEdit:disabled { color: $UI_COLOR_BORDER; }
    PlotSettingsPopup QPushButton {
        background-color: $UI_COLOR_BACKGROUND;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND;
        border-radius: 4px;
        padding: 3px 8px;
        min-height: 22px;
        color: $UI_COLOR_TEXT_MUTED;
    }
    PlotSettingsPopup QPushButton:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    PlotSettingsPopup QPushButton:checked {
        background-color: $UI_COLOR_INVERT;
        color: $UI_COLOR_TEXT_INVERSE;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    PlotSettingsPopup QPushButton:disabled { color: $UI_COLOR_BORDER; }
    PlotSettingsPopup QCheckBox {
        spacing: 6px;
        color: $UI_COLOR_TEXT;
    }
    PlotSettingsPopup QCheckBox::indicator {
        width: 14px; height: 14px;
        border-radius: 3px;
        border: 1px solid $UI_COLOR_BORDER_LIGHT;
        background: $UI_COLOR_BACKGROUND;
    }
    PlotSettingsPopup QCheckBox::indicator:checked {
        background: $UI_COLOR_INVERT;
        border-color: $UI_COLOR_TEXT;
    }
    PlotSettingsPopup QFrame#sep {
        background-color: $UI_COLOR_BORDER;
        max-height: 1px;
        min-height: 1px;
    }
""")


def _parse_axis_number(text: str, unit: str) -> float | None:
    s = text.strip().replace(",", ".")
    if not s:
        return None
    if unit and s.endswith(unit):
        s_plain = s[:-len(unit)].strip()
    else:
        s_plain = s
    try:
        v = float(s_plain)
        return v if math.isfinite(v) else None
    except ValueError:
        pass
    if unit in _SI_UNITS:
        try:
            v = si_parse(s, unit)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            pass
    return None


class PlotSettingsPopup(QFrame):
    """Интерактивная тёмная панель настроек графика: SCALE, MEM и выбор метрик ANALYSIS галочками."""

    def __init__(self, plot, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.plot = plot
        self.setStyleSheet(_POPUP_STYLE)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)

        # Заголовок
        hdr = QHBoxLayout()
        title = QLabel("PLOT SETTINGS")
        title.setObjectName("sec")
        close_btn = QPushButton("×")
        close_btn.setFixedWidth(24)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Close plot settings (Esc)")
        close_btn.clicked.connect(self.close)
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        # --- SCALE ---
        sec_scale = QLabel("SCALE")
        sec_scale.setObjectName("sec")
        lay.addWidget(sec_scale)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self.auto_btn = self._btn("AUTO", "Use device / automatic Y scale", self._on_auto, checkable=True)
        self.man_btn = self._btn("MAN", "Use manual Y scale (REF / DIV / DIVS)", self._on_man, checkable=True)
        self.fit_btn = self._btn("FIT", "Fit Y scale tightly to current trace and switch to MAN", self._on_fit)
        self.reset_btn = self._btn("RESET", "Reset Y scale to AUTO and X range to full span", self._on_reset)
        for b in (self.auto_btn, self.man_btn, self.fit_btn, self.reset_btn):
            mode_row.addWidget(b)
        lay.addLayout(mode_row)

        scale_grid = QGridLayout()
        scale_grid.setHorizontalSpacing(6)
        scale_grid.setVerticalSpacing(4)
        self.ref_edit = self._edit("Top of Y scale (REF); Enter to apply", self._apply_scale)
        self.div_edit = self._edit("Y value per division (DIV); Enter to apply", self._apply_scale)
        self.divs_edit = self._edit("Number of Y grid divisions (2..20)", self._apply_scale)
        self.xmin_edit = self._edit("Manual X minimum (leave blank for full span)", self._apply_x)
        self.xmax_edit = self._edit("Manual X maximum (leave blank for full span)", self._apply_x)

        scale_grid.addWidget(self._lbl("REF"), 0, 0)
        scale_grid.addWidget(self.ref_edit, 0, 1)
        scale_grid.addWidget(self._lbl("DIV"), 0, 2)
        scale_grid.addWidget(self.div_edit, 0, 3)

        scale_grid.addWidget(self._lbl("DIVS"), 1, 0)
        scale_grid.addWidget(self.divs_edit, 1, 1)
        self.grid_cb = QCheckBox("Grid")
        self.grid_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.grid_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.grid_cb.setToolTip("Show / hide coordinate grid lines")
        self.grid_cb.toggled.connect(self._on_grid)
        self.points_cb = QCheckBox("Points")
        self.points_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.points_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.points_cb.setToolTip("Draw sample dots along the trace")
        self.points_cb.toggled.connect(self._on_points)
        disp_row = QHBoxLayout()
        disp_row.setContentsMargins(0, 0, 0, 0)
        disp_row.setSpacing(8)
        disp_row.addWidget(self.grid_cb)
        disp_row.addWidget(self.points_cb)
        scale_grid.addLayout(disp_row, 1, 2, 1, 2)

        scale_grid.addWidget(self._lbl("X MIN"), 2, 0)
        scale_grid.addWidget(self.xmin_edit, 2, 1)
        scale_grid.addWidget(self._lbl("X MAX"), 2, 2)
        scale_grid.addWidget(self.xmax_edit, 2, 3)
        lay.addLayout(scale_grid)

        lay.addWidget(self._sep())

        # --- MEMORY (MEM) ---
        mem_hdr = QHBoxLayout()
        mem_lbl = QLabel("MEM")
        mem_lbl.setObjectName("sec")
        self.mem_info = QLabel("")
        self.mem_info.setObjectName("muted")
        mem_hdr.addWidget(mem_lbl)
        mem_hdr.addStretch()
        mem_hdr.addWidget(self.mem_info)
        lay.addLayout(mem_hdr)

        mem_row = QHBoxLayout()
        mem_row.setSpacing(4)
        self.mem_store_btn = self._btn("STORE", "Capture current trace into memory (or double-click graph)", self._on_mem_store)
        self.mem_show_btn = self._btn("SHOW", "Show / hide stored memory trace overlay", self._on_mem_show, checkable=True)
        self.mem_diff_btn = self._btn("DATA−MEM", "Display difference trace: Live − Memory", self._on_mem_diff, checkable=True)
        self.mem_clear_btn = self._btn("CLEAR", "Clear stored memory trace", self._on_mem_clear)
        for b in (self.mem_store_btn, self.mem_show_btn, self.mem_diff_btn, self.mem_clear_btn):
            mem_row.addWidget(b)
        lay.addLayout(mem_row)

        lay.addWidget(self._sep())

        # --- ANALYSIS ---
        an_hdr = QHBoxLayout()
        an_lbl = QLabel("ANALYSIS")
        an_lbl.setObjectName("sec")
        all_btn = self._btn("ALL", "Enable all analysis metrics", lambda: self._set_all_analysis(True))
        none_btn = self._btn("NONE", "Disable all analysis metrics", lambda: self._set_all_analysis(False))
        an_hdr.addWidget(an_lbl)
        an_hdr.addStretch()
        an_hdr.addWidget(all_btn)
        an_hdr.addWidget(none_btn)
        lay.addLayout(an_hdr)

        an_grid = QGridLayout()
        an_grid.setHorizontalSpacing(10)
        an_grid.setVerticalSpacing(4)
        self.analysis_checks: dict[str, QCheckBox] = {}
        for idx, (key, label, tip) in enumerate(ANALYSIS_METRICS):
            cb = QCheckBox(label)
            cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.setToolTip(tip)
            cb.toggled.connect(lambda checked, k=key: self._on_analysis_toggled(k, checked))
            self.analysis_checks[key] = cb
            an_grid.addWidget(cb, idx // 2, idx % 2)
        lay.addLayout(an_grid)

        self.sync_from_plot()

    @staticmethod
    def _lbl(text: str) -> QLabel:
        l = QLabel(text)
        l.setObjectName("muted")
        return l

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setObjectName("sep")
        return f

    @staticmethod
    def _btn(text: str, tip: str, slot, *, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        if checkable:
            b.clicked.connect(slot)
        else:
            b.clicked.connect(slot)
        return b

    @staticmethod
    def _edit(tip: str, slot) -> QLineEdit:
        e = QLineEdit()
        e.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        e.setMinimumWidth(86)
        e.setToolTip(tip)
        e.editingFinished.connect(slot)
        return e

    def sync_from_plot(self) -> None:
        st = self.plot.plot_settings_state()
        is_man = bool(st["manual_scale"])
        self.auto_btn.setChecked(not is_man)
        self.man_btn.setChecked(is_man)
        yu = st.get("y_unit", "")
        xu = st.get("x_unit", "")
        ref = st.get("ref")
        div = st.get("div")
        divs = st.get("divs_y", 6)
        self.ref_edit.blockSignals(True)
        self.div_edit.blockSignals(True)
        self.divs_edit.blockSignals(True)
        self.xmin_edit.blockSignals(True)
        self.xmax_edit.blockSignals(True)
        self.ref_edit.setText(_fmt_y(ref, yu) if ref is not None else "")
        self.div_edit.setText(_fmt_y(div, yu, diff=True) if div is not None else "")
        self.divs_edit.setText(str(int(divs)))
        self.divs_edit.setEnabled(bool(st.get("supports_divs", True)))
        xr = st.get("manual_x_range")
        self.xmin_edit.setText(_fmt_x(xr[0], xu) if xr else "")
        self.xmax_edit.setText(_fmt_x(xr[1], xu) if xr else "")
        self.xmin_edit.setPlaceholderText("auto")
        self.xmax_edit.setPlaceholderText("auto")
        self.xmin_edit.setEnabled(bool(st.get("supports_x_zoom", True)))
        self.xmax_edit.setEnabled(bool(st.get("supports_x_zoom", True)))
        self.ref_edit.blockSignals(False)
        self.div_edit.blockSignals(False)
        self.divs_edit.blockSignals(False)
        self.xmin_edit.blockSignals(False)
        self.xmax_edit.blockSignals(False)

        self.grid_cb.blockSignals(True)
        self.grid_cb.setChecked(bool(st.get("show_grid", True)))
        self.grid_cb.blockSignals(False)
        self.points_cb.blockSignals(True)
        self.points_cb.setChecked(bool(st.get("show_points", False)))
        self.points_cb.setEnabled(bool(st.get("supports_points", True)))
        self.points_cb.blockSignals(False)

        has_mem = bool(st.get("has_memory", False))
        mem_cnt = int(st.get("memory_count", 0))
        self.mem_info.setText(f"{mem_cnt} stored" if has_mem else "empty (2× click)")
        self.mem_show_btn.setEnabled(has_mem)
        self.mem_diff_btn.setEnabled(has_mem)
        self.mem_clear_btn.setEnabled(has_mem)
        self.mem_show_btn.setChecked(has_mem and bool(st.get("memory_visible", True)))
        self.mem_diff_btn.setChecked(has_mem and bool(st.get("memory_diff", False)))

        enabled = set(st.get("analysis_metrics", ()))
        for key, cb in self.analysis_checks.items():
            cb.blockSignals(True)
            cb.setChecked(key in enabled)
            cb.blockSignals(False)

    # ------------------------------------------------------------ обработчики
    def _on_auto(self) -> None:
        self.plot.set_auto_scale()
        self.sync_from_plot()

    def _on_man(self) -> None:
        self._apply_scale()

    def _on_fit(self) -> None:
        self.plot.autoscale_once()
        self.sync_from_plot()

    def _on_reset(self) -> None:
        self.plot.reset_view()
        self.sync_from_plot()

    def _apply_scale(self) -> None:
        st = self.plot.plot_settings_state()
        yu = st.get("y_unit", "")
        ref = _parse_axis_number(self.ref_edit.text(), yu)
        div_u = "dB" if yu == "dBm" else yu
        div = _parse_axis_number(self.div_edit.text(), div_u)
        if ref is None:
            ref = st.get("ref")
        if div is None or div <= 0:
            div = st.get("div")
        try:
            divs = max(2, min(20, int(float(self.divs_edit.text().strip()))))
        except ValueError:
            divs = int(st.get("divs_y", 6))
        if ref is not None and div is not None and div > 0:
            self.plot.set_manual_scale(ref, div, divs)
        self.sync_from_plot()

    def _apply_x(self) -> None:
        st = self.plot.plot_settings_state()
        xu = st.get("x_unit", "")
        t0, t1 = self.xmin_edit.text().strip(), self.xmax_edit.text().strip()
        if not t0 and not t1:
            self.plot.set_x_zoom(None, None)
            self.sync_from_plot()
            return
        cur = st.get("x_bounds", (0.0, 1.0))
        x0 = _parse_axis_number(t0, xu) if t0 else cur[0]
        x1 = _parse_axis_number(t1, xu) if t1 else cur[1]
        if x0 is not None and x1 is not None and x1 > x0:
            self.plot.set_x_zoom(x0, x1)
        self.sync_from_plot()

    def _on_grid(self, checked: bool) -> None:
        self.plot.set_show_grid(checked)

    def _on_points(self, checked: bool) -> None:
        self.plot.set_show_points(checked)

    def _on_mem_store(self) -> None:
        self.plot.store_memory()
        self.sync_from_plot()

    def _on_mem_show(self) -> None:
        self.plot.set_memory_visible(self.mem_show_btn.isChecked())
        self.sync_from_plot()

    def _on_mem_diff(self) -> None:
        self.plot.set_memory_diff(self.mem_diff_btn.isChecked())
        self.sync_from_plot()

    def _on_mem_clear(self) -> None:
        self.plot.clear_memory()
        self.sync_from_plot()

    def _on_analysis_toggled(self, key: str, checked: bool) -> None:
        self.plot.toggle_analysis_metric(key, checked)

    def _set_all_analysis(self, on: bool) -> None:
        self.plot.set_analysis_metrics(list(ANALYSIS_KEYS) if on else [])
        self.sync_from_plot()

    def show_at(self, global_pos: QPoint) -> None:
        self.adjustSize()
        sz = self.sizeHint()
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        x, y = global_pos.x(), global_pos.y()
        if screen is not None:
            avail: QRect = screen.availableGeometry()
            x = max(avail.left() + 4, min(x, avail.right() - sz.width() - 4))
            y = max(avail.top() + 4, min(y, avail.bottom() - sz.height() - 4))
        self.move(x, y)
        self.show()
        self.raise_()
