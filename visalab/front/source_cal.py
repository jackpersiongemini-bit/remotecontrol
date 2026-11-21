"""Калибровка выходного тракта источника СВЧ (генератор ВЧ и ВАЦ в CW).

Общий блок для :class:`RFGenWindow` (вкладка ``CAL``) и :class:`VNAWindow` (вкладка ``PWR``):
график поправки по частоте, выбор CSV-таблицы из папки ``calibrations`` (общий формат с ваттметром),
автокалибровка :class:`SourceAutoCalibration` по подключённому ваттметру, постоянный коэффициент
усиления ``GAIN`` и итоговая поправка ``OFFS = CAL(f) − GAIN``, записываемая в ``power.offset`` и
``power.offset_state`` прибора.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6 import sip
from PyQt6.QtCore import QObject, QSettings, pyqtSignal

from ..coord import (COORD, Instrument, ScenarioError, SourceAutoCalibration, acal_file_name, interpolate,
                     load_csv)
from .instrument_window import InstrumentWindow
from .params import PChoice, PNLabel, PanelLine
from .power_meter import CalPlot, LNumber, LToggle, OFFSET_LIMIT

__all__ = ["SourceCal", "SRC_ACAL_TIP"]

SRC_ACAL_TIP = ("Auto calibration of the source path: sweeps START..STOP over PTS points while the connected "
                "power meter measures power at the reference plane; fits GAIN and saves a CSV table")


class SourceCal(QObject):
    """Контроллер и набор строк таблицы для калибровки мощности источника СВЧ."""

    acal_requested = pyqtSignal(float, float, int)

    def __init__(self, owner: InstrumentWindow, proxy: Instrument, settings: QSettings, prefix: str,
                 source_profile: str, freq_getter: Callable[[], float | None]):
        super().__init__(owner)
        self.owner = owner
        self.proxy = proxy
        self.settings = settings
        self.prefix = prefix
        self.source_profile = source_profile
        self._freq_getter = freq_getter
        self._acal = None
        self._acal_curve: list[tuple[float, float]] = []

        self.cal_dir = Path(settings.fileName()).resolve().parent / "calibrations"
        self.cal_points: list[tuple[float, float]] = []

        self.cal_plot = CalPlot()
        self.cal_plot.setToolTip("Source calibration table: path offset vs frequency; line - current frequency")

        self.cal_choice = PChoice([("NONE", "")])
        self.cal_choice.setToolTip(f"Source calibration table (CSV: freq_hz, dB) from {self.cal_dir}")
        self.cal_choice.pressed.connect(self.refresh_tables)
        self.refresh_tables(settings.value(f"{prefix}/SRC_CAL_FILE", "", type=str))
        self.cal_choice.changed.connect(self._table_chosen)

        self.acal_start = LNumber(None, "GHz", 1e3, 1000e9, 6, scale=1e9, si_unit="Hz", hint="START")
        self.acal_stop = LNumber(None, "GHz", 1e3, 1000e9, 6, scale=1e9, si_unit="Hz", hint="STOP")
        self.acal_points = LNumber(None, "", 2, 100000, 6, hint="PTS")
        for w, name in ((self.acal_start, "SRC_ACAL_START"), (self.acal_stop, "SRC_ACAL_STOP"),
                        (self.acal_points, "SRC_ACAL_PTS")):
            v = settings.value(f"{prefix}/{name}")
            if v not in (None, ""):
                try:
                    w.set_value(float(v), emit=False)
                except (TypeError, ValueError):
                    pass
            w.changed.connect(lambda val, n=name: settings.setValue(f"{prefix}/{n}", val))
            w.setMinimumWidth(0)
        self.acal_start.setToolTip("Source ACAL start frequency")
        self.acal_stop.setToolTip("Source ACAL stop frequency")
        self.acal_points.setToolTip("Source ACAL number of frequency points from START to STOP")
        self.acal_run = InstrumentWindow._tool("▶", SRC_ACAL_TIP, self.run_acal)

        self.gain_on = LToggle([("OFF", False), ("ON", True)],
                               settings.value(f"{prefix}/SRC_GAIN_ON", False, type=bool))
        self.gain_on.setToolTip("GAIN on / off: subtract the path gain from the source offset")
        self.gain = LNumber(settings.value(f"{prefix}/SRC_GAIN", 0.0, type=float), "dB", -100.0, 100.0)
        self.gain.setToolTip("Path gain in dB (negative for cable loss, positive for amplifier; subtracted)")
        self.gain_on.changed.connect(lambda v: self._local_changed("SRC_GAIN_ON", v))
        self.gain.changed.connect(lambda v: self._local_changed("SRC_GAIN", v))

        self.offset_label = PNLabel(proxy, "power.offset") if proxy.supports("power.offset") else None
        if self.offset_label is not None:
            self.offset_label.setToolTip("Total source power offset (CAL(f) − GAIN): hardware outputs POW + OFFS")

        COORD.run_ended.connect(self._on_run_ended)

    def lines(self) -> list:
        """Строки для ``PanelTable``: график, TABL | OFFS, ACAL, GAIN."""
        plot_line = PanelLine("", self.cal_plot)
        plot_line._name.hide()
        tabl_row = [PanelLine("TABL", self.cal_choice)]
        if self.offset_label is not None:
            tabl_row.append(PanelLine("OFFS", self.offset_label))
        return [
            plot_line,
            tuple(tabl_row),
            PanelLine("ACAL", self.acal_start, self.acal_stop, self.acal_points, self.acal_run),
            PanelLine("GAIN", self.gain_on, self.gain),
        ]

    # ------------------------------------------------------------ таблицы CSV
    def table_files(self) -> list[str]:
        return sorted(p.name for p in self.cal_dir.glob("*.csv")) if self.cal_dir.is_dir() else []

    def refresh_tables(self, select: str | None = None) -> None:
        current = self.cal_choice.value if select is None else select
        self.cal_choice.blockSignals(True)
        self.cal_choice.set_choices([("NONE", "")] + [(Path(n).stem, n) for n in self.table_files()], current)
        self.cal_choice.blockSignals(False)
        if select is not None:
            self._load_table(self.cal_choice.value)

    def _load_table(self, name: str) -> None:
        try:
            self.cal_points = load_csv(self.cal_dir / name) if name else []
        except OSError as exc:
            if hasattr(self.owner, "error_log"):
                self.owner._log("CAL TABLE", f"{name}: {exc}")
            self.cal_points = []
        self.cal_plot.set_points(self.cal_points)

    def _table_chosen(self, name) -> None:
        self._load_table(name or "")
        self._local_changed("SRC_CAL_FILE", name or "")

    # ------------------------------------------------------------ поправка
    def _local_changed(self, name: str, value) -> None:
        self.settings.setValue(f"{self.prefix}/{name}", value)
        self.apply_correction()

    def correction_parts(self) -> dict[str, float]:
        """Слагаемые поправки, dB: CAL(f) и −GAIN."""
        cal = interpolate(self.cal_points, self._freq_getter())
        gain = -self.gain.value if self.gain_on.value else 0.0
        return {"cal": cal, "gain": gain}

    def total_correction(self) -> float:
        total = sum(self.correction_parts().values())
        return min(max(total, -OFFSET_LIMIT), OFFSET_LIMIT)

    def apply_correction(self) -> None:
        """Записать поправку ``power.offset`` / ``power.offset_state`` в прибор, не вызывая скачков при вкл/выкл."""
        if sip.isdeleted(self) or sip.isdeleted(self.owner):
            return
        self.cal_plot.set_freq(self._freq_getter())
        total = round(self.total_correction(), 3)
        p = self.proxy
        if not p.is_connected or self.owner._resetting or COORD.busy(p) is not None:
            return
        on = abs(total) > 5e-4
        if not on:
            # Выключаем сначала state, затем обнуляем offset - мощность на разъёме не дёргается
            if p.supports("power.offset_state") and p.value("power.offset_state") is not False:
                p.set("power.offset_state", False)
            if p.supports("power.offset"):
                cur = p.value("power.offset")
                if cur is None or abs(cur) > 5e-4:
                    p.set("power.offset", 0.0)
            return
        if p.supports("power.offset"):
            cur = p.value("power.offset")
            if cur is None or abs(cur - total) > 5e-4:
                p.set("power.offset", total)
        if p.supports("power.offset_state") and p.value("power.offset_state") is not True:
            p.set("power.offset_state", True)

    # ------------------------------------------------------------ автокалибровка (SRC ACAL)
    def run_acal(self) -> None:
        if self._acal is not None:
            self._acal.stop()
            return
        start, stop, pts = self.acal_start.value, self.acal_stop.value, self.acal_points.value
        if None in (start, stop, pts) or stop <= start or pts < 2:
            self.owner._log("SRC ACAL", "set START < STOP and PTS >= 2")
            return
        self.acal_requested.emit(float(start), float(stop), int(pts))
        path = self.cal_dir / acal_file_name(start, stop, pts, prefix="SRC_ACAL")
        scenario = SourceAutoCalibration(start, stop, int(pts), source_profile=self.source_profile, path=path)
        try:
            run = COORD.start(scenario, bind={self.source_profile: self.proxy})
        except ScenarioError as exc:
            self.owner._log("SRC ACAL", str(exc))
            return
        self._acal = run
        self._acal_curve = []
        run.point.connect(self._acal_point)
        run.message.connect(self._acal_message)
        run.ended.connect(self._acal_ended)
        self._acal_ui(True)

    def _acal_message(self, text: str) -> None:
        if not sip.isdeleted(self) and not sip.isdeleted(self.owner):
            self.owner._log("SRC ACAL", text)

    def _acal_ui(self, running: bool) -> None:
        self.acal_run.setText("■" if running else "▶")
        self.acal_run.setToolTip("Stop source auto calibration (the instruments are returned as they were)"
                                 if running else SRC_ACAL_TIP)
        for w in (self.acal_start, self.acal_stop, self.acal_points, self.cal_choice):
            w.setEnabled(not running)

    def _acal_point(self, point: dict) -> None:
        """Пока идёт ACAL, график показывает измеренную поправку тракта (уровень источника − показание ваттметра)."""
        self._acal_curve.append((point["freq"], point["level"] - point["power"]))
        self.cal_plot.set_points(self._acal_curve)

    def _acal_ended(self, run) -> None:
        self._acal = None
        if sip.isdeleted(self) or sip.isdeleted(self.owner):
            return
        self._acal_ui(False)
        result = run.result if run.state == run.DONE else None
        if result is not None and result.path is not None:
            name = result.path.name
            self.refresh_tables(select=name)
            self.settings.setValue(f"{self.prefix}/SRC_CAL_FILE", name)
            self.gain.set_value(round(result.gain, 3), emit=False)
            self.settings.setValue(f"{self.prefix}/SRC_GAIN", self.gain.value)
            self.gain_on.blockSignals(True)
            self.gain_on.set_value(True)
            self.gain_on.blockSignals(False)
            self.settings.setValue(f"{self.prefix}/SRC_GAIN_ON", True)
        else:
            self.cal_plot.set_points(self.cal_points)
            self.owner._log("SRC ACAL", run.error or run.state)
        self.apply_correction()

    def _on_run_ended(self, run) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.owner):
            return
        if self.proxy in run.instruments():
            self.apply_correction()

    def cleanup(self) -> None:
        if self._acal is not None:
            for sig, slot in ((self._acal.point, self._acal_point),
                              (self._acal.message, self._acal_message),
                              (self._acal.ended, self._acal_ended)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._acal = None
        try:
            COORD.run_ended.disconnect(self._on_run_ended)
        except (TypeError, RuntimeError):
            pass
