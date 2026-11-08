"""Драйвер векторных анализаторов цепей Keysight PNA / PNA-L (N5231A, N5230A/C, N5222A ...).

По справке PNA "Programming Commands" (SCPI):
* измерение - именованный параметр канала: ``CALC1:PAR:CAT:EXT?`` (список «имя,S-параметр»),
  ``CALC1:PAR:SEL 'имя'``, ``CALC1:PAR:MOD:EXT 'S21'`` - сменить S-параметр выбранного измерения,
  ``CALC1:PAR:DEF:EXT 'имя','S11'`` + ``DISP:WIND1:TRAC1:FEED 'имя'`` - создать;
* формат ``CALC1:FORM MLOG|PHAS|UPH|GDEL|SWR|MLIN|REAL|IMAG``, данные ``CALC1:DATA? FDATA``
  (в формате измерения) и ``CALC1:DATA? SDATA`` (комплексные, пары re,im) при ``FORM:DATA ASCII,0``;
* стимул ``SENS1:FREQ:STAR|STOP|CENT|SPAN``, ``SENS1:SWE:POIN``, ``SENS1:SWE:TIME`` (+ ``:AUTO``),
  ``SENS1:BWID`` (полоса ПЧ), ``SOUR1:POW1`` (dBm), ``OUTP`` (мощность источника);
* усреднение ``SENS1:AVER``, ``SENS1:AVER:COUN``; коррекция ``SENS1:CORR:STAT``;
* маркер ``CALC1:MARK1``, ``CALC1:MARK1:X``, ``CALC1:MARK1:Y?`` (два числа), ``CALC1:MARK1:FUNC:EXEC MAX|MIN``;
* развёртка ``SENS1:SWE:MODE SING`` + ``*OPC`` - бит OPC по окончании, ``SENS1:SWE:MODE HOLD`` - стоп.

Подключение не создаёт и не удаляет измерения пользователя. Читаются ВСЕ измерения канала 1
(до ``MEAS_SLOTS``, в порядке каталога): после каждой завершённой развёртки для каждого
публикуются ``m{n}.trace`` (в формате измерения) и ``m{n}.sdata`` (комплексная), плюс имя,
S-параметр и формат. Если измерений нет - создаётся ``VL_MEAS``. Легаси-ключи ``trace`` /
``sparam`` / ``format`` и маркер относятся к первому измерению (m1) и его не меняют.
Как и анализатор спектра, ВАЦ ведётся однократными развёртками: наружу уходят только
целые трассы. Ось X считается линейной (SENS:SWE:TYPE LIN).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.instrument import VISAInstrument
from ..core.params import Action, Kind, Param
from ..core.scpi import parse_number
from ..core.trace import Trace
from ..profiles import NETWORK_ANALYZER

__all__ = ["KeysightPNA", "PNA_MODELS", "PNAModel"]

F, I, B, E = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM


@dataclass(frozen=True)
class PNAModel:
    name: str
    f_min: float
    f_max: float
    max_points: int = 32001


PNA_MODELS = {
    "N5230C-420": PNAModel("N5230C-420", 300e3, 20e9),
    "N5231A": PNAModel("N5231A", 300e3, 13.5e9),
    "N5232A": PNAModel("N5232A", 300e3, 20e9),
    "N5222A": PNAModel("N5222A", 10e6, 26.5e9),
}

Y_UNITS = {"MLOG": "dB", "PHASE": "deg", "UPHASE": "deg", "GDELAY": "s"}
FREQ_KEYS = ("freq.start", "freq.stop", "freq.center", "freq.span")
MEAS_NAME = "VL_MEAS"
MEAS_SLOTS = 8                   # сколько измерений канала 1 выставляется наружу (m1..m8)
FORMATS = ("MLOG", "PHASE", "UPHASE", "GDELAY", "SWR", "LIN_MAG", "REAL", "IMAG")
FORMAT_MAP = (("PHASE", "PHAS"), ("UPHASE", "UPH"), ("GDELAY", "GDEL"), ("LIN_MAG", "MLIN"))
_MEAS_KEY = re.compile(r"m(\d+)\.(\w+)")


class KeysightPNA(VISAInstrument):
    NAME = "Keysight PNA-L N5231A"
    PROFILE = NETWORK_ANALYZER
    POLL_INTERVAL_MS = 100
    TIMEOUT_MS = 10000            # трасса 32001 точка в ASCII

    def __init__(self, transport, model: PNAModel | None = None, parent=None,
                 command_delay_s: float | None = None):
        self.model = model or PNA_MODELS["N5231A"]
        self._continuous = True
        self._armed = False
        self._complex = True
        self._sweep: dict[str, Any] = {}       # последняя завершённая развёртка: ключи m{n}.*, trace, meas.count
        self.meas_names: list[str] = []
        self.meas_name = ""
        super().__init__(transport, parent, command_delay_s)

    @property
    def channels(self) -> int:
        """Число слотов измерений m{n} (раскрытие ``m{n}.*`` профиля)."""
        return MEAS_SLOTS

    # ------------------------------------------------------------ описание
    def build_params(self):
        m = self.model
        st, rs, sw, mk = "Анализ/Стимул", "Анализ/Измерение", "Анализ/Развёртка", "Анализ/Маркер"
        freq = dict(kind=F, unit="Hz", minimum=m.f_min, maximum=m.f_max, tolerance=1e-9,
                    also_read=FREQ_KEYS)
        ch = dict(channel=1)          # перед CALC-командой выбрать наше измерение
        per_meas = []
        for n in range(1, MEAS_SLOTS + 1):
            sec, mch = f"Измерения/m{n}", dict(channel=n)
            per_meas += [
                Param(f"m{n}.name", f"Имя m{n}", Kind.STR, section=sec, group="SWEEP"),
                Param(f"m{n}.sparam", f"S-параметр m{n}", E, section=sec, query="<catalog>",
                      write="CALC1:PAR:MOD:EXT '{value}'", choices=("S11", "S21", "S12", "S22"), **mch),
                Param(f"m{n}.format", f"Формат m{n}", E, section=sec, query="CALC1:FORM?",
                      write="CALC1:FORM {value}", choices=FORMATS, enum_map=FORMAT_MAP, **mch),
                Param(f"m{n}.trace", f"Трасса m{n}", Kind.TRACE, "", section=sec, group="SWEEP"),
                Param(f"m{n}.sdata", f"Комплексная трасса m{n}", Kind.TRACE, "", section=sec, group="SWEEP"),
            ]
        return per_meas + [
            Param("trace", "Трасса", Kind.TRACE, "", section="Трасса/Трасса", group="SWEEP"),
            Param("meas.count", "Измерений", I, section=rs, group="SWEEP"),
            Param("meas.complex", "Комплексные данные", B, section=rs, query="<virtual>", write="<virtual>"),
            Param("meas.armed", "Идёт развёртка", B, section=sw, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=sw, query="<virtual>", write="<virtual>"),
            Param("sparam", "S-параметр", E, section=rs, query="<catalog>",
                  write="CALC1:PAR:MOD:EXT '{value}'", choices=("S11", "S21", "S12", "S22"), **ch),
            Param("format", "Формат", E, section=rs, query="CALC1:FORM?", write="CALC1:FORM {value}",
                  choices=FORMATS,
                  labels=("ЛогАмп, dB", "Фаза", "Фаза развёрн.", "ГВЗ", "КСВН", "ЛинАмп", "Re", "Im"),
                  enum_map=FORMAT_MAP,
                  also_read=("marker.value",), **ch),
            Param("cal.state", "Коррекция", B, section=rs, query="SENS1:CORR:STAT?",
                  write="SENS1:CORR:STAT {value}"),
            Param("freq.start", "Старт", section=st, query="SENS1:FREQ:STAR?", write="SENS1:FREQ:STAR {value}",
                  **freq),
            Param("freq.stop", "Стоп", section=st, query="SENS1:FREQ:STOP?", write="SENS1:FREQ:STOP {value}",
                  **freq),
            Param("freq.center", "Центр", section=st, query="SENS1:FREQ:CENT?", write="SENS1:FREQ:CENT {value}",
                  **freq),
            Param("freq.span", "Полоса", section=st, query="SENS1:FREQ:SPAN?", write="SENS1:FREQ:SPAN {value}",
                  **{**freq, "minimum": 0.0}),
            Param("source.power", "Мощность", F, "dBm", section=st, query="SOUR1:POW1?",
                  write="SOUR1:POW1 {value}", minimum=-90.0, maximum=20.0, tolerance=1e-3),
            Param("rf.output", "Источник вкл.", B, section=st, query="OUTP?", write="OUTP {value}"),
            Param("bw.if", "Полоса ПЧ", F, "Hz", section=sw, query="SENS1:BWID?", write="SENS1:BWID {value}",
                  minimum=1.0, maximum=15e6, tolerance=1e-3, also_read=("sweep.time",)),
            Param("sweep.points", "Точек", I, section=sw, query="SENS1:SWE:POIN?", write="SENS1:SWE:POIN {value}",
                  minimum=1, maximum=m.max_points, also_read=("sweep.time",)),
            Param("sweep.time", "Время развёртки", F, "s", section=sw, query="SENS1:SWE:TIME?",
                  write="SENS1:SWE:TIME {value}", minimum=0.0, maximum=86400.0, tolerance=1e-3,
                  also_read=("sweep.time_auto",)),
            Param("sweep.time_auto", "Время авто", B, section=sw, query="SENS1:SWE:TIME:AUTO?",
                  write="SENS1:SWE:TIME:AUTO {value}", also_read=("sweep.time",)),
            Param("avg.state", "Усреднение", B, section=sw, query="SENS1:AVER?", write="SENS1:AVER {value}"),
            Param("avg.count", "Усреднений", I, section=sw, query="SENS1:AVER:COUN?",
                  write="SENS1:AVER:COUN {value}", minimum=1, maximum=65536),
            Param("marker.state", "Маркер", B, section=mk, query="CALC1:MARK1?", write="CALC1:MARK1 {value}",
                  also_read=("marker.freq", "marker.value"), **ch),
            Param("marker.freq", "Частота маркера", F, "Hz", section=mk, query="CALC1:MARK1:X?",
                  write="CALC1:MARK1:X {value}", minimum=0.0, maximum=m.f_max, tolerance=1e-6,
                  also_read=("marker.value", "marker.state"), **ch),
            Param("marker.value", "Значение маркера", F, "", section=mk, query="<marker>", **ch),
        ]

    def build_actions(self):
        return [Action("single", "Одна развёртка"), Action("abort", "Стоп"),
                Action("marker_peak", "Маркер на максимум"), Action("marker_min", "Маркер на минимум"),
                Action("reset", "Preset *RST",
                       confirm="Preset удалит измерения и выключит калибровку. Продолжить?")]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self._armed = False
        self._sweep = {}
        self.write("FORM:DATA ASCII,0")
        cat = self._catalog()
        if not cat:
            self.write(f"CALC1:PAR:DEF:EXT '{MEAS_NAME}','S11'")
            self.write("DISP:WIND1:STAT ON")
            self.write(f"DISP:WIND1:TRAC1:FEED '{MEAS_NAME}'")
            cat = self._catalog() or {MEAS_NAME: "S11"}
        self._remember(cat)

    def on_disconnecting(self) -> None:
        if self._armed:
            self.write("SENS1:SWE:MODE HOLD")
            self._armed = False

    def _catalog(self) -> dict[str, str]:
        """{имя измерения: S-параметр} канала 1."""
        raw = self.query("CALC1:PAR:CAT:EXT?").strip().strip('"').strip()
        if not raw or raw.upper().startswith("NO CATALOG"):
            return {}
        items = [t.strip() for t in raw.split(",")]
        return dict(zip(items[0::2], (s.upper() for s in items[1::2])))

    def _remember(self, cat: dict[str, str]) -> None:
        self.meas_names = list(cat)[:MEAS_SLOTS]
        if self.meas_names:
            self.meas_name = self.meas_names[0]

    def select_channel(self, channel: int) -> None:
        """Канал = номер измерения (1 - первое, оно же легаси-ключи); нет такого - ошибка."""
        if channel == 1 or not self.meas_names:
            name = self.meas_name
        elif channel <= len(self.meas_names):
            name = self.meas_names[channel - 1]
        else:
            raise ValueError(f"нет измерения m{channel} (на канале 1 их {len(self.meas_names)})")
        self.write(f"CALC1:PAR:SEL '{name}'")

    def write_param(self, spec: Param, value: Any) -> None:
        if spec.key == "meas.continuous":
            self._continuous = bool(value)
            if self._continuous and not self._armed:
                self._start_sweep()
            return
        if spec.key == "meas.complex":
            self._complex = bool(value)
            return
        was_armed = self._armed
        self._abort()
        try:
            super().write_param(spec, value)
        finally:                                  # отказ записи не должен останавливать развёртки
            if was_armed or self._continuous:
                self._start_sweep()

    def read_param_raw(self, spec: Param) -> Any:
        key = spec.key
        if key == "meas.continuous":
            return self._continuous
        if key == "meas.armed":
            return self._armed
        if key == "meas.complex":
            return self._complex
        if key == "sparam":
            return self._catalog().get(self.meas_name)
        m = _MEAS_KEY.fullmatch(key)
        if m and m.group(2) in ("sparam", "format"):
            n = int(m.group(1))
            if n > len(self.meas_names):
                return None                       # такого измерения нет
            if m.group(2) == "sparam":
                return self._catalog().get(self.meas_names[n - 1])
        if key in ("marker.value", "marker.freq") and not self._values.get("marker.state"):
            return None
        if key == "marker.value":
            self.select_channel(1)
            return parse_number(self.query("CALC1:MARK1:Y?").split(",")[0])
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        return dict(self._sweep)

    def _sync_all(self) -> None:
        super()._sync_all()
        if self._continuous and not self._armed:
            self._start_sweep()

    # ------------------------------------------------------- развёртки
    def _set_armed(self, armed: bool) -> None:
        if armed != self._armed or "meas.armed" not in self._values:
            self._armed = armed
            self._publish("meas.armed", armed)

    def _start_sweep(self) -> None:
        self.arm_opc("SENS1:SWE:MODE SING")
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            self.write("SENS1:SWE:MODE HOLD")
            self._set_armed(False)

    def _read_trace(self) -> None:
        """Все измерения канала 1 после завершённой развёртки: форматированные и комплексные данные."""
        start, stop = self._values.get("freq.start"), self._values.get("freq.stop")
        if start is None or stop is None:
            start, stop = self.query_float("SENS1:FREQ:STAR?"), self.query_float("SENS1:FREQ:STOP?")
        cat = self._catalog()
        if cat:
            self._remember(cat)
        stim = {k: self._values.get(k) for k in ("bw.if", "source.power", "avg.state", "avg.count", "cal.state")}
        out: dict[str, Any] = {"meas.count": len(self.meas_names)}
        for n in range(1, MEAS_SLOTS + 1):
            if n > len(self.meas_names):                       # измерения нет (или удалено на приборе)
                out.update({f"m{n}.name": None, f"m{n}.trace": None, f"m{n}.sdata": None})
                continue
            name = self.meas_names[n - 1]
            sparam = cat.get(name)
            self.write(f"CALC1:PAR:SEL '{name}'")
            fmt = self._params[f"m{n}.format"].from_scpi(self.query("CALC1:FORM?"))
            y = [parse_number(t) for t in self.query("CALC1:DATA? FDATA").split(",") if t.strip()]
            meta = {**stim, "name": name, "sparam": sparam, "format": fmt}
            out[f"m{n}.name"] = name
            out[f"m{n}.trace"] = Trace.linear(start, stop, y, x_unit="Hz", y_unit=Y_UNITS.get(fmt, ""),
                                              name=sparam or name, meta=meta)
            out[f"m{n}.sdata"] = None
            if self._complex:
                raw = [parse_number(t) for t in self.query("CALC1:DATA? SDATA").split(",") if t.strip()]
                out[f"m{n}.sdata"] = Trace.linear(start, stop, raw[0::2], im=raw[1::2], x_unit="Hz", y_unit="",
                                                  name=sparam or name, meta={**meta, "format": "SDATA"})
            for k, v in ((f"m{n}.sparam", sparam), (f"m{n}.format", fmt)):
                if v is not None and self._values.get(k) != v:
                    self._publish(k, v)
        out["trace"] = out["m1.trace"] if self.meas_names else None
        self._sweep = out
        for k, v in out.items():
            self._publish(k, v)
        if self.meas_names:                                    # легаси-ключи = первое измерение
            for legacy, k in (("sparam", "m1.sparam"), ("format", "m1.format")):
                if self._values.get(k) is not None and self._values.get(legacy) != self._values[k]:
                    self._publish(legacy, self._values[k])
        if self._values.get("marker.state"):
            self._read_keys(["marker.freq", "marker.value"])

    def poll(self) -> None:
        if self._armed and self.opc_done("развёртка"):
            self._set_armed(False)
            self._read_trace()
            if self._continuous:
                self._start_sweep()

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "single":
            self._abort()
            self._continuous = False
            self._publish("meas.continuous", False)
            self._start_sweep()
            return None
        if name == "abort":
            self._abort()
            self.write("SENS1:SWE:MODE HOLD")
            return None
        if name in ("marker_peak", "marker_min"):
            self.select_channel(1)
            if not self._values.get("marker.state"):
                self.write("CALC1:MARK1 ON")
                self._values["marker.state"] = True
            self.write(f"CALC1:MARK1:FUNC:EXEC {'MAX' if name == 'marker_peak' else 'MIN'}")
            self._read_keys(["marker.state", "marker.freq", "marker.value"])
            return None
        if name == "reset":
            self._abort()
            self.write("*RST")
            self.query("*OPC?")
            self.on_connected()
            self._sync_all()
            return None
        return super().run_custom_action(name, args)
