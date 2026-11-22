"""Панели графиков и таблиц главного окна: ``LivePanel``, ``CurrentPanel``, ``SincEmblem``, ``ScenarioChip``.

Каждая панель графика / таблицы имеет **ровно 2 вкладки** (``PLOT | SET`` на базе ``PTabBar``),
строку процесса над графиком и две компактные векторные кнопки действий (очистить, скрыть).
Правый клик по графикам и заголовкам ничего не открывает; двойной левый клик на графике сохраняет
``DATA → MEM``. Настройки не сбрасываются при обновлении данных графика.
"""
from __future__ import annotations

import json
import math

from PyQt6 import sip
from PyQt6.QtCore import QPointF, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                             QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ..coord import Activity, ActivityMonitor, profile_name
from ..core.params import ConnectionState
from ..core.trace import Trace
from ..core.units import si_format
from .instrument_dock import TILE_MIN_HEIGHT
from .live_plot import LivePlot, nearest_index
from .live_tables import (CURRENT_UNITS, PLOT_MIN_HEIGHT, PM_UNITS, CurrentTable, PowerTable, _CURR_UNIT_LABELS,
                          _PM_UNIT_LABELS, _dbm, _format_current, _format_power, _num)
from .params import IndicatorButton, PTabBar, Panel
from .plot_tools import (ANALYSIS_KEYS, ANALYSIS_METRICS, PlotSettingsWidget, analyze_trace, eval_math_expr,
                         format_analysis, validate_math_expr)
from .pm_view import (PowerCurvePlot, RangeSet, filter_pm_matrix, filter_pm_matrix_by_ranges, load_pm_points_csv,
                      parse_range_expr)
from .power_meter import pm_time_scale
from .split_layout import PLOT_LAYOUT_MODES, GraphSplitter, PaneLeaf, PaneNode, build_preset_tree
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR,
                    UI_COLOR_TEXT, UI_TITLE_BAR_ITEM_SIZE_PX, render_qss, scrollbar_qss)
from .widgets import CommitLineEdit
from .window import PanelClearButton, PanelCloseButton, mark_growing, short_address

__all__ = ["DEVICE_DIVS_Y", "SincEmblem", "ScenarioChip", "CurrentPanel", "LivePanel", "_vna_scale"]

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
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent; border: none;")
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
        y_zero = pad_top + plot_h * 0.78
        y_scale = (y_zero - pad_top) * (0.76 + 0.24 * pulse)

        pts = [
            QPointF(pad_x + plot_w * i / (self._SAMPLES - 1), y_zero - y * y_scale)
            for i, y in enumerate(self._ys)
        ]
        curve = QPainterPath(pts[0])
        for pt in pts[1:]:
            curve.lineTo(pt)

        p.setBrush(Qt.BrushStyle.NoBrush)
        glow_color = QColor(UI_COLOR_TEXT)
        glow_color.setAlpha(int(18 + 38 * pulse))
        glow_pen = QPen(glow_color, 3.2)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(glow_pen)
        p.drawPath(curve)

        fg_color = QColor(UI_COLOR_TEXT)
        fg_color.setAlpha(int(200 + 55 * pulse))
        fg_pen = QPen(fg_color, 1.6)
        fg_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        fg_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(fg_pen)
        p.drawPath(curve)

        peak = QPointF(pad_x + plot_w * 0.5, y_zero - y_scale)
        dot_r = 1.15 + 0.45 * pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(UI_COLOR_TEXT))
        p.drawEllipse(peak, dot_r, dot_r)
        p.end()


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


def _icon_btn(kind: str, tip: str, slot) -> QPushButton:
    """Компактная кнопка заголовка панели в стиле кнопок заголовка окна (инверсия цвета при наведении)."""
    cls = PanelClearButton if kind == "clear" else PanelCloseButton
    b = cls()
    b.setProperty("_icon_kind", kind)
    b.setToolTip(tip)
    b.clicked.connect(slot)
    return b


def _panel_tabs(first_label: str, first_tip: str, second_tip: str, on_changed) -> PTabBar:
    """Стандартная пара вкладок ``PLOT | SET`` для каждой панели графика/таблицы."""
    tb = PTabBar()
    tb.addTab(first_label)
    tb.setTabToolTip(0, first_tip)
    tb.addTab("SET")
    tb.setTabToolTip(1, second_tip)
    tb.setCurrentIndex(0)
    tb.currentChanged.connect(on_changed)
    return tb


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
        QWidget#panelSettingsBody QLabel#sec { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT; }
        QWidget#panelSettingsBody QLabel#muted { color: $UI_COLOR_BORDER_LIGHT; }
        QWidget#panelSettingsBody QLabel#err { color: $UI_COLOR_ERROR; }
        QWidget#panelSettingsBody QLineEdit[invalid="true"] { border-color: $UI_COLOR_ERROR; }
        QWidget#panelSettingsBody QFrame#sep { background-color: $UI_COLOR_BORDER; max-height: 1px; min-height: 1px; }
    """) + "\n" + scrollbar_qss("QScrollArea#panelSettingsScroll "))
    return area


class CurrentPanel(Panel):
    """Панель таблицы токов одного канала ИП: 2 вкладки (``PLOT | SET``), очистка и скрытие."""

    TABS = ("PLOT", "SET")
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

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel(f"{monitor.name(proxy)} CH{self.channel}")
        self.title.setObjectName("heading")
        self.title.setToolTip(f"{monitor.name(proxy)} CH{self.channel}: {proxy.name} ({short_address(proxy.address)})")
        self.tabs = _panel_tabs(
            "PLOT",
            "Current table view",
            "Settings for this current table (RF source, units, ranges, math, analysis)",
            lambda idx: self.set_settings_open(idx == 1),
        )
        self.process_label = QLabel()
        self.process_label.setObjectName("heading")
        self.process_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.level = self.process_label

        self.clear_button = _icon_btn("clear", "Clear this channel's current table", self.clear_data)
        self.close_button = _icon_btn("close", "Hide this current table",
                                      lambda: self.close_requested.emit(self.panel_key))
        head.addWidget(self.title)
        head.addWidget(self.tabs)
        head.addWidget(self.process_label, 1)
        head.addWidget(self.clear_button)
        head.addWidget(self.close_button)
        lay.addLayout(head)

        saved_unit = str(settings.value(f"MAIN/CURR_UNIT/{self.panel_key}", "AUTO") or "AUTO") if settings else "AUTO"
        if saved_unit not in CURRENT_UNITS:
            saved_unit = "AUTO"

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
        self._apply_saved_rf_source()

    def _build_settings_view(self, saved_unit: str) -> QWidget:
        w = QWidget()
        w.setObjectName("panelSettingsBody")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

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
        self.freq_range_edit = CommitLineEdit(
            "Frequency range set expression (Enter to apply, blank = all)",
            self._apply_freq_range,
            placeholder="{1G}+{[2G,3G]}-{[2.5G]}",
            object_name="cfg",
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        self.freq_range_edit.set_committed(self._freq_range.raw, force=True)
        self.power_range_edit = CommitLineEdit(
            "Power range set expression in dBm (Enter to apply, blank = all)",
            self._apply_power_range,
            placeholder="{12}+{[15,17]}-{[16]}",
            object_name="cfg",
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        self.power_range_edit.set_committed(self._power_range.raw, force=True)
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
        self.math_edit = CommitLineEdit(
            "Math formula applied to current values y (A) at frequency x (Hz); Enter to apply",
            self._apply_math,
            placeholder="e.g. y * 1000, y - 0.01",
            object_name="cfg",
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        self.math_edit.set_committed(self._math_expr, force=True)
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
        idx = 1 if on else 0
        self.stack.setCurrentIndex(idx)
        if self.tabs.currentIndex() != idx:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(idx)
            self.tabs.blockSignals(False)

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
        self.freq_range_edit.set_committed(self._freq_range.raw, invalid=False, force=True)
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
        self.power_range_edit.set_committed(self._power_range.raw, invalid=False, force=True)
        self.range_status.setObjectName("muted")
        self.range_status.setText("")
        if self.settings is not None:
            self.settings.setValue(f"MAIN/CURR_POWER_RANGE/{self.panel_key}", self._power_range.raw)
        self.refresh()

    def _apply_math(self) -> None:
        expr = self.math_edit.text().strip()
        if not expr:
            self._math_expr = ""
            self.math_edit.set_committed("", invalid=False, force=True)
        else:
            ok, err = validate_math_expr(expr)
            if not ok:
                self.math_edit.set_invalid(True)
                self.range_status.setObjectName("err")
                self.range_status.setText(err)
                return
            self._math_expr = expr
            self.math_edit.set_committed(expr, invalid=False, force=True)
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


class LivePanel(Panel):
    """Панель живых данных приёмника: 2 вкладки (``PLOT | SET``), процесс над графиком, очистка и скрытие.

    Настройки открываются на второй вкладке и **не перезаписываются** при приходе новых данных
    с прибора, чтобы пользователь мог спокойно вводить параметры.
    """

    TABS = ("PLOT", "SET")
    close_requested = pyqtSignal(object)

    def __init__(self, proxy, monitor: ActivityMonitor, parent=None, settings: QSettings | None = None):
        self.proxy = proxy
        self.monitor = monitor
        self.settings = settings
        self.profile = profile_name(proxy)
        self.panel_key = f"{monitor.name(proxy)}:{proxy.address}"
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

        self.pm_csv_path: str = ""
        self.pm_csv_points: list[tuple[float, float | None]] = []
        self.pm_csv_enabled: bool = False

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Заголовок панели: имя, 2 вкладки (PLOT | SET), процесс над графиком, очистка и скрытие
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("heading")
        self.title.setToolTip(f"{monitor.name(proxy)}: {proxy.name} ({short_address(proxy.address)})")
        self.tabs = _panel_tabs(
            "PLOT",
            "Live graph / table view",
            "Settings for this graph (scale, memory, math, analysis)",
            lambda idx: self.set_settings_open(idx == 1),
        )
        self.process_label = QLabel()
        self.process_label.setObjectName("heading")
        self.process_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.level = self.process_label

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
        head.addWidget(self.tabs)
        head.addWidget(self.process_label, 1)
        head.addWidget(self.clear_button)
        head.addWidget(self.close_button)
        lay.addLayout(head)

        # Стек: страница 0 — график / таблица, страница 1 — настройки
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
        self.power_table: PowerTable | None = None
        self.pm_plot: PowerCurvePlot | None = None
        self.pm_splitter: QSplitter | None = None
        if self.profile == "power_meter":
            self.pm_splitter = QSplitter(Qt.Orientation.Horizontal)
            self.pm_splitter.setChildrenCollapsible(False)
            self.pm_splitter.setHandleWidth(8)
            self.pm_plot = mark_growing(PowerCurvePlot(unit=saved_unit, min_height=PLOT_MIN_HEIGHT))
            self.pm_plot.bind_settings(self.settings, f"MAIN/PM_PLOT/{self.panel_key}")
            self.pm_plot.settings_changed.connect(lambda: self._sync_settings_view(force=False))
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
        self._sync_settings_view(force=True)

    def _build_settings_view(self, saved_unit: str) -> QWidget:
        w = QWidget()
        w.setObjectName("panelSettingsBody")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

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
        self.pm_freq_range_edit: CommitLineEdit | None = None
        self.pm_power_range_edit: CommitLineEdit | None = None
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
            self.pm_freq_range_edit = CommitLineEdit(
                "Frequency range set expression (SI units allowed; Enter to apply, blank = all)",
                self._apply_pm_freq_range,
                placeholder="{1G}+{[2G,3G],[4G,5G]}-{[2.5G]}",
                object_name="cfg",
                align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            )
            self.pm_freq_range_edit.set_committed(self.pm_freq_range.raw, force=True)
            self.pm_power_range_edit = CommitLineEdit(
                "Source power range set expression in dBm (Enter to apply, blank = all)",
                self._apply_pm_power_range,
                placeholder="{12}+{[15,17]}-{[16]}",
                object_name="cfg",
                align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            )
            self.pm_power_range_edit.set_committed(self.pm_power_range.raw, force=True)
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
        self._sync_settings_view(force=True)

    def _sync_settings_view(self, *, force: bool = False) -> None:
        if not hasattr(self, "plot_settings_widget"):
            return
        target = self._current_settings_plot()
        self.plot_settings_widget.set_plot(target, force=force)

    def set_settings_open(self, on: bool, *, plot_idx: int | None = None) -> None:
        on = bool(on)
        if plot_idx is not None:
            self._selected_plot_idx = max(0, int(plot_idx))
        if on:
            self._rebuild_plot_selector()
            self._sync_settings_view(force=True)
        idx = 1 if on else 0
        self.stack.setCurrentIndex(idx)
        if self.tabs.currentIndex() != idx:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(idx)
            self.tabs.blockSignals(False)

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
        self._sync_settings_view(force=True)

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
            p.settings_changed.connect(lambda: self._sync_settings_view(force=False))
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

    def _active_plots(self) -> list[LivePlot]:
        return self.plots[:self._active_plot_count]

    def refresh(self, a: Activity) -> None:
        self.title.setText(a.name)
        status_txt = "OFF" if not a.connected else ""
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
        # Процесс выводится строго над графиком; вкладку настроек при обновлении графика НЕ трогаем
        self.process_label.setText(self._process_override or status_txt)

    def _vna_markers_for_items(self, items: list[tuple[str, Trace]], total_traces: int) -> list[tuple[str, float, float, int]]:
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
            for name, tr in traces:
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

    # ------------------------------------------------------------ настройки ваттметра
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
            self.pm_freq_range_edit.set_committed(rs.raw, invalid=False, force=True)
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
            self.pm_power_range_edit.set_committed(rs.raw, invalid=False, force=True)
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
