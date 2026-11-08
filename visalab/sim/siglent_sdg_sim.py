"""Симулятор Siglent SDG (2 канала) по мануалу SDG Series.

Поведение, полезное для тестов драйвера:
* ответы с заголовками и единицами: ``C1:BSWV WVTP,SINE,FRQ,1000HZ,...``;
* частота выше максимума модели и амплитуда выше предела нагрузки молча
  ограничиваются (как у прибора) -> драйвер должен показать COERCED;
* неверный параметр -> бит EXE в *ESR, неизвестная команда -> CME (+ CMR код);
* модуляция/свип/пакет взаимоисключающие.
"""
from __future__ import annotations

from copy import deepcopy

from ..core.scpi import parse_number
from .base import ESR_CME, ESR_EXE, SimError, SimInstrument

WAVES = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC")
MOD_FIELDS = {
    "AM": ("SRC", "MDSP", "FRQ", "DEPTH"), "DSBAM": ("SRC", "MDSP", "FRQ"),
    "FM": ("SRC", "MDSP", "FRQ", "DEVI"), "PM": ("SRC", "MDSP", "FRQ", "DEVI"),
    "PWM": ("SRC", "MDSP", "FRQ", "DEVI"), "ASK": ("SRC", "KFRQ"),
    "FSK": ("SRC", "KFRQ", "HFRQ"), "PSK": ("SRC", "KFRQ"),
}
_UNITS = {"FRQ": "HZ", "PERI": "S", "AMP": "V", "OFST": "V", "HLEV": "V", "LLEV": "V",
          "WIDTH": "S", "RISE": "S", "FALL": "S", "DLY": "S", "KFRQ": "HZ", "HFRQ": "HZ",
          "TIME": "S", "START": "HZ", "STOP": "HZ", "PRD": "S", "DLAY": "S"}


def _fmt(key: str, value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}{_UNITS.get(key, '')}"
    return str(value)


class SiglentSDGSim(SimInstrument):
    IDN = "Siglent Technologies,SDG2042X,SDG2XSIM000001,2.01.01.35R3"
    MAX_FREQ = 40e6
    CMR_SUPPORTED = False
    ECHO_COMMON_HEADER = True    # "*ESR 0", "*OPC 1" - как в мануале

    def __init__(self, idn: str | None = None, max_freq: float | None = None):
        if idn:
            self.IDN = idn
        if max_freq:
            self.MAX_FREQ = max_freq
        self.cmr = 0
        super().__init__()

    # Siglent не имеет SYST:ERR? - ошибки только в ESR (и CMR)
    def error(self, code: int, message: str, esr_bit: int) -> None:
        self.esr |= esr_bit
        if esr_bit == ESR_CME:
            self.cmr = 1

    def reset(self) -> None:
        base = {"WVTP": "SINE", "FRQ": 1000.0, "AMP": 4.0, "OFST": 0.0, "PHSE": 0.0,
                "DUTY": 50.0, "SYM": 50.0, "WIDTH": 0.0002, "DLY": 0.0}
        mod = {"STATE": "OFF", "TYPE": "AM",
               **{t: {f: ("INT" if f == "SRC" else "SINE" if f == "MDSP" else
                          100.0 if f in ("FRQ", "DEPTH", "KFRQ", "DEVI") else 10000.0)
                      for f in fields} for t, fields in MOD_FIELDS.items()}}
        sweep = {"STATE": "OFF", "TIME": 1.0, "STOP": 1500.0, "START": 500.0, "TRSR": "INT",
                 "TRMD": "OFF", "SWMD": "LINE", "DIR": "UP"}
        burst = {"STATE": "OFF", "PRD": 0.01, "STPS": 0.0, "TRSR": "INT", "TRMD": "OFF",
                 "TIME": "1", "DLAY": 0.0, "GATE_NCYC": "NCYC"}
        self.ch = {n: {"BSWV": dict(base), "OUTP": {"STATE": "OFF", "LOAD": "HZ", "PLRT": "NOR"},
                       "SYNC": "OFF", "MDWV": deepcopy(mod), "SWWV": dict(sweep), "BTWV": dict(burst)}
                   for n in (1, 2)}

    def register(self) -> None:
        self.on("C#:BSWV", self._bswv_set, self._bswv_get)
        self.on("C#:BASIC_WAVE", self._bswv_set, self._bswv_get)
        self.on("C#:OUTP", self._outp_set, self._outp_get)
        self.on("C#:OUTPUT", self._outp_set, self._outp_get)
        self.on("C#:SYNC", self._sync_set, lambda a, g: f"C{g[0]}:SYNC {self._c(g)['SYNC']}")
        self.on("C#:MDWV", self._mdwv_set, self._mdwv_get)
        self.on("C#:SWWV", self._swwv_set, self._swwv_get)
        self.on("C#:BTWV", self._btwv_set, self._btwv_get)
        self.on("PACP", self._pacp)
        self.on("EQPHASE", lambda a, g: setattr(self, "eqphase_count", getattr(self, "eqphase_count", 0) + 1))
        if self.CMR_SUPPORTED:
            self.on("CMR", get=self._cmr_get)

    # ------------------------------------------------------------- помощники
    def _c(self, g) -> dict:
        n = int(g[0])
        if n not in self.ch:
            raise SimError(-1, "bad channel", ESR_EXE)
        return self.ch[n]

    @staticmethod
    def _tokens(args: str) -> list[str]:
        return [t.strip() for t in args.split(",") if t.strip()]

    def _max_amp(self, c: dict) -> float:
        return 20.0 if c["OUTP"]["LOAD"] == "HZ" else 10.0

    # --------------------------------------------------------------- BSWV
    def _bswv_apply(self, c: dict, key: str, raw: str) -> None:
        b = c["BSWV"]
        key = key.upper()
        if key == "WVTP":
            w = raw.upper()
            if w not in WAVES:
                raise SimError(-1, "bad wave", ESR_EXE)
            b["WVTP"] = w
            return
        v = parse_number(raw)
        if key == "FRQ":
            if v <= 0:
                raise SimError(-1, "bad freq", ESR_EXE)
            b["FRQ"] = min(v, self.MAX_FREQ)
        elif key == "PERI":
            if v <= 0:
                raise SimError(-1, "bad period", ESR_EXE)
            b["FRQ"] = min(1.0 / v, self.MAX_FREQ)
        elif key == "AMP":
            if v < 0:
                raise SimError(-1, "bad amp", ESR_EXE)
            b["AMP"] = min(max(v, 0.002), self._max_amp(c))
        elif key == "OFST":
            b["OFST"] = v
        elif key in ("HLEV", "LLEV"):
            hi = b["OFST"] + b["AMP"] / 2
            lo = b["OFST"] - b["AMP"] / 2
            hi, lo = (v, lo) if key == "HLEV" else (hi, v)
            if hi <= lo:
                raise SimError(-1, "high<=low", ESR_EXE)
            b["AMP"] = min(hi - lo, self._max_amp(c))
            b["OFST"] = (hi + lo) / 2
        elif key == "PHSE":
            b["PHSE"] = v % 360
        elif key in ("DUTY", "SYM"):
            if not 0 <= v <= 100:
                raise SimError(-1, "bad %", ESR_EXE)
            b[key] = v
        elif key in ("WIDTH", "RISE", "FALL", "DLY", "STDEV", "MEAN"):
            b[key] = v
        else:
            raise SimError(-1, "bad key", ESR_EXE)

    def _bswv_set(self, args, g):
        c = self._c(g)
        t = self._tokens(args)
        if len(t) % 2:
            raise SimError(-1, "missing parameter", ESR_EXE)
        for k, v in zip(t[::2], t[1::2]):
            self._bswv_apply(c, k, v)

    def _bswv_fields(self, c: dict) -> list[tuple[str, object]]:
        b = c["BSWV"]
        w = b["WVTP"]
        if w == "DC":
            return [("WVTP", w), ("OFST", b["OFST"])]
        if w == "NOISE":
            return [("WVTP", w), ("STDEV", 0.5), ("MEAN", 0.0)]
        f = [("WVTP", w), ("FRQ", b["FRQ"]), ("PERI", 1.0 / b["FRQ"]), ("AMP", b["AMP"]),
             ("OFST", b["OFST"]), ("HLEV", b["OFST"] + b["AMP"] / 2), ("LLEV", b["OFST"] - b["AMP"] / 2)]
        if w != "PULSE":
            f.append(("PHSE", b["PHSE"]))
        if w in ("SQUARE", "PULSE"):
            f.append(("DUTY", b["DUTY"]))
        if w == "RAMP":
            f.append(("SYM", b["SYM"]))
        if w == "PULSE":
            f.append(("WIDTH", b["WIDTH"]))
            f.append(("DLY", b.get("DLY", 0.0)))
        return f

    def _bswv_get(self, args, g):
        c = self._c(g)
        return f"C{g[0]}:BSWV " + ",".join(f"{k},{_fmt(k, v)}" for k, v in self._bswv_fields(c))

    # --------------------------------------------------------------- OUTP
    def _outp_set(self, args, g):
        o = self._c(g)["OUTP"]
        t = self._tokens(args)
        i = 0
        while i < len(t):
            k = t[i].upper()
            if k in ("ON", "OFF"):
                o["STATE"] = k
                i += 1
            elif k == "LOAD":
                v = t[i + 1].upper()
                if v != "HZ":
                    n = parse_number(v)
                    if not 50 <= n <= 100000:
                        raise SimError(-1, "bad load", ESR_EXE)
                    v = f"{n:g}"
                o["LOAD"] = v
                b = self._c(g)["BSWV"]
                b["AMP"] = min(b["AMP"], self._max_amp(self._c(g)))
                i += 2
            elif k == "PLRT":
                v = t[i + 1].upper()
                if v not in ("NOR", "INVT"):
                    raise SimError(-1, "bad plrt", ESR_EXE)
                o["PLRT"] = v
                i += 2
            else:
                raise SimError(-1, "bad outp", ESR_EXE)

    def _outp_get(self, args, g):
        o = self._c(g)["OUTP"]
        return f"C{g[0]}:OUTP {o['STATE']},LOAD,{o['LOAD']},PLRT,{o['PLRT']}"

    def _sync_set(self, args, g):
        v = args.strip().upper()
        if v not in ("ON", "OFF"):
            raise SimError(-1, "bad sync", ESR_EXE)
        self._c(g)["SYNC"] = v

    # ------------------------------------------------- режимы MDWV/SWWV/BTWV
    def _exclusive_on(self, c: dict, which: str) -> None:
        for other in ("MDWV", "SWWV", "BTWV"):
            if other != which:
                c[other]["STATE"] = "OFF"
        c[which]["STATE"] = "ON"

    def _state_token(self, c: dict, which: str, value: str) -> None:
        v = value.upper()
        if v == "ON":
            if c["BSWV"]["WVTP"] in ("NOISE", "DC") or (which == "SWWV" and c["BSWV"]["WVTP"] == "PULSE"):
                raise SimError(-1, "carrier not allowed", ESR_EXE)
            self._exclusive_on(c, which)
        elif v == "OFF":
            c[which]["STATE"] = "OFF"
        else:
            raise SimError(-1, "bad state", ESR_EXE)

    def _mdwv_set(self, args, g):
        c = self._c(g)
        m = c["MDWV"]
        t = self._tokens(args)
        i = 0
        while i < len(t):
            k = t[i].upper()
            if k == "STATE":
                self._state_token(c, "MDWV", t[i + 1])
                i += 2
            elif k in MOD_FIELDS:
                m["TYPE"] = k
                i += 1
                while i + 1 < len(t) and t[i].upper() in MOD_FIELDS[k]:
                    if m["STATE"] != "ON":
                        raise SimError(-1, "set STATE ON first", ESR_EXE)
                    f, raw = t[i].upper(), t[i + 1]
                    m[k][f] = raw.upper() if f in ("SRC", "MDSP") else parse_number(raw)
                    i += 2
            elif k == "CARR":
                i += 1
                while i + 1 < len(t) and t[i].upper() not in MOD_FIELDS and t[i].upper() not in ("STATE", "CARR"):
                    self._bswv_apply(c, t[i], t[i + 1])
                    i += 2
            else:
                raise SimError(-1, "bad mdwv", ESR_EXE)

    def _carr(self, c: dict) -> str:
        return "CARR," + ",".join(f"{k},{_fmt(k, v)}" for k, v in self._bswv_fields(c) if k not in ("PERI", "HLEV", "LLEV"))

    def _mdwv_get(self, args, g):
        c = self._c(g)
        m = c["MDWV"]
        if m["STATE"] != "ON":
            return f"C{g[0]}:MDWV STATE,OFF"
        t = m["TYPE"]
        fields = ",".join(f"{f},{_fmt(f, m[t][f])}" for f in MOD_FIELDS[t])
        return f"C{g[0]}:MDWV STATE,ON,{t},{fields},{self._carr(c)}"

    def _generic_set(self, which: str, allowed: dict, args, g):
        c = self._c(g)
        d = c[which]
        t = self._tokens(args)
        i = 0
        while i < len(t):
            k = t[i].upper()
            if k == "STATE":
                self._state_token(c, which, t[i + 1])
                i += 2
            elif k == "MTRIG":
                i += 1
            elif k == "CARR":
                i += 1
                while i + 1 < len(t) and t[i].upper() not in allowed and t[i].upper() != "STATE":
                    self._bswv_apply(c, t[i], t[i + 1])
                    i += 2
            elif k in allowed:
                if d["STATE"] != "ON":
                    raise SimError(-1, "set STATE ON first", ESR_EXE)
                kind = allowed[k]
                raw = t[i + 1]
                if isinstance(kind, tuple):
                    if raw.upper() not in kind:
                        raise SimError(-1, "bad value", ESR_EXE)
                    d[k] = raw.upper()
                elif kind == "cycles":
                    d[k] = "INF" if raw.upper() == "INF" else str(int(parse_number(raw)))
                else:
                    v = parse_number(raw)
                    if v < 0:
                        raise SimError(-1, "negative", ESR_EXE)
                    d[k] = min(v, self.MAX_FREQ) if k in ("START", "STOP") else v
                i += 2
            else:
                raise SimError(-1, f"bad {which}", ESR_EXE)

    _SW_ALLOWED = {"TIME": float, "STOP": float, "START": float, "TRSR": ("EXT", "INT", "MAN"),
                   "TRMD": ("ON", "OFF"), "SWMD": ("LINE", "LOG"), "DIR": ("UP", "DOWN"),
                   "EDGE": ("RISE", "FALL")}
    _BT_ALLOWED = {"PRD": float, "STPS": float, "GATE_NCYC": ("GATE", "NCYC"), "TRSR": ("EXT", "INT", "MAN"),
                   "DLAY": float, "PLRT": ("NEG", "POS"), "TRMD": ("RISE", "FALL", "OFF"),
                   "EDGE": ("RISE", "FALL"), "TIME": "cycles"}

    def _swwv_set(self, args, g):
        self._generic_set("SWWV", self._SW_ALLOWED, args, g)

    def _btwv_set(self, args, g):
        self._generic_set("BTWV", self._BT_ALLOWED, args, g)

    def _generic_get(self, which: str, order: tuple, g) -> str:
        c = self._c(g)
        d = c[which]
        if d["STATE"] != "ON":
            return f"C{g[0]}:{which} STATE,OFF"
        body = ",".join(f"{k},{_fmt(k, d[k])}" for k in order if k in d)
        return f"C{g[0]}:{which} STATE,ON,{body},{self._carr(c)}"

    def _swwv_get(self, args, g):
        return self._generic_get("SWWV", ("TIME", "STOP", "START", "TRSR", "TRMD", "SWMD", "DIR"), g)

    def _btwv_get(self, args, g):
        return self._generic_get("BTWV", ("PRD", "STPS", "TRSR", "TRMD", "TIME", "DLAY", "GATE_NCYC"), g)

    def _pacp(self, args, g):
        dst, src = (int(x.strip().upper().lstrip("C")) for x in args.split(","))
        self.ch[dst] = deepcopy(self.ch[src])

    def _cmr_get(self, args, g):
        v, self.cmr = self.cmr, 0
        return f"CMR {v}"
