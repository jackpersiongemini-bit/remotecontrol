"""Проверка стенда: приборы подключаются как в окнах (тот же драйвер, поток, адрес), читаются все
параметры, идёт опрос - итог в отчёт: на что прибор не ответил, что не поддерживает, что не разобрано,
какие ошибки в его очереди (``SYST:ERR?``), сколько заняло подключение и чтение.

В приборы пишется только то, что пишет любое подключение (``*CLS``, ``*ESE``). Запуск - ``run_check.py``.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from PyQt6.QtCore import QCoreApplication, QEventLoop

from .core import ConnectionState, PStatus

__all__ = ["CheckResult", "run_check", "format_report"]

#: часть сообщения драйвера о ключе, которого нет у модели / опции (ошибка команды -1xx)
UNSUPPORTED_MARK = "не поддерживает"
#: длина строки сообщения в отчёте
MESSAGE_CHARS = 240


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 прибор, 2 прибора, 5 приборов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        form = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{n} {form}"


def _short(message: str) -> str:
    text = (message or "").strip().splitlines()[0] if message and message.strip() else "error"
    return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS - 1] + "…"


@dataclass
class CheckResult:
    key: str                                   # запись lab.json (e4446a)
    name: str                                  # драйвер (Agilent E4446A)
    address: str
    idn: str = ""
    connected_s: float | None = None           # от запроса подключения до CONNECTED
    synced_s: float | None = None              # от CONNECTED до конца чтения всех параметров
    params: int = 0                            # параметров прочитано (со значением)
    final_state: str = ""
    poll_s: float = 0.0                        # сколько шёл опрос после чтения
    comm: list[str] = field(default_factory=list)
    instrument_errors: list[str] = field(default_factory=list)
    key_errors: dict[str, str] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    updates: Counter = field(default_factory=Counter)        # обновления ключей во время опроса

    @property
    def status(self) -> str:
        """FAIL - не подключился / потерял связь; WARN - есть замечания; OK."""
        if self.connected_s is None or self.final_state != ConnectionState.CONNECTED.value:
            return "FAIL"
        if self.synced_s is None or self.comm or self.instrument_errors or self.key_errors or self.unsupported:
            return "WARN"
        return "OK"


class _Watch:
    """Сигналы одного прибора -> CheckResult."""

    def __init__(self, proxy, result: CheckResult, t0: float):
        self.proxy, self.r, self.t0 = proxy, result, t0
        self.connected_at: float | None = None
        self.synced_at: float | None = None
        proxy.state_changed.connect(self._state)
        proxy.identified.connect(self._idn)
        proxy.sync_done.connect(self._synced)
        proxy.comm_error.connect(self._comm)
        proxy.instrument_error.connect(self._instrument_error)
        proxy.param_status.connect(self._status)
        proxy.param_updated.connect(self._value)

    @property
    def settled(self) -> bool:
        """Прочитан целиком или подключиться не удалось."""
        return self.synced_at is not None or self.proxy.state is ConnectionState.ERROR

    def _state(self, state) -> None:
        if state is ConnectionState.CONNECTED and self.connected_at is None:
            self.connected_at = time.monotonic()
            self.r.connected_s = self.connected_at - self.t0

    def _idn(self, idn: str) -> None:
        self.r.idn = idn.strip()

    def _synced(self) -> None:
        if self.synced_at is None and self.connected_at is not None:
            self.synced_at = time.monotonic()
            self.r.synced_s = self.synced_at - self.connected_at
            self.r.params = sum(1 for k in self.proxy.params if self.proxy.value(k) is not None)

    def _comm(self, message: str) -> None:
        text = _short(message)
        if text not in self.r.comm:
            self.r.comm.append(text)

    def _instrument_error(self, code: int, message: str) -> None:
        text = f"{code} {_short(message)}"
        if text not in self.r.instrument_errors:
            self.r.instrument_errors.append(text)

    def _status(self, key: str, status, message: str) -> None:
        if status not in (PStatus.ERROR, PStatus.INVALID):
            return
        if UNSUPPORTED_MARK in (message or ""):
            if key not in self.r.unsupported:
                self.r.unsupported.append(key)
        else:
            self.r.key_errors[key] = _short(message)

    def _value(self, key: str, _value) -> None:
        if self.synced_at is not None:
            self.r.updates[key] += 1


def _spin(until, timeout_s: float) -> bool:
    app = QCoreApplication.instance()
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if until():
            return True
        time.sleep(0.01)
    return until()


def run_check(targets, poll_s: float = 5.0, timeout_s: float = 60.0) -> list[CheckResult]:
    """``targets`` - [(запись lab.json, прибор координатора)] (ещё не подключённые). Подключить все сразу (без
    автоподключения: нужна сама ошибка), дождаться чтения всех параметров (не дольше ``timeout_s``),
    опрашивать ``poll_s`` и вернуть результаты. Приборы не закрываются - это делает вызывающий."""
    t0 = time.monotonic()
    watches = []
    for key, proxy in targets:
        w = _Watch(proxy, CheckResult(key, proxy.name, proxy.address), t0)
        watches.append(w)
        proxy.set_auto_reconnect(False)
        proxy.open()
    _spin(lambda: all(w.settled for w in watches), timeout_s)
    started = time.monotonic()
    _spin(lambda: False, poll_s)
    for w in watches:
        w.r.final_state = w.proxy.state.value
        if w.synced_at is not None:
            w.r.poll_s = time.monotonic() - max(started, w.synced_at)
    return [w.r for w in watches]


def format_report(results: list[CheckResult], head: list[str] | tuple = ()) -> str:
    counts = Counter(r.status for r in results)
    lines = [*head, "",
             f"Итог: {plural(len(results), ('прибор', 'прибора', 'приборов'))} - без замечаний {counts['OK']}, "
             f"с замечаниями {counts['WARN']}, не подключились или потеряли связь {counts['FAIL']}"]
    for r in results:
        lines += ["", f"[{r.status}] {r.key}  {r.name}  {r.address}"]
        if r.idn:
            lines.append(f"    *IDN?: {r.idn}")
        if r.connected_s is None:
            lines.append("    не подключился" + (f": {r.comm[0]}" if r.comm else ""))
        else:
            synced = (f"{r.synced_s:.2f} с ({plural(r.params, ('параметр', 'параметра', 'параметров'))})"
                      if r.synced_s is not None
                      else "не закончилось")
            lines.append(f"    подключение {r.connected_s:.2f} с, чтение всех параметров {synced}")
            if r.final_state != ConnectionState.CONNECTED.value:
                lines.append(f"    в конце проверки: {r.final_state} - связь потеряна")
            if r.updates:
                top = ", ".join(f"{k} ×{n}" for k, n in r.updates.most_common(6))
                n = plural(sum(r.updates.values()), ("обновление", "обновления", "обновлений"))
                lines.append(f"    опрос {r.poll_s:.0f} с: {n} ({top})")
        if r.unsupported:
            lines.append("    не поддерживает (нет у модели или опции - драйвер спрашивает зря): "
                         + ", ".join(sorted(r.unsupported)))
        if r.key_errors:
            lines.append("    ошибки параметров:")
            lines += [f"      {k}: {m}" for k, m in sorted(r.key_errors.items())]
        if r.instrument_errors:
            lines.append("    ошибки прибора (SYST:ERR?):")
            lines += [f"      {m}" for m in r.instrument_errors]
        if r.comm and r.connected_s is not None:
            lines.append("    связь:")
            lines += [f"      {m}" for m in r.comm]
    return "\n".join(lines) + "\n"
