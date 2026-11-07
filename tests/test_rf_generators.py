"""E8267D и SMF100A: разные SCPI, один и тот же внешний интерфейс."""
import pytest

from visalab.core import PStatus
from visalab.drivers import E8267D_MODELS, KeysightE8267D, RSSMF100A
from visalab.sim import KeysightE8267DSim, RSSMF100ASim

from conftest import flush

GENS = [(KeysightE8267D, KeysightE8267DSim), (RSSMF100A, RSSMF100ASim)]
IDS = ["E8267D", "SMF100A"]

SCENARIO = [   # одни и те же ключи и значения для любого генератора
    ("rf.frequency", 10e9), ("rf.power", -10.0), ("rf.output", True),
    ("am.source", "INT"), ("am.depth", 30.0), ("am.freq", 1000.0), ("am.state", True),
    ("pulse.period", 1e-3), ("pulse.width", 10e-6), ("pulse.state", True),
    ("fm.deviation", 100e3), ("fm.source", "EXT"),
]


@pytest.fixture(params=GENS, ids=IDS)
def gen(request, make):
    return make(*request.param)


def test_same_keys_on_both_generators(qapp):
    from visalab.core import SimTransport
    a = KeysightE8267D(SimTransport(KeysightE8267DSim()))
    b = RSSMF100A(SimTransport(RSSMF100ASim()))
    assert set(a.params) == set(b.params) and set(a.actions) == set(b.actions)
    for k in a.params:
        pa, pb = a.params[k], b.params[k]
        assert (pa.kind, pa.unit, pa.label, pa.section, pa.choices) == (pb.kind, pb.unit, pb.label, pb.section,
                                                                       pb.choices)


def test_same_scenario_same_result(gen):
    drv, sim, rec = gen
    for key, value in SCENARIO:
        drv.set_param(key, value); flush(drv)
        assert rec.last_status(key)[0] is PStatus.WRITTEN, (key, rec.last_status(key))
        assert rec.values[key] == pytest.approx(value) if isinstance(value, float) else rec.values[key] == value
    assert not rec.errors


def test_vendor_commands_differ(make):
    psg, psg_sim, _ = make(KeysightE8267D, KeysightE8267DSim)
    smf, smf_sim, _ = make(RSSMF100A, RSSMF100ASim)
    for d in (psg, smf):
        d.set_param("fm.source", "EXT"); flush(d)
        d.set_param("am.freq", 2000); flush(d)
    assert ":FM:SOUR EXT1" in psg_sim.history and psg_sim.s["fm.source"] == "EXT1"
    assert "FM:SOUR EXT" in smf_sim.history
    assert ":AM:INT:FREQ 2000" in psg_sim.history and "LFO:FREQ 2000" in smf_sim.history


def test_psg_ext2_reads_as_standard_ext(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    sim.s["am.source"] = "EXT2"
    drv.read_param("am.source")
    assert rec.values["am.source"] == "EXT"


def test_smf_shared_lf_generator(make):
    """У SMF100A один НЧ-генератор: запись am.freq меняет fm.freq и pm.freq - GUI это видит."""
    drv, sim, rec = make(RSSMF100A, RSSMF100ASim)
    drv.set_param("am.freq", 5000); flush(drv)
    assert rec.values["fm.freq"] == rec.values["pm.freq"] == 5000


def test_psg_independent_lf_frequencies(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    drv.set_param("am.freq", 5000); flush(drv)
    assert rec.values["fm.freq"] == 400


def test_fm_and_pm_exclusive(gen):
    drv, sim, rec = gen
    drv.set_param("fm.state", True); flush(drv)
    drv.set_param("pm.state", True); flush(drv)
    assert rec.values["pm.state"] is True and rec.values["fm.state"] is False   # перечитано
    assert rec.last_status("pm.state")[0] is PStatus.WRITTEN and not rec.errors   # без -221


def test_sim_reports_settings_conflict(gen):
    """Прибор (симулятор) сам режим не выключает: прямой SCPI даёт -221."""
    drv, sim, rec = gen
    sim.s["fm.state"] = True
    drv.raw_write(drv.params["pm.state"].write.format(value="1"))
    assert rec.errors and rec.errors[0][0] == -221 and sim.s["pm.state"] is False


def test_range_check_by_model(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim, driver_kw={"model": E8267D_MODELS["E8267D-520"]})
    drv.set_param("rf.frequency", 30e9); flush(drv)     # опция 520 - до 20 ГГц
    assert rec.last_status("rf.frequency")[0] is PStatus.INVALID
    assert not any(h.startswith(":FREQ 3") for h in sim.history)


def test_instrument_error_reported(gen):
    drv, sim, rec = gen
    sim.LIMITS = {**sim.LIMITS, "rf.power": (-130.0, 10.0)}   # прибор строже мягкого предела
    drv.set_param("rf.power", 20); flush(drv)
    status, msg = rec.last_status("rf.power")
    assert status is PStatus.ERROR and "out of range" in msg
    assert rec.errors and rec.errors[0][0] == -222


def test_smf_alc_auto_reads_as_on(make):
    drv, sim, rec = make(RSSMF100A, RSSMF100ASim)
    assert sim.s["rf.alc"] == "AUTO" and rec.values["rf.alc"] is True
    drv.set_param("rf.alc", False); flush(drv)
    assert sim.s["rf.alc"] == "OFF" and rec.values["rf.alc"] is False
