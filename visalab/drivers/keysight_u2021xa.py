"""Драйвер USB-датчика пиковой и средней мощности Keysight U2021XA (U2020 X-Series).

По "U2020 X-Series USB Peak and Average Power Sensors Programming Guide":
* частота поправки ``FREQ`` (1 кГц ... 1000 ГГц по синтаксису, датчик 50 МГц ... 18 ГГц);
* режим ``DET:FUNC NORM|AVER``: NORMal - пик/среднее/строб/запуск по уровню, AVERage -
  только средняя мощность. ``TRIG:SOUR INT`` при AVER даёт -221 Settings conflict, поэтому
  драйвер сам переводит датчик в NORMal перед выбором запуска по уровню;
* запуск ``TRIG:SOUR BUS|EXT|HOLD|IMM|INT``, ``TRIG:LEV`` (-40...+20 dBm), ``TRIG:LEV:AUTO``,
  ``TRIG:SLOP POS|NEG`` (только при INT/EXT, иначе -221), ``TRIG:DEL`` (-1...1 с),
  ``TRIG:HOLD`` (1 мкс...0.4 с);
* строб 1: ``SWE1:OFFS:TIME`` (-1...1 с), ``SWE1:TIME`` (0...1 с);
* результаты: блоки CALC1..3 с ``CALC<n>:FEED1 "POW:AVER|PEAK|PTAV[ ON SWEEP1]"``, чтение ``FETC<n>?``.
  ``ON SWEEP1`` (строб) разрешён только при запуске INT/EXT, PEAK/PTAV - только в NORMal;
* ``INITiate`` - перекрываемая команда: ``INIT;*OPC`` взводит бит OPC регистра ESR по окончании
  измерения. Драйвер ждёт этот бит в опросе, не блокируя поток запросом (при запуске по уровню
  сигнала может не быть сколько угодно долго);
* ``CAL:ZERO:AUTO ONCE`` / ``CAL:AUTO ONCE`` + ``*OPC?`` (несколько секунд);
* временная область: ``TRAC:STAT ON`` (только DET NORM и INIT:CONT OFF), ``SENS:TRAC:TIME`` (25 нс...1 с),
  ``SENS:TRAC:OFFS:TIME`` (-1...1 с от задержанного запуска), ``TRAC:UNIT DBM``; после каждого измерения
  ``TRAC:DATA? MRES`` - 1000 точек двоичным блоком float32. При включённой трассе свободный счёт
  идёт по одному измерению (INIT;*OPC), а не INIT:CONT ON.

Что драйвер меняет в приборе при подключении (без *RST): единицы CALC1..3 в dBm и
назначение CALC1..3 (среднее/пик/пик-среднее; в стробе 1 при запуске INT/EXT) - чтобы стандартные ключи
``meas.power``/``meas.peak``/``meas.ptav`` всегда значили одно и то же.
"""
from __future__ import annotations

import math
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, ConnectionState, Kind, Param
from ..core.trace import Trace
from ..profiles import POWER_METER

__all__ = ["KeysightU2021XA"]

F, I, B, E = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM

#: CALC-блок -> стандартный ключ и назначение в режиме NORMal
FEEDS = {1: ("meas.power", "POW:AVER"),
         2: ("meas.peak", "POW:PEAK"),
         3: ("meas.ptav", "POW:PTAV")}
#: аппаратный строб ``... ON SWEEP<n>`` включается только для внешнего триггера EXT;
#: режим INT (LEVEL) реализован программно по точкам TRAC:DATA? поверх свободного запуска IMM
GATED_SOURCES = ("EXT",)
RESULT_KEYS = tuple(k for k, _ in FEEDS.values()) + ("trace",)
#: параметры запуска / строба: их смена в триггерном режиме делает прежний результат неактуальным
TRIG_REARM_CLEAR_KEYS = frozenset(("trig.level", "trig.level_auto", "trig.slope", "trig.delay", "trig.holdoff",
                                   "gate.start", "gate.length"))
TRACE_RES = "MRES"                     # 1000 точек
SW_LEVEL_MIN_TRACE_S = 1.25e-3         # дефолтное окно захвата для программного LEVEL (≥ 1 периода при ≥ 1 кГц, шаг 1.25 мкс)
CAL_TIMEOUT_MS = 30000


class KeysightU2021XA(VISAInstrument):
    NAME = "Keysight U2021XA"
    PROFILE = POWER_METER
    POLL_INTERVAL_MS = 200

    def __init__(self, transport, model=None, parent=None, command_delay_s: float | None = None,
                 read_pause_s: float | None = None):
        self.model = model
        self._continuous = True
        self._armed = False
        self._results: dict[str, float | None] = dict.fromkeys(RESULT_KEYS)
        #: программный видео-уровень (LEVEL / INT): хранится в драйвере, без записи TRIG:SOUR INT / TRIG:LEV в датчик
        self._sw_trig_level: float = 0.0
        self._sw_trig_level_auto: bool = True
        self._sw_trig_slope: str = "POS"
        self._user_tracing: bool = False
        self._user_trace_time_set: bool = False
        self._hw_trace_time: float = 100e-6
        self._trig_level_min = -40.0
        self._trig_level_max = 20.0
        self._batch_active = False
        self._batch_mode_changed = False
        super().__init__(transport, parent, command_delay_s, read_pause_s)

    # ------------------------------------------------------------ описание
    def build_params(self):
        meas, trig, sens = "Измерение/Результат", "Измерение/Запуск", "Датчик/Настройки"
        return [
            Param("meas.power", "Средняя мощность", F, "dBm", section=meas, group="MEAS"),
            Param("meas.peak", "Пиковая мощность", F, "dBm", section=meas, group="MEAS"),
            Param("meas.ptav", "Пик / среднее", F, "dB", section=meas, group="MEAS"),
            Param("trace", "Мощность во времени", Kind.TRACE, "dBm", section=meas, group="MEAS"),
            Param("meas.armed", "Ждём запуск", B, section=meas, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=trig, query="<virtual>", write="<virtual>"),
            # offset / offset.state читаются до trig.level, чтобы сразу учесть поправку в шкале уровня запуска
            # CORR:GAIN2 на реальных U2020X может сам включить CORR:GAIN2:STAT - перечитываем offset.state до trig.level
            Param("offset", "Поправка", F, "dB", section=sens, query="CORR:GAIN2?",
                  write="CORR:GAIN2 {value}", minimum=-100.0, maximum=100.0, tolerance=1e-3,
                  also_read=("offset.state",)),
            Param("offset.state", "Поправка вкл.", B, section=sens, query="CORR:GAIN2:STAT?",
                  write="CORR:GAIN2:STAT {value}"),
            Param("trig.source", "Запуск", E, section=trig, query="TRIG:SOUR?", write="TRIG:SOUR {value}",
                  choices=("IMM", "INT", "EXT", "BUS", "HOLD"),
                  labels=("Свободный", "По уровню", "Внешний", "Программный", "Удержание"),
                  enum_map=(("INT", "INT"), ("INT", "INT1"), ("INT", "INTERNAL"),
                            ("EXT", "EXT"), ("EXT", "EXTERNAL"), ("IMM", "IMM"), ("IMM", "IMMEDIATE")),
                  also_read=("detector", "trig.level", "trig.level_auto", "trig.slope")),
            Param("trig.level", "Уровень запуска", F, "dBm", section=trig, query="<virtual>",
                  write="<virtual>", minimum=-140.0, maximum=120.0, tolerance=1e-3,
                  also_read=("trig.level_auto",)),
            Param("trig.level_auto", "Уровень авто", B, section=trig, query="<virtual>",
                  write="<virtual>", also_read=("trig.level",)),
            Param("trig.slope", "Фронт", E, section=trig, query="TRIG:SLOP?", write="TRIG:SLOP {value}",
                  choices=("POS", "NEG"), labels=("Нарастающий", "Спадающий"),
                  enum_map=(("POS", "POS"), ("POS", "POSITIVE"), ("NEG", "NEG"), ("NEG", "NEGATIVE"))),
            Param("trig.delay", "Задержка", F, "s", section=trig, query="TRIG:DEL?",
                  write="TRIG:DEL {value}", minimum=-1.0, maximum=1.0, tolerance=1e-4),
            Param("trig.holdoff", "Блокировка", F, "s", section=trig, query="TRIG:HOLD?",
                  write="TRIG:HOLD {value}", minimum=1e-6, maximum=0.4, tolerance=1e-4),
            Param("gate.start", "Начало строба", F, "s", section=trig, query="SWE1:OFFS:TIME?",
                  write="SWE1:OFFS:TIME {value}", minimum=-1.0, maximum=1.0, tolerance=1e-4),
            Param("gate.length", "Длина строба", F, "s", section=trig, query="SWE1:TIME?",
                  write="SWE1:TIME {value}", minimum=0.0, maximum=1.0, tolerance=1e-4),
            Param("trace.state", "Трасса", B, section=trig, query="TRAC:STAT?", write="TRAC:STAT {value}"),
            Param("trace.time", "Длительность трассы", F, "s", section=trig, query="SENS:TRAC:TIME?",
                  write="SENS:TRAC:TIME {value}", minimum=25e-9, maximum=1.0, tolerance=1e-4),
            Param("trace.offset", "Начало трассы", F, "s", section=trig, query="SENS:TRAC:OFFS:TIME?",
                  write="SENS:TRAC:OFFS:TIME {value}", minimum=-1.0, maximum=1.0, tolerance=1e-4),
            Param("freq", "Частота сигнала", F, "Hz", section=sens, query="FREQ?", write="FREQ {value}",
                  minimum=1e3, maximum=1000e9, tolerance=1e-9),
            Param("detector", "Режим", E, section=sens, query="DET:FUNC?", write="DET:FUNC {value}",
                  choices=("AVERAGE", "NORMAL"), labels=("Только среднее", "Пик/строб/запуск"),
                  enum_map=(("AVERAGE", "AVER"), ("NORMAL", "NORM")),
                  also_read=("trig.source", "trig.delay", *RESULT_KEYS)),
            Param("video_bw", "Видеополоса", E, section=sens, query="BAND:VID?", write="BAND:VID {value}",
                  choices=("OFF", "LOW", "MED", "HIGH"), enum_map=(("MED", "MED"), ("MED", "MEDIUM"))),
            Param("avg.state", "Усреднение", B, section=sens, query="AVER?", write="AVER {value}"),
            Param("avg.count", "Число усреднений", I, section=sens, query="AVER:COUN?",
                  write="AVER:COUN {value}", minimum=1, maximum=1024, also_read=("avg.auto",)),
            Param("avg.auto", "Усреднения авто", B, section=sens, query="AVER:COUN:AUTO?",
                  write="AVER:COUN:AUTO {value}", also_read=("avg.count",)),
        ]

    def build_actions(self):
        return [
            Action("single", "Одно измерение"),
            Action("abort", "Отменить ожидание"),
            Action("restart", "Перезапустить измерение"),
            Action("trigger", "Программный запуск (BUS)"),
            Action("zero", "Обнуление", confirm="Снимите сигнал со входа датчика. Обнулить?"),
            Action("calibrate", "Калибровка"),
            Action("reset", "Сброс *RST", confirm="Сбросить датчик к заводским настройкам?"),
        ]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self._armed = False
        self._results = dict.fromkeys(RESULT_KEYS)
        self._sw_trig_level = 0.0
        self._sw_trig_level_auto = True
        self._sw_trig_slope = "POS"
        self._user_tracing = False
        self._user_trace_time_set = False
        self._hw_trace_time = 100e-6
        self._trig_level_min = -40.0
        self._trig_level_max = 20.0
        self._batch_active = False
        self._batch_mode_changed = False
        self.write("INIT:CONT OFF")
        self.write("ABOR")
        for n in FEEDS:
            self.write(f"UNIT{n}:POW DBM")
        self.write("TRAC:UNIT DBM")
        det = "NORMAL" if self.query("DET:FUNC?").strip().upper().startswith("NORM") else "AVERAGE"
        hw_src = self._params["trig.source"].from_scpi(self.query("TRIG:SOUR?"))
        if hw_src == "INT":
            # Аппаратный INT не используем: переводим физический датчик в IMM, оставляя режим LEVEL (INT) программным
            if det != "NORMAL":
                self.write("DET:FUNC NORM")
                det = "NORMAL"
            self.write("TRIG:SOUR IMM")
        self._values["detector"] = det
        self._values["trig.source"] = hw_src
        self._configure_feeds(det, hw_src)
        self.opc_latched = False

    def on_closed(self) -> None:
        self._armed = False
        self._results = dict.fromkeys(RESULT_KEYS)
        self._user_tracing = False
        self._user_trace_time_set = False
        self._trig_level_min = -40.0
        self._trig_level_max = 20.0
        self._batch_active = False
        self._batch_mode_changed = False

    def on_disconnecting(self) -> None:
        if self._armed:
            self.write("ABOR")
            self._armed = False

    def _configure_feeds(self, detector: str, source: str) -> None:
        """Назначение CALC1..3 под режим (правила мануала U2020X):
        PEAK/PTAV - только при DET NORM; ``ON SWEEP1`` (аппаратный строб) - только при TRIG:SOUR EXT.
        В свободном счёте и в программном LEVEL датчик стоит без ``ON SWEEP1``."""
        gate = " ON SWEEP1" if source in GATED_SOURCES else ""
        for n, (_, feed) in FEEDS.items():
            if detector != "NORMAL" and n > 1:
                break
            self.write(f'CALC{n}:FEED1 "{feed}{gate}"')

    @property
    def _normal(self) -> bool:
        return self._values.get("detector") == "NORMAL"

    @property
    def _sw_level(self) -> bool:
        return self._values.get("trig.source") == "INT"

    @property
    def _tracing(self) -> bool:
        return (self._values.get("trace.state") is True) or self._sw_level

    @property
    def _free_run(self) -> bool:
        return self._values.get("trig.source", "IMM") == "IMM"

    def is_triggered_mode(self) -> bool:
        return (not self._free_run) or (not self._continuous)

    def _stop_acquisition(self) -> tuple[bool, bool]:
        """Остановить измерение перед записью настройки: (было взведено, был INIT:CONT ON)."""
        was_armed = self._armed
        was_cont_free = self._free_run and self._continuous and not self._tracing
        if was_cont_free:
            self.write("INIT:CONT OFF")
        if (was_armed and not self._sw_level) or was_cont_free:
            self.write("ABOR")
        if was_armed or was_cont_free:
            self._set_armed(False)
        return was_armed, was_cont_free

    def _active_offset(self) -> float:
        """Активная поправка CORR:GAIN2, дБ (0, если выключена)."""
        if self._values.get("offset.state") is True:
            off = self._values.get("offset")
            if isinstance(off, (int, float)) and math.isfinite(off):
                return float(off)
        return 0.0

    def _desired_sw_trace_time(self) -> float:
        """Длительность захвата SENS:TRAC:TIME на датчике в режиме программного LEVEL."""
        user_t = float(self._values.get("trace.time") or 100e-6)
        delay = abs(float(self._values.get("trig.delay") or 0.0))
        g_start = abs(float(self._values.get("gate.start") or 0.0))
        g_len = max(0.0, float(self._values.get("gate.length") or 0.0))
        need = (delay + g_start + g_len) * 2.2
        base = user_t if self._user_trace_time_set else max(user_t, SW_LEVEL_MIN_TRACE_S)
        return min(1.0, max(25e-9, base, need))

    def _sync_hw_trace_for_mode(self) -> None:
        """Поддерживать аппаратные TRAC:STAT и SENS:TRAC:TIME для текущего режима."""
        if not self._normal:
            self._hw_trace_time = float(self._values.get("trace.time") or 100e-6)
            return
        if self._sw_level:
            target_t = self._desired_sw_trace_time()
            self.write(f"SENS:TRAC:TIME {self._params['trace.time'].to_scpi(target_t)}")
            self._hw_trace_time = target_t
            self.write("TRAC:STAT ON")
        else:
            user_t = float(self._values.get("trace.time") or 100e-6)
            if abs(self._hw_trace_time - user_t) > 1e-12:
                self.write(f"SENS:TRAC:TIME {self._params['trace.time'].to_scpi(user_t)}")
                self._hw_trace_time = user_t
            self.write(f"TRAC:STAT {'ON' if self._user_tracing else 'OFF'}")

    def _clear_results(self) -> None:
        """Сбросить устаревшие результаты измерения и уведомить GUI (до нового запуска - пусто)."""
        self._results = dict.fromkeys(RESULT_KEYS)
        for k in RESULT_KEYS:
            if self._values.get(k) is not None:
                self._publish(k, None)

    def _flush_pending(self) -> None:
        if self._state is not ConnectionState.CONNECTED or not self._pending:
            super()._flush_pending()
            return
        was_armed, was_cont_free = self._stop_acquisition()
        self._batch_active = True
        self._batch_mode_changed = False
        try:
            super()._flush_pending()
        finally:
            self._batch_active = False
            mode_changed = self._batch_mode_changed
            self._batch_mode_changed = False
            if self._state is ConnectionState.CONNECTED and not self._stopping:
                if mode_changed or was_cont_free:
                    self._apply_run_mode()
                elif was_armed:
                    self._arm()

    def _trig_level_bounds(self) -> tuple[float, float]:
        """Допустимый диапазон trig.level в шкале показаний (с учётом активной поправки)."""
        off = self._active_offset()
        return self._trig_level_min + off, self._trig_level_max + off

    def validate_param(self, spec: Param, value: Any) -> Any:
        value = super().validate_param(spec, value)
        if spec.key == "trig.level":
            lo, hi = self._trig_level_bounds()
            if value < lo - 1e-6 or value > hi + 1e-6:
                off = self._active_offset()
                raise ValueError(
                    f"{spec.label}: {value:g} дБм вне диапазона {lo:g}..{hi:g} дБм (поправка {off:+g} дБ)"
                )
        return value

    def write_param(self, spec: Param, value: Any) -> None:
        key = spec.key
        if key == "meas.continuous":
            self._continuous = bool(value)
            if self._batch_active:
                self._batch_mode_changed = True
            else:
                self._apply_run_mode()
            return
        was_armed, was_cont_free = (False, False) if self._batch_active else self._stop_acquisition()
        if key in ("trig.source", "detector"):
            self._change_mode(spec, value, rearm=not self._batch_active)
            if self._batch_active:
                self._batch_mode_changed = True
            return
        if key == "trace.state":
            self._set_tracing(bool(value), rearm=not self._batch_active)
            if self._batch_active:
                self._batch_mode_changed = True
            return
        try:
            if key == "trig.level":
                self._sw_trig_level = float(value)
                self._sw_trig_level_auto = False
            elif key == "trig.level_auto":
                self._sw_trig_level_auto = bool(value)
            elif key == "trig.slope":
                self._sw_trig_slope = str(value)
                if self._values.get("trig.source") == "EXT":
                    super().write_param(spec, value)
            elif key == "trace.time":
                self._user_trace_time_set = True
                super().write_param(spec, value)
                self._values["trace.time"] = float(value)
                self._hw_trace_time = float(value)
                if self._sw_level:
                    self._sync_hw_trace_for_mode()
            else:
                super().write_param(spec, value)
                if key in ("trig.delay", "gate.start", "gate.length"):
                    self._values[key] = value
                    if self._sw_level:
                        self._sync_hw_trace_for_mode()
            if not self._free_run and key in TRIG_REARM_CLEAR_KEYS:
                self._clear_results()
        finally:
            if not self._batch_active:
                if was_cont_free:
                    self._apply_run_mode()
                elif was_armed:
                    self._arm()

    def _set_tracing(self, on: bool, rearm: bool = True) -> None:
        """Трасса требует DET NORM и INIT:CONT OFF: режим датчика переключается сам."""
        try:
            self._user_tracing = bool(on)
            if on and not self._normal:
                self._change_mode(self._params["detector"], "NORMAL", rearm=False)
                self._publish("detector", "NORMAL")
            self.write("INIT:CONT OFF")
            self._abort()
            hw_on = bool(on) or self._sw_level
            super().write_param(self._params["trace.state"], hw_on)
            self._values["trace.state"] = bool(on)
            if not hw_on:
                self._results["trace"] = None
                self._publish("trace", None)
        finally:
            if rearm:
                self._apply_run_mode()

    def _change_mode(self, spec: Param, value: Any, rearm: bool = True) -> None:
        """Смена запуска/режима без аппаратного TRIG:SOUR INT:
        для LEVEL (INT) физический датчик ставится в DET NORM + TRIG:SOUR IMM + TRAC:STAT ON."""
        det = self._values.get("detector", "NORMAL")
        src = self._values.get("trig.source", "IMM")
        if spec.key == "detector":
            new_det = value
            new_src = "IMM" if value == "AVERAGE" and src == "INT" else src
        else:
            new_src = value
            new_det = "NORMAL" if value == "INT" else det
        hw_new_src = "IMM" if new_src == "INT" else new_src
        try:
            if src in GATED_SOURCES and new_src not in GATED_SOURCES:
                self._configure_feeds(det, new_src)  # снять строб, пока источник ещё EXT
            if new_det == "AVERAGE":
                if self._user_tracing or src == "INT":
                    self.write("TRAC:STAT OFF")
                self._user_tracing = False
                self._values["trace.state"] = False
                self._publish("trace.state", False)
            if new_det != det:
                self.write(f"DET:FUNC {'NORM' if new_det == 'NORMAL' else 'AVER'}")
            self.write(f"TRIG:SOUR {hw_new_src}")
            if new_src == "EXT":
                self.write(f"TRIG:SLOP {self._sw_trig_slope}")
            self._values["detector"], self._values["trig.source"] = new_det, new_src
            self._configure_feeds(new_det, new_src)
            if new_det == "NORMAL":
                self._sync_hw_trace_for_mode()
            self._clear_results()
        finally:
            if rearm:
                self._apply_run_mode()

    def read_param_raw(self, spec: Param) -> Any:
        if spec.key == "meas.continuous":
            return self._continuous
        if spec.key == "meas.armed":
            return self._armed
        if spec.key == "trig.source":
            hw = super().read_param_raw(spec)
            if self._sw_level and hw == "IMM":
                return "INT"
            return hw
        if spec.key == "trig.level":
            return self._sw_trig_level
        if spec.key == "trig.level_auto":
            return self._sw_trig_level_auto
        if spec.key == "trig.slope":
            if self._values.get("trig.source") == "EXT":
                self._sw_trig_slope = super().read_param_raw(spec)
            return self._sw_trig_slope
        if spec.key == "trace.state":
            hw = super().read_param_raw(spec)
            if not self._sw_level:
                self._user_tracing = bool(hw)
            return self._user_tracing
        if spec.key == "trace.time":
            if not self._normal:
                return None
            hw_t = super().read_param_raw(spec)
            self._hw_trace_time = float(hw_t)
            if self._sw_level and not self._user_trace_time_set:
                return float(self._values.get("trace.time") or 100e-6)
            return hw_t
        if spec.key == "trace.offset" and not self._normal:
            return None
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        # Результаты не запрашиваются при синхронизации: FETC? без готового измерения
        # даёт -230 "Data corrupt or stale". Отдаём последнее полученное.
        return dict(self._results)

    def _sync_all(self) -> None:
        super()._sync_all()
        if self._sw_level:
            self._sync_hw_trace_for_mode()
        self._apply_run_mode()

    # ------------------------------------------------------ цикл измерения
    def _apply_run_mode(self) -> None:
        """Свободный счёт - INIT:CONT ON и чтение при опросе; иначе - взвод по одному."""
        self._abort()
        if self._free_run and self._continuous and not self._tracing:
            self.write("INIT:CONT ON")
        else:
            self.write("INIT:CONT OFF")
            if self._continuous:
                self._arm()
        self._sync_poll_timer()

    def _arm(self) -> None:
        if self._sw_level:
            self._set_armed(True)
            return
        self.arm_opc("INIT")
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            if not self._sw_level:
                self.write("ABOR")
            self._set_armed(False)
        self._opc_pending = False
        self.opc_latched = False

    def _set_armed(self, armed: bool) -> None:
        if armed != self._armed or "meas.armed" not in self._values:
            self._armed = armed
            self._publish("meas.armed", armed)

    @staticmethod
    def _clean_reading(v: float) -> float | None:
        """SCPI-сентинелы (9.91E+37) и NaN/Inf не публикуются как валидная мощность."""
        return v if math.isfinite(v) and abs(v) < 9.9e36 else None

    @staticmethod
    def _integrate_window_mw(ts: list[float], mw: list[float], w0: float, w1: float) -> tuple[float, float] | None:
        """Средняя и пиковая линейная мощность (мВт) на отрезке ``[w0, w1]`` методом трапеций с интерполяцией краёв."""
        n = len(ts)
        if n == 0 or w1 < ts[0] or w0 > ts[-1]:
            return None
        a = max(w0, ts[0])
        b = min(w1, ts[-1])
        if b < a:
            return None
        dt = (ts[-1] - ts[0]) / (n - 1) if n > 1 else 0.0
        if dt <= 0.0 or abs(b - a) <= 1e-15:
            idx = min(max(0, int(round((a - ts[0]) / dt))) if dt > 0 else 0, n - 1)
            val = mw[idx]
            return (val, val) if math.isfinite(val) else None

        def interp_at(t: float) -> float:
            pos = (t - ts[0]) / dt
            i0 = min(max(0, int(math.floor(pos))), n - 1)
            i1 = min(i0 + 1, n - 1)
            frac = pos - i0
            v0, v1 = mw[i0], mw[i1]
            if not (math.isfinite(v0) and math.isfinite(v1)):
                return v0 if math.isfinite(v0) else v1
            return v0 + frac * (v1 - v0)

        pts_t = [a]
        pts_v = [interp_at(a)]
        i_first = max(0, int(math.ceil((a - ts[0]) / dt + 1e-12)))
        i_last = min(n - 1, int(math.floor((b - ts[0]) / dt - 1e-12)))
        for idx in range(i_first, i_last + 1):
            if math.isfinite(mw[idx]):
                pts_t.append(ts[idx])
                pts_v.append(mw[idx])
        pts_t.append(b)
        pts_v.append(interp_at(b))
        finite_v = [v for v in pts_v if math.isfinite(v)]
        if not finite_v:
            return None
        area = 0.0
        span = 0.0
        for i in range(len(pts_t) - 1):
            seg = pts_t[i + 1] - pts_t[i]
            if seg > 0 and math.isfinite(pts_v[i]) and math.isfinite(pts_v[i + 1]):
                area += 0.5 * (pts_v[i] + pts_v[i + 1]) * seg
                span += seg
        avg = area / span if span > 0 else sum(finite_v) / len(finite_v)
        return avg, max(finite_v)

    def _process_sw_level(self, y: list[float]) -> tuple[dict[str, Any], float | None]:
        """Программный запуск по видео-уровню (LEVEL) по массиву TRAC:DATA?."""
        out: dict[str, Any] = dict.fromkeys(RESULT_KEYS)
        n = len(y)
        start = float(self._values.get("trace.offset") or 0.0)
        cap_len = float(self._hw_trace_time or self._values.get("trace.time") or 100e-6)
        dt = cap_len / (n - 1) if n > 1 else 0.0
        ts_raw = [start + i * dt for i in range(n)]
        meta = {
            "trigger": "INT",
            "delay": self._values.get("trig.delay"),
            "gate": (self._values.get("gate.start"), self._values.get("gate.length")),
        }

        finite = [v for v in y if math.isfinite(v)]
        if not finite or n < 2:
            out["trace"] = Trace.linear(start, start + cap_len, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)
            return out, None

        off = self._active_offset()
        detected_auto_lvl: float | None = None
        if self._sw_trig_level_auto:
            y_min, y_max = min(finite), max(finite)
            floor_dbm = -40.0 + off
            if y_max < floor_dbm or (y_max - y_min) < 1.0:
                out["trace"] = Trace.linear(start, start + cap_len, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)
                return out, None
            lo_ref = max(y_min, floor_dbm)
            lvl = round(max(floor_dbm, min(20.0 + off, 0.5 * (y_max + lo_ref))), 1)
            detected_auto_lvl = lvl
        else:
            lvl = self._sw_trig_level

        slope = self._sw_trig_slope
        crossings: list[tuple[int, float]] = []
        for i in range(1, n):
            y0, y1 = y[i - 1], y[i]
            if not (math.isfinite(y0) and math.isfinite(y1)):
                continue
            if slope == "NEG":
                if y0 > lvl >= y1:
                    crossings.append((i, 0.5 * (ts_raw[i - 1] + ts_raw[i])))
            else:
                if y0 < lvl <= y1:
                    crossings.append((i, 0.5 * (ts_raw[i - 1] + ts_raw[i])))

        if not crossings:
            out["trace"] = Trace.linear(start, start + cap_len, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)
            return out, None

        delay = float(self._values.get("trig.delay") or 0.0)
        g_start = float(self._values.get("gate.start") or 0.0)
        g_len = max(0.0, float(self._values.get("gate.length") or 0.0))
        win_rel_end = delay + g_start + g_len

        chosen_idx, t_trig = crossings[0]
        for idx_c, tc in crossings:
            if tc + delay + g_start >= ts_raw[0] and tc + win_rel_end <= ts_raw[-1]:
                chosen_idx, t_trig = idx_c, tc
                break

        mw = [10.0 ** (v / 10.0) if math.isfinite(v) else math.nan for v in y]
        if g_len > 0.0:
            w0 = t_trig + delay + g_start
            w1 = w0 + g_len
            res = self._integrate_window_mw(ts_raw, mw, w0, w1)
        else:
            # Если длина строба = 0, усредняем саму полку импульса от найденного фронта
            j = chosen_idx
            if slope == "NEG":
                while j < n and math.isfinite(y[j]) and y[j] <= lvl:
                    j += 1
            else:
                while j < n and math.isfinite(y[j]) and y[j] >= lvl:
                    j += 1
            seg = [mw[k] for k in range(chosen_idx, max(chosen_idx + 1, j)) if math.isfinite(mw[k])]
            res = (sum(seg) / len(seg), max(seg)) if seg else None

        if res is not None:
            avg_mw, peak_mw = res
            p_avg = 10.0 * math.log10(max(avg_mw, 1e-30))
            p_peak = 10.0 * math.log10(max(peak_mw, 1e-30))
            out["meas.power"] = self._clean_reading(p_avg)
            out["meas.peak"] = self._clean_reading(p_peak)
            if out["meas.power"] is not None and out["meas.peak"] is not None:
                out["meas.ptav"] = self._clean_reading(p_peak - p_avg)

        # Выравниваем трассу так, чтобы фронт запуска стоял в t = 0 (с учётом trace.offset)
        x_aligned = [t - t_trig + start for t in ts_raw]
        out["trace"] = Trace(x_aligned, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)
        return out, detected_auto_lvl

    def _fetch(self) -> None:
        if self._sw_level:
            raw = self.query_float_block(f"TRAC:DATA? {TRACE_RES}")
            y = [v if (math.isfinite(v) and abs(v) < 9.9e36) else math.nan for v in raw] if raw else []
            out, auto_lvl = self._process_sw_level(y) if y else (dict.fromkeys(RESULT_KEYS), None)
            self._results = out
            for k, v in out.items():
                self._publish(k, v)
            if auto_lvl is not None:
                self._sw_trig_level = auto_lvl
                if not self._params["trig.level"].same(auto_lvl, self._values.get("trig.level")):
                    self._publish("trig.level", auto_lvl)
            return

        keys = RESULT_KEYS if self._normal else RESULT_KEYS[:1]
        out = dict.fromkeys(RESULT_KEYS)
        for n, (key, _) in FEEDS.items():
            if key in keys:
                out[key] = self._clean_reading(self.query_float(f"FETC{n}?"))
        if self._tracing and self._normal:
            out["trace"] = self._read_trace()
        self._results = out
        for k, v in out.items():
            self._publish(k, v)

    def _read_trace(self) -> Trace | None:
        y = self.query_float_block(f"TRAC:DATA? {TRACE_RES}")
        if not y:
            return None
        y = [v if (math.isfinite(v) and abs(v) < 9.9e36) else math.nan for v in y]
        start = float(self._values.get("trace.offset") or 0.0)
        length = float(self._values.get("trace.time") or 100e-6)
        meta = {"trigger": self._values.get("trig.source"), "delay": self._values.get("trig.delay"),
                "gate": (self._values.get("gate.start"), self._values.get("gate.length"))}
        return Trace.linear(start, start + length, y, x_unit="s", y_unit="dBm", name="trace", meta=meta)

    def after_resync(self, cleared: bool) -> None:
        """После Device Clear перезапустить взведённое измерение через ABOR + INIT;*OPC."""
        if cleared and self._armed:
            self.write("ABOR")
            self._opc_pending = False
            self.opc_latched = False
            self._arm()
            return
        super().after_resync(cleared)

    def after_poll_timeout(self) -> None:
        """Чтение результата прервал таймаут (обмен уже восстановлен): сбросить и взвести снова."""
        if self._continuous and not self._armed and not (self._free_run and not self._tracing):
            self.write("ABOR")
            self._arm()

    def poll(self) -> None:
        if self._free_run and self._continuous and not self._tracing:
            self._fetch()
            return
        if not self._armed:
            return
        if self._sw_level:
            self.write("INIT")
            self.query("*OPC?")
            self._set_armed(False)
            try:
                self._fetch()
            finally:
                if self._continuous and self._state is ConnectionState.CONNECTED:
                    self._set_armed(True)
            return
        if self.opc_done():
            self._set_armed(False)
            try:
                self._fetch()
            except ValueError:
                if self._continuous and self._state is ConnectionState.CONNECTED:
                    self._arm()
                raise
            if self._continuous and self._state is ConnectionState.CONNECTED:
                self._arm()

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "single":
            self._continuous = False
            self._publish("meas.continuous", False)
            self._abort()
            self.write("INIT:CONT OFF")
            if self._free_run or self._sw_level:    # IMM / программный LEVEL: захват сразу
                if self._sw_level:
                    self._clear_results()
                self.write("INIT")
                self.query("*OPC?")
                self._fetch()
            else:
                self._clear_results()
                self._arm()
            self._sync_poll_timer()
            return None
        if name == "abort":
            self._continuous = False
            self._publish("meas.continuous", False)
            self._abort()
            self.write("INIT:CONT OFF")
            self.write("ABOR")
            self._clear_results()
            self._sync_poll_timer()
            return None
        if name == "restart":
            was_armed, was_cont_free = self._stop_acquisition()
            if not (was_armed or was_cont_free):
                self.write("ABOR")
            if was_cont_free or self._continuous:
                self._apply_run_mode()
            elif was_armed:
                self._arm()
            return None
        if name == "trigger":
            self.write("*TRG")
            return None
        if name in ("zero", "calibrate"):
            self._stop_acquisition()
            old = self.transport.timeout_ms
            self.transport.timeout_ms = CAL_TIMEOUT_MS
            try:
                self.write("CAL:ZERO:AUTO ONCE" if name == "zero" else "CAL:AUTO ONCE")
                self.query("*OPC?")
            finally:
                self.transport.timeout_ms = old
                self._apply_run_mode()
            return None
        if name == "reset":
            self._abort()
            self.write("*RST")
            self.query("*OPC?")
            self.on_connected()
            self._sync_all()
            return None
        return super().run_custom_action(name, args)
