"""Координатор: единая точка, через которую UI работает с приборами.

UI <> координатор <> абстрактные приборы (профили, InstrumentProxy) <> драйверы <> pyvisa.

Порт для UI (входной порт слоя):

* приборы: ``open_instrument(тип, адрес)`` даёт :class:`~visalab.coord.instrument.Instrument` - прибор,
  с которым работает окно; ``close_instrument`` / ``close_all`` - закрыть; реестр ``links`` и
  ``find(профиль)`` - приборы процесса по профилю;
* каталог драйверов для окна настроек подключения - транзитом из слоя 3: ``kinds``, ``default_delay``,
  ``models``, ``model_class``, ``list_resources``;
* сценарии измерений (:mod:`visalab.coord.scenario`): ``start(сценарий)`` подбирает приборы по
  профилям, запускает и следит, чтобы один прибор не вели два сценария сразу (``busy``); пока прибор
  ведёт сценарий, запросы окна, которые меняют прибор, в него не уходят (арбитраж ``Instrument``);
* при выходе из программы - остановить сценарии и дать им вернуть приборы (``stop_all``);
* прибор, с которым пропала связь во время сценария: что сценарий не успел вернуть (выключить выход ВЧ),
  координатор записывает, когда прибор снова подключится (``pending_restore``).

Выходной порт слоя - порт абстрактных приборов (:mod:`visalab.instruments`). Окна знают только
координатор и ключи профилей; какой драйвер у прибора, координатору не важно.
"""
from __future__ import annotations

import logging
import time

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, pyqtSignal

from .. import instruments as _instruments
from ..core.params import ConnectionState
from ..profiles import PROFILES
from .instrument import Instrument
from .registry import LINKS, ProxyLinks
from .scenario import Device, Run, Scenario, ScenarioError

log = logging.getLogger(__name__)

__all__ = ["Coordinator", "COORD"]

#: закрыть прибор, который ведёт сценарий (сменили адрес посреди ACAL): столько ждать, пока сценарий вернёт приборы
CLOSE_SCENARIO_WAIT_MS = 5000


class Coordinator(QObject):
    run_started = pyqtSignal(object)        # Run
    run_progress = pyqtSignal(object)       # Run: изменились done / total / text
    run_ended = pyqtSignal(object)          # Run (state: done / failed / stopped)

    def __init__(self, links: ProxyLinks = LINKS, parent=None):
        super().__init__(parent)
        self.links = links
        self._runs: list[Run] = []
        self._deferred: dict = {}           # прибор -> ([(ключ, значение, сценарий)], слот state_changed)

    # ------------------------------------------------------------ каталог драйверов (транзитом из слоя 3)
    @staticmethod
    def kinds(profile: str | None = None) -> list[str]:
        """Типы драйверов (``e8267d``...) - все или одного класса приборов (``signal_generator``...)."""
        return _instruments.kinds(profile)

    @staticmethod
    def default_delay(kind: str) -> float:
        """Пауза между командами по умолчанию для драйвера, с."""
        return _instruments.default_delay(kind)

    @staticmethod
    def models(kind: str) -> dict:
        """Встроенные модели драйвера (пределы и функции): {имя: модель}."""
        return _instruments.models(kind)

    @staticmethod
    def model_class(kind: str):
        """Класс модели драйвера (по нему окно строит форму своей модели) или None."""
        return _instruments.model_class(kind)

    @staticmethod
    def list_resources(backend: str = "") -> list[str]:
        """Какие приборы видит VISA."""
        return _instruments.list_resources(backend)

    # ------------------------------------------------------------ приборы
    def open_instrument(self, kind: str, address: str | None, model=None, delay: float | None = None,
                        backend: str = "", latency: float = 0.0) -> Instrument:
        """Прибор по типу драйвера (``e8267d``...) и адресу (VISA, IP, IP:порт, ``sim``) - в реестре, ещё не
        подключён (``open()``). ``model`` - имя встроенной модели, своя модель или None (по *IDN?);
        ``delay`` - пауза между командами, с (None - по умолчанию драйвера); ``latency`` - задержка симулятора."""
        return self.attach(_instruments.open_instrument(kind, address, model=model, delay=delay, backend=backend,
                                                        latency=latency))

    def attach(self, proxy) -> Instrument:
        """Выдать UI уже созданный абстрактный прибор (слой 3): обернуть и внести в реестр."""
        inst = Instrument(proxy, owner=self)
        self.links.add(inst)
        return inst

    def close_instrument(self, inst: Instrument, timeout_ms: int | None = None) -> None:
        """Убрать прибор из реестра, закрыть и дождаться его потока."""
        self.close_all([inst], timeout_ms)

    def close_all(self, instruments=None, timeout_ms: int | None = None) -> None:
        """Закрыть приборы (по умолчанию - все в реестре) параллельно: сначала всем запрос, потом ждать
        каждого (иначе на выходе время закрытия приборов складывается). Прибор ведёт сценарий (в окне
        сменили адрес посреди ACAL) - сценарий сначала останавливается и возвращает приборы."""
        insts = [i for i in (self.links.proxies() if instruments is None else instruments) if not sip.isdeleted(i)]
        holding = [run for run in self._runs if any(i in run.instruments() for i in insts)]
        if holding:
            self._stop_runs(holding, CLOSE_SCENARIO_WAIT_MS)
        for inst in insts:
            self.links.remove(inst)
            for key, value, who in self._forget(inst):
                log.warning("%s: %s %s = %s не возвращено - прибор закрыт", who, inst.name, key, value)
            inst._proxy.request_shutdown()
        for inst in insts:
            inst._proxy.wait_shutdown(timeout_ms)

    def find(self, profile: str) -> Instrument | None:
        """Последний открытый прибор класса (``signal_generator``...) или None."""
        return self.links.find(profile)

    def proxies(self) -> list[Instrument]:
        """Приборы процесса в порядке открытия."""
        return self.links.proxies()

    # ------------------------------------------------------------ сценарии
    def runs(self) -> list[Run]:
        return list(self._runs)

    def busy(self, inst) -> Run | None:
        """Сценарий, который сейчас ведёт этот прибор (окну - не вмешиваться), или None."""
        for run in self._runs:
            if inst in run.instruments():
                return run
        return None

    def start(self, scenario: Scenario, bind: dict | None = None) -> Run:
        """Запустить сценарий. ``bind`` - {профиль: прибор}, остальное - последний открытый прибор
        профиля. Нет прибора, не подключён, занят другим сценарием - ScenarioError (ничего не начато)."""
        devices: dict[str, Device] = {}
        for profile in scenario.needs:
            inst = (bind or {}).get(profile) or self.find(profile)
            title = PROFILES[profile].title if profile in PROFILES else profile
            if inst is None:
                raise ScenarioError(f"{scenario.NAME}: no {profile.replace('_', ' ')} open ({title})")
            dev = Device(inst)
            if not inst.is_connected:
                raise ScenarioError(f"{scenario.NAME}: {dev.name} ({inst.name}) is not connected")
            other = self.busy(inst)
            if other is not None:
                raise ScenarioError(f"{scenario.NAME}: {dev.name} is busy with {other.name}")
            devices[profile] = dev
        run = Run(scenario, devices)
        log.info("%s: start (%s)", run.name, ", ".join(f"{d.name} {d.proxy.name} {d.proxy.address}"
                                                      for d in devices.values()))
        run.progress.connect(lambda *_a, r=run: self.run_progress.emit(r))
        run.ended.connect(self._on_ended)
        self._runs.append(run)
        self.run_started.emit(run)
        run.start()
        return run

    def _on_ended(self, run: Run) -> None:
        if run in self._runs:
            self._runs.remove(run)
        detail = f": {run.error}" if run.error and run.error != run.state else ""
        (log.info if run.state == run.DONE else log.warning)("%s: %s%s (%d/%d)", run.name, run.state, detail,
                                                               run.done, run.total)
        for inst, key, value in run.deferred:
            self._defer(inst, key, value, run.name)
        self.run_ended.emit(run)

    # ------------------------------------------------------------ возврат после переподключения
    def pending_restore(self, inst) -> list[tuple[str, object]]:
        """Что будет записано в прибор, когда он снова подключится: [(ключ, значение)]."""
        return [(k, v) for k, v, _who in self._deferred.get(inst, ([], None))[0]]

    def _defer(self, inst, key: str, value, who: str) -> None:
        if inst.is_connected:                    # связь уже вернулась (переподключился, пока сценарий кончался)
            self._apply(inst, [(key, value, who)])
            return
        if inst not in self._deferred:
            slot = (lambda state, i=inst: self._on_reconnected(i, state))
            inst.state_changed.connect(slot)
            self._deferred[inst] = ([], slot)
        self._deferred[inst][0].append((key, value, who))

    def _on_reconnected(self, inst, state) -> None:
        if state is not ConnectionState.CONNECTED:
            return
        items = self._forget(inst)
        self._apply(inst, items)

    def _forget(self, inst) -> list:
        """Снять ожидание переподключения прибора; вернуть, что ждало записи."""
        items, slot = self._deferred.pop(inst, ([], None))
        if slot is not None:
            try:
                inst.state_changed.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        return items

    @staticmethod
    def _apply(inst, items) -> None:
        for key, value, who in items:
            log.warning("%s: %s %s = %s - связь вернулась, возвращаю", who, inst.name, key, value)
            inst._proxy.set(key, value)          # возврат сценария - мимо арбитража окна

    def stop_all(self, wait_ms: int = 0) -> bool:
        """Остановить все сценарии; ``wait_ms`` - дать им вернуть приборы (генератор - выключить ВЧ...),
        потом прервать оставшиеся. True - всё закончилось само."""
        return self._stop_runs(list(self._runs), wait_ms)

    def _stop_runs(self, runs: list[Run], wait_ms: int) -> bool:
        for run in runs:
            run.stop()
        clean = self._wait(runs, wait_ms) if wait_ms > 0 else not any(r in self._runs for r in runs)
        for run in [r for r in runs if r in self._runs]:
            log.warning("%s: не успел вернуть приборы за %d мс - прерван", run.name, wait_ms)
            run.abort()
        return clean

    def wait_idle(self, timeout_ms: int) -> bool:
        """Крутить события, пока идут сценарии (не дольше timeout_ms). Работает и при выходе из
        программы, когда вложенный QEventLoop уже не запускается."""
        return self._wait(None, timeout_ms)

    def _wait(self, runs: list[Run] | None, timeout_ms: int) -> bool:
        """Ждать конца сценариев ``runs`` (None - всех), не дольше timeout_ms. True - закончились."""
        def going() -> bool:
            return bool(self._runs) if runs is None else any(r in self._runs for r in runs)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while going() and time.monotonic() < deadline:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.005)
        return not going()


#: координатор процесса (как и реестр LINKS - один на программу)
COORD = Coordinator()
