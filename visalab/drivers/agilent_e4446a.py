"""Драйвер анализаторов спектра Agilent PSA (E4440A/E4443A/E4445A/E4446A/E4447A/E4448A).

По "PSA Series User's and Programmer's Reference, Volume 1" (E4440-90607):
* частота ``FREQ:CENT|SPAN|STAR|STOP``; RBW ``BAND[:RES]`` (1 Гц...8 МГц) и ``:AUTO``,
  VBW ``BAND:VID`` (1 Гц...50 МГц) и ``:AUTO``;
* опорный уровень ``DISP:WIND:TRAC:Y:RLEV``, масштаб ``DISP:WIND:TRAC:Y:PDIV``;
  аттенюатор ``POW:ATT`` и ``:AUTO``;
* развёртка ``SWE:TIME`` и ``:AUTO``, точки ``SWE:POIN`` (101...8192, в нулевой полосе от 2);
* детектор ``DET NORM|AVER|POS|SAMP|NEG|QPE|RMS``; усреднение ``AVER``, ``AVER:COUN``;
* опора ``ROSC:SOUR INT|EXT``; вход ``INP:COUP AC|DC``, предусилитель ``POW:GAIN``, смещение
  опорного уровня ``DISP:WIND:TRAC:Y:RLEV:OFFS``; тип развёртки ``SWE:TYPE AUTO|SWE|FFT``;
* режим трассы 1 ``TRAC:MODE WRIT|MAXH|MINH|VIEW|BLAN``;
* запуск ``TRIG:SOUR IMM|VID|EXT1|LINE``, ``TRIG:VID:LEV`` (dBm), ``TRIG:SLOP POS|NEG``,
  ``TRIG:DEL`` (-150 мс...500 мс) и ``TRIG:DEL:STAT``;
* нулевая полоса - ``FREQ:SPAN 0``: ось трассы - время (0...SWE:TIME);
* трасса ``TRAC:DATA? TRACE1`` в формате ``FORM ASC`` (значения через запятую, dBm);
* маркер ``CALC:MARK1:STAT``, ``:X``, ``:Y?``, ``:MAX``;
* состояния в памяти прибора (диск ``C:``, справка 2.6 "File"): ``MMEM:STOR:STAT 1,'C:\\ИМЯ.STA'``
  (расширение .STA обязательно), ``MMEM:LOAD:STAT 1,'C:\\ИМЯ.STA'``, ``MMEM:DEL 'C:\\ИМЯ.STA'``,
  ``MMEM:CAT? 'C:\\'`` -> ``<занято>,<свободно>{,"<файл>,<тип>,<размер>"}``. Имя файла - DOS: до 8 знаков,
  только A-Z и 0-9 (прибор переводит в верхний регистр); драйвер приводит имя к этому виду и
  возвращает из ``state_save`` то имя, под которым состояние записано;
* ``INP:COUP`` есть не у всех моделей: "Not available on 40 GHz or 50 GHz analyzers" (E4446A/47A/48A) -
  на запрос прибор не отвечает, поэтому у этих моделей ключа ``input.coupling`` нет (``PSAModel.dc_coupling``);
* ``INIT:CONT OFF`` + ``INIT`` - одна развёртка, конец которой ловится через ``*OPC``.

Драйвер всегда ведёт прибор однократными развёртками (INIT:CONT OFF): после окончания
каждой читает целую трассу и, если ``meas.continuous``, запускает следующую. Так наружу
никогда не уходит недорисованная трасса. При подключении ставится ``FORM ASC``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, Kind, Param
from ..core.units import parse_number
from ..core.trace import Trace
from ..profiles import SPECTRUM_ANALYZER

__all__ = ["AgilentPSA", "PSA_MODELS", "PSAModel"]

F, I, B, E = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM


@dataclass(frozen=True)
class PSAModel:
    name: str
    f_max: float
    #: выбор связи входа AC/DC (``INP:COUP``); у 40 и 50 ГГц моделей его нет
    dc_coupling: bool = True


PSA_MODELS = {
    "E4440A": PSAModel("E4440A", 26.5e9), "E4443A": PSAModel("E4443A", 6.7e9),
    "E4445A": PSAModel("E4445A", 13.2e9), "E4446A": PSAModel("E4446A", 44.0e9, dc_coupling=False),
    "E4447A": PSAModel("E4447A", 42.98e9, dc_coupling=False), "E4448A": PSAModel("E4448A", 50.0e9, dc_coupling=False),
}

FREQ_KEYS = ("freq.center", "freq.span", "freq.start", "freq.stop")
#: что прибор пересчитывает сам при смене полосы/RBW (связанные «авто»)
COUPLED = ("bw.res", "bw.video", "sweep.time", "sweep.points")
SWEEP_TIMEOUT_MS = 60000
STATE_EXT = ".STA"
STATE_DIR = "C:\\"
#: имя файла PSA: до 8 знаков, только A-Z и 0-9
_STATE_NAME = re.compile(r"[A-Z0-9]{1,8}")


def state_file_name(name: str) -> str:
    """Имя состояния -> допустимое имя файла PSA (верхний регистр, только A-Z0-9, до 8 знаков)."""
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())[:8]


class AgilentPSA(VISAInstrument):
    NAME = "Agilent PSA E4446A"
    PROFILE = SPECTRUM_ANALYZER
    POLL_INTERVAL_MS = 100
    TIMEOUT_MS = 5000          # трасса 8192 точки в ASCII - ~100 КБ

    def __init__(self, transport, model: PSAModel | None = None, parent=None,
                 command_delay_s: float | None = None):
        self.model = model or PSA_MODELS["E4446A"]
        self._continuous = True
        self._armed = False
        self._trace: Trace | None = None
        super().__init__(transport, parent, command_delay_s)

    # ------------------------------------------------------------ описание
    def build_params(self):
        fmax = self.model.f_max
        fr, bw, amp, sw, mk = "Анализ/Частота", "Анализ/Полосы", "Анализ/Амплитуда", "Анализ/Развёртка", \
            "Анализ/Маркер"
        freq = dict(kind=F, unit="Hz", minimum=0.0, maximum=fmax, tolerance=1e-9)
        return [
            Param("trace", "Трасса", Kind.TRACE, "dBm", section="Спектр/Трасса", group="SWEEP"),
            Param("meas.armed", "Идёт развёртка", B, section=sw, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=sw, query="<virtual>", write="<virtual>"),
            Param("freq.center", "Центр", section=fr, query="FREQ:CENT?", write="FREQ:CENT {value}",
                  also_read=FREQ_KEYS + COUPLED, **freq),
            Param("freq.span", "Полоса обзора", section=fr, query="FREQ:SPAN?", write="FREQ:SPAN {value}",
                  also_read=FREQ_KEYS + COUPLED, **freq),
            Param("freq.start", "Старт", section=fr, query="FREQ:STAR?", write="FREQ:STAR {value}",
                  also_read=FREQ_KEYS + COUPLED, **freq),
            Param("freq.stop", "Стоп", section=fr, query="FREQ:STOP?", write="FREQ:STOP {value}",
                  also_read=FREQ_KEYS + COUPLED, **freq),
            Param("bw.res", "RBW", F, "Hz", section=bw, query="BAND?", write="BAND {value}",
                  minimum=1.0, maximum=8e6, tolerance=1e-3, also_read=("bw.res_auto", *COUPLED)),
            Param("bw.res_auto", "RBW авто", B, section=bw, query="BAND:AUTO?", write="BAND:AUTO {value}",
                  also_read=COUPLED),
            Param("bw.video", "VBW", F, "Hz", section=bw, query="BAND:VID?", write="BAND:VID {value}",
                  minimum=1.0, maximum=50e6, tolerance=1e-3, also_read=("bw.video_auto", "sweep.time")),
            Param("bw.video_auto", "VBW авто", B, section=bw, query="BAND:VID:AUTO?",
                  write="BAND:VID:AUTO {value}", also_read=("bw.video", "sweep.time")),
            Param("ref_level", "Опорный уровень", F, "dBm", section=amp, query="DISP:WIND:TRAC:Y:RLEV?",
                  write="DISP:WIND:TRAC:Y:RLEV {value}", minimum=-170.0, maximum=30.0, tolerance=1e-3,
                  also_read=("atten",)),
            Param("scale_div", "Масштаб", F, "dB", section=amp, query="DISP:WIND:TRAC:Y:PDIV?",
                  write="DISP:WIND:TRAC:Y:PDIV {value}", minimum=0.1, maximum=20.0, tolerance=1e-3),
            Param("atten", "Аттенюатор", F, "dB", section=amp, query="POW:ATT?", write="POW:ATT {value}",
                  minimum=0.0, maximum=70.0, tolerance=1e-3, also_read=("atten_auto",)),
            Param("atten_auto", "Аттенюатор авто", B, section=amp, query="POW:ATT:AUTO?",
                  write="POW:ATT:AUTO {value}", also_read=("atten",)),
            Param("detector", "Детектор", E, section=amp, query="DET?", write="DET {value}",
                  choices=("NORMAL", "PEAK", "NEG_PEAK", "SAMPLE", "AVERAGE", "RMS", "QPEAK"),
                  enum_map=(("NORMAL", "NORM"), ("PEAK", "POS"), ("NEG_PEAK", "NEG"), ("SAMPLE", "SAMP"),
                            ("AVERAGE", "AVER"), ("QPEAK", "QPE")),
                  also_read=("sweep.time",)),
            Param("sweep.time", "Время развёртки", F, "s", section=sw, query="SWE:TIME?",
                  write="SWE:TIME {value}", minimum=1e-6, maximum=4000.0, tolerance=1e-3,
                  also_read=("sweep.time_auto",)),
            Param("sweep.time_auto", "Время авто", B, section=sw, query="SWE:TIME:AUTO?",
                  write="SWE:TIME:AUTO {value}", also_read=("sweep.time",)),
            Param("sweep.points", "Точек", I, section=sw, query="SWE:POIN?", write="SWE:POIN {value}",
                  minimum=2, maximum=8192, also_read=("sweep.time",)),
            Param("avg.state", "Усреднение", B, section=sw, query="AVER?", write="AVER {value}"),
            Param("avg.count", "Усреднений", I, section=sw, query="AVER:COUN?", write="AVER:COUN {value}",
                  minimum=1, maximum=8192),
            Param("ref.source", "Опора 10 МГц", E, section="Анализ/Опора", query="ROSC:SOUR?",
                  write="ROSC:SOUR {value}", choices=("INT", "EXT")),
            Param("ref_offset", "Смещение опорного уровня", F, "dB", section=amp,
                  query="DISP:WIND:TRAC:Y:RLEV:OFFS?", write="DISP:WIND:TRAC:Y:RLEV:OFFS {value}",
                  minimum=-327.0, maximum=327.0, tolerance=1e-3),
            Param("preamp", "Предусилитель", B, section=amp, query="POW:GAIN?", write="POW:GAIN {value}",
                  also_read=("atten",)),
            Param("sweep.type", "Тип развёртки", E, section=sw, query="SWE:TYPE?", write="SWE:TYPE {value}",
                  choices=("AUTO", "SWEPT", "FFT"), enum_map=(("SWEPT", "SWE"),), also_read=("sweep.time",)),
            Param("trace.mode", "Режим трассы", E, section="Спектр/Трасса", query="TRAC:MODE?",
                  write="TRAC:MODE {value}", choices=("WRITE", "MAXHOLD", "MINHOLD", "VIEW", "BLANK"),
                  enum_map=(("WRITE", "WRIT"), ("MAXHOLD", "MAXH"), ("MINHOLD", "MINH"), ("BLANK", "BLAN"))),
            Param("trig.source", "Запуск", E, section="Анализ/Запуск", query="TRIG:SOUR?",
                  write="TRIG:SOUR {value}", choices=("FREE", "VIDEO", "EXT", "LINE"),
                  enum_map=(("FREE", "IMM"), ("VIDEO", "VID"), ("EXT", "EXT1"))),
            Param("trig.level", "Уровень видеозапуска", F, "dBm", section="Анализ/Запуск",
                  query="TRIG:VID:LEV?", write="TRIG:VID:LEV {value}", minimum=-170.0, maximum=30.0,
                  tolerance=1e-3),
            Param("trig.slope", "Фронт запуска", E, section="Анализ/Запуск", query="TRIG:SLOP?",
                  write="TRIG:SLOP {value}", choices=("POS", "NEG")),
            Param("trig.delay", "Задержка запуска", F, "s", section="Анализ/Запуск", query="TRIG:DEL?",
                  write="TRIG:DEL {value}", minimum=-0.15, maximum=0.5, tolerance=1e-9),
            Param("trig.delay_state", "Задержка запуска вкл", B, section="Анализ/Запуск",
                  query="TRIG:DEL:STAT?", write="TRIG:DEL:STAT {value}"),
            Param("marker.state", "Маркер", B, section=mk, query="CALC:MARK1:STAT?",
                  write="CALC:MARK1:STAT {value}", also_read=("marker.freq", "marker.level")),
            Param("marker.freq", "Частота маркера", F, "Hz", section=mk, query="CALC:MARK1:X?",
                  write="CALC:MARK1:X {value}", minimum=0.0, maximum=fmax, tolerance=1e-6,
                  also_read=("marker.level", "marker.state")),
            Param("marker.level", "Уровень маркера", F, "dBm", section=mk, query="<marker>"),
        ] + ([Param("input.coupling", "Связь входа", E, section=amp, query="INP:COUP?", write="INP:COUP {value}",
                    choices=("AC", "DC"))] if self.model.dc_coupling else [])

    def build_actions(self):
        return [Action("single", "Одна развёртка"), Action("abort", "Прервать"),
                Action("marker_peak", "Маркер на пик"),
                Action("reset", "Сброс *RST", confirm="Сбросить анализатор (Preset)?"),
                Action("state_list", "Состояния в памяти"), Action("state_save", "Сохранить состояние"),
                Action("state_recall", "Вызвать состояние"),
                Action("state_delete", "Удалить состояние", confirm="Удалить состояние из памяти анализатора?")]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self._armed = False
        self._trace = None
        self.write("FORM ASC")

    def on_disconnecting(self) -> None:
        if self._armed:
            self.write("ABOR")
            self._armed = False

    def write_param(self, spec: Param, value: Any) -> None:
        if spec.key == "meas.continuous":
            self._continuous = bool(value)
            if self._continuous and not self._armed:
                self._start_sweep()
            return
        # Изменение настройки перезапускает развёртку: текущая трасса уже не про новые условия.
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
        if key == "marker.level":
            if not self._values.get("marker.state"):
                return None
            return self.query_float("CALC:MARK1:Y?")
        if key == "marker.freq" and not self._values.get("marker.state"):
            return None
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        return {"trace": self._trace}      # трасса читается только после конца развёртки

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
        self.arm_opc("INIT:CONT OFF", "INIT")
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            self.write("ABOR")
            self._set_armed(False)

    def _read_trace(self) -> None:
        # Ось - по прибору, а не по кэшу: частоты могли поменять с передней панели (Local) или другой
        # программой. Изменились - перечитать и связанные ключи, окно покажет новые значения.
        old = (self._values.get("freq.start"), self._values.get("freq.stop"))
        self._read_keys(["freq.start", "freq.stop"])
        start, stop = self._values.get("freq.start"), self._values.get("freq.stop")
        if (start, stop) != old:
            self._read_keys(["freq.center", "freq.span", "sweep.time"])
        if start is None or stop is None:
            start, stop = self.query_float("FREQ:STAR?"), self.query_float("FREQ:STOP?")
        y = [parse_number(t) for t in self.query("TRAC:DATA? TRACE1").split(",") if t.strip()]
        span, swt = self._values.get("freq.span"), self._values.get("sweep.time")
        zero_span = span == 0 and swt is not None
        meta = {k: self._values.get(k) for k in ("bw.res", "bw.video", "detector", "ref_level", "atten",
                                                  "sweep.time", "avg.state", "avg.count")}
        if zero_span:                                  # нулевая полоса: ось - время развёртки
            self._trace = Trace.linear(0.0, swt, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)
        else:
            self._trace = Trace.linear(start, stop, y, x_unit="Hz", y_unit="dBm", name="trace", meta=meta)
        self._publish("trace", self._trace)
        if self._values.get("marker.state"):
            self._read_keys(["marker.freq", "marker.level"])

    def after_poll_timeout(self) -> None:
        """Чтение трассы прервал таймаут (обмен уже восстановлен): развёртки не должны остановиться."""
        if self._continuous and not self._armed:
            self._start_sweep()

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
            self.write("ABOR")
            return None
        if name == "marker_peak":
            if not self._values.get("marker.state"):
                self.write("CALC:MARK1:STAT ON")
                self._values["marker.state"] = True
            self.write("CALC:MARK1:MAX")
            self._read_keys(["marker.state", "marker.freq", "marker.level"])
            return None
        if name == "reset":
            self._abort()
            old = self.transport.timeout_ms
            self.transport.timeout_ms = SWEEP_TIMEOUT_MS
            try:
                self.write("*RST")
                self.query("*OPC?")
            finally:
                self.transport.timeout_ms = old
            self.on_connected()
            self._sync_all()
            return None
        if name == "state_list":
            return self._state_list()
        if name in ("state_save", "state_recall", "state_delete"):
            return self._state_file_action(name, args)
        return super().run_custom_action(name, args)

    # ------------------------------------------------- состояния в памяти прибора
    def _state_list(self) -> list[str]:
        reply = self.query(f"MMEM:CAT? '{STATE_DIR}'")
        entries = re.findall(r'"([^"]*)"', reply) or reply.split(",")   # "ИМЯ.STA,,812" или без кавычек
        names = []
        for entry in entries:
            file = entry.split(",")[0].strip().strip("'")
            if file.upper().endswith(STATE_EXT):
                names.append(file[:-len(STATE_EXT)].upper())
        return sorted(set(names))

    def _state_file_action(self, name: str, args: tuple) -> str | None:
        state = state_file_name(args[0]) if args else ""
        if not _STATE_NAME.fullmatch(state):
            raise ValueError(f"имя состояния: буквы A-Z и цифры, до 8 знаков (получено {args[0] if args else ''!r})")
        path = f"'{STATE_DIR}{state}{STATE_EXT}'"
        if name == "state_delete":
            self.write(f"MMEM:DEL {path}")
            return None
        was_armed = self._armed
        self._abort()
        if name == "state_save":
            if state in self._state_list():                      # запись поверх: старый файл сначала удалить
                self.write(f"MMEM:DEL {path}")
            self.write(f"MMEM:STOR:STAT 1,{path}")
            self._check_errors(context=name)
            if was_armed or self._continuous:
                self._start_sweep()
            return state
        self.write(f"MMEM:LOAD:STAT 1,{path}")                   # state_recall
        self._check_errors(context=name)
        self.write("FORM ASC")
        self._sync_all()                                        # значения прибора изменились; развёртка - как была
        if was_armed and not self._armed:
            self._start_sweep()
        return None
