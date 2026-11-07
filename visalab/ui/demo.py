"""Демо-окна без приборов: описания окон на ключах профилей и фальшивые измерения.

Только для проверки внешнего вида. Ключи - те же, что в ``visalab/profiles.py``,
поэтому потом эти же описания пойдут в окна с настоящими приборами.
"""
from __future__ import annotations

import random

from .model import ActionSpec, DemoModel, FieldSpec, Kind, WindowSpec

F, I, B, E = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM


def _psu_fields(channels=(1, 2, 3)):
    out = []
    for n in channels:
        g = f"Channels/Channel {n}"
        out += [
            FieldSpec(f"ch{n}.output", "Output", B, group=g),
            FieldSpec(f"ch{n}.voltage", "Voltage", F, "V", g, minimum=0, maximum=30 if n < 3 else 5),
            FieldSpec(f"ch{n}.current", "Current limit", F, "A", g, minimum=0, maximum=3),
            FieldSpec(f"ch{n}.meas_v", "V meas", F, "V", g, readonly=True),
            FieldSpec(f"ch{n}.meas_i", "I meas", F, "A", g, readonly=True),
            FieldSpec(f"ch{n}.mode", "Mode", E, group=g, choices=("CV", "CC", "OFF"), readonly=True),
        ]
    for n in channels:
        g = f"Protection/Channel {n}"
        out += [
            FieldSpec(f"ch{n}.ovp_state", "OVP", B, group=g),
            FieldSpec(f"ch{n}.ovp_level", "OVP level", F, "V", g),
            FieldSpec(f"ch{n}.vlimit", "Voltage limit", F, "V", g),
        ]
    out.append(FieldSpec("combine", "CH1+CH2 combine", E, group="General/Channels",
                         choices=("NONE", "SER", "PAR", "TRAC"),
                         labels=("independent", "series", "parallel", "tracking")))
    return tuple(out)


PSU = WindowSpec(
    id="power_supply", title="Power Supply", profile="power_supply",
    fields=_psu_fields(),
    actions=(ActionSpec("all_off", "All off"), ActionSpec("clear_ovp", "Clear OVP")),
    toggles=("ch1.output", "ch2.output", "ch3.output"),
    readouts=("ch1.meas_v", "ch2.meas_v", "ch3.meas_v", "ch1.meas_i", "ch2.meas_i", "ch3.meas_i"),
    default_selected=("ch1.voltage", "ch1.current", "ch2.voltage", "ch2.current"),
)


def _psu_tick(m: DemoModel) -> None:
    for n in (1, 2, 3):
        on = bool(m.value(f"ch{n}.output"))
        v = (m.value(f"ch{n}.voltage") or 0.0) if on else 0.0
        i = 0.12 * v / 5 + random.uniform(0, 2e-3) if on else 0.0
        m.set_value(f"ch{n}.meas_v", v + random.uniform(-1e-3, 1e-3) if on else 0.0)
        m.set_value(f"ch{n}.meas_i", i)
        m.set_value(f"ch{n}.mode", "CV" if on else "OFF")


def _gen_fields(channels=(1, 2)):
    out = []
    for n in channels:
        g = f"Channel {n}"
        out += [
            FieldSpec(f"ch{n}.output", "Output", B, group=f"{g}/Signal"),
            FieldSpec(f"ch{n}.wave", "Waveform", E, group=f"{g}/Signal",
                      choices=("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC"),
                      labels=("sine", "square", "ramp", "pulse", "noise", "arb", "DC")),
            FieldSpec(f"ch{n}.frequency", "Frequency", F, "Hz", f"{g}/Signal", minimum=1e-6, maximum=120e6),
            FieldSpec(f"ch{n}.amplitude", "Amplitude", F, "Vpp", f"{g}/Signal"),
            FieldSpec(f"ch{n}.offset", "Offset", F, "V", f"{g}/Signal"),
            FieldSpec(f"ch{n}.phase", "Phase", F, "deg", f"{g}/Signal"),
            FieldSpec(f"ch{n}.load", "Load", E, group=f"{g}/Output", choices=("50", "HIZ"),
                      labels=("50 Ω", "Hi-Z")),
            FieldSpec(f"ch{n}.polarity", "Polarity", E, group=f"{g}/Output", choices=("NORM", "INV")),
            FieldSpec(f"ch{n}.mod_state", "Modulation", B, group=f"{g}/Modulation"),
            FieldSpec(f"ch{n}.mod_type", "Type", E, group=f"{g}/Modulation",
                      choices=("AM", "FM", "PM", "FSK", "ASK", "PSK", "DSBAM", "PWM")),
            FieldSpec(f"ch{n}.mod_freq", "Mod freq", F, "Hz", f"{g}/Modulation"),
            FieldSpec(f"ch{n}.mod_depth", "Depth", F, "%", f"{g}/Modulation"),
            FieldSpec(f"ch{n}.sweep_state", "Sweep", B, group=f"{g}/Sweep"),
            FieldSpec(f"ch{n}.sweep_start", "Start", F, "Hz", f"{g}/Sweep"),
            FieldSpec(f"ch{n}.sweep_stop", "Stop", F, "Hz", f"{g}/Sweep"),
            FieldSpec(f"ch{n}.sweep_time", "Time", F, "s", f"{g}/Sweep"),
        ]
    return tuple(out)


GENERATOR = WindowSpec(
    id="function_generator", title="Function Generator", profile="function_generator",
    fields=_gen_fields(),
    actions=(ActionSpec("all_off", "All off"),),
    toggles=("ch1.output", "ch2.output"),
    readouts=("ch1.frequency", "ch1.amplitude", "ch2.frequency", "ch2.amplitude"),
    default_selected=("ch1.wave", "ch1.frequency", "ch1.amplitude", "ch1.offset"),
)

RF = WindowSpec(
    id="signal_generator", title="RF Signal Generator", profile="signal_generator",
    fields=(
        FieldSpec("rf.output", "RF output", B, group="RF/Carrier"),
        FieldSpec("rf.frequency", "Frequency", F, "Hz", "RF/Carrier"),
        FieldSpec("rf.power", "Power", F, "dBm", "RF/Carrier"),
        FieldSpec("rf.alc", "ALC", B, group="RF/Carrier"),
        FieldSpec("mod.output", "Modulation", B, group="Modulation/General"),
        FieldSpec("am.state", "AM", B, group="Modulation/AM"),
        FieldSpec("am.depth", "Depth", F, "%", "Modulation/AM"),
        FieldSpec("am.freq", "Frequency", F, "Hz", "Modulation/AM"),
        FieldSpec("fm.state", "FM", B, group="Modulation/FM"),
        FieldSpec("fm.deviation", "Deviation", F, "Hz", "Modulation/FM"),
        FieldSpec("fm.freq", "Frequency", F, "Hz", "Modulation/FM"),
        FieldSpec("pulse.state", "Pulse", B, group="Pulse/Pulse"),
        FieldSpec("pulse.period", "Period", F, "s", "Pulse/Pulse"),
        FieldSpec("pulse.width", "Width", F, "s", "Pulse/Pulse"),
    ),
    actions=(ActionSpec("all_off", "RF off"),),
    toggles=("rf.output", "mod.output"),
    readouts=("rf.frequency", "rf.power"),
    default_selected=("rf.frequency", "rf.power"),
)

INITIAL = {
    "power_supply": {**{f"ch{n}.{k}": v for n in (1, 2, 3) for k, v in (
        ("output", False), ("voltage", 5.0 if n < 3 else 3.3), ("current", 1.0), ("ovp_state", False),
        ("ovp_level", 31.0 if n < 3 else 5.5), ("vlimit", 30.0 if n < 3 else 5.0))}, "combine": "NONE"},
    "function_generator": {**{f"ch{n}.{k}": v for n in (1, 2) for k, v in (
        ("output", False), ("wave", "SINE"), ("frequency", 1e3 * n), ("amplitude", 1.0), ("offset", 0.0),
        ("phase", 0.0), ("load", "HIZ"), ("polarity", "NORM"), ("mod_state", False), ("mod_type", "AM"),
        ("mod_freq", 100.0), ("mod_depth", 50.0), ("sweep_state", False), ("sweep_start", 100.0),
        ("sweep_stop", 1e4), ("sweep_time", 1.0))}},
    "signal_generator": {"rf.output": False, "rf.frequency": 1e9, "rf.power": -10.0, "rf.alc": True,
                         "mod.output": False, "am.state": False, "am.depth": 30.0, "am.freq": 1e3,
                         "fm.state": False, "fm.deviation": 10e3, "fm.freq": 1e3, "pulse.state": False,
                         "pulse.period": 1e-3, "pulse.width": 1e-4},
}

SPECS = {"psu": PSU, "generator": GENERATOR, "rf": RF}
SUBTITLES = {"psu": "demo, no instrument", "generator": "demo, no instrument", "rf": "demo, no instrument"}


def demo_model(name: str, parent=None) -> DemoModel:
    spec = SPECS[name]
    return DemoModel(spec, INITIAL[spec.id], tick=_psu_tick if spec is PSU else None, parent=parent)
