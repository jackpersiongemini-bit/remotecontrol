"""Контроллер и интерфейс sweep-режимов для ваттметра (PM) и анализатора спектра (SA).

Общий блок для :class:`PowerMeterWindow` (вкладка ``SWP``) и :class:`SpectrumWindow` (вкладка ``SWP``):
график живых результатов (:class:`LivePlot`), выбор режима (``FREQ`` — по частоте, ``POW`` — по мощности
источника, ``2D`` — матрица частота × мощность), закон сетки (``LIN`` / ``LOG``), однократный или
непрерывный проход (``SING`` / ``CONT``), переключение оси Y (``POUT`` дБм / ``GAIN`` дБ) и экспорт
в CSV (``sweeps/``).
"""
from __future__ import annotations

import datetime
import math
from pathlib import Path
from typing import Callable

from PyQt6 import sip
from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton

from ..coord import (COORD, Instrument, PowerMeterSweep, ScenarioError, SpectrumSweep, SweepPoint, SweepResult,
                     sweep_points)
from ..core.export import write_traces_csv
from ..core.units import si_format
from .instrument_window import InstrumentWindow
from .live_plot import LivePlot
from .params import PanelLine
from .window import mark_growing

__all__ = ["SweepController", "SWEEP_MODES"]

SWEEP_MODES = ("FREQ", "POW", "2D")
_RUN_TIP = "Run coordinated sweep with the connected RF generator (or VNA in CW); click again to stop"
_STOP_TIP = "Stop the running sweep and restore instruments"


class SweepController(QObject):
    """Контроллер и строки ``PanelTable`` для sweep-режимов ваттметра и анализатора спектра."""

    running_changed = pyqtSignal(bool)
    progress_changed = pyqtSignal(int, int, str)

    def __init__(self, owner: InstrumentWindow, proxy: Instrument, settings: QSettings, prefix: str,
                 receiver_profile: str, offset_fn: Callable[[float], float | None] | None = None):
        super().__init__(owner)
        from .power_meter import LNumber, LToggle

        class _OptionalLNumber(LNumber):
            """LNumber, которое можно очистить пустой строкой (возврат к None / KEEP)."""

            cleared = pyqtSignal()

            def _commit(self) -> None:
                if self.isReadOnly() or not self.isModified():
                    return
                if not self.text().strip():
                    self.setModified(False)
                    if self._value is not None:
                        self.clear_value()
                        self.cleared.emit()
                    else:
                        self.show_value(None)
                    return
                super()._commit()

        self.owner = owner
        self.proxy = proxy
        self.settings = settings
        self.prefix = prefix
        self.receiver_profile = receiver_profile
        self.is_sa = (receiver_profile == "spectrum_analyzer")
        self._offset_fn = offset_fn
        self.run = None
        self._run_mode = "FREQ"
        self.points: list[SweepPoint] = []
        self.last_result: SweepResult | None = None

        self.sweep_dir = Path(settings.fileName()).resolve().parent / "sweeps"

        self.plot = mark_growing(LivePlot(min_height=160))
        self.plot.setToolTip("Live sweep result: hover for crosshair readout")

        saved_mode = str(settings.value(f"{prefix}/SWP_MODE", "FREQ") or "FREQ")
        if saved_mode not in SWEEP_MODES:
            saved_mode = "FREQ"
        self.mode = LToggle([("FREQ", "FREQ"), ("POW", "POW"), ("2D", "2D")], saved_mode)
        self.mode.setToolTip("Sweep mode: FREQ (frequency), POW (source power / P1dB), 2D (frequency × power)")
        self.mode.changed.connect(self._on_mode_changed)

        self.law = LToggle([("LIN", False), ("LOG", True)],
                           settings.value(f"{prefix}/SWP_LOG", False, type=bool))
        self.law.setToolTip("Frequency step law: LIN (linear) or LOG (logarithmic)")
        self.law.changed.connect(lambda v: settings.setValue(f"{prefix}/SWP_LOG", bool(v)))

        self.repeat = LToggle([("SING", False), ("CONT", True)],
                              settings.value(f"{prefix}/SWP_REPEAT", False, type=bool))
        self.repeat.setToolTip("SING - one sweep pass; CONT - repeat continuously until stopped")
        self.repeat.changed.connect(lambda v: settings.setValue(f"{prefix}/SWP_REPEAT", bool(v)))

        self.run_button = InstrumentWindow._tool("▶", _RUN_TIP, self.toggle_run)
        self.save_button = self._chip_btn("CSV", "Save current sweep curve(s) to CSV in the sweeps folder",
                                          self.save_csv)
        self.clear_button = self._chip_btn("CLR", "Clear sweep results and graph", self.clear)

        def _load_float(name: str, default: float | None) -> float | None:
            raw = settings.value(f"{prefix}/{name}", None)
            if raw in (None, ""):
                return default
            try:
                fv = float(raw)
                return fv if math.isfinite(fv) else default
            except (TypeError, ValueError):
                return default

        self.f_start = LNumber(_load_float("SWP_F_START", 1e9), "GHz", 1e3, 1000e9, 6,
                               scale=1e9, si_unit="Hz", hint="START")
        self.f_stop = LNumber(_load_float("SWP_F_STOP", 3e9), "GHz", 1e3, 1000e9, 6,
                              scale=1e9, si_unit="Hz", hint="STOP")
        self.f_points = LNumber(_load_float("SWP_F_PTS", 11.0), "", 2, 10000, 6, hint="PTS")
        self.f_start.setToolTip("Sweep start frequency (Hz, supports SI suffixes like 500M, 2.4G)")
        self.f_stop.setToolTip("Sweep stop frequency (Hz, supports SI suffixes)")
        self.f_points.setToolTip("Number of frequency points (2..10000)")

        self.p_start = LNumber(_load_float("SWP_P_START", -20.0), "dBm", -140.0, 40.0, 4, hint="PSTR")
        self.p_stop = LNumber(_load_float("SWP_P_STOP", 0.0), "dBm", -140.0, 40.0, 4, hint="PSTP")
        self.p_points = LNumber(_load_float("SWP_P_PTS", 11.0), "", 2, 1000, 5, hint="PTS")
        self.p_start.setToolTip("Source power sweep start level (dBm)")
        self.p_stop.setToolTip("Source power sweep stop level (dBm)")
        self.p_points.setToolTip("Number of source power points (2..1000)")

        default_dwell = 0.05 if self.is_sa else 0.2
        self.dwell = LNumber(_load_float("SWP_DWELL", default_dwell), "ms", 0.0, 60.0, 4,
                             scale=1e-3, si_unit="s", hint="DWELL")
        self.dwell.setToolTip("Settling pause per point after stepping frequency/power (ms or s)")

        saved_view = str(settings.value(f"{prefix}/SWP_VIEW", "POUT") or "POUT")
        if saved_view not in ("POUT", "GAIN"):
            saved_view = "POUT"
        self.view = LToggle([("POUT", "POUT"), ("GAIN", "GAIN")], saved_view)
        self.view.setToolTip("Plot Y axis: POUT (measured power, dBm) or GAIN (Pout − Pin, dB)")
        self.view.changed.connect(self._on_view_changed)

        self.sa_track = None
        self.sa_span = None
        if self.is_sa:
            self.sa_track = LToggle([("TRK", True), ("FIX", False)],
                                    settings.value(f"{prefix}/SWP_SA_TRACK", True, type=bool))
            self.sa_track.setToolTip("TRK - tune analyzer center frequency to the generator at each point; FIX - keep center fixed")
            self.sa_track.changed.connect(lambda v: settings.setValue(f"{prefix}/SWP_SA_TRACK", bool(v)))
            self.sa_span = _OptionalLNumber(_load_float("SWP_SA_SPAN", None), "MHz", 0.0, 100e9, 5,
                                            scale=1e6, si_unit="Hz", hint="KEEP")
            self.sa_span.setToolTip("Analyzer span during sweep (blank/KEEP = current span, 0 = fast zero-span, e.g. 1 MHz = tracking span)")
            self.sa_span.changed.connect(lambda v: settings.setValue(f"{prefix}/SWP_SA_SPAN", v))
            self.sa_span.cleared.connect(lambda: settings.remove(f"{prefix}/SWP_SA_SPAN"))
            self.sa_span.setMinimumWidth(0)

        for w, key in ((self.f_start, "SWP_F_START"), (self.f_stop, "SWP_F_STOP"), (self.f_points, "SWP_F_PTS"),
                       (self.p_start, "SWP_P_START"), (self.p_stop, "SWP_P_STOP"), (self.p_points, "SWP_P_PTS"),
                       (self.dwell, "SWP_DWELL")):
            w.changed.connect(lambda val, k=key: settings.setValue(f"{prefix}/{k}", val))
            w.setMinimumWidth(0)

        self.status = QLabel("READY")
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status.setStyleSheet("color: #8a8a8a;")
        self.status.setToolTip("Sweep progress and summary (min / max / flatness / P1dB)")

        self._update_controls()

    @staticmethod
    def _chip_btn(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("chip")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    @property
    def running(self) -> bool:
        return self.run is not None

    def lines(self) -> list:
        """Строки для ``PanelTable``: график, SWP, FRQ, PWR, OPT, статус."""
        plot_line = PanelLine("", self.plot)
        plot_line._name.hide()
        status_line = PanelLine("", self.status)
        status_line._name.hide()
        swp_line = PanelLine("SWP", self.mode, self.law, self.repeat, self.run_button,
                             self.save_button, self.clear_button).set_help(
            "Coordinated sweep: mode (FREQ / POW / 2D), grid law (LIN / LOG), single/continuous, run, CSV export"
        )
        frq_line = PanelLine("FRQ", self.f_start, self.f_stop, self.f_points).set_help(
            "Frequency sweep range: START, STOP, and number of points"
        )
        pwr_line = PanelLine("PWR", self.p_start, self.p_stop, self.p_points).set_help(
            "Source power sweep range: START, STOP (dBm), and number of points"
        )
        opt_ws = [self.dwell]
        if self.is_sa and self.sa_track is not None and self.sa_span is not None:
            opt_ws.extend([self.sa_track, self.sa_span])
        opt_ws.append(self.view)
        opt_line = PanelLine("OPT", *opt_ws).set_help(
            "Sweep options: dwell time, tracking span (SA), and plot Y axis (POUT / GAIN)"
        )
        return [plot_line, swp_line, frq_line, pwr_line, opt_line, status_line]

    # ------------------------------------------------------------ состояние элементов
    def _on_mode_changed(self, value) -> None:
        self.settings.setValue(f"{self.prefix}/SWP_MODE", str(value))
        self._update_controls()

    def _on_view_changed(self, value) -> None:
        self.settings.setValue(f"{self.prefix}/SWP_VIEW", str(value))
        self.plot.set_scale(None)
        self._refresh_plot()

    def _update_controls(self) -> None:
        running = self.run is not None
        mode = str(self.mode.value or "FREQ")
        use_f = mode in ("FREQ", "2D")
        use_p = mode in ("POW", "2D")
        self.run_button.setText("■" if running else "▶")
        self.run_button.setToolTip(_STOP_TIP if running else _RUN_TIP)
        self.mode.setEnabled(not running)
        self.repeat.setEnabled(not running)
        self.law.setEnabled(not running and use_f)
        for w in (self.f_start, self.f_stop, self.f_points):
            w.setEnabled(not running and use_f)
        for w in (self.p_start, self.p_stop, self.p_points):
            w.setEnabled(not running and use_p)
        self.dwell.setEnabled(not running)
        if self.sa_track is not None and self.sa_span is not None:
            self.sa_track.setEnabled(not running)
            self.sa_span.setEnabled(not running)
        has_pts = bool(self.points)
        self.save_button.setEnabled(not running and has_pts)
        self.clear_button.setEnabled(not running and has_pts)

    # ------------------------------------------------------------ запуск и остановка
    def build_scenario(self):
        """Построить сценарий sweep по полям UI (или бросить ValueError)."""
        mode = str(self.mode.value or "FREQ")
        freqs: list[float] = []
        powers: list[float] = []
        if mode in ("FREQ", "2D"):
            fs, fe, fn = self.f_start.value, self.f_stop.value, self.f_points.value
            if None in (fs, fe, fn):
                raise ValueError("set frequency START, STOP and PTS")
            freqs = sweep_points(fs, fe, int(round(fn)), log=bool(self.law.value))
        if mode in ("POW", "2D"):
            ps, pe, pn = self.p_start.value, self.p_stop.value, self.p_points.value
            if None in (ps, pe, pn):
                raise ValueError("set power START, STOP and PTS")
            powers = sweep_points(ps, pe, int(round(pn)), log=False)
        dwell = self.dwell.value if self.dwell.value is not None else (0.05 if self.is_sa else 0.2)
        repeat = bool(self.repeat.value)
        if self.is_sa:
            track_center = bool(self.sa_track.value) if self.sa_track is not None else True
            track_span = self.sa_span.value if self.sa_span is not None else None
            return SpectrumSweep(mode, freqs=freqs, powers=powers, settle_s=dwell,
                                 track_center=track_center, track_span=track_span, repeat=repeat)
        return PowerMeterSweep(mode, freqs=freqs, powers=powers, settle_s=dwell,
                               repeat=repeat, offset_fn=self._offset_fn)

    def toggle_run(self) -> None:
        if self.run is not None:
            self.run.stop()
            return
        try:
            scenario = self.build_scenario()
        except ValueError as exc:
            self.owner._log("SWEEP", str(exc))
            self.status.setText(str(exc))
            return
        try:
            run = COORD.start(scenario, bind={self.receiver_profile: self.proxy})
        except ScenarioError as exc:
            self.owner._log("SWEEP", str(exc))
            self.status.setText(str(exc))
            return
        self.run = run
        self._run_mode = scenario.mode
        self.points = []
        self.last_result = None
        self.plot.set_traces([])
        self.status.setText(f"{scenario.NAME} {scenario.mode} 0/{len(scenario._schedule())}")
        run.progress.connect(self._on_progress)
        run.point.connect(self._on_point)
        run.message.connect(self._on_message)
        run.ended.connect(self._on_ended)
        self._update_controls()
        self.running_changed.emit(True)

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if sip.isdeleted(self):
            return
        self.status.setText(f"{done}/{total}  {text}" if total else text)
        self.progress_changed.emit(done, total, text)

    def _on_point(self, pt: dict) -> None:
        if sip.isdeleted(self):
            return
        if pt.get("index") == 0 and pt.get("cycle", 0) > 0:
            self.points = []
        self.points.append(SweepPoint(
            freq=float(pt["freq"]),
            src_power=float(pt["src_power"]),
            power=float(pt["power"]),
            gain=float(pt["gain"]),
            floor=pt.get("floor"),
            peak_freq=pt.get("peak_freq"),
        ))
        self._refresh_plot()

    def _on_message(self, text: str) -> None:
        if not sip.isdeleted(self) and not sip.isdeleted(self.owner):
            self.owner._log("SWEEP", text)

    def _on_ended(self, run) -> None:
        self.run = None
        if sip.isdeleted(self) or sip.isdeleted(self.owner):
            return
        if run.state == run.DONE and isinstance(run.result, SweepResult):
            self.last_result = run.result
            self.points = list(run.result.points)
        elif self.points:
            self.last_result = SweepResult(mode=self._run_mode, receiver=self.receiver_profile,
                                           points=list(self.points))
        self._refresh_plot()
        if run.state == run.FAILED:
            msg = run.error or "failed"
            self.owner._log("SWEEP", msg)
            self.status.setText(f"ERR: {msg}")
        elif run.state == run.STOPPED:
            summary = self.last_result.summary(str(self.view.value)) if self.last_result else "stopped"
            self.status.setText(f"STOPPED · {summary}")
        else:
            summary = self.last_result.summary(str(self.view.value)) if self.last_result else "DONE"
            self.status.setText(summary)
        self._update_controls()
        self.running_changed.emit(False)

    # ------------------------------------------------------------ график и экспорт
    def _current_result(self) -> SweepResult | None:
        if not self.points:
            return None
        return SweepResult(mode=self._run_mode, receiver=self.receiver_profile, points=list(self.points))

    def _refresh_plot(self) -> None:
        res = self._current_result()
        view = str(self.view.value or "POUT")
        if res is None:
            self.plot.set_traces([])
            return
        traces = res.traces(view)
        self.plot.set_traces(traces, points=(len(self.points) <= 1))
        if self.run is None:
            self.status.setText(res.summary(view))

    def clear(self) -> None:
        if self.run is not None:
            return
        self.points = []
        self.last_result = None
        self.plot.set_traces([])
        self.status.setText("READY")
        self._update_controls()

    def save_csv(self) -> Path | None:
        res = self._current_result()
        if res is None:
            return None
        tag = "SA" if self.is_sa else "PM"
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.sweep_dir / f"{tag}_SWP_{res.mode}_{stamp}.csv"
        pout_traces = res.traces("POUT")
        gain_traces = res.traces("GAIN")
        if res.mode in ("FREQ", "POW") and pout_traces and gain_traces:
            traces = [pout_traces[0][1], gain_traces[0][1]]
            labels = ["Pout", "Gain"]
        else:
            view = str(self.view.value or "POUT")
            selected = res.traces(view)
            traces = [t for _n, t in selected]
            labels = [n for n, _t in selected]
        if not traces:
            return None
        comments = [
            f"instrument: {self.proxy.name} ({self.proxy.address})",
            f"mode: {res.mode}, points: {len(res.points)}",
            f"summary: {res.summary('POUT')}",
        ]
        try:
            saved = write_traces_csv(path, traces, labels=labels, comments=comments)
        except (OSError, ValueError) as exc:
            self.owner._log("SWEEP", f"save failed: {exc}")
            return None
        self.owner._log("SWEEP", f"saved {saved.name} ({len(res.points)} points)")
        self.status.setText(f"SAVED {saved.name}")
        return saved

    def cleanup(self) -> None:
        if self.run is not None:
            for sig, slot in ((self.run.progress, self._on_progress),
                              (self.run.point, self._on_point),
                              (self.run.message, self._on_message),
                              (self.run.ended, self._on_ended)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self.run = None
