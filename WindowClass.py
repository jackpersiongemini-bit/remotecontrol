from qframelesswindow import FramelessWindow, TitleBar
from PyQt6.QtWidgets import QAbstractButton, QWidget
from PyQt6.QtCore import (
    QEasingCurve,
    QLineF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QPainter, QPen, QColor
import textwrap

from WidgetFabric import Button
from Shared import *

class WindowBar(TitleBar):
    STYLE_SHEET = render_qss("""
        WindowBar,
        WindowBar QLabel {
            background-color: $UI_COLOR_CONTROL;
        }
        WindowBar {
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
        }
        WindowBar QWidget {
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        WindowBar QWidget:hover {
            background-color: $UI_COLOR_TEXT_MUTED;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        WindowBar WriteIndicator,
        WindowBar WriteIndicator:hover {
            background-color: transparent;
            border: none;
        }
        WindowBar IconButtonBase {
            max-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            min-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            background-color: $UI_COLOR_BORDER;
        }
        WindowBar IconButtonBase[status="process"] {
            background-color: $UI_COLOR_PROCESS;
        }
        WindowBar CustomOnButton[status="OFF"] {
            background-color: $UI_COLOR_BORDER;
        }
        WindowBar CustomOnButton[status="ON"] {
            background-color: $UI_COLOR_SUCCESS;
        }
        WindowBar CustomOnButton[status="ON"]:hover {
            background-color: $UI_COLOR_SUCCESS_HOVER;
        }
        WindowBar CustomOnButton[status="error"] {
            background-color: $UI_COLOR_ERROR;
        }
        WindowBar CustomOnButton[status="error"]:hover {
            background-color: $UI_COLOR_ERROR;
        }
        WindowBar CustomCloseButton:hover {
            background-color: $UI_COLOR_ERROR;
        }
        WindowBar ModuleAddressWidget {
            max-width: ${UI_TITLE_BAR_WIDE_ITEM_SIZE_PX}px;
            min-width: ${UI_TITLE_BAR_WIDE_ITEM_SIZE_PX}px;
            max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            background-color: $UI_COLOR_BACKGROUND;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        WindowBar ModuleAddressWidget[status="process"] {
            background-color: $UI_COLOR_PROCESS;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        WindowBar QComboBox {
            max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            padding: 0 10px;
            background-color: $UI_COLOR_BACKGROUND;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            color: $UI_COLOR_TEXT;
        }
        WindowBar QComboBox:hover {
            color: $UI_COLOR_TEXT_INVERSE;
        }
        WindowBar QComboBox::drop-down {
            background: transparent;
            border-top-right-radius: ${UI_BORDER_RADIUS_PX}px;
            border-bottom-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        WindowBar QComboBox::down-arrow {
            image: none;
            width: 10px;
            height: 6px;
        }
        WindowBar QComboBox QAbstractItemView {
            padding: 2px;
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT;
            outline: none;
            selection-background-color: $UI_COLOR_TEXT_MUTED;
            selection-color: $UI_COLOR_TEXT_INVERSE;
        }
        WindowBar QComboBox QAbstractItemView:hover {
            background-color: $UI_COLOR_BACKGROUND;
        }
        WindowBar QComboBox QAbstractItemView::item:selected,
        WindowBar QComboBox QAbstractItemView::item:hover {
            background-color: $UI_COLOR_TEXT_MUTED;
            color: $UI_COLOR_TEXT_INVERSE;
        }
    """)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE_SHEET)
        self.minBtn.hide() # скрываем стандартные кнопки - они не подаются QSS
        self.maxBtn.hide()
        self.closeBtn.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground) # включение отрисовки background
        self.setFixedHeight(WINDOW_BAR_HEIGHT) # высота titlebar
        self.hBoxLayout.setContentsMargins(7,7,7,7) # настраиваем layout
        self.hBoxLayout.setSpacing(5)
        self.setDoubleClickEnabled(False) # Отключаем doubleClicked

        self.close_button = CustomCloseButton()
        self.close_button.clicked.connect(self._close_window)

        self.minimized_button = CustomMinButton()
        self.minimized_button.clicked.connect(self._minimize_window)

        # self.pin_button = CustomUnpinButton()

        # self.hBoxLayout.addWidget(self.pin_button)
        self.hBoxLayout.addWidget(self.minimized_button)
        self.hBoxLayout.addWidget(self.close_button)

    def _close_window(self):
        self.window().close()

    def _minimize_window(self):
        self.window().showMinimized()

    def insert_widget(self, widget, index = 0):
        self.hBoxLayout.insertWidget(index, widget)

    def insert_control_widget(self, widget):
        """Insert a widget immediately before minimize/close controls."""
        index = self.hBoxLayout.indexOf(self.minimized_button)
        self.hBoxLayout.insertWidget(index, widget)


class Window(FramelessWindow):
    STYLE_SHEET = render_qss("""
        QWidget {
            background-color: $UI_COLOR_BACKGROUND;
            font-family: $UI_FONT_FAMILY;
            color: $UI_COLOR_TEXT;
        }
        QToolTip {
            padding: 3px 6px;
            color: $UI_COLOR_TEXT;
            background-color: $UI_COLOR_PANEL;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-size: ${UI_FONT_SIZE_PT}pt;
        }
    """)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._size_settings_key = (
            f"WINDOW_SIZE/"
            f"{type(self).__module__}.{type(self).__qualname__}"
        )
        self.setStyleSheet(self.STYLE_SHEET)
        self.setTitleBar(WindowBar(self))
        self.titleBar.raise_()
        self.recall_size()

    def insert_titlebar_widget(self, widget, index=0):
        self.titleBar.insert_widget(widget, index)

    def insert_titlebar_control_widget(self, widget):
        self.titleBar.insert_control_widget(widget)

    def recall_size(self):
        size = settings.value(
            self._size_settings_key,
            self.size(),
            type=QSize,
        )
        self.resize(size)

    def save_size(self):
        settings.setValue(
            self._size_settings_key,
            self.size(),
        )

    def closeEvent(self, event):
        self.save_size()
        return super().closeEvent(event)


class WriteIndicator(QWidget):
    """Aggregated write-state light with per-control details in its tooltip."""

    STATE_COLORS = {
        WriteState.OFF: QColor(UI_COLOR_BORDER),
        WriteState.PROCESS: QColor(UI_COLOR_PROCESS),
        WriteState.ERROR: QColor(UI_COLOR_ERROR),
        WriteState.VERIFIED: QColor(UI_COLOR_SUCCESS),
    }
    STATE_TEXT = {
        WriteState.OFF: "not written",
        WriteState.PROCESS: "writing",
        WriteState.ERROR: "error",
        WriteState.VERIFIED: "written",
    }
    TOOLTIP_LINE_WIDTH = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = ()
        self._message = ""
        self._active = False
        self._state = WriteState.OFF
        self.setFixedSize(UI_TITLE_BAR_ITEM_SIZE_PX, UI_TITLE_BAR_ITEM_SIZE_PX)
        self.setToolTip("No tracked controls")

    @property
    def state(self):
        return self._state

    @property
    def items(self):
        return self._items

    def set_items(self, items, message="", active=True):
        normalized_items = []
        for item in items:
            if len(item) == 2:
                name, state = item
                detail = ""
            elif len(item) == 3:
                name, state, detail = item
            else:
                raise ValueError("WriteIndicator items must contain 2 or 3 values")
            normalized_items.append(
                (
                    str(name),
                    state if isinstance(state, WriteState) else WriteState(state),
                    str(detail or ""),
                )
            )
        self._items = tuple(normalized_items)
        self._message = str(message or "")
        self._active = bool(active)
        self._state = self._aggregate_state()
        tooltip_lines = []
        for name, state, detail in self._items:
            if state is WriteState.VERIFIED:
                continue
            if state is WriteState.ERROR and detail:
                tooltip_lines.append(f"{name}: {detail}")
            else:
                tooltip_lines.append(f"{name}: {self.STATE_TEXT[state]}")
        if tooltip_lines and self._message:
            tooltip_lines.extend(("", self._message))
        wrapped_lines = []
        for line in tooltip_lines:
            if not line:
                wrapped_lines.append("")
                continue
            wrapped_lines.extend(
                textwrap.wrap(
                    line,
                    width=self.TOOLTIP_LINE_WIDTH,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or (line,)
            )
        self.setToolTip(
            "\n".join(wrapped_lines)
            or "No errors or unwritten data"
        )
        self.update()

    def _aggregate_state(self):
        states = tuple(state for _name, state, _detail in self._items)
        if WriteState.ERROR in states:
            return WriteState.ERROR
        if WriteState.PROCESS in states:
            return WriteState.PROCESS
        if self._active and states and all(
            state is WriteState.VERIFIED for state in states
        ):
            return WriteState.VERIFIED
        return WriteState.OFF

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = min(self.width(), self.height()) - 6
        circle = QRectF(
            (self.width() - diameter) / 2,
            (self.height() - diameter) / 2,
            diameter,
            diameter,
        )
        painter.setPen(QPen(QColor(UI_COLOR_PANEL), 1))
        painter.setBrush(self.STATE_COLORS[self._state])
        painter.drawEllipse(circle)


class StatusIndicator(WriteIndicator):
    """Shows write progress independently from per-control errors."""

    def set_items(self, items, message="", active=True):
        status_items = []
        for item in items:
            state = item[1] if isinstance(item[1], WriteState) else WriteState(item[1])
            if state is not WriteState.ERROR:
                status_items.append(item)
        super().set_items(status_items, "", active)
        if not status_items:
            self.setToolTip("No pending status")


class ErrorIndicator(WriteIndicator):
    """Shows only errors and their diagnostic details."""

    def set_items(self, items, message="", active=True):
        error_items = []
        for item in items:
            state = item[1] if isinstance(item[1], WriteState) else WriteState(item[1])
            if state is WriteState.ERROR:
                error_items.append(item)
        super().set_items(error_items, message if error_items else "", active)
        if not error_items:
            self._state = WriteState.VERIFIED if active else WriteState.OFF
            self.setToolTip("No errors")
            self.update()


class Switch(QAbstractButton):
    """Compact animated ON/OFF switch intended for a window title bar."""

    state_changed = pyqtSignal(str)

    OFF_COLOR = QColor(UI_COLOR_ERROR)
    ON_COLOR = QColor(UI_COLOR_SUCCESS)
    HANDLE_COLOR = QColor(UI_COLOR_CONTROL)
    HANDLE_BORDER_COLOR = QColor(UI_COLOR_BORDER)

    def __init__(self, state=OFF, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(self.sizeHint())

        self._handle_position = self._position_for_state(False)
        self._animation = QPropertyAnimation(self, b"handle_position", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        checked = self._checked_from_value(state)
        self.setChecked(checked)
        self._handle_position = self._position_for_state(checked)

        self.toggled.connect(self._animate_handle)
        self.clicked.connect(self._emit_state_changed)

    @staticmethod
    def _checked_from_value(value):
        if value in ON_EQV:
            return True
        if value in OFF_EQV:
            return False
        raise ValueError(f"Unsupported switch value: {value!r}")

    def sizeHint(self):
        return QSize(
            int(UI_TITLE_BAR_WIDE_ITEM_SIZE_PX * 0.9), 
            int(UI_TITLE_BAR_ITEM_SIZE_PX * 0.9)
            )

    def minimumSizeHint(self):
        return self.sizeHint()

    def value(self):
        return ON if self.isChecked() else OFF

    def state(self):
        return self.value()

    def set_value(self, value):
        self.setChecked(self._checked_from_value(value))

    def _position_for_state(self, checked):
        margin = float(UI_SWITCH_MARGIN_PX)
        diameter = max(0.0, self.height() - 2.0 * margin)
        return self.width() - margin - diameter if checked else margin

    def _emit_state_changed(self, checked):
        self.state_changed.emit(ON if checked else OFF)

    def _animate_handle(self, checked):
        self._animation.stop()
        self._animation.setStartValue(self._handle_position)
        self._animation.setEndValue(self._position_for_state(checked))
        self._animation.start()
        self.update()

    def get_handle_position(self):
        return self._handle_position

    def set_handle_position(self, position):
        self._handle_position = float(position)
        self.update()

    handle_position = pyqtProperty(
        float,
        fget=get_handle_position,
        fset=set_handle_position,
    )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        track_color = QColor(self.ON_COLOR if self.isChecked() else self.OFF_COLOR)
        if self.underMouse() and self.isEnabled():
            track_color = track_color.lighter(112)
        if not self.isEnabled():
            track_color.setAlpha(110)

        track = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, track.height() / 2.0, track.height() / 2.0)

        margin = float(UI_SWITCH_MARGIN_PX)
        diameter = max(0.0, self.height() - 2.0 * margin)
        handle = QRectF(self._handle_position, margin, diameter, diameter)
        painter.setPen(QPen(self.HANDLE_BORDER_COLOR, 0.75))
        painter.setBrush(self.HANDLE_COLOR)
        painter.drawEllipse(handle)


class IconButtonBase(Button):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Цвета иконки задаем здесь
        self.icon_color_normal = QColor(UI_COLOR_TEXT_MUTED) # Исходный цвет
        self.icon_color_hover = QColor(UI_COLOR_TEXT_INVERSE)  # Цвет при наведении
        self._icon_color = self.icon_color_normal
        self._current_icon_color = self.icon_color_normal

    def enterEvent(self, event):
        super().enterEvent(event)
        self._icon_color = self.icon_color_hover
        self.update() # Заставляем иконку перерисоваться при наведении

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._icon_color = self._current_icon_color
        self.update() # Возвращаем цвет обратно

    def set_status(self, status):
        self.setProperty('status', status)
        self.style().unpolish(self)
        self.style().polish(self)
        if status != 'ok':
            self._icon_color = self.icon_color_hover
            self._current_icon_color = self.icon_color_hover
        else:
            self._icon_color = self.icon_color_normal
            self._current_icon_color = self.icon_color_normal
        self.update()

    def status(self):
        return self.property('status')

    def status_process(self):
        self.set_status('process')
        self.setDisabled(True)
        self.setCursor(Qt.CursorShape.WaitCursor)

    def status_ok(self):
        self.set_status('ok')
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    
    def status_error(self):
        self.set_status('error')
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def status_on(self):
        self.set_status(ON)
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def status_off(self):
        self.set_status(OFF)
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class CustomMinButton(IconButtonBase):
    def paintEvent(self, event):
        # Сначала даем Qt отрисовать фон кнопки (и стили из QSS, если есть)
        super().paintEvent(event)
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Настраиваем перо (цвет и толщину линии)
        pen = QPen(self._icon_color)
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH) # <-- Та самая толщина линии
        pen.setCapStyle(Qt.PenCapStyle.RoundCap) # Делает концы линии скругленными
        painter.setPen(pen)
        
        padding = 5 # Отступ от краев кнопки
        # Рисуем минус (горизонтальную линию) по центру
        y = self.height() - padding
        
        painter.drawLine(padding, y, self.width() - padding, y)

class CustomCloseButton(IconButtonBase):
    def paintEvent(self, event):
        super().paintEvent(event)
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Настраиваем перо (цвет и толщину)
        pen = QPen(self._icon_color)
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        
        padding = 5 # Отступ от краев кнопки
        
        # Первая линия: из левого верхнего в правый нижний угол
        painter.drawLine(padding, padding, self.width() - padding, self.height() - padding)
        
        # Вторая линия: из левого нижнего в правый верхний угол
        painter.drawLine(padding, self.height() - padding, self.width() - padding, padding)

class CustomInitButton(IconButtonBase):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        pen = QPen(self._icon_color) # Используем твою переменную для hover
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin) # Делает стыки углов мягкими
        painter.setPen(pen)
        
        # Строгий расчет центра с плавающей точкой
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        
        # База (нижняя горизонтальная черта)
        painter.drawLine(QLineF(cx - 4.5, cy + 4.5, cx + 4.5, cy + 4.5))
        
        # Ствол стрелки (строго по центру)
        painter.drawLine(QLineF(cx, cy - 4.5, cx, cy + 1.5))
        
        # Усики стрелки (направлены вниз, симметричны)
        painter.drawLine(QLineF(cx, cy + 1.5, cx - 3.0, cy - 1.5))
        painter.drawLine(QLineF(cx, cy + 1.5, cx + 3.0, cy - 1.5))

class CustomOnButton(IconButtonBase):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        pen = QPen(self._icon_color)
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r = 4.5 # Симметричный радиус
        
        # Дуга
        rect = QRectF(cx - r, cy - r, r * 2.0, r * 2.0)
        painter.drawArc(rect, 120 * 16, 300 * 16)
        
        # Вертикальная линия (строго по центру)
        painter.drawLine(QLineF(cx, cy - r - 1.0, cx, cy + 0.5))

class CustomSyncButton(IconButtonBase):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        pen = QPen(self._icon_color)
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        
        offset_y = 2.5 # Идеально симметричный сдвиг стрелок от центра
        line_len = 4.5 # Половина длины стрелки
        arr_size = 2.5 # Размер усиков
        
        # Верхняя стрелка (вправо)
        y1 = cy - offset_y
        painter.drawLine(QLineF(cx - line_len, y1, cx + line_len, y1))
        painter.drawLine(QLineF(cx + line_len, y1, cx + line_len - arr_size, y1 - arr_size))
        painter.drawLine(QLineF(cx + line_len, y1, cx + line_len - arr_size, y1 + arr_size))
        
        # Нижняя стрелка (влево)
        y2 = cy + offset_y
        painter.drawLine(QLineF(cx - line_len, y2, cx + line_len, y2))
        painter.drawLine(QLineF(cx - line_len, y2, cx - line_len + arr_size, y2 - arr_size))
        painter.drawLine(QLineF(cx - line_len, y2, cx - line_len + arr_size, y2 + arr_size))
        
class CustomPinButton(IconButtonBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pin_state = False

    """Кнопка закрепления (булавка)"""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        pen = QPen(self._icon_color)
        pen.setWidth(WIDGET_TITLE_BAR_BUTTON_LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        
        # Головка булавки (круг)
        painter.drawEllipse(QRectF(cx - 3.0, cy - 4.0, 6.0, 6.0))
        
        # Иголка
        painter.drawLine(QLineF(cx, cy + 2.0, cx, cy + 6.0))

        if self.pin_state:
            # Диагональное зачеркивание
            painter.drawLine(QLineF(cx - 4.0, cy - 5.0, cx + 4.0, cy + 5.0))

        
