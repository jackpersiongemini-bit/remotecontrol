"""Координатор: единая точка, через которую UI работает с несколькими приборами сразу.

UI <> координатор <> абстрактные приборы (профили, InstrumentProxy) <> драйверы <> pyvisa.

* приборы процесса - реестр ``links`` (окна регистрируют свои прокси), поиск по профилю;
* сценарии измерений (:mod:`visalab.coord.scenario`): ``start(сценарий)`` подбирает приборы по
  профилям, запускает и следит, чтобы один прибор не вели два сценария сразу (``busy``);
* при выходе из программы - остановить сценарии и дать им вернуть приборы (``stop_all``).

Окна знают только координатор и ключи профилей; какой драйвер у прибора, координатору не важно.
"""
from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, pyqtSignal

from ..profiles import PROFILES
from .registry import LINKS, ProxyLinks
from .scenario import Device, Run, Scenario, ScenarioError

log = logging.getLogger(__name__)

__all__ = ["Coordinator", "COORD"]


class Coordinator(QObject):
    run_started = pyqtSignal(object)        # Run
    run_progress = pyqtSignal(object)       # Run: изменились done / total / text
    run_ended = pyqtSignal(object)          # Run (state: done / failed / stopped)

    def __init__(self, links: ProxyLinks = LINKS, parent=None):
        super().__init__(parent)
        self.links = links
        self._runs: list[Run] = []

    # ------------------------------------------------------------ приборы
    def find(self, profile: str):
        """Последний открытый прибор класса (``signal_generator``...) или None."""
        return self.links.find(profile)

    def proxies(self) -> list:
        return self.links.proxies()

    # ------------------------------------------------------------ сценарии
    def runs(self) -> list[Run]:
        return list(self._runs)

    def busy(self, proxy) -> Run | None:
        """Сценарий, который сейчас ведёт этот прибор (окну - не вмешиваться), или None."""
        for run in self._runs:
            if proxy in run.proxies():
                return run
        return None

    def start(self, scenario: Scenario, bind: dict | None = None) -> Run:
        """Запустить сценарий. ``bind`` - {профиль: прокси}, остальное - последний открытый прибор
        профиля. Нет прибора, не подключён, занят другим сценарием - ScenarioError (ничего не начато)."""
        devices: dict[str, Device] = {}
        for profile in scenario.needs:
            proxy = (bind or {}).get(profile) or self.find(profile)
            title = PROFILES[profile].title if profile in PROFILES else profile
            if proxy is None:
                raise ScenarioError(f"{scenario.NAME}: no {profile.replace('_', ' ')} open ({title})")
            dev = Device(proxy)
            if not proxy.is_connected:
                raise ScenarioError(f"{scenario.NAME}: {dev.name} ({proxy.name}) is not connected")
            other = self.busy(proxy)
            if other is not None:
                raise ScenarioError(f"{scenario.NAME}: {dev.name} is busy with {other.name}")
            devices[profile] = dev
        run = Run(scenario, devices)
        run.progress.connect(lambda *_a, r=run: self.run_progress.emit(r))
        run.ended.connect(self._on_ended)
        self._runs.append(run)
        self.run_started.emit(run)
        run.start()
        return run

    def _on_ended(self, run: Run) -> None:
        if run in self._runs:
            self._runs.remove(run)
        self.run_ended.emit(run)

    def stop_all(self, wait_ms: int = 0) -> bool:
        """Остановить все сценарии; ``wait_ms`` - дать им вернуть приборы (генератор - выключить ВЧ...),
        потом прервать оставшиеся. True - всё закончилось само."""
        for run in list(self._runs):
            run.stop()
        clean = self.wait_idle(wait_ms) if wait_ms > 0 else not self._runs
        for run in list(self._runs):
            log.warning("%s: не успел вернуть приборы за %d мс - прерван", run.name, wait_ms)
            run.abort()
        return clean

    def wait_idle(self, timeout_ms: int) -> bool:
        """Крутить события, пока идут сценарии (не дольше timeout_ms). Работает и при выходе из
        программы, когда вложенный QEventLoop уже не запускается."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while self._runs and time.monotonic() < deadline:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.005)
        return not self._runs


#: координатор процесса (как и реестр LINKS - один на программу)
COORD = Coordinator()
