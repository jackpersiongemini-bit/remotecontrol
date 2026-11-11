"""Транспортный слой: единый интерфейс поверх pyvisa и симуляторов.

Драйвер прибора общается только с :class:`Transport`, поэтому один и тот же драйвер
работает с реальным прибором (:class:`PyVisaTransport`) и без железа
(:class:`SimTransport`). Транспорт не потокобезопасен намеренно: им владеет ровно
один рабочий поток прибора (см. ``VISAInstrument``).
"""
from __future__ import annotations

import logging
import struct
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..sim.base import SimInstrument

log = logging.getLogger(__name__)

__all__ = ["TransportError", "TransportTimeout", "TransportNotSupported", "Transport", "PyVisaTransport", "SimTransport",
           "parse_float_block", "float_block"]


class TransportError(Exception):
    """Ошибка связи (нет прибора, обрыв USB, ошибка VISA)."""


class TransportTimeout(TransportError):
    """Прибор не ответил за отведённое время."""


class TransportNotSupported(TransportError):
    """Интерфейс не умеет эту операцию VISA (VI_ERROR_NSUP_OPER: serial poll, device clear...)."""


def float_block(values) -> str:
    """IEEE 488.2 блок ``#<n><len><данные>`` из float32 big-endian (строка latin-1 - для симуляторов)."""
    data = struct.pack(f">{len(values)}f", *values)
    size = str(len(data))
    return f"#{len(size)}{size}" + data.decode("latin-1")


def parse_float_block(raw: bytes) -> list[float]:
    """Разобрать IEEE 488.2 блок float32 big-endian (``#3400....``)."""
    start = raw.find(b"#")
    if start < 0 or len(raw) < start + 2:
        raise TransportError("нет двоичного блока в ответе")
    ndig = int(raw[start + 1:start + 2])
    size = int(raw[start + 2:start + 2 + ndig])
    data = raw[start + 2 + ndig:start + 2 + ndig + size]
    if len(data) != size or size % 4:
        raise TransportError(f"двоичный блок: ждали {size} байт, пришло {len(data)}")
    return list(struct.unpack(f">{size // 4}f", data))


class Transport(ABC):
    """Минимальный набор операций, нужный базовому классу прибора."""

    address: str = ""
    #: Поддерживает ли интерфейс аппаратный serial poll (USBTMC/GPIB - да, сокет/RS232 - нет).
    supports_serial_poll: bool = False

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def write(self, message: str) -> None: ...

    @abstractmethod
    def read(self) -> str: ...

    def query(self, message: str) -> str:
        self.write(message)
        return self.read()

    def query_float_block(self, message: str) -> list[float]:
        """Запрос с ответом - двоичным блоком float32 big-endian (TRAC:DATA? у U2020X)."""
        self.write(message)
        return parse_float_block(self.read().encode("latin-1"))

    def discard_input(self, wait_ms: int = 200, max_reads: int = 50) -> None:
        """Выбросить ответы, пришедшие с опозданием (после таймаута): читать, пока прибор молчит
        ``wait_ms``. Таймаут транспорта возвращается прежним."""
        old = self.timeout_ms
        self.timeout_ms = wait_ms
        try:
            for _ in range(max_reads):
                try:
                    self.read()
                except TransportTimeout:
                    break
        finally:
            self.timeout_ms = old

    @abstractmethod
    def read_stb(self) -> int: ...

    @abstractmethod
    def clear(self) -> None: ...

    @property
    @abstractmethod
    def timeout_ms(self) -> int: ...

    @timeout_ms.setter
    @abstractmethod
    def timeout_ms(self, value: int) -> None: ...


class PyVisaTransport(Transport):
    """Транспорт через pyvisa.

    ``backend``: ``""`` - системный VISA (NI/Keysight), ``"@py"`` - pyvisa-py,
    ``"path.yaml@sim"`` - pyvisa-sim. ``open_kwargs`` передаются в
    ``open_resource`` (например ``baud_rate`` для ASRL).
    """

    def __init__(self, address: str, backend: str = "", timeout_ms: int = 2000,
                 read_termination: str = "\n", write_termination: str = "\n", **open_kwargs):
        self.address = address
        self._backend = backend
        self._timeout_ms = timeout_ms
        self._read_term = read_termination
        self._write_term = write_termination
        self._open_kwargs = open_kwargs
        self._rm = None
        self._res = None

    # pyvisa импортируется лениво, чтобы симулятор работал и без него
    def open(self) -> None:
        import pyvisa
        from pyvisa import constants
        try:
            self._rm = pyvisa.ResourceManager(self._backend)
            self._res = self._rm.open_resource(
                self.address,
                read_termination=self._read_term,
                write_termination=self._write_term,
                timeout=self._timeout_ms,
                **self._open_kwargs,
            )
        except Exception as exc:  # pyvisa.Error, OSError, ValueError ...
            self.close()
            raise TransportError(f"{self.address}: не удалось открыть: {exc}") from exc
        try:
            # pyvisa по умолчанию декодирует ascii: любой байт > 127 (имя файла, мусор после сбоя) ронял
            # чтение UnicodeDecodeError мимо обработки ошибок связи. latin-1 декодирует любой байт.
            self._res.encoding = "latin-1"
        except AttributeError:
            pass
        itype = getattr(self._res, "interface_type", None)
        rclass = getattr(self._res, "resource_class", "")
        self.supports_serial_poll = (
            itype in (constants.InterfaceType.usb, constants.InterfaceType.gpib, constants.InterfaceType.tcpip)
            and rclass == "INSTR"
        )

    def close(self) -> None:
        """Закрыть только свой ресурс. ResourceManager у pyvisa ОДИН на процесс (на библиотеку VISA):
        закрыть его - оборвать сессии всех остальных приборов («session ... does not support this
        operation» / «invalid session»)."""
        if self._res is not None:
            try:
                self._res.close()
            except Exception as exc:  # закрытие не должно ронять поток
                log.debug("close %s: %s", self.address, exc)
        self._res = None
        self._rm = None

    @property
    def is_open(self) -> bool:
        return self._res is not None

    def _call(self, fn, *args):
        import pyvisa
        if self._res is None:
            raise TransportError(f"{self.address}: ресурс не открыт")
        try:
            return fn(*args)
        except pyvisa.errors.VisaIOError as exc:
            if exc.error_code == pyvisa.constants.StatusCode.error_timeout:
                raise TransportTimeout(f"{self.address}: таймаут") from exc
            if exc.error_code == pyvisa.constants.StatusCode.error_nonsupported_operation:
                raise TransportNotSupported(f"{self.address}: {exc}") from exc
            raise TransportError(f"{self.address}: {exc}") from exc
        except (pyvisa.Error, OSError, UnicodeDecodeError) as exc:
            raise TransportError(f"{self.address}: {exc}") from exc

    def write(self, message: str) -> None:
        self._call(self._res.write, message)

    def read(self) -> str:
        return self._call(self._res.read).strip()

    def query_float_block(self, message: str) -> list[float]:
        return list(self._call(lambda: self._res.query_binary_values(message, datatype="f", is_big_endian=True)))

    def read_stb(self) -> int:
        return int(self._call(self._res.read_stb))

    def discard_input(self, wait_ms: int = 200, max_reads: int = 50) -> None:
        old = self._timeout_ms
        self.timeout_ms = wait_ms
        try:
            for _ in range(max_reads):
                try:
                    self._call(self._res.read_raw)      # байты как есть, без декодирования
                except TransportTimeout:
                    break
        finally:
            self.timeout_ms = old

    def clear(self) -> None:
        self._call(self._res.clear)

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._timeout_ms = int(value)
        if self._res is not None:
            self._res.timeout = self._timeout_ms


class SimTransport(Transport):
    """Транспорт поверх Python-симулятора прибора.

    ``latency_s`` добавляет задержку на каждую операцию, чтобы проверить, что GUI не
    блокируется. ``fail_after`` - после стольких операций связь "рвётся" (для тестов).
    """

    supports_serial_poll = True

    def __init__(self, simulator: "SimInstrument", address: str = "SIM::INSTR",
                 latency_s: float = 0.0, fail_after: int | None = None):
        self.sim = simulator
        self.address = address
        self.latency_s = latency_s
        self.fail_after = fail_after
        self._ops = 0
        self._open = False
        self._timeout_ms = 2000
        #: как ведёт себя реальный прибор, ответивший позже таймаута: запросы, начинающиеся с этих строк,
        #: читаются с таймаутом, а их ответ остаётся в буфере и достаётся следующему чтению (сдвиг)
        self.late: set[str] = set()
        self._late_answers: list[str] = []
        self._last_write = ""

    def _tick(self):
        if not self._open:
            raise TransportError(f"{self.address}: ресурс не открыт")
        self._ops += 1
        if self.fail_after is not None and self._ops > self.fail_after:
            raise TransportError(f"{self.address}: связь потеряна (симуляция)")
        if self.latency_s:
            time.sleep(self.latency_s)

    def open(self) -> None:
        if not self.sim.powered:
            raise TransportError(f"{self.address}: прибор не найден (симуляция)")
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, message: str) -> None:
        self._tick()
        self._last_write = message
        self.sim.write(message)
        # опоздавший ответ уже в буфере интерфейса: прибор отменить его не может
        if self._late_answers:
            self.sim._out[:0] = self._late_answers
            self._late_answers = []

    def read(self) -> str:
        self._tick()
        if any(self._last_write.upper().startswith(p.upper()) for p in self.late):
            self.late = {p for p in self.late if not self._last_write.upper().startswith(p.upper())}
            data = self.sim.read()
            if data is not None:
                self._late_answers.append(data)
            raise TransportTimeout(f"{self.address}: таймаут (ответ опоздал, симуляция)")
        data = self.sim.read()
        if data is None:
            raise TransportTimeout(f"{self.address}: таймаут (нет данных)")
        return data

    def discard_input(self, wait_ms: int = 200, max_reads: int = 50) -> None:
        self._tick()
        self._late_answers = []
        self.sim._out.clear()

    def read_stb(self) -> int:
        self._tick()
        return self.sim.status_byte()

    def clear(self) -> None:
        self._tick()
        self.sim.device_clear()

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._timeout_ms = int(value)
