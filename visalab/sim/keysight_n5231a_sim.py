"""Симулятор ВАЦ Keysight PNA-L N5231A с моделью полосового фильтра на входе.

DUT: резонатор с центральной частотой ``f0``, нагруженной добротностью ``q`` и потерями
``il_db``: S21 = S12 = k/(1 + jQ(f/f0 - f0/f)), S11 = S22 = 1 - S21/k*(1 - r) (отражение
вне полосы ~1). Этого достаточно, чтобы проверять форматы, маркеры и развёртки.
Измерения - как на приборе: каталог «имя,S-параметр», выбор ``CALC:PAR:SEL``.
"""
from __future__ import annotations

import cmath
import math

from .base import ESR_EXE, ESR_OPC, SimError, SimInstrument

__all__ = ["KeysightPNASim"]

_FMT = {"MLOG": "MLOG", "PHAS": "PHAS", "PHASE": "PHAS", "UPH": "UPH", "UPHASE": "UPH", "GDEL": "GDEL",
        "GDELAY": "GDEL", "SWR": "SWR", "MLIN": "MLIN", "REAL": "REAL", "IMAG": "IMAG"}


class KeysightPNASim(SimInstrument):
    IDN = "Agilent Technologies,N5231A,MY00000001,A.09.90.02"
    F_MIN, F_MAX = 300e3, 13.5e9

    def reset(self) -> None:
        self.c = {"start": 10e6, "stop": 13.5e9, "poin": 201, "bwid": 35e3, "pow": -5.0, "outp": True,
                  "swt": 0.02, "swt_auto": True, "aver": False, "coun": 1, "corr": False, "mode": "CONT",
                  "form": "ASC"}
        self.meas = {"CH1_S11_1": {"s": "S11", "fmt": "MLOG"}}
        self.selected = ""
        self.marker = {"on": False, "x": 1e9}
        self.sweeping = False
        self.opc_pending = False
        self.sweeps_done = 0
        if not hasattr(self, "dut"):
            self.dut = {"f0": 2e9, "q": 20.0, "il_db": 1.0}
            self.hold_sweeps = False
        self._auto_time()

    # ------------------------------------------------------------ физика
    def _auto_time(self) -> None:
        if self.c["swt_auto"]:
            self.c["swt"] = self.c["poin"] / self.c["bwid"] * 1.2

    def s(self, name: str, f: float) -> complex:
        d = self.dut
        k = 10 ** (-d["il_db"] / 20)
        s21 = k / complex(1, d["q"] * (f / d["f0"] - d["f0"] / f))
        if name in ("S21", "S12"):
            return s21
        return 1 - s21 / k * 0.98

    def freqs(self) -> list[float]:
        n, a, b = self.c["poin"], self.c["start"], self.c["stop"]
        return [a + (b - a) * i / (n - 1) if n > 1 else a for i in range(n)]

    def sdata(self, name: str) -> list[float]:
        """Комплексные данные измерения: re, im, re, im, ..."""
        out: list[float] = []
        for f in self.freqs():
            v = self.s(self.meas[name]["s"], f)
            out += [v.real, v.imag]
        return out

    def fdata(self, name: str) -> list[float]:
        m = self.meas[name]
        fs = self.freqs()
        vals = [self.s(m["s"], f) for f in fs]
        fmt = m["fmt"]
        if fmt == "MLOG":
            return [20 * math.log10(max(abs(v), 1e-15)) for v in vals]
        if fmt == "MLIN":
            return [abs(v) for v in vals]
        if fmt in ("PHAS", "UPH"):
            ph = [math.degrees(cmath.phase(v)) for v in vals]
            if fmt == "UPH":
                out, off = [ph[0]], 0.0
                for a, b in zip(ph, ph[1:]):
                    off += -360.0 if b - a > 180 else 360.0 if b - a < -180 else 0.0
                    out.append(b + off)
                return out
            return ph
        if fmt == "GDEL":
            ph = [cmath.phase(v) for v in vals]
            out = []
            for i in range(len(fs)):
                j0, j1 = max(0, i - 1), min(len(fs) - 1, i + 1)
                dphi = math.remainder(ph[j1] - ph[j0], 2 * math.pi)
                df = fs[j1] - fs[j0]
                out.append(-dphi / (2 * math.pi * df) if df else 0.0)
            return out
        if fmt == "SWR":
            return [(1 + min(abs(v), 0.999999)) / (1 - min(abs(v), 0.999999)) for v in vals]
        if fmt == "REAL":
            return [v.real for v in vals]
        return [v.imag for v in vals]

    def finish_sweep(self) -> None:
        if not self.sweeping:
            return
        self.sweeping = False
        self.sweeps_done += 1
        self.c["mode"] = "HOLD"
        if self.opc_pending:
            self.opc_pending = False
            self.esr |= ESR_OPC

    # ------------------------------------------------------------ команды
    def _register_common(self) -> None:
        super()._register_common()
        self._handlers = [h for h in self._handlers if h[0] != "*OPC"]

        def opc(a, g):
            if self.sweeping:
                self.opc_pending = True
            else:
                self.esr |= ESR_OPC

        self.on("*OPC", opc, lambda a, g: "1")

    def register(self) -> None:
        self.on("SYSTem:ERRor[:NEXT]", get=self._err)
        self.on("FORMat[:DATA]", self._form_set, lambda a, g: "ASC,0")
        self.on("CALCulate#:PARameter:CATalog:EXTended", get=self._cat)
        self.on("CALCulate#:PARameter:CATalog", get=self._cat)
        self.on("CALCulate#:PARameter:SELect", self._sel, lambda a, g: f'"{self.selected}"')
        self.on("CALCulate#:PARameter:DEFine:EXTended", self._define)
        self.on("CALCulate#:PARameter:MODify:EXTended", self._modify)
        self.on("DISPlay:WINDow#:STATe", lambda a, g: None)
        self.on("DISPlay:WINDow#:TRACe#:FEED", lambda a, g: None)
        self.on("CALCulate#:FORMat", self._fmt_set, lambda a, g: self._cur()["fmt"])
        self.on("CALCulate#:DATA", get=self._data)
        self.on("CALCulate#:MARKer#:FUNCtion:EXECute", self._mk_exec)
        self.on("CALCulate#:MARKer#:X", self._mk_x, lambda a, g: f"{self.marker['x']:+.11E}")
        self.on("CALCulate#:MARKer#:Y", get=self._mk_y)
        self.on("CALCulate#:MARKer#[:STATe]", self._mk_state, lambda a, g: "1" if self.marker["on"] else "0")
        self.on("SENSe#:FREQuency:STARt", self._f("start"), self._g(lambda: self.c["start"]))
        self.on("SENSe#:FREQuency:STOP", self._f("stop"), self._g(lambda: self.c["stop"]))
        self.on("SENSe#:FREQuency:CENTer", self._f("center"), self._g(lambda: (self.c["start"] + self.c["stop"]) / 2))
        self.on("SENSe#:FREQuency:SPAN", self._f("span"), self._g(lambda: self.c["stop"] - self.c["start"]))
        self.on("SENSe#:SWEep:POINts", self._n("poin", 1, 32001, integer=True), lambda a, g: str(self.c["poin"]))
        self.on("SENSe#:SWEep:TIME:AUTO", self._b("swt_auto"), self._gb("swt_auto"))
        self.on("SENSe#:SWEep:TIME", self._n("swt", 0, 86400, auto="swt_auto"), self._g(lambda: self.c["swt"]))
        self.on("SENSe#:SWEep:MODE", self._mode_set, lambda a, g: self.c["mode"])
        self.on("SENSe#:BWIDth[:RESolution]", self._bwid_set, self._g(lambda: self.c["bwid"]))
        self.on("SENSe#:AVERage:COUNt", self._n("coun", 1, 65536, integer=True), lambda a, g: str(self.c["coun"]))
        self.on("SENSe#:AVERage[:STATe]", self._b("aver"), self._gb("aver"))
        self.on("SENSe#:CORRection[:STATe]", self._b("corr"), self._gb("corr"))
        self.on("SOURce#:POWer#[:LEVel][:IMMediate][:AMPLitude]", self._n("pow", -90, 20),
                self._g(lambda: self.c["pow"]))
        self.on("OUTPut[:STATe]", self._b("outp"), self._gb("outp"))
        self.on("ABORt", lambda a, g: setattr(self, "sweeping", False))

    def _err(self, a, g):
        code, msg = self.errors.popleft() if self.errors else (0, "No error")
        return f'{code:+d},"{msg}"'

    # помощники
    def _g(self, fn):
        return lambda a, g: f"{fn():+.11E}"

    def _gb(self, k):
        return lambda a, g: "1" if self.c[k] else "0"

    def _b(self, k):
        def fn(a, g):
            self.c[k] = self.boolean(a)
            self._auto_time()
        return fn

    def _n(self, k, lo, hi, integer=False, auto=None):
        def fn(a, g):
            v = self.num(a, lo, hi)
            self.c[k] = int(round(v)) if integer else v
            if auto:
                self.c[auto] = False
            self._auto_time()
        return fn

    def _bwid_set(self, a, g):
        v = self.num(a, 1, 15e6)
        steps = [10 ** e * m for e in range(0, 8) for m in (1, 1.5, 2, 3, 5, 7)]
        self.c["bwid"] = min(steps, key=lambda s: abs(s - v))    # прибор округляет к ряду
        self._auto_time()

    def _f(self, which):
        def fn(a, g):
            c = self.c
            if which == "span":
                v = self.num(a, 0, self.F_MAX - self.F_MIN)
                mid = (c["start"] + c["stop"]) / 2
                c["start"], c["stop"] = max(self.F_MIN, mid - v / 2), min(self.F_MAX, mid + v / 2)
                return
            v = self.num(a, self.F_MIN, self.F_MAX)
            if which == "start":
                c["start"], c["stop"] = v, max(v, c["stop"])
            elif which == "stop":
                c["stop"], c["start"] = v, min(v, c["start"])
            else:
                half = (c["stop"] - c["start"]) / 2
                c["start"], c["stop"] = max(self.F_MIN, v - half), min(self.F_MAX, v + half)
        return fn

    def _form_set(self, a, g):
        if not a.strip().upper().startswith("ASC"):
            raise SimError(-221, "Settings conflict", ESR_EXE)

    def _cat(self, a, g):
        if not self.meas:
            return '"NO CATALOG"'
        return '"' + ",".join(f"{n},{m['s']}" for n, m in self.meas.items()) + '"'

    def _cur(self) -> dict:
        if self.selected not in self.meas:
            raise SimError(-221, "Settings conflict; no measurement selected", ESR_EXE)
        return self.meas[self.selected]

    def _sel(self, a, g):
        name = a.strip().strip("'\"")
        if name not in self.meas:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.selected = name

    def _define(self, a, g):
        name, s = (t.strip().strip("'\"") for t in a.split(","))
        self.meas[name] = {"s": s.upper(), "fmt": "MLOG"}

    def _modify(self, a, g):
        s = a.strip().strip("'\"").upper()
        if s not in ("S11", "S21", "S12", "S22"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur()["s"] = s

    def _fmt_set(self, a, g):
        t = a.strip().upper()
        if t not in _FMT:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur()["fmt"] = _FMT[t]

    def _data(self, a, g):
        kind = a.strip().upper()
        if kind not in ("FDATA", "SDATA"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur()
        data = self.fdata(self.selected) if kind == "FDATA" else self.sdata(self.selected)
        return ",".join(f"{v:+.10E}" for v in data)

    def _mode_set(self, a, g):
        t = a.strip().upper()
        mode = {"HOLD": "HOLD", "CONT": "CONT", "CONTINUOUS": "CONT", "SING": "SING", "SINGLE": "SING"}.get(t)
        if mode is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.c["mode"] = mode
        if mode == "SING":
            self.sweeping = True
            if not self.hold_sweeps:
                self.finish_sweep()
        elif mode == "HOLD":
            self.sweeping = False

    def _mk_state(self, a, g):
        self.marker["on"] = self.boolean(a)

    def _mk_x(self, a, g):
        self.marker["x"] = self.num(a, 0, self.F_MAX)
        self.marker["on"] = True

    def _mk_y(self, a, g):
        if not self.marker["on"]:
            raise SimError(-221, "Settings conflict", ESR_EXE)
        data, fs = self.fdata(self.selected), self.freqs()
        i = min(range(len(fs)), key=lambda k: abs(fs[k] - self.marker["x"]))
        return f"{data[i]:+.10E},+0.00000000000E+000"

    def _mk_exec(self, a, g):
        data, fs = self.fdata(self.selected), self.freqs()
        pick = max if a.strip().upper().startswith("MAX") else min
        i = pick(range(len(data)), key=data.__getitem__)
        self.marker.update(on=True, x=fs[i])
