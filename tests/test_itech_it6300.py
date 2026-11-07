import pytest

from visalab.core import ConnectionState, PStatus
from visalab.drivers import ItechIT6300
from visalab.sim import ItechIT6300Sim

from conftest import flush


@pytest.fixture
def psu(make):
    return make(ItechIT6300, ItechIT6300Sim)


def test_connect(psu):
    drv, sim, rec = psu
    assert drv.state is ConnectionState.CONNECTED and drv.model.name == "IT6322"
    assert sim.remote and "*RST" not in sim.history
    assert rec.values["ch3.vlimit"] == 5.0 and rec.values["combine"] == "NONE"


def test_channel_selected_before_each_command(psu):
    drv, sim, rec = psu
    sim.sel = 0
    drv.set_param("ch2.voltage", 5); flush(drv)
    i = sim.history.index("VOLT 5")
    assert sim.history[i - 1] == "INST:NSEL 2"
    assert sim.chs[1].volt == 5 and sim.chs[0].volt == 0
    assert rec.last_status("ch2.voltage")[0] is PStatus.WRITTEN


def test_rating_validation(psu):
    drv, sim, rec = psu
    drv.set_param("ch3.voltage", 12); flush(drv)          # CH3 только 5 В
    assert rec.last_status("ch3.voltage")[0] is PStatus.INVALID
    assert sim.chs[2].volt == 0


def test_cv_cc_and_measurement(psu):
    drv, sim, rec = psu
    sim.set_load(1, 10.0)
    drv.set_param("ch1.voltage", 12); flush(drv)
    drv.set_param("ch1.current", 1.0); flush(drv)
    drv.set_param("ch1.output", True); flush(drv)
    drv.poll()
    assert rec.values["ch1.mode"] == "CC"                 # 12 В / 10 Ом = 1.2 А > 1 А
    assert rec.values["ch1.meas_i"] == pytest.approx(1.0)
    assert rec.values["ch1.meas_v"] == pytest.approx(10.0)
    assert rec.values["ch1.meas_p"] == pytest.approx(10.0)
    sim.set_load(1, 100.0)
    drv.poll()
    assert rec.values["ch1.mode"] == "CV" and rec.values["ch1.meas_v"] == pytest.approx(12.0)


def test_ovp_trip_and_clear(psu):
    drv, sim, rec = psu
    sim.set_load(2, 1000.0)
    for k, v in (("ch2.ovp_level", 10), ("ch2.ovp_state", True), ("ch2.voltage", 12), ("ch2.output", True)):
        drv.set_param(k, v); flush(drv)
    drv.poll()
    assert rec.values["ch2.ovp_tripped"] is True
    assert sim.chs[1].output is False
    drv.run_action("clear_ovp", (2,))
    assert rec.values["ch2.ovp_tripped"] is False


def test_syst_err_queue(psu):
    drv, sim, rec = psu
    drv.raw_write("INST:NSEL 1;:VOLT:LIMIT 10")
    drv.raw_write("VOLT 20")                              # выше VOLT:LIMIT -> -222
    assert (-222, "Data out of range [VOLT 20]") in rec.errors
    drv.raw_write("FOO:BAR")
    assert any(c == -113 for c, _ in rec.errors)


def test_vlimit_rereads_voltage(psu):
    drv, sim, rec = psu
    drv.set_param("ch1.voltage", 20); flush(drv)
    drv.set_param("ch1.vlimit", 15); flush(drv)
    assert rec.values["ch1.voltage"] == 15


def test_combine_modes(psu):
    drv, sim, rec = psu
    drv.set_param("combine", "PAR"); flush(drv)
    assert sim.combine["PAR"] and rec.values["combine"] == "PAR"
    drv.set_param("combine", "SER"); flush(drv)           # через снятие PAR - без ошибки
    assert rec.values["combine"] == "SER" and not rec.errors
    assert rec.last_status("combine")[0] is PStatus.WRITTEN


def test_all_off(psu):
    drv, sim, rec = psu
    drv.set_param("output_all", True); flush(drv)
    assert all(c.output for c in sim.chs) and rec.values["ch3.output"] is True
    drv.run_action("all_off", ())
    assert not any(c.output for c in sim.chs) and rec.values["output_all"] is False


def test_connection_lost_during_poll(make):
    drv, sim, rec = make(ItechIT6300, ItechIT6300Sim, fail_after=60)
    for _ in range(20):
        drv._on_poll()
    assert drv.state is ConnectionState.ERROR
    assert rec.comm and "потеряна" in rec.comm[-1]


def test_device_absent(make):
    sim = ItechIT6300Sim()
    sim.powered = False
    drv, _, rec = make(ItechIT6300, ItechIT6300Sim, sim=sim)
    assert drv.state is ConnectionState.ERROR and rec.comm


def test_not_connected_write_is_reported(make):
    drv, sim, rec = make(ItechIT6300, ItechIT6300Sim, connect=False)
    drv.set_param("ch1.voltage", 1); flush(drv)
    assert rec.last_status("ch1.voltage")[0] is PStatus.ERROR
