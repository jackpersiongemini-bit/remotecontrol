"""Профили классов приборов - внешний контракт «чёрного ящика».

Снаружи (GUI, сценарии измерений) прибор виден ТОЛЬКО через профиль своего класса:
имена ключей, тип значения, единицы (СИ), допустимые токены ENUM и имена действий.
Как драйвер переводит это в SCPI своего прибора - его внутреннее дело. Поэтому
замена E8267D на SMF100A (или SDG на другой генератор) не меняет ни одного ключа
и ни одного сигнала: меняется только драйвер.

Правила (проверяются при создании драйвера, нарушение = :class:`ProfileError`):

* каждый ключ драйвера - либо ключ профиля, либо расширение производителя ``x.*``;
* ключ профиля у драйвера имеет тот же тип, ту же единицу и тот же режим
  (только чтение / запись), ENUM - только токены из профиля;
* обязательные ключи и действия профиля есть у каждого драйвера класса;
* действия - либо из профиля, либо ``x.*``.

Необязательные ключи профиля прибор может не поддерживать - тогда ключа просто нет
(см. ``capabilities``). Но если функция есть, она называется так, как в профиле.

Канальные ключи пишутся с ``{n}``: ``ch{n}.voltage`` -> ``ch1.voltage``, ``ch2.voltage``...
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .params import Action, Kind, Param

__all__ = ["Slot", "ActionSlot", "Profile", "ProfileError", "EXT_PREFIX"]

#: префикс расширений производителя (вне контракта)
EXT_PREFIX = "x."


class ProfileError(Exception):
    """Драйвер не соответствует профилю своего класса."""


@dataclass(frozen=True)
class Slot:
    """Стандартный параметр класса приборов.

    readonly  True - только чтение (измерения), False - запись обязательна,
              None - как позволяет прибор
    choices   стандартные токены ENUM; у драйвера может быть подмножество
    """
    key: str
    kind: Kind
    unit: str = ""
    required: bool = False
    readonly: bool | None = False
    choices: tuple[str, ...] = ()
    doc: str = ""


@dataclass(frozen=True)
class ActionSlot:
    name: str
    required: bool = False
    doc: str = ""


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    slots: tuple[Slot, ...]
    actions: tuple[ActionSlot, ...] = ()
    doc: str = ""

    # ----------------------------------------------------------- раскрытие
    def expand(self, channels: int = 1) -> dict[str, Slot]:
        out: dict[str, Slot] = {}
        for s in self.slots:
            for key in _expand(s.key, channels):
                out[key] = s
        return out

    def expand_actions(self, channels: int = 1) -> dict[str, ActionSlot]:
        out: dict[str, ActionSlot] = {}
        for a in self.actions:
            for name in _expand(a.name, channels):
                out[name] = a
        return out

    def keys(self, channels: int = 1, required_only: bool = False) -> list[str]:
        return [k for k, s in self.expand(channels).items() if s.required or not required_only]

    # ------------------------------------------------------------ проверка
    def problems(self, params: Mapping[str, Param], actions: Mapping[str, Action],
                 channels: int = 1) -> list[str]:
        """Список нарушений контракта (пустой - драйвер соответствует профилю)."""
        slots = self.expand(channels)
        out: list[str] = []
        for key, p in params.items():
            if key.startswith(EXT_PREFIX):
                continue
            s = slots.get(key)
            if s is None:
                out.append(f"{key}: нет в профиле {self.name} (расширения называть {EXT_PREFIX}*)")
                continue
            if p.kind is not s.kind:
                out.append(f"{key}: тип {p.kind.value}, по профилю {s.kind.value}")
            if p.unit != s.unit:
                out.append(f"{key}: единица {p.unit!r}, по профилю {s.unit!r}")
            if s.readonly is not None and p.readonly != s.readonly:
                out.append(f"{key}: {'только чтение' if p.readonly else 'запись'}, "
                           f"по профилю {'только чтение' if s.readonly else 'запись'}")
            if s.kind is Kind.ENUM and s.choices:
                extra = {c.upper() for c in p.choices} - {c.upper() for c in s.choices}
                if extra:
                    out.append(f"{key}: токены {sorted(extra)} не из профиля {s.choices}")
        for key, s in slots.items():
            if s.required and key not in params:
                out.append(f"{key}: обязательный параметр профиля {self.name} отсутствует")
        act_slots = self.expand_actions(channels)
        for name in actions:
            if not name.startswith(EXT_PREFIX) and name not in act_slots:
                out.append(f"действие {name}: нет в профиле {self.name}")
        for name, a in act_slots.items():
            if a.required and name not in actions:
                out.append(f"действие {name}: обязательное по профилю {self.name}")
        return out

    def check(self, params: Mapping[str, Param], actions: Mapping[str, Action], channels: int = 1,
              who: str = "") -> None:
        bad = self.problems(params, actions, channels)
        if bad:
            raise ProfileError(f"{who or 'драйвер'} не соответствует профилю {self.name}:\n  "
                               + "\n  ".join(bad))


def _expand(template: str, channels: int) -> Iterable[str]:
    if "{n}" not in template:
        return (template,)
    return tuple(template.format(n=n) for n in range(1, channels + 1))
