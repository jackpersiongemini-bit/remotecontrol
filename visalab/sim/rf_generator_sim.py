"""Симуляторы СВЧ-генераторов: Keysight PSG E8267D и R&S SMF100A.

Общая модель состояния, разный «диалект»: шаблоны команд, формат чисел в ответах,
токены источников, связь частот НЧ-генератора, поведение ALC. Именно эти различия
драйверы прячут за профилем ``signal_generator``.
"""
from __future__ import annotations

from typing import Callable

from .base import ESR_EXE, SimError, SimInstrument

__all__ = ["RFGenSim", "KeysightE8267DSim", "RSSMF100ASim"]


class RFGenSim(SimInstrument):
    #: (шаблон команды из мануала, имя ячейки состояния, тип: f/b/e/ro)
    TABLE: tuple[tuple[str, str, str], ...] = ()
    DEFAULTS: dict = {}
    LIMITS: dict[str, tuple[float, float]] = {}
    #: допустимые токены ENUM (вход -> что хранить/возвращать)
    ENUM_TOKENS: dict[str, dict[str, str]] = {}
    #: взаимоисключающие (включение одного выключает другое)
    EXCLUSIVE = (("fm", "pm"),)

    def reset(self) -> None:
        self.s = dict(self.DEFAULTS)

    def fmt_num(self, v: float) -> str:
        return repr(float(v))

    def register(self) -> None:
        self.on("SYSTem:ERRor[:NEXT]", get=self._err)
        for pattern, cell, typ in self.TABLE:
            self.on(pattern, None if typ == "ro" else self._setter(cell, typ), self._getter(cell, typ))

    def _err(self, a, g):
        code, msg = self.errors.popleft() if self.errors else (0, "No error")
        return f'{code:+d},"{msg}"' if self.SIGNED_ERR else f'{code},"{msg}"'

    SIGNED_ERR = False

    def _setter(self, cell: str, typ: str) -> Callable:
        def fn(args: str, groups: tuple):
            if typ == "f":
                lo, hi = self.LIMITS.get(cell, (-1e30, 1e30))
                self.s[cell] = self.num(args, lo, hi)
            elif typ == "b":
                on = self.boolean(args)
                self.s[cell] = on
                if on:
                    for a, b in self.EXCLUSIVE:
                        if cell == f"{a}.state":
                            self.s[f"{b}.state"] = False
                        elif cell == f"{b}.state":
                            self.s[f"{a}.state"] = False
            else:
                token = args.strip().upper()
                table = self.ENUM_TOKENS.get(cell, {})
                if token not in table:
                    raise SimError(-224, "Illegal parameter value", ESR_EXE)
                self.s[cell] = table[token]
        return fn

    def _getter(self, cell: str, typ: str) -> Callable:
        def fn(args: str, groups: tuple):
            v = self.s[cell]
            if typ == "f":
                return self.fmt_num(v)
            if typ == "b":
                return "1" if v else "0"
            return str(v)
        return fn


_COMMON_DEFAULTS = {
    "rf.output": False, "rf.frequency": 1e9, "rf.power": -135.0, "rf.alc": True,
    "ref.source": "INT", "mod.output": True,
    "am.state": False, "am.depth": 0.1, "am.source": "INT", "am.freq": 400.0,
    "fm.state": False, "fm.dev": 1000.0, "fm.source": "INT", "fm.freq": 400.0,
    "pm.state": False, "pm.dev": 0.0, "pm.source": "INT", "pm.freq": 400.0,
    "pulse.state": False, "pulse.source": "INT", "pulse.period": 1e-3, "pulse.width": 1e-6,
}


class KeysightE8267DSim(RFGenSim):
    IDN = "Agilent Technologies, E8267D, US00000001, C.06.20"
    SIGNED_ERR = True
    DEFAULTS = dict(_COMMON_DEFAULTS)
    LIMITS = {"rf.frequency": (250e3, 44e9), "rf.power": (-135.0, 25.0), "am.depth": (0.0, 100.0),
              "fm.dev": (0.0, 40e6), "pm.dev": (0.0, 160.0), "am.freq": (0.5, 1e6),
              "fm.freq": (0.5, 1e6), "pm.freq": (0.5, 1e6),
              "pulse.period": (70e-9, 42.0), "pulse.width": (10e-9, 42.0)}
    _SRC = {"INT": "INT", "INTERNAL": "INT", "INT1": "INT", "EXT1": "EXT1", "EXT2": "EXT2"}
    ENUM_TOKENS = {"am.source": _SRC, "fm.source": _SRC, "pm.source": _SRC,
                   "pulse.source": {"INT": "INT", "INTERNAL": "INT", "EXT": "EXT", "EXTERNAL": "EXT"}}
    TABLE = (
        ("OUTPut[:STATe]", "rf.output", "b"),
        ("OUTPut:MODulation[:STATe]", "mod.output", "b"),
        ("FREQuency[:CW]", "rf.frequency", "f"),
        ("POWer[:LEVel][:IMMediate][:AMPLitude]", "rf.power", "f"),
        ("POWer:ALC[:STATe]", "rf.alc", "b"),
        ("ROSCillator:SOURce", "ref.source", "ro"),
        ("AM[1]:STATe", "am.state", "b"),
        ("AM[1]:DEPTh[:LINear]", "am.depth", "f"),
        ("AM[1]:SOURce", "am.source", "e"),
        ("AM[1]:INTernal[1]:FREQuency", "am.freq", "f"),
        ("FM[1]:STATe", "fm.state", "b"),
        ("FM[1]:DEViation", "fm.dev", "f"),
        ("FM[1]:SOURce", "fm.source", "e"),
        ("FM[1]:INTernal[1]:FREQuency", "fm.freq", "f"),
        ("PM[1]:STATe", "pm.state", "b"),
        ("PM[1]:DEViation", "pm.dev", "f"),
        ("PM[1]:SOURce", "pm.source", "e"),
        ("PM[1]:INTernal[1]:FREQuency", "pm.freq", "f"),
        ("PULM:STATe", "pulse.state", "b"),
        ("PULM:SOURce", "pulse.source", "e"),
        ("PULM:INTernal[1]:PERiod", "pulse.period", "f"),
        ("PULM:INTernal[1]:PWIDth", "pulse.width", "f"),
    )

    def fmt_num(self, v: float) -> str:
        return f"{float(v):+.14E}"          # PSG: "+1.00000000000000E+09"


class RSSMF100ASim(RFGenSim):
    IDN = "Rohde&Schwarz,SMF100A,1167.0000k02/100001,3.1.19.2-3.01.203.30"
    DEFAULTS = {**_COMMON_DEFAULTS, "rf.alc": "AUTO", "lfo": 1000.0, "rf.power": -30.0}
    LIMITS = {"rf.frequency": (100e3, 43.5e9), "rf.power": (-130.0, 25.0), "am.depth": (0.0, 100.0),
              "fm.dev": (0.0, 40e6), "pm.dev": (0.0, 160.0), "lfo": (0.1, 1e6),
              "pulse.period": (100e-9, 85.0), "pulse.width": (20e-9, 85.0)}
    _SRC = {"INT": "INT", "INTERNAL": "INT", "EXT": "EXT", "EXTERNAL": "EXT"}
    ENUM_TOKENS = {"am.source": _SRC, "fm.source": _SRC, "pm.source": _SRC, "pulse.source": _SRC,
                   "ref.source": _SRC}
    TABLE = (
        ("OUTPut[1][:STATe]", "rf.output", "b"),
        ("[SOURce[1]:]MODulation[:ALL][:STATe]", "mod.output", "b"),
        ("[SOURce[1]:]FREQuency[:CW]", "rf.frequency", "f"),
        ("[SOURce[1]:]POWer[:LEVel][:IMMediate][:AMPLitude]", "rf.power", "f"),
        ("[SOURce[1]:]ROSCillator:SOURce", "ref.source", "e"),
        ("[SOURce[1]:]AM:STATe", "am.state", "b"),
        ("[SOURce[1]:]AM[:DEPTh]", "am.depth", "f"),
        ("[SOURce[1]:]AM:SOURce", "am.source", "e"),
        ("[SOURce[1]:]FM:STATe", "fm.state", "b"),
        ("[SOURce[1]:]FM[:DEViation]", "fm.dev", "f"),
        ("[SOURce[1]:]FM:SOURce", "fm.source", "e"),
        ("[SOURce[1]:]PM:STATe", "pm.state", "b"),
        ("[SOURce[1]:]PM[:DEViation]", "pm.dev", "f"),
        ("[SOURce[1]:]PM:SOURce", "pm.source", "e"),
        ("[SOURce[1]:]LFOutput:FREQuency", "lfo", "f"),
        ("[SOURce[1]:]PULM:STATe", "pulse.state", "b"),
        ("[SOURce[1]:]PULM:SOURce", "pulse.source", "e"),
        ("[SOURce[1]:]PULM:PERiod", "pulse.period", "f"),
        ("[SOURce[1]:]PULM:WIDTh", "pulse.width", "f"),
    )

    def register(self) -> None:
        super().register()
        self.on("[SOURce[1]:]POWer:ALC[:STATe]", self._alc_set, lambda a, g: (
            "AUTO" if self.s["rf.alc"] == "AUTO" else "1" if self.s["rf.alc"] == "ON" else "0"))

    def _alc_set(self, args, groups):
        t = args.strip().upper()
        if t in ("1", "ON"):
            self.s["rf.alc"] = "ON"
        elif t in ("0", "OFF"):
            self.s["rf.alc"] = "OFF"
        elif t == "AUTO":
            self.s["rf.alc"] = "AUTO"
        else:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)

    def fmt_num(self, v: float) -> str:
        v = float(v)
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else f"{v:g}"
