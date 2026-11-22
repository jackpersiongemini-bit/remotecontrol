"""Контейнер (док-панель) для окон приборов: прикрепление и открепление по клику.

Окна приборов (``InstrumentWindow``) могут работать как отдельные плавающие окна или
прикрепляться внутрь ``InstrumentDock`` (встроен в ``MainWindow`` либо открыт отдельным
окном ``InstrumentDockWindow`` при автономном запуске прибора). При прикреплении окно
прибора остаётся тем же виджетом ``InstrumentWindow`` со всеми сигналами, привязками и
вкладками — меняется только его родитель и режим заголовка.
"""
from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE,
                    UI_COLOR_TEXT_MUTED, WINDOW_BAR_HEIGHT, checkbox_qss, render_qss, scrollbar_qss)
from .window import Window

__all__ = ["InstrumentDock", "InstrumentDockWindow", "active_dock", "register_dock", "unregister_dock"]

_DOCKS: list["InstrumentDock"] = []
_STANDALONE_WINDOW: "InstrumentDockWindow | None" = None


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
    """Вернуть основной док процесса (обычно док ``MainWindow``). При ``create=True`` создаст
    отдельное окно ``InstrumentDockWindow``, если главного окна в процессе нет."""
    global _STANDALONE_WINDOW
    _prune_docks()
    if _DOCKS:
        # 1. Предпочитаем видимый док внутри открытого MainWindow (самый свежий)
        for d in reversed(_DOCKS):
            if not d.is_standalone and d.window().isVisible():
                return d
        # 2. Иначе самый свежий не-standalone док
        for d in reversed(_DOCKS):
            if not d.is_standalone:
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


class _DockCard(QFrame):
    """Рамка вокруг прикреплённого окна прибора в списке дока."""

    def __init__(self, win: QWidget, dock: "InstrumentDock", parent=None):
        super().__init__(parent)
        self.setObjectName("dockCard")
        self.instrument_window = win
        self._dock = dock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.card_layout = lay
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        win.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt
        if obj is self.instrument_window and not sip.isdeleted(self):
            et = event.type()
            if et == QEvent.Type.HideToParent:
                self.setVisible(False)
                self._dock._update_state()
            elif et == QEvent.Type.ShowToParent:
                self.setVisible(True)
                self._dock._update_state()
        return super().eventFilter(obj, event)


class InstrumentDock(QFrame):
    """Панель-контейнер для прикреплённых окон приборов с прокруткой и кнопками DOCK ALL / UNDOCK ALL."""

    #: состав или видимость прикреплённых приборов изменились
    changed = pyqtSignal()
    #: пользователь нажал кнопку скрытия дока
    close_requested = pyqtSignal()

    STYLE_SHEET = render_qss("""
        InstrumentDock {
            background-color: $UI_COLOR_PANEL;
            border: 1px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentDock QWidget#dockHeader, InstrumentDock QWidget#dockHost {
            background: transparent;
            border: none;
        }
        InstrumentDock QScrollArea#dockScroll {
            background: transparent;
            border: none;
        }
        InstrumentDock QLabel#dockTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
        }
        InstrumentDock QLabel#dockEmpty {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 20px 12px;
        }
        InstrumentDock QPushButton#dockBtn {
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT_MUTED;
            border: 1px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: 9pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            padding: 2px 8px;
            min-height: 24px;
            max-height: 24px;
        }
        InstrumentDock QPushButton#dockBtn:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            border-color: $UI_COLOR_INVERT_HOVER;
        }
        InstrumentDock QPushButton#dockBtn:disabled {
            color: $UI_COLOR_BORDER;
            border-color: $UI_COLOR_BORDER;
        }
        InstrumentDock QFrame#dockCard {
            background-color: $UI_COLOR_BACKGROUND;
            border: 1px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
    """) + scrollbar_qss("InstrumentDock ") + "\n" + checkbox_qss("InstrumentDock ")

    def __init__(self, parent=None, *, closable: bool = True, is_standalone: bool = False):
        super().__init__(parent)
        self.is_standalone = is_standalone
        self._cards: dict[object, _DockCard] = {}
        self.setStyleSheet(self.STYLE_SHEET)
        self.setMinimumWidth(340)

        header = QWidget(self)
        header.setObjectName("dockHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(6, 4, 6, 4)
        hl.setSpacing(4)
        self.title_label = QLabel("INSTRUMENTS")
        self.title_label.setObjectName("dockTitle")
        self.dock_all_button = self._btn("DOCK ALL", "Attach all instrument windows into this dock", self.dock_all)
        self.undock_all_button = self._btn("UNDOCK ALL", "Detach all instruments into separate floating windows",
                                           self.undock_all)
        hl.addWidget(self.title_label)
        hl.addStretch(1)
        hl.addWidget(self.dock_all_button)
        hl.addWidget(self.undock_all_button)
        self.close_button: QPushButton | None = None
        if closable:
            self.close_button = self._btn("×", "Hide instrument dock panel", self.close_requested.emit)
            self.close_button.setFixedWidth(26)
            hl.addWidget(self.close_button)

        self._host = QWidget()
        self._host.setObjectName("dockHost")
        self._cards_layout = QVBoxLayout(self._host)
        self._cards_layout.setContentsMargins(4, 4, 4, 4)
        self._cards_layout.setSpacing(8)
        self._empty_label = QLabel("NO DOCKED INSTRUMENTS\nClick the dock button on any instrument window or DOCK ALL")
        self._empty_label.setObjectName("dockEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._cards_layout.addWidget(self._empty_label)
        self._cards_layout.addStretch(1)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("dockScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setWidget(self._host)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        root.addWidget(header)
        root.addWidget(self.scroll, 1)

        register_dock(self)
        self._update_state()

    @staticmethod
    def _btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("dockBtn")
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

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
                self._cards_layout.removeWidget(card)
                card.deleteLater()

    def _update_state(self) -> None:
        self._prune_cards()
        shown = [w for w in self._cards if not w.isHidden()]
        total = len(self._cards)
        self.title_label.setText(f"INSTRUMENTS ({len(shown)})" if shown else "INSTRUMENTS")
        self._empty_label.setVisible(len(shown) == 0)
        self.undock_all_button.setEnabled(total > 0)
        self.changed.emit()

    # ------------------------------------------------------------ прикрепить / открепить
    def attach(self, win, *, show: bool = True, save: bool = True) -> None:
        """Прикрепить ``InstrumentWindow`` в этот док."""
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

        card = _DockCard(win, self, self._host)
        win.hide()
        win.setParent(card, Qt.WindowType.Widget)
        card.card_layout.addWidget(win)
        # вставляем перед завершающим stretch
        insert_idx = max(0, self._cards_layout.count() - 1)
        self._cards_layout.insertWidget(insert_idx, card)
        self._cards[win] = card

        win._on_dock_attached(self, save=save)
        card.setVisible(bool(show))
        win.setVisible(bool(show))
        win._fit_height()
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
            self._cards_layout.removeWidget(card)
            card.hide()
            card.deleteLater()

        fallback_pos = self.mapToGlobal(QPoint(24, 24)) if self.isVisible() else None
        win._on_dock_detached(show=show, save=save, fallback_pos=fallback_pos)
        self._update_state()

    def reveal(self, win) -> None:
        """Показать прикреплённое окно прибора и прокрутить док к нему."""
        self._prune_cards()
        card = self._cards.get(win)
        if card is None:
            return
        card.show()
        win.show()
        win._fit_height()
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
    """Отдельное плавающее окно-док (используется при автономном запуске окна прибора без MainWindow)."""

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(settings, parent)
        self.setWindowTitle("INSTRUMENTS")
        self.dock = InstrumentDock(self, closable=False, is_standalone=True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(0)
        lay.addWidget(self.dock, 1)
        self.setMinimumWidth(400)
        self.recall_size(QSize(460, 680))
