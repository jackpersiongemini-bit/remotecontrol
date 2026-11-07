"""Симулятор ITECH IT6322 (3 канала) по IT6300 Programming Guide.

Физика: на каждом выходе резистивная нагрузка ``load_ohm`` (меняется из тестов).
U = min(Uуст, Iуст*R) -> режим CV или CC. OVP: при U > порога и включённой защите
выход отключается и взводится бит OV (9) в STAT:QUES:INST:ISUMn:COND.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import ESR_CME, ESR_EXE, SimError, SimInstrument


@dataclass
class _Ch:
    umax: float
    imax: float
    volt: float = 0.0
    curr: float = 0.0
    output: bool = False
    ovp_state: bool = False
    ovp_level: float = 0.0
    vlimit: float = 0.0
    ovp_tripped: bool = False
    load_ohm: float = 10.0
    extra: dict = field(default_factory=dict)


class ItechIT6300Sim(SimInstrument):
    IDN = "ITECH Ltd., IT6322A, 800000000000000001, V1.07-V1.04"
    RATINGS = ((30.0, 3.0), (30.0, 3.0), (5.0, 3.0))

    def reset(self) -> None:
        # *RST по мануалу: OUTP OFF, CURR MAX, VOLT:LIMIT MAX, INST:COM:OFF (VOLT MIN)
        old_loads = [c.load_ohm for c in getattr(self, "chs", [])] or [10.0] * len(self.RATINGS)
        self.chs = [_Ch(u, i, volt=0.0, curr=i, ovp_level=u * 1.1, vlimit=u, load_ohm=r)
                    for (u, i), r in zip(self.RATINGS, old_loads)]
        self.sel = 0
        self.combine = {"SER": False, "PAR": False, "TRAC": False}
        self.remote = False

    @property
    def c(self) -> _Ch:
        return self.chs[self.sel]

    # ----------------------------------------------------------- физика
    def solve(self, n: int) -> tuple[float, float, str]:
        """(U, I, режим) для канала n (0-based)."""
        ch = self.chs[n]
        if not ch.output:
            return 0.0, 0.0, "OFF"
        v_cv = ch.volt
        i_cv = v_cv / ch.load_ohm if ch.load_ohm > 0 else float("inf")
        if i_cv <= ch.curr:
            return v_cv, i_cv, "CV"
        return ch.curr * ch.load_ohm, ch.curr, "CC"

    def _check_ovp(self) -> None:
        for n, ch in enumerate(self.chs):
            v, _, _ = self.solve(n)
            if ch.ovp_state and ch.output and v > ch.ovp_level:
                ch.output = False
                ch.ovp_tripped = True

    def set_load(self, channel: int, ohm: float) -> None:
        """Для тестов: изменить нагрузку канала (1-based)."""
        self.chs[channel - 1].load_ohm = ohm
        self._check_ovp()

    # --------------------------------------------------------- команды
    def register(self) -> None:
        self.on("SYSTem:ERRor", get=self._err)
        self.on("SYSTem:VERSion", get=lambda a, g: "1991.1")
        self.on("SYSTem:REMote", lambda a, g: setattr(self, "remote", True))
        self.on("SYSTem:LOCal", lambda a, g: setattr(self, "remote", False))
        self.on("SYSTem:RWLock[:STATe]", lambda a, g: setattr(self, "remote", True))
        self.on("SYSTem:BEEPer", lambda a, g: None)

        self.on("INSTrument:NSELect", self._nsel, lambda a, g: str(self.sel + 1))
        self.on("INSTrument[:SELect]", self._isel, lambda a, g: f"CH{self.sel + 1}")

        self.on("OUTPut[:STATe][:ALL]", self._outp_all, lambda a, g: "1" if any(c.output for c in self.chs) else "0")
        self.on("[SOURce:]CHANnel:OUTPut[:STATe]", self._chan_outp, lambda a, g: "1" if self.c.output else "0")
        for mode in ("SER", "PAR", "TRAC"):
            long = {"SER": "SERies", "PAR": "PARallel", "TRAC": "TRACk"}[mode]
            self.on(f"OUTPut:{long}[:STATe]", self._combine_setter(mode),
                    lambda a, g, m=mode: "1" if self.combine[m] else "0")

        self.on("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]", self._volt, self._volt_q)
        self.on("[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]", self._curr, self._curr_q)
        self.on("[SOURce:]VOLTage:PROTection:STATe",
                lambda a, g: setattr(self.c, "ovp_state", self.boolean(a)) or self._check_ovp(),
                lambda a, g: "1" if self.c.ovp_state else "0")
        self.on("[SOURce:]VOLTage:PROTection[:LEVel]", self._ovp_level,
                lambda a, g: f"{self.c.ovp_level:.3f}")
        self.on("[SOURce:]VOLTage:PROTection:TRIPed", get=lambda a, g: "1" if self.c.ovp_tripped else "0")
        self.on("[SOURce:]VOLTage:PROTection:CLEar", lambda a, g: setattr(self.c, "ovp_tripped", False))
        self.on("[SOURce:]VOLTage:LIMIT[:LEVel]", self._vlimit, lambda a, g: f"{self.c.vlimit:.3f}")

        self.on("MEASure[:SCALar]:VOLTage[:DC]", get=lambda a, g: f"{self.solve(self.sel)[0]:.4f}")
        self.on("FETCh[:VOLTage][:DC]", get=lambda a, g: f"{self.solve(self.sel)[0]:.4f}")
        self.on("MEASure[:SCALar]:CURRent[:DC]", get=lambda a, g: f"{self.solve(self.sel)[1]:.4f}")
        self.on("FETCh:CURRent[:DC]", get=lambda a, g: f"{self.solve(self.sel)[1]:.4f}")
        self.on("MEASure[:SCALar]:POWer[:DC]",
                get=lambda a, g: f"{self.solve(self.sel)[0] * self.solve(self.sel)[1]:.4f}")
        self.on("MEASure[:SCALar]:CURRent:ALL[:DC]",
                get=lambda a, g: ", ".join(f"{self.solve(n)[1]:.4f}" for n in range(len(self.chs))))
        self.on("MEASure[:SCALar][:VOLTage]:ALL[:DC]",
                get=lambda a, g: ", ".join(f"{self.solve(n)[0]:.4f}" for n in range(len(self.chs))))

        self.on("STATus:QUEStionable:INSTrument:ISUMmary#:CONDition", get=self._cond)
        self.on("STATus:PRESet", lambda a, g: None)
        self.on("*SAV", lambda a, g: None)
        self.on("*RCL", lambda a, g: None)
        self.on("*TRG", lambda a, g: None)

    def _err(self, a, g):
        if not self.errors:
            return '0,"No error"'
        code, msg = self.errors.popleft()
        return f'{code},"{msg}"'

    def _nsel(self, a, g):
        n = int(self.num(a, 1, len(self.chs)))
        self.sel = n - 1

    def _isel(self, a, g):
        t = a.strip().upper()
        if not t.startswith("CH") or not t[2:].isdigit() or not 1 <= int(t[2:]) <= len(self.chs):
            raise SimError(-224, "Illegal parameter value", ESR_EXE)
        self.sel = int(t[2:]) - 1

    def _outp_all(self, a, g):
        on = self.boolean(a)
        for ch in self.chs:
            ch.output = on and not ch.ovp_tripped
        self._check_ovp()

    def _chan_outp(self, a, g):
        on = self.boolean(a)
        if on and self.c.ovp_tripped:
            raise SimError(-221, "Settings conflict (OVP tripped)", ESR_EXE)
        self.c.output = on
        self._check_ovp()

    def _combine_setter(self, mode):
        def setter(a, g):
            on = self.boolean(a)
            if on and any(v for k, v in self.combine.items() if k != mode):
                raise SimError(-221, "Settings conflict", ESR_EXE)
            self.combine[mode] = on
        return setter

    def _volt(self, a, g):
        v = self.num(a, 0.0, self.c.umax, 0.0)
        if v > self.c.vlimit:
            raise SimError(-222, "Data out of range", ESR_EXE)
        self.c.volt = v
        self._check_ovp()

    def _volt_q(self, a, g):
        if a.strip().upper().startswith("MAX"):
            return f"{self.c.umax:.3f}"
        if a.strip().upper().startswith("MIN"):
            return "0.000"
        return f"{self.c.volt:.3f}"

    def _curr(self, a, g):
        self.c.curr = self.num(a, 0.0, self.c.imax, self.c.imax)
        self._check_ovp()

    def _curr_q(self, a, g):
        if a.strip().upper().startswith("MAX"):
            return f"{self.c.imax:.3f}"
        if a.strip().upper().startswith("MIN"):
            return "0.000"
        return f"{self.c.curr:.3f}"

    def _ovp_level(self, a, g):
        self.c.ovp_level = self.num(a, 0.0, self.c.umax * 1.1, self.c.umax * 1.1)
        self._check_ovp()

    def _vlimit(self, a, g):
        self.c.vlimit = self.num(a, 0.0, self.c.umax, self.c.umax)
        self.c.volt = min(self.c.volt, self.c.vlimit)

    def _cond(self, a, g):
        n = int(g[0]) - 1
        if not 0 <= n < len(self.chs):
            raise SimError(-114, "Header suffix out of range", ESR_CME)
        _, _, mode = self.solve(n)
        cond = {"CV": 0x1, "CC": 0x2}.get(mode, 0)
        if self.chs[n].ovp_tripped:
            cond |= 0x200
        return str(cond)
