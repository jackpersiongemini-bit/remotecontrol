"""Симулятор ВАЦ Keysight PNA-L N5231A с моделью полосового фильтра на входе.

DUT: резонатор с центральной частотой ``f0``, нагруженной добротностью ``q`` и потерями
``il_db``: S21 = S12 = k/(1 + jQ(f/f0 - f0/f)), S11 = S22 = 1 - S21/k*(1 - r) (отражение
вне полосы ~1). Этого достаточно, чтобы проверять форматы, маркеры и развёртки.
Измерения - как на приборе: каталог «имя,S-параметр», выбор ``CALC:PAR:SEL``.
"""
from __future__ import annotations

import cmath
import copy
import math

from .base import ESR_EXE, ESR_OPC, SimError, SimInstrument

__all__ = ["KeysightPNASim"]

_FMT = {"MLOG": "MLOG", "PHAS": "PHAS", "PHASE": "PHAS", "UPH": "UPH", "UPHASE": "UPH", "GDEL": "GDEL",
        "GDELAY": "GDEL", "SWR": "SWR", "MLIN": "MLIN", "REAL": "REAL", "IMAG": "IMAG", "POL": "POL", "POLAR": "POL",
        "SMIT": "SMIT", "SMITH": "SMIT"}


DEFAULT_DIR = "C:\\Users\\Public\\Documents\\Network Analyzer"
CONNECTORS = ("APC 3.5 female", "APC 3.5 male", "Type N (50) female", "Type N (50) male", "Not used")
#: семейство разъёма -> наборы (идеальный - как пользовательское определение набора на приборе)
KITS = {"APC 3.5": ("85052D", "Ideal 3.5mm", "N4691-60004 ECal"), "Type N": ("85054D", "Ideal Type N")}

class KeysightPNASim(SimInstrument):
    IDN = "Agilent Technologies,N5231A,MY00000001,A.09.90.02"
    F_MIN, F_MAX = 300e3, 13.5e9

    def reset(self) -> None:
        self.c = {"start": 10e6, "stop": 13.5e9, "poin": 201, "bwid": 35e3, "pow": -5.0, "outp": True,
                  "swt": 0.02, "swt_auto": True, "aver": False, "coun": 1, "corr": False, "mode": "CONT", "tmode": "CHAN",
                  "form": "ASC", "type": "LIN", "cw": 1e9, "cset": "", "interp": True}
        self.meas = {"CH1_S11_1": {"s": "S11", "fmt": "MLOG", "ch": 1}}
        self.selected = ""                               # выбранное измерение канала 1
        self.sel_other: dict[int, str] = {}              # то же у остальных каналов
        self.other: dict[int, dict] = {}                 # стимул каналов 2...
        self.marker = {"on": False, "x": 1e9}
        self.trig = {"sour": "IMM", "scop": "ALL", "slop": "POS", "type": "EDGE", "del": 0.0}
        self.trig_count = 0                              # триггеров в текущей развёртке (режим POINT)
        self.windows: dict[int, dict[int, str]] = {1: {1: "CH1_S11_1"}}     # окно -> {номер трейса: измерение}
        self.sweeping = False
        self.opc_pending = False
        self.sweeps_done = 0
        if not hasattr(self, "dirs"):                    # папки и файлы переживают *RST
            self.dirs: dict[str, dict[str, tuple]] = {DEFAULT_DIR: {}, DEFAULT_DIR + "\\Lab": {}}
            self.cdir = DEFAULT_DIR
            self.files: dict[str, tuple] = self.dirs[self.cdir]     # "имя.csa" -> состояние, текущая папка
        if not hasattr(self, "csets"):                   # cal set'ы переживают *RST: имя -> стимул калибровки
            self.csets: dict[str, dict] = {
                "CAL_1-3G": {"start": 1e9, "stop": 3e9, "poin": 201, "type": "LIN", "cw": 1e9, "bwid": 1e3,
                             "pow": -10.0},
                "CAL_FULL": {"start": 10e6, "stop": 13.5e9, "poin": 401, "type": "LIN", "cw": 1e9, "bwid": 10e3,
                             "pow": -5.0}}
        self.snp_form = "MA"
        self.guid = {"conn": {1: "Not used", 2: "Not used"}, "kit": {1: "", 2: ""}, "steps": [], "done": set(),
                     "ch": 1}
        if not hasattr(self, "dut"):
            self.dut = {"f0": 2e9, "q": 20.0, "il_db": 1.0}
            self.hold_sweeps = False
        self._auto_time()

    # ------------------------------------------------------------ физика
    def _auto_time(self, st: dict | None = None) -> None:
        st = self.c if st is None else st
        if st["swt_auto"]:
            st["swt"] = st["poin"] / st["bwid"] * 1.2

    def s(self, name: str, f: float) -> complex:
        d = self.dut
        k = 10 ** (-d["il_db"] / 20)
        s21 = k / complex(1, d["q"] * (f / d["f0"] - d["f0"] / f))
        if name in ("S21", "S12"):
            return s21
        return 1 - s21 / k * 0.98

    def stim(self, ch: int) -> dict:
        """Стимул канала: канал 1 - ``self.c``, остальные создаются при первом обращении."""
        if ch == 1:
            return self.c
        if ch not in self.other:
            st = {"start": 1e9, "stop": 2e9, "poin": 101, "type": "LIN", "cw": 1e9, "bwid": 35e3, "pow": -5.0,
                  "swt_auto": True, "swt": 0.0, "aver": False, "coun": 1, "corr": False, "mode": "CONT", "tmode": "CHAN",
                  "cset": "", "interp": True}
            self._auto_time(st)
            self.other[ch] = st
        return self.other[ch]

    def freqs(self, ch: int = 1) -> list[float]:
        st = self.stim(ch)
        n, a, b = st["poin"], st["start"], st["stop"]
        if st["type"] == "CW":
            return [st["cw"]] * n
        return [a + (b - a) * i / (n - 1) if n > 1 else a for i in range(n)]

    def sdata(self, name: str) -> list[float]:
        """Комплексные данные измерения: re, im, re, im, ..."""
        out: list[float] = []
        for f in self.freqs(self.meas[name]["ch"]):
            v = self.s(self.meas[name]["s"], f)
            out += [v.real, v.imag]
        return out

    def fdata(self, name: str) -> list[float]:
        """Данные в формате измерения; сглаживание - скользящее среднее по апертуре (% точек)."""
        out = self._fdata(name)
        m = self.meas[name]
        if not m.get("smo") or m["fmt"] in ("POL", "SMIT") or len(out) < 3:
            return out
        half = max(1, round(m.get("aper", 1.5) / 100 * len(out))) // 2 or 1
        return [sum(out[max(0, i - half):i + half + 1]) / len(out[max(0, i - half):i + half + 1])
                for i in range(len(out))]

    def _fdata(self, name: str) -> list[float]:
        m = self.meas[name]
        fs = self.freqs(m["ch"])
        vals = [self.s(m["s"], f) for f in fs]
        if m.get("edel"):                                # электрическая задержка: компенсация набега фазы
            vals = [v * cmath.exp(2j * math.pi * f * m["edel"]) for v, f in zip(vals, fs)]
        mem, fn = m.get("mem"), m.get("math", "NORM")
        if fn != "NORM" and mem is not None and len(mem) == len(vals):
            op = {"DIV": lambda d, p: d / p if p else 0j, "SUBT": lambda d, p: d - p,
                  "ADD": lambda d, p: d + p, "MULT": lambda d, p: d * p}[fn]
            vals = [op(d, p) for d, p in zip(vals, mem)]
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
        if fmt in ("POL", "SMIT"):                       # комплексные форматы: пары re,im
            return [x for v in vals for x in (v.real, v.imag)]
        if fmt == "REAL":
            return [v.real for v in vals]
        return [v.imag for v in vals]

    def finish_sweep(self) -> None:
        if not self.sweeping:
            return
        self.sweeping = False
        self.sweeps_done += 1
        for st in (self.c, *self.other.values()):       # однократные развёртки закончились
            if st["mode"] == "SING":
                st["mode"] = "HOLD"
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
        self.on("SYSTem:CHANnels:CATalog", get=lambda a, g: '"' + ",".join(
            str(c) for c in sorted({1} | {m["ch"] for m in self.meas.values()} | set(self.other))) + '"')
        self.on("FORMat[:DATA]", self._form_set, lambda a, g: "ASC,0")
        self.on("CALCulate#:PARameter:CATalog:EXTended", get=self._cat)
        self.on("CALCulate#:PARameter:CATalog", get=self._cat)
        self.on("CALCulate#:PARameter:SELect", self._sel, lambda a, g: f'"{self._selected(self._ch(g))}"')
        self.on("CALCulate#:PARameter:DEFine:EXTended", self._define)
        self.on("CALCulate#:PARameter:MODify:EXTended", self._modify)
        self.on("CALCulate#:PARameter:DELete", self._delete)
        self.on("DISPlay:WINDow#:STATe", self._win_state, lambda a, g: "1" if int(g[0]) in self.windows else "0")
        self.on("DISPlay:WINDow#:CATalog", get=self._win_cat)
        self.on("DISPlay:WINDow#:TRACe#:FEED", self._feed)
        self.on("CALCulate#:FORMat", self._fmt_set, lambda a, g: self._cur(g)["fmt"])
        self.on("CALCulate#:PARameter:WNUMber", get=lambda a, g: str(self._place(g)[0]))
        self.on("CALCulate#:PARameter:TNUMber", get=lambda a, g: str(self._place(g)[1]))
        for scpi, key, lo, hi, dflt in (("PDIVision", "pdiv", 1e-18, 1e12, 10.0),
                                        ("RLEVel", "rlev", -1e12, 1e12, 0.0), ("RPOSition", "rpos", 0, 20, 5.0)):
            self.on(f"DISPlay:WINDow#:TRACe#:Y[:SCALe]:{scpi}", self._scale_set(key, lo, hi),
                    lambda a, g, k=key, d=dflt: f"{self._wt(g).get(k, d):+.11E}")
        self.on("DISPlay:WINDow#:TRACe#:Y[:SCALe]:AUTO", self._autoscale)
        self.on("CALCulate#:CORRection:EDELay[:TIME]", self._meas_num("edel", -10, 10),
                lambda a, g: f"{self._cur(g).get('edel', 0.0):+.11E}")
        self.on("CALCulate#:SMOothing:APERture", self._meas_num("aper", 1, 25),
                lambda a, g: f"{self._cur(g).get('aper', 1.5):+.11E}")
        self.on("CALCulate#:SMOothing[:STATe]", self._smo_set, lambda a, g: "1" if self._cur(g).get("smo") else "0")
        self.on("CALCulate#:DATA", get=self._data)
        self.on("CALCulate#:MATH:MEMorize", self._memorize)
        self.on("CALCulate#:MATH:FUNCtion", self._math_set, lambda a, g: self._cur(g).get("math", "NORM"))
        self.on("MMEMory:STORe[:FILE]", self._st_store)
        self.on("MMEMory:LOAD[:FILE]", self._st_load)
        self.on("MMEMory:CATalog", get=lambda a, g: '"' + ",".join(sorted(self.files) + self.subdirs()) + '"')
        self.on("MMEMory:DELete", self._file_del)
        self.on("MMEMory:CDIRectory", self._cdir, lambda a, g: f'"{self.cdir}"')
        self.on("CALCulate#:MARKer#:FUNCtion:EXECute", self._mk_exec)
        self.on("CALCulate#:MARKer#:X", self._mk_x, lambda a, g: f"{self.marker['x']:+.11E}")
        self.on("CALCulate#:MARKer#:Y", get=self._mk_y)
        self.on("CALCulate#:MARKer#[:STATe]", self._mk_state, lambda a, g: "1" if self.marker["on"] else "0")
        self.on("SENSe#:FREQuency:STARt", self._f("start"), self._gs("start"))
        self.on("SENSe#:FREQuency:STOP", self._f("stop"), self._gs("stop"))
        self.on("SENSe#:FREQuency:CENTer", self._f("center"), self._gs("center"))
        self.on("SENSe#:FREQuency:SPAN", self._f("span"), self._gs("span"))
        self.on("SENSe#:SWEep:POINts", self._n("poin", 1, 32001, integer=True),
                lambda a, g: str(self.stim(self._ch(g))["poin"]))
        self.on("SENSe#:SWEep:TYPE", self._type_set, lambda a, g: self.stim(self._ch(g))["type"])
        self.on("SENSe#:FREQuency:CW", self._cw_set, self._gs("cw"))
        self.on("SENSe#:FREQuency:FIXed", self._cw_set, self._gs("cw"))
        self.on("SENSe#:SWEep:TIME:AUTO", self._b("swt_auto"), self._gb("swt_auto"))
        self.on("SENSe#:SWEep:TIME", self._n("swt", 0, 86400, auto="swt_auto"), self._gs("swt"))
        self.on("SENSe#:SWEep:MODE", self._mode_set, lambda a, g: self.stim(self._ch(g))["mode"])
        self.on("SENSe#:BWIDth[:RESolution]", self._bwid_set, self._gs("bwid"))
        self.on("SENSe#:AVERage:COUNt", self._n("coun", 1, 65536, integer=True), lambda a, g: str(self.stim(self._ch(g))["coun"]))
        self.on("SENSe#:AVERage[:STATe]", self._b("aver"), self._gb("aver"))
        self.on("SENSe#:CORRection[:STATe]", self._corr_set, self._gb("corr"))
        self.on("SENSe#:CORRection:INTerpolate[:STATe]", self._b("interp"), self._gb("interp"))
        self.on("SENSe#:CORRection:CSET:ACTivate", self._cset_act,
                lambda a, g: f'"{self.stim(self._ch(g))["cset"] or "No Calset Selected"}"')
        self.on("SENSe#:CORRection:CSET:DEACtivate", self._cset_deact)
        self.on("CSET:CATalog", get=lambda a, g: '"' + ",".join(sorted(self.csets)) + '"')
        self.on("SENSe#:CORRection:CSET:CATalog", get=lambda a, g: '"' + ",".join(sorted(self.csets)) + '"')
        self.on("CSET:DELete", self._cset_del)
        self.on("MMEMory:STORe:TRACe:FORMat:SNP", lambda a, g: setattr(self, "snp_form", a.strip().upper()),
                lambda a, g: self.snp_form)
        self.on("CALCulate#:DATA:SNP:PORTs", get=self._snp)
        g = "SENSe#:CORRection:COLLect:GUIDed:"
        self.on(g + "CONNector:CATalog", get=lambda a, g_: '"' + ",".join(CONNECTORS) + '"')
        self.on(g + "CONNector:PORT#", self._guid_conn, lambda a, g_: f'"{self.guid["conn"][int(g_[1])]}"')
        self.on(g + "CKIT:CATalog", get=self._guid_kits)
        self.on(g + "CKIT:PORT#", self._guid_kit, lambda a, g_: f'"{self.guid["kit"][int(g_[1])]}"')
        self.on(g + "INITiate[:IMMediate]", self._guid_init)
        self.on(g + "STEPs", get=lambda a, g_: str(len(self.guid["steps"])))
        self.on(g + "DESCription", get=self._guid_desc)
        self.on(g + "ACQuire", self._guid_acq)
        self.on(g + "SAVE:CSET", self._guid_save)
        self.on("SOURce#:POWer#[:LEVel][:IMMediate][:AMPLitude]", self._n("pow", -90, 20),
                self._gs("pow"))
        self.on("OUTPut[:STATe]", self._b("outp"), self._gb("outp"))
        self.on("ABORt", lambda a, g: setattr(self, "sweeping", False))
        self.on("TRIGger[:SEQuence]:SOURce", self._tenum("sour", {"IMM": "IMM", "IMMEDIATE": "IMM", "EXT": "EXT",
                                                                   "EXTERNAL": "EXT", "MAN": "MAN", "MANUAL": "MAN"}),
                lambda a, g: self.trig["sour"])
        self.on("TRIGger[:SEQuence]:SCOPe", self._tenum("scop", {"ALL": "ALL", "CURR": "CURR", "CURRENT": "CURR"}),
                lambda a, g: self.trig["scop"])
        self.on("TRIGger[:SEQuence]:SLOPe", self._tenum("slop", {"POS": "POS", "POSITIVE": "POS", "NEG": "NEG",
                                                                  "NEGATIVE": "NEG"}),
                lambda a, g: self.trig["slop"])
        self.on("TRIGger[:SEQuence]:TYPE", self._tenum("type", {"EDGE": "EDGE", "LEV": "LEV", "LEVEL": "LEV"}),
                lambda a, g: self.trig["type"])
        self.on("TRIGger[:SEQuence]:DELay", self._trig_delay, lambda a, g: f"{self.trig['del']:+.11E}")
        self.on("SENSe#:SWEep:TRIGger:MODE", self._tmode_set, lambda a, g: self.stim(self._ch(g))["tmode"])
        self.on("INITiate#[:IMMediate]", lambda a, g: self.trigger())

    def _err(self, a, g):
        code, msg = self.errors.popleft() if self.errors else (0, "No error")
        return f'{code:+d},"{msg}"'

    # помощники
    @staticmethod
    def _ch(g) -> int:
        return int(g[0]) if g and g[0] else 1

    def _gs(self, which):
        def fn(a, g):
            st = self.stim(self._ch(g))
            v = {"center": (st["start"] + st["stop"]) / 2, "span": st["stop"] - st["start"]}.get(which)
            return f"{(st[which] if v is None else v):+.11E}"
        return fn

    def _g(self, fn):
        return lambda a, g: f"{fn():+.11E}"

    def _gb(self, k):
        return lambda a, g: "1" if self.stim(self._ch(g))[k] else "0"

    def _b(self, k):
        def fn(a, g):
            st = self.stim(self._ch(g))
            st[k] = self.boolean(a)
            self._auto_time(st)
        return fn

    def _n(self, k, lo, hi, integer=False, auto=None):
        def fn(a, g):
            v = self.num(a, lo, hi)
            target = self.stim(self._ch(g))
            target[k] = int(round(v)) if integer else v
            if auto:
                target[auto] = False
            self._auto_time(target)
        return fn

    def _bwid_set(self, a, g):
        v = self.num(a, 1, 15e6)
        steps = [10 ** e * m for e in range(0, 8) for m in (1, 1.5, 2, 3, 5, 7)]
        st = self.stim(self._ch(g))
        st["bwid"] = min(steps, key=lambda s: abs(s - v))        # прибор округляет к ряду
        self._auto_time(st)

    def _f(self, which):
        def fn(a, g):
            c = self.stim(self._ch(g))
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
        mine = {n: m for n, m in self.meas.items() if m["ch"] == self._ch(g)}
        if not mine:
            return '"NO CATALOG"'
        return '"' + ",".join(f"{n},{m['s']}" for n, m in mine.items()) + '"'

    def _selected(self, ch: int) -> str:
        return self.selected if ch == 1 else self.sel_other.get(ch, "")

    def _cur(self, g=()) -> dict:
        name = self._selected(self._ch(g))
        if name not in self.meas:
            raise SimError(-221, "Settings conflict; no measurement selected", ESR_EXE)
        return self.meas[name]

    def _sel(self, a, g):
        name, ch = a.strip().strip("'\""), self._ch(g)
        if name not in self.meas or self.meas[name]["ch"] != ch:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if ch == 1:
            self.selected = name
        else:
            self.sel_other[ch] = name

    # ------------------------------------------------------------ масштаб и обработка
    def _place(self, g) -> tuple[int, int]:
        """(окно, трейс) выбранного измерения канала; (0, 0) - измерение не показано ни в одном окне."""
        name = self._cur(g) and self._selected(self._ch(g))
        for w, traces in self.windows.items():
            for t, n in traces.items():
                if n == name:
                    return w, t
        return 0, 0

    def _wt(self, g) -> dict:
        name = self.windows.get(int(g[0]), {}).get(int(g[1]))
        if name not in self.meas:
            raise SimError(-221, "Settings conflict; no such trace", ESR_EXE)
        return self.meas[name]

    def _scale_set(self, key, lo, hi):
        def fn(a, g):
            self._wt(g)[key] = self.num(a, lo, hi)
        return fn

    def _autoscale(self, a, g):
        m = self._wt(g)
        name = next(n for n, mm in self.meas.items() if mm is m)
        data = self.fdata(name)
        lo, hi = min(data), max(data)
        step = max((hi - lo) / 8, 1e-12)
        nice = 10 ** math.floor(math.log10(step))
        step = next(k * nice for k in (1, 2, 5, 10) if k * nice >= step)
        m.update(pdiv=step, rlev=round((hi + lo) / 2 / step) * step, rpos=5.0)

    def _meas_num(self, key, lo, hi):
        def fn(a, g):
            self._cur(g)[key] = self.num(a, lo, hi)
        return fn

    def _smo_set(self, a, g):
        self._cur(g)["smo"] = self.boolean(a)

    def _define(self, a, g):
        name, s = (t.strip().strip("'\"") for t in a.split(","))
        self.meas[name] = {"s": s.upper(), "fmt": "MLOG", "ch": self._ch(g)}

    def _modify(self, a, g):
        s = a.strip().strip("'\"").upper()
        if s not in ("S11", "S21", "S12", "S22"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur(g)["s"] = s

    def _fmt_set(self, a, g):
        t = a.strip().upper()
        if t not in _FMT:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur(g)["fmt"] = _FMT[t]

    def _data(self, a, g):
        kind = a.strip().upper()
        if kind not in ("FDATA", "SDATA"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self._cur(g)
        name = self._selected(self._ch(g))
        data = self.fdata(name) if kind == "FDATA" else self.sdata(name)
        return ",".join(f"{v:+.10E}" for v in data)

    def _mode_set(self, a, g):
        t = a.strip().upper()
        mode = {"HOLD": "HOLD", "CONT": "CONT", "CONTINUOUS": "CONT", "SING": "SING", "SINGLE": "SING"}.get(t)
        if mode is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.stim(self._ch(g))["mode"] = mode
        if mode == "SING":
            self.sweeping = True
            self.trig_count = 0
            if not self.hold_sweeps and self.trig["sour"] == "IMM":     # EXT / MAN - ждать триггера
                self.finish_sweep()
        elif mode == "HOLD":
            self.sweeping = False

    # ------------------------------------------------------------ запуск
    def _tenum(self, key, table):
        def fn(a, g):
            t = a.strip().upper()
            if t not in table:
                raise SimError(-224, "Illegal parameter value", ESR_EXE)
            self.trig[key] = table[t]
        return fn

    def _trig_delay(self, a, g):
        self.trig["del"] = self.num(a, 0, 3)

    def _tmode_set(self, a, g):
        t = a.strip().upper()
        mode = {"CHAN": "CHAN", "CHANNEL": "CHAN", "SWE": "SWE", "SWEEP": "SWE", "POIN": "POIN", "POINT": "POIN",
                "TRAC": "TRAC", "TRACE": "TRAC"}.get(t)
        if mode is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.stim(self._ch(g))["tmode"] = mode

    def trigger(self) -> None:
        """Импульс на входе внешнего запуска (или INIT:IMM при ручном): в режиме POINT - одна точка."""
        if not self.sweeping:
            return
        self.trig_count += 1
        points = self.c["poin"] if self.c["tmode"] == "POIN" else 1
        if self.trig_count >= points and not self.hold_sweeps:
            self.finish_sweep()

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
        if len(data) == 2 * len(fs):                      # комплексный формат
            return f"{data[2 * i]:+.10E},{data[2 * i + 1]:+.10E}"
        return f"{data[i]:+.10E},+0.00000000000E+000"

    def _mk_exec(self, a, g):
        data, fs = self.fdata(self.selected), self.freqs()
        if len(data) == 2 * len(fs):                      # комплексный формат - по модулю
            data = [math.hypot(a, b) for a, b in zip(data[0::2], data[1::2])]
        pick = max if a.strip().upper().startswith("MAX") else min
        i = pick(range(len(data)), key=data.__getitem__)
        self.marker.update(on=True, x=fs[i])

    # ------------------------------------------------------- память, математика
    def _memorize(self, a, g):
        m = self._cur(g)
        m["mem"] = [self.s(m["s"], f) for f in self.freqs(m["ch"])]

    def _math_set(self, a, g):
        t = a.strip().upper()
        fn = {"NORM": "NORM", "NORMAL": "NORM", "DIV": "DIV", "DIVIDE": "DIV", "SUBT": "SUBT",
              "SUBTRACT": "SUBT", "ADD": "ADD", "MULT": "MULT", "MULTIPLY": "MULT"}.get(t)
        if fn is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        m = self._cur(g)
        if fn != "NORM" and m.get("mem") is None:
            raise SimError(-221, "Settings conflict; no memory trace", ESR_EXE)
        m["math"] = fn

    # ----------------------------------------------------------- Touchstone
    def _snp(self, a, g):
        """Частоты, затем Re и Im каждого S-параметра (S11 S21 S12 S22) - столбцами, как в SnP-файле."""
        ports = sorted({int(p) for p in a.strip().strip("'\"").split(",") if p.strip()})
        if not ports or any(p not in (1, 2) for p in ports) or getattr(self, "snp_form", "RI") != "RI":
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        fs = self.freqs(self._ch(g))
        out = list(fs)
        for b in ports:
            for a_ in ports:
                vals = [self.s(f"S{a_}{b}", f) for f in fs]
                out += [v.real for v in vals] + [v.imag for v in vals]
        return ",".join(f"{v:+.11E}" for v in out)

    # ----------------------------------------------------------- калибровка
    def _corr_set(self, a, g):
        st = self.stim(self._ch(g))
        on = self.boolean(a)
        if on and not st["cset"]:
            raise SimError(-221, "Settings conflict; no calibration", ESR_EXE)
        st["corr"] = on

    def _cset_act(self, a, g):
        name, _, stim = a.rpartition(",")
        if not name:
            name, stim = stim, "0"
        name = name.strip().strip("'\"")
        if name not in self.csets:
            raise SimError(-224, "Illegal parameter value; no such cal set", ESR_EXE)
        st = self.stim(self._ch(g))
        cal = self.csets[name]
        if self.boolean(stim):
            st.update(cal)
            self._auto_time(st)
        st["cset"] = name
        same = all(st[k] == cal[k] for k in ("start", "stop", "poin", "type"))
        st["corr"] = same or (st["interp"] and st["type"] == cal["type"]
                              and cal["start"] <= st["start"] and st["stop"] <= cal["stop"])

    def _cset_deact(self, a, g):
        st = self.stim(self._ch(g))
        st["cset"], st["corr"] = "", False

    def _cset_del(self, a, g):
        name = a.strip().strip("'\"")
        if name not in self.csets:
            raise SimError(-224, "Illegal parameter value; no such cal set", ESR_EXE)
        del self.csets[name]
        for st in (self.c, *self.other.values()):
            if st.get("cset") == name:
                st["cset"], st["corr"] = "", False

    # ----------------------------------------------------------- калибровка (guided)
    @staticmethod
    def _family(conn: str) -> str:
        return next((f for f in KITS if conn.startswith(f)), "")

    def _guid_conn(self, a, g):
        conn = a.strip().strip("'\"")
        if conn not in CONNECTORS:
            raise SimError(-224, "Illegal parameter value; unknown connector", ESR_EXE)
        self.guid["conn"][int(g[1])] = conn

    def _guid_kits(self, a, g):
        return '"' + ",".join(KITS.get(self._family(a.strip().strip("'\"")), ())) + '"'

    def _guid_kit(self, a, g):
        port, kit = int(g[1]), a.strip().strip("'\"")
        if kit not in KITS.get(self._family(self.guid["conn"][port]), ()):
            raise SimError(-224, "Illegal parameter value; kit does not fit the connector", ESR_EXE)
        self.guid["kit"][port] = kit

    def _guid_init(self, a, g):
        gd = self.guid
        ports = [p for p in (1, 2) if gd["conn"][p] != "Not used"]
        if not ports or any(not gd["kit"][p] for p in ports):
            raise SimError(-221, "Settings conflict; cal ports not defined", ESR_EXE)
        steps = []
        if all("ECal" in gd["kit"][p] for p in ports):
            steps.append(f"Connect ECal module to port{'s' if len(ports) > 1 else ''} "
                         + " and ".join(str(p) for p in ports))
        else:
            for p in ports:
                steps += [f"Connect {gd['conn'][p]} {std} to port {p}" for std in ("OPEN", "SHORT", "LOAD")]
            if len(ports) == 2:
                steps.append("Connect THRU between port 1 and port 2")
        gd.update(steps=steps, done=set(), ch=self._ch(g))

    def _guid_desc(self, a, g):
        k = int(self.num(a, 1, max(1, len(self.guid["steps"]))))
        return f'"{self.guid["steps"][k - 1]}"'

    def _guid_acq(self, a, g):
        t = a.strip().upper()
        if not t.startswith("STAN") or not self.guid["steps"]:
            raise SimError(-221, "Settings conflict; guided cal not initiated", ESR_EXE)
        k = int(t[4:] or 0)
        if not 1 <= k <= len(self.guid["steps"]):
            raise SimError(-222, "Data out of range", ESR_EXE)
        self.guid["done"].add(k)
        self.sweeping = True                             # стандарт меряется развёрткой
        if not self.hold_sweeps:
            self.finish_sweep()

    def _guid_save(self, a, g):
        gd = self.guid
        if not gd["steps"] or len(gd["done"]) < len(gd["steps"]):
            raise SimError(-221, "Settings conflict; cal not complete", ESR_EXE)
        name = a.split(",")[0].strip().strip("'\"")
        st = self.stim(gd["ch"])
        self.csets[name] = {k: st[k] for k in ("start", "stop", "poin", "type", "cw", "bwid", "pow")}
        st["cset"], st["corr"] = name, True
        gd.update(steps=[], done=set())

    # ----------------------------------------------------------- файлы состояний
    @staticmethod
    def _file(a) -> str:
        name = a.strip().strip("'\"")
        if not name.lower().endswith((".csa", ".cst", ".sta", ".cal")):
            raise SimError(-256, "File name not found", ESR_EXE)
        return name

    def _st_store(self, a, g):
        self.files[self._file(a)] = copy.deepcopy((self.c, self.meas, self.other, self.marker))

    def _st_load(self, a, g):
        name = self._file(a)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        c, self.meas, self.other, self.marker = copy.deepcopy(self.files[name])
        c["form"] = self.c["form"]
        self.c = c
        self.selected, self.sel_other = "", {}
        self.sweeping = False

    def subdirs(self) -> list[str]:
        """Имена папок внутри текущей (папки-предки известных папок тоже существуют)."""
        pre, out = self.cdir.lower() + "\\", set()
        for d in self.dirs:
            if d.lower().startswith(pre):
                out.add(d[len(pre):].split("\\")[0])
        return sorted(out)

    def _cdir(self, a, g):
        path = a.strip().strip("'\"").rstrip("\\")
        key = next((d for d in self.dirs if d.lower() == path.lower()), None)
        if key is None and any(d.lower().startswith(path.lower() + "\\") for d in self.dirs) and path:
            key = path                                   # папка-предок: есть, файлов в ней нет
            self.dirs[key] = {}
        if key is None:
            raise SimError(-256, "File name not found", ESR_EXE)
        self.cdir, self.files = key, self.dirs[key]

    def _file_del(self, a, g):
        name = self._file(a)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        del self.files[name]

    # ----------------------------------------------------------------- CW
    def _type_set(self, a, g):
        t = {"LIN": "LIN", "LINEAR": "LIN", "LOG": "LOG", "LOGARITHMIC": "LOG", "POW": "POW", "POWER": "POW",
             "CW": "CW", "SEGM": "SEGM", "SEGMENT": "SEGM"}.get(a.strip().upper())
        if t is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.stim(self._ch(g))["type"] = t

    def _cw_set(self, a, g):
        self.stim(self._ch(g))["cw"] = self.num(a, self.F_MIN, self.F_MAX)

    # ----------------------------------------------------- создание / удаление
    def _delete(self, a, g):
        name = a.strip().strip("'\"")
        if name not in self.meas or self.meas[name]["ch"] != self._ch(g):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        del self.meas[name]
        for traces in self.windows.values():
            for t in [t for t, n in traces.items() if n == name]:
                del traces[t]
        if self.selected == name:
            self.selected = ""
        self.sel_other = {c: n for c, n in self.sel_other.items() if n != name}

    def _win_state(self, a, g):
        w = int(g[0])
        if self.boolean(a):
            self.windows.setdefault(w, {})
        else:
            self.windows.pop(w, None)

    def _win_cat(self, a, g):
        traces = self.windows.get(int(g[0]))
        return '"' + (",".join(str(t) for t in sorted(traces)) if traces else "EMPTY") + '"'

    def _feed(self, a, g):
        w, t = int(g[0]), int(g[1])
        name = a.strip().strip("'\"")
        if w not in self.windows or name not in self.meas or self.windows[w].get(t) in self.meas:
            raise SimError(-221, "Settings conflict", ESR_EXE)
        self.windows[w][t] = name
