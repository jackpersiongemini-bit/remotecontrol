"""Таблицы поправки по частоте (CSV «freq_hz, dB») и подбор GAIN + таблицы по измерению тракта.

Используются окном ваттметра (вкладка CAL) и сценарием автокалибровки ACAL координатора.
Поправка ваттметра:  P = Pизм + CAL(f) − GAIN + 10·lg(100 / DUTY).
"""
from __future__ import annotations

import bisect
import re
from pathlib import Path

__all__ = ["load_csv", "save_csv", "interpolate", "fit_gain_table"]


def load_csv(path) -> list[tuple[float, float]]:
    """Таблица калибровки: строки «частота_Hz, dB» (разделитель , ; или таб; заголовок и
    строки не из двух чисел пропускаются)."""
    points = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [x for x in re.split(r"[,;\t ]+", line.strip()) if x]
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return sorted(points)


def save_csv(path, points) -> None:
    Path(path).write_text("freq_hz,db\n" + "".join(f"{f:.10g},{d:.6g}\n" for f, d in points), encoding="utf-8")


def interpolate(table: list[tuple[float, float]], freq: float | None) -> float:
    """Поправка на частоте: линейно между точками, за краями - крайняя точка, пустая таблица - 0."""
    if not table or freq is None:
        return 0.0
    pts = sorted(table)
    xs = [f for f, _ in pts]
    if freq <= xs[0]:
        return pts[0][1]
    if freq >= xs[-1]:
        return pts[-1][1]
    i = bisect.bisect_right(xs, freq)
    (f0, d0), (f1, d1) = pts[i - 1], pts[i]
    return d0 + (d1 - d0) * (freq - f0) / (f1 - f0) if f1 != f0 else d1


def fit_gain_table(response: list[tuple[float, float]]) -> tuple[float, list[tuple[float, float]]]:
    """Коэффициент передачи тракта R(f), dB (плюс - усиление, минус - потери) -> (GAIN, таблица CAL).

    GAIN - среднее R (лучшая постоянная), CAL(f) = GAIN − R(f) - неравномерность по частоте.
    Тогда CAL(f) − GAIN = −R(f): исправленное показание равно уровню на входе тракта."""
    if not response:
        raise ValueError("нет точек")
    gain = sum(r for _f, r in response) / len(response)
    return gain, [(f, gain - r) for f, r in sorted(response)]
