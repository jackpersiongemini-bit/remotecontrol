"""Отдельное независимое окно приборов (``InstrumentDockWindow``) — равноправный аналог окна графиков.

В заголовке окна — плитки всех приборов стенда (VNA, SA, PM, GEN, PULSE, PSU: индикатор открытого окна,
красная подсветка ошибки, клик — открыть/показать прибор) и кнопка полноэкранного режима (F11).

Внутри — две вкладки в стиле главного окна (``Panel(PTabBar)``):
* Вкладка 0 (``FAV``) — только избранное прикреплённых приборов (без настроек), автоматически
  сгруппированное в несколько колонок по ширине окна.
* Вкладка 1 (``SET``) — все настройки приборов (вкладки параметров, подключение ``SET`` и выбор
  элементов избранного ``★``) в той же многоколоночной группировке.
"""
from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..core.params import ConnectionState
from .params import IndicatorButton, PTabBar, Panel, _repolish, paint_indicator
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR, UI_COLOR_INDICATOR, UI_COLOR_INVERT,
                    UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE,
                    UI_COLOR_TEXT_MUTED, UI_FONT_SIZE_PT, UI_MONOSPACE_FONT_FAMILY, UI_TITLE_BAR_ITEM_SIZE_PX,
                    WINDOW_BAR_HEIGHT, checkbox_qss, render_qss, scrollbar_qss)
from .window import CustomFullButton, Window, WindowBar, short_address

__all__ = ["InstrumentTile", "TILE_MIN_HEIGHT", "InstrumentDock", "InstrumentDockWindow",
           "active_dock", "register_dock", "unregister_dock"]

TILE_MIN_HEIGHT = UI_TITLE_BAR_ITEM_SIZE_PX

_DOCKS: list["InstrumentDock"] = []
_STANDALONE_WINDOW: "InstrumentDockWindow | None" = None

_TILE_QSS = render_qss("""
    QWidget#tilesHost { background: transparent; border: none; }
    InstrumentTile, QPushButton#chip[tile="true"] {
        background-color: $UI_COLOR_BACKGROUND;
        color: $UI_COLOR_TEXT_MUTED;
        border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        border-radius: ${UI_BORDER_RADIUS_PX}px;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        padding: 0 10px;
    }
    InstrumentTile:hover, QPushButton#chip[tile="true"]:hover {
        background-color: $UI_COLOR_INVERT_HOVER;
        color: $UI_COLOR_TEXT_INVERSE;
        border-color: $UI_COLOR_INVERT_HOVER;
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
    }
    InstrumentTile[wstate="error"], QPushButton#chip[tile="true"][wstate="error"] {
        color: $UI_COLOR_ERROR;
    }
""")


def register_dock(dock: "InstrumentDock") -> None:
    _prune_docks()
    if dock in _DOCKS:
        _DOCKS.remove(dock)
    _DOCKS.append(dock)


def unregister_dock(dock: "InstrumentDock") -> None:
    _DOCKS[:] = [d for d in _DOCKS if d is not dock and not sip.isdeleted(d)]


def _prune_docks() -> None:
    _DOCKS[:] = [d for d in _DOCKS if not sip.isdeleted(d)]


def active_dock(settings: QSettings | None = None, create: bool = False) -> "InstrumentDock | None":
    """Вернуть основной док процесса. При ``create=True`` создаст отдельное окно
    ``InstrumentDockWindow``, если ни одного дока ещё нет."""
    global _STANDALONE_WINDOW
    _prune_docks()
    if _DOCKS:
        for d in reversed(_DOCKS):
            if d.window().isVisible():
                return d
        return _DOCKS[-1]
    if not create:
        return None
    if _STANDALONE_WINDOW is None or sip.isdeleted(_STANDALONE_WINDOW):
        s = settings or QSettings()
        _STANDALONE_WINDOW = InstrumentDockWindow(s)
    _STANDALONE_WINDOW.show()
    _STANDALONE_WINDOW.raise_()
    return _STANDALONE_WINDOW.dock


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


class _DockCard(QFrame):
    """Карточка одного прикреплённого прибора внутри многоколоночной сетки дока."""

    def __init__(self, win: QWidget, dock: "InstrumentDock", parent=None):
        super().__init__(parent)
        self.setObjectName("dockCard")
        self.instrument_window = win
        self._dock = dock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.card_layout = lay
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.sync_width()
        win.installEventFilter(self)

    def sync_width(self) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.instrument_window):
            return
        w = max(self.instrument_window.minimumWidth(), 360)
        self.setMinimumWidth(w + 2)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt
        if obj is self.instrument_window and not sip.isdeleted(self):
            et = event.type()
            if et == QEvent.Type.HideToParent:
                self.setVisible(False)
                self._dock._relayout_cards(force=True)
                self._dock._update_state()
            elif et == QEvent.Type.ShowToParent:
                self.setVisible(True)
                self._dock._relayout_cards(force=True)
                self._dock._update_state()
        return super().eventFilter(obj, event)


class InstrumentDock(QFrame):
    """Приборная панель с двумя вкладками (FAV / SET) и адаптивной многоколоночной группировкой."""

    TABS = ("FAV", "SET")

    #: состав, видимость или вкладка прикреплённых приборов изменились
    changed = pyqtSignal()
    #: пользователь нажал кнопку скрытия дока
    close_requested = pyqtSignal()

    STYLE_SHEET = render_qss("""
        InstrumentDock {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
        }
        InstrumentDock QWidget#dockHost, InstrumentDock QWidget#dockCol {
            background: transparent;
            border: none;
        }
        InstrumentDock QScrollArea#dockScroll {
            background: transparent;
            border: none;
        }
        InstrumentDock QLabel#dockTitle {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 0 4px;
        }
        InstrumentDock QLabel#dockEmpty {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 24px 12px;
        }
        InstrumentDock QFrame#dockCard {
            background-color: $UI_COLOR_BACKGROUND;
            border: 1px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
    """) + scrollbar_qss("InstrumentDock ") + "\n" + checkbox_qss("InstrumentDock ")

    def __init__(self, parent=None, *, settings: QSettings | None = None, closable: bool = False,
                 is_standalone: bool = True):
        super().__init__(parent)
        self.settings = settings
        self.is_standalone = is_standalone
        self._cards: dict[object, _DockCard] = {}
        self._column_widgets: list[QWidget] = []
        self._last_ncols: int = 0
        self._relayouting: bool = False
        self.setStyleSheet(self.STYLE_SHEET)
        self.setMinimumWidth(360)

        # Верхняя панель вкладок 1-в-1 как в MainWindow (Panel + PTabBar + стандартный шрифт)
        self.tabs = PTabBar()
        self.tabs.addTab("FAV")
        self.tabs.setTabToolTip(0, "Favorites of all docked instruments (favorites only)")
        self.tabs.addTab("SET")
        self.tabs.setTabToolTip(1, "All instrument settings and favorites configuration (★)")
        saved_tab = settings.value("DOCK/TAB", 0, type=int) if settings is not None else 0
        self.tabs.setCurrentIndex(1 if saved_tab == 1 else 0)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.title_label = QLabel("")
        self.title_label.setObjectName("dockTitle")
        self.dock_all_button = self._chip_btn("DOCK ALL", "Attach all instrument windows into this panel", self.dock_all)
        self.undock_all_button = self._chip_btn("UNDOCK ALL", "Detach all instruments into separate floating windows",
                                                self.undock_all)
        tabs_bar = QWidget()
        tabs_lay = QHBoxLayout(tabs_bar)
        tabs_lay.setContentsMargins(0, 0, 0, 0)
        tabs_lay.setSpacing(6)
        tabs_lay.addWidget(self.tabs, 1)
        tabs_lay.addWidget(self.title_label)
        tabs_lay.addWidget(self.dock_all_button)
        tabs_lay.addWidget(self.undock_all_button)
        self.close_button: QPushButton | None = None
        if closable:
            self.close_button = self._chip_btn("×", "Hide instrument dock panel", self.close_requested.emit)
            self.close_button.setFixedWidth(28)
            tabs_lay.addWidget(self.close_button)
        self.tabs_panel = Panel(tabs_bar)
        self.tabs_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # Контейнер многоколоночной группировки карточек
        self._host = QWidget()
        self._host.setObjectName("dockHost")
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.setSpacing(0)

        self._columns_host = QWidget(self._host)
        self._columns_host.setObjectName("dockHost")
        self._columns_layout = QHBoxLayout(self._columns_host)
        self._columns_layout.setContentsMargins(0, 0, 0, 0)
        self._columns_layout.setSpacing(8)
        self._cards_layout = self._columns_layout

        self._empty_label = QLabel("NO DOCKED INSTRUMENTS\nClick the dock button on any instrument window or DOCK ALL")
        self._empty_label.setObjectName("dockEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)

        self._host_layout.addWidget(self._columns_host, 1)
        self._host_layout.addWidget(self._empty_label, 1)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("dockScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setWidget(self._host)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(self.tabs_panel)
        root.addWidget(self.scroll, 1)

        register_dock(self)
        self._relayout_cards(force=True)
        self._update_state()

    @staticmethod
    def _chip_btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("chip")
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(28)
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------ вкладки FAV / SET
    @property
    def view_mode(self) -> str:
        """``'fav'`` (вкладка 0: только избранное) или ``'all'`` (вкладка 1: все настройки и выбор избранного)."""
        return "all" if self.tabs.currentIndex() == 1 else "fav"

    @property
    def column_count(self) -> int:
        """Текущее число колонок группировки видимых приборов."""
        return self._last_ncols

    def set_tab(self, index: int) -> None:
        index = 1 if int(index) == 1 else 0
        if self.tabs.currentIndex() != index:
            self.tabs.setCurrentIndex(index)
        else:
            self._apply_view_mode()

    def set_view_mode(self, mode: str) -> None:
        self.set_tab(1 if mode == "all" else 0)

    def _on_tab_changed(self, index: int) -> None:
        if self.settings is not None:
            self.settings.setValue("DOCK/TAB", 1 if index == 1 else 0)
        self._apply_view_mode()
        self.changed.emit()

    def _apply_view_mode(self) -> None:
        self._prune_cards()
        mode = self.view_mode
        for win, card in list(self._cards.items()):
            if not sip.isdeleted(win):
                win.set_dock_view_mode(mode)
                card.sync_width()
        self._relayout_cards(force=True)
        self._host.adjustSize()

    # ------------------------------------------------------------ адаптивная многоколоночная группировка
    def _visible_cards(self) -> list[_DockCard]:
        self._prune_cards()
        return [
            card for win, card in self._cards.items()
            if not sip.isdeleted(win) and not sip.isdeleted(card) and not win.isHidden()
        ]

    def _compute_columns(self, visible_cards: list[_DockCard]) -> int:
        n = len(visible_cards)
        if n <= 1:
            return n
        avail = self.scroll.viewport().width()
        if avail <= 100:
            avail = max(self.width() - 16, 400)
        col_min = max((c.minimumWidth() for c in visible_cards), default=372)
        spacing = self._columns_layout.spacing()
        ncols = max(1, (avail + spacing) // (col_min + spacing))
        return min(n, ncols)

    def _clear_columns(self) -> None:
        # Сначала перенести карточки обратно в self._columns_host, чтобы deleteLater колонок их не удалил
        for card in self._cards.values():
            if not sip.isdeleted(card) and card.parentWidget() is not self._columns_host:
                card.setParent(self._columns_host)
        for col_w in self._column_widgets:
            if not sip.isdeleted(col_w):
                self._columns_layout.removeWidget(col_w)
                col_w.hide()
                col_w.deleteLater()
        self._column_widgets.clear()

    def _relayout_cards(self, *, force: bool = False) -> None:
        if self._relayouting or sip.isdeleted(self):
            return
        self._relayouting = True
        try:
            visible_cards = self._visible_cards()
            if not visible_cards:
                if force or self._column_widgets or self._last_ncols != 0:
                    self._clear_columns()
                    self._last_ncols = 0
                self._columns_host.hide()
                self._empty_label.show()
                return

            ncols = self._compute_columns(visible_cards)
            if not force and ncols == self._last_ncols and len(self._column_widgets) == ncols:
                return

            self._empty_label.hide()
            self._columns_host.show()
            self._clear_columns()

            col_layouts: list[QVBoxLayout] = []
            col_heights = [0] * ncols
            for _ in range(ncols):
                col_w = QWidget(self._columns_host)
                col_w.setObjectName("dockCol")
                col_w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                cl = QVBoxLayout(col_w)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(8)
                self._columns_layout.addWidget(col_w, 1)
                col_w.show()
                self._column_widgets.append(col_w)
                col_layouts.append(cl)

            # Балансировка по высоте (masonry): каждый прибор попадает в самую короткую колонку
            for card in visible_cards:
                col_idx = min(range(ncols), key=lambda idx: col_heights[idx])
                card.setParent(self._column_widgets[col_idx])
                col_layouts[col_idx].addWidget(card)
                card.show()
                hint_h = card.sizeHint().height()
                col_heights[col_idx] += max(hint_h, 120) + 8

            for cl in col_layouts:
                cl.addStretch(1)
            self._last_ncols = ncols
        finally:
            self._relayouting = False

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        visible_cards = self._visible_cards()
        if visible_cards and self._compute_columns(visible_cards) != self._last_ncols:
            self._relayout_cards(force=True)

    def showEvent(self, e):  # noqa: N802 - Qt
        super().showEvent(e)
        visible_cards = self._visible_cards()
        if visible_cards and self._compute_columns(visible_cards) != self._last_ncols:
            self._relayout_cards(force=True)

    # ------------------------------------------------------------ состав
    def windows(self) -> list:
        """Все живые прикреплённые окна приборов (включая временно скрытые)."""
        self._prune_cards()
        return [c.instrument_window for c in self._cards.values()]

    def visible_windows(self) -> list:
        """Прикреплённые окна приборов, которые не скрыты кнопкой закрытия."""
        return [w for w in self.windows() if not w.isHidden()]

    def contains(self, win) -> bool:
        self._prune_cards()
        return win in self._cards

    def _prune_cards(self) -> None:
        dead = [w for w, c in self._cards.items() if sip.isdeleted(w) or sip.isdeleted(c)]
        for w in dead:
            card = self._cards.pop(w)
            if not sip.isdeleted(card):
                card.hide()
                card.deleteLater()

    def _update_state(self) -> None:
        self._prune_cards()
        shown = [w for w in self._cards if not w.isHidden()]
        total = len(self._cards)
        self.title_label.setText(f"({len(shown)})" if shown else "")
        self._empty_label.setVisible(len(shown) == 0)
        self._columns_host.setVisible(len(shown) > 0)
        self.undock_all_button.setEnabled(total > 0)
        self.changed.emit()

    # ------------------------------------------------------------ прикрепить / открепить
    def attach(self, win, *, show: bool = True, save: bool = True) -> None:
        """Прикрепить ``InstrumentWindow`` в этот док (автоматически группируется по колонкам)."""
        if win is None or sip.isdeleted(win):
            return
        self._prune_cards()
        if win in self._cards:
            if show:
                self.reveal(win)
            return
        other = getattr(win, "_dock", None)
        if other is not None and other is not self and not sip.isdeleted(other):
            other.detach(win, show=False, save=False)

        if win.isWindow():
            win._floating_pos = QPoint(win.pos())
            win._floating_size = QSize(win.size())

        card = _DockCard(win, self, self._columns_host)
        win.hide()
        win.setParent(card, Qt.WindowType.Widget)
        card.card_layout.addWidget(win)
        self._cards[win] = card

        win._on_dock_attached(self, save=save)
        card.sync_width()
        card.setVisible(bool(show))
        win.setVisible(bool(show))
        win._fit_height()
        self._relayout_cards(force=True)
        if show:
            self.scroll.ensureWidgetVisible(card)
        self._update_state()

    def detach(self, win, *, show: bool = True, save: bool = True) -> None:
        """Открепить ``InstrumentWindow`` обратно в отдельное плавающее окно."""
        if win is None or sip.isdeleted(win):
            return
        self._prune_cards()
        card = self._cards.pop(win, None)
        if card is not None and not sip.isdeleted(card):
            win.removeEventFilter(card)
            card.card_layout.removeWidget(win)
            card.hide()
            card.deleteLater()

        fallback_pos = self.mapToGlobal(QPoint(24, 24)) if self.isVisible() else None
        win._on_dock_detached(show=show, save=save, fallback_pos=fallback_pos)
        self._relayout_cards(force=True)
        self._update_state()

    def reveal(self, win) -> None:
        """Показать прикреплённое окно прибора и прокрутить до него."""
        self._prune_cards()
        card = self._cards.get(win)
        if card is None:
            return
        card.sync_width()
        card.show()
        win.show()
        win._fit_height()
        self._relayout_cards(force=True)
        self.scroll.ensureWidgetVisible(card)
        self._update_state()

    def dock_all(self) -> None:
        """Прикрепить все окна приборов текущего процесса."""
        from .instrument_window import all_instrument_windows
        for win in all_instrument_windows():
            if not self.contains(win):
                self.attach(win, show=True, save=True)
            else:
                self.reveal(win)

    def undock_all(self) -> None:
        """Открепить все прикреплённые окна приборов."""
        for win in list(self.windows()):
            was_shown = not win.isHidden()
            self.detach(win, show=was_shown, save=True)


class InstrumentDockWindow(Window):
    """Независимое окно приборов (аналог ``MainWindow`` для инструментов).

    В заголовке — плитки всех приборов стенда (VNA, SA, PM, GEN, PULSE, PSU) и F11.
    Внутри — ``InstrumentDock`` с вкладками ``FAV`` (только избранное) и ``SET`` (настройки и
    настройка состава избранного), сгруппированными в адаптивную многоколоночную сетку.
    """

    SETTINGS_GROUP = "INSTRUMENTS"
    visibility_changed = pyqtSignal(bool)

    def __init__(self, settings: QSettings, monitor=None, parent=None):
        super().__init__(settings, parent)
        self.monitor = monitor
        self.tiles: dict[object, InstrumentTile] = {}
        self.setWindowTitle("INSTRUMENTS")

        # Плитки приборов (VNA, SA, PM, GEN, PULSE, PSU) — в заголовке окна инструментов
        self.titleBar.title_label.hide()
        self.titleBar.setStyleSheet(WindowBar.STYLE_SHEET + "\n" + _TILE_QSS)
        self._tiles_host = QWidget(self.titleBar)
        self._tiles_host.setObjectName("tilesHost")
        self._tiles_host.setStyleSheet(_TILE_QSS)
        self.tiles_layout = QHBoxLayout(self._tiles_host)
        self.tiles_layout.setContentsMargins(0, 0, 0, 0)
        self.tiles_layout.setSpacing(4)
        self.titleBar.insert_widget(self._tiles_host, 0)

        self.full_button = CustomFullButton()
        self.full_button.setToolTip("Full screen (F11)")
        self.full_button.clicked.connect(self.toggle_full_screen)
        self.titleBar.insert_control_widget(self.full_button, after_minimize=True)
        self._full_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        self._full_key.activated.connect(self.toggle_full_screen)

        self.dock = InstrumentDock(self, settings=settings, closable=False, is_standalone=True)
        self.dock.changed.connect(self._refresh_all_tiles)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.dock, 1)
        self.setMinimumWidth(420)
        self.setMinimumHeight(280)
        self.recall_size(QSize(960, 640))

        if self.monitor is not None:
            from .instrument_window import WINDOW_EVENTS
            self.monitor.roster_changed.connect(self.rebuild_tiles)
            self.monitor.changed.connect(self.update_tile)
            WINDOW_EVENTS.changed.connect(self.update_tile)
            self.rebuild_tiles()

    # ------------------------------------------------------------ плитки приборов в заголовке
    def rebuild_tiles(self) -> None:
        if sip.isdeleted(self) or self.monitor is None:
            return
        proxies = self.monitor.proxies()
        for p in [p for p in self.tiles if p not in proxies]:
            self.tiles.pop(p).deleteLater()
        for i, p in enumerate(proxies):
            if p not in self.tiles:
                tile = InstrumentTile(p, self._tiles_host)
                tile.clicked.connect(lambda _=False, pr=p: self.open_instrument(pr))
                self.tiles[p] = tile
            self.tiles_layout.insertWidget(i, self.tiles[p])
        for p in proxies:
            self.update_tile(p)

    def _refresh_all_tiles(self) -> None:
        if sip.isdeleted(self):
            return
        for p in list(self.tiles):
            self.update_tile(p)

    def update_tile(self, proxy) -> None:
        if sip.isdeleted(self) or self.monitor is None:
            return
        tile = self.tiles.get(proxy)
        if tile is None or sip.isdeleted(tile):
            return
        from .instrument_window import window_for
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

    def open_instrument(self, proxy) -> None:
        """Открыть / показать окно прибора (в доке или отдельным окном)."""
        from .instrument_window import window_for
        w = window_for(proxy)
        if w is None:
            return
        if w.is_docked:
            if w._dock is self.dock:
                if self.isMinimized():
                    self.showNormal()
                self.show()
                self.raise_()
                self.activateWindow()
                self.dock.reveal(w)
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

    # ------------------------------------------------------------ во весь экран
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

    def cleanup(self) -> None:
        if self.monitor is not None:
            from .instrument_window import WINDOW_EVENTS
            for sig, slot in (
                (self.monitor.roster_changed, self.rebuild_tiles),
                (self.monitor.changed, self.update_tile),
                (WINDOW_EVENTS.changed, self.update_tile),
            ):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass

    def showEvent(self, e):  # noqa: N802 - Qt
        super().showEvent(e)
        self.visibility_changed.emit(True)
        self._refresh_all_tiles()

    def hideEvent(self, e):  # noqa: N802 - Qt
        super().hideEvent(e)
        self.visibility_changed.emit(False)
        self._refresh_all_tiles()

    def closeEvent(self, event):  # noqa: N802 - Qt
        super().closeEvent(event)
        self.visibility_changed.emit(False)
        self._refresh_all_tiles()
