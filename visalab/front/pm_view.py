"""График ваттметра по частоте/мощности, загрузка/редактирование CSV-списка целевых точек и фильтрация.

* ``load_pm_points_csv`` / ``save_pm_points_csv`` / ``filter_pm_matrix`` — работа с CSV-файлом целевых точек
  ``(частота Hz, мощность источника dBm | None)``;
* :class:`PowerCurvePlot` — график ваттметра: отдельная кривая на каждый уровень подаваемой мощности,
  поддержка единиц (dBm / W / dBW / V / dB), подписи значений мощности над точками максимальной по мощности
  кривой и под точками минимальной по мощности кривой;
* :class:`PMPointsDialog` — встроенный тёмный редактор CSV-списка точек (загрузка, правка ячеек, генератор
  сетки, импорт текущих измерений, сохранение).
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.units import si_format, si_parse
from .live_plot import nice_range
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL,
                    UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT,
                    UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, UI_TRACE_COLORS, render_qss)

__all__ = [
    "parse_freq_hz",
    "parse_power_dbm",
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


def load_pm_points_csv(path) -> list[tuple[float, float | None]]:
    """Загрузить список целевых точек ``[(freq_hz, power_dbm | None)]`` из CSV.

    Поддерживает разделители ``,`` / ``;`` / ``\\t``, необязательный заголовок, SI-суффиксы частот
    (``1 GHz``, ``2.4G``, ``500M``), 1 колонку (только частота -> любая мощность), 2 колонки
    (``freq, power``) и несколько колонок мощностей в строке (``freq, p1, p2, ...``).
    """
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
            continue  # строка заголовка или мусор
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
    """Отфильтровать измеренную матрицу ваттметра по списку целевых точек из CSV.

    Возвращает ``(out_cols, out_rows, planned_cells)``, где ``out_rows`` содержит все целевые частоты
    из CSV (даже ещё не измеренные), а ``planned_cells`` — множество запланированных ячеек ``(freq, col_p)``.
    """
    if not target_points:
        return list(columns), list(rows), set()

    by_tgt_freq: dict[float, list[float | None]] = {}
    for f, p in target_points:
        by_tgt_freq.setdefault(float(f), []).append(p)

    # Какие колонки мощностей нужны: все явно заданные в CSV + измеренные, попавшие под wildcard (None)
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
        # Отмечаем запланированные ячейки
        for col_p in out_cols:
            if wildcard or any(_match_power(col_p, tp) for tp in tgt_ps if tp is not None):
                planned.add((f_tgt, col_p))
        # Ищем совпадающие измерения
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

    Точки кривой с максимальной мощностью подписываются измеренным значением СВЕРХУ точки,
    а точки кривой с минимальной мощностью — СНИЗУ точки.
    """

    DIVS_X, DIVS_Y = 10, 6
    _SI_UNITS = ("W", "V")

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
        self._font.setPointSizeF(8)
        self._lbl_font = QFont(self._font)
        self._lbl_font.setPointSizeF(7.5)
        #: последние нарисованные подписи точек: [(placement "above"|"below", text, pt_x, pt_y, lbl_y)]
        self.point_labels: list[tuple[str, str, float, float, float]] = []
        self._curves: list[tuple[float | None, str, list[tuple[float, float, float]]]] = []
        self._scale: tuple[float, float, float] | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(min_height)
        self.setMinimumWidth(200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Power meter curves (one per source power). Top curve labeled above points, bottom curve below")

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
        all_y = [yv for _cp, _lbl, pts in curves for _f, yv, _dbm in pts]
        if not all_y:
            self._scale = None
            return
        lo, hi = min(all_y), max(all_y)
        span = max(hi - lo, 2.0 if self.unit not in self._SI_UNITS else max(abs(hi) * 0.2, 1e-12))
        # Дополнительный запас сверху и снизу под подписи точек максимальной и минимальной кривых
        pad = span * 0.22
        lo_p = max(0.0, lo - pad) if self.unit in self._SI_UNITS and lo >= 0 else lo - pad
        hi_p = hi + pad
        self._scale = nice_range(lo_p, hi_p, self.DIVS_Y)

    def _x_bounds(self) -> tuple[float, float]:
        xs = [f for _cp, _lbl, pts in self._curves for f, _y, _d in pts]
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
        """Индексы (max_idx, min_idx) кривых по мощности; при 1 кривой - (0, None)."""
        if not self._curves:
            return None, None
        if len(self._curves) == 1:
            return 0, None
        # Сортируем по средней измеренной мощности (и по мощности источника при равенстве)
        keys = [
            (sum(yv for _f, yv, _d in pts) / len(pts), -1e9 if cp is None else cp, idx)
            for idx, (cp, _lbl, pts) in enumerate(self._curves)
        ]
        max_idx = max(keys)[2]
        min_idx = min(keys)[2]
        return max_idx, (None if min_idx == max_idx else min_idx)

    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(8, 22, -8, -22)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = e.position()
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._mouse = None
        self.update()

    def _draw_point_labels(
        self,
        p: QPainter,
        pts_px: list[tuple[float, float, float]],
        r: QRectF,
        color: QColor,
        placement: str,
    ) -> None:
        """Подписать точки кривой мощностью: ``above`` — над точкой, ``below`` — под точкой."""
        if not pts_px:
            return
        p.setFont(self._lbl_font)
        fm = QFontMetrics(self._lbl_font)
        th = fm.height()
        p.setPen(color)
        occupied: list[tuple[float, float]] = []
        # Приоритет: крайние точки, затем промежуточные слева направо
        order = list(range(len(pts_px)))
        if len(order) > 2:
            order = [0, len(pts_px) - 1, *range(1, len(pts_px) - 1)]
        for idx in order:
            x_px, y_px, y_val = pts_px[idx]
            text = _fmt_short_value(y_val, self.unit)
            if not text:
                continue
            tw = fm.horizontalAdvance(text) + 4
            lx = max(r.left() + 2, min(x_px - tw / 2.0, r.right() - tw - 2))
            if any(not (lx + tw + 2 < ox0 or lx > ox1 + 2) for ox0, ox1 in occupied):
                continue
            occupied.append((lx, lx + tw))
            if placement == "above":
                ly = max(r.top() + 2, y_px - th - 4)
            else:
                ly = min(r.bottom() - th - 2, y_px + 5)
            p.drawText(QRectF(lx, ly, tw, th), Qt.AlignmentFlag.AlignCenter, text)
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
        r = self._plot_rect()
        p.setFont(self._font)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        grid_pen = QPen(QColor(UI_COLOR_BORDER), 1)
        grid_pen.setCosmetic(True)
        p.setPen(grid_pen)
        for i in range(self.DIVS_X + 1):
            x = r.left() + r.width() * i / self.DIVS_X
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i in range(self.DIVS_Y + 1):
            y = r.top() + r.height() * i / self.DIVS_Y
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        muted = QColor(UI_COLOR_BORDER_LIGHT)
        if not self._curves or self._scale is None:
            p.setPen(muted)
            msg = "NO CSV POINTS MEASURED YET" if self.target_freqs else "NO POINTS YET"
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, msg)
            p.end()
            return pm

        x0, x1 = self._x_bounds()
        lo, hi, step = self._scale

        def px(x: float) -> float:
            return r.left() + (x - x0) / (x1 - x0) * r.width() if x1 != x0 else r.center().x()

        def py(y: float) -> float:
            return r.bottom() - (min(max(y, lo), hi) - lo) / (hi - lo) * r.height()

        # Метки целевых частот из CSV по нижней оси
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
        rendered_pts: dict[int, tuple[QColor, list[tuple[float, float, float]]]] = {}
        for i, (_cp, _lbl, pts) in enumerate(self._curves):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            pts_px = [(px(f), py(yv), yv) for f, yv, _d in pts]
            rendered_pts[i] = (color, pts_px)
            if len(pts_px) > 1:
                p.setBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(color, 1.4)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawPolyline(QPolygonF([QPointF(x, y) for x, y, _v in pts_px]))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            for x_px, y_px, _v in pts_px:
                p.drawEllipse(QPointF(x_px, y_px), 3.0, 3.0)

        # Подписи мощности: максимальная кривая — сверху точек, минимальная — снизу точек
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
        for i, (_cp, lbl, _pts) in enumerate(self._curves):
            color = QColor(UI_TRACE_COLORS[i % len(UI_TRACE_COLORS)])
            p.setPen(color)
            w = p.fontMetrics().horizontalAdvance(lbl) + 12
            if x_leg + w > r.right() - 120:
                break
            p.drawText(QRectF(x_leg, 3, w, 15), Qt.AlignmentFlag.AlignLeft, lbl)
            x_leg += w
        p.setPen(muted)
        if self.unit in self._SI_UNITS:
            scale_s = f"{si_format(hi, self.unit, 4)}  {si_format(step, self.unit, 3)}/div"
        else:
            div_u = "dB" if self.unit == "dBm" else self.unit
            scale_s = f"{hi:g} {self.unit}  {step:g} {div_u}/div"
        p.drawText(QRectF(r.left(), 3, r.width(), 15), Qt.AlignmentFlag.AlignRight, scale_s)

        # Ось X снизу
        bottom = QRectF(r.left(), r.bottom() + 3, r.width(), 15)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, si_format(x0, "Hz", 4))
        p.drawText(bottom, Qt.AlignmentFlag.AlignHCenter, f"{si_format((x1 - x0) / self.DIVS_X, 'Hz', 3)}/div")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, si_format(x1, "Hz", 4))
        p.end()
        return pm

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.width() <= 0 or self.height() <= 0:
            return
        key = (self.width(), self.height(), self.devicePixelRatioF(), self.unit,
               tuple((cp, tuple(pts)) for cp, _lbl, pts in self._curves),
               tuple(self.target_freqs), self._scale)
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_base()
            self._cache_key = key
        p = QPainter(self)
        if not p.isActive():
            return
        p.drawPixmap(0, 0, self._cache)
        m = self._mouse
        if m is None or not self._curves or self._scale is None:
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
        for _cp, lbl, pts in self._curves:
            for f, yv, _d in pts:
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
        p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        p.setPen(QColor(UI_COLOR_TEXT))
        p.drawText(QRectF(r.left(), r.top() + 2, r.width() - 4, 14), Qt.AlignmentFlag.AlignRight,
                   f"{si_format(f, 'Hz', 5)}  {_fmt_short_value(yv, self.unit)}  ({lbl})")


# ------------------------------------------------------------ редактор CSV-списка целевых точек
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

        # Заголовок и путь файла
        top = QHBoxLayout()
        hdr = QLabel("TARGET POINTS (CSV)")
        hdr.setObjectName("heading")
        self.file_label = QLabel()
        self.file_label.setObjectName("muted")
        top.addWidget(hdr)
        top.addStretch()
        top.addWidget(self.file_label)
        lay.addLayout(top)

        # Кнопки работы с CSV и строками
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

        # Таблица редактирования точек
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["FREQ (Hz / GHz)", "POWER (dBm, blank = any)"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        # Быстрый генератор сетки частота × мощность
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

        # Нижние кнопки OK / Cancel
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
