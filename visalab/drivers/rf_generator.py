"""Общая основа драйверов СВЧ-генераторов (профиль ``signal_generator``).

Всё, что видно снаружи (ключи, подписи, разделы окна, единицы, токены), задаётся
здесь и в профиле одинаково для всех генераторов класса. Конкретный драйвер
(E8267D, SMF100A, ...) задаёт только таблицу ``COMMANDS``: какой SCPI-командой его
прибор пишет и читает каждый стандартный ключ, и при необходимости хуки.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, Kind, Param
from ..profiles import SIGNAL_GENERATOR

__all__ = ["RFSignalGenerator", "RFGenModel", "LABELS"]


@dataclass(frozen=True)
class RFGenModel:
    name: str
    f_min: float = 100e3      # Гц
    f_max: float = 44e9
    p_min: float = -145.0     # дБм (мягкие пределы: окончательно ограничивает прибор)
    p_max: float = 30.0


#: стандартные подписи и разделы окна - одинаковы для любого генератора класса
LABELS: dict[str, tuple[str, str]] = {
    "rf.output": ("Выход ВЧ", "ВЧ/Выход"),
    "rf.frequency": ("Частота", "ВЧ/Выход"),
    "rf.power": ("Мощность", "ВЧ/Выход"),
    "power.offset": ("Поправка мощности", "ВЧ/Калибровка"),
    "power.offset_state": ("Калибровка мощности", "ВЧ/Калибровка"),
    "rf.alc": ("ALC (АРУ)", "ВЧ/Выход"),
    "ref.source": ("Опора 10 МГц", "ВЧ/Опора"),
    "mod.output": ("Модуляция (общий)", "Модуляция/Общее"),
    "am.state": ("AM", "Модуляция/AM"),
    "am.depth": ("Глубина", "Модуляция/AM"),
    "am.source": ("Источник", "Модуляция/AM"),
    "am.freq": ("Частота НЧ", "Модуляция/AM"),
    "fm.state": ("FM", "Модуляция/FM"),
    "fm.deviation": ("Девиация", "Модуляция/FM"),
    "fm.source": ("Источник", "Модуляция/FM"),
    "fm.freq": ("Частота НЧ", "Модуляция/FM"),
    "pm.state": ("ФМ (ΦM)", "Модуляция/ΦM"),
    "pm.deviation": ("Девиация", "Модуляция/ΦM"),
    "pm.source": ("Источник", "Модуляция/ΦM"),
    "pm.freq": ("Частота НЧ", "Модуляция/ΦM"),
    "pulse.state": ("Импульсная мод.", "Импульс/Импульсная модуляция"),
    "pulse.source": ("Источник", "Импульс/Импульсная модуляция"),
    "pulse.period": ("Период", "Импульс/Импульсная модуляция"),
    "pulse.width": ("Длительность", "Импульс/Импульсная модуляция"),
}


class RFSignalGenerator(VISAInstrument):
    PROFILE = SIGNAL_GENERATOR
    DEFAULT_MODEL = RFGenModel("generic")
    #: стандартный ключ -> (шаблон записи с {value} или None, запрос чтения, доп. поля Param)
    COMMANDS: dict[str, tuple[str | None, str, dict]] = {}

    def __init__(self, transport, model: RFGenModel | None = None, parent=None,
                 command_delay_s: float | None = None, read_pause_s: float | None = None):
        self.model = model or self.DEFAULT_MODEL
        self._power_offset = 0.0
        self._power_offset_state = False
        super().__init__(transport, parent, command_delay_s, read_pause_s)

    def _active_power_offset(self) -> float:
        return self._power_offset if self._power_offset_state else 0.0

    # ------------------------------------------------------------ описание
    def build_params(self):
        slots = self.PROFILE.expand()
        m = self.model
        limits = {"rf.frequency": (m.f_min, m.f_max),
                  # статические пределы шире на ±100 дБ; фактические с учётом поправки проверяет validate_param()
                  "rf.power": (m.p_min - 100.0, m.p_max + 100.0),
                  "am.depth": (0.0, 100.0)}
        params = []
        for key, (write, query, extra) in self.COMMANDS.items():
            extra = dict(extra)
            if key.startswith("x."):
                params.append(Param(key, extra.pop("label", key), write=write, query=query, **extra))
                continue
            slot = slots[key]
            label, section = LABELS[key]
            kw: dict[str, Any] = {"kind": slot.kind, "unit": slot.unit, "section": section}
            if slot.kind is Kind.ENUM:
                kw["choices"] = slot.choices
            if key in limits:
                kw["minimum"], kw["maximum"] = limits[key]
            elif slot.kind is Kind.FLOAT:
                kw["minimum"] = 0.0
            if slot.kind is Kind.FLOAT:
                kw["tolerance"] = 1e-9 if key == "rf.frequency" else 1e-4
            kw.update(extra)
            params.append(Param(key, label, write=write, query=query, **kw))
        if "rf.power" in self.COMMANDS:
            off_label, off_sec = LABELS["power.offset"]
            st_label, st_sec = LABELS["power.offset_state"]
            params += [
                Param("power.offset", off_label, Kind.FLOAT, unit="dB", section=off_sec,
                      query="<virtual>", write="<virtual>", minimum=-100.0, maximum=100.0,
                      tolerance=1e-3, also_read=("rf.power",)),
                Param("power.offset_state", st_label, Kind.BOOL, section=st_sec,
                      query="<virtual>", write="<virtual>", also_read=("rf.power",)),
            ]
        return params

    def build_actions(self):
        return [Action("all_off", "Выключить ВЧ"),
                Action("reset", "Сброс *RST", confirm="Сбросить генератор к заводским настройкам?")]

    #: взаимоисключающие режимы: прибор отвечает -221 "Settings conflict", если включить
    #: второй при включённом первом. Драйвер сам выключает мешающий режим перед включением.
    EXCLUSIVE = {"fm.state": ("pm.state",), "pm.state": ("fm.state",)}

    # --------------------------------------------------------------- хуки
    def on_closed(self) -> None:
        self._power_offset = 0.0
        self._power_offset_state = False

    def validate_param(self, spec: Param, value: Any) -> Any:
        val = super().validate_param(spec, value)
        if spec.key == "rf.power":
            off = self._active_power_offset()
            lo, hi = self.model.p_min - off, self.model.p_max - off
            if val < lo - 1e-9:
                raise ValueError(f"{spec.key}: минимум {lo:g} dBm (прибор {self.model.p_min:g} dBm, поправка {off:+.2f} dB)")
            if val > hi + 1e-9:
                raise ValueError(f"{spec.key}: максимум {hi:g} dBm (прибор {self.model.p_max:g} dBm, поправка {off:+.2f} dB)")
        return val

    def read_param_raw(self, spec: Param) -> Any:
        if spec.key == "power.offset":
            return self._power_offset
        if spec.key == "power.offset_state":
            return self._power_offset_state
        val = super().read_param_raw(spec)
        if spec.key == "rf.power" and isinstance(val, (int, float)):
            return float(val) - self._active_power_offset()
        return val

    def write_param(self, spec: Param, value: Any) -> None:
        if spec.key == "power.offset_state":
            # Включение / выключение поправки не меняет мощность на разъёме прибора (безопасно для DUT и датчика)
            self._power_offset_state = bool(value)
            return
        if spec.key == "power.offset":
            old_off = self._active_power_offset()
            self._power_offset = float(value)
            new_off = self._active_power_offset()
            cur = self._values.get("rf.power")
            if self._power_offset_state and abs(new_off - old_off) > 1e-6 and isinstance(cur, (int, float)) \
                    and "rf.power" in self._params:
                raw = max(self.model.p_min, min(self.model.p_max, float(cur) + new_off))
                super().write_param(self._params["rf.power"], raw)
            return
        if spec.key == "rf.power":
            super().write_param(spec, float(value) + self._active_power_offset())
            return
        if value is True:
            for other in self.EXCLUSIVE.get(spec.key, ()):
                o = self._params.get(other)
                if o is not None and o.write and other not in self._no_answer and self._values.get(other) is not False:
                    self.write(o.write.format(value=o.to_scpi(False)))
        super().write_param(spec, value)

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "all_off":
            spec = self._params["rf.output"]
            self.write(spec.write.format(value=spec.to_scpi(False)))
            self._read_keys(["rf.output"])
            return None
        return super().run_custom_action(name, args)
