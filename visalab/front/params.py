"""Виджеты параметров в духе Parameter.py Алексея, но поверх прибора координатора (Instrument).

* ``PLineEdit`` - число с единицей (SI-префиксы при вводе и показе), запись по Enter/уходу фокуса;
* ``PSpinBox``  - то же со стрелками: шаг - разряд под курсором (как ручка прибора);
* ``PNLabel``   - то же, только показ;
* ``PSButton``  - кнопка, клик переключает на следующий вариант ({"OFF": False, "ON": True});
* ``PLabel``    - текст / значение прибора;
* ``PBGroup``   - взаимоисключающие кнопки (пресеты);
* ``PanelLine`` / ``PanelTable`` / ``Panel`` - строки «подпись | виджеты» в панели.

Виджет знает только прокси и ключ профиля. Состояние записи (``wstate``: process / error /
verified) окрашивает рамку и собирается ``WriteTracker`` для индикаторов в заголовке окна.
"""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import QEvent, QLocale, QObject, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractSpinBox, QButtonGroup, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QPushButton, QSizePolicy, QTabBar, QToolButton, QToolTip, QVBoxLayout,
                             QWidget)

from ..core.params import ConnectionState, Kind, PStatus
from ..coord import Instrument
from ..core.units import SI_PREFIXES, si_format, si_parse
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_INDICATOR_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER_LIGHT, UI_COLOR_INDICATOR, UI_COLOR_INDICATOR_FRAME, UI_COLOR_INVERT,
                    UI_COLOR_INVERT_HOVER, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_CONTROL_HEIGHT_PX, render_qss, scrollbar_qss)
from .help import param_help
from .window import WriteState

__all__ = ["PLineEdit", "PSpinBox", "StepLineEdit", "PNLabel", "PSButton", "PChoice", "PLabel", "PBGroup", "PTabBar", "PanelLine", "PanelTable", "Panel",
           "WriteTracker"]

_WSTATE = {PStatus.WRITING: "process", PStatus.ERROR: "error", PStatus.INVALID: "error",
           PStatus.WRITTEN: "verified", PStatus.COERCED: "verified", PStatus.UNKNOWN: ""}


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    for child in w.findChildren(QWidget):
        child.style().unpolish(child)
        child.style().polish(child)


class _Bound:
    """Привязка к ключу: значение, статус записи, доступность по состоянию связи."""

    #: как менять значение (``help.HOW``): spin / edit / button / menu / label
    HOW = ""

    def bind(self, proxy: Instrument, key: str) -> None:
        self.proxy, self.key = proxy, key
        self.spec = proxy.params[key]
        self._value: Any = None
        self._status_message = ""
        proxy.subscribe(key, self._on_value, self._on_status)
        proxy.state_changed.connect(lambda _s: (self._update_enabled(), self.refresh_tip()))
        self.refresh_tip()
        self._update_enabled()

    def how(self) -> str:
        return self.HOW

    def help_text(self) -> str:
        """Подсказка: что это и как работает (``help.param_help``), без ошибки записи. Текст, который
        окно задало через ``setToolTip``, заменяет общее описание ключа (диапазон и ключ остаются)."""
        return param_help(self.proxy, self.key, self.how(), text=getattr(self, "_custom_tip", ""),
                          extra=self.help_extra())

    def help_extra(self) -> str:
        return ""

    def refresh_tip(self) -> None:
        if not hasattr(self, "key"):
            return
        msg = getattr(self, "_status_message", "")
        QWidget.setToolTip(self, self.help_text() + (f"\n{msg}" if msg else ""))

    def set_custom_tip(self, text: str) -> None:
        """``setToolTip`` поля прибора: своё описание вместо общего."""
        self._custom_tip = text or ""
        if hasattr(self, "key"):
            self.refresh_tip()
        else:
            QWidget.setToolTip(self, text)

    def _update_enabled(self) -> None:
        self.setEnabled(self.proxy.is_connected and self._value is not None)

    def _on_value(self, value: Any) -> None:
        self._value = value
        self._update_enabled()
        self.show_value(value)

    def _on_status(self, status: PStatus, message: str) -> None:
        self.setProperty("wstate", _WSTATE.get(status, ""))
        _repolish(self)
        self._status_message = message
        self.refresh_tip()

    def show_value(self, value: Any) -> None:  # pragma: no cover
        raise NotImplementedError


#: префиксы, которые предлагает список единиц (порядок - по возрастанию)
UNIT_PREFIXES = {"s": ("n", "µ", "m", ""), "Hz": ("", "k", "M", "G"), "V": ("m", ""), "Vpp": ("m", ""),
                 "A": ("m", ""), "W": ("m", "")}


class PLineEdit(QLineEdit, _Bound):
    """Число слева, единица справа - кнопка со списком префиксов (ns / µs / ms / s).

    Введённое число берётся в выбранной единице; можно ввести и с префиксом («30u», «1.5 kHz»).
    Пока единицу не выбрали вручную, префикс подбирается под значение.
    """

    setToolTip = _Bound.set_custom_tip
    HOW = "edit"

    def __init__(self, proxy: Instrument, key: str, unit: str | None = None, digits: int = 6, parent=None):
        super().__init__(parent)
        self.unit = unit if unit is not None else proxy.params[key].unit
        self.digits = digits
        self.prefixes = UNIT_PREFIXES.get(self.unit, ("",))
        self.prefix = "" if "" in self.prefixes else self.prefixes[-1]
        self.pinned = False                       # единица выбрана вручную
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.unit_button: QPushButton | None = None
        if self.unit:
            b = QPushButton(self.unit, self)
            b.setObjectName("unit")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if len(self.prefixes) > 1:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(self._unit_menu)
                b.setToolTip(f"Unit: click to choose the prefix ({', '.join(p + self.unit for p in self.prefixes)})")
            else:
                b.setProperty("static", True)
                b.setToolTip(f"Unit: {self.unit}")
            self.unit_button = b
        self.bind(proxy, key)
        self.editingFinished.connect(self.change_value)
        self._update_unit()

    @property
    def value(self):
        return self._value

    # -------------------------------------------------------- единица
    def _factor(self) -> float:
        return SI_PREFIXES[self.prefix] if self.prefix else 1.0

    def _auto_prefix(self, value) -> None:
        if self.pinned or not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
            return
        chosen = self.prefixes[0]
        for p in self.prefixes:
            if abs(value) >= (SI_PREFIXES[p] if p else 1.0) * 0.9999999:
                chosen = p
        self.prefix = chosen

    def _update_unit(self) -> None:
        if self.unit_button is None:
            return
        self.unit_button.setText(f"{self.prefix}{self.unit}")
        self._place_unit()

    def _place_unit(self) -> None:
        b = self.unit_button
        if b is None:
            return
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(2, 2, w, max(self.height() - 4, 0))   # единица слева, число справа
        self.setTextMargins(w, 0, 0, 0)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        self._place_unit()

    def _unit_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for p in self.prefixes:
            act = menu.addAction(f"{p}{self.unit}")
            act.setCheckable(True)
            act.setChecked(p == self.prefix)
            act.triggered.connect(lambda _=False, p=p: self.set_prefix(p))
        menu.popup(self.unit_button.mapToGlobal(self.unit_button.rect().bottomLeft()))

    def set_prefix(self, prefix: str) -> None:
        """Выбрать единицу вручную: значение не меняется, меняется только показ."""
        self.prefix, self.pinned = prefix, True
        self._update_unit()
        self.show_value(self._value, force=True)

    # -------------------------------------------------------- значение
    def _editing(self) -> bool:
        return self.hasFocus() and not self.isReadOnly()

    def show_value(self, value, force: bool = False):
        editing = self._editing() and not force
        if value is None:
            text = ""
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and self.unit:
            if not editing:                       # пока пользователь печатает, единицу не трогаем
                self._auto_prefix(value)
                self._update_unit()
            text = f"{value / self._factor():.{self.digits}g}"
        else:
            text = str(value)
        if editing:                               # в режиме ввода текущее значение - в подсказке
            self.setPlaceholderText(text)
            return
        self.setText(text)
        self.setModified(False)

    # -------------------------------------------------------- ввод
    def focusInEvent(self, e):  # noqa: N802 - Qt
        super().focusInEvent(e)
        if not self.isReadOnly():                 # значение уходит в подсказку, печатать можно сразу
            self.setPlaceholderText(self.text())
            self.clear()
            self.setModified(False)

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)                  # editingFinished -> change_value
        self.setPlaceholderText("")
        self.show_value(self._value, force=True)

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:          # отмена правки
            self.clear()
            self.setModified(False)
            self.clearFocus()
            return
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()

    def _parse(self, text: str):
        if self.spec.kind not in (Kind.FLOAT, Kind.INT):
            return text.strip()
        try:
            return float(text.strip().replace(",", ".")) * self._factor()
        except ValueError:
            return si_parse(text, self.unit)        # с префиксом: «30u», «1.5 kHz»

    def change_value(self) -> None:
        if self.isReadOnly() or not self.isModified():
            return
        self.setModified(False)
        try:
            value = self.spec.validate(self._parse(self.text()))
        except ValueError as exc:
            self._on_status(PStatus.INVALID, str(exc))
            self.show_value(self._value)
            return
        self.set_value(value)
        self.show_value(self._value)

    def set_value(self, value: Any) -> bool:
        """Отправить значение в драйвер (проверку, отличается ли оно от текущего, делает драйвер)."""
        self.proxy.set(self.key, value)
        return True


class StepLineEdit(QLineEdit):
    """Поле спинбокса: подчёркивает разряд, который меняют колесо и стрелки."""

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        spin = self.parent()
        idx = spin.step_index() if isinstance(spin, PSpinBox) else -1
        text = self.text()
        if not 0 <= idx < len(text):
            return
        fm = self.fontMetrics()
        cur = self.cursorPosition()
        x0 = self.cursorRect().center().x()          # граница перед символом cur; от неё - к idx
        left = x0 + fm.horizontalAdvance(text[cur:idx]) if idx >= cur else x0 - fm.horizontalAdvance(text[idx:cur])
        width = fm.horizontalAdvance(text[idx])
        y = self.cursorRect().center().y() + fm.ascent() // 2 + 2
        p = QPainter(self)
        pen = QPen(QColor(UI_COLOR_TEXT_MUTED if spin.step_setting is None else UI_COLOR_TEXT))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawLine(QPointF(left + 0.5, y + 0.5), QPointF(left + width - 0.5, y + 0.5))


class PSpinBox(QDoubleSpinBox, _Bound):
    """Число со стрелками / колесом (как ручка прибора); меняется подчёркнутый разряд.

    Шаг (Алексей 2026-11-14): AUTO - последняя показанная цифра («1.25 GHz» - 0.01 GHz; пока крутят,
    шаг не меняется, «1.30» показывается с нулём); в поле с курсором - разряд слева от курсора. Правый
    клик - меню шага: AUTO или постоянный (1 kHz, 10 kHz...), он же подчёркнут и показан с нужными
    знаками. ``step_changed(шаг или None)`` - окно хранит выбор в ini.

    Единица - кнопка слева со списком префиксов (как у PLineEdit). Ввод с клавиатуры пишется
    по Enter / уходу фокуса, шаг стрелкой или колесом - сразу.
    """

    setToolTip = _Bound.set_custom_tip
    HOW = "spin"

    step_changed = pyqtSignal(object)        # шаг в единицах параметра (Hz, dBm...) или None - AUTO

    def __init__(self, proxy: Instrument, key: str, prefixes: tuple[str, ...] | None = None, parent=None):
        #: постоянный шаг в единицах параметра; None - AUTO (последняя цифра)
        self.step_setting: float | None = None
        self._step_lock: float | None = None    # AUTO: шаг (в единицах префикса), пока крутят колесо
        self._dirty = False                     # QAbstractSpinBox::interpretText() сбрасывает isModified() до editingFinished
        super().__init__(parent)
        self.setLineEdit(StepLineEdit(self))
        self.lineEdit().textEdited.connect(self._mark_dirty)
        self.unit = proxy.params[key].unit
        self.prefixes = prefixes or UNIT_PREFIXES.get(self.unit, ("",))
        self.prefix = "" if "" in self.prefixes else self.prefixes[0]
        self.pinned = False
        self.setLocale(QLocale.c())
        self.setKeyboardTracking(False)
        self.setDecimals(9)
        self.setRange(-1e15, 1e15)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.setCorrectionMode(QAbstractSpinBox.CorrectionMode.CorrectToPreviousValue)
        self.unit_button: QPushButton | None = None
        if self.unit:
            b = QPushButton(self.unit, self)
            b.setObjectName("unit")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if len(self.prefixes) > 1:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(self._unit_menu)
                b.setToolTip(f"Unit: click to choose the prefix ({', '.join(p + self.unit for p in self.prefixes)})")
            else:
                b.setProperty("static", True)
                b.setToolTip(f"Unit: {self.unit}")
            self.unit_button = b
        self.bind(proxy, key)
        self.editingFinished.connect(self.commit)
        self._update_unit()

    @property
    def value_si(self):
        return self._value

    # -------------------------------------------------------- единица
    def _factor(self) -> float:
        return SI_PREFIXES[self.prefix] if self.prefix else 1.0

    def _auto_prefix(self, value) -> None:
        if self.pinned or not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
            return
        chosen = self.prefixes[0]
        for p in self.prefixes:
            if abs(value) >= (SI_PREFIXES[p] if p else 1.0) * 0.9999999:
                chosen = p
        self.prefix = chosen

    def _update_unit(self) -> None:
        if self.unit_button is None:
            return
        self.unit_button.setText(f"{self.prefix}{self.unit}")
        self._place_unit()

    def _place_unit(self) -> None:
        b = self.unit_button
        if b is None:
            return
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(2, 2, w, max(self.height() - 4, 0))
        self.lineEdit().setTextMargins(w, 0, 0, 0)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        self._place_unit()

    def _unit_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for p in self.prefixes:
            act = menu.addAction(f"{p}{self.unit}")
            act.setCheckable(True)
            act.setChecked(p == self.prefix)
            act.triggered.connect(lambda _=False, p=p: self.set_prefix(p))
        menu.popup(self.unit_button.mapToGlobal(self.unit_button.rect().bottomLeft()))

    def set_prefix(self, prefix: str) -> None:
        self.prefix = prefix
        self.pinned = True
        self._update_unit()
        self.show_value(self._value, force=True)

    def paintEvent(self, e):  # noqa: N802 - Qt
        """Стрелки рисуются сами: QSS у кнопок спинбокса убирает стандартные."""
        super().paintEvent(e)
        from PyQt6.QtWidgets import QStyle, QStyleOptionSpinBox
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(UI_COLOR_TEXT_MUTED if self.isEnabled() else UI_COLOR_BORDER_LIGHT))
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for sc, up in ((QStyle.SubControl.SC_SpinBoxUp, True), (QStyle.SubControl.SC_SpinBoxDown, False)):
            r = QRectF(self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, opt, sc, self))
            cx, cy, a = r.center().x(), r.center().y() + (1 if up else -1), 3.0
            d = -a / 2 if up else a / 2
            p.drawLine(QPointF(cx - a, cy - d), QPointF(cx, cy + d))
            p.drawLine(QPointF(cx, cy + d), QPointF(cx + a, cy - d))

    # -------------------------------------------------------- текст
    def textFromValue(self, v: float) -> str:  # noqa: N802 - Qt
        text = f"{v:.9f}".rstrip("0").rstrip(".")
        text = "0" if text in ("", "-0") else text
        step = self._shown_step()
        if step is not None and math.isfinite(step) and 0 < step < 1:  # знаков - не меньше, чем у шага
            need = min(9, max(0, round(-math.log10(step))))
            whole, _, frac = text.partition(".")
            if len(frac) < need:
                text = f"{whole}.{frac.ljust(need, '0')}"
        return text

    def valueFromText(self, text: str) -> float:  # noqa: N802 - Qt
        try:
            return float(text.strip().replace(",", "."))
        except ValueError:
            return self.value()

    def _mark_dirty(self, _text: str = "") -> None:
        self._dirty = True

    def show_value(self, value, force: bool = False):
        if not force and self.hasFocus() and (self._dirty or self.lineEdit().isModified()):
            return                                      # пользователь печатает
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not self.hasFocus():
                self._auto_prefix(value)
                self._update_unit()
            self.blockSignals(True)
            self.setValue(value / self._factor())
            self.blockSignals(False)
        else:
            self.lineEdit().setText("")
        self._dirty = False
        self.lineEdit().setModified(False)

    # -------------------------------------------------------- шаг и запись
    @staticmethod
    def last_digit(text: str) -> float:
        """Вес последней цифры: «1.25» - 0.01, «40» - 1, «0» - 1."""
        _whole, dot, frac = text.strip().partition(".")
        return 10.0 ** -len(frac) if dot and frac else 1.0

    def _shown_step(self) -> float | None:
        """Шаг, под который показываются знаки (в единицах префикса): постоянный или AUTO, пока крутят."""
        if self.step_setting:
            return self.step_setting / self._factor()
        return self._step_lock

    def _digit_step(self) -> float:
        """Шаг в единицах префикса: постоянный; в поле с курсором - разряд слева от курсора;
        иначе - последняя показанная цифра (AUTO)."""
        if self.step_setting:
            return self.step_setting / self._factor()
        le = self.lineEdit()
        text = le.text()
        if not self.hasFocus():
            return self._step_lock or self.last_digit(text)
        pos = le.cursorPosition()
        dot = text.find(".")
        dot = len(text) if dot < 0 else dot
        min_pos = 2 if text.startswith("-") and len(text) > 1 else 1
        pos = max(pos, min_pos)
        exp = dot - pos if pos <= dot else dot - pos + 1
        return 10.0 ** exp

    def step_index(self) -> int:
        """Позиция подчёркнутой цифры в тексте (её меняет шаг) или -1."""
        text = self.lineEdit().text()
        if not text or not self.isEnabled():
            return -1
        step = self._digit_step()
        if not math.isfinite(step) or step <= 0:
            return -1
        exp = round(math.log10(step))
        dot = text.find(".")
        dot = len(text) if dot < 0 else dot
        idx = dot - 1 - exp if exp >= 0 else dot - exp
        return idx if 0 <= idx < len(text) and text[idx].isdigit() else -1

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt
        le = self.lineEdit()
        from_end = len(le.text()) - le.cursorPosition()
        step = self._digit_step()
        if self.step_setting is None:
            self._step_lock = step                # пока крутят - тот же разряд («1.29» + 0.01 = «1.30»)
        self.blockSignals(True)
        self.setValue(self.value() + steps * step)
        self.blockSignals(False)
        le.setCursorPosition(max(len(le.text()) - from_end, 0))
        self.commit(force=True)

    def _release_step(self) -> None:
        """Кончили крутить (мышь ушла, фокус ушёл, печатают): AUTO снова по последней цифре."""
        if self._step_lock is not None:
            self._step_lock = None
            if not (self.hasFocus() and (self._dirty or self.lineEdit().isModified())):
                self.show_value(self._value, force=True)
        self.lineEdit().update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        super().leaveEvent(e)
        if not self.hasFocus():
            self._release_step()

    def focusOutEvent(self, e):  # noqa: N802 - Qt
        super().focusOutEvent(e)
        self._release_step()

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        if e.key() == Qt.Key.Key_Escape:
            self._dirty = False
            self.show_value(self._value, force=True)
            self.clearFocus()
            return
        super().keyPressEvent(e)
        self.lineEdit().update()                  # курсор сдвинулся - подчёркнут другой разряд

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        super().mouseReleaseEvent(e)
        self.lineEdit().update()

    # -------------------------------------------------------- настройка шага
    def step_choices(self) -> list[float]:
        """Постоянные шаги для меню (в единицах параметра): 1 / 10 / 100 каждого префикса."""
        factors = sorted({SI_PREFIXES[p] if p else 1.0 for p in self.prefixes})
        if len(factors) == 1:                     # без префиксов (dBm, %): от 0.001 до 10
            return [factors[0] * m for m in (0.001, 0.01, 0.1, 1.0, 10.0)]
        return [f * m for f in factors for m in (1.0, 10.0, 100.0)][:-2]

    def step_label(self, step: float | None) -> str:
        if step is None:
            return "AUTO"
        return si_format(step, self.unit, 4) if self.unit else f"{step:g}"

    def help_extra(self) -> str:
        step = self.step_label(self.step_setting)
        return f"STEP {step}" + (" (the last shown digit)" if self.step_setting is None else "")

    def set_step(self, step: float | None, emit: bool = True) -> None:
        """Постоянный шаг (единицы параметра) или None - AUTO."""
        if step is not None:
            try:
                f = float(step)
                step = f if (math.isfinite(f) and f > 0) else None
            except (TypeError, ValueError):
                step = None
        changed = step != self.step_setting
        self.step_setting = step
        self._step_lock = None
        self.show_value(self._value, force=True)
        self.lineEdit().update()
        self.refresh_tip()
        if changed and emit:
            self.step_changed.emit(step)

    def contextMenuEvent(self, e):  # noqa: N802 - Qt
        """Правый клик - шаг колеса и стрелок."""
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        title = menu.addAction("STEP")
        title.setEnabled(False)
        for step in (None, *self.step_choices()):
            act = menu.addAction(self.step_label(step) + ("  (last digit)" if step is None else ""))
            act.setCheckable(True)
            act.setChecked(step == self.step_setting or (step is None and self.step_setting is None))
            act.triggered.connect(lambda _=False, st=step: self.set_step(st))
        menu.popup(e.globalPos())
        e.accept()

    def commit(self, force: bool = False) -> None:
        if not force and not (self._dirty or self.lineEdit().isModified()):
            return
        self._dirty = False
        self.lineEdit().setModified(False)
        value = self.value() * self._factor()
        try:
            value = self.spec.validate(value)
        except ValueError as exc:
            self._on_status(PStatus.INVALID, str(exc))
            self.show_value(self._value, force=True)
            return
        self.set_value(value)

    def set_value(self, value: Any) -> bool:
        self.proxy.set(self.key, value)
        return True


class PNLabel(PLineEdit):
    """Число только для показа (DUTY)."""

    HOW = "label"

    def __init__(self, proxy: Instrument, key: str, unit: str | None = None, digits: int = 6, parent=None):
        super().__init__(proxy, key, unit, digits, parent)
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


def paint_indicator(p: QPainter, rect: QRectF, bar: str = UI_COLOR_INDICATOR) -> None:
    """Индикатор «включено/выбрано»: тонкая рамка и толстая нижняя граница по скруглению.

    Рисуется вручную: QSS с разной толщиной границ даёт кривые углы на скруглениях.
    """
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(UI_COLOR_INDICATOR_FRAME), UI_BORDER_WIDTH_PX))
    p.drawRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
    clip = QPainterPath()
    clip.addRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
    p.setClipPath(clip)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(bar))
    p.drawRect(QRectF(rect.left(), rect.bottom() - UI_INDICATOR_WIDTH_PX, rect.width(), UI_INDICATOR_WIDTH_PX))
    p.restore()


class IndicatorButton(QPushButton):
    """QPushButton с индикатором, когда он «включён» (``indicated()``; по умолчанию - isChecked)."""

    def indicated(self) -> bool:
        return self.isChecked()

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if self.indicated():
            p = QPainter(self)
            paint_indicator(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class PSButton(IndicatorButton, _Bound):
    """Кнопка-переключатель: клик - следующий вариант. ``states`` - {подпись: значение}.

    Подпись показывает только значение прибора (``NKNW`` - пока не известно).
    """

    setToolTip = _Bound.set_custom_tip
    UNKNOWN = "NKNW"

    def how(self) -> str:
        return "menu" if len(self._data) > 2 else "button"

    def indicated(self) -> bool:
        return self.property("state") in ("true", "on")

    def __init__(self, proxy: Instrument, key: str, states, parent=None):
        super().__init__(self.UNKNOWN, parent)
        pairs = list(states.items()) if isinstance(states, dict) else list(states)   # или [(подпись, значение)]
        if not pairs:
            raise ValueError("PSButton requires at least one state")
        self._names = tuple(n for n, _v in pairs)
        self._data = tuple(v for _n, v in pairs)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bind(proxy, key)
        self.clicked.connect(self.next_value)

    @property
    def value(self):
        return self._value

    @staticmethod
    def _norm(value):
        return str(value).upper() if isinstance(value, str) else value

    def _index(self, value) -> int:
        for i, data in enumerate(self._data):
            if value is not None and self._norm(data) == self._norm(value):
                return i
        return -1

    def show_value(self, value):
        i = self._index(value)
        self.setText(self._names[i] if i >= 0 else self.UNKNOWN)
        self.setProperty("state", str(self._data[i]).lower() if i >= 0 else "")
        _repolish(self)

    def next_value(self) -> None:
        """2 варианта - переключение по клику; больше - выпадающий список под кнопкой."""
        if len(self._data) > 2:
            menu = QMenu(self)
            menu.setObjectName("psmenu")
            current = self._index(self._value)
            for i, name in enumerate(self._names):
                act = menu.addAction(name)
                act.setCheckable(True)
                act.setChecked(i == current)
                act.triggered.connect(lambda _=False, v=self._data[i]: self.set_value(v))
            menu.setMinimumWidth(self.width())
            menu.popup(self.mapToGlobal(self.rect().bottomLeft()))
            return
        i = self._index(self._value)
        self.set_value(self._data[(i + 1) % len(self._data)], force=len(self._data) == 1)

    def set_value(self, value, force: bool = False) -> bool:
        self.proxy.set(self.key, value)
        return True


class PChoice(QPushButton):
    """Выбор из списка без привязки к прибору (настройки окна): клик - меню под кнопкой."""

    changed = pyqtSignal(object)

    def __init__(self, choices, value=None, parent=None):
        super().__init__(parent)
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._menu)
        self.set_choices(choices, value)

    def set_choices(self, choices, value=None) -> None:
        pairs = list(choices.items()) if isinstance(choices, dict) else list(choices)   # [(подпись, значение)]
        self._names = tuple(n for n, _v in pairs)
        self._data = tuple(v for _n, v in pairs)
        self.set_value(value if value in self._data else (self._data[0] if self._data else None))

    @property
    def value(self):
        return self._value

    def set_value(self, value) -> None:
        self._value = value
        i = self._data.index(value) if value in self._data else -1
        self.setText(self._names[i] if i >= 0 else "")
        self.changed.emit(value)

    def _menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for name, v in zip(self._names, self._data):
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(v == self._value)
            act.triggered.connect(lambda _=False, v=v: self.set_value(v))
        menu.setMinimumWidth(self.width())
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))


class PLabel(QLabel, _Bound):
    """Показ значения прибора (или просто текст, если ``proxy`` не задан)."""

    setToolTip = _Bound.set_custom_tip
    HOW = "label"

    def __init__(self, proxy: Instrument | None = None, key: str = "", unit: str | None = None,
                 digits: int = 6, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if proxy is not None:
            self.unit = unit if unit is not None else proxy.params[key].unit
            self.digits = digits
            self.bind(proxy, key)

    def _update_enabled(self) -> None:
        pass

    def show_value(self, value):
        if value is None:
            self.setText("")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self.setText(si_format(value, self.unit, self.digits))
        else:
            self.setText(str(value))


class PBGroup(QWidget):
    """Группа взаимоисключающих кнопок (без привязки к прибору - для пресетов и т.п.)."""

    selected = pyqtSignal(int)

    def __init__(self, names: list[str] | tuple[str, ...] = (), columns: int = 0, parent=None):
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.columns = columns                   # 0 - в одну строку, иначе сетка
        self.group_layout = QGridLayout(self)
        self.group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_layout.setSpacing(4)
        self._buttons: list[QPushButton] = []
        for name in names:
            self.add_button(name)
        self._group.idClicked.connect(self.selected)

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def _relayout(self) -> None:
        for b in self._buttons:
            self.group_layout.removeWidget(b)
        for i, b in enumerate(self._buttons):
            r, c = divmod(i, self.columns) if self.columns else (0, i)
            self.group_layout.addWidget(b, r, c)

    def _make_button(self, name: str) -> QPushButton:
        return QPushButton(name)

    def add_button(self, name: str) -> QPushButton:
        b = self._make_button(name)            # выбор (не включение) - инверсия, как у вкладки
        b.setObjectName("chip")
        b.setCheckable(True)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._group.addButton(b, len(self._buttons))
        self._buttons.append(b)
        self._relayout()
        return b

    def remove_button(self, index: int) -> None:
        if not 0 <= index < len(self._buttons):
            return
        b = self._buttons.pop(index)
        self._group.removeButton(b)
        for i, other in enumerate(self._buttons):
            self._group.setId(other, i)
        self.group_layout.removeWidget(b)
        b.deleteLater()
        self._relayout()

    def checked_index(self) -> int:
        return self._group.checkedId()

    def set_checked(self, index: int) -> None:
        if index < 0:
            self.clear()
        elif self._group.button(index):
            self._group.button(index).setChecked(True)

    def clear(self) -> None:
        self._group.setExclusive(False)
        for b in self._group.buttons():
            b.setChecked(False)
        self._group.setExclusive(True)


class PTabBar(QTabBar):
    """Вкладки (QTabBar) в стиле панели: выбранная - фон поля, отмеченная ``set_on`` - инверсия.

    QSS не умеет красить отдельные вкладки по своему признаку, поэтому вкладки рисуются здесь.
    Вкладки не помещаются - полоса прокручивается колесом мыши (и наклоном колеса) или кнопками мыши
    «назад» / «вперёд»; стрелок нет (Алексей). Выбранная вкладка при прокрутке не меняется.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(True)
        self.setDrawBase(False)
        self.setDocumentMode(True)
        # прокрутку полосы делает сам QTabBar (его кнопки-стрелки), сами стрелки скрыты: tabLayoutChange
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on: set[int] = set()
        self._hover = -1
        self._wheel = 0                   # накопленный поворот колеса (сенсорные колёса дают доли шага)
        self.show_selection = True        # False - полоса только как индикаторы (свёрнутое окно)

    # ------------------------------------------------------------ прокрутка без стрелок
    def tabLayoutChange(self) -> None:  # noqa: N802 - Qt
        super().tabLayoutChange()
        for b in self.findChildren(QToolButton):     # QTabBar показывает стрелки при каждой раскладке
            b.hide()

    def scroll_tabs(self, steps: int) -> bool:
        """Сдвинуть полосу на ``steps`` вкладок (минус - влево). False - сдвигать некуда."""
        name = "ScrollLeftButton" if steps < 0 else "ScrollRightButton"
        button = self.findChild(QToolButton, name)
        moved = False
        for _ in range(abs(steps)):
            if button is None or not button.isEnabled():
                break
            button.click()                # QTabBar сдвигает полосу к следующей скрытой вкладке
            moved = True
        return moved

    def wheelEvent(self, e):  # noqa: N802 - Qt
        d = e.angleDelta()
        self._wheel += d.x() if abs(d.x()) > abs(d.y()) else d.y()
        steps = int(self._wheel / 120)    # шаг колеса - 120 (1/8 градуса)
        if not steps:
            e.accept()
            return
        self._wheel -= steps * 120
        if self.scroll_tabs(-steps):      # от себя / влево - к началу полосы
            e.accept()
        else:
            self._wheel = 0
            e.ignore()                    # полоса и так видна целиком - колесо достаётся окну

    def mousePressEvent(self, e):  # noqa: N802 - Qt
        if e.button() in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
            self.scroll_tabs(-1 if e.button() == Qt.MouseButton.BackButton else 1)
            e.accept()
            return
        super().mousePressEvent(e)

    def set_on(self, index: int, on: bool) -> None:
        if on != (index in self._on):
            (self._on.add if on else self._on.discard)(index)
            self.update()

    def is_on(self, index: int) -> bool:
        return index in self._on

    def tabSizeHint(self, index):  # noqa: N802 - Qt
        hint = super().tabSizeHint(index)
        return QSize(max(hint.width(), 56), UI_CONTROL_HEIGHT_PX)

    def minimumTabSizeHint(self, index):  # noqa: N802 - Qt
        return self.tabSizeHint(index)

    def mouseMoveEvent(self, e):  # noqa: N802 - Qt
        super().mouseMoveEvent(e)
        hover = self.tabAt(e.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, e):  # noqa: N802 - Qt
        super().leaveEvent(e)
        self._hover = -1
        self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(self.count()):
            rect = QRectF(self.tabRect(i)).adjusted(2.5, 0.5, -2.5, -0.5)
            selected = self.show_selection and i == self.currentIndex()
            on, hover = i in self._on, i == self._hover
            # фон: наведение и выбранная вкладка - инверсия
            bg = UI_COLOR_INVERT_HOVER if hover else UI_COLOR_INVERT if selected else None
            if bg:
                p.setBrush(QColor(bg))
                p.drawRoundedRect(rect, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)
            if on and not selected:       # режим включён (у выбранной видно по STAT): рамка + толстая граница
                paint_indicator(p, rect, UI_COLOR_TEXT_INVERSE if hover else UI_COLOR_INDICATOR)
            fg = UI_COLOR_TEXT_INVERSE if (hover or selected) else \
                UI_COLOR_TEXT_MUTED if on else UI_COLOR_BORDER_LIGHT
            font = QFont(self.font())
            font.setBold(selected)
            p.setFont(font)
            p.setPen(QColor(fg))
            p.drawText(rect.adjusted(0, 0, 0, -1 if on else 0), Qt.AlignmentFlag.AlignCenter, self.tabText(i))
            p.setPen(Qt.PenStyle.NoPen)


class _LineName(QLabel):
    """Подпись строки. Подсказка - своя (``PanelLine.set_help``) или первого поля строки, у которого
    она есть: наводят на имя параметра, а объясняет его поле (что это, диапазон, ключ)."""

    def __init__(self, text: str, line: "PanelLine"):
        super().__init__(text)
        self._line = line

    def tip(self) -> str:
        if self.toolTip():
            return self.toolTip()
        for w in self._line.widgets:
            text = w.help_text() if isinstance(w, _Bound) and hasattr(w, "key") else w.toolTip()
            if text:
                return text
        return ""

    def event(self, e):  # noqa: D401 - Qt
        if e.type() == QEvent.Type.ToolTip and not self.toolTip():
            text = self.tip()
            if text:
                QToolTip.showText(e.globalPos(), text, self)
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)


class PanelLine(QWidget):
    """Строка панели: подпись слева, дальше виджеты (по одному на столбец)."""

    def __init__(self, name: str, *widgets: QWidget, parent=None):
        super().__init__(parent)
        self.widgets: list[QWidget] = []
        self._name = _LineName(name, self)
        self._name.setObjectName("lineName")
        self._name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.line_layout = QHBoxLayout(self)
        self.line_layout.setContentsMargins(0, 0, 0, 0)
        self.line_layout.setSpacing(6)
        self.line_layout.addWidget(self._name)
        self.add_widget(*widgets)

    def add_widget(self, *widgets: QWidget) -> None:
        for w in widgets:
            self.widgets.append(w)
            self.line_layout.addWidget(w)

    @property
    def name(self) -> str:
        return self._name.text()

    def set_help(self, text: str) -> "PanelLine":
        """Своя подсказка подписи (для строк без поля прибора)."""
        self._name.setToolTip(text)
        return self

    def help(self) -> str:
        """Подсказка подписи строки (своя или первого поля)."""
        return self._name.tip()

    def set_label_width(self, width: int) -> None:
        self._name.setFixedWidth(width)

    def label_hint(self) -> int:
        self._name.ensurePolished()
        return self._name.sizeHint().width()


class PanelTable(QWidget):
    """Строки панели одна под другой; строка - один ``PanelLine`` или группа из 2-3 ``PanelLine`` в ряд."""

    ROW_SPACING = 10

    def __init__(self, *items: PanelLine | tuple[PanelLine, ...] | list[PanelLine], parent=None):
        super().__init__(parent)
        self.lines: list[PanelLine] = []
        self._rows: list[tuple[PanelLine, ...]] = []
        self.table_layout = QVBoxLayout(self)
        self.table_layout.setContentsMargins(0, 0, 0, 0)
        self.table_layout.setSpacing(4)
        self.table_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.add_line(*items)

    def add_line(self, *items: PanelLine | tuple[PanelLine, ...] | list[PanelLine]) -> None:
        for item in items:
            if isinstance(item, (tuple, list)):
                group = tuple(ln for ln in item if ln is not None)
                if not group:
                    continue
                if len(group) == 1:
                    self._append_single(group[0])
                else:
                    self._append_group(group)
            else:
                self._append_single(item)

    def _append_single(self, line: PanelLine) -> None:
        self.lines.append(line)
        self._rows.append((line,))
        self.table_layout.addWidget(line)

    @staticmethod
    def _is_fixed_width(w: QWidget) -> bool:
        return (w.minimumWidth() == w.maximumWidth() and w.minimumWidth() > 0) or w.objectName() == "tool"

    def _append_group(self, group: tuple[PanelLine, ...]) -> None:
        box = QWidget(self)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(self.ROW_SPACING)
        flexes = [sum(1 for w in ln.widgets if not self._is_fixed_width(w)) for ln in group]
        any_flex = any(flexes)
        for line, flex in zip(group, flexes):
            self.lines.append(line)
            lay.addWidget(line, flex if any_flex else 1)
        self._rows.append(group)
        self.table_layout.addWidget(box)

    def first_column_lines(self) -> list[PanelLine]:
        """Левый столбец каждой строки (для выравнивания подписей между вкладками)."""
        src = [row[0] for row in self._rows] if self._rows else list(self.lines)
        return [ln for ln in src if not ln._name.isHidden()]

    def build(self, column_width: int | None = None) -> None:
        """Вызывать после применения стиля (размеры зависят от шрифта)."""
        if not self._rows:
            return
        col_widths: dict[int, int] = {}
        for row in self._rows:
            for col, line in enumerate(row):
                if not line._name.isHidden():
                    col_widths[col] = max(col_widths.get(col, 0), line.label_hint())
        for row in self._rows:
            multi = len(row) > 1
            flex_total = sum(
                sum(1 for w in ln.widgets if not self._is_fixed_width(w))
                for ln in row
            )
            if column_width:
                min_w = min(column_width, 60) if (multi and flex_total >= 3) else column_width
            else:
                min_w = None
            for col, line in enumerate(row):
                if not line._name.isHidden() and col in col_widths:
                    line.set_label_width(col_widths[col])
                for w in line.widgets:
                    if self._is_fixed_width(w):
                        continue
                    if min_w:
                        w.setMinimumWidth(min_w)
                    if multi:
                        w.setSizePolicy(QSizePolicy.Policy.Ignored, w.sizePolicy().verticalPolicy())
                        line.line_layout.setStretchFactor(w, 1)


class Panel(QFrame):
    STYLE_SHEET = render_qss("""
        Panel, Panel QWidget {
            border: none;
            background-color: $UI_COLOR_PANEL;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            color: $UI_COLOR_TEXT_MUTED;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        Panel QLabel#lineName { padding-right: 6px; }
        Panel QLabel#heading { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; color: $UI_COLOR_TEXT; }
        Panel PLineEdit, Panel PSpinBox, Panel QLineEdit#cfg, Panel PSButton, Panel QPushButton#choice,
        Panel QPushButton#chip, Panel QPushButton#tool {
            background-color: $UI_COLOR_BACKGROUND;
            min-height: ${UI_CONTROL_HEIGHT_PX}px; max-height: ${UI_CONTROL_HEIGHT_PX}px;
            padding: 0 6px;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND;
        }
        Panel PLineEdit:focus, Panel PSpinBox:focus { border-color: $UI_COLOR_TEXT_MUTED; }
        Panel PSpinBox { padding: 0 0 0 0; }
        Panel CustomLockButton {
            min-width: ${UI_CONTROL_HEIGHT_PX}px; max-width: ${UI_CONTROL_HEIGHT_PX}px;
            min-height: ${UI_CONTROL_HEIGHT_PX}px; max-height: ${UI_CONTROL_HEIGHT_PX}px;
            background-color: $UI_COLOR_BACKGROUND; border: none; border-radius: ${UI_BORDER_RADIUS_PX}px; padding: 0;
        }
        Panel CustomLockButton:hover { background-color: $UI_COLOR_INVERT_HOVER; }
        Panel CustomLockButton:checked { background-color: $UI_COLOR_INVERT; }
        Panel QCheckBox { spacing: 0; padding: 0; color: $UI_COLOR_TEXT_MUTED; }
        Panel QCheckBox::indicator {
            width: 14px; height: 14px; border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER_LIGHT;
            border-radius: 3px; background: $UI_COLOR_BACKGROUND;
        }
        Panel QCheckBox::indicator:hover { border-color: $UI_COLOR_TEXT; }
        Panel QCheckBox::indicator:checked { background: $UI_COLOR_INVERT; border-color: $UI_COLOR_INVERT; }
        Panel PSpinBox::up-button, Panel PSpinBox::down-button {
            subcontrol-origin: border; width: 16px; border: none; background: transparent;
        }
        Panel PSpinBox::up-button { subcontrol-position: top right; }
        Panel PSpinBox::down-button { subcontrol-position: bottom right; }
        Panel PSpinBox::up-button:hover, Panel PSpinBox::down-button:hover { background: $UI_COLOR_INVERT_HOVER; }
        Panel PLineEdit[readOnly="true"], Panel PLabel {
            background-color: transparent; border-color: transparent;
        }
        Panel PSButton[state="true"], Panel PSButton[state="on"] {
            background-color: $UI_COLOR_BACKGROUND;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT;
        }
        Panel PSButton:hover, Panel QPushButton#choice:hover, Panel QPushButton#chip:hover, Panel QPushButton#tool:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel PSButton:disabled, Panel PLineEdit:disabled, Panel QPushButton#chip:disabled { color: $UI_COLOR_BORDER; }
        Panel QPushButton#chip { padding: 2px 8px; min-height: 0; max-height: 1000px; }
        Panel QPushButton#chip[tight="true"] { padding: 2px 4px; }
        Panel QPushButton#chip:checked {
            background-color: $UI_COLOR_INVERT;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel QPushButton#chip:checked:hover, Panel PSButton[state="true"]:hover, Panel PSButton[state="on"]:hover {
            background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel QPushButton#tool {
            min-width: ${UI_CONTROL_HEIGHT_PX}px; max-width: ${UI_CONTROL_HEIGHT_PX}px; padding: 0;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        Panel QPushButton#tool[half="true"] { min-height: 0; max-height: 1000px; }
        QMenu#psmenu { border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER; padding: 4px; }
        QMenu#psmenu::item { padding: 4px 12px; border-radius: ${UI_BORDER_RADIUS_PX}px; }
        QMenu#psmenu::item:selected { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        QMenu#psmenu::item:checked { font-weight: $UI_FONT_WEIGHT_SEMIBOLD; }
        QMenu#psmenu::indicator { width: 0; }
        Panel PTabBar { background: transparent; }
        Panel QPushButton#unit {
            background-color: transparent; border: none; padding: 0 4px; min-height: 0;
            color: $UI_COLOR_BORDER_LIGHT; font-weight: normal;
        }
        Panel QPushButton#unit:hover { background-color: $UI_COLOR_INVERT_HOVER; color: $UI_COLOR_TEXT_INVERSE; }
        Panel QPushButton#unit[static="true"]:hover { background-color: transparent; color: $UI_COLOR_BORDER_LIGHT; }
        Panel QPlainTextEdit#errorLog {
            background-color: $UI_COLOR_BACKGROUND; color: $UI_COLOR_TEXT;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND; border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 2px 4px;
        }
        Panel QPushButton#tool[wide="true"] { min-width: ${UI_COLUMN_WIDTH_PX}px; max-width: ${UI_COLUMN_WIDTH_PX}px; }
        Panel [wstate="process"] { border-color: $UI_COLOR_PROCESS; }
        Panel [wstate="error"] { border-color: $UI_COLOR_ERROR; }
        Panel QPushButton#chip[wstate="error"] { color: $UI_COLOR_ERROR; }
        Panel QTableWidget#pmTable, Panel QTableWidget#pmTable QWidget { background-color: $UI_COLOR_BACKGROUND; }
        Panel QTableWidget#pmTable { color: $UI_COLOR_TEXT_MUTED; padding: 2px 4px; }
        Panel QTableWidget#pmTable::item { border: none; padding: 0 8px; }
        Panel QTableWidget#pmTable QHeaderView::section {
            background-color: $UI_COLOR_BACKGROUND; color: $UI_COLOR_BORDER_LIGHT; border: none;
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER; padding: 3px 8px; font-weight: normal;
        }
        Panel QTableWidget#pmTable QTableCornerButton::section { background: transparent; border: none; }
    """) + scrollbar_qss("Panel ")

    def __init__(self, table: QWidget, column_width: int | None = None, parent=None):
        """``table`` - PanelTable (будет выровнена) или любой виджет со своими таблицами."""
        super().__init__(parent)
        self.setStyleSheet(self.STYLE_SHEET)
        self.table = table
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(0)
        lay.addWidget(table)
        if isinstance(table, PanelTable):
            table.build(column_width)


class WriteTracker(QObject):
    """Собирает статусы записи выбранных ключей для индикаторов в заголовке."""

    changed = pyqtSignal(list, bool)          # [(name, WriteState, detail)], active

    def __init__(self, proxy: Instrument, keys: list[str], names: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.keys = list(keys)
        self.names = names or {}
        self._state: dict[str, tuple[WriteState, str]] = {}
        proxy.param_status.connect(self._on_status)
        proxy.state_changed.connect(self._on_conn)
        proxy.comm_error.connect(lambda m: self._set("connection", WriteState.ERROR, m))

    def _set(self, key, state, detail=""):
        self._state[key] = (state, detail)
        self.emit()

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if key not in self.keys:
            return
        state = {PStatus.WRITING: WriteState.PROCESS, PStatus.ERROR: WriteState.ERROR,
                 PStatus.INVALID: WriteState.ERROR}.get(status, WriteState.VERIFIED)
        if status is PStatus.UNKNOWN:
            self._state.pop(key, None)
            self.emit()
            return
        self._set(key, state, message)

    def _on_conn(self, state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            self._state.pop("connection", None)
        elif state is ConnectionState.DISCONNECTED:
            self._state.clear()
        self.emit()

    def note_error(self, key: str, detail: str) -> None:
        """Ошибка не по ключу окна (ошибка прибора из опроса, EQPHASE): индикатор горит до CLEAR."""
        self._set(key, WriteState.ERROR, detail)

    def clear_errors(self) -> None:
        """CLEAR в журнале: ошибки сброшены, индикатор гаснет до следующей."""
        self._state = {k: v for k, v in self._state.items() if v[0] is not WriteState.ERROR}
        self.emit()

    def emit(self) -> None:
        items = [(self.names.get(k, k), s, d) for k, (s, d) in self._state.items()]
        self.changed.emit(items, self.proxy.is_connected)
