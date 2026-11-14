"""Частота ваттметра следует за источником (Алексей 2026-11-14: «меняю частоту на генераторе, а на
ваттметре частота не меняется»).

Источник - генератор сигнала (``rf.frequency``) или ВАЦ в режиме генератора (CW: ``gen.state``,
``freq.cw``). Поменялась частота источника (или ВАЦ перешёл в CW) - датчики ваттметров, у которых
слежение включено (по умолчанию - да), ставятся на неё: поправка датчика зависит от частоты.

Не трогаем ваттметр, который ведёт сценарий (ACAL ставит частоты сам), не подключённый и частоту вне
диапазона датчика. Частоту датчика можно поменять руками - она держится до следующей смены у источника.
"""
from __future__ import annotations

from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSignal

from .registry import profile_name

__all__ = ["FrequencyFollow", "source_frequency"]

_SOURCE_KEYS = {"signal_generator": ("rf.frequency",), "network_analyzer": ("freq.cw", "gen.state")}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def source_frequency(inst) -> float | None:
    """Частота источника: генератор - ``rf.frequency``, ВАЦ - ``freq.cw`` только в режиме генератора."""
    prof = profile_name(inst)
    if prof == "signal_generator":
        return _num(inst.value("rf.frequency"))
    if prof == "network_analyzer" and inst.value("gen.state") is True:
        return _num(inst.value("freq.cw"))
    return None


class FrequencyFollow(QObject):
    """Слежение частоты ваттметров за источником. ``changed(ваттметр)`` - включили / выключили слежение
    или датчик поставлен на частоту источника (``source(ваттметр)`` - от кого)."""

    changed = pyqtSignal(object)

    def __init__(self, coordinator, parent=None):
        super().__init__(parent)
        self.coord = coordinator
        self._enabled: dict[Any, bool] = {}
        self._sources: dict[Any, Any] = {}          # ваттметр -> источник, за которым он сейчас
        self._last: Any = None                       # источник, у которого частота менялась последней
        self._conns: dict[Any, tuple] = {}
        coordinator.links.changed.connect(self._sync)
        self._sync()

    # ------------------------------------------------------------ настройка
    def enabled(self, pm) -> bool:
        return self._enabled.get(pm, True)

    def set_enabled(self, pm, on: bool) -> None:
        """Слежение для ваттметра (окно хранит выбор в ini). Включили - сразу на частоту источника."""
        self._enabled[pm] = bool(on)
        if on:
            src = self.current_source()
            f = source_frequency(src) if src is not None else None
            if f is not None:
                self._push(pm, f, src)
        self.changed.emit(pm)

    def source(self, pm):
        """Источник, на частоту которого поставлен датчик, или None."""
        return self._sources.get(pm) if self.enabled(pm) else None

    def current_source(self):
        """Чья частота сейчас главная: последний источник, у которого она менялась, иначе - генератор
        сценариев (``COORD.find_for``)."""
        if self._last is not None and self._last in self.coord.links.proxies() and \
                source_frequency(self._last) is not None:
            return self._last
        return self.coord.find_for("signal_generator")

    # ------------------------------------------------------------ приборы
    def _sync(self) -> None:
        current = self.coord.links.proxies()
        for inst in [i for i in self._conns if i not in current]:
            for sig, slot in self._conns.pop(inst):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._enabled.pop(inst, None)
            self._sources.pop(inst, None)
        for inst in current:
            if inst in self._conns:
                continue
            prof = profile_name(inst)
            conns = []
            if prof in _SOURCE_KEYS:
                slot = (lambda key, value, i=inst: self._on_source(i, key))
                inst.param_updated.connect(slot)
                conns.append((inst.param_updated, slot))
            elif prof == "power_meter":
                slot = (lambda i=inst: self._on_meter_synced(i))
                inst.sync_done.connect(slot)
                conns.append((inst.sync_done, slot))
            self._conns[inst] = tuple(conns)

    def _on_source(self, src, key: str) -> None:
        if sip.isdeleted(self) or key not in _SOURCE_KEYS.get(profile_name(src), ()):
            return
        f = source_frequency(src)
        if f is None:
            return
        self._last = src
        for pm in self.coord.links.proxies():
            if profile_name(pm) == "power_meter" and self.enabled(pm):
                self._push(pm, f, src)

    def _on_meter_synced(self, pm) -> None:
        """Ваттметр подключился - сразу на частоту источника."""
        if sip.isdeleted(self) or not self.enabled(pm):
            return
        src = self.current_source()
        f = source_frequency(src) if src is not None else None
        if f is not None:
            self._push(pm, f, src)

    def _push(self, pm, freq: float, src) -> None:
        if not pm.is_connected or self.coord.busy(pm) is not None or self.coord.busy(src) is not None:
            return                                   # сценарий сам ставит частоты
        spec = pm.params.get("freq")
        if spec is None or spec.readonly:
            return
        try:
            freq = spec.validate(freq)
        except (TypeError, ValueError):
            return                                   # вне диапазона датчика
        self._sources[pm] = src
        cur = pm.value("freq")
        if cur is None or not spec.same(freq, cur):
            pm.set("freq", freq)
        self.changed.emit(pm)
