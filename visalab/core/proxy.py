"""GUI-сторона прибора.

``InstrumentProxy`` живёт в GUI-потоке, создаёт поток прибора и общается с ним
только сигналами Qt (queued connection). GUI подключается к сигналам прокси, а не
воркера, и никогда не ждёт ответа прибора синхронно.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal

from .instrument import VISAInstrument
from .params import Action, ConnectionState, Param, PStatus
from .transport import Transport

log = logging.getLogger(__name__)

__all__ = ["InstrumentProxy"]

_Q = Qt.ConnectionType.QueuedConnection


class InstrumentProxy(QObject):
    # ------------------------------------------- запросы GUI -> прибор
    _req_open = pyqtSignal()
    _req_close = pyqtSignal()
    _req_sync = pyqtSignal()
    _req_set = pyqtSignal(str, object)
    _req_read = pyqtSignal(str)
    _req_action = pyqtSignal(str, object)
    _req_raw_write = pyqtSignal(str)
    _req_raw_query = pyqtSignal(str)
    _req_poll = pyqtSignal(int)
    _req_log_poll = pyqtSignal(bool)
    _req_delay = pyqtSignal(float)
    _req_shutdown = pyqtSignal()

    # ------------------------------------------- события прибор -> GUI
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

    def __init__(self, driver_cls: type[VISAInstrument], transport: Transport,
                 parent: QObject | None = None, **driver_kwargs):
        super().__init__(parent)
        self.address = transport.address
        self.driver_cls = driver_cls                      # для окна настроек подключения
        self._thread = QThread()
        self._thread.setObjectName(f"{driver_cls.NAME}@{transport.address}")
        self._worker = driver_cls(transport, **driver_kwargs)
        self.name = self._worker.NAME
        # Метаданные неизменяемы (frozen dataclass), их безопасно читать из GUI.
        self.params: dict[str, Param] = dict(self._worker.params)
        self.actions: dict[str, Action] = dict(self._worker.actions)
        #: класс прибора и поддерживаемые ключи профиля - то, на что опирается GUI
        self.profile = self._worker.PROFILE
        self.channels = self._worker.channels
        self.capabilities: frozenset[str] = self._worker.capabilities
        self.state = ConnectionState.DISCONNECTED
        self.idn = ""
        self._values: dict[str, Any] = {}
        self._statuses: dict[str, PStatus] = {}
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._status_subs: dict[str, list[Callable]] = defaultdict(list)

        self._worker.moveToThread(self._thread)
        w = self._worker
        self._requests = (
            (self._req_open, w.open_connection), (self._req_close, w.close_connection),
            (self._req_sync, w.sync_all), (self._req_set, w.set_param),
            (self._req_read, w.read_param), (self._req_action, w.run_action),
            (self._req_raw_write, w.raw_write), (self._req_raw_query, w.raw_query),
            (self._req_poll, w.set_poll_interval), (self._req_log_poll, w.set_log_polling),
            (self._req_delay, w.set_command_delay),
        )
        for sig, slot in self._requests:
            sig.connect(slot, _Q)
        self._req_shutdown.connect(w.shutdown, _Q)
        self._shut_down = False
        # события воркера -> внутренние обработчики (в GUI-потоке) -> сигналы прокси
        w.state_changed.connect(self._on_state, _Q)
        w.identified.connect(self._on_idn, _Q)
        w.param_updated.connect(self._on_value, _Q)
        w.param_status.connect(self._on_status, _Q)
        for name in ("instrument_error", "comm_error", "scpi_log", "action_done",
                     "sync_done", "raw_response"):
            getattr(w, name).connect(getattr(self, name), _Q)
        # Воркер НЕ удаляется через finished -> deleteLater: тогда его (и QTimer) удалял бы поток прибора,
        # которому для этого нужен GIL, а GUI-поток в ~QThread ждёт этот поток, держа GIL, - взаимная
        # блокировка (программа виснет на выходе или при смене адреса). Воркером владеет Python; при закрытии
        # он сам возвращается в GUI-поток (VISAInstrument.shutdown) и удаляется вместе с прокси.
        self._thread.start()

    # ------------------------------------------------------------ API GUI
    def open(self) -> None:
        self._req_open.emit()

    def close(self) -> None:
        self._req_close.emit()

    def sync(self) -> None:
        self._req_sync.emit()

    def set(self, key: str, value: Any) -> None:
        self._req_set.emit(key, value)

    def read(self, key: str) -> None:
        self._req_read.emit(key)

    def action(self, name: str, *args) -> None:
        self._req_action.emit(name, args)

    def write_raw(self, command: str) -> None:
        self._req_raw_write.emit(command)

    def query_raw(self, command: str) -> None:
        self._req_raw_query.emit(command)

    def set_poll_interval(self, ms: int) -> None:
        self._req_poll.emit(int(ms))

    def set_command_delay(self, seconds: float) -> None:
        """Пауза между командами прибору, с (0 - без паузы)."""
        self._req_delay.emit(float(seconds))

    def set_log_polling(self, on: bool) -> None:
        """Показывать ли в журнале трафик периодического опроса."""
        self._req_log_poll.emit(bool(on))

    def supports(self, key: str) -> bool:
        """Есть ли у прибора этот параметр/действие (стандартный ключ профиля или x.*)."""
        return key in self.params or key in self.actions

    def value(self, key: str, default: Any = None) -> Any:
        """Последнее известное значение (кэш в GUI-потоке, без обращения к прибору)."""
        return self._values.get(key, default)

    def status(self, key: str) -> PStatus:
        return self._statuses.get(key, PStatus.UNKNOWN)

    def subscribe(self, key: str, on_value: Callable[[Any], None],
                  on_status: Callable[[PStatus, str], None] | None = None) -> None:
        """Подписка виджета на один ключ: диспетчеризация словарём, без фильтрации."""
        self._subs[key].append(on_value)
        if on_status:
            self._status_subs[key].append(on_status)

    def request_shutdown(self) -> None:
        """Попросить поток прибора закрыть ресурс и завершиться (не ждать). Запросы после этого
        прибору не уходят: воркер вернётся в GUI-поток, и обмен с прибором там заблокировал бы окна."""
        if self._shut_down:
            return
        self._shut_down = True
        self._req_shutdown.emit()
        for sig, _slot in self._requests:
            try:
                sig.disconnect()
            except TypeError:
                pass

    def shutdown_wait_ms(self) -> int:
        """Сколько ждать поток: он может быть внутри запроса (до таймаута транспорта) и потом ещё
        вернуть прибор (on_disconnecting) - несколько команд."""
        timeout = int(getattr(self._worker.transport, "timeout_ms", 2000) or 2000)
        return max(3000, 3 * timeout + 2000)

    def wait_shutdown(self, timeout_ms: int | None = None) -> None:
        """Дождаться завершения потока. ``terminate()`` - только крайний случай: убитый посреди вызова
        VISA поток может оставить библиотеку VISA (и GIL) занятыми - программа падает или виснет на выходе."""
        timeout_ms = self.shutdown_wait_ms() if timeout_ms is None else timeout_ms
        # wait() - без проверки isRunning(): тот в Qt 6 ложен, пока поток ещё завершается (isInFinish), а
        # прокси, удалённый в этот момент, ждал бы поток в ~QThread с захваченным GIL.
        if not self._thread.wait(timeout_ms):
            log.warning("%s: поток не завершился за %d мс, принудительно", self.name, timeout_ms)
            self._thread.terminate()
            self._thread.wait(2000)
        self.state = ConnectionState.DISCONNECTED   # события из очереди могут прийти позже

    def shutdown(self, timeout_ms: int | None = None) -> None:
        """Корректно закрыть прибор и дождаться завершения потока."""
        self.request_shutdown()
        self.wait_shutdown(timeout_ms)

    @staticmethod
    def shutdown_all(proxies) -> None:
        """Закрыть несколько приборов параллельно: сначала всем запрос, потом ждать каждого
        (иначе на выходе время закрытия приборов складывается)."""
        proxies = list(proxies)
        for p in proxies:
            p.request_shutdown()
        for p in proxies:
            p.wait_shutdown()

    @property
    def is_connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    # ------------------------------------------------------ внутреннее
    def _on_state(self, state: ConnectionState) -> None:
        self.state = state
        if state is not ConnectionState.CONNECTED:
            self._values.clear()
        self.state_changed.emit(state)

    def _on_idn(self, idn: str) -> None:
        self.idn = idn
        self.identified.emit(idn)

    def _on_value(self, key: str, value: Any) -> None:
        self._values[key] = value
        for cb in self._subs.get(key, ()):
            cb(value)
        self.param_updated.emit(key, value)

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        self._statuses[key] = status
        for cb in self._status_subs.get(key, ()):
            cb(status, message)
        self.param_status.emit(key, status, message)
