"""Реестр приборов процесса: кто открыт, по профилю, без знания драйверов.

Слой координатора (UI <> координатор <> абстрактные приборы <> драйверы <> pyvisa): в реестре -
приборы, которые выдал координатор (:class:`visalab.coord.instrument.Instrument`); окна и сценарии
находят их по профилю.
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

__all__ = ["ProxyLinks", "LINKS", "profile_name"]


def profile_name(proxy) -> str:
    """Имя профиля прибора (``power_meter``...) или пустая строка."""
    return getattr(getattr(proxy, "profile", None), "name", "") or ""


class ProxyLinks(QObject):
    """Приборы, открытые в этом процессе: окно может брать значения другого прибора
    (ваттметр - скважность у ГСС) по имени профиля, не зная драйвера."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._proxies: list = []

    def add(self, proxy) -> None:
        if proxy not in self._proxies:
            self._proxies.append(proxy)
            self.changed.emit()

    def remove(self, proxy) -> None:
        if proxy in self._proxies:
            self._proxies.remove(proxy)
            try:
                self.changed.emit()
            except RuntimeError:                 # выход из программы: реестр уже удалён
                pass

    def proxies(self) -> list:
        """Все приборы в порядке открытия."""
        return list(self._proxies)

    def find(self, profile: str):
        """Последний открытый прибор с этим профилем (``function_generator``...) или None."""
        for proxy in reversed(self._proxies):
            if profile_name(proxy) == profile:
                return proxy
        return None


#: общий для процесса реестр приборов (его ведёт координатор ``COORD``)
LINKS = ProxyLinks()
