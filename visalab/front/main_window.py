"""Главное окно: что сейчас измеряется.

Сверху - плитки всех открытых приборов (класс и коротко состояние; индикатор - прибор измеряет
или источник включён; клик - окно прибора). Ниже - панели живых данных приёмников, которые
сейчас измеряют (решает координатор, ``visalab.coord.ActivityMonitor``):

* ВАЦ - трейсы включённых измерений (по графику на каждую единицу: dB, deg, s...);
* ваттметр - показания точками за последнюю минуту (или трасса во времени, если включена);
* анализатор спектра - трасса, пик, шумовая дорожка.

У панели - источник сигнала, если координатор его нашёл: «← GEN 1 GHz  PATH −10.0 dB».
ALL - показывать и приёмники, которые сейчас ничего не измеряют. SET - пороги «измеряет / не измеряет»
(сохраняются в ini, группа MAIN/THRESHOLDS). Окно знает только ключи профилей.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..coord import ActivityMonitor, profile_name
from ..coord.activity import Activity
from ..core.params import ConnectionState
from ..core.scpi import si_format, si_parse
from ..core.trace import Trace
from .live_plot import LivePlot
from .params import IndicatorButton, Panel, PanelLine, PanelTable
from .theme import WINDOW_BAR_HEIGHT
from .window import Window, short_address

__all__ = ["MainWindow", "InstrumentTile", "LivePanel"]

#: сколько секунд показаний ваттметра на графике
PM_WINDOW_S = 60.0

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


def _output_on(proxy) -> bool:
    """Какой-нибудь выход прибора включён (ГСС, источник питания...)."""
    return any(proxy.value(k) is True for k in proxy.params if k.endswith(".output") or k == "output")


class InstrumentTile(IndicatorButton):
    """Плитка прибора: класс сверху, состояние снизу; индикатор - измеряет / выход включён."""

    def __init__(self, proxy, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.setObjectName("chip")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{proxy.name} {proxy.address}\nClick - open the instrument window")
        self.active = False
        self.setMinimumWidth(72)

    def indicated(self) -> bool:
        return self.active

    def show_state(self, name: str, line: str, active: bool) -> None:
        self.setText(f"{name}\n{line}")
        self.active = active
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
        head.addWidget(self.title)
        head.addWidget(self.address)
        head.addStretch()
        head.addWidget(self.source)
        head.addWidget(self.level)
        lay.addLayout(head)
        self.plots_box = QVBoxLayout()
        self.plots_box.setSpacing(6)
        lay.addLayout(self.plots_box)
        self.plots: list[LivePlot] = []
        super().__init__(body, parent=parent)
        self.address.setStyleSheet("color: #8a8a8a;")
        self.source.setStyleSheet("color: #8a8a8a;")

    def _plot(self, i: int) -> LivePlot:
        while len(self.plots) <= i:
            p = LivePlot(min_height=170)
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
        plot = self._plot(0)
        self._use_plots(1)
        tr = self.proxy.value("trace")
        if self.proxy.value("trace.state") is True and isinstance(tr, Trace):
            plot.x_range = None
            plot.set_traces([("", tr)])
        else:
            now = self.monitor.clock()
            pts = [(t - now, v) for t, v in self.monitor.history.get(self.proxy, ()) if now - t <= PM_WINDOW_S]
            plot.x_range = (-PM_WINDOW_S, 0.0)
            plot.set_traces([("", Trace(tuple(t for t, _ in pts), tuple(v for _, v in pts), "s", "dBm"))]
                            if pts else [], points=True)
        if a.connected:
            self.level.setText(_dbm(a.level) or "NO DATA")


class MainWindow(Window):
    """Главное окно лаборатории: плитки приборов и живые данные того, что измеряет."""

    SETTINGS_GROUP = "MAIN"

    def __init__(self, settings: QSettings, monitor: ActivityMonitor | None = None, parent=None):
        super().__init__(settings, parent)
        self.setWindowTitle("LAB")
        self.monitor = monitor or ActivityMonitor(parent=self)
        self.quit_on_close = False
        self.tiles: dict[object, InstrumentTile] = {}
        self.panels: dict[object, LivePanel] = {}

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
        self.set_button = QPushButton("SET")
        self.set_button.setObjectName("chip")
        self.set_button.setCheckable(True)
        self.set_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_button.setToolTip("Thresholds: when an instrument counts as measuring")
        self.strip_layout.addStretch()
        self.strip_layout.addWidget(self.all_button)
        self.strip_layout.addWidget(self.set_button)
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
            f.setFixedWidth(130)
            f.show_value(getattr(self.monitor.th, name))
            f.editingFinished.connect(lambda n=name: self._apply_setting(n))
            self.fields[name] = f
            line = PanelLine(label, f)
            line.line_layout.addStretch()
            line.setToolTip(tip)
            lines.append(line)
        self.settings_panel = Panel(PanelTable(*lines))
        self.settings_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.settings_panel.setVisible(False)
        self.set_button.toggled.connect(self.settings_panel.setVisible)

        self.idle = QLabel("NOTHING IS MEASURING")
        self.idle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idle.setStyleSheet("color: #8a8a8a; padding: 24px;")
        self.live_layout = QVBoxLayout()
        self.live_layout.setSpacing(8)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.strip_panel)
        lay.addWidget(self.settings_panel)
        lay.addLayout(self.live_layout)
        lay.addWidget(self.idle)
        lay.addStretch()

        self.monitor.roster_changed.connect(self._rebuild)
        self.monitor.changed.connect(self._on_changed)
        self._timer = QTimer(self)                  # время на графике ваттметра идёт и без новых точек
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
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
                self.live_layout.addWidget(self.panels[p])
        for p in proxies:
            self._on_changed(p)
        self._update_visibility()

    # ------------------------------------------------------------ обновление
    def _on_changed(self, proxy) -> None:
        tile = self.tiles.get(proxy)
        if tile is None:
            return
        prof = profile_name(proxy)
        name = self.monitor.name(proxy)
        connected = proxy.state is ConnectionState.CONNECTED
        if prof in ("network_analyzer", "spectrum_analyzer", "power_meter"):
            a = self.monitor.activity(proxy)
            if not connected:
                line = "OFF"
            elif not a.measuring:
                line = "IDLE"
            elif prof == "network_analyzer":
                line = "SWEEP"
            else:
                line = f"{a.level:.1f} dBm" if a.level is not None else "MEAS"
            tile.show_state(name, line, a.measuring)
            panel = self.panels.get(proxy)
            if panel is not None:
                panel.refresh(a)
                self._update_visibility()
        elif prof == "signal_generator":
            on = connected and proxy.value("rf.output") is True
            line = si_format(proxy.value("rf.frequency"), "Hz", 4) if on else ("RF OFF" if connected else "OFF")
            tile.show_state(name, line, on)
        else:
            on = connected and _output_on(proxy)
            tile.show_state(name, ("OUT ON" if on else "ON") if connected else "OFF", on)

    def _tick(self) -> None:
        for p, panel in self.panels.items():
            if panel.isVisible() and panel.profile == "power_meter":
                panel.refresh(self.monitor.activity(p))

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
            if panel.isVisibleTo(self) != vis:
                panel.setVisible(vis)
            shown += vis
        self.idle.setVisible(shown == 0)

    def visible_panels(self) -> list[LivePanel]:
        return [panel for panel in self.panels.values() if panel.isVisibleTo(self)]

    # ------------------------------------------------------------ окна приборов
    def open_instrument(self, proxy) -> None:
        """Показать окно прибора (окна открыты в этом же процессе, возможно скрыты)."""
        for w in QApplication.topLevelWidgets():
            if getattr(w, "proxy", None) is proxy and w is not self:
                if w.isMinimized():
                    w.showNormal()
                w.show()
                w.raise_()
                w.activateWindow()
                return

    def closeEvent(self, event):  # noqa: N802 - Qt
        super().closeEvent(event)
        if self.quit_on_close:
            QApplication.quit()

