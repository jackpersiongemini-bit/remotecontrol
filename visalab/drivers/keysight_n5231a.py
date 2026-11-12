"""Драйвер векторных анализаторов цепей Keysight PNA / PNA-L (N5231A, N5230A/C, N5222A ...).

По справке PNA "Programming Commands" (SCPI):
* измерение - именованный параметр канала: ``CALC1:PAR:CAT:EXT?`` (список «имя,S-параметр»),
  ``CALC1:PAR:SEL 'имя'``, ``CALC1:PAR:MOD:EXT 'S21'`` - сменить S-параметр выбранного измерения,
  ``CALC1:PAR:DEF:EXT 'имя','S11'`` + ``DISP:WIND1:TRAC1:FEED 'имя'`` - создать;
* формат ``CALC1:FORM MLOG|PHAS|UPH|GDEL|SWR|MLIN|REAL|IMAG``, данные ``CALC1:DATA? FDATA``
  (в формате измерения) и ``CALC1:DATA? SDATA`` (комплексные, пары re,im) при ``FORM:DATA ASCII,0``;
* тип развёртки ``SENS1:SWE:TYPE LIN|LOG|POW|CW|SEGM``; в CW частота ``SENS1:FREQ:CW``
  (ВАЦ как генератор: мощность ``SOUR1:POW1``, ``OUTP``), ось трассы - время 0..``SWE:TIME``;
* стимул ``SENS1:FREQ:STAR|STOP|CENT|SPAN``, ``SENS1:SWE:POIN``, ``SENS1:SWE:TIME`` (+ ``:AUTO``),
  ``SENS1:BWID`` (полоса ПЧ), ``SOUR1:POW1`` (dBm), ``OUTP`` (мощность источника);
* усреднение ``SENS1:AVER``, ``SENS1:AVER:COUN``; коррекция ``SENS1:CORR:STAT``;
* калибровка канала - cal set (набор поправок в памяти прибора): список ``CSET:CAT?`` (действие
  ``cal_list``), удаление ``CSET:DEL "имя"`` (``cal_delete``); ключ ``cal.set`` канала ``stim.channel`` -
  ``SENS<c>:CORR:CSET:ACT? NAME`` / ``SENS<c>:CORR:CSET:ACT "имя",<1|0>`` (1 - взять и стимул из cal set,
  ключ ``cal.stim``), пустое имя - ``SENS<c>:CORR:CSET:DEAC``; интерполяция ``SENS<c>:CORR:INT``;
* сама калибровка канала ``stim.channel`` - пошаговая (guided) ``SENS<c>:CORR:COLL:GUID:...``: разъёмы
  ``CONN:CAT?`` (``cal_conns``), наборы разъёма ``CKIT:CAT? "разъём"`` (``cal_kits``, наборы с «ideal» в
  имени - первыми), ``cal_start`` (разъём и набор порта 1, порта 2; пустой разъём - порт не калибруется):
  ``CONN:PORT<n>``, ``CKIT:PORT<n>``, ``INIT``, ``STEPS?``, ``DESC? <шаг>`` -> список шагов; ``cal_measure``
  (номер шага) - ``ACQ STAN<шаг>`` + ``*OPC`` без блокировки (ключ ``cal.busy``); ``cal_status`` -> (шаги,
  измеренные); ``cal_save`` (имя) - ``SAVE:CSET "имя"``, cal set сразу применяется к каналу; ``cal_abort``.
  Пока идёт калибровка, развёртки не запускаются. Калибровка только по перемычке (response THRU) трейса
  ``trace.sel`` (S21 / S12): ``cal_start_thru`` - ``CALC<c>:PAR:SEL``, ``SENS<c>:CORR:COLL:METH TRAN1``, шаг
  один (``SENS<c>:CORR:COLL:ACQ THRU``); ``cal_save`` - ``SENS<c>:CORR:COLL:SAVE``, затем
  ``SENS<c>:CORR:CSET:COPY "имя"`` и ``ACT "имя",0``;
* S-параметры канала для Touchstone: действие ``snp_read`` (порты, по умолчанию "1,2") - одна развёртка
  канала ``stim.channel`` (при внутреннем запуске), ``MMEM:STOR:TRAC:FORM:SNP RI``,
  ``CALC<c>:DATA:SNP:PORT? "1,2"`` (частоты, затем Re и Im каждого S-параметра в порядке S11 S21 S12 S22)
  -> {"S11": Trace(комплексная), ...};
* маркер ``CALC1:MARK1``, ``CALC1:MARK1:X``, ``CALC1:MARK1:Y?`` (два числа), ``CALC1:MARK1:FUNC:EXEC MAX|MIN``;
  маркеры трейса ``trace.sel``: номер ``mark.n`` (1..10), ``mark.state`` / ``mark.x`` / ``mark.y`` -
  ``CALC<c>:MARK<n>``, ``:X``, ``:Y?`` (первое число) его измерения; действия ``mark_peak`` / ``mark_min``
  (``:FUNC:EXEC MAX|MIN``), ``mark_aoff`` (``CALC<c>:MARK:AOFF``), ``mark_table`` -> [(n, x, y)] включённых;
  ``mark.y`` перечитывается после каждой развёртки;
* запуск ``TRIG:SOUR IMM|EXT|MAN`` (внутренний / внешний BNC / ручной), ``TRIG:SCOP ALL|CURR``
  (триггер на все каналы по очереди или на текущий), режим канала ``SENS1:SWE:TRIG:MODE CHAN|SWE|POIN|TRAC``
  (один триггер - канал / развёртка / точка / трасса), вход внешнего триггера ``TRIG:SLOP POS|NEG``,
  ``TRIG:TYPE EDGE|LEV``, ``TRIG:DEL`` (s); ручной запуск ``INIT<c>:IMM``. Однократная развёртка (SING)
  при EXT / MAN ждёт триггера: ``*OPC`` приходит после него, таймаута нет;
* развёртка ``SENS1:SWE:MODE SING`` + ``*OPC`` - бит OPC по окончании, ``SENS1:SWE:MODE HOLD`` - стоп;
* память трассы ``CALC1:MATH:MEM`` (данные -> память выбранного измерения), математика
  ``CALC1:MATH:FUNC NORM|DIV|SUBT|ADD|MULT`` (над комплексными данными, до формата; SDATA - без неё);
* масштаб и обработка трейса ``trace.sel`` («канал:имя»): окно и номер трейса - ``CALC<c>:PAR:WNUM?`` /
  ``TNUM?`` выбранного измерения, ``DISP:WIND<w>:TRAC<t>:Y:PDIV|RLEV|RPOS``, ``...:Y:AUTO`` (действие
  ``autoscale``); электрическая задержка ``CALC<c>:CORR:EDEL:TIME``, сглаживание ``CALC<c>:SMO[:STAT]``,
  ``CALC<c>:SMO:APER`` (% обзора). Задержка и сглаживание меняют и читаемые данные, масштаб - нет;
* состояния в памяти прибора: ``MMEM:STOR "имя.csa"`` (состояние + калибровка), ``MMEM:LOAD``,
  ``MMEM:DEL``, ``MMEM:CAT?`` (список файлов текущей папки; читаются только имена); папка -
  ``MMEM:CDIR "C:\\..."`` (ключ ``state.dir``, путь на диске анализатора).

Подключение не создаёт и не удаляет измерения пользователя (если измерений нет - создаётся
``VL_MEAS``). Что читать, выбирает пользователь: ``MEAS_SLOTS`` слотов ``m1..m16``, у слота -
канал (``m{n}.channel``), имя измерения (``m{n}.name``) и ``m{n}.enabled``. Читаются только
включённые слоты, и только каналы, которые им нужны. После каждой завершённой развёртки для
каждого включённого слота публикуются ``m{n}.trace`` (в формате измерения) и ``m{n}.sdata``
(комплексная). По умолчанию слоты занимают измерения канала 1 по порядку каталога, включён
только m1. Таблица измерений для окна: ``meas_table`` -> [(канал, имя, S-параметр, формат, математика)];
``meas_config`` (канал, имя, что, значение) - S-параметр / формат / математика любого измерения;
``meas_create`` (канал, S-параметр) - новое измерение в окне канала, ``meas_delete`` (канал, имя),
``meas_move`` (канал, имя, новый канал) - пересоздать измерение в другом канале (S-параметр и формат
сохраняются, память и математика - нет; слот, который его читал, переходит на новое);
все три возвращают новую таблицу. Действия: ``meas_catalog`` (каталог всех каналов), ``meas_all`` (занять слоты первыми
измерениями всех каналов и включить), ``meas_none``. Легаси-ключи ``trace`` / ``sparam`` /
``format`` и маркер - первое измерение канала 1 (``trace`` = m1.trace).
Память и математика - у каждого слота: ``m{n}.math``, действие ``mem_store`` (аргументы - номера
слотов, без аргументов - все включённые). Состояния: ``state_list`` / ``state_save`` / ``state_recall``
/ ``state_delete``; после вызова состояния каталог измерений перечитывается и слоты собираются
заново (как при подключении: включён m1).
Режим генератора ``gen.state``: канал ``stim.channel`` переходит в CW, остальные каналы не
развёртываются (стоят в HOLD, их слоты не читаются), канал стимула закреплён (``gen.channel``).
Перед включением запоминаются тип развёртки, старт/стоп, точки, полоса ПЧ, время, усреднение,
коррекция, мощность и ``OUTP`` канала, а также режим развёртки ``SENS<c>:SWE:MODE`` всех каналов
(остальные ставятся в HOLD); выключение возвращает всё как было (частота CW остаётся; SING / GROUP
возвращаются как HOLD, чтобы не запустить развёртку).
Отключение от прибора в режиме генератора тоже возвращает настройки.
Как и анализатор спектра, ВАЦ ведётся однократными развёртками: наружу уходят только
целые трассы. Ось X считается линейной от ``SENS<c>:FREQ:STAR`` до ``STOP`` канала измерения
(в CW - от 0 до времени развёртки, в секундах).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, Kind, Param
from ..core.units import parse_number
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
#: ключи стимула: относятся к каналу ``stim.channel`` (в SCPI ``SENS1:`` / ``SOUR1:`` -> номер канала)
STIM_KEYS = (*FREQ_KEYS, "freq.cw", "sweep.type", "sweep.points", "sweep.time", "sweep.time_auto", "bw.if",
             "avg.state", "avg.count", "cal.state", "cal.set", "cal.interp", "source.power", "trig.mode")
MEAS_NAME = "VL_MEAS"
MEAS_SLOTS = 16                  # слотов выбора измерений (m1..m16)
FORMATS = ("MLOG", "PHASE", "UPHASE", "GDELAY", "SWR", "LIN_MAG", "REAL", "IMAG", "POLAR", "SMITH")
FORMAT_MAP = (("PHASE", "PHAS"), ("UPHASE", "UPH"), ("GDELAY", "GDEL"), ("LIN_MAG", "MLIN"), ("POLAR", "POL"),
              ("SMITH", "SMIT"))
COMPLEX_FORMATS = ("POLAR", "SMITH")     # FDATA - пары re,im: трасса с Trace.im
#: что режим генератора запоминает и возвращает (в этом порядке)
GEN_KEYS = ("sweep.type", "freq.start", "freq.stop", "sweep.points", "bw.if", "sweep.time_auto", "sweep.time",
            "avg.state", "avg.count", "cal.state", "source.power", "rf.output")
#: ключи трейса ``trace.sel``: масштаб на экране прибора и обработка данных
SCALE_KEYS = ("scale.div", "scale.ref", "scale.rpos", "scale.edel", "smooth.state", "smooth.aper")
#: маркер ``mark.n`` трейса ``trace.sel``
MARK_KEYS = ("mark.state", "mark.x", "mark.y")
MARKS = 10
SWEEP_TYPES = ("LIN", "LOG", "POWER", "CW", "SEGMENT")
SWEEP_MAP = (("POWER", "POW"), ("SEGMENT", "SEGM"))
TRIG_SOURCES = ("IMM", "EXT", "MAN")
TRIG_MODES = ("CHANNEL", "SWEEP", "POINT", "TRACE")
TRIG_MODE_MAP = (("CHANNEL", "CHAN"), ("SWEEP", "SWE"), ("POINT", "POIN"), ("TRACE", "TRAC"))
_MEAS_KEY = re.compile(r"m(\d+)\.(\w+)")
SLOT_KEYS = ("channel", "name", "enabled", "sparam", "format", "math")
MATH = ("NORM", "DIV", "SUBT", "ADD", "MULT")
STATE_EXT = ".csa"
_STATE_NAME = re.compile(r"[A-Za-z0-9_\-]{1,32}")


@dataclass
class _Slot:
    """Слот выбора измерения: что именно читать и читать ли."""
    channel: int = 1
    name: str = ""
    enabled: bool = False


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
        self._complex = False                  # SDATA - только по запросу (комплексные форматы есть сами)
        self._mark_n = 1
        self._cal_stim = True                  # cal set применяется вместе со своим стимулом
        #: идущая калибровка: {"channel", "steps": [описание], "done": {шаг}, "busy": шаг | None}
        self._cal: dict | None = None
        self._sweep: dict[str, Any] = {}       # последняя завершённая развёртка: ключи m{n}.*, trace, meas.count
        self._slots = [_Slot() for _ in range(MEAS_SLOTS)]
        self._armed_channels: list[int] = [1]
        self.meas_name = ""
        self._stim_ch = 1
        #: режим генератора: (канал, снимок GEN_KEYS, {канал: SWE:MODE до включения})
        self._gen: tuple[int, dict[str, Any], dict[int, str]] | None = None
        self._sel: tuple[int, str] | None = None                # trace.sel: (канал, имя измерения)
        super().__init__(transport, parent, command_delay_s)

    @property
    def channels(self) -> int:
        """Число слотов измерений m{n} (раскрытие ``m{n}.*`` профиля)."""
        return MEAS_SLOTS

    # ------------------------------------------------------------ описание
    def build_params(self):
        m = self.model
        st, rs, sw, mk = "Анализ/Стимул", "Анализ/Измерение", "Анализ/Развёртка", "Анализ/Маркер"
        tg = "Анализ/Запуск"
        sc = "Анализ/Масштаб"
        freq = dict(kind=F, unit="Hz", minimum=m.f_min, maximum=m.f_max, tolerance=1e-9,
                    also_read=FREQ_KEYS)
        ch = dict(channel=1)          # перед CALC-командой выбрать наше измерение
        per_meas = []
        for n in range(1, MEAS_SLOTS + 1):
            sec, rd = f"Измерения/m{n}", (f"m{n}.sparam", f"m{n}.format")
            per_meas += [
                Param(f"m{n}.channel", f"Канал m{n}", I, section=sec, query="<virtual>", write="<virtual>",
                      minimum=1, maximum=200, also_read=rd),
                Param(f"m{n}.name", f"Имя m{n}", Kind.STR, section=sec, query="<virtual>", write="<virtual>",
                      also_read=rd),
                Param(f"m{n}.enabled", f"Читать m{n}", B, section=sec, query="<virtual>", write="<virtual>"),
                Param(f"m{n}.sparam", f"S-параметр m{n}", E, section=sec, query="<catalog>", write="<custom>",
                      choices=("S11", "S21", "S12", "S22")),
                Param(f"m{n}.format", f"Формат m{n}", E, section=sec, query="<format>", write="<custom>",
                      choices=FORMATS, enum_map=FORMAT_MAP),
                Param(f"m{n}.math", f"Математика m{n}", E, section=sec, query="<math>", write="<custom>",
                      choices=MATH, labels=("Данные", "Д/П", "Д-П", "Д+П", "Д*П")),
                Param(f"m{n}.trace", f"Трасса m{n}", Kind.TRACE, "", section=sec, group="SWEEP"),
                Param(f"m{n}.sdata", f"Комплексная трасса m{n}", Kind.TRACE, "", section=sec, group="SWEEP"),
            ]
        return per_meas + [
            Param("trace", "Трасса", Kind.TRACE, "", section="Трасса/Трасса", group="SWEEP"),
            Param("meas.count", "Измерений", I, section=rs, group="SWEEP"),
            Param("stim.channel", "Канал стимула", I, section=st, query="<virtual>", write="<virtual>",
                  minimum=1, maximum=200, also_read=STIM_KEYS),
            Param("state.dir", "Папка состояний на анализаторе", Kind.STR, section="Память",
                  query="MMEM:CDIR?", write='MMEM:CDIR "{value}"'),
            Param("trace.sel", "Трейс (масштаб)", Kind.STR, section=sc, query="<virtual>", write="<virtual>",
                  also_read=(*SCALE_KEYS, *MARK_KEYS)),
            Param("mark.n", "Маркер трейса", I, section=mk, query="<virtual>", write="<virtual>", minimum=1,
                  maximum=MARKS, also_read=MARK_KEYS),
            Param("mark.state", "Маркер вкл.", B, section=mk, query="<mark>", write="<mark>",
                  also_read=("mark.x", "mark.y")),
            Param("mark.x", "X маркера", F, "Hz", section=mk, query="<mark>", write="<mark>", minimum=0.0,
                  maximum=m.f_max, tolerance=1e-6, also_read=("mark.state", "mark.y")),
            Param("mark.y", "Значение маркера", F, "", section=mk, query="<mark>"),
            Param("scale.div", "Цена деления", F, "", section=sc, query="<scale>", write="<scale>",
                  minimum=1e-18, maximum=1e12, tolerance=1e-6),
            Param("scale.ref", "Опорный уровень", F, "", section=sc, query="<scale>", write="<scale>",
                  minimum=-1e12, maximum=1e12, tolerance=1e-6),
            Param("scale.rpos", "Опорная клетка", F, "div", section=sc, query="<scale>", write="<scale>",
                  minimum=0.0, maximum=20.0, tolerance=1e-6),
            Param("scale.edel", "Электрическая задержка", F, "s", section=sc, query="<scale>", write="<scale>",
                  minimum=-10.0, maximum=10.0, tolerance=1e-15),
            Param("smooth.state", "Сглаживание", B, section=sc, query="<scale>", write="<scale>"),
            Param("smooth.aper", "Апертура сглаживания", F, "%", section=sc, query="<scale>", write="<scale>",
                  minimum=1.0, maximum=25.0, tolerance=1e-3),
            Param("gen.state", "Режим генератора", B, section=st, query="<virtual>", write="<virtual>",
                  also_read=(*STIM_KEYS, "rf.output", "gen.channel", "stim.channel")),
            Param("gen.channel", "Канал генератора", I, section=st, query="<virtual>"),
            Param("meas.complex", "Комплексные данные", B, section=rs, query="<virtual>", write="<virtual>"),
            Param("meas.armed", "Идёт развёртка", B, section=sw, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=sw, query="<virtual>", write="<virtual>"),
            Param("sparam", "S-параметр", E, section=rs, query="<catalog>",
                  write="CALC1:PAR:MOD:EXT '{value}'", choices=("S11", "S21", "S12", "S22"), **ch),
            Param("format", "Формат", E, section=rs, query="CALC1:FORM?", write="CALC1:FORM {value}",
                  choices=FORMATS,
                  labels=("ЛогАмп, dB", "Фаза", "Фаза развёрн.", "ГВЗ", "КСВН", "ЛинАмп", "Re", "Im", "Полярный",
                          "Смит"),
                  enum_map=FORMAT_MAP,
                  also_read=("marker.value",), **ch),
            Param("cal.state", "Коррекция", B, section=rs, query="SENS1:CORR:STAT?",
                  write="SENS1:CORR:STAT {value}"),
            Param("cal.set", "Cal set канала", Kind.STR, section=rs, query="<virtual>", write="<virtual>",
                  also_read=STIM_KEYS),
            Param("cal.stim", "Cal set со стимулом", B, section=rs, query="<virtual>", write="<virtual>"),
            Param("cal.busy", "Идёт измерение стандарта", B, section=rs, query="<virtual>"),
            Param("cal.interp", "Интерполяция калибровки", B, section=rs, query="SENS1:CORR:INT?",
                  write="SENS1:CORR:INT {value}", also_read=("cal.state",)),
            Param("freq.start", "Старт", section=st, query="SENS1:FREQ:STAR?", write="SENS1:FREQ:STAR {value}",
                  **freq),
            Param("freq.stop", "Стоп", section=st, query="SENS1:FREQ:STOP?", write="SENS1:FREQ:STOP {value}",
                  **freq),
            Param("freq.center", "Центр", section=st, query="SENS1:FREQ:CENT?", write="SENS1:FREQ:CENT {value}",
                  **freq),
            Param("freq.span", "Полоса", section=st, query="SENS1:FREQ:SPAN?", write="SENS1:FREQ:SPAN {value}",
                  **{**freq, "minimum": 0.0}),
            Param("sweep.type", "Тип развёртки", E, section=sw, query="SENS1:SWE:TYPE?",
                  write="SENS1:SWE:TYPE {value}", choices=SWEEP_TYPES,
                  labels=("Лин.", "Лог.", "Мощность", "CW", "Сегменты"), enum_map=SWEEP_MAP,
                  also_read=("sweep.time", "freq.cw")),
            Param("freq.cw", "Частота CW", section=st, query="SENS1:FREQ:CW?", write="SENS1:FREQ:CW {value}",
                  **{**freq, "also_read": ()}),
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
            Param("trig.source", "Запуск", E, section=tg, query="TRIG:SOUR?", write="TRIG:SOUR {value}",
                  choices=TRIG_SOURCES, labels=("Внутренний", "Внешний", "Ручной")),
            Param("trig.scope", "Область запуска", E, section=tg, query="TRIG:SCOP?", write="TRIG:SCOP {value}",
                  choices=("ALL", "CURRENT"), labels=("Все каналы", "Текущий канал"),
                  enum_map=(("CURRENT", "CURR"),)),
            Param("trig.mode", "Режим запуска канала", E, section=tg, query="SENS1:SWE:TRIG:MODE?",
                  write="SENS1:SWE:TRIG:MODE {value}", choices=TRIG_MODES,
                  labels=("Канал", "Развёртка", "Точка", "Трасса"), enum_map=TRIG_MODE_MAP),
            Param("trig.slope", "Фронт внешнего запуска", E, section=tg, query="TRIG:SLOP?",
                  write="TRIG:SLOP {value}", choices=("POS", "NEG"), enum_map=(("POS", "POS"), ("NEG", "NEG"))),
            Param("trig.type", "Тип внешнего запуска", E, section=tg, query="TRIG:TYPE?", write="TRIG:TYPE {value}",
                  choices=("EDGE", "LEVEL"), labels=("Фронт", "Уровень"), enum_map=(("LEVEL", "LEV"),)),
            Param("trig.delay", "Задержка запуска", F, "s", section=tg, query="TRIG:DEL?", write="TRIG:DEL {value}",
                  minimum=0.0, maximum=3.0, tolerance=1e-9),
            Param("marker.state", "Маркер", B, section=mk, query="CALC1:MARK1?", write="CALC1:MARK1 {value}",
                  also_read=("marker.freq", "marker.value"), **ch),
            Param("marker.freq", "Частота маркера", F, "Hz", section=mk, query="CALC1:MARK1:X?",
                  write="CALC1:MARK1:X {value}", minimum=0.0, maximum=m.f_max, tolerance=1e-6,
                  also_read=("marker.value", "marker.state"), **ch),
            Param("marker.value", "Значение маркера", F, "", section=mk, query="<marker>", **ch),
        ]

    def build_actions(self):
        return [Action("single", "Одна развёртка"), Action("abort", "Стоп"),
                Action("trigger", "Ручной запуск (TRIG:SOUR MAN)"),
                Action("autoscale", "Автомасштаб трейса trace.sel"),
                Action("mark_peak", "Маркер mark.n трейса на максимум"), Action("mark_min", "Маркер на минимум"),
                Action("mark_aoff", "Выключить маркеры трейса"), Action("mark_table", "Маркеры трейса"),
                Action("marker_peak", "Маркер на максимум"), Action("marker_min", "Маркер на минимум"),
                Action("meas_catalog", "Каталог измерений"), Action("meas_all", "Читать все измерения"),
                Action("meas_none", "Не читать измерения"),
                Action("meas_table", "Таблица измерений"), Action("meas_config", "Настроить измерение"),
                Action("meas_create", "Создать измерение"), Action("meas_move", "Перенести измерение в канал"),
                Action("meas_delete", "Удалить измерение", confirm="Удалить измерение с прибора?"),
                Action("mem_store", "Данные в память"),
                Action("state_list", "Состояния в памяти"), Action("state_dirs", "Папки на диске анализатора"), Action("state_save", "Сохранить состояние"),
                Action("state_recall", "Вызвать состояние"),
                Action("state_delete", "Удалить состояние", confirm="Удалить состояние из памяти анализатора?"),
                Action("cal_list", "Cal set'ы в памяти прибора"),
                Action("cal_delete", "Удалить cal set", confirm="Удалить cal set из памяти анализатора?"),
                Action("cal_conns", "Разъёмы для калибровки"), Action("cal_kits", "Наборы для разъёма"),
                Action("cal_start", "Начать калибровку"), Action("cal_measure", "Измерить стандарт"),
                Action("cal_status", "Шаги калибровки"), Action("cal_save", "Сохранить калибровку в cal set"),
                Action("cal_abort", "Прервать калибровку"),
                Action("cal_start_thru", "Калибровка по перемычке (response THRU) трейса trace.sel"),
                Action("snp_read", "S-параметры канала (Touchstone)"),
                Action("reset", "Preset *RST",
                       confirm="Preset удалит измерения и выключит калибровку. Продолжить?")]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self._armed = False
        self._cal = None
        self._stim_ch = 1
        self._gen = None                      # после *RST / вызова состояния возвращать нечего
        self._sweep = {}
        self.write("FORM:DATA ASCII,0")
        cat = self._catalog()
        if not cat:
            self.write(f"CALC1:PAR:DEF:EXT '{MEAS_NAME}','S11'")
            self.write("DISP:WIND1:STAT ON")
            self.write(f"DISP:WIND1:TRAC1:FEED '{MEAS_NAME}'")
            cat = self._catalog() or {MEAS_NAME: "S11"}
        self.meas_name = next(iter(cat))
        self._sel = (1, self.meas_name)
        self._slots = [_Slot() for _ in range(MEAS_SLOTS)]
        for slot, name in zip(self._slots, cat):               # по умолчанию - канал 1 по порядку, читаем только m1
            slot.name = name
        self._slots[0].enabled = True

    def on_disconnecting(self) -> None:
        self._cal = None
        if self._armed:
            self._abort()
        if self._gen is not None:             # не оставлять прибор в режиме генератора
            gen, self._gen = self._gen, None
            try:
                self._gen_restore(*gen)
            except Exception:                 # отключение не должно срываться
                pass

    def _catalog(self, channel: int = 1) -> dict[str, str]:
        """{имя измерения: S-параметр} канала."""
        raw = self.query(f"CALC{channel}:PAR:CAT:EXT?").strip().strip('"').strip()
        if not raw or raw.upper().startswith("NO CATALOG"):
            return {}
        items = [t.strip() for t in raw.split(",")]
        return dict(zip(items[0::2], (s.upper() for s in items[1::2])))

    def _channels(self) -> list[int]:
        raw = self.query("SYST:CHAN:CAT?")
        return sorted({int(t) for t in re.findall(r"\d+", raw)}) or [1]

    def select_channel(self, channel: int) -> None:
        """Выбор измерения легаси-ключей (первое измерение канала 1)."""
        self.write(f"CALC1:PAR:SEL '{self.meas_name}'")

    def _select(self, slot: _Slot) -> None:
        self.write(f"CALC{slot.channel}:PAR:SEL '{slot.name}'")

    def _slot_of(self, key: str) -> tuple[int, _Slot, str] | None:
        m = _MEAS_KEY.fullmatch(key)
        if m and 1 <= int(m.group(1)) <= MEAS_SLOTS:
            return int(m.group(1)), self._slots[int(m.group(1)) - 1], m.group(2)
        return None

    def _active_channels(self) -> list[int]:
        """Каналы, которые нужно развернуть: включённые слоты (иначе - канал 1); генератор - только его канал."""
        if self._gen is not None:
            return [self._gen[0]]
        return sorted({s.channel for s in self._slots if s.enabled and s.name}) or [1]

    def write_param(self, spec: Param, value: Any) -> None:
        if spec.key == "meas.continuous":
            self._continuous = bool(value)
            if self._continuous and not self._armed:
                self._start_sweep()
            return
        if spec.key == "meas.complex":
            self._complex = bool(value)
            return
        if spec.key == "gen.state":
            self._set_gen(bool(value))
            return
        if spec.key == "cal.stim":
            self._cal_stim = bool(value)
            return
        if spec.key == "cal.set" and self._gen is not None and self._gen[0] == self._stim_ch:
            raise ValueError("режим генератора: сначала выключите CW, потом меняйте cal set")
        if spec.key == "trace.sel":
            c, _, name = str(value).partition(":")
            if not c.strip().isdigit() or name not in self._catalog(int(c)):
                raise ValueError(f"нет трейса {value!r} (нужно «канал:имя»)")
            self._sel = (int(c), name)
            return
        if spec.key == "mark.n":
            n = int(value)
            if not 1 <= n <= MARKS:
                raise ValueError(f"маркер 1..{MARKS}, получено {n}")
            self._mark_n = n
            return
        if spec.key in ("mark.state", "mark.x"):
            cmd = self._mark_cmd()
            self.write((cmd if spec.key == "mark.state" else cmd + ":X") + " " + spec.to_scpi(value))
            return
        if spec.key in SCALE_KEYS:
            cmd = self._scale_cmd(spec.key)
            if not cmd:
                raise ValueError(f"трейс {self._sel[1]!r} не показан на экране анализатора - масштаба нет")
            self.write(cmd + " " + spec.to_scpi(value))
            return
        if spec.key == "stim.channel":
            c = int(value)
            if self._gen is not None and c != self._gen[0]:
                raise ValueError(f"режим генератора на канале {self._gen[0]}: сначала выключите CW")
            if c not in self._channels():
                raise ValueError(f"нет канала {c} (каналы: {self._channels()})")
            self._stim_ch = c
            return
        spec = self._stim_spec(spec)
        was_armed = self._armed
        self._abort()
        try:
            if spec.key == "cal.set":
                self._cal_apply(str(value or "").strip())
            elif not self._write_slot(spec, value):
                super().write_param(spec, value)
        finally:                                  # отказ записи не должен останавливать развёртки
            if was_armed or self._continuous:
                self._start_sweep()

    def _write_slot(self, spec: Param, value: Any) -> bool:
        found = self._slot_of(spec.key)
        if found is None:
            return False
        n, slot, what = found
        if what == "enabled":
            slot.enabled = bool(value)
        elif what == "channel":
            slot.channel = int(value)
        elif what == "name":
            name = str(value).strip()
            if name and name not in self._catalog(slot.channel):
                raise ValueError(f"нет измерения {name!r} на канале {slot.channel}")
            slot.name = name
        else:                                      # sparam / format - у выбранного измерения
            if not slot.name:
                raise ValueError(f"слот m{n} не назначен (задайте m{n}.name)")
            self._select(slot)
            if what == "sparam":
                self.write(f"CALC{slot.channel}:PAR:MOD:EXT '{spec.to_scpi(value)}'")
            elif what == "math":
                self.write(f"CALC{slot.channel}:MATH:FUNC {spec.to_scpi(value)}")
            else:
                self.write(f"CALC{slot.channel}:FORM {spec.to_scpi(value)}")
        return True

    def read_param_raw(self, spec: Param) -> Any:
        key = spec.key
        if key == "meas.continuous":
            return self._continuous
        if key == "meas.armed":
            return self._armed
        if key == "meas.complex":
            return self._complex
        if key == "stim.channel":
            return self._stim_ch
        if key == "gen.state":
            return self._gen is not None
        if key == "cal.stim":
            return self._cal_stim
        if key == "cal.busy":
            return self._cal is not None and self._cal["busy"] is not None
        if key == "cal.set":
            name = self.query(f"SENS{self._stim_ch}:CORR:CSET:ACT? NAME").strip().strip('"').strip()
            return "" if not name or name.upper().startswith("NO ") else name
        if key == "trace.sel":
            return f"{self._sel[0]}:{self._sel[1]}" if self._sel else None
        if key == "mark.n":
            return self._mark_n
        if key in MARK_KEYS:
            if not self._sel or self._sel[1] not in self._catalog(self._sel[0]):
                return None
            cmd = self._mark_cmd()
            on = self.query(cmd + "?").strip() in ("1", "ON")
            if key == "mark.state":
                return on
            if key == "mark.x":
                return spec.from_scpi(self.query(cmd + ":X?"))
            return parse_number(self.query(cmd + ":Y?").split(",")[0]) if on else None
        if key in SCALE_KEYS:
            if not self._sel or self._sel[1] not in self._catalog(self._sel[0]):
                return None
            cmd = self._scale_cmd(key)
            return spec.from_scpi(self.query(cmd + "?")) if cmd else None
        if key == "gen.channel":
            return self._gen[0] if self._gen is not None else None
        if key in STIM_KEYS:
            return super().read_param_raw(self._stim_spec(spec))
        if key == "sparam":
            return self._catalog().get(self.meas_name)
        found = self._slot_of(key)
        if found is not None:
            n, slot, what = found
            if what == "enabled":
                return slot.enabled
            if what == "channel":
                return slot.channel
            if what == "name":
                return slot.name or None
            if not slot.name:
                return None
            cat = self._catalog(slot.channel)
            if slot.name not in cat:
                return None                       # измерение удалено на приборе
            if what == "sparam":
                return cat[slot.name]
            self._select(slot)
            cmd = "MATH:FUNC" if what == "math" else "FORM"
            return spec.from_scpi(self.query(f"CALC{slot.channel}:{cmd}?"))
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
        if self._cal is not None:                  # калибровка: стандарты меряет cal_measure
            return
        self._armed_channels = self._active_channels()
        self.arm_opc(*[f"SENS{c}:SWE:MODE SING" for c in self._armed_channels])
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            for c in self._armed_channels:
                self.write(f"SENS{c}:SWE:MODE HOLD")
            self._set_armed(False)

    def _stim_spec(self, spec: Param, channel: int | None = None) -> Param:
        """Ключ стимула -> команды для канала ``channel`` (по умолчанию ``stim.channel``)."""
        c = self._stim_ch if channel is None else channel
        if spec.key not in STIM_KEYS or c == 1:
            return spec
        sub = lambda t: t and t.replace("SENS1:", f"SENS{c}:").replace("SOUR1:", f"SOUR{c}:")
        return replace(spec, query=sub(spec.query), write=sub(spec.write))

    # ------------------------------------------------------- маркеры трейса trace.sel
    def _mark_cmd(self, n: int | None = None) -> str:
        if not self._sel:
            raise ValueError("трейс не выбран (trace.sel)")
        c, name = self._sel
        self.write(f"CALC{c}:PAR:SEL '{name}'")
        return f"CALC{c}:MARK{self._mark_n if n is None else n}"

    def _mark_table(self) -> list[tuple[int, float, float | None]]:
        out = []
        if not self._sel or self._sel[1] not in self._catalog(self._sel[0]):
            return out
        for n in range(1, MARKS + 1):
            cmd = self._mark_cmd(n)
            if self.query(cmd + "?").strip() in ("1", "ON"):
                out.append((n, parse_number(self.query(cmd + ":X?")),
                            parse_number(self.query(cmd + ":Y?").split(",")[0])))
        return out

    # ------------------------------------------------------- масштаб трейса trace.sel
    def _scale_cmd(self, key: str) -> str:
        """Команда (без ?) ключа масштаба/обработки для трейса ``trace.sel``; выбирает его измерение."""
        if not self._sel:
            raise ValueError("трейс не выбран (trace.sel)")
        c, name = self._sel
        self.write(f"CALC{c}:PAR:SEL '{name}'")
        if key == "scale.edel":
            return f"CALC{c}:CORR:EDEL:TIME"
        if key == "smooth.state":
            return f"CALC{c}:SMO:STAT"
        if key == "smooth.aper":
            return f"CALC{c}:SMO:APER"
        w = int(parse_number(self.query(f"CALC{c}:PAR:WNUM?")))
        t = int(parse_number(self.query(f"CALC{c}:PAR:TNUM?")))
        if w < 1 or t < 1:
            return ""                                        # измерение не показано ни в одном окне
        return f"DISP:WIND{w}:TRAC{t}:Y:" + {"scale.div": "PDIV", "scale.ref": "RLEV", "scale.rpos": "RPOS"}[key]

    # ------------------------------------------------------- режим генератора
    def _set_gen(self, on: bool) -> None:
        if on == (self._gen is not None):
            return
        was_armed = self._armed
        self._abort()
        try:
            if on:
                c = self._stim_ch
                snap = {k: super(KeysightPNA, self).read_param_raw(self._stim_spec(self._params[k], c))
                        for k in GEN_KEYS}
                modes = {ch: self.query(f"SENS{ch}:SWE:MODE?").strip().upper() for ch in self._channels()}
                self._gen = (c, snap, modes)
                for ch, mode in modes.items():               # остальные каналы стоят (в т.ч. бывшие CONT)
                    if ch != c and mode != "HOLD":
                        self.write(f"SENS{ch}:SWE:MODE HOLD")
                self.write(f"SENS{c}:SWE:TYPE CW")
            else:
                gen, self._gen = self._gen, None
                self._gen_restore(*gen)
        finally:
            if was_armed or self._continuous:
                self._start_sweep()

    def _gen_restore(self, channel: int, snap: dict[str, Any], modes: dict[int, str]) -> None:
        """Вернуть настройки канала и режимы развёртки всех каналов, запомненные при включении генератора
        (ошибка одной настройки не мешает остальным)."""
        first: Exception | None = None
        for k in GEN_KEYS:
            v = snap.get(k)
            if v is None or (k == "sweep.time" and snap.get("sweep.time_auto")):
                continue
            try:
                super().write_param(self._stim_spec(self._params[k], channel), v)
            except Exception as e:  # noqa: BLE001
                first = first or e
        existing = set(self._channels())
        for ch, mode in modes.items():
            if ch not in existing:                           # канал удалили в режиме генератора
                continue
            mode = "HOLD" if mode.startswith(("SING", "GRO")) else mode
            try:
                self.write(f"SENS{ch}:SWE:MODE {mode}")
            except Exception as e:  # noqa: BLE001
                first = first or e
        if first is not None:
            raise first

    def _stimulus(self, channel: int) -> tuple[float, float, str]:
        """Ось X трасс канала: (начало, конец, единица). CW - время развёртки.

        Всегда с прибора, а не из кэша: стимул могли поменять на экране PNA. У канала, который показывают
        ключи стимула (stim.channel), новые значения публикуются - окно увидит их без SYNC."""
        typ = start = stop = None
        if channel == self._stim_ch:
            keys = ("sweep.type", "freq.start", "freq.stop")
            old = tuple(self._values.get(k) for k in keys)
            self._read_keys(keys)
            typ, start, stop = (self._values.get(k) for k in keys)
            if (typ, start, stop) != old:
                self._read_keys(("freq.center", "freq.span", "sweep.points", "sweep.time"))
        if typ is None:
            typ = self.query(f"SENS{channel}:SWE:TYPE?").strip().upper()
        if typ == "CW":
            return 0.0, self.query_float(f"SENS{channel}:SWE:TIME?"), "s"
        if start is not None and stop is not None:
            return start, stop, "Hz"
        return self.query_float(f"SENS{channel}:FREQ:STAR?"), self.query_float(f"SENS{channel}:FREQ:STOP?"), "Hz"

    def _read_trace(self) -> None:
        """Включённые слоты после завершённой развёртки: форматированные и комплексные данные."""
        stim = {k: self._values.get(k) for k in ("bw.if", "source.power", "avg.state", "avg.count", "cal.state")}
        cats: dict[int, dict[str, str]] = {}
        axes: dict[int, tuple[float, float, str]] = {}
        out: dict[str, Any] = {}
        count = 0
        for n, slot in enumerate(self._slots, 1):
            out[f"m{n}.trace"] = out[f"m{n}.sdata"] = None
            if not (slot.enabled and slot.name) or (self._gen is not None and slot.channel != self._gen[0]):
                continue                                       # генератор: остальные каналы не развёртываются
            c = slot.channel
            cat = cats.setdefault(c, self._catalog(c))
            sparam = cat.get(slot.name)
            if sparam is None:                                 # удалено на приборе
                continue
            start, stop, x_unit = axes.setdefault(c, self._stimulus(c))
            self._select(slot)
            fmt = self._params[f"m{n}.format"].from_scpi(self.query(f"CALC{c}:FORM?"))
            math = self._params[f"m{n}.math"].from_scpi(self.query(f"CALC{c}:MATH:FUNC?"))
            y = [parse_number(t) for t in self.query(f"CALC{c}:DATA? FDATA").split(",") if t.strip()]
            meta = {**(stim if c == self._stim_ch else {}), "channel": c, "name": slot.name, "sparam": sparam, "format": fmt, "math": math}
            if fmt in COMPLEX_FORMATS:                         # пары re,im
                out[f"m{n}.trace"] = Trace.linear(start, stop, y[0::2], im=y[1::2], x_unit=x_unit, y_unit="",
                                                  name=sparam, meta=meta)
            else:
                out[f"m{n}.trace"] = Trace.linear(start, stop, y, x_unit=x_unit, y_unit=Y_UNITS.get(fmt, ""),
                                                  name=sparam, meta=meta)
            if self._complex:
                raw = [parse_number(t) for t in self.query(f"CALC{c}:DATA? SDATA").split(",") if t.strip()]
                out[f"m{n}.sdata"] = Trace.linear(start, stop, raw[0::2], im=raw[1::2], x_unit=x_unit, y_unit="",
                                                  name=sparam, meta={**meta, "format": "SDATA", "math": "NORM"})
            count += 1
            for k, v in ((f"m{n}.sparam", sparam), (f"m{n}.format", fmt), (f"m{n}.math", math)):
                if self._values.get(k) != v:
                    self._publish(k, v)
        out["trace"] = out["m1.trace"]
        out["meas.count"] = count
        self._sweep = out
        for k, v in out.items():
            self._publish(k, v)
        if self._values.get("marker.state"):
            self._read_keys(["marker.freq", "marker.value"])
        if self._values.get("mark.state"):
            self._read_keys(["mark.y"])

    def after_poll_timeout(self) -> None:
        """Чтение трассы прервал таймаут (обмен уже восстановлен): развёртки не должны остановиться."""
        if self._continuous and not self._armed:
            self._start_sweep()

    def poll(self) -> None:
        if self._cal is not None and self._cal["busy"] is not None:
            if self.opc_done("калибровка"):
                self._cal["done"].add(self._cal["busy"])
                self._cal["busy"] = None
                self._publish("cal.busy", False)
            return
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
        if name == "autoscale":
            cmd = self._scale_cmd("scale.div")
            if not cmd:
                raise ValueError(f"трейс {self._sel[1]!r} не показан на экране анализатора")
            self.write(cmd.rsplit(":", 1)[0] + ":AUTO")
            self._read_keys(SCALE_KEYS)
            return None
        if name == "trigger":
            if not self._armed:                          # нечего запускать: взвести одну развёртку
                self._start_sweep()
            scope = self._values.get("trig.scope")
            for c in ([self._armed_channels[0]] if scope == "ALL" else self._armed_channels):
                self.write(f"INIT{c}:IMM")
            return None
        if name == "meas_catalog":
            return [(c, n, sp) for c in self._channels() for n, sp in self._catalog(c).items()]
        if name in ("meas_all", "meas_none"):
            was_armed = self._armed
            self._abort()
            try:
                if name == "meas_all":
                    entries = [(c, n) for c in self._channels() for n in self._catalog(c)][:MEAS_SLOTS]
                    self._slots = [_Slot(c, n, True) for c, n in entries] + \
                                  [_Slot() for _ in range(MEAS_SLOTS - len(entries))]
                else:
                    for slot in self._slots:
                        slot.enabled = False
                self._read_keys([f"m{n}.{w}" for n in range(1, MEAS_SLOTS + 1)
                                 for w in SLOT_KEYS])
            finally:
                if was_armed or self._continuous:
                    self._start_sweep()
            return None
        if name in ("mark_peak", "mark_min"):
            cmd = self._mark_cmd()
            self.write(f"{cmd}:FUNC:EXEC {'MAX' if name == 'mark_peak' else 'MIN'}")
            self._read_keys(MARK_KEYS)
            return self._mark_table()
        if name == "mark_aoff":
            c = self._sel[0]
            self._mark_cmd()
            self.write(f"CALC{c}:MARK:AOFF")
            self._read_keys(MARK_KEYS)
            return []
        if name == "mark_table":
            return self._mark_table()
        if name in ("marker_peak", "marker_min"):
            self.select_channel(1)
            if not self._values.get("marker.state"):
                self.write("CALC1:MARK1 ON")
                self._values["marker.state"] = True
            self.write(f"CALC1:MARK1:FUNC:EXEC {'MAX' if name == 'marker_peak' else 'MIN'}")
            self._read_keys(["marker.state", "marker.freq", "marker.value"])
            return None
        if name == "meas_table":
            return self._meas_table()
        if name in ("meas_config", "meas_create", "meas_delete", "meas_move"):
            was_armed = self._armed
            self._abort()
            try:
                getattr(self, f"_{name}")(*args)
                self._check_errors(context=name)
                self._read_keys([f"m{n}.{w}" for n in range(1, MEAS_SLOTS + 1) for w in SLOT_KEYS])
            finally:
                if was_armed or self._continuous:
                    self._start_sweep()
            return self._meas_table()
        if name == "mem_store":
            return self._mem_store(args)
        if name == "cal_list":
            return self._cal_list()
        if name.startswith("cal_") and name != "cal_delete":
            return self._cal_action(name, args)
        if name == "cal_delete":
            cset = str(args[0]) if args else ""
            if cset not in self._cal_list():
                raise ValueError(f"нет cal set {cset!r}")
            self.write(f'CSET:DEL "{cset}"')
            self._check_errors(context=name)
            self._read_keys(["cal.set", "cal.state"])
            return self._cal_list()
        if name == "snp_read":
            return self._snp_read(args[0] if args else "1,2")
        if name == "state_list":
            return self._state_list()
        if name == "state_dirs":
            return self._state_dirs()
        if name in ("state_save", "state_recall", "state_delete"):
            return self._state_file_action(name, args)
        if name == "reset":
            self._abort()
            self.write("*RST")
            self.query("*OPC?")
            self.on_connected()
            self._sync_all()
            return None
        return super().run_custom_action(name, args)

    # -------------------------------------------------------- память трасс
    def _mem_store(self, args: tuple) -> list[int]:
        """Данные -> память у слотов ``args`` (номера), без аргументов - у всех включённых."""
        nums = [int(a) for a in args] if args else \
            [n for n, s in enumerate(self._slots, 1) if s.enabled and s.name]
        for n in nums:
            if not 1 <= n <= MEAS_SLOTS or not self._slots[n - 1].name:
                raise ValueError(f"слот m{n} не назначен")
        for n in nums:                               # между развёртками: в памяти - целая трасса
            slot = self._slots[n - 1]
            self._select(slot)
            self.write(f"CALC{slot.channel}:MATH:MEM")
        self._check_errors(context="mem_store")
        return nums

    # ------------------------------------------------- калибровка (cal set)
    def _cal_list(self) -> list[str]:
        names = [e.strip() for e in re.split(r'[",]', self.query("CSET:CAT?")) if e.strip()]
        return sorted(set(n for n in names if not n.upper().startswith("NO ")), key=str.lower)

    def _cal_apply(self, name: str) -> None:
        """Cal set ``name`` -> канал ``stim.channel`` (пусто - снять); со стимулом cal set, если ``cal.stim``."""
        c = self._stim_ch
        if not name:
            self.write(f"SENS{c}:CORR:CSET:DEAC")
        else:
            if name not in self._cal_list():
                raise ValueError(f"нет cal set {name!r} в памяти анализатора")
            self.write(f'SENS{c}:CORR:CSET:ACT "{name}",{1 if self._cal_stim else 0}')
            self.query("*OPC?")
        self._check_errors(context="cal.set")

    # ------------------------------------------------- S-параметры канала (Touchstone)
    def _snp_read(self, ports) -> dict[str, Trace]:
        plist = sorted({int(p) for p in re.findall(r"\d+", str(ports))})
        if not plist or any(p not in (1, 2) for p in plist):
            raise ValueError(f"порты 1 и/или 2, получено {ports!r}")
        if self._cal is not None:
            raise ValueError("идёт калибровка")
        names = [f"S{a}{b}" for b in plist for a in plist]          # S11 S21 S12 S22 - порядок Touchstone
        c = self._stim_ch
        was_armed = self._armed
        self._abort()
        try:
            self.write("MMEM:STOR:TRAC:FORM:SNP RI")
            if self.query("TRIG:SOUR?").strip().upper().startswith("IMM"):   # EXT / MAN - последние данные
                self.write(f"SENS{c}:SWE:MODE SING")
                self.query("*OPC?")
            reply = self.query(f'CALC{c}:DATA:SNP:PORT? "{",".join(map(str, plist))}"')
            self._check_errors(context="snp_read")
        finally:
            if was_armed or self._continuous:
                self._start_sweep()
        vals = [parse_number(t) for t in reply.split(",") if t.strip()]
        n, rest = divmod(len(vals), 1 + 2 * len(names))
        if rest or not n:
            raise ValueError(f"SNP: {len(vals)} чисел не делятся на {1 + 2 * len(names)} столбцов")
        freqs = tuple(vals[:n])
        meta = {"channel": c, "format": "SDATA", "cal.set": self._values.get("cal.set") if c == self._stim_ch else None,
                "cal.state": self._values.get("cal.state"), "bw.if": self._values.get("bw.if"),
                "source.power": self._values.get("source.power")}
        out = {}
        for k, name in enumerate(names):
            re_, im = vals[n * (1 + 2 * k):n * (2 + 2 * k)], vals[n * (2 + 2 * k):n * (3 + 2 * k)]
            out[name] = Trace(freqs, tuple(re_), x_unit="Hz", y_unit="", name=name, im=tuple(im),
                              meta={**meta, "sparam": name})
        return out

    # ------------------------------------------------- калибровка (guided)
    @staticmethod
    def _names(reply: str) -> list[str]:
        return [e.strip() for e in re.split(r'[",]', reply) if e.strip()]

    def _cal_action(self, name: str, args: tuple) -> Any:
        c = self._cal["channel"] if self._cal is not None else self._stim_ch
        guid = f"SENS{c}:CORR:COLL:GUID"
        if name == "cal_conns":
            return [n for n in self._names(self.query(f"{guid}:CONN:CAT?")) if n.lower() != "not used"]
        if name == "cal_kits":
            kits = self._names(self.query(f'{guid}:CKIT:CAT? "{args[0] if args else ""}"'))
            return sorted(kits, key=lambda k: "ideal" not in k.lower())        # идеальные - первыми
        if name == "cal_status":
            return self._cal_status()
        if name == "cal_start":
            if self._gen is not None:
                raise ValueError("режим генератора: сначала выключите CW")
            conn1, kit1, conn2, kit2 = (list(args) + ["", "", "", ""])[:4]
            ports = [(n, str(conn).strip(), str(kit).strip()) for n, conn, kit in ((1, conn1, kit1), (2, conn2, kit2))]
            if not any(conn for _n, conn, _k in ports):
                raise ValueError("калибровка: задайте разъём хотя бы одного порта")
            for n, conn, kit in ports:
                if conn and not kit:
                    raise ValueError(f"калибровка: нет набора для порта {n}")
            self._abort()
            c = self._stim_ch
            guid = f"SENS{c}:CORR:COLL:GUID"
            for n, conn, kit in ports:
                self.write(f'{guid}:CONN:PORT{n} "{conn or "Not used"}"')
                if conn:
                    self.write(f'{guid}:CKIT:PORT{n} "{kit}"')
            self.write(f"{guid}:INIT")
            self._check_errors(context=name)
            steps = int(parse_number(self.query(f"{guid}:STEP?")))
            self._cal = {"channel": c, "done": set(), "busy": None, "kind": "guided",
                         "steps": [self.query(f"{guid}:DESC? {k}").strip().strip('"') for k in range(1, steps + 1)]}
            return self._cal_status()
        if name == "cal_start_thru":
            if self._gen is not None:
                raise ValueError("режим генератора: сначала выключите CW")
            c, meas = self._sel
            sparam = self._catalog(c).get(meas)
            if sparam not in ("S21", "S12"):
                raise ValueError(f"калибровка по перемычке - для трейса S21 / S12 (выбран {meas!r}: {sparam})")
            self._abort()
            self.write(f"CALC{c}:PAR:SEL '{meas}'")
            self.write(f"SENS{c}:CORR:COLL:METH TRAN1")
            self._check_errors(context=name)
            a, b = sparam[1], sparam[2]
            self._cal = {"channel": c, "done": set(), "busy": None, "kind": "thru", "meas": meas,
                         "steps": [f"Connect THRU between port {b} and port {a} ({sparam} {meas})"]}
            return self._cal_status()
        if self._cal is None:
            raise ValueError("калибровка не начата (cal_start)")
        if name == "cal_measure":
            k = int(args[0]) if args else 0
            if not 1 <= k <= len(self._cal["steps"]):
                raise ValueError(f"шаг 1..{len(self._cal['steps'])}, получено {k}")
            if self._cal["busy"] is not None:
                raise ValueError(f"идёт измерение шага {self._cal['busy']}")
            if self._cal["kind"] == "thru":
                self.write(f"CALC{c}:PAR:SEL '{self._cal['meas']}'")
                self.arm_opc(f"SENS{c}:CORR:COLL:ACQ THRU")
            else:
                self.arm_opc(f"{guid}:ACQ STAN{k}")
            self._cal["busy"] = k
            self._publish("cal.busy", True)
            return k
        if name == "cal_save":
            cset = str(args[0]).strip() if args else ""
            if not _STATE_NAME.fullmatch(cset):
                raise ValueError(f"имя cal set: буквы, цифры, _ и -, до 32 знаков (получено {cset!r})")
            if self._cal["busy"] is not None or len(self._cal["done"]) < len(self._cal["steps"]):
                raise ValueError("калибровка: измерены не все стандарты")
            if self._cal["kind"] == "thru":
                self.write(f"CALC{c}:PAR:SEL '{self._cal['meas']}'")
                self.write(f"SENS{c}:CORR:COLL:SAVE")
                self.query("*OPC?")
                self.write(f'SENS{c}:CORR:CSET:COPY "{cset}"')
                self.write(f'SENS{c}:CORR:CSET:ACT "{cset}",0')
            else:
                self.write(f'{guid}:SAVE:CSET "{cset}"')
            self.query("*OPC?")
            self._check_errors(context=name)
            self._cal_end()
            return self._cal_list()
        if name == "cal_abort":
            self._cal_end()
            return None
        return super().run_custom_action(name, args)

    def _cal_status(self) -> tuple[list[str], list[int]]:
        if self._cal is None:
            return [], []
        return list(self._cal["steps"]), sorted(self._cal["done"])

    def _cal_end(self) -> None:
        """Выйти из калибровки: перечитать калибровку и стимул канала, продолжить развёртки."""
        c = self._cal["channel"] if self._cal else self._stim_ch
        self._cal = None
        self._publish("cal.busy", False)
        if c == self._stim_ch:
            self._read_keys(STIM_KEYS)
        if self._continuous:
            self._start_sweep()

    # ------------------------------------------------- состояния в памяти прибора
    def _state_dirs(self) -> tuple[str, list[str]]:
        """(текущая папка, имена её подпапок): элементы ``MMEM:CAT?`` без расширения считаются папками."""
        path = self.query("MMEM:CDIR?").strip().strip('"')
        names = [e.strip() for e in re.split(r'[",]', self.query("MMEM:CAT?")) if e.strip()]
        return path, sorted({n for n in names if "." not in n}, key=str.lower)

    def _state_list(self) -> list[str]:
        reply = self.query("MMEM:CAT?")
        names = []
        for entry in re.split(r'[",]', reply):                  # "a.csa,b.cst,..." - только имена
            file = entry.strip()
            if file.lower().endswith(STATE_EXT):
                names.append(file[:-len(STATE_EXT)])
        return sorted(set(names))

    def _state_file_action(self, name: str, args: tuple) -> None:
        state = str(args[0]) if args else ""
        if not _STATE_NAME.fullmatch(state):
            raise ValueError(f"имя состояния: буквы, цифры, _ и -, до 32 знаков (получено {state!r})")
        file = f'"{state}{STATE_EXT}"'
        if name == "state_delete":
            self.write(f"MMEM:DEL {file}")
            self._check_errors(context=name)
            return
        was_armed = self._armed
        self._abort()
        if name == "state_save":
            try:
                self.write(f"MMEM:STOR {file}")
                self._check_errors(context=name)
            finally:
                if was_armed or self._continuous:
                    self._start_sweep()
            return
        self.write(f"MMEM:LOAD {file}")                          # state_recall
        self._check_errors(context=name)
        self.query("*OPC?")
        self.on_connected()                                      # измерения и каналы могли смениться
        self._sync_all()                                         # перечитать всё; непрерывная - запустится
        if was_armed and not self._armed:
            self._start_sweep()

    # ------------------------------------------------- таблица измерений (окно)
    def _meas_table(self) -> list[tuple[int, str, str, str, str]]:
        fmt, math = self._params["m1.format"], self._params["m1.math"]
        out = []
        for c in self._channels():
            for name, sparam in self._catalog(c).items():
                self.write(f"CALC{c}:PAR:SEL '{name}'")
                out.append((c, name, sparam, fmt.from_scpi(self.query(f"CALC{c}:FORM?")),
                            math.from_scpi(self.query(f"CALC{c}:MATH:FUNC?"))))
        return out

    def _meas_config(self, channel, name, what, value) -> None:
        c, name, what = int(channel), str(name), str(what)
        if name not in self._catalog(c):
            raise ValueError(f"нет измерения {name!r} на канале {c}")
        if what not in ("sparam", "format", "math"):
            raise ValueError(f"meas_config: sparam / format / math, получено {what!r}")
        spec = self._params[f"m1.{what}"]
        value = spec.validate(value)
        self.write(f"CALC{c}:PAR:SEL '{name}'")
        if what == "sparam":
            self.write(f"CALC{c}:PAR:MOD:EXT '{spec.to_scpi(value)}'")
        else:
            self.write(f"CALC{c}:{'FORM' if what == 'format' else 'MATH:FUNC'} {spec.to_scpi(value)}")

    def _meas_create(self, channel, sparam) -> str:
        c = int(channel)
        if not 1 <= c <= 200:
            raise ValueError(f"канал 1..200, получено {c}")
        sparam = self._params["m1.sparam"].validate(str(sparam).upper())
        names = set(self._catalog(c))
        k = 1
        while f"CH{c}_{sparam}_{k}" in names:
            k += 1
        name = f"CH{c}_{sparam}_{k}"
        self.write(f"CALC{c}:PAR:DEF:EXT '{name}','{sparam}'")
        # окно с номером канала; трейс - следующий свободный номер в нём
        if self.query(f"DISP:WIND{c}:STAT?").strip() not in ("1", "ON"):
            self.write(f"DISP:WIND{c}:STAT ON")
        used = [int(t) for t in re.findall(r"\d+", self.query(f"DISP:WIND{c}:CAT?"))]
        self.write(f"DISP:WIND{c}:TRAC{max(used, default=0) + 1}:FEED '{name}'")
        return name

    def _meas_delete(self, channel, name) -> None:
        c, name = int(channel), str(name)
        if name not in self._catalog(c):
            raise ValueError(f"нет измерения {name!r} на канале {c}")
        self.write(f"CALC{c}:PAR:DEL '{name}'")
        for slot in self._slots:
            if slot.channel == c and slot.name == name:
                slot.name, slot.enabled = "", False
        if c == 1 and name == self.meas_name:
            self.meas_name = next(iter(self._catalog(1)), "")

    def _meas_move(self, channel, name, new_channel) -> None:
        c, name, nc = int(channel), str(name), int(new_channel)
        cat = self._catalog(c)
        if name not in cat:
            raise ValueError(f"нет измерения {name!r} на канале {c}")
        if nc == c:
            return
        self.write(f"CALC{c}:PAR:SEL '{name}'")
        fmt = self.query(f"CALC{c}:FORM?").strip()
        new = self._meas_create(nc, cat[name])
        self.write(f"CALC{nc}:PAR:SEL '{new}'")
        self.write(f"CALC{nc}:FORM {fmt}")
        self.write(f"CALC{c}:PAR:DEL '{name}'")
        for slot in self._slots:
            if slot.channel == c and slot.name == name:
                slot.channel, slot.name = nc, new
        if c == 1 and name == self.meas_name:
            self.meas_name = next(iter(self._catalog(1)), "")
