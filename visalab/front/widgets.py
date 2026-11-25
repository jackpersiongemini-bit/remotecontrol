"""Общие переиспользуемые примитивы интерфейса ``visalab.front``.

* ``CommitLineEdit`` — стандартное поле ввода настроек: коммит по Enter и уходу фокуса,
  откат по Esc, подсветка ``[invalid="true"]`` и защита активного ввода от перезаписи.
* ``UnitCommitLineEdit`` — числовое поле с единицей измерения (SI-форматирование/разбор).
* ``MatrixTable`` — базовая таблица-матрица «частота × мощность источника» для ``PowerTable``
  и ``CurrentTable`` с общими заголовками, подсветкой активной ячейки и обновлением темы.
"""
from __future__ import annotations

import math
from typing import Callable

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.units import si_format, si_parse
from .params import IndicatorButton, _repolish, paint_indicator
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_CONTROL, UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL, UI_COLOR_TEXT,
                    UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED, UI_COMPACT_HEIGHT_PX, UI_CONTROL_HEIGHT_PX,
                    UI_FONT_SIZE_PT, UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, UI_TITLE_BAR_ITEM_SIZE_PX,
                    render_qss)
from .window import CustomExpandButton

__all__ = ["CommitLineEdit", "UnitCommitLineEdit", "MatrixTable", "CollapsibleSection", "SincEmblem", "ScenarioChip"]


class CommitLineEdit(QLineEdit):
    """Поле ввода с единым поведением: Enter и уход фокуса применяют, Esc возвращает прежнее значение.

    ``set_committed(text)`` по умолчанию не трогает поле, пока пользователь в нём печатает
    (``hasFocus()`` или ``isModified()``), чтобы внешние обновления не сбрасывали ввод.
    """

    committed = pyqtSignal()

    def __init__(self, tip: str = "", on_commit: Callable[[], None] | None = None, *,
                 placeholder: str = "", object_name: str = "",
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                 parent=None):
        super().__init__(parent)
        self._committed = ""
        self._on_commit = on_commit
        self._escaped = False
        if object_name:
            self.setObjectName(object_name)
        if placeholder:
            self.setPlaceholderText(placeholder)
        if tip:
            self.setToolTip(tip)
        self.setAlignment(align)
        self.returnPressed.connect(self._commit)

    def set_invalid(self, invalid: bool) -> None:
        flag = bool(invalid)
        if bool(self.property("invalid")) != flag:
            self.setProperty("invalid", flag)
            st = self.style()
            if st is not None:
                st.unpolish(self)
                st.polish(self)

    def set_committed(self, text: str, *, invalid: bool = False, force: bool = False) -> None:
        self._committed = text
        self.set_invalid(invalid)
        if not force and (self.hasFocus() or self.isModified()):
            return
        self.blockSignals(True)
        self.setText(text)
        self.setModified(False)
        self.blockSignals(False)

    def _commit(self) -> None:
        self._escaped = False
        self.setModified(False)
        if self._on_commit is not None:
            self._on_commit()
        self.committed.emit()

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self._escaped = True
            self.set_committed(self._committed, force=True)
            self.clearFocus()
            return
        self._escaped = False
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        if self._escaped:
            self._escaped = False
            self.set_committed(self._committed, force=True)
            return
        if self.isModified() or self.text() != self._committed:
            self._commit()
        else:
            self.set_committed(self._committed, force=True)


class UnitCommitLineEdit(CommitLineEdit):
    """Числовое поле порога с единицей измерения: Enter и focusOut применяют, Esc возвращает."""

    focus_committed = pyqtSignal()

    def __init__(self, unit: str, scale: float = 1.0, parent=None):
        self.unit = unit
        self.scale = scale
        self.value = 0.0
        super().__init__(object_name="cfg", parent=parent)
        self.committed.connect(self.focus_committed.emit)

    def show_value(self, v: float, *, force: bool = True) -> None:
        self.value = v
        shown = v * self.scale
        txt = si_format(shown, self.unit, 6) if self.unit == "Hz" else f"{shown:g} {self.unit}"
        self.set_committed(txt, force=force)

    def parsed(self) -> float:
        return si_parse(self.text(), self.unit) / self.scale


class MatrixTable(QTableWidget):
    """Базовая матричная таблица «частота × мощность источника» с обновлением темы и автоскроллом."""

    EMPTY = "NO POINTS YET"
    ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    ALIGN = (ALIGN_LEFT, ALIGN_RIGHT)
    FREQ_TOOLTIP = "Frequency"
    ROW_HEIGHT = 20

    def __init__(self, object_name: str, *, min_height: int = 240, parent=None):
        super().__init__(0, 2, parent)
        self.setObjectName(object_name)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(self.ROW_HEIGHT)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.horizontalHeader()
        hh.setMinimumSectionSize(76)
        hh.setHighlightSections(False)
        self.setMinimumHeight(min_height)

        self.columns: list[float | None] = []
        self.rows: list[tuple[float, dict[float | None, float]]] = []
        self.current: float | None = None
        self.current_power: float | None = None
        self._cur_cell: tuple[int, int] = (-1, -1)
        self._normal_font = QFont(self.font())
        self._bold_font = QFont(self.font())
        self._bold_font.setBold(True)
        self._cur_color = QColor(UI_COLOR_TEXT)
        self._muted_color = QColor(UI_COLOR_TEXT_MUTED)
        self._dim_color = QColor(UI_COLOR_BORDER_LIGHT)

    # ------------------------------------------------------------ хуки подклассов
    def _col_header_text(self, col_p: float | None, has_numeric: bool) -> str:
        raise NotImplementedError

    def _col_header_tooltip(self, col_p: float | None) -> str:
        return ""

    def _format_cell(self, freq: float, col_p: float | None, value: float | None) -> tuple[str, bool]:
        """Вернуть ``(текст, приглушённый)`` для ячейки матрицы."""
        raise NotImplementedError

    # ------------------------------------------------------------ общая логика
    @staticmethod
    def _same_power(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a is b
        return abs(a - b) <= 5e-4

    def _setup_headers(self, columns: list[float | None]) -> None:
        has_num = any(c is not None for c in columns)
        shown_cols = columns if columns else [None]
        labels = ["FREQ", *(self._col_header_text(c, has_num) for c in shown_cols)]
        if self.columnCount() != len(labels):
            self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)
        hh = self.horizontalHeader()
        mode = QHeaderView.ResizeMode.Stretch if len(labels) <= 5 else QHeaderView.ResizeMode.ResizeToContents
        hh.setSectionResizeMode(mode)
        hh.setStretchLastSection(False)
        for col in range(len(labels)):
            item = self.horizontalHeaderItem(col)
            if item is None:
                continue
            item.setTextAlignment(self.ALIGN_LEFT if col == 0 else self.ALIGN_RIGHT)
            item.setToolTip(self.FREQ_TOOLTIP if col == 0 else self._col_header_tooltip(shown_cols[col - 1]))

    def refresh_theme(self) -> None:
        self._cur_color = QColor(UI_COLOR_TEXT)
        self._muted_color = QColor(UI_COLOR_TEXT_MUTED)
        self._dim_color = QColor(UI_COLOR_BORDER_LIGHT)
        self._render(force=True)

    def _style_cell(self, row: int, col: int, active: bool, dim: bool = False) -> None:
        item = self.item(row, col)
        if item is not None:
            item.setFont(self._bold_font if active else self._normal_font)
            item.setForeground(self._cur_color if active else (self._dim_color if dim else self._muted_color))

    def _render(self, force: bool = False) -> None:
        old_cell = self._cur_cell
        self._setup_headers(self.columns)
        shown_cols = self.columns if self.columns else [None]
        if self.rowCount() != len(self.rows):
            self.setRowCount(len(self.rows))
        cur_row, cur_col = -1, -1
        for i, (f, values) in enumerate(self.rows):
            is_cur_row = self.current is not None and abs(f - self.current) < 0.5
            if is_cur_row:
                cur_row = i
            f_item = self.item(i, 0)
            if f_item is None:
                f_item = QTableWidgetItem()
                f_item.setTextAlignment(self.ALIGN_LEFT)
                self.setItem(i, 0, f_item)
            f_txt = si_format(f, "Hz", 9)
            if f_item.text() != f_txt:
                f_item.setText(f_txt)
            self._style_cell(i, 0, is_cur_row)
            for j, col_p in enumerate(shown_cols):
                col = j + 1
                is_cur_cell = is_cur_row and self._same_power(col_p, self.current_power)
                if is_cur_cell:
                    cur_col = col
                item = self.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(self.ALIGN_RIGHT)
                    self.setItem(i, col, item)
                text, dim = self._format_cell(f, col_p, values.get(col_p))
                if item.text() != text:
                    item.setText(text)
                self._style_cell(i, col, is_cur_cell, dim=dim)
        new_cell = (cur_row, cur_col if cur_col >= 0 else 0)
        self._cur_cell = new_cell
        if (force or new_cell != old_cell) and cur_row >= 0:
            target = self.item(cur_row, max(0, cur_col))
            if target is not None:
                self.scrollToItem(target, QAbstractItemView.ScrollHint.EnsureVisible)
        self.viewport().update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if not self.rows:
            p = QPainter(self.viewport())
            p.setPen(QColor(UI_COLOR_BORDER_LIGHT))
            p.drawText(QRectF(self.viewport().rect()), Qt.AlignmentFlag.AlignCenter, self.EMPTY)


class _SectionHeader(QFrame):
    """Кликабельная шапка сворачиваемого подраздела с опциональным индикатором активности."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = False
        self.setObjectName("sectionHeader")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active != self._active:
            self._active = active
            self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(e.position().toPoint())
            while child is not None and child is not self:
                if isinstance(child, QAbstractButton):
                    super().mouseReleaseEvent(e)
                    return
                child = child.parentWidget()
            self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if self._active:
            p = QPainter(self)
            paint_indicator(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class CollapsibleSection(QFrame):
    """Стандартный сворачиваемый подраздел настроек (аккордеон-секция) с сохранением состояния в ``QSettings``."""

    toggled = pyqtSignal(bool)

    STYLE_SHEET = render_qss("""
        CollapsibleSection {
            background-color: $UI_COLOR_PANEL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        CollapsibleSection QFrame#sectionHeader {
            background-color: $UI_COLOR_CONTROL;
            border: none;
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-top-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-top-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        CollapsibleSection QFrame#sectionHeader[expanded="false"] {
            border-bottom: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        CollapsibleSection QFrame#sectionHeader:hover {
            background-color: $UI_COLOR_BORDER;
        }
        CollapsibleSection QLabel#sectionTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            padding: 0 2px;
        }
        CollapsibleSection CustomExpandButton {
            min-width: ${UI_COMPACT_HEIGHT_PX}px; max-width: ${UI_COMPACT_HEIGHT_PX}px;
            min-height: ${UI_COMPACT_HEIGHT_PX}px; max-height: ${UI_COMPACT_HEIGHT_PX}px;
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 0;
        }
        CollapsibleSection CustomExpandButton:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
        }
        CollapsibleSection QWidget#sectionBody {
            background-color: $UI_COLOR_PANEL;
            border: none;
            border-bottom-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-bottom-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
    """)

    def __init__(self, title: str, content: QWidget | None = None, *, expanded: bool = True,
                 settings: QSettings | None = None, settings_key: str = "", parent=None):
        super().__init__(parent)
        self.title = title
        self._settings = settings
        self._settings_key = settings_key
        self.setStyleSheet(self.STYLE_SHEET)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.header = _SectionHeader(self)
        self.header.setFixedHeight(UI_CONTROL_HEIGHT_PX + 6)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(8, 4, 6, 4)
        hl.setSpacing(6)

        self.title_label = QLabel(title, self.header)
        self.title_label.setObjectName("sectionTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.expand_button = CustomExpandButton(self.header)
        self.expand_button.setToolTip(f"Expand / collapse {title}")

        hl.addWidget(self.title_label)
        hl.addStretch(1)
        hl.addWidget(self.expand_button)

        self.body = QWidget(self)
        self.body.setObjectName("sectionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 8, 8, 8)
        self.body_layout.setSpacing(6)
        if content is not None:
            self.body_layout.addWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.header)
        root.addWidget(self.body)

        self.header.clicked.connect(self.toggle)
        self.expand_button.clicked.connect(self.toggle)

        initial = bool(expanded)
        if settings is not None and settings_key and settings.contains(settings_key):
            initial = bool(settings.value(settings_key, initial, type=bool))
        self._expanded = not initial
        self.set_expanded(initial, save=False, emit=False)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_active(self, active: bool) -> None:
        self.header.set_active(active)

    def set_content(self, widget: QWidget) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.body_layout.addWidget(widget)

    def set_expanded(self, on: bool, *, save: bool = True, emit: bool = True) -> None:
        on = bool(on)
        if on == self._expanded:
            return
        self._expanded = on
        self.body.setVisible(on)
        self.expand_button.set_expanded(on)
        self.header.setProperty("expanded", "true" if on else "false")
        _repolish(self.header)
        if save and self._settings is not None and self._settings_key:
            self._settings.setValue(self._settings_key, on)
        if emit:
            self.toggled.emit(on)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)


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
    """Идущие сценарии координатора: имя и прогресс; клик - открыть настройки прибора сценария."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chip")
        self.setProperty("tile", True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(68)
        self.setFixedHeight(UI_TITLE_BAR_ITEM_SIZE_PX)
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
                        + f"\nClick - open {who.split(' + ')[-1]} settings")
        self.update()
