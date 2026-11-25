"""Главное окно графиков (``GraphsWindow``): живые данные лаборатории поверх ``PlotWidget``.

Структура по аналогии с ``InstrumentsWindow``:
* Верхний ``WindowBar``: эмблема ``SincEmblem``, кнопка ``INST`` (показать/скрыть
  ``InstrumentsWindow``), переключатель темы ``LIGHT/DARK``, индикатор активного сценария
  (``ScenarioChip``) и кнопки ``minimize``, ``maximize`` (``F11``), ``close``.
* Основная область разделена горизонтальным ``GraphSplitter`` на две части:
  - слева — **монолитный** главный график на всю высоту (без скроллов, не исчезает и не открепляется);
  - справа — вертикально прокручиваемая лента остальных графиков с живым перетаскиванием за шапку,
    сортировкой с линией вставки и автоскроллом, откреплением в отдельное окно ``Window`` при
    отпускании вне правой части, возвратом обратно перетаскиванием или по ``×``, кликом по шапке —
    переводом в главный график, сохранением порядка и делителя в ``QSettings``.
* Подключённый ``ActivityMonitor`` автоматически строит карточки для запущенных приёмников
  (``VNA``, ``SA``, ``PM``) и каналов источников питания (``PSU CH1``..``CHn`` с переключением
  режима ``TIME`` — V/I/P по времени, и ``FREQ`` — ток по частоте и мощности генератора).
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QRect, QSettings, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from ..coord import COORD, ActivityMonitor, Coordinator, profile_name
from ..core.trace import Trace
from .params import _repolish
from .plot_widget import PlotSeries, PlotSettingsWindow, PlotWidget
from .split_layout import GraphSplitter
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_CONTROL, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL,
                    UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT,
                    UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, UI_TITLE_BAR_ITEM_SIZE_PX, WINDOW_BAR_HEIGHT,
                    apply_theme, current_theme, render_qss, scrollbar_qss)
from .widgets import ScenarioChip, SincEmblem
from .window import (CustomFullButton, PanelClearButton, PanelSettingsButton, Window, WindowBar,
                     set_mac_floating_level)

__all__ = ["DEFAULT_MAIN_GRAPH", "DEFAULT_SIDE_GRAPHS", "GraphCard", "GraphsWindow"]

DEFAULT_MAIN_GRAPH: str = "VNA"
DEFAULT_SIDE_GRAPHS: tuple[str, ...] = (
    "SA",
    "PM",
    "PSU CH1",
)
UI_REFRESH_MS = 60


_MODE_BTN_QSS = render_qss("""
    QPushButton {
        min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
        padding: 0 8px;
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT_MUTED;
        border: none;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    QPushButton:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
    }
""")


_BAR_CHIP_QSS = render_qss("""
    QPushButton {
        min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
        padding: 0 10px;
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT_MUTED;
        border: none;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    QPushButton:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
    }
    QPushButton:checked {
        background-color: $UI_COLOR_INVERT;
        color: $UI_COLOR_TEXT_INVERSE;
    }
    QPushButton:checked:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
    }
""")


_SCENARIO_CHIP_QSS = render_qss("""
    ScenarioChip {
        min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
        padding: 0 10px;
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT_MUTED;
        border: none;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
    }
    ScenarioChip:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
""")


class GraphCard(QFrame):
    """Карточка графика: монолитная (главный график слева) или перетаскиваемая/открепляемая (справа)."""

    drag_started = pyqtSignal(str, QPoint, QPoint)
    settings_requested = pyqtSignal(str)
    promote_requested = pyqtSignal(str)
    mode_changed = pyqtSignal(str, str)

    STYLE_SHEET = render_qss("""
        GraphCard {
            background-color: $UI_COLOR_PANEL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        GraphCard[floating="true"], GraphCard[monolithic="true"] {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: 0px;
        }
        GraphCard QWidget#cardBar {
            background-color: $UI_COLOR_CONTROL;
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-top-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-top-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        GraphCard QLabel#cardTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            padding: 0 4px;
        }
        GraphCard QWidget#cardBody {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-bottom-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-bottom-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        GraphCard[floating="true"] QWidget#cardBody, GraphCard[monolithic="true"] QWidget#cardBody {
            border-radius: 0px;
        }
        GraphCard QLabel#plotPlaceholder {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
        }
        GraphCard IconButtonBase {
            min-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 0;
        }
        GraphCard IconButtonBase:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
        }
        GraphCard QPushButton#modeBtn {
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            padding: 0 8px;
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT_MUTED;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        GraphCard QPushButton#modeBtn:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            border-color: $UI_COLOR_INVERT_HOVER;
        }
    """)

    def __init__(self, title: str, *, monolithic: bool = False, min_body_height: int = 240,
                 modes: tuple[str, ...] = (), initial_mode: str = "", parent=None):
        super().__init__(parent)
        self.graph_name = title
        self.monolithic = bool(monolithic)
        self._min_body_height = int(min_body_height)
        self._modes: tuple[str, ...] = tuple(modes)
        self._mode: str = initial_mode if initial_mode in self._modes else (self._modes[0] if self._modes else "")
        self._floating = False
        self._drag_start_pos: QPoint | None = None
        self._drag_hotspot = QPoint()
        self.setStyleSheet(self.STYLE_SHEET)
        self.setProperty("monolithic", self.monolithic)
        if self.monolithic:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.bar = QWidget(self)
        self.bar.setObjectName("cardBar")
        self.bar.setFixedHeight(WINDOW_BAR_HEIGHT)
        self._bar_layout = QHBoxLayout(self.bar)
        self._bar_layout.setContentsMargins(8, 7, 6, 7)
        self._bar_layout.setSpacing(4)

        self.title_label = QLabel(title, self.bar)
        self.title_label.setObjectName("cardTitle")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

        self.mode_button: QPushButton | None = None
        if len(self._modes) >= 2:
            self.mode_button = self._make_mode_button(self.bar)

        self.settings_button = PanelSettingsButton(self.bar)
        self.settings_button.setToolTip(f"{title} settings")
        self.settings_button.clicked.connect(lambda: self.settings_requested.emit(self.graph_name))
        self.clear_button = PanelClearButton(self.bar)
        self.clear_button.setToolTip(f"Reset {title} view")

        self._bar_layout.addWidget(self.title_label, 1)
        if self.mode_button is not None:
            self._bar_layout.addWidget(self.mode_button)
        self._bar_layout.addWidget(self.settings_button)
        self._bar_layout.addWidget(self.clear_button)

        self.bar.installEventFilter(self)
        self.title_label.installEventFilter(self)

        self.body = QWidget(self)
        self.body.setObjectName("cardBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 4, 4, 4)
        self.body_layout.setSpacing(0)

        self.plot = PlotWidget(empty_text=title, parent=self.body)
        self.body_layout.addWidget(self.plot, 1)
        self.clear_button.clicked.connect(self.plot.reset_view)

        hb = PanelSettingsButton(self.body)
        hb.setToolTip(f"{title} settings")
        hb.clicked.connect(lambda: self.settings_requested.emit(self.graph_name))
        hb.hide()
        self.hover_settings_button: PanelSettingsButton = hb

        self.hover_mode_button: QPushButton | None = None
        if len(self._modes) >= 2:
            hm = self._make_mode_button(self.body)
            hm.hide()
            self.hover_mode_button = hm

        self.body.installEventFilter(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.bar)
        root.addWidget(self.body, 1)
        self._sync_mode_buttons()
        self.set_monolithic(self.monolithic)

    def _make_mode_button(self, parent: QWidget) -> QPushButton:
        b = QPushButton(self._mode, parent)
        b.setObjectName("modeBtn")
        b.setFixedHeight(UI_TITLE_BAR_ITEM_SIZE_PX)
        b.setStyleSheet(_MODE_BTN_QSS)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(self.cycle_mode)
        return b

    @property
    def current_mode(self) -> str:
        return self._mode

    def _sync_mode_buttons(self) -> None:
        if not self._modes:
            return
        idx = self._modes.index(self._mode) if self._mode in self._modes else 0
        nxt = self._modes[(idx + 1) % len(self._modes)]
        tip = f"Graph mode: {self._mode} (click to switch to {nxt})"
        for btn in (self.mode_button, self.hover_mode_button):
            if btn is not None and not sip.isdeleted(btn):
                btn.setText(self._mode)
                btn.setToolTip(tip)

    def set_mode(self, mode: str, *, emit: bool = False) -> None:
        if not self._modes or mode not in self._modes or mode == self._mode:
            return
        self._mode = mode
        self._sync_mode_buttons()
        if self.monolithic:
            self._place_hover_settings()
        if emit:
            self.mode_changed.emit(self.graph_name, self._mode)

    def cycle_mode(self) -> None:
        if len(self._modes) < 2:
            return
        idx = self._modes.index(self._mode) if self._mode in self._modes else 0
        self.set_mode(self._modes[(idx + 1) % len(self._modes)], emit=True)

    def set_monolithic(self, monolithic: bool) -> None:
        self.monolithic = bool(monolithic)
        self._drag_start_pos = None
        self.setProperty("monolithic", self.monolithic)
        if self.monolithic:
            self.bar.hide()
            self.plot.set_top_pad(0.0)
            extra = 66.0 if self.hover_mode_button is not None else 0.0
            self.plot.set_top_right_pad(UI_TITLE_BAR_ITEM_SIZE_PX + 8.0 + extra)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.body.setMinimumHeight(140)
            self.title_label.setCursor(Qt.CursorShape.ArrowCursor)
            self.bar.setCursor(Qt.CursorShape.ArrowCursor)
            self.bar.setToolTip("")
            self.hover_settings_button.hide()
            if self.hover_mode_button is not None:
                self.hover_mode_button.hide()
        else:
            self.bar.setVisible(not self._floating)
            self.plot.set_top_pad(0.0)
            self.plot.set_top_right_pad(0.0)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.body.setMinimumHeight(self._min_body_height)
            self.title_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.bar.setCursor(Qt.CursorShape.PointingHandCursor)
            self.bar.setToolTip(f"Click to make {self.graph_name} main · Drag to reorder or detach")
            self.hover_settings_button.hide()
            if self.hover_mode_button is not None:
                self.hover_mode_button.hide()
        _repolish(self)

    def _place_hover_settings(self) -> None:
        hb = self.hover_settings_button
        if hb is None or sip.isdeleted(hb):
            return
        sx = max(4, self.body.width() - UI_TITLE_BAR_ITEM_SIZE_PX - 24)
        hb.setGeometry(sx, 22, UI_TITLE_BAR_ITEM_SIZE_PX, UI_TITLE_BAR_ITEM_SIZE_PX)
        hb.raise_()
        hm = self.hover_mode_button
        if hm is not None and not sip.isdeleted(hm):
            mw = max(48, hm.sizeHint().width())
            hm.setGeometry(max(4, sx - mw - 4), 22, mw, UI_TITLE_BAR_ITEM_SIZE_PX)
            hm.raise_()

    def set_floating(self, floating: bool) -> None:
        floating = bool(floating)
        self._floating = floating
        self.setProperty("floating", floating)
        self.bar.setVisible(not floating)
        if floating:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.body.setMinimumHeight(140)
            self.title_label.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.body.setMinimumHeight(self._min_body_height)
            self.title_label.setCursor(Qt.CursorShape.OpenHandCursor)
        _repolish(self)

    def take_bar_widgets(self) -> tuple[QLabel, QPushButton | None, PanelSettingsButton, PanelClearButton]:
        widgets = [self.title_label, self.settings_button, self.clear_button]
        if self.mode_button is not None:
            widgets.append(self.mode_button)
        for w in widgets:
            self._bar_layout.removeWidget(w)
        return (self.title_label, self.mode_button, self.settings_button, self.clear_button)

    def restore_bar_widgets(self) -> None:
        widgets = [self.title_label]
        if self.mode_button is not None:
            widgets.append(self.mode_button)
        widgets.extend([self.settings_button, self.clear_button])
        for w in widgets:
            w.setParent(self.bar)
            w.show()
        self._bar_layout.addWidget(self.title_label, 1)
        if self.mode_button is not None:
            self._bar_layout.addWidget(self.mode_button)
        self._bar_layout.addWidget(self.settings_button)
        self._bar_layout.addWidget(self.clear_button)
        _repolish(self.title_label)

    def _is_drag_target(self, obj, pos: QPoint) -> bool:
        if obj is self.title_label:
            return True
        if obj is self.bar:
            child = self.bar.childAt(pos)
            while child is not None and child is not self.bar:
                if isinstance(child, QAbstractButton) or child is self.title_label:
                    return False
                child = child.parentWidget()
            return True
        return False

    def eventFilter(self, obj, event):  # noqa: N802 - Qt
        if obj is self.body and self.hover_settings_button is not None:
            if not self.monolithic:
                return super().eventFilter(obj, event)
            et = event.type()
            if et == QEvent.Type.Resize:
                self._place_hover_settings()
            elif et == QEvent.Type.Enter:
                self._place_hover_settings()
                self.hover_settings_button.show()
                if self.hover_mode_button is not None:
                    self.hover_mode_button.show()
            elif et == QEvent.Type.Leave:
                self.hover_settings_button.hide()
                if self.hover_mode_button is not None:
                    self.hover_mode_button.hide()
            return super().eventFilter(obj, event)
        if self.monolithic or self._floating:
            return super().eventFilter(obj, event)
        et = event.type()
        if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self._is_drag_target(obj, event.position().toPoint()):
                gpos = event.globalPosition().toPoint()
                self._drag_start_pos = gpos
                self._drag_hotspot = self.bar.mapFromGlobal(gpos)
                return True
        elif et == QEvent.Type.MouseMove and self._drag_start_pos is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                gpos = event.globalPosition().toPoint()
                if (gpos - self._drag_start_pos).manhattanLength() >= QApplication.startDragDistance():
                    hotspot = QPoint(self._drag_hotspot)
                    self._drag_start_pos = None
                    self.drag_started.emit(self.graph_name, gpos, hotspot)
                    return True
            else:
                self._drag_start_pos = None
        elif et == QEvent.Type.MouseButtonRelease:
            was_click = self._drag_start_pos is not None and event.button() == Qt.MouseButton.LeftButton
            self._drag_start_pos = None
            if was_click:
                self.promote_requested.emit(self.graph_name)
                return True
        return super().eventFilter(obj, event)


class _FloatingGraphBar(WindowBar):
    """Заголовок откреплённого окна графика: живое перемещение окна + возврат при отпускании над правой лентой."""

    drag_moved = pyqtSignal(QPoint)
    drag_finished = pyqtSignal(QPoint)

    STYLE_SHEET_EXTRA = render_qss("""
        WindowBar QLabel#cardTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            padding: 0 4px;
        }
        WindowBar QPushButton#modeBtn {
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            padding: 0 8px;
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT_MUTED;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        WindowBar QPushButton#modeBtn:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            border-color: $UI_COLOR_INVERT_HOVER;
        }
    """)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos: QPoint | None = None
        self._dragging = False
        self._drag_offset = QPoint()
        self.setStyleSheet(self.STYLE_SHEET + "\n" + self.STYLE_SHEET_EXTRA)

    def mousePressEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton and self.canDrag(e.pos()):
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self._owner_window().pos()
            self._dragging = False
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        if self._press_pos is not None or self._dragging:
            gpos = e.globalPosition().toPoint()
            if not (e.buttons() & Qt.MouseButton.LeftButton):
                was_dragging = self._dragging
                self._dragging = False
                self._press_pos = None
                if was_dragging:
                    self.drag_finished.emit(gpos)
                return
            if not self._dragging:
                if (gpos - self._press_pos).manhattanLength() < QApplication.startDragDistance():
                    e.accept()
                    return
                self._dragging = True
            self._owner_window().move(gpos - self._drag_offset)
            self.drag_moved.emit(gpos)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton and (self._dragging or self._press_pos is not None):
            was_dragging = self._dragging
            self._dragging = False
            self._press_pos = None
            if was_dragging:
                self.drag_finished.emit(e.globalPosition().toPoint())
            e.accept()
            return
        super().mouseReleaseEvent(e)


class _FloatingGraphWindow(Window):
    """Обычное безрамочное окно ``Window`` для откреплённой карточки графика:
    живое перемещение за заголовок, свободное растягивание за края, возврат в правую ленту по перетаскиванию или ``×``."""

    dock_back_requested = pyqtSignal(str)
    drag_moved = pyqtSignal(str, QPoint)
    drag_dropped = pyqtSignal(str, QPoint)
    activated = pyqtSignal(str)

    def __init__(self, card: GraphCard, settings: QSettings, width_px: int, height_px: int, parent=None):
        super().__init__(settings, parent)
        self.card = card
        self._closing_for_dock = False
        self._size_key = f"WINDOW_SIZE/GraphFloat.{card.graph_name}"
        self._pos_key = f"WINDOW_POS/GraphFloat.{card.graph_name}"
        self._heights_key = f"WINDOW_HEIGHTS/GraphFloat.{card.graph_name}"

        bar = _FloatingGraphBar(self)
        self.setTitleBar(bar)
        self.titleBar.raise_()
        self.titleBar.title_label.hide()
        self.titleBar.minimized_button.hide()
        self.setWindowTitle(card.graph_name)

        title_lbl, mode_btn, set_btn, clr_btn = card.take_bar_widgets()
        title_lbl.setParent(self.titleBar)
        self.titleBar.insert_widget(title_lbl, 0, 1)
        title_lbl.show()
        _repolish(title_lbl)

        controls = [b for b in (mode_btn, set_btn, clr_btn) if b is not None]
        for btn in controls:
            btn.setParent(self.titleBar)
            self.titleBar.insert_control_widget(btn, after_minimize=False)
            btn.show()

        bar.drag_moved.connect(lambda gpos: self.drag_moved.emit(card.graph_name, gpos))
        bar.drag_finished.connect(lambda gpos: self.drag_dropped.emit(card.graph_name, gpos))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, WINDOW_BAR_HEIGHT, 0, 0)
        lay.setSpacing(0)

        card.setParent(self)
        card.set_floating(True)
        card.show()
        lay.addWidget(card, 1)

        w = max(340, min(int(width_px), 680))
        h = max(240, min(int(height_px), 520))
        self.setMinimumWidth(300)
        self.setMinimumHeight(WINDOW_BAR_HEIGHT + 160)
        self.resize(w, h)
        self._ensure_resizable()

    def release_card(self) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.card):
            return
        widgets = [self.card.title_label, self.card.settings_button, self.card.clear_button]
        if self.card.mode_button is not None:
            widgets.append(self.card.mode_button)
        for w in widgets:
            self.titleBar.hBoxLayout.removeWidget(w)
        if self.layout() is not None:
            self.layout().removeWidget(self.card)
        self.card.restore_bar_widgets()

    def event(self, e):  # noqa: N802 - Qt
        res = super().event(e)
        if e.type() == QEvent.Type.WindowActivate and not sip.isdeleted(self.card):
            self.activated.emit(self.card.graph_name)
        return res

    def move_clamped(self, pos: QPoint) -> None:
        self.move(self._clamp_pos(pos))

    def closeEvent(self, event):  # noqa: N802 - Qt
        if not self._closing_for_dock and not sip.isdeleted(self.card):
            self.dock_back_requested.emit(self.card.graph_name)
            event.accept()
            return
        super().closeEvent(event)


class _GraphListHost(QWidget):
    """Контейнер карточек графиков в правой ленте с живым перетаскиванием, линией вставки и автоскроллом."""

    order_changed = pyqtSignal(list)
    live_drag_moved = pyqtSignal(str, QPoint, QPoint)
    live_drag_finished = pyqtSignal(str, QPoint)
    live_drag_cancelled = pyqtSignal(str)

    _SCROLL_MARGIN_PX = 36
    _SCROLL_STEP_PX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("paneHost")
        self._scroll_area: QScrollArea | None = None
        self._scroll_delta = 0
        self._active_drag_name: str | None = None
        self._active_drag_hotspot = QPoint()

        self._indicator = QFrame(self)
        self._indicator.setObjectName("dropIndicator")
        self._indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._indicator.setFixedHeight(3)
        self._indicator.hide()

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(25)
        self._scroll_timer.timeout.connect(self._auto_scroll_tick)

    def bind_scroll(self, scroll: QScrollArea) -> None:
        self._scroll_area = scroll

    def begin_live_drag(self, name: str, hotspot: QPoint) -> None:
        self._active_drag_name = name
        self._active_drag_hotspot = QPoint(hotspot)
        self.grabMouse(Qt.CursorShape.ClosedHandCursor)

    def _end_live_drag(self) -> str | None:
        name = self._active_drag_name
        self._active_drag_name = None
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._stop_drag_feedback()
        return name

    def _cards(self) -> list[GraphCard]:
        lay = self.layout()
        if lay is None:
            return []
        out: list[GraphCard] = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, GraphCard):
                out.append(w)
        return out

    def card_order(self) -> list[str]:
        return [c.graph_name for c in self._cards()]

    def _drop_slot(self, y: int) -> tuple[int, int]:
        cards = self._cards()
        if not cards:
            return 0, 0
        for idx, card in enumerate(cards):
            if y < card.geometry().center().y():
                if idx == 0:
                    line_y = max(0, card.geometry().top() - 2)
                else:
                    line_y = (cards[idx - 1].geometry().bottom() + card.geometry().top()) // 2
                return idx, line_y
        return len(cards), cards[-1].geometry().bottom() + 4

    def _update_indicator(self, line_y: int) -> None:
        right_margin = self.layout().contentsMargins().right() if self.layout() is not None else 0
        width = max(10, self.width() - right_margin)
        self._indicator.setGeometry(0, max(0, line_y - 1), width, 3)
        self._indicator.raise_()
        self._indicator.show()

    def _stop_drag_feedback(self) -> None:
        self._indicator.hide()
        self._scroll_delta = 0
        if self._scroll_timer.isActive():
            self._scroll_timer.stop()

    def _update_auto_scroll(self, host_pos: QPoint) -> None:
        if self._scroll_area is None:
            return
        vp = self._scroll_area.viewport()
        vp_y = vp.mapFromGlobal(self.mapToGlobal(host_pos)).y()
        if vp_y < self._SCROLL_MARGIN_PX:
            self._scroll_delta = -self._SCROLL_STEP_PX
        elif vp_y > vp.height() - self._SCROLL_MARGIN_PX:
            self._scroll_delta = self._SCROLL_STEP_PX
        else:
            self._scroll_delta = 0
        if self._scroll_delta and not self._scroll_timer.isActive():
            self._scroll_timer.start()
        elif not self._scroll_delta and self._scroll_timer.isActive():
            self._scroll_timer.stop()

    def _auto_scroll_tick(self) -> None:
        if self._scroll_area is None or not self._scroll_delta:
            self._scroll_timer.stop()
            return
        bar = self._scroll_area.verticalScrollBar()
        bar.setValue(bar.value() + self._scroll_delta)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt
        if self._active_drag_name is not None:
            gpos = event.globalPosition().toPoint()
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                name = self._end_live_drag()
                if name is not None:
                    self.live_drag_finished.emit(name, gpos)
                return
            self.live_drag_moved.emit(self._active_drag_name, gpos, self._active_drag_hotspot)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt
        if self._active_drag_name is not None and event.button() == Qt.MouseButton.LeftButton:
            gpos = event.globalPosition().toPoint()
            name = self._end_live_drag()
            if name is not None:
                self.live_drag_finished.emit(name, gpos)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt
        if self._active_drag_name is not None and event.key() == Qt.Key.Key_Escape:
            name = self._end_live_drag()
            if name is not None:
                self.live_drag_cancelled.emit(name)
            event.accept()
            return
        super().keyPressEvent(event)


class GraphsWindow(Window):
    """Главное окно графиков: слева — монолитный главный график, справа — док-лента графиков со скроллом."""

    SETTINGS_GROUP = "MAIN"
    _ORDER_SETTINGS_KEY = "GRAPHS/ORDER"
    _MAIN_SETTINGS_KEY = "GRAPHS/MAIN"
    _SPLIT_SETTINGS_KEY = "GRAPHS/SPLIT"

    STYLE_SHEET_EXTRA = render_qss("""
        QScrollArea#paneScroll, QWidget#paneHost {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
        }
        QFrame#paneFrame {
            background-color: $UI_COLOR_BACKGROUND;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        QFrame#dropIndicator {
            background-color: $UI_COLOR_INVERT;
            border: none;
            border-radius: 1px;
        }
        QLabel#emptyGraphs {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 24px;
        }
        QSplitter { background: $UI_COLOR_BACKGROUND; }
        QSplitter::handle { background: transparent; }
        QSplitter::handle:horizontal { width: 8px; }
    """) + "\n" + scrollbar_qss("QScrollArea#paneScroll ")

    def __init__(self, settings: QSettings | None = None, monitor: ActivityMonitor | None = None, *,
                 instruments_window=None, coordinator: Coordinator | None = None,
                 main_graph: str | None = None, side_graphs: tuple[str, ...] | None = None, parent=None):
        self._settings = settings or QSettings()
        saved_theme = str(self._settings.value(f"{self.SETTINGS_GROUP}/THEME", "light", type=str) or "light")
        apply_theme(saved_theme)
        super().__init__(self._settings, parent)
        self.setWindowTitle("LAB")
        self.monitor: ActivityMonitor | None = monitor
        self.coord: Coordinator = coordinator if coordinator is not None else COORD
        self.instruments_window = instruments_window
        self.quit_on_close = False
        self.before_quit: list = []

        self._floating_windows: dict[str, _FloatingGraphWindow] = {}
        self._detached_slots: dict[str, int] = {}
        self._mac_floating_ok = False
        self.plot_settings_window: PlotSettingsWindow | None = None
        self.all_cards: dict[str, GraphCard] = {}
        self.side_cards: dict[str, GraphCard] = {}
        self.main_card: GraphCard | None = None
        self._card_sources: dict[str, tuple[str, Any, int | None]] = {}
        self._dirty_cards: set[str] = set()
        self._cleaned_up = False

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(UI_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._flush_updates)

        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state_changed)

        # WindowBar: слева SincEmblem, INST, DARK/LIGHT; справа ScenarioChip, minimize, maximize, close
        self.titleBar.title_label.hide()

        self.emblem = SincEmblem(self.titleBar)
        self.titleBar.insert_widget(self.emblem, 0)

        self.instruments_button = self._bar_chip(
            "INST", "Show / bring instruments window to front", checkable=True,
        )
        self.instruments_button.clicked.connect(self._on_instruments_button)
        self.titleBar.insert_widget(self.instruments_button, 1)
        if self.instruments_window is None:
            self.instruments_button.hide()
        else:
            self.instruments_window.visibility_changed.connect(self._on_instruments_visibility_changed)
            self._on_instruments_visibility_changed(self.instruments_window.isVisible())

        self.theme_button = self._bar_chip(
            "DARK" if current_theme() == "light" else "LIGHT",
            "Switch between light and dark theme",
        )
        self.theme_button.clicked.connect(self.toggle_theme)
        self.titleBar.insert_widget(self.theme_button, 2)

        self.scenario_chip = ScenarioChip(self.titleBar)
        self.scenario_chip.setStyleSheet(_SCENARIO_CHIP_QSS)
        self.scenario_chip.clicked.connect(self._open_scenario)
        self.scenario_chip.hide()
        self.titleBar.insert_control_widget(self.scenario_chip)

        self.maximize_button = CustomFullButton()
        self.maximize_button.setToolTip("Maximize / restore (F11)")
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.titleBar.insert_control_widget(self.maximize_button, after_minimize=True)
        self._full_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        self._full_key.activated.connect(self.toggle_maximized)

        # Левая часть: монолитный главный график на всю высоту БЕЗ скроллов
        self.left_frame = QFrame()
        self.left_frame.setObjectName("paneFrame")
        self._left_layout = QVBoxLayout(self.left_frame)
        self._left_layout.setContentsMargins(6, 6, 6, 6)
        self._left_layout.setSpacing(0)

        # Правая часть: вертикальный скролл с перетаскиваемыми/открепляемыми карточками графиков
        self.right_frame, self.right_scroll, self.right_host, self.right_layout = self._make_scroll_pane(
            host_cls=_GraphListHost
        )
        self.right_host.bind_scroll(self.right_scroll)
        self.right_host.order_changed.connect(self._save_order)
        self.right_host.live_drag_moved.connect(self._on_live_drag_moved)
        self.right_host.live_drag_finished.connect(self._on_live_drag_finished)
        self.right_host.live_drag_cancelled.connect(self.dock_card)
        self.right_layout.addStretch(1)

        self.splitter = GraphSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.left_frame)
        self.splitter.addWidget(self.right_frame)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self._restore_split()
        self.splitter.splitterMoved.connect(lambda _p, _i: self._save_split())
        self.splitter.equalized.connect(self._save_split)
        self.splitter.setStyleSheet(self.STYLE_SHEET_EXTRA)

        self.empty_label = QLabel("NO INSTRUMENTS", self)
        self.empty_label.setObjectName("emptyGraphs")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        root.setSpacing(0)
        root.addWidget(self.splitter, 1)
        root.addWidget(self.empty_label, 1)

        # Наполнение карточек: либо из ActivityMonitor, либо статическим списком (для автономных тестов)
        if self.monitor is not None:
            self.monitor.roster_changed.connect(self._rebuild_from_monitor)
            self.monitor.changed.connect(self._on_monitor_changed)
            self._rebuild_from_monitor()
        else:
            mg = DEFAULT_MAIN_GRAPH if main_graph is None else main_graph
            sg = DEFAULT_SIDE_GRAPHS if side_graphs is None else side_graphs
            specs = [
                (n, ("TIME", "FREQ") if n.startswith("PSU") else (("FREQ", "TIME") if n == "PM" else ()))
                for n in [mg, *[s for s in sg if s != mg]]
            ]
            self._sync_cards(specs, preferred_main=mg)

        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            sig.connect(self._on_runs)
        self._on_runs()

        self.titleBar.raise_()
        self.setMinimumWidth(760)
        self.setMinimumHeight(460)
        self.recall_size(QSize(1240, 760))

    @staticmethod
    def _bar_chip(text: str, tip: str, *, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("barChip")
        b.setFixedHeight(UI_TITLE_BAR_ITEM_SIZE_PX)
        b.setStyleSheet(_BAR_CHIP_QSS)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        return b

    @staticmethod
    def _make_scroll_pane(host_cls=QWidget) -> tuple[QFrame, QScrollArea, QWidget, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("paneFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(6, 6, 6, 6)
        fl.setSpacing(0)

        host = host_cls()
        host.setObjectName("paneHost")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(8)

        scroll = QScrollArea()
        scroll.setObjectName("paneScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(host)

        fl.addWidget(scroll, 1)
        return frame, scroll, host, hl

    # ------------------------------------------------------------ сохранение настроек
    def _plot_settings_group(self, name: str, mode: str = "", default_mode: str = "") -> str:
        if mode and default_mode and mode != default_mode:
            return f"GRAPHS/PLOT/{name}/{mode}"
        return f"GRAPHS/PLOT/{name}"

    def _load_mode(self, name: str, modes: tuple[str, ...]) -> str:
        if not modes:
            return ""
        saved = str(self._settings.value(f"GRAPHS/MODE/{name}", "", type=str) or "").strip().upper()
        return saved if saved in modes else modes[0]

    def _load_order(self, names: tuple[str, ...]) -> list[str]:
        raw = self._settings.value(self._ORDER_SETTINGS_KEY)
        saved: list[str] = []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    saved = [str(x) for x in parsed]
            except (ValueError, TypeError):
                pass
        elif isinstance(raw, (list, tuple)):
            saved = [str(x) for x in raw]
        valid = set(names)
        ordered = [name for name in saved if name in valid]
        seen = set(ordered)
        for name in names:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
        return ordered

    def _save_order(self, order: list[str]) -> None:
        merged = list(order)
        seen = set(merged)
        for name in self.side_cards:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        self._settings.setValue(self._ORDER_SETTINGS_KEY, json.dumps(merged))
        self._settings.sync()

    def _save_split(self) -> None:
        if not hasattr(self, "splitter") or sip.isdeleted(self.splitter):
            return
        if not self.right_frame.isVisible():
            return
        w = self.splitter.current_weights()
        if w is not None and len(w) == 2:
            self._settings.setValue(self._SPLIT_SETTINGS_KEY, json.dumps([round(x, 5) for x in w]))

    def _restore_split(self) -> None:
        raw = self._settings.value(self._SPLIT_SETTINGS_KEY, "", type=str)
        try:
            vals = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            vals = []
        if isinstance(vals, list) and len(vals) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in vals):
            weights = [float(vals[0]), float(vals[1])]
        else:
            weights = [0.55, 0.45]
        self.splitter.apply_weights(weights)

    # ------------------------------------------------------------ состав карточек и живые данные
    def _make_card(self, name: str, *, mono: bool, parent_w: QWidget, modes: tuple[str, ...] = ()) -> GraphCard:
        initial_mode = self._load_mode(name, modes)
        default_mode = modes[0] if modes else ""
        card = GraphCard(name, monolithic=mono, min_body_height=240, modes=modes, initial_mode=initial_mode,
                         parent=parent_w)
        card.plot.attach_settings(self._settings, self._plot_settings_group(name, initial_mode, default_mode))
        card.drag_started.connect(self._on_card_drag_started)
        card.settings_requested.connect(self.open_plot_settings)
        card.promote_requested.connect(self.set_main_graph)
        card.mode_changed.connect(self._on_card_mode_changed)
        self.all_cards[name] = card
        return card

    def _remove_card(self, name: str) -> None:
        if name in self._floating_windows:
            self.dock_card(name)
        card = self.all_cards.pop(name, None)
        self.side_cards.pop(name, None)
        if card is None or sip.isdeleted(card):
            return
        if self.plot_settings_window is not None and not sip.isdeleted(self.plot_settings_window):
            if self.plot_settings_window.current_name == name:
                self.plot_settings_window.close()
        if not sip.isdeleted(card.plot):
            card.plot.save_state()
            card.plot.detach_settings()
        if self.main_card is card:
            self._left_layout.removeWidget(card)
            self.main_card = None
        else:
            self.right_layout.removeWidget(card)
        card.hide()
        card.setParent(None)
        card.deleteLater()

    def _sync_cards(self, specs: list[tuple[str, tuple[str, ...]]], *, preferred_main: str | None = None) -> None:
        """Привести набор ``GraphCard`` в соответствие со списком ``[(name, modes), ...]``."""
        desired_names = [n for n, _m in specs]
        desired_set = set(desired_names)
        modes_by_name = dict(specs)

        # Убрать исчезнувшие карточки
        for name in [n for n in list(self.all_cards) if n not in desired_set]:
            self._remove_card(name)

        if not desired_names:
            self.splitter.hide()
            self.empty_label.show()
            return

        self.empty_label.hide()
        self.splitter.show()

        saved_main = str(self._settings.value(self._MAIN_SETTINGS_KEY, "", type=str) or "").strip()
        if self.main_card is not None and self.main_card.graph_name in desired_set:
            active_main = self.main_card.graph_name
        elif saved_main in desired_set:
            active_main = saved_main
        elif preferred_main in desired_set:
            active_main = preferred_main
        elif DEFAULT_MAIN_GRAPH in desired_set:
            active_main = DEFAULT_MAIN_GRAPH
        else:
            active_main = desired_names[0]

        # Главная карточка
        if active_main not in self.all_cards:
            card = self._make_card(active_main, mono=True, parent_w=self.left_frame,
                                   modes=modes_by_name.get(active_main, ()))
            self._left_layout.addWidget(card, 1)
            self.main_card = card
        elif self.main_card is None or self.main_card.graph_name != active_main:
            # Перевести существующую боковую карточку в главную
            card = self.all_cards[active_main]
            if active_main in self._floating_windows:
                self.dock_card(active_main)
            self.right_layout.removeWidget(card)
            self.side_cards.pop(active_main, None)
            card.set_monolithic(True)
            card.setParent(self.left_frame)
            self._left_layout.addWidget(card, 1)
            card.show()
            self.main_card = card

        # Остальные карточки в правой ленте
        remaining = tuple(n for n in desired_names if n != active_main)
        ordered_side = self._load_order(remaining)
        new_side = [n for n in ordered_side if n not in self.all_cards]
        if new_side:
            # Убираем завершающий stretch, добавляем новые в нужном порядке, возвращаем stretch
            stretch_idx = self.right_layout.count() - 1
            if stretch_idx >= 0 and self.right_layout.itemAt(stretch_idx).widget() is None:
                self.right_layout.takeAt(stretch_idx)
            for name in new_side:
                card = self._make_card(name, mono=False, parent_w=self.right_host, modes=modes_by_name.get(name, ()))
                self.side_cards[name] = card
                self.right_layout.addWidget(card)
            # Выровнять порядок закреплённых карточек по сохранённому
            docked_order = [n for n in ordered_side if n in self.side_cards and n not in self._floating_windows]
            for idx, name in enumerate(docked_order):
                card = self.side_cards[name]
                self.right_layout.removeWidget(card)
                self.right_layout.insertWidget(idx, card)
            self.right_layout.addStretch(1)

        has_right = bool(self.side_cards)
        self.right_frame.setVisible(has_right)
        if has_right:
            self._restore_split()

    def _rebuild_from_monitor(self) -> None:
        if self.monitor is None or sip.isdeleted(self):
            return
        sources: dict[str, tuple[str, Any, int | None]] = {}
        specs: list[tuple[str, tuple[str, ...]]] = []
        for p in self.monitor.receivers():
            prof = profile_name(p)
            kind = {"network_analyzer": "vna", "spectrum_analyzer": "sa", "power_meter": "pm"}.get(prof)
            if kind is None:
                continue
            name = self.monitor.name(p)
            sources[name] = (kind, p, None)
            specs.append((name, ("FREQ", "TIME") if kind == "pm" else ()))
        for p, ch, label in self.monitor.psu_channels():
            sources[label] = ("psu", p, ch)
            specs.append((label, ("TIME", "FREQ")))

        self._card_sources = sources
        self._sync_cards(specs)
        for name in list(self._card_sources):
            self._refresh_card(name)

    def _on_monitor_changed(self, proxy) -> None:
        if self.monitor is None or sip.isdeleted(self):
            return
        matched = False
        for name, (_kind, p, _ch) in self._card_sources.items():
            if p is proxy:
                self._dirty_cards.add(name)
                matched = True
        if matched and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_updates(self) -> None:
        if sip.isdeleted(self):
            return
        dirty = list(self._dirty_cards)
        self._dirty_cards.clear()
        for name in dirty:
            self._refresh_card(name)

    def _on_card_mode_changed(self, name: str, mode: str) -> None:
        self._settings.setValue(f"GRAPHS/MODE/{name}", mode)
        self._settings.sync()
        card = self.all_cards.get(name)
        if card is not None and not sip.isdeleted(card):
            default_mode = card._modes[0] if card._modes else ""
            card.plot.save_state()
            card.plot.detach_settings()
            card.plot.clear()
            card.plot.reset_view()
            card.plot.attach_settings(self._settings, self._plot_settings_group(name, mode, default_mode))
            self._refresh_card(name)
            sw = self.plot_settings_window
            if sw is not None and not sip.isdeleted(sw) and sw.current_name == name:
                sw.sync_from_plot()

    def _refresh_card(self, name: str) -> None:
        if self.monitor is None or sip.isdeleted(self):
            return
        card = self.all_cards.get(name)
        src = self._card_sources.get(name)
        if card is None or src is None or sip.isdeleted(card) or sip.isdeleted(card.plot):
            return
        kind, proxy, ch = src
        if sip.isdeleted(proxy):
            return

        if kind == "vna":
            traces = self.monitor.vna_traces(proxy)
            if traces:
                series = [
                    PlotSeries(label, tuple(tr.x), tuple(tr.y), y_unit=tr.y_unit or "dB")
                    for label, tr in traces
                ]
                x_u = traces[0][1].x_unit or "Hz"
                y_u = traces[0][1].y_unit or "dB"
                card.plot.set_series(series, x_unit=x_u, y_unit=y_u)
            else:
                card.plot.clear()

        elif kind == "sa":
            tr = proxy.value("trace")
            if isinstance(tr, Trace) and len(tr) > 1:
                card.plot.set_series(
                    [PlotSeries(str(tr.name or "TRACE 1"), tuple(tr.x), tuple(tr.y))],
                    x_unit=tr.x_unit or "Hz",
                    y_unit=tr.y_unit or "dBm",
                )
            else:
                card.plot.clear()

        elif kind == "pm":
            trace_on = proxy.value("trace.state") is True
            was_trace_on = bool(getattr(card, "_last_pm_trace_on", False))
            card._last_pm_trace_on = trace_on
            if trace_on and not was_trace_on and card.current_mode != "TIME":
                card.set_mode("TIME", emit=True)
                return

            if card.current_mode == "TIME":
                if trace_on:
                    tr = proxy.value("trace")
                    if isinstance(tr, Trace) and len(tr) > 0:
                        card.plot.set_series(
                            [PlotSeries("TRACE", tuple(tr.x), tuple(tr.y))],
                            x_unit=tr.x_unit or "s",
                            y_unit=tr.y_unit or "dBm",
                        )
                    else:
                        card.plot.set_axes(x_unit="s", y_unit="dBm")
                        card.plot.clear()
                else:
                    hist = list(self.monitor.history.get(proxy, ()))
                    if hist:
                        t0 = hist[0][0]
                        xs = tuple(t - t0 for t, _ in hist)
                        ys = tuple(v for _, v in hist)
                        card.plot.set_series([PlotSeries("PM", xs, ys, show_points=True)], x_unit="s", y_unit="dBm")
                    else:
                        card.plot.set_axes(x_unit="s", y_unit="dBm")
                        card.plot.clear()
            else:
                cols, rows = self.monitor.pm_matrix(proxy)
                if rows:
                    series: list[PlotSeries] = []
                    for col in cols:
                        xs = tuple(f for f, vals in rows if col in vals)
                        ys = tuple(vals[col] for f, vals in rows if col in vals)
                        if xs:
                            label = f"{col:g} dBm" if col is not None else "MEAS"
                            series.append(PlotSeries(label, xs, ys, show_points=True))
                    if series:
                        card.plot.set_series(series, x_unit="Hz", y_unit="dBm")
                    else:
                        card.plot.set_axes(x_unit="Hz", y_unit="dBm")
                        card.plot.clear()
                else:
                    card.plot.set_axes(x_unit="Hz", y_unit="dBm")
                    card.plot.clear()

        elif kind == "psu" and ch is not None:
            if card.current_mode == "FREQ":
                cols, rows = self.monitor.psu_matrix(proxy, ch)
                if rows:
                    series = []
                    for col in cols:
                        xs = tuple(f for f, vals in rows if col in vals)
                        ys = tuple(vals[col] for f, vals in rows if col in vals)
                        if xs:
                            label = f"{col:g} dBm" if col is not None else "MEAS"
                            series.append(PlotSeries(label, xs, ys, show_points=True, y_unit="A"))
                    if series:
                        card.plot.set_series(series, x_unit="Hz", y_unit="A")
                    else:
                        card.plot.set_axes(x_unit="Hz", y_unit="A")
                        card.plot.clear()
                else:
                    card.plot.set_axes(x_unit="Hz", y_unit="A")
                    card.plot.clear()
            else:
                samples = self.monitor.psu_time_series(proxy, ch)
                if samples:
                    xs_v, ys_v = [], []
                    xs_i, ys_i = [], []
                    xs_p, ys_p = [], []
                    for t, v, i, p in samples:
                        if v is not None:
                            xs_v.append(t)
                            ys_v.append(v)
                        if i is not None:
                            xs_i.append(t)
                            ys_i.append(i)
                        if p is not None:
                            xs_p.append(t)
                            ys_p.append(p)
                    series = []
                    if xs_v:
                        series.append(PlotSeries(f"CH{ch} V", tuple(xs_v), tuple(ys_v), y_unit="V"))
                    if xs_i:
                        series.append(PlotSeries(f"CH{ch} I", tuple(xs_i), tuple(ys_i), y_unit="A"))
                    if xs_p:
                        series.append(PlotSeries(f"CH{ch} P", tuple(xs_p), tuple(ys_p), y_unit="W"))
                    if series:
                        card.plot.set_series(series, x_unit="s", y_unit=series[0].y_unit or "V")
                    else:
                        card.plot.set_axes(x_unit="s", y_unit="V")
                        card.plot.clear()
                else:
                    card.plot.set_axes(x_unit="s", y_unit="V")
                    card.plot.clear()

    # ------------------------------------------------------------ сценарии, тема и окно инструментов
    def _on_runs(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        self.scenario_chip.show_runs(self.coord.runs())

    def _open_scenario(self) -> None:
        runs = self.coord.runs()
        if runs and runs[0].devices and self.instruments_window is not None and not sip.isdeleted(self.instruments_window):
            proxy = list(runs[0].devices.values())[-1].instrument
            self.instruments_window.open_instrument_by_proxy(proxy)

    def set_theme(self, name: str, *, save: bool = True) -> None:
        theme = apply_theme(name)
        if save:
            self._settings.setValue(f"{self.SETTINGS_GROUP}/THEME", theme)
        self.theme_button.setText("DARK" if theme == "light" else "LIGHT")

    def toggle_theme(self) -> None:
        self.set_theme("dark" if current_theme() == "light" else "light")

    def set_instruments_visible(self, on: bool) -> None:
        iw = self.instruments_window
        if iw is None or sip.isdeleted(iw):
            return
        on = bool(on)
        if on:
            if iw.isMinimized():
                iw.showNormal()
            iw.show()
            iw.raise_()
            iw.activateWindow()
        else:
            iw.save_size()
            iw.hide()
        self._on_instruments_visibility_changed(on)

    def _on_instruments_button(self) -> None:
        iw = self.instruments_window
        if iw is None or sip.isdeleted(iw):
            return
        if not iw.isVisible() or iw.isMinimized():
            self.set_instruments_visible(True)
            return
        # Если окна пересекаются (на экране ноутбука GraphsWindow закрыло собой InstrumentsWindow),
        # клик по INST выводит InstrumentsWindow на передний план вместо скрытия.
        if self.frameGeometry().intersects(iw.frameGeometry()):
            iw.raise_()
            iw.activateWindow()
            self._on_instruments_visibility_changed(True)
            return
        self.set_instruments_visible(False)

    def _on_instruments_visibility_changed(self, visible: bool) -> None:
        if sip.isdeleted(self):
            return
        self.instruments_button.blockSignals(True)
        self.instruments_button.setChecked(bool(visible))
        self.instruments_button.blockSignals(False)

    # ------------------------------------------------------------ управление карточками
    def card(self, name: str) -> GraphCard | None:
        return self.all_cards.get(name)

    def graph_order(self) -> list[str]:
        return self.right_host.card_order()

    def is_detached(self, name: str) -> bool:
        return name in self._floating_windows

    def set_main_graph(self, name: str) -> None:
        """Сделать график ``name`` главным (слева), а прежний главный вернуть на его место в правой ленте."""
        if name not in self.all_cards or self.main_card is None or name == self.main_card.graph_name:
            return
        if name in self._floating_windows:
            self.dock_card(name)
        new_main = self.all_cards[name]
        old_main = self.main_card

        order = self.right_host.card_order()
        slot = order.index(name) if name in order else 0

        self.right_layout.removeWidget(new_main)
        self._left_layout.removeWidget(old_main)

        self.side_cards.pop(name, None)
        self.side_cards[old_main.graph_name] = old_main

        old_main.set_monolithic(False)
        old_main.setParent(self.right_host)
        self.right_layout.insertWidget(slot, old_main)
        old_main.show()

        new_main.set_monolithic(True)
        new_main.setParent(self.left_frame)
        self._left_layout.addWidget(new_main, 1)
        new_main.show()
        self.main_card = new_main

        self.right_layout.invalidate()
        self.right_layout.activate()
        self._left_layout.invalidate()
        self._left_layout.activate()

        self._settings.setValue(self._MAIN_SETTINGS_KEY, name)
        self._save_order(self.right_host.card_order())

    def _on_card_drag_started(self, name: str, global_pos: QPoint, hotspot: QPoint) -> None:
        self.detach_card(name, global_pos - hotspot, activate=False)
        self.right_host.begin_live_drag(name, hotspot)
        self._preview_floating_dock(name, global_pos)

    def _on_live_drag_moved(self, name: str, global_pos: QPoint, hotspot: QPoint) -> None:
        fw = self._floating_windows.get(name)
        if fw is not None and not sip.isdeleted(fw):
            fw.move_clamped(global_pos - hotspot)
            fw.raise_()
        self._preview_floating_dock(name, global_pos)

    def _on_live_drag_finished(self, name: str, global_pos: QPoint) -> None:
        self._finish_floating_dock(name, global_pos)

    def detach_card(self, name: str, global_pos: QPoint | None = None, *, activate: bool = True) -> None:
        """Открепить карточку графика ``name`` в отдельное окно ``Window``."""
        if name not in self.side_cards or name in self._floating_windows:
            return
        card = self.side_cards[name]
        cur_order = self.right_host.card_order()
        self._detached_slots[name] = cur_order.index(name) if name in cur_order else len(cur_order)
        width_px = card.width() or self.right_host.width() or 460
        height_px = card.height() or (WINDOW_BAR_HEIGHT + 240)
        self.right_layout.removeWidget(card)
        self.right_layout.invalidate()
        self.right_layout.activate()

        fw = _FloatingGraphWindow(card, self._settings, width_px, height_px)
        fw.dock_back_requested.connect(self.dock_card)
        fw.drag_moved.connect(self._preview_floating_dock)
        fw.drag_dropped.connect(self._finish_floating_dock)
        fw.activated.connect(self._touch_floating_order)
        self._floating_windows[name] = fw
        if global_pos is None:
            global_pos = self.frameGeometry().topRight() + QPoint(12, 0)
        fw.move_clamped(global_pos)
        fw.show()
        if fw.windowHandle() is not None and self.windowHandle() is not None:
            fw.windowHandle().setTransientParent(self.windowHandle())
        app = QGuiApplication.instance()
        is_active = app is None or app.applicationState() == Qt.ApplicationState.ApplicationActive
        if set_mac_floating_level(fw, is_active):
            self._mac_floating_ok = True
        fw.raise_()
        if activate:
            fw.activateWindow()

    def open_plot_settings(self, name: str) -> None:
        """Открыть (или переключить) отдельное плавающее окно настроек графика ``name``."""
        card = self.all_cards.get(name)
        if card is None:
            return
        sw = self.plot_settings_window
        if sw is not None and not sip.isdeleted(sw) and sw.isVisible() and sw.current_name == name:
            sw.close()
            return
        first_open = sw is None or sip.isdeleted(sw)
        if first_open:
            sw = PlotSettingsWindow(self._settings)
            self.plot_settings_window = sw
        sw.bind_plot(name, card.plot)
        if first_open and not sw._pos_restored:
            fg = self.frameGeometry()
            pos = QPoint(fg.right() - sw.width() - 16, fg.top() + WINDOW_BAR_HEIGHT + 16)
            sw.move(sw._clamp_pos(pos))
        sw.show()
        if sw.windowHandle() is not None and self.windowHandle() is not None:
            sw.windowHandle().setTransientParent(self.windowHandle())
        app = QGuiApplication.instance()
        is_active = app is None or app.applicationState() == Qt.ApplicationState.ApplicationActive
        if set_mac_floating_level(sw, is_active):
            self._mac_floating_ok = True
        sw.raise_()
        sw.activateWindow()

    def _on_app_state_changed(self, state: Qt.ApplicationState) -> None:
        if sip.isdeleted(self):
            return
        active = state == Qt.ApplicationState.ApplicationActive
        for fw in list(self._floating_windows.values()):
            if not sip.isdeleted(fw):
                set_mac_floating_level(fw, active)
        sw = self.plot_settings_window
        if sw is not None and not sip.isdeleted(sw):
            set_mac_floating_level(sw, active)

    def _touch_floating_order(self, name: str) -> None:
        fw = self._floating_windows.pop(name, None)
        if fw is not None:
            self._floating_windows[name] = fw

    def _raise_floating_windows(self) -> None:
        if sip.isdeleted(self) or not self.isVisible() or self.isMinimized():
            return
        for fw in list(self._floating_windows.values()):
            if not sip.isdeleted(fw) and fw.isVisible() and not fw.isMinimized():
                fw.raise_()
        sw = self.plot_settings_window
        if sw is not None and not sip.isdeleted(sw) and sw.isVisible() and not sw.isMinimized():
            sw.raise_()

    def _right_pane_global_rect(self) -> QRect:
        return QRect(self.right_frame.mapToGlobal(QPoint(0, 0)), self.right_frame.size())

    def _preview_floating_dock(self, _name: str, global_pos: QPoint) -> None:
        if self.isVisible() and not self.isMinimized() and self._right_pane_global_rect().contains(global_pos):
            local_pos = self.right_host.mapFromGlobal(global_pos)
            _, line_y = self.right_host._drop_slot(local_pos.y())
            self.right_host._update_indicator(line_y)
            self.right_host._update_auto_scroll(local_pos)
        else:
            self.right_host._stop_drag_feedback()

    def _finish_floating_dock(self, name: str, global_pos: QPoint) -> None:
        self.right_host._stop_drag_feedback()
        if self.isVisible() and not self.isMinimized() and self._right_pane_global_rect().contains(global_pos):
            local_y = self.right_host.mapFromGlobal(global_pos).y()
            slot, _ = self.right_host._drop_slot(local_y)
            self.dock_card(name, slot)
        else:
            fw = self._floating_windows.get(name)
            if fw is not None and not sip.isdeleted(fw):
                fw.raise_()
                fw.activateWindow()

    def dock_card(self, name: str, slot: int | None = None) -> None:
        """Вернуть откреплённую карточку графика ``name`` обратно в правую ленту."""
        if name not in self.side_cards or name not in self._floating_windows:
            return
        card = self.side_cards[name]
        fw = self._floating_windows.pop(name)
        cur_order = self.right_host.card_order()
        fallback_slot = self._detached_slots.pop(name, len(cur_order))
        insert_idx = fallback_slot if slot is None else slot
        insert_idx = max(0, min(insert_idx, len(cur_order)))

        fw._closing_for_dock = True
        fw.release_card()
        card.set_floating(False)
        card.setParent(self.right_host)
        self.right_layout.insertWidget(insert_idx, card)
        card.show()
        self.right_layout.invalidate()
        self.right_layout.activate()

        if not sip.isdeleted(fw):
            fw.hide()
            fw.deleteLater()
        if slot is not None:
            self._save_order(self.right_host.card_order())

    def _dock_all_floating(self) -> None:
        for name in list(self._floating_windows.keys()):
            self.dock_card(name)

    def event(self, e):  # noqa: N802 - Qt
        res = super().event(e)
        if e.type() == QEvent.Type.WindowActivate and not getattr(self, "_mac_floating_ok", False):
            sw = getattr(self, "plot_settings_window", None)
            has_float = bool(getattr(self, "_floating_windows", None)) or (
                sw is not None and not sip.isdeleted(sw) and sw.isVisible()
            )
            if has_float:
                QTimer.singleShot(0, self._raise_floating_windows)
        return res

    def show_window(self) -> None:
        if self._settings.value(f"{self.SETTINGS_GROUP}/MAXIMIZED", False, type=bool):
            self.showMaximized()
            self.maximize_button.set_full(True)
        else:
            self.show()

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.maximize_button.set_full(False)
            self._settings.setValue(f"{self.SETTINGS_GROUP}/MAXIMIZED", False)
        else:
            self.showMaximized()
            self.maximize_button.set_full(True)
            self._settings.setValue(f"{self.SETTINGS_GROUP}/MAXIMIZED", True)

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._refresh_timer.stop()
        self._dirty_cards.clear()
        self._save_split()
        if hasattr(self, "emblem") and not sip.isdeleted(self.emblem):
            self.emblem.stop()
        app = QGuiApplication.instance()
        if app is not None:
            try:
                app.applicationStateChanged.disconnect(self._on_app_state_changed)
            except (TypeError, RuntimeError):
                pass
        if self.monitor is not None and not sip.isdeleted(self.monitor):
            try:
                self.monitor.roster_changed.disconnect(self._rebuild_from_monitor)
            except (TypeError, RuntimeError):
                pass
            try:
                self.monitor.changed.disconnect(self._on_monitor_changed)
            except (TypeError, RuntimeError):
                pass
        for sig in (self.coord.run_started, self.coord.run_progress, self.coord.run_ended):
            try:
                sig.disconnect(self._on_runs)
            except (TypeError, RuntimeError):
                pass
        sw = self.plot_settings_window
        self.plot_settings_window = None
        if sw is not None and not sip.isdeleted(sw):
            sw._closing_for_shutdown = True
            sw.close()
            sw.deleteLater()
        self._dock_all_floating()
        for card in self.all_cards.values():
            if not sip.isdeleted(card) and not sip.isdeleted(card.plot):
                card.plot.save_state()
                card.plot.detach_settings()
        iw = self.instruments_window
        if iw is not None and not sip.isdeleted(iw):
            try:
                iw.visibility_changed.disconnect(self._on_instruments_visibility_changed)
            except (TypeError, RuntimeError):
                pass
            if self.quit_on_close:
                iw.hide_on_close = False
                iw.save_size()
                iw.cleanup()
                iw.hide()
                iw.close()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if self.quit_on_close:
            for fn in self.before_quit:
                fn()
        self.cleanup()
        super().closeEvent(event)
        self._settings.sync()
        if self.quit_on_close:
            if self.monitor is not None and not sip.isdeleted(self.monitor):
                self.monitor.stop()
            QApplication.quit()
