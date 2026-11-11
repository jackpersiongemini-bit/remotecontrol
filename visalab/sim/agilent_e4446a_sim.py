"""Симулятор анализатора спектра Agilent PSA E4446A.

Спектр: шумовая дорожка + список синусоид ``sim.signals = [(частота, dBm), ...]``,
каждая размыта фильтром RBW (гауссова форма). Связи «авто» упрощены, но как у прибора:
RBW ~ полоса/100 (ряд 1-3), VBW = RBW, время развёртки из полосы и RBW.
Развёртка: ``INIT`` при ``INIT:CONT OFF`` заканчивается сразу (или по ``sim.finish_sweep()``,
если ``sim.hold_sweeps = True``), ``*OPC`` взводит бит OPC по её окончании.
"""
from __future__ import annotations

import copy
import math
import re

from .base import ESR_CME, ESR_EXE, ESR_OPC, SimError, SimInstrument

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
    #: INP:COUP есть только у моделей до 26.5 ГГц; у E4446A (44 ГГц) - нет: -113 и нет ответа
    DC_COUPLING = False

    def reset(self) -> None:
        self.c = {"center": 22e9, "span": 44e9, "rbw": 3e6, "rbw_auto": True, "vbw": 3e6, "vbw_auto": True,
                  "rlev": 0.0, "pdiv": 10.0, "att": 10.0, "att_auto": True, "swt": 0.1, "swt_auto": True,
                  "poin": 601, "det": "POS", "aver": False, "coun": 100, "rosc": "INT", "cont": True,
                  "form": "ASC", "roff": 0.0, "gain": False, "coup": "AC", "swtype": "AUTO", "tmode": "WRIT",
                  "trig": "IMM", "tlev": -25.0, "tslope": "POS", "tdel": 1e-6, "tdel_on": False}
        self.held: list[float] | None = None            # накопленная трасса MAXH / MINH
        self.held_for: tuple | None = None
        self.waiting_trigger = False
        self.marker = {"on": False, "x": 22e9}
        self.sweeping = False
        self.opc_pending = False
        self.sweeps_done = 0
        self.last: list[float] | None = None
        if not hasattr(self, "signals"):
            self.signals: list[tuple[float, float]] = [(1e9, -20.0)]
            self.noise_dbm = -90.0
            self.hold_sweeps = False
            self.files: dict[str, tuple[dict, dict]] = {}      # состояния в «памяти прибора»
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
            out.append(10 * math.log10(mw) + self.c["roff"])
        return out

    def finish_sweep(self) -> None:
        if not self.sweeping:
            return
        self.last = self._apply_mode(self.spectrum())
        self.sweeping = False
        self.waiting_trigger = False
        self.sweeps_done += 1
        if self.opc_pending:
            self.opc_pending = False
            self.esr |= ESR_OPC

    def _apply_mode(self, data: list[float]) -> list[float]:
        """Режим трассы 1: WRIT - как есть, MAXH / MINH - копим по развёрткам (рестарт при смене
        частоты, полосы или точек), VIEW - трасса не обновляется, BLAN - тоже."""
        mode, c = self.c["tmode"], self.c
        if mode in ("VIEW", "BLAN") and self.last is not None:
            return self.last
        if mode not in ("MAXH", "MINH"):
            return data
        key = (c["center"], c["span"], c["poin"])
        if self.held is None or self.held_for != key:
            self.held, self.held_for = list(data), key
        else:
            pick = max if mode == "MAXH" else min
            self.held = [pick(a, b) for a, b in zip(self.held, data)]
        return list(self.held)

    def fire_trigger(self) -> None:
        """Тест: пришёл запуск (видео / внешний / сеть) - ждущая развёртка выполняется."""
        if self.sweeping and self.waiting_trigger:
            self.finish_sweep()

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
        self.on("DISPlay:WINDow[1]:TRACe:Y[:SCALe]:RLEVel:OFFSet", self._n("roff", -327, 327),
                self._g(lambda: self.c["roff"]))
        self.on("[SENSe:]POWer[:RF]:GAIN[:STATe]", self._b("gain"), self._gb("gain"))
        if self.DC_COUPLING:
            self.on("INPut:COUPling", self._enum("coup", {"AC": "AC", "DC": "DC"}), lambda a, g: self.c["coup"])
        self.on("[SENSe:]SWEep:TYPE", self._enum("swtype", {"AUTO": "AUTO", "SWE": "SWE", "SWEEP": "SWE",
                                                           "FFT": "FFT"}), lambda a, g: self.c["swtype"])
        self.on("TRACe[1]:MODE", self._tmode_set, lambda a, g: self.c["tmode"])
        self.on("TRIGger[:SEQuence]:SOURce", self._enum("trig", {"IMM": "IMM", "IMMEDIATE": "IMM", "VID": "VID",
                "VIDEO": "VID", "LINE": "LINE", "EXT": "EXT1", "EXT1": "EXT1", "EXTERNAL": "EXT1",
                "EXTERNAL1": "EXT1", "EXT2": "EXT2", "EXTERNAL2": "EXT2", "RFB": "RFB"}),
                lambda a, g: self.c["trig"])
        self.on("TRIGger[:SEQuence]:VIDeo:LEVel", self._n("tlev", -170, 30), self._g(lambda: self.c["tlev"]))
        self.on("TRIGger[:SEQuence]:SLOPe", self._enum("tslope", {"POS": "POS", "POSITIVE": "POS", "NEG": "NEG",
                "NEGATIVE": "NEG"}), lambda a, g: self.c["tslope"])
        self.on("TRIGger[:SEQuence]:DELay:STATe", self._b("tdel_on"), self._gb("tdel_on"))
        self.on("TRIGger[:SEQuence]:DELay", self._n("tdel", -0.15, 0.5), self._g(lambda: self.c["tdel"]))
        self.on("MMEMory:STORe:STATe", self._st_store)
        self.on("MMEMory:LOAD:STATe", self._st_load)
        self.on("MMEMory:CATalog", get=self._cat)
        self.on("MMEMory:DELete", self._file_del)

    def _enum(self, key, table):
        def fn(a, g):
            t = a.strip().upper()
            if t not in table:
                raise SimError(-224, "Illegal parameter value", ESR_EXE)
            self.c[key] = table[t]
        return fn

    def _tmode_set(self, a, g):
        t = a.strip().upper()
        modes = {"WRIT": "WRIT", "WRITE": "WRIT", "MAXH": "MAXH", "MAXHOLD": "MAXH", "MINH": "MINH",
                 "MINHOLD": "MINH", "VIEW": "VIEW", "BLAN": "BLAN", "BLANK": "BLAN"}
        if t not in modes:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.c["tmode"] = modes[t]
        self.held = None

    # состояния: файлы C:\ИМЯ.STA (DOS-имя до 8 знаков A-Z0-9, прибор переводит в верхний регистр)
    @staticmethod
    def _file_name(text: str, with_num: bool) -> str:
        m = re.fullmatch(r"\s*1\s*,\s*(['\"])(.+)\1\s*" if with_num else r"\s*(['\"])(.+)\1\s*", text)
        if not m:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        path = m.group(2).upper()
        name = path.split("\\")[-1].split(":")[-1]
        stem, dot, ext = name.partition(".")
        if not re.fullmatch(r"[A-Z0-9]{1,8}", stem) or (dot and not re.fullmatch(r"[A-Z0-9]{1,3}", ext)):
            raise SimError(-256, "File name not found", ESR_EXE)
        return name

    def _st_store(self, a, g):
        name = self._file_name(a, True)
        if not name.endswith(".STA"):                       # "The .sta extension is required"
            raise SimError(-256, "File name not found", ESR_EXE)
        self.files[name] = (copy.deepcopy(self.c), copy.deepcopy(self.marker))

    def _st_load(self, a, g):
        name = self._file_name(a, True)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        c, marker = copy.deepcopy(self.files[name])
        c["form"] = self.c["form"]                  # формат обмена в состояние не входит
        self.c, self.marker = c, marker
        self.sweeping = False
        self._couple()

    def _cat(self, a, g):
        if not a.strip():                           # :MMEMory:CATalog? <dir_name> - каталог обязателен
            raise SimError(-109, "Missing parameter", ESR_CME)
        entries = ",".join(f'"{n},,812"' for n in sorted(self.files))
        return f"{812 * len(self.files)},1000000,{entries}" if entries else "0,1000000"

    def _file_del(self, a, g):
        name = self._file_name(a, False)
        if name not in self.files:
            raise SimError(-256, "File name not found", ESR_EXE)
        del self.files[name]

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
        if self.c["trig"] != "IMM":
            self.waiting_trigger = True            # ждём запуск: sim.fire_trigger()
        elif not self.hold_sweeps:
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
