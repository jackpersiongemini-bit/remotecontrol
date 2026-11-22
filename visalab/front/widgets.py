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

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem

from ..core.units import si_format, si_parse
from .theme import UI_COLOR_BORDER_LIGHT, UI_COLOR_TEXT, UI_COLOR_TEXT_MUTED

__all__ = ["CommitLineEdit", "UnitCommitLineEdit", "MatrixTable"]


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
