"""Драйвер Keysight (Agilent) PSG E8267D / E8257D.

По "E8257D/E8267D/E8663D PSG Signal Generators SCPI Command Reference" (E8251-90356):
* ``:FREQuency[:CW]``, ``:POWer[:LEVel][:IMMediate][:AMPLitude]`` (дБм), ``:OUTPut[:STATe]``,
  ``:OUTPut:MODulation[:STATe]`` - общий выключатель модуляции, ``:POWer:ALC[:STATe]``;
* ``:AM[1]:DEPTh``, ``:AM[1]:SOURce INT|EXT1|EXT2``, ``:AM[1]:INTernal[1]:FREQuency``;
  то же для ``:FM[1]:DEViation`` и ``:PM[1]:DEViation`` (рад);
* FM и ΦM используют общий тракт и одновременно не включаются;
* импульсная модуляция ``:PULM:STATe``, ``:PULM:SOURce INT|EXT``,
  ``:PULM:INTernal:PERiod`` / ``:PULM:INTernal:PWIDth`` (опции UNU/UNW);
* опора ``:ROSCillator:SOURce?`` - только чтение (выбор автоматический);
* ошибки ``:SYSTem:ERRor?`` -> ``+0,"No error"``.

Стандартные токены профиля -> токены PSG: ``EXT`` -> ``EXT1`` (``EXT2`` читается как ``EXT``).
"""
from __future__ import annotations

from .rf_generator import RFGenModel, RFSignalGenerator

__all__ = ["KeysightE8267D", "E8267D_MODELS"]

#: опции частоты: 520 - 20 ГГц, 532 - 31.8 ГГц, 544 - 44 ГГц. Нижняя граница 250 кГц.
E8267D_MODELS = {
    "E8267D-520": RFGenModel("E8267D-520", 250e3, 20e9, -135.0, 25.0),
    "E8267D-532": RFGenModel("E8267D-532", 250e3, 31.8e9, -135.0, 25.0),
    "E8267D-544": RFGenModel("E8267D-544", 250e3, 44e9, -135.0, 25.0),
}

_EXT = {"enum_map": (("EXT", "EXT1"), ("EXT", "EXT2"))}


class KeysightE8267D(RFSignalGenerator):
    NAME = "Keysight E8267D"
    DEFAULT_MODEL = RFGenModel("E8267D (опция не задана)", 250e3, 44e9, -135.0, 30.0)
    COMMANDS = {
        "rf.output": (":OUTP {value}", ":OUTP?", {}),
        "rf.frequency": (":FREQ {value}", ":FREQ?", {}),
        "rf.power": (":POW {value}", ":POW?", {}),
        "rf.alc": (":POW:ALC {value}", ":POW:ALC?", {}),
        "ref.source": (None, ":ROSC:SOUR?", {}),
        "mod.output": (":OUTP:MOD {value}", ":OUTP:MOD?", {}),
        "am.state": (":AM:STAT {value}", ":AM:STAT?", {}),
        "am.depth": (":AM:DEPT {value}", ":AM:DEPT?", {}),
        "am.source": (":AM:SOUR {value}", ":AM:SOUR?", _EXT),
        "am.freq": (":AM:INT:FREQ {value}", ":AM:INT:FREQ?", {}),
        "fm.state": (":FM:STAT {value}", ":FM:STAT?", {"also_read": ("pm.state",)}),
        "fm.deviation": (":FM:DEV {value}", ":FM:DEV?", {}),
        "fm.source": (":FM:SOUR {value}", ":FM:SOUR?", _EXT),
        "fm.freq": (":FM:INT:FREQ {value}", ":FM:INT:FREQ?", {}),
        "pm.state": (":PM:STAT {value}", ":PM:STAT?", {"also_read": ("fm.state",)}),
        "pm.deviation": (":PM:DEV {value}", ":PM:DEV?", {}),
        "pm.source": (":PM:SOUR {value}", ":PM:SOUR?", _EXT),
        "pm.freq": (":PM:INT:FREQ {value}", ":PM:INT:FREQ?", {}),
        "pulse.state": (":PULM:STAT {value}", ":PULM:STAT?", {}),
        "pulse.source": (":PULM:SOUR {value}", ":PULM:SOUR?", {}),
        "pulse.period": (":PULM:INT:PER {value}", ":PULM:INT:PER?", {}),
        "pulse.width": (":PULM:INT:PWID {value}", ":PULM:INT:PWID?", {}),
    }
