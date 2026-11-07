"""Модель окна прибора для фронтенда.

Фронтенд не знает ни драйверов, ни SCPI, ни InstrumentProxy. Окно видит только:

* ``WindowSpec`` - описание окна: поля (ключи профиля класса приборов), действия,
  что вынести в верхнюю панель;
* ``InstrumentModel`` - значения полей и сигналы. Окно просит изменить значение
  (``edit_requested``) или выполнить действие (``action_requested``), а значения
  приходят обратно через ``set_value`` -> ``value_changed``.

Кто стоит за моделью, окну всё равно: сейчас ``DemoModel`` (эхо, без приборов), потом
адаптер к ``InstrumentProxy``. Так интерфейс не зависит от модели прибора.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..core.params import Kind

__all__ = ["Kind", "FieldSpec", "ActionSpec", "WindowSpec", "InstrumentModel", "DemoModel"]


@dataclass(frozen=True)
class FieldSpec:
    """Одно поле окна.

    key      ключ профиля (``ch1.voltage``, ``rf.frequency``) - он же связь с прибором
    group    ``"Вкладка/Группа"`` на полном экране настроек
    """
    key: str
    label: str
    kind: Kind = Kind.FLOAT
    unit: str = ""
    group: str = "General/General"
    choices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    readonly: bool = False
    minimum: float | None = None
    maximum: float | None = None

    @property
    def caption(self) -> str:
        """Подпись вне своей группы: с номером канала (``CH2 Напряжение``)."""
        head, dot, _ = self.key.partition(".")
        if dot and head.startswith("ch") and head[2:].isdigit():
            return f"CH{head[2:]} {self.label}"
        return self.label


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    confirm: str | None = None      # текст подтверждения для опасных действий


@dataclass(frozen=True)
class WindowSpec:
    """Описание окна прибора.

    toggles   BOOL-поля, которые показываются большими кнопками сверху (выходы)
    readouts  поля, которые показываются крупно сверху (измерения, вывод)
    default_selected  что стоит в таблице компактного окна по умолчанию
    """
    id: str
    title: str
    profile: str
    fields: tuple[FieldSpec, ...]
    actions: tuple[ActionSpec, ...] = ()
    toggles: tuple[str, ...] = ()
    readouts: tuple[str, ...] = ()
    default_selected: tuple[str, ...] = ()

    def field(self, key: str) -> FieldSpec:
        return self._index()[key]

    def _index(self) -> dict[str, FieldSpec]:
        idx = self.__dict__.get("_idx")
        if idx is None:
            idx = {f.key: f for f in self.fields}
            object.__setattr__(self, "_idx", idx)
        return idx

    def has(self, key: str) -> bool:
        return key in self._index()


class InstrumentModel(QObject):
    """Значения и сигналы одного окна. Живёт в GUI-потоке."""

    #: окно -> прибор
    edit_requested = pyqtSignal(str, object)
    action_requested = pyqtSignal(str)
    connect_requested = pyqtSignal(bool)
    #: прибор -> окно
    value_changed = pyqtSignal(str, object)
    state_changed = pyqtSignal(str)          # "disconnected" | "connecting" | "connected" | "error"
    message = pyqtSignal(str, bool)          # текст, ошибка?
    #: выбор полей для таблицы компактного окна
    selection_changed = pyqtSignal(tuple)

    def __init__(self, spec: WindowSpec, parent: QObject | None = None):
        super().__init__(parent)
        self.spec = spec
        self.state = "disconnected"
        self._values: dict[str, Any] = {}
        self._selected: tuple[str, ...] = tuple(k for k in spec.default_selected if spec.has(k))

    # ------------------------------------------------------------- значения
    def value(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set_value(self, key: str, value: Any) -> None:
        """Пришло значение со стороны прибора."""
        self._values[key] = value
        self.value_changed.emit(key, value)

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def request(self, key: str, value: Any) -> None:
        """Окно просит записать значение."""
        self.edit_requested.emit(key, value)

    def run(self, action: str) -> None:
        self.action_requested.emit(action)

    # ---------------------------------------------------------------- выбор
    @property
    def selected(self) -> tuple[str, ...]:
        return self._selected

    def set_selected(self, keys) -> None:
        keys = tuple(dict.fromkeys(k for k in keys if self.spec.has(k)))
        if keys != self._selected:
            self._selected = keys
            self.selection_changed.emit(keys)

    def select(self, key: str, on: bool) -> None:
        if on and key not in self._selected:
            self.set_selected(self._selected + (key,))
        elif not on and key in self._selected:
            self.set_selected(tuple(k for k in self._selected if k != key))

    def move(self, key: str, delta: int) -> None:
        keys = list(self._selected)
        i = keys.index(key)
        j = max(0, min(len(keys) - 1, i + delta))
        keys.insert(j, keys.pop(i))
        self.set_selected(keys)


class DemoModel(InstrumentModel):
    """Модель без прибора: принимает любые значения, для просмотра и отладки интерфейса.

    ``initial`` - начальные значения, ``tick`` - функция ``(model) -> None``, которую
    демо вызывает раз в ``tick_ms`` при «подключении» (обновить измерения).
    """

    def __init__(self, spec: WindowSpec, initial: dict[str, Any] | None = None,
                 tick: Callable[["DemoModel"], None] | None = None, tick_ms: int = 500, parent=None):
        super().__init__(spec, parent)
        self._initial = dict(initial or {})
        self._tick = tick
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(lambda: self._tick and self._tick(self))
        self.edit_requested.connect(self._echo)
        self.action_requested.connect(lambda a: self.message.emit(f"action: {a}", False))
        self.connect_requested.connect(self._connect)

    def _connect(self, on: bool) -> None:
        if on:
            self.set_state("connected")
            for k, v in self._initial.items():
                self.set_value(k, v)
            if self._tick:
                self._tick(self)
                self._timer.start()
        else:
            self._timer.stop()
            self.set_state("disconnected")
            for k in list(self._values):
                self.set_value(k, None)
            self._values.clear()

    def _echo(self, key: str, value: Any) -> None:
        if self.state == "connected":
            self.set_value(key, value)
            self.message.emit(f"{key} = {value}", False)
