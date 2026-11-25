"""Настраиваемое «избранное» для окон приборов (``InstrumentWindow``).

Позволяет для любого прибора выбирать, какие строки отображаются в верхней панели
(``preset_panel``): можно скрывать стандартные строки избранного и закреплять любые
параметры со вкладок прибора. Закреплённые виджеты привязываются к тем же ключам
прокси и синхронизируются с вкладками в реальном времени. Выбор сохраняется в
``QSettings`` отдельно для каждого прибора.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QSettings, Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .params import PLabel, PLineEdit, PNLabel, PSButton, PSpinBox, PSpinEdit, PanelLine, PanelTable, _Bound
from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_INVERT_HOVER, UI_COLOR_PANEL, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_COLUMN_WIDTH_PX, UI_FONT_SIZE_PT, checkbox_qss, render_qss, scrollbar_qss)

__all__ = ["FavoriteItem", "FavoritesManager", "FavoritesPopup", "clone_bound_widget"]

_TOP_WIDGET_LABELS = {
    "PowerReadout": "READOUT",
    "SourceGrid": "CHANNELS",
}


@dataclass(frozen=True)
class FavoriteItem:
    """Один пункт списка избранного: либо встроенная строка ``top_lines``, либо строка со вкладки."""

    item_id: str
    section: str
    label: str
    tip: str
    is_default_top: bool
    top_index: int = -1
    source_line: PanelLine | None = None
    keys: tuple[str, ...] = ()


def _top_line_label(line: PanelLine, idx: int) -> str:
    name = line._name.text().strip()
    if name:
        return name
    if len(line.widgets) == 1:
        w = line.widgets[0]
        if isinstance(w, QPushButton) and w.text().strip():
            return w.text().strip()
        cls_name = type(w).__name__
        if cls_name in _TOP_WIDGET_LABELS:
            return _TOP_WIDGET_LABELS[cls_name]
    return f"ROW {idx + 1}"


def _is_cloneable_bound(w: QWidget, proxy) -> bool:
    if not isinstance(w, _Bound) or getattr(w, "proxy", None) is not proxy or not hasattr(w, "key"):
        return False
    return isinstance(w, (PSpinBox, PNLabel, PLineEdit, PSButton, PLabel))


def clone_bound_widget(orig: QWidget, proxy) -> QWidget | None:
    """Создать синхронизированную копию привязанного к ``proxy`` виджета."""
    key = getattr(orig, "key", None)
    if not key or not proxy.supports(key):
        return None
    clone: QWidget | None = None
    if isinstance(orig, PSpinBox):
        prefixes = getattr(orig, "prefixes", None)
        clone = PSpinBox(proxy, key, prefixes=prefixes)
        step = getattr(orig, "step_setting", None)
        if step is not None:
            clone.set_step(step, emit=False)
    elif isinstance(orig, PSpinEdit):
        clone = PSpinEdit(proxy, key, unit=getattr(orig, "unit", None), digits=getattr(orig, "digits", 6),
                          prefixes=getattr(orig, "prefixes", None))
        step = getattr(orig, "step_setting", None)
        if step is not None:
            clone.set_step(step, emit=False)
    elif isinstance(orig, PNLabel):
        clone = PNLabel(proxy, key, unit=getattr(orig, "unit", None), digits=getattr(orig, "digits", 6))
    elif isinstance(orig, PLineEdit):
        clone = PLineEdit(proxy, key, unit=getattr(orig, "unit", None), digits=getattr(orig, "digits", 6))
    elif isinstance(orig, PSButton):
        pairs = list(zip(getattr(orig, "_names", ()), getattr(orig, "_data", ())))
        if not pairs:
            return None
        clone = PSButton(proxy, key, pairs)
    elif isinstance(orig, PLabel):
        clone = PLabel(proxy, key)
    if clone is None:
        return None
    if getattr(orig, "unit", "") and hasattr(clone, "prefix") and hasattr(clone, "_update_unit"):
        pref = getattr(orig, "prefix", "")
        if pref in getattr(clone, "prefixes", ()):
            clone.prefix = pref
            clone.pinned = bool(getattr(orig, "pinned", False))
            clone._update_unit()
    custom_tip = getattr(orig, "_custom_tip", "")
    if custom_tip and hasattr(clone, "set_custom_tip"):
        clone.set_custom_tip(custom_tip)
    elif orig.toolTip():
        clone.setToolTip(orig.toolTip())
    cur = proxy.value(key)
    if cur is not None and hasattr(clone, "_on_value"):
        clone._on_value(cur)
    elif getattr(orig, "_saved_value", None) is not None and hasattr(clone, "show_value"):
        clone._saved_value = orig._saved_value
        clone.show_value(orig._saved_value)
    return clone


class FavoritesManager:
    """Управляет составом верхней панели избранного конкретного ``InstrumentWindow``."""

    def __init__(self, window, settings: QSettings, prefix: str):
        self.window = window
        self.settings = settings
        self.settings_key = f"{prefix}/FAVORITES"
        self.items: list[FavoriteItem] = []
        self._by_id: dict[str, FavoriteItem] = {}
        self.hidden_top: set[str] = set()
        self.pinned: list[str] = []
        self._pinned_widgets: list[QWidget] = []
        self._pinned_lines: list[PanelLine] = []
        self._extra_host: QWidget | None = None
        self._extra_layout: QVBoxLayout | None = None
        self._empty_label: QLabel | None = None

    def discover(self) -> None:
        """Собрать все доступные пункты избранного: ``top_lines`` и параметры со вкладок."""
        self.items.clear()
        self._by_id.clear()
        win = self.window

        # 1. Встроенные строки избранного
        for idx, line in enumerate(win.top_lines):
            label = _top_line_label(line, idx)
            item_id = f"top:{idx}:{label}"
            tip = line._name.toolTip() or (line.widgets[0].toolTip() if line.widgets else "") or f"Show {label} in favorites"
            item = FavoriteItem(
                item_id=item_id,
                section="DEFAULT",
                label=label,
                tip=tip,
                is_default_top=True,
                top_index=idx,
                source_line=line,
            )
            self.items.append(item)
            self._by_id[item_id] = item

        # 2. Параметры со всех вкладок прибора (кроме вкладки SET)
        seen_ids: set[str] = set(self._by_id)
        for tab_title, _states, table in win.page_specs:
            if not isinstance(table, PanelTable):
                continue
            for line in table.lines:
                bound = [w for w in line.widgets if _is_cloneable_bound(w, win.proxy)]
                if not bound:
                    continue
                keys = tuple(w.key for w in bound)
                label = line._name.text().strip() or keys[0].upper()
                item_id = f"param:{tab_title}:{label}:{','.join(keys)}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                tip = line._name.toolTip() or bound[0].toolTip() or f"Show {tab_title} / {label} in favorites"
                item = FavoriteItem(
                    item_id=item_id,
                    section=tab_title,
                    label=label,
                    tip=tip,
                    is_default_top=False,
                    source_line=line,
                    keys=keys,
                )
                self.items.append(item)
                self._by_id[item_id] = item

    def attach_to_panel(self) -> None:
        """Добавить в ``preset_panel`` контейнер для закреплённых параметров и заглушку."""
        panel = self.window.preset_panel
        lay = panel.layout()
        if lay is None:
            return
        self._extra_host = QWidget(panel)
        self._extra_layout = QVBoxLayout(self._extra_host)
        self._extra_layout.setContentsMargins(0, 0, 0, 0)
        self._extra_layout.setSpacing(4)
        self._extra_host.hide()

        self._empty_label = QLabel("Favorites empty — configure in SET (★)", panel)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(render_qss("color: $UI_COLOR_BORDER_LIGHT; padding: 4px 0;"))
        self._empty_label.hide()

        lay.addWidget(self._extra_host)
        lay.addWidget(self._empty_label)

    def load(self) -> None:
        """Загрузить сохранённый выбор избранного из ``QSettings`` и применить."""
        raw = self.settings.value(self.settings_key, "", type=str)
        self.hidden_top.clear()
        self.pinned.clear()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    hidden = data.get("hidden_top", [])
                    pinned = data.get("pinned", [])
                    if isinstance(hidden, list):
                        self.hidden_top = {str(x) for x in hidden if str(x) in self._by_id}
                    if isinstance(pinned, list):
                        self.pinned = [str(x) for x in pinned if str(x) in self._by_id and not self._by_id[str(x)].is_default_top]
            except (TypeError, ValueError):
                pass
        self.apply(save=False, fit=False)

    def save(self) -> None:
        if not self.hidden_top and not self.pinned:
            self.settings.remove(self.settings_key)
        else:
            payload = {"hidden_top": sorted(self.hidden_top), "pinned": list(self.pinned)}
            self.settings.setValue(self.settings_key, json.dumps(payload))

    def is_customized(self) -> bool:
        return bool(self.hidden_top or self.pinned)

    def is_enabled(self, item_id: str) -> bool:
        item = self._by_id.get(item_id)
        if item is None:
            return False
        if item.is_default_top:
            return item_id not in self.hidden_top
        return item_id in self.pinned

    def set_enabled(self, item_id: str, enabled: bool) -> None:
        item = self._by_id.get(item_id)
        if item is None:
            return
        if item.is_default_top:
            if enabled:
                self.hidden_top.discard(item_id)
            else:
                self.hidden_top.add(item_id)
        else:
            if enabled and item_id not in self.pinned:
                self.pinned.append(item_id)
            elif not enabled and item_id in self.pinned:
                self.pinned.remove(item_id)
        self.apply(save=True, fit=True)

    def reset(self) -> None:
        self.hidden_top.clear()
        self.pinned.clear()
        self.apply(save=True, fit=True)

    def pinned_lines(self) -> list[PanelLine]:
        """Текущие закреплённые строки параметров в ``preset_panel``."""
        return [ln for ln in self._pinned_lines if not sip.isdeleted(ln)]

    def cleanup(self) -> None:
        self._clear_pinned()

    def _clear_pinned(self) -> None:
        for w in self._pinned_widgets:
            if not sip.isdeleted(w) and hasattr(w, "unbind"):
                w.unbind()
        self._pinned_widgets.clear()
        self._pinned_lines.clear()
        if self._extra_layout is not None and not sip.isdeleted(self._extra_layout):
            while self._extra_layout.count():
                it = self._extra_layout.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()

    def apply(self, *, save: bool = False, fit: bool = True) -> None:
        win = self.window
        # 1. Видимость встроенных строк top_lines
        visible_top = 0
        for item in self.items:
            if item.is_default_top and item.source_line is not None and not sip.isdeleted(item.source_line):
                show = item.item_id not in self.hidden_top
                item.source_line.setVisible(show)
                if show:
                    visible_top += 1

        # 2. Пересборка закреплённых строк со вкладок
        self._clear_pinned()
        built_lines: list[PanelLine] = []
        for item_id in self.pinned:
            item = self._by_id.get(item_id)
            if item is None or item.source_line is None:
                continue
            clones: list[QWidget] = []
            for orig in item.source_line.widgets:
                if _is_cloneable_bound(orig, win.proxy):
                    c = clone_bound_widget(orig, win.proxy)
                    if c is not None:
                        clones.append(c)
                        self._pinned_widgets.append(c)
            if not clones:
                continue
            pline = PanelLine(item.label, *clones)
            if item.source_line._name.toolTip():
                pline.set_help(item.source_line._name.toolTip())
            built_lines.append(pline)
        self._pinned_lines = list(built_lines)

        if self._extra_host is not None and self._extra_layout is not None:
            self._extra_layout.setContentsMargins(0, 8 if visible_top > 0 else 0, 0, 0)
            if built_lines:
                extra_table = PanelTable(*built_lines)
                extra_table.build(UI_COLUMN_WIDTH_PX)
                # Выровнять ширину подписей с видимыми встроенными строками
                top_table = getattr(win.preset_panel, "table", None)
                label_w = 0
                if isinstance(top_table, PanelTable):
                    for ln in top_table.first_column_lines():
                        if not ln.isHidden():
                            label_w = max(label_w, ln.label_hint())
                for ln in extra_table.first_column_lines():
                    label_w = max(label_w, ln.label_hint())
                if label_w > 0:
                    for ln in extra_table.first_column_lines():
                        ln.set_label_width(label_w)
                self._extra_layout.addWidget(extra_table)
                self._extra_host.show()
            else:
                self._extra_host.hide()

        # Если скрыты все встроенные строки — спрятать пустую таблицу top_lines, чтобы не занимала место
        top_table = getattr(win.preset_panel, "table", None)
        if top_table is not None and not sip.isdeleted(top_table):
            top_table.setVisible(visible_top > 0)

        if self._empty_label is not None and not sip.isdeleted(self._empty_label):
            self._empty_label.setVisible(visible_top == 0 and len(built_lines) == 0)

        if hasattr(win, "fav_button") and not sip.isdeleted(win.fav_button):
            win.fav_button.set_customized(self.is_customized())

        if save:
            self.save()
        if fit and not sip.isdeleted(win):
            win._fit_height()


class FavoritesPopup(QFrame):
    """Всплывающая панель выбора элементов «избранного» для окна прибора."""

    STYLE_SHEET = render_qss("""
        FavoritesPopup {
            background-color: $UI_COLOR_PANEL;
            border: 1px solid $UI_COLOR_BORDER_LIGHT;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        FavoritesPopup QWidget#favBody, FavoritesPopup QScrollArea#favScroll {
            background: transparent;
            border: none;
        }
        FavoritesPopup QLabel#favTitle {
            color: $UI_COLOR_TEXT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            background: transparent;
        }
        FavoritesPopup QLabel#favSection {
            color: $UI_COLOR_BORDER_LIGHT;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            padding-top: 6px;
            background: transparent;
        }
        FavoritesPopup QPushButton#favReset {
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT_MUTED;
            border: 1px solid $UI_COLOR_BORDER;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            padding: 2px 8px;
            min-height: ${UI_COMPACT_HEIGHT_PX}px;
            max-height: ${UI_COMPACT_HEIGHT_PX}px;
        }
        FavoritesPopup QPushButton#favReset:hover {
            background-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            border-color: $UI_COLOR_INVERT_HOVER;
        }
        FavoritesPopup QCheckBox {
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            color: $UI_COLOR_TEXT;
            padding: 2px 0;
        }
    """) + scrollbar_qss("FavoritesPopup ") + "\n" + checkbox_qss("FavoritesPopup ")

    def __init__(self, manager: FavoritesManager, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.manager = manager
        self.checkboxes: dict[str, QCheckBox] = {}
        self.setStyleSheet(self.STYLE_SHEET)
        self.setMinimumWidth(250)
        self.setMaximumWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        title = QLabel("FAVORITES")
        title.setObjectName("favTitle")
        self.reset_button = QPushButton("RESET")
        self.reset_button.setObjectName("favReset")
        self.reset_button.setToolTip("Restore default favorites for this instrument")
        self.reset_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.clicked.connect(self._on_reset)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.reset_button)
        root.addLayout(head)

        body = QWidget()
        body.setObjectName("favBody")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(2, 0, 6, 4)
        bl.setSpacing(3)

        sections: list[tuple[str, list[FavoriteItem]]] = []
        by_sec: dict[str, list[FavoriteItem]] = {}
        for item in manager.items:
            if item.section not in by_sec:
                by_sec[item.section] = []
                sections.append((item.section, by_sec[item.section]))
            by_sec[item.section].append(item)

        for sec_name, sec_items in sections:
            lbl = QLabel(sec_name)
            lbl.setObjectName("favSection")
            bl.addWidget(lbl)
            for item in sec_items:
                cb = QCheckBox(item.label)
                cb.setCursor(Qt.CursorShape.PointingHandCursor)
                cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                cb.setToolTip(item.tip)
                cb.setChecked(manager.is_enabled(item.item_id))
                cb.toggled.connect(lambda checked, iid=item.item_id: self._on_toggled(iid, checked))
                self.checkboxes[item.item_id] = cb
                bl.addWidget(cb)

        bl.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("favScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        scroll.setMaximumHeight(340)
        root.addWidget(scroll, 1)

    def _on_toggled(self, item_id: str, checked: bool) -> None:
        self.manager.set_enabled(item_id, checked)

    def _on_reset(self) -> None:
        self.manager.reset()
        for item_id, cb in self.checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(self.manager.is_enabled(item_id))
            cb.blockSignals(False)

    def show_under(self, anchor: QWidget) -> None:
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 4))
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left() + 4, min(pos.x(), avail.right() - self.width() - 4))
            y = max(avail.top() + 4, min(pos.y(), avail.bottom() - self.height() - 4))
            pos = QPoint(x, y)
        self.move(pos)
        self.show()
        self.raise_()
