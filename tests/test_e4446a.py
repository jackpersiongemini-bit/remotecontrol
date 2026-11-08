"""Анализатор спектра E4446A: целые развёртки, трасса Trace, маркер, связи «авто»."""
import pytest

from visalab.core import PStatus, Trace
from visalab.drivers import AgilentPSA
from visalab.sim import AgilentPSASim

from conftest import flush


@pytest.fixture
def sa(make):
    sim = AgilentPSASim()
    sim.signals = [(1.0e9, -20.0), (1.2e9, -45.0)]
    return make(AgilentPSA, AgilentPSASim, sim=sim)


def setp(drv, rec, key, value):
    drv.set_param(key, value); flush(drv)
    return rec.last_status(key)[0]


def test_connect_no_reset_ascii_single_sweeps(sa):
    drv, sim, rec = sa
    assert "*RST" not in sim.history and sim.c["form"] == "ASC"
    assert sim.c["cont"] is False                       # драйвер ведёт однократными развёртками
    drv.poll()
    t = rec.values["trace"]
    assert isinstance(t, Trace) and len(t) == 601 and t.x_unit == "Hz" and t.y_unit == "dBm"
    assert t.x[0] == pytest.approx(0.0) and t.x[-1] == pytest.approx(44e9)


def test_trace_shows_signal_and_marker_peak(sa):
    drv, sim, rec = sa
    for k, v in (("freq.center", 1.1e9), ("freq.span", 400e6)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN
    assert rec.values["freq.start"] == pytest.approx(0.9e9)        # связанное перечитано
    assert rec.values["bw.res"] == pytest.approx(3e6)              # RBW авто: полоса/100 -> ряд 1-3
    drv.poll()
    f, p = rec.values["trace"].peak()
    assert f == pytest.approx(1e9, abs=2e6) and p == pytest.approx(-20, abs=0.5)
    drv.run_action("marker_peak")
    assert rec.values["marker.state"] is True
    assert rec.values["marker.freq"] == pytest.approx(1e9, abs=2e6)
    assert rec.values["marker.level"] == pytest.approx(-20, abs=0.5)
    assert rec.values["trace"].meta["bw.res"] == pytest.approx(3e6)


def test_only_complete_sweeps_are_published(sa):
    drv, sim, rec = sa
    sim.hold_sweeps = True                               # следующая развёртка «идёт» долго
    drv.poll()                                           # первая готова -> трасса, взвод следующей
    first = rec.values["trace"]
    for _ in range(3):
        drv._on_poll()
    assert rec.values["trace"] is first and rec.values["meas.armed"] is True
    assert not rec.comm                                  # долгая развёртка - не обрыв
    sim.finish_sweep()
    drv.poll()
    assert rec.values["trace"] is not first


def test_single_sweep(sa):
    drv, sim, rec = sa
    drv.poll()
    drv.run_action("single")
    assert rec.values["meas.continuous"] is False
    n = sim.sweeps_done
    drv.poll(); drv.poll(); drv.poll()
    assert sim.sweeps_done == n and rec.values["meas.armed"] is False


def test_setting_change_restarts_sweep(sa):
    drv, sim, rec = sa
    sim.hold_sweeps = True
    drv.poll()
    setp(drv, rec, "ref_level", -10.0)
    i = sim.history.index("DISP:WIND:TRAC:Y:RLEV -10")
    assert "ABOR" in sim.history[:i] and sim.history[i + 1:].count("INIT") >= 1


def test_attenuator_step_reported_as_coerced(sa):
    drv, sim, rec = sa
    assert setp(drv, rec, "atten", 15) is PStatus.COERCED        # шаг 2 дБ
    assert rec.values["atten"] == 16 and rec.values["atten_auto"] is False


def test_detector_standard_tokens(sa):
    drv, sim, rec = sa
    assert setp(drv, rec, "detector", "PEAK") is PStatus.WRITTEN
    assert sim.c["det"] == "POS" and rec.values["detector"] == "PEAK"
    assert setp(drv, rec, "detector", "NEG_PEAK") is PStatus.WRITTEN and sim.c["det"] == "NEG"


def test_marker_off_reads_none_without_error(sa):
    drv, sim, rec = sa
    drv.read_param("marker.level")
    assert rec.values["marker.level"] is None and not rec.errors


def test_states_in_instrument_memory(sa):
    drv, sim, rec = sa
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("state_list")
    assert actions[-1] == ("state_list", [])
    for k, v in (("freq.center", 1.1e9), ("freq.span", 400e6)):
        setp(drv, rec, k, v)
    drv.run_action("state_save", ("fund",))
    setp(drv, rec, "freq.center", 5e9)
    setp(drv, rec, "freq.span", 1e6)
    drv.run_action("state_save", ("other",))
    drv.run_action("state_list")
    assert actions[-1] == ("state_list", ["fund", "other"]) and set(sim.files) == {"fund.state", "other.state"}
    drv.run_action("state_recall", ("fund",))
    assert rec.values["freq.center"] == pytest.approx(1.1e9) and rec.values["freq.span"] == pytest.approx(400e6)
    assert sim.c["form"] == "ASC" and not rec.comm
    drv.run_action("state_delete", ("other",))
    drv.run_action("state_list")
    assert actions[-1] == ("state_list", ["fund"])
    n = len(rec.errors)
    drv.run_action("state_recall", ("missing",))                 # нет файла - ошибка прибора, а не обрыв
    assert len(rec.errors) > n and not rec.comm
    drv.run_action("state_save", ("bad name",))                  # недопустимое имя в прибор не уходит
    assert "bad name" not in " ".join(sim.history)


def test_input_trace_sweep_settings(sa):
    drv, sim, rec = sa
    for key, value, check in (("ref_offset", 10.0, lambda: sim.c["roff"] == 10.0),
                              ("preamp", True, lambda: sim.c["gain"] is True),
                              ("input.coupling", "DC", lambda: sim.c["coup"] == "DC"),
                              ("sweep.type", "SWEPT", lambda: sim.c["swtype"] == "SWE"),
                              ("trace.mode", "MAXHOLD", lambda: sim.c["tmode"] == "MAXH")):
        assert setp(drv, rec, key, value) is PStatus.WRITTEN, key
        assert check() and rec.values[key] == value, key
    assert not rec.errors and not rec.comm
    drv.poll()
    assert rec.values["trace"].y[0] > -90 + 9               # смещение опорного уровня сдвигает трассу


def test_trigger_settings_and_waiting_sweep(sa):
    drv, sim, rec = sa
    for key, value in (("trig.source", "VIDEO"), ("trig.level", -30.0), ("trig.slope", "NEG"),
                       ("trig.delay", 0.01), ("trig.delay_state", True)):
        assert setp(drv, rec, key, value) is PStatus.WRITTEN, key
    assert (sim.c["trig"], sim.c["tlev"], sim.c["tslope"]) == ("VID", -30.0, "NEG")
    assert sim.c["tdel"] == pytest.approx(0.01) and sim.c["tdel_on"] is True
    sweeps = sim.sweeps_done
    for _ in range(3):
        drv.poll()
    assert sim.sweeps_done == sweeps and rec.values["meas.armed"] is True and not rec.comm   # ждём запуск
    sim.fire_trigger()
    drv.poll()
    assert sim.sweeps_done == sweeps + 1
    assert setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN and sim.c["trig"] == "EXT1"
    assert setp(drv, rec, "trig.source", "FREE") is PStatus.WRITTEN and sim.c["trig"] == "IMM"


def test_max_hold_accumulates(sa):
    drv, sim, rec = sa
    for k, v in (("freq.center", 1.0e9), ("freq.span", 10e6)):
        setp(drv, rec, k, v)
    sim.signals = [(1.0e9, -50.0)]
    setp(drv, rec, "trace.mode", "MAXHOLD")
    drv.poll()
    low = rec.values["trace"].peak()[1]
    sim.signals = [(1.0e9, -20.0)]
    drv.poll(); drv.poll()                         # уже взведённая развёртка снята со старым сигналом
    assert rec.values["trace"].peak()[1] > low + 20
    sim.signals = [(1.0e9, -60.0)]
    drv.poll(); drv.poll()
    assert rec.values["trace"].peak()[1] == pytest.approx(-20, abs=0.5)    # максимум удержан


def test_zero_span_trace_is_time(sa):
    drv, sim, rec = sa
    assert setp(drv, rec, "freq.center", 1.0e9) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.span", 0.0) is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.time", 2e-3) is PStatus.WRITTEN
    drv.poll()
    t = rec.values["trace"]
    assert t.x_unit == "s" and t.x[0] == 0.0 and t.x[-1] == pytest.approx(2e-3) and t.y_unit == "dBm"
    assert setp(drv, rec, "freq.span", 100e6) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["trace"].x_unit == "Hz"
