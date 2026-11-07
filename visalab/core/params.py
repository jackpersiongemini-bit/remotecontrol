"""Декларативное описание параметров прибора.

Каждый драйвер описывает свои параметры списком :class:`Param`. Из этого описания
базовый класс умеет писать, читать, проверять и синхронизировать параметры, а GUI -
строить окно. Новый прибор = новый список Param + при необходимости пара хуков.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .scpi import format_bool, format_number, parse_bool, parse_number

__all__ = ["PStatus", "Kind", "Param", "Action", "ConnectionState"]


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class PStatus(Enum):
    """Состояние параметра с точки зрения GUI."""
    UNKNOWN = "unknown"      # значение не прочитано
    WRITING = "writing"      # отправлено, ждём подтверждения
    WRITTEN = "written"      # прибор подтвердил запрошенное значение
    COERCED = "coerced"      # прибор принял, но округлил/ограничил значение
    INVALID = "invalid"      # значение отклонено до отправки (диапазон, формат)
    ERROR = "error"          # прибор сообщил об ошибке


class Kind(Enum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"
    STR = "str"


@dataclass(frozen=True)
class Param:
    """Описание одного параметра.

    key      уникальный ключ в рамках прибора, например ``"C1.frequency"``
    label    подпись в GUI
    kind     тип значения
    section  ``"Вкладка/Группа"`` для автопостроения окна
    write    шаблон команды записи с ``{value}``; ``None`` - только чтение
    query    SCPI-запрос чтения (если параметр не входит в group)
    group    имя группового запроса: один запрос возвращает несколько параметров
             (драйвер реализует ``read_group``)
    channel  номер канала, если перед командой нужно выбрать канал
             (драйвер реализует ``select_channel``)
    choices  допустимые значения ENUM - стандартные токены профиля класса приборов
    enum_map перевод стандартного токена в токен прибора: ``(("HIZ", "HZ"), ...)``.
             Чего нет в карте, уходит в прибор как есть. При чтении ответ прибора
             переводится обратно (несколько токенов прибора могут давать один стандартный).
    labels   подписи для choices в GUI
    poll     читать периодически (измерения, состояние CV/CC)
    tolerance относительный допуск при сверке записанного и прочитанного
    """

    key: str
    label: str
    kind: Kind = Kind.FLOAT
    unit: str = ""
    section: str = ""
    write: str | None = None
    query: str | None = None
    group: str | None = None
    channel: int | None = None
    choices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    poll: bool = False
    tolerance: float = 1e-6
    bool_tokens: tuple[str, str] = ("1", "0")   # (on, off) при записи
    #: ключи, которые прибор может изменить при записи этого параметра
    #: (например, включение свипа выключает модуляцию) - перечитываются после записи
    also_read: tuple[str, ...] = ()
    enum_map: tuple[tuple[str, str], ...] = ()
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def readonly(self) -> bool:
        return self.write is None

    # --- преобразования -----------------------------------------------------
    def validate(self, value: Any) -> Any:
        """Приводит значение к типу и проверяет диапазон. ``ValueError`` если нельзя."""
        if self.kind is Kind.FLOAT:
            v = float(value)
        elif self.kind is Kind.INT:
            v = int(round(float(value)))
        elif self.kind is Kind.BOOL:
            v = value if isinstance(value, bool) else parse_bool(value)
        elif self.kind is Kind.ENUM:
            v = str(value).strip().upper()
            if self.choices and v not in (c.upper() for c in self.choices):
                raise ValueError(f"{self.label}: {value!r} не из {self.choices}")
        else:
            v = str(value)
        if self.kind in (Kind.FLOAT, Kind.INT):
            if self.minimum is not None and v < self.minimum:
                raise ValueError(f"{self.label}: {v} < минимума {self.minimum}")
            if self.maximum is not None and v > self.maximum:
                raise ValueError(f"{self.label}: {v} > максимума {self.maximum}")
        return v

    def to_scpi(self, value: Any) -> str:
        if self.kind in (Kind.FLOAT, Kind.INT):
            return format_number(value)
        if self.kind is Kind.BOOL:
            return format_bool(value, *self.bool_tokens)
        if self.kind is Kind.ENUM and self.enum_map:
            token = str(value).upper()
            return next((dev for std, dev in self.enum_map if std == token), str(value))
        return str(value)

    def from_scpi(self, raw: str) -> Any:
        raw = str(raw).strip()
        if self.kind is Kind.FLOAT:
            return parse_number(raw)
        if self.kind is Kind.INT:
            return int(round(parse_number(raw)))
        if self.kind is Kind.BOOL:
            return parse_bool(raw)
        if self.kind is Kind.ENUM:
            token = raw.strip('"').upper()
            for std, dev in self.enum_map:
                if dev.upper() == token:
                    return std
            return token
        return raw.strip('"')

    def same(self, a: Any, b: Any) -> bool:
        """Совпадает ли запрошенное значение с прочитанным."""
        if a is None or b is None:
            return False
        if self.kind is Kind.FLOAT:
            a, b = float(a), float(b)
            return abs(a - b) <= max(abs(a), abs(b), 1e-12) * self.tolerance
        if self.kind in (Kind.ENUM, Kind.STR):
            return str(a).upper() == str(b).upper()
        return a == b


@dataclass(frozen=True)
class Action:
    """Действие прибора без значения (сброс, триггер, снятие защиты)."""
    name: str
    label: str
    confirm: str | None = None   # текст подтверждения в GUI для опасных действий
