"""Стандартный тип данных для трасс (спектр, развёртки ВАЦ, огибающая мощности).

Трасса - неизменяемый объект, который драйвер публикует как значение параметра
вида ``Kind.TRACE`` через обычный сигнал ``param_updated``. Снаружи у трассы любого
прибора одинаковый вид: ось X, ось Y, их единицы и метаданные развёртки.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["Trace"]


@dataclass(frozen=True)
class Trace:
    x: tuple[float, ...]
    y: tuple[float, ...]
    x_unit: str = "Hz"
    y_unit: str = "dBm"
    name: str = ""
    timestamp: float = field(default_factory=time.time)
    #: условия измерения (rbw, vbw, detector, ...) - для протокола
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self):
        if len(self.x) != len(self.y):
            raise ValueError(f"трасса {self.name}: {len(self.x)} точек X и {len(self.y)} точек Y")

    def __len__(self) -> int:
        return len(self.y)

    @classmethod
    def linear(cls, start: float, stop: float, y, **kw) -> "Trace":
        """Трасса с равномерной осью X от start до stop (как у анализатора)."""
        y = tuple(float(v) for v in y)
        n = len(y)
        step = (stop - start) / (n - 1) if n > 1 else 0.0
        return cls(tuple(start + i * step for i in range(n)), y, **kw)

    def peak(self) -> tuple[float, float]:
        """(x, y) максимума."""
        i = max(range(len(self.y)), key=self.y.__getitem__)
        return self.x[i], self.y[i]
