"""Стенд из симуляторов: выход генераторов попадает на анализатор спектра и ваттметр.

Часть симулированного мира (слой 1, под транспортом): программа выше о стенде не знает, как не знает
о кабелях на настоящем столе. Каждые ``INTERVAL_MS`` мс включённые генераторы (rf.output / rf.frequency /
rf.power) становятся сигналами анализатора (``signals``) и мощностью на датчике ваттметра (сумма,
непрерывный сигнал) с потерями тракта ``loss_db`` (+ ``slope_db_per_ghz`` на каждый ГГц частоты: кабель
теряет больше на высоких частотах - есть что калибровать ACAL).

``BENCH`` - стенд процесса. На него сам встаёт симулятор, который каталог драйверов создал для адреса
``sim`` (``run_main.py --sim``, окна на симуляторах), пока его ресурс открыт (:class:`SimTransport`
с ``bench``). Тракт - 10 dB + 0.2 dB/ГГц. Стенд работает в своём потоке, пока на нём есть генератор и
приёмник. Тесты собирают свой ``SimBench`` из нужных симуляторов и зовут ``step()`` сами.
"""
from __future__ import annotations

import logging
import math
import threading

from .agilent_e4446a_sim import AgilentPSASim
from .keysight_u2021xa_sim import KeysightU2021XASim
from .rf_generator_sim import RFGenSim

log = logging.getLogger(__name__)

__all__ = ["SimBench", "BENCH"]


class SimBench:
    #: шаг стенда, который работает сам (симуляторы встали через ``join``), мс
    INTERVAL_MS = 200

    def __init__(self, generators=(), analyzers=(), meters=(), loss_db: float = 10.0, slope_db_per_ghz: float = 0.0):
        self.generators = list(generators)       # RFGenSim (ячейки s["rf.output"], ...)
        self.analyzers = list(analyzers)         # AgilentPSASim (signals)
        self.meters = list(meters)               # KeysightU2021XASim (set_signal)
        self.loss_db = loss_db
        self.slope_db_per_ghz = slope_db_per_ghz
        self._lock = threading.RLock()           # встают и уходят из потоков приборов
        self._stop: threading.Event | None = None

    def loss(self, freq: float) -> float:
        """Потери тракта на частоте, dB (положительное число)."""
        return self.loss_db + self.slope_db_per_ghz * float(freq) / 1e9

    def tones(self) -> list[tuple[float, float]]:
        """[(частота, dBm на входе приёмника)] включённых генераторов."""
        out = []
        with self._lock:
            generators = list(self.generators)
        for g in generators:
            s = getattr(g, "s", {})
            if s.get("rf.output"):
                f = float(s["rf.frequency"])
                out.append((f, float(s["rf.power"]) - self.loss(f)))
        return out

    def step(self) -> None:
        tones = self.tones()
        with self._lock:
            analyzers, meters = list(self.analyzers), list(self.meters)
        for sa in analyzers:
            sa.signals = list(tones)
        total_mw = sum(10 ** (p / 10) for _f, p in tones)
        dbm = 10 * math.log10(total_mw) if total_mw > 0 else -120.0
        for pm in meters:
            if pm.signal.get("on_dbm") != dbm or pm.signal.get("width") is not None:
                pm.set_signal(on_dbm=dbm, width=None)

    # ------------------------------------------------------------ симуляторы встают сами
    def _group(self, sim) -> list | None:
        if isinstance(sim, RFGenSim):
            return self.generators
        if isinstance(sim, AgilentPSASim):
            return self.analyzers
        if isinstance(sim, KeysightU2021XASim):
            return self.meters
        return None                              # источник питания, ГСС, ВАЦ - к тракту не подключены

    def join(self, sim) -> None:
        """Поставить симулятор на стенд (ресурс открыт). Генератор, анализатор, ваттметр - по классу."""
        with self._lock:
            group = self._group(sim)
            if group is not None and not any(s is sim for s in group):
                group.append(sim)
            self._run_if_needed()

    def leave(self, sim) -> None:
        """Убрать симулятор со стенда (ресурс закрыт): ушедший генератор больше не светит на приёмники."""
        with self._lock:
            was_generator = any(s is sim for s in self.generators)
            for group in (self.generators, self.analyzers, self.meters):
                group[:] = [s for s in group if s is not sim]
            self._run_if_needed()
        if was_generator:
            self.step()

    @property
    def running(self) -> bool:
        return self._stop is not None

    def _run_if_needed(self) -> None:
        needed = bool(self.generators) and bool(self.analyzers or self.meters)
        if needed and self._stop is None:
            self._stop = threading.Event()
            threading.Thread(target=self._loop, args=(self._stop,), name="sim bench", daemon=True).start()
        elif not needed and self._stop is not None:
            self._stop.set()
            self._stop = None

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.INTERVAL_MS / 1000.0):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - стенд не должен останавливаться из-за одного шага
                log.exception("стенд симуляторов: шаг")


#: стенд процесса: симуляторы каталога (адрес ``sim``) встают на него сами
BENCH = SimBench(loss_db=10.0, slope_db_per_ghz=0.2)
