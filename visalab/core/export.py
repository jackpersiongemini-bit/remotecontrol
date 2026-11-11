"""Сохранение трасс в файл: CSV (одна таблица на несколько трасс) и Touchstone ``.s1p`` / ``.s2p``.

Трассы с одинаковой осью X (один канал ВАЦ) делят столбец X; у трасс с другой осью - свой
столбец X рядом. Комплексная трасса (``im``) - два столбца, Re и Im. Строки короче самой длинной
трассы дополняются пустыми ячейками. Разделитель - запятая, десятичная точка, UTF-8; строки
комментариев начинаются с ``#``.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

from .trace import Trace

__all__ = ["write_traces_csv", "write_touchstone", "TOUCHSTONE_ORDER"]

#: порядок S-параметров в строке Touchstone 1.x (для 2 портов - S11 S21 S12 S22)
TOUCHSTONE_ORDER = {1: ("S11",), 2: ("S11", "S21", "S12", "S22")}


def _head(label: str, unit: str) -> str:
    return f"{label}, {unit}" if unit else label


def write_traces_csv(path, traces: Sequence[Trace], labels: Sequence[str] | None = None,
                     comments: Iterable[str] = ()) -> Path:
    """Записать трассы в CSV. ``labels`` - подписи столбцов (по умолчанию ``Trace.name``)."""
    if not traces:
        raise ValueError("нет трасс для сохранения")
    labels = list(labels) if labels is not None else [t.name or f"trace{i + 1}" for i, t in enumerate(traces)]
    groups: list[tuple[Trace, list[int]]] = []             # (трасса с осью X, индексы трасс этой оси)
    for i, t in enumerate(traces):
        for first, members in groups:
            if first.x == t.x and first.x_unit == t.x_unit:
                members.append(i)
                break
        else:
            groups.append((t, [i]))
    header: list[str] = []
    columns: list[Sequence[float]] = []
    for n, (first, members) in enumerate(groups, 1):
        axis = "Time" if first.x_unit == "s" else "Freq" if first.x_unit == "Hz" else "X"
        header.append(_head(axis if len(groups) == 1 else f"{axis} {n}", first.x_unit))
        columns.append(first.x)
        for i in members:
            t = traces[i]
            if t.is_complex:
                header += [f"{labels[i]} Re", f"{labels[i]} Im"]
                columns += [t.y, t.im]
            else:
                header.append(_head(labels[i], t.y_unit))
                columns.append(t.y)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = max(len(c) for c in columns)
    with path.open("w", newline="", encoding="utf-8") as f:
        for line in comments:
            f.write(f"# {line}\n")
        w = csv.writer(f)
        w.writerow(header)
        for r in range(rows):
            w.writerow([f"{c[r]:.10g}" if r < len(c) else "" for c in columns])
    return path


def write_touchstone(path, sparams: dict[str, Trace], comments: Iterable[str] = (), z0: float = 50.0) -> Path:
    """Touchstone 1.x (``.s1p`` / ``.s2p``): ``# HZ S RI R 50``, строка - частота и Re, Im каждого S-параметра.

    ``sparams`` - комплексные трассы {"S11": ..., "S21": ...} с одной осью частот; число портов - по ним.
    """
    ports = 2 if len(sparams) > 1 else 1
    order = TOUCHSTONE_ORDER[ports]
    missing = [k for k in order if k not in sparams]
    if missing:
        raise ValueError(f"нет S-параметров {missing} для s{ports}p")
    traces = [sparams[k] for k in order]
    freqs = traces[0].x
    if any(t.x != freqs or not t.is_complex for t in traces):
        raise ValueError("S-параметры должны быть комплексными и с одной осью частот")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="\n", encoding="utf-8") as f:
        for line in comments:
            f.write(f"! {line}\n")
        f.write(f"! columns: freq, {', '.join(f'{k} Re, {k} Im' for k in order)}\n")
        f.write(f"# HZ S RI R {z0:g}\n")
        for i, fr in enumerate(freqs):
            vals = " ".join(f"{t.y[i]: .9e} {t.im[i]: .9e}" for t in traces)
            f.write(f"{fr:.9e} {vals}\n")
    return path
