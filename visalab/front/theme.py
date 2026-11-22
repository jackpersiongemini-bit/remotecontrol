"""Токены оформления, светлая/тёмная темы и ``render_qss`` — как в Shared.py Алексея.

По умолчанию активна светлая тема (``light``); ``apply_theme("dark" | "light")`` мгновенно
перекрашивает все открытые окна, панели, таблицы и графики процесса без перезапуска.
"""
from __future__ import annotations

from string import Template
import sys
from typing import Callable

ON, OFF = "ON", "OFF"
ON_EQV = (True, 1, "1", "ON", "On", "on")
OFF_EQV = (False, 0, "0", "OFF", "Off", "off")

THEMES = ("light", "dark")

BASE_METRICS = {
    "UI_FONT_FAMILY": '"Segoe UI", "Inter", "DejaVu Sans", sans-serif',
    "UI_MONOSPACE_FONT_FAMILY": '"Consolas", "DejaVu Sans Mono", monospace',
    "UI_FONT_SIZE_PT": 10,
    "UI_FONT_WEIGHT_SEMIBOLD": 600,
    "UI_BORDER_WIDTH_PX": 1,
    "UI_INDICATOR_WIDTH_PX": 4,            # толстая граница-индикатор «включено / выбрано»
    "UI_BORDER_RADIUS_PX": 6,
    "UI_TITLE_BAR_ITEM_SIZE_PX": 26,
    "UI_TITLE_BAR_WIDE_ITEM_SIZE_PX": 52,
    "UI_SWITCH_MARGIN_PX": 3,
    "UI_SWITCH_WIDTH_PX": 38,              # переключатель связи в заголовке: ниже кнопок (Алексей 2026-11-14)
    "UI_SWITCH_HEIGHT_PX": 18,
    "UI_COMPACT_HEIGHT_PX": 24,            # компактные кнопки/поля в настройках графиков и всплывающих панелях
    "UI_CONTROL_HEIGHT_PX": 28,
    "UI_COLUMN_WIDTH_PX": 84,
    "UI_SCROLLBAR_WIDTH_PX": 8,            # полоса прокрутки: дорожка без фона, ручка - тонкая скруглённая
}

DARK_COLORS = {
    # Тёмная палитра Алексея: тёмно-серый #222 и почти белый, остальное - их оттенки.
    "UI_COLOR_BACKGROUND": "#222222",      # фон окна и полей ввода
    "UI_COLOR_PANEL": "#2b2b2b",           # фон панелей
    "UI_COLOR_CONTROL": "#2b2b2b",         # заголовок окна
    "UI_COLOR_BORDER": "#444444",
    "UI_COLOR_BORDER_LIGHT": "#8a8a8a",    # единицы измерения в полях
    "UI_COLOR_INDICATOR_FRAME": "#5a5a5a", # тонкая рамка вокруг «включённого» элемента
    "UI_COLOR_INDICATOR": "#888888",       # полоса-индикатор «включено / открыто» (не слепяще-белая)
    "UI_COLOR_TEXT": "#f2f2f2",
    "UI_COLOR_TEXT_MUTED": "#e6e6e6",      # текст в панелях
    "UI_COLOR_INVERT": "#949494",          # инверсия (приглушённый серый вместо слишком белого)
    "UI_COLOR_INVERT_HOVER": "#a4a4a4",    # инверсия при наведении
    "UI_COLOR_TEXT_INVERSE": "#222222",
    "UI_COLOR_ACCENT": "#f2f2f2",
    "UI_COLOR_SUCCESS": "#2e9d4f",
    "UI_COLOR_SUCCESS_HOVER": "#38b55c",
    "UI_COLOR_ERROR": "#d13438",
    "UI_COLOR_PROCESS": "#e3a21a",
}

LIGHT_COLORS = {
    # Светлая лабораторная палитра: нейтральные монохромные тона в стиле тёмной темы, но на светлом фоне.
    "UI_COLOR_BACKGROUND": "#f4f4f4",      # фон окна, графиков и полей ввода
    "UI_COLOR_PANEL": "#e8e8e8",           # фон панелей
    "UI_COLOR_CONTROL": "#e0e0e0",         # заголовок окна
    "UI_COLOR_BORDER": "#c8c8c8",          # рамки и сетка графиков
    "UI_COLOR_BORDER_LIGHT": "#6e6e6e",    # единицы измерения, подписи осей, приглушённый текст
    "UI_COLOR_INDICATOR_FRAME": "#9e9e9e", # тонкая рамка вокруг активного элемента и ручка скроллбара
    "UI_COLOR_INDICATOR": "#6e6e6e",       # полоса-индикатор «включено / открыто»
    "UI_COLOR_TEXT": "#1c1c1c",            # основной текст
    "UI_COLOR_TEXT_MUTED": "#2b2b2b",      # текст в панелях
    "UI_COLOR_INVERT": "#525252",          # инверсия (активная вкладка / чип)
    "UI_COLOR_INVERT_HOVER": "#3c3c3c",    # инверсия при наведении
    "UI_COLOR_TEXT_INVERSE": "#ffffff",    # текст на инвертированных элементах
    "UI_COLOR_ACCENT": "#1c1c1c",
    "UI_COLOR_SUCCESS": "#1f883d",
    "UI_COLOR_SUCCESS_HOVER": "#2da44e",
    "UI_COLOR_ERROR": "#cf222e",
    "UI_COLOR_PROCESS": "#c77c02",
}

DARK_TRACE_COLORS = ("#f2f2f2", "#e3a21a", "#5aa9e6", "#4fbf6f", "#e0605f", "#b48ad8", "#9a9a9a", "#d8c36a")
LIGHT_TRACE_COLORS = ("#1c1c1c", "#d97706", "#0284c7", "#15803d", "#dc2626", "#7c3aed", "#4b5563", "#a16207")

_CURRENT_THEME: str = "light"
TOKENS: dict[str, object] = {**LIGHT_COLORS, **BASE_METRICS}
#: цвета трасс на графиках (подбираются под текущую тему)
UI_TRACE_COLORS: tuple[str, ...] = LIGHT_TRACE_COLORS
WINDOW_BAR_HEIGHT = int(TOKENS["UI_TITLE_BAR_ITEM_SIZE_PX"]) + 14
TITLE_BAR_BUTTON_LINE_WIDTH = 1.2

globals().update(TOKENS)


# ------------------------------------------------------------ рецепты QSS для живой смены темы
#: правила полос прокрутки; ``$UI_...`` подставит render_qss
_SCROLLBAR_RULES = (
    ("QScrollBar:vertical", "background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;"
                            " width: ${UI_SCROLLBAR_WIDTH_PX}px;"),
    ("QScrollBar:horizontal", "background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;"
                              " height: ${UI_SCROLLBAR_WIDTH_PX}px;"),
    ("QScrollBar::handle:vertical", "background: $UI_COLOR_INDICATOR_FRAME; border: none; margin: 1px;"
                                    " border-radius: ${SCROLLBAR_HANDLE_RADIUS_PX}px; min-height: 24px;"),
    ("QScrollBar::handle:horizontal", "background: $UI_COLOR_INDICATOR_FRAME; border: none; margin: 1px;"
                                      " border-radius: ${SCROLLBAR_HANDLE_RADIUS_PX}px; min-width: 24px;"),
    ("QScrollBar::handle:hover", "background: $UI_COLOR_BORDER_LIGHT;"),
    ("QScrollBar::handle:pressed", "background: $UI_COLOR_INVERT;"),
    ("QScrollBar::add-line, QScrollBar::sub-line", "width: 0; height: 0; border: none; background: none;"),
    ("QScrollBar::up-arrow, QScrollBar::down-arrow, QScrollBar::left-arrow, QScrollBar::right-arrow",
     "width: 0; height: 0; border: none; background: none;"),
    ("QScrollBar::add-page, QScrollBar::sub-page", "background: none;"),
    ("QAbstractScrollArea::corner", "background: transparent; border: none;"),
)

#: правила чекбоксов: кружок, при выборе внутри ещё один кружок
_CHECKBOX_RULES = (
    ("QCheckBox", "spacing: 6px; padding: 0; color: $UI_COLOR_TEXT_MUTED; background: transparent; border: none;"),
    ("QCheckBox:disabled", "color: $UI_COLOR_BORDER_LIGHT;"),
    ("QCheckBox::indicator",
     "width: 14px; height: 14px; border-radius: 8px;"
     " border: 1px solid $UI_COLOR_BORDER_LIGHT; background: $UI_COLOR_BACKGROUND;"),
    ("QCheckBox::indicator:hover", "border-color: $UI_COLOR_TEXT;"),
    ("QCheckBox::indicator:checked",
     "border-color: $UI_COLOR_TEXT;"
     " background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
     " stop:0 $UI_COLOR_TEXT, stop:0.46 $UI_COLOR_TEXT,"
     " stop:0.56 $UI_COLOR_BACKGROUND, stop:1 $UI_COLOR_BACKGROUND);"),
    ("QCheckBox::indicator:checked:hover",
     "border-color: $UI_COLOR_INVERT_HOVER;"
     " background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
     " stop:0 $UI_COLOR_TEXT, stop:0.46 $UI_COLOR_TEXT,"
     " stop:0.56 $UI_COLOR_BACKGROUND, stop:1 $UI_COLOR_BACKGROUND);"),
    ("QCheckBox::indicator:disabled",
     "border-color: $UI_COLOR_BORDER; background: $UI_COLOR_BACKGROUND;"),
    ("QCheckBox::indicator:checked:disabled",
     "border-color: $UI_COLOR_BORDER;"
     " background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
     " stop:0 $UI_COLOR_BORDER_LIGHT, stop:0.46 $UI_COLOR_BORDER_LIGHT,"
     " stop:0.56 $UI_COLOR_BACKGROUND, stop:1 $UI_COLOR_BACKGROUND);"),
)

_QSS_REGISTRY: dict[str, tuple[tuple[str, str], ...]] = {}
_THEME_LISTENERS: list[Callable[[str], None]] = []


def _eval_part(kind: str, arg: str) -> str:
    if kind == "lit":
        return arg
    if kind == "tpl":
        return Template(arg).safe_substitute({k: str(v) for k, v in TOKENS.items()})
    if kind == "sb":
        radius = (int(TOKENS["UI_SCROLLBAR_WIDTH_PX"]) - 2) // 2
        lines = []
        for selector, body in _SCROLLBAR_RULES:
            scoped = ", ".join(arg + part.strip() for part in selector.split(","))
            lines.append(f"{scoped} {{ {body} }}")
        return Template("\n".join(lines)).safe_substitute(
            {**{k: str(v) for k, v in TOKENS.items()}, "SCROLLBAR_HANDLE_RADIUS_PX": str(radius)})
    if kind == "cb":
        lines = []
        for selector, body in _CHECKBOX_RULES:
            scoped = ", ".join(arg + part.strip() for part in selector.split(","))
            lines.append(f"{scoped} {{ {body} }}")
        return Template("\n".join(lines)).safe_substitute({k: str(v) for k, v in TOKENS.items()})
    return arg


def _eval_parts(parts: tuple[tuple[str, str], ...]) -> str:
    return "".join(_eval_part(k, a) for k, a in parts)


class _ThemedQSS(str):
    """Строка QSS, помнящая исходные шаблоны — при смене темы перерендеривается заново."""

    _parts: tuple[tuple[str, str], ...]

    def __new__(cls, text: str, parts: tuple[tuple[str, str], ...]):
        obj = super().__new__(cls, text)
        obj._parts = parts
        _QSS_REGISTRY[str(obj)] = parts
        return obj

    def __add__(self, other: str) -> "_ThemedQSS":
        if isinstance(other, _ThemedQSS):
            parts = self._parts + other._parts
        elif isinstance(other, str):
            parts = self._parts + (("lit", other),)
        else:
            return NotImplemented
        return _ThemedQSS(super().__add__(other), parts)

    def __radd__(self, other: str) -> "_ThemedQSS":
        if isinstance(other, str):
            parts = (("lit", other),) + self._parts
            return _ThemedQSS(other + str(self), parts)
        return NotImplemented

    def rerender(self) -> "_ThemedQSS":
        return _ThemedQSS(_eval_parts(self._parts), self._parts)


def render_qss(template: str) -> str:
    parts = (("tpl", template),)
    return _ThemedQSS(_eval_parts(parts), parts)


def scrollbar_qss(scope: str = "") -> str:
    """Полосы прокрутки в цветах темы (без стрелок, дорожка прозрачная, ручка в цветах рамки)."""
    parts = (("sb", scope),)
    return _ThemedQSS(_eval_parts(parts), parts)


def checkbox_qss(scope: str = "") -> str:
    """Чекбоксы в стиле «кружок с внутренним кружком при выборе»."""
    parts = (("cb", scope),)
    return _ThemedQSS(_eval_parts(parts), parts)


def mono_font(pt: float | None = None, *, bold: bool = False):
    """Стандартный моноширинный шрифт интерфейса (по умолчанию ``UI_FONT_SIZE_PT`` = 10pt)."""
    from PyQt6.QtGui import QFont
    family = str(TOKENS["UI_MONOSPACE_FONT_FAMILY"]).split(",")[0].strip(' "')
    f = QFont(family)
    f.setPointSizeF(float(TOKENS["UI_FONT_SIZE_PT"]) if pt is None else float(pt))
    if bold:
        f.setBold(True)
    return f


def button_qss(scope: str = "", *, height_token: str = "UI_COMPACT_HEIGHT_PX", border_token: str = "UI_COLOR_BORDER") -> str:
    """Стандартные кнопки в едином стиле темы (высота, скругление, инверсия при наведении/выборе)."""
    s = scope.strip()
    sel = f"{s} " if s else ""
    return render_qss(f"""
        {sel}QPushButton {{
            background-color: $UI_COLOR_BACKGROUND;
            border: ${{UI_BORDER_WIDTH_PX}}px solid ${border_token};
            border-radius: ${{UI_BORDER_RADIUS_PX}}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${{UI_FONT_SIZE_PT}}pt;
            padding: 2px 8px;
            min-height: ${{{height_token}}}px;
            color: $UI_COLOR_TEXT_MUTED;
        }}
        {sel}QPushButton:hover {{
            background-color: $UI_COLOR_INVERT_HOVER;
            border-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }}
        {sel}QPushButton:checked {{
            background-color: $UI_COLOR_INVERT;
            border-color: $UI_COLOR_INVERT;
            color: $UI_COLOR_TEXT_INVERSE;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        }}
        {sel}QPushButton:checked:hover {{
            background-color: $UI_COLOR_INVERT_HOVER;
            border-color: $UI_COLOR_INVERT_HOVER;
            color: $UI_COLOR_TEXT_INVERSE;
        }}
        {sel}QPushButton:disabled {{
            color: $UI_COLOR_BORDER;
            border-color: $UI_COLOR_BORDER;
        }}
    """)


def input_qss(scope: str = "", *, height_token: str = "UI_COMPACT_HEIGHT_PX") -> str:
    """Стандартные поля ввода ``QLineEdit`` (фокус, ошибка ``[invalid="true"]``, disabled)."""
    s = scope.strip()
    sel = f"{s} " if s else ""
    return render_qss(f"""
        {sel}QLineEdit {{
            background-color: $UI_COLOR_BACKGROUND;
            border: ${{UI_BORDER_WIDTH_PX}}px solid $UI_COLOR_BORDER;
            border-radius: ${{UI_BORDER_RADIUS_PX}}px;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${{UI_FONT_SIZE_PT}}pt;
            padding: 2px 6px;
            min-height: ${{{height_token}}}px;
            color: $UI_COLOR_TEXT;
        }}
        {sel}QLineEdit:focus {{ border-color: $UI_COLOR_TEXT_MUTED; }}
        {sel}QLineEdit:disabled {{ color: $UI_COLOR_BORDER; }}
        {sel}QLineEdit[invalid="true"] {{ border-color: $UI_COLOR_ERROR; }}
    """)


def table_qss(scope: str = "") -> str:
    """Стандартная таблица ``QTableWidget`` с горизонтальными разделителями."""
    s = scope.strip()
    sel = f"{s} " if s else ""
    return render_qss(f"""
        {sel}QTableWidget, {sel}QTableWidget QWidget {{
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_TEXT;
        }}
        {sel}QTableWidget {{
            border: ${{UI_BORDER_WIDTH_PX}}px solid $UI_COLOR_BORDER;
            border-radius: ${{UI_BORDER_RADIUS_PX}}px;
            gridline-color: transparent;
        }}
        {sel}QTableWidget::item {{
            border: none;
            border-bottom: ${{UI_BORDER_WIDTH_PX}}px solid $UI_COLOR_BORDER;
            padding: 2px 8px;
        }}
        {sel}QTableWidget QHeaderView::section {{
            background-color: $UI_COLOR_BACKGROUND;
            color: $UI_COLOR_BORDER_LIGHT;
            border: none;
            border-bottom: ${{UI_BORDER_WIDTH_PX}}px solid $UI_COLOR_BORDER;
            padding: 3px 8px;
            font-weight: normal;
        }}
        {sel}QTableWidget QTableCornerButton::section {{ background: transparent; border: none; }}
    """)


def current_theme() -> str:
    """Текущая активная тема: ``'light'`` или ``'dark'``."""
    return _CURRENT_THEME


def on_theme_changed(callback: Callable[[str], None]) -> None:
    if callback not in _THEME_LISTENERS:
        _THEME_LISTENERS.append(callback)


def apply_theme(name: str) -> str:
    """Переключить тему (``'light'`` | ``'dark'``) и мгновенно обновить все открытые виджеты."""
    global _CURRENT_THEME, UI_TRACE_COLORS
    norm = "dark" if str(name or "").strip().lower() == "dark" else "light"
    changed = norm != _CURRENT_THEME
    _CURRENT_THEME = norm
    palette = DARK_COLORS if norm == "dark" else LIGHT_COLORS
    traces = DARK_TRACE_COLORS if norm == "dark" else LIGHT_TRACE_COLORS

    TOKENS.update(palette)
    UI_TRACE_COLORS = traces
    globals().update(TOKENS)

    # Обновить импортированные токены и классовые STYLE_SHEET / STYLE во всех загруженных модулях visalab.front.*
    color_keys = set(palette)
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith("visalab.front"):
            continue
        mod_dict = getattr(mod, "__dict__", None)
        if not isinstance(mod_dict, dict):
            continue
        for k in color_keys:
            if k in mod_dict:
                mod_dict[k] = palette[k]
        if "UI_TRACE_COLORS" in mod_dict:
            mod_dict["UI_TRACE_COLORS"] = traces
        for attr_name, obj in list(mod_dict.items()):
            if isinstance(obj, _ThemedQSS):
                mod_dict[attr_name] = obj.rerender()
            elif isinstance(obj, type):
                for attr in ("STYLE_SHEET", "STYLE"):
                    val = obj.__dict__.get(attr)
                    if isinstance(val, _ThemedQSS):
                        setattr(obj, attr, val.rerender())

    if not changed:
        return _CURRENT_THEME

    # Перекрасить все живые виджеты Qt
    try:
        from PyQt6 import sip
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return _CURRENT_THEME

    app = QApplication.instance()
    if app is not None:
        all_widgets = list(app.allWidgets())
        for w in all_widgets:
            if sip.isdeleted(w):
                continue
            ss = w.styleSheet()
            if ss and ss in _QSS_REGISTRY:
                parts = _QSS_REGISTRY[ss]
                new_ss = _ThemedQSS(_eval_parts(parts), parts)
                w.setStyleSheet(new_ss)
        for w in all_widgets:
            if sip.isdeleted(w):
                continue
            st = w.style()
            if st is not None:
                st.unpolish(w)
                st.polish(w)
            if hasattr(w, "refresh_theme"):
                try:
                    w.refresh_theme()
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(w, "_cache") and hasattr(w, "_cache_key"):
                w._cache = None
            w.update()

    for cb in list(_THEME_LISTENERS):
        try:
            cb(_CURRENT_THEME)
        except Exception:  # noqa: BLE001
            pass

    return _CURRENT_THEME
