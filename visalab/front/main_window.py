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

from PyQt6 import sip
from PyQt6.QtCore import QRectF, QSettings, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..coord import COORD, Activity, ActivityMonitor, Coordinator, profile_name
from ..core.params import ConnectionState
from ..core.units import si_format, si_parse
from ..core.trace import Trace
from .instrument_window import WINDOW_EVENTS, window_for
from .live_plot import LivePlot
from .params import IndicatorButton, Panel, PanelLine, PanelTable, PTabBar, _repolish, paint_indicator
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR, UI_COLOR_TEXT, UI_COLOR_TEXT_MUTED,
                    WINDOW_BAR_HEIGHT)
from .window import CustomFullButton, Window, mark_growing, short_address

__all__ = ["MainWindow", "InstrumentTile", "LivePanel", "ScenarioChip", "PowerTable"]

#: страница SET: (подпись, поле Thresholds, единица, множитель показа, подсказка)
SETTINGS = (
    ("SA MARG", "sa_margin_db", "dB", 1.0, "Spectrum analyzer measures when its peak is this much above the noise floor"),
    ("PM FLR", "pm_floor_dbm", "dBm", 1.0, "Power meter measures when its reading is not below this level"),
    ("VNA THRU", "vna_thru_db", "dB", 1.0, "VNA measures when any S21 / S12 rises above this level (something between the ports)"),
    ("VNA REFL", "vna_refl_db", "dB", 1.0, "VNA measures when any S11 / S22 drops below this level (port is loaded, not open)"),
    ("HOLD", "hold_s", "s", 1.0, "Keep showing an instrument this long after its signal is gone"),
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
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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

    def indicated(self) -> bool:
        return self.window_open

    def show_state(self, name: str, window_open: bool, error: bool, tip: str = "") -> None:
        if self.text() != name:
            self.setText(name)
        if error != self.error:
            self.setProperty("wstate", "error" if error else "")
            _repolish(self)
        self.window_open, self.error = bool(window_open), bool(error)
        if tip:
            self.setToolTip(tip)
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        QPushButton.paintEvent(self, e)
        p = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.window_open:                  # окно открыто - индикатор (при ошибке - красная полоса)
            paint_indicator(p, rect, UI_COLOR_ERROR if self.error else UI_COLOR_TEXT_MUTED)
        if self.error:                        # ошибка - красная рамка поверх
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(UI_COLOR_ERROR), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.25, 0.25, -0.25, -0.25), UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)


class PowerTable(QTableWidget):
    """Ваттметр в главном окне: частота датчика и мощность в этой точке (последнее показание с сигналом);
    строка текущей частоты датчика выделена (Алексей 2026-11-14: вместо графика показаний во времени)."""

    EMPTY = "NO POINTS YET: each sensor frequency gets a row\nwith the last reading that had a signal"
    ALIGN = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __init__(self, parent=None):
        super().__init__(0, 2, parent)
        self.setObjectName("pmTable")
        self.setHorizontalHeaderLabels(["FREQ", "POWER"])
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(20)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        for col, align in enumerate(self.ALIGN):                # частота - слева, мощность - справа
            self.horizontalHeaderItem(col).setTextAlignment(align)
        self.setMinimumHeight(170)
        self.rows: list[tuple[float, float, float]] = []
        self.current: float | None = None

    def set_rows(self, rows, current: float | None = None) -> None:
        """rows: [(Hz, dBm, время)] по частоте; ``current`` - частота датчика (строка выделена, видна)."""
        rows = list(rows)
        moved = current != self.current or len(rows) != len(self.rows)
        self.rows, self.current = rows, current
        self.setRowCount(len(rows))
        bold = QFont(self.font())
        bold.setBold(True)
        cur_row = -1
        for i, (f, v, _t) in enumerate(rows):
            is_cur = current is not None and abs(f - current) < 0.5
            if is_cur:
                cur_row = i
            for col, text in enumerate((si_format(f, "Hz", 9), f"{v:.2f} dBm")):
                item = self.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(self.ALIGN[col])
                    self.setItem(i, col, item)
                if item.text() != text:
                    item.setText(text)
                item.setFont(bold if is_cur else self.font())
                item.setForeground(QColor(UI_COLOR_TEXT if is_cur else UI_COLOR_TEXT_MUTED))
        if moved and cur_row >= 0:
            self.scrollToItem(self.item(cur_row, 0), QAbstractItemView.ScrollHint.EnsureVisible)
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
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(72)
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
    """Живые данные одного приёмника."""

    def __init__(self, proxy, monitor: ActivityMonitor, parent=None):
        self.proxy = proxy
        self.monitor = monitor
        self.profile = profile_name(proxy)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("heading")
        self.address = QLabel(short_address(proxy.address))
        self.source = QLabel()
        self.source.setObjectName("source")
        self.level = QLabel()
        self.level.setObjectName("heading")
        self.clear_button = QPushButton("CLEAR")
        self.clear_button.setObjectName("chip")
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("Clear the frequency / power table")
        self.clear_button.clicked.connect(lambda: self.monitor.clear_pm_table(self.proxy))
        self.clear_button.hide()
        head.addWidget(self.title)
        head.addWidget(self.address)
        head.addStretch()
        head.addWidget(self.source)
        head.addWidget(self.level)
        head.addWidget(self.clear_button)
        lay.addLayout(head)
        self.plots_box = QVBoxLayout()
        self.plots_box.setSpacing(6)
        lay.addLayout(self.plots_box, 1)
        self.plots: list[LivePlot] = []
        self.power_table: PowerTable | None = None      # не ``table``: так Panel зовёт своё содержимое
        if self.profile == "power_meter":
            self.power_table = PowerTable()
            lay.addWidget(self.power_table, 1)
        super().__init__(body, parent=parent)
        self.address.setStyleSheet("color: #8a8a8a;")
        self.source.setStyleSheet("color: #8a8a8a;")

    def _plot(self, i: int) -> LivePlot:
        while len(self.plots) <= i:
            p = mark_growing(LivePlot(min_height=170))
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
            self.source.setText(f"CW {si_format(self.proxy.value('freq.cw'), 'Hz', 6)}")
        else:
            self.source.setText("")

    def _refresh_vna(self, a: Activity) -> None:
        traces = self.monitor.vna_traces(self.proxy)
        groups: dict[str, list] = {}
        for name, tr in traces:                      # одна шкала - одна единица
            groups.setdefault(tr.y_unit, []).append((name, tr))
        for i, items in enumerate(groups.values()):
            self._plot(i).set_traces(items)
        self._use_plots(max(1, len(groups)))
        if not groups:
            self._plot(0).set_traces([])
        if a.connected:
            self.level.setText(f"{len(traces)} TRC" if traces else "NO TRC")

    def _refresh_sa(self, a: Activity) -> None:
        tr = self.proxy.value("trace")
        plot = self._plot(0)
        self._use_plots(1)
        plot.set_traces([("", tr)] if isinstance(tr, Trace) else [])
        if a.measuring and a.level is not None and isinstance(tr, Trace):
            x, _y = tr.peak()
            plot.set_marker(x, a.level)
        else:
            plot.set_marker(None)
        if a.connected and a.level is not None:
            where = f" @ {si_format(a.freq, 'Hz', 6)}" if a.freq is not None and a.measuring else ""
            self.level.setText(f"{_dbm(a.level)}{where}" if a.measuring else f"NOISE {_dbm(a.floor)}")

    def _refresh_pm(self, a: Activity) -> None:
        """Временной режим (TRAC ON) - трасса во времени; иначе - таблица частота / мощность."""
        tr = self.proxy.value("trace")
        timed = self.proxy.value("trace.state") is True and isinstance(tr, Trace)
        if timed:
            plot = self._plot(0)
            plot.x_range = None
            plot.set_traces([("", tr)])
        self._use_plots(1 if timed else 0)
        self.power_table.setVisible(not timed)
        self.clear_button.setVisible(not timed)
        if not timed:
            self.power_table.set_rows(self.monitor.pm_table(self.proxy), _num(self.proxy.value("freq")))
        if a.connected:
            self.level.setText(_dbm(a.level) or "NO DATA")


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

        # во весь экран: кнопка в заголовке и F11 (Алексей 2026-11-14)
        self.full_button = CustomFullButton()
        self.full_button.setToolTip("Full screen (F11)")
        self.full_button.clicked.connect(self.toggle_full_screen)
        self.titleBar.insert_control_widget(self.full_button)
        self._full_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        self._full_key.activated.connect(self.toggle_full_screen)

        strip = QWidget()
        self.strip_layout = QHBoxLayout(strip)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(4)
        self.all_button = QPushButton("ALL")
        self.all_button.setObjectName("chip")
        self.all_button.setCheckable(True)
        self.all_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.setToolTip("Show all receivers, also those that measure nothing now")
        self.all_button.setChecked(settings.value(f"{self.SETTINGS_GROUP}/SHOW_ALL", False, type=bool))
        self.all_button.toggled.connect(self._on_all)
        self.scenario_chip = ScenarioChip()
        self.scenario_chip.clicked.connect(self._open_scenario)
        self.scenario_chip.hide()
        self.strip_layout.addStretch()
        self.strip_layout.addWidget(self.scenario_chip)
        self.strip_layout.addWidget(self.all_button)
        self.strip_panel = Panel(strip)
        self.strip_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.fields: dict[str, SettingField] = {}
        lines = []
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
            lines.append(line)
        self.settings_panel = Panel(PanelTable(*lines))
        self.settings_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.idle = QLabel("NOTHING IS MEASURING")
        self.idle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle.setStyleSheet("color: #8a8a8a; padding: 24px;")
        self.live_page = QWidget()
        live = QVBoxLayout(self.live_page)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)
        self.live_layout = QVBoxLayout()
        self.live_layout.setSpacing(8)
        live.addLayout(self.live_layout, 1)
        live.addWidget(self.idle)

        # вкладки: LIVE - что измеряется, SET - пороги (как вкладки в окнах приборов)
        self.tabs = PTabBar()
        for title in self.TABS:
            self.tabs.addTab(title)
        self.tabs.setTabToolTip(1, "Thresholds: when an instrument counts as measuring")
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
        WINDOW_EVENTS.changed.connect(self._update_tile)    # окно прибора открыли / закрыли, ошибка
        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            sig.connect(self._on_runs)
        self._on_runs()
        self.setMinimumWidth(560)
        self.recall_size(QSize(720, 420))
        self._rebuild()

    # ------------------------------------------------------------ состав
    def _rebuild(self) -> None:
        proxies = self.monitor.proxies()
        for p in [p for p in self.tiles if p not in proxies]:
            self.tiles.pop(p).deleteLater()
        for p in [p for p in self.panels if p not in proxies]:
            self.panels.pop(p).deleteLater()
        for i, p in enumerate(proxies):
            if p not in self.tiles:
                tile = InstrumentTile(p)
                tile.clicked.connect(lambda _=False, p=p: self.open_instrument(p))
                self.tiles[p] = tile
            self.strip_layout.insertWidget(i, self.tiles[p])
        for p in self.monitor.receivers():
            if p not in self.panels:
                self.panels[p] = LivePanel(p, self.monitor)
                self.live_layout.addWidget(self.panels[p], 1)
        for p in proxies:
            self._on_changed(p)
        self._update_visibility()

    # ------------------------------------------------------------ обновление
    def _on_changed(self, proxy) -> None:
        self._update_tile(proxy)
        panel = self.panels.get(proxy)
        if panel is not None:
            panel.refresh(self.monitor.activity(proxy))
            self._update_visibility()

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

    def _update_visibility(self) -> None:
        show_all = self.all_button.isChecked()
        shown = 0
        for p, panel in self.panels.items():
            vis = show_all or self.monitor.activity(p).measuring
            if panel.isVisibleTo(self.live_page) != vis:
                panel.setVisible(vis)
            shown += vis
        self.idle.setVisible(shown == 0)

    def visible_panels(self) -> list[LivePanel]:
        return [panel for panel in self.panels.values() if panel.isVisibleTo(self.live_page)]

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

    def closeEvent(self, event):  # noqa: N802 - Qt
        if self.quit_on_close:
            for fn in self.before_quit:
                fn()
        super().closeEvent(event)
        if self.quit_on_close:
            self.monitor.stop()
            try:
                WINDOW_EVENTS.changed.disconnect(self._update_tile)
            except TypeError:
                pass
            for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
                try:
                    sig.disconnect(self._on_runs)
                except TypeError:
                    pass
            QApplication.quit()
