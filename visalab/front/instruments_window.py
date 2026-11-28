"""Новое окно инструментов:

* Верхний ``WindowBar`` содержит только кнопки ``minimize``, ``maximize`` и ``close``.
* Основная область разделена на две части (``GraphSplitter``), обе с вертикальным скроллом:
  - слева — карточки приборов (заголовок: вкл/выкл, ошибки, sync, пресет, кликабельное
    название прибора; ниже — избранное прибора);
  - справа — настройки выбранного прибора без вкладок, разбитые на сворачиваемые подразделы
    (:class:`CollapsibleSection`), с кнопками «развернуть всё / свернуть всё» и ``×``,
    звёздочками закрепления строк по наведению и сохранением состояния каждого подраздела в ``QSettings``.
* ``attach_instrument(name, win)`` подключает реальное окно прибора (``SpectrumWindow`` и др.)
  в левую карточку и правые подразделы без дублирования логики.
"""
from __future__ import annotations

import json
import sys

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QMimeData, QPoint, QRect, QSettings, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QDrag, QGuiApplication, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .favorites import clone_bound_widget
from .params import PSpinBox, PSpinEdit, Panel, PanelLine, PanelTable, _repolish
from .split_layout import GraphSplitter
from .theme import (OFF, UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_CONTROL, UI_COLOR_ERROR, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER,
                    UI_COLOR_PANEL, UI_COLOR_PROCESS, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_COLUMN_WIDTH_PX, UI_FONT_SIZE_PT, UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY,
                    UI_SEARCH_FONT_SIZE_PT, UI_TITLE_BAR_ITEM_SIZE_PX, WINDOW_BAR_HEIGHT, render_qss, scrollbar_qss)
from .widgets import CollapsibleSection
from .window import (AddressLabel, CollapseAllButton, CustomFullButton, CustomInitButton, CustomSyncButton,
                     ErrorIndicator, ExpandAllButton, PanelCloseButton, PinStarButton, Switch, Window, WindowBar,
                     set_mac_floating_level as _set_mac_floating_level)

__all__ = ["DEFAULT_INSTRUMENTS", "INSTRUMENT_SECTIONS", "InstrumentCard", "InstrumentSettingsView",
           "SearchResultsView", "InstrumentsWindow"]

DEFAULT_INSTRUMENTS: tuple[str, ...] = (
    "VNA",
    "SA",
    "PM",
    "GEN",
    "PULSE",
    "PSU",
)

INSTRUMENT_SECTIONS: dict[str, tuple[str, ...]] = {
    "VNA": ("TRAC", "CH", "PWR", "TRIG", "CAL", "SAVE", "SET"),
    "SA": ("FREQ", "BW", "AMPL", "SWP", "TRAC", "TRIG", "SET"),
    "PM": ("MAIN", "TRIG", "CAL", "TIME", "SWP", "SET"),
    "GEN": ("RF", "MOD", "PULS", "SWP", "CAL", "SET"),
    "PULSE": ("CH1", "CH2", "TRIG", "SET"),
    "PSU": ("CH", "PROT", "OUT", "RULES", "SET"),
}

#: Краткие полные названия подразделов для правого окна настроек (в избранном остаются сокращения)
SECTION_FULL_NAMES: dict[str, str] = {
    "FREQ": "Frequency",
    "BW": "Bandwidth",
    "AMPL": "Amplitude",
    "SWP": "Sweep",
    "TRAC": "Trace",
    "TRIG": "Trigger",
    "SCAL": "Scale",
    "MARK": "Markers",
    "STIM": "Stimulus",
    "PWR": "Power",
    "CAL": "Calibration",
    "SAVE": "Save / Recall",
    "MAIN": "Measurement",
    "TIME": "Time Domain",
    "RF": "RF Output",
    "AM": "AM Modulation",
    "FM": "FM Modulation",
    "PM": "PM Modulation",
    "MOD": "Modulation",
    "BRST": "Burst",
    "PULS": "Pulse",
    "CH": "Channels",
    "CH1": "Channel 1",
    "CH2": "Channel 2",
    "PROT": "Protection",
    "OUT": "Output",
    "RULES": "Safety Rules",
    "SET": "Connection",
}

#: Краткие полные названия строк параметров в правом окне настроек (по подразделу или общие)
PARAM_FULL_NAMES: dict[tuple[str, str], str] = {
    ("SWP", "MODE"): "Sweep Mode",
    ("SWP", "TIME"): "Sweep Time",
    ("SWP", "TYPE"): "Sweep Type",
    ("SWP", "SWP"): "Auto Sweep",
    ("SWP", "RUN"): "Control",
    ("SWP", "FRQ"): "Freq Range",
    ("SWP", "PWR"): "Power Range",
    ("SWP", "OPT"): "Dwell / View",
    ("SWP", "TRK"): "SA Track",
    ("TRAC", "MODE"): "Trace Mode",
    ("TRAC", "CH"): "Channel",
    ("TRAC", "SPAR"): "S-Param",
    ("TRAC", "FMT"): "Format",
    ("TRAC", "EDEL"): "Elec Delay",
    ("TRAC", "SMO"): "Smoothing",
    ("CH", "IFBW"): "IF BW",
    ("CH", "PSTR"): "Power Start",
    ("CH", "PSTP"): "Power Stop",
    ("PWR", "COUP"): "Port Couple",
    ("PWR", "P1"): "Port 1",
    ("PWR", "P2"): "Port 2",
    ("PWR", "P3"): "Port 3",
    ("PWR", "P4"): "Port 4",
    ("SAVE", "SNP"): "Touchstone",
    ("SET", "DLY"): "Cmd Delay",
}

PARAM_FULL_NAMES_COMMON: dict[str, str] = {
    "CENT": "Center",
    "SPAN": "Span",
    "STAR": "Start",
    "STOP": "Stop",
    "ZERO": "Zero Span",
    "RBW": "Res BW",
    "VBW": "Video BW",
    "RLEV": "Ref Level",
    "ROFF": "Ref Offset",
    "SDIV": "Scale / Div",
    "DET": "Detector",
    "CPL": "Coupling",
    "ATT": "Attenuation",
    "PRE": "Preamp",
    "TIME": "Time",
    "PTS": "Points",
    "TYPE": "Type",
    "REF": "Reference",
    "MODE": "Mode",
    "AVG": "Average",
    "SAVG": "Software Avg",
    "SRC": "Source",
    "LVL": "Level",
    "SLOP": "Slope",
    "DLY": "Delay",
    "DRV": "Driver",
    "MDL": "Model",
    "ADDR": "Address",
    "POLL": "Poll Pause",
}


def _full_section_title(code: str) -> str:
    return SECTION_FULL_NAMES.get(code, code)


class _HoverStarFilter(QWidget):
    """Прокси-фильтр: отслеживает наведение мыши на ``PanelLine`` и подсвечивает её ``PinStarButton``."""

    def __init__(self, star: PinStarButton, parent=None):
        super().__init__(parent)
        self._star = star
        self.hide()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt
        if sip.isdeleted(self._star):
            return False
        et = event.type()
        if et == QEvent.Type.Enter:
            self._star.set_row_hovered(True)
        elif et == QEvent.Type.Leave:
            self._star.set_row_hovered(False)
        return False


def _attach_row_star(line, star: PinStarButton) -> _HoverStarFilter:
    """Вставить звёздочку в начало ``PanelLine`` и привязать hover по всей строке."""
    line.line_layout.insertWidget(0, star, 0, Qt.AlignmentFlag.AlignVCenter)
    flt = _HoverStarFilter(star, line)
    line.installEventFilter(flt)
    return flt


def _expand_table_labels(sec_code: str, page: QWidget) -> None:
    """Переименовать подписи строк ``PanelTable`` в краткие полные названия для правого окна настроек
    и выровнять ширину колонок подписей (сокращения в ``FavoritesManager`` при этом не меняются)."""
    if not isinstance(page, PanelTable):
        return
    for line in page.lines:
        raw = line._name.text().strip()
        if not raw or line._name.isHidden():
            continue
        full = PARAM_FULL_NAMES.get((sec_code, raw)) or PARAM_FULL_NAMES_COMMON.get(raw)
        if full:
            line._name.setText(full)
    # Выровнять ширину подписей по колонкам внутри таблицы
    col_widths: dict[int, int] = {}
    for row in page._rows:
        for col, line in enumerate(row):
            if not line._name.isHidden():
                line._name.setMinimumWidth(0)
                line._name.setMaximumWidth(16777215)
                col_widths[col] = max(col_widths.get(col, 0), line.label_hint())
    for row in page._rows:
        for col, line in enumerate(row):
            if not line._name.isHidden() and col in col_widths:
                line.set_label_width(col_widths[col])


CARD_DRAG_MIME = "application/x-visalab-instrument-card"


class InstrumentCard(QFrame):
    """Карточка прибора в левой половине окна: заголовок (вкл/выкл, ошибки, sync, пресет,
    название прибора — двойной клик открывает настройки, перетаскивание меняет порядок)
    и область избранного."""

    settings_requested = pyqtSignal(str)
    drag_started = pyqtSignal(str, QPoint, QPoint)

    STYLE_SHEET = render_qss("""
        InstrumentCard {
            background-color: $UI_COLOR_PANEL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentCard[selected="true"] {
            border-color: $UI_COLOR_INVERT;
        }
        InstrumentCard[dragging="true"] {
            border-style: dashed;
            border-color: $UI_COLOR_BORDER_LIGHT;
        }
        InstrumentCard[floating="true"] {
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: 0px;
        }
        InstrumentCard[floating="true"] QWidget#cardBody {
            background-color: $UI_COLOR_BACKGROUND;
            border-radius: 0px;
        }
        InstrumentCard QWidget#cardBar {
            background-color: $UI_COLOR_CONTROL;
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-top-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-top-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentCard QWidget#cardBody {
            background-color: $UI_COLOR_PANEL;
            border: none;
            border-bottom-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-bottom-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentCard IconButtonBase {
            max-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; min-width: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            background-color: $UI_COLOR_BORDER;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentCard IconButtonBase:hover { background-color: $UI_COLOR_INVERT_HOVER; }
        InstrumentCard IconButtonBase[status="process"] { background-color: $UI_COLOR_PROCESS; }
        InstrumentCard ErrorIndicator[err="true"] { background-color: $UI_COLOR_ERROR; }
        InstrumentCard ErrorIndicator[err="true"]:hover { background-color: $UI_COLOR_INVERT_HOVER; }
        InstrumentCard AddressLabel {
            min-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px; max-height: ${UI_TITLE_BAR_ITEM_SIZE_PX}px;
            padding: 0 8px;
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT_MUTED;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
        }
        InstrumentCard AddressLabel:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        InstrumentCard AddressLabel[selected="true"] {
            background-color: $UI_COLOR_INVERT;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
    """)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.instrument_name = name
        self.instrument_window = None
        self._has_placeholders = True
        self._idn_conn = None
        self._selected = False
        self._dragging = False
        self._floating = False
        self._drag_start_pos: QPoint | None = None
        self.setStyleSheet(self.STYLE_SHEET)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.bar = QWidget(self)
        self.bar.setObjectName("cardBar")
        self.bar.setFixedHeight(WINDOW_BAR_HEIGHT)
        self._bar_layout = QHBoxLayout(self.bar)
        self._bar_layout.setContentsMargins(6, 7, 6, 7)
        self._bar_layout.setSpacing(3)

        self.on_button = Switch(OFF, self.bar)
        self.on_button.setToolTip("Connect / disconnect")
        self.error_indicator = ErrorIndicator(self.bar)
        self.sync_button = CustomSyncButton(self.bar)
        self.sync_button.setToolTip("Sync")
        self.preset_button = CustomInitButton(self.bar)
        self.preset_button.setToolTip("Preset / Init")
        self.address = AddressLabel(name, self.bar)
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.address.setCursor(Qt.CursorShape.PointingHandCursor)
        self.address.setToolTip(f"Click to open {name} settings · drag to reorder or detach")

        self._bar_layout.addWidget(self.on_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._bar_layout.addWidget(self.error_indicator)
        self._bar_layout.addWidget(self.sync_button)
        self._bar_layout.addWidget(self.preset_button)
        self._bar_layout.addWidget(self.address, 1)
        _repolish(self.address)

        self.address.installEventFilter(self)
        self.bar.installEventFilter(self)

        self.body = QWidget(self)
        self.body.setObjectName("cardBody")
        self.body.setMinimumHeight(64)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.bar)
        root.addWidget(self.body)

    def bind_window(self, win) -> None:
        """Подключить (или переподключить) живое ``InstrumentWindow``: кнопки заголовка и избранное."""
        if self._idn_conn is not None:
            old_sig, old_slot = self._idn_conn
            try:
                old_sig.disconnect(old_slot)
            except (TypeError, RuntimeError):
                pass
            self._idn_conn = None
        self.instrument_window = win
        # Заменяем прежние кнопки (заглушки или контролы старого окна) живыми контролами нового окна
        for old in (self.on_button, self.error_indicator, self.sync_button, self.preset_button):
            if old is None or sip.isdeleted(old):
                continue
            self._bar_layout.removeWidget(old)
            old.hide()
            if self._has_placeholders:
                old.deleteLater()
            else:
                old.setParent(None)
        self._has_placeholders = False

        self.on_button = win.on_button
        self.error_indicator = win.error_indicator
        self.sync_button = win.sync_button
        self.preset_button = win.init_button
        for idx, btn in enumerate((self.on_button, self.error_indicator, self.sync_button, self.preset_button)):
            btn.setParent(self.bar)
            align = Qt.AlignmentFlag.AlignVCenter if btn is self.on_button else Qt.AlignmentFlag(0)
            self._bar_layout.insertWidget(idx, btn, 0, align)
            btn.show()

        self._sync_title()
        slot = lambda _idn: self._sync_title()
        win.proxy.identified.connect(slot)
        self._idn_conn = (win.proxy.identified, slot)

        # Избранное прибора — прямо в тело карточки
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
        win.preset_panel.setParent(self.body)
        win.preset_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        win.preset_panel.show()
        self.body.setMinimumHeight(0)
        self.body_layout.addWidget(win.preset_panel)
        self.refresh_geometry()

    def refresh_geometry(self) -> None:
        win = self.instrument_window
        if win is not None and not sip.isdeleted(win):
            for w in (win.preset_panel, getattr(win.favorites, "_extra_host", None)):
                if w is not None and not sip.isdeleted(w):
                    if w.layout() is not None:
                        w.layout().invalidate()
                        w.layout().activate()
                    w.updateGeometry()
        if self.body.layout() is not None:
            self.body.layout().invalidate()
            self.body.layout().activate()
        self.body.updateGeometry()
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None and not sip.isdeleted(parent):
            if parent.layout() is not None:
                parent.layout().invalidate()
                parent.layout().activate()
            parent.updateGeometry()
        win_host = self.window()
        if isinstance(win_host, _FloatingCardWindow) and not sip.isdeleted(win_host):
            win_host.adjust_height()

    def _sync_title(self) -> None:
        win = self.instrument_window
        if win is None or sip.isdeleted(self) or sip.isdeleted(win):
            return
        drv = (win.proxy.name or self.instrument_name).strip()
        label = f"{self.instrument_name} · {drv}" if drv.upper() != self.instrument_name.upper() else self.instrument_name
        self.address.setText(label)
        self._update_title_tooltip()

    def _update_title_tooltip(self) -> None:
        win = self.instrument_window
        tip = win.address.toolTip() if win is not None and not sip.isdeleted(win) else ""
        if self._floating:
            hint = "Click for settings · drag to move or drop back into panel"
        else:
            hint = "Click for settings · drag to reorder or detach"
        self.address.setToolTip(f"{tip}\n{hint}" if tip else hint)

    def set_floating(self, floating: bool) -> None:
        floating = bool(floating)
        self._floating = floating
        self.setProperty("floating", floating)
        self.bar.setVisible(not floating)
        self.address.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred if floating else QSizePolicy.Policy.Fixed,
        )
        _repolish(self)
        self._update_title_tooltip()

    def take_bar_widgets(self) -> tuple[QWidget, QWidget, QWidget, QWidget, AddressLabel]:
        """Забрать виджеты заголовка карточки (для размещения в ``WindowBar`` плавающего окна)."""
        widgets = (self.on_button, self.error_indicator, self.sync_button, self.preset_button, self.address)
        for w in widgets:
            self._bar_layout.removeWidget(w)
        return widgets

    def restore_bar_widgets(self) -> None:
        """Вернуть виджеты заголовка обратно в ``card.bar`` при прикреплении карточки."""
        widgets = (self.on_button, self.error_indicator, self.sync_button, self.preset_button, self.address)
        for w in widgets:
            w.setParent(self.bar)
            w.show()
        self._bar_layout.addWidget(self.on_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._bar_layout.addWidget(self.error_indicator)
        self._bar_layout.addWidget(self.sync_button)
        self._bar_layout.addWidget(self.preset_button)
        self._bar_layout.addWidget(self.address, 1)
        _repolish(self.address)

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", selected)
        self.address.setProperty("selected", selected)
        _repolish(self)
        _repolish(self.address)

    def set_dragging(self, dragging: bool) -> None:
        dragging = bool(dragging)
        if dragging == self._dragging:
            return
        self._dragging = dragging
        self.setProperty("dragging", dragging)
        _repolish(self)

    def _is_title_drag_target(self, obj, pos: QPoint) -> bool:
        if obj is self.address:
            return True
        if obj is self.bar:
            child = self.bar.childAt(pos)
            while child is not None and child is not self.bar:
                if isinstance(child, QAbstractButton) or child is self.address:
                    return False
                child = child.parentWidget()
            return True
        return False

    def eventFilter(self, obj, event):  # noqa: N802 - Qt
        if self._floating:
            # В плавающем окне заголовок обрабатывает _FloatingWindowBar (клик / перетаскивание)
            return super().eventFilter(obj, event)
        et = event.type()
        if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self._is_title_drag_target(obj, event.position().toPoint()):
                gpos = event.globalPosition().toPoint()
                self._drag_start_pos = gpos
                self._drag_hotspot = self.bar.mapFromGlobal(gpos)
                return True
        elif et == QEvent.Type.MouseMove and self._drag_start_pos is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                gpos = event.globalPosition().toPoint()
                if (gpos - self._drag_start_pos).manhattanLength() >= QApplication.startDragDistance():
                    hotspot = getattr(self, "_drag_hotspot", QPoint(self.bar.width() // 2, self.bar.height() // 2))
                    self._drag_start_pos = None
                    self.drag_started.emit(self.instrument_name, gpos, hotspot)
                    return True
            else:
                self._drag_start_pos = None
        elif et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._drag_start_pos is not None:
                self._drag_start_pos = None
                self.settings_requested.emit(self.instrument_name)
                return True
        return super().eventFilter(obj, event)


def _replace_spinboxes_with_line_edits(win, table: PanelTable) -> None:
    """В правом меню настроек не должно быть спинбоксов — заменяем PSpinBox / PSpinEdit на обычный PLineEdit."""
    replaced = False
    for ln in table.lines:
        for idx, w in enumerate(list(ln.widgets)):
            if sip.isdeleted(w) or not isinstance(w, (PSpinBox, PSpinEdit)):
                continue
            key = getattr(w, "key", None)
            if not key:
                continue
            edit = clone_bound_widget(w, win.proxy, as_spinbox=False)
            if edit is None:
                continue
            edit.setEnabled(w.isEnabled())
            edit.setVisible(not w.isHidden())
            lay_idx = ln.line_layout.indexOf(w)
            ln.line_layout.removeWidget(w)
            if lay_idx >= 0:
                ln.line_layout.insertWidget(lay_idx, edit)
            else:
                ln.line_layout.addWidget(edit)
            ln.widgets[idx] = edit
            if isinstance(getattr(win, "widgets", None), dict) and win.widgets.get(key) is w:
                win.widgets[key] = edit
            saved = getattr(win, "saved_widgets", None)
            if isinstance(saved, list):
                for si, sw in enumerate(saved):
                    if sw is w:
                        saved[si] = edit
            if hasattr(w, "unbind"):
                w.unbind()
            w.hide()
            w.setParent(None)
            w.deleteLater()
            replaced = True
    if replaced:
        table.build(UI_COLUMN_WIDTH_PX)


class InstrumentSettingsView(QFrame):
    """Панель настроек одного прибора в правой половине окна: вертикальный список сворачиваемых
    подразделов (``CollapsibleSection``) без вкладок + кнопки «развернуть всё / свернуть всё»."""

    close_requested = pyqtSignal()

    STYLE_SHEET = render_qss("""
        InstrumentSettingsView {
            background-color: transparent;
            border: none;
        }
        InstrumentSettingsView QFrame#settingsHeader {
            background-color: $UI_COLOR_CONTROL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentSettingsView QFrame#settingsPinned {
            background-color: $UI_COLOR_PANEL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        InstrumentSettingsView QLabel#settingsTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            padding: 0 4px;
        }
        InstrumentSettingsView QLabel#sectionPlaceholder {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 14px 4px;
        }
        InstrumentSettingsView ExpandAllButton,
        InstrumentSettingsView CollapseAllButton,
        InstrumentSettingsView PanelCloseButton,
        InstrumentSettingsView CustomFavButton {
            min-width: 26px; max-width: 26px;
            min-height: 24px; max-height: 24px;
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 0;
        }
        InstrumentSettingsView ExpandAllButton:hover,
        InstrumentSettingsView CollapseAllButton:hover,
        InstrumentSettingsView PanelCloseButton:hover,
        InstrumentSettingsView CustomFavButton:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
        }
    """)

    def __init__(self, name: str, section_names: tuple[str, ...] = ("SET",), *,
                 settings: QSettings | None = None, parent=None):
        super().__init__(parent)
        self.instrument_name = name
        self._settings = settings
        self.instrument_window = None
        self.error_section: CollapsibleSection | None = None
        self.pinned_section: QFrame | None = None
        self.fav_button = None
        self.setStyleSheet(self.STYLE_SHEET)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        # Заголовок правой панели: название + (★) + развернуть всё + свернуть всё + закрыть
        header = QFrame(self)
        header.setObjectName("settingsHeader")
        header.setFixedHeight(WINDOW_BAR_HEIGHT)
        self._header_layout = QHBoxLayout(header)
        self._header_layout.setContentsMargins(8, 6, 6, 6)
        self._header_layout.setSpacing(4)

        self.title_label = QLabel(f"{name} SETTINGS", header)
        self.title_label.setObjectName("settingsTitle")
        self.expand_all_button = ExpandAllButton(header)
        self.expand_all_button.setToolTip(f"Expand all {name} sections")
        self.expand_all_button.clicked.connect(self.expand_all)
        self.collapse_all_button = CollapseAllButton(header)
        self.collapse_all_button.setToolTip(f"Collapse all {name} sections")
        self.collapse_all_button.clicked.connect(self.collapse_all)
        self.close_button = PanelCloseButton(header)
        self.close_button.setToolTip(f"Close {name} settings")
        self.close_button.clicked.connect(self.close_requested.emit)

        self._header_layout.addWidget(self.title_label)
        self._header_layout.addStretch(1)
        self._header_layout.addWidget(self.expand_all_button)
        self._header_layout.addWidget(self.collapse_all_button)
        self._header_layout.addWidget(self.close_button)

        self._sections_host = QWidget(self)
        self._sections_layout = QVBoxLayout(self._sections_host)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(8)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(header)
        root.addWidget(self._sections_host)
        root.addStretch(1)

        self.sections: dict[str, CollapsibleSection] = {}
        self._build_placeholder_sections(section_names)

    def _clear_sections(self) -> None:
        self.sections.clear()
        self.error_section = None
        self.pinned_section = None
        while self._sections_layout.count():
            item = self._sections_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _build_placeholder_sections(self, section_names: tuple[str, ...]) -> None:
        self._clear_sections()
        for sec_code in section_names:
            sec_title = _full_section_title(sec_code)
            key = f"INSTRUMENTS/SECTIONS/{self.instrument_name}/{sec_code}"
            placeholder = QLabel(f"{self.instrument_name} — {sec_title}")
            placeholder.setObjectName("sectionPlaceholder")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sec = CollapsibleSection(sec_title, placeholder, expanded=True, settings=self._settings,
                                     settings_key=key, parent=self._sections_host)
            self.sections[sec_code] = sec
            self._sections_layout.addWidget(sec)

    def bind_window(self, win, on_show_errors=None, on_favorites_changed=None) -> None:
        """Загрузить в правую панель все реальные подразделы настроек из ``InstrumentWindow``."""
        for sig, slot in getattr(self, "_bound_conns", ()):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._bound_conns: list = []
        self.instrument_window = win
        self._on_favorites_changed = on_favorites_changed
        win._hosted_in_sections = True
        self.fav_stars: dict[str, PinStarButton] = {}
        self._fav_filters: list[_HoverStarFilter] = []
        self._left_fav_filters: list[_HoverStarFilter] = []
        self._clear_sections()

        # Журнал ошибок (появляется сверху при клике по индикатору ошибок)
        err_panel = Panel(win.error_page)
        err_panel.layout().setContentsMargins(0, 0, 0, 0)
        win.error_page.show()
        self.error_section = CollapsibleSection("ERRORS", err_panel, expanded=True, parent=self._sections_host)
        self.error_section.hide()
        self._sections_layout.addWidget(self.error_section)

        def _toggle_errors(show: bool) -> None:
            if sip.isdeleted(self) or self.error_section is None:
                return
            self.error_section.setVisible(bool(show))
            if show:
                self.error_section.set_expanded(True, save=False)
                if on_show_errors is not None:
                    on_show_errors()

        win._hosted_errors_cb = _toggle_errors

        # Несворачиваемая шапка настроек (например, каналы и трассы VNA — всегда видны сверху настроек)
        pinned_table = getattr(win, "settings_header_table", None)
        if pinned_table is not None:
            pinned = QFrame(self._sections_host)
            pinned.setObjectName("settingsPinned")
            pinned.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            pl = QVBoxLayout(pinned)
            pl.setContentsMargins(8, 8, 8, 8)
            pl.setSpacing(6)
            pnl = Panel(pinned_table)
            pnl.layout().setContentsMargins(0, 0, 0, 0)
            pinned_table.show()
            pl.addWidget(pnl)
            self.pinned_section = pinned
            self._sections_layout.addWidget(pinned)

        # Сопоставляем строки настроек с пунктами FavoritesManager для звёздочек по наведению
        fav_by_line = {
            id(it.source_line): it
            for it in win.favorites.items
            if not it.is_default_top and it.source_line is not None
        }

        # Все подразделы прибора (с краткими полными названиями справа; в избранном остаются сокращения)
        self._state_keys: dict[str, tuple[str, ...]] = {}
        for i, page in enumerate(win.pages):
            sec_code = win.tabs.tabText(i) or f"SEC {i + 1}"
            sec_title = _full_section_title(sec_code)
            if isinstance(page, PanelTable):
                _replace_spinboxes_with_line_edits(win, page)
            _expand_table_labels(sec_code, page)
            if isinstance(page, PanelTable):
                has_any_pinnable = any(id(ln) in fav_by_line for ln in page.lines)
                for ln in page.lines:
                    fav_item = fav_by_line.get(id(ln))
                    if fav_item is not None:
                        star = PinStarButton(ln)
                        star.setChecked(win.favorites.is_enabled(fav_item.item_id))
                        star.setToolTip(f"Pin / unpin {fav_item.label} in favorites")
                        star.clicked.connect(
                            lambda checked, iid=fav_item.item_id: self._toggle_favorite(iid, checked)
                        )
                        self.fav_stars[fav_item.item_id] = star
                        self._fav_filters.append(_attach_row_star(ln, star))
                    elif has_any_pinnable and not ln._name.isHidden():
                        spacer = QWidget(ln)
                        spacer.setFixedSize(18, 20)
                        spacer.setStyleSheet("background: transparent; border: none;")
                        ln.line_layout.insertWidget(0, spacer, 0, Qt.AlignmentFlag.AlignVCenter)
            key = f"INSTRUMENTS/SECTIONS/{self.instrument_name}/{sec_code}"
            panel = Panel(page)
            panel.layout().setContentsMargins(0, 0, 0, 0)
            page.show()
            sec = CollapsibleSection(sec_title, panel, expanded=True, settings=self._settings,
                                     settings_key=key, parent=self._sections_host)
            self.sections[sec_code] = sec
            self._sections_layout.addWidget(sec)
            if i < len(win._page_states):
                self._state_keys[sec_code] = win._page_states[i]

        self._attach_left_unpin_stars()
        on_param = lambda _k, _v: self._refresh_active_states()
        win.proxy.param_updated.connect(on_param)
        self._bound_conns.append((win.proxy.param_updated, on_param))
        if hasattr(win, "sweep"):
            on_swp = lambda _on: self._refresh_active_states()
            win.sweep.running_changed.connect(on_swp)
            self._bound_conns.append((win.sweep.running_changed, on_swp))
        self._refresh_active_states()

    def _toggle_favorite(self, item_id: str, checked: bool) -> None:
        win = self.instrument_window
        if win is None or sip.isdeleted(self) or sip.isdeleted(win):
            return
        win.favorites.set_enabled(item_id, bool(checked))
        self._sync_fav_stars()
        self._attach_left_unpin_stars()
        cb = getattr(self, "_on_favorites_changed", None)
        if cb is not None:
            cb()

    def _sync_fav_stars(self) -> None:
        win = self.instrument_window
        if win is None or sip.isdeleted(self) or sip.isdeleted(win):
            return
        for iid, star in self.fav_stars.items():
            if not sip.isdeleted(star):
                star.blockSignals(True)
                star.setChecked(win.favorites.is_enabled(iid))
                star.blockSignals(False)
                star.update()

    def _attach_left_unpin_stars(self) -> None:
        """Добавить звёздочки открепления по наведению на закреплённые строки в левой карточке."""
        win = self.instrument_window
        if win is None or sip.isdeleted(self) or sip.isdeleted(win):
            return
        self._left_fav_filters.clear()
        pinned_ids = list(win.favorites.pinned)
        pinned_lines = win.favorites.pinned_lines()
        for iid, ln in zip(pinned_ids, pinned_lines):
            if sip.isdeleted(ln) or ln.findChild(PinStarButton) is not None:
                continue
            star = PinStarButton(ln, hover_only=True)
            star.setChecked(True)
            star.setToolTip("Remove from favorites")
            star.clicked.connect(lambda _checked, item_id=iid: self._toggle_favorite(item_id, False))
            self._left_fav_filters.append(_attach_row_star(ln, star))

    def _refresh_active_states(self) -> None:
        win = self.instrument_window
        if win is None or sip.isdeleted(self) or sip.isdeleted(win):
            return
        for sec_name, sec in self.sections.items():
            states = self._state_keys.get(sec_name, ())
            active = any(win.proxy.value(k) is True for k in states)
            if sec_name == "SWP" and hasattr(win, "sweep") and win.sweep.running:
                active = True
            sec.set_active(active)

    def expand_all(self) -> None:
        for sec in self.sections.values():
            sec.set_expanded(True)

    def collapse_all(self) -> None:
        for sec in self.sections.values():
            sec.set_expanded(False)


class _SearchLineEdit(QLineEdit):
    """Компактное поле поиска в ``WindowBar``: ``Esc`` очищает запрос и снимает фокус."""

    def keyPressEvent(self, event):  # noqa: N802 - Qt
        if event.key() == Qt.Key.Key_Escape:
            if self.text():
                self.clear()
            else:
                self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class SearchResultsView(QFrame):
    """Отдельный виджет результатов поиска настроек (заменяет правую панель при непустом запросе,
    не изменяя состояние ``InstrumentSettingsView``). Сверху — выбранный прибор, ниже — остальные."""

    close_requested = pyqtSignal()
    jump_requested = pyqtSignal(str, str)

    STYLE_SHEET = render_qss("""
        SearchResultsView {
            background-color: transparent;
            border: none;
        }
        SearchResultsView QFrame#settingsHeader {
            background-color: $UI_COLOR_CONTROL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        SearchResultsView QLabel#settingsTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            padding: 0 4px;
        }
        SearchResultsView QLabel#searchCount {
            color: $UI_COLOR_TEXT_MUTED;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 0 6px;
        }
        SearchResultsView PanelCloseButton {
            min-width: 26px; max-width: 26px;
            min-height: 24px; max-height: 24px;
            background-color: $UI_COLOR_BACKGROUND;
            border: none;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            padding: 0;
        }
        SearchResultsView PanelCloseButton:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
        }
        SearchResultsView QFrame#searchGroup {
            background-color: $UI_COLOR_PANEL;
            border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        SearchResultsView QFrame#searchGroup[current="true"] {
            border-color: $UI_COLOR_INVERT;
        }
        SearchResultsView QWidget#searchGroupBody {
            background-color: $UI_COLOR_PANEL;
            border: none;
            border-bottom-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-bottom-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        SearchResultsView Panel {
            background-color: transparent;
            border: none;
        }
        SearchResultsView QWidget#searchGroupBar {
            background-color: $UI_COLOR_CONTROL;
            border-bottom: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BORDER;
            border-top-left-radius: ${UI_BORDER_RADIUS_PX}px;
            border-top-right-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        SearchResultsView QLabel#searchGroupTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
        }
        SearchResultsView QPushButton#searchSectionLink {
            color: $UI_COLOR_TEXT_MUTED;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
            border: none;
            padding: 2px 4px;
            text-align: left;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        SearchResultsView QPushButton#searchSectionLink:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        SearchResultsView QLabel#searchEmpty {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 28px 12px;
        }
    """)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bound_clones: list[QWidget] = []
        self._hover_filters: list[_HoverStarFilter] = []
        self.setStyleSheet(self.STYLE_SHEET)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        header = QFrame(self)
        header.setObjectName("settingsHeader")
        header.setFixedHeight(WINDOW_BAR_HEIGHT)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 6, 6, 6)
        hl.setSpacing(4)

        self.title_label = QLabel("SEARCH", header)
        self.title_label.setObjectName("settingsTitle")
        self.count_label = QLabel("", header)
        self.count_label.setObjectName("searchCount")
        self.close_button = PanelCloseButton(header)
        self.close_button.setToolTip("Clear search")
        self.close_button.clicked.connect(self.close_requested.emit)

        hl.addWidget(self.title_label)
        hl.addStretch(1)
        hl.addWidget(self.count_label)
        hl.addWidget(self.close_button)

        self._body_host = QWidget(self)
        self._body_layout = QVBoxLayout(self._body_host)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(8)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(header)
        root.addWidget(self._body_host)
        root.addStretch(1)

    def clear_results(self) -> None:
        for w in self._bound_clones:
            if not sip.isdeleted(w) and hasattr(w, "unbind"):
                w.unbind()
        self._bound_clones.clear()
        self._hover_filters.clear()
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def set_results(self, query: str, ordered_views: list[tuple[str, InstrumentSettingsView, str]],
                    selected_name: str | None) -> int:
        """Перестроить результаты поиска. ``ordered_views``: ``[(name, view, display_title), ...]``."""
        self.clear_results()
        tokens = [t for t in query.strip().lower().split() if t]
        if not tokens:
            self.count_label.setText("")
            return 0

        # Сначала выбранный прибор, ниже — все остальные
        prioritized = list(ordered_views)
        if selected_name is not None:
            prioritized.sort(key=lambda item: 0 if item[0] == selected_name else 1)

        total_matches = 0
        for inst_name, view, display_title in prioritized:
            win = view.instrument_window
            grouped: dict[str, tuple[str, list[tuple[str, object, list[QWidget]]]]] = {}
            if win is not None and not sip.isdeleted(win):
                fav_by_line = {
                    id(it.source_line): it
                    for it in win.favorites.items
                    if not it.is_default_top and it.source_line is not None
                }
                for i, page in enumerate(win.pages):
                    sec_code = win.tabs.tabText(i) or f"SEC {i + 1}"
                    sec_title = _full_section_title(sec_code)
                    sec_hay = f"{inst_name} {display_title} {sec_code} {sec_title}"
                    if not isinstance(page, PanelTable):
                        if all(tok in sec_hay.lower() for tok in tokens):
                            grouped.setdefault(sec_code, (sec_title, []))[1].append((sec_title, None, []))
                        continue
                    for ln in page.lines:
                        if ln.isHidden():
                            continue
                        full_label = ln._name.text().strip()
                        fav_item = fav_by_line.get(id(ln))
                        if not full_label and fav_item is None:
                            continue
                        short_label = fav_item.label if fav_item is not None else ""
                        btn_texts: list[str] = []
                        for w in ln.widgets:
                            if sip.isdeleted(w):
                                continue
                            if isinstance(w, QAbstractButton) and w.text().strip():
                                btn_texts.append(w.text().strip())
                        row_hay = " ".join([sec_hay, full_label, short_label, *btn_texts]).lower()
                        if not all(tok in row_hay for tok in tokens):
                            continue
                        clones: list[QWidget] = []
                        for w in ln.widgets:
                            if sip.isdeleted(w):
                                continue
                            c = clone_bound_widget(w, win.proxy, as_spinbox=False)
                            if c is not None:
                                clones.append(c)
                        row_title = full_label or short_label or (" / ".join(btn_texts) if btn_texts else sec_title)
                        grouped.setdefault(sec_code, (sec_title, []))[1].append((row_title, fav_item, clones))
            else:
                for sec_code, sec in view.sections.items():
                    sec_hay = f"{inst_name} {display_title} {sec_code} {sec.title}".lower()
                    if all(tok in sec_hay for tok in tokens):
                        grouped.setdefault(sec_code, (sec.title, []))[1].append((sec.title, None, []))

            if not grouped:
                continue

            inst_count = sum(len(rows) for _, rows in grouped.values())
            total_matches += inst_count
            self._add_instrument_group(inst_name, display_title, view, grouped, is_current=(inst_name == selected_name),
                                       match_count=inst_count)

        if total_matches == 0:
            self.count_label.setText("0 matches")
            empty = QLabel("No matching settings", self._body_host)
            empty.setObjectName("searchEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.addWidget(empty)
        else:
            suffix = "match" if total_matches == 1 else "matches"
            self.count_label.setText(f"{total_matches} {suffix}")
        return total_matches

    def _add_instrument_group(self, inst_name: str, display_title: str, view: InstrumentSettingsView,
                              grouped: dict[str, tuple[str, list[tuple[str, object, list[QWidget]]]]],
                              *, is_current: bool, match_count: int) -> None:
        win = view.instrument_window
        card = QFrame(self._body_host)
        card.setObjectName("searchGroup")
        card.setProperty("current", is_current)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        bar = QWidget(card)
        bar.setObjectName("searchGroupBar")
        bar.setFixedHeight(32)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 4, 10, 4)
        bl.setSpacing(6)
        title = QLabel(display_title, bar)
        title.setObjectName("searchGroupTitle")
        cnt = QLabel(str(match_count), bar)
        cnt.setObjectName("searchCount")
        bl.addWidget(title)
        bl.addStretch(1)
        bl.addWidget(cnt)
        cl.addWidget(bar)

        body = QWidget(card)
        body.setObjectName("searchGroupBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(8, 6, 8, 8)
        body_lay.setSpacing(6)

        all_lines: list[PanelLine] = []
        built_sections: list[tuple[str, str, bool, list[PanelLine], list[tuple[PanelLine, object]]]] = []
        for sec_code, (sec_title, rows) in grouped.items():
            # Если в разделе только одна строка-заглушка с тем же именем (неподключённый прибор), не дублируем заголовок
            is_single_section_jump = len(rows) == 1 and not rows[0][2] and rows[0][0] == sec_title
            panel_lines: list[PanelLine] = []
            stars_info: list[tuple[PanelLine, object]] = []
            for row_title, fav_item, clones in rows:
                if clones:
                    self._bound_clones.extend(clones)
                    ln = PanelLine(row_title, *clones)
                else:
                    btn = QPushButton("Open →")
                    btn.setObjectName("choice")
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    btn.clicked.connect(lambda _c=False, n=inst_name, sc=sec_code: self.jump_requested.emit(n, sc))
                    ln = PanelLine(row_title, btn)
                panel_lines.append(ln)
                stars_info.append((ln, fav_item))
                all_lines.append(ln)
            built_sections.append((sec_code, sec_title, not is_single_section_jump, panel_lines, stars_info))

        has_any_pinnable = any(fi is not None for _, _, _, _, si in built_sections for _, fi in si)

        for sec_code, sec_title, show_sec_link, panel_lines, stars_info in built_sections:
            if show_sec_link:
                sec_btn = QPushButton(f"{sec_title} ↗", body)
                sec_btn.setObjectName("searchSectionLink")
                sec_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                sec_btn.setToolTip(f"Open {inst_name} · {sec_title}")
                sec_btn.clicked.connect(lambda _c=False, n=inst_name, sc=sec_code: self.jump_requested.emit(n, sc))
                body_lay.addWidget(sec_btn)

            table = PanelTable(*panel_lines)
            for ln, fav_item in stars_info:
                if fav_item is not None and win is not None and not sip.isdeleted(win):
                    star = PinStarButton(ln)
                    star.setChecked(win.favorites.is_enabled(fav_item.item_id))
                    star.setToolTip(f"Pin / unpin {fav_item.label} in favorites")
                    star.clicked.connect(
                        lambda checked, v=view, iid=fav_item.item_id: v._toggle_favorite(iid, checked)
                    )
                    self._hover_filters.append(_attach_row_star(ln, star))
                elif has_any_pinnable and not ln._name.isHidden():
                    spacer = QWidget(ln)
                    spacer.setFixedSize(18, 20)
                    spacer.setStyleSheet("background: transparent; border: none;")
                    ln.line_layout.insertWidget(0, spacer, 0, Qt.AlignmentFlag.AlignVCenter)
            sec_panel = Panel(table, parent=body)
            sec_panel.setStyleSheet(
                Panel.STYLE_SHEET + "\n" + render_qss("Panel, Panel QWidget { font-size: ${UI_SEARCH_FONT_SIZE_PT}pt; }")
            )
            sec_panel.layout().setContentsMargins(0, 0, 0, 0)
            body_lay.addWidget(sec_panel)

        for ln in all_lines:
            if not ln._name.isHidden():
                ln._name.ensurePolished()
        max_lbl = max((ln.label_hint() for ln in all_lines if not ln._name.isHidden()), default=0)
        if max_lbl > 0:
            for ln in all_lines:
                if not ln._name.isHidden():
                    ln.set_label_width(max_lbl)

        cl.addWidget(body)
        self._body_layout.addWidget(card)


class _FloatingWindowBar(WindowBar):
    """Заголовок откреплённого окна прибора: клик открывает настройки, перетаскивание двигает окно."""

    title_clicked = pyqtSignal()
    drag_moved = pyqtSignal(QPoint)
    drag_finished = pyqtSignal(QPoint)

    STYLE_SHEET_EXTRA = render_qss("""
        WindowBar AddressLabel {
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
        }
        WindowBar AddressLabel:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }
        WindowBar AddressLabel[selected="true"] {
            background-color: $UI_COLOR_INVERT;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
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
            else:
                self.title_clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)


class _FloatingCardWindow(Window):
    """Обычное безрамочное окно ``Window`` для откреплённой карточки прибора:
    живое перемещение за заголовок, изменение размера за края, кнопки прикрепить / свернуть / закрыть."""

    dock_back_requested = pyqtSignal(str)
    drag_moved = pyqtSignal(str, QPoint)
    drag_dropped = pyqtSignal(str, QPoint)
    activated = pyqtSignal(str)

    def __init__(self, card: InstrumentCard, settings: QSettings, width_px: int, parent=None):
        super().__init__(settings, parent)
        self.card = card
        self._closing_for_dock = False
        self._last_auto_h = 0
        self._size_key = f"WINDOW_SIZE/InstrumentFloat.{card.instrument_name}"
        self._pos_key = f"WINDOW_POS/InstrumentFloat.{card.instrument_name}"
        self._heights_key = f"WINDOW_HEIGHTS/InstrumentFloat.{card.instrument_name}"

        bar = _FloatingWindowBar(self)
        self.setTitleBar(bar)
        self.titleBar.raise_()
        self.titleBar.title_label.hide()
        self.titleBar.minimized_button.hide()
        self.setWindowTitle(card.instrument_name)

        # Переносим контролы заголовка карточки прямо в WindowBar окна
        on_btn, err_ind, sync_btn, preset_btn, addr = card.take_bar_widgets()
        for idx, w in enumerate((on_btn, err_ind, sync_btn, preset_btn)):
            w.setParent(self.titleBar)
            self.titleBar.insert_widget(w, idx, 0)
            w.show()
        addr.setParent(self.titleBar)
        self.titleBar.insert_widget(addr, 4, 1)
        addr.show()
        _repolish(addr)

        bar.title_clicked.connect(lambda: card.settings_requested.emit(card.instrument_name))
        bar.drag_moved.connect(lambda gpos: self.drag_moved.emit(card.instrument_name, gpos))
        bar.drag_finished.connect(lambda gpos: self.drag_dropped.emit(card.instrument_name, gpos))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, WINDOW_BAR_HEIGHT, 0, 0)
        lay.setSpacing(0)

        card.setParent(self)
        card.set_floating(True)
        card.show()
        lay.addWidget(card)

        w = max(340, min(int(width_px), 560))
        self.setMinimumWidth(300)
        self.resize(w, max(120, WINDOW_BAR_HEIGHT + card.body.sizeHint().height()))
        self.adjust_height(force=True)

    def adjust_height(self, force: bool = False) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.card):
            return
        self.card.body.layout().invalidate()
        self.card.layout().invalidate()
        self.layout().invalidate()
        need_h = max(WINDOW_BAR_HEIGHT + 64, WINDOW_BAR_HEIGHT + self.card.body.sizeHint().height())
        self.setMinimumHeight(need_h)
        if force or self.height() < need_h or self.height() == self._last_auto_h:
            self.resize(self.width(), need_h)
        self._last_auto_h = need_h
        self._ensure_resizable()

    def release_card(self) -> None:
        """Вернуть контролы заголовка и тело карточки обратно перед прикреплением."""
        if sip.isdeleted(self) or sip.isdeleted(self.card):
            return
        for w in (self.card.on_button, self.card.error_indicator, self.card.sync_button,
                  self.card.preset_button, self.card.address):
            self.titleBar.hBoxLayout.removeWidget(w)
        if self.layout() is not None:
            self.layout().removeWidget(self.card)
        self.card.restore_bar_widgets()

    def event(self, e):  # noqa: N802 - Qt
        res = super().event(e)
        et = e.type()
        if et == QEvent.Type.LayoutRequest:
            self.adjust_height()
        elif et == QEvent.Type.WindowActivate and not sip.isdeleted(self.card):
            self.activated.emit(self.card.instrument_name)
        return res

    def move_clamped(self, pos: QPoint) -> None:
        self.move(self._clamp_pos(pos))

    def closeEvent(self, event):  # noqa: N802 - Qt
        if not self._closing_for_dock and not sip.isdeleted(self.card):
            self.dock_back_requested.emit(self.card.instrument_name)
            event.accept()
            return
        super().closeEvent(event)


class _CardListHost(QWidget):
    """Контейнер карточек приборов в левой панели с поддержкой живого перетаскивания и drop-индикатора."""

    order_changed = pyqtSignal(list)
    external_card_dropped = pyqtSignal(str, int)
    live_drag_moved = pyqtSignal(str, QPoint, QPoint)
    live_drag_finished = pyqtSignal(str, QPoint)
    live_drag_cancelled = pyqtSignal(str)

    _SCROLL_MARGIN_PX = 36
    _SCROLL_STEP_PX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("paneHost")
        self.setAcceptDrops(True)
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

    def bind_scroll(self, scroll: QScrollArea) -> None:
        self._scroll_area = scroll

    def _cards(self) -> list[InstrumentCard]:
        lay = self.layout()
        if lay is None:
            return []
        out: list[InstrumentCard] = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, InstrumentCard):
                out.append(w)
        return out

    def card_order(self) -> list[str]:
        return [c.instrument_name for c in self._cards()]

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

    def move_card(self, name: str, dst_slot: int) -> bool:
        """Переставить карточку ``name`` в слот ``dst_slot`` (индекс вставки до удаления)."""
        lay = self.layout()
        if not isinstance(lay, QVBoxLayout):
            return False
        cards = self._cards()
        src_idx = next((i for i, c in enumerate(cards) if c.instrument_name == name), -1)
        if src_idx < 0:
            return False
        dst_idx = dst_slot - 1 if dst_slot > src_idx else dst_slot
        dst_idx = max(0, min(dst_idx, len(cards) - 1))
        if dst_idx == src_idx:
            return False
        card = cards[src_idx]
        lay.removeWidget(card)
        lay.insertWidget(dst_idx, card)
        lay.invalidate()
        lay.activate()
        self.order_changed.emit(self.card_order())
        return True

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

    def dragEnterEvent(self, event):  # noqa: N802 - Qt
        if event.mimeData().hasFormat(CARD_DRAG_MIME):
            event.acceptProposedAction()
            pos = event.position().toPoint()
            _, line_y = self._drop_slot(pos.y())
            self._update_indicator(line_y)
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt
        if event.mimeData().hasFormat(CARD_DRAG_MIME):
            event.acceptProposedAction()
            pos = event.position().toPoint()
            _, line_y = self._drop_slot(pos.y())
            self._update_indicator(line_y)
            self._update_auto_scroll(pos)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt
        self._stop_drag_feedback()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # noqa: N802 - Qt
        self._stop_drag_feedback()
        if not event.mimeData().hasFormat(CARD_DRAG_MIME):
            event.ignore()
            return
        name = bytes(event.mimeData().data(CARD_DRAG_MIME)).decode("utf-8", errors="ignore")
        slot, _ = self._drop_slot(event.position().toPoint().y())
        if name in self.card_order():
            self.move_card(name, slot)
        else:
            self.external_card_dropped.emit(name, slot)
        event.acceptProposedAction()


class InstrumentsWindow(Window):
    """Окно инструментов из двух вертикально прокручиваемых частей (слева — избранное, справа — настройки)."""

    visibility_changed = pyqtSignal(bool)

    _ORDER_SETTINGS_KEY = "INSTRUMENTS/ORDER"
    _SPLIT_SETTINGS_KEY = "INSTRUMENTS/SPLIT"

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
        QLabel#rightEmpty {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            background: transparent;
            padding: 24px 12px;
        }
        QSplitter { background: $UI_COLOR_BACKGROUND; }
        QSplitter::handle { background: transparent; }
        QSplitter::handle:horizontal { width: 8px; }
    """) + "\n" + scrollbar_qss("QScrollArea#paneScroll ")

    def __init__(self, settings: QSettings | None = None, instruments: tuple[str, ...] = DEFAULT_INSTRUMENTS,
                 parent=None):
        self._settings = settings or QSettings()
        super().__init__(self._settings, parent)
        self.setWindowTitle("INSTRUMENTS")
        self.hide_on_close = False
        self._graphs_window = None
        self.selected_instrument: str | None = None
        self._attached_windows: dict[str, object] = {}
        self._floating_windows: dict[str, _FloatingCardWindow] = {}
        self._detached_slots: dict[str, int] = {}
        self._mac_floating_ok = False
        self._search_active = False
        self._pre_search_scroll = 0
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state_changed)

        # WindowBar: слева Sync All + поиск, справа minimize / maximize / close
        self.titleBar.title_label.hide()

        self.sync_all_button = CustomSyncButton(self.titleBar)
        self.sync_all_button.setToolTip("Sync all connected instruments")
        self.sync_all_button.clicked.connect(self.sync_all)
        self.titleBar.insert_widget(self.sync_all_button, 0)

        self.search_edit = _SearchLineEdit(self.titleBar)
        self.search_edit.setObjectName("windowSearch")
        self.search_edit.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.search_edit.setFixedHeight(UI_TITLE_BAR_ITEM_SIZE_PX)
        self.search_edit.setFixedWidth(300)
        self.search_edit.setPlaceholderText("Search settings...")
        self.search_edit.setToolTip("Search settings across all instruments (Ctrl+F / Cmd+F)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setStyleSheet(render_qss("""
            QLineEdit {
                padding: 0 8px;
                background-color: $UI_COLOR_BACKGROUND;
                color: $UI_COLOR_TEXT;
                border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND;
                border-radius: ${UI_BORDER_RADIUS_PX}px;
                font-family: $UI_MONOSPACE_FONT_FAMILY;
                font-size: ${UI_SEARCH_FONT_SIZE_PT}pt;
            }
            QLineEdit:focus {
                border-color: $UI_COLOR_TEXT_MUTED;
            }
        """))
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.titleBar.insert_widget(self.search_edit, 2)

        self._find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self._find_shortcut.activated.connect(self._focus_search)

        self.maximize_button = CustomFullButton()
        self.maximize_button.setToolTip("Maximize / restore")
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.titleBar.insert_control_widget(self.maximize_button, after_minimize=True)

        # Левая часть: вертикальный скролл с перетаскиваемыми карточками приборов
        self.left_frame, self.left_scroll, self.left_host, self.left_layout = self._make_scroll_pane(
            host_cls=_CardListHost
        )
        self.left_host.bind_scroll(self.left_scroll)
        self.left_host.order_changed.connect(self._save_order)
        self.left_host.external_card_dropped.connect(self.dock_card)
        self.left_host.live_drag_moved.connect(self._on_live_drag_moved)
        self.left_host.live_drag_finished.connect(self._on_live_drag_finished)
        self.left_host.live_drag_cancelled.connect(self.dock_card)

        ordered_instruments = self._load_order(instruments)
        self.cards: dict[str, InstrumentCard] = {}
        for name in ordered_instruments:
            card = InstrumentCard(name, self.left_host)
            card.settings_requested.connect(self.open_settings)
            card.drag_started.connect(self._on_card_drag_started)
            self.cards[name] = card
            self.left_layout.addWidget(card)
        self.left_layout.addStretch(1)

        # Правая часть: вертикальный скролл с подразделами настроек выбранного прибора
        self.right_frame, self.right_scroll, self.right_host, self.right_layout = self._make_scroll_pane()
        self.right_empty = QLabel("Click an instrument title to open its settings", self.right_host)
        self.right_empty.setObjectName("rightEmpty")
        self.right_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right_empty.setWordWrap(True)
        self.right_layout.addWidget(self.right_empty, 1)

        self.settings_views: dict[str, InstrumentSettingsView] = {}
        for name in ordered_instruments:
            view = InstrumentSettingsView(
                name,
                INSTRUMENT_SECTIONS.get(name, ("SET",)),
                settings=self._settings,
                parent=self.right_host,
            )
            view.close_requested.connect(self.close_settings)
            view.hide()
            self.settings_views[name] = view
            self.right_layout.addWidget(view)

        self.search_view = SearchResultsView(self.right_host)
        self.search_view.close_requested.connect(self.clear_search)
        self.search_view.jump_requested.connect(self._jump_to_section)
        self.search_view.hide()
        self.right_layout.addWidget(self.search_view)
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

        root = QVBoxLayout(self)
        root.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        root.setSpacing(0)
        root.addWidget(self.splitter, 1)

        self.titleBar.raise_()
        self.setMinimumWidth(720)
        self.setMinimumHeight(420)
        self.recall_size(QSize(1120, 720))

    def _load_order(self, instruments: tuple[str, ...]) -> list[str]:
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
        valid = set(instruments)
        ordered = [name for name in saved if name in valid]
        seen = set(ordered)
        for name in instruments:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
        return ordered

    def _save_order(self, order: list[str]) -> None:
        merged = list(order)
        seen = set(merged)
        for name in self.cards:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        self._settings.setValue(self._ORDER_SETTINGS_KEY, json.dumps(merged))
        self._settings.sync()

    def instrument_order(self) -> list[str]:
        return self.left_host.card_order()

    def is_detached(self, name: str) -> bool:
        return name in self._floating_windows

    def _on_card_drag_started(self, name: str, global_pos: QPoint, hotspot: QPoint) -> None:
        """Сразу вынуть карточку из левой колонки в живое окно под курсором и отслеживать перетаскивание."""
        self.detach_card(name, global_pos - hotspot, activate=False)
        self.left_host.begin_live_drag(name, hotspot)
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
        """Открепить карточку ``name`` в отдельное обычное окно ``Window``."""
        if name not in self.cards or name in self._floating_windows:
            return
        card = self.cards[name]
        cur_order = self.left_host.card_order()
        self._detached_slots[name] = cur_order.index(name) if name in cur_order else len(cur_order)
        width_px = card.width() or self.left_host.width() or 420
        self.left_layout.removeWidget(card)
        self.left_layout.invalidate()
        self.left_layout.activate()

        fw = _FloatingCardWindow(card, self._settings, width_px)
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
        if _set_mac_floating_level(fw, is_active):
            self._mac_floating_ok = True
        fw.raise_()
        if activate:
            fw.activateWindow()

    def bind_graphs_window(self, graphs_win) -> None:
        """Связать с главным окном графиков."""
        self._graphs_window = graphs_win

    def _show_graphs_window(self) -> None:
        gw = self._graphs_window
        if gw is None or sip.isdeleted(gw):
            return
        if gw.isMinimized():
            gw.showNormal()
        gw.show()
        gw.raise_()
        gw.activateWindow()

    def sync_all(self) -> None:
        """Синхронизировать все подключённые приборы."""
        for win in list(self._attached_windows.values()):
            if not sip.isdeleted(win) and hasattr(win, "_sync"):
                win._sync()

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def clear_search(self) -> None:
        self.search_edit.clear()

    def _all_instrument_names_ordered(self) -> list[str]:
        names = list(self.left_host.card_order())
        seen = set(names)
        for name in self.cards:
            if name not in seen:
                names.append(name)
                seen.add(name)
        return names

    def _on_search_changed(self, text: str) -> None:
        query = text.strip()
        if not query:
            if self._search_active:
                self._search_active = False
                self.search_view.hide()
                self.search_view.clear_results()
                if self.selected_instrument and self.selected_instrument in self.settings_views:
                    self.right_empty.hide()
                    for key, view in self.settings_views.items():
                        view.setVisible(key == self.selected_instrument)
                else:
                    for view in self.settings_views.values():
                        view.hide()
                    self.right_empty.show()
                scroll_val = self._pre_search_scroll
                QTimer.singleShot(0, lambda v=scroll_val: (
                    not sip.isdeleted(self) and self.right_scroll.verticalScrollBar().setValue(v)
                ))
            return

        if not self._search_active:
            self._pre_search_scroll = self.right_scroll.verticalScrollBar().value()
            self._search_active = True
        self.right_empty.hide()
        for view in self.settings_views.values():
            view.hide()
        ordered_views = [
            (name, self.settings_views[name], self.cards[name].address.text())
            for name in self._all_instrument_names_ordered()
            if name in self.settings_views
        ]
        self.search_view.set_results(query, ordered_views, self.selected_instrument)
        self.search_view.show()
        self.right_scroll.verticalScrollBar().setValue(0)

    def _jump_to_section(self, name: str, sec_code: str) -> None:
        self.search_edit.clear()
        self.open_settings(name)
        view = self.settings_views.get(name)
        if view is None:
            return
        sec = view.sections.get(sec_code)
        if sec is not None:
            sec.set_expanded(True)
            QTimer.singleShot(0, lambda s=sec: (
                not sip.isdeleted(self) and not sip.isdeleted(s)
                and self.right_scroll.ensureWidgetVisible(s, 0, 24)
            ))

    def _on_app_state_changed(self, state: Qt.ApplicationState) -> None:
        if sip.isdeleted(self):
            return
        active = state == Qt.ApplicationState.ApplicationActive
        for fw in list(self._floating_windows.values()):
            if not sip.isdeleted(fw):
                _set_mac_floating_level(fw, active)

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

    def _left_pane_global_rect(self) -> QRect:
        return QRect(self.left_frame.mapToGlobal(QPoint(0, 0)), self.left_frame.size())

    def _preview_floating_dock(self, _name: str, global_pos: QPoint) -> None:
        if self.isVisible() and not self.isMinimized() and self._left_pane_global_rect().contains(global_pos):
            local_pos = self.left_host.mapFromGlobal(global_pos)
            _, line_y = self.left_host._drop_slot(local_pos.y())
            self.left_host._update_indicator(line_y)
            self.left_host._update_auto_scroll(local_pos)
        else:
            self.left_host._stop_drag_feedback()

    def _finish_floating_dock(self, name: str, global_pos: QPoint) -> None:
        self.left_host._stop_drag_feedback()
        if self.isVisible() and not self.isMinimized() and self._left_pane_global_rect().contains(global_pos):
            local_y = self.left_host.mapFromGlobal(global_pos).y()
            slot, _ = self.left_host._drop_slot(local_y)
            self.dock_card(name, slot)
        else:
            fw = self._floating_windows.get(name)
            if fw is not None and not sip.isdeleted(fw):
                fw.raise_()
                fw.activateWindow()

    def dock_card(self, name: str, slot: int | None = None) -> None:
        """Вернуть откреплённую карточку ``name`` обратно в левую колонку (на позицию ``slot``)."""
        if name not in self.cards or name not in self._floating_windows:
            return
        card = self.cards[name]
        fw = self._floating_windows.pop(name)
        cur_order = self.left_host.card_order()
        fallback_slot = self._detached_slots.pop(name, len(cur_order))
        insert_idx = fallback_slot if slot is None else slot
        insert_idx = max(0, min(insert_idx, len(cur_order)))

        fw._closing_for_dock = True
        fw.release_card()
        card.set_floating(False)
        card.setParent(self.left_host)
        self.left_layout.insertWidget(insert_idx, card)
        card.show()
        card.refresh_geometry()

        if not sip.isdeleted(fw):
            fw.hide()
            fw.deleteLater()
        if slot is not None:
            self._save_order(self.left_host.card_order())

    def _dock_all_floating(self) -> None:
        for name in list(self._floating_windows.keys()):
            self.dock_card(name)

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

    def _save_split(self) -> None:
        if not hasattr(self, "splitter") or sip.isdeleted(self.splitter):
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
            weights = [0.45, 0.55]
        self.splitter.apply_weights(weights)

    def attach_instrument(self, name: str, win) -> None:
        """Подключить (или переподключить) реальное ``InstrumentWindow`` к слоту ``name``."""
        if name not in self.cards or name not in self.settings_views:
            raise KeyError(f"Unknown instrument slot: {name!r}")
        was_floating = name in self._floating_windows
        float_pos = self._floating_windows[name].pos() if was_floating else None
        if was_floating:
            self.dock_card(name)
        self._attached_windows[name] = win
        win.hide()
        self.cards[name].bind_window(win)
        self.settings_views[name].bind_window(
            win,
            on_show_errors=lambda n=name: self.open_settings(n),
            on_favorites_changed=lambda c=self.cards[name]: c.refresh_geometry(),
        )
        self.cards[name].refresh_geometry()
        if was_floating:
            self.detach_card(name, float_pos, activate=False)
        if self._search_active:
            self._on_search_changed(self.search_edit.text())

    def open_instrument_by_proxy(self, proxy) -> bool:
        """Показать окно инструментов и открыть настройки прибора по его ``proxy``."""
        for name, win in self._attached_windows.items():
            if not sip.isdeleted(win) and getattr(win, "proxy", None) is proxy:
                if self.isMinimized():
                    self.showNormal()
                self.show()
                self.raise_()
                self.activateWindow()
                self.open_settings(name)
                return True
        return False

    def open_settings(self, name: str) -> None:
        """Открыть настройки выбранного прибора в правой половине окна."""
        if name not in self.settings_views:
            return
        if self._search_active:
            self._search_active = False
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
            self.search_view.hide()
            self.search_view.clear_results()
        self.selected_instrument = name
        self.right_empty.hide()
        for key, card in self.cards.items():
            card.set_selected(key == name)
        for key, view in self.settings_views.items():
            view.setVisible(key == name)
        self.right_scroll.verticalScrollBar().setValue(0)
        if name in self._floating_windows:
            if self.isMinimized():
                self.showNormal()
            QTimer.singleShot(0, self._raise_floating_windows)

    def event(self, e):  # noqa: N802 - Qt
        res = super().event(e)
        if e.type() == QEvent.Type.WindowActivate and self._floating_windows and not self._mac_floating_ok:
            QTimer.singleShot(0, self._raise_floating_windows)
        return res

    def close_settings(self) -> None:
        """Закрыть настройки в правой половине окна."""
        self.selected_instrument = None
        for card in self.cards.values():
            card.set_selected(False)
        for view in self.settings_views.values():
            view.hide()
        self.right_empty.show()

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.maximize_button.set_full(False)
        else:
            self.showMaximized()
            self.maximize_button.set_full(True)

    def cleanup(self) -> None:
        self._save_split()
        app = QGuiApplication.instance()
        if app is not None:
            try:
                app.applicationStateChanged.disconnect(self._on_app_state_changed)
            except (TypeError, RuntimeError):
                pass
        self.search_view.clear_results()
        self._dock_all_floating()
        for win in list(self._attached_windows.values()):
            if not sip.isdeleted(win):
                win.cleanup()
        self._attached_windows.clear()

    def showEvent(self, event):  # noqa: N802 - Qt
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):  # noqa: N802 - Qt
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def closeEvent(self, event):  # noqa: N802 - Qt
        self._save_split()
        if self.hide_on_close:
            self.save_size()
            self._settings.sync()
            self.hide()
            event.accept()
            return
        self.search_view.clear_results()
        self._dock_all_floating()
        super().closeEvent(event)
        self._settings.sync()
