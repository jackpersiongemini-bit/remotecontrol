import pytest

from visalab.core import ConnectionState, PStatus
from visalab.drivers import SiglentSDG
from visalab.drivers.siglent_sdg import SDG_MODELS, parse_siglent_response
from visalab.sim import SiglentSDGSim

from conftest import flush


@pytest.fixture
def sdg(make):
    return make(SiglentSDG, SiglentSDGSim, driver_kw={"model": SDG_MODELS["SDG2042X"]})


def test_parser():
    lead, pairs, pre = parse_siglent_response(
        "C1:MDWV STATE,ON,AM,MDSP,SINE,SRC,INT,FRQ,100HZ,DEPTH,100,CARR,WVTP,RAMP,FRQ,1000HZ")
    assert lead == [] and pre == ["AM", "CARR"]
    assert pairs["AM.FRQ"] == "100HZ" and pairs["CARR.FRQ"] == "1000HZ" and pairs["STATE"] == "ON"
    lead, pairs, _ = parse_siglent_response("C1: OUTP ON, LOAD, HZ, PLRT, NOR")
    assert lead == ["ON"] and pairs == {"LOAD": "HZ", "PLRT": "NOR"}


def test_connect_reads_state_without_reset(sdg):
    drv, sim, rec = sdg
    assert drv.state is ConnectionState.CONNECTED
    assert drv.model.name == "SDG2042X"           # модель задана явно, не по *IDN?
    assert "*RST" not in sim.history
    assert rec.values["C1.frequency"] == 1000.0
    assert rec.values["C1.wave"] == "SINE"
    assert rec.values["C1.duty"] is None          # у синуса нет скважности
    assert rec.values["C2.load"] == "HZ"


def test_set_frequency(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.frequency", 2.5e6)
    flush(drv)
    assert sim.ch[1]["BSWV"]["FRQ"] == 2.5e6
    assert "C1:BSWV FRQ,2500000" in sim.history
    assert rec.last_status("C1.frequency")[0] is PStatus.WRITTEN
    assert rec.values["C1.frequency"] == 2.5e6


def test_writes_are_coalesced(sdg):
    drv, sim, rec = sdg
    for f in (1e3, 2e3, 3e3, 4e3):
        drv.set_param("C1.frequency", f)
    flush(drv)
    assert [h for h in sim.history if h.startswith("C1:BSWV FRQ")] == ["C1:BSWV FRQ,4000"]


def test_out_of_model_range_is_rejected_before_sending(sdg):
    drv, sim, rec = sdg
    n = len(sim.history)
    drv.set_param("C1.frequency", 100e6)          # > 40 МГц у SDG2042X
    flush(drv)
    assert rec.last_status("C1.frequency")[0] is PStatus.INVALID
    assert not any("FRQ" in h for h in sim.history[n:])


def test_coerced_amplitude_on_50_ohm(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.load", "50")
    flush(drv)
    drv.set_param("C1.amplitude", 15)             # прибор ограничит до 10 Vpp
    flush(drv)
    status, msg = rec.last_status("C1.amplitude")
    assert status is PStatus.COERCED and rec.values["C1.amplitude"] == 10.0


def test_modes_are_exclusive(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.mod_state", True)
    flush(drv)
    assert rec.values["C1.mod_state"] is True and rec.values["C1.mod_type"] == "AM"
    drv.set_param("C1.sweep_state", True)
    flush(drv)
    assert rec.values["C1.sweep_state"] is True
    assert rec.values["C1.mod_state"] is False    # перечитано через also_read


def test_mod_freq_uses_current_type(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.mod_state", True); flush(drv)
    drv.set_param("C1.mod_type", "FM"); flush(drv)
    drv.set_param("C1.mod_freq", 250); flush(drv)
    assert "C1:MDWV FM,FRQ,250" in sim.history
    assert rec.values["C1.mod_freq"] == 250


def test_burst_infinite(sdg):
    drv, sim, rec = sdg
    drv.set_param("C2.burst_state", True); flush(drv)
    drv.set_param("C2.burst_cycles", "INF"); flush(drv)
    assert rec.values["C2.burst_cycles"] == "INF"
    assert rec.last_status("C2.burst_cycles")[0] is PStatus.WRITTEN


def test_instrument_error_from_esr(sdg):
    drv, sim, rec = sdg
    drv.raw_write("C1:BSWV FOO,1")               # EXE
    drv.raw_write("BLAH 1")                      # CME
    codes = [m for _, m in rec.errors]
    assert any("исполнения" in m for m in codes) and any("команды" in m for m in codes)


def test_error_param_marks_status(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.wave", "NOISE"); flush(drv)
    drv.set_param("C1.sweep_state", True); flush(drv)   # свип с шумом запрещён
    assert rec.last_status("C1.sweep_state")[0] is PStatus.ERROR
    assert rec.values["C1.sweep_state"] is False


def test_trigger_without_mode_reports(sdg):
    drv, sim, rec = sdg
    drv.run_action("trigger_1", ())
    assert rec.comm and "свип" in rec.comm[-1]
    assert drv.state is ConnectionState.CONNECTED


def test_output_and_all_off(sdg):
    drv, sim, rec = sdg
    drv.set_param("C1.output", True); flush(drv)
    assert sim.ch[1]["OUTP"]["STATE"] == "ON" and rec.values["C1.output"] is True
    drv.run_action("all_off", ())
    assert rec.values["C1.output"] is False


def test_response_header_is_stripped(sdg):
    """SDG отвечает "*ESR 1" и "C1:SYNC OFF" - драйвер должен понимать это."""
    drv, sim, rec = sdg
    sim.write("*ESR?")
    assert sim.read().startswith("*ESR ")          # симулятор ведёт себя как прибор
    drv.raw_write("BLAH 1")                        # CME -> *ESR 32
    assert rec.errors and not rec.comm             # *ESR? разобран, а не упал
    assert drv.query_int("*OPC?") == 1
    assert drv.query("C1:SYNC?") == "OFF"


def test_unknown_idn_is_not_an_error(make):
    drv, sim, rec = make(SiglentSDG, SiglentSDGSim, sim=SiglentSDGSim(idn="ACME,XYZ,1,1"))
    assert drv.state is ConnectionState.CONNECTED and not rec.comm
