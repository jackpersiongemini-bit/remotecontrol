"""Токены оформления и ``render_qss`` - как в Shared.py Алексея.

Все цвета и размеры здесь; QSS пишется с ``$UI_...`` / ``${UI_...}`` и подставляется
``render_qss``. Цвета - по Parameter.py / словам Алексея; Shared.py появится - поменять значения ниже.
"""
from __future__ import annotations

from string import Template

ON, OFF = "ON", "OFF"
ON_EQV = (True, 1, "1", "ON", "On", "on")
OFF_EQV = (False, 0, "0", "OFF", "Off", "off")

TOKENS = {
    # Палитра Алексея: тёмно-серый #222 и почти белый, остальное - их оттенки.
    "UI_COLOR_BACKGROUND": "#222222",      # фон окна и полей ввода
    "UI_COLOR_PANEL": "#2b2b2b",           # фон панелей
    "UI_COLOR_CONTROL": "#2b2b2b",         # заголовок окна
    "UI_COLOR_BORDER": "#444444",
    "UI_COLOR_BORDER_LIGHT": "#8a8a8a",    # единицы измерения в полях
    "UI_COLOR_INDICATOR_FRAME": "#5a5a5a", # тонкая рамка вокруг «включённого» элемента
    "UI_COLOR_TEXT": "#f2f2f2",
    "UI_COLOR_TEXT_MUTED": "#e6e6e6",      # текст в панелях
    "UI_COLOR_INVERT": "#b8b8b8",          # инверсия
    "UI_COLOR_INVERT_HOVER": "#9a9a9a",    # инверсия при наведении (единственное место инверсии)
    "UI_COLOR_TEXT_INVERSE": "#222222",
    "UI_COLOR_ACCENT": "#f2f2f2",
    "UI_COLOR_SUCCESS": "#2e9d4f",
    "UI_COLOR_SUCCESS_HOVER": "#38b55c",
    "UI_COLOR_ERROR": "#d13438",
    "UI_COLOR_PROCESS": "#e3a21a",
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
    "UI_CONTROL_HEIGHT_PX": 28,
    "UI_COLUMN_WIDTH_PX": 84,
}
WINDOW_BAR_HEIGHT = TOKENS["UI_TITLE_BAR_ITEM_SIZE_PX"] + 14
TITLE_BAR_BUTTON_LINE_WIDTH = 1.2

globals().update(TOKENS)


def render_qss(template: str) -> str:
    return Template(template).safe_substitute({k: str(v) for k, v in TOKENS.items()})
