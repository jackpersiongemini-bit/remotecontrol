"""Базовый симулятор SCPI-прибора.

Моделирует то, что важно для проверки драйвера без железа:
* разбор сообщений с ``;``, длинной/короткой формой команд;
* регистры IEEE 488.2 (ESR/ESE/STB/SRE) и очередь ошибок SCPI;
* выходной буфер: чтение без запроса = таймаут, как у реального прибора.
"""
from __future__ import annotations

from collections import deque
from typing import Callable

from ..core.scpi import match_header, parse_number, split_header, split_units

__all__ = ["SimInstrument", "SimError"]

ESR_OPC, ESR_QYE, ESR_DDE, ESR_EXE, ESR_CME, ESR_PON = 1, 4, 8, 16, 32, 128


class SimError(Exception):
    """Ошибка, которую симулятор кладёт в очередь (код SCPI, текст, бит ESR)."""

    def __init__(self, code: int, message: str, esr_bit: int = ESR_EXE):
        super().__init__(message)
        self.code, self.message, self.esr_bit = code, message, esr_bit


Handler = Callable[[str, tuple], "str | None"]


class SimInstrument:
    IDN = "SIM,GENERIC,0,1.0"
    #: команда-заголовок для неизвестной команды
    ERR_UNDEFINED = (-113, "Undefined header")
    ERR_RANGE = (-222, "Data out of range")
    ERR_PARAM = (-224, "Illegal parameter value")

    def __init__(self):
        self.powered = True
        self._out: list[str] = []
        self.errors: deque[tuple[int, str]] = deque(maxlen=20)
        self.esr = ESR_PON
        self.ese = 0
        self.sre = 0
        self.history: list[str] = []    # все принятые единицы (для тестов)
        self._handlers: list[tuple[str, Handler | None, Handler | None]] = []
        self._register_common()
        self.register()
        self.reset()

    # ---------------------------------------------------------- расширение
    def register(self) -> None:
        """Подкласс регистрирует свои команды через :meth:`on`."""

    def reset(self) -> None:
        """Состояние после *RST."""

    def on(self, pattern: str, set_: Handler | None = None, get: Handler | None = None) -> None:
        """Обработчики: ``fn(args: str, groups: tuple) -> str | None``."""
        self._handlers.append((pattern, set_, get))

    def error(self, code: int, message: str, esr_bit: int) -> None:
        self.errors.append((code, message))
        self.esr |= esr_bit

    # --------------------------------------------------------- интерфейс
    def write(self, message: str) -> None:
        self._out.clear()  # новое сообщение отменяет непрочитанный ответ (IEEE 488.2)
        answers = []
        for unit in split_units(message):
            self.history.append(unit)
            ans = self._dispatch(unit)
            if ans is not None:
                answers.append(ans)
        if answers:
            self._out.append(";".join(answers))

    def read(self) -> str | None:
        if not self._out:
            self.error(-420, "Query UNTERMINATED", ESR_QYE)
            return None
        return self._out.pop(0)

    def status_byte(self) -> int:
        stb = 0
        if self.errors:
            stb |= 0x04
        if self._out:
            stb |= 0x10
        if self.esr & self.ese:
            stb |= 0x20
        if stb & self.sre:
            stb |= 0x40
        return stb

    def device_clear(self) -> None:
        self._out.clear()

    # ----------------------------------------------------------- разбор
    def _dispatch(self, unit: str) -> str | None:
        try:
            header, args, is_query = split_header(unit)
        except ValueError:
            self.error(-102, "Syntax error", ESR_CME)
            return None
        for pattern, set_, get in self._handlers:
            m = match_header(pattern, header)
            if not m:
                continue
            fn = get if is_query else set_
            if fn is None:
                self.error(*self.ERR_UNDEFINED, ESR_CME)
                return None
            try:
                return fn(args, m.groups())
            except SimError as exc:
                self.error(exc.code, exc.message, exc.esr_bit)
            except (ValueError, IndexError, KeyError):
                self.error(*self.ERR_PARAM, ESR_EXE)
            return None
        self.error(*self.ERR_UNDEFINED, ESR_CME)
        return None

    # ------------------------------------------------- общие команды 488.2
    def _register_common(self) -> None:
        def esr_q(a, g):
            v, self.esr = self.esr, 0
            return str(v)

        def cls(a, g):
            self.esr = 0
            self.errors.clear()

        def rst(a, g):
            self.reset()

        def opc(a, g):
            self.esr |= ESR_OPC

        self.on("*IDN", get=lambda a, g: self.IDN)
        self.on("*CLS", cls)
        self.on("*ESE", lambda a, g: setattr(self, "ese", int(parse_number(a)) & 0xFF),
                lambda a, g: str(self.ese))
        self.on("*ESR", get=esr_q)
        self.on("*SRE", lambda a, g: setattr(self, "sre", int(parse_number(a)) & 0xBF),
                lambda a, g: str(self.sre))
        self.on("*STB", get=lambda a, g: str(self.status_byte()))
        self.on("*OPC", opc, lambda a, g: "1")
        self.on("*RST", rst)
        self.on("*TST", get=lambda a, g: "0")
        self.on("*WAI", lambda a, g: None)

    # ------------------------------------------------------- помощники
    @staticmethod
    def num(text: str, minimum: float, maximum: float, default: float | None = None) -> float:
        """Число с поддержкой MIN/MAX/DEF; вне диапазона -> SimError -222."""
        t = text.strip().upper()
        if t in ("MIN", "MINIMUM"):
            return minimum
        if t in ("MAX", "MAXIMUM"):
            return maximum
        if t in ("DEF", "DEFAULT"):
            return default if default is not None else minimum
        v = parse_number(text)
        if not minimum <= v <= maximum:
            raise SimError(-222, "Data out of range", ESR_EXE)
        return v

    @staticmethod
    def boolean(text: str) -> bool:
        t = text.strip().upper()
        if t in ("1", "ON"):
            return True
        if t in ("0", "OFF"):
            return False
        raise SimError(-224, "Illegal parameter value", ESR_EXE)
