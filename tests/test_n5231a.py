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


def test_all_measurements_formatted_and_complex(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = {"s": "S21", "fmt": "PHAS"}
    sim.meas["USER_S12"] = {"s": "S12", "fmt": "MLIN"}
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    for k, v in (("freq.start", 1e9), ("freq.stop", 3e9), ("sweep.points", 201)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    drv.poll()
    assert rec.values["meas.count"] == 3 and not rec.errors and not rec.comm
    assert [rec.values[f"m{n}.name"] for n in (1, 2, 3, 4)] == ["CH1_S11_1", "USER_S21", "USER_S12", None]
    assert [rec.values[f"m{n}.sparam"] for n in (1, 2, 3)] == ["S11", "S21", "S12"]
    assert [rec.values[f"m{n}.format"] for n in (1, 2, 3)] == ["MLOG", "PHASE", "LIN_MAG"]
    assert rec.values["m4.trace"] is None and rec.values["m4.sdata"] is None
    # форматированные данные: у каждого измерения свой формат и своя единица
    assert [rec.values[f"m{n}.trace"].y_unit for n in (1, 2, 3)] == ["dB", "deg", ""]
    assert all(len(rec.values[f"m{n}.trace"]) == 201 for n in (1, 2, 3))
    assert rec.values["trace"] is rec.values["m1.trace"]                 # легаси-ключ = первое измерение
    # комплексные данные: Re в y, Im в im; из них восстанавливается любой формат
    s21 = rec.values["m2.sdata"]
    assert s21.is_complex and len(s21.im) == 201 and s21.meta["format"] == "SDATA"
    z = s21.complex_y()
    assert math.degrees(__import__("cmath").phase(z[100])) == pytest.approx(rec.values["m2.trace"].y[100], abs=1e-6)
    assert 20 * math.log10(abs(rec.values["m3.sdata"].complex_y()[100])) == pytest.approx(
        20 * math.log10(rec.values["m3.trace"].y[100]), abs=1e-6)
    assert rec.values["m1.sdata"].meta["sparam"] == "S11"


def test_per_measurement_settings_and_limits(make):
    sim = KeysightPNASim()
    sim.meas["B"] = {"s": "S21", "fmt": "MLOG"}
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert setp(drv, rec, "m2.format", "PHASE") is PStatus.WRITTEN
    assert sim.meas["B"]["fmt"] == "PHAS" and sim.meas["CH1_S11_1"]["fmt"] == "MLOG"    # первое не тронуто
    assert setp(drv, rec, "m2.sparam", "S12") is PStatus.WRITTEN and sim.meas["B"]["s"] == "S12"
    n = len(rec.errors)
    assert setp(drv, rec, "m5.format", "PHASE") is PStatus.ERROR          # такого измерения нет
    assert not rec.comm and len(rec.errors) >= n
    drv.poll()
    assert rec.values["m2.trace"].y_unit == "deg" and rec.values["m2.sparam"] == "S12"


def test_measurements_change_on_instrument_and_complex_off(make):
    sim = KeysightPNASim()
    sim.meas["B"] = {"s": "S21", "fmt": "MLOG"}
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    drv.poll()
    assert rec.values["meas.count"] == 2 and rec.values["m2.trace"] is not None
    del sim.meas["B"]                                                     # пользователь удалил измерение на приборе
    drv.poll()
    assert rec.values["meas.count"] == 1 and rec.values["m2.trace"] is None and rec.values["m2.name"] is None
    assert setp(drv, rec, "meas.complex", False) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["m1.sdata"] is None and rec.values["m1.trace"] is not None
    assert not any("SDATA" in c for c in sim.history[-6:])
