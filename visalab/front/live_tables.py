"""Таблицы живых данных главного окна на базе единой :class:`MatrixTable`:
``PowerTable`` (ваттметр: частота × мощность источника) и ``CurrentTable`` (ток канала ИП).
"""
from __future__ import annotations

import math

from ..core.units import si_format
from .widgets import MatrixTable

__all__ = [
    "PLOT_MIN_HEIGHT",
    "PM_UNITS",
    "CURRENT_UNITS",
    "_PM_UNIT_LABELS",
    "_CURR_UNIT_LABELS",
    "_dbm",
    "_num",
    "_format_power",
    "_format_current",
    "PowerTable",
    "CurrentTable",
]

PLOT_MIN_HEIGHT = 240
PM_UNITS = ("dBm", "W", "dBW", "V", "dB")
_PM_UNIT_LABELS = {"dBm": "dBm", "W": "W", "dBW": "dBW", "V": "V (50 Ω)", "dB": "dB (Pout − Pin)"}
CURRENT_UNITS = ("AUTO", "mA", "A", "µA")
_CURR_UNIT_LABELS = {"AUTO": "Auto (SI)", "mA": "mA", "A": "A", "µA": "µA"}


def _dbm(v: float | None) -> str:
    return "" if v is None else f"{v:.2f} dBm"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0) if abs(dbm) <= 300.0 else math.nan


def _fmt_num(value: float) -> str:
    text = f"{round(float(value), 3):.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _format_power(dbm: float, unit: str, src_dbm: float | None = None) -> str:
    if unit == "W":
        w = _dbm_to_w(dbm)
        return si_format(w, "W", 4) if math.isfinite(w) else "--- W"
    if unit == "dBW":
        return f"{dbm - 30.0:.2f} dBW"
    if unit == "V":
        w = _dbm_to_w(dbm)
        return si_format(math.sqrt(w * 50.0), "V", 4) if math.isfinite(w) and w >= 0.0 else "--- V"
    if unit == "dB":
        return f"{dbm - src_dbm:+.2f} dB" if src_dbm is not None else "---"
    return f"{dbm:.2f} dBm"


def _format_current(amps: float, unit: str) -> str:
    if not math.isfinite(amps):
        return "---"
    if unit == "mA":
        return f"{amps * 1e3:.2f} mA"
    if unit == "A":
        return f"{amps:.4f} A"
    if unit == "µA":
        return f"{amps * 1e6:.1f} µA"
    return si_format(amps, "A", 4)


def _format_col_header(src_dbm: float | None, unit: str, has_numeric: bool) -> str:
    if src_dbm is None:
        return "---" if has_numeric else "POWER"
    if unit == "W":
        w = _dbm_to_w(src_dbm)
        return si_format(w, "W", 4) if math.isfinite(w) else f"{_fmt_num(src_dbm)} dBm"
    if unit == "dBW":
        return f"{_fmt_num(src_dbm - 30.0)} dBW"
    if unit == "V":
        w = _dbm_to_w(src_dbm)
        return si_format(math.sqrt(w * 50.0), "V", 4) if math.isfinite(w) and w >= 0.0 else f"{_fmt_num(src_dbm)} dBm"
    return f"{_fmt_num(src_dbm)} dBm"


class PowerTable(MatrixTable):
    """Ваттметр в главном окне: матрица «частота датчика × подаваемая мощность источника»;
    поддерживает переключение единиц (dBm / W / dBW / V / dB)."""

    EMPTY = "NO POINTS YET: each sensor frequency gets a row\nand each source power level gets a column"
    FREQ_TOOLTIP = "Sensor frequency"
    ROW_HEIGHT = 20

    def __init__(self, unit: str = "dBm", parent=None):
        super().__init__("pmTable", min_height=PLOT_MIN_HEIGHT, parent=parent)
        self.unit = unit if unit in PM_UNITS else "dBm"
        self.planned_cells: set[tuple[float, float | None]] = set()
        self._setup_headers([])

    def _col_header_text(self, col_p: float | None, has_numeric: bool) -> str:
        return _format_col_header(col_p, self.unit, has_numeric)

    def _col_header_tooltip(self, col_p: float | None) -> str:
        if col_p is None:
            return "Measured power (no matched RF source)"
        w = _dbm_to_w(col_p)
        w_txt = f" ({si_format(w, 'W', 4)})" if math.isfinite(w) else ""
        return f"Input source power: {_fmt_num(col_p)} dBm{w_txt}"

    def _format_cell(self, freq: float, col_p: float | None, value: float | None) -> tuple[str, bool]:
        if value is not None:
            return _format_power(value, self.unit, col_p), False
        if (freq, col_p) in self.planned_cells:
            return "---", True
        return "", False

    def set_unit(self, unit: str) -> None:
        if unit not in PM_UNITS or unit == self.unit:
            return
        self.unit = unit
        self._render(force=True)

    def set_rows(self, rows, current: float | None = None) -> None:
        """Обратная совместимость: [(Hz, dBm, время)] -> таблица с одним столбцом POWER."""
        rows = list(rows)
        cols: list[float | None] = [None] if rows else []
        matrix_rows = [(f, {None: v}) for f, v, _t in rows]
        self.set_matrix(cols, matrix_rows, current, None)

    def set_matrix(self, columns: list[float | None], rows: list[tuple[float, dict[float | None, float]]],
                   current: float | None = None, current_power: float | None = None,
                   planned_cells: set[tuple[float, float | None]] | None = None) -> None:
        cols = list(columns)
        rows = list(rows)
        planned = set(planned_cells) if planned_cells else set()
        if (cols == self.columns and rows == self.rows and planned == self.planned_cells
                and current == self.current and self._same_power(current_power, self.current_power)):
            return
        self.columns = cols
        self.rows = rows
        self.planned_cells = planned
        self.current = current
        self.current_power = current_power
        self._render(force=False)


class CurrentTable(MatrixTable):
    """Таблица токов канала ИП: матрица «частота генератора × выходная мощность генератора»."""

    EMPTY = "NO POINTS YET: turn on an RF generator and PSU output\neach frequency gets a row and each generator power gets a column"
    FREQ_TOOLTIP = "RF generator frequency"
    ROW_HEIGHT = 22

    def __init__(self, unit: str = "AUTO", parent=None):
        super().__init__("currTable", min_height=PLOT_MIN_HEIGHT, parent=parent)
        self.unit = unit if unit in CURRENT_UNITS else "AUTO"
        self._setup_headers([])

    def _col_header_text(self, col_p: float | None, has_numeric: bool) -> str:
        return _format_col_header(col_p, "dBm", has_numeric)

    def _col_header_tooltip(self, col_p: float | None) -> str:
        if col_p is None:
            return "Measured PSU current (generator output power unknown)"
        w = _dbm_to_w(col_p)
        w_txt = f" ({si_format(w, 'W', 4)})" if math.isfinite(w) else ""
        return f"RF generator output power: {_fmt_num(col_p)} dBm{w_txt}"

    def _format_cell(self, _freq: float, _col_p: float | None, value: float | None) -> tuple[str, bool]:
        return (_format_current(value, self.unit) if value is not None else ""), False

    def set_unit(self, unit: str) -> None:
        if unit not in CURRENT_UNITS or unit == self.unit:
            return
        self.unit = unit
        self._render(force=True)

    def set_matrix(self, columns: list[float | None], rows: list[tuple[float, dict[float | None, float]]],
                   current: float | None = None, current_power: float | None = None) -> None:
        cols = list(columns)
        rows = list(rows)
        if (cols == self.columns and rows == self.rows and current == self.current
                and self._same_power(current_power, self.current_power)):
            return
        self.columns = cols
        self.rows = rows
        self.current = current
        self.current_power = current_power
        self._render(force=False)
