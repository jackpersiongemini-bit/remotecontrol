"""Реестр приборов процесса: кто открыт, по профилю, без знания драйверов.

Слой координатора (UI <> координатор <> абстрактные приборы <> драйверы <> pyvisa):
окна регистрируют свои InstrumentProxy, координатор и другие окна находят их по профилю.
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from ..core.proxy import InstrumentProxy

__all__ = ["ProxyLinks", "LINKS", "profile_name"]


def profile_name(proxy) -> str:
    """Имя профиля прибора (``power_meter``...) или пустая строка."""
    prof = getattr(proxy, "profile", None) or getattr(getattr(proxy, "driver_cls", None), "PROFILE", None)
    return getattr(prof, "name", "") or ""


class ProxyLinks(QObject):
    """Прокси открытых в этом процессе окон: окно может брать значения другого прибора
    (ваттметр - скважность у ГСС) по имени профиля, не зная драйвера."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._proxies: list[InstrumentProxy] = []

    def add(self, proxy: InstrumentProxy) -> None:
        if proxy not in self._proxies:
            self._proxies.append(proxy)
            self.changed.emit()

    def remove(self, proxy: InstrumentProxy) -> None:
        if proxy in self._proxies:
            self._proxies.remove(proxy)
            try:
                self.changed.emit()
            except RuntimeError:                 # выход из программы: реестр уже удалён
                pass

    def proxies(self) -> list[InstrumentProxy]:
        """Все приборы в порядке открытия."""
        return list(self._proxies)

    def find(self, profile: str) -> InstrumentProxy | None:
        """Последний открытый прибор с этим профилем (``function_generator``...)."""
        for proxy in reversed(self._proxies):
            prof = getattr(getattr(proxy, "driver_cls", None), "PROFILE", None)
            if prof is not None and prof.name == profile:
                return proxy
        return None


#: общий для процесса реестр приборов окон
LINKS = ProxyLinks()
