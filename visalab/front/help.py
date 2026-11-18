"""Подсказки к параметрам: что это и как работает (Алексей 2026-11-14: «навожу на параметр, а подсказка
не высвечивается»).

Текст - по стандартному ключу профиля (``ch{n}.voltage``, ``m{n}.format``), отдельно для каждого класса
приборов: у ключа ``detector`` ваттметра и анализатора спектра разный смысл. Под текстом - диапазон
(пределы модели прибора), как менять значение в этом поле и сам ключ (его пишут в правилах PSU и сценариях).

``tests/test_help.py`` проверяет, что у каждого ключа каждого профиля есть подсказка.
"""
from __future__ import annotations

import re
import textwrap

from ..coord import profile_name
from ..core.units import si_format

__all__ = ["HELP", "help_text", "param_help", "pattern_key", "wrap"]

#: ширина строки подсказки (символов): Qt не переносит обычный текст подсказки сам
WRAP = 72

_COMMON = {
    "meas.continuous": "Continuous measurement: ON - the instrument measures again and again by itself; "
                       "OFF - single: one measurement per request.",
    "meas.armed": "The measurement is armed / running and the instrument is waiting for it to finish.",
    "trace": "Last measured trace (read after every finished measurement).",
}

HELP: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------ источник питания
    "power_supply": {
        "ch{n}.output": "Channel output on / off. Off: the terminals carry no voltage.",
        "ch{n}.voltage": "Voltage set point. In CV mode the output holds this voltage; if the load needs "
                         "more current than the current limit, the channel goes to CC and the voltage drops.",
        "ch{n}.current": "Current limit. In CC mode the output holds this current and the voltage goes down "
                         "to keep it.",
        "ch{n}.meas_v": "Voltage measured at the output (read periodically).",
        "ch{n}.meas_i": "Current measured at the output (read periodically).",
        "ch{n}.meas_p": "Output power measured by the instrument (V × I).",
        "ch{n}.mode": "Regulation mode now: CV - constant voltage, CC - constant current (current limit "
                      "reached), OFF - output off.",
        "ch{n}.ovp_state": "Over-voltage protection on / off. When the output exceeds the OVP level the "
                           "instrument switches the output off.",
        "ch{n}.ovp_level": "Over-voltage protection threshold. The output switches off above it.",
        "ch{n}.ovp_tripped": "OVP has tripped and the output is off. Clear it (TRIP) before switching on again.",
        "ch{n}.vlimit": "Software limit of the voltage set point: a larger set point is refused by the "
                        "instrument. Protects the device under test from a typing mistake.",
        "output_all": "All outputs on / off at once.",
        "combine": "How CH1 and CH2 work together: NONE - separate, SER - in series (voltages add), "
                   "PAR - in parallel (currents add), TRAC - CH2 tracks CH1 set points.",
    },
    # ------------------------------------------------------- функциональный генератор
    "function_generator": {
        "ch{n}.output": "Channel output on / off.",
        "ch{n}.wave": "Waveform of the channel: sine, square, ramp, pulse, noise, arbitrary or DC.",
        "ch{n}.frequency": "Signal frequency. Period = 1 / frequency, changing one changes the other.",
        "ch{n}.period": "Signal period = 1 / frequency.",
        "ch{n}.amplitude": "Peak-to-peak amplitude on the load selected in LOAD (50 Ω or HiZ). On HiZ the "
                           "same setting gives twice the voltage of 50 Ω.",
        "ch{n}.offset": "DC offset added to the signal.",
        "ch{n}.high": "High level of the signal (= offset + amplitude / 2).",
        "ch{n}.low": "Low level of the signal (= offset − amplitude / 2).",
        "ch{n}.phase": "Phase of the channel relative to the other channel.",
        "ch{n}.duty": "Duty cycle of a square / pulse: time high as % of the period.",
        "ch{n}.symmetry": "Ramp symmetry: rising part as % of the period (50 % - triangle).",
        "ch{n}.width": "Pulse width (time high). Must be shorter than the period.",
        "ch{n}.delay": "Pulse delay from the start of the period (or from the trigger).",
        "ch{n}.load": "Load the amplitude is calculated for: 50 Ω or HiZ (high impedance). It does not "
                      "change the output impedance, only how levels are shown and set.",
        "ch{n}.polarity": "Output polarity: NORM or INV (inverted).",
        "ch{n}.sync": "Sync output of the channel on / off (a TTL pulse every period).",
        "ch{n}.mod_state": "Modulation of the channel on / off.",
        "ch{n}.mod_type": "Modulation type: AM, FM, PM, FSK, ASK, PSK, DSB-AM, PWM.",
        "ch{n}.mod_source": "Modulating signal: INT - internal generator, EXT - rear input.",
        "ch{n}.mod_shape": "Shape of the internal modulating signal.",
        "ch{n}.mod_freq": "Frequency of the internal modulating signal.",
        "ch{n}.mod_depth": "AM depth in %.",
        "ch{n}.mod_dev": "Deviation: in Hz for FM, in degrees for PM.",
        "ch{n}.sweep_state": "Frequency sweep on / off. While it is on, the output sweeps from START to STOP.",
        "ch{n}.sweep_time": "Time of one sweep from START to STOP.",
        "ch{n}.sweep_start": "Sweep start frequency.",
        "ch{n}.sweep_stop": "Sweep stop frequency.",
        "ch{n}.sweep_mode": "Sweep law: LINE - linear, LOG - logarithmic.",
        "ch{n}.sweep_dir": "Sweep direction: UP (START → STOP) or DOWN.",
        "ch{n}.sweep_trig": "What starts a sweep: INT - repeats by itself, EXT - external trigger input, "
                            "MAN - manual trigger.",
        "ch{n}.burst_state": "Burst mode on / off: the output gives a number of cycles per trigger.",
        "ch{n}.burst_mode": "Burst type: NCYC - a set number of cycles per trigger, GATE - output while "
                            "the external gate is active.",
        "ch{n}.burst_period": "Repetition period of bursts (internal trigger).",
        "ch{n}.burst_cycles": "Cycles in one burst, or INF - until the trigger stops it.",
        "ch{n}.burst_trig": "What starts a burst: INT - internal period, EXT - trigger input, MAN - manual.",
        "ch{n}.burst_delay": "Delay of the burst after the trigger.",
    },
    # ------------------------------------------------------------- генератор ВЧ
    "signal_generator": {
        "rf.output": "RF output on / off. Off: no signal at the RF connector, settings stay.",
        "rf.frequency": "CW frequency of the RF output. Power meters with FOLW on follow it.",
        "rf.power": "Output level (amplitude) in dBm.",
        "rf.alc": "Automatic level control: the generator holds the output power by its internal detector. "
                  "Turn it off for pulses shorter than the ALC can follow.",
        "mod.output": "Master switch of all modulations. Off: no modulation even if AM / FM / PM / pulse is on.",
        "am.state": "Amplitude modulation on / off.",
        "am.depth": "AM depth in %.",
        "am.source": "Modulating signal for AM: INT - internal, EXT - external input.",
        "am.freq": "Frequency of the internal AM signal.",
        "fm.state": "Frequency modulation on / off.",
        "fm.deviation": "FM peak deviation.",
        "fm.source": "Modulating signal for FM: INT - internal, EXT - external input.",
        "fm.freq": "Frequency of the internal FM signal.",
        "pm.state": "Phase modulation on / off.",
        "pm.deviation": "PM peak deviation in radians.",
        "pm.source": "Modulating signal for PM: INT - internal, EXT - external input.",
        "pm.freq": "Frequency of the internal PM signal.",
        "pulse.state": "Pulse modulation on / off: the RF is switched on only during the pulse.",
        "pulse.source": "Pulse source: INT - internal pulse generator (PERI, WID), EXT - pulse input.",
        "pulse.period": "Period of the internal pulse.",
        "pulse.width": "Width of the internal pulse (RF on).",
        "ref.source": "10 MHz frequency reference: INT - internal, EXT - from the rear 10 MHz input.",
    },
    # -------------------------------------------------------------------- ваттметр
    "power_meter": {
        "meas.power": "Measured average power (in the gate when DET is NORM). OFFS is already added.",
        "meas.peak": "Peak power in the gate (DET NORM).",
        "meas.ptav": "Peak to average ratio in the gate (DET NORM).",
        "meas.continuous": "Continuous measurement: ON - the sensor measures again and again; "
                           "OFF - one measurement per trigger.",
        "meas.armed": "The measurement is armed and waits for the trigger.",
        "freq": "Frequency of the signal. The sensor corrects its reading by its calibration factor at "
                "this frequency, so a wrong frequency gives a wrong power. With FOLW on it follows the "
                "generator (or the VNA in CW).",
        "avg.state": "Averaging on / off. More averages - less noise, slower reading.",
        "avg.count": "Number of averages.",
        "avg.auto": "AUTO: the meter chooses the number of averages by the signal level; MAN: AVG count.",
        "offset": "Offset in dB added to the reading: attenuator or coupler loss in front of the sensor.",
        "offset.state": "Offset on / off.",
        "detector": "AVG - average power only (fast, widest range); NORM - peak, peak/average, gate and "
                    "trigger work, needed for the TIME trace.",
        "video_bw": "Video bandwidth of the sensor in NORM mode: wider - follows faster pulses, more noise.",
        "trig.source": "FREE - measures all the time; LEVEL - starts when the input power crosses TLVL; "
                       "EXT - external trigger input.",
        "trig.level": "Power level that starts a measurement (TRIG LEVEL).",
        "trig.level_auto": "AUTO: the trigger level is set by the meter to the middle of the signal.",
        "trig.slope": "Trigger on the rising (POS) or falling (NEG) edge.",
        "trig.delay": "Delay of the measurement after the trigger.",
        "trig.holdoff": "Time after a trigger when new triggers are ignored.",
        "gate.start": "Start of the measurement gate from the (delayed) trigger.",
        "gate.length": "Length of the measurement gate.",
        "trace": "Power versus time after the trigger (TIME tab, DET NORM).",
        "trace.state": "Time domain mode: read a power vs time trace after every measurement (needs DET NORM).",
        "trace.time": "Length of the time trace.",
        "trace.offset": "Start of the time trace from the delayed trigger.",
    },
    # --------------------------------------------------------------- анализатор спектра
    "spectrum_analyzer": {
        "trace": "Last finished sweep.",
        "meas.continuous": "CONT - sweeps again and again; SINGLE - one sweep per request.",
        "meas.armed": "A sweep is running, waiting for it to finish.",
        "freq.center": "Center frequency of the screen. CENT and SPAN set the same range as START and STOP.",
        "freq.span": "Frequency range of the screen around CENT. 0 Hz - zero span (power vs time at CENT).",
        "freq.start": "Start frequency of the screen.",
        "freq.stop": "Stop frequency of the screen.",
        "bw.res": "Resolution bandwidth (RBW): narrower - lower noise floor and better separation of close "
                  "signals, but slower sweep.",
        "bw.res_auto": "AUTO: RBW is coupled to the span.",
        "bw.video": "Video bandwidth (VBW): smooths the trace; narrower - less noise, slower sweep.",
        "bw.video_auto": "AUTO: VBW is coupled to RBW.",
        "ref_level": "Reference level: the power at the top line of the screen. Signals above it overload "
                     "the mixer.",
        "scale_div": "Vertical scale in dB per division.",
        "atten": "Input attenuator. More attenuation - less distortion from strong signals, higher noise floor.",
        "atten_auto": "AUTO: the attenuator is coupled to the reference level.",
        "sweep.time": "Time of one sweep.",
        "sweep.time_auto": "AUTO: the fastest sweep time that is still accurate for RBW / VBW / span.",
        "sweep.points": "Number of points in a trace.",
        "detector": "How one point is made from the samples it covers: NORMAL - peak and minimum by turns, "
                    "PEAK - maximum, NEG_PEAK - minimum, SAMPLE - one sample, AVERAGE / RMS - average "
                    "(noise, modulated signals), QPEAK - quasi-peak (EMC).",
        "avg.state": "Trace averaging over sweeps on / off.",
        "avg.count": "Number of sweeps averaged.",
        "ref.source": "10 MHz frequency reference: INT - internal, EXT - rear input.",
        "ref_offset": "Offset of the reference level: external amplifier (−) or attenuator (+) in front of "
                      "the input, so the screen shows the power at the device.",
        "preamp": "Internal preamplifier on / off: lower noise floor for weak signals. Do not use with "
                  "strong signals.",
        "input.coupling": "Input coupling: AC blocks DC (protects the input), DC is needed for low frequencies.",
        "sweep.type": "Sweep type: SWEPT (classic), FFT, or AUTO - the analyzer chooses the faster one.",
        "trace.mode": "Trace 1: WRITE - every sweep, MAXHOLD / MINHOLD - keeps the maximum / minimum over "
                      "sweeps, VIEW - frozen.",
        "trig.source": "What starts a sweep: FREE - runs by itself, VIDEO - signal level, EXT - external input, "
                       "LINE - mains.",
        "trig.level": "Video trigger level.",
        "trig.slope": "Trigger on the rising (POS) or falling (NEG) edge.",
        "trig.delay": "Delay of the sweep after the trigger (negative - pre-trigger).",
        "trig.delay_state": "Trigger delay on / off.",
        "marker.state": "Marker 1 on / off.",
        "marker.freq": "Marker frequency.",
        "marker.level": "Trace level at the marker.",
    },
    # ------------------------------------------------------------------ ВАЦ
    "network_analyzer": {
        "trace": "Trace of the selected measurement in its format.",
        "meas.continuous": "CONT - sweeps again and again; SINGLE - one sweep per request (RUN).",
        "meas.armed": "A sweep is running, waiting for it to finish.",
        "sparam": "S-parameter of the measurement: S11 / S22 - reflection, S21 / S12 - transmission.",
        "format": "Display format of the measurement: log magnitude, phase, group delay, SWR, Smith...",
        "stim.channel": "Channel the stimulus settings (frequencies, points, IFBW, power, averaging, "
                        "calibration) refer to.",
        "freq.start": "Start frequency of the sweep.",
        "freq.stop": "Stop frequency of the sweep.",
        "freq.center": "Center frequency of the sweep. CENT and SPAN set the same range as START and STOP.",
        "freq.span": "Width of the sweep around CENT.",
        "sweep.points": "Number of points per sweep. More points - finer frequency step, slower sweep.",
        "sweep.time": "Time of one sweep.",
        "sweep.time_auto": "AUTO: the fastest sweep time for points and IFBW.",
        "sweep.type": "Sweep type: LIN, LOG, POWER sweep, SEGMENT, or CW - one frequency (the VNA works as a signal "
                      "generator, the trace axis is time).",
        "freq.cw": "Frequency in CW mode (VNA as a generator). Power meters with FOLW on follow it.",
        "trig.source": "What starts a sweep: IMM - by itself, EXT - external trigger input, MAN - trigger "
                       "button / RUN.",
        "trig.scope": "One trigger starts ALL channels one after another or only the CURRENT channel.",
        "trig.mode": "What one trigger does in the channel: the whole channel, one sweep, one point or "
                     "one trace.",
        "trig.slope": "Edge of the external trigger: POS or NEG.",
        "trig.type": "External trigger by EDGE or by LEVEL.",
        "trig.delay": "Delay after the external trigger.",
        "state.dir": "Folder for instrument states on the analyzer disk.",
        "trace.sel": "Trace (channel:name) that the scale, marker and smoothing settings refer to.",
        "scale.div": "Scale per division on the analyzer screen (units of the trace format).",
        "scale.ref": "Reference level on the analyzer screen.",
        "scale.rpos": "Grid line of the reference level.",
        "scale.edel": "Electrical delay: removes the linear phase of a cable / fixture. Changes the phase "
                      "data read from the analyzer.",
        "smooth.state": "Trace smoothing on / off (changes the data read).",
        "smooth.aper": "Smoothing aperture in % of the span.",
        "gen.state": "Generator mode: the channel goes to CW at freq CW and the selected port stays continuously "
                     "on without retrace blanking while other channels stop sweeping. OFF restores the channel.",
        "gen.port": "Output port used in generator mode (Port 1 or Port 2).",
        "gen.channel": "Channel used in generator mode (empty - generator mode off).",
        "bw.if": "IF bandwidth: narrower - lower noise, slower sweep.",
        "source.power": "Source power at the port.",
        "rf.output": "Source power on / off (RF at the ports).",
        "avg.state": "Sweep averaging on / off.",
        "avg.count": "Number of sweeps averaged.",
        "cal.state": "Error correction (calibration) of the channel on / off.",
        "cal.set": "Cal set used by the channel (empty - none). Choosing one applies it to the channel.",
        "cal.stim": "Apply the cal set together with its stimulus (frequencies, points, IFBW, power).",
        "cal.interp": "Interpolate the calibration when the stimulus differs from the cal set.",
        "cal.busy": "Calibration: a standard is being measured.",
        "mark.n": "Marker number (1..10) of the selected trace.",
        "mark.state": "Marker on / off.",
        "mark.x": "Marker position (frequency; time in CW).",
        "mark.y": "Trace value at the marker, read after every sweep.",
        "marker.state": "Marker 1 on / off.",
        "marker.freq": "Marker 1 frequency.",
        "marker.value": "Trace value at marker 1, in the trace units.",
        "meas.count": "Number of measurements on channel 1.",
        "meas.complex": "Read complex data (Re / Im) of the measurements too.",
        "m{n}.channel": "Analyzer channel of this measurement slot.",
        "m{n}.name": "Measurement name on the analyzer (empty - the slot is not used).",
        "m{n}.enabled": "Read this measurement after every sweep.",
        "m{n}.sparam": "S-parameter of the measurement: S11 / S22 - reflection, S21 / S12 - transmission.",
        "m{n}.format": "Format of the measurement: log magnitude, phase, unwrapped phase, group delay, SWR...",
        "m{n}.math": "Math with the memory trace: DATA, DATA/MEM, DATA−MEM, DATA+MEM, DATA×MEM. "
                     "MEM stores the current trace first.",
        "m{n}.trace": "Measurement trace in its format.",
        "m{n}.sdata": "Complex trace of the measurement (Re, Im).",
    },
}

#: как менять значение - по виду поля
HOW = {
    "spin": "Wheel / ↑↓: change the underlined digit (under the cursor). Type a value, Enter - write. "
            "Right click: step.",
    "edit": "Type a value (a prefix is fine: 1.5k, 30u), Enter - write. Unit button: prefix.",
    "button": "Click: switch.",
    "menu": "Click: choose.",
    "label": "Read from the instrument.",
}


def pattern_key(key: str) -> str:
    """``ch2.voltage`` -> ``ch{n}.voltage``, ``m3.format`` -> ``m{n}.format``."""
    return re.sub(r"^(ch|m)\d+\.", r"\1{n}.", key)


def help_text(profile: str, key: str) -> str:
    """Текст подсказки ключа профиля ('' - нет)."""
    pat = pattern_key(key)
    return HELP.get(profile, {}).get(pat) or _COMMON.get(pat, "")


def wrap(text: str) -> str:
    return "\n".join(textwrap.fill(p, WRAP) if p else "" for p in text.split("\n"))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _join(own: str, general: str) -> str:
    """Текст окна (как выглядит это поле) и общее описание ключа; текст окна, который почти
    повторяет общее описание, не показывается."""
    if not own or not general:
        return own or general
    words = _words(own)
    if words and len(words & _words(general)) >= 0.7 * len(words):
        return general
    return f"{own}\n{general}"


def _range(spec) -> str:
    lo, hi = getattr(spec, "minimum", None), getattr(spec, "maximum", None)
    if lo is None and hi is None:
        return ""
    unit = spec.unit or ""

    def fmt(v):
        return si_format(v, unit, 6) if unit and unit not in ("dB", "dBm", "%", "deg", "rad", "div") \
            else f"{v:g} {unit}".strip()
    if lo is not None and hi is not None:
        return f"Range {fmt(lo)} .. {fmt(hi)}"
    return f"Min {fmt(lo)}" if lo is not None else f"Max {fmt(hi)}"


def param_help(inst, key: str, how: str = "", message: str = "", text: str = "", extra: str = "") -> str:
    """Подсказка поля прибора: что это (``text`` окна и общее описание ключа), диапазон, как менять,
    ``extra`` (шаг спинбокса), ключ и ошибка записи, если есть."""
    spec = inst.params.get(key) if hasattr(inst, "params") else None
    text = _join(text, help_text(profile_name(inst), key)) or (getattr(spec, "label", "") or "")
    parts = [text] if text else []
    if spec is not None and not spec.readonly and how != "label":
        r = _range(spec)
        if r:
            parts.append(r)
    if how in HOW and not (how != "label" and spec is not None and spec.readonly):
        parts.append(HOW[how])
    if extra:
        parts.append(extra)
    parts.append(f"Key: {key}")
    if message:
        parts.append(message)
    return wrap("\n".join(parts))
