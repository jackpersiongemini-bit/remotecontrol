"""Слой 3: абстрактный прибор.

``InstrumentProxy`` живёт в GUI-потоке, создаёт поток прибора и общается с драйвером в нём только
сигналами Qt (queued connection). Слой выше (координатор) видит прибор только так: ключи и действия
профиля, значения, статусы записи, состояние связи - и никогда не ждёт ответа прибора синхронно.
Какой у прибора драйвер и транспорт, отсюда не видно: прокси получает готовый драйвер из каталога
слоя 2 (:func:`visalab.drivers.create_driver`).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Callable, Iterable

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal

from ..applog import Throttle
from ..core.params import Action, ConnectionState, Param, PStatus
from ..core.trace import Trace
from ..drivers import VISAInstrument, kind_of

log = logging.getLogger(__name__)

__all__ = ["InstrumentProxy"]

_Q = Qt.ConnectionType.QueuedConnection
_SPARAM_RE = re.compile(r"^S([1-4])([1-4])$")


def _norm_sparam(name: str) -> str:
    sp = str(name).strip().upper()
    if not _SPARAM_RE.match(sp):
        raise ValueError(f"некорректный S-параметр: {name!r} (ожидается S11..S44)")
    return sp


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
    _req_read_pause = pyqtSignal(float)
    _req_log_poll = pyqtSignal(bool)
    _req_delay = pyqtSignal(float)
    _req_auto = pyqtSignal(bool)
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
    #: связи нет, прибор переподключится сам через столько секунд; 0 - не ждём (см. ``reconnecting``)
    reconnect_pending = pyqtSignal(float)
    #: завершена развёртка ВАЦ: ``{sparam: Trace(комплексная)}`` для всех активных S-параметров
    sweep_updated = pyqtSignal(dict)

    def __init__(self, driver: VISAInstrument | type[VISAInstrument], transport=None,
                 parent: QObject | None = None, kind: str | None = None, **driver_kwargs):
        """``driver`` - готовый драйвер (из :func:`visalab.drivers.create_driver`). Тестам и отладке:
        ``InstrumentProxy(КлассДрайвера, транспорт, **аргументы драйвера)`` - собрать драйвер здесь."""
        super().__init__(parent)
        if transport is not None:
            driver = driver(transport, **driver_kwargs)
        if driver.parent() is not None:
            driver.setParent(None)
        self._worker = driver
        self.address = driver.address
        #: базовый таймаут драйвера (неизменная копия для GUI-потока без гонки с воркером)
        self._base_timeout_ms = max(int(getattr(driver, "TIMEOUT_MS", 2000) or 2000), driver.timeout_ms)
        #: тип драйвера в каталоге (``e8267d``...) - окну настроек подключения
        self.kind = kind or kind_of(driver) or ""
        self._thread = QThread()
        self._thread.setObjectName(f"{driver.NAME}@{driver.address}")
        self.name = self._worker.NAME
        # Метаданные неизменяемы (frozen dataclass), их безопасно читать из GUI.
        self.params: dict[str, Param] = dict(self._worker.params)
        self.actions: dict[str, Action] = dict(self._worker.actions)
        #: класс прибора и поддерживаемые ключи профиля - то, на что опирается GUI
        self.profile = self._worker.PROFILE
        self.channels = self._worker.channels
        self._default_ports = int(getattr(self._worker, "ports", 0) or 0)
        self.capabilities: frozenset[str] = self._worker.capabilities
        self.state = ConnectionState.DISCONNECTED
        #: связь потеряна, прибор переподключается сам (выключатель окна - отменить: ``close()``)
        self.reconnecting = False
        self.idn = ""
        self._values: dict[str, Any] = {}
        self._channel_traces: dict[tuple[int, str], Trace] = {}
        self._statuses: dict[str, PStatus] = {}
        self._in_flight: set[str] = set()
        self._actions_in_flight = 0
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._status_subs: dict[str, list[Callable]] = defaultdict(list)
        self._throttle = Throttle()          # журнал в файл: одинаковое сообщение - не чаще раза в минуту

        self._worker.moveToThread(self._thread)
        w = self._worker
        self._requests = (
            (self._req_open, w.open_connection), (self._req_close, w.close_connection),
            (self._req_sync, w.sync_all), (self._req_set, w.set_param),
            (self._req_read, w.read_param), (self._req_action, w.run_action),
            (self._req_raw_write, w.raw_write), (self._req_raw_query, w.raw_query),
            (self._req_poll, w.set_poll_interval), (self._req_read_pause, w.set_read_pause),
            (self._req_log_poll, w.set_log_polling),
            (self._req_delay, w.set_command_delay), (self._req_auto, w.set_auto_reconnect),
        )
        for sig, slot in self._requests:
            sig.connect(slot, _Q)
        self._req_shutdown.connect(w.shutdown, _Q)
        self._shut_down = False
        # события воркера -> внутренние обработчики (в GUI-потоке) -> сигналы прокси
        self._worker_conns = [
            (w.state_changed, self._on_state),
            (w.identified, self._on_idn),
            (w.param_updated, self._on_value),
            (w.param_status, self._on_status),
            (w.reconnect_pending, self._on_reconnect),
            (w.instrument_error, self._on_instrument_error),
            (w.comm_error, self._on_comm_error),
            (w.action_done, self._on_action_done),
        ]
        for name in ("scpi_log", "sync_done", "raw_response"):
            self._worker_conns.append((getattr(w, name), getattr(self, name)))
        for sig, slot in self._worker_conns:
            sig.connect(slot, _Q)
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

    _AUTO_SIBLING = {
        "bw.res": "bw.res_auto",
        "bw.video": "bw.video_auto",
        "atten": "atten_auto",
        "sweep.time": "sweep.time_auto",
        "trig.level": "trig.level_auto",
        "avg.count": "avg.auto",
    }

    def should_skip_write(self, key: str, value: Any) -> bool:
        """Не отправлять запись драйверу, если в прокси уже хранится эквивалентное значение."""
        if self._shut_down or not self.is_connected or self._actions_in_flight > 0 or key in self._in_flight:
            return False
        spec = self.params.get(key)
        if spec is None or spec.readonly:
            return False
        current = self._values.get(key)
        if current is None or self._statuses.get(key) in (PStatus.ERROR, PStatus.INVALID):
            return False
        for k in self._in_flight:
            other = self.params.get(k)
            if other is not None and key in other.also_read:
                return False
        if key == "output_all":
            return False                              # действует на все каналы сразу
        auto = self._AUTO_SIBLING.get(key)
        if auto and self._values.get(auto) is True:
            return False                              # ручная запись выключает AUTO
        if key == "mark.x" and self._values.get("mark.state") is not True:
            return False                              # запись X включает маркер
        if key == "marker.freq" and self._values.get("marker.state") is not True:
            return False
        if key == "cal.set" and value and self._values.get("cal.state") is not True:
            return False                              # повторный выбор cal set включает коррекцию
        if key == "s.active":
            wanted = {t.strip().upper() for t in re.split(r"[,\s;]+", str(value or "").strip()) if t.strip()}
            cur_set = {t.strip().upper() for t in re.split(r"[,\s;]+", str(current or "").strip()) if t.strip()}
            return wanted == cur_set
        if key == "s.all_active":
            wanted = {t.strip().upper() for t in re.split(r"[,\s;]+", str(value or "").strip()) if t.strip()}
            cur_set = {t.strip().upper() for t in re.split(r"[,\s;]+", str(current or "").strip()) if t.strip()}
            return wanted == cur_set
        try:
            val = spec.validate(value)
        except (TypeError, ValueError):
            return False
        return spec.same(val, current)

    def set(self, key: str, value: Any) -> None:
        if self.should_skip_write(key, value):
            QTimer.singleShot(
                0,
                lambda k=key: (not self._shut_down and k not in self._in_flight)
                and self._on_status(k, PStatus.WRITTEN, ""),
            )
            return
        self._in_flight.add(key)
        self._req_set.emit(key, value)

    def read(self, key: str) -> None:
        self._req_read.emit(key)

    def action(self, name: str, *args) -> None:
        self._actions_in_flight += 1
        self._req_action.emit(name, args)

    def write_raw(self, command: str) -> None:
        self._req_raw_write.emit(command)

    def query_raw(self, command: str) -> None:
        self._req_raw_query.emit(command)

    def set_poll_interval(self, ms: int) -> None:
        self._req_poll.emit(int(ms))

    def set_read_pause(self, seconds: float) -> None:
        """Пауза между считываниями в непрерывном режиме, с (минимум 50 мс)."""
        self._req_read_pause.emit(float(seconds))

    def set_command_delay(self, seconds: float) -> None:
        """Пауза между командами прибору, с (0 - без паузы)."""
        self._req_delay.emit(float(seconds))

    def set_log_polling(self, on: bool) -> None:
        """Показывать ли в журнале трафик периодического опроса."""
        self._req_log_poll.emit(bool(on))

    def set_auto_reconnect(self, on: bool) -> None:
        """Переподключаться ли самому после обрыва связи (по умолчанию да)."""
        self._req_auto.emit(bool(on))

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

    def unsubscribe(self, key: str, on_value: Callable[[Any], None] | None = None,
                    on_status: Callable[[PStatus, str], None] | None = None) -> None:
        """Снять подписку виджета на ключ."""
        if on_value is not None and key in self._subs:
            self._subs[key] = [cb for cb in self._subs[key] if cb != on_value]
        if on_status is not None and key in self._status_subs:
            self._status_subs[key] = [cb for cb in self._status_subs[key] if cb != on_status]

    # ------------------------------------------------------------ S-параметры ВАЦ
    @property
    def ports(self) -> int:
        """Число физических портов ВАЦ (2 или 4; 0 для остальных приборов)."""
        v = self._values.get("ports")
        return int(v) if isinstance(v, int) and v > 0 else self._default_ports

    def active_sparams(self) -> list[str]:
        """Активные (читаемые) S-параметры ВАЦ в каноническом порядке, например ``['S11', 'S41']``."""
        raw = self._values.get("s.active")
        if isinstance(raw, str):
            return [t.strip().upper() for t in re.split(r"[,\s;]+", raw.strip()) if t.strip()]
        out: list[str] = []
        for r in range(1, 5):
            for c in range(1, 5):
                if self._values.get(f"s{r}{c}.enabled") is True:
                    out.append(f"S{r}{c}")
        return out

    def sparam_trace(self, name: str) -> Trace | None:
        """Последняя комплексная трасса указанного S-параметра (``'S11'``, ``'S41'``...) или ``None``."""
        sp = _norm_sparam(name)
        tr = self._values.get(f"{sp.lower()}.trace")
        return tr if isinstance(tr, Trace) else None

    def sparam_traces(self) -> dict[str, Trace]:
        """Последний снимок комплексных трасс всех активных S-параметров канала ``stim.channel``."""
        out: dict[str, Trace] = {}
        ch = int(self._values.get("stim.channel") or 1)
        for sp in self.active_sparams():
            tr = self._channel_traces.get((ch, sp)) or self._values.get(f"{sp.lower()}.trace")
            if isinstance(tr, Trace):
                out[sp] = tr
        return out

    def channels_list(self) -> list[int]:
        """Существующие каналы ВАЦ, например ``[1, 2]``."""
        raw = self._values.get("channels.list")
        if isinstance(raw, str) and raw.strip():
            chs = sorted({int(t) for t in re.findall(r"\d+", raw)})
            if chs:
                return chs
        cur = self._values.get("stim.channel")
        return [int(cur)] if isinstance(cur, int) and cur >= 1 else [1]

    def channel_trace(self, channel: int, name: str) -> Trace | None:
        """Последняя комплексная трасса пары ``(channel, sparam)`` или ``None``."""
        sp = _norm_sparam(name)
        tr = self._channel_traces.get((int(channel), sp))
        if isinstance(tr, Trace):
            return tr
        if int(channel) == int(self._values.get("stim.channel") or 1):
            t = self._values.get(f"{sp.lower()}.trace")
            return t if isinstance(t, Trace) else None
        return None

    def channel_traces(self) -> dict[tuple[int, str], Trace]:
        """Последний снимок комплексных трасс всех активных пар ``(канал, S-параметр)``."""
        return dict(self._channel_traces)

    def set_channel_sparams(self, pairs: Iterable[tuple[int, str]]) -> None:
        """Задать активные пары ``(канал, S-параметр)`` по всем каналам сразу (через ``s.all_active``)."""
        norm: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()
        for ch, sp in pairs:
            c = int(ch)
            s = _norm_sparam(sp)
            if (c, s) not in seen:
                seen.add((c, s))
                norm.append((c, s))
        norm.sort(key=lambda p: (p[0], int(p[1][1]), int(p[1][2])))
        self.set("s.all_active", ",".join(f"{c}:{s}" for c, s in norm))

    def set_sparams(self, sparams: Iterable[str] | str) -> None:
        """Задать набор S-параметров для измерения и чтения (напр. ``['S11', 'S41']`` или ``'S11,S41'``)."""
        if isinstance(sparams, str):
            items = [t for t in re.split(r"[,\s;]+", sparams.strip()) if t]
        else:
            items = list(sparams)
        norm = [_norm_sparam(s) for s in items]
        ordered = [f"S{r}{c}" for r in range(1, 5) for c in range(1, 5) if f"S{r}{c}" in norm]
        self.set("s.active", ",".join(ordered))

    def enable_sparam(self, name: str, enabled: bool = True) -> None:
        """Включить или выключить измерение и чтение одного S-параметра (напр. ``enable_sparam('S41', True)``)."""
        sp = _norm_sparam(name)
        self.set(f"{sp.lower()}.enabled", bool(enabled))

    def subscribe_sparam(self, name: str, on_value: Callable[[Trace | None], None]) -> None:
        """Подписаться на обновления комплексной трассы S-параметра (напр. ``'S41'``)."""
        sp = _norm_sparam(name)
        self.subscribe(f"{sp.lower()}.trace", on_value)

    def unsubscribe_sparam(self, name: str, on_value: Callable[[Trace | None], None]) -> None:
        """Снять подписку на обновления комплексной трассы S-параметра."""
        sp = _norm_sparam(name)
        self.unsubscribe(f"{sp.lower()}.trace", on_value)

    def request_shutdown(self) -> None:
        """Попросить поток прибора закрыть ресурс и завершиться (не ждать). Запросы после этого
        прибору не уходят: воркер вернётся в GUI-поток, и обмен с прибором там заблокировал бы окна."""
        if self._shut_down:
            return
        self._shut_down = True
        self._worker._stopping = True
        self._req_shutdown.emit()
        for sig, _slot in self._requests:
            try:
                sig.disconnect()
            except TypeError:
                pass

    def shutdown_wait_ms(self) -> int:
        """Сколько ждать поток: он может быть внутри запроса (до таймаута транспорта) и потом ещё
        вернуть прибор (on_disconnecting) - несколько команд."""
        return max(3000, 3 * self._base_timeout_ms + 2000)

    def wait_shutdown(self, timeout_ms: int | None = None) -> None:
        """Дождаться завершения потока. ``terminate()`` - только крайний случай: убитый посреди вызова
        VISA поток может оставить библиотеку VISA (и GIL) занятыми - программа падает или виснет на выходе."""
        if not self._shut_down:
            self.request_shutdown()
        timeout_ms = self.shutdown_wait_ms() if timeout_ms is None else timeout_ms
        # wait() - без проверки isRunning(): тот в Qt 6 ложен, пока поток ещё завершается (isInFinish), а
        # прокси, удалённый в этот момент, ждал бы поток в ~QThread с захваченным GIL.
        if not self._thread.wait(timeout_ms):
            log.warning("%s: поток не завершился за %d мс, принудительно", self.name, timeout_ms)
            self._thread.terminate()
            self._thread.wait(2000)
        for sig, slot in self._worker_conns:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._subs.clear()
        self._status_subs.clear()
        self.reconnecting = False
        self._values.clear()
        self._channel_traces.clear()
        self._statuses.clear()
        self._in_flight.clear()
        self._actions_in_flight = 0
        if self.state is not ConnectionState.DISCONNECTED:
            self.state = ConnectionState.DISCONNECTED
            self.state_changed.emit(ConnectionState.DISCONNECTED)

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
        if self._shut_down and state is not ConnectionState.DISCONNECTED:
            return
        if state is not self.state:
            log.info("%s %s: %s", self.name, self.address, state.value)
        self.state = state
        if state is not ConnectionState.CONNECTED:
            self._values.clear()
            self._statuses.clear()
            self._in_flight.clear()
            self._actions_in_flight = 0
        if state in (ConnectionState.CONNECTED, ConnectionState.DISCONNECTED):
            self.reconnecting = False
        self.state_changed.emit(state)

    def _on_reconnect(self, seconds: float) -> None:
        if self._shut_down:
            return
        self.reconnecting = seconds > 0 and self.state is not ConnectionState.CONNECTED
        self.reconnect_pending.emit(float(seconds))

    def _on_idn(self, idn: str) -> None:
        if self._shut_down:
            return
        self.idn = idn
        log.info("%s %s: *IDN? %s", self.name, self.address, idn)
        self.identified.emit(idn)

    def _on_comm_error(self, message: str) -> None:
        if self._shut_down:
            return
        self._actions_in_flight = 0
        if self._throttle("comm", message):
            log.warning("%s %s: %s", self.name, self.address, message)
        self.comm_error.emit(message)

    def _on_instrument_error(self, code: int, message: str) -> None:
        if self._shut_down:
            return
        self._actions_in_flight = 0
        if self._throttle(f"error {code}", message):
            log.warning("%s %s: ошибка прибора %d: %s", self.name, self.address, code, message)
        self.instrument_error.emit(code, message)

    def _on_action_done(self, name: str, result: Any) -> None:
        if self._shut_down:
            return
        self._actions_in_flight = max(0, self._actions_in_flight - 1)
        self.action_done.emit(name, result)

    def _on_value(self, key: str, value: Any) -> None:
        if self._shut_down:
            return
        self._values[key] = value
        if key.startswith("s") and key.endswith(".trace") and len(key) == 9:
            sp = key[:3].upper()
            if isinstance(value, Trace):
                ch = int((value.meta or {}).get("channel") or self._values.get("stim.channel") or 1)
                self._channel_traces[(ch, sp)] = value
            elif value is None:
                ch = int(self._values.get("stim.channel") or 1)
                self._channel_traces.pop((ch, sp), None)
        elif key == "s.all_active" and isinstance(value, str):
            allowed: set[tuple[int, str]] = set()
            for tok in re.split(r"[,\s;]+", value.strip()):
                if ":" in tok:
                    c_s, _, sp_s = tok.partition(":")
                    if c_s.isdigit():
                        allowed.add((int(c_s), sp_s.upper()))
            for k in [k for k in self._channel_traces if k not in allowed]:
                self._channel_traces.pop(k, None)
        dead: list[Callable] = []
        for cb in list(self._subs.get(key, ())):
            try:
                cb(value)
            except RuntimeError:
                dead.append(cb)
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка в обработчике %s", self.name, key)
        if dead and key in self._subs:
            self._subs[key] = [cb for cb in self._subs[key] if cb not in dead]
        self.param_updated.emit(key, value)
        if key == "meas.count" and self.supports("s.active"):
            self.sweep_updated.emit(self.sparam_traces())

    def _on_status(self, key: str, status: PStatus, message: str) -> None:
        if self._shut_down:
            return
        if status is not PStatus.WRITING:
            self._in_flight.discard(key)
        if status in (PStatus.ERROR, PStatus.INVALID) and self._throttle(f"key {key}", message):
            log.warning("%s %s: %s %s: %s", self.name, self.address, key, status.value, message)
        self._statuses[key] = status
        dead: list[Callable] = []
        for cb in list(self._status_subs.get(key, ())):
            try:
                cb(status, message)
            except RuntimeError:
                dead.append(cb)
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка в обработчике статуса %s", self.name, key)
        if dead and key in self._status_subs:
            self._status_subs[key] = [cb for cb in self._status_subs[key] if cb not in dead]
        self.param_status.emit(key, status, message)
