"""Базовый класс фонового слоя прибора (замена VISAClass).

Принципы
--------
* Один прибор = один объект ``VISAInstrument`` = один собственный ``QThread``.
  Транспорт трогается ТОЛЬКО из этого потока, поэтому блокировки не нужны:
  очередь событий Qt сама сериализует все операции.
* GUI не вызывает методы прибора напрямую. Он работает через
  :class:`~visalab.core.proxy.InstrumentProxy`, который шлёт сигналы-запросы,
  а прибор отвечает сигналами-событиями. Сигналы у каждого экземпляра свои,
  никакой глобальной шины с фильтрацией по адресу.
* Любой слот защищён: исключение не роняет приложение (PyQt6 по умолчанию
  вызывает qFatal на необработанном исключении в слоте), а превращается в сигнал.
* При подключении ничего не сбрасывается (*RST только отдельным действием):
  читаем текущее состояние прибора.
* Ошибки прибора проверяются через стандартную модель IEEE 488.2:
  STB (serial poll) -> бит ESB -> *ESR? -> очередь ошибок прибора.

Как написать драйвер нового прибора
-----------------------------------
1. Унаследоваться от ``VISAInstrument``, задать ``NAME`` и ``PROFILE`` (класс прибора,
   см. ``visalab/profiles.py``). Ключи и действия - только из профиля или ``x.*``.
2. Реализовать ``build_params()`` (список :class:`Param`) и при необходимости
   ``build_actions()``.
3. Если прибор возвращает несколько параметров одним запросом - ``read_group()``.
4. Если перед командой нужно выбрать канал - ``select_channel()``.
5. Если у прибора своя очередь ошибок - ``read_error_queue()``.
6. Написать симулятор в ``visalab/sim`` и тесты.
"""
from __future__ import annotations

import functools
import logging
import time
import traceback
from typing import Any, Callable, Iterable

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from .params import Action, ConnectionState, Param, PStatus
from .profile import Profile
from .scpi import parse_number, strip_response_header
from .transport import Transport, TransportError, TransportTimeout

log = logging.getLogger(__name__)

__all__ = ["VISAInstrument", "InstrumentError", "ESR_BITS"]

#: Биты Standard Event Status Register (IEEE 488.2)
ESR_BITS = {
    0: ("OPC", "операция завершена"),
    1: ("RQC", "запрос управления"),
    2: ("QYE", "ошибка запроса (данные потеряны)"),
    3: ("DDE", "аппаратная ошибка прибора"),
    4: ("EXE", "ошибка исполнения (параметр вне диапазона или недопустим)"),
    5: ("CME", "ошибка команды (синтаксис)"),
    6: ("URQ", "запрос пользователя"),
    7: ("PON", "включение питания"),
}
STB_EAV = 0x04   # в очереди ошибок что-то есть (SCPI-99)
STB_MAV = 0x10   # в выходном буфере есть данные
STB_ESB = 0x20   # сработал разрешённый бит ESR


class InstrumentError(Exception):
    """Прибор сообщил об ошибке в ответ на команду."""


def guarded(fn: Callable) -> Callable:
    """Слот, который никогда не пробрасывает исключение в цикл событий Qt."""

    @functools.wraps(fn)
    def wrapper(self: "VISAInstrument", *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except TransportError as exc:
            self._on_transport_error(exc)
        except Exception as exc:  # noqa: BLE001 - последний рубеж
            log.exception("%s: ошибка в %s", self.NAME, fn.__name__)
            self.comm_error.emit(f"{fn.__name__}: {exc}\n{traceback.format_exc(limit=3)}")
    return wrapper


class VISAInstrument(QObject):
    # ------------------------------------------------------------ метаданные
    NAME: str = "Generic SCPI"
    #: профиль класса приборов (внешний контракт). Драйвер, не соответствующий
    #: профилю, не создаётся (ProfileError) - так «чёрный ящик» проверяется всегда.
    PROFILE: Profile | None = None
    #: прибор повторяет заголовок команды в ответе ("*ESR 1") - снимать его
    STRIP_RESPONSE_HEADER: bool = False
    #: маска *ESE: QYE|DDE|EXE|CME
    ESE_MASK: int = 0x3C
    POLL_INTERVAL_MS: int = 500
    TIMEOUT_MS: int = 2000
    #: сколько подряд неудачных опросов считать обрывом связи
    MAX_POLL_FAILURES: int = 3
    #: проверять ошибки прибора сразу после каждой записи пользователя
    CHECK_AFTER_WRITE: bool = True
    #: минимальная пауза между обращениями к прибору, с (между любыми write/query).
    #: Пауза выдерживается в потоке прибора, GUI при этом не блокируется.
    COMMAND_DELAY_S: float = 0.0
    #: команда SCPI очереди ошибок (None - у прибора её нет)
    ERROR_QUERY: str | None = "SYST:ERR?"

    # ------------------------------------------- сигналы: прибор -> GUI
    state_changed = pyqtSignal(object)            # ConnectionState
    identified = pyqtSignal(str)                  # ответ *IDN?
    param_updated = pyqtSignal(str, object)       # key, значение
    param_status = pyqtSignal(str, object, str)   # key, PStatus, сообщение
    instrument_error = pyqtSignal(int, str)       # ошибка из прибора
    comm_error = pyqtSignal(str)                  # ошибка связи/программы
    scpi_log = pyqtSignal(str, str)               # направление (">>"/"<<"), текст
    action_done = pyqtSignal(str, object)         # имя действия, результат
    sync_done = pyqtSignal()
    raw_response = pyqtSignal(str, str)           # запрос, ответ (консоль)

    def __init__(self, transport: Transport, parent: QObject | None = None,
                 command_delay_s: float | None = None):
        super().__init__(parent)
        self.transport = transport
        self.command_delay_s = self.COMMAND_DELAY_S if command_delay_s is None else float(command_delay_s)
        self._last_io = 0.0
        self._params: dict[str, Param] = {p.key: p for p in self.build_params()}
        self._actions: dict[str, Action] = {a.name: a for a in self.build_actions()}
        if self.PROFILE is not None:
            self.PROFILE.check(self._params, self._actions, self.channels, who=self.NAME)
        self._values: dict[str, Any] = {}
        self._state = ConnectionState.DISCONNECTED
        self._pending: dict[str, Any] = {}
        self._flush_scheduled = False
        self._poll_failures = 0
        self._quiet = False            # не логировать трафик опроса
        self.log_polling = False
        self.idn = ""
        self._timer = QTimer(self)     # переедет в поток вместе с self
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._on_poll)

    # ================================================ описание (переопределять)
    def build_params(self) -> Iterable[Param]:
        return []

    def build_actions(self) -> Iterable[Action]:
        return [Action("reset", "Сброс *RST", confirm="Сбросить прибор к заводским настройкам?")]

    @property
    def channels(self) -> int:
        """Число каналов (для раскрытия ``ch{n}.*`` профиля)."""
        return 1

    @property
    def capabilities(self) -> frozenset[str]:
        """Какие ключи профиля этот прибор поддерживает."""
        if self.PROFILE is None:
            return frozenset()
        return frozenset(self.PROFILE.expand(self.channels)) & frozenset(self._params)

    @property
    def params(self) -> dict[str, Param]:
        return self._params

    @property
    def actions(self) -> dict[str, Action]:
        return self._actions

    @property
    def state(self) -> ConnectionState:
        return self._state

    # ================================================== низкоуровневый обмен
    # Вызывать только из потока прибора (из слотов и хуков).
    def _pace(self) -> None:
        """Выдерживает command_delay_s с момента предыдущего обращения к прибору."""
        if self.command_delay_s > 0:
            wait = self._last_io + self.command_delay_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def write(self, command: str) -> None:
        if not self._quiet:
            self.scpi_log.emit(">>", command)
        self._pace()
        try:
            self.transport.write(command)
        finally:
            self._last_io = time.monotonic()

    def query(self, command: str) -> str:
        if not self._quiet:
            self.scpi_log.emit(">>", command)
        self._pace()
        try:
            answer = self.transport.query(command).strip()
        finally:
            self._last_io = time.monotonic()
        if self.STRIP_RESPONSE_HEADER:
            answer = strip_response_header(answer)
        if not self._quiet:
            self.scpi_log.emit("<<", answer)
        return answer

    def query_float(self, command: str) -> float:
        return parse_number(self.query(command))

    def query_int(self, command: str) -> int:
        return int(round(parse_number(self.query(command))))

    # ======================================================== хуки драйвера
    def on_connected(self) -> None:
        """Настройка после открытия (без *RST). По умолчанию - ничего."""

    def on_disconnecting(self) -> None:
        """Перед закрытием (например, вернуть прибор в LOCAL)."""

    def check_identity(self, idn: str) -> None:
        """Хук после *IDN?. По умолчанию ничего не проверяет: модель задаётся явно в конфиге."""

    def select_channel(self, channel: int) -> None:
        """Выбор канала перед канальной командой (по умолчанию не нужен)."""

    def read_group(self, group: str) -> dict[str, Any]:
        """Читает групповой запрос и возвращает {key: значение}."""
        raise NotImplementedError(f"{self.NAME}: группа {group!r} не реализована")

    def write_param(self, spec: Param, value: Any) -> None:
        """Отправка значения. Переопределить для нестандартных параметров."""
        if spec.channel is not None:
            self.select_channel(spec.channel)
        self.write(spec.write.format(value=spec.to_scpi(value)))

    def read_param_raw(self, spec: Param) -> Any:
        """Чтение одиночного параметра (не группы)."""
        if spec.query is None:
            raise NotImplementedError(f"{spec.key}: нет query")
        if spec.channel is not None:
            self.select_channel(spec.channel)
        return spec.from_scpi(self.query(spec.query))

    def poll(self) -> None:
        """Периодическая задача. По умолчанию читает параметры с poll=True."""
        self._read_keys([k for k, p in self._params.items() if p.poll])

    def read_error_queue(self) -> list[tuple[int, str]]:
        """Вычитывает очередь ошибок SCPI (``SYST:ERR?``) до кода 0."""
        if not self.ERROR_QUERY:
            return []
        errors = []
        for _ in range(32):  # защита от бесконечного цикла
            raw = self.query(self.ERROR_QUERY)
            code_txt, _, msg = raw.partition(",")
            try:
                code = int(parse_number(code_txt))
            except ValueError:
                errors.append((-1, raw))
                break
            if code == 0:
                break
            errors.append((code, msg.strip().strip('"')))
        return errors

    def run_custom_action(self, name: str, args: tuple) -> Any:
        """Действия драйвера. Базовый класс знает только reset."""
        if name == "reset":
            self.write("*RST")
            self.query("*OPC?")
            self._sync_all()
            return None
        raise KeyError(f"{self.NAME}: неизвестное действие {name!r}")

    # ===================================================== слоты (из GUI)
    @pyqtSlot()
    @guarded
    def open_connection(self) -> None:
        if self._state is ConnectionState.CONNECTED:
            return
        self._set_state(ConnectionState.CONNECTING)
        self.transport.timeout_ms = self.TIMEOUT_MS
        self.transport.open()
        try:
            self.transport.clear()
        except TransportError:
            pass  # не все интерфейсы поддерживают device clear
        self.idn = self.query("*IDN?")
        self.check_identity(self.idn)
        self.identified.emit(self.idn)
        self.write("*CLS")
        self.write(f"*ESE {self.ESE_MASK}")
        self.on_connected()
        self._poll_failures = 0
        self._set_state(ConnectionState.CONNECTED)
        self._sync_all()
        self._timer.start()

    @pyqtSlot()
    @guarded
    def close_connection(self) -> None:
        self._close(ConnectionState.DISCONNECTED, graceful=True)

    @pyqtSlot()
    @guarded
    def sync_all(self) -> None:
        self._require_connected()
        self._sync_all()

    @pyqtSlot(str, object)
    @guarded
    def set_param(self, key: str, value: Any) -> None:
        """Запрос записи. Повторные запросы до отправки схлопываются в последний."""
        self._pending[key] = value
        if not self._flush_scheduled:
            self._flush_scheduled = True
            QTimer.singleShot(0, self._flush_pending)

    @pyqtSlot(str)
    @guarded
    def read_param(self, key: str) -> None:
        self._require_connected()
        self._read_keys([key])

    @pyqtSlot(str, object)
    @guarded
    def run_action(self, name: str, args: Any = ()) -> None:
        self._require_connected()
        args = tuple(args) if isinstance(args, (list, tuple)) else (args,)
        result = self.run_custom_action(name, args)
        self._check_errors(context=name)
        self.action_done.emit(name, result)

    @pyqtSlot(str)
    @guarded
    def raw_write(self, command: str) -> None:
        self._require_connected()
        self.write(command)
        self._check_errors(context=command)

    @pyqtSlot(str)
    @guarded
    def raw_query(self, command: str) -> None:
        self._require_connected()
        self.raw_response.emit(command, self.query(command))

    @pyqtSlot(int)
    @guarded
    def set_poll_interval(self, ms: int) -> None:
        self._timer.setInterval(max(50, int(ms)))

    @pyqtSlot(float)
    def set_command_delay(self, seconds: float) -> None:
        self.command_delay_s = max(0.0, float(seconds))

    @pyqtSlot(bool)
    def set_log_polling(self, on: bool) -> None:
        self.log_polling = bool(on)

    @pyqtSlot()
    def shutdown(self) -> None:
        """Закрыть ресурс и завершить поток. Вызывается прокси при закрытии окна."""
        try:
            self._close(ConnectionState.DISCONNECTED, graceful=True)
        except Exception:  # noqa: BLE001
            log.exception("%s: shutdown", self.NAME)
        thread = self.thread()
        if thread is not QThread.currentThread() or thread is None:
            return
        thread.quit()

    # ===================================================== внутренняя логика
    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state)

    def _require_connected(self) -> None:
        if self._state is not ConnectionState.CONNECTED:
            raise TransportError(f"{self.NAME}: прибор не подключён")

    def _close(self, state: ConnectionState, graceful: bool) -> None:
        self._timer.stop()
        self._pending.clear()
        if graceful and self.transport.is_open and self._state is ConnectionState.CONNECTED:
            try:
                self.on_disconnecting()
            except TransportError:
                pass
        self.transport.close()
        for key in self._values:
            self.param_status.emit(key, PStatus.UNKNOWN, "")
        self._values.clear()
        self._set_state(state)

    def _on_transport_error(self, exc: TransportError) -> None:
        self.comm_error.emit(str(exc))
        if self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            self._close(ConnectionState.ERROR, graceful=False)

    def _publish(self, key: str, value: Any) -> None:
        self._values[key] = value
        self.param_updated.emit(key, value)

    def _read_keys(self, keys: Iterable[str]) -> None:
        """Читает набор ключей: каждая группа запрашивается один раз."""
        groups_done: set[str] = set()
        for key in keys:
            spec = self._params[key]
            if spec.group:
                if spec.group in groups_done:
                    continue
                groups_done.add(spec.group)
                result = self.read_group(spec.group)
                for k, p in self._params.items():
                    if p.group != spec.group:
                        continue
                    # Нет в ответе = не применимо в текущем режиме (например DUTY у синуса).
                    v = result.get(k)
                    if v is not None or self._values.get(k) is not None or k not in self._values:
                        self._publish(k, v)
            else:
                try:
                    self._publish(key, self.read_param_raw(spec))
                except ValueError as exc:   # прибор вернул непонятное
                    self.param_status.emit(key, PStatus.ERROR, f"ответ не распознан: {exc}")

    def _sync_all(self) -> None:
        self._read_keys(list(self._params))
        self._check_errors(context="sync")
        self.sync_done.emit()

    def _flush_pending(self) -> None:
        self._flush_scheduled = False
        try:
            self._require_connected()
        except TransportError as exc:
            for key in self._pending:
                self.param_status.emit(key, PStatus.ERROR, str(exc))
            self._pending.clear()
            return
        while self._pending:
            key, value = self._pending.popitem()
            try:
                self._apply(key, value)
            except TransportError as exc:
                self._on_transport_error(exc)
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("%s: запись %s", self.NAME, key)
                self.param_status.emit(key, PStatus.ERROR, str(exc))

    def _apply(self, key: str, value: Any) -> None:
        spec = self._params.get(key)
        if spec is None:
            raise KeyError(f"{self.NAME}: нет параметра {key!r}")
        if spec.readonly:
            self.param_status.emit(key, PStatus.INVALID, "параметр только для чтения")
            return
        try:
            value = spec.validate(value)
        except (ValueError, TypeError) as exc:
            self.param_status.emit(key, PStatus.INVALID, str(exc))
            self._read_keys([key])  # вернуть GUI фактическое значение
            return
        self.param_status.emit(key, PStatus.WRITING, "")
        self.write_param(spec, value)
        errors = self._check_errors(context=key) if self.CHECK_AFTER_WRITE else []
        self._read_keys([key, *spec.also_read])      # обратное чтение
        actual = self._values.get(key)
        if errors:
            self.param_status.emit(key, PStatus.ERROR, "; ".join(m for _, m in errors))
        elif spec.same(value, actual):
            self.param_status.emit(key, PStatus.WRITTEN, "")
        else:
            self.param_status.emit(key, PStatus.COERCED, f"запрошено {value}, установлено {actual}")

    def _check_errors(self, context: str = "") -> list[tuple[int, str]]:
        """*ESR? -> очередь ошибок. Возвращает и публикует найденные ошибки."""
        esr = self.query_int("*ESR?")
        if not esr & self.ESE_MASK:
            return []
        errors = self.read_error_queue()
        if not errors:   # у прибора нет очереди ошибок - расшифровываем ESR
            errors = [(-bit, desc) for bit, (_, desc) in ESR_BITS.items()
                      if esr & self.ESE_MASK & (1 << bit)]
        for code, msg in errors:
            text = f"{msg} [{context}]" if context else msg
            self.instrument_error.emit(code, text)
        return errors

    def _read_status_byte(self) -> int:
        if self.transport.supports_serial_poll:
            return self.transport.read_stb()
        return self.query_int("*STB?")

    @pyqtSlot()
    def _on_poll(self) -> None:
        if self._state is not ConnectionState.CONNECTED:
            return
        self._quiet = not self.log_polling
        try:
            stb = self._read_status_byte()
            if stb & (STB_ESB | STB_EAV):
                self._check_errors(context="опрос")
            self.poll()
            self._poll_failures = 0
        except TransportTimeout as exc:
            self._poll_failures += 1
            if self._poll_failures >= self.MAX_POLL_FAILURES:
                self._on_transport_error(exc)
        except TransportError as exc:
            self._on_transport_error(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: опрос", self.NAME)
            self.comm_error.emit(f"опрос: {exc}")
        finally:
            self._quiet = False
