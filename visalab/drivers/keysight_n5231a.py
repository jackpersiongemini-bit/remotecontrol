"""Драйвер векторных анализаторов цепей Keysight PNA / PNA-L (2 и 4 порта: N5230C-420, N5231A, N5232A, N5222A...).

Принцип работы (только данные S-параметров, без дисплея прибора):
* читаются исключительно сырые комплексные данные ``CALC<c>:DATA? SDATA`` (Re, Im) в двоичном
  формате IEEE 488.2 ``FORM:DATA REAL,64`` + ``FORM:BORD NORM`` (с авто-откатом на ``ASCII,0``,
  если прибор не принял двоичный формат);
* точная ось X стимула запрашивается через ``SENS<c>:X?`` (с кэшированием и сбросом при смене
  стимула) — корректно работает для ``LIN``, ``LOG``, ``SEGMENT``, ``POWER`` и ``CW``;
* дисплейные настройки прибора (шкалы, маркеры, лимиты, форматы ``FDATA``, экранная математика)
  не читаются и не настраиваются — всё это живёт на стороне ПК;
* под капотом драйвер сам прозрачно создаёт / переиспользует измерения ``CALC<c>:PAR:DEF:EXT``
  (и минимально привязывает их к окну через ``DISP:WIND:TRAC:FEED``, чтобы PNA гарантированно
  выполнял их развёртку) и удаляет созданные собой измерения, когда параметр выключен — чтобы
  прибор не тратил время на лишние проходы источников по неиспользуемым портам;
* число портов (2 или 4) определяется при подключении по ``SYST:CAP:HARD:PORT:COUN?`` (с откатом
  на ``model.ports``): поддерживаются все ``S11..S44``, попортовая мощность ``source.power_p1..p4``,
  CW-генератор на любой порт ``1..ports``, Touchstone ``snp_read`` (1..4 порта) и калибровка 1..4 портов.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Iterable

from .base import VISAInstrument
from ..core.params import Action, ConnectionState, Kind, Param
from ..core.trace import Trace
from ..core.units import parse_number
from ..profiles import NETWORK_ANALYZER
from ..transport import TransportError, TransportTimeout

__all__ = ["KeysightPNA", "PNA_MODELS", "PNAModel", "ALL_SPARAMS", "MAX_PORTS"]

F, I, B, E, S = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM, Kind.STR

MAX_PORTS = 4
ALL_SPARAMS: tuple[str, ...] = tuple(
    f"S{r}{c}" for r in range(1, MAX_PORTS + 1) for c in range(1, MAX_PORTS + 1)
)
_SPARAM_RE = re.compile(r"^S(\d)(\d)$")
_SPARAM_KEY_RE = re.compile(r"^s(\d)(\d)\.(enabled|trace)$")
_PORT_POW_RE = re.compile(r"^source\.power_p(\d)$")


@dataclass(frozen=True)
class PNAModel:
    name: str
    f_min: float
    f_max: float
    max_points: int = 32001
    ports: int = 2


PNA_MODELS = {
    "N5230C-420": PNAModel("N5230C-420", 300e3, 20e9, ports=4),
    "N5231A": PNAModel("N5231A", 300e3, 13.5e9, ports=2),
    "N5232A": PNAModel("N5232A", 300e3, 20e9, ports=2),
    "N5222A": PNAModel("N5222A", 10e6, 26.5e9, ports=2),
    "N5224A": PNAModel("N5224A", 10e6, 43.5e9, ports=4),
    "N5234A": PNAModel("N5234A", 10e6, 43.5e9, ports=4),
    "N5242A": PNAModel("N5242A", 10e6, 26.5e9, ports=4),
}

FREQ_KEYS = ("freq.start", "freq.stop", "freq.center", "freq.span")
PORT_POWER_KEYS = tuple(f"source.power_p{p}" for p in range(1, MAX_PORTS + 1))
SPARAM_ENABLED_KEYS = tuple(f"{sp.lower()}.enabled" for sp in ALL_SPARAMS)
SPARAM_TRACE_KEYS = tuple(f"{sp.lower()}.trace" for sp in ALL_SPARAMS)

#: ключи стимула и канала ``stim.channel`` (в SCPI ``SENS1:`` / ``SOUR1:`` -> номер канала)
STIM_KEYS = (
    *FREQ_KEYS, "freq.cw", "sweep.type", "sweep.points", "sweep.time", "sweep.time_auto", "bw.if",
    "avg.state", "avg.count", "cal.state", "cal.set", "cal.interp",
    "source.power", "source.power_coupled", *PORT_POWER_KEYS, "trig.mode",
)
#: ключи, запись которых меняет ось X стимула канала
X_AXIS_KEYS = (*FREQ_KEYS, "freq.cw", "sweep.type", "sweep.points", "sweep.time", "sweep.time_auto",
               "cal.set", "gen.state", "stim.channel")
#: что режим генератора запоминает и возвращает (в этом порядке)
GEN_KEYS = ("sweep.type", "freq.start", "freq.stop", "sweep.points", "bw.if", "sweep.time_auto", "sweep.time",
            "avg.state", "avg.count", "cal.state", "source.power", "rf.output")

SWEEP_TYPES = ("LIN", "LOG", "POWER", "CW", "SEGMENT")
SWEEP_MAP = (("LIN", "LIN"), ("LIN", "LINEAR"), ("LOG", "LOG"), ("LOG", "LOGARITHMIC"),
             ("POWER", "POW"), ("POWER", "POWER"), ("CW", "CW"), ("SEGMENT", "SEGM"), ("SEGMENT", "SEGMENT"))
TRIG_SOURCES = ("IMM", "EXT", "MAN")
TRIG_SOURCE_MAP = (("IMM", "IMM"), ("IMM", "IMMEDIATE"), ("IMM", "INT"), ("IMM", "INTERNAL"),
                   ("EXT", "EXT"), ("EXT", "EXTERNAL"),
                   ("MAN", "MAN"), ("MAN", "MANUAL"))
TRIG_MODES = ("CHANNEL", "SWEEP", "POINT", "TRACE")
TRIG_MODE_MAP = (("CHANNEL", "CHAN"), ("SWEEP", "SWE"), ("POINT", "POIN"), ("TRACE", "TRAC"))
STATE_EXT = ".csa"
_STATE_NAME = re.compile(r"[A-Za-z0-9_\-]{1,32}")
#: бит 4 регистра STATus:OPERation (Measuring / Sweeping: переход 1 -> 0 по NTR = развёртка завершена)
OPER_SWEEP_BIT = 16


def _sparam_ports(sp: str) -> tuple[int, int]:
    m = _SPARAM_RE.match(sp)
    if not m:
        raise ValueError(f"некорректный S-параметр: {sp!r} (ожидается S11..S44)")
    return int(m.group(1)), int(m.group(2))


def _canonical_sparams(items: Iterable[str]) -> list[str]:
    wanted = {s.strip().upper() for s in items if s and s.strip()}
    for sp in wanted:
        _sparam_ports(sp)
    return [sp for sp in ALL_SPARAMS if sp in wanted]


class KeysightPNA(VISAInstrument):
    NAME = "Keysight PNA"
    PROFILE = NETWORK_ANALYZER
    POLL_INTERVAL_MS = 200
    TIMEOUT_MS = 10000

    _POWER_RAW_MIN = -90.0
    _POWER_RAW_MAX = 20.0

    def __init__(self, transport, model: PNAModel | None = None, parent=None,
                 command_delay_s: float | None = None, read_pause_s: float | None = None):
        self.model = model or PNA_MODELS["N5230C-420"]
        self._ports: int = self.model.ports
        self._binary: bool = True
        self._continuous: bool = True
        self._armed: bool = False
        self._oper_seen: bool = False
        self._oper_supported: bool = True
        self._x_query_mode: str = "sens"          # "sens" -> SENS#:X? | "calc" -> CALC#:X? | "computed"
        self._x_cache: dict[int, tuple[tuple[float, ...], str, bool]] = {}
        self._last_cont_read: float = 0.0
        self._cal_stim: bool = True
        #: идущая калибровка: {"channel", "steps": [описание], "done": {шаг}, "busy": шаг | None, "kind": ...}
        self._cal: dict | None = None
        self._sweep: dict[str, Any] = {}          # последняя завершённая развёртка: s{r}{c}.trace, trace, meas.count
        self._active_sparams: set[str] = set()    # активные S-параметры для чтения на канале _stim_ch
        self._owned_meas: set[tuple[int, str]] = set()   # измерения, созданные самим драйвером (VL_CH...)
        self._armed_channels: list[int] = [1]
        self._stim_ch: int = 1
        self._gen_port: int = 1
        self._power_offset: float = 0.0
        self._power_offset_state: bool = False
        #: режим генератора: (канал, снимок GEN_KEYS, {канал: SWE:MODE}, {pow_coup, pow_modes, pow_levels, trig_source})
        self._gen: tuple[int, dict[str, Any], dict[int, str], dict[str, Any]] | None = None
        super().__init__(transport, parent, command_delay_s, read_pause_s)

    @property
    def ports(self) -> int:
        """Число физических портов подключённого ВАЦ (2 или 4)."""
        return self._ports

    # ------------------------------------------------------------ описание
    def build_params(self):
        m = self.model
        st, rs, sw, tg = "Анализ/Стимул", "Анализ/Измерение", "Анализ/Развёртка", "Анализ/Запуск"
        freq = dict(kind=F, unit="Hz", minimum=m.f_min, maximum=m.f_max, tolerance=1e-9,
                    also_read=FREQ_KEYS)
        sparam_params = []
        for sp in ALL_SPARAMS:
            lo = sp.lower()
            sparam_params += [
                Param(f"{lo}.enabled", f"Читать {sp}", B, section=rs, query="<virtual>", write="<virtual>",
                      also_read=("s.active",)),
                Param(f"{lo}.trace", f"Трасса {sp}", Kind.TRACE, "", section=rs, group="SWEEP"),
            ]
        port_pow_params = [
            Param(f"source.power_p{p}", f"Мощность порта {p}", F, "dBm", section=st,
                  query=f"SOUR1:POW{p}?", write=f"SOUR1:POW{p} {{value}}",
                  minimum=self._POWER_RAW_MIN, maximum=self._POWER_RAW_MAX, tolerance=1e-3,
                  also_read=("source.power", *PORT_POWER_KEYS))
            for p in range(1, MAX_PORTS + 1)
        ]
        return sparam_params + port_pow_params + [
            Param("ports", "Портов", I, section=rs, query="<virtual>"),
            Param("s.active", "Активные S-параметры", S, section=rs, query="<virtual>", write="<virtual>",
                  also_read=SPARAM_ENABLED_KEYS),
            Param("trace", "Трасса", Kind.TRACE, "", section="Трасса/Трасса", group="SWEEP"),
            Param("meas.count", "Измерений", I, section=rs, group="SWEEP"),
            Param("meas.armed", "Идёт развёртка", B, section=sw, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=sw, query="<virtual>", write="<virtual>"),
            Param("stim.channel", "Канал стимула", I, section=st, query="<virtual>", write="<virtual>",
                  minimum=1, maximum=200, also_read=STIM_KEYS),
            Param("state.dir", "Папка состояний на анализаторе", S, section="Память",
                  query="MMEM:CDIR?", write='MMEM:CDIR "{value}"'),
            Param("gen.state", "Режим генератора", B, section=st, query="<virtual>", write="<virtual>",
                  also_read=(*STIM_KEYS, "rf.output", "gen.channel", "gen.port", "stim.channel", "trig.source")),
            Param("gen.port", "Порт генератора", I, section=st, query="<virtual>", write="<virtual>",
                  minimum=1, maximum=MAX_PORTS, also_read=("source.power",)),
            Param("gen.channel", "Канал генератора", I, section=st, query="<virtual>"),
            Param("cal.state", "Коррекция", B, section=rs, query="SENS1:CORR:STAT?",
                  write="SENS1:CORR:STAT {value}"),
            Param("cal.set", "Cal set канала", S, section=rs, query="<virtual>", write="<virtual>",
                  also_read=STIM_KEYS),
            Param("cal.stim", "Cal set со стимулом", B, section=rs, query="<virtual>", write="<virtual>"),
            Param("cal.busy", "Идёт измерение стандарта", B, section=rs, query="<virtual>"),
            Param("cal.interp", "Интерполяция калибровки", B, section=rs, query="SENS1:CORR:INT?",
                  write="SENS1:CORR:INT {value}", also_read=("cal.state",)),
            Param("power.offset", "Поправка мощности", F, "dB", section=st, query="<virtual>",
                  write="<virtual>", minimum=-100.0, maximum=100.0, tolerance=1e-3,
                  also_read=("source.power",)),
            Param("power.offset_state", "Калибровка мощности", B, section=st, query="<virtual>",
                  write="<virtual>", also_read=("source.power",)),
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
                  write="SOUR1:POW1 {value}", minimum=-190.0, maximum=120.0, tolerance=1e-3,
                  also_read=PORT_POWER_KEYS),
            Param("source.power_coupled", "Связать мощность портов", B, section=st,
                  query="SOUR1:POW:COUP?", write="SOUR1:POW:COUP {value}",
                  also_read=("source.power", *PORT_POWER_KEYS)),
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
                  choices=TRIG_SOURCES, labels=("Внутренний", "Внешний", "Ручной"), enum_map=TRIG_SOURCE_MAP),
            Param("trig.scope", "Область запуска", E, section=tg, query="TRIG:SCOP?", write="TRIG:SCOP {value}",
                  choices=("ALL", "CURRENT"), labels=("Все каналы", "Текущий канал"),
                  enum_map=(("ALL", "ALL"), ("ALL", "GLOB"), ("ALL", "GLOBAL"),
                            ("CURRENT", "CURR"), ("CURRENT", "ACT"), ("CURRENT", "ACTIVE"), ("CURRENT", "CHAN"))),
            Param("trig.mode", "Режим запуска канала", E, section=tg, query="SENS1:SWE:TRIG:MODE?",
                  write="SENS1:SWE:TRIG:MODE {value}", choices=TRIG_MODES,
                  labels=("Канал", "Развёртка", "Точка", "Трасса"), enum_map=TRIG_MODE_MAP),
            Param("trig.slope", "Фронт внешнего запуска", E, section=tg, query="TRIG:SLOP?",
                  write="TRIG:SLOP {value}", choices=("POS", "NEG"),
                  enum_map=(("POS", "POS"), ("POS", "POSITIVE"), ("NEG", "NEG"), ("NEG", "NEGATIVE"))),
            Param("trig.type", "Тип внешнего запуска", E, section=tg, query="TRIG:TYPE?", write="TRIG:TYPE {value}",
                  choices=("EDGE", "LEVEL"), labels=("Фронт", "Уровень"),
                  enum_map=(("EDGE", "EDGE"), ("LEVEL", "LEV"), ("LEVEL", "LEVEL"))),
            Param("trig.delay", "Задержка запуска", F, "s", section=tg, query="TRIG:DEL?", write="TRIG:DEL {value}",
                  minimum=0.0, maximum=3.0, tolerance=1e-9),
        ]

    def build_actions(self):
        return [
            Action("single", "Одна развёртка"),
            Action("abort", "Стоп"),
            Action("trigger", "Ручной запуск (TRIG:SOUR MAN)"),
            Action("sparam_set", "Задать активные S-параметры"),
            Action("sparam_all", "Читать все S-параметры"),
            Action("sparam_none", "Не читать S-параметры"),
            Action("snp_read", "S-параметры канала (Touchstone)"),
            Action("state_list", "Состояния в памяти"),
            Action("state_dirs", "Папки на диске анализатора"),
            Action("state_save", "Сохранить состояние"),
            Action("state_recall", "Вызвать состояние"),
            Action("state_delete", "Удалить состояние", confirm="Удалить состояние из памяти анализатора?"),
            Action("cal_list", "Cal set'ы в памяти прибора"),
            Action("cal_delete", "Удалить cal set", confirm="Удалить cal set из памяти анализатора?"),
            Action("cal_conns", "Разъёмы для калибровки"),
            Action("cal_kits", "Наборы для разъёма"),
            Action("cal_start", "Начать калибровку"),
            Action("cal_start_thru", "Калибровка по перемычке (response THRU)"),
            Action("cal_measure", "Измерить стандарт"),
            Action("cal_status", "Шаги калибровки"),
            Action("cal_save", "Сохранить калибровку в cal set"),
            Action("cal_abort", "Прервать калибровку"),
            Action("reset", "Preset *RST",
                   confirm="Preset удалит измерения и выключит калибровку. Продолжить?"),
        ]

    # --------------------------------------------------------------- хуки
    def _drain_probe_errors(self) -> list[tuple[int, str]]:
        """Снять ошибки прибора после пробных запросов совместимости без зажигания индикатора ошибок."""
        try:
            self.read_esr()
            return self.read_error_queue()
        except Exception:  # noqa: BLE001
            return []

    def _detect_ports(self) -> int:
        old_timeout = self.transport.timeout_ms
        self.transport.timeout_ms = min(old_timeout, 1500)
        try:
            raw = self.query("SYST:CAP:HARD:PORT:COUN?")
            n = int(round(parse_number(raw)))
            if 1 <= n <= MAX_PORTS:
                return n
        except TransportTimeout:
            if not self._try_resync():
                raise
            self._drain_probe_errors()
        except Exception:  # noqa: BLE001
            self._drain_probe_errors()
        finally:
            self.transport.timeout_ms = old_timeout
        return self.model.ports

    def _configure_data_format(self) -> None:
        """Включить двоичный формат IEEE 488.2 REAL,64 (big-endian); при отказе откатиться на ASCII."""
        self._binary = True
        try:
            self.write("FORM:BORD NORM")
            self.write("FORM:DATA REAL,64")
            errors = self._drain_probe_errors()
            if errors:
                self._binary = False
                self.write("FORM:DATA ASCII,0")
                self._drain_probe_errors()
        except Exception:  # noqa: BLE001
            self._binary = False
            try:
                self.write("FORM:DATA ASCII,0")
            except Exception:  # noqa: BLE001
                pass

    def _query_data_block(self, command: str) -> list[float]:
        """Запросить массив чисел (SDATA / X / SNP) в двоичном REAL,64 или ASCII."""
        if self._binary:
            return self.query_binary_floats(command, datatype="d", big_endian=True)
        raw = self.query(command)
        return [parse_number(t) for t in raw.split(",") if t.strip()]

    def on_connected(self) -> None:
        self._armed = False
        self._cal = None
        self._stim_ch = 1
        self._gen_port = 1
        self._gen = None
        self._sweep = {}
        self._x_cache = {}
        self._x_query_mode = "sens"
        self._owned_meas = set()
        self._ports = self._detect_ports()
        self._configure_data_format()

        cat = self._catalog(1)
        valid_in_cat = [sp for sp in cat.values() if self._is_valid_sparam(sp)]
        if not valid_in_cat:
            self._ensure_sparam_meas(1, "S11")
            self._active_sparams = {"S11"}
        else:
            # по умолчанию включаем первый существующий S-параметр канала 1 (не удаляя чужие измерения)
            self._active_sparams = {valid_in_cat[0]}

        self._oper_supported = True
        self._setup_oper_sync()
        self._continuous = True
        self._ensure_continuous()
        self._oper_seen = False
        self._last_cont_read = time.monotonic()

    def on_disconnecting(self) -> None:
        self._cal = None
        if self._armed and not self._continuous:
            self._abort()
        if self._gen is not None:
            gen, self._gen = self._gen, None
            try:
                self._gen_restore(*gen)
            except Exception:  # noqa: BLE001
                pass

    def on_closed(self) -> None:
        self._armed = False
        self._cal = None
        self._gen = None
        self._sweep = {}
        self._x_cache = {}
        self._active_sparams = set()
        self._owned_meas = set()
        self._power_offset = 0.0
        self._power_offset_state = False

    def _active_power_offset(self) -> float:
        return self._power_offset if self._power_offset_state else 0.0

    def _is_valid_sparam(self, sp: str) -> bool:
        m = _SPARAM_RE.match(sp.upper())
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        return 1 <= a <= self._ports and 1 <= b <= self._ports

    def _check_sparam(self, sp: str) -> str:
        sp = str(sp).strip().upper()
        a, b = _sparam_ports(sp)
        if a > self._ports or b > self._ports:
            raise ValueError(f"{sp}: прибор имеет {self._ports} порта (доступны S11..S{self._ports}{self._ports})")
        return sp

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

    def _active_channels(self) -> list[int]:
        if self._gen is not None:
            return [self._gen[0]]
        return [self._stim_ch]

    # ------------------------------------------------- прозрачное управление измерениями PNA
    def _ensure_sparam_meas(self, channel: int, sparam: str, cat: dict[str, str] | None = None) -> str:
        """Найти или создать измерение ``sparam`` на канале ``channel`` (с тихой привязкой к окну,
        чтобы PNA гарантированно его свипировал)."""
        cat = self._catalog(channel) if cat is None else cat
        owned_name = f"VL_CH{channel}_{sparam}"
        if cat.get(owned_name) == sparam:
            return owned_name
        for name, sp in cat.items():
            if sp == sparam:
                return name
        name = owned_name
        k = 1
        while name in cat:
            k += 1
            name = f"VL_CH{channel}_{sparam}_{k}"
        self.write(f"CALC{channel}:PAR:DEF:EXT '{name}','{sparam}'")
        self._feed_measurement(channel, name)
        self._owned_meas.add((channel, name))
        return name

    def _feed_measurement(self, channel: int, name: str) -> None:
        """Привязать измерение к окну прибора (без чтения/изменения шкал, маркеров и формата),
        чтобы прошивка PNA включала его в развёртку канала."""
        win = channel
        try:
            if self.query(f"DISP:WIND{win}:STAT?").strip().upper() not in ("1", "ON"):
                self.write(f"DISP:WIND{win}:STAT ON")
            used = [int(t) for t in re.findall(r"\d+", self.query(f"DISP:WIND{win}:CAT?"))]
            tnum = max(used, default=0) + 1
            self.write(f"DISP:WIND{win}:TRAC{tnum}:FEED '{name}'")
        except Exception:  # noqa: BLE001 - если окно channel недоступно, пробуем окно 1
            self._drain_probe_errors()
            try:
                if self.query("DISP:WIND1:STAT?").strip().upper() not in ("1", "ON"):
                    self.write("DISP:WIND1:STAT ON")
                used = [int(t) for t in re.findall(r"\d+", self.query("DISP:WIND1:CAT?"))]
                self.write(f"DISP:WIND1:TRAC{max(used, default=0) + 1}:FEED '{name}'")
            except Exception:  # noqa: BLE001
                self._drain_probe_errors()

    def _release_sparam_meas(self, channel: int, sparam: str) -> None:
        """Удалить созданное драйвером измерение выключенного S-параметра (если на канале остаётся
        хотя бы одно другое измерение), чтобы PNA не гонял лишние проходы источников."""
        cat = self._catalog(channel)
        if len(cat) <= 1:
            return
        for name, sp in list(cat.items()):
            if sp == sparam and (channel, name) in self._owned_meas and len(cat) > 1:
                self.write(f"CALC{channel}:PAR:DEL '{name}'")
                self._owned_meas.discard((channel, name))
                cat.pop(name, None)

    def _active_sparams_list(self) -> list[str]:
        return [sp for sp in ALL_SPARAMS if sp in self._active_sparams]

    def _set_active_sparams(self, sparams: Iterable[str]) -> list[str]:
        wanted = [self._check_sparam(sp) for sp in _canonical_sparams(sparams)]
        wanted_set = set(wanted)
        old_set = set(self._active_sparams)
        added = [sp for sp in wanted if sp not in old_set]
        removed = [sp for sp in ALL_SPARAMS if sp in old_set and sp not in wanted_set]
        c = self._stim_ch
        if added:
            cat = self._catalog(c)
            for sp in added:
                name = self._ensure_sparam_meas(c, sp, cat)
                cat[name] = sp
        self._active_sparams = wanted_set
        for sp in removed:
            self._release_sparam_meas(c, sp)
            key = f"{sp.lower()}.trace"
            if self._sweep.get(key) is not None or self._values.get(key) is not None:
                self._sweep[key] = None
                self._publish(key, None)
        for sp in ALL_SPARAMS:
            en_key = f"{sp.lower()}.enabled"
            en_val = sp in self._active_sparams
            if self._values.get(en_key) is not en_val:
                self._publish(en_key, en_val)
        active_str = ",".join(wanted)
        if self._values.get("s.active") != active_str:
            self._publish("s.active", active_str)
        if not wanted:
            self._sweep["trace"] = None
            self._sweep["meas.count"] = 0
            self._publish("trace", None)
            self._publish("meas.count", 0)
        return wanted

    # ------------------------------------------------------------ валидация и запись
    def validate_param(self, spec: Param, value: Any) -> Any:
        val = super().validate_param(spec, value)
        if spec.key == "source.power":
            off = self._active_power_offset()
            lo, hi = self._POWER_RAW_MIN - off, self._POWER_RAW_MAX - off
            if val < lo - 1e-9:
                raise ValueError(f"{spec.key}: минимум {lo:g} dBm (прибор {self._POWER_RAW_MIN:g} dBm, поправка {off:+.2f} dB)")
            if val > hi + 1e-9:
                raise ValueError(f"{spec.key}: максимум {hi:g} dBm (прибор {self._POWER_RAW_MAX:g} dBm, поправка {off:+.2f} dB)")
        elif spec.key == "gen.port":
            if int(val) > self._ports:
                raise ValueError(f"gen.port: прибор имеет {self._ports} порта (получено {val})")
        elif spec.key == "s.active":
            raw = str(val).strip()
            if raw:
                for tok in re.split(r"[,\s;]+", raw):
                    if tok:
                        self._check_sparam(tok)
        else:
            m_sp = _SPARAM_KEY_RE.match(spec.key)
            if m_sp and m_sp.group(3) == "enabled" and bool(val):
                self._check_sparam(f"S{m_sp.group(1)}{m_sp.group(2)}")
            m_pp = _PORT_POW_RE.match(spec.key)
            if m_pp and int(m_pp.group(1)) > self._ports:
                raise ValueError(f"{spec.key}: прибор имеет {self._ports} порта")
        return val

    def write_param(self, spec: Param, value: Any) -> None:
        key = spec.key
        if key == "meas.continuous":
            on = bool(value)
            self._continuous = on
            if on:
                if self._is_triggered():
                    self._start_sweep()
                else:
                    self._ensure_continuous()
                    self._clear_oper_events()
                self._last_cont_read = time.monotonic()
            else:
                self._abort()
            self._sync_poll_timer()
            return
        if key == "gen.state":
            self._set_gen(bool(value))
            self._x_cache.pop(self._stim_ch, None)
            return
        if key == "gen.port":
            port = int(value)
            if not 1 <= port <= self._ports:
                raise ValueError(f"gen.port: 1..{self._ports}, получено {port}")
            self._gen_port = port
            if self._gen is not None:
                c = self._gen[0]
                for p in range(1, self._ports + 1):
                    if p != port:
                        self.write(f"SOUR{c}:POW{p}:MODE OFF")
                self.write(f"SOUR{c}:POW{port}:MODE ON")
            return
        if key == "cal.stim":
            self._cal_stim = bool(value)
            return
        if key == "cal.set" and self._gen is not None and self._gen[0] == self._stim_ch:
            raise ValueError("режим генератора: сначала выключите CW, потом меняйте cal set")
        if key == "power.offset_state":
            self._power_offset_state = bool(value)
            return
        if key == "power.offset":
            old_off = self._active_power_offset()
            self._power_offset = float(value)
            new_off = self._active_power_offset()
            cur = self._values.get("source.power")
            if self._power_offset_state and abs(new_off - old_off) > 1e-6 and isinstance(cur, (int, float)):
                raw = max(self._POWER_RAW_MIN, min(self._POWER_RAW_MAX, float(cur) + new_off))
                pspec = self._stim_spec(self._params["source.power"])
                was_armed = self._armed
                if not self._continuous:
                    self._abort()
                try:
                    super().write_param(pspec, raw)
                finally:
                    self._resume_after_write(was_armed)
            return
        if key == "stim.channel":
            c = int(value)
            if self._gen is not None and c != self._gen[0]:
                raise ValueError(f"режим генератора на канале {self._gen[0]}: сначала выключите CW")
            if c not in self._channels():
                raise ValueError(f"нет канала {c} (каналы: {self._channels()})")
            self._stim_ch = c
            self._x_cache.pop(c, None)
            return

        # управление активными S-параметрами (s.active и s{r}{c}.enabled)
        m_sp = _SPARAM_KEY_RE.match(key)
        if key == "s.active" or (m_sp and m_sp.group(3) == "enabled"):
            was_armed = self._armed
            if not self._continuous:
                self._abort()
            added_any = False
            try:
                if key == "s.active":
                    toks = [t for t in re.split(r"[,\s;]+", str(value).strip()) if t]
                    before = set(self._active_sparams)
                    self._set_active_sparams(toks)
                    added_any = bool(self._active_sparams - before)
                else:
                    sp = f"S{m_sp.group(1)}{m_sp.group(2)}"
                    wanted = set(self._active_sparams)
                    if bool(value):
                        wanted.add(sp)
                        added_any = sp not in self._active_sparams
                    else:
                        wanted.discard(sp)
                    self._set_active_sparams(wanted)
            finally:
                self._resume_after_write(was_armed, immediate_read=added_any)
            return

        spec = self._stim_spec(spec)
        was_armed = self._armed
        if not self._continuous:
            self._abort()
        try:
            if spec.key == "cal.set":
                self._cal_apply(str(value or "").strip())
            elif spec.key == "source.power":
                super().write_param(spec, float(value) + self._active_power_offset())
            else:
                super().write_param(spec, value)
        finally:
            if spec.key in X_AXIS_KEYS:
                self._x_cache.pop(self._stim_ch, None)
            triggered = (value in ("EXT", "MAN")) if spec.key == "trig.source" else None
            self._resume_after_write(was_armed, triggered_override=triggered)

    def _resume_after_write(self, was_armed: bool, *, immediate_read: bool = False,
                            triggered_override: bool | None = None) -> None:
        if self._continuous:
            if self._cal is None:
                triggered = self._is_triggered() if triggered_override is None else triggered_override
                if triggered:
                    self._start_sweep()
                else:
                    self._ensure_continuous()
                    self._clear_oper_events()
                self._last_cont_read = 0.0 if immediate_read else time.monotonic()
        elif was_armed:
            self._start_sweep()

    def after_write(self, spec: Param, value: Any) -> None:
        if spec.key in X_AXIS_KEYS:
            self._x_cache.pop(self._stim_ch, None)
        if spec.key in ("trig.source", "gen.state"):
            self._clear_oper_events()
            if self._continuous and self._cal is None:
                if self._is_triggered():
                    if not self._opc_pending:
                        self._start_sweep()
                else:
                    self._opc_pending = False
                    self.opc_latched = False
                    self._ensure_continuous()
            self._sync_poll_timer()

    def read_param_raw(self, spec: Param) -> Any:
        key = spec.key
        if key == "ports":
            return self._ports
        if key == "s.active":
            return ",".join(self._active_sparams_list())
        m_sp = _SPARAM_KEY_RE.match(key)
        if m_sp and m_sp.group(3) == "enabled":
            return f"S{m_sp.group(1)}{m_sp.group(2)}" in self._active_sparams
        m_pp = _PORT_POW_RE.match(key)
        if m_pp and int(m_pp.group(1)) > self._ports:
            return None
        if key == "meas.continuous":
            return self._continuous
        if key == "meas.armed":
            return self._armed
        if key == "stim.channel":
            return self._stim_ch
        if key == "gen.state":
            return self._gen is not None
        if key == "gen.port":
            return self._gen_port
        if key == "gen.channel":
            return self._gen[0] if self._gen is not None else None
        if key == "cal.stim":
            return self._cal_stim
        if key == "cal.busy":
            return self._cal is not None and self._cal["busy"] is not None
        if key == "cal.set":
            name = self.query(f"SENS{self._stim_ch}:CORR:CSET:ACT? NAME").strip().strip('"').strip()
            return "" if not name or name.upper().startswith("NO ") else name
        if key == "power.offset":
            return self._power_offset
        if key == "power.offset_state":
            return self._power_offset_state
        if key in STIM_KEYS:
            val = super().read_param_raw(self._stim_spec(spec))
            if key == "source.power" and isinstance(val, (int, float)):
                return float(val) - self._active_power_offset()
            return val
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        return dict(self._sweep)

    def _safe_read_trace(self) -> None:
        try:
            self._read_trace()
        except TransportTimeout:
            self._try_resync()
            self._drain_probe_errors()
        except Exception:  # noqa: BLE001
            pass

    def _sync_all(self) -> None:
        self._x_cache.clear()
        self._read_keys(list(self._params))
        if self._cal is None and not self._is_triggered() and self._active_sparams:
            self._safe_read_trace()
        if self._continuous:
            if self._is_triggered():
                self._start_sweep()
            else:
                self._ensure_continuous()
                self._clear_oper_events()
            self._last_cont_read = time.monotonic() if self._sweep.get("meas.count") else 0.0
        self._sync_poll_timer()
        self._check_errors(context="sync")
        self.sync_done.emit()

    # ------------------------------------------------------- развёртки
    def _set_armed(self, armed: bool) -> None:
        if armed != self._armed or "meas.armed" not in self._values:
            self._armed = armed
            self._publish("meas.armed", armed)

    def _setup_oper_sync(self) -> None:
        try:
            self.write("STAT:OPER:PTR 0")
            self.write(f"STAT:OPER:NTR {OPER_SWEEP_BIT}")
            self._clear_oper_events()
        except Exception:  # noqa: BLE001
            self._oper_supported = False

    def _clear_oper_events(self) -> None:
        if not self._oper_supported:
            return
        try:
            self.query("STAT:OPER:EVEN?")
        except TransportTimeout:
            self._try_resync()
            self._drain_probe_errors()
            self._oper_supported = False
        except Exception:  # noqa: BLE001
            self._oper_supported = False

    def _ensure_continuous(self) -> None:
        if self._cal is not None:
            return
        channels = self._active_channels()
        self._armed_channels = channels
        for c in channels:
            mode = self.query(f"SENS{c}:SWE:MODE?").strip().upper()
            if not mode.startswith("CONT"):
                self.write(f"SENS{c}:SWE:MODE CONT")
        self._set_armed(True)

    def _start_sweep(self) -> None:
        if self._cal is not None:
            return
        self._armed_channels = self._active_channels()
        use_group = (
            not self._continuous
            and self._values.get("avg.state") is True
            and isinstance(self._values.get("avg.count"), int)
            and self._values["avg.count"] > 1
        )
        cmds: list[str] = []
        for c in self._armed_channels:
            if use_group:
                self.write(f"SENS{c}:SWE:GRO:COUN {int(self._values['avg.count'])}")
                cmds.append(f"SENS{c}:SWE:MODE GRO")
            else:
                cmds.append(f"SENS{c}:SWE:MODE SING")
        self.arm_opc(*cmds)
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            for c in self._armed_channels:
                self.write(f"SENS{c}:SWE:MODE HOLD")
            self._set_armed(False)
        self._opc_pending = False
        self.opc_latched = False

    def _stim_spec(self, spec: Param, channel: int | None = None) -> Param:
        c = self._stim_ch if channel is None else channel
        if spec.key not in STIM_KEYS:
            return spec
        q, w = spec.query, spec.write
        if c != 1:
            q = q and q.replace("SENS1:", f"SENS{c}:").replace("SOUR1:", f"SOUR{c}:")
            w = w and w.replace("SENS1:", f"SENS{c}:").replace("SOUR1:", f"SOUR{c}:")
        if spec.key == "source.power" and self._gen is not None and self._gen_port != 1:
            q = q and q.replace(":POW1?", f":POW{self._gen_port}?")
            w = w and w.replace(":POW1 ", f":POW{self._gen_port} ")
        return replace(spec, query=q, write=w)

    # ------------------------------------------------------- режим генератора (1..4 порта)
    def _set_gen(self, on: bool) -> None:
        if on == (self._gen is not None):
            return
        was_armed = self._armed
        self._abort()
        try:
            if on:
                c = self._stim_ch
                port = min(self._gen_port, self._ports)
                self._gen_port = port
                snap = {k: super(KeysightPNA, self).read_param_raw(self._stim_spec(self._params[k], c))
                        for k in GEN_KEYS}
                modes = {ch: self.query(f"SENS{ch}:SWE:MODE?").strip().upper() for ch in self._channels()}
                extra = {
                    "pow_coup": self.query(f"SOUR{c}:POW:COUP?").strip(),
                    "pow_modes": {p: self.query(f"SOUR{c}:POW{p}:MODE?").strip().upper()
                                  for p in range(1, self._ports + 1)},
                    "pow_levels": {p: parse_number(self.query(f"SOUR{c}:POW{p}?"))
                                   for p in range(1, self._ports + 1)},
                    "trig_source": self._params["trig.source"].from_scpi(self.query("TRIG:SOUR?")),
                }
                gen_tuple = (c, snap, modes, extra)
                try:
                    for ch, mode in modes.items():
                        if ch != c and mode != "HOLD":
                            self.write(f"SENS{ch}:SWE:MODE HOLD")
                    if extra["trig_source"] != "IMM":
                        self.write("TRIG:SOUR IMM")
                    if snap.get("cal.state"):
                        self.write(f"SENS{c}:CORR:STAT 0")
                    self.write(f"SENS{c}:SWE:TYPE CW")
                    self.write(f"SOUR{c}:POW:COUP 0")
                    for p in range(1, self._ports + 1):
                        if p != port:
                            self.write(f"SOUR{c}:POW{p}:MODE OFF")
                    self.write(f"SOUR{c}:POW{port}:MODE ON")
                except Exception:
                    try:
                        self._gen_restore(*gen_tuple)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                self._gen = gen_tuple
            else:
                gen, self._gen = self._gen, None
                self._gen_restore(*gen)
        finally:
            self._x_cache.pop(self._stim_ch, None)
            if self._continuous:
                self._ensure_continuous()
                self._clear_oper_events()
                self._last_cont_read = time.monotonic()
            elif was_armed:
                self._start_sweep()

    def _gen_restore(self, channel: int, snap: dict[str, Any], modes: dict[int, str],
                     extra: dict[str, Any] | None = None) -> None:
        first: Exception | None = None
        for k in GEN_KEYS:
            v = snap.get(k)
            if v is None or (k == "sweep.time" and snap.get("sweep.time_auto")):
                continue
            try:
                super().write_param(self._stim_spec(self._params[k], channel), v)
            except Exception as e:  # noqa: BLE001
                first = first or e
        if extra:
            for p, lvl in (extra.get("pow_levels") or {}).items():
                if p == 1:
                    continue
                try:
                    self.write(f"SOUR{channel}:POW{p} {lvl}")
                except Exception as e:  # noqa: BLE001
                    first = first or e
            if "pow_coup" in extra:
                try:
                    self.write(f"SOUR{channel}:POW:COUP {extra['pow_coup']}")
                except Exception as e:  # noqa: BLE001
                    first = first or e
            for p, pm in (extra.get("pow_modes") or {}).items():
                try:
                    self.write(f"SOUR{channel}:POW{p}:MODE {pm}")
                except Exception as e:  # noqa: BLE001
                    first = first or e
            if "trig_source" in extra:
                try:
                    ts = self._params["trig.source"].to_scpi(extra["trig_source"])
                    self.write(f"TRIG:SOUR {ts}")
                except Exception as e:  # noqa: BLE001
                    first = first or e
        existing = set(self._channels())
        for ch, mode in modes.items():
            if ch not in existing:
                continue
            mode = "HOLD" if mode.startswith(("SING", "GRO")) else mode
            try:
                self.write(f"SENS{ch}:SWE:MODE {mode}")
            except Exception as e:  # noqa: BLE001
                first = first or e
        if first is not None:
            raise first

    # ------------------------------------------------------- точная ось X и чтение SDATA
    def _sweep_type_and_unit(self, channel: int) -> tuple[str, str, bool]:
        """(тип развёртки, единица оси X, равномерная ли ось)."""
        typ = self._values.get("sweep.type") if channel == self._stim_ch else None
        if not typ:
            raw = self.query(f"SENS{channel}:SWE:TYPE?")
            typ = self._params["sweep.type"].from_scpi(raw)
        if typ == "CW":
            return typ, "s", True
        if typ == "POWER":
            return typ, "dBm", False
        return typ, "Hz", (typ == "LIN")

    def _read_x_axis(self, channel: int, expected_len: int | None = None) -> tuple[tuple[float, ...], str, bool]:
        """Точная ось X канала через ``SENS<c>:X?`` (с кэшированием и откатом на ``CALC<c>:X?`` / расчёт)."""
        cached = self._x_cache.get(channel)
        if cached is not None and (expected_len is None or len(cached[0]) == expected_len):
            return cached
        typ, x_unit, uniform = self._sweep_type_and_unit(channel)
        xs: list[float] | None = None
        for mode, cmd in (("sens", f"SENS{channel}:X?"), ("calc", f"CALC{channel}:X?")):
            if self._x_query_mode == "computed":
                break
            if self._x_query_mode == "calc" and mode == "sens":
                continue
            try:
                vals = self._query_data_block(cmd)
                if vals and (expected_len is None or len(vals) == expected_len):
                    xs = vals
                    self._x_query_mode = mode
                    break
            except TransportTimeout:
                if not self._try_resync():
                    raise
                self._drain_probe_errors()
                self._x_query_mode = "calc" if mode == "sens" else "computed"
            except (TransportError, ValueError):
                self._drain_probe_errors()
                self._x_query_mode = "calc" if mode == "sens" else "computed"
        if xs is None:
            n = expected_len or int(self._values.get("sweep.points") or self.query_int(f"SENS{channel}:SWE:POIN?"))
            if typ == "CW":
                swt = self.query_float(f"SENS{channel}:SWE:TIME?")
                xs = [swt * i / (n - 1) if n > 1 else 0.0 for i in range(n)]
            else:
                a = self.query_float(f"SENS{channel}:FREQ:STAR?")
                b = self.query_float(f"SENS{channel}:FREQ:STOP?")
                if typ == "LOG" and a > 0 and b > 0 and n > 1:
                    ratio = b / a
                    xs = [a * (ratio ** (i / (n - 1))) for i in range(n)]
                else:
                    xs = [a + (b - a) * i / (n - 1) if n > 1 else a for i in range(n)]
        res = (tuple(float(v) for v in xs), x_unit, uniform)
        self._x_cache[channel] = res
        return res

    def _read_trace(self) -> None:
        """Прочитать комплексные ``SDATA`` всех активных S-параметров канала ``stim.channel``."""
        c = self._stim_ch
        active = self._active_sparams_list()
        if not active:
            if self._sweep.get("meas.count") != 0 or self._sweep.get("trace") is not None:
                for sp in ALL_SPARAMS:
                    k = f"{sp.lower()}.trace"
                    if self._sweep.get(k) is not None or self._values.get(k) is not None:
                        self._sweep[k] = None
                        self._publish(k, None)
                self._sweep["trace"] = None
                self._sweep["meas.count"] = 0
                self._publish("trace", None)
                self._publish("meas.count", 0)
            return

        stim_meta = {
            k: self._values.get(k)
            for k in ("sweep.type", "bw.if", "source.power", "avg.state", "avg.count", "cal.state", "cal.set")
        }
        cat = self._catalog(c)
        out: dict[str, Any] = {}
        first_trace: Trace | None = None
        count = 0

        for sp in active:
            meas_name = self._ensure_sparam_meas(c, sp, cat)
            cat[meas_name] = sp
            self.write(f"CALC{c}:PAR:SEL '{meas_name}'")
            raw = self._query_data_block(f"CALC{c}:DATA? SDATA")
            pairs = len(raw) // 2
            if not pairs:
                continue
            raw = raw[:2 * pairs]
            re_vals = tuple(float(v) for v in raw[0::2])
            im_vals = tuple(float(v) for v in raw[1::2])
            x_vals, x_unit, uniform = self._read_x_axis(c, expected_len=pairs)
            meta = {
                **stim_meta,
                "channel": c,
                "name": meas_name,
                "sparam": sp,
                "format": "SDATA",
            }
            tr = Trace(x_vals, re_vals, x_unit=x_unit, y_unit="", name=sp, im=im_vals,
                       uniform=uniform, meta=meta)
            key = f"{sp.lower()}.trace"
            out[key] = tr
            if first_trace is None:
                first_trace = tr
            count += 1

        # очистить трассы выключенных параметров (публикуя None только если было не None)
        for sp in ALL_SPARAMS:
            key = f"{sp.lower()}.trace"
            if key not in out:
                if self._sweep.get(key) is not None or self._values.get(key) is not None:
                    self._sweep[key] = None
                    self._publish(key, None)

        # публикуем все активные трассы, затем trace и meas.count (последним — триггер sweep_updated)
        for key, tr in out.items():
            self._sweep[key] = tr
            self._publish(key, tr)
        self._sweep["trace"] = first_trace
        self._sweep["meas.count"] = count
        self._publish("trace", first_trace)
        self._publish("meas.count", count)

    def _is_triggered(self) -> bool:
        return self._values.get("trig.source") in ("EXT", "MAN")

    def is_triggered_mode(self) -> bool:
        return self._is_triggered() or not self._continuous

    def after_poll_timeout(self) -> None:
        if self._continuous:
            if self._is_triggered():
                self._start_sweep()
            else:
                self._ensure_continuous()
        elif self._armed:
            self._start_sweep()

    def _cont_fallback_s(self) -> float:
        swt = float(self._values.get("sweep.time") or 0.1)
        return max(min(max(0.3, swt), 1.0), self.read_pause_s)

    def poll(self) -> None:
        if self._cal is not None:
            if self._cal["busy"] is not None and self.opc_done("калибровка"):
                self._cal["done"].add(self._cal["busy"])
                self._cal["busy"] = None
                self._publish("cal.busy", False)
            return
        if self._continuous:
            if self._is_triggered():
                if not self._opc_pending:
                    self._start_sweep()
                    return
                if self.opc_done("развёртка"):
                    self._set_armed(False)
                    self._last_cont_read = time.monotonic()
                    self._read_trace()
                    if self._continuous and self._state is ConnectionState.CONNECTED:
                        self._start_sweep()
                return
            if self._oper_supported:
                try:
                    even = int(parse_number(self.query("STAT:OPER:EVEN?")))
                except ValueError:
                    self._oper_supported = False
                    even = 0
                except TransportTimeout:
                    if not self._try_resync():
                        raise
                    self._drain_probe_errors()
                    self._oper_supported = False
                    even = 0
                if even & OPER_SWEEP_BIT:
                    self._oper_seen = True
                    self._last_cont_read = time.monotonic()
                    self._read_trace()
                    return
            fallback_s = self._cont_fallback_s()
            if time.monotonic() - self._last_cont_read >= fallback_s:
                self._last_cont_read = time.monotonic()
                self._read_trace()
            return
        if self._armed and self.opc_done("развёртка"):
            self._set_armed(False)
            self._read_trace()

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "single":
            self._abort()
            self._continuous = False
            self._publish("meas.continuous", False)
            self._start_sweep()
            self._sync_poll_timer()
            return None
        if name == "abort":
            self._continuous = False
            self._publish("meas.continuous", False)
            self._abort()
            for c in self._active_channels():
                self.write(f"SENS{c}:SWE:MODE HOLD")
            self._sync_poll_timer()
            return None
        if name == "trigger":
            if not self._armed and not self._continuous:
                self._start_sweep()
            elif self._continuous and self._is_triggered() and not self._opc_pending:
                self._start_sweep()
            scope = self._values.get("trig.scope")
            channels = self._active_channels()
            for c in ([channels[0]] if scope == "ALL" else channels):
                self.write(f"INIT{c}:IMM")
            return None
        if name in ("sparam_set", "sparam_all", "sparam_none"):
            was_armed = self._armed
            if not self._continuous:
                self._abort()
            try:
                if name == "sparam_all":
                    wanted = [f"S{r}{c}" for r in range(1, self._ports + 1) for c in range(1, self._ports + 1)]
                elif name == "sparam_none":
                    wanted = []
                else:
                    # принимаем ("S11", "S41") или (["S11", "S41"],) или ("S11,S41",)
                    flat: list[str] = []
                    for a in args:
                        if isinstance(a, (list, tuple, set)):
                            flat.extend(str(x) for x in a)
                        else:
                            flat.extend(t for t in re.split(r"[,\s;]+", str(a).strip()) if t)
                    wanted = flat
                res = self._set_active_sparams(wanted)
                self._check_errors(context=name)
                return res
            finally:
                self._resume_after_write(was_armed, immediate_read=bool(self._active_sparams))
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
            default_ports = ",".join(str(p) for p in range(1, self._ports + 1))
            return self._snp_read(args[0] if args else default_ports)
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

    # ------------------------------------------------- калибровка (cal set)
    def _cal_list(self) -> list[str]:
        names = [e.strip() for e in re.split(r'[",]', self.query("CSET:CAT?")) if e.strip()]
        return sorted(set(n for n in names if not n.upper().startswith("NO ")), key=str.lower)

    def _cal_apply(self, name: str) -> None:
        c = self._stim_ch
        if not name:
            self.write(f"SENS{c}:CORR:CSET:DEAC")
        else:
            if name not in self._cal_list():
                raise ValueError(f"нет cal set {name!r} в памяти анализатора")
            self.write(f'SENS{c}:CORR:CSET:ACT "{name}",{1 if self._cal_stim else 0}')
            self.query("*OPC?")
        self._x_cache.pop(c, None)

    # ------------------------------------------------- S-параметры канала (Touchstone 1..4 порта)
    def _snp_read(self, ports) -> dict[str, Trace]:
        plist = sorted({int(p) for p in re.findall(r"\d+", str(ports))})
        if not plist or any(not (1 <= p <= self._ports) for p in plist):
            raise ValueError(f"порты 1..{self._ports}, получено {ports!r}")
        if self._cal is not None:
            raise ValueError("идёт калибровка")
        if len(plist) == 2:
            p1, p2 = plist
            names = [f"S{p1}{p1}", f"S{p2}{p1}", f"S{p1}{p2}", f"S{p2}{p2}"]
        else:
            names = [f"S{r}{c}" for r in plist for c in plist]
        c = self._stim_ch
        was_armed = self._armed
        self._abort()
        old_timeout = self.transport.timeout_ms
        swt = float(self._values.get("sweep.time") or 0.1)
        self.transport.timeout_ms = max(old_timeout, int(swt * len(plist) * 1000) + 10000)
        try:
            self.write("MMEM:STOR:TRAC:FORM:SNP RI")
            if self.query("TRIG:SOUR?").strip().upper().startswith("IMM"):
                self.write(f"SENS{c}:SWE:MODE SING")
                self.query("*OPC?")
            vals = self._query_data_block(f'CALC{c}:DATA:SNP:PORT? "{",".join(map(str, plist))}"')
            self._check_errors(context="snp_read")
        finally:
            self.transport.timeout_ms = old_timeout
            if self._continuous:
                self._ensure_continuous()
                self._clear_oper_events()
                self._last_cont_read = time.monotonic()
            elif was_armed:
                self._start_sweep()
        n, rest = divmod(len(vals), 1 + 2 * len(names))
        if rest or not n:
            raise ValueError(f"SNP: {len(vals)} чисел не делятся на {1 + 2 * len(names)} столбцов")
        freqs = tuple(float(v) for v in vals[:n])
        meta = {
            "channel": c, "format": "SDATA",
            "cal.set": self._values.get("cal.set") if c == self._stim_ch else None,
            "cal.state": self._values.get("cal.state"),
            "bw.if": self._values.get("bw.if"),
            "source.power": self._values.get("source.power"),
        }
        out: dict[str, Trace] = {}
        for k, sp in enumerate(names):
            re_ = tuple(float(v) for v in vals[n * (1 + 2 * k):n * (2 + 2 * k)])
            im_ = tuple(float(v) for v in vals[n * (2 + 2 * k):n * (3 + 2 * k)])
            out[sp] = Trace(freqs, re_, x_unit="Hz", y_unit="", name=sp, im=im_, meta={**meta, "sparam": sp})
        return out

    # ------------------------------------------------- калибровка (guided и THRU, 1..4 порта)
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
            return sorted(kits, key=lambda k: "ideal" not in k.lower())
        if name == "cal_status":
            return self._cal_status()
        if name == "cal_start":
            if self._gen is not None:
                raise ValueError("режим генератора: сначала выключите CW")
            padded = (list(args) + [""] * (2 * self._ports))[:2 * self._ports]
            ports = [(p, str(padded[2 * (p - 1)]).strip(), str(padded[2 * (p - 1) + 1]).strip())
                     for p in range(1, self._ports + 1)]
            if not any(conn for _p, conn, _k in ports):
                raise ValueError("калибровка: задайте разъём хотя бы одного порта")
            for p, conn, kit in ports:
                if conn and not kit:
                    raise ValueError(f"калибровка: нет набора для порта {p}")
            was_armed = self._armed
            self._abort()
            c = self._stim_ch
            guid = f"SENS{c}:CORR:COLL:GUID"
            try:
                for p, conn, kit in ports:
                    self.write(f'{guid}:CONN:PORT{p} "{conn or "Not used"}"')
                    if conn:
                        self.write(f'{guid}:CKIT:PORT{p} "{kit}"')
                self.write(f"{guid}:INIT")
                errors = self._check_errors(context=name)
                if errors:
                    raise ValueError("; ".join(m for _, m in errors))
                steps = int(parse_number(self.query(f"{guid}:STEP?")))
                if steps <= 0:
                    raise ValueError("калибровка: прибор не вернул шаги")
                self._cal = {"channel": c, "done": set(), "busy": None, "kind": "guided",
                             "steps": [self.query(f"{guid}:DESC? {k}").strip().strip('"') for k in range(1, steps + 1)]}
                return self._cal_status()
            except Exception:
                if self._cal is None:
                    self._resume_after_write(was_armed)
                raise
        if name == "cal_start_thru":
            if self._gen is not None:
                raise ValueError("режим генератора: сначала выключите CW")
            if args and str(args[0]).strip():
                sparam = self._check_sparam(str(args[0]))
            else:
                trans = [sp for sp in self._active_sparams_list() if sp[1] != sp[2]]
                if not trans:
                    raise ValueError("калибровка по перемычке: укажите S-параметр передачи (напр. S21 или S41)")
                sparam = trans[0]
            a, b = _sparam_ports(sparam)
            if a == b:
                raise ValueError(f"калибровка по перемычке - только для передачи Sij (i != j), получено {sparam}")
            was_armed = self._armed
            self._abort()
            c = self._stim_ch
            try:
                meas = self._ensure_sparam_meas(c, sparam)
                self.write(f"CALC{c}:PAR:SEL '{meas}'")
                self.write(f"SENS{c}:CORR:COLL:METH TRAN1")
                errors = self._check_errors(context=name)
                if errors:
                    raise ValueError("; ".join(m for _, m in errors))
                self._cal = {"channel": c, "done": set(), "busy": None, "kind": "thru", "meas": meas,
                             "steps": [f"Connect THRU between port {b} and port {a} ({sparam})"]}
                return self._cal_status()
            except Exception:
                if self._cal is None:
                    self._resume_after_write(was_armed)
                raise
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
            errors = self._check_errors(context=name)
            if errors:
                raise ValueError("; ".join(m for _, m in errors))
            self._cal_end()
            return self._cal_list()
        if name == "cal_abort":
            if self._cal is not None and self._cal["busy"] is not None:
                try:
                    self.write("ABOR")
                    self.write(f"SENS{c}:SWE:MODE HOLD")
                except Exception:  # noqa: BLE001
                    pass
                self._opc_pending = False
                self.opc_latched = False
            self._cal_end()
            return None
        return super().run_custom_action(name, args)

    def _cal_status(self) -> tuple[list[str], list[int]]:
        if self._cal is None:
            return [], []
        return list(self._cal["steps"]), sorted(self._cal["done"])

    def _cal_end(self) -> None:
        c = self._cal["channel"] if self._cal else self._stim_ch
        self._cal = None
        self._publish("cal.busy", False)
        self._x_cache.pop(c, None)
        if c == self._stim_ch:
            self._read_keys(STIM_KEYS)
        if self._continuous:
            self._ensure_continuous()
            self._clear_oper_events()
            self._last_cont_read = time.monotonic()

    # ------------------------------------------------- состояния в памяти прибора
    def _state_dirs(self) -> tuple[str, list[str]]:
        path = self.query("MMEM:CDIR?").strip().strip('"')
        names = [e.strip() for e in re.split(r'[",]', self.query("MMEM:CAT?")) if e.strip()]
        return path, sorted({n for n in names if "." not in n}, key=str.lower)

    def _state_list(self) -> list[str]:
        reply = self.query("MMEM:CAT?")
        names = []
        for entry in re.split(r'[",]', reply):
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
        if name == "state_save":
            old_timeout = self.transport.timeout_ms
            self.transport.timeout_ms = max(old_timeout, 30000)
            try:
                self.write(f"MMEM:STOR {file}")
                self.query("*OPC?")
                self._check_errors(context=name)
            finally:
                self.transport.timeout_ms = old_timeout
            return
        self._abort()
        old_timeout = self.transport.timeout_ms
        self.transport.timeout_ms = max(old_timeout, 30000)
        recalled = False
        try:
            self.write(f"MMEM:LOAD {file}")
            self.query("*OPC?")
            errors = self._check_errors(context=name)
            if errors:
                raise ValueError("; ".join(m for _, m in errors))
            recalled = True
        finally:
            self.transport.timeout_ms = old_timeout
            if not recalled:
                self._resume_after_write(was_armed)
        self.on_connected()
        self._sync_all()
