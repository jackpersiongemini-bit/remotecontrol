"""Симулятор анализатора спектра Agilent PSA E4446A.

Спектр: шумовая дорожка + список синусоид ``sim.signals = [(частота, dBm), ...]``,
каждая размыта фильтром RBW (гауссова форма). Связи «авто» упрощены, но как у прибора:
RBW ~ полоса/100 (ряд 1-3), VBW = RBW, время развёртки из полосы и RBW.
Развёртка: ``INIT`` при ``INIT:CONT OFF`` заканчивается сразу (или по ``sim.finish_sweep()``,
если ``sim.hold_sweeps = True``), ``*OPC`` взводит бит OPC по её окончании.
"""
from __future__ import annotations

import math

from .base import ESR_EXE, ESR_OPC, SimError, SimInstrument

__all__ = ["AgilentPSASim"]

_DET = {"NORM": "NORM", "NORMAL": "NORM", "POS": "POS", "POSITIVE": "POS", "NEG": "NEG", "NEGATIVE": "NEG",
        "SAMP": "SAMP", "SAMPLE": "SAMP", "AVER": "AVER", "AVERAGE": "AVER", "RMS": "RMS",
        "QPE": "QPE", "QPEAK": "QPE"}


def _round_13(x: float) -> float:
    """Ближайшее снизу из ряда 1-3-10."""
    if x <= 1:
        return 1.0
    e = math.floor(math.log10(x))
    m = x / 10 ** e
    return (3.0 if m >= 3 else 1.0) * 10 ** e


class AgilentPSASim(SimInstrument):
    IDN = "Agilent Technologies, E4446A, MY00000001, A.11.21"
    F_MAX = 44e9

    def reset(self) -> None:
        self.c = {"center": 22e9, "span": 44e9, "rbw": 3e6, "rbw_auto": True, "vbw": 3e6, "vbw_auto": True,
                  "rlev": 0.0, "pdiv": 10.0, "att": 10.0, "att_auto": True, "swt": 0.1, "swt_auto": True,
                  "poin": 601, "det": "POS", "aver": False, "coun": 100, "rosc": "INT", "cont": True,
                  "form": "ASC"}
        self.marker = {"on": False, "x": 22e9}
        self.sweeping = False
        self.opc_pending = False
        self.sweeps_done = 0
        self.last: list[float] | None = None
        if not hasattr(self, "signals"):
            self.signals: list[tuple[float, float]] = [(1e9, -20.0)]
            self.noise_dbm = -90.0
            self.hold_sweeps = False
        self._couple()

    # ------------------------------------------------------------ физика
    def _couple(self) -> None:
        c = self.c
        if c["rbw_auto"]:
            c["rbw"] = min(3e6, max(1.0, _round_13(c["span"] / 100))) if c["span"] > 0 else 3e6
        if c["vbw_auto"]:
            c["vbw"] = c["rbw"]
        if c["att_auto"]:
            c["att"] = max(0.0, min(70.0, 10.0 + 2 * round(c["rlev"] / 2)))
        if c["swt_auto"]:
            c["swt"] = max(1e-3, 2.5 * c["span"] / (c["rbw"] * min(c["rbw"], c["vbw"]))) if c["span"] > 0 else 1e-3

    @property
    def start(self) -> float:
        return self.c["center"] - self.c["span"] / 2

    @property
    def stop(self) -> float:
        return self.c["center"] + self.c["span"] / 2

    def spectrum(self) -> list[float]:
        n = self.c["poin"]
        rbw = self.c["rbw"]
        out = []
        for i in range(n):
            f = self.start + (self.stop - self.start) * i / (n - 1) if n > 1 else self.c["center"]
            mw = 10 ** (self.noise_dbm / 10) * rbw / 1e6
            for fs, p in self.signals:
                mw += 10 ** (p / 10) * math.exp(-0.5 * ((f - fs) / (rbw / 2.355)) ** 2)
            out.append(10 * math.log10(mw))
        return out

    def finish_sweep(self) -> None:
        if not self.sweeping:
            return
        self.last = self.spectrum()
        self.sweeping = False
        self.sweeps_done += 1
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
        f = self._freq
        self.on("[SENSe:]FREQuency:CENTer", f("center"), self._g(lambda: self.c["center"]))
        self.on("[SENSe:]FREQuency:SPAN", f("span"), self._g(lambda: self.c["span"]))
        self.on("[SENSe:]FREQuency:STARt", f("start"), self._g(lambda: self.start))
        self.on("[SENSe:]FREQuency:STOP", f("stop"), self._g(lambda: self.stop))
        self.on("[SENSe:]BANDwidth[:RESolution]:AUTO", self._b("rbw_auto"), self._gb("rbw_auto"))
        self.on("[SENSe:]BANDwidth[:RESolution]", self._n("rbw", 1, 8e6, "rbw_auto"), self._g(lambda: self.c["rbw"]))
        self.on("[SENSe:]BWIDth[:RESolution]", self._n("rbw", 1, 8e6, "rbw_auto"), self._g(lambda: self.c["rbw"]))
        self.on("[SENSe:]BANDwidth:VIDeo:AUTO", self._b("vbw_auto"), self._gb("vbw_auto"))
        self.on("[SENSe:]BANDwidth:VIDeo", self._n("vbw", 1, 50e6, "vbw_auto"), self._g(lambda: self.c["vbw"]))
        self.on("DISPlay:WINDow[1]:TRACe:Y[:SCALe]:RLEVel", self._n("rlev", -170, 30), self._g(lambda: self.c["rlev"]))
        self.on("DISPlay:WINDow[1]:TRACe:Y[:SCALe]:PDIVision", self._n("pdiv", 0.1, 20),
                self._g(lambda: self.c["pdiv"]))
        self.on("[SENSe:]POWer[:RF]:ATTenuation:AUTO", self._b("att_auto"), self._gb("att_auto"))
        self.on("[SENSe:]POWer[:RF]:ATTenuation", self._att_set, self._g(lambda: self.c["att"]))
        self.on("[SENSe:]SWEep:TIME:AUTO", self._b("swt_auto"), self._gb("swt_auto"))
        self.on("[SENSe:]SWEep:TIME", self._n("swt", 1e-6, 4000, "swt_auto"), self._g(lambda: self.c["swt"]))
        self.on("[SENSe:]SWEep:POINts", self._points_set, lambda a, g: str(self.c["poin"]))
        self.on("[SENSe:]DETector[:FUNCtion]", self._det_set, lambda a, g: self.c["det"])
        self.on("[SENSe:]AVERage:COUNt", self._count_set, lambda a, g: str(self.c["coun"]))
        self.on("[SENSe:]AVERage[:STATe]", self._b("aver"), self._gb("aver"))
        self.on("[SENSe:]ROSCillator:SOURce", self._rosc_set, lambda a, g: self.c["rosc"])
        self.on("FORMat[:TRACe][:DATA]", self._form_set, lambda a, g: self.c["form"])
        self.on("INITiate:CONTinuous", self._cont_set, self._gb("cont"))
        self.on("INITiate[:IMMediate]", self._init)
        self.on("ABORt", lambda a, g: setattr(self, "sweeping", False))
        self.on("TRACe[:DATA]", get=self._trace)
        self.on("CALCulate:MARKer[1]:STATe", self._mk_state, lambda a, g: "1" if self.marker["on"] else "0")
        self.on("CALCulate:MARKer[1]:X", self._mk_x, self._mk_x_get)
        self.on("CALCulate:MARKer[1]:Y", get=self._mk_y)
        self.on("CALCulate:MARKer[1]:MAXimum", self._mk_max)

    def _err(self, a, g):
        code, msg = self.errors.popleft() if self.errors else (0, "No error")
        return f'{code:+d},"{msg}"'

    # помощники
    @staticmethod
    def _fmt(v: float) -> str:
        return f"{v:+.8E}"

    def _g(self, fn):
        return lambda a, g: self._fmt(fn())

    def _gb(self, k):
        return lambda a, g: "1" if self.c[k] else "0"

    def _b(self, k):
        def fn(a, g):
            self.c[k] = self.boolean(a)
            self._couple()
        return fn

    def _n(self, k, lo, hi, auto=None):
        def fn(a, g):
            self.c[k] = self.num(a, lo, hi)
            if auto:
                self.c[auto] = False
            self._couple()
        return fn

    def _freq(self, which):
        def fn(a, g):
            v = self.num(a, 0, self.F_MAX if which != "span" else self.F_MAX)
            c = self.c
            if which == "center":
                c["center"] = v
            elif which == "span":
                c["span"] = v
            elif which == "start":
                stop = self.stop
                c["span"], c["center"] = max(0.0, stop - v), (v + stop) / 2
            else:
                start = self.start
                c["span"], c["center"] = max(0.0, v - start), (v + start) / 2
            self._couple()
        return fn

    def _att_set(self, a, g):
        v = self.num(a, 0, 70)
        self.c["att"] = 2 * round(v / 2)     # шаг 2 дБ - округление проверяет COERCED
        self.c["att_auto"] = False

    def _points_set(self, a, g):
        self.c["poin"] = int(self.num(a, 2 if self.c["span"] == 0 else 101, 8192))
        self._couple()

    def _det_set(self, a, g):
        t = a.strip().upper()
        if t not in _DET:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.c["det"] = _DET[t]

    def _count_set(self, a, g):
        self.c["coun"] = int(self.num(a, 1, 8192))

    def _rosc_set(self, a, g):
        t = a.strip().upper()
        self.c["rosc"] = {"INT": "INT", "INTERNAL": "INT", "EXT": "EXT", "EXTERNAL": "EXT"}[t]

    def _form_set(self, a, g):
        t = a.strip().upper().replace(" ", "")
        if t not in ("ASC", "ASCII", "REAL,32", "REAL,64", "INT,32"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.c["form"] = "ASC" if t.startswith("ASC") else t

    def _cont_set(self, a, g):
        self.c["cont"] = self.boolean(a)

    def _init(self, a, g):
        if self.c["cont"]:
            return                      # по мануалу при CONT ON команда игнорируется
        self.sweeping = True
        if not self.hold_sweeps:
            self.finish_sweep()

    def _trace(self, a, g):
        if a.strip().upper() not in ("TRACE1", "TRACE2", "TRACE3"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if self.c["form"] != "ASC":
            raise SimError(-221, "Settings conflict", ESR_EXE)   # бинарный формат симулятор не отдаёт
        data = self.last if self.last is not None else self.spectrum()
        return ",".join(f"{v:+.3E}" for v in data)

    def _mk_state(self, a, g):
        self.marker["on"] = self.boolean(a)

    def _mk_x(self, a, g):
        self.marker["x"] = self.num(a, 0, self.F_MAX)
        self.marker["on"] = True

    def _mk_x_get(self, a, g):
        return self._fmt(self.marker["x"])

    def _mk_y(self, a, g):
        if not self.marker["on"]:
            raise SimError(-221, "Settings conflict", ESR_EXE)
        data = self.last if self.last is not None else self.spectrum()
        n = len(data)
        i = round((self.marker["x"] - self.start) / (self.stop - self.start) * (n - 1)) if self.c["span"] else 0
        return self._fmt(data[max(0, min(n - 1, i))])

    def _mk_max(self, a, g):
        data = self.last if self.last is not None else self.spectrum()
        n = len(data)
        i = max(range(n), key=data.__getitem__)
        self.marker["on"] = True
        self.marker["x"] = self.start + (self.stop - self.start) * i / (n - 1) if n > 1 else self.c["center"]
