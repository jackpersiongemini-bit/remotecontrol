"""Окна на qframelesswindow (PyQt6-Frameless-Window).

* ``FramelessFrame``  - базовое окно без системной рамки: свой заголовок
  (индикатор, название, подзаголовок, место под виджеты, кнопки окна), тема,
  строка состояния снизу;
* ``InstrumentWindow`` - окно прибора, две версии в одном окне:
  полная (верхняя панель + все настройки + таблица выбранных)
  и компактная (верхняя панель + таблица выбранных);
* ``MacroWindow``     - окно макросов (пока заготовка).
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QCoreApplication, QSettings, QSize, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QPushButton, QSplitter, QVBoxLayout,
                             QWidget)
from qframelesswindow import FramelessWindow, TitleBar

from .model import InstrumentModel
from .panels import ActionBar, ModeSwitch, ParamTable, SettingsPanel, StatusLed
from .theme import STYLE

__all__ = ["FramelessFrame", "InstrumentWindow", "MacroWindow"]


class FrameTitleBar(TitleBar):
    """Заголовок: [LED] Название  подзаголовок ..... [свои виджеты] [_ □ ×]."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(34)
        self.led = StatusLed(self)
        self.title = QLabel(self)
        self.title.setObjectName("titleText")
        self.sub = QLabel(self)
        self.sub.setObjectName("titleSub")
        lay = self.hBoxLayout
        lay.insertSpacing(0, 10)
        lay.insertWidget(1, self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.insertWidget(2, self.title, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.insertWidget(3, self.sub, 0, Qt.AlignmentFlag.AlignVCenter)
        self._extra_at = 5          # после stretch (индекс 4)
        for b in (self.minBtn, self.maxBtn, self.closeBtn):
            b.setNormalColor(QColor("#9aa4af"))
            b.setHoverColor(QColor("#ffffff"))
            b.setHoverBackgroundColor(QColor(255, 255, 255, 26))
            b.setPressedBackgroundColor(QColor(255, 255, 255, 50))
        self.closeBtn.setHoverBackgroundColor(QColor("#c42b1c"))
        parent.windowTitleChanged.connect(self.title.setText)

    def add_widget(self, w: QWidget) -> None:
        self.hBoxLayout.insertWidget(self._extra_at, w, 0, Qt.AlignmentFlag.AlignVCenter)
        self._extra_at += 1
        self.hBoxLayout.insertSpacing(self._extra_at, 10)
        self._extra_at += 1


class FramelessFrame(FramelessWindow):
    """Окно без системной рамки. Содержимое кладётся в ``self.body`` (QVBoxLayout)."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setTitleBar(FrameTitleBar(self))
        self.setObjectName("frameRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, self.titleBar.height(), 1, 1)
        outer.setSpacing(0)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        outer.addLayout(self.body, 1)
        self.status_line = QLabel("")
        self.status_line.setObjectName("statusLine")
        outer.addWidget(self.status_line)
        self.setWindowTitle(title)
        self.titleBar.sub.setText(subtitle)
        # FramelessWindow ставит фильтр событий на всё приложение - снять до выхода,
        # иначе при закрытии программы Qt может обратиться к уже удалённому окну.
        QCoreApplication.instance().aboutToQuit.connect(self._detach)

    def _detach(self) -> None:
        QCoreApplication.instance().removeEventFilter(self)

    def show_message(self, text: str, error: bool = False) -> None:
        color = "#f85149" if error else "#7a828e"
        self.status_line.setStyleSheet(f"color: {color};")
        self.status_line.setText(f"{time.strftime('%H:%M:%S')}  {text}")


class InstrumentWindow(FramelessFrame):
    """Окно прибора. Знает только ``InstrumentModel`` - никаких драйверов."""

    SIZES = {ModeSwitch.FULL: QSize(1180, 720), ModeSwitch.COMPACT: QSize(620, 560)}

    def __init__(self, model: InstrumentModel, subtitle: str = "", settings: QSettings | None = None,
                 mode: str | None = None, parent=None):
        super().__init__(model.spec.title, subtitle, parent)
        self.model = model
        if model.parent() is None:      # модель живёт столько же, сколько окно
            model.setParent(self)
        self._settings = settings
        self._sizes = dict(self.SIZES)

        self.mode_switch = ModeSwitch()
        self.titleBar.add_widget(self.mode_switch)
        self.action_bar = ActionBar(model)
        self.settings_panel = SettingsPanel(model)
        self.table = ParamTable(model)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.settings_panel)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 8, 8, 8)
        self.table_caption = QLabel("Selected parameters")
        self.table_caption.setStyleSheet("color: #7a828e;")
        rl.addWidget(self.table_caption)
        rl.addWidget(self.table, 1)
        right.setMinimumWidth(340)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(8, 8, 0, 0)
        hl.addWidget(self.splitter)
        self.body.addWidget(self.action_bar)
        self.body.addWidget(holder, 1)

        model.state_changed.connect(self.titleBar.led.set_state)
        model.state_changed.connect(lambda s: self.show_message({"connected": "Connected",
                                                                 "disconnected": "Disconnected"}.get(s, s)))
        model.message.connect(self.show_message)
        self.titleBar.led.set_state(model.state)
        self._restore()
        self.mode_switch.changed.connect(self._apply_mode)
        model.selection_changed.connect(self._save)
        self._mode = None
        self._apply_mode(mode or self._load("mode", ModeSwitch.FULL))
        if self.mode_switch.mode != self._mode:
            self.mode_switch.set_mode(self._mode)

    # ----------------------------------------------------------------- режимы
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self.mode_switch.set_mode(mode)

    def _apply_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        if self._mode is not None:
            self._sizes[self._mode] = self.size()
        self._mode = mode
        full = mode == ModeSwitch.FULL
        self.settings_panel.setVisible(full)
        self.action_bar.set_compact(not full)
        self.table_caption.setVisible(full)
        self.adjustSize()
        self.resize(self._sizes[mode].expandedTo(self.minimumSizeHint()))
        self._save()

    # ----------------------------------------------------- сохранение выбора
    def _load(self, name, default):
        if self._settings is None:
            return default
        v = self._settings.value(f"{self.model.spec.id}/{name}", default)
        return v if v is not None else default

    def _restore(self) -> None:
        sel = self._load("selected", None)
        if sel is not None:
            self.model.set_selected([s for s in (sel if isinstance(sel, list) else [sel]) if s])

    def _save(self, *_):
        if self._settings is None:
            return
        self._settings.setValue(f"{self.model.spec.id}/selected", list(self.model.selected))
        if self._mode:
            self._settings.setValue(f"{self.model.spec.id}/mode", self._mode)


class MacroWindow(FramelessFrame):
    """Окно макросов для типовых измерений. Пока только заготовка."""

    def __init__(self, parent=None):
        super().__init__("Macros", "measurement routines", parent)
        row = QHBoxLayout()
        row.setContentsMargins(10, 10, 10, 10)
        self.macros = QListWidget()
        self.macros.setMaximumWidth(260)
        row.addWidget(self.macros)
        right = QVBoxLayout()
        self.placeholder = QLabel("Measurement routines will live here:\n"
                                  "pick a macro, set its parameters, run it, watch progress and results.")
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.placeholder, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.run_btn = QPushButton("Run")
        self.stop_btn = QPushButton("Stop")
        for b in (self.run_btn, self.stop_btn):
            b.setEnabled(False)
            buttons.addWidget(b)
        right.addLayout(buttons)
        row.addLayout(right, 1)
        self.body.addLayout(row)
        self.resize(760, 480)
