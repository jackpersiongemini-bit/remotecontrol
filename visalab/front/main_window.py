"""Главное окно: какие приборы открыты и что сейчас измеряется.

Сверху - плитки приборов: что это (VNA, SA, PM, GEN, PULSE, PSU), индикатор - окно прибора открыто,
красная плитка - у прибора ошибка (горит, пока её не сбросили CLEAR в журнале окна); клик - окно прибора
(Алексей 2026-11-14: «не что работает, а что открыто и у чего ошибка»). Ниже - панели живых данных
приёмников, которые сейчас измеряют (решает координатор, ``visalab.coord.ActivityMonitor``):

* ВАЦ - трейсы включённых измерений (по графику на каждую единицу: dB, deg, s...);
* ваттметр - таблица: частота датчика и мощность в этой точке (последнее показание с сигналом на каждой
  частоте); во временном режиме (TRAC ON) - трасса во времени;
* анализатор спектра - трасса, пик, шумовая дорожка.

У панели - источник сигнала, если координатор его нашёл: «← GEN 1 GHz  PATH −10.0 dB».
Пока идёт сценарий координатора (ACAL...), в полосе плиток - его фишка «ACAL 12/101» (клик - окно прибора).
ALL - показывать и приёмники, которые сейчас ничего не измеряют. Вкладки: LIVE - живые данные,
SET - пороги «измеряет / не измеряет» (сохраняются в ini, группа MAIN/THRESHOLDS). Полный экран - кнопка в
заголовке или F11. Окно знает только ключи профилей.
"""
from __future__ import annotations

import json
import math
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QMimeData, QPoint, QPointF, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDrag, QFont, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..coord import COORD, Activity, ActivityMonitor, Coordinator, profile_name
from ..core.params import ConnectionState
from ..core.units import si_format, si_parse
from ..core.trace import Trace
from .instrument_dock import InstrumentDock, InstrumentDockWindow, unregister_dock
from .instrument_window import WINDOW_EVENTS, all_instrument_windows, window_for
from .live_plot import LivePlot, nearest_index
from .params import IndicatorButton, Panel, PanelLine, PanelTable, PTabBar, _repolish, paint_indicator
from .plot_tools import (ANALYSIS_KEYS, ANALYSIS_METRICS, PlotSettingsWidget, analyze_trace, eval_math_expr,
                         format_analysis, make_panel_icon, validate_math_expr)
from .pm_view import (PMPointsDialog, PowerCurvePlot, RangeSet, filter_pm_matrix, filter_pm_matrix_by_ranges,
                      load_pm_points_csv, parse_range_expr)
from .power_meter import pm_time_scale
from .split_layout import (CUSTOM_LAYOUT_MODE, DROP_ZONES, LAYOUT_DESCRIPTIONS, LAYOUT_MODES, PLOT_LAYOUT_MODES,
                           DropZoneOverlay, GraphSplitter, PaneLeaf, PaneNode, PaneSplit, build_preset_tree,
                           clone_tree, detect_drop_zone, dock_panel, equalize_tree, layout_preset_icon, leaf_order,
                           project_tree, swap_panels_in_tree, sync_weights_into_tree, tree_from_dict,
                           tree_structure_sig, tree_to_dict)
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR,
                    UI_COLOR_INDICATOR, UI_COLOR_TEXT, UI_COLOR_TEXT_MUTED, UI_TITLE_BAR_ITEM_SIZE_PX,
                    WINDOW_BAR_HEIGHT, checkbox_qss, render_qss, scrollbar_qss)
from .window import CustomFullButton, Window, WindowBar, mark_growing, short_address

__all__ = ["MainWindow", "SincEmblem", "InstrumentTile", "LivePanel", "CurrentPanel", "ScenarioChip", "PowerTable",
           "CurrentTable", "LAYOUT_MODES", "CUSTOM_LAYOUT_MODE", "PLOT_LAYOUT_MODES", "PM_UNITS", "CURRENT_UNITS"]

TILE_MIN_HEIGHT = UI_TITLE_BAR_ITEM_SIZE_PX
PLOT_MIN_HEIGHT = 240
RIGHT_PANEL_MIN_HEIGHT = 320
UI_REFRESH_MS = 60
PM_UNITS = ("dBm", "W", "dBW", "V", "dB")
_PM_UNIT_LABELS = {"dBm": "dBm", "W": "W", "dBW": "dBW", "V": "V (50 Ω)", "dB": "dB (Pout − Pin)"}
CURRENT_UNITS = ("AUTO", "mA", "A", "µA")
_CURR_UNIT_LABELS = {"AUTO": "Auto (SI)", "mA": "mA", "A": "A", "µA": "µA"}
_PANEL_MIME = "application/x-visalab-livepanel"

#: страница SET: (подпись, поле Thresholds, единица, множитель показа, подсказка)
SETTINGS = (
    ("SA MARG", "sa_margin_db", "dB", 1.0, "Spectrum analyzer shows peak marker and source when peak is this much above the noise floor"),
    ("PM FLR", "pm_floor_dbm", "dBm", 1.0, "Power meter matches a signal source when its reading is not below this level"),
    ("HOLD", "hold_s", "s", 1.0, "Keep showing an instrument this long after it stops outputting data"),
    ("FTOL", "freq_tol_hz", "Hz", 1.0, "Frequency match with a generator: at least this close"),
    ("FREL", "freq_tol_rel", "%", 100.0, "Frequency match with a generator: or this close relative to the frequency"),
)


class SettingField(QLineEdit):
    """Порог: число с единицей; Enter и уход фокуса - применить, Esc - вернуть."""

    focus_committed = pyqtSignal()

    def __init__(self, unit: str, scale: float, parent=None):
        super().__init__(parent)
        self.setObjectName("cfg")
        self.unit, self.scale = unit, scale
        self.value = 0.0
        self._escaped = False
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self._escaped = True
            self.show_value(self.value)
            self.clearFocus()
            return
        self._escaped = False
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setModified(False)
            self.clearFocus()

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        if self._escaped:
            self._escaped = False
            self.show_value(self.value)
            return
        if self.isModified():
            self.setModified(False)
            self.focus_committed.emit()
        else:
            self.show_value(self.value)

    def show_value(self, v: float) -> None:
        self.value = v
        shown = v * self.scale
        self.setText(si_format(shown, self.unit, 6) if self.unit == "Hz" else f"{shown:g} {self.unit}")
        self.setModified(False)

    def parsed(self) -> float:
        return si_parse(self.text(), self.unit) / self.scale


def _dbm(v: float | None) -> str:
    return "" if v is None else f"{v:.2f} dBm"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


#: вертикальных делений на экране анализатора спектра (PSA) и ВАЦ (PNA)
DEVICE_DIVS_Y = 10


def _vna_scale(tr: Trace) -> tuple[float, float, float] | None:
    """Шкала Y трассы ВАЦ ``(lo, hi, step)`` по метаданным прибора (10 делений); для POLAR/SMITH - None."""
    meta = tr.meta or {}
    if meta.get("format") in ("POLAR", "SMITH"):
        return None
    pdiv = _num(meta.get("scale_div"))
    rlev = _num(meta.get("scale_ref"))
    rpos = _num(meta.get("scale_rpos"))
    if pdiv is None or rlev is None or rpos is None or pdiv <= 0:
        return None
    lo = rlev - rpos * pdiv
    hi = lo + DEVICE_DIVS_Y * pdiv
    if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo):
        return None
    return (lo, hi, pdiv)


class SincEmblem(QWidget):
    """Пульсирующая эмблема sinc(x) = sin(πx)/(πx) без рамки в левой части заголовка главного окна."""

    WIDTH = 44
    HEIGHT = UI_TITLE_BAR_ITEM_SIZE_PX
    TICK_MS = 33
    PERIOD_S = 2.6
    _SAMPLES = 72
    _X_MAX = 3.2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sincEmblem")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setToolTip("VISALAB")
        self._phase = 0.0
        self._xs = [(-self._X_MAX + 2.0 * self._X_MAX * i / (self._SAMPLES - 1)) for i in range(self._SAMPLES)]
        self._ys = [1.0 if abs(x) < 1e-9 else math.sin(math.pi * x) / (math.pi * x) for x in self._xs]
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._step)

    @property
    def pulse(self) -> float:
        """Текущая фаза пульсации в диапазоне [0, 1]."""
        return 0.5 - 0.5 * math.cos(self._phase)

    def _step(self) -> None:
        self._phase = (self._phase + 2.0 * math.pi * (self.TICK_MS / 1000.0) / self.PERIOD_S) % (2.0 * math.pi)
        self.update()

    def showEvent(self, e):  # noqa: N802 - Qt
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, e):  # noqa: N802 - Qt
        self._timer.stop()
        super().hideEvent(e)

    def stop(self) -> None:
        self._timer.stop()

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.width() <= 0 or self.height() <= 0:
            return
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pulse = self.pulse

        pad_x, pad_top, pad_bot = 3.0, 4.0, 3.0
        plot_w = self.width() - 2.0 * pad_x
        plot_h = self.height() - pad_top - pad_bot
        # sinc(x) лежит в [-0.217, 1.0]: ось 0 на ~78% высоты
        y_zero = pad_top + plot_h * 0.78
        y_scale = (y_zero - pad_top) * (0.76 + 0.24 * pulse)

        pts = [
            QPointF(pad_x + plot_w * i / (self._SAMPLES - 1), y_zero - y * y_scale)
            for i, y in enumerate(self._ys)
        ]
        curve = QPainterPath(pts[0])
        for pt in pts[1:]:
            curve.lineTo(pt)

        # Деликатное мягкое свечение без рамки и подложки
        p.setBrush(Qt.BrushStyle.NoBrush)
        glow_color = QColor(UI_COLOR_TEXT)
        glow_color.setAlpha(int(18 + 38 * pulse))
        glow_pen = QPen(glow_color, 3.2)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(glow_pen)
        p.drawPath(curve)

        # Чёткая основная линия sinc(x)
        fg_color = QColor(UI_COLOR_TEXT)
        fg_color.setAlpha(int(200 + 55 * pulse))
        fg_pen = QPen(fg_color, 1.6)
        fg_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        fg_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(fg_pen)
        p.drawPath(curve)

        # Мягкий акцент в главном максимуме (x = 0)
        peak = QPointF(pad_x + plot_w * 0.5, y_zero - y_scale)
        dot_r = 1.15 + 0.45 * pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_TEXT))
        p.drawEllipse(peak, dot_r, dot_r)
        p.end()


class InstrumentTile(IndicatorButton):
    """Плитка прибора: что это (VNA, PM...); индикатор - окно прибора открыто, красная - у прибора ошибка."""

    def __init__(self, proxy, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.setObjectName("chip")
        self.setProperty("tile", True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.window_open = False
        self.error = False
        self.setMinimumWidth(54)
        self.setFixedHeight(TILE_MIN_HEIGHT)

    def indicated(self) -> bool:
        return self.window_open

    def show_state(self, name: str, window_open: bool, error: bool, tip: str = "") -> None:
        window_open, error = bool(window_open), bool(error)
        changed = (self.text() != name) or (window_open != self.window_open) or (error != self.error)
        if self.text() != name:
            self.setText(name)
        if error != self.error:
            self.setProperty("wstate", "error" if error else "")
            _repolish(self)
        self.window_open, self.error = window_open, error
        if tip and self.toolTip() != tip:
            self.setToolTip(tip)
        if changed:
            self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        QPushButton.paintEvent(self, e)
        p = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.window_open:                  # окно открыто - индикатор (при ошибке - красная полоса)
            paint_indicator(p, rect, UI_COLOR_ERROR if self.error else UI_COLOR_INDICATOR)
        if self.error:                        # ошибка - красная рамка поверх
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(UI_COLOR_ERROR), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.25, 0.25, -0.25, -0.25), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)


def _dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0) if abs(dbm) <= 300.0 else math.nan


def _fmt_num(value: float) -> str:
    text = f"{round(float(value), 3):.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _format_power(dbm: float, unit: str, src_dbm: float | None = None) -> str:
    if unit == "W":
        w = _dbm_to_w(dbm)
        return si_format(w, "W", 4) if math.isfinite(w) else "--- W"
    if unit == "dBW":
        return f"{dbm - 30.0:.2f} dBW"
    if unit == "V":
        w = _dbm_to_w(dbm)
        return si_format(math.sqrt(w * 50.0), "V", 4) if math.isfinite(w) and w >= 0.0 else "--- V"
    if unit == "dB":
        return f"{dbm - src_dbm:+.2f} dB" if src_dbm is not None else "---"
    return f"{dbm:.2f} dBm"


def _format_col_header(src_dbm: float | None, unit: str, has_numeric: bool) -> str:
    if src_dbm is None:
        return "---" if has_numeric else "POWER"
    if unit == "W":
        w = _dbm_to_w(src_dbm)
        return si_format(w, "W", 4) if math.isfinite(w) else f"{_fmt_num(src_dbm)} dBm"
    if unit == "dBW":
        return f"{_fmt_num(src_dbm - 30.0)} dBW"
    if unit == "V":
        w = _dbm_to_w(src_dbm)
        return si_format(math.sqrt(w * 50.0), "V", 4) if math.isfinite(w) and w >= 0.0 else f"{_fmt_num(src_dbm)} dBm"
    return f"{_fmt_num(src_dbm)} dBm"


class PowerTable(QTableWidget):
    """Ваттметр в главном окне: матрица «частота датчика × подаваемая мощность источника»;
    поддерживает переключение единиц (dBm / W / dBW / V / dB)."""

    EMPTY = "NO POINTS YET: each sensor frequency gets a row\nand each source power level gets a column"
    ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    ALIGN = (ALIGN_LEFT, ALIGN_RIGHT)

    def __init__(self, unit: str = "dBm", parent=None):
        super().__init__(0, 2, parent)
        self.setObjectName("pmTable")
        self.unit = unit if unit in PM_UNITS else "dBm"
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(20)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.horizontalHeader()
        hh.setMinimumSectionSize(76)
        hh.setHighlightSections(False)
        self.setMinimumHeight(PLOT_MIN_HEIGHT)
        self.columns: list[float | None] = []
        self.rows: list[tuple[float, dict[float | None, float]]] = []
        self.planned_cells: set[tuple[float, float | None]] = set()
        self.current: float | None = None
        self.current_power: float | None = None
        self._cur_cell: tuple[int, int] = (-1, -1)
        self._normal_font = QFont(self.font())
        self._bold_font = QFont(self.font())
        self._bold_font.setBold(True)
        self._cur_color = QColor(UI_COLOR_TEXT)
        self._muted_color = QColor(UI_COLOR_TEXT_MUTED)
        self._dim_color = QColor(UI_COLOR_BORDER_LIGHT)
        self._setup_headers([])

    def _setup_headers(self, columns: list[float | None]) -> None:
        has_num = any(c is not None for c in columns)
        shown_cols = columns if columns else [None]
        labels = ["FREQ", *(_format_col_header(c, self.unit, has_num) for c in shown_cols)]
        if self.columnCount() != len(labels):
            self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)
        hh = self.horizontalHeader()
        mode = QHeaderView.ResizeMode.Stretch if len(labels) <= 5 else QHeaderView.ResizeMode.ResizeToContents
        hh.setSectionResizeMode(mode)
        hh.setStretchLastSection(False)
        for col in range(len(labels)):
            item = self.horizontalHeaderItem(col)
            if item is None:
                continue
            item.setTextAlignment(self.ALIGN_LEFT if col == 0 else self.ALIGN_RIGHT)
            if col == 0:
                item.setToolTip("Sensor frequency")
            else:
                src_p = shown_cols[col - 1]
                if src_p is None:
                    item.setToolTip("Measured power (no matched RF source)")
                else:
                    w = _dbm_to_w(src_p)
                    w_txt = f" ({si_format(w, 'W', 4)})" if math.isfinite(w) else ""
                    item.setToolTip(f"Input source power: {_fmt_num(src_p)} dBm{w_txt}")

    def set_unit(self, unit: str) -> None:
        if unit not in PM_UNITS or unit == self.unit:
            return
        self.unit = unit
        self._render(force=True)

    def _style_cell(self, row: int, col: int, active: bool, dim: bool = False) -> None:
        item = self.item(row, col)
        if item is not None:
            item.setFont(self._bold_font if active else self._normal_font)
            item.setForeground(self._cur_color if active else (self._dim_color if dim else self._muted_color))

    @staticmethod
    def _same_power(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a is b
        return abs(a - b) <= 5e-4

    def set_rows(self, rows, current: float | None = None) -> None:
        """Обратная совместимость: [(Hz, dBm, время)] -> таблица с одним столбцом POWER."""
        rows = list(rows)
        cols: list[float | None] = [None] if rows else []
        matrix_rows = [(f, {None: v}) for f, v, _t in rows]
        self.set_matrix(cols, matrix_rows, current, None)

    def set_matrix(self, columns: list[float | None], rows: list[tuple[float, dict[float | None, float]]],
                   current: float | None = None, current_power: float | None = None,
                   planned_cells: set[tuple[float, float | None]] | None = None) -> None:
        """columns: [P_in dBm | None]; rows: [(Hz, {P_in: P_meas dBm})]."""
        cols = list(columns)
        rows = list(rows)
        planned = set(planned_cells) if planned_cells else set()
        if (cols == self.columns and rows == self.rows and planned == self.planned_cells
                and current == self.current and self._same_power(current_power, self.current_power)):
            return
        self.columns = cols
        self.rows = rows
        self.planned_cells = planned
        self.current = current
        self.current_power = current_power
        self._render(force=False)

    def _render(self, force: bool = False) -> None:
        old_cell = self._cur_cell
        self._setup_headers(self.columns)
        shown_cols = self.columns if self.columns else [None]
        if self.rowCount() != len(self.rows):
            self.setRowCount(len(self.rows))
        cur_row, cur_col = -1, -1
        for i, (f, values) in enumerate(self.rows):
            is_cur_row = self.current is not None and abs(f - self.current) < 0.5
            if is_cur_row:
                cur_row = i
            f_item = self.item(i, 0)
            if f_item is None:
                f_item = QTableWidgetItem()
                f_item.setTextAlignment(self.ALIGN_LEFT)
                self.setItem(i, 0, f_item)
            f_txt = si_format(f, "Hz", 9)
            if f_item.text() != f_txt:
                f_item.setText(f_txt)
            self._style_cell(i, 0, is_cur_row)
            for j, col_p in enumerate(shown_cols):
                col = j + 1
                is_cur_cell = is_cur_row and self._same_power(col_p, self.current_power)
                if is_cur_cell:
                    cur_col = col
                item = self.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(self.ALIGN_RIGHT)
                    self.setItem(i, col, item)
                v = values.get(col_p)
                if v is not None:
                    text = _format_power(v, self.unit, col_p)
                    dim = False
                elif (f, col_p) in self.planned_cells:
                    text = "---"
                    dim = True
                else:
                    text = ""
                    dim = False
                if item.text() != text:
                    item.setText(text)
                self._style_cell(i, col, is_cur_cell, dim=dim)
        new_cell = (cur_row, cur_col if cur_col >= 0 else 0)
        self._cur_cell = new_cell
        if (force or new_cell != old_cell) and cur_row >= 0:
            target = self.item(cur_row, max(0, cur_col))
            if target is not None:
                self.scrollToItem(target, QAbstractItemView.ScrollHint.EnsureVisible)
        self.viewport().update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if not self.rows:
            p = QPainter(self.viewport())
            p.setPen(QColor(UI_COLOR_BORDER_LIGHT))
            p.drawText(QRectF(self.viewport().rect()), Qt.AlignmentFlag.AlignCenter, self.EMPTY)


def _format_current(amps: float, unit: str) -> str:
    if not math.isfinite(amps):
        return "---"
    if unit == "mA":
        return f"{amps * 1e3:.2f} mA"
    if unit == "A":
        return f"{amps:.4f} A"
    if unit == "µA":
        return f"{amps * 1e6:.1f} µA"
    return si_format(amps, "A", 4)


class CurrentTable(QTableWidget):
    """Таблица токов канала ИП: матрица «частота генератора × выходная мощность генератора»."""

    EMPTY = "NO POINTS YET: turn on an RF generator and PSU output\neach frequency gets a row and each generator power gets a column"
    ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    def __init__(self, unit: str = "AUTO", parent=None):
        super().__init__(0, 2, parent)
        self.setObjectName("currTable")
        self.unit = unit if unit in CURRENT_UNITS else "AUTO"
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(22)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.horizontalHeader()
        hh.setMinimumSectionSize(76)
        hh.setHighlightSections(False)
        self.setMinimumHeight(PLOT_MIN_HEIGHT)
        self.columns: list[float | None] = []
        self.rows: list[tuple[float, dict[float | None, float]]] = []
        self.current: float | None = None
        self.current_power: float | None = None
        self._cur_cell: tuple[int, int] = (-1, -1)
        self._normal_font = QFont(self.font())
        self._bold_font = QFont(self.font())
        self._bold_font.setBold(True)
        self._cur_color = QColor(UI_COLOR_TEXT)
        self._muted_color = QColor(UI_COLOR_TEXT_MUTED)
        self._setup_headers([])

    def _setup_headers(self, columns: list[float | None]) -> None:
        has_num = any(c is not None for c in columns)
        shown_cols = columns if columns else [None]
        labels = ["FREQ", *(_format_col_header(c, "dBm", has_num) for c in shown_cols)]
        if self.columnCount() != len(labels):
            self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)
        hh = self.horizontalHeader()
        mode = QHeaderView.ResizeMode.Stretch if len(labels) <= 5 else QHeaderView.ResizeMode.ResizeToContents
        hh.setSectionResizeMode(mode)
        hh.setStretchLastSection(False)
        for col in range(len(labels)):
            item = self.horizontalHeaderItem(col)
            if item is None:
                continue
            item.setTextAlignment(self.ALIGN_LEFT if col == 0 else self.ALIGN_RIGHT)
            if col == 0:
                item.setToolTip("RF generator frequency")
            else:
                src_p = shown_cols[col - 1]
                if src_p is None:
                    item.setToolTip("Measured PSU current (generator output power unknown)")
                else:
                    w = _dbm_to_w(src_p)
                    w_txt = f" ({si_format(w, 'W', 4)})" if math.isfinite(w) else ""
                    item.setToolTip(f"RF generator output power: {_fmt_num(src_p)} dBm{w_txt}")

    def set_unit(self, unit: str) -> None:
        if unit not in CURRENT_UNITS or unit == self.unit:
            return
        self.unit = unit
        self._render(force=True)

    def _style_cell(self, row: int, col: int, active: bool) -> None:
        item = self.item(row, col)
        if item is not None:
            item.setFont(self._bold_font if active else self._normal_font)
            item.setForeground(self._cur_color if active else self._muted_color)

    @staticmethod
    def _same_power(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a is b
        return abs(a - b) <= 5e-4

    def set_matrix(self, columns: list[float | None], rows: list[tuple[float, dict[float | None, float]]],
                   current: float | None = None, current_power: float | None = None) -> None:
        cols = list(columns)
        rows = list(rows)
        if cols == self.columns and rows == self.rows and current == self.current \
                and self._same_power(current_power, self.current_power):
            return
        self.columns = cols
        self.rows = rows
        self.current = current
        self.current_power = current_power
        self._render(force=False)

    def _render(self, force: bool = False) -> None:
        old_cell = self._cur_cell
        self._setup_headers(self.columns)
        shown_cols = self.columns if self.columns else [None]
        if self.rowCount() != len(self.rows):
            self.setRowCount(len(self.rows))
        cur_row, cur_col = -1, -1
        for i, (f, values) in enumerate(self.rows):
            is_cur_row = self.current is not None and abs(f - self.current) < 0.5
            if is_cur_row:
                cur_row = i
            f_item = self.item(i, 0)
            if f_item is None:
                f_item = QTableWidgetItem()
                f_item.setTextAlignment(self.ALIGN_LEFT)
                self.setItem(i, 0, f_item)
            f_txt = si_format(f, "Hz", 9)
            if f_item.text() != f_txt:
                f_item.setText(f_txt)
            self._style_cell(i, 0, is_cur_row)
            for j, col_p in enumerate(shown_cols):
                col = j + 1
                is_cur_cell = is_cur_row and self._same_power(col_p, self.current_power)
                if is_cur_cell:
                    cur_col = col
                item = self.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(self.ALIGN_RIGHT)
                    self.setItem(i, col, item)
                v = values.get(col_p)
                text = _format_current(v, self.unit) if v is not None else ""
                if item.text() != text:
                    item.setText(text)
                self._style_cell(i, col, is_cur_cell)
        new_cell = (cur_row, cur_col if cur_col >= 0 else 0)
        self._cur_cell = new_cell
        if (force or new_cell != old_cell) and cur_row >= 0:
            target = self.item(cur_row, max(0, cur_col))
            if target is not None:
                self.scrollToItem(target, QAbstractItemView.ScrollHint.EnsureVisible)
        self.viewport().update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if not self.rows:
            p = QPainter(self.viewport())
            p.setPen(QColor(UI_COLOR_BORDER_LIGHT))
            p.drawText(QRectF(self.viewport().rect()), Qt.AlignmentFlag.AlignCenter, self.EMPTY)


def _icon_btn(kind: str, tip: str, slot, *, checkable: bool = False) -> QPushButton:
    """Компактная кнопка заголовка панели с векторной HiDPI-иконкой."""
    b = QPushButton()
    b.setObjectName("tool")
    b.setIcon(make_panel_icon(kind, 15))
    b.setIconSize(QSize(15, 15))
    b.setFixedSize(26, 24)
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setToolTip(tip)
    b.clicked.connect(slot)
    return b


class _RangeCommitEdit(QLineEdit):
    """Поле ввода строки диапазонов/формул в настройках графика: Enter и уход фокуса применяют, Esc откатывает."""

    def __init__(self, placeholder: str, tip: str, on_commit, parent=None):
        super().__init__(parent)
        self._committed = ""
        self._on_commit = on_commit
        self._escaped = False
        self.setObjectName("cfg")
        self.setPlaceholderText(placeholder)
        self.setToolTip(tip)
        self.returnPressed.connect(self._commit)

    def set_committed(self, text: str, *, invalid: bool = False) -> None:
        self._committed = text
        self.blockSignals(True)
        self.setText(text)
        self.setModified(False)
        self.blockSignals(False)
        self.set_invalid(invalid)

    def set_invalid(self, invalid: bool) -> None:
        self.setProperty("invalid", bool(invalid))
        self.style().unpolish(self)
        self.style().polish(self)

    def _commit(self) -> None:
        self._escaped = False
        self._on_commit()
        self.clearFocus()

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self._escaped = True
            self.set_committed(self._committed)
            self.clearFocus()
            return
        self._escaped = False
        super().keyPressEvent(e)

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        if self._escaped:
            self._escaped = False
            return
        if self.text() != self._committed:
            self._on_commit()


def _settings_scroll_area(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setObjectName("panelSettingsScroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    area.setStyleSheet(render_qss("""
        QScrollArea#panelSettingsScroll, QWidget#panelSettingsBody {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        QWidget#panelSettingsBody QLabel#sec { font-weight: 600; color: $UI_COLOR_TEXT; }
        QWidget#panelSettingsBody QLabel#muted { color: $UI_COLOR_BORDER_LIGHT; }
        QWidget#panelSettingsBody QLabel#err { color: $UI_COLOR_ERROR; }
        QWidget#panelSettingsBody QLineEdit[invalid="true"] { border-color: $UI_COLOR_ERROR; }
        QWidget#panelSettingsBody QFrame#sep { background-color: $UI_COLOR_BORDER; max-height: 1px; min-height: 1px; }
    """) + "\n" + scrollbar_qss("QScrollArea#panelSettingsScroll "))
    return area


class CurrentPanel(Panel):
    """Панель таблицы токов одного канала ИП (частота × мощность генератора) со встроенными настройками и анализом."""

    move_requested = pyqtSignal(str, int)
    swap_requested = pyqtSignal(str, str)
    close_requested = pyqtSignal(str)

    def __init__(self, proxy, channel: int, monitor: ActivityMonitor, parent=None, settings: QSettings | None = None):
        self.proxy = proxy
        self.channel = int(channel)
        self.monitor = monitor
        self.settings = settings
        self.profile = "power_supply"
        self.panel_key = f"CURR:{monitor.name(proxy)}:CH{self.channel}:{proxy.address}"
        self.plots: list[LivePlot] = []
        self.power_table: PowerTable | None = None
        self._drag_pos: QPoint | None = None
        self._process_override: str = ""
        self._rf_choice_key: str = ""
        self._freq_range: RangeSet = RangeSet("", (), "freq")
        self._power_range: RangeSet = RangeSet("", (), "power")
        self._math_expr: str = ""
        self.analysis_metrics: list[str] = []
        if settings is not None:
            self._rf_choice_key = str(settings.value(f"MAIN/CURR_RF/{self.panel_key}", "") or "")
            raw_f = str(settings.value(f"MAIN/CURR_FREQ_RANGE/{self.panel_key}", "", type=str) or "").strip()
            raw_p = str(settings.value(f"MAIN/CURR_POWER_RANGE/{self.panel_key}", "", type=str) or "").strip()
            try:
                self._freq_range = parse_range_expr(raw_f, "freq")
            except ValueError:
                self._freq_range = RangeSet("", (), "freq")
            try:
                self._power_range = parse_range_expr(raw_p, "power")
            except ValueError:
                self._power_range = RangeSet("", (), "power")
            expr = str(settings.value(f"MAIN/CURR_MATH/{self.panel_key}", "", type=str) or "").strip()
            self._math_expr = expr if (expr and validate_math_expr(expr)[0]) else ""
            raw_an = str(settings.value(f"MAIN/CURR_ANALYSIS/{self.panel_key}", "", type=str) or "")
            try:
                items = json.loads(raw_an) if raw_an else []
                if isinstance(items, list):
                    self.analysis_metrics = [k for k in items if k in ANALYSIS_KEYS]
            except (TypeError, ValueError):
                self.analysis_metrics = []

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Облегчённый заголовок: имя прибора + процесс над графиком + 3 иконки (настройки, clear, закрыть)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel(f"{monitor.name(proxy)} CH{self.channel}")
        self.title.setObjectName("heading")
        self.title.setToolTip(f"{monitor.name(proxy)} CH{self.channel}: {proxy.name} ({short_address(proxy.address)})")
        self.process_label = QLabel()
        self.process_label.setObjectName("heading")
        self.process_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.level = self.process_label  # совместимость

        self.settings_button = _icon_btn("settings", "Toggle settings for this table",
                                         self._on_settings_toggled, checkable=True)
        self.clear_button = _icon_btn("clear", "Clear this channel's current table", self.clear_data)
        self.close_button = _icon_btn("close", "Hide this current table",
                                      lambda: self.close_requested.emit(self.panel_key))
        head.addWidget(self.title)
        head.addWidget(self.process_label, 1)
        head.addWidget(self.settings_button)
        head.addWidget(self.clear_button)
        head.addWidget(self.close_button)
        lay.addLayout(head)

        saved_unit = str(settings.value(f"MAIN/CURR_UNIT/{self.panel_key}", "AUTO") or "AUTO") if settings else "AUTO"
        if saved_unit not in CURRENT_UNITS:
            saved_unit = "AUTO"

        # Содержимое: страница 0 — таблица + строка анализа; страница 1 — встроенные настройки
        self.stack = QStackedWidget()
        content_page = QWidget()
        c_lay = QVBoxLayout(content_page)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(4)
        self.current_table = mark_growing(CurrentTable(unit=saved_unit))
        c_lay.addWidget(self.current_table, 1)
        self.analysis_label = QLabel()
        self.analysis_label.setObjectName("source")
        self.analysis_label.setWordWrap(True)
        self.analysis_label.hide()
        c_lay.addWidget(self.analysis_label)
        self.stack.addWidget(content_page)

        self._settings_body = self._build_settings_view(saved_unit)
        self._settings_scroll = _settings_scroll_area(self._settings_body)
        self.stack.addWidget(self._settings_scroll)
        lay.addWidget(self.stack, 1)

        super().__init__(body, parent=parent)
        self.setAcceptDrops(True)
        self._apply_saved_rf_source()

    def _build_settings_view(self, saved_unit: str) -> QWidget:
        w = QWidget()
        w.setObjectName("panelSettingsBody")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        hdr = QLabel("CURRENT TABLE SETTINGS")
        hdr.setObjectName("sec")
        done_btn = QPushButton("DONE")
        done_btn.setObjectName("chip")
        done_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.setToolTip("Return to current table")
        done_btn.clicked.connect(lambda: self.set_settings_open(False))
        top.addWidget(hdr)
        top.addStretch()
        top.addWidget(done_btn)
        lay.addLayout(top)

        # Выбор RF-источника
        rf_row = QHBoxLayout()
        rf_row.setSpacing(6)
        rf_lbl = QLabel("RF SOURCE")
        rf_lbl.setObjectName("muted")
        self.rf_button = QPushButton("RF: AUTO")
        self.rf_button.setObjectName("chip")
        self.rf_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rf_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rf_button.setToolTip("Select RF generator source for frequency / output power axes")
        self.rf_button.clicked.connect(self._open_rf_menu)
        rf_row.addWidget(rf_lbl)
        rf_row.addWidget(self.rf_button)
        rf_row.addStretch()
        lay.addLayout(rf_row)

        # Единицы тока
        u_row = QHBoxLayout()
        u_row.setSpacing(4)
        u_lbl = QLabel("UNIT")
        u_lbl.setObjectName("muted")
        u_row.addWidget(u_lbl)
        self.unit_buttons: dict[str, QPushButton] = {}
        for u in CURRENT_UNITS:
            b = QPushButton(u)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setChecked(u == saved_unit)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(_CURR_UNIT_LABELS[u])
            b.clicked.connect(lambda _=False, unit=u: self.set_unit(unit))
            self.unit_buttons[u] = b
            u_row.addWidget(b)
        u_row.addStretch()
        lay.addLayout(u_row)

        # Диапазоны частот и мощностей
        sep1 = QFrame()
        sep1.setObjectName("sep")
        lay.addWidget(sep1)
        rng_lbl = QLabel("RANGES  {f1}+{[f2,f3],[f4,f5]}-{[f6]}")
        rng_lbl.setObjectName("sec")
        lay.addWidget(rng_lbl)
        rng_grid = QGridLayout()
        rng_grid.setHorizontalSpacing(6)
        rng_grid.setVerticalSpacing(4)
        fl = QLabel("FREQ")
        fl.setObjectName("muted")
        pl = QLabel("POWER")
        pl.setObjectName("muted")
        self.freq_range_edit = _RangeCommitEdit(
            "{1G}+{[2G,3G]}-{[2.5G]}",
            "Frequency range set expression (Enter to apply, blank = all)",
            self._apply_freq_range,
        )
        self.freq_range_edit.set_committed(self._freq_range.raw)
        self.power_range_edit = _RangeCommitEdit(
            "{12}+{[15,17]}-{[16]}",
            "Power range set expression in dBm (Enter to apply, blank = all)",
            self._apply_power_range,
        )
        self.power_range_edit.set_committed(self._power_range.raw)
        rng_grid.addWidget(fl, 0, 0)
        rng_grid.addWidget(self.freq_range_edit, 0, 1)
        rng_grid.addWidget(pl, 1, 0)
        rng_grid.addWidget(self.power_range_edit, 1, 1)
        lay.addLayout(rng_grid)
        self.range_status = QLabel("")
        self.range_status.setObjectName("muted")
        lay.addWidget(self.range_status)

        # MATH для токов
        sep2 = QFrame()
        sep2.setObjectName("sep")
        lay.addWidget(sep2)
        m_row = QHBoxLayout()
        m_row.setSpacing(6)
        ml = QLabel("MATH f(x,y)")
        ml.setObjectName("sec")
        self.math_edit = _RangeCommitEdit(
            "e.g. y * 1000, y - 0.01",
            "Math formula applied to current values y (A) at frequency x (Hz); Enter to apply",
            self._apply_math,
        )
        self.math_edit.set_committed(self._math_expr)
        m_row.addWidget(ml)
        m_row.addWidget(self.math_edit, 1)
        lay.addLayout(m_row)

        # ANALYSIS для токов
        sep3 = QFrame()
        sep3.setObjectName("sep")
        lay.addWidget(sep3)
        an_hdr = QHBoxLayout()
        an_lbl = QLabel("ANALYSIS")
        an_lbl.setObjectName("sec")
        all_b = QPushButton("ALL")
        all_b.setObjectName("chip")
        all_b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        all_b.setCursor(Qt.CursorShape.PointingHandCursor)
        all_b.setToolTip("Enable all analysis metrics")
        all_b.clicked.connect(lambda: self.set_analysis_metrics(list(ANALYSIS_KEYS)))
        none_b = QPushButton("NONE")
        none_b.setObjectName("chip")
        none_b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        none_b.setCursor(Qt.CursorShape.PointingHandCursor)
        none_b.setToolTip("Disable all analysis metrics")
        none_b.clicked.connect(lambda: self.set_analysis_metrics([]))
        an_hdr.addWidget(an_lbl)
        an_hdr.addStretch()
        an_hdr.addWidget(all_b)
        an_hdr.addWidget(none_b)
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
            cb.setChecked(key in self.analysis_metrics)
            cb.toggled.connect(lambda checked, k=key: self._toggle_analysis(k, checked))
            self.analysis_checks[key] = cb
            an_grid.addWidget(cb, idx // 2, idx % 2)
        lay.addLayout(an_grid)
        lay.addStretch(1)
        return w

    def set_process_text(self, text: str) -> None:
        self._process_override = str(text or "").strip()
        if self._process_override:
            self.process_label.setText(self._process_override)

    def set_settings_open(self, on: bool) -> None:
        on = bool(on)
        self.stack.setCurrentIndex(1 if on else 0)
        self.settings_button.blockSignals(True)
        self.settings_button.setChecked(on)
        self.settings_button.blockSignals(False)

    def _on_settings_toggled(self) -> None:
        self.set_settings_open(self.settings_button.isChecked())

    def clear_data(self) -> None:
        self.monitor.clear_psu_table(self.proxy, self.channel)

    def _apply_freq_range(self) -> None:
        raw = self.freq_range_edit.text().strip()
        try:
            self._freq_range = parse_range_expr(raw, "freq")
        except ValueError as exc:
            self.freq_range_edit.set_invalid(True)
            self.range_status.setObjectName("err")
            self.range_status.setText(str(exc))
            return
        self.freq_range_edit.set_committed(self._freq_range.raw, invalid=False)
        self.range_status.setObjectName("muted")
        self.range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_FREQ_RANGE/{self.panel_key}", self._freq_range.raw)
        self.refresh()

    def _apply_power_range(self) -> None:
        raw = self.power_range_edit.text().strip()
        try:
            self._power_range = parse_range_expr(raw, "power")
        except ValueError as exc:
            self.power_range_edit.set_invalid(True)
            self.range_status.setObjectName("err")
            self.range_status.setText(str(exc))
            return
        self.power_range_edit.set_committed(self._power_range.raw, invalid=False)
        self.range_status.setObjectName("muted")
        self.range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_POWER_RANGE/{self.panel_key}", self._power_range.raw)
        self.refresh()

    def _apply_math(self) -> None:
        expr = self.math_edit.text().strip()
        if not expr:
            self._math_expr = ""
            self.math_edit.set_committed("", invalid=False)
        else:
            ok, err = validate_math_expr(expr)
            if not ok:
                self.math_edit.set_invalid(True)
                self.range_status.setObjectName("err")
                self.range_status.setText(err)
                return
            self._math_expr = expr
            self.math_edit.set_committed(expr, invalid=False)
            self.range_status.setObjectName("muted")
            self.range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_MATH/{self.panel_key}", self._math_expr)
        self.refresh()

    def set_analysis_metrics(self, metrics: list[str] | tuple[str, ...]) -> None:
        cleaned = [k for k in ANALYSIS_KEYS if k in set(metrics)]
        self.analysis_metrics = cleaned
        for k, cb in self.analysis_checks.items():
            cb.blockSignals(True)
            cb.setChecked(k in cleaned)
            cb.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_ANALYSIS/{self.panel_key}", json.dumps(cleaned))
        self.refresh()

    def _toggle_analysis(self, key: str, on: bool) -> None:
        cur = set(self.analysis_metrics)
        if on:
            cur.add(key)
        else:
            cur.discard(key)
        self.set_analysis_metrics(list(cur))

    def _rf_proxy_key(self, rf_proxy) -> str:
        return f"{self.monitor.name(rf_proxy)}:{rf_proxy.address}"

    def _apply_saved_rf_source(self) -> None:
        if not self._rf_choice_key:
            self.monitor.set_psu_rf_source(self.proxy, self.channel, None)
            self.rf_button.setText("RF: AUTO")
            return
        for p in self.monitor.rf_source_proxies():
            if self._rf_proxy_key(p) == self._rf_choice_key:
                self.monitor.set_psu_rf_source(self.proxy, self.channel, p)
                self.rf_button.setText(f"RF: {self.monitor.name(p)}")
                return
        self.monitor.set_psu_rf_source(self.proxy, self.channel, None)
        self.rf_button.setText("RF: AUTO")

    def set_rf_source(self, rf_proxy) -> None:
        self._rf_choice_key = "" if rf_proxy is None else self._rf_proxy_key(rf_proxy)
        if self.settings is not None:
            if self._rf_choice_key:
                self.settings.setValue(f"MAIN/CURR_RF/{self.panel_key}", self._rf_choice_key)
            else:
                self.settings.remove(f"MAIN/CURR_RF/{self.panel_key}")
        self.monitor.set_psu_rf_source(self.proxy, self.channel, rf_proxy)
        self.rf_button.setText("RF: AUTO" if rf_proxy is None else f"RF: {self.monitor.name(rf_proxy)}")
        self.refresh()

    def _open_rf_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        cur_pref = self.monitor.psu_rf_source.get((self.proxy, self.channel))
        act_auto = menu.addAction("AUTO")
        act_auto.setCheckable(True)
        act_auto.setChecked(cur_pref is None)
        act_auto.triggered.connect(lambda _=False: self.set_rf_source(None))
        for p in self.monitor.rf_source_proxies():
            act = menu.addAction(f"{self.monitor.name(p)} ({short_address(p.address)})")
            act.setCheckable(True)
            act.setChecked(cur_pref is p)
            act.triggered.connect(lambda _=False, rp=p: self.set_rf_source(rp))
        menu.popup(self.rf_button.mapToGlobal(self.rf_button.rect().bottomLeft()))

    def set_unit(self, unit: str) -> None:
        if unit not in CURRENT_UNITS:
            return
        self.current_table.set_unit(unit)
        for u, b in self.unit_buttons.items():
            b.blockSignals(True)
            b.setChecked(u == unit)
            b.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_UNIT/{self.panel_key}", unit)
        self.refresh()

    def refresh(self, _activity=None) -> None:
        self.title.setText(f"{self.monitor.name(self.proxy)} CH{self.channel}")
        self._apply_saved_rf_source()
        src = self.monitor.psu_source(self.proxy, self.channel)
        cols, rows = self.monitor.psu_matrix(self.proxy, self.channel)
        cols, rows = filter_pm_matrix_by_ranges(cols, rows, self._freq_range, self._power_range)
        if self._math_expr:
            m_rows: list[tuple[float, dict[float | None, float]]] = []
            for f, vals in rows:
                nv = {p: eval_math_expr(self._math_expr, f, v) for p, v in vals.items()}
                nv = {p: v for p, v in nv.items() if math.isfinite(v)}
                if nv:
                    m_rows.append((f, nv))
            rows = m_rows
        cur_f = None if src is None else src.freq
        cur_p = None if src is None or src.power is None else round(float(src.power), 3)
        self.current_table.set_matrix(cols, rows, cur_f, cur_p)

        # Анализ по данным таблицы токов
        if self.analysis_metrics and rows:
            xs = [f for f, vals in rows for _v in vals.values()]
            ys = [v for _f, vals in rows for v in vals.values()]
            tr = Trace(tuple(xs), tuple(ys), "Hz", "A", name="I", uniform=False)
            stats = analyze_trace(tr)
            tokens = format_analysis(stats, self.analysis_metrics, "Hz", "A")
            if tokens:
                self.analysis_label.setText("  ·  ".join(tokens))
                self.analysis_label.show()
            else:
                self.analysis_label.hide()
        else:
            self.analysis_label.hide()

        if self._process_override:
            self.process_label.setText(self._process_override)
            return
        if self.proxy.state is not ConnectionState.CONNECTED:
            self.process_label.setText("OFF")
            return
        ch_on = self.proxy.value(f"ch{self.channel}.output")
        cur_i = _num(self.proxy.value(f"ch{self.channel}.meas_i"))
        cur_v = _num(self.proxy.value(f"ch{self.channel}.meas_v"))
        if ch_on is False:
            self.process_label.setText("OFF")
        elif cur_i is not None:
            v_txt = f"  {cur_v:.2f} V" if cur_v is not None else ""
            self.process_label.setText(f"{_format_current(cur_i, self.current_table.unit)}{v_txt}")
        else:
            self.process_label.setText("NO DATA")


class ScenarioChip(IndicatorButton):
    """Идущие сценарии координатора: имя и прогресс; клик - окно прибора, которым сценарий меряет."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chip")
        self.setProperty("tile", True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(68)
        self.setFixedHeight(TILE_MIN_HEIGHT)
        self.runs: list = []

    def indicated(self) -> bool:
        return bool(self.runs)

    def show_runs(self, runs: list) -> None:
        self.runs = list(runs)
        self.setVisible(bool(self.runs))
        if not self.runs:
            return
        run = self.runs[0]
        more = f" +{len(self.runs) - 1}" if len(self.runs) > 1 else ""
        self.setText(f"{run.name}{more} {run.done}/{run.total}" if run.total else f"{run.name}{more} RUN")
        who = " + ".join(d.name for d in run.devices.values())
        self.setToolTip("\n".join(f"{r.name}: {' + '.join(d.name for d in r.devices.values())}"
                                  + (f"  {r.done}/{r.total} {r.text}" if r.total else "") for r in self.runs)
                        + f"\nClick - open the {who.split(' + ')[-1]} window")
        self.update()


class LivePanel(Panel):
    """Живые данные одного приёмника: облегчённый заголовок (имя, процесс, настройки / очистка / закрыть)
    и встроенная страница настроек вместо графика (SCALE, MEM, MATH, ANALYSIS и параметры прибора)."""

    move_requested = pyqtSignal(object, int)
    swap_requested = pyqtSignal(str, str)
    dock_requested = pyqtSignal(str, str, str)
    maximize_requested = pyqtSignal(object)
    context_menu_requested = pyqtSignal(object, QPoint)
    close_requested = pyqtSignal(object)

    def __init__(self, proxy, monitor: ActivityMonitor, parent=None, settings: QSettings | None = None):
        self.proxy = proxy
        self.monitor = monitor
        self.settings = settings
        self.profile = profile_name(proxy)
        self.panel_key = f"{monitor.name(proxy)}:{proxy.address}"
        self._drag_pos: QPoint | None = None
        self._maximized = False
        self._process_override: str = ""
        self._active_plot_count = 0
        self._selected_plot_idx = 0
        self._plots_sig: tuple | None = None
        self._plot_root: QWidget | None = None
        self._plot_splitters: list[tuple[str, GraphSplitter]] = []

        saved_vna_split = bool(settings.value(f"MAIN/VNA_SPLIT/{self.panel_key}", False, type=bool)) if settings else False
        saved_plot_mode = str(settings.value(f"MAIN/PLOT_LAYOUT/{self.panel_key}", PLOT_LAYOUT_MODES[0])
                              or PLOT_LAYOUT_MODES[0]) if settings else PLOT_LAYOUT_MODES[0]
        self.plot_layout_mode = saved_plot_mode if saved_plot_mode in PLOT_LAYOUT_MODES else PLOT_LAYOUT_MODES[0]
        self._vna_split_traces = saved_vna_split

        saved_unit = str(settings.value(f"MAIN/PM_UNIT/{self.panel_key}", "dBm") or "dBm") if settings else "dBm"
        if saved_unit not in PM_UNITS:
            saved_unit = "dBm"
        saved_pm_plot = bool(settings.value(f"MAIN/PM_SHOW_PLOT/{self.panel_key}", True, type=bool)) if settings else True
        saved_pm_table = bool(settings.value(f"MAIN/PM_SHOW_TABLE/{self.panel_key}", True, type=bool)) if settings else True
        if not saved_pm_plot and not saved_pm_table:
            saved_pm_plot = True
        self._pm_show_plot = saved_pm_plot
        self._pm_show_table = saved_pm_table

        # Диапазоны частот и мощностей ваттметра: {f1}+{[f2,f3],[f4,f5]}-{[f6]}
        raw_pm_f = str(settings.value(f"MAIN/PM_FREQ_RANGE/{self.panel_key}", "", type=str) or "").strip() if settings else ""
        raw_pm_p = str(settings.value(f"MAIN/PM_POWER_RANGE/{self.panel_key}", "", type=str) or "").strip() if settings else ""
        try:
            self.pm_freq_range: RangeSet = parse_range_expr(raw_pm_f, "freq")
        except ValueError:
            self.pm_freq_range = RangeSet("", (), "freq")
        try:
            self.pm_power_range: RangeSet = parse_range_expr(raw_pm_p, "power")
        except ValueError:
            self.pm_power_range = RangeSet("", (), "power")

        # Совместимость со старым CSV API (если вызывается из тестов)
        self.pm_csv_path: str = ""
        self.pm_csv_points: list[tuple[float, float | None]] = []
        self.pm_csv_enabled: bool = False

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Облегчённый заголовок: имя прибора + процесс над графиком + 3 иконки (настройки, clear, закрыть)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("heading")
        self.title.setToolTip(f"{monitor.name(proxy)}: {proxy.name} ({short_address(proxy.address)})")
        self.process_label = QLabel()
        self.process_label.setObjectName("heading")
        self.process_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.level = self.process_label  # совместимость

        self.settings_button = _icon_btn(
            "settings",
            "Open / close settings for this graph (scale, memory, math, analysis)",
            self._on_settings_toggled,
            checkable=True,
        )
        self.clear_button = _icon_btn(
            "clear",
            "Clear measurement data / table / stored memory",
            self.clear_data,
        )
        self.close_button = _icon_btn(
            "close",
            "Hide this measurement panel",
            lambda: self.close_requested.emit(self.proxy),
        )
        head.addWidget(self.title)
        head.addWidget(self.process_label, 1)
        head.addWidget(self.settings_button)
        head.addWidget(self.clear_button)
        head.addWidget(self.close_button)
        lay.addLayout(head)

        # Стек: страница 0 — график / таблица, страница 1 — встроенные настройки вместо графика
        self.stack = QStackedWidget()
        self._content_page = QWidget()
        c_lay = QVBoxLayout(self._content_page)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(0)

        self._plots_host = QWidget()
        self.plots_box = QVBoxLayout(self._plots_host)
        self.plots_box.setContentsMargins(0, 0, 0, 0)
        self.plots_box.setSpacing(0)
        c_lay.addWidget(self._plots_host, 1)

        self.plots: list[LivePlot] = []
        self.power_table: PowerTable | None = None      # не ``table``: так Panel зовёт своё содержимое
        self.pm_plot: PowerCurvePlot | None = None
        self.pm_splitter: QSplitter | None = None
        if self.profile == "power_meter":
            self.pm_splitter = QSplitter(Qt.Orientation.Horizontal)
            self.pm_splitter.setChildrenCollapsible(False)
            self.pm_splitter.setHandleWidth(8)
            self.pm_plot = mark_growing(PowerCurvePlot(unit=saved_unit, min_height=PLOT_MIN_HEIGHT))
            self.pm_plot.bind_settings(self.settings, f"MAIN/PM_PLOT/{self.panel_key}")
            self.pm_plot.settings_requested.connect(lambda: self.set_settings_open(True))
            self.pm_plot.settings_changed.connect(self._sync_settings_view)
            self.power_table = PowerTable(unit=saved_unit)
            self.pm_splitter.addWidget(self.pm_plot)
            self.pm_splitter.addWidget(self.power_table)
            self.pm_splitter.setStretchFactor(0, 1)
            self.pm_splitter.setStretchFactor(1, 1)
            self._restore_pm_splitter()
            self.pm_splitter.splitterMoved.connect(lambda _p, _i: self._save_pm_splitter())
            c_lay.addWidget(self.pm_splitter, 1)
            self._plots_host.hide()

        self.stack.addWidget(self._content_page)
        self._settings_body = self._build_settings_view(saved_unit)
        self._settings_scroll = _settings_scroll_area(self._settings_body)
        self.stack.addWidget(self._settings_scroll)
        lay.addWidget(self.stack, 1)

        super().__init__(body, parent=parent)
        self.setMinimumWidth(220)
        self._sync_settings_view()

    @staticmethod
    def _tool_btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _build_settings_view(self, saved_unit: str) -> QWidget:
        w = QWidget()
        w.setObjectName("panelSettingsBody")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        hdr = QLabel("GRAPH SETTINGS")
        hdr.setObjectName("sec")
        done_btn = QPushButton("DONE")
        done_btn.setObjectName("chip")
        done_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.setToolTip("Return to graph view")
        done_btn.clicked.connect(lambda: self.set_settings_open(False))
        top.addWidget(hdr)
        top.addStretch()
        top.addWidget(done_btn)
        lay.addLayout(top)

        # Выбор графика (если у ВАЦ несколько графиков)
        self._plot_selector_host = QWidget()
        self._plot_selector_lay = QHBoxLayout(self._plot_selector_host)
        self._plot_selector_lay.setContentsMargins(0, 0, 0, 0)
        self._plot_selector_lay.setSpacing(4)
        self._plot_selector_host.hide()
        lay.addWidget(self._plot_selector_host)

        # Опции VNA (разделение трасс и сетка графиков)
        self.split_traces_cb: QCheckBox | None = None
        self.plot_layout_buttons: dict[str, QPushButton] = {}
        if self.profile == "network_analyzer":
            vna_row = QHBoxLayout()
            vna_row.setSpacing(8)
            self.split_traces_cb = QCheckBox("Split each trace")
            self.split_traces_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.split_traces_cb.setCursor(Qt.CursorShape.PointingHandCursor)
            self.split_traces_cb.setChecked(self._vna_split_traces)
            self.split_traces_cb.setToolTip("Show each VNA trace in its own graph")
            self.split_traces_cb.toggled.connect(self.set_split_traces)
            vna_row.addWidget(self.split_traces_cb)
            vna_row.addStretch()
            for mode in PLOT_LAYOUT_MODES:
                b = QPushButton(mode)
                b.setObjectName("chip")
                b.setCheckable(True)
                b.setChecked(mode == self.plot_layout_mode)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip(f"Arrange VNA plots as {mode}")
                b.clicked.connect(lambda _=False, m=mode: self.set_plot_layout_mode(m))
                self.plot_layout_buttons[mode] = b
                vna_row.addWidget(b)
            lay.addLayout(vna_row)
            sep = QFrame()
            sep.setObjectName("sep")
            lay.addWidget(sep)

        # Опции PM (график/таблица, единицы, множества диапазонов частот и мощностей)
        self.pm_plot_cb: QCheckBox | None = None
        self.pm_table_cb: QCheckBox | None = None
        self.pm_unit_buttons: dict[str, QPushButton] = {}
        self.pm_freq_range_edit: _RangeCommitEdit | None = None
        self.pm_power_range_edit: _RangeCommitEdit | None = None
        self.pm_range_status: QLabel | None = None
        if self.profile == "power_meter":
            view_row = QHBoxLayout()
            view_row.setSpacing(10)
            self.pm_plot_cb = QCheckBox("Graph")
            self.pm_plot_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.pm_plot_cb.setCursor(Qt.CursorShape.PointingHandCursor)
            self.pm_plot_cb.setChecked(self._pm_show_plot)
            self.pm_plot_cb.setToolTip("Show / hide power meter frequency-power curve graph")
            self.pm_plot_cb.toggled.connect(self.set_pm_show_plot)
            self.pm_table_cb = QCheckBox("Table")
            self.pm_table_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.pm_table_cb.setCursor(Qt.CursorShape.PointingHandCursor)
            self.pm_table_cb.setChecked(self._pm_show_table)
            self.pm_table_cb.setToolTip("Show / hide power meter frequency × power table")
            self.pm_table_cb.toggled.connect(self.set_pm_show_table)
            view_row.addWidget(self.pm_plot_cb)
            view_row.addWidget(self.pm_table_cb)
            view_row.addStretch()
            lay.addLayout(view_row)

            unit_row = QHBoxLayout()
            unit_row.setSpacing(4)
            ul = QLabel("UNIT")
            ul.setObjectName("muted")
            unit_row.addWidget(ul)
            for u in PM_UNITS:
                b = QPushButton(u)
                b.setObjectName("chip")
                b.setCheckable(True)
                b.setChecked(u == saved_unit)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip(_PM_UNIT_LABELS[u])
                b.clicked.connect(lambda _=False, unit=u: self.set_pm_unit(unit))
                self.pm_unit_buttons[u] = b
                unit_row.addWidget(b)
            unit_row.addStretch()
            lay.addLayout(unit_row)

            sep_r = QFrame()
            sep_r.setObjectName("sep")
            lay.addWidget(sep_r)

            rng_hdr = QHBoxLayout()
            rl = QLabel("RANGES  {f1}+{[f2,f3],[f4,f5]}-{[f6]}")
            rl.setObjectName("sec")
            clr_rng = QPushButton("RESET")
            clr_rng.setObjectName("chip")
            clr_rng.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            clr_rng.setCursor(Qt.CursorShape.PointingHandCursor)
            clr_rng.setToolTip("Clear frequency and power range filters (show all points)")
            clr_rng.clicked.connect(self._clear_pm_ranges)
            rng_hdr.addWidget(rl)
            rng_hdr.addStretch()
            rng_hdr.addWidget(clr_rng)
            lay.addLayout(rng_hdr)

            rng_grid = QGridLayout()
            rng_grid.setHorizontalSpacing(6)
            rng_grid.setVerticalSpacing(4)
            fl = QLabel("FREQ")
            fl.setObjectName("muted")
            pl = QLabel("POWER")
            pl.setObjectName("muted")
            self.pm_freq_range_edit = _RangeCommitEdit(
                "{1G}+{[2G,3G],[4G,5G]}-{[2.5G]}",
                "Frequency range set expression (SI units allowed; Enter to apply, blank = all)",
                self._apply_pm_freq_range,
            )
            self.pm_freq_range_edit.set_committed(self.pm_freq_range.raw)
            self.pm_power_range_edit = _RangeCommitEdit(
                "{12}+{[15,17]}-{[16]}",
                "Source power range set expression in dBm (Enter to apply, blank = all)",
                self._apply_pm_power_range,
            )
            self.pm_power_range_edit.set_committed(self.pm_power_range.raw)
            rng_grid.addWidget(fl, 0, 0)
            rng_grid.addWidget(self.pm_freq_range_edit, 0, 1)
            rng_grid.addWidget(pl, 1, 0)
            rng_grid.addWidget(self.pm_power_range_edit, 1, 1)
            lay.addLayout(rng_grid)

            self.pm_range_status = QLabel("")
            self.pm_range_status.setObjectName("muted")
            lay.addWidget(self.pm_range_status)

            sep_p = QFrame()
            sep_p.setObjectName("sep")
            lay.addWidget(sep_p)

        # Единая панель SCALE + MEM + MATH + ANALYSIS для всех графиков (VNA, SA, PM)
        self.plot_settings_widget = PlotSettingsWidget(self._current_settings_plot(), w)
        lay.addWidget(self.plot_settings_widget)
        lay.addStretch(1)
        return w

    def _current_settings_plot(self):
        if self.profile == "power_meter":
            if self._active_plot_count > 0 and self.plots:
                return self.plots[0]
            return self.pm_plot
        active = self._active_plots()
        if not active:
            return self._plot(0) if self.profile in ("network_analyzer", "spectrum_analyzer") else None
        idx = max(0, min(self._selected_plot_idx, len(active) - 1))
        return active[idx]

    def _rebuild_plot_selector(self) -> None:
        while self._plot_selector_lay.count():
            item = self._plot_selector_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        active = self._active_plots()
        if self.profile != "network_analyzer" or len(active) < 2:
            self._plot_selector_host.hide()
            return
        self._selected_plot_idx = max(0, min(self._selected_plot_idx, len(active) - 1))
        lbl = QLabel("PLOT")
        lbl.setObjectName("muted")
        self._plot_selector_lay.addWidget(lbl)
        for i, pl in enumerate(active):
            names = ", ".join(n for n, _t in pl.traces if n) or f"Plot {i + 1}"
            b = QPushButton(f"{i + 1}: {names}")
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setChecked(i == self._selected_plot_idx)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"Edit settings for plot {i + 1}")
            b.clicked.connect(lambda _=False, idx=i: self._select_settings_plot(idx))
            self._plot_selector_lay.addWidget(b)
        self._plot_selector_lay.addStretch()
        self._plot_selector_host.show()

    def _select_settings_plot(self, idx: int) -> None:
        self._selected_plot_idx = max(0, int(idx))
        self._rebuild_plot_selector()
        self._sync_settings_view()

    def _sync_settings_view(self) -> None:
        if not hasattr(self, "plot_settings_widget"):
            return
        target = self._current_settings_plot()
        self.plot_settings_widget.set_plot(target)

    def set_settings_open(self, on: bool, *, plot_idx: int | None = None) -> None:
        on = bool(on)
        if plot_idx is not None:
            self._selected_plot_idx = max(0, int(plot_idx))
        if on:
            self._rebuild_plot_selector()
            self._sync_settings_view()
        self.stack.setCurrentIndex(1 if on else 0)
        self.settings_button.blockSignals(True)
        self.settings_button.setChecked(on)
        self.settings_button.blockSignals(False)

    def _on_settings_toggled(self) -> None:
        self.set_settings_open(self.settings_button.isChecked())

    def set_process_text(self, text: str) -> None:
        self._process_override = str(text or "").strip()
        if self._process_override:
            self.process_label.setText(self._process_override)

    def clear_data(self) -> None:
        """Кнопка очистки (иконка корзины) в заголовке панели."""
        if self.profile == "power_meter":
            self.monitor.clear_pm_table(self.proxy)
            if self.pm_plot is not None:
                self.pm_plot.clear_memory()
            for p in self.plots:
                p.clear_memory()
        else:
            for p in self._active_plots():
                p.clear_memory()
                if p.math_mode != "OFF":
                    p.set_math("OFF")
        self._sync_settings_view()

    def set_maximized(self, on: bool) -> None:
        self._maximized = bool(on)

    # ------------------------------------------------------------ внутренние графики панели
    def set_split_traces(self, on: bool) -> None:
        on = bool(on)
        self._vna_split_traces = on
        if self.split_traces_cb is not None and self.split_traces_cb.isChecked() != on:
            self.split_traces_cb.blockSignals(True)
            self.split_traces_cb.setChecked(on)
            self.split_traces_cb.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/VNA_SPLIT/{self.panel_key}", on)
        if self.profile == "network_analyzer":
            self._refresh_vna(self.monitor.activity(self.proxy))

    def set_plot_layout_mode(self, mode: str) -> None:
        if mode not in PLOT_LAYOUT_MODES or mode == self.plot_layout_mode:
            return
        self._save_plot_splitters()
        self.plot_layout_mode = mode
        for m, b in self.plot_layout_buttons.items():
            b.blockSignals(True)
            b.setChecked(m == mode)
            b.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PLOT_LAYOUT/{self.panel_key}", mode)
        self._plots_sig = None
        self._use_plots(self._active_plot_count)

    def equalize_plots(self) -> None:
        for _tag, sp in self._plot_splitters:
            if not sip.isdeleted(sp):
                sp.equalize()

    def _plot_splitter_key(self, tag: str, count: int) -> str:
        return f"MAIN/PLOT_SPLITTER/{self.panel_key}/{self.plot_layout_mode}/{tag}_{count}"

    def _save_plot_splitters(self) -> None:
        if self.settings is None:
            return
        for tag, sp in self._plot_splitters:
            if not sip.isdeleted(sp):
                w = sp.current_weights()
                if w is not None:
                    self.settings.setValue(self._plot_splitter_key(tag, len(w)), json.dumps([round(x, 5) for x in w]))

    def _load_plot_weights(self, tag: str, count: int, default: list[float]) -> list[float]:
        if self.settings is None:
            return default
        raw = self.settings.value(self._plot_splitter_key(tag, count), "", type=str)
        try:
            vals = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            vals = []
        if isinstance(vals, list) and len(vals) == count and all(isinstance(x, (int, float)) and x > 0 for x in vals):
            return [float(x) for x in vals]
        return default

    def _build_plot_node(self, node: PaneNode, path: str = "root", parent: QWidget | None = None) -> QWidget:
        if isinstance(node, PaneLeaf):
            plot = self.plots[int(node.key)]
            plot.show()
            return plot
        orient = Qt.Orientation.Horizontal if node.orientation == "H" else Qt.Orientation.Vertical
        sp = GraphSplitter(orient, parent or self._plots_host)
        for i, child in enumerate(node.children):
            sp.addWidget(self._build_plot_node(child, f"{path}_{i}", sp))
            sp.setStretchFactor(i, 1)
        sp.apply_weights(self._load_plot_weights(path, len(node.children), node.weights))
        sp.splitterMoved.connect(lambda _p, _i: self._save_plot_splitters())
        sp.equalized.connect(self._save_plot_splitters)
        self._plot_splitters.append((path, sp))
        sp.show()
        return sp

    def _plot(self, i: int) -> LivePlot:
        while len(self.plots) <= i:
            idx = len(self.plots)
            p = mark_growing(LivePlot(min_height=PLOT_MIN_HEIGHT))
            p.bind_settings(self.settings, f"MAIN/PLOT/{self.panel_key}/{idx}")
            p.settings_changed.connect(self._sync_settings_view)
            p.settings_requested.connect(lambda ix=idx: self.set_settings_open(True, plot_idx=ix))
            p.setParent(self._plots_host)
            p.hide()
            self.plots.append(p)
        return self.plots[i]

    def _use_plots(self, n: int) -> None:
        self._active_plot_count = max(0, int(n))
        if self._active_plot_count == 0:
            self._plots_host.hide()
            for p in self.plots:
                p.hide()
            return
        self._plot(self._active_plot_count - 1)
        self._plots_host.show()
        sig = (self.plot_layout_mode, self._active_plot_count)
        if sig == self._plots_sig:
            for i, p in enumerate(self.plots):
                p.setVisible(i < self._active_plot_count)
            return
        self._save_plot_splitters()
        self._plots_sig = sig
        for p in self.plots:
            p.setParent(self._plots_host)
            p.hide()
        if self._plot_root is not None:
            self.plots_box.removeWidget(self._plot_root)
            if isinstance(self._plot_root, GraphSplitter):
                self._plot_root.deleteLater()
            self._plot_root = None
        self._plot_splitters.clear()
        if self._active_plot_count == 1:
            root: QWidget = self.plots[0]
            root.show()
        else:
            tree = build_preset_tree(self.plot_layout_mode, [str(i) for i in range(self._active_plot_count)])
            root = self._build_plot_node(tree) if tree is not None else self.plots[0]
        self._plot_root = root
        self.plots_box.addWidget(root, 1)
        if self.stack.currentIndex() == 1:
            self._rebuild_plot_selector()
            self._sync_settings_view()

    def _active_plots(self) -> list[LivePlot]:
        return self.plots[:self._active_plot_count]

    def refresh(self, a: Activity) -> None:
        self.title.setText(a.name)
        if not a.connected:
            status_txt = "OFF"
        else:
            status_txt = ""
        if self.profile == "network_analyzer":
            vna_txt = self._refresh_vna(a)
            if a.connected:
                status_txt = vna_txt
        elif self.profile == "spectrum_analyzer":
            sa_txt = self._refresh_sa(a)
            if a.connected:
                status_txt = sa_txt
        elif self.profile == "power_meter":
            pm_txt = self._refresh_pm(a)
            if a.connected:
                status_txt = pm_txt
        # Процесс выводится строго над графиком (активный сценарий в приоритете над статусом измерения)
        self.process_label.setText(self._process_override or status_txt)
        if self.stack.currentIndex() == 1:
            self._sync_settings_view()

    def _vna_markers_for_items(self, items: list[tuple[str, Trace]], total_traces: int) -> list[tuple[str, float, float, int]]:
        """Собрать активные маркеры для трасс одного графика ВАЦ."""
        markers_out: list[tuple[str, float, float, int]] = []
        multi = len(items) > 1
        sel_raw = str(self.proxy.value("trace.sel") or "")
        for idx, (tname, tr) in enumerate(items):
            meta = tr.meta or {}
            raw_list = meta.get("markers")
            if raw_list is not None:
                for entry in raw_list:
                    if len(entry) < 2:
                        continue
                    mn, mx = entry[0], _num(entry[1])
                    my = _num(entry[2]) if len(entry) > 2 else None
                    if mx is None:
                        continue
                    if my is None and len(tr) > 0:
                        my = _num(tr.y[nearest_index(tr, mx)])
                    if my is None:
                        continue
                    lbl = f"{tname} M{mn}" if (multi and tname) else f"M{mn}"
                    markers_out.append((lbl, mx, my, idx))
            else:
                # Фоллбек для трасс без meta["markers"]: одиночный маркер mark.* или marker.*
                tr_id = f"{meta.get('channel', 1)}:{meta.get('name', '')}"
                is_sel = bool(sel_raw) and sel_raw == tr_id
                if (is_sel or total_traces == 1) and self.proxy.value("mark.state") is True:
                    mx = _num(self.proxy.value("mark.x"))
                    my = _num(self.proxy.value("mark.y"))
                    if mx is not None:
                        if my is None and len(tr) > 0:
                            my = _num(tr.y[nearest_index(tr, mx)])
                        if my is not None:
                            mn = self.proxy.value("mark.n") or 1
                            lbl = f"{tname} M{mn}" if (multi and tname) else f"M{mn}"
                            markers_out.append((lbl, mx, my, idx))
                elif idx == 0 and total_traces == 1 and self.proxy.value("marker.state") is True:
                    mx = _num(self.proxy.value("marker.freq"))
                    my = _num(self.proxy.value("marker.value"))
                    if mx is not None:
                        if my is None and len(tr) > 0:
                            my = _num(tr.y[nearest_index(tr, mx)])
                        if my is not None:
                            lbl = f"{tname} M1" if (multi and tname) else "M1"
                            markers_out.append((lbl, mx, my, idx))
        return markers_out

    def _refresh_vna(self, a: Activity) -> str:
        traces = self.monitor.vna_traces(self.proxy)
        split_each = self._vna_split_traces
        if split_each:
            groups_list: list[tuple[tuple[float, float, float] | None, list]] = [
                (_vna_scale(tr), [(name, tr)]) for name, tr in traces
            ]
        else:
            groups: dict[tuple, tuple[tuple[float, float, float] | None, list]] = {}
            for name, tr in traces:                      # одна шкала и единица -> один график
                scale = _vna_scale(tr)
                scale_key = None if scale is None else (round(scale[0], 12), round(scale[1], 12), round(scale[2], 12))
                key = (tr.y_unit, scale_key)
                entry = groups.get(key)
                if entry is None:
                    groups[key] = (scale, [(name, tr)])
                else:
                    entry[1].append((name, tr))
            groups_list = list(groups.values())
        for i, (scale, items) in enumerate(groups_list):
            plot = self._plot(i)
            plot.set_scale(scale, divs_y=DEVICE_DIVS_Y if scale is not None else None)
            plot.set_traces(items)
            plot.set_markers(self._vna_markers_for_items(items, len(traces)))
            lim_segs = []
            lim_fail: bool | None = None
            for _tname, tr in items:
                meta = tr.meta or {}
                segs = meta.get("limits")
                if segs:
                    lim_segs.extend(segs)
                lf = meta.get("limit_fail")
                if lf is True:
                    lim_fail = True
                elif lf is False and lim_fail is None:
                    lim_fail = False
            plot.set_limit_lines(lim_segs, fail=lim_fail if lim_segs else None)
        self._use_plots(max(1, len(groups_list)))
        if not groups_list:
            plot = self._plot(0)
            plot.set_scale(None)
            plot.set_traces([])
            plot.set_markers([])
            plot.set_limit_lines([], fail=None)
        if self.proxy.value("gen.state") is True:
            port = self.proxy.value("gen.port")
            pt = f" P{port}" if port in (1, 2) else ""
            rf_off = " (RF OFF)" if self.proxy.value("rf.output") is False else ""
            return f"CW{pt} {si_format(self.proxy.value('freq.cw'), 'Hz', 6)}{rf_off}"
        return f"{len(traces)} TRC" if traces else "NO TRC"

    def _refresh_sa(self, a: Activity) -> str:
        tr = self.proxy.value("trace")
        plot = self._plot(0)
        self._use_plots(1)
        rlev = _num(self.proxy.value("ref_level"))
        sdiv = _num(self.proxy.value("scale_div"))
        if isinstance(tr, Trace) and tr.meta:
            if rlev is None:
                rlev = _num(tr.meta.get("ref_level"))
            if sdiv is None:
                sdiv = _num(tr.meta.get("scale_div"))
        if rlev is not None and sdiv is not None and sdiv > 0:
            plot.set_scale((rlev - DEVICE_DIVS_Y * sdiv, rlev, sdiv), divs_y=DEVICE_DIVS_Y)
        else:
            plot.set_scale(None)
        plot.set_traces([("", tr)] if isinstance(tr, Trace) else [])
        present = bool(a.signal)
        if present and a.level is not None and isinstance(tr, Trace) and len(tr) > 0:
            x, _y = tr.peak()
            plot.set_marker(x, a.level)
        else:
            plot.set_marker(None)
        if a.level is not None:
            where = f" @ {si_format(a.freq, 'Hz', 6)}" if a.freq is not None and present else ""
            return f"{_dbm(a.level)}{where}" if present else f"NOISE {_dbm(a.floor)}"
        return ""

    # ------------------------------------------------------------ настройки ваттметра (диапазоны, вид, единицы)
    def _save_pm_splitter(self) -> None:
        if self.settings is None or self.pm_splitter is None or sip.isdeleted(self.pm_splitter):
            return
        sizes = self.pm_splitter.sizes()
        if len(sizes) == 2 and all(s > 0 for s in sizes):
            self.settings.setValue(f"MAIN/PM_VIEW_SPLIT/{self.panel_key}", json.dumps(sizes))

    def _restore_pm_splitter(self) -> None:
        if self.pm_splitter is None:
            return
        raw = self.settings.value(f"MAIN/PM_VIEW_SPLIT/{self.panel_key}", "", type=str) if self.settings else ""
        try:
            sizes = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            sizes = []
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(x, int) and x > 0 for x in sizes):
            self.pm_splitter.setSizes(sizes)
        else:
            self.pm_splitter.setSizes([560, 440])

    def set_pm_show_plot(self, on: bool) -> None:
        if self.pm_plot is None:
            return
        on = bool(on)
        if not on and not self._pm_show_table:
            self._pm_show_table = True
            if self.pm_table_cb is not None:
                self.pm_table_cb.blockSignals(True)
                self.pm_table_cb.setChecked(True)
                self.pm_table_cb.blockSignals(False)
            if self.settings is not None:
                self.settings.setValue(f"MAIN/PM_SHOW_TABLE/{self.panel_key}", True)
        self._pm_show_plot = on
        if self.pm_plot_cb is not None and self.pm_plot_cb.isChecked() != on:
            self.pm_plot_cb.blockSignals(True)
            self.pm_plot_cb.setChecked(on)
            self.pm_plot_cb.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_SHOW_PLOT/{self.panel_key}", on)
        self.refresh(self.monitor.activity(self.proxy))

    def set_pm_show_table(self, on: bool) -> None:
        if self.power_table is None:
            return
        on = bool(on)
        if not on and not self._pm_show_plot:
            self._pm_show_plot = True
            if self.pm_plot_cb is not None:
                self.pm_plot_cb.blockSignals(True)
                self.pm_plot_cb.setChecked(True)
                self.pm_plot_cb.blockSignals(False)
            if self.settings is not None:
                self.settings.setValue(f"MAIN/PM_SHOW_PLOT/{self.panel_key}", True)
        self._pm_show_table = on
        if self.pm_table_cb is not None and self.pm_table_cb.isChecked() != on:
            self.pm_table_cb.blockSignals(True)
            self.pm_table_cb.setChecked(on)
            self.pm_table_cb.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_SHOW_TABLE/{self.panel_key}", on)
        self.refresh(self.monitor.activity(self.proxy))

    def set_pm_freq_range(self, expr: str) -> bool:
        """Задать диапазон частот строкой ``{f1}+{[f2,f3],[f4,f5]}-{[f6]}``."""
        try:
            rs = parse_range_expr(expr, "freq")
        except ValueError as exc:
            if self.pm_freq_range_edit is not None:
                self.pm_freq_range_edit.set_invalid(True)
            if self.pm_range_status is not None:
                self.pm_range_status.setObjectName("err")
                self.pm_range_status.setText(f"Freq: {exc}")
            return False
        self.pm_freq_range = rs
        if self.pm_freq_range_edit is not None:
            self.pm_freq_range_edit.set_committed(rs.raw, invalid=False)
        if self.pm_range_status is not None:
            self.pm_range_status.setObjectName("muted")
            self.pm_range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_FREQ_RANGE/{self.panel_key}", rs.raw)
        self.refresh(self.monitor.activity(self.proxy))
        return True

    def set_pm_power_range(self, expr: str) -> bool:
        """Задать диапазон мощностей строкой ``{p1}+{[p2,p3]}-{[p4]}`` (dBm)."""
        try:
            rs = parse_range_expr(expr, "power")
        except ValueError as exc:
            if self.pm_power_range_edit is not None:
                self.pm_power_range_edit.set_invalid(True)
            if self.pm_range_status is not None:
                self.pm_range_status.setObjectName("err")
                self.pm_range_status.setText(f"Power: {exc}")
            return False
        self.pm_power_range = rs
        if self.pm_power_range_edit is not None:
            self.pm_power_range_edit.set_committed(rs.raw, invalid=False)
        if self.pm_range_status is not None:
            self.pm_range_status.setObjectName("muted")
            self.pm_range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_POWER_RANGE/{self.panel_key}", rs.raw)
        self.refresh(self.monitor.activity(self.proxy))
        return True

    def _apply_pm_freq_range(self) -> None:
        if self.pm_freq_range_edit is not None:
            self.set_pm_freq_range(self.pm_freq_range_edit.text())

    def _apply_pm_power_range(self) -> None:
        if self.pm_power_range_edit is not None:
            self.set_pm_power_range(self.pm_power_range_edit.text())

    def _clear_pm_ranges(self) -> None:
        self.set_pm_freq_range("")
        self.set_pm_power_range("")

    # Совместимость со старыми тестами CSV
    def set_pm_csv_enabled(self, on: bool) -> None:
        self.pm_csv_enabled = bool(on) and bool(self.pm_csv_points)
        self.refresh(self.monitor.activity(self.proxy))

    def set_pm_csv_points(self, points: list[tuple[float, float | None]], *,
                          csv_path: str | None = None, enable: bool | None = None) -> None:
        self.pm_csv_points = [(float(f), None if p is None else round(float(p), 3)) for f, p in points]
        if csv_path is not None:
            self.pm_csv_path = str(csv_path)
        if enable is not None:
            self.pm_csv_enabled = bool(enable) and bool(self.pm_csv_points)
        elif not self.pm_csv_points:
            self.pm_csv_enabled = False
        self.refresh(self.monitor.activity(self.proxy))

    def load_pm_csv(self, path, *, enable: bool = True) -> list[tuple[float, float | None]]:
        pts = load_pm_points_csv(path)
        self.set_pm_csv_points(pts, csv_path=str(path), enable=enable)
        return pts

    def set_pm_unit(self, unit: str) -> None:
        if self.power_table is None or unit not in PM_UNITS:
            return
        self.power_table.set_unit(unit)
        if self.pm_plot is not None:
            self.pm_plot.set_unit(unit)
        for u, b in self.pm_unit_buttons.items():
            b.blockSignals(True)
            b.setChecked(u == unit)
            b.blockSignals(False)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_UNIT/{self.panel_key}", unit)
        self.refresh(self.monitor.activity(self.proxy))

    def _refresh_pm(self, a: Activity) -> str:
        """Временной режим (TRAC ON) - трасса во времени; иначе - график кривых по мощностям и/или таблица."""
        tr = self.proxy.value("trace")
        timed = self.proxy.value("trace.state") is True and isinstance(tr, Trace)
        if timed:
            plot = self._plot(0)
            plot.x_range = None
            scale, divs_y, y_unit, y_transform = pm_time_scale(self.proxy, tr, self.settings)
            plot.set_scale(scale, divs_y=divs_y, y_unit=y_unit, y_transform=y_transform)
            plot.set_traces([("", tr)])
        self._use_plots(1 if timed else 0)
        show_normal = not timed
        if self.pm_splitter is not None:
            self.pm_splitter.setVisible(show_normal)
        if self.pm_plot is not None:
            self.pm_plot.setVisible(show_normal and self._pm_show_plot)
        if self.power_table is not None:
            self.power_table.setVisible(show_normal and self._pm_show_table)
        if show_normal:
            cols, rows = self.monitor.pm_matrix(self.proxy)
            planned: set[tuple[float, float | None]] = set()
            target_freqs: list[float] = []
            if self.pm_csv_enabled and self.pm_csv_points:
                cols, rows, planned = filter_pm_matrix(cols, rows, self.pm_csv_points)
                target_freqs = sorted({f for f, _p in self.pm_csv_points})
            else:
                cols, rows = filter_pm_matrix_by_ranges(cols, rows, self.pm_freq_range, self.pm_power_range)
            cur_p = None if a.source is None or a.source.power is None else round(float(a.source.power), 3)
            self.power_table.set_matrix(cols, rows, _num(self.proxy.value("freq")), cur_p, planned_cells=planned)
            if self.pm_plot is not None:
                self.pm_plot.set_matrix(cols, rows, unit=self.power_table.unit, target_freqs=target_freqs)
        u = self.power_table.unit if self.power_table is not None else "dBm"
        if a.level is None:
            return "WAIT TRIG" if self.proxy.value("meas.armed") is True else "NO DATA"
        if u == "dB":
            src_p = None if a.source is None else a.source.power
            return _format_power(a.level, "dB", src_p) if src_p is not None else _dbm(a.level)
        return _format_power(a.level, u)


class MainWindow(Window):
    """Главное окно лаборатории: плитки приборов и живые данные того, что измеряет."""

    SETTINGS_GROUP = "MAIN"
    TABS = ("LIVE", "SET")

    def __init__(self, settings: QSettings, monitor: ActivityMonitor | None = None, parent=None,
                 coordinator: Coordinator | None = None):
        super().__init__(settings, parent)
        self.setWindowTitle("LAB")
        self.monitor = monitor or ActivityMonitor(parent=self)
        self.coord = coordinator if coordinator is not None else COORD
        self.quit_on_close = False
        #: вызывается при закрытии главного окна до выхода (окна приборов ещё в прежнем виде)
        self.before_quit: list = []
        self.tiles: dict[object, InstrumentTile] = {}
        self.panels: dict[object, LivePanel] = {}
        self.current_panels: dict[tuple[object, int], CurrentPanel] = {}
        self.current_controls: dict[str, QCheckBox] = {}
        self.restore_chips: dict[str, QPushButton] = {}
        self.display_rules: dict[str, dict[str, Any]] = {}
        self.display_controls: dict[str, tuple[QCheckBox, QCheckBox, SettingField]] = {}
        self._root_widget: QWidget | None = None
        self._splitters: list[tuple[str, PaneSplit, GraphSplitter]] = []
        self._active_tree: PaneNode | None = None
        self._maximized_key: str | None = None
        self._arranged_sig: tuple | None = None
        self._visible_proxies: set = set()
        self._measuring_proxies: set = set()
        self._dirty_panels: set = set()
        self._dirty_current: set[tuple[object, int]] = set()
        self._dirty_vis = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(UI_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._flush_panels)
        self.panel_order: list[str] = self._load_list(f"{self.SETTINGS_GROUP}/PANEL_ORDER")
        self.hidden_panels: set[str] = set(self._load_list(f"{self.SETTINGS_GROUP}/HIDDEN_PANELS"))
        self._custom_tree: PaneNode | None = self._load_split_tree()
        self.enabled_current_panels: set[str] = set(self._load_list(f"{self.SETTINGS_GROUP}/ENABLED_CURRENT_TABLES"))
        saved_mode = str(settings.value(f"{self.SETTINGS_GROUP}/LAYOUT_MODE", LAYOUT_MODES[0]) or LAYOUT_MODES[0])
        if saved_mode == CUSTOM_LAYOUT_MODE and self._custom_tree is not None:
            self.layout_mode = CUSTOM_LAYOUT_MODE
        elif saved_mode in LAYOUT_MODES:
            self.layout_mode = saved_mode
        else:
            self.layout_mode = LAYOUT_MODES[0]

        # Заголовок окна: эмблема sinc слева, кнопки приборов (VNA, SA, PM...) вместо надписи LAB,
        # справа - фишка активного сценария и системные кнопки (_ / ⛶ / ×)
        self.titleBar.title_label.hide()
        _tile_qss = render_qss("""
            QWidget#tilesHost { background: transparent; border: none; }
            InstrumentTile, ScenarioChip {
                background-color: $UI_COLOR_BACKGROUND;
                color: $UI_COLOR_TEXT_MUTED;
                border: ${UI_BORDER_WIDTH_PX}px solid #3c3c3c;
                border-radius: ${UI_BORDER_RADIUS_PX}px;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                padding: 0 10px;
            }
            InstrumentTile:hover, ScenarioChip:hover {
                background-color: $UI_COLOR_INVERT_HOVER;
                color: $UI_COLOR_TEXT_INVERSE;
                border-color: $UI_COLOR_INVERT_HOVER;
                font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            }
            InstrumentTile[wstate="error"] {
                color: $UI_COLOR_ERROR;
            }
        """)
        self.titleBar.setStyleSheet(WindowBar.STYLE_SHEET + "\n" + _tile_qss)
        self.emblem = SincEmblem(self.titleBar)
        self._tiles_host = QWidget(self.titleBar)
        self._tiles_host.setObjectName("tilesHost")
        self._tiles_host.setStyleSheet(_tile_qss)
        self.tiles_layout = QHBoxLayout(self._tiles_host)
        self.tiles_layout.setContentsMargins(0, 0, 0, 0)
        self.tiles_layout.setSpacing(4)
        self.titleBar.insert_widget(self.emblem, 0)
        self.titleBar.insert_widget(self._tiles_host, 1)

        self.scenario_chip = ScenarioChip(self.titleBar)
        self.scenario_chip.clicked.connect(self._open_scenario)
        self.scenario_chip.hide()
        self.titleBar.insert_control_widget(self.scenario_chip)

        # во весь экран: кнопка в заголовке правее «свернуть» и F11
        self.full_button = CustomFullButton()
        self.full_button.setToolTip("Full screen (F11)")
        self.full_button.clicked.connect(self.toggle_full_screen)
        self.titleBar.insert_control_widget(self.full_button, after_minimize=True)
        self._full_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        self._full_key.activated.connect(self.toggle_full_screen)
        self._esc_key = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_key.activated.connect(self._exit_maximize)

        self.restore_layout = QHBoxLayout()
        self.restore_layout.setContentsMargins(0, 0, 0, 0)
        self.restore_layout.setSpacing(4)
        self.layout_button = QPushButton(self.layout_mode)
        self.layout_button.setObjectName("chip")
        self.layout_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.layout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.layout_button.setMinimumHeight(28)
        self.layout_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.layout_button.setToolTip(
            "Split screen layout: 1 COL, 2 COL, ROW, MAIN L, MAIN T (or custom SPLIT by dragging panel headers to edges). "
            "Click - next preset, right click - choose"
        )
        self.layout_button.clicked.connect(self._cycle_layout_mode)
        self.layout_button.customContextMenuRequested.connect(
            lambda pos: self._open_split_menu(self.layout_button.mapToGlobal(pos))
        )
        self.split_menu_button = QPushButton("⊞")
        self.split_menu_button.setObjectName("chip")
        self.split_menu_button.setProperty("tight", True)
        self.split_menu_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.split_menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.split_menu_button.setMinimumWidth(30)
        self.split_menu_button.setMinimumHeight(28)
        self.split_menu_button.setToolTip("Choose screen split preset, equalize dividers, or reset custom split")
        self.split_menu_button.clicked.connect(
            lambda: self._open_split_menu(self.split_menu_button.mapToGlobal(QPoint(0, self.split_menu_button.height())))
        )
        self.all_button = QPushButton("ALL")
        self.all_button.setObjectName("chip")
        self.all_button.setCheckable(True)
        self.all_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.setMinimumHeight(28)
        self.all_button.setToolTip("Show all receivers, also those without data now")
        self.all_button.setChecked(settings.value(f"{self.SETTINGS_GROUP}/SHOW_ALL", False, type=bool))
        self.all_button.toggled.connect(self._on_all)
        self.dock_button = QPushButton("DOCK")
        self.dock_button.setObjectName("chip")
        self.dock_button.setCheckable(True)
        self.dock_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dock_button.setMinimumHeight(28)
        self.dock_button.setToolTip("Show / hide the instrument dock panel (attach or detach instrument windows)")
        self.dock_button.clicked.connect(self._on_dock_button)
        self.current_button = QPushButton("CURR")
        self.current_button.setObjectName("chip")
        self.current_button.setCheckable(True)
        self.current_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.current_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.current_button.setMinimumHeight(28)
        self.current_button.setToolTip("Choose power supply channels to show current tables vs frequency and RF power")
        self.current_button.clicked.connect(self._open_current_menu)
        self.current_button.hide()

        self.fields: dict[str, SettingField] = {}
        by_name: dict[str, PanelLine] = {}
        for label, name, unit, scale, tip in SETTINGS:
            saved = settings.value(f"{self.SETTINGS_GROUP}/THRESHOLDS/{name}", None)
            if saved is not None:
                try:
                    setattr(self.monitor.th, name, float(saved))
                except (TypeError, ValueError):
                    pass
            f = SettingField(unit, scale)
            f.setToolTip(tip)
            f.setMinimumWidth(130)                  # поле тянется с окном, как в окнах приборов
            f.show_value(getattr(self.monitor.th, name))
            f.returnPressed.connect(lambda n=name: self._apply_setting(n))
            f.focus_committed.connect(lambda n=name: self._apply_setting(n))
            self.fields[name] = f
            line = PanelLine(label, f)
            line.setToolTip(tip)
            by_name[name] = line
        settings_table = PanelTable(
            (by_name["sa_margin_db"], by_name["pm_floor_dbm"]),
            (by_name["freq_tol_hz"], by_name["freq_tol_rel"]),
            by_name["hold_s"],
        )
        settings_table.build(130)
        settings_body = QWidget()
        s_lay = QVBoxLayout(settings_body)
        s_lay.setContentsMargins(0, 0, 0, 0)
        s_lay.setSpacing(10)
        s_lay.addWidget(settings_table)
        graphs_head = QLabel("DISPLAYED GRAPHS")
        graphs_head.setObjectName("heading")
        s_lay.addWidget(graphs_head)
        self.display_host = QWidget()
        self.display_layout = QVBoxLayout(self.display_host)
        self.display_layout.setContentsMargins(0, 0, 0, 0)
        self.display_layout.setSpacing(4)
        s_lay.addWidget(self.display_host)
        self.current_head = QLabel("CURRENT TABLES (PSU)")
        self.current_head.setObjectName("heading")
        self.current_head.hide()
        s_lay.addWidget(self.current_head)
        self.current_host = QWidget()
        self.current_settings_layout = QVBoxLayout(self.current_host)
        self.current_settings_layout.setContentsMargins(0, 0, 0, 0)
        self.current_settings_layout.setSpacing(4)
        self.current_host.hide()
        s_lay.addWidget(self.current_host)
        self.settings_panel = Panel(settings_body)
        self.settings_panel.setStyleSheet(
            Panel.STYLE_SHEET + "\n" + checkbox_qss("Panel ") + render_qss("""
            Panel QCheckBox { color: $UI_COLOR_TEXT; }
        """))
        self.settings_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.idle = QLabel("NOTHING IS MEASURING")
        self.idle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle.setStyleSheet("color: #8a8a8a; padding: 24px;")
        self.live_page = QWidget()
        live = QVBoxLayout(self.live_page)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)

        # Двухпанельный экран LIVE:
        # Слева (50%, без прокрутки) — активный прибор по видеоуровню (или VNA по умолчанию);
        # Справа (50%, вертикальная прокрутка) — все остальные выбранные графики и таблицы.
        self._active_host = QWidget()
        self._active_host.setObjectName("activeHost")
        self.active_layout = QVBoxLayout(self._active_host)
        self.active_layout.setContentsMargins(0, 0, 0, 0)
        self.active_layout.setSpacing(0)

        self._right_host = QWidget()
        self._right_host.setObjectName("rightHost")
        self.right_layout = QVBoxLayout(self._right_host)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(8)

        self.right_scroll = QScrollArea()
        self.right_scroll.setObjectName("rightScroll")
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.right_scroll.setWidget(self._right_host)

        self.live_splitter = GraphSplitter(Qt.Orientation.Horizontal)
        self.live_splitter.addWidget(self._active_host)
        self.live_splitter.addWidget(self.right_scroll)
        self.live_splitter.setStretchFactor(0, 1)
        self.live_splitter.setStretchFactor(1, 1)
        self.live_splitter.splitterMoved.connect(lambda _p, _i: self._save_live_split())
        self.live_splitter.equalized.connect(self._save_live_split)
        self.live_splitter.setStyleSheet(render_qss("""
            QScrollArea#rightScroll, QWidget#activeHost, QWidget#rightHost { background: $UI_COLOR_BACKGROUND; border: none; }
            QSplitter { background: $UI_COLOR_BACKGROUND; }
            QSplitter::handle { background: transparent; }
            QSplitter::handle:horizontal { width: 8px; }
        """) + "\n" + scrollbar_qss("QScrollArea#rightScroll "))
        self._restore_live_split()

        # Алиасы для совместимости
        self._live_host = self._active_host
        self.live_layout = self.active_layout
        self.live_scroll = self.live_splitter
        self._active_panel_key: str | None = None
        self._right_panel_keys: list[str] = []

        live.addWidget(self.live_splitter, 1)
        live.addWidget(self.idle)

        # вкладки: LIVE - что измеряется, SET - пороги; справа - восстановление панелей, DOCK, CURR, ALL
        self.tabs = PTabBar()
        for title in self.TABS:
            self.tabs.addTab(title)
        self.tabs.setTabToolTip(1, "Thresholds and displayed graphs settings")
        self.layout_button.hide()
        self.split_menu_button.hide()
        tabs_bar = QWidget()
        tabs_lay = QHBoxLayout(tabs_bar)
        tabs_lay.setContentsMargins(0, 0, 0, 0)
        tabs_lay.setSpacing(6)
        tabs_lay.addWidget(self.tabs, 1)
        tabs_lay.addLayout(self.restore_layout)
        tabs_lay.addWidget(self.dock_button)
        tabs_lay.addWidget(self.current_button)
        tabs_lay.addWidget(self.all_button)
        self.tabs_panel = Panel(tabs_bar)
        self.tabs_panel.setStyleSheet(
            Panel.STYLE_SHEET + render_qss('\nQMenu#psmenu::separator { height: 1px; background: $UI_COLOR_BORDER; margin: 4px 6px; }')
        )
        self.tabs_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # Отдельное окно приборной панели (независимо от окна графиков): 2 вкладки (FAV / ALL), горизонтальный скролл
        self.instrument_dock_window = InstrumentDockWindow(settings)
        self.instrument_dock = self.instrument_dock_window.dock
        self.instrument_dock.changed.connect(self._on_dock_changed)
        self.instrument_dock_window.visibility_changed.connect(self._on_dock_visibility_changed)
        self.instrument_dock_window.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.tabs_panel)
        lay.addWidget(self.live_page, 1)            # растянули окно - графики растут (Алексей 2026-11-18)
        lay.addWidget(self.settings_panel)
        lay.addStretch(0)                            # SET: поля сверху, пустое место снизу
        self.tabs.currentChanged.connect(self.set_page)
        self.set_page(settings.value(f"{self.SETTINGS_GROUP}/TAB", 0, type=int))

        self.monitor.roster_changed.connect(self._rebuild)
        self.monitor.changed.connect(self._on_changed)
        WINDOW_EVENTS.changed.connect(self._on_window_event)    # окно прибора открыли / закрыли, ошибка, шкала TIME
        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            sig.connect(self._on_runs)
        self._on_runs()
        self.setMinimumWidth(560)
        self.recall_size(QSize(860, 760))
        self._rebuild()
        self.restore_docked_windows()

    # ------------------------------------------------------------ состав
    def _load_list(self, key: str) -> list[str]:
        raw = self.settings.value(key, "", type=str)
        try:
            items = json.loads(raw) if raw else []
            return [str(x) for x in items] if isinstance(items, list) else []
        except (TypeError, ValueError):
            return []

    def _save_list(self, key: str, items) -> None:
        self.settings.setValue(key, json.dumps(list(items)))

    def _load_split_tree(self) -> PaneNode | None:
        raw = self.settings.value(f"{self.SETTINGS_GROUP}/SPLIT_TREE", "", type=str)
        if not raw:
            return None
        try:
            return tree_from_dict(json.loads(raw))
        except (TypeError, ValueError):
            return None

    def _save_split_tree(self) -> None:
        data = tree_to_dict(self._custom_tree)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/SPLIT_TREE", json.dumps(data) if data is not None else "")

    def _panel_key(self, proxy) -> str:
        panel = self.panels.get(proxy)
        if panel is not None:
            return panel.panel_key
        return f"{self.monitor.name(proxy)}:{proxy.address}"

    def _rebuild(self) -> None:
        if sip.isdeleted(self):
            return
        self._save_splitters()
        proxies = self.monitor.proxies()
        for p in [p for p in self.tiles if p not in proxies]:
            self.tiles.pop(p).deleteLater()
        for p in [p for p in self.panels if p not in proxies]:
            panel = self.panels.pop(p)
            panel.setParent(None)
            panel.deleteLater()
        psu_ch_list = self.monitor.psu_channels()
        valid_curr = {(p, ch) for p, ch, _lbl in psu_ch_list}
        for key in [k for k in self.current_panels if k not in valid_curr]:
            panel = self.current_panels.pop(key)
            panel.setParent(None)
            panel.deleteLater()
        for i, p in enumerate(proxies):
            if p not in self.tiles:
                tile = InstrumentTile(p, self._tiles_host)
                tile.clicked.connect(lambda _=False, p=p: self.open_instrument(p))
                self.tiles[p] = tile
            self.tiles_layout.insertWidget(i, self.tiles[p])
        for p in self.monitor.receivers():
            if p not in self.panels:
                panel = LivePanel(p, self.monitor, settings=self.settings)
                panel.move_requested.connect(self.move_panel)
                panel.swap_requested.connect(self.swap_panels)
                panel.dock_requested.connect(self.dock_panel_to)
                panel.maximize_requested.connect(self.toggle_maximize_panel)
                panel.context_menu_requested.connect(self._open_panel_menu)
                panel.close_requested.connect(self.hide_panel)
                panel.hide()
                self.panels[p] = panel
                if panel.panel_key not in self.panel_order:
                    self.panel_order.append(panel.panel_key)
        for p, ch, _lbl in psu_ch_list:
            ck = (p, ch)
            if ck not in self.current_panels:
                cpanel = CurrentPanel(p, ch, self.monitor, settings=self.settings)
                cpanel.move_requested.connect(self.move_panel)
                cpanel.swap_requested.connect(self.swap_panels)
                cpanel.close_requested.connect(lambda pk: self.set_current_panel_enabled(pk, False))
                cpanel.hide()
                self.current_panels[ck] = cpanel
                if cpanel.panel_key not in self.panel_order:
                    self.panel_order.append(cpanel.panel_key)
        for p in proxies:
            self._update_tile(p)
            panel = self.panels.get(p)
            if panel is not None:
                act = self.monitor.activity(p)
                if act.measuring:
                    self._measuring_proxies.add(p)
                else:
                    self._measuring_proxies.discard(p)
                panel.refresh(act)
        for cpanel in self.current_panels.values():
            cpanel.refresh()
        self._dirty_panels.clear()
        self._dirty_current.clear()
        self._dirty_vis = False
        self._update_current_button()
        self._rebuild_display_settings()
        self._update_visibility()

    # ------------------------------------------------------------ обновление
    def _on_changed(self, proxy) -> None:
        if sip.isdeleted(self):
            return
        self._update_tile(proxy)
        if profile_name(proxy) == "power_supply":
            for ck, cpanel in self.current_panels.items():
                if ck[0] is proxy:
                    self._dirty_current.add(ck)
            if self._dirty_current and not self._refresh_timer.isActive():
                self._refresh_timer.start()
            return
        panel = self.panels.get(proxy)
        if panel is None:
            return
        self._dirty_panels.add(proxy)
        vis_changed = (proxy in self._visible_proxies) != self._should_show(proxy)
        meas_changed = (proxy in self._measuring_proxies) != self.monitor.activity(proxy).measuring
        visible_widgets = [p for p in self._ordered_all_panels() if self._panel_visible(p)]
        new_active = self._pick_active_panel(visible_widgets)
        active_changed = (new_active.panel_key if new_active is not None else None) != self._active_panel_key
        if vis_changed or meas_changed or active_changed:
            if vis_changed or active_changed:
                self._dirty_vis = True
            self._flush_panels()
        elif not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_panels(self) -> None:
        if sip.isdeleted(self):
            return
        dirty = list(self._dirty_panels)
        self._dirty_panels.clear()
        dirty_curr = list(self._dirty_current)
        self._dirty_current.clear()
        need_vis = self._dirty_vis
        self._dirty_vis = False
        for proxy in dirty:
            panel = self.panels.get(proxy)
            if panel is not None and not sip.isdeleted(panel):
                act = self.monitor.activity(proxy)
                if act.measuring:
                    self._measuring_proxies.add(proxy)
                else:
                    self._measuring_proxies.discard(proxy)
                panel.refresh(act)
        for ck in dirty_curr:
            cpanel = self.current_panels.get(ck)
            if cpanel is not None and not sip.isdeleted(cpanel):
                cpanel.refresh()
        if need_vis:
            self._update_visibility()

    def _on_window_event(self, proxy) -> None:
        if sip.isdeleted(self):
            return
        self._update_tile(proxy)
        panel = self.panels.get(proxy)
        if panel is not None and panel.profile == "power_meter":
            self._dirty_panels.add(proxy)
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()

    def _update_tile(self, proxy) -> None:
        """Плитка: имя прибора; индикатор - его окно открыто; красная - ошибка: индикатор ошибок окна горит
        (до CLEAR в журнале), связь не установилась или потеряна."""
        if sip.isdeleted(self):
            return
        tile = self.tiles.get(proxy)
        if tile is None or sip.isdeleted(tile):
            return
        name = self.monitor.name(proxy)
        w = window_for(proxy)
        window_open = w is not None and w.isVisible()
        lost = proxy.state is ConnectionState.ERROR or bool(getattr(proxy, "reconnecting", False))
        errors = w is not None and w.has_errors()
        if proxy.state is ConnectionState.CONNECTED:
            link = "connected"
        elif lost:
            link = "no connection, reconnecting" if getattr(proxy, "reconnecting", False) else "connection error"
        elif proxy.state is ConnectionState.CONNECTING:
            link = "connecting"
        else:
            link = "off"
        tip = [f"{name}: {proxy.name}", short_address(proxy.address), link]
        if errors:
            tip.append(w.error_indicator.toolTip())
        if w is not None and w.is_docked:
            state_txt = "Window is docked" if window_open else "Window is docked (hidden)"
        else:
            state_txt = "Window is open" if window_open else "Window is closed"
        tip.append(f"{state_txt}. Click - show the window")
        tile.show_state(name, window_open, errors or lost, "\n".join(tip))

    def _on_runs(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        runs = self.coord.runs()
        self.scenario_chip.show_runs(runs)
        # Пробросить текущий процесс сценария прямо в заголовок участвующих панелей
        proc_by_proxy: dict[object, str] = {}
        for run in runs:
            prog = f"{run.done}/{run.total}" if run.total else "RUN"
            detail = f" · {run.text}" if run.text else ""
            txt = f"{run.name} {prog}{detail}"
            for dev in (run.devices or {}).values():
                proc_by_proxy[dev.instrument] = txt
        for proxy, panel in self.panels.items():
            if not sip.isdeleted(panel):
                panel.set_process_text(proc_by_proxy.get(proxy, ""))
                if not proc_by_proxy.get(proxy):
                    panel.refresh(self.monitor.activity(proxy))
        for (proxy, _ch), cpanel in self.current_panels.items():
            if not sip.isdeleted(cpanel):
                cpanel.set_process_text(proc_by_proxy.get(proxy, ""))
                if not proc_by_proxy.get(proxy):
                    cpanel.refresh()

    def _open_scenario(self) -> None:
        runs = self.coord.runs()
        if runs and runs[0].devices:
            self.open_instrument(list(runs[0].devices.values())[-1].instrument)

    def set_page(self, index: int) -> None:
        """0 - LIVE (живые данные), 1 - SET (пороги)."""
        index = index if 0 <= index < len(self.TABS) else 0
        if self.tabs.currentIndex() != index:
            self.tabs.setCurrentIndex(index)            # вызовет set_page ещё раз
            return
        self.live_page.setVisible(index == 0)
        self.settings_panel.setVisible(index == 1)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/TAB", index)

    def _apply_setting(self, name: str) -> None:
        f = self.fields[name]
        try:
            value = f.parsed()
        except ValueError:
            f.show_value(getattr(self.monitor.th, name))       # мусор - вернуть как было
            return
        self.settings.setValue(f"{self.SETTINGS_GROUP}/THRESHOLDS/{name}", value)
        self.monitor.set_threshold(name, value)
        f.show_value(value)

    def _on_all(self, on: bool) -> None:
        self.settings.setValue(f"{self.SETTINGS_GROUP}/SHOW_ALL", bool(on))
        self._update_visibility()

    def _cycle_layout_mode(self) -> None:
        if self.layout_mode in LAYOUT_MODES:
            nxt = LAYOUT_MODES[(LAYOUT_MODES.index(self.layout_mode) + 1) % len(LAYOUT_MODES)]
        else:
            nxt = LAYOUT_MODES[0]
        self.set_layout_mode(nxt)

    def set_layout_mode(self, mode: str) -> None:
        if mode == CUSTOM_LAYOUT_MODE:
            if self._custom_tree is None:
                return
        elif mode not in LAYOUT_MODES:
            return
        if mode == self.layout_mode and self._maximized_key is None:
            return
        self._save_splitters()
        self._maximized_key = None
        self.layout_mode = mode
        self.layout_button.setText(mode)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/LAYOUT_MODE", mode)
        self._arranged_sig = None
        self._update_visibility()

    # ------------------------------------------------------------ отображение графиков (SET)
    @staticmethod
    def _default_display_level(profile: str) -> float:
        if profile == "spectrum_analyzer":
            return -50.0
        if profile == "power_meter":
            return -60.0
        return -40.0

    def _display_rule(self, proxy) -> dict[str, Any]:
        key = self._panel_key(proxy)
        rule = self.display_rules.get(key)
        if rule is not None:
            return rule
        prof = profile_name(proxy)
        base = f"{self.SETTINGS_GROUP}/DISPLAY/{key}"
        by_level = bool(self.settings.value(f"{base}/BY_LEVEL", False, type=bool))
        default_lvl = self._default_display_level(prof)
        raw_lvl = self.settings.value(f"{base}/LEVEL", None)
        try:
            lvl = float(raw_lvl) if raw_lvl not in (None, "") else default_lvl
            if not math.isfinite(lvl):
                lvl = default_lvl
        except (TypeError, ValueError):
            lvl = default_lvl
        rule = {"by_level": by_level, "level": lvl}
        self.display_rules[key] = rule
        return rule

    def _rebuild_display_settings(self) -> None:
        self.display_controls.clear()
        while self.display_layout.count():
            item = self.display_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        receivers = self._ordered_receivers()
        if not receivers:
            empty = QLabel("No receivers connected")
            empty.setStyleSheet("color: #8a8a8a;")
            self.display_layout.addWidget(empty)
            self._rebuild_current_settings()
            return
        for p in receivers:
            key = self._panel_key(p)
            name = self.monitor.name(p)
            prof = profile_name(p)
            unit = "dB" if prof == "network_analyzer" else "dBm"
            rule = self._display_rule(p)
            shown = key not in self.hidden_panels
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            lbl = QLabel(name)
            lbl.setObjectName("lineName")
            lbl.setFixedWidth(56)
            lbl.setToolTip(f"{name}: {p.name} ({short_address(p.address)})")
            show_cb = QCheckBox("Show")
            show_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            show_cb.setCursor(Qt.CursorShape.PointingHandCursor)
            show_cb.setChecked(shown)
            show_cb.setToolTip(f"Show {name} in LIVE (unchecked - always hidden)")
            level_cb = QCheckBox("Above level")
            level_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            level_cb.setCursor(Qt.CursorShape.PointingHandCursor)
            level_cb.setChecked(rule["by_level"])
            level_cb.setEnabled(shown)
            level_cb.setToolTip(f"Show {name} only when its measured signal level exceeds the threshold")
            level_edit = SettingField(unit, 1.0)
            level_edit.setMinimumWidth(110)
            level_edit.show_value(rule["level"])
            level_edit.setEnabled(shown and rule["by_level"])
            level_edit.setToolTip(f"Video / signal level threshold for {name} ({unit}): show when level >= this value")
            show_cb.toggled.connect(lambda checked, k=key: self.set_panel_shown(k, checked))
            level_cb.toggled.connect(lambda checked, k=key: self.set_panel_by_level(k, checked))
            level_edit.returnPressed.connect(lambda k=key: self._apply_display_level(k))
            level_edit.focus_committed.connect(lambda k=key: self._apply_display_level(k))
            rl.addWidget(lbl)
            rl.addWidget(show_cb)
            rl.addWidget(level_cb)
            rl.addWidget(level_edit, 1)
            self.display_layout.addWidget(row)
            self.display_controls[key] = (show_cb, level_cb, level_edit)
        self._rebuild_current_settings()

    # ------------------------------------------------------------ выбор таблиц токов ИП
    def _update_current_button(self) -> None:
        has_psu = bool(self.current_panels)
        self.current_button.setVisible(has_psu)
        if not has_psu:
            return
        active = sum(1 for cp in self.current_panels.values() if cp.panel_key in self.enabled_current_panels)
        self.current_button.setText(f"CURR ({active})" if active else "CURR")
        self.current_button.blockSignals(True)
        self.current_button.setChecked(active > 0)
        self.current_button.blockSignals(False)

    def _open_current_menu(self) -> None:
        self._update_current_button()
        if not self.current_panels:
            return
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for (p, ch), cp in self.current_panels.items():
            label = f"{self.monitor.name(p)} CH{ch}"
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(cp.panel_key in self.enabled_current_panels)
            act.triggered.connect(lambda checked=False, pk=cp.panel_key: self.set_current_panel_enabled(pk, checked))
        menu.addSeparator()
        act_all = menu.addAction("Show all")
        act_all.triggered.connect(self._show_all_current_panels)
        act_none = menu.addAction("Hide all")
        act_none.triggered.connect(self._hide_all_current_panels)
        menu.popup(self.current_button.mapToGlobal(self.current_button.rect().bottomLeft()))

    def _show_all_current_panels(self) -> None:
        for cp in self.current_panels.values():
            self.enabled_current_panels.add(cp.panel_key)
        self._persist_current_panels()

    def _hide_all_current_panels(self) -> None:
        for cp in self.current_panels.values():
            self.enabled_current_panels.discard(cp.panel_key)
        self._persist_current_panels()

    def set_current_panel_enabled(self, panel_key: str, enabled: bool) -> None:
        """Включить / выключить отдельную таблицу тока для выбранного канала ИП."""
        enabled = bool(enabled)
        if enabled:
            self.enabled_current_panels.add(panel_key)
        else:
            self.enabled_current_panels.discard(panel_key)
        self._persist_current_panels()

    def _persist_current_panels(self) -> None:
        self._save_list(f"{self.SETTINGS_GROUP}/ENABLED_CURRENT_TABLES", sorted(self.enabled_current_panels))
        for pk, cb in self.current_controls.items():
            if not sip.isdeleted(cb):
                cb.blockSignals(True)
                cb.setChecked(pk in self.enabled_current_panels)
                cb.blockSignals(False)
        self._update_current_button()
        for cp in self.current_panels.values():
            if cp.panel_key in self.enabled_current_panels:
                cp.refresh()
        self._update_visibility()

    def _rebuild_current_settings(self) -> None:
        self.current_controls.clear()
        while self.current_settings_layout.count():
            item = self.current_settings_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        has_psu = bool(self.current_panels)
        self.current_head.setVisible(has_psu)
        self.current_host.setVisible(has_psu)
        if not has_psu:
            return
        for (p, ch), cp in self.current_panels.items():
            pk = cp.panel_key
            label = f"{self.monitor.name(p)} CH{ch}"
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            lbl = QLabel(label)
            lbl.setObjectName("lineName")
            lbl.setFixedWidth(72)
            lbl.setToolTip(f"{label}: {p.name} ({short_address(p.address)})")
            cb = QCheckBox("Show current table (Freq × RF Power)")
            cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.setChecked(pk in self.enabled_current_panels)
            cb.setToolTip(f"Show a dedicated current table for {label} in LIVE")
            cb.toggled.connect(lambda checked, k=pk: self.set_current_panel_enabled(k, checked))
            rl.addWidget(lbl)
            rl.addWidget(cb)
            rl.addStretch(1)
            self.current_settings_layout.addWidget(row)
            self.current_controls[pk] = cb

    def set_panel_shown(self, key: str, shown: bool) -> None:
        """Включить / выключить отображение графика прибора (галочка Show в SET или × / + в LIVE)."""
        shown = bool(shown)
        changed = (key in self.hidden_panels) == shown
        if shown:
            self.hidden_panels.discard(key)
        else:
            self.hidden_panels.add(key)
        if changed:
            self._save_list(f"{self.SETTINGS_GROUP}/HIDDEN_PANELS", sorted(self.hidden_panels))
        ctrls = self.display_controls.get(key)
        if ctrls is not None:
            show_cb, level_cb, level_edit = ctrls
            if not sip.isdeleted(show_cb):
                show_cb.blockSignals(True)
                show_cb.setChecked(shown)
                show_cb.blockSignals(False)
                level_cb.setEnabled(shown)
                level_edit.setEnabled(shown and level_cb.isChecked())
        if changed:
            self._update_visibility()

    def set_panel_by_level(self, key: str, enabled: bool) -> None:
        """Включить / выключить показ графика только при превышении порога уровня."""
        enabled = bool(enabled)
        rule = self.display_rules.setdefault(key, {"by_level": False, "level": -50.0})
        rule["by_level"] = enabled
        self.settings.setValue(f"{self.SETTINGS_GROUP}/DISPLAY/{key}/BY_LEVEL", enabled)
        ctrls = self.display_controls.get(key)
        if ctrls is not None:
            show_cb, level_cb, level_edit = ctrls
            if not sip.isdeleted(level_cb):
                level_cb.blockSignals(True)
                level_cb.setChecked(enabled)
                level_cb.blockSignals(False)
                level_edit.setEnabled(show_cb.isChecked() and enabled)
        self._update_visibility()

    def set_panel_level(self, key: str, level: float) -> None:
        """Задать порог видеоуровня для показа графика (dBm / dB)."""
        lvl = float(level)
        if not math.isfinite(lvl):
            return
        rule = self.display_rules.setdefault(key, {"by_level": False, "level": lvl})
        rule["level"] = lvl
        self.settings.setValue(f"{self.SETTINGS_GROUP}/DISPLAY/{key}/LEVEL", lvl)
        ctrls = self.display_controls.get(key)
        if ctrls is not None and not sip.isdeleted(ctrls[2]):
            ctrls[2].show_value(lvl)
        self._update_visibility()

    def _apply_display_level(self, key: str) -> None:
        ctrls = self.display_controls.get(key)
        if ctrls is None or sip.isdeleted(ctrls[2]):
            return
        edit = ctrls[2]
        rule = self.display_rules.get(key)
        try:
            value = edit.parsed()
        except ValueError:
            if rule is not None:
                edit.show_value(rule["level"])
            return
        self.set_panel_level(key, value)

    def _receiver_level(self, proxy) -> float | None:
        """Текущий измеренный уровень сигнала приёмника (dBm для SA/PM, max dB для VNA)."""
        prof = profile_name(proxy)
        a = self.monitor.activity(proxy)
        if prof == "spectrum_analyzer":
            return a.level
        if prof == "power_meter":
            lvl = a.level
            tr = proxy.value("trace")
            if proxy.value("trace.state") is True and isinstance(tr, Trace) and len(tr) > 0:
                finite = [float(y) for y in tr.y if math.isfinite(y)]
                if finite:
                    peak = max(finite)
                    return peak if lvl is None else max(lvl, peak)
            return lvl
        if prof == "network_analyzer":
            peaks = []
            has_non_mag = False
            for n in range(1, 17):
                if not proxy.value(f"m{n}.enabled"):
                    continue
                tr = proxy.value(f"m{n}.trace")
                if not isinstance(tr, Trace) or len(tr) < 2:
                    continue
                mag = self.monitor.slot_magnitude_db(tr) if hasattr(self.monitor, "slot_magnitude_db") else None
                vals = mag if mag is not None else [float(y) for y in tr.y if math.isfinite(y)]
                finite = [float(v) for v in vals if math.isfinite(v)]
                if mag is None and (tr.meta or {}).get("format") in ("PHASE", "UPHASE", "GDELAY", "REAL", "IMAG"):
                    has_non_mag = True
                elif finite:
                    peaks.append(max(finite))
            if not peaks and not has_non_mag:
                for _name, tr in self.monitor.vna_traces(proxy):
                    finite = [float(y) for y in tr.y if math.isfinite(y)]
                    if finite:
                        peaks.append(max(finite))
            return math.inf if (has_non_mag and not peaks) else (max(peaks) if peaks else None)
        return None

    def hide_panel(self, proxy) -> None:
        """Закрыть (скрыть) панель измерения; вернуть можно чипом «+ ИМЯ» сверху или галочкой в SET."""
        self.set_panel_shown(self._panel_key(proxy), False)

    def show_panel(self, key: str) -> None:
        self.set_panel_shown(key, True)

    def move_panel(self, item, delta: int) -> None:
        """Сдвинуть панель в порядке показа среди видимых панелей."""
        key = item if isinstance(item, str) else self._panel_key(item)
        if self.layout_mode == CUSTOM_LAYOUT_MODE and self._active_tree is not None:
            vis_keys = leaf_order(self._active_tree)
        else:
            vis_keys = [p.panel_key for p in self._ordered_all_panels() if self._panel_visible(p)]
        if key not in vis_keys:
            return
        i = vis_keys.index(key)
        j = i + (-1 if delta < 0 else 1)
        if not 0 <= j < len(vis_keys):
            return
        self.swap_panels(vis_keys[i], vis_keys[j])

    def swap_panels(self, src_key: str, dst_key: str) -> None:
        if src_key == dst_key:
            return
        for k in (src_key, dst_key):
            if k not in self.panel_order:
                self.panel_order.append(k)
        i, j = self.panel_order.index(src_key), self.panel_order.index(dst_key)
        self.panel_order[i], self.panel_order[j] = self.panel_order[j], self.panel_order[i]
        self._save_list(f"{self.SETTINGS_GROUP}/PANEL_ORDER", self.panel_order)
        if self._custom_tree is not None:
            self._custom_tree = swap_panels_in_tree(self._custom_tree, src_key, dst_key)
            self._save_split_tree()
        self._arranged_sig = None
        self._update_visibility()

    def dock_panel_to(self, src_key: str, dst_key: str, zone: str) -> None:
        """Разделить область целевой панели (left / right / top / bottom) или обменять местами (center)."""
        if not src_key or not dst_key or src_key == dst_key or zone not in DROP_ZONES:
            return
        if zone == "center" and self.layout_mode != CUSTOM_LAYOUT_MODE:
            self.swap_panels(src_key, dst_key)
            return
        self._save_splitters()
        ordered_vis = [self._panel_key(p) for p in self._ordered_receivers() if self._should_show(p)]
        if self.layout_mode == CUSTOM_LAYOUT_MODE and self._custom_tree is not None:
            base = project_tree(self._custom_tree, ordered_vis)
            # Сохраним и скрытые узлы, если они уже были в дереве, чтобы не терять их позиции
            base_full = self._custom_tree
            new_tree = dock_panel(base_full, src_key, dst_key, zone)
            # Если какой-то из ключей был новым и отсутствовал в base_full, подстрахуемся проекцией
            if new_tree is None or src_key not in leaf_order(new_tree) or dst_key not in leaf_order(new_tree):
                new_tree = dock_panel(base, src_key, dst_key, zone)
        else:
            base = clone_tree(self._active_tree) if self._active_tree is not None else build_preset_tree(self.layout_mode, ordered_vis)
            new_tree = dock_panel(base, src_key, dst_key, zone)
        if new_tree is None:
            return
        self._custom_tree = new_tree
        self._maximized_key = None
        self.layout_mode = CUSTOM_LAYOUT_MODE
        self.layout_button.setText(CUSTOM_LAYOUT_MODE)
        order = leaf_order(new_tree)
        self.panel_order = order + [k for k in self.panel_order if k not in order]
        self._save_list(f"{self.SETTINGS_GROUP}/PANEL_ORDER", self.panel_order)
        self._save_split_tree()
        self.settings.setValue(f"{self.SETTINGS_GROUP}/LAYOUT_MODE", self.layout_mode)
        self._arranged_sig = None
        self._update_visibility()

    def toggle_maximize_panel(self, proxy) -> None:
        """Развернуть одну панель на всю область графиков (повторно - вернуть разделённый экран)."""
        key = self._panel_key(proxy)
        self.set_maximized_panel(None if self._maximized_key == key else key)

    def set_maximized_panel(self, key: str | None) -> None:
        if key == self._maximized_key:
            return
        self._save_splitters()
        self._maximized_key = key
        self._arranged_sig = None
        self._update_visibility()

    def _exit_maximize(self) -> None:
        if self._maximized_key is not None:
            self.set_maximized_panel(None)

    def equalize_splits(self) -> None:
        """Выровнять все делители экрана (и внутренних графиков панелей) поровну."""
        for _tag, node, sp in self._splitters:
            if not sip.isdeleted(sp):
                sp.equalize()
                node.weights = [1.0 / len(node.children)] * len(node.children)
        if self.layout_mode == CUSTOM_LAYOUT_MODE and self._custom_tree is not None:
            self._custom_tree = equalize_tree(self._custom_tree)
            self._save_split_tree()
        else:
            self._save_splitters()
        for panel in self.panels.values():
            if not sip.isdeleted(panel):
                panel.equalize_plots()

    def reset_split_layout(self) -> None:
        """Сбросить пользовательское разделение и пропорции, вернуться к базовой раскладке."""
        self._custom_tree = None
        self._maximized_key = None
        self._save_split_tree()
        self.settings.remove(f"{self.SETTINGS_GROUP}/SPLITTER_WEIGHTS")
        self.layout_mode = LAYOUT_MODES[0]
        self.layout_button.setText(self.layout_mode)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/LAYOUT_MODE", self.layout_mode)
        self._arranged_sig = None
        self._update_visibility()

    def _open_split_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self.strip_panel)
        menu.setObjectName("psmenu")
        for mode in LAYOUT_MODES:
            act = menu.addAction(layout_preset_icon(mode), f"{mode}   {LAYOUT_DESCRIPTIONS[mode]}")
            act.setCheckable(True)
            act.setChecked(self.layout_mode == mode and self._maximized_key is None)
            act.triggered.connect(lambda _=False, m=mode: self.set_layout_mode(m))
        if self._custom_tree is not None:
            act = menu.addAction(layout_preset_icon(CUSTOM_LAYOUT_MODE),
                                 f"{CUSTOM_LAYOUT_MODE}   {LAYOUT_DESCRIPTIONS[CUSTOM_LAYOUT_MODE]}")
            act.setCheckable(True)
            act.setChecked(self.layout_mode == CUSTOM_LAYOUT_MODE and self._maximized_key is None)
            act.triggered.connect(lambda _=False: self.set_layout_mode(CUSTOM_LAYOUT_MODE))
        menu.addSeparator()
        has_splits = bool(self._splitters) or any(p._active_plot_count >= 2 for p in self.panels.values())
        eq_act = menu.addAction("Equalize splits")
        eq_act.setEnabled(has_splits)
        eq_act.triggered.connect(self.equalize_splits)
        if self._maximized_key is not None:
            restore_act = menu.addAction("Restore split view (Esc)")
            restore_act.triggered.connect(self._exit_maximize)
        reset_act = menu.addAction("Reset split layout")
        reset_act.triggered.connect(self.reset_split_layout)
        menu.popup(global_pos)

    def _open_panel_menu(self, proxy, global_pos: QPoint) -> None:
        panel = self.panels.get(proxy)
        if panel is None:
            return
        src_key = panel.panel_key
        eligible = [p for p in self._ordered_receivers() if self._should_show(p)]
        others = [p for p in eligible if p is not proxy]
        menu = QMenu(panel)
        menu.setObjectName("psmenu")
        if self._maximized_key == src_key:
            act = menu.addAction("Restore split view")
        else:
            act = menu.addAction("Maximize panel")
            act.setEnabled(len(eligible) > 1)
        act.triggered.connect(lambda _=False, pr=proxy: self.toggle_maximize_panel(pr))
        if others:
            menu.addSeparator()
            for title, zone in (("Split left of", "left"), ("Split right of", "right"),
                                ("Split above", "top"), ("Split below", "bottom"),
                                ("Swap with", "center")):
                sub = menu.addMenu(title)
                sub.setObjectName("psmenu")
                for other in others:
                    dst_key = self._panel_key(other)
                    name = self.monitor.name(other)
                    a = sub.addAction(name)
                    a.triggered.connect(lambda _=False, s=src_key, d=dst_key, z=zone: self.dock_panel_to(s, d, z))
        menu.addSeparator()
        if self._splitters or panel._active_plot_count >= 2:
            eq = menu.addAction("Equalize splits")
            eq.triggered.connect(self.equalize_splits)
        hide_act = menu.addAction("Hide panel")
        hide_act.triggered.connect(lambda _=False, pr=proxy: self.hide_panel(pr))
        menu.popup(global_pos)

    def _ordered_receivers(self) -> list:
        rank = {k: i for i, k in enumerate(self.panel_order)}
        receivers = list(self.panels)
        return sorted(receivers, key=lambda p: (rank.get(self._panel_key(p), len(rank)), receivers.index(p)))

    def _ordered_all_panels(self) -> list:
        rank = {k: i for i, k in enumerate(self.panel_order)}
        items = list(self.panels.values()) + list(self.current_panels.values())
        return sorted(items, key=lambda p: (rank.get(p.panel_key, len(rank)), items.index(p)))

    def _panel_visible(self, panel) -> bool:
        if isinstance(panel, CurrentPanel):
            return panel.panel_key in self.enabled_current_panels
        return self._should_show(panel.proxy)

    def _is_eligible(self, proxy) -> bool:
        if self.all_button.isChecked():
            return True
        rule = self._display_rule(proxy)
        if rule["by_level"]:
            if not self.monitor.activity(proxy).measuring:
                return False
            lvl = self._receiver_level(proxy)
            return lvl is not None and lvl >= rule["level"]
        return True

    def _should_show(self, proxy) -> bool:
        return self._is_eligible(proxy) and self._panel_key(proxy) not in self.hidden_panels

    def _splitter_weights_key(self, tag: str, count: int) -> str:
        return f"{self.SETTINGS_GROUP}/SPLITTER_WEIGHTS/{self.layout_mode}/{tag}_{count}"

    def _load_preset_weights(self, tag: str, count: int, default: list[float]) -> list[float]:
        raw = self.settings.value(self._splitter_weights_key(tag, count), "", type=str)
        try:
            vals = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            vals = []
        if isinstance(vals, list) and len(vals) == count and all(isinstance(x, (int, float)) and x > 0 for x in vals):
            return [float(x) for x in vals]
        return default

    def _save_splitters(self) -> None:
        updated = False
        for tag, node, sp in self._splitters:
            if sip.isdeleted(sp):
                continue
            weights = sp.current_weights()
            if weights is None or len(weights) != len(node.children):
                continue
            node.weights = weights
            updated = True
            if self.layout_mode != CUSTOM_LAYOUT_MODE:
                self.settings.setValue(
                    self._splitter_weights_key(tag, len(weights)),
                    json.dumps([round(w, 5) for w in weights]),
                )
        if updated and self.layout_mode == CUSTOM_LAYOUT_MODE and self._custom_tree is not None and self._active_tree is not None:
            self._custom_tree = sync_weights_into_tree(self._custom_tree, self._active_tree)
            self._save_split_tree()

    def _build_split_widget(self, node: PaneNode, by_key: dict[str, LivePanel], path: str = "root",
                            parent: QWidget | None = None) -> QWidget:
        if isinstance(node, PaneLeaf):
            panel = by_key[node.key]
            panel.show()
            return panel
        orient = Qt.Orientation.Horizontal if node.orientation == "H" else Qt.Orientation.Vertical
        sp = GraphSplitter(orient, parent or self._live_host)
        for i, child in enumerate(node.children):
            sp.addWidget(self._build_split_widget(child, by_key, f"{path}_{i}", sp))
            sp.setStretchFactor(i, 1)
        weights = (node.weights if self.layout_mode == CUSTOM_LAYOUT_MODE
                   else self._load_preset_weights(path, len(node.children), node.weights))
        node.weights = list(weights)
        sp.apply_weights(weights)
        sp.splitterMoved.connect(lambda _p, _i: self._save_splitters())
        sp.equalized.connect(self._save_splitters)
        self._splitters.append((path, node, sp))
        sp.show()
        return sp

    def _update_restore_chips(self, ordered: list) -> None:
        wanted = {self._panel_key(p): self.monitor.name(p)
                  for p in ordered if self._is_eligible(p) and self._panel_key(p) in self.hidden_panels}
        for key in [k for k in self.restore_chips if k not in wanted]:
            btn = self.restore_chips.pop(key)
            self.restore_layout.removeWidget(btn)
            btn.deleteLater()
        for key, name in wanted.items():
            if key not in self.restore_chips:
                b = QPushButton(f"+ {name}")
                b.setObjectName("chip")
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setMinimumHeight(28)
                b.setToolTip(f"Show {name} measurement panel")
                b.clicked.connect(lambda _=False, k=key: self.show_panel(k))
                self.restore_chips[key] = b
                self.restore_layout.addWidget(b)

    def _save_live_split(self) -> None:
        if not hasattr(self, "live_splitter") or sip.isdeleted(self.live_splitter):
            return
        if not self.right_scroll.isVisible():
            return
        w = self.live_splitter.current_weights()
        if w is not None and len(w) == 2:
            self.settings.setValue(f"{self.SETTINGS_GROUP}/LIVE_SPLIT", json.dumps([round(x, 5) for x in w]))

    def _restore_live_split(self) -> None:
        raw = self.settings.value(f"{self.SETTINGS_GROUP}/LIVE_SPLIT", "", type=str)
        try:
            vals = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            vals = []
        if isinstance(vals, list) and len(vals) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in vals):
            weights = [float(vals[0]), float(vals[1])]
        else:
            weights = [0.5, 0.5]
        self.live_splitter.apply_weights(weights)

    def _is_actively_measuring_by_level(self, proxy) -> bool:
        """True, если не-VNA приёмник (SA / PM) сейчас измеряет и его уровень >= порога видеоуровня."""
        prof = profile_name(proxy)
        if prof == "network_analyzer":
            return False
        if not self.monitor.activity(proxy).measuring:
            return False
        lvl = self._receiver_level(proxy)
        if lvl is None:
            return False
        return lvl >= self._display_rule(proxy)["level"]

    def _pick_active_panel(self, visible_widgets: list) -> Any | None:
        """Выбрать панель для левой половины экрана:
        1) приёмник (SA / PM), измеряющий выше порога видеоуровня (самый свежий сигнал);
        2) если никто не измеряет — векторный анализатор (VNA);
        3) если VNA нет — первая доступная панель.
        """
        if not visible_widgets:
            return None
        active_non_vna: list[tuple[float, float, int, LivePanel]] = []
        vna_panel = None
        for idx, panel in enumerate(visible_widgets):
            if not isinstance(panel, LivePanel):
                continue
            prof = profile_name(panel.proxy)
            if prof == "network_analyzer":
                if vna_panel is None:
                    vna_panel = panel
            elif self._is_actively_measuring_by_level(panel.proxy):
                t_sig = float(self.monitor._last_signal.get(panel.proxy, 0.0) or 0.0)
                t_dat = float(self.monitor._last_data.get(panel.proxy, 0.0) or 0.0)
                active_non_vna.append((t_sig, t_dat, -idx, panel))
        if active_non_vna:
            active_non_vna.sort(reverse=True)
            return active_non_vna[0][3]
        if vna_panel is not None:
            return vna_panel
        return visible_widgets[0]

    def active_panel(self):
        """Панель, отображаемая сейчас в левой половине экрана (без скролла)."""
        if self._active_panel_key is None:
            return None
        for p in (*self.panels.values(), *self.current_panels.values()):
            if p.panel_key == self._active_panel_key:
                return p
        return None

    def right_panels(self) -> list:
        """Панели в правой прокручиваемой колонке."""
        by_key = {p.panel_key: p for p in (*self.panels.values(), *self.current_panels.values())}
        return [by_key[k] for k in self._right_panel_keys if k in by_key]

    def _update_visibility(self) -> None:
        ordered = self._ordered_receivers()
        self._update_restore_chips(ordered)
        eligible = [p for p in ordered if self._should_show(p)]
        self._visible_proxies = set(eligible)
        visible_widgets = [p for p in self._ordered_all_panels() if self._panel_visible(p)]
        by_key = {p.panel_key: p for p in visible_widgets}

        active = self._pick_active_panel(visible_widgets)
        active_key = active.panel_key if active is not None else None
        right_keys = [p.panel_key for p in visible_widgets if p.panel_key != active_key]

        sig = (active_key, tuple(right_keys))
        if sig != self._arranged_sig:
            self._save_live_split()
            self._arranged_sig = sig
            self._active_panel_key = active_key
            self._right_panel_keys = right_keys

            # Аккуратно убрать все панели из обоих контейнеров без удаления виджетов
            while self.active_layout.count():
                self.active_layout.takeAt(0)
            while self.right_layout.count():
                item = self.right_layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().hide()
            for panel in (*self.panels.values(), *self.current_panels.values()):
                panel.setParent(self._active_host)
                panel.hide()

            if active_key is not None and active_key in by_key:
                ap = by_key[active_key]
                ap.setMinimumHeight(0)
                ap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                self.active_layout.addWidget(ap, 1)
                ap.show()

            for k in right_keys:
                rp = by_key[k]
                rp.setMinimumHeight(RIGHT_PANEL_MIN_HEIGHT)
                rp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                self.right_layout.addWidget(rp)
                rp.show()
            if right_keys:
                self.right_layout.addStretch(1)

            has_right = bool(right_keys)
            self.right_scroll.setVisible(has_right)
            if has_right:
                self._restore_live_split()

        has_any = active_key is not None
        self.live_splitter.setVisible(has_any)
        self.idle.setVisible(not has_any)

    def visible_panels(self) -> list:
        out = []
        ap = self.active_panel()
        if ap is not None:
            out.append(ap)
        out.extend(self.right_panels())
        return out

    # ------------------------------------------------------------ во весь экран
    def show_window(self) -> None:
        """Показать как при выходе: во весь экран или обычным окном (ini MAIN/FULL_SCREEN)."""
        if self.settings.value(f"{self.SETTINGS_GROUP}/FULL_SCREEN", False, type=bool):
            self.set_full_screen(True)
        else:
            self.show()

    def set_full_screen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
        self.full_button.set_full(on)
        self.full_button.setToolTip("Exit full screen (F11)" if on else "Full screen (F11)")
        self.settings.setValue(f"{self.SETTINGS_GROUP}/FULL_SCREEN", bool(on))

    def toggle_full_screen(self) -> None:
        self.set_full_screen(not self.isFullScreen())

    # ------------------------------------------------------------ док приборов (отдельное окно)
    def set_dock_visible(self, on: bool, *, save: bool = True) -> None:
        on = bool(on)
        dw = self.instrument_dock_window
        if not sip.isdeleted(dw):
            if on:
                if dw.isMinimized():
                    dw.showNormal()
                dw.show()
                dw.raise_()
                dw.activateWindow()
            else:
                dw.save_size()
                dw.hide()
        self.dock_button.blockSignals(True)
        self.dock_button.setChecked(on)
        self.dock_button.blockSignals(False)
        if save:
            self.settings.setValue(f"{self.SETTINGS_GROUP}/DOCK_VISIBLE", on)
        for proxy in list(self.tiles):
            self._update_tile(proxy)

    def _on_dock_visibility_changed(self, visible: bool) -> None:
        if sip.isdeleted(self):
            return
        self.dock_button.blockSignals(True)
        self.dock_button.setChecked(bool(visible))
        self.dock_button.blockSignals(False)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/DOCK_VISIBLE", bool(visible))
        for proxy in list(self.tiles):
            self._update_tile(proxy)

    def _on_dock_button(self) -> None:
        self.set_dock_visible(self.dock_button.isChecked())

    def _on_dock_changed(self) -> None:
        if sip.isdeleted(self):
            return
        shown = self.instrument_dock.visible_windows()
        self.dock_button.setText(f"DOCK ({len(shown)})" if shown else "DOCK")
        if shown and not self.instrument_dock_window.isVisible():
            self.set_dock_visible(True)
        for proxy in list(self.tiles):
            self._update_tile(proxy)

    def restore_docked_windows(self) -> None:
        """Прикрепить в док окна приборов, которые были прикреплены в прошлом сеансе."""
        restored_visible = False
        for w in all_instrument_windows():
            if w.want_docked() and not w.is_docked:
                was_open = w.isVisible()
                self.instrument_dock.attach(w, show=was_open, save=False)
                if was_open:
                    restored_visible = True
        want_open = self.settings.value(f"{self.SETTINGS_GROUP}/DOCK_VISIBLE", False, type=bool)
        if want_open or restored_visible:
            self.set_dock_visible(True, save=False)

    # ------------------------------------------------------------ окна приборов
    def open_instrument(self, proxy) -> None:
        """Показать окно прибора (окна открыты в этом же процессе, возможно скрыты или прикреплены
        к доку); не вспомогательные окна (TraceEditor, CAL...)."""
        w = window_for(proxy)
        if w is None:
            return
        if w.is_docked:
            if w._dock is self.instrument_dock:
                self.set_dock_visible(True)
                self.instrument_dock.reveal(w)
            else:
                dw = w._dock.window()
                if dw is not None:
                    dw.show()
                    dw.raise_()
                w._dock.reveal(w)
            return
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def cleanup(self) -> None:
        self._refresh_timer.stop()
        self.emblem.stop()
        self._dirty_panels.clear()
        self._dirty_current.clear()
        self.monitor.stop()
        if hasattr(self, "instrument_dock") and not sip.isdeleted(self.instrument_dock):
            unregister_dock(self.instrument_dock)
            for w in list(self.instrument_dock.windows()):
                self.instrument_dock.detach(w, show=False, save=False)
        dw = getattr(self, "instrument_dock_window", None)
        if dw is not None and not sip.isdeleted(dw):
            dw.save_size()
            dw.hide()
            dw.close()
        try:
            WINDOW_EVENTS.changed.disconnect(self._on_window_event)
        except (TypeError, RuntimeError):
            pass
        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            try:
                sig.disconnect(self._on_runs)
            except (TypeError, RuntimeError):
                pass

    def closeEvent(self, event):  # noqa: N802 - Qt
        self._refresh_timer.stop()
        self._dirty_panels.clear()
        self._dirty_current.clear()
        self._save_splitters()
        self._save_live_split()
        dw = getattr(self, "instrument_dock_window", None)
        if dw is not None and not sip.isdeleted(dw):
            dw.save_size()
        for panel in self.panels.values():
            if not sip.isdeleted(panel):
                panel._save_plot_splitters()
        if self.quit_on_close:
            for fn in self.before_quit:
                fn()
        super().closeEvent(event)
        self.settings.sync()
        if self.quit_on_close:
            self.cleanup()
            QApplication.quit()
