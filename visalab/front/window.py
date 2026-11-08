"""Окно без системной рамки и его заголовок - по WindowClass.py Алексея.

``WindowBar``: слева виджеты прибора (кнопки ON / Sync / Init, адрес, индикаторы),
справа «свернуть» и «закрыть». Иконки кнопок рисуются QPainter, цвета - из theme.
"""
from __future__ import annotations

import textwrap
from enum import Enum

from PyQt6.QtCore import QLineF, QRectF, QSettings, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QPushButton, QWidget
from qframelesswindow import FramelessWindow, TitleBar

from .theme import (OFF, ON, TITLE_BAR_BUTTON_LINE_WIDTH, UI_COLOR_BORDER, UI_COLOR_ERROR, UI_COLOR_PANEL,
                    UI_COLOR_PROCESS, UI_COLOR_SUCCESS, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_TITLE_BAR_ITEM_SIZE_PX, WINDOW_BAR_HEIGHT, render_qss)

__all__ = ["WriteState", "Window", "WindowBar", "StatusIndicator", "ErrorIndicator", "AddressLabel", "short_address",
           "CustomExpandButton",
           "CustomOnButton", "CustomSyncButton", "CustomInitButton"]


class WriteState(Enum):
    OFF = "off"
    PROCESS = "process"
    ERROR = "error"
    VERIFIED = "verified"


class WindowBar(TitleBar):
    STYLE_SHEET = render_qss("""
        WindowBar, WindowBar QLabel { background-color: $UI_COLOR_CONTROL; }
        WindowBar { border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER; }
        WindowBar QLabel#windowTitle { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; padding: 0 6px; }
        WindowBar IconButtonBase {
            max-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; min-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            background-color: $UI_COLOR_BORDER; border: none; border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        WindowBar IconButtonBase:hover { background-color: $UI_COLOR_INVERT_HOVER; }
        WindowBar IconButtonBase[status="process"] { background-color: $UI_COLOR_PROCESS; }
        WindowBar CustomOnButton[status="ON"] { background-color: $UI_COLOR_SUCCESS; }
        WindowBar CustomOnButton[status="ON"]:hover { background-color: $UI_COLOR_SUCCESS_HOVER; }
        WindowBar CustomOnButton[status="error"] { background-color: $UI_COLOR_ERROR; }
        WindowBar CustomCloseButton:hover { background-color: $UI_COLOR_ERROR; }
        WindowBar AddressLabel {
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            padding: 0 8px; background-color: $UI_COLOR_BACKGROUND; color: $UI_COLOR_TEXT_MUTED;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
    """)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE_SHEET)
        for b in (self.minBtn, self.maxBtn, self.closeBtn):     # стандартные не стилизуются QSS
            b.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(WINDOW_BAR_HEIGHT)
        self.hBoxLayout.setContentsMargins(7, 7, 7, 7)
        self.hBoxLayout.setSpacing(5)
        self.setDoubleClickEnabled(False)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("windowTitle")
        self.minimized_button = CustomMinButton()
        self.minimized_button.setToolTip("Minimize")
        self.minimized_button.clicked.connect(lambda: self.window().showMinimized())
        self.close_button = CustomCloseButton()
        self.close_button.setToolTip("Close")
        self.close_button.clicked.connect(lambda: self.window().close())
        self.hBoxLayout.insertWidget(0, self.title_label)
        self.hBoxLayout.addWidget(self.minimized_button)
        self.hBoxLayout.addWidget(self.close_button)

    def insert_widget(self, widget, index: int = 0) -> None:
        self.hBoxLayout.insertWidget(index, widget)

    def insert_control_widget(self, widget) -> None:
        """Перед «свернуть»/«закрыть» (после растяжки)."""
        self.hBoxLayout.insertWidget(self.hBoxLayout.indexOf(self.minimized_button), widget)


class Window(FramelessWindow):
    STYLE_SHEET = render_qss("""
        QWidget { background-color: $UI_COLOR_BACKGROUND; font-family: $UI_FONT_FAMILY;
                  font-size: ${UI_FONT_SIZE_PT}pt; color: $UI_COLOR_TEXT; }
        QToolTip { padding: 3px 6px; color: $UI_COLOR_TEXT; background-color: $UI_COLOR_PANEL;
                   border: 1px solid $UI_COLOR_BORDER; border-radius: ${UI_BORDER_RADIUS_PX}px; }
    """)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._size_key = f"WINDOW_SIZE/{type(self).__module__}.{type(self).__qualname__}"
        self.setStyleSheet(self.STYLE_SHEET)
        self.setTitleBar(WindowBar(self))
        self.titleBar.raise_()
        self.windowTitleChanged.connect(self.titleBar.title_label.setText)

    def recall_size(self, default: QSize) -> None:
        self.resize(self.settings.value(self._size_key, default, type=QSize))

    def save_size(self) -> None:
        self.settings.setValue(self._size_key, self.size())

    def closeEvent(self, event):  # noqa: N802 - Qt
        self.save_size()
        super().closeEvent(event)


# ------------------------------------------------------------- индикаторы
class WriteIndicator(QWidget):
    """Сводный огонёк записи; подробности по каждому параметру - в подсказке."""

    STATE_COLORS = {WriteState.OFF: UI_COLOR_BORDER, WriteState.PROCESS: UI_COLOR_PROCESS,
                    WriteState.ERROR: UI_COLOR_ERROR, WriteState.VERIFIED: UI_COLOR_SUCCESS}
    STATE_TEXT = {WriteState.OFF: "not written", WriteState.PROCESS: "writing",
                  WriteState.ERROR: "error", WriteState.VERIFIED: "written"}
    EMPTY = "No errors or unwritten data"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: tuple = ()
        self._active = False
        self.state = WriteState.OFF
        self.setFixedSize(UI_TITLE_BAR_ITEM_SIZE_PX, UI_TITLE_BAR_ITEM_SIZE_PX)
        self.setToolTip(self.EMPTY)

    def _filter(self, items):
        return items

    def set_items(self, items, active: bool = True) -> None:
        """items: [(name, WriteState, detail)]"""
        self._items = tuple(self._filter(items))
        self._active = active
        states = [s for _n, s, _d in self._items]
        if WriteState.ERROR in states:
            self.state = WriteState.ERROR
        elif WriteState.PROCESS in states:
            self.state = WriteState.PROCESS
        elif active:
            self.state = WriteState.VERIFIED
        else:
            self.state = WriteState.OFF
        lines = []
        for name, state, detail in self._items:
            if state is WriteState.VERIFIED:
                continue
            text = f"{name}: {detail}" if state is WriteState.ERROR and detail else f"{name}: {self.STATE_TEXT[state]}"
            lines.extend(textwrap.wrap(text, 72) or [text])
        self.setToolTip("\n".join(lines) or self.EMPTY)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = min(self.width(), self.height()) - 8
        p.setPen(QPen(QColor(UI_COLOR_PANEL), 1))
        p.setBrush(QColor(self.STATE_COLORS[self.state]))
        p.drawEllipse(QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d))


class StatusIndicator(WriteIndicator):
    """Только ход записи (без ошибок)."""
    EMPTY = "No pending writes"

    def _filter(self, items):
        return [i for i in items if i[1] is not WriteState.ERROR]


class ErrorIndicator(WriteIndicator):
    """Только ошибки и их подробности."""
    EMPTY = "No errors"

    def _filter(self, items):
        return [i for i in items if i[1] is WriteState.ERROR]


class AddressLabel(QLabel):
    pass


# ------------------------------------------------------- кнопки-иконки
class IconButtonBase(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.icon_color_normal = QColor(UI_COLOR_TEXT_MUTED)
        self.icon_color_hover = QColor(UI_COLOR_TEXT_INVERSE)
        self._icon_color = self._rest_color = self.icon_color_normal

    def enterEvent(self, e):  # noqa: N802 - Qt
        super().enterEvent(e)
        self._icon_color = self.icon_color_hover
        self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        super().leaveEvent(e)
        self._icon_color = self._rest_color
        self.update()

    def set_status(self, status: str) -> None:
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)
        self._rest_color = self.icon_color_normal if status in ("ok", OFF) else self.icon_color_hover
        self._icon_color = self._rest_color
        self.setEnabled(status != "process")
        self.setCursor(Qt.CursorShape.WaitCursor if status == "process" else Qt.CursorShape.PointingHandCursor)
        self.update()

    def status(self) -> str:
        return self.property("status") or "ok"

    def _pen(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._icon_color)
        pen.setWidth(TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)


class CustomMinButton(IconButtonBase):
    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        y = self.height() - 7
        p.drawLine(7, y, self.width() - 7, y)


class CustomCloseButton(IconButtonBase):
    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        m = 8
        p.drawLine(m, m, self.width() - m, self.height() - m)
        p.drawLine(m, self.height() - m, self.width() - m, m)


class CustomInitButton(IconButtonBase):
    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy = self.width() / 2, self.height() / 2
        p.drawLine(QLineF(cx - 4.5, cy + 4.5, cx + 4.5, cy + 4.5))
        p.drawLine(QLineF(cx, cy - 4.5, cx, cy + 1.5))
        p.drawLine(QLineF(cx, cy + 1.5, cx - 3.0, cy - 1.5))
        p.drawLine(QLineF(cx, cy + 1.5, cx + 3.0, cy - 1.5))


class CustomOnButton(IconButtonBase):
    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy, r = self.width() / 2, self.height() / 2, 4.5
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 120 * 16, 300 * 16)
        p.drawLine(QLineF(cx, cy - r - 1.0, cx, cy + 0.5))


class CustomSyncButton(IconButtonBase):
    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy, dy, ln, a = self.width() / 2, self.height() / 2, 2.5, 4.5, 2.5
        y1, y2 = cy - dy, cy + dy
        p.drawLine(QLineF(cx - ln, y1, cx + ln, y1))
        p.drawLine(QLineF(cx + ln, y1, cx + ln - a, y1 - a))
        p.drawLine(QLineF(cx + ln, y1, cx + ln - a, y1 + a))
        p.drawLine(QLineF(cx - ln, y2, cx + ln, y2))
        p.drawLine(QLineF(cx - ln, y2, cx - ln + a, y2 - a))
        p.drawLine(QLineF(cx - ln, y2, cx - ln + a, y2 + a))


class CustomExpandButton(IconButtonBase):
    """Шеврон: вниз - развернуть, вверх - свернуть (свойство ``expanded``)."""

    expanded = False

    def set_expanded(self, on: bool) -> None:
        self.expanded = bool(on)
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy, a = self.width() / 2, self.height() / 2, 4.0
        d = -a / 2 if self.expanded else a / 2
        p.drawLine(QLineF(cx - a, cy - d, cx, cy + d))
        p.drawLine(QLineF(cx, cy + d, cx + a, cy - d))


def short_address(address: str) -> str:
    """Короткая подпись VISA-адреса: IP для TCPIP, серийный номер для USB."""
    parts = [p for p in address.split("::") if p]
    if not parts:
        return address
    head = parts[0].upper()
    if head.startswith("TCPIP") and len(parts) >= 2:
        return parts[1]
    if head.startswith("USB") and len(parts) >= 4:
        return parts[3]
    return address
