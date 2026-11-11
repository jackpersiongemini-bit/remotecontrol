"""Стенд из симуляторов: выход генераторов попадает на анализатор спектра и ваттметр.

Только для работы без железа (``run_main.py --sim``): каждые ``interval`` мс включённые генераторы
(rf.output / rf.frequency / rf.power) становятся сигналами анализатора (``signals``) и мощностью
на датчике ваттметра (сумма, непрерывный сигнал) с потерями тракта ``loss_db``.
"""
from __future__ import annotations

import math

__all__ = ["SimBench"]


class SimBench:
    def __init__(self, generators=(), analyzers=(), meters=(), loss_db: float = 10.0):
        self.generators = list(generators)       # RFGenSim (ячейки s["rf.output"], ...)
        self.analyzers = list(analyzers)         # AgilentPSASim (signals)
        self.meters = list(meters)               # KeysightU2021XASim (set_signal)
        self.loss_db = loss_db

    def tones(self) -> list[tuple[float, float]]:
        """[(частота, dBm на входе приёмника)] включённых генераторов."""
        out = []
        for g in self.generators:
            s = getattr(g, "s", {})
            if s.get("rf.output"):
                out.append((float(s["rf.frequency"]), float(s["rf.power"]) - self.loss_db))
        return out

    def step(self) -> None:
        tones = self.tones()
        for sa in self.analyzers:
            sa.signals = list(tones)
        total_mw = sum(10 ** (p / 10) for _f, p in tones)
        dbm = 10 * math.log10(total_mw) if total_mw > 0 else -120.0
        for pm in self.meters:
            if pm.signal.get("on_dbm") != dbm or pm.signal.get("width") is not None:
                pm.set_signal(on_dbm=dbm, width=None)
