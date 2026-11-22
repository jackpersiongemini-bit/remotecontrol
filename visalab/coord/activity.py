"""Кто что сейчас измеряет (слой координатора).

``ActivityMonitor`` следит за всеми приборами реестра ``LINKS`` только через ключи профилей:

* приёмники - ``network_analyzer``, ``power_meter``, ``spectrum_analyzer``;
* источники - ``signal_generator`` (rf.output / rf.frequency / rf.power) и ВАЦ в режиме генератора
  (gen.state / freq.cw / source.power).

Приёмник «измеряет», если за последние ``hold_s`` секунд пришли данные с сигналом:

* анализатор спектра - пик трассы выше шумовой дорожки (медианы трассы) на ``sa_margin_db``;
* ваттметр - ``meas.power`` не ниже ``pm_floor_dbm``;
* ВАЦ - развёртки идут всегда, поэтому тоже по уровню: хоть один S21 / S12 выше ``vna_thru_db``
  (есть передача - что-то стоит между портами) или S11 / S22 ниже ``vna_refl_db`` (порт нагружен,
  а не открыт). Модуль берётся из комплексных данных слота или из формата MLOG / LIN_MAG / SWR /
  POLAR / SMITH. Трейс без модуля (PHASE, GDELAY...) по уровню не судится - пока идут развёртки, ВАЦ
  с таким трейсом измеряет (Алексей: «трейс на фазе - и ничего не происходит»). rf.output = False - не измеряет.

Для анализатора и ваттметра координатор ищет источник: включённый генератор на частоте пика
(для ваттметра - на частоте датчика ``freq``; если включён один генератор - он) и считает
путь ``level - power`` в dB (минус - потери).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from PyQt6 import sip
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..core.params import ConnectionState
from ..core.trace import Trace
from .registry import LINKS, ProxyLinks, profile_name

__all__ = ["ActivityMonitor", "Activity", "Source", "Thresholds", "RECEIVERS", "SHORT_NAMES",
           "spectrum_peak", "match_source", "vna_signal", "slot_magnitude_db", "magnitude_db", "short_name"]

RECEIVERS = ("network_analyzer", "power_meter", "spectrum_analyzer")
#: короткие имена классов приборов - плитки главного окна и заголовки окон (Алексей 2026-11-14: генератор
#: импульсов - PULSE, не SDG)
SHORT_NAMES = {"network_analyzer": "VNA", "power_meter": "PM", "spectrum_analyzer": "SA",
               "signal_generator": "GEN", "function_generator": "PULSE", "power_supply": "PSU"}
#: сколько последних показаний ваттметра хранится (точки на графике)
HISTORY = 2000
_SOURCE_KEYS = frozenset(("rf.output", "rf.frequency", "rf.power", "gen.state", "freq.cw", "source.power"))


@dataclass
class Thresholds:
    sa_margin_db: float = 10.0       # пик над шумовой дорожкой
    pm_floor_dbm: float = -60.0      # ниже - датчик «ничего не видит»
    vna_thru_db: float = -40.0       # S21 / S12: максимум выше - передача есть
    vna_refl_db: float = -3.0        # S11 / S22: минимум ниже - порт нагружен
    hold_s: float = 5.0              # столько секунд после последнего сигнала прибор ещё «измеряет»
    freq_tol_rel: float = 1e-3       # совпадение частоты с генератором: 0.1 %...
    freq_tol_hz: float = 100e3       # ...но не меньше 100 kHz (и не меньше двух точек трассы)


@dataclass(frozen=True)
class Source:
    proxy: Any
    name: str           # GEN, GEN2, VNA...
    freq: float
    power: float | None


@dataclass
class Activity:
    proxy: Any
    profile: str
    name: str
    connected: bool = False
    measuring: bool = False
    signal: bool = False
    level: float | None = None       # dBm: пик спектра / показание ваттметра
    freq: float | None = None        # Hz: частота пика / частота датчика
    floor: float | None = None       # шумовая дорожка анализатора, dBm
    source: Source | None = None
    path_db: float | None = None     # level - мощность источника
    updated: float | None = None     # время последних данных (monotonic)


# ------------------------------------------------------------------ расчёты без Qt
def spectrum_peak(trace: Trace, margin_db: float) -> tuple[bool, float, float, float]:
    """(есть сигнал, x пика, уровень пика, шумовая дорожка). Дорожка - медиана трассы."""
    ys = sorted(v for v in trace.y if math.isfinite(v))
    if not ys:
        return False, (trace.x[0] if trace.x else 0.0), -200.0, -200.0
    floor = ys[len(ys) // 2]
    x, peak = trace.peak()
    return peak - floor >= margin_db, x, peak, floor


def slot_magnitude_db(trace: Trace) -> list[float] | None:
    """Модуль S-параметра в dB из трассы слота (комплексной или MLOG / LIN_MAG / SWR); None - нельзя."""
    fmt = (trace.meta or {}).get("format")
    if trace.is_complex:
        return list(magnitude_db(trace).y)
    if fmt == "MLOG":
        return list(trace.y)
    if fmt == "LIN_MAG":
        return [20 * math.log10(max(abs(v), 1e-15)) for v in trace.y]
    if fmt == "SWR":
        out = []
        for v in trace.y:
            if math.isinf(v) and v > 0:
                out.append(0.0)
            elif v >= 1:
                out.append(20 * math.log10(max((v - 1) / (v + 1), 1e-15)))
            else:
                out.append(-300.0)
        return out
    return None


def vna_signal(sparam: str | None, mag_db: list[float], thru_db: float, refl_db: float) -> bool:
    """Есть ли что-то на портах ВАЦ по уровню одного S-параметра."""
    if not mag_db:
        return False
    if sparam in ("S21", "S12"):
        return max(mag_db) >= thru_db
    if sparam in ("S11", "S22"):
        return min(mag_db) <= refl_db
    return False


def match_source(freq: float | None, sources: list[Source], tol_hz: float) -> Source | None:
    """Ближайший по частоте источник в пределах ``tol_hz``."""
    if freq is None:
        return None
    best = min(sources, key=lambda s: abs(s.freq - freq), default=None)
    return best if best is not None and abs(best.freq - freq) <= tol_hz else None


def magnitude_db(trace: Trace) -> Trace:
    """Комплексная трасса (POLAR / SMITH) -> модуль в dB; остальные - как есть."""
    if not trace.is_complex:
        return trace
    hypot = math.hypot
    log10 = math.log10
    y = tuple(20.0 * log10(max(hypot(r, i), 1e-15)) for r, i in zip(trace.y, trace.im))
    return Trace(trace.x, y, trace.x_unit, "dB", trace.name, trace.timestamp, trace.meta, uniform=trace.uniform)


def _trace_has_signal(trace: Trace, sparam: str | None, thru_db: float, refl_db: float) -> bool:
    """Быстрая проверка уровня без конвертации всех 32k точек в dB."""
    if sparam not in ("S21", "S12", "S11", "S22") or not len(trace):
        return False
    is_thru = sparam in ("S21", "S12")
    fmt = (trace.meta or {}).get("format")
    if trace.is_complex:
        thr = thru_db if is_thru else refl_db
        if thr < -300.0:
            return is_thru
        thr_sq = 10.0 ** (thr / 10.0)
        if is_thru:
            return any(r * r + i * i >= thr_sq for r, i in zip(trace.y, trace.im))
        return any(r * r + i * i <= thr_sq for r, i in zip(trace.y, trace.im))
    if fmt == "MLOG":
        return max(trace.y) >= thru_db if is_thru else min(trace.y) <= refl_db
    if fmt == "LIN_MAG":
        thr = thru_db if is_thru else refl_db
        if thr < -300.0:
            return is_thru
        lin_thr = 10.0 ** (thr / 20.0)
        if is_thru:
            return any(abs(v) >= lin_thr for v in trace.y)
        return any(abs(v) <= lin_thr for v in trace.y)
    if fmt == "SWR":
        thr = thru_db if is_thru else refl_db
        if thr >= 0.0:
            return not is_thru
        if thr < -300.0:
            return is_thru
        g = 10.0 ** (thr / 20.0)
        swr_thr = (1.0 + g) / (1.0 - g)
        if is_thru:
            return any(v >= swr_thr for v in trace.y)
        return any(v <= swr_thr for v in trace.y)
    mag = slot_magnitude_db(trace)
    return bool(mag is not None and vna_signal(sparam, mag, thru_db, refl_db))


def short_name(proxy, proxies: list) -> str:
    """VNA, PM, SA, GEN...; второй прибор того же класса - GEN2."""
    prof = profile_name(proxy)
    base = SHORT_NAMES.get(prof, prof.upper()[:4] or "DEV")
    same = [p for p in proxies if profile_name(p) == prof]
    n = same.index(proxy) + 1 if proxy in same else 1
    return base if n == 1 else f"{base}{n}"


def _num(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        fv = float(v)
        return fv if math.isfinite(fv) else None
    return None


# ------------------------------------------------------------------ монитор
class ActivityMonitor(QObject):
    """Следит за приборами реестра. ``changed(proxy)`` - у прибора новые данные или состояние,
    ``roster_changed`` - открыли / закрыли прибор."""

    changed = pyqtSignal(object)
    roster_changed = pyqtSignal()
    slot_magnitude_db = staticmethod(slot_magnitude_db)

    def __init__(self, links: ProxyLinks = LINKS, thresholds: Thresholds | None = None, parent=None,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.links = links
        self.th = thresholds or Thresholds()
        self.clock = clock
        self._conns: dict[Any, list] = {}
        self._last_data: dict[Any, float] = {}
        self._last_signal: dict[Any, float] = {}
        self._updated: dict[Any, float] = {}
        self._sa: dict[Any, tuple[bool, float, float, float]] = {}
        self._measuring: dict[Any, bool] = {}
        self.history: dict[Any, deque] = {}
        #: ваттметр -> {(частота датчика Hz, мощность источника dBm | None): (время, dBm)}
        self.pm_points: dict[Any, dict[tuple[float, float | None], tuple[float, float]]] = {}
        self._mag_cache: dict[tuple[Any, str], tuple[Trace, Trace]] = {}
        links.changed.connect(self._sync_roster)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        self._sync_roster()

    # ------------------------------------------------------------ состав
    def proxies(self) -> list:
        return self.links.proxies()

    def receivers(self) -> list:
        return [p for p in self.proxies() if profile_name(p) in RECEIVERS]

    def name(self, proxy) -> str:
        return short_name(proxy, self.proxies())

    def stop(self) -> None:
        """Отключиться от реестра и приборов (выход из программы: объекты Qt уже удаляются)."""
        self._timer.stop()
        try:
            self.links.changed.disconnect(self._sync_roster)
        except (TypeError, RuntimeError):
            pass
        for p in list(self._conns):
            self._drop(p)
        self._mag_cache.clear()

    def _drop(self, p) -> None:
        conns = self._conns.pop(p, [])
        if not sip.isdeleted(p):                 # у удалённого QObject отключать нечего (иначе segfault)
            for sig, slot in conns:
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass

    def _sync_roster(self) -> None:
        if sip.isdeleted(self):
            return
        current = self.proxies()
        for p in [p for p in self._conns if p not in current]:
            self._drop(p)
            for d in (self._last_data, self._last_signal, self._updated, self._sa, self._measuring,
                      self.history, self.pm_points):
                d.pop(p, None)
            self._purge_mag_cache(p)
        for p in current:
            if p in self._conns:
                continue
            on_value = (lambda k, v, p=p: self._on_value(p, k, v))
            on_state = (lambda s, p=p: self._on_state(p, s))
            p.param_updated.connect(on_value)
            p.state_changed.connect(on_state)
            self._conns[p] = [(p.param_updated, on_value), (p.state_changed, on_state)]
            pending = getattr(p, "reconnect_pending", None)      # плитка: «переподключается»
            if pending is not None:
                on_retry = (lambda _s, p=p: self._emit(p))
                pending.connect(on_retry)
                self._conns[p].append((pending, on_retry))
            self.history[p] = deque(maxlen=HISTORY)
            if p.state is ConnectionState.CONNECTED:
                prof, now = profile_name(p), self.clock()
                if prof == "network_analyzer" and self._has_vna_traces(p):
                    self._updated[p] = self._last_data[p] = now
                    self._measuring[p] = True
                elif prof == "spectrum_analyzer":
                    tr = p.value("trace")
                    if isinstance(tr, Trace) and len(tr) > 1:
                        self._sa[p] = spectrum_peak(tr, self.th.sa_margin_db)
                        self._updated[p] = self._last_data[p] = now
                        self._measuring[p] = True
        self.roster_changed.emit()

    def _purge_mag_cache(self, proxy) -> None:
        for k in [k for k in self._mag_cache if k[0] is proxy]:
            self._mag_cache.pop(k, None)

    # ------------------------------------------------------------ события приборов
    def _on_state(self, proxy, state) -> None:
        if sip.isdeleted(self) or sip.isdeleted(proxy):
            return
        if state is not ConnectionState.CONNECTED:
            self._last_data.pop(proxy, None)
            self._last_signal.pop(proxy, None)
            self._sa.pop(proxy, None)
            if proxy in self.history:
                self.history[proxy].clear()
            self._purge_mag_cache(proxy)
        if profile_name(proxy) in ("signal_generator", "network_analyzer"):
            for r in self.receivers():
                if r is not proxy:
                    self._emit(r)
        self._emit(proxy)

    def _on_value(self, proxy, key: str, value) -> None:
        if sip.isdeleted(self) or sip.isdeleted(proxy):
            return
        prof, now = profile_name(proxy), self.clock()
        if key in _SOURCE_KEYS and prof in ("signal_generator", "network_analyzer"):
            for r in self.receivers():          # источник изменился - подписи у приёмников тоже
                if r is not proxy:
                    self._emit(r)
        if prof not in RECEIVERS and (key in _SOURCE_KEYS or key.endswith("output")):
            self.changed.emit(proxy)            # плитка генератора / источника питания
            return
        if prof == "power_meter" and key == "meas.power":
            v = _num(value)
            if v is None:
                self._emit(proxy)
                return
            self.history[proxy].append((now, v))
            self._updated[proxy] = now
            self._last_data[proxy] = now
            if v >= self.th.pm_floor_dbm:
                self._last_signal[proxy] = now
                f = _num(proxy.value("freq"))
                if f is not None:
                    sources = self.sources(exclude=proxy)
                    src = match_source(f, sources, self._tol(f, 0.0))
                    if src is None and len(sources) == 1:
                        src = sources[0]
                    pk = None if src is None or src.power is None else round(float(src.power), 3)
                    points = self.pm_points.setdefault(proxy, {})
                    pt_key = (float(round(f)), pk)
                    points.pop(pt_key, None)
                    points[pt_key] = (now, v)             # O(1) LRU по порядку вставки
                    while len(points) > HISTORY:
                        points.pop(next(iter(points)))
            self._emit(proxy)
        elif prof == "power_meter" and key == "trace" and isinstance(value, Trace) and len(value) > 0:
            self._updated[proxy] = now
            self._last_data[proxy] = now
            self._emit(proxy)
        elif prof == "power_meter" and key in ("trace.state", "meas.armed"):
            self._emit(proxy)
        elif prof == "spectrum_analyzer" and key == "trace" and isinstance(value, Trace) and len(value) > 1:
            self._sa[proxy] = spectrum_peak(value, self.th.sa_margin_db)
            self._updated[proxy] = now
            self._last_data[proxy] = now
            if self._sa[proxy][0]:
                self._last_signal[proxy] = now
            self._emit(proxy)
        elif prof == "spectrum_analyzer" and key in ("ref_level", "scale_div"):
            self._emit(proxy)
        elif prof == "network_analyzer" and key == "trace":
            self._updated[proxy] = now                # ``trace`` публикуется последним в развёртке
            if self._has_vna_traces(proxy):
                self._last_data[proxy] = now
            else:
                self._last_data.pop(proxy, None)
            if proxy.value("rf.output") is not False and self.vna_has_signal(proxy):
                self._last_signal[proxy] = now
            self._emit(proxy)                         # одна перерисовка на развёртку
        elif prof == "network_analyzer" and (
            (key.startswith("m") and key.endswith((".enabled", ".trace")))
            or key in ("scale.div", "scale.ref", "scale.rpos")
        ):
            if self._has_vna_traces(proxy):
                self._last_data[proxy] = now
            else:
                self._last_data.pop(proxy, None)
            self._emit(proxy)
        elif prof == "network_analyzer" and key == "rf.output" and value is False:
            self._last_signal.pop(proxy, None)
            self._emit(proxy)

    def _has_vna_traces(self, proxy) -> bool:
        """Есть ли хотя бы одна непустая трасса у ВАЦ (без конвертации в dB)."""
        has_slots = False
        for n in range(1, 17):
            if proxy.supports(f"m{n}.enabled"):
                has_slots = True
            if not proxy.value(f"m{n}.enabled"):
                continue
            tr = proxy.value(f"m{n}.trace")
            if isinstance(tr, Trace) and len(tr) >= 2:
                return True
        if has_slots:
            return False
        tr = proxy.value("trace")
        return isinstance(tr, Trace) and len(tr) > 1

    def vna_has_signal(self, proxy) -> bool:
        """Хоть один включённый слот показывает по уровню, что на портах что-то есть. Слот, у которого
        модуля нет (фаза, групповая задержка...), по уровню не судится: он сам - «измеряет»."""
        for n in range(1, 17):
            if not proxy.value(f"m{n}.enabled"):
                continue
            has_mag = False
            seen = False
            for key in (f"m{n}.sdata", f"m{n}.trace"):
                tr = proxy.value(key)
                if not isinstance(tr, Trace) or not len(tr):
                    continue
                seen = True
                fmt = (tr.meta or {}).get("format")
                if tr.is_complex or fmt in ("MLOG", "LIN_MAG", "SWR"):
                    has_mag = True
                    sp = (tr.meta or {}).get("sparam") or proxy.value(f"m{n}.sparam")
                    if _trace_has_signal(tr, sp, self.th.vna_thru_db, self.th.vna_refl_db):
                        return True
                    break
            if not has_mag and seen:                 # фаза / задержка: данные идут - значит, измеряет
                return True
        return False

    def _emit(self, proxy) -> None:
        if sip.isdeleted(self) or sip.isdeleted(proxy):
            return
        self._measuring[proxy] = self._is_measuring(proxy)
        self.changed.emit(proxy)

    def set_threshold(self, name: str, value: float) -> None:
        """Изменить порог (поле ``Thresholds``) и пересчитать, кто измеряет, по последним данным."""
        if name not in Thresholds.__dataclass_fields__:
            raise KeyError(name)
        setattr(self.th, name, float(value))
        for p in self.receivers():
            if not self._is_measuring(p):
                self._last_signal.pop(p, None)
                self._emit(p)
                continue
            prof, tr = profile_name(p), p.value("trace")
            if prof == "spectrum_analyzer" and isinstance(tr, Trace) and len(tr) > 1:
                self._sa[p] = spectrum_peak(tr, self.th.sa_margin_db)
                if self._sa[p][0]:
                    self._last_signal[p] = self.clock()
                else:
                    self._last_signal.pop(p, None)
            elif prof == "network_analyzer":
                if p.value("rf.output") is not False and self.vna_has_signal(p):
                    self._last_signal[p] = self.clock()
                else:
                    self._last_signal.pop(p, None)
            elif prof == "power_meter" and self.history.get(p):
                if self.history[p][-1][1] >= self.th.pm_floor_dbm:
                    self._last_signal[p] = self.history[p][-1][0]
                else:
                    self._last_signal.pop(p, None)
            self._emit(p)

    def tick(self) -> None:
        """Раз в полсекунды: кто перестал измерять (сигнал пропал / данные не идут)."""
        if sip.isdeleted(self):
            return
        for p in self.receivers():
            if self._measuring.get(p) != self._is_measuring(p):
                self._emit(p)

    # ------------------------------------------------------------ результат
    def _is_measuring(self, proxy) -> bool:
        """Прибор подключён и выдаёт данные (за последние ``hold_s`` секунд; у ВАЦ - пока есть читаемые трассы)."""
        if proxy.state is not ConnectionState.CONNECTED:
            return False
        if profile_name(proxy) == "network_analyzer":
            return self._has_vna_traces(proxy)
        t = self._last_data.get(proxy)
        if t is None or self.clock() - t >= self.th.hold_s:
            return False
        return True

    def _has_signal(self, proxy) -> bool:
        """Есть ли сигнал выше порога (для маркера пика и подбора источника)."""
        t = self._last_signal.get(proxy)
        return proxy.state is ConnectionState.CONNECTED and t is not None and self.clock() - t < self.th.hold_s

    def sources(self, exclude=None) -> list[Source]:
        """Включённые источники СВЧ: генераторы и ВАЦ в режиме генератора."""
        out = []
        for p in self.proxies():
            if p is exclude or p.state is not ConnectionState.CONNECTED:
                continue
            prof = profile_name(p)
            if prof == "signal_generator" and p.value("rf.output") is True:
                f = _num(p.value("rf.frequency"))
                if f is not None:
                    out.append(Source(p, self.name(p), f, _num(p.value("rf.power"))))
            elif prof == "network_analyzer" and p.value("gen.state") is True and p.value("rf.output") is not False:
                f = _num(p.value("freq.cw"))
                if f is not None:
                    out.append(Source(p, self.name(p), f, _num(p.value("source.power"))))
        return out

    def activity(self, proxy) -> Activity:
        prof = profile_name(proxy)
        a = Activity(proxy, prof, self.name(proxy), connected=proxy.state is ConnectionState.CONNECTED,
                     measuring=self._is_measuring(proxy), signal=self._has_signal(proxy),
                     updated=self._updated.get(proxy))
        sources = self.sources(exclude=proxy)
        if prof == "spectrum_analyzer" and proxy in self._sa:
            present, x, peak, floor = self._sa[proxy]
            tr = proxy.value("trace")
            a.level, a.floor = peak, floor
            if isinstance(tr, Trace) and len(tr) > 0 and tr.x_unit == "Hz":
                a.freq = x
                step = abs(tr.x[-1] - tr.x[0]) / max(len(tr) - 1, 1)
            else:                                    # нулевая полоса: частота - центр
                a.freq, step = _num(proxy.value("freq.center")), 0.0
            if present and a.measuring and a.signal:
                a.source = match_source(a.freq, sources, self._tol(a.freq, step))
        elif prof == "power_meter":
            waiting = proxy.value("meas.power") is None and proxy.value("meas.armed") is True
            hist = self.history.get(proxy)
            if hist and not waiting:
                a.level = hist[-1][1]
            a.freq = _num(proxy.value("freq"))
            if a.measuring and a.signal:
                a.source = match_source(a.freq, sources, self._tol(a.freq, 0.0))
                if a.source is None and len(sources) == 1:     # датчик широкополосный: единственный источник
                    a.source = sources[0]
        if a.source is not None and a.source.power is not None and a.level is not None:
            a.path_db = a.level - a.source.power
        return a

    def _tol(self, freq: float | None, step: float) -> float:
        return max(self.th.freq_tol_hz, self.th.freq_tol_rel * abs(freq or 0.0), 2 * step)

    def pm_table(self, proxy) -> list[tuple[float, float, float]]:
        """Таблица ваттметра (сводка по частоте): [(частота Hz, dBm, время)] по возрастанию частоты -
        на каждой частоте последнее показание с сигналом (не ниже ``pm_floor_dbm``)."""
        by_freq: dict[float, tuple[float, float]] = {}
        for (f, _p), (t, v) in self.pm_points.get(proxy, {}).items():
            prev = by_freq.get(f)
            if prev is None or t >= prev[0]:
                by_freq[f] = (t, v)
        return sorted((f, v, t) for f, (t, v) in by_freq.items())

    def pm_matrix(self, proxy) -> tuple[list[float | None], list[tuple[float, dict[float | None, float]]]]:
        """Матрица ваттметра: ``(columns, rows)``, где ``columns`` - отсортированные уровни мощности
        источника (dBm; ``None`` в конце, если были измерения без источника), а ``rows`` -
        ``[(частота Hz, {уровень источника: измеренная мощность dBm})]`` по возрастанию частоты."""
        pts = self.pm_points.get(proxy, {})
        if not pts:
            return [], []
        num_cols = sorted({p for _f, p in pts if p is not None})
        cols: list[float | None] = list(num_cols)
        if any(p is None for _f, p in pts):
            cols.append(None)
        by_freq: dict[float, dict[float | None, float]] = {}
        for (f, p), (_t, v) in pts.items():
            by_freq.setdefault(f, {})[p] = v
        return cols, sorted(by_freq.items())

    def clear_pm_table(self, proxy) -> None:
        self.pm_points.pop(proxy, None)
        self.changed.emit(proxy)

    def _magnitude_db_cached(self, proxy, slot_key: str, trace: Trace) -> Trace:
        if not trace.is_complex:
            return trace
        key = (proxy, slot_key)
        entry = self._mag_cache.get(key)
        if entry is not None and entry[0] is trace:
            return entry[1]
        res = magnitude_db(trace)
        self._mag_cache[key] = (trace, res)
        return res

    def vna_traces(self, proxy) -> list[tuple[str, Trace]]:
        """Трейсы включённых слотов ВАЦ: [(«1·S21 MLOG», трасса)]."""
        out = []
        for n in range(1, 17):
            if not proxy.value(f"m{n}.enabled"):
                continue
            tr = proxy.value(f"m{n}.trace")
            if not isinstance(tr, Trace) or len(tr) < 2:
                continue
            meta = tr.meta or {}
            ch = meta.get("channel", proxy.value(f"m{n}.channel"))
            sp = meta.get("sparam", proxy.value(f"m{n}.sparam"))
            fmt = meta.get("format", proxy.value(f"m{n}.format"))
            label = f"{ch if ch is not None else '?'}·{sp or tr.name or f'm{n}'}"
            out.append((f"{label} {fmt}" if fmt else label, self._magnitude_db_cached(proxy, f"m{n}.trace", tr)))
        if not out:
            tr = proxy.value("trace")
            if isinstance(tr, Trace) and len(tr) > 1:
                out.append((str(proxy.value("sparam") or "TRACE"), self._magnitude_db_cached(proxy, "trace", tr)))
        return out
