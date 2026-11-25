"""Сохранение трасс в файл: CSV (одна таблица на несколько трасс) и Touchstone ``.s1p`` / ``.s2p``.

Трассы с одинаковой осью X (один канал ВАЦ) делят столбец X; у трасс с другой осью - свой
столбец X рядом. Комплексная трасса (``im``) - два столбца, Re и Im. Строки короче самой длинной
трассы дополняются пустыми ячейками. Разделитель - запятая, десятичная точка, UTF-8; строки
комментариев начинаются с ``#``.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Iterable, Sequence

from .trace import Trace

__all__ = ["write_traces_csv", "write_touchstone", "TOUCHSTONE_ORDER"]

#: порядок S-параметров в Touchstone 1.x:
#: 1 порт - S11; 2 порта - S11 S21 S12 S22; 3/4 порта - построчно S11 S12 ... S1N, S21 ...
TOUCHSTONE_ORDER = {
    1: ("S11",),
    2: ("S11", "S21", "S12", "S22"),
    3: tuple(f"S{r}{c}" for r in range(1, 4) for c in range(1, 4)),
    4: tuple(f"S{r}{c}" for r in range(1, 5) for c in range(1, 5)),
}


def _touchstone_order(sparams: dict[str, Trace]) -> tuple[int, tuple[str, ...]]:
    """Определить число портов и порядок ключей S-параметров для Touchstone 1.x."""
    keys = {k.upper(): k for k in sparams}
    ports_set: set[int] = set()
    for ku in keys:
        if len(ku) == 3 and ku[0] == "S" and ku[1].isdigit() and ku[2].isdigit():
            ports_set.add(int(ku[1]))
            ports_set.add(int(ku[2]))
    plist = sorted(ports_set)
    n_ports = len(plist)
    if n_ports == 1:
        p = plist[0]
        order = (f"S{p}{p}",)
    elif n_ports == 2:
        p1, p2 = plist
        order = (f"S{p1}{p1}", f"S{p2}{p1}", f"S{p1}{p2}", f"S{p2}{p2}")
    elif n_ports in (3, 4):
        order = tuple(f"S{a}{b}" for a in plist for b in plist)
    else:
        raise ValueError(f"Touchstone поддерживает 1..4 порта, получено {sorted(sparams)}")
    missing = [k for k in order if k not in keys]
    if missing:
        raise ValueError(f"нет S-параметров {missing} для s{n_ports}p")
    return n_ports, tuple(keys[k] for k in order)


def _head(label: str, unit: str) -> str:
    return f"{label}, {unit}" if unit else label


def _comment_lines(comments: Iterable[str]) -> list[str]:
    out: list[str] = []
    for line in comments:
        parts = str(line).splitlines()
        out.extend(parts or [""])
    return out


def write_traces_csv(path, traces: Sequence[Trace], labels: Sequence[str] | None = None,
                     comments: Iterable[str] = ()) -> Path:
    """Записать трассы в CSV. ``labels`` - подписи столбцов (по умолчанию ``Trace.name``)."""
    if not traces:
        raise ValueError("нет трасс для сохранения")
    labels = list(labels) if labels is not None else [t.name or f"trace{i + 1}" for i, t in enumerate(traces)]
    if len(labels) != len(traces):
        raise ValueError(f"число подписей ({len(labels)}) не совпадает с числом трасс ({len(traces)})")
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
        for line in _comment_lines(comments):
            f.write(f"# {line}\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in range(rows):
            w.writerow([f"{c[r]:.10g}" if r < len(c) else "" for c in columns])
    return path


def write_touchstone(path, sparams: dict[str, Trace], comments: Iterable[str] = (), z0: float = 50.0) -> Path:
    """Touchstone 1.x (``.s1p`` / ``.s2p`` / ``.s3p`` / ``.s4p``): ``# HZ S RI R 50``,
    частота и Re, Im каждого S-параметра.

    ``sparams`` - комплексные трассы {"S11": ..., "S21": ...} с одной осью частот; число портов - по ним.
    """
    if not math.isfinite(z0) or z0 <= 0:
        raise ValueError(f"недопустимое опорное сопротивление z0={z0!r}")
    if not sparams:
        raise ValueError("нет S-параметров для Touchstone")
    n_ports, order = _touchstone_order(sparams)
    traces = [sparams[k] for k in order]
    freqs = traces[0].x
    if not freqs:
        raise ValueError("пустая ось частот для Touchstone")
    if traces[0].x_unit and traces[0].x_unit.lower() != "hz":
        raise ValueError(f"Touchstone требует ось частот в Hz, получено {traces[0].x_unit!r}")
    if any(t.x != freqs or not t.is_complex for t in traces):
        raise ValueError("S-параметры должны быть комплексными и с одной осью частот")
    if any(not math.isfinite(fr) for fr in freqs) or any(
            not (math.isfinite(v) and math.isfinite(im)) for t in traces for v, im in zip(t.y, t.im)):
        raise ValueError("данные Touchstone содержат NaN или Inf")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="\n", encoding="utf-8") as f:
        for line in _comment_lines(comments):
            f.write(f"! {line}\n")
        f.write(f"! columns: freq, {', '.join(f'{k} Re, {k} Im' for k in order)}\n")
        f.write(f"# HZ S RI R {z0:g}\n")
        if n_ports <= 2:
            for i, fr in enumerate(freqs):
                vals = " ".join(f"{t.y[i]: .9e} {t.im[i]: .9e}" for t in traces)
                f.write(f"{fr:.9e} {vals}\n")
        else:
            # Touchstone 1.x для 3 и 4 портов: по одной строке матрицы (n_ports пар) на строку текста
            for i, fr in enumerate(freqs):
                for row in range(n_ports):
                    chunk = traces[row * n_ports:(row + 1) * n_ports]
                    vals = " ".join(f"{t.y[i]: .9e} {t.im[i]: .9e}" for t in chunk)
                    prefix = f"{fr:.9e} " if row == 0 else " " * 16
                    f.write(f"{prefix}{vals}\n")
    return path
