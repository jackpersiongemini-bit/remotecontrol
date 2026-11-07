"""Драйвер Rohde & Schwarz SMF100A.

По "R&S SMF100A Operating Manual" (раздел Remote Control Commands):
* ``[SOURce:]FREQuency[:CW]``, ``[SOURce:]POWer[:LEVel][:IMMediate][:AMPLitude]`` (дБм),
  ``OUTPut[:STATe]``, ``[SOURce:]MODulation[:ALL][:STATe]`` - общий выключатель модуляции;
* ``[SOURce:]POWer:ALC[:STATe] ON|OFF|AUTO`` - по умолчанию AUTO, читается как включённая;
* ``AM:DEPTh``, ``FM:DEViation``, ``PM:DEViation`` (рад), ``AM|FM|PM:SOURce INT|EXT``;
* у SMF100A ОДИН внутренний НЧ-генератор ``[SOURce:]LFOutput:FREQuency`` на AM, FM и ΦM:
  ``am.freq``, ``fm.freq``, ``pm.freq`` - одна величина (после записи перечитываются все три).
  У E8267D это три независимые частоты. Снаружи разницы нет: те же ключи;
* импульсная модуляция ``PULM:STATe``, ``PULM:SOURce INT|EXT``, ``PULM:PERiod``, ``PULM:WIDTh``
  (опция SMF-K23/K3);
* опора ``[SOURce:]ROSCillator:SOURce INT|EXT`` - можно переключать;
* ошибки ``SYSTem:ERRor?`` -> ``0,"No error"``.
"""
from __future__ import annotations

from typing import Any

from ..core.params import Param
from .rf_generator import RFGenModel, RFSignalGenerator

__all__ = ["RSSMF100A", "SMF100A_MODELS"]

#: опции частоты. Без SMF-B2 нижняя граница 1 ГГц, с B2 - 100 кГц.
SMF100A_MODELS = {
    "SMF100A-B112": RFGenModel("SMF100A-B112", 100e3, 12.75e9, -130.0, 25.0),
    "SMF100A-B120": RFGenModel("SMF100A-B120", 100e3, 20e9, -130.0, 25.0),
    "SMF100A-B122": RFGenModel("SMF100A-B122", 100e3, 22e9, -130.0, 25.0),
    "SMF100A-B131": RFGenModel("SMF100A-B131", 100e3, 31.8e9, -130.0, 25.0),
    "SMF100A-B140": RFGenModel("SMF100A-B140", 100e3, 40e9, -130.0, 25.0),
    "SMF100A-B144": RFGenModel("SMF100A-B144", 100e3, 43.5e9, -130.0, 25.0),
}

_LFO = ("LFO:FREQ {value}", "LFO:FREQ?")


class RSSMF100A(RFSignalGenerator):
    NAME = "R&S SMF100A"
    DEFAULT_MODEL = RFGenModel("SMF100A (опция не задана)", 100e3, 43.5e9, -145.0, 30.0)
    COMMANDS = {
        "rf.output": ("OUTP {value}", "OUTP?", {}),
        "rf.frequency": ("FREQ {value}", "FREQ?", {}),
        "rf.power": ("POW {value}", "POW?", {}),
        "rf.alc": ("POW:ALC {value}", "POW:ALC?", {"bool_tokens": ("ON", "OFF")}),
        "ref.source": ("ROSC:SOUR {value}", "ROSC:SOUR?", {}),
        "mod.output": ("MOD {value}", "MOD?", {}),
        "am.state": ("AM:STAT {value}", "AM:STAT?", {}),
        "am.depth": ("AM:DEPT {value}", "AM:DEPT?", {}),
        "am.source": ("AM:SOUR {value}", "AM:SOUR?", {}),
        "am.freq": (*_LFO, {"also_read": ("fm.freq", "pm.freq")}),
        "fm.state": ("FM:STAT {value}", "FM:STAT?", {"also_read": ("pm.state",)}),
        "fm.deviation": ("FM:DEV {value}", "FM:DEV?", {}),
        "fm.source": ("FM:SOUR {value}", "FM:SOUR?", {}),
        "fm.freq": (*_LFO, {"also_read": ("am.freq", "pm.freq")}),
        "pm.state": ("PM:STAT {value}", "PM:STAT?", {"also_read": ("fm.state",)}),
        "pm.deviation": ("PM:DEV {value}", "PM:DEV?", {}),
        "pm.source": ("PM:SOUR {value}", "PM:SOUR?", {}),
        "pm.freq": (*_LFO, {"also_read": ("am.freq", "fm.freq")}),
        "pulse.state": ("PULM:STAT {value}", "PULM:STAT?", {}),
        "pulse.source": ("PULM:SOUR {value}", "PULM:SOUR?", {}),
        "pulse.period": ("PULM:PER {value}", "PULM:PER?", {}),
        "pulse.width": ("PULM:WIDT {value}", "PULM:WIDT?", {}),
    }

    def read_param_raw(self, spec: Param) -> Any:
        if spec.key == "rf.alc":     # ON | OFF | AUTO (и формы ONSample/OFFTable)
            raw = self.query(spec.query).strip().upper()
            return raw in ("1", "ON", "AUTO") or raw.startswith("ONS")
        return super().read_param_raw(spec)
