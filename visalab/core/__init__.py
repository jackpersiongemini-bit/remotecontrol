"""Общий словарь портов - не слой.

То, на чём говорят все порты: типы значений и состояний (:mod:`.params`), контракт класса приборов
(:mod:`.profile`, стандартные профили - :mod:`visalab.profiles`), трасса (:mod:`.trace`) и её запись
в файлы (:mod:`.export`), числа и SI-префиксы (:mod:`.units`). Модули словаря не знают ни о приборах,
ни о соединениях, поэтому ими пользуется любой слой (см. README, «Слои и порты»).
"""
from .params import Action, ConnectionState, Kind, Param, PStatus
from .profile import ActionSlot, Profile, ProfileError, Slot
from .trace import Trace

__all__ = ["Param", "Action", "Trace", "Profile", "Slot", "ActionSlot", "ProfileError", "Kind", "PStatus",
           "ConnectionState"]
