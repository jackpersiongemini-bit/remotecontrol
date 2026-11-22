"""График ваттметра по частоте/мощности, фильтрация по диапазонам множеств ``{f1}+{[f2,f3],[f4,f5]}-{[f6]}``,
поддержка масштаба, памяти (MEM), математики (MATH) и анализа (ANALYSIS), а также совместимость с CSV-точками.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.trace import Trace
from ..core.units import si_format, si_parse
from .live_plot import nice_range
from .plot_tools import (ANALYSIS_KEYS, MATH_MODES, PlotSettingsPopup, analyze_trace, format_analysis, trace_math,
                         validate_math_expr)
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL,
                    UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT,
                    UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, UI_TRACE_COLORS, current_theme, render_qss)

__all__ = [
    "parse_freq_hz",
    "parse_power_dbm",
    "RangeSet",
    "parse_range_expr",
    "filter_pm_matrix_by_ranges",
    "load_pm_points_csv",
    "save_pm_points_csv",
    "filter_pm_matrix",
    "PowerCurvePlot",
    "PMPointsDialog",
]


def _dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0) if abs(dbm) <= 300.0 else math.nan


def _convert_y(dbm: float, unit: str, src_dbm: float | None = None) -> float:
    if not math.isfinite(dbm):
        return math.nan
    if unit == "W":
        return _dbm_to_w(dbm)
    if unit == "dBW":
        return dbm - 30.0
    if unit == "V":
        w = _dbm_to_w(dbm)
        return math.sqrt(w * 50.0) if math.isfinite(w) and w >= 0.0 else math.nan
    if unit == "dB":
        return (dbm - src_dbm) if src_dbm is not None else math.nan
    return dbm


def _fmt_short_value(val: float, unit: str) -> str:
    if not math.isfinite(val):
        return ""
    if unit in ("W", "V"):
        return si_format(val, unit, 3)
    if unit == "dB":
        return f"{val:+.1f} dB"
    return f"{val:.1f} {unit}" if unit else f"{val:.2f}"


def _fmt_source_power(src_dbm: float | None) -> str:
    if src_dbm is None:
        return "MEAS"
    txt = f"{round(float(src_dbm), 3):.3f}".rstrip("0").rstrip(".")
    txt = "0" if txt in ("", "-0") else txt
    return f"{txt} dBm"


def parse_freq_hz(text: str) -> float | None:
    s = str(text or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
        return v if math.isfinite(v) and v > 0 else None
    except ValueError:
        pass
    try:
        v = si_parse(s, "Hz")
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_power_dbm(text: str) -> float | None:
    s = str(text or "").strip().replace(",", ".")
    if not s or s.lower() in ("*", "any", "all", "-", "---", "none"):
        return None
    for suf in ("dBm", "dbm", "DBM"):
        if s.endswith(suf):
            s = s[:-len(suf)].strip()
            break
    try:
        v = float(s)
        return round(v, 3) if math.isfinite(v) else None
    except ValueError:
        return None


# ------------------------------------------------------------ множества диапазонов {f1}+{[f2,f3],[f4,f5]}-{[f6]}
@dataclass(frozen=True)
class RangeSet:
    """Множество точек и отрезков вида ``{f1}+{[f2,f3],[f4,f5]}-{[f6]}``."""

    raw: str
    ops: tuple[tuple[str, tuple[tuple[float, float], ...]], ...]
    kind: str = "freq"  # "freq" | "power"

    @property
    def is_empty(self) -> bool:
        return not self.ops

    def _tol(self, v: float) -> float:
        if self.kind == "power":
            return 0.05
        return max(1.0, 1e-6 * abs(v))

    def contains(self, value: float | None) -> bool:
        if self.is_empty:
            return True
        if value is None or not math.isfinite(value):
            return False
        x = float(value)
        included = False
        for op, items in self.ops:
            hit = any((lo - self._tol(lo)) <= x <= (hi + self._tol(hi)) for lo, hi in items)
            if op == "+":
                if hit:
                    included = True
            else:
                if hit:
                    included = False
        return included

    def bounds(self) -> tuple[float, float] | None:
        vals = [v for op, items in self.ops if op == "+" for lo, hi in items for v in (lo, hi)]
        return (min(vals), max(vals)) if vals else None


def _split_top_level_terms(expr: str) -> list[tuple[str, str]]:
    """Разбить выражение ``{...}+{...}-{...}`` на термы ``[('+', '...'), ('-', '...')]`` на нулевом уровне скобок."""
    terms: list[tuple[str, str]] = []
    brace = 0
    bracket = 0
    op = "+"
    buf: list[str] = []
    n = len(expr)
    i = 0
    while i < n:
        ch = expr[i]
        if ch == "{":
            brace += 1
            buf.append(ch)
        elif ch == "}":
            brace -= 1
            if brace < 0:
                raise ValueError("Unmatched '}'")
            buf.append(ch)
        elif ch == "[":
            bracket += 1
            buf.append(ch)
        elif ch == "]":
            bracket -= 1
            if bracket < 0:
                raise ValueError("Unmatched ']'")
            buf.append(ch)
        elif ch in ("+", "-") and brace == 0 and bracket == 0:
            cur = "".join(buf).strip()
            # Унарный знак в начале числа или после 'e'/'E' (например -20 или 1e-3) не разделяет термы
            prev_ch = cur[-1] if cur else ""
            if not cur or prev_ch in ("e", "E", ","):
                buf.append(ch)
            else:
                terms.append((op, cur))
                op = ch
                buf = []
        else:
            buf.append(ch)
        i += 1
    if brace != 0:
        raise ValueError("Unclosed '{'")
    if bracket != 0:
        raise ValueError("Unclosed '['")
    tail = "".join(buf).strip()
    if tail:
        terms.append((op, tail))
    elif terms:
        raise ValueError(f"Trailing '{op}' operator")
    return terms


def _split_items(group: str) -> list[str]:
    """Разбить содержимое ``{...}`` по запятым вне ``[...]``."""
    s = group.strip()
    if s.startswith("{"):
        if not s.endswith("}"):
            raise ValueError("Expected closing '}'")
        s = s[1:-1].strip()
    if not s:
        return []
    parts: list[str] = []
    bracket = 0
    buf: list[str] = []
    for ch in s:
        if ch == "[":
            bracket += 1
            buf.append(ch)
        elif ch == "]":
            bracket -= 1
            if bracket < 0:
                raise ValueError("Unmatched ']'")
            buf.append(ch)
        elif ch == "," and bracket == 0:
            item = "".join(buf).strip()
            if not item:
                raise ValueError("Empty element near ','")
            parts.append(item)
            buf = []
        else:
            buf.append(ch)
    if bracket != 0:
        raise ValueError("Unclosed '['")
    last = "".join(buf).strip()
    if last:
        parts.append(last)
    return parts


def _parse_scalar(text: str, kind: str) -> float:
    if kind == "power":
        s = str(text or "").strip()
        for suf in ("dBm", "dbm", "DBM"):
            if s.endswith(suf):
                s = s[:-len(suf)].strip()
                break
        try:
            v = float(s)
        except ValueError as exc:
            raise ValueError(f"Invalid power value: {text!r}") from exc
        if not math.isfinite(v):
            raise ValueError(f"Invalid power value: {text!r}")
        return round(v, 3)
    v = parse_freq_hz(text)
    if v is None:
        raise ValueError(f"Invalid frequency value: {text!r}")
    return float(v)


def parse_range_expr(expr: str, kind: str = "freq") -> RangeSet:
    """Разобрать строку множества диапазонов вида ``{f1}+{[f2,f3],[f4,f5]}-{[f6]}``.

    * Пустая строка -> пустой фильтр (проходят все значения).
    * ``{v}`` / ``[v]`` / ``v`` -> одиночная точка.
    * ``[a, b]`` -> замкнутый отрезок ``[min(a, b), max(a, b)]``.
    * ``+`` объединяет множества, ``-`` вычитает (слева направо).
    * Для ``kind="freq"`` поддерживаются SI-суффиксы (``1G``, ``500M``, ``2.4 GHz``);
      для ``kind="power"`` — значения в dBm, включая отрицательные (``{-20}+{[-15,-5]}-{[-10]}``).
    """
    raw = str(expr or "").strip()
    if not raw:
        return RangeSet("", (), kind)
    terms = _split_top_level_terms(raw)
    ops: list[tuple[str, tuple[tuple[float, float], ...]]] = []
    for op, grp in terms:
        items_str = _split_items(grp)
        if not items_str:
            raise ValueError(f"Empty set in {grp!r}")
        parsed_items: list[tuple[float, float]] = []
        for it in items_str:
            if it.startswith("["):
                if not it.endswith("]"):
                    raise ValueError(f"Unclosed interval {it!r}")
                inner = it[1:-1].strip()
                if not inner:
                    raise ValueError("Empty interval []")
                bounds = [b.strip() for b in inner.split(",")]
                if len(bounds) == 1:
                    val = _parse_scalar(bounds[0], kind)
                    parsed_items.append((val, val))
                elif len(bounds) == 2:
                    a = _parse_scalar(bounds[0], kind)
                    b = _parse_scalar(bounds[1], kind)
                    parsed_items.append((min(a, b), max(a, b)))
                else:
                    raise ValueError(f"Interval must have 1 or 2 bounds: {it!r}")
            else:
                val = _parse_scalar(it, kind)
                parsed_items.append((val, val))
        ops.append((op, tuple(parsed_items)))
    return RangeSet(raw, tuple(ops), kind)


def filter_pm_matrix_by_ranges(
    columns: list[float | None],
    rows: list[tuple[float, dict[float | None, float]]],
    freq_range: RangeSet | None = None,
    power_range: RangeSet | None = None,
) -> tuple[list[float | None], list[tuple[float, dict[float | None, float]]]]:
    """Отфильтровать матрицу измерений по множествам диапазонов частоты и мощности."""
    use_f = freq_range is not None and not freq_range.is_empty
    use_p = power_range is not None and not power_range.is_empty
    if not use_f and not use_p:
        return list(columns), list(rows)
    out_cols = [c for c in columns if (not use_p or power_range.contains(c))]
    col_set = set(out_cols)
    out_rows: list[tuple[float, dict[float | None, float]]] = []
    for f, vals in rows:
        if use_f and not freq_range.contains(f):
            continue
        filtered = {p: v for p, v in vals.items() if p in col_set}
        if filtered:
            out_rows.append((f, filtered))
    return out_cols, out_rows


# ------------------------------------------------------------ CSV-функции (совместимость)
def load_pm_points_csv(path) -> list[tuple[float, float | None]]:
    """Загрузить список целевых точек ``[(freq_hz, power_dbm | None)]`` из CSV."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    pts: list[tuple[float, float | None]] = []
    seen: set[tuple[float, float | None]] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        sep = ";" if ";" in s else ("\t" if "\t" in s else ",")
        cells = [c.strip() for c in next(csv.reader([s], delimiter=sep))]
        if not cells:
            continue
        freq = parse_freq_hz(cells[0])
        if freq is None:
            continue
        f_key = float(round(freq, 3))
        if len(cells) == 1:
            pair = (f_key, None)
            if pair not in seen:
                seen.add(pair)
                pts.append(pair)
        else:
            added = False
            for cell in cells[1:]:
                if not cell.strip():
                    continue
                p = parse_power_dbm(cell)
                pair = (f_key, p)
                if pair not in seen:
                    seen.add(pair)
                    pts.append(pair)
                added = True
            if not added:
                pair = (f_key, None)
                if pair not in seen:
                    seen.add(pair)
                    pts.append(pair)
    pts.sort(key=lambda fp: (fp[0], -1e9 if fp[1] is None else fp[1]))
    return pts


def save_pm_points_csv(path, points: list[tuple[float, float | None]]) -> Path:
    """Сохранить список целевых точек в CSV (``freq_hz,power_dbm``)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(points, key=lambda fp: (fp[0], -1e9 if fp[1] is None else fp[1]))
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["freq_hz", "power_dbm"])
        for freq, pwr in ordered:
            w.writerow([f"{freq:.10g}", "" if pwr is None else f"{pwr:g}"])
    return p


def _match_freq(f_meas: float, f_tgt: float) -> bool:
    return abs(f_meas - f_tgt) <= max(1.0, 1e-6 * abs(f_tgt))


def _match_power(p_meas: float | None, p_tgt: float | None) -> bool:
    if p_tgt is None:
        return True
    if p_meas is None:
        return False
    return abs(p_meas - p_tgt) <= 0.05


def filter_pm_matrix(
    columns: list[float | None],
    rows: list[tuple[float, dict[float | None, float]]],
    target_points: list[tuple[float, float | None]],
) -> tuple[list[float | None], list[tuple[float, dict[float | None, float]]], set[tuple[float, float | None]]]:
    """Отфильтровать измеренную матрицу ваттметра по списку целевых точек из CSV."""
    if not target_points:
        return list(columns), list(rows), set()

    by_tgt_freq: dict[float, list[float | None]] = {}
    for f, p in target_points:
        by_tgt_freq.setdefault(float(f), []).append(p)

    num_cols: set[float] = {p for _f, p in target_points if p is not None}
    has_none_col = False
    for f_tgt, tgt_ps in by_tgt_freq.items():
        if None not in tgt_ps:
            continue
        for f_m, vals in rows:
            if _match_freq(f_m, f_tgt):
                for p_m in vals:
                    if p_m is None:
                        has_none_col = True
                    else:
                        num_cols.add(round(float(p_m), 3))

    out_cols: list[float | None] = sorted(num_cols)
    if has_none_col or (not out_cols):
        out_cols.append(None)

    out_rows: list[tuple[float, dict[float | None, float]]] = []
    planned: set[tuple[float, float | None]] = set()
    for f_tgt in sorted(by_tgt_freq):
        tgt_ps = by_tgt_freq[f_tgt]
        wildcard = None in tgt_ps
        row_vals: dict[float | None, float] = {}
        for col_p in out_cols:
            if wildcard or any(_match_power(col_p, tp) for tp in tgt_ps if tp is not None):
                planned.add((f_tgt, col_p))
        for f_m, vals in rows:
            if not _match_freq(f_m, f_tgt):
                continue
            for p_m, meas_v in vals.items():
                for col_p in out_cols:
                    if (f_tgt, col_p) not in planned:
                        continue
                    if col_p is None and p_m is None:
                        row_vals[None] = meas_v
                    elif col_p is not None and p_m is not None and abs(p_m - col_p) <= 0.05:
                        row_vals[col_p] = meas_v
        out_rows.append((f_tgt, row_vals))
    return out_cols, out_rows, planned


# ------------------------------------------------------------ график ваттметра (кривая на каждую мощность)
class PowerCurvePlot(QWidget):
    """График ваттметра: для каждого уровня мощности источника — своя кривая по частоте.

    Подписываются ТОЛЬКО две крайние кривые по мощности:
    * максимальная мощность (например, 17 dBm) — СВЕРХУ точек;
    * минимальная мощность (например, 12 dBm) — СНИЗУ точек;
    * промежуточные мощности (например, 16 dBm) — без числовых подписей.
    Поддерживает масштаб (SCALE), память (MEM), математику (MATH) и анализ (ANALYSIS).
    """

    DIVS_X, DIVS_Y = 10, 6
    _SI_UNITS = ("W", "V")
    settings_changed = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self, unit: str = "dBm", parent=None, min_height: int = 240):
        super().__init__(parent)
        self.unit = unit
        self.columns: list[float | None] = []
        self.rows: list[tuple[float, dict[float | None, float]]] = []
        self.target_freqs: list[float] = []
        self._mouse: QPointF | None = None
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self._font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        self._font.setPointSizeF(10.0)
        self._lbl_font = QFont(self._font)
        self._lbl_font.setPointSizeF(9.5)

        # Настройки шкалы, памяти (MEM), математики (MATH) и анализа
        self.manual_scale: bool = False
        self._manual_scale: tuple[float, float, float] | None = None
        self._manual_divs_y: int = self.DIVS_Y
        self._divs_y: int = self.DIVS_Y
        self.manual_x_range: tuple[float, float] | None = None
        self.show_grid: bool = True
        self.show_points: bool = True
        self.memory: list[tuple[str, Trace]] = []
        self.memory_visible: bool = True
        self.math_mode: str = "OFF"
        self.math_expr: str = ""
        self.analysis_metrics: list[str] = []
        self._settings: QSettings | None = None
        self._settings_key: str = ""
        self._popup: PlotSettingsPopup | None = None

        #: последние нарисованные подписи точек: [(placement "above"|"below", text, pt_x, pt_y, lbl_y)]
        self.point_labels: list[tuple[str, str, float, float, float]] = []
        self._curves: list[tuple[float | None, str, list[tuple[float, float, float]]]] = []
        self._eff_curves: list[tuple[float | None, str, list[tuple[float, float]]]] = []
        self._scale: tuple[float, float, float] | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(min_height)
        self.setMinimumWidth(200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Double-click: store / clear MEM · Wheel: zoom Y · Right-click: plot settings")

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

    # ------------------------------------------------------------ сохранение настроек
    def bind_settings(self, settings: QSettings | None, key: str) -> None:
        self._settings = settings
        self._settings_key = key
        if settings is None or not key:
            return
        base = key
        self.show_grid = bool(settings.value(f"{base}/GRID", True, type=bool))
        self.show_points = bool(settings.value(f"{base}/POINTS", True, type=bool))
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
        self.math_mode = "EXPR" if (saved_mode == "EXPR" and self.math_expr) else "OFF"
        self._rebuild_effective()
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
    def set_unit(self, unit: str) -> None:
        if unit == self.unit:
            return
        self.unit = unit
        self._rebuild_curves()
        self.update()

    def set_matrix(
        self,
        columns: list[float | None],
        rows: list[tuple[float, dict[float | None, float]]],
        *,
        unit: str | None = None,
        target_freqs: list[float] | None = None,
    ) -> None:
        u = self.unit if unit is None else unit
        tf = list(target_freqs or [])
        cols = list(columns)
        rws = [(f, dict(vals)) for f, vals in rows]
        if u == self.unit and cols == self.columns and rws == self.rows and tf == self.target_freqs:
            return
        self.unit = u
        self.columns = cols
        self.rows = rws
        self.target_freqs = tf
        self._rebuild_curves()
        self.update()

    def _effective_unit(self) -> str:
        if self._math_active():
            if self.math_mode == "SUB":
                return "dB" if self.unit in ("dBm", "dB") else self.unit
            if self.math_mode in ("DIV", "MUL"):
                return ""
        return self.unit

    def _as_traces(self) -> list[tuple[str, Trace]]:
        out: list[tuple[str, Trace]] = []
        for _cp, lbl, pts in self._curves:
            xs = tuple(f for f, _yv, _d in pts)
            ys = tuple(yv for _f, yv, _d in pts)
            out.append((lbl, Trace(xs, ys, "Hz", self.unit, name=lbl, uniform=False)))
        return out

    def _rebuild_curves(self) -> None:
        """Собрать кривые [(src_power, label, [(freq_hz, y_display, raw_dbm), ...])]."""
        curves: list[tuple[float | None, str, list[tuple[float, float, float]]]] = []
        for col_p in self.columns:
            pts: list[tuple[float, float, float]] = []
            for f, vals in self.rows:
                v_dbm = vals.get(col_p)
                if v_dbm is None or not math.isfinite(v_dbm):
                    continue
                yv = _convert_y(v_dbm, self.unit, col_p)
                if math.isfinite(yv):
                    pts.append((float(f), float(yv), float(v_dbm)))
            if pts:
                pts.sort(key=lambda t: t[0])
                curves.append((col_p, _fmt_source_power(col_p), pts))
        self._curves = curves
        self._rebuild_effective()

    def _rebuild_effective(self) -> None:
        eff: list[tuple[float | None, str, list[tuple[float, float]]]] = []
        math_on = self._math_active()
        for i, (cp, lbl, pts) in enumerate(self._curves):
            if math_on:
                xs = tuple(f for f, _y, _d in pts)
                ys = tuple(yv for _f, yv, _d in pts)
                live_tr = Trace(xs, ys, "Hz", self.unit, name=lbl, uniform=False)
                mem_tr = (self.memory[i][1] if i < len(self.memory) else self.memory[0][1]) if self.memory else None
                mtr = trace_math(live_tr, mem_tr, self.math_mode, self.math_expr)
                m_pts = [(float(x), float(y)) for x, y in zip(mtr.x, mtr.y) if math.isfinite(x) and math.isfinite(y)]
                if m_pts:
                    prefix = {"SUB": "Δ", "ADD": "+", "DIV": "÷", "MUL": "×", "EXPR": "f:"}.get(self.math_mode, "")
                    eff.append((cp, f"{prefix}{lbl}", m_pts))
            else:
                eff.append((cp, lbl, [(f, yv) for f, yv, _d in pts]))
        self._eff_curves = eff
        self._rescale()

    def _rescale(self) -> None:
        if self.manual_scale and self._manual_scale is not None:
            self._divs_y = self._manual_divs_y
            self._scale = self._manual_scale
            return
        self._divs_y = self.DIVS_Y
        x_lim = self.manual_x_range
        all_y = [
            yv for _cp, _lbl, pts in self._eff_curves for f, yv in pts
            if math.isfinite(yv) and (x_lim is None or x_lim[0] <= f <= x_lim[1])
        ]
        if not all_y:
            self._scale = None
            return
        eu = self._effective_unit()
        lo, hi = min(all_y), max(all_y)
        span = max(hi - lo, 2.0 if eu not in self._SI_UNITS else max(abs(hi) * 0.2, 1e-12))
        pad = span * 0.24
        lo_p = max(0.0, lo - pad) if eu in self._SI_UNITS and lo >= 0 else lo - pad
        hi_p = hi + pad
        self._scale = nice_range(lo_p, hi_p, self._divs_y)

    # ------------------------------------------------------------ API настроек (единый с LivePlot)
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
        self._divs_y = self.DIVS_Y
        self._rescale()
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def autoscale_once(self) -> None:
        x_lim = self.manual_x_range
        all_y = [
            yv for _cp, _lbl, pts in self._eff_curves for f, yv in pts
            if math.isfinite(yv) and (x_lim is None or x_lim[0] <= f <= x_lim[1])
        ]
        if not all_y:
            return
        lo_n, hi_n, step_n = nice_range(min(all_y), max(all_y), self._divs_y)
        self.set_manual_scale(hi_n, step_n, self._divs_y)

    def set_x_zoom(self, x_min: float | None, x_max: float | None) -> None:
        if x_min is None or x_max is None:
            self.manual_x_range = None
        else:
            x0, x1 = float(x_min), float(x_max)
            if math.isfinite(x0) and math.isfinite(x1) and x1 > x0:
                self.manual_x_range = (x0, x1)
        self._rescale()
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def reset_view(self) -> None:
        self.manual_x_range = None
        self.set_auto_scale()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = bool(on)
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def set_show_points(self, on: bool) -> None:
        self.show_points = bool(on)
        self._save_settings()
        self.settings_changed.emit()
        self.update()

    def store_memory(self) -> bool:
        trs = self._as_traces()
        if not trs:
            return False
        self.memory = trs
        self.memory_visible = True
        self._rebuild_effective()
        self.settings_changed.emit()
        self.update()
        return True

    def clear_memory(self) -> None:
        if not self.memory and self.math_mode not in ("SUB", "ADD", "DIV", "MUL"):
            return
        self.memory = []
        if self.math_mode in ("SUB", "ADD", "DIV", "MUL"):
            self.math_mode = "OFF"
        self._rebuild_effective()
        self.settings_changed.emit()
        self.update()

    def toggle_memory(self) -> bool:
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
        self._rebuild_effective()
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

    def _primary_trace(self) -> Trace | None:
        if not self._eff_curves:
            return None
        max_idx, _min_idx = self._min_max_curve_indices()
        idx = max_idx if max_idx is not None else 0
        _cp, lbl, pts = self._eff_curves[idx]
        if not pts:
            return None
        return Trace(tuple(f for f, _y in pts), tuple(y for _f, y in pts), "Hz", self._effective_unit(), name=lbl, uniform=False)

    def analysis_results(self) -> dict:
        tr = self._primary_trace()
        return analyze_trace(tr, x_range=self.manual_x_range) if tr is not None else {}

    def analysis_lines(self) -> list[str]:
        if not self.analysis_metrics:
            return []
        tr = self._primary_trace()
        if tr is None:
            return []
        stats = analyze_trace(tr, x_range=self.manual_x_range)
        tokens = format_analysis(stats, self.analysis_metrics, "Hz", self._effective_unit())
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

    def open_settings(self, global_pos: QPoint | None = None) -> PlotSettingsPopup:
        if self._popup is not None:
            self._popup.close()
        self._popup = PlotSettingsPopup(self, self)
        pos = global_pos if global_pos is not None else self.mapToGlobal(QPoint(self.width() - 280, 24))
        self._popup.show_at(pos)
        return self._popup

    def plot_settings_state(self) -> dict:
        ref = self._scale[1] if self._scale is not None else None
        div = self._scale[2] if self._scale is not None else None
        return {
            "manual_scale": self.manual_scale,
            "ref": ref,
            "div": div,
            "divs_y": self._divs_y,
            "y_unit": self._effective_unit(),
            "x_unit": "Hz",
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

    # ------------------------------------------------------------ оси и крайние кривые
    def _x_bounds(self) -> tuple[float, float]:
        if self.manual_x_range is not None:
            return self.manual_x_range
        xs = [f for _cp, _lbl, pts in self._eff_curves for f, _y in pts]
        if self.target_freqs:
            xs.extend(self.target_freqs)
        if not xs:
            return 0.0, 1.0
        x0, x1 = min(xs), max(xs)
        if x1 == x0:
            delta = max(abs(x0) * 0.05, 1e6)
            return x0 - delta, x1 + delta
        pad = (x1 - x0) * 0.04
        return x0 - pad, x1 + pad

    def _min_max_curve_indices(self) -> tuple[int | None, int | None]:
        """Индексы (max_idx, min_idx) кривых по мощности источника; промежуточные не подписываются."""
        if not self._eff_curves:
            return None, None
        if len(self._eff_curves) == 1:
            return 0, None
        has_src = all(cp is not None for cp, _lbl, _pts in self._eff_curves)
        if has_src:
            keys = [(float(cp), sum(yv for _f, yv in pts) / len(pts), idx)
                    for idx, (cp, _lbl, pts) in enumerate(self._eff_curves)]
        else:
            keys = [(sum(yv for _f, yv in pts) / len(pts), -1e9 if cp is None else float(cp), idx)
                    for idx, (cp, _lbl, pts) in enumerate(self._eff_curves)]
        max_idx = max(keys)[2]
        min_idx = min(keys)[2]
        return max_idx, (None if min_idx == max_idx else min_idx)

    def _analysis_height(self, lines: list[str] | None = None) -> int:
        if lines is None:
            lines = self.analysis_lines()
        return (len(lines) * 18 + 6) if lines else 0

    def _plot_rect(self, analysis_h: int | None = None) -> QRectF:
        ah = self._analysis_height() if analysis_h is None else analysis_h
        return QRectF(self.rect()).adjusted(8, 25, -8, -(25 + ah))

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

    def contextMenuEvent(self, e):  # noqa: N802 - Qt
        if self.receivers(self.settings_requested) > 0:
            self.settings_requested.emit()
        else:
            self.open_settings(e.globalPos())
        e.accept()

    # ------------------------------------------------------------ рисование
    def _draw_point_labels(
        self,
        p: QPainter,
        pts_px: list[tuple[float, float, float]],
        r: QRectF,
        color: QColor,
        placement: str,
    ) -> None:
        """Подписать точки кривой мощностью: ``above`` — над точкой, ``below`` — под точкой (с непрозрачной подложкой)."""
        if not pts_px:
            return
        p.setFont(self._lbl_font)
        fm = QFontMetrics(self._lbl_font)
        th = fm.height() + 2
        occupied: list[tuple[float, float]] = []
        order = list(range(len(pts_px)))
        if len(order) > 2:
            order = [0, len(pts_px) - 1, *range(1, len(pts_px) - 1)]
        eu = self._effective_unit()
        bg = QColor(UI_COLOR_BACKGROUND)
        bg.setAlpha(224)
        for idx in order:
            x_px, y_px, y_val = pts_px[idx]
            text = _fmt_short_value(y_val, eu)
            if not text:
                continue
            tw = fm.horizontalAdvance(text) + 8
            lx = max(r.left() + 2, min(x_px - tw / 2.0, r.right() - tw - 2))
            if any(not (lx + tw + 3 < ox0 or lx > ox1 + 3) for ox0, ox1 in occupied):
                continue
            occupied.append((lx, lx + tw))
            if placement == "above":
                ly = max(r.top() + 2, y_px - th - 5)
            else:
                ly = min(r.bottom() - th - 2, y_px + 6)
            box = QRectF(lx, ly, tw, th)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(box, 3, 3)
            p.setPen(color)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            self.point_labels.append((placement, text, x_px, y_px, ly))

    def _render_base(self) -> QPixmap:
        self.point_labels = []
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
        if not self._eff_curves or self._scale is None:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "NO POINTS YET")
            p.end()
            return pm

        x0, x1 = self._x_bounds()
        lo, hi, step = self._scale

        def px(x: float) -> float:
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y: float) -> float:
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        if self.target_freqs:
            p.setPen(QPen(muted, 1))
            for tf in self.target_freqs:
                tx = px(tf)
                if r.left() <= tx <= r.right():
                    p.drawLine(QPointF(tx, r.bottom() - 4), QPointF(tx, r.bottom()))

        max_idx, min_idx = self._min_max_curve_indices()
        p.save()
        p.setClipRect(r.adjusted(-4, -4, 4, 4))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Сохранённая память (пунктиром под живыми кривыми)
        if self.memory and self.memory_visible and not self._math_active():
            for i, (_mlbl, mtr) in enumerate(self.memory):
                mc = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
                mc.setAlpha(115)
                mpts = [QPointF(px(x), py(y)) for x, y in zip(mtr.x, mtr.y) if math.isfinite(x) and math.isfinite(y)]
                if len(mpts) > 1:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    pen = QPen(mc, 1.2, Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    p.setPen(pen)
                    p.drawPolyline(QPolygonF(mpts))

        rendered_pts: dict[int, tuple[QColor, list[tuple[float, float, float]]]] = {}
        for i, (_cp, _lbl, pts) in enumerate(self._eff_curves):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            pts_px = [(px(f), py(yv), yv) for f, yv in pts]
            rendered_pts[i] = (color, pts_px)
            if len(pts_px) > 1:
                p.setBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(color, 1.5)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawPolyline(QPolygonF([QPointF(x, y) for x, y, _v in pts_px]))
            if self.show_points or len(pts_px) == 1:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                for x_px, y_px, _v in pts_px:
                    p.drawEllipse(QPointF(x_px, y_px), 3.2, 3.2)

        # Подписи мощности ТОЛЬКО для крайних кривых: max-мощность сверху, min-мощность снизу
        if max_idx is not None and max_idx in rendered_pts:
            c_max, pts_max = rendered_pts[max_idx]
            self._draw_point_labels(p, pts_max, r, c_max, "above")
        if min_idx is not None and min_idx in rendered_pts:
            c_min, pts_min = rendered_pts[min_idx]
            self._draw_point_labels(p, pts_min, r, c_min, "below")
        p.restore()

        # Легенда (сверху слева) и шкала Y (сверху справа)
        p.setFont(self._font)
        x_leg = r.left() + 2
        for i, (_cp, lbl, _pts) in enumerate(self._eff_curves):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            p.setPen(color)
            w = p.fontMetrics().horizontalAdvance(lbl) + 14
            if x_leg + w > r.right() - 140:
                break
            p.drawText(QRectF(x_leg, 3, w, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
            x_leg += w
        if self._math_active():
            tag_map = {"SUB": "[DATA−MEM]", "ADD": "[DATA+MEM]", "DIV": "[DATA/MEM]", "MUL": "[DATA×MEM]"}
            tag = f"[f: {self.math_expr}]" if self.math_mode == "EXPR" else tag_map.get(self.math_mode, "[MATH]")
            p.setPen(muted)
            tw = p.fontMetrics().horizontalAdvance(tag) + 10
            p.drawText(QRectF(x_leg, 3, tw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tag)
        elif self.memory:
            tag = "[MEM]" if self.memory_visible else "[MEM OFF]"
            p.setPen(muted)
            tw = p.fontMetrics().horizontalAdvance(tag) + 10
            p.drawText(QRectF(x_leg, 3, tw, 19), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tag)

        eu = self._effective_unit()
        p.setPen(muted)
        if eu in self._SI_UNITS:
            scale_s = f"{si_format(hi, eu, 4)}  {si_format(step, eu, 3)}/div"
        else:
            div_u = "dB" if eu == "dBm" else eu
            top_s = f"{hi:g} {eu}" if eu else f"{hi:g}"
            div_s = f"{step:g} {div_u}/div" if div_u else f"{step:g}/div"
            scale_s = f"{top_s}  {div_s}"
        if self.manual_scale:
            scale_s += "  MAN"
        p.drawText(QRectF(r.left(), 3, r.width(), 19), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, scale_s)

        # Ось X снизу
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 19)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, si_format(x0, "Hz", 4))
        p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   f"{si_format((x1 - x0) / self.DIVS_X, 'Hz', 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, si_format(x1, "Hz", 4))

        # Полоса выбранных метрик анализа под осью X
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
        key = (
            self.width(), self.height(), self.devicePixelRatioF(), self.unit,
            tuple((cp, lbl, tuple(pts)) for cp, lbl, pts in self._eff_curves),
            tuple((n, id(t)) for n, t in self.memory),
            self.memory_visible, self.math_mode, self.math_expr,
            self.show_grid, self.show_points, self.manual_scale, self.manual_x_range,
            tuple(self.target_freqs), self._scale, self._divs_y, tuple(self.analysis_metrics), current_theme(),
        )
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_base()
            self._cache_key = key
        p = QPainter(self)
        if not p.isActive():
            return
        p.drawPixmap(0, 0, self._cache)
        m = self._mouse
        if m is None or not self._eff_curves or self._scale is None:
            return
        r = self._plot_rect()
        if not r.contains(m):
            return
        x0, x1 = self._x_bounds()
        lo, hi, _step = self._scale

        def px(x: float) -> float:
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y: float) -> float:
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        best = None
        best_d2 = math.inf
        for _cp, lbl, pts in self._eff_curves:
            for f, yv in pts:
                cx, cy = px(f), py(yv)
                d2 = (cx - m.x()) ** 2 + (cy - m.y()) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best = (cx, cy, f, yv, lbl)
        if best is None:
            return
        cx, cy, f, yv, lbl = best
        p.setFont(self._font)
        p.setPen(QPen(QColor(UI_COLOR_BORDER_LIGHT), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        readout = f"{si_format(f, 'Hz', 5)}  {_fmt_short_value(yv, self._effective_unit())}  ({lbl})"
        fm = QFontMetrics(self._font)
        bw = fm.horizontalAdvance(readout) + 14
        bh = fm.height() + 6
        bx = (r.left() + 6) if cx >= r.center().x() else (r.right() - bw - 6)
        bx = max(r.left() + 4, min(bx, r.right() - bw - 4))
        badge = QRectF(bx, r.top() + 5, bw, bh)
        bg = QColor(UI_COLOR_BACKGROUND)
        bg.setAlpha(238)
        p.setPen(QPen(QColor(UI_COLOR_BORDER_LIGHT), 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor(UI_COLOR_TEXT))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, readout)


# ------------------------------------------------------------ редактор CSV-списка целевых точек (совместимость)
_DIALOG_STYLE = render_qss("""
    PMPointsDialog {
        background-color: $UI_COLOR_PANEL;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        color: $UI_COLOR_TEXT_MUTED;
    }
    PMPointsDialog QLabel#heading { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT; }
    PMPointsDialog QLabel#muted { color: $UI_COLOR_BORDER_LIGHT; }
    PMPointsDialog QLineEdit {
        background-color: $UI_COLOR_BACKGROUND;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: 4px;
        padding: 2px 6px;
        min-height: 24px;
        color: $UI_COLOR_TEXT;
    }
    PMPointsDialog QPushButton {
        background-color: $UI_COLOR_BACKGROUND;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: 4px;
        padding: 3px 10px;
        min-height: 24px;
        color: $UI_COLOR_TEXT_MUTED;
    }
    PMPointsDialog QPushButton:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    PMPointsDialog QTableWidget {
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
    }
    PMPointsDialog QTableWidget::item {
        border: none;
        border-bottom: 1px solid $UI_COLOR_BORDER;
        padding: 2px 8px;
    }
    PMPointsDialog QHeaderView::section {
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_BORDER_LIGHT;
        border: none;
        border-bottom: 1px solid $UI_COLOR_BORDER;
        padding: 4px 8px;
    }
""")


class PMPointsDialog(QDialog):
    """Загрузка, редактирование, генерация и сохранение CSV-файла целевых точек ваттметра."""

    def __init__(
        self,
        points: list[tuple[float, float | None]],
        csv_path: str = "",
        measured_points: list[tuple[float, float | None]] = (),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Power meter CSV points")
        self.setStyleSheet(_DIALOG_STYLE)
        self.resize(540, 460)
        self.csv_path = str(csv_path or "")
        self._measured = list(measured_points)
        self._result_points: list[tuple[float, float | None]] = list(points)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        hdr = QLabel("TARGET POINTS (CSV)")
        hdr.setObjectName("heading")
        self.file_label = QLabel()
        self.file_label.setObjectName("muted")
        top.addWidget(hdr)
        top.addStretch()
        top.addWidget(self.file_label)
        lay.addLayout(top)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.load_btn = self._btn("LOAD CSV", "Load target points from a CSV file", self.load_file)
        self.save_btn = self._btn("SAVE", "Save edited points to the current CSV file", self.save_file)
        self.save_as_btn = self._btn("SAVE AS", "Save edited points to a new CSV file", self.save_file_as)
        self.add_btn = self._btn("+ ROW", "Add a new point row", self.add_row)
        self.del_btn = self._btn("− ROW", "Remove selected row(s)", self.delete_rows)
        self.from_meas_btn = self._btn("FROM MEAS", "Import currently measured (freq, power) points", self.import_measured)
        self.clear_btn = self._btn("CLEAR", "Clear all rows", self.clear_rows)
        for b in (self.load_btn, self.save_btn, self.save_as_btn, self.add_btn, self.del_btn,
                  self.from_meas_btn, self.clear_btn):
            bar.addWidget(b)
        self.from_meas_btn.setEnabled(bool(self._measured))
        lay.addLayout(bar)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["FREQ (Hz / GHz)", "POWER (dBm, blank = any)"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        gen_row = QHBoxLayout()
        gen_row.setSpacing(4)
        self.gen_f1 = self._edit("1G", "Grid start frequency (e.g. 1G, 500M)")
        self.gen_f2 = self._edit("3G", "Grid stop frequency (e.g. 3G, 18G)")
        self.gen_fn = self._edit("5", "Number of frequency points")
        self.gen_p1 = self._edit("-10", "Grid start source power (dBm, blank = any)")
        self.gen_p2 = self._edit("0", "Grid stop source power (dBm)")
        self.gen_pn = self._edit("3", "Number of power levels")
        self.gen_btn = self._btn("+ GRID", "Append a uniform Freq × Power grid to the table", self.append_grid)
        for w, lbl in ((self.gen_f1, "F"), (self.gen_f2, ".."), (self.gen_fn, "×"),
                       (self.gen_p1, "P"), (self.gen_p2, ".."), (self.gen_pn, "×")):
            l = QLabel(lbl)
            l.setObjectName("muted")
            gen_row.addWidget(l)
            gen_row.addWidget(w)
        gen_row.addWidget(self.gen_btn)
        lay.addLayout(gen_row)

        foot = QHBoxLayout()
        self.hint_label = QLabel("Format: freq_hz, power_dbm (SI suffixes like 2.4G supported)")
        self.hint_label.setObjectName("muted")
        self.ok_btn = self._btn("APPLY", "Apply points and close", self._on_accept)
        self.cancel_btn = self._btn("CANCEL", "Discard changes", self.reject)
        foot.addWidget(self.hint_label, 1)
        foot.addWidget(self.ok_btn)
        foot.addWidget(self.cancel_btn)
        lay.addLayout(foot)

        self._update_file_label()
        self.set_points(points)

    @staticmethod
    def _btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    @staticmethod
    def _edit(text: str, tip: str) -> QLineEdit:
        e = QLineEdit(text)
        e.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        e.setMaximumWidth(64)
        e.setToolTip(tip)
        return e

    def _update_file_label(self) -> None:
        self.file_label.setText(Path(self.csv_path).name if self.csv_path else "unsaved")
        self.file_label.setToolTip(self.csv_path or "No CSV file linked yet")

    def set_points(self, points: list[tuple[float, float | None]]) -> None:
        self.table.setRowCount(0)
        for freq, pwr in points:
            self._append_row_values(si_format(freq, "Hz", 9), "" if pwr is None else f"{pwr:g}")

    def _append_row_values(self, freq_text: str, power_text: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(freq_text))
        self.table.setItem(r, 1, QTableWidgetItem(power_text))

    def collect_points(self) -> list[tuple[float, float | None]]:
        pts: list[tuple[float, float | None]] = []
        seen: set[tuple[float, float | None]] = set()
        for r in range(self.table.rowCount()):
            fi = self.table.item(r, 0)
            pi = self.table.item(r, 1)
            freq = parse_freq_hz(fi.text() if fi else "")
            if freq is None:
                continue
            pwr = parse_power_dbm(pi.text() if pi else "")
            pair = (float(round(freq, 3)), pwr)
            if pair not in seen:
                seen.add(pair)
                pts.append(pair)
        pts.sort(key=lambda fp: (fp[0], -1e9 if fp[1] is None else fp[1]))
        return pts

    def add_row(self) -> None:
        self._append_row_values("1 GHz", "-10")
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)

    def delete_rows(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def clear_rows(self) -> None:
        self.table.setRowCount(0)

    def import_measured(self) -> None:
        if not self._measured:
            return
        cur = self.collect_points()
        merged = list({*cur, *self._measured})
        merged.sort(key=lambda fp: (fp[0], -1e9 if fp[1] is None else fp[1]))
        self.set_points(merged)

    def append_grid(self) -> None:
        f1 = parse_freq_hz(self.gen_f1.text())
        f2 = parse_freq_hz(self.gen_f2.text())
        try:
            fn = max(1, int(float(self.gen_fn.text().strip())))
        except ValueError:
            return
        if f1 is None or f2 is None:
            return
        freqs = [f1] if fn == 1 else [f1 + (f2 - f1) * i / (fn - 1) for i in range(fn)]
        p1 = parse_power_dbm(self.gen_p1.text())
        p2 = parse_power_dbm(self.gen_p2.text())
        if p1 is None:
            powers: list[float | None] = [None]
        elif p2 is None:
            powers = [p1]
        else:
            try:
                pn = max(1, int(float(self.gen_pn.text().strip())))
            except ValueError:
                pn = 1
            powers = [p1] if pn == 1 else [round(p1 + (p2 - p1) * i / (pn - 1), 3) for i in range(pn)]
        cur = self.collect_points()
        seen = set(cur)
        for f in freqs:
            fk = float(round(f, 3))
            for p in powers:
                if (fk, p) not in seen:
                    seen.add((fk, p))
                    cur.append((fk, p))
        cur.sort(key=lambda fp: (fp[0], -1e9 if fp[1] is None else fp[1]))
        self.set_points(cur)

    def load_file(self) -> None:
        start_dir = str(Path(self.csv_path).parent) if self.csv_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Load CSV points", start_dir, "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            pts = load_pm_points_csv(path)
        except OSError as exc:
            self.hint_label.setText(f"Load error: {exc}")
            return
        self.csv_path = path
        self._update_file_label()
        self.set_points(pts)
        self.hint_label.setText(f"Loaded {len(pts)} point(s)")

    def save_file(self) -> Path | None:
        if not self.csv_path:
            return self.save_file_as()
        pts = self.collect_points()
        try:
            saved = save_pm_points_csv(self.csv_path, pts)
        except OSError as exc:
            self.hint_label.setText(f"Save error: {exc}")
            return None
        self.hint_label.setText(f"Saved {len(pts)} point(s) to {saved.name}")
        return saved

    def save_file_as(self) -> Path | None:
        start_dir = str(Path(self.csv_path).parent) if self.csv_path else ""
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV points", start_dir, "CSV files (*.csv);;All files (*)")
        if not path:
            return None
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self.csv_path = path
        self._update_file_label()
        return self.save_file()

    def _on_accept(self) -> None:
        self._result_points = self.collect_points()
        self.accept()

    def points(self) -> list[tuple[float, float | None]]:
        return list(self._result_points)
