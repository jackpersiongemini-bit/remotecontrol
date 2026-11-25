"""Инструменты графиков: анализ трасс, математика (DATA −/+/÷/× MEM и формула f(x, y, mem))
и встроенная панель настроек графика (вкладка SET).
"""
from __future__ import annotations

import ast
from bisect import bisect_left
from dataclasses import dataclass
import math
from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from ..core.trace import Trace
from ..core.units import si_format, si_parse
from .theme import (UI_BORDER_RADIUS_PX, UI_BORDER_WIDTH_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER,
                    UI_COLOR_BORDER_LIGHT, UI_COLOR_ERROR, UI_COLOR_INVERT, UI_COLOR_INVERT_HOVER,
                    UI_COLOR_PANEL, UI_COLOR_TEXT, UI_COLOR_TEXT_INVERSE, UI_COLOR_TEXT_MUTED,
                    UI_FONT_SIZE_PT, UI_FONT_WEIGHT_SEMIBOLD, UI_MONOSPACE_FONT_FAMILY, button_qss, checkbox_qss,
                    input_qss, render_qss)
from .widgets import CommitLineEdit

__all__ = [
    "ANALYSIS_METRICS",
    "ANALYSIS_KEYS",
    "MATH_MODES",
    "MATH_LABELS",
    "XRangeSet",
    "parse_x_range_expr",
    "analyze_trace",
    "format_analysis",
    "validate_math_expr",
    "eval_math_expr",
    "trace_math",
    "trace_diff",
    "PlotSettingsWidget",
]

#: (ключ, короткая подпись, подсказка)
ANALYSIS_METRICS: tuple[tuple[str, str, str], ...] = (
    ("mean", "MEAN", "Arithmetic mean of displayed trace values"),
    ("min", "MIN", "Minimum value and its X coordinate"),
    ("max", "MAX", "Maximum value and its X coordinate"),
    ("std", "STD", "Standard deviation (RMS spread around the mean)"),
    ("pp", "P-P", "Peak-to-peak span (Max − Min)"),
    ("flatness", "FLAT", "Flatness: ±(Max − Min)/2 relative to the linear best-fit line"),
    ("nonlin", "NONLIN", "Nonlinearity: max deviation from the linear best-fit line"),
    ("slope", "SLOPE", "Linear regression slope (dY / dX)"),
    ("rms", "RMS", "Root mean square value (power-domain average for dB/dBm)"),
    ("bw3", "BW-3dB", "-3 dB bandwidth around the peak"),
)
ANALYSIS_KEYS: tuple[str, ...] = tuple(k for k, _lbl, _tip in ANALYSIS_METRICS)

MATH_MODES: tuple[str, ...] = ("OFF", "SUB", "ADD", "DIV", "MUL", "EXPR")
MATH_LABELS: dict[str, str] = {
    "OFF": "OFF",
    "SUB": "D−M",
    "ADD": "D+M",
    "DIV": "D/M",
    "MUL": "D×M",
    "EXPR": "f(x,y)",
}
_MATH_TAGS: dict[str, str] = {
    "SUB": "DATA−MEM",
    "ADD": "DATA+MEM",
    "DIV": "DATA/MEM",
    "MUL": "DATA×MEM",
    "EXPR": "MATH",
}
_SI_UNITS = ("W", "s", "Hz", "V", "A")

# ------------------------------------------------------------ безопасный вычислитель формул MATH
_SAFE_FUNCS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "db": lambda v: 10.0 * math.log10(v) if v > 0 else math.nan,
    "db20": lambda v: 20.0 * math.log10(abs(v)) if v != 0 else math.nan,
    "lin": lambda v: 10.0 ** (v / 10.0),
}
_SAFE_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e}
_SAFE_VARS: frozenset[str] = frozenset(("x", "y", "mem", "m"))
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: (a / b) if b != 0 else math.nan,
    ast.FloorDiv: lambda a, b: (a // b) if b != 0 else math.nan,
    ast.Mod: lambda a, b: (a % b) if b != 0 else math.nan,
    ast.Pow: lambda a, b: a ** b,
}
_UNARY_OPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


def _check_ast(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _check_ast(node.body)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("Only numeric constants are allowed")
    elif isinstance(node, ast.Name):
        name = node.id.lower()
        if name not in _SAFE_VARS and name not in _SAFE_CONSTS:
            raise ValueError(f"Unknown symbol: {node.id}")
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ValueError("Unsupported operator")
        _check_ast(node.left)
        _check_ast(node.right)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ValueError("Unsupported unary operator")
        _check_ast(node.operand)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id.lower() not in _SAFE_FUNCS:
            raise ValueError("Unsupported function")
        if node.keywords:
            raise ValueError("Keyword arguments are not supported")
        for arg in node.args:
            _check_ast(arg)
    else:
        raise ValueError(f"Unsupported syntax: {type(node).__name__}")


def _eval_node(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id.lower()]
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, env))
    if isinstance(node, ast.BinOp):
        a = _eval_node(node.left, env)
        b = _eval_node(node.right, env)
        return _BIN_OPS[type(node.op)](a, b)
    if isinstance(node, ast.Call):
        fn = _SAFE_FUNCS[node.func.id.lower()]
        args = [_eval_node(a, env) for a in node.args]
        return float(fn(*args))
    return math.nan


def validate_math_expr(expr: str) -> tuple[bool, str]:
    """Проверить математическую формулу для трассы (переменные ``x``, ``y``, ``mem``)."""
    src = str(expr or "").strip()
    if not src:
        return False, "Empty expression"
    try:
        tree = ast.parse(src, mode="eval")
        _check_ast(tree)
    except (SyntaxError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def _compile_math_expr(expr: str) -> ast.AST | None:
    src = str(expr or "").strip()
    if not src:
        return None
    try:
        tree = ast.parse(src, mode="eval")
        _check_ast(tree)
        return tree
    except (SyntaxError, ValueError):
        return None


def eval_math_expr(expr: str, x: float, y: float, mem: float = math.nan) -> float:
    """Вычислить формулу ``expr`` в одной точке ``(x, y, mem)``."""
    tree = _compile_math_expr(expr)
    if tree is None:
        return math.nan
    env = {**_SAFE_CONSTS, "x": float(x), "y": float(y), "mem": float(mem), "m": float(mem)}
    try:
        val = float(_eval_node(tree, env))
        return val if math.isfinite(val) else math.nan
    except (ValueError, ZeroDivisionError, OverflowError, TypeError):
        return math.nan


# ------------------------------------------------------------ форматирование и анализ трасс
def _fmt_y(value: float, unit: str, *, diff: bool = False) -> str:
    if not math.isfinite(value):
        return "---"
    u = "dB" if (diff and unit == "dBm") else unit
    if u in _SI_UNITS:
        return si_format(value, u, 4)
    if u:
        return f"{value:.2f} {u}"
    return f"{value:.3g}"


def _fmt_x(value: float, unit: str) -> str:
    if not math.isfinite(value):
        return "---"
    if unit in _SI_UNITS:
        return si_format(value, unit, 4)
    return f"{value:g} {unit}".strip()


def _fmt_slope(slope: float, x_unit: str, y_unit: str, span_x: float) -> str:
    if not math.isfinite(slope):
        return "---"
    yu = "dB" if y_unit == "dBm" else (y_unit or "1")
    if x_unit == "Hz":
        if span_x >= 1e8:
            return f"{slope * 1e9:+.2f} {yu}/GHz"
        if span_x >= 1e5:
            return f"{slope * 1e6:+.2f} {yu}/MHz"
        if span_x >= 1e2:
            return f"{slope * 1e3:+.2f} {yu}/kHz"
        return f"{slope:+.3g} {yu}/Hz"
    if x_unit == "s":
        if span_x <= 1e-4:
            return f"{slope * 1e-6:+.2f} {yu}/µs"
        if span_x <= 0.1:
            return f"{slope * 1e-3:+.2f} {yu}/ms"
        return f"{slope:+.2f} {yu}/s"
    if x_unit:
        return f"{slope:+.3f} {yu}/{x_unit}"
    return f"{slope:+.3g}"


def _bw3(xs: list[float], ys: list[float]) -> float | None:
    """Ширина полосы по уровню −3 дБ относительно максимума (с линейной интерполяцией границ)."""
    n = len(ys)
    if n < 3:
        return None
    pk_i = max(range(n), key=lambda i: ys[i])
    thr = ys[pk_i] - 3.0
    left_x: float | None = None
    for i in range(pk_i - 1, -1, -1):
        if ys[i] <= thr:
            dy = ys[i + 1] - ys[i]
            t = (thr - ys[i]) / dy if dy != 0 else 0.0
            left_x = xs[i] + t * (xs[i + 1] - xs[i])
            break
    right_x: float | None = None
    for i in range(pk_i + 1, n):
        if ys[i] <= thr:
            dy = ys[i - 1] - ys[i]
            t = (thr - ys[i]) / dy if dy != 0 else 0.0
            right_x = xs[i] + t * (xs[i - 1] - xs[i])
            break
    if left_x is None or right_x is None or right_x <= left_x:
        return None
    return right_x - left_x


@dataclass(frozen=True)
class XRangeSet:
    """Множество точек и отрезков по оси X вида ``[x,y]+5-[a,b]`` (поддерживает и ``{...}``)."""

    raw: str
    ops: tuple[tuple[str, tuple[tuple[float, float], ...]], ...]

    @property
    def is_empty(self) -> bool:
        return not self.ops

    def contains(self, value: float | None) -> bool:
        if self.is_empty:
            return True
        if value is None or not math.isfinite(value):
            return False
        x = float(value)
        included = self.ops[0][0] == "-"
        for op, items in self.ops:
            hit = False
            for lo, hi in items:
                if lo == hi:
                    tol = max(1e-9, 1e-6 * abs(lo))
                else:
                    tol = 1e-9 * max(abs(lo), abs(hi), 1.0)
                if (lo - tol) <= x <= (hi + tol):
                    hit = True
                    break
            if op == "+" and hit:
                included = True
            elif op == "-" and hit:
                included = False
        return included

    def intervals(self, full_lo: float, full_hi: float) -> list[tuple[float, float]]:
        """Вернуть отсортированный список включённых отрезков внутри ``[full_lo, full_hi]``."""
        if self.is_empty or full_hi < full_lo:
            return []
        active: list[tuple[float, float]] = [(full_lo, full_hi)] if self.ops[0][0] == "-" else []
        for op, items in self.ops:
            if op == "+":
                for lo, hi in items:
                    a, b = max(full_lo, lo), min(full_hi, hi)
                    if b >= a:
                        active.append((a, b))
                active.sort()
                merged: list[tuple[float, float]] = []
                for a, b in active:
                    if merged and a <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                    else:
                        merged.append((a, b))
                active = merged
            else:
                for lo, hi in items:
                    nxt: list[tuple[float, float]] = []
                    for a, b in active:
                        if hi < a or lo > b:
                            nxt.append((a, b))
                        else:
                            if a < lo:
                                nxt.append((a, lo))
                            if b > hi:
                                nxt.append((hi, b))
                    active = nxt
        return active


def _split_range_terms(expr: str) -> list[tuple[str, str]]:
    terms: list[tuple[str, str]] = []
    brace = 0
    bracket = 0
    op = "+"
    buf: list[str] = []
    n = len(expr)
    for i, ch in enumerate(expr):
        if ch == "{":
            brace += 1
            buf.append(ch)
        elif ch == "}":
            brace -= 1
            if brace < 0:
                raise ValueError("Unmatched '}'")
            buf.append(ch)
        elif ch == "[":
            bracket += 1
            buf.append(ch)
        elif ch == "]":
            bracket -= 1
            if bracket < 0:
                raise ValueError("Unmatched ']'")
            buf.append(ch)
        elif ch in ("+", "-") and brace == 0 and bracket == 0:
            cur = "".join(buf).strip()
            prev_ch = cur[-1] if cur else ""
            rest = expr[i + 1:].lstrip()
            if not cur and not terms and ch == "-" and rest.startswith(("[", "{")):
                op = "-"
            elif not cur or prev_ch in ("e", "E", ","):
                buf.append(ch)
            else:
                terms.append((op, cur))
                op = ch
                buf = []
        else:
            buf.append(ch)
    if brace != 0:
        raise ValueError("Unclosed '{'")
    if bracket != 0:
        raise ValueError("Unclosed '['")
    tail = "".join(buf).strip()
    if tail:
        terms.append((op, tail))
    elif terms or op == "-":
        raise ValueError(f"Trailing '{op}' operator")
    return terms


def _split_range_items(group: str) -> list[str]:
    s = group.strip()
    if s.startswith("{"):
        if not s.endswith("}"):
            raise ValueError("Expected closing '}'")
        s = s[1:-1].strip()
    if not s:
        return []
    parts: list[str] = []
    bracket = 0
    buf: list[str] = []
    for ch in s:
        if ch == "[":
            bracket += 1
            buf.append(ch)
        elif ch == "]":
            bracket -= 1
            if bracket < 0:
                raise ValueError("Unmatched ']'")
            buf.append(ch)
        elif ch == "," and bracket == 0:
            item = "".join(buf).strip()
            if not item:
                raise ValueError("Empty element near ','")
            parts.append(item)
            buf = []
        else:
            buf.append(ch)
    if bracket != 0:
        raise ValueError("Unclosed '['")
    last = "".join(buf).strip()
    if last:
        parts.append(last)
    return parts


def _parse_x_scalar(text: str, unit: str) -> float:
    s = str(text or "").strip().replace(",", ".")
    if not s:
        raise ValueError("Empty number")
    try:
        val = float(si_parse(s, unit or ""))
    except ValueError:
        if unit and s.lower().endswith(unit.lower()):
            val = float(s[:-len(unit)].strip())
        else:
            val = float(s)
    if not math.isfinite(val):
        raise ValueError(f"Invalid value: {text!r}")
    return val


def parse_x_range_expr(expr: str, unit: str = "") -> XRangeSet:
    """Разобрать выражение диапазона X вида ``[x,y]+5-[a,b]`` (или ``{...}``) с SI-префиксами."""
    raw = str(expr or "").strip()
    if not raw:
        return XRangeSet("", ())
    terms = _split_range_terms(raw)
    ops: list[tuple[str, tuple[tuple[float, float], ...]]] = []
    for op, grp in terms:
        items_str = _split_range_items(grp)
        if not items_str:
            raise ValueError(f"Empty term {grp!r}")
        parsed_items: list[tuple[float, float]] = []
        for it in items_str:
            if it.startswith("["):
                if not it.endswith("]"):
                    raise ValueError(f"Unclosed interval {it!r}")
                inner = it[1:-1].strip()
                if not inner:
                    raise ValueError("Empty interval []")
                bounds = [b.strip() for b in inner.split(",")]
                if len(bounds) == 1:
                    val = _parse_x_scalar(bounds[0], unit)
                    parsed_items.append((val, val))
                elif len(bounds) == 2:
                    a = _parse_x_scalar(bounds[0], unit)
                    b = _parse_x_scalar(bounds[1], unit)
                    parsed_items.append((min(a, b), max(a, b)))
                else:
                    raise ValueError(f"Interval must have 1 or 2 bounds: {it!r}")
            else:
                val = _parse_x_scalar(it, unit)
                parsed_items.append((val, val))
        ops.append((op, tuple(parsed_items)))
    return XRangeSet(raw, tuple(ops))


def analyze_trace(trace: Trace | None, *, x_range: tuple[float, float] | None = None,
                  x_filter: XRangeSet | Callable[[float], bool] | None = None,
                  y_transform=None) -> dict[str, Any]:
    """Посчитать набор статистических и ВЧ-метрик трассы в пределах ``x_range`` / ``x_filter``."""
    if trace is None or len(trace) == 0:
        return {}
    xs: list[float] = []
    ys: list[float] = []
    x_lo = x_hi = None
    if x_range is not None and math.isfinite(x_range[0]) and math.isfinite(x_range[1]):
        x_lo, x_hi = (x_range[0], x_range[1]) if x_range[0] <= x_range[1] else (x_range[1], x_range[0])
    check_x = None
    if isinstance(x_filter, XRangeSet):
        check_x = x_filter.contains if not x_filter.is_empty else None
    elif callable(x_filter):
        check_x = x_filter
    isfinite = math.isfinite
    for x, y in zip(trace.x, trace.y):
        if not (isfinite(x) and isfinite(y)):
            continue
        if x_lo is not None and (x < x_lo or x > x_hi):
            continue
        if check_x is not None and not check_x(x):
            continue
        yv = y_transform(y) if y_transform is not None else y
        if not isfinite(yv):
            continue
        xs.append(float(x))
        ys.append(float(yv))
    n = len(ys)
    if n == 0:
        return {}
    mean = sum(ys) / n
    min_i = min(range(n), key=lambda i: ys[i])
    max_i = max(range(n), key=lambda i: ys[i])
    y_min, x_min = ys[min_i], xs[min_i]
    y_max, x_max = ys[max_i], xs[max_i]
    pp = y_max - y_min
    var = sum((y - mean) ** 2 for y in ys) / n
    std = math.sqrt(var)
    if trace.y_unit in ("dB", "dBm") and y_transform is None:
        # В логарифмических единицах (dB/dBm) RMS считаем в линейной области мощности
        lin_mean = sum(10.0 ** (max(-300.0, min(300.0, y)) / 10.0) for y in ys) / n
        rms = 10.0 * math.log10(lin_mean) if lin_mean > 0.0 else math.nan
    else:
        rms = math.sqrt(sum(y * y for y in ys) / n)

    slope: float | None = None
    nonlin: float | None = None
    flatness: float = 0.0
    span_x = abs(xs[-1] - xs[0]) if n >= 2 else 0.0
    if n >= 2:
        mx = sum(xs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0.0:
            sxy = sum((x - mx) * (y - mean) for x, y in zip(xs, ys))
            a = sxy / sxx
            b = mean - a * mx
            slope = a
            residuals = [y - (a * x + b) for x, y in zip(xs, ys)]
        else:
            slope = 0.0
            residuals = [y - mean for y in ys]
        nonlin = max(abs(r) for r in residuals)
        flatness = 0.5 * (max(residuals) - min(residuals))

    bw3 = _bw3(xs, ys) if trace.y_unit in ("dB", "dBm") and y_transform is None else None
    return {
        "count": n,
        "mean": mean,
        "min": y_min,
        "min_x": x_min,
        "max": y_max,
        "max_x": x_max,
        "std": std,
        "rms": rms,
        "pp": pp,
        "flatness": flatness,
        "slope": slope,
        "nonlin": nonlin,
        "bw3": bw3,
        "span_x": span_x,
    }


def format_analysis(stats: dict[str, Any], metrics: list[str] | tuple[str, ...],
                    x_unit: str, y_unit: str) -> list[str]:
    """Отформатировать выбранные метрики в список коротких элементов ``["MEAN ...", ...]``."""
    if not stats or not metrics:
        return []
    out: list[str] = []
    span_x = float(stats.get("span_x", 0.0) or 0.0)
    for key in metrics:
        if key == "mean" and "mean" in stats:
            out.append(f"MEAN {_fmt_y(stats['mean'], y_unit)}")
        elif key == "min" and "min" in stats:
            out.append(f"MIN {_fmt_y(stats['min'], y_unit)}@{_fmt_x(stats['min_x'], x_unit)}")
        elif key == "max" and "max" in stats:
            out.append(f"MAX {_fmt_y(stats['max'], y_unit)}@{_fmt_x(stats['max_x'], x_unit)}")
        elif key == "std" and "std" in stats:
            out.append(f"STD {_fmt_y(stats['std'], y_unit, diff=True)}")
        elif key == "pp" and "pp" in stats:
            out.append(f"P-P {_fmt_y(stats['pp'], y_unit, diff=True)}")
        elif key == "flatness" and "flatness" in stats:
            out.append(f"FLAT ±{_fmt_y(stats['flatness'], y_unit, diff=True)}")
        elif key == "nonlin":
            v = stats.get("nonlin")
            out.append(f"NONLIN {_fmt_y(v, y_unit, diff=True)}" if v is not None else "NONLIN ---")
        elif key == "slope":
            v = stats.get("slope")
            out.append(f"SLOPE {_fmt_slope(v, x_unit, y_unit, span_x)}" if v is not None else "SLOPE ---")
        elif key == "rms" and "rms" in stats:
            out.append(f"RMS {_fmt_y(stats['rms'], y_unit)}")
        elif key == "bw3":
            v = stats.get("bw3")
            out.append(f"BW-3dB {_fmt_x(v, x_unit)}" if v is not None else "BW-3dB ---")
    return out


def _interp_y(xs, ys, x: float) -> float:
    n = len(xs)
    if n == 0:
        return math.nan
    if n == 1:
        return float(ys[0])
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    i = bisect_left(xs, x)
    if i <= 0:
        return float(ys[0])
    if i >= n:
        return float(ys[-1])
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return float(y0)
    t = (x - x0) / (x1 - x0)
    return float(y0 + t * (y1 - y0))


def trace_math(
    live: Trace,
    mem: Trace | None,
    mode: str,
    expr: str = "",
    y_transform=None,
) -> Trace:
    """Применить математическую операцию к трассе: ``SUB / ADD / DIV / MUL / EXPR``."""
    mode = str(mode or "OFF").upper()
    if mode == "OFF":
        return live
    tree = _compile_math_expr(expr) if mode == "EXPR" else None
    if mode == "EXPR" and tree is None:
        return live
    if mode in ("SUB", "ADD", "DIV", "MUL") and mem is None:
        return live

    same_grid = (
        mem is not None
        and len(live.x) == len(mem.x)
        and len(live.x) > 0
        and live.x[0] == mem.x[0]
        and live.x[-1] == mem.x[-1]
    )
    mem_xs = list(mem.x) if (mem is not None and not same_grid) else []
    mem_ys = (
        [y_transform(v) if (y_transform is not None and math.isfinite(v)) else v for v in mem.y]
        if (mem is not None and not same_grid)
        else []
    )

    ys: list[float] = []
    isfinite = math.isfinite
    for i, (xl, yl) in enumerate(zip(live.x, live.y)):
        if not (isfinite(xl) and isfinite(yl)):
            ys.append(math.nan)
            continue
        a = y_transform(yl) if y_transform is not None else float(yl)
        if not isfinite(a):
            ys.append(math.nan)
            continue
        if mem is None:
            b = math.nan
        elif same_grid:
            ym = mem.y[i]
            b = (y_transform(ym) if (y_transform is not None and isfinite(ym)) else float(ym)) if isfinite(ym) else math.nan
        else:
            b = _interp_y(mem_xs, mem_ys, xl)

        if mode == "SUB":
            v = a - b if isfinite(b) else math.nan
        elif mode == "ADD":
            v = a + b if isfinite(b) else math.nan
        elif mode == "DIV":
            v = (a / b) if (isfinite(b) and abs(b) > 1e-30) else math.nan
        elif mode == "MUL":
            v = a * b if isfinite(b) else math.nan
        elif mode == "EXPR" and tree is not None:
            env = {**_SAFE_CONSTS, "x": float(xl), "y": a, "mem": b, "m": b}
            try:
                v = float(_eval_node(tree, env))
            except (ValueError, ZeroDivisionError, OverflowError, TypeError):
                v = math.nan
        else:
            v = a
        ys.append(v if isfinite(v) else math.nan)

    if mode == "SUB":
        y_unit = "dB" if live.y_unit in ("dBm", "dB") else live.y_unit
        prefix = "Δ"
    elif mode == "ADD":
        y_unit = live.y_unit
        prefix = "+"
    elif mode == "DIV":
        y_unit = ""
        prefix = "÷"
    elif mode == "MUL":
        y_unit = ""
        prefix = "×"
    else:
        y_unit = live.y_unit
        prefix = "f:"
    name = f"{prefix}{live.name}" if live.name else _MATH_TAGS.get(mode, "MATH")
    return Trace(
        live.x,
        tuple(ys),
        live.x_unit,
        y_unit,
        name=name,
        timestamp=live.timestamp,
        meta=dict(live.meta or {}),
        uniform=live.uniform,
    )


def trace_diff(live: Trace, mem: Trace, y_transform=None) -> Trace:
    """Вычислить разность ``live − mem`` (обёртка над :func:`trace_math`)."""
    return trace_math(live, mem, "SUB", y_transform=y_transform)


# ------------------------------------------------------------ встроенная панель настроек графика (вкладка SET)
_SETTINGS_STYLE = render_qss("""
    PlotSettingsWidget {
        background-color: $UI_COLOR_PANEL;
    }
    PlotSettingsWidget QWidget {
        background: transparent;
        font-family: $UI_MONOSPACE_FONT_FAMILY;
        font-size: ${UI_FONT_SIZE_PT}pt;
        color: $UI_COLOR_TEXT_MUTED;
    }
    PlotSettingsWidget QLabel#sec {
        font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
        color: $UI_COLOR_TEXT;
    }
    PlotSettingsWidget QLabel#muted {
        color: $UI_COLOR_BORDER_LIGHT;
    }
    PlotSettingsWidget QLabel#err {
        color: $UI_COLOR_ERROR;
    }
    PlotSettingsWidget QCheckBox {
        spacing: 6px;
        color: $UI_COLOR_TEXT;
    }
    PlotSettingsWidget QFrame#sep {
        background-color: $UI_COLOR_BORDER;
        max-height: 1px;
        min-height: 1px;
    }
""") + "\n" + input_qss("PlotSettingsWidget") + "\n" + button_qss(
    "PlotSettingsWidget", border_token="UI_COLOR_BACKGROUND"
) + "\n" + checkbox_qss("PlotSettingsWidget ")


def _parse_axis_number(text: str, unit: str) -> float | None:
    s = text.strip().replace(",", ".")
    if not s:
        return None
    if unit and s.endswith(unit):
        s_plain = s[:-len(unit)].strip()
    else:
        s_plain = s
    try:
        v = float(s_plain)
        return v if math.isfinite(v) else None
    except ValueError:
        pass
    if unit in _SI_UNITS:
        try:
            v = si_parse(s, unit)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            pass
    return None


class PlotSettingsWidget(QWidget):
    """Встроенная панель настроек графика: SCALE, MEM, MATH и ANALYSIS (для всех типов графиков)."""

    def __init__(self, plot=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.plot = plot
        self.setStyleSheet(_SETTINGS_STYLE)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # --- SCALE ---
        sec_scale = QLabel("SCALE")
        sec_scale.setObjectName("sec")
        lay.addWidget(sec_scale)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self.auto_btn = self._btn("AUTO", "Use device / automatic Y scale", self._on_auto, checkable=True)
        self.man_btn = self._btn("MAN", "Use manual Y scale (REF / DIV / DIVS)", self._on_man, checkable=True)
        self.fit_btn = self._btn("FIT", "Fit Y scale tightly to current trace and switch to MAN", self._on_fit)
        self.reset_btn = self._btn("RESET", "Reset Y scale to AUTO and X range to full span", self._on_reset)
        for b in (self.auto_btn, self.man_btn, self.fit_btn, self.reset_btn):
            mode_row.addWidget(b)
        lay.addLayout(mode_row)

        scale_grid = QGridLayout()
        scale_grid.setHorizontalSpacing(6)
        scale_grid.setVerticalSpacing(4)
        self.ref_edit = CommitLineEdit("Top of Y scale (REF); Enter to apply", self._apply_scale)
        self.div_edit = CommitLineEdit("Y value per division (DIV); Enter to apply", self._apply_scale)
        self.divs_edit = CommitLineEdit("Number of Y grid divisions (2..20); Enter to apply", self._apply_scale)
        self.xmin_edit = CommitLineEdit("Manual X minimum (blank = full span); Enter to apply", self._apply_x)
        self.xmax_edit = CommitLineEdit("Manual X maximum (blank = full span); Enter to apply", self._apply_x)
        for ed in (self.ref_edit, self.div_edit, self.divs_edit, self.xmin_edit, self.xmax_edit):
            ed.setMinimumWidth(82)

        scale_grid.addWidget(self._lbl("REF"), 0, 0)
        scale_grid.addWidget(self.ref_edit, 0, 1)
        scale_grid.addWidget(self._lbl("DIV"), 0, 2)
        scale_grid.addWidget(self.div_edit, 0, 3)

        scale_grid.addWidget(self._lbl("DIVS"), 1, 0)
        scale_grid.addWidget(self.divs_edit, 1, 1)
        self.grid_cb = QCheckBox("Grid")
        self.grid_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.grid_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.grid_cb.setToolTip("Show / hide coordinate grid lines")
        self.grid_cb.toggled.connect(self._on_grid)
        self.points_cb = QCheckBox("Points")
        self.points_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.points_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.points_cb.setToolTip("Draw sample dots along the trace")
        self.points_cb.toggled.connect(self._on_points)
        disp_row = QHBoxLayout()
        disp_row.setContentsMargins(0, 0, 0, 0)
        disp_row.setSpacing(8)
        disp_row.addWidget(self.grid_cb)
        disp_row.addWidget(self.points_cb)
        scale_grid.addLayout(disp_row, 1, 2, 1, 2)

        scale_grid.addWidget(self._lbl("X MIN"), 2, 0)
        scale_grid.addWidget(self.xmin_edit, 2, 1)
        scale_grid.addWidget(self._lbl("X MAX"), 2, 2)
        scale_grid.addWidget(self.xmax_edit, 2, 3)
        lay.addLayout(scale_grid)

        lay.addWidget(self._sep())

        # --- MEMORY (MEM) ---
        mem_hdr = QHBoxLayout()
        mem_lbl = QLabel("MEM")
        mem_lbl.setObjectName("sec")
        self.mem_info = QLabel("")
        self.mem_info.setObjectName("muted")
        mem_hdr.addWidget(mem_lbl)
        mem_hdr.addStretch()
        mem_hdr.addWidget(self.mem_info)
        lay.addLayout(mem_hdr)

        mem_row = QHBoxLayout()
        mem_row.setSpacing(4)
        self.mem_store_btn = self._btn("STORE", "Capture current trace into memory (or double-click graph)", self._on_mem_store)
        self.mem_show_btn = self._btn("SHOW", "Show / hide stored memory trace overlay", self._on_mem_show, checkable=True)
        self.mem_diff_btn = self._btn("DATA−MEM", "Display difference trace: Live − Memory", self._on_mem_diff, checkable=True)
        self.mem_clear_btn = self._btn("CLEAR", "Clear stored memory trace", self._on_mem_clear)
        for b in (self.mem_store_btn, self.mem_show_btn, self.mem_diff_btn, self.mem_clear_btn):
            mem_row.addWidget(b)
        lay.addLayout(mem_row)

        lay.addWidget(self._sep())

        # --- MATH ---
        math_hdr = QHBoxLayout()
        math_lbl = QLabel("MATH")
        math_lbl.setObjectName("sec")
        self.math_status = QLabel("")
        self.math_status.setObjectName("muted")
        math_hdr.addWidget(math_lbl)
        math_hdr.addStretch()
        math_hdr.addWidget(self.math_status)
        lay.addLayout(math_hdr)

        math_row = QHBoxLayout()
        math_row.setSpacing(4)
        self.math_buttons: dict[str, QPushButton] = {}
        math_tips = {
            "OFF": "Disable trace math (show live data)",
            "SUB": "Live − Memory",
            "ADD": "Live + Memory",
            "DIV": "Live / Memory",
            "MUL": "Live × Memory",
            "EXPR": "Custom math formula f(x, y, mem)",
        }
        for mode in MATH_MODES:
            b = self._btn(MATH_LABELS[mode], math_tips[mode], lambda _=False, m=mode: self._on_math_mode(m), checkable=True)
            self.math_buttons[mode] = b
            math_row.addWidget(b)
        lay.addLayout(math_row)

        expr_row = QHBoxLayout()
        expr_row.setSpacing(6)
        expr_row.addWidget(self._lbl("f(x,y)"))
        self.math_expr_edit = CommitLineEdit(
            "Custom math formula using x, y, mem (e.g. y + 3, y - mem, 10**(y/10)); Enter to apply",
            self._apply_math_expr,
            placeholder="y - mem, y + 3, abs(y) ...",
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        self.math_expr_edit.setMinimumWidth(82)
        expr_row.addWidget(self.math_expr_edit, 1)
        lay.addLayout(expr_row)

        lay.addWidget(self._sep())

        # --- ANALYSIS ---
        an_hdr = QHBoxLayout()
        an_lbl = QLabel("ANALYSIS")
        an_lbl.setObjectName("sec")
        all_btn = self._btn("ALL", "Enable all analysis metrics", lambda: self._set_all_analysis(True))
        none_btn = self._btn("NONE", "Disable all analysis metrics", lambda: self._set_all_analysis(False))
        an_hdr.addWidget(an_lbl)
        an_hdr.addStretch()
        an_hdr.addWidget(all_btn)
        an_hdr.addWidget(none_btn)
        lay.addLayout(an_hdr)

        an_grid = QGridLayout()
        an_grid.setHorizontalSpacing(10)
        an_grid.setVerticalSpacing(4)
        self.analysis_checks: dict[str, QCheckBox] = {}
        for idx, (key, label, tip) in enumerate(ANALYSIS_METRICS):
            cb = QCheckBox(label)
            cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.setToolTip(tip)
            cb.toggled.connect(lambda checked, k=key: self._on_analysis_toggled(k, checked))
            self.analysis_checks[key] = cb
            an_grid.addWidget(cb, idx // 2, idx % 2)
        lay.addLayout(an_grid)

        if self.plot is not None:
            self.sync_from_plot()

    @staticmethod
    def _lbl(text: str) -> QLabel:
        l = QLabel(text)
        l.setObjectName("muted")
        return l

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setObjectName("sep")
        return f

    @staticmethod
    def _btn(text: str, tip: str, slot, *, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def set_plot(self, plot, *, force: bool = True) -> None:
        changed = plot is not self.plot
        self.plot = plot
        if plot is not None:
            self.sync_from_plot(force=force or changed)

    def sync_from_plot(self, *, force: bool = False) -> None:
        if self.plot is None:
            return
        st = self.plot.plot_settings_state()
        is_man = bool(st["manual_scale"])
        self.auto_btn.setChecked(not is_man)
        self.man_btn.setChecked(is_man)
        yu = st.get("y_unit", "")
        xu = st.get("x_unit", "")
        ref = st.get("ref")
        div = st.get("div")
        divs = st.get("divs_y", 6)
        self.ref_edit.set_committed(_fmt_y(ref, yu) if ref is not None else "", force=force)
        self.div_edit.set_committed(_fmt_y(div, yu, diff=True) if div is not None else "", force=force)
        self.divs_edit.set_committed(str(int(divs)), force=force)
        self.divs_edit.setEnabled(bool(st.get("supports_divs", True)))
        xr = st.get("manual_x_range")
        self.xmin_edit.set_committed(_fmt_x(xr[0], xu) if xr else "", force=force)
        self.xmax_edit.set_committed(_fmt_x(xr[1], xu) if xr else "", force=force)
        self.xmin_edit.setPlaceholderText("auto")
        self.xmax_edit.setPlaceholderText("auto")
        self.xmin_edit.setEnabled(bool(st.get("supports_x_zoom", True)))
        self.xmax_edit.setEnabled(bool(st.get("supports_x_zoom", True)))

        self.grid_cb.blockSignals(True)
        self.grid_cb.setChecked(bool(st.get("show_grid", True)))
        self.grid_cb.blockSignals(False)
        self.points_cb.blockSignals(True)
        self.points_cb.setChecked(bool(st.get("show_points", False)))
        self.points_cb.setEnabled(bool(st.get("supports_points", True)))
        self.points_cb.blockSignals(False)

        has_mem = bool(st.get("has_memory", False))
        mem_cnt = int(st.get("memory_count", 0))
        self.mem_info.setText(f"{mem_cnt} stored" if has_mem else "empty (2× click)")
        self.mem_show_btn.setEnabled(has_mem)
        self.mem_diff_btn.setEnabled(has_mem)
        self.mem_clear_btn.setEnabled(has_mem)
        self.mem_show_btn.setChecked(has_mem and bool(st.get("memory_visible", True)))
        math_mode = str(st.get("math_mode", "SUB" if st.get("memory_diff", False) else "OFF") or "OFF").upper()
        math_expr = str(st.get("math_expr", "") or "")
        self.mem_diff_btn.setChecked(has_mem and math_mode == "SUB")

        for mode, btn in self.math_buttons.items():
            need_mem = mode in ("SUB", "ADD", "DIV", "MUL")
            btn.setEnabled(True if not need_mem else has_mem)
            btn.setChecked(mode == math_mode)
        self.math_expr_edit.set_committed(math_expr, invalid=False, force=force)
        if math_mode == "EXPR" and math_expr:
            self.math_status.setObjectName("muted")
            self.math_status.setText(f"f = {math_expr}")
        elif math_mode != "OFF":
            self.math_status.setObjectName("muted")
            self.math_status.setText(_MATH_TAGS.get(math_mode, math_mode))
        else:
            self.math_status.setObjectName("muted")
            self.math_status.setText("off")

        enabled = set(st.get("analysis_metrics", ()))
        for key, cb in self.analysis_checks.items():
            cb.blockSignals(True)
            cb.setChecked(key in enabled)
            cb.blockSignals(False)

    # ------------------------------------------------------------ обработчики
    def _on_auto(self) -> None:
        if self.plot is None:
            return
        self.plot.set_auto_scale()
        self.sync_from_plot(force=True)

    def _on_man(self) -> None:
        self._apply_scale()

    def _on_fit(self) -> None:
        if self.plot is None:
            return
        self.plot.autoscale_once()
        self.sync_from_plot(force=True)

    def _on_reset(self) -> None:
        if self.plot is None:
            return
        self.plot.reset_view()
        self.sync_from_plot(force=True)

    def _apply_scale(self) -> None:
        if self.plot is None:
            return
        st = self.plot.plot_settings_state()
        yu = st.get("y_unit", "")
        ref = _parse_axis_number(self.ref_edit.text(), yu)
        div_u = "dB" if yu == "dBm" else yu
        div = _parse_axis_number(self.div_edit.text(), div_u)
        if ref is None:
            ref = st.get("ref")
        if div is None or div <= 0:
            div = st.get("div")
        try:
            divs = max(2, min(20, int(float(self.divs_edit.text().strip()))))
        except ValueError:
            divs = int(st.get("divs_y", 6))
        if ref is not None and div is not None and div > 0:
            self.plot.set_manual_scale(ref, div, divs)
        self.sync_from_plot(force=True)

    def _apply_x(self) -> None:
        if self.plot is None:
            return
        st = self.plot.plot_settings_state()
        xu = st.get("x_unit", "")
        t0, t1 = self.xmin_edit.text().strip(), self.xmax_edit.text().strip()
        if not t0 and not t1:
            self.plot.set_x_zoom(None, None)
            self.sync_from_plot(force=True)
            return
        cur = st.get("x_bounds", (0.0, 1.0))
        x0 = _parse_axis_number(t0, xu) if t0 else cur[0]
        x1 = _parse_axis_number(t1, xu) if t1 else cur[1]
        if x0 is not None and x1 is not None and x1 > x0:
            self.plot.set_x_zoom(x0, x1)
        self.sync_from_plot(force=True)

    def _on_grid(self, checked: bool) -> None:
        if self.plot is not None:
            self.plot.set_show_grid(checked)

    def _on_points(self, checked: bool) -> None:
        if self.plot is not None:
            self.plot.set_show_points(checked)

    def _on_mem_store(self) -> None:
        if self.plot is None:
            return
        self.plot.store_memory()
        self.sync_from_plot(force=True)

    def _on_mem_show(self) -> None:
        if self.plot is None:
            return
        self.plot.set_memory_visible(self.mem_show_btn.isChecked())
        self.sync_from_plot(force=True)

    def _on_mem_diff(self) -> None:
        if self.plot is None:
            return
        self.plot.set_memory_diff(self.mem_diff_btn.isChecked())
        self.sync_from_plot(force=True)

    def _on_mem_clear(self) -> None:
        if self.plot is None:
            return
        self.plot.clear_memory()
        self.sync_from_plot(force=True)

    def _on_math_mode(self, mode: str) -> None:
        if self.plot is None:
            return
        if mode == "EXPR":
            expr = self.math_expr_edit.text().strip()
            if not expr:
                expr = "y - mem" if self.plot.plot_settings_state().get("has_memory") else "y"
                self.math_expr_edit.set_committed(expr, force=True)
            ok, err = validate_math_expr(expr)
            if not ok:
                self.math_status.setObjectName("err")
                self.math_status.setText(err)
                return
            self.plot.set_math("EXPR", expr)
        else:
            self.plot.set_math(mode)
        self.sync_from_plot(force=True)

    def _apply_math_expr(self) -> None:
        if self.plot is None:
            return
        expr = self.math_expr_edit.text().strip()
        if not expr:
            self.plot.set_math("OFF", "")
            self.sync_from_plot(force=True)
            return
        ok, err = validate_math_expr(expr)
        if not ok:
            self.math_expr_edit.set_invalid(True)
            self.math_status.setObjectName("err")
            self.math_status.setText(err)
            return
        self.plot.set_math("EXPR", expr)
        self.sync_from_plot(force=True)

    def _on_analysis_toggled(self, key: str, checked: bool) -> None:
        if self.plot is not None:
            self.plot.toggle_analysis_metric(key, checked)

    def _set_all_analysis(self, on: bool) -> None:
        if self.plot is None:
            return
        self.plot.set_analysis_metrics(list(ANALYSIS_KEYS) if on else [])
        self.sync_from_plot(force=True)
