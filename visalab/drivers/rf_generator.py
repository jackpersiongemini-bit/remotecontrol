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
                 command_delay_s: float | None = None):
        self.model = model or self.DEFAULT_MODEL
        super().__init__(transport, parent, command_delay_s)

    # ------------------------------------------------------------ описание
    def build_params(self):
        slots = self.PROFILE.expand()
        m = self.model
        limits = {"rf.frequency": (m.f_min, m.f_max), "rf.power": (m.p_min, m.p_max),
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
        return params

    def build_actions(self):
        return [Action("all_off", "Выключить ВЧ"),
                Action("reset", "Сброс *RST", confirm="Сбросить генератор к заводским настройкам?")]

    #: взаимоисключающие режимы: прибор отвечает -221 "Settings conflict", если включить
    #: второй при включённом первом. Драйвер сам выключает мешающий режим перед включением.
    EXCLUSIVE = {"fm.state": ("pm.state",), "pm.state": ("fm.state",)}

    # --------------------------------------------------------------- хуки
    def write_param(self, spec: Param, value: Any) -> None:
        if value is True:
            for other in self.EXCLUSIVE.get(spec.key, ()):
                o = self._params.get(other)
                if o is not None and o.write:
                    self.write(o.write.format(value=o.to_scpi(False)))
        super().write_param(spec, value)

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "all_off":
            spec = self._params["rf.output"]
            self.write(spec.write.format(value=spec.to_scpi(False)))
            self._read_keys(["rf.output"])
            return None
        return super().run_custom_action(name, args)
