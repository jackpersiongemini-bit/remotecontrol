"""Симулятор ВАЦ Keysight PNA / PNA-L (2 или 4 порта, по умолчанию 4 порта N5230C-420).

DUT: резонатор с центральной частотой ``f0``, нагруженной добротностью ``q`` и потерями ``il_db``:
* S21 = S12 = k / (1 + jQ(f/f0 - f0/f));
* для остальных пар передачи (S31, S41, S32, S42, S43...) — тот же резонатор с добавочными потерями
  и фазовым сдвигом, зависящим от пары портов (удобно проверять чтение S41, S31 и т.д.);
* для отражения (S11, S22, S33, S44) = 1 - s21/k * 0.98;
* ``dut = None`` — открытые порты (передача ~ -100 dB, отражение ~ 0 dB).

Поддерживает двоичную передачу IEEE 488.2 (``FORM:DATA REAL,64|REAL,32|ASC``, ``FORM:BORD NORM|SWAP``)
для ``CALC#:DATA? SDATA|FDATA``, ``SENS#:X?``, ``CALC#:X?`` и ``CALC#:DATA:SNP:PORT?``.
"""
from __future__ import annotations

import cmath
import copy
import math
import re

from ..transport import binary_block
from .base import ESR_EXE, ESR_OPC, SimError, SimInstrument

__all__ = ["KeysightPNASim"]

_FMT = {"MLOG": "MLOG", "PHAS": "PHAS", "PHASE": "PHAS", "UPH": "UPH", "UPHASE": "UPH", "GDEL": "GDEL",
        "GDELAY": "GDEL", "SWR": "SWR", "MLIN": "MLIN", "REAL": "REAL", "IMAG": "IMAG", "POL": "POL", "POLAR": "POL",
        "SMIT": "SMIT", "SMITH": "SMIT"}

_SPARAM_RE = re.compile(r"^S(\d)(\d)$")

DEFAULT_DIR = "C:\\Users\\Public\\Documents\\Network Analyzer"
CONNECTORS = ("APC 3.5 female", "APC 3.5 male", "Type N (50) female", "Type N (50) male", "Not used")
#: семейство разъёма -> наборы (идеальный - как пользовательское определение набора на приборе)
KITS = {"APC 3.5": ("85052D", "Ideal 3.5mm", "N4691-60004 ECal"), "Type N": ("85054D", "Ideal Type N")}


class KeysightPNASim(SimInstrument):
    IDN = "Keysight Technologies,N5230C,MY00000001,A.09.90.02"
    F_MIN, F_MAX = 300e3, 20e9

    def __init__(self, ports: int = 4):
        self.ports = max(1, min(4, int(ports)))
        if self.ports == 2:
            self.IDN = "Agilent Technologies,N5231A,MY00000001,A.09.90.02"
            self.F_MAX = 13.5e9
        super().__init__()

    def _valid_sparam(self, text: str) -> str:
        s = text.strip().strip("'\"").upper()
        m = _SPARAM_RE.match(s)
        if not m:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        a, b = int(m.group(1)), int(m.group(2))
        if not (1 <= a <= self.ports and 1 <= b <= self.ports):
            raise SimError(-224, f"Illegal parameter value; port out of range 1..{self.ports}", ESR_EXE)
        return s

    def _new_stim(self, start: float = 10e6, stop: float | None = None, poin: int = 201) -> dict:
        if stop is None:
            stop = self.F_MAX
        st: dict = {
            "start": start, "stop": stop, "poin": poin, "bwid": 35e3,
            "pow": -5.0,
            "pow_levels": {p: -5.0 for p in range(1, self.ports + 1)},
            "pow_modes": {p: "AUTO" for p in range(1, self.ports + 1)},
            "pow_coup": True, "outp": True,
            "pow_start": -20.0, "pow_stop": 0.0,
            "swt": 0.02, "swt_auto": True, "aver": False, "coun": 1, "gro_coun": 1,
            "corr": False, "mode": "CONT", "tmode": "CHAN",
            "type": "LIN", "cw": 1e9, "cset": "", "interp": True,
        }
        self._auto_time(st)
        return st

    def reset(self) -> None:
        self.c = self._new_stim(10e6, self.F_MAX, 201)
        self.form_data = ("ASC", 0)                       # ("ASC", 0) | ("REAL", 32) | ("REAL", 64)
        self.form_bord = "NORM"                           # "NORM" (big-endian) | "SWAP" (little-endian)
        self.meas = {"CH1_S11_1": {"s": "S11", "fmt": "MLOG", "ch": 1}}
        self.selected = ""                                # выбранное измерение канала 1
        self.sel_other: dict[int, str] = {}               # то же у остальных каналов
        self.other: dict[int, dict] = {}                  # стимул каналов 2...
        self.trig = {"sour": "IMM", "scop": "ALL", "slop": "POS", "type": "EDGE", "del": 0.0}
        self.trig_count = 0                               # триггеров в текущей развёртке (режим POINT)
        self.windows: dict[int, dict[int, str]] = {1: {1: "CH1_S11_1"}}
        self.sweeping = True
        self.opc_pending = False
        self.sweeps_done = 0
        self.oper_ptr = 0
        self.oper_ntr = 0
        self.oper_even = 0
        self.oper_cond = 16
        if not hasattr(self, "dirs"):                     # папки и файлы переживают *RST
            self.dirs: dict[str, dict[str, tuple]] = {DEFAULT_DIR: {}, DEFAULT_DIR + "\\Lab": {}}
            self.cdir = DEFAULT_DIR
            self.files: dict[str, tuple] = self.dirs[self.cdir]
        if not hasattr(self, "csets"):                    # cal set'ы переживают *RST
            self.csets: dict[str, dict] = {
                "CAL_1-3G": {"start": 1e9, "stop": 3e9, "poin": 201, "type": "LIN", "cw": 1e9, "bwid": 1e3,
                             "pow": -10.0},
                "CAL_FULL": {"start": 10e6, "stop": self.F_MAX, "poin": 401, "type": "LIN", "cw": 1e9, "bwid": 10e3,
                             "pow": -5.0}}
        self.snp_form = "MA"
        self.coll: dict = {}
        self.guid = {
            "conn": {p: "Not used" for p in range(1, self.ports + 1)},
            "kit": {p: "" for p in range(1, self.ports + 1)},
            "steps": [], "done": set(), "ch": 1,
        }
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
        m = _SPARAM_RE.match(name.upper())
        a, b = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        if d is None:                                     # порты открыты: передачи нет, полное отражение
            return complex(0.999, 0) if a == b else complex(1e-5, 0)
        k = 10 ** (-d["il_db"] / 20)
        s21 = k / complex(1, d["q"] * (f / d["f0"] - d["f0"] / f))
        if a == b:
            return 1 - s21 / k * 0.98
        if {a, b} == {1, 2}:
            return s21
        # для портов 3 и 4 — реалистично отличимая передача (доп. потери и фазовый сдвиг)
        extra_db = 2.0 * (abs(a - b) - 1) + 0.5 * (max(a, b) - 2)
        phase_rad = 0.25 * (a + b)
        return s21 * (10 ** (-extra_db / 20)) * cmath.exp(1j * phase_rad)

    def stim(self, ch: int) -> dict:
        """Стимул канала: канал 1 - ``self.c``, остальные создаются при первом обращении."""
        if ch == 1:
            return self.c
        if ch not in self.other:
            self.other[ch] = self._new_stim(1e9, 2e9, 101)
        return self.other[ch]

    def x_axis(self, ch: int = 1) -> list[float]:
        """Точная ось X канала (SENS#:X?): частоты (LIN/LOG/SEGM), мощность (POW) или время (CW)."""
        st = self.stim(ch)
        n = st["poin"]
        typ = st["type"]
        if typ == "CW":
            swt = st["swt"]
            return [swt * i / (n - 1) if n > 1 else 0.0 for i in range(n)]
        if typ == "POW":
            a, b = st["pow_start"], st["pow_stop"]
            return [a + (b - a) * i / (n - 1) if n > 1 else a for i in range(n)]
        a, b = st["start"], st["stop"]
        if typ == "LOG" and a > 0 and b > 0 and n > 1:
            ratio = b / a
            return [a * (ratio ** (i / (n - 1))) for i in range(n)]
        return [a + (b - a) * i / (n - 1) if n > 1 else a for i in range(n)]

    def freqs(self, ch: int = 1) -> list[float]:
        """Частоты в точках развёртки (для расчёта S(f) DUT)."""
        st = self.stim(ch)
        n = st["poin"]
        if st["type"] in ("CW", "POW"):
            return [st["cw"]] * n
        return self.x_axis(ch)

    def _encode_numbers(self, values: list[float]) -> str:
        """Упаковать массив чисел по текущему ``FORM:DATA`` / ``FORM:BORD``."""
        kind, bits = self.form_data
        if kind == "REAL":
            datatype = "f" if bits == 32 else "d"
            return binary_block(values, datatype=datatype, big_endian=(self.form_bord == "NORM"))
        return ",".join(f"{v:+.11E}" for v in values)

    def sdata(self, name: str) -> list[float]:
        """Комплексные данные измерения: re, im, re, im, ..."""
        out: list[float] = []
        for f in self.freqs(self.meas[name]["ch"]):
            v = self.s(self.meas[name]["s"], f)
            out += [v.real, v.imag]
        return out

    def fdata(self, name: str) -> list[float]:
        m = self.meas[name]
        fs = self.freqs(m["ch"])
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
        if fmt in ("POL", "SMIT"):
            return [x for v in vals for x in (v.real, v.imag)]
        if fmt == "REAL":
            return [v.real for v in vals]
        return [v.imag for v in vals]

    def _has_cont(self) -> bool:
        return any(st["mode"] == "CONT" for st in (self.c, *self.other.values()))

    def finish_sweep(self) -> None:
        if not self.sweeping:
            return
        self.sweeping = False
        self.sweeps_done += 1
        self.oper_cond &= ~16
        if self.oper_ntr & 16:
            self.oper_even |= 16
        for st in (self.c, *self.other.values()):       # однократные / групповые развёртки закончились
            if st["mode"] in ("SING", "GRO"):
                st["mode"] = "HOLD"
        if self.opc_pending:
            self.opc_pending = False
            self.esr |= ESR_OPC
        if self._has_cont():
            self.sweeping = True
            self.trig_count = 0
            self.oper_cond |= 16

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
        self.on("SYSTem:CAPabilities:HARDware:PORT:COUNt", get=lambda a, g: str(self.ports))
        self.on("SYSTem:CHANnels:CATalog", get=lambda a, g: '"' + ",".join(
            str(c) for c in sorted({1} | {m["ch"] for m in self.meas.values()} | set(self.other))) + '"')
        self.on("FORMat[:DATA]", self._form_set, self._form_get)
        self.on("FORMat:BORDer", self._bord_set, lambda a, g: self.form_bord)
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
        self.on("CALCulate#:DATA", get=self._data)
        self.on("CALCulate#:X", get=lambda a, g: self._encode_numbers(self.x_axis(self._ch(g))))
        self.on("SENSe#:X[:VALues]", get=lambda a, g: self._encode_numbers(self.x_axis(self._ch(g))))
        self.on("MMEMory:STORe[:FILE]", self._st_store)
        self.on("MMEMory:LOAD[:FILE]", self._st_load)
        self.on("MMEMory:CATalog", get=lambda a, g: '"' + ",".join(sorted(self.files) + self.subdirs()) + '"')
        self.on("MMEMory:DELete", self._file_del)
        self.on("MMEMory:CDIRectory", self._cdir, lambda a, g: f'"{self.cdir}"')
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
        self.on("SENSe#:SWEep:GROup:COUNt", self._n("gro_coun", 1, 65536, integer=True),
                lambda a, g: str(self.stim(self._ch(g))["gro_coun"]))
        self.on("SENSe#:SWEep:MODE", self._mode_set, lambda a, g: self.stim(self._ch(g))["mode"])
        self.on("SENSe#:BWIDth[:RESolution]", self._bwid_set, self._gs("bwid"))
        self.on("SENSe#:AVERage:COUNt", self._n("coun", 1, 65536, integer=True),
                lambda a, g: str(self.stim(self._ch(g))["coun"]))
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
        self.on("SENSe#:CORRection:COLLect:METHod", self._coll_meth, lambda a, g: self.coll.get("meth", "NONE"))
        self.on("SENSe#:CORRection:COLLect[:ACQuire]", self._coll_acq)
        self.on("SENSe#:CORRection:COLLect:SAVE", self._coll_save)
        self.on("SENSe#:CORRection:CSET:COPY", self._cset_copy)
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
        self.on("SOURce#:POWer:COUPle", self._pow_coup_set, self._gb("pow_coup"))
        self.on("SOURce#:POWer#:MODE", self._pow_mode_set, self._pow_mode_get)
        self.on("SOURce#:POWer#[:LEVel][:IMMediate][:AMPLitude]", self._pow_set, self._pow_get)
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
        self.on("STATus:OPERation:PTRansition", lambda a, g: setattr(self, "oper_ptr", int(self.num(a, 0, 65535))),
                lambda a, g: str(self.oper_ptr))
        self.on("STATus:OPERation:NTRansition", lambda a, g: setattr(self, "oper_ntr", int(self.num(a, 0, 65535))),
                lambda a, g: str(self.oper_ntr))
        self.on("STATus:OPERation:CONDition", get=lambda a, g: str(self.oper_cond))
        self.on("STATus:OPERation[:EVENt]", get=self._oper_even_get)

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

    def _gb(self, k):
        return lambda a, g: "1" if self.stim(self._ch(g))[k] else "0"

    def _b(self, k):
        def fn(a, g):
            st = self.stim(self._ch(g))
            st[k] = self.boolean(a)
            self._auto_time(st)
        return fn

    def _pow_coup_set(self, a, g):
        st = self.stim(self._ch(g))
        st["pow_coup"] = self.boolean(a)

    def _pow_port(self, g) -> int:
        p = int(g[1]) if len(g) > 1 and g[1] else 1
        if not 1 <= p <= self.ports:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        return p

    def _pow_set(self, a, g):
        v = self.num(a, -90, 20)
        st = self.stim(self._ch(g))
        p = self._pow_port(g)
        if st["pow_coup"]:
            for k in range(1, self.ports + 1):
                st["pow_levels"][k] = v
        else:
            st["pow_levels"][p] = v
        st["pow"] = st["pow_levels"][1]

    def _pow_get(self, a, g):
        st = self.stim(self._ch(g))
        p = self._pow_port(g)
        return f"{st['pow_levels'][p]:+.11E}"

    def _pow_mode_set(self, a, g):
        tok = a.strip().upper()
        if tok not in ("AUTO", "ON", "OFF"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        st = self.stim(self._ch(g))
        st["pow_modes"][self._pow_port(g)] = tok

    def _pow_mode_get(self, a, g):
        st = self.stim(self._ch(g))
        return st["pow_modes"][self._pow_port(g)]

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
        st["bwid"] = min(steps, key=lambda s: abs(s - v))
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
        parts = [p.strip().upper() for p in a.split(",") if p.strip()]
        if not parts:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if parts[0].startswith("ASC"):
            self.form_data = ("ASC", 0)
            return
        if parts[0].startswith("REAL"):
            bits = int(parts[1]) if len(parts) > 1 else 64
            if bits not in (32, 64):
                raise SimError(-224, "Illegal parameter value", ESR_EXE)
            self.form_data = ("REAL", bits)
            return
        raise SimError(-224, "Illegal parameter value", ESR_EXE)

    def _form_get(self, a, g) -> str:
        kind, bits = self.form_data
        return f"{kind},{bits}"

    def _bord_set(self, a, g):
        t = a.strip().upper()
        if t.startswith("NORM"):
            self.form_bord = "NORM"
        elif t.startswith("SWAP"):
            self.form_bord = "SWAP"
        else:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)

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

    def _define(self, a, g):
        parts = [t.strip().strip("'\"") for t in a.split(",")]
        if len(parts) != 2 or not parts[0]:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        name, sp = parts[0], self._valid_sparam(parts[1])
        self.meas[name] = {"s": sp, "fmt": "MLOG", "ch": self._ch(g)}

    def _modify(self, a, g):
        self._cur(g)["s"] = self._valid_sparam(a)

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
        return self._encode_numbers(data)

    def _oper_even_get(self, a, g) -> str:
        val = self.oper_even
        self.oper_even = 0
        if self._has_cont() and self.trig["sour"] == "IMM" and not self.hold_sweeps:
            if not self.sweeping:
                self.sweeping = True
                self.oper_cond |= 16
            self.finish_sweep()
        return str(val)

    def _mode_set(self, a, g):
        t = a.strip().upper()
        mode = {
            "HOLD": "HOLD",
            "CONT": "CONT", "CONTINUOUS": "CONT",
            "SING": "SING", "SINGLE": "SING",
            "GRO": "GRO", "GROUPS": "GRO",
        }.get(t)
        if mode is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.stim(self._ch(g))["mode"] = mode
        if mode in ("SING", "CONT", "GRO"):
            self.sweeping = True
            self.trig_count = 0
            self.oper_cond |= 16
            if not self.hold_sweeps and self.trig["sour"] == "IMM":
                self.finish_sweep()
        elif mode == "HOLD":
            self.sweeping = self._has_cont()
            if not self.sweeping:
                self.oper_cond &= ~16

    # ------------------------------------------------------------ запуск
    def _tenum(self, key, table):
        def fn(a, g):
            t = a.strip().upper()
            if t not in table:
                raise SimError(-224, "Illegal parameter value", ESR_EXE)
            self.trig[key] = table[t]
            if key == "sour" and self.trig["sour"] == "IMM" and self.sweeping and not self.hold_sweeps:
                self.finish_sweep()
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

    # ----------------------------------------------------------- калибровка без мастера (response THRU)
    def _coll_meth(self, a, g):
        meth = a.strip().upper()
        if meth not in ("TRAN1", "TRAN2", "REFL1", "REFL3", "SPARSOLT"):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.coll = {"meth": meth, "ch": self._ch(g), "done": set()}

    def _coll_acq(self, a, g):
        std = a.strip().upper()
        if self.coll.get("meth") != "TRAN1" or std != "THRU":
            raise SimError(-221, "Settings conflict; calibration method", ESR_EXE)
        self.coll["done"].add(std)
        self.sweeping = True
        if not self.hold_sweeps:
            self.finish_sweep()

    def _coll_save(self, a, g):
        if "THRU" not in self.coll.get("done", ()):
            raise SimError(-221, "Settings conflict; cal not complete", ESR_EXE)
        ch = self.coll["ch"]
        st = self.stim(ch)
        name = f"CH{ch}_CALREG"
        self.csets[name] = {k: st[k] for k in ("start", "stop", "poin", "type", "cw", "bwid", "pow")}
        st["cset"], st["corr"] = name, True
        self.coll = {}

    def _cset_copy(self, a, g):
        st = self.stim(self._ch(g))
        if not st["cset"]:
            raise SimError(-221, "Settings conflict; no cal set", ESR_EXE)
        self.csets[a.strip().strip("'\"")] = dict(self.csets[st["cset"]])

    # ----------------------------------------------------------- Touchstone (1..4 порта)
    def _snp(self, a, g):
        """Частоты, затем Re и Im каждого S-параметра столбцами (порядок Touchstone 1.x):
        1 порт - S11; 2 порта - S11 S21 S12 S22; 3/4 порта - построчно S{r}{c}."""
        plist = sorted({int(p) for p in a.strip().strip("'\"").split(",") if p.strip()})
        if not plist or any(not (1 <= p <= self.ports) for p in plist) or getattr(self, "snp_form", "RI") != "RI":
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if len(plist) == 2:
            p1, p2 = plist
            names = [f"S{p1}{p1}", f"S{p2}{p1}", f"S{p1}{p2}", f"S{p2}{p2}"]
        else:
            names = [f"S{r}{c}" for r in plist for c in plist]
        fs = self.freqs(self._ch(g))
        out = list(fs)
        for sp in names:
            vals = [self.s(sp, f) for f in fs]
            out += [v.real for v in vals] + [v.imag for v in vals]
        return self._encode_numbers(out)

    # ----------------------------------------------------------- калибровка
    def _corr_set(self, a, g):
        ch = self._ch(g)
        st = self.stim(ch)
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
        ch = self._ch(g)
        st = self.stim(ch)
        cal = self.csets[name]
        if self.boolean(stim):
            st.update(cal)
            if "pow" in cal:
                for p in range(1, self.ports + 1):
                    st["pow_levels"][p] = cal["pow"]
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

    # ----------------------------------------------------------- калибровка (guided, 1..4 порта)
    @staticmethod
    def _family(conn: str) -> str:
        return next((f for f in KITS if conn.startswith(f)), "")

    def _guid_conn(self, a, g):
        port = int(g[1])
        if not 1 <= port <= self.ports:
            raise SimError(-224, "Illegal parameter value; invalid port", ESR_EXE)
        conn = a.strip().strip("'\"")
        if conn not in CONNECTORS:
            raise SimError(-224, "Illegal parameter value; unknown connector", ESR_EXE)
        self.guid["conn"][port] = conn

    def _guid_kits(self, a, g):
        return '"' + ",".join(KITS.get(self._family(a.strip().strip("'\"")), ())) + '"'

    def _guid_kit(self, a, g):
        port, kit = int(g[1]), a.strip().strip("'\"")
        if not 1 <= port <= self.ports:
            raise SimError(-224, "Illegal parameter value; invalid port", ESR_EXE)
        if kit not in KITS.get(self._family(self.guid["conn"][port]), ()):
            raise SimError(-224, "Illegal parameter value; kit does not fit the connector", ESR_EXE)
        self.guid["kit"][port] = kit

    def _guid_init(self, a, g):
        gd = self.guid
        ports = [p for p in range(1, self.ports + 1) if gd["conn"][p] != "Not used"]
        if not ports or any(not gd["kit"][p] for p in ports):
            raise SimError(-221, "Settings conflict; cal ports not defined", ESR_EXE)
        steps = []
        if all("ECal" in gd["kit"][p] for p in ports):
            steps.append(f"Connect ECal module to port{'s' if len(ports) > 1 else ''} "
                         + " and ".join(str(p) for p in ports))
        else:
            for p in ports:
                steps += [f"Connect {gd['conn'][p]} {std} to port {p}" for std in ("OPEN", "SHORT", "LOAD")]
            if len(ports) >= 2:
                # для N портов достаточно N-1 перемычек (от первого порта к каждому остальному)
                p0 = ports[0]
                for pj in ports[1:]:
                    steps.append(f"Connect THRU between port {p0} and port {pj}")
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
        self.sweeping = True
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
        self.files[self._file(a)] = copy.deepcopy((self.c, self.meas, self.other, self.windows))

    def _st_load(self, a, g):
        name = self._file(a)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        c, self.meas, self.other, self.windows = copy.deepcopy(self.files[name])
        self.c = c
        self.selected, self.sel_other = "", {}
        self.sweeping = False

    def subdirs(self) -> list[str]:
        pre, out = self.cdir.lower() + "\\", set()
        for d in self.dirs:
            if d.lower().startswith(pre):
                out.add(d[len(pre):].split("\\")[0])
        return sorted(out)

    def _cdir(self, a, g):
        path = a.strip().strip("'\"").rstrip("\\")
        key = next((d for d in self.dirs if d.lower() == path.lower()), None)
        if key is None and any(d.lower().startswith(path.lower() + "\\") for d in self.dirs) and path:
            key = path
            self.dirs[key] = {}
        if key is None:
            raise SimError(-256, "File name not found", ESR_EXE)
        self.cdir, self.files = key, self.dirs[key]

    def _file_del(self, a, g):
        name = self._file(a)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        del self.files[name]

    # ----------------------------------------------------------------- CW / тип развёртки
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
