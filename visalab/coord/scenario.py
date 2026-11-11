"""Сценарии измерений координатора: шаги над несколькими приборами только через ключи профилей.

Сценарий - генератор Python. Он отдаёт (``yield``) шаг и получает обратно его результат::

    def steps(self, run):
        gen, pm = run.device("signal_generator"), run.device("power_meter")
        yield gen.set("rf.frequency", 1e9)            # ждём, пока прибор подтвердит (WRITTEN)
        yield run.sleep(0.3)                          # установление
        p = yield pm.measure("meas.power")            # следующее свежее показание
        yield (gen.set(...), pm.set(...))             # кортеж - шаги параллельно (приборы в своих потоках)

Всё асинхронно на сигналах Qt в GUI-потоке: окна не замирают, приборы работают в своих потоках.
Ошибка шага (прибор ответил ERROR / INVALID, нет ответа за таймаут, связь пропала) или остановка
(``Run.stop``) приходят в генератор исключением :class:`ScenarioError` / :class:`Stopped` - возврат
приборов в исходное состояние пишется в ``finally``. Повторный ``stop`` прерывает и возврат.

Сценарий не знает драйверов: ``run.device(профиль)`` даёт :class:`Device` - прибор класса с ключами
профиля (``visalab/profiles.py``). Какие приборы взять, решает координатор (``Coordinator.start``).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..core.params import ConnectionState, Kind, PStatus
from ..core.scpi import si_format
from .activity import SHORT_NAMES
from .registry import profile_name

log = logging.getLogger(__name__)

__all__ = ["ScenarioError", "Stopped", "Scenario", "Run", "Device", "Step", "SetStep", "MeasureStep",
           "ActionStep", "ReadStep", "SleepStep", "AllStep",
           "SET_TIMEOUT_S", "MEASURE_TIMEOUT_S", "ACTION_TIMEOUT_S"]

SET_TIMEOUT_S = 10.0
MEASURE_TIMEOUT_S = 10.0
ACTION_TIMEOUT_S = 30.0
#: столько ошибок чтения ключа подряд при ожидании показания - отказ (одна - не повод: связь восстанавливается)
MEASURE_ERRORS = 3


class ScenarioError(Exception):
    """Шаг сценария не выполнен (текст - для журнала окна)."""


class Stopped(ScenarioError):
    """Сценарий остановлен пользователем."""


def _fmt(value: Any, unit: str = "") -> str:
    if isinstance(value, float) and unit in ("Hz", "s", "V", "A", "W"):
        return si_format(value, unit, 6)
    if isinstance(value, float):
        return f"{value:g} {unit}".strip()
    return str(value)


# ===================================================================== шаги
class Step:
    """Один асинхронный шаг: ``start(done, fail)`` - потом ровно один вызов done(значение) или fail(текст)."""

    timeout_s: float | None = None

    def __init__(self):
        self._done: Callable[[Any], None] | None = None
        self._fail: Callable[[str], None] | None = None
        self._timer: QTimer | None = None
        self._conns: list[tuple[Any, Callable]] = []
        self.active = False

    def describe(self) -> str:
        return type(self).__name__

    def start(self, done: Callable[[Any], None], fail: Callable[[str], None]) -> None:
        self._done, self._fail, self.active = done, fail, True
        if self.timeout_s:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(lambda: self.fail(f"{self.describe()}: no reply in {self.timeout_s:g} s"))
            self._timer.start(int(self.timeout_s * 1000))
        try:
            self.begin()
        except ScenarioError as exc:
            self.fail(str(exc))

    def begin(self) -> None:
        raise NotImplementedError

    def connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._conns.append((signal, slot))

    def finish(self, value: Any = None) -> None:
        if self.active:
            done = self._done
            self.cancel()
            done(value)

    def fail(self, message: str) -> None:
        if self.active:
            fail = self._fail
            self.cancel()
            fail(message)

    def cancel(self) -> None:
        """Перестать ждать (сам прибору ничего не посылает)."""
        self.active = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        for signal, slot in self._conns:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._conns.clear()


class SleepStep(Step):
    def __init__(self, seconds: float):
        super().__init__()
        self.seconds = max(0.0, float(seconds))

    def describe(self) -> str:
        return f"pause {self.seconds:g} s"

    def begin(self) -> None:
        self._sleep = QTimer()
        self._sleep.setSingleShot(True)
        self._sleep.timeout.connect(lambda: self.finish(None))
        self._sleep.start(int(self.seconds * 1000))

    def cancel(self) -> None:
        sleep = getattr(self, "_sleep", None)
        if sleep is not None:
            sleep.stop()
        super().cancel()


class AllStep(Step):
    """Несколько шагов одновременно; результат - список в том же порядке, первая ошибка - отказ всех."""

    def __init__(self, steps: Iterable[Step]):
        super().__init__()
        self.steps = list(steps)

    def describe(self) -> str:
        return " + ".join(s.describe() for s in self.steps)

    def begin(self) -> None:
        self._left = len(self.steps)
        self._results: list[Any] = [None] * len(self.steps)
        if not self.steps:
            self.finish([])
            return
        for i, s in enumerate(self.steps):
            if not self.active:
                return
            s.start(lambda v, i=i: self._one_done(i, v), self.fail)

    def _one_done(self, i: int, value: Any) -> None:
        self._results[i] = value
        self._left -= 1
        if self._left == 0:
            self.finish(list(self._results))

    def cancel(self) -> None:
        for s in self.steps:
            s.cancel()
        super().cancel()


class DeviceStep(Step):
    """Шаг над одним прибором: отказ сразу, если прибор не подключён или связь пропала."""

    def __init__(self, device: "Device", timeout_s: float | None):
        super().__init__()
        self.device = device
        self.proxy = device.proxy
        self.timeout_s = timeout_s

    def begin(self) -> None:
        if not self.proxy.is_connected:
            raise ScenarioError(f"{self.device.name}: not connected")
        self.connect(self.proxy.state_changed, self._on_state)
        self.begin_device()

    def begin_device(self) -> None:
        raise NotImplementedError

    def _on_state(self, state) -> None:
        if state is not ConnectionState.CONNECTED:
            self.fail(f"{self.device.name}: connection lost")


class SetStep(DeviceStep):
    """Записать значение ключа и дождаться подтверждения прибора (WRITTEN; COERCED - тоже успех,
    результат - фактическое значение). ERROR / INVALID - отказ."""

    def __init__(self, device: "Device", key: str, value: Any, timeout_s: float | None = SET_TIMEOUT_S):
        super().__init__(device, timeout_s)
        self.key, self.value = key, value

    def describe(self) -> str:
        spec = self.proxy.params.get(self.key)
        return f"{self.device.name} {self.key} = {_fmt(self.value, spec.unit if spec else '')}"

    def begin_device(self) -> None:
        self.value = self.device.check(self.key, self.value)
        self.spec = self.proxy.params[self.key]
        self.connect(self.proxy.param_status, self._on_status)
        self.proxy.set(self.key, self.value)

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if key != self.key:
            return
        if status is PStatus.WRITTEN:
            actual = self.proxy.value(key)
            # подтверждение более ранней записи этого ключа (окно, другой шаг) - ждём свою
            if self.spec.same(self.value, actual):
                self.finish(actual)
        elif status is PStatus.COERCED:
            self.finish(self.proxy.value(key))
        elif status in (PStatus.ERROR, PStatus.INVALID):
            self.fail(f"{self.describe()}: {message or status.value}")


class MeasureStep(DeviceStep):
    """Свежее показание: пропустить ``skip`` обновлений ключа (могли быть сняты до изменений),
    затем среднее из ``count`` следующих. Значение None (результата нет) не считается."""

    def __init__(self, device: "Device", key: str, skip: int = 1, count: int = 1,
                 timeout_s: float | None = MEASURE_TIMEOUT_S):
        super().__init__(device, timeout_s)
        self.key, self.skip, self.count = key, max(0, int(skip)), max(1, int(count))

    def describe(self) -> str:
        return f"{self.device.name} {self.key}"

    def begin_device(self) -> None:
        if not self.device.supports(self.key):
            raise ScenarioError(f"{self.device.name}: no {self.key}")
        self._seen, self._values, self._errors = 0, [], 0
        self.connect(self.proxy.param_updated, self._on_value)
        self.connect(self.proxy.param_status, self._on_status)

    def _on_value(self, key: str, value: Any) -> None:
        if key != self.key or value is None:
            return
        self._errors = 0
        self._seen += 1
        if self._seen <= self.skip:
            return
        self._values.append(value)
        if len(self._values) >= self.count:
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in self._values):
                self.finish(sum(self._values) / len(self._values))
            else:
                self.finish(self._values[-1])

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if key == self.key and status is PStatus.ERROR:
            self._errors += 1
            if self._errors >= MEASURE_ERRORS:
                self.fail(f"{self.describe()}: {message or 'read error'}")


class ReadStep(DeviceStep):
    """Прочитать ключ из прибора сейчас."""

    def __init__(self, device: "Device", key: str, timeout_s: float | None = SET_TIMEOUT_S):
        super().__init__(device, timeout_s)
        self.key = key

    def describe(self) -> str:
        return f"{self.device.name} {self.key}?"

    def begin_device(self) -> None:
        if not self.device.supports(self.key):
            raise ScenarioError(f"{self.device.name}: no {self.key}")
        self.connect(self.proxy.param_updated, self._on_value)
        self.connect(self.proxy.param_status, self._on_status)
        self.proxy.read(self.key)

    def _on_value(self, key: str, value: Any) -> None:
        if key == self.key:
            self.finish(value)

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if key == self.key and status is PStatus.ERROR:
            self.fail(f"{self.describe()}: {message or 'read error'}")


class ActionStep(DeviceStep):
    """Действие профиля (single, marker_peak...): ждём его окончания (action_done)."""

    def __init__(self, device: "Device", name: str, args: tuple = (), timeout_s: float | None = ACTION_TIMEOUT_S):
        super().__init__(device, timeout_s)
        self.name, self.args = name, tuple(args)

    def describe(self) -> str:
        return f"{self.device.name} {self.name}"

    def begin_device(self) -> None:
        if self.name not in self.proxy.actions:
            raise ScenarioError(f"{self.device.name}: no action {self.name}")
        self.connect(self.proxy.action_done, self._on_done)
        self.proxy.action(self.name, *self.args)

    def _on_done(self, name: str, result: Any) -> None:
        if name == self.name:
            self.finish(result)


# ===================================================================== прибор
class Device:
    """Абстрактный прибор для сценария: класс (профиль) и его ключи. Драйвер не виден."""

    def __init__(self, proxy, name: str = ""):
        self.proxy = proxy
        self.profile = profile_name(proxy)
        self.name = name or SHORT_NAMES.get(self.profile, "") or proxy.name

    # ----------------------------------------------------------- без обращения к прибору
    @property
    def connected(self) -> bool:
        return self.proxy.is_connected

    def supports(self, key: str) -> bool:
        return self.proxy.supports(key)

    def value(self, key: str, default: Any = None) -> Any:
        """Последнее известное значение (кэш окна)."""
        return self.proxy.value(key, default)

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not self.supports(k)]
        if missing:
            raise ScenarioError(f"{self.name}: no {', '.join(missing)}")

    def check(self, key: str, value: Any) -> Any:
        """Значение, которое прибор примет (тип, диапазон модели), - иначе ScenarioError."""
        spec = self.proxy.params.get(key)
        if spec is None:
            raise ScenarioError(f"{self.name}: no {key}")
        if spec.readonly or spec.kind is Kind.TRACE:
            raise ScenarioError(f"{self.name}: {key} is read-only")
        try:
            return spec.validate(value)
        except (TypeError, ValueError) as exc:
            raise ScenarioError(f"{self.name} {key} = {_fmt(value, spec.unit)}: {exc}") from None

    # ----------------------------------------------------------- шаги
    def set(self, key: str, value: Any, timeout_s: float | None = SET_TIMEOUT_S) -> SetStep:
        return SetStep(self, key, value, timeout_s)

    def measure(self, key: str, skip: int = 1, count: int = 1,
                timeout_s: float | None = MEASURE_TIMEOUT_S) -> MeasureStep:
        return MeasureStep(self, key, skip, count, timeout_s)

    def read(self, key: str, timeout_s: float | None = SET_TIMEOUT_S) -> ReadStep:
        return ReadStep(self, key, timeout_s)

    def action(self, name: str, *args, timeout_s: float | None = ACTION_TIMEOUT_S) -> ActionStep:
        return ActionStep(self, name, args, timeout_s)


# ===================================================================== сценарий и его запуск
class Scenario:
    """Базовый сценарий. ``needs`` - профили приборов; ``steps(run)`` - генератор шагов;
    что вернёт генератор (``return``), станет ``Run.result``."""

    NAME = "SCEN"
    needs: tuple[str, ...] = ()

    def steps(self, run: "Run"):
        raise NotImplementedError
        yield  # noqa: unreachable - это генератор


class Run(QObject):
    """Выполнение сценария. Состояние: running -> done / failed / stopped."""

    RUNNING, DONE, FAILED, STOPPED = "running", "done", "failed", "stopped"

    progress = pyqtSignal(int, int, str)     # сделано, всего, текст
    point = pyqtSignal(object)               # очередная точка данных (dict)
    message = pyqtSignal(str)                # строка для журнала
    ended = pyqtSignal(object)               # Run

    def __init__(self, scenario: Scenario, devices: dict[str, Device], parent=None):
        super().__init__(parent)
        self.scenario = scenario
        self.devices = devices
        self.state = "idle"
        self.error = ""
        self.result: Any = None
        self.done = self.total = 0
        self.text = ""
        self.started_at = 0.0
        self._gen = None
        self._step: Step | None = None
        self._seq = 0
        self._stopping = False
        self._first_error: ScenarioError | None = None

    # ----------------------------------------------------------- для сценария
    @property
    def name(self) -> str:
        return self.scenario.NAME

    @property
    def running(self) -> bool:
        return self.state == self.RUNNING

    @property
    def stopping(self) -> bool:
        return self._stopping

    def device(self, profile: str) -> Device:
        dev = self.devices.get(profile)
        if dev is None:
            raise ScenarioError(f"{self.name}: no {profile}")
        return dev

    def proxies(self) -> list:
        return [d.proxy for d in self.devices.values()]

    @staticmethod
    def sleep(seconds: float) -> SleepStep:
        return SleepStep(seconds)

    @staticmethod
    def all(*steps: Step) -> AllStep:
        return AllStep(steps)

    def report(self, done: int, total: int, text: str = "") -> None:
        self.done, self.total, self.text = int(done), int(total), text
        self.progress.emit(self.done, self.total, text)

    def add_point(self, point: dict) -> None:
        self.point.emit(point)

    def log(self, text: str) -> None:
        log.info("%s: %s", self.name, text)
        self.message.emit(text)

    # ----------------------------------------------------------- управление
    def start(self) -> None:
        if self.state != "idle":
            return
        self.state = self.RUNNING
        self.started_at = time.monotonic()
        try:
            self._gen = self.scenario.steps(self)
        except Exception as exc:  # noqa: BLE001
            self._end(self.FAILED, str(exc))
            return
        self._schedule()

    def stop(self) -> None:
        """Остановить: сценарий получает Stopped и возвращает приборы; второй раз - прервать и возврат."""
        if self.state != self.RUNNING:
            return
        if self._stopping:
            self.abort()
            return
        self._stopping = True
        self._interrupt(Stopped("stopped"))

    def abort(self) -> None:
        """Прервать сразу, без возврата приборов (выход из программы)."""
        if self.state != self.RUNNING:
            return
        self._stopping = True
        self._cancel_step()
        gen, self._gen = self._gen, None
        if gen is not None:
            try:
                gen.close()
            except RuntimeError:            # yield в finally после close
                pass
            except Exception:  # noqa: BLE001
                log.exception("%s: abort", self.name)
        self._end(self.STOPPED, str(self._first_error or "aborted"))

    # ----------------------------------------------------------- движок
    def _cancel_step(self) -> None:
        step, self._step = self._step, None
        if step is not None:
            step.cancel()

    def _interrupt(self, exc: ScenarioError) -> None:
        self._cancel_step()
        self._schedule(None, exc)

    def _schedule(self, value: Any = None, exc: BaseException | None = None) -> None:
        """Продолжить сценарий из цикла событий (не из обработчика сигнала прибора)."""
        self._seq += 1
        seq = self._seq
        QTimer.singleShot(0, lambda: self._resume(seq, value, exc))

    def _resume(self, seq: int, value: Any, exc: BaseException | None) -> None:
        if seq != self._seq or self.state != self.RUNNING or self._gen is None:
            return
        if isinstance(exc, ScenarioError) and self._first_error is None:
            self._first_error = exc
        try:
            step = self._gen.throw(exc) if exc is not None else self._gen.send(value)
        except StopIteration as fin:
            self.result = fin.value
            if self._stopping:
                self._end(self.STOPPED, str(self._first_error or "stopped"))
            else:
                self._end(self.DONE, "")
            return
        except Stopped:
            self._end(self.STOPPED, "stopped")
            return
        except ScenarioError as e:
            first = self._first_error
            msg = str(first) if first is not None and not isinstance(first, Stopped) else str(e)
            self._end(self.STOPPED if isinstance(first, Stopped) else self.FAILED, msg)
            return
        except Exception as e:  # noqa: BLE001 - ошибка в коде сценария
            log.exception("%s: ошибка сценария", self.name)
            self._end(self.FAILED, f"{type(e).__name__}: {e}")
            return
        if isinstance(step, (tuple, list)):
            step = AllStep(step)
        if step is None:
            self._schedule()
            return
        if not isinstance(step, Step):
            self._interrupt(ScenarioError(f"{self.name}: not a step: {step!r}"))
            return
        self._step = step
        step.start(lambda v, s=step: self._step_done(s, v), lambda m, s=step: self._step_failed(s, m))

    def _step_done(self, step: Step, value: Any) -> None:
        if step is self._step:
            self._step = None
            self._schedule(value)

    def _step_failed(self, step: Step, message: str) -> None:
        if step is self._step:
            self._step = None
            self._schedule(None, ScenarioError(message))

    def _end(self, state: str, error: str) -> None:
        self._cancel_step()
        self._seq += 1
        gen, self._gen = self._gen, None
        if gen is not None:
            try:
                gen.close()
            except Exception:  # noqa: BLE001
                pass
        self.state, self.error = state, error
        if state != self.DONE and error:
            log.info("%s: %s (%s)", self.name, state, error)
        self.ended.emit(self)
