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

from PyQt6 import sip
from PyQt6.QtCore import QMimeData, QPoint, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from ..coord import COORD, Activity, ActivityMonitor, Coordinator, profile_name
from ..core.params import ConnectionState
from ..core.units import si_format, si_parse
from ..core.trace import Trace
from .instrument_window import WINDOW_EVENTS, window_for
from .live_plot import LivePlot
from .params import IndicatorButton, Panel, PanelLine, PanelTable, PTabBar, _repolish, paint_indicator
from .power_meter import pm_time_scale
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR,
                    UI_COLOR_INDICATOR, UI_COLOR_TEXT, UI_COLOR_TEXT_MUTED, WINDOW_BAR_HEIGHT, render_qss)
from .window import CustomFullButton, Window, mark_growing, short_address

__all__ = ["MainWindow", "InstrumentTile", "LivePanel", "ScenarioChip", "PowerTable", "LAYOUT_MODES", "PM_UNITS"]

TILE_MIN_HEIGHT = 38
PLOT_MIN_HEIGHT = 240
UI_REFRESH_MS = 60
LAYOUT_MODES = ("1 COL", "2 COL", "ROW")
PM_UNITS = ("dBm", "W", "dBW", "V", "dB")
_PM_UNIT_LABELS = {"dBm": "dBm", "W": "W", "dBW": "dBW", "V": "V (50 Ω)", "dB": "dB (Pout − Pin)"}
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
    """Порог: число с единицей; Enter / уход фокуса - применить, Esc - вернуть."""

    def __init__(self, unit: str, scale: float, parent=None):
        super().__init__(parent)
        self.setObjectName("cfg")
        self.unit, self.scale = unit, scale
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self.show_value(self.value)
            self.clearFocus()
            return
        super().keyPressEvent(e)

    def show_value(self, v: float) -> None:
        self.value = v
        shown = v * self.scale
        self.setText(si_format(shown, self.unit, 6) if self.unit == "Hz" else f"{shown:g} {self.unit}")

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
        self.setMinimumWidth(72)
        self.setMinimumHeight(TILE_MIN_HEIGHT)

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

    def set_rows(self, rows, current: float | None = None) -> None:
        """Обратная совместимость: [(Hz, dBm, время)] -> таблица с одним столбцом POWER."""
        rows = list(rows)
        cols: list[float | None] = [None] if rows else []
        matrix_rows = [(f, {None: v}) for f, v, _t in rows]
        self.set_matrix(cols, matrix_rows, current, None)

    def set_matrix(self, columns: list[float | None], rows: list[tuple[float, dict[float | None, float]]],
                   current: float | None = None, current_power: float | None = None) -> None:
        """columns: [P_in dBm | None]; rows: [(Hz, {P_in: P_meas dBm})]."""
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
                text = _format_power(v, self.unit, col_p) if v is not None else ""
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


class ScenarioChip(IndicatorButton):
    """Идущие сценарии координатора: имя и прогресс; клик - окно прибора, которым сценарий меряет."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chip")
        self.setProperty("tile", True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(72)
        self.setMinimumHeight(TILE_MIN_HEIGHT)
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
        self.setText(f"{run.name}{more}\n{run.done}/{run.total}" if run.total else f"{run.name}{more}\nRUN")
        who = " + ".join(d.name for d in run.devices.values())
        self.setToolTip("\n".join(f"{r.name}: {' + '.join(d.name for d in r.devices.values())}"
                                  + (f"  {r.done}/{r.total} {r.text}" if r.total else "") for r in self.runs)
                        + f"\nClick - open the {who.split(' + ')[-1]} window")
        self.update()


class LivePanel(Panel):
    """Живые данные одного приёмника: можно переставлять (кнопками / перетаскиванием) и закрывать."""

    move_requested = pyqtSignal(object, int)
    swap_requested = pyqtSignal(str, str)
    close_requested = pyqtSignal(object)

    def __init__(self, proxy, monitor: ActivityMonitor, parent=None, settings: QSettings | None = None):
        self.proxy = proxy
        self.monitor = monitor
        self.settings = settings
        self.profile = profile_name(proxy)
        self.panel_key = f"{monitor.name(proxy)}:{proxy.address}"
        self._drag_pos: QPoint | None = None
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("heading")
        self.address = QLabel(short_address(proxy.address))
        self.source = QLabel()
        self.source.setObjectName("source")
        self.level = QLabel()
        self.level.setObjectName("heading")
        saved_unit = str(settings.value(f"MAIN/PM_UNIT/{self.panel_key}", "dBm") or "dBm") if settings else "dBm"
        if saved_unit not in PM_UNITS:
            saved_unit = "dBm"
        self.unit_button = QPushButton(saved_unit)
        self.unit_button.setObjectName("chip")
        self.unit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.unit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unit_button.setToolTip("Power table unit (dBm / W / dBW / V / dB gain). Click - next, right click - choose")
        self.unit_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.unit_button.clicked.connect(self._next_pm_unit)
        self.unit_button.customContextMenuRequested.connect(self._pm_unit_menu)
        self.unit_button.hide()
        self.clear_button = QPushButton("CLEAR")
        self.clear_button.setObjectName("chip")
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("Clear the frequency / power table")
        self.clear_button.clicked.connect(lambda: self.monitor.clear_pm_table(self.proxy))
        self.clear_button.hide()
        self.move_prev_button = self._tool_btn("◀", "Move measurement panel earlier",
                                               lambda: self.move_requested.emit(self.proxy, -1))
        self.move_next_button = self._tool_btn("▶", "Move measurement panel later",
                                               lambda: self.move_requested.emit(self.proxy, 1))
        self.close_button = self._tool_btn("×", "Hide this measurement panel",
                                           lambda: self.close_requested.emit(self.proxy))
        head.addWidget(self.title)
        head.addWidget(self.address)
        head.addStretch()
        head.addWidget(self.source)
        head.addWidget(self.level)
        head.addWidget(self.unit_button)
        head.addWidget(self.clear_button)
        head.addWidget(self.move_prev_button)
        head.addWidget(self.move_next_button)
        head.addWidget(self.close_button)
        lay.addLayout(head)
        self.plots_box = QVBoxLayout()
        self.plots_box.setSpacing(6)
        lay.addLayout(self.plots_box, 1)
        self.plots: list[LivePlot] = []
        self.power_table: PowerTable | None = None      # не ``table``: так Panel зовёт своё содержимое
        if self.profile == "power_meter":
            self.power_table = PowerTable(unit=saved_unit)
            lay.addWidget(self.power_table, 1)
        super().__init__(body, parent=parent)
        self.setAcceptDrops(True)
        self.address.setStyleSheet("color: #8a8a8a;")
        self.source.setStyleSheet("color: #8a8a8a;")

    @staticmethod
    def _tool_btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def mousePressEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= 40:
            self._drag_pos = e.position().toPoint()
        else:
            self._drag_pos = None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        if (self._drag_pos is not None and (e.buttons() & Qt.MouseButton.LeftButton)
                and (e.position().toPoint() - self._drag_pos).manhattanLength() >= QApplication.startDragDistance()):
            self._drag_pos = None
            mime = QMimeData()
            mime.setData(_PANEL_MIME, self.panel_key.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def dragEnterEvent(self, e):  # noqa: N802 - Qt
        if e.mimeData().hasFormat(_PANEL_MIME):
            e.acceptProposedAction()
            return
        super().dragEnterEvent(e)

    def dropEvent(self, e):  # noqa: N802 - Qt
        if e.mimeData().hasFormat(_PANEL_MIME):
            src = bytes(e.mimeData().data(_PANEL_MIME)).decode("utf-8")
            if src and src != self.panel_key:
                self.swap_requested.emit(src, self.panel_key)
            e.acceptProposedAction()
            return
        super().dropEvent(e)

    def _plot(self, i: int) -> LivePlot:
        while len(self.plots) <= i:
            p = mark_growing(LivePlot(min_height=PLOT_MIN_HEIGHT))
            self.plots.append(p)
            self.plots_box.addWidget(p)
        return self.plots[i]

    def _use_plots(self, n: int) -> None:
        for i, p in enumerate(self.plots):
            p.setVisible(i < n)

    def refresh(self, a: Activity) -> None:
        self.title.setText(a.name)
        if not a.connected:
            self.level.setText("OFF")
        if self.profile == "network_analyzer":
            self._refresh_vna(a)
        elif self.profile == "spectrum_analyzer":
            self._refresh_sa(a)
        elif self.profile == "power_meter":
            self._refresh_pm(a)
        if a.source is not None:
            path = f"  PATH {a.path_db:+.1f} dB" if a.path_db is not None else ""
            self.source.setText(f"← {a.source.name} {si_format(a.source.freq, 'Hz', 6)}{path}")
        elif self.profile == "network_analyzer" and self.proxy.value("gen.state") is True:
            port = self.proxy.value("gen.port")
            pt = f" P{port}" if port in (1, 2) else ""
            rf_off = " (RF OFF)" if self.proxy.value("rf.output") is False else ""
            self.source.setText(f"CW{pt} {si_format(self.proxy.value('freq.cw'), 'Hz', 6)}{rf_off}")
        else:
            self.source.setText("")

    def _refresh_vna(self, a: Activity) -> None:
        traces = self.monitor.vna_traces(self.proxy)
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
        for i, (scale, items) in enumerate(groups.values()):
            plot = self._plot(i)
            plot.set_scale(scale, divs_y=DEVICE_DIVS_Y if scale is not None else None)
            plot.set_traces(items)
        self._use_plots(max(1, len(groups)))
        if not groups:
            plot = self._plot(0)
            plot.set_scale(None)
            plot.set_traces([])
        if a.connected:
            self.level.setText(f"{len(traces)} TRC" if traces else "NO TRC")

    def _refresh_sa(self, a: Activity) -> None:
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
        if a.connected and a.level is not None:
            where = f" @ {si_format(a.freq, 'Hz', 6)}" if a.freq is not None and present else ""
            self.level.setText(f"{_dbm(a.level)}{where}" if present else f"NOISE {_dbm(a.floor)}")

    def set_pm_unit(self, unit: str) -> None:
        if self.power_table is None or unit not in PM_UNITS:
            return
        self.power_table.set_unit(unit)
        self.unit_button.setText(unit)
        if self.settings is not None:
            self.settings.setValue(f"MAIN/PM_UNIT/{self.panel_key}", unit)
        self.refresh(self.monitor.activity(self.proxy))

    def _next_pm_unit(self) -> None:
        if self.power_table is None:
            return
        idx = PM_UNITS.index(self.power_table.unit)
        self.set_pm_unit(PM_UNITS[(idx + 1) % len(PM_UNITS)])

    def _pm_unit_menu(self, pos: QPoint) -> None:
        if self.power_table is None:
            return
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for u in PM_UNITS:
            act = menu.addAction(_PM_UNIT_LABELS[u])
            act.setCheckable(True)
            act.setChecked(u == self.power_table.unit)
            act.triggered.connect(lambda _=False, unit=u: self.set_pm_unit(unit))
        menu.popup(self.unit_button.mapToGlobal(pos))

    def _refresh_pm(self, a: Activity) -> None:
        """Временной режим (TRAC ON) - трасса во времени; иначе - таблица частота × подаваемая мощность."""
        tr = self.proxy.value("trace")
        timed = self.proxy.value("trace.state") is True and isinstance(tr, Trace)
        if timed:
            plot = self._plot(0)
            plot.x_range = None
            scale, divs_y, y_unit, y_transform = pm_time_scale(self.proxy, tr, self.settings)
            plot.set_scale(scale, divs_y=divs_y, y_unit=y_unit, y_transform=y_transform)
            plot.set_traces([("", tr)])
        self._use_plots(1 if timed else 0)
        self.power_table.setVisible(not timed)
        self.unit_button.setVisible(not timed)
        self.clear_button.setVisible(not timed)
        if not timed:
            cols, rows = self.monitor.pm_matrix(self.proxy)
            cur_p = None if a.source is None or a.source.power is None else round(float(a.source.power), 3)
            self.power_table.set_matrix(cols, rows, _num(self.proxy.value("freq")), cur_p)
        if a.connected:
            u = self.power_table.unit
            if a.level is None:
                self.level.setText("NO DATA")
            elif u == "dB":
                src_p = None if a.source is None else a.source.power
                self.level.setText(_format_power(a.level, "dB", src_p) if src_p is not None else _dbm(a.level))
            else:
                self.level.setText(_format_power(a.level, u))


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
        self.restore_chips: dict[str, QPushButton] = {}
        self.display_rules: dict[str, dict[str, Any]] = {}
        self.display_controls: dict[str, tuple[QCheckBox, QCheckBox, SettingField]] = {}
        self._root_splitter: QSplitter | None = None
        self._splitters: list[tuple[str, QSplitter]] = []
        self._arranged_sig: tuple | None = None
        self._visible_proxies: set = set()
        self._measuring_proxies: set = set()
        self._dirty_panels: set = set()
        self._dirty_vis = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(UI_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._flush_panels)
        self.panel_order: list[str] = self._load_list(f"{self.SETTINGS_GROUP}/PANEL_ORDER")
        self.hidden_panels: set[str] = set(self._load_list(f"{self.SETTINGS_GROUP}/HIDDEN_PANELS"))
        saved_mode = str(settings.value(f"{self.SETTINGS_GROUP}/LAYOUT_MODE", LAYOUT_MODES[0]) or LAYOUT_MODES[0])
        self.layout_mode = saved_mode if saved_mode in LAYOUT_MODES else LAYOUT_MODES[0]

        # во весь экран: кнопка в заголовке правее «свернуть» и F11
        self.full_button = CustomFullButton()
        self.full_button.setToolTip("Full screen (F11)")
        self.full_button.clicked.connect(self.toggle_full_screen)
        self.titleBar.insert_control_widget(self.full_button, after_minimize=True)
        self._full_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        self._full_key.activated.connect(self.toggle_full_screen)

        strip = QWidget()
        self.strip_layout = QHBoxLayout(strip)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(4)
        self.restore_layout = QHBoxLayout()
        self.restore_layout.setContentsMargins(0, 0, 0, 0)
        self.restore_layout.setSpacing(4)
        self.layout_button = QPushButton(self.layout_mode)
        self.layout_button.setObjectName("chip")
        self.layout_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.layout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.layout_button.setMinimumHeight(28)
        self.layout_button.setToolTip("Measurement panel layout: 1 COL (stack), 2 COL (grid), ROW (side by side). Click to switch")
        self.layout_button.clicked.connect(self._cycle_layout_mode)
        self.all_button = QPushButton("ALL")
        self.all_button.setObjectName("chip")
        self.all_button.setCheckable(True)
        self.all_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.setMinimumHeight(28)
        self.all_button.setToolTip("Show all receivers, also those without data now")
        self.all_button.setChecked(settings.value(f"{self.SETTINGS_GROUP}/SHOW_ALL", False, type=bool))
        self.all_button.toggled.connect(self._on_all)
        self.scenario_chip = ScenarioChip()
        self.scenario_chip.clicked.connect(self._open_scenario)
        self.scenario_chip.hide()
        self.strip_layout.addStretch()
        self.strip_layout.addLayout(self.restore_layout)
        self.strip_layout.addWidget(self.scenario_chip)
        self.strip_layout.addWidget(self.layout_button)
        self.strip_layout.addWidget(self.all_button)
        self.strip_panel = Panel(strip)
        self.strip_panel.setStyleSheet(
            Panel.STYLE_SHEET + f'\nPanel QPushButton#chip[tile="true"] {{ min-height: {TILE_MIN_HEIGHT}px; }}'
        )
        self.strip_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

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
            f.editingFinished.connect(lambda n=name: self._apply_setting(n))
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
        self.settings_panel = Panel(settings_body)
        self.settings_panel.setStyleSheet(Panel.STYLE_SHEET + render_qss("""
            Panel QCheckBox { color: $UI_COLOR_TEXT; spacing: 6px; background: transparent; border: none; }
            Panel QCheckBox:disabled { color: $UI_COLOR_BORDER_LIGHT; }
            Panel QCheckBox::indicator { width: 14px; height: 14px; border-radius: 3px;
                border: 1px solid $UI_COLOR_BORDER_LIGHT; background: $UI_COLOR_BACKGROUND; }
            Panel QCheckBox::indicator:checked { background: $UI_COLOR_INVERT; border-color: $UI_COLOR_TEXT; }
        """))
        self.settings_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.idle = QLabel("NOTHING IS MEASURING")
        self.idle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle.setStyleSheet("color: #8a8a8a; padding: 24px;")
        self.live_page = QWidget()
        live = QVBoxLayout(self.live_page)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)
        self._live_host = QWidget()
        self._live_host.setObjectName("liveHost")
        self.live_layout = QVBoxLayout(self._live_host)
        self.live_layout.setContentsMargins(0, 0, 0, 0)
        self.live_layout.setSpacing(0)
        self.live_scroll = QScrollArea()
        self.live_scroll.setWidgetResizable(True)
        self.live_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.live_scroll.setWidget(self._live_host)
        self.live_scroll.setStyleSheet(render_qss("""
            QScrollArea, QWidget#liveHost { background: $UI_COLOR_BACKGROUND; border: none; }
            QSplitter { background: $UI_COLOR_BACKGROUND; }
            QSplitter::handle { background: transparent; }
            QSplitter::handle:vertical { height: 8px; }
            QSplitter::handle:horizontal { width: 8px; }
            QSplitter::handle:hover { background: $UI_COLOR_BORDER_LIGHT; border-radius: 2px; }
        """))
        live.addWidget(self.live_scroll, 1)
        live.addWidget(self.idle)

        # вкладки: LIVE - что измеряется, SET - пороги (как вкладки в окнах приборов)
        self.tabs = PTabBar()
        for title in self.TABS:
            self.tabs.addTab(title)
        self.tabs.setTabToolTip(1, "Thresholds and displayed graphs settings")
        self.tabs_panel = Panel(self.tabs)
        self.tabs_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.strip_panel)
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
        for i, p in enumerate(proxies):
            if p not in self.tiles:
                tile = InstrumentTile(p)
                tile.clicked.connect(lambda _=False, p=p: self.open_instrument(p))
                self.tiles[p] = tile
            self.strip_layout.insertWidget(i, self.tiles[p])
        for p in self.monitor.receivers():
            if p not in self.panels:
                panel = LivePanel(p, self.monitor, settings=self.settings)
                panel.move_requested.connect(self.move_panel)
                panel.swap_requested.connect(self.swap_panels)
                panel.close_requested.connect(self.hide_panel)
                panel.hide()
                self.panels[p] = panel
                if panel.panel_key not in self.panel_order:
                    self.panel_order.append(panel.panel_key)
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
        self._dirty_panels.clear()
        self._dirty_vis = False
        self._rebuild_display_settings()
        self._update_visibility()

    # ------------------------------------------------------------ обновление
    def _on_changed(self, proxy) -> None:
        if sip.isdeleted(self):
            return
        self._update_tile(proxy)
        panel = self.panels.get(proxy)
        if panel is None:
            return
        self._dirty_panels.add(proxy)
        vis_changed = (proxy in self._visible_proxies) != self._should_show(proxy)
        meas_changed = (proxy in self._measuring_proxies) != self.monitor.activity(proxy).measuring
        if vis_changed or meas_changed:
            if vis_changed:
                self._dirty_vis = True
            self._flush_panels()
        elif not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_panels(self) -> None:
        if sip.isdeleted(self):
            return
        dirty = list(self._dirty_panels)
        self._dirty_panels.clear()
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
        tip.append(("Window is open" if window_open else "Window is closed") + ". Click - show the window")
        tile.show_state(name, window_open, errors or lost, "\n".join(tip))

    def _on_runs(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        self.scenario_chip.show_runs(self.coord.runs())

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
        nxt = LAYOUT_MODES[(LAYOUT_MODES.index(self.layout_mode) + 1) % len(LAYOUT_MODES)]
        self.set_layout_mode(nxt)

    def set_layout_mode(self, mode: str) -> None:
        if mode not in LAYOUT_MODES or mode == self.layout_mode:
            return
        self._save_splitters()
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
            level_edit.editingFinished.connect(lambda k=key: self._apply_display_level(k))
            rl.addWidget(lbl)
            rl.addWidget(show_cb)
            rl.addWidget(level_cb)
            rl.addWidget(level_edit, 1)
            self.display_layout.addWidget(row)
            self.display_controls[key] = (show_cb, level_cb, level_edit)

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

    def move_panel(self, proxy, delta: int) -> None:
        """Сдвинуть панель в порядке показа среди видимых панелей."""
        ordered = self._ordered_receivers()
        vis = [p for p in ordered if self._should_show(p)]
        if proxy not in vis:
            return
        i = vis.index(proxy)
        j = i + (-1 if delta < 0 else 1)
        if not 0 <= j < len(vis):
            return
        self.swap_panels(self._panel_key(vis[i]), self._panel_key(vis[j]))

    def swap_panels(self, src_key: str, dst_key: str) -> None:
        if src_key == dst_key:
            return
        for k in (src_key, dst_key):
            if k not in self.panel_order:
                self.panel_order.append(k)
        i, j = self.panel_order.index(src_key), self.panel_order.index(dst_key)
        self.panel_order[i], self.panel_order[j] = self.panel_order[j], self.panel_order[i]
        self._save_list(f"{self.SETTINGS_GROUP}/PANEL_ORDER", self.panel_order)
        self._arranged_sig = None
        self._update_visibility()

    def _ordered_receivers(self) -> list:
        rank = {k: i for i, k in enumerate(self.panel_order)}
        receivers = list(self.panels)
        return sorted(receivers, key=lambda p: (rank.get(self._panel_key(p), len(rank)), receivers.index(p)))

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

    def _splitter_key(self, tag: str, count: int) -> str:
        return f"{self.SETTINGS_GROUP}/SPLITTER_SIZES/{self.layout_mode}/{tag}_{count}"

    def _save_splitters(self) -> None:
        for tag, sp in self._splitters:
            if not sip.isdeleted(sp):
                sizes = sp.sizes()
                if len(sizes) > 1 and any(sizes):
                    self.settings.setValue(self._splitter_key(tag, len(sizes)), json.dumps(sizes))

    def _restore_splitter(self, tag: str, sp: QSplitter) -> None:
        raw = self.settings.value(self._splitter_key(tag, sp.count()), "", type=str)
        try:
            sizes = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            sizes = []
        if isinstance(sizes, list) and len(sizes) == sp.count() and all(isinstance(x, int) and x > 0 for x in sizes):
            sp.setSizes(sizes)
        else:
            sp.setSizes([1000] * sp.count())

    def _make_splitter(self, orientation: Qt.Orientation, tag: str) -> QSplitter:
        sp = QSplitter(orientation)
        sp.setChildrenCollapsible(False)
        sp.setHandleWidth(8)
        sp.splitterMoved.connect(lambda _p, _i: self._save_splitters())
        self._splitters.append((tag, sp))
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

    def _update_visibility(self) -> None:
        ordered = self._ordered_receivers()
        self._update_restore_chips(ordered)
        visible = [p for p in ordered if self._should_show(p)]
        self._visible_proxies = set(visible)
        sig = (self.layout_mode, tuple(self._panel_key(p) for p in visible))
        if sig != self._arranged_sig:
            self._save_splitters()
            self._arranged_sig = sig
            for panel in self.panels.values():
                panel.setParent(self._live_host)
                panel.hide()
            if self._root_splitter is not None:
                self.live_layout.removeWidget(self._root_splitter)
                self._root_splitter.deleteLater()
                self._root_splitter = None
            self._splitters.clear()
            if visible:
                if self.layout_mode == "ROW":
                    root = self._make_splitter(Qt.Orientation.Horizontal, "root")
                    for i, p in enumerate(visible):
                        root.addWidget(self.panels[p])
                        root.setStretchFactor(i, 1)
                        self.panels[p].show()
                    self._restore_splitter("root", root)
                elif self.layout_mode == "2 COL" and len(visible) > 1:
                    root = self._make_splitter(Qt.Orientation.Vertical, "root")
                    for r, i in enumerate(range(0, len(visible), 2)):
                        chunk = visible[i:i + 2]
                        if len(chunk) == 2:
                            row_sp = self._make_splitter(Qt.Orientation.Horizontal, f"row{r}")
                            for c, p in enumerate(chunk):
                                row_sp.addWidget(self.panels[p])
                                row_sp.setStretchFactor(c, 1)
                                self.panels[p].show()
                            self._restore_splitter(f"row{r}", row_sp)
                            root.addWidget(row_sp)
                        else:
                            root.addWidget(self.panels[chunk[0]])
                            self.panels[chunk[0]].show()
                        root.setStretchFactor(r, 1)
                    self._restore_splitter("root", root)
                else:
                    root = self._make_splitter(Qt.Orientation.Vertical, "root")
                    for i, p in enumerate(visible):
                        root.addWidget(self.panels[p])
                        root.setStretchFactor(i, 1)
                        self.panels[p].show()
                    self._restore_splitter("root", root)
                self._root_splitter = root
                self.live_layout.addWidget(root, 1)
        for i, p in enumerate(visible):
            panel = self.panels[p]
            panel.move_prev_button.setEnabled(i > 0)
            panel.move_next_button.setEnabled(i < len(visible) - 1)
        self.live_scroll.setVisible(bool(visible))
        self.idle.setVisible(not visible)

    def visible_panels(self) -> list[LivePanel]:
        return [self.panels[p] for p in self._ordered_receivers() if self._should_show(p)]

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

    # ------------------------------------------------------------ окна приборов
    def open_instrument(self, proxy) -> None:
        """Показать окно прибора (окна открыты в этом же процессе, возможно скрыты); не вспомогательные
        окна (TraceEditor, CAL...)."""
        w = window_for(proxy)
        if w is None:
            return
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def cleanup(self) -> None:
        self._refresh_timer.stop()
        self._dirty_panels.clear()
        self.monitor.stop()
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
        self._save_splitters()
        if self.quit_on_close:
            for fn in self.before_quit:
                fn()
        super().closeEvent(event)
        if self.quit_on_close:
            self.cleanup()
            QApplication.quit()
