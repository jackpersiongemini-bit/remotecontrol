"""Прибор, который координатор выдаёт UI: ``Instrument``.

Окно прибора, главное окно, проверка стенда получают прибор только от координатора
(``COORD.open_instrument``) и говорят с ним только через этот объект: ключи и действия профиля,
значения, статусы записи, состояние связи, сигналы Qt - те же, что у абстрактного прибора слоя 3
(:class:`visalab.instruments.InstrumentProxy`), который лежит под ним. Сам ``Instrument`` добавляет то,
ради чего между UI и прибором стоит координатор:

* реестр: прибор, выданный координатором, видят другие окна и сценарии (``COORD.find(профиль)``);
* арбитраж: пока прибор ведёт сценарий (ACAL, перебор частот), запросы UI, которые меняют прибор
  (``set``, ``action``, ``write_raw``, ``query_raw``), в прибор не уходят. Отказ записи виден окну как
  ошибка ключа (``param_status`` ERROR, виджет снова показывает значение прибора), отказ действия или
  команды - сигналом ``refused``. Чтение, синхронизация, подключение и отключение идут как обычно. Сценарий пишет в
  прибор мимо арбитража - через абстрактный прибор (:class:`visalab.coord.scenario.Device`);
* закрытие: ``shutdown`` (или ``COORD.close_instrument``) - убрать из реестра и закрыть поток прибора.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from ..core.params import Action, ConnectionState, Param, PStatus

log = logging.getLogger(__name__)

__all__ = ["Instrument"]


class Instrument(QObject):
    # события прибора - те же, что у абстрактного прибора (слой 3)
    state_changed = pyqtSignal(object)
    identified = pyqtSignal(str)
    param_updated = pyqtSignal(str, object)
    param_status = pyqtSignal(str, object, str)
    instrument_error = pyqtSignal(int, str)
    comm_error = pyqtSignal(str)
    scpi_log = pyqtSignal(str, str)
    action_done = pyqtSignal(str, object)
    sync_done = pyqtSignal()
    raw_response = pyqtSignal(str, str)
    #: связи нет, прибор переподключится сам через столько секунд; 0 - не ждём (см. ``reconnecting``)
    reconnect_pending = pyqtSignal(float)
    #: действие или команда SCPI от UI не отправлены: прибор ведёт сценарий координатора (текст - для журнала
    #: окна); отказ записи ключа приходит как ``param_status`` ERROR этого ключа
    refused = pyqtSignal(str)

    _FORWARDED = ("state_changed", "identified", "instrument_error", "comm_error", "scpi_log", "action_done",
                  "sync_done", "raw_response", "reconnect_pending")

    def __init__(self, proxy, owner=None, parent: QObject | None = None):
        """``proxy`` - абстрактный прибор слоя 3; ``owner`` - координатор, который его выдал (реестр,
        арбитраж). Без ``owner`` - прибор вне координатора: арбитража нет (тестам окон)."""
        super().__init__(parent)
        self._proxy = proxy
        self._owner = owner
        # метаданные неизменяемы - копия при создании
        self.name: str = proxy.name
        self.address: str = proxy.address
        #: тип драйвера в каталоге (``e8267d``...) - окну настроек подключения
        self.kind: str = proxy.kind
        self.profile = proxy.profile
        self.params: dict[str, Param] = proxy.params
        self.actions: dict[str, Action] = proxy.actions
        self.channels = proxy.channels
        self.capabilities: frozenset[str] = proxy.capabilities
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._status_subs: dict[str, list[Callable]] = defaultdict(list)
        for name in self._FORWARDED:
            getattr(proxy, name).connect(getattr(self, name))
        proxy.param_updated.connect(self._on_value)
        proxy.param_status.connect(self._on_status)

    def __repr__(self) -> str:
        return f"<Instrument {self.name} {self.address}>"

    # ------------------------------------------------------------ состояние (кэш в GUI-потоке)
    @property
    def state(self) -> ConnectionState:
        return self._proxy.state

    @property
    def is_connected(self) -> bool:
        return self._proxy.is_connected

    @property
    def reconnecting(self) -> bool:
        """Связь потеряна, прибор переподключается сам (выключатель окна - отменить: ``close()``)."""
        return self._proxy.reconnecting

    @property
    def idn(self) -> str:
        return self._proxy.idn

    def supports(self, key: str) -> bool:
        """Есть ли у прибора этот параметр/действие (стандартный ключ профиля или x.*)."""
        return self._proxy.supports(key)

    def value(self, key: str, default: Any = None) -> Any:
        """Последнее известное значение (без обращения к прибору)."""
        return self._proxy.value(key, default)

    def status(self, key: str) -> PStatus:
        return self._proxy.status(key)

    def subscribe(self, key: str, on_value: Callable[[Any], None],
                  on_status: Callable[[PStatus, str], None] | None = None) -> None:
        """Подписка виджета на один ключ: диспетчеризация словарём, без фильтрации."""
        self._subs[key].append(on_value)
        if on_status:
            self._status_subs[key].append(on_status)

    # ------------------------------------------------------------ арбитраж
    def holder(self):
        """Сценарий координатора, который сейчас ведёт прибор (``Run``), или None."""
        return self._owner.busy(self) if self._owner is not None else None

    def _refused(self, request: str, key: str | None = None) -> bool:
        """Прибор ведёт сценарий - запрос UI не отправлять. True - отказано."""
        run = self.holder()
        if run is None:
            return False
        message = f"не отправлено: прибором управляет {run.name}"
        log.info("%s %s: %s - %s", self.name, self.address, request, message)
        if key is None:
            self.refused.emit(f"{request}: {message}")
            return True
        for cb in self._status_subs.get(key, ()):
            cb(PStatus.ERROR, message)
        self.param_status.emit(key, PStatus.ERROR, message)
        current = self._proxy.value(key)
        if current is not None:                    # виджет снова показывает то, что в приборе
            for cb in self._subs.get(key, ()):
                cb(current)
        return True

    # ------------------------------------------------------------ запросы UI
    def open(self) -> None:
        self._proxy.open()

    def close(self) -> None:
        self._proxy.close()

    def sync(self) -> None:
        self._proxy.sync()

    def read(self, key: str) -> None:
        self._proxy.read(key)

    def set(self, key: str, value: Any) -> None:
        if not self._refused(f"{key} = {value}", key):
            self._proxy.set(key, value)

    def action(self, name: str, *args) -> None:
        if not self._refused(name):
            self._proxy.action(name, *args)

    def write_raw(self, command: str) -> None:
        if not self._refused(command):
            self._proxy.write_raw(command)

    def query_raw(self, command: str) -> None:
        if not self._refused(command):
            self._proxy.query_raw(command)

    def set_poll_interval(self, ms: int) -> None:
        self._proxy.set_poll_interval(ms)

    def set_command_delay(self, seconds: float) -> None:
        """Пауза между командами прибору, с (0 - без паузы)."""
        self._proxy.set_command_delay(seconds)

    def set_log_polling(self, on: bool) -> None:
        """Показывать ли в журнале трафик периодического опроса."""
        self._proxy.set_log_polling(on)

    def set_auto_reconnect(self, on: bool) -> None:
        """Переподключаться ли самому после обрыва связи (по умолчанию да)."""
        self._proxy.set_auto_reconnect(on)

    def shutdown(self, timeout_ms: int | None = None) -> None:
        """Убрать из реестра координатора, закрыть прибор и дождаться его потока."""
        if self._owner is not None:
            self._owner.close_instrument(self, timeout_ms)
        else:
            self._proxy.shutdown(timeout_ms)

    # ------------------------------------------------------------ события прибора
    def _on_value(self, key: str, value: Any) -> None:
        for cb in self._subs.get(key, ()):
            cb(value)
        self.param_updated.emit(key, value)

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        for cb in self._status_subs.get(key, ()):
            cb(status, message)
        self.param_status.emit(key, status, message)
