"""Окно без системной рамки и его заголовок - по WindowClass.py Алексея.

``WindowBar``: слева виджеты прибора (кнопки ON / Sync / Init, адрес, индикаторы),
справа «свернуть» и «закрыть». Иконки кнопок рисуются QPainter, цвета - из theme.
"""
from __future__ import annotations

import json
import textwrap
from contextlib import contextmanager
from enum import Enum

from PyQt6.QtCore import QEasingCurve, QLineF, QPointF, QPropertyAnimation, QRectF, QSettings, QSize, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractButton, QLabel, QPushButton, QSizePolicy, QWidget
from qframelesswindow import FramelessWindow, TitleBar

from .theme import (OFF, ON, ON_EQV, TITLE_BAR_BUTTON_LINE_WIDTH, UI_COLOR_BORDER, UI_COLOR_CONTROL, UI_COLOR_ERROR,
                    UI_COLOR_PANEL, UI_SWITCH_HEIGHT_PX, UI_SWITCH_MARGIN_PX, UI_SWITCH_WIDTH_PX,
                    UI_COLOR_PROCESS, UI_COLOR_SUCCESS, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_TITLE_BAR_ITEM_SIZE_PX, WINDOW_BAR_HEIGHT, render_qss, scrollbar_qss)

__all__ = ["WriteState", "Window", "WindowBar", "StatusIndicator", "ErrorIndicator", "AddressLabel", "short_address",
           "CustomExpandButton", "CustomFullButton", "CustomLockButton", "Switch",
           "CustomOnButton", "CustomSyncButton", "CustomInitButton", "mark_growing", "QWIDGETSIZE_MAX"]

QWIDGETSIZE_MAX = (1 << 24) - 1


def mark_growing(widget: QWidget) -> QWidget:
    """Виджет растёт по высоте вместе с окном (график, журнал): окно с ним можно растянуть ниже его
    обычного размера и выше - лишняя высота достаётся ему (``Window.fit_to``)."""
    pol = widget.sizePolicy()
    pol.setVerticalPolicy(QSizePolicy.Policy.Expanding)
    widget.setSizePolicy(pol)
    widget.setProperty("grows", True)
    return widget


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
        WindowBar ErrorIndicator[err="true"] { background-color: $UI_COLOR_ERROR; }
        WindowBar ErrorIndicator[err="true"]:hover { background-color: $UI_COLOR_INVERT_HOVER; }
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

    def insert_control_widget(self, widget, after_minimize: bool = False) -> None:
        """Перед «свернуть»/«закрыть» (или правее «свернуть», перед «закрыть»)."""
        anchor = self.close_button if after_minimize else self.minimized_button
        self.hBoxLayout.insertWidget(self.hBoxLayout.indexOf(anchor), widget)

    def canDrag(self, pos):  # noqa: N802 - qframelesswindow
        return not self.window().isFullScreen() and super().canDrag(pos)     # во весь экран окно не таскается


class Window(FramelessWindow):
    STYLE_SHEET = render_qss("""
        QWidget { background-color: $UI_COLOR_BACKGROUND; font-family: $UI_FONT_FAMILY;
                  font-size: ${UI_FONT_SIZE_PT}pt; color: $UI_COLOR_TEXT; }
        QToolTip { padding: 3px 6px; color: $UI_COLOR_TEXT; background-color: $UI_COLOR_PANEL;
                   border: 1px solid $UI_COLOR_BORDER; border-radius: ${UI_BORDER_RADIUS_PX}px; }
    """) + scrollbar_qss()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        name = f"{type(self).__module__}.{type(self).__qualname__}"
        self._size_key = f"WINDOW_SIZE/{name}"
        self._heights_key = f"WINDOW_HEIGHTS/{name}"
        #: вид окна (``view_key``) -> высота, до которой его растянули руками (ini, при закрытии / выходе)
        try:
            saved = json.loads(settings.value(self._heights_key, "", type=str) or "{}")
            self.view_heights = {str(k): int(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
        except (TypeError, ValueError):
            self.view_heights = {}
        self._saved_heights = dict(self.view_heights)
        self._fit_target: int | None = None          # высота, которую ставит fit_to (остальное - руками)
        self._fit_hint, self._fit_grows = 0, False
        self._fitting = 0
        self.setStyleSheet(self.STYLE_SHEET)
        self.setTitleBar(WindowBar(self))
        self.titleBar.raise_()
        self.windowTitleChanged.connect(self.titleBar.title_label.setText)

    def recall_size(self, default: QSize) -> None:
        self.resize(self.settings.value(self._size_key, default, type=QSize))

    def save_size(self) -> None:
        """Размер и растянутые высоты видов - в ini (только если изменились: QSettings переписывает файл)."""
        size = self.size()
        if self.isFullScreen() or self.isMaximized():      # обычный размер, а не размер экрана
            size = self.normalGeometry().size()
            if not size.isValid() or size.isEmpty():
                return
        if self.settings.value(self._size_key, None, type=QSize) != size:
            self.settings.setValue(self._size_key, size)
        if self.view_heights != self._saved_heights:
            self.settings.setValue(self._heights_key, json.dumps(self.view_heights, sort_keys=True))
            self._saved_heights = dict(self.view_heights)

    # ------------------------------------------------------------ высота по содержимому
    def view_key(self) -> str:
        """Что сейчас показано (вкладка, журнал...): у каждого вида своя растянутая высота."""
        return "main"

    @contextmanager
    def fitting(self):
        """Высоту меняет программа (раскладка, ``fit_to``), а не пользователь - не запоминать."""
        self._fitting += 1
        try:
            yield
        finally:
            self._fitting -= 1

    def fit_to(self, layout, grows: bool = False) -> None:
        """Высота окна - по содержимому ``layout``; растянуть можно всегда (Алексей 2026-11-14: «окна должны
        растягиваться»). ``grows`` - в виде есть график / журнал (``mark_growing``): окно можно и сжать до
        их минимума. Растянутая руками высота запоминается для вида и возвращается, когда он снова открыт."""
        hint = layout.sizeHint().height()
        minimum = layout.minimumSize().height() if grows else hint
        saved = self.view_heights.get(self.view_key())
        want = max(saved, minimum) if saved else hint
        with self.fitting():                           # setMinimumHeight сам меняет высоту окна
            self._fit_hint, self._fit_grows = hint, grows
            self.setMinimumHeight(minimum)
            self.setMaximumHeight(QWIDGETSIZE_MAX)
            if self.isMaximized() or self.isFullScreen():
                return
            self._fit_target = want
            if self.height() != want:
                self.resize(self.width(), want)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        h = e.size().height()
        if (self._fit_target is not None and not self._fitting and self.isVisible() and e.oldSize().height() > 0
                and h != e.oldSize().height() and h != self._fit_target
                and not (self.isMaximized() or self.isFullScreen())):
            key = self.view_key()                      # растянули руками - запомнить для этого вида
            if not self._fit_grows and h <= self._fit_hint:
                self.view_heights.pop(key, None)       # сжали до содержимого - снова по содержимому
            else:
                self.view_heights[key] = h
            self._fit_target = h

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


class AddressLabel(QLabel):
    """Что за прибор (VNA, PM...) в заголовке, адрес - в подсказке; двойной клик открывает настройки
    подключения (Алексей 2026-11-14: по одному клику открывались случайно)."""

    double_clicked = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)


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
        pen.setWidthF(TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)


class CustomLockButton(IconButtonBase):
    """Замок (кнопка с фиксацией): закрыт - checked. Рисуется вектором, как кнопки заголовка."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.toggled.connect(lambda _on: self.update())

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        if self.isChecked():                      # инверсия: тёмный значок на светлом фоне
            self._icon_color = QColor(UI_COLOR_TEXT_INVERSE)
        elif not self.underMouse():
            self._icon_color = self._rest_color
        self._pen(p)
        cx, cy = self.width() / 2, self.height() / 2
        bw, bh = 10.0, 7.0                        # корпус
        body = QRectF(cx - bw / 2, cy - 0.5, bw, bh)
        p.drawRoundedRect(body, 1.5, 1.5)
        r = 3.0                                   # дужка: открыт - поднята над корпусом
        base = cy - 0.5 - (0.0 if self.isChecked() else 2.5)
        top = base - 4.5
        path = QPainterPath()
        path.moveTo(cx - r, base)
        path.lineTo(cx - r, top + r)
        path.arcTo(QRectF(cx - r, top, 2 * r, 2 * r), 180, -180)
        path.lineTo(cx + r, base)
        p.drawPath(path)
        p.drawLine(QLineF(cx, body.top() + 2.5, cx, body.top() + 4.5))   # скважина


class ErrorIndicator(IconButtonBase):
    """Кнопка ошибок записи / связи: треугольник с «!». Есть ошибки - красный фон.

    Подробности - в подсказке, клик открывает журнал ошибок.
    """

    EMPTY = "No errors"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = WriteState.OFF
        self._items: tuple = ()
        self.setToolTip(self.EMPTY)

    def set_items(self, items, active: bool = True) -> None:
        """items: [(name, WriteState, detail)] - учитываются только ошибки."""
        self._items = tuple(i for i in items if i[1] is WriteState.ERROR)
        self.state = WriteState.ERROR if self._items else (WriteState.VERIFIED if active else WriteState.OFF)
        lines = []
        for name, _state, detail in self._items:
            text = f"{name}: {detail}" if detail else f"{name}: error"
            lines.extend(textwrap.wrap(text, 72) or [text])
        self.setToolTip("\n".join(lines) or self.EMPTY)
        self.setProperty("err", self.state is WriteState.ERROR)
        self.style().unpolish(self)
        self.style().polish(self)
        self._rest_color = QColor(UI_COLOR_TEXT if self.state is WriteState.ERROR else UI_COLOR_TEXT_MUTED)
        self._icon_color = self.icon_color_hover if self.underMouse() else self._rest_color
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy = self.width() / 2, self.height() / 2
        h, w = 9.5, 11.0                          # треугольник
        top, base = cy - h / 2 - 0.5, cy + h / 2 - 0.5
        path = QPainterPath()
        path.moveTo(cx, top)
        path.lineTo(cx + w / 2, base)
        path.lineTo(cx - w / 2, base)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QLineF(cx, top + 3.2, cx, base - 3.0))       # «!»
        p.drawPoint(QPointF(cx, base - 1.4))


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


class CustomFullButton(IconButtonBase):
    """Во весь экран: уголки наружу; в полноэкранном режиме (``full``) - уголки внутрь."""

    full = False

    def set_full(self, on: bool) -> None:
        self.full = bool(on)
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        p = QPainter(self)
        self._pen(p)
        cx, cy, a, c = self.width() / 2, self.height() / 2, 5.0, 3.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                if self.full:                     # уголок внутри квадрата, концы - наружу
                    x, y = cx + sx * (a - c), cy + sy * (a - c)
                    p.drawLine(QLineF(x, y, x + sx * c, y))
                    p.drawLine(QLineF(x, y, x, y + sy * c))
                else:                             # уголок в вершине квадрата, концы - внутрь
                    x, y = cx + sx * a, cy + sy * a
                    p.drawLine(QLineF(x, y, x - sx * c, y))
                    p.drawLine(QLineF(x, y, x, y - sy * c))


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


class Switch(QAbstractButton):
    """Анимированный переключатель ON/OFF для заголовка окна (по WindowClass.py).

    ``set_status``: ON / OFF / "process" (связь устанавливается) / "error" / "retry" (включён, но связи
    нет - прибор переподключается сам: ручка справа, цвет ошибки; клик - отключить).
    """

    COLORS = {OFF: UI_COLOR_ERROR, ON: UI_COLOR_SUCCESS, "process": UI_COLOR_PROCESS, "error": UI_COLOR_ERROR,
              "retry": UI_COLOR_ERROR}
    HANDLE_COLOR = QColor(UI_COLOR_CONTROL)
    HANDLE_BORDER_COLOR = QColor(UI_COLOR_BORDER)

    def __init__(self, state=OFF, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.sizeHint())
        self._status = ON if state in ON_EQV else OFF
        self._handle_position = self._position_for_state(False)
        self._animation = QPropertyAnimation(self, b"handle_position", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        checked = state in ON_EQV
        self.setChecked(checked)
        self._handle_position = self._position_for_state(checked)
        self.toggled.connect(self._animate_handle)

    def sizeHint(self):  # noqa: N802 - Qt
        return QSize(UI_SWITCH_WIDTH_PX, UI_SWITCH_HEIGHT_PX)

    def minimumSizeHint(self):  # noqa: N802 - Qt
        return self.sizeHint()

    def value(self) -> str:
        return ON if self.isChecked() else OFF

    def set_value(self, value) -> None:
        self.setChecked(value in ON_EQV)

    def status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        """Состояние связи: ручка - включено/выключено, цвет дорожки - статус."""
        self._status = status
        self.blockSignals(True)
        self.setChecked(status in (ON, "process", "retry"))
        self.blockSignals(False)
        self._animate_handle(self.isChecked())
        self.setCursor(Qt.CursorShape.WaitCursor if status == "process" else Qt.CursorShape.PointingHandCursor)
        self.update()

    def _position_for_state(self, checked: bool) -> float:
        margin = float(UI_SWITCH_MARGIN_PX)
        diameter = max(0.0, self.height() - 2.0 * margin)
        return self.width() - margin - diameter if checked else margin

    def _animate_handle(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._handle_position)
        self._animation.setEndValue(self._position_for_state(checked))
        self._animation.start()
        self.update()

    def get_handle_position(self) -> float:
        return self._handle_position

    def set_handle_position(self, position: float) -> None:
        self._handle_position = float(position)
        self.update()

    handle_position = pyqtProperty(float, fget=get_handle_position, fset=set_handle_position)

    def paintEvent(self, event):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.COLORS.get(self._status, UI_COLOR_ERROR))
        if self.underMouse() and self.isEnabled():
            color = color.lighter(112)
        if not self.isEnabled():
            color.setAlpha(110)
        track = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(track, track.height() / 2.0, track.height() / 2.0)
        margin = float(UI_SWITCH_MARGIN_PX)
        d = max(0.0, self.height() - 2.0 * margin)
        p.setPen(QPen(self.HANDLE_BORDER_COLOR, 0.75))
        p.setBrush(self.HANDLE_COLOR)
        p.drawEllipse(QRectF(self._handle_position, margin, d, d))
