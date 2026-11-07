"""Фронтенд: окна приборов на qframelesswindow.

Окна знают только ``WindowSpec`` + ``InstrumentModel`` (ключи профилей), без драйверов.
"""
from .frame import FramelessFrame, InstrumentWindow, MacroWindow
from .model import ActionSpec, DemoModel, FieldSpec, InstrumentModel, WindowSpec
from .panels import ActionBar, ModeSwitch, ParamTable, SettingsPanel

__all__ = ["FramelessFrame", "InstrumentWindow", "MacroWindow", "ActionSpec", "DemoModel", "FieldSpec",
           "InstrumentModel", "WindowSpec", "ActionBar", "ModeSwitch", "ParamTable", "SettingsPanel"]
