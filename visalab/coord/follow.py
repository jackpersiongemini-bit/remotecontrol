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
        self._source_freqs: dict[Any, float | None] = {}
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

    def stop(self) -> None:
        """Отключиться от реестра и приборов."""
        try:
            self.coord.links.changed.disconnect(self._sync)
        except (TypeError, RuntimeError):
            pass
        for inst in list(self._conns):
            conns = self._conns.pop(inst, ())
            if not sip.isdeleted(inst):
                for sig, slot in conns:
                    try:
                        sig.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass
        self._sources.clear()
        self._source_freqs.clear()
        self._last = None

    # ------------------------------------------------------------ приборы
    def _sync(self) -> None:
        if sip.isdeleted(self):
            return
        current = self.coord.links.proxies()
        affected: list[Any] = []
        for inst in [i for i in self._conns if i not in current]:
            conns = self._conns.pop(inst)
            if not sip.isdeleted(inst):
                for sig, slot in conns:
                    try:
                        sig.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass
            self._enabled.pop(inst, None)
            self._sources.pop(inst, None)
            self._source_freqs.pop(inst, None)
            if self._last is inst:
                self._last = None
            for pm, src in list(self._sources.items()):
                if src is inst:
                    self._sources.pop(pm, None)
                    affected.append(pm)
        for inst in current:
            if inst in self._conns:
                continue
            prof = profile_name(inst)
            conns = []
            if prof in _SOURCE_KEYS:
                self._source_freqs[inst] = source_frequency(inst)
                slot = (lambda key, value, i=inst: self._on_source(i, key))
                inst.param_updated.connect(slot)
                conns.append((inst.param_updated, slot))
                st_slot = (lambda _s, i=inst: not i.is_connected and self._source_freqs.pop(i, None))
                inst.state_changed.connect(st_slot)
                conns.append((inst.state_changed, st_slot))
            elif prof == "power_meter":
                slot = (lambda i=inst: self._on_meter_synced(i))
                inst.sync_done.connect(slot)
                conns.append((inst.sync_done, slot))
            self._conns[inst] = tuple(conns)
        for pm in affected:
            if not sip.isdeleted(pm):
                self.changed.emit(pm)

    def _on_source(self, src, key: str) -> None:
        if sip.isdeleted(self) or sip.isdeleted(src) or key not in _SOURCE_KEYS.get(profile_name(src), ()):
            return
        f = source_frequency(src)
        prev = self._source_freqs.get(src)
        self._source_freqs[src] = f
        if f is None:
            if self._last is src:
                self._last = None
            for pm, cur_src in list(self._sources.items()):
                if cur_src is src:
                    self._sources.pop(pm, None)
                    if not sip.isdeleted(pm):
                        self.changed.emit(pm)
            return
        self._last = src
        freq_changed = (prev is not None and abs(f - prev) > max(1e-3, 1e-9 * abs(f))) or (
            key == "gen.state" and prev is None
        )
        for inst in self.coord.links.proxies():
            prof = profile_name(inst)
            if prof == "power_meter":
                if self.enabled(inst):
                    self._push(inst, f, src, restart=freq_changed)
                elif freq_changed:
                    self._restart(inst, src)
            elif prof == "spectrum_analyzer" and freq_changed:
                self._restart(inst, src)

    def _on_meter_synced(self, pm) -> None:
        """Ваттметр подключился - сразу на частоту источника."""
        if sip.isdeleted(self) or not self.enabled(pm):
            return
        src = self.current_source()
        f = source_frequency(src) if src is not None else None
        if f is not None:
            self._push(pm, f, src)

    def _restart(self, inst, src) -> None:
        if not inst.is_connected or self.coord.busy(inst) is not None or self.coord.busy(src) is not None:
            return
        if "restart" in inst.actions:
            inst.action("restart")

    def _push(self, pm, freq: float, src, restart: bool = False) -> None:
        if not pm.is_connected or self.coord.busy(pm) is not None or self.coord.busy(src) is not None:
            return                                   # сценарий сам ставит частоты
        spec = pm.params.get("freq")
        if spec is None or spec.readonly:
            return
        try:
            freq = spec.validate(freq)
        except (TypeError, ValueError):
            if restart:
                self._restart(pm, src)
            return                                   # вне диапазона датчика
        self._sources[pm] = src
        cur = pm.value("freq")
        if cur is None or not spec.same(freq, cur):
            pm.set("freq", freq)
        elif restart:
            self._restart(pm, src)
        self.changed.emit(pm)
