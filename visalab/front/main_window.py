"""Главное окно графиков (``MainWindow``): двухпанельный экран живых данных (``LIVE``) и настройки (``SET``).

Экран ``LIVE`` разделён горизонтальным делителем ``GraphSplitter``:
* Слева (50%, без прокрутки) — активный приёмник, определяемый по видеоуровню (или ВАЦ, если никто не измеряет).
* Справа (50%, вертикальная прокрутка) — все остальные выбранные графики и таблицы токов.

Плитки приборов (VNA, SA, PM, GEN, PULSE, PSU) принадлежат независимому окну инструментов
(:class:`InstrumentDockWindow`). Панели графиков, таблицы и базовые виджеты вынесены в модули
``live_panels.py``, ``live_tables.py`` и ``widgets.py``.
"""
from __future__ import annotations

import json
import math
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from ..coord import COORD, ActivityMonitor, Coordinator, profile_name
from ..core.trace import Trace
from .instrument_dock import (InstrumentDock, InstrumentDockWindow, InstrumentTile, TILE_MIN_HEIGHT,
                              unregister_dock)
from .instrument_window import WINDOW_EVENTS, all_instrument_windows, window_for
from .live_panels import DEVICE_DIVS_Y, CurrentPanel, LivePanel, ScenarioChip, SincEmblem
from .live_tables import CURRENT_UNITS, PLOT_MIN_HEIGHT, PM_UNITS, CurrentTable, PowerTable
from .params import Panel, PanelLine, PanelTable, PTabBar
from .split_layout import PLOT_LAYOUT_MODES, GraphSplitter
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_INVERT_HOVER, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE,
                    UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT, UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY,
                    WINDOW_BAR_HEIGHT, apply_theme, checkbox_qss, current_theme, render_qss, scrollbar_qss)
from .widgets import UnitCommitLineEdit
from .window import CustomFullButton, Window, WindowBar, short_address

__all__ = [
    "MainWindow",
    "SincEmblem",
    "InstrumentTile",
    "TILE_MIN_HEIGHT",
    "LivePanel",
    "CurrentPanel",
    "ScenarioChip",
    "PowerTable",
    "CurrentTable",
    "PLOT_LAYOUT_MODES",
    "PM_UNITS",
    "CURRENT_UNITS",
]

RIGHT_PANEL_MIN_HEIGHT = 320
UI_REFRESH_MS = 60

#: страница SET: (подпись, поле Thresholds, единица, множитель показа, подсказка)
SETTINGS = (
    ("SA MARG", "sa_margin_db", "dB", 1.0,
     "Spectrum analyzer shows peak marker and source when peak is this much above the noise floor"),
    ("PM FLR", "pm_floor_dbm", "dBm", 1.0,
     "Power meter matches a signal source when its reading is not below this level"),
    ("HOLD", "hold_s", "s", 1.0,
     "Keep showing an instrument this long after it stops outputting data"),
    ("FTOL", "freq_tol_hz", "Hz", 1.0,
     "Frequency match with a generator: at least this close"),
    ("FREL", "freq_tol_rel", "%", 100.0,
     "Frequency match with a generator: or this close relative to the frequency"),
)


class MainWindow(Window):
    """Главное окно графиков: двухпанельный LIVE-экран и настройки порогов/отображения (SET)."""

    SETTINGS_GROUP = "MAIN"
    TABS = ("LIVE", "SET")

    def __init__(self, settings: QSettings, monitor: ActivityMonitor | None = None, parent=None,
                 coordinator: Coordinator | None = None):
        saved_theme = str(settings.value(f"{self.SETTINGS_GROUP}/THEME", "light", type=str) or "light")
        apply_theme(saved_theme)
        super().__init__(settings, parent)
        self.setWindowTitle("LAB")
        self.monitor = monitor or ActivityMonitor(parent=self)
        self.coord = coordinator if coordinator is not None else COORD
        self.quit_on_close = False
        #: вызывается при закрытии главного окна до выхода (окна приборов ещё в прежнем виде)
        self.before_quit: list = []

        self.panels: dict[object, LivePanel] = {}
        self.current_panels: dict[tuple[object, int], CurrentPanel] = {}
        self.current_controls: dict[str, QCheckBox] = {}
        self.restore_chips: dict[str, QPushButton] = {}
        self.display_rules: dict[str, dict[str, Any]] = {}
        self.display_controls: dict[str, tuple[QCheckBox, QCheckBox, UnitCommitLineEdit]] = {}
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
        self.enabled_current_panels: set[str] = set(self._load_list(f"{self.SETTINGS_GROUP}/ENABLED_CURRENT_TABLES"))

        # Заголовок окна графиков: эмблема sinc слева, справа - фишка активного сценария и (_ / ⛶ / ×)
        self.titleBar.title_label.hide()
        _scenario_qss = render_qss("""
            ScenarioChip {
                background-color: $UI_COLOR_BACKGROUND;
                color: $UI_COLOR_TEXT_MUTED;
                border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
                border-radius: ${UI_BORDER_RADIUS_PX}px;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_FONT_SIZE_PT}pt;
                padding: 0 10px;
            }
            ScenarioChip:hover {
                background-color: $UI_COLOR_INVERT_HOVER;
                color: $UI_COLOR_TEXT_INVERSE;
                border-color: $UI_COLOR_INVERT_HOVER;
                font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            }
        """)
        self.titleBar.setStyleSheet(WindowBar.STYLE_SHEET + "\n" + _scenario_qss)
        self.emblem = SincEmblem(self.titleBar)
        self.titleBar.insert_widget(self.emblem, 0)

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

        # Верхняя панель вкладок (LIVE | SET) и кнопок управления
        self.restore_layout = QHBoxLayout()
        self.restore_layout.setContentsMargins(0, 0, 0, 0)
        self.restore_layout.setSpacing(4)

        self.all_button = self._chip_btn(
            "ALL", "Show all receivers, also those without data now",
            checkable=True,
        )
        self.all_button.setChecked(settings.value(f"{self.SETTINGS_GROUP}/SHOW_ALL", False, type=bool))
        self.all_button.toggled.connect(self._on_all)

        self.dock_button = self._chip_btn(
            "DOCK", "Show / hide the instrument dock panel (attach or detach instrument windows)",
            checkable=True,
        )
        self.dock_button.clicked.connect(self._on_dock_button)

        self.theme_button = self._chip_btn(
            "DARK" if current_theme() == "light" else "LIGHT",
            "Switch between light and dark theme",
        )
        self.theme_button.clicked.connect(self.toggle_theme)

        self.current_button = self._chip_btn(
            "CURR", "Choose power supply channels to show current tables vs frequency and RF power",
            checkable=True,
        )
        self.current_button.clicked.connect(self._open_current_menu)
        self.current_button.hide()

        # Страница SET: пороги, отображаемые графики и таблицы токов
        self.fields: dict[str, UnitCommitLineEdit] = {}
        by_name: dict[str, PanelLine] = {}
        for label, name, unit, scale, tip in SETTINGS:
            saved = settings.value(f"{self.SETTINGS_GROUP}/THRESHOLDS/{name}", None)
            if saved is not None:
                try:
                    setattr(self.monitor.th, name, float(saved))
                except (TypeError, ValueError):
                    pass
            f = UnitCommitLineEdit(unit, scale)
            f.setToolTip(tip)
            f.setMinimumWidth(130)
            f.show_value(getattr(self.monitor.th, name))
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

        # Двухпанельный экран LIVE: слева активный по видеоуровню (без скролла), справа скролл остальных
        self.idle = QLabel("NOTHING IS MEASURING")
        self.idle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle.setStyleSheet(render_qss("color: $UI_COLOR_BORDER_LIGHT; padding: 24px;"))
        self.live_page = QWidget()
        live = QVBoxLayout(self.live_page)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)

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

        self._live_host = self._active_host
        self.live_layout = self.active_layout
        self.live_scroll = self.live_splitter
        self._active_panel_key: str | None = None
        self._right_panel_keys: list[str] = []

        live.addWidget(self.live_splitter, 1)
        live.addWidget(self.idle)

        self.tabs = PTabBar()
        for title in self.TABS:
            self.tabs.addTab(title)
        self.tabs.setTabToolTip(0, "Live graphs and measurement tables")
        self.tabs.setTabToolTip(1, "Thresholds and displayed graphs settings")
        tabs_bar = QWidget()
        tabs_lay = QHBoxLayout(tabs_bar)
        tabs_lay.setContentsMargins(0, 0, 0, 0)
        tabs_lay.setSpacing(6)
        tabs_lay.addWidget(self.tabs, 1)
        tabs_lay.addLayout(self.restore_layout)
        tabs_lay.addWidget(self.dock_button)
        tabs_lay.addWidget(self.theme_button)
        tabs_lay.addWidget(self.current_button)
        tabs_lay.addWidget(self.all_button)
        self.tabs_panel = Panel(tabs_bar)
        self.tabs_panel.setStyleSheet(
            Panel.STYLE_SHEET + render_qss('\nQMenu#psmenu::separator { height: 1px; background: $UI_COLOR_BORDER; margin: 4px 6px; }')
        )
        self.tabs_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # Независимое окно инструментов: плитки VNA/SA/PM/GEN/PULSE/PSU в заголовке, FAV/SET и многоколоночная сетка
        self.instrument_dock_window = InstrumentDockWindow(settings, monitor=self.monitor)
        self.instrument_dock = self.instrument_dock_window.dock
        self.tiles = self.instrument_dock_window.tiles
        self.instrument_dock.changed.connect(self._on_dock_changed)
        self.instrument_dock_window.visibility_changed.connect(self._on_dock_visibility_changed)
        self.instrument_dock_window.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.tabs_panel)
        lay.addWidget(self.live_page, 1)
        lay.addWidget(self.settings_panel)
        lay.addStretch(0)
        self.tabs.currentChanged.connect(self.set_page)
        self.set_page(settings.value(f"{self.SETTINGS_GROUP}/TAB", 0, type=int))

        self.monitor.roster_changed.connect(self._rebuild)
        self.monitor.changed.connect(self._on_changed)
        WINDOW_EVENTS.changed.connect(self._on_window_event)
        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            sig.connect(self._on_runs)
        self._on_runs()
        self.setMinimumWidth(560)
        self.recall_size(QSize(860, 760))
        self._rebuild()
        self.restore_docked_windows()

    @staticmethod
    def _chip_btn(text: str, tip: str, *, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("chip")
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(28)
        b.setToolTip(tip)
        return b

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
        proxies = self.monitor.proxies()
        if not sip.isdeleted(self.instrument_dock_window):
            self.instrument_dock_window.rebuild_tiles()
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
        for p in self.monitor.receivers():
            if p not in self.panels:
                panel = LivePanel(p, self.monitor, settings=self.settings)
                panel.close_requested.connect(self.hide_panel)
                panel.hide()
                self.panels[p] = panel
                if panel.panel_key not in self.panel_order:
                    self.panel_order.append(panel.panel_key)
        for p, ch, _lbl in psu_ch_list:
            ck = (p, ch)
            if ck not in self.current_panels:
                cpanel = CurrentPanel(p, ch, self.monitor, settings=self.settings)
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
            for ck in self.current_panels:
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
        if sip.isdeleted(self):
            return
        dw = getattr(self, "instrument_dock_window", None)
        if dw is not None and not sip.isdeleted(dw):
            dw.update_tile(proxy)

    def _on_runs(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        runs = self.coord.runs()
        self.scenario_chip.show_runs(runs)
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
            self.tabs.setCurrentIndex(index)
            return
        self.live_page.setVisible(index == 0)
        self.settings_panel.setVisible(index == 1)
        self.settings.setValue(f"{self.SETTINGS_GROUP}/TAB", index)

    def _apply_setting(self, name: str) -> None:
        f = self.fields[name]
        try:
            value = f.parsed()
        except ValueError:
            f.show_value(getattr(self.monitor.th, name))
            return
        self.settings.setValue(f"{self.SETTINGS_GROUP}/THRESHOLDS/{name}", value)
        self.monitor.set_threshold(name, value)
        f.show_value(value)

    def _on_all(self, on: bool) -> None:
        self.settings.setValue(f"{self.SETTINGS_GROUP}/SHOW_ALL", bool(on))
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
            empty.setStyleSheet(render_qss("color: $UI_COLOR_BORDER_LIGHT;"))
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
            level_edit = UnitCommitLineEdit(unit, 1.0)
            level_edit.setMinimumWidth(110)
            level_edit.show_value(rule["level"])
            level_edit.setEnabled(shown and rule["by_level"])
            level_edit.setToolTip(f"Video / signal level threshold for {name} ({unit}): show when level >= this value")
            show_cb.toggled.connect(lambda checked, k=key: self.set_panel_shown(k, checked))
            level_cb.toggled.connect(lambda checked, k=key: self.set_panel_by_level(k, checked))
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
        self.set_panel_shown(self._panel_key(proxy), False)

    def show_panel(self, key: str) -> None:
        self.set_panel_shown(key, True)

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

    def _update_restore_chips(self, ordered: list) -> None:
        wanted = {self._panel_key(p): self.monitor.name(p)
                  for p in ordered if self._is_eligible(p) and self._panel_key(p) in self.hidden_panels}
        for key in [k for k in self.restore_chips if k not in wanted]:
            btn = self.restore_chips.pop(key)
            self.restore_layout.removeWidget(btn)
            btn.deleteLater()
        for key, name in wanted.items():
            if key not in self.restore_chips:
                b = self._chip_btn(f"+ {name}", f"Show {name} measurement panel")
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
        if self._active_panel_key is None:
            return None
        for p in (*self.panels.values(), *self.current_panels.values()):
            if p.panel_key == self._active_panel_key:
                return p
        return None

    def right_panels(self) -> list:
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

    # ------------------------------------------------------------ тема оформления (LIGHT / DARK)
    def set_theme(self, name: str, *, save: bool = True) -> None:
        theme = apply_theme(name)
        if save:
            self.settings.setValue(f"{self.SETTINGS_GROUP}/THEME", theme)
        self.theme_button.setText("DARK" if theme == "light" else "LIGHT")

    def toggle_theme(self) -> None:
        self.set_theme("dark" if current_theme() == "light" else "light")

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
            dw.cleanup()
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
