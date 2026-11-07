"""Симулятор датчика мощности Keysight U2021XA с моделью входного сигнала.

Сигнал: импульсный (``on_dbm`` в течение ``width`` каждые ``period``, иначе ``off_dbm``)
или непрерывный (``width = None``). Меняется из тестов: ``sim.set_signal(...)``.

Модель запуска (как в мануале U2020 X-Series):
* IMM - измерение сразу; INIT:CONT ON - свободный счёт;
* INT - по уровню: нужен перепад через ``TRIG:LEV`` (импульс с on >= уровня > off).
  Непрерывный сигнал перепада не даёт - запуска нет, измерение ждёт;
* EXT - ``sim.fire_external()``; BUS - ``*TRG``; HOLD - только ``TRIG:IMM``.
* ``INIT`` перекрываемая: ``*OPC`` взводит бит OPC только по окончании измерения.
Конфликты -221: INT при DET:FUNC AVER, TRIG:SLOP не при INT/EXT, FEED с ``ON SWEEP`` не при INT/EXT,
FEED PEAK/PTAV при AVER. FETC? без данных -> -230.
"""
from __future__ import annotations

import math

from .base import ESR_EXE, ESR_OPC, SimError, SimInstrument

__all__ = ["KeysightU2021XASim"]


def _lin(dbm: float) -> float:
    return 10 ** (dbm / 10)


def _db(mw: float) -> float:
    return 10 * math.log10(max(mw, 1e-30))


class KeysightU2021XASim(SimInstrument):
    IDN = "Keysight Technologies,U2021XA,MY00000001,A1.03.05"

    def reset(self) -> None:
        self.cfg = {"freq": 50e6, "det": "NORM", "vid": "OFF", "aver": True, "coun": 4, "coun_auto": True,
                    "gain2": 0.0, "gain2_stat": False, "src": "IMM", "lev": 0.0, "lev_auto": True,
                    "slop": "POS", "del": 0.0, "hold": 1e-6, "offs": 0.0, "swe": 100e-6, "cont": False}
        self.units = {n: "DBM" for n in range(1, 5)}
        self.feeds = {n: "POW:AVER" for n in range(1, 5)}
        self.measuring = False
        self.opc_pending = False
        self.results: dict[int, float] | None = None
        self.zeroed = self.calibrated = False
        if not hasattr(self, "signal"):
            self.signal = {"on_dbm": -10.0, "off_dbm": -120.0, "period": 1e-3, "width": 100e-6}

    # --------------------------------------------------------------- тесты
    def set_signal(self, on_dbm: float | None = None, width: float | None | str = "keep",
                   period: float | None = None, off_dbm: float | None = None) -> None:
        if on_dbm is not None:
            self.signal["on_dbm"] = on_dbm
        if width != "keep":
            self.signal["width"] = width
        if period is not None:
            self.signal["period"] = period
        if off_dbm is not None:
            self.signal["off_dbm"] = off_dbm
        self._try_trigger("INT")

    def fire_external(self) -> None:
        self._try_trigger("EXT")

    # ------------------------------------------------------------- физика
    def _power_at(self, t: float) -> float:
        s = self.signal
        if s["width"] is None:
            return _lin(s["on_dbm"])
        return _lin(s["on_dbm"] if (t % s["period"]) < s["width"] else s["off_dbm"])

    def _compute(self, triggered: bool) -> dict[str, float]:
        s, c = self.signal, self.cfg
        if c["det"] == "AVER" or not triggered:
            if s["width"] is None:
                avg = peak = _lin(s["on_dbm"])
            else:
                duty = min(1.0, s["width"] / s["period"])
                avg = _lin(s["on_dbm"]) * duty + _lin(s["off_dbm"]) * (1 - duty)
                peak = _lin(s["on_dbm"])
        else:
            t0 = c["del"] + c["offs"]
            n = 2000
            pts = [self._power_at(t0 + c["swe"] * i / n) for i in range(n + 1)] if c["swe"] > 0 \
                else [self._power_at(t0)]
            avg, peak = sum(pts) / len(pts), max(pts)
        off = c["gain2"] if c["gain2_stat"] else 0.0
        return {"AVER": _db(avg) + off, "PEAK": _db(peak) + off, "PTAV": _db(peak) - _db(avg),
                "MIN": _db(min(avg, peak)) + off}

    def _complete(self, triggered: bool) -> None:
        r = self._compute(triggered)
        self.results = {n: r[self.feeds[n].split(":")[1].split()[0][:4]] for n in self.feeds}
        self.measuring = False
        if self.opc_pending:
            self.opc_pending = False
            self.esr |= ESR_OPC

    def _edge_available(self) -> bool:
        s, lev = self.signal, self.cfg["lev"]
        return s["width"] is not None and s["on_dbm"] >= lev > s["off_dbm"]

    def _try_trigger(self, event: str) -> None:
        if not self.measuring:
            return
        src = self.cfg["src"]
        if src == "IMM" or (src == event == "INT" and self._edge_available()) or (src == event != "INT"):
            self._complete(triggered=src != "IMM")

    # ------------------------------------------------------------ команды
    def _register_common(self) -> None:
        super()._register_common()
        self._handlers = [h for h in self._handlers if h[0] not in ("*OPC", "*TRG")]

        def opc(a, g):
            if self.measuring:
                self.opc_pending = True
            else:
                self.esr |= ESR_OPC

        self.on("*OPC", opc, lambda a, g: "1")
        self.on("*TRG", lambda a, g: self._bus_trigger())

    def _bus_trigger(self) -> None:
        if self.measuring and self.cfg["src"] == "BUS":
            self._complete(triggered=True)

    def register(self) -> None:
        self.on("SYSTem:ERRor", get=self._err)
        num = self._num
        self.on("[SENSe[1]:]FREQuency[:CW]", num("freq", 1e3, 1000e9), self._get("freq"))
        self.on("[SENSe[1]:]DETector:FUNCtion", self._det_set, lambda a, g: self.cfg["det"])
        self.on("[SENSe[1]:]BANDwidth:VIDeo", self._enum("vid", {"OFF": "OFF", "LOW": "LOW", "MED": "MED",
                                                                 "MEDIUM": "MED", "HIGH": "HIGH"}),
                lambda a, g: self.cfg["vid"])
        self.on("[SENSe[1]:]AVERage:COUNt:AUTO", self._bool("coun_auto"), self._get("coun_auto"))
        self.on("[SENSe[1]:]AVERage:COUNt", self._count_set, lambda a, g: str(self.cfg["coun"]))
        self.on("[SENSe[1]:]AVERage[:STATe]", self._bool("aver"), self._get("aver"))
        self.on("[SENSe[1]:]CORRection:GAIN2:STATe", self._bool("gain2_stat"), self._get("gain2_stat"))
        self.on("[SENSe[1]:]CORRection:GAIN2[:INPut][:MAGNitude]", num("gain2", -100, 100), self._get("gain2"))
        self.on("[SENSe[1]:]SWEep[1]:OFFSet:TIME", num("offs", -1, 1), self._get("offs"))
        self.on("[SENSe[1]:]SWEep[1]:TIME", num("swe", 0, 1), self._get("swe"))
        self.on("TRIGger[:SEQuence[1]]:SOURce", self._src_set, lambda a, g: self.cfg["src"])
        self.on("TRIGger[:SEQuence]:LEVel:AUTO", self._bool("lev_auto"), self._get("lev_auto"))
        self.on("TRIGger[:SEQuence]:LEVel", self._lev_set, self._get("lev"))
        self.on("TRIGger[:SEQuence]:SLOPe", self._slop_set, lambda a, g: self.cfg["slop"])
        self.on("TRIGger[:SEQuence]:DELay", num("del", -1, 1), self._get("del"))
        self.on("TRIGger[:SEQuence]:HOLDoff", num("hold", 1e-6, 0.4), self._get("hold"))
        self.on("TRIGger[:SEQuence[1]]:IMMediate", lambda a, g: self.measuring and self._complete(True))
        self.on("INITiate[1]:CONTinuous", self._cont_set, self._get("cont"))
        self.on("INITiate[1][:IMMediate]", self._init)
        self.on("ABORt[1]", lambda a, g: setattr(self, "measuring", False))
        self.on("UNIT#:POWer", self._unit_set, lambda a, g: self.units[int(g[0])])
        self.on("CALCulate#:FEED[1]", self._feed_set, lambda a, g: f'"{self.feeds[int(g[0])]}"')
        self.on("FETCh#[:SCALar][:POWer:AC]", get=self._fetch)
        self.on("CALibration[1]:ZERO:AUTO", lambda a, g: setattr(self, "zeroed", True))
        self.on("CALibration[1]:AUTO", lambda a, g: setattr(self, "calibrated", True))

    def _err(self, a, g):
        code, msg = self.errors.popleft() if self.errors else (0, "No error")
        return f'{code:+d},"{msg}"'

    # помощники
    def _get(self, k):
        def fn(a, g):
            v = self.cfg[k]
            if isinstance(v, bool):
                return "1" if v else "0"
            return f"{v:+.6E}" if isinstance(v, float) else str(v)
        return fn

    def _num(self, k, lo, hi):
        def fn(a, g):
            self.cfg[k] = self.num(a, lo, hi)
        return fn

    def _bool(self, k):
        def fn(a, g):
            self.cfg[k] = self.boolean(a)
        return fn

    def _enum(self, k, table):
        def fn(a, g):
            t = a.strip().upper()
            if t not in table:
                raise SimError(-224, "Illegal parameter value", ESR_EXE)
            self.cfg[k] = table[t]
        return fn

    def _det_set(self, a, g):
        t = a.strip().upper()
        mode = {"NORM": "NORM", "NORMAL": "NORM", "AVER": "AVER", "AVERAGE": "AVER"}.get(t)
        if mode is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if mode == "AVER" and self.cfg["src"] == "INT":
            raise SimError(-221, "Settings conflict", ESR_EXE)
        if mode == "AVER" and self.cfg["del"] < 0:
            self.cfg["del"] = 0.0
        self.cfg["det"] = mode

    def _src_set(self, a, g):
        t = a.strip().upper()
        src = {"IMM": "IMM", "IMMEDIATE": "IMM", "INT": "INT", "INT1": "INT", "INTERNAL": "INT",
               "EXT": "EXT", "EXTERNAL": "EXT", "BUS": "BUS", "HOLD": "HOLD"}.get(t)
        if src is None:
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        if src == "INT" and self.cfg["det"] == "AVER":
            raise SimError(-221, "Settings conflict", ESR_EXE)
        self.cfg["src"] = src

    def _slop_set(self, a, g):
        if self.cfg["src"] not in ("INT", "EXT"):
            raise SimError(-221, "Settings conflict", ESR_EXE)
        t = a.strip().upper()
        self.cfg["slop"] = {"POS": "POS", "POSITIVE": "POS", "NEG": "NEG", "NEGATIVE": "NEG"}[t]

    def _lev_set(self, a, g):
        self.cfg["lev"] = self.num(a, -40, 20)
        self.cfg["lev_auto"] = False

    def _count_set(self, a, g):
        self.cfg["coun"] = int(self.num(a, 1, 1024))
        self.cfg["coun_auto"] = False

    def _cont_set(self, a, g):
        self.cfg["cont"] = self.boolean(a)
        if self.cfg["cont"]:
            self.measuring = False

    def _init(self, a, g):
        if self.cfg["cont"]:
            raise SimError(-213, "Init ignored", ESR_EXE)
        self.measuring = True
        self._try_trigger("INT")

    def _unit_set(self, a, g):
        self.units[int(g[0])] = {"DBM": "DBM", "W": "W"}[a.strip().upper()]

    def _feed_set(self, a, g):
        feed = a.strip().strip('"').upper()
        if "ON SWEEP" in feed and self.cfg["src"] not in ("INT", "EXT"):
            raise SimError(-221, "Settings conflict", ESR_EXE)        # как на приборе (проверено)
        if self.cfg["det"] == "AVER" and ("PEAK" in feed or "PTAV" in feed):
            raise SimError(-221, "Settings conflict", ESR_EXE)
        self.feeds[int(g[0])] = feed

    def _fetch(self, a, g):
        n = int(g[0])
        if self.cfg["cont"] and self.cfg["src"] == "IMM":
            r = self._compute(triggered=False)
            return f"{r[self.feeds[n].split(':')[1].split()[0][:4]]:+.6E}"
        if self.results is None or self.measuring:
            raise SimError(-230, "Data corrupt or stale", ESR_EXE)
        return f"{self.results[n]:+.6E}"
