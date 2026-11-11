"""Сохранение трасс в файл (CSV): одна таблица на несколько трасс.

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

__all__ = ["write_traces_csv"]


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
