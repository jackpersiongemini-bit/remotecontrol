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
* результаты: блоки CALC1..3 с ``CALC<n>:FEED1 "POW:AVER|PEAK|PTAV ON SWEEP1"``, чтение ``FETC<n>?``;
* ``INITiate`` - перекрываемая команда: ``INIT;*OPC`` взводит бит OPC регистра ESR по окончании
  измерения. Драйвер ждёт этот бит в опросе, не блокируя поток запросом (при запуске по уровню
  сигнала может не быть сколько угодно долго);
* ``CAL:ZERO:AUTO ONCE`` / ``CAL:AUTO ONCE`` + ``*OPC?`` (несколько секунд).

Что драйвер меняет в приборе при подключении (без *RST): единицы CALC1..3 в dBm и
назначение CALC1..3 (среднее/пик/пик-среднее в стробе 1) - чтобы стандартные ключи
``meas.power``/``meas.peak``/``meas.ptav`` всегда значили одно и то же.
"""
from __future__ import annotations

from typing import Any

from ..core.instrument import VISAInstrument
from ..core.params import Action, Kind, Param
from ..profiles import POWER_METER

__all__ = ["KeysightU2021XA"]

F, I, B, E = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM

#: CALC-блок -> стандартный ключ и назначение в режиме NORMal
FEEDS = {1: ("meas.power", "POW:AVER ON SWEEP1"),
         2: ("meas.peak", "POW:PEAK ON SWEEP1"),
         3: ("meas.ptav", "POW:PTAV ON SWEEP1")}
RESULT_KEYS = tuple(k for k, _ in FEEDS.values())
CAL_TIMEOUT_MS = 30000


class KeysightU2021XA(VISAInstrument):
    NAME = "Keysight U2021XA"
    PROFILE = POWER_METER
    POLL_INTERVAL_MS = 200

    def __init__(self, transport, model=None, parent=None, command_delay_s: float | None = None):
        self.model = model
        self._continuous = True
        self._armed = False
        self._results: dict[str, float | None] = dict.fromkeys(RESULT_KEYS)
        super().__init__(transport, parent, command_delay_s)

    # ------------------------------------------------------------ описание
    def build_params(self):
        meas, trig, sens = "Измерение/Результат", "Измерение/Запуск", "Датчик/Настройки"
        return [
            Param("meas.power", "Средняя мощность", F, "dBm", section=meas, group="MEAS"),
            Param("meas.peak", "Пиковая мощность", F, "dBm", section=meas, group="MEAS"),
            Param("meas.ptav", "Пик / среднее", F, "dB", section=meas, group="MEAS"),
            Param("meas.armed", "Ждём запуск", B, section=meas, query="<virtual>"),
            Param("meas.continuous", "Непрерывно", B, section=trig, query="<virtual>", write="<virtual>"),
            Param("trig.source", "Запуск", E, section=trig, query="TRIG:SOUR?", write="TRIG:SOUR {value}",
                  choices=("IMM", "INT", "EXT", "BUS", "HOLD"),
                  labels=("Свободный", "По уровню", "Внешний", "Программный", "Удержание"),
                  enum_map=(("INT", "INT"), ("INT", "INT1"), ("EXT", "EXT"), ("IMM", "IMM")),
                  also_read=("detector",)),
            Param("trig.level", "Уровень запуска", F, "dBm", section=trig, query="TRIG:LEV?",
                  write="TRIG:LEV {value}", minimum=-40.0, maximum=20.0, tolerance=1e-3,
                  also_read=("trig.level_auto",)),
            Param("trig.level_auto", "Уровень авто", B, section=trig, query="TRIG:LEV:AUTO?",
                  write="TRIG:LEV:AUTO {value}"),
            Param("trig.slope", "Фронт", E, section=trig, query="TRIG:SLOP?", write="TRIG:SLOP {value}",
                  choices=("POS", "NEG"), labels=("Нарастающий", "Спадающий")),
            Param("trig.delay", "Задержка", F, "s", section=trig, query="TRIG:DEL?",
                  write="TRIG:DEL {value}", minimum=-1.0, maximum=1.0, tolerance=1e-4),
            Param("trig.holdoff", "Блокировка", F, "s", section=trig, query="TRIG:HOLD?",
                  write="TRIG:HOLD {value}", minimum=1e-6, maximum=0.4, tolerance=1e-4),
            Param("gate.start", "Начало строба", F, "s", section=trig, query="SWE1:OFFS:TIME?",
                  write="SWE1:OFFS:TIME {value}", minimum=-1.0, maximum=1.0, tolerance=1e-4),
            Param("gate.length", "Длина строба", F, "s", section=trig, query="SWE1:TIME?",
                  write="SWE1:TIME {value}", minimum=0.0, maximum=1.0, tolerance=1e-4),
            Param("freq", "Частота сигнала", F, "Hz", section=sens, query="FREQ?", write="FREQ {value}",
                  minimum=1e3, maximum=1000e9, tolerance=1e-9),
            Param("detector", "Режим", E, section=sens, query="DET:FUNC?", write="DET:FUNC {value}",
                  choices=("AVERAGE", "NORMAL"), labels=("Только среднее", "Пик/строб/запуск"),
                  enum_map=(("AVERAGE", "AVER"), ("NORMAL", "NORM")),
                  also_read=("trig.source", "trig.delay", *RESULT_KEYS)),
            Param("video_bw", "Видеополоса", E, section=sens, query="BAND:VID?", write="BAND:VID {value}",
                  choices=("OFF", "LOW", "MED", "HIGH")),
            Param("avg.state", "Усреднение", B, section=sens, query="AVER?", write="AVER {value}"),
            Param("avg.count", "Число усреднений", I, section=sens, query="AVER:COUN?",
                  write="AVER:COUN {value}", minimum=1, maximum=1024, also_read=("avg.auto",)),
            Param("avg.auto", "Усреднения авто", B, section=sens, query="AVER:COUN:AUTO?",
                  write="AVER:COUN:AUTO {value}", also_read=("avg.count",)),
            Param("offset", "Поправка", F, "dB", section=sens, query="CORR:GAIN2?",
                  write="CORR:GAIN2 {value}", minimum=-100.0, maximum=100.0, tolerance=1e-3),
            Param("offset.state", "Поправка вкл.", B, section=sens, query="CORR:GAIN2:STAT?",
                  write="CORR:GAIN2:STAT {value}"),
        ]

    def build_actions(self):
        return [
            Action("single", "Одно измерение"),
            Action("abort", "Отменить ожидание"),
            Action("trigger", "Программный запуск (BUS)"),
            Action("zero", "Обнуление", confirm="Снимите сигнал со входа датчика. Обнулить?"),
            Action("calibrate", "Калибровка"),
            Action("reset", "Сброс *RST", confirm="Сбросить датчик к заводским настройкам?"),
        ]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self._armed = False
        self._results = dict.fromkeys(RESULT_KEYS)
        for n in FEEDS:
            self.write(f"UNIT{n}:POW DBM")
        self._configure_feeds(self.query("DET:FUNC?").strip().upper().startswith("NORM"))
        self.opc_latched = False

    def on_disconnecting(self) -> None:
        if self._armed:
            self.write("ABOR")
            self._armed = False

    def _configure_feeds(self, normal: bool) -> None:
        if normal:
            for n, (_, feed) in FEEDS.items():
                self.write(f'CALC{n}:FEED1 "{feed}"')
        else:
            self.write('CALC1:FEED1 "POW:AVER"')

    @property
    def _normal(self) -> bool:
        return self._values.get("detector") == "NORMAL"

    @property
    def _free_run(self) -> bool:
        return self._values.get("trig.source", "IMM") == "IMM"

    def write_param(self, spec: Param, value: Any) -> None:
        key = spec.key
        if key == "meas.continuous":
            self._continuous = bool(value)
            self._apply_run_mode()
            return
        was_armed = self._armed
        self._abort()                            # настройки меняем при остановленном измерении
        if key == "trig.source" and value == "INT" and not self._normal:
            # запуск по уровню возможен только в режиме NORMal (иначе -221)
            self.write("DET:FUNC NORM")
            self._configure_feeds(True)
            self._values["detector"] = "NORMAL"
        if key == "detector" and value == "AVERAGE" and self._values.get("trig.source") == "INT":
            self.write("TRIG:SOUR IMM")          # обратный конфликт: AVER + INT
            self._values["trig.source"] = "IMM"
        super().write_param(spec, value)
        if key == "detector":
            self._configure_feeds(value == "NORMAL")
        if key in ("trig.source", "detector"):
            self._values[key] = value
            self._results = dict.fromkeys(RESULT_KEYS)
            self._apply_run_mode()
        elif was_armed:
            self._arm()

    def read_param_raw(self, spec: Param) -> Any:
        if spec.key == "meas.continuous":
            return self._continuous
        if spec.key == "meas.armed":
            return self._armed
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        # Результаты не запрашиваются при синхронизации: FETC? без готового измерения
        # даёт -230 "Data corrupt or stale". Отдаём последнее полученное.
        return dict(self._results)

    def _sync_all(self) -> None:
        super()._sync_all()
        self._apply_run_mode()

    # ------------------------------------------------------ цикл измерения
    def _apply_run_mode(self) -> None:
        """Свободный счёт - INIT:CONT ON и чтение при опросе; иначе - взвод по одному."""
        self._abort()
        if self._free_run and self._continuous:
            self.write("INIT:CONT ON")
        else:
            self.write("INIT:CONT OFF")
            if self._continuous:
                self._arm()

    def _arm(self) -> None:
        self.read_esr()                  # сбросить старый OPC
        self.opc_latched = False
        self.write("INIT")
        self.write("*OPC")
        self._set_armed(True)

    def _abort(self) -> None:
        if self._armed:
            self.write("ABOR")
            self._set_armed(False)

    def _set_armed(self, armed: bool) -> None:
        if armed != self._armed or "meas.armed" not in self._values:
            self._armed = armed
            self._publish("meas.armed", armed)

    def _fetch(self) -> None:
        keys = RESULT_KEYS if self._normal else RESULT_KEYS[:1]
        out = dict.fromkeys(RESULT_KEYS)
        for n, (key, _) in FEEDS.items():
            if key in keys:
                out[key] = self.query_float(f"FETC{n}?")
        self._results = out
        for k, v in out.items():
            self._publish(k, v)

    def poll(self) -> None:
        if self._free_run and self._continuous:
            self._fetch()
            return
        if not self._armed:
            return
        esr = self.read_esr()
        if esr & self.ESE_MASK:
            self._check_errors(context="измерение", esr=esr)
        if self.opc_latched:
            self.opc_latched = False
            self._set_armed(False)
            self._fetch()
            if self._continuous:
                self._arm()

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "single":
            self._abort()
            self.write("INIT:CONT OFF")
            if self._free_run:              # IMM: измерение сразу, можно дождаться
                self.write("INIT")
                self.query("*OPC?")
                self._fetch()
            else:
                self._arm()
            return None
        if name == "abort":
            self._abort()
            self.write("ABOR")
            return None
        if name == "trigger":
            self.write("*TRG")
            return None
        if name in ("zero", "calibrate"):
            self._abort()
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
