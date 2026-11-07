"""ВАЦ N5231A: измерения пользователя не трогаются, форматы, маркер, целые развёртки."""
import math

import pytest

from visalab.core import PStatus, Trace
from visalab.drivers import KeysightPNA
from visalab.sim import KeysightPNASim

from conftest import flush


@pytest.fixture
def vna(make):
    return make(KeysightPNA, KeysightPNASim)


def setp(drv, rec, key, value):
    drv.set_param(key, value); flush(drv)
    return rec.last_status(key)[0]


def test_uses_existing_measurement(vna):
    drv, sim, rec = vna
    assert drv.meas_name == "CH1_S11_1" and list(sim.meas) == ["CH1_S11_1"]
    assert "*RST" not in sim.history and rec.values["sparam"] == "S11"
    assert not rec.errors


def test_creates_measurement_when_none(make):
    sim = KeysightPNASim()
    sim.meas.clear()
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert drv.meas_name == "VL_MEAS" and sim.meas["VL_MEAS"]["s"] == "S11" and not rec.errors


def test_s21_filter_trace_and_marker(vna):
    drv, sim, rec = vna
    for k, v in (("freq.start", 1e9), ("freq.stop", 3e9), ("sweep.points", 401), ("sparam", "S21")):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    assert sim.meas["CH1_S11_1"]["s"] == "S21"          # то же измерение, другой S-параметр
    drv.poll()
    t = rec.values["trace"]
    assert isinstance(t, Trace) and len(t) == 401 and t.y_unit == "dB" and t.name == "S21"
    f, y = t.peak()
    assert f == pytest.approx(2e9, rel=1e-2) and y == pytest.approx(-1.0, abs=0.05)
    drv.run_action("marker_peak")
    assert rec.values["marker.freq"] == pytest.approx(2e9, rel=1e-2)
    assert rec.values["marker.value"] == pytest.approx(-1.0, abs=0.05)


def test_format_changes_trace_units(vna):
    drv, sim, rec = vna
    for fmt, unit in (("PHASE", "deg"), ("GDELAY", "s"), ("SWR", ""), ("MLOG", "dB")):
        assert setp(drv, rec, "format", fmt) is PStatus.WRITTEN
        drv.poll()
        assert rec.values["trace"].y_unit == unit and rec.values["trace"].meta["format"] == fmt
    assert sim.meas["CH1_S11_1"]["fmt"] == "MLOG"


def test_calc_commands_select_measurement_first(vna):
    drv, sim, rec = vna
    sim.selected = ""
    setp(drv, rec, "format", "PHASE")
    i = sim.history.index("CALC1:FORM PHAS")
    assert sim.history[i - 1] == "CALC1:PAR:SEL 'CH1_S11_1'" and not rec.errors


def test_only_complete_sweeps(vna):
    drv, sim, rec = vna
    sim.hold_sweeps = True
    drv.poll()
    first = rec.values["trace"]
    for _ in range(3):
        drv._on_poll()
    assert rec.values["trace"] is first and rec.values["meas.armed"] is True and not rec.comm
    sim.finish_sweep()
    drv.poll()
    assert rec.values["trace"] is not first


def test_ifbw_rounded_is_coerced(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "bw.if", 1234) is PStatus.COERCED
    assert rec.values["bw.if"] == 1000


def test_s11_return_loss_at_resonance(vna):
    drv, sim, rec = vna
    setp(drv, rec, "freq.start", 1.5e9); setp(drv, rec, "freq.stop", 2.5e9)
    drv.poll()
    t = rec.values["trace"]
    f, y = min(zip(t.x, t.y), key=lambda p: p[1])
    assert f == pytest.approx(2e9, rel=1e-2) and y < -20
    assert math.isfinite(y)
