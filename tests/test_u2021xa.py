"""U2021XA: свободный счёт, запуск по уровню, строб, конфликты режимов."""
import math

import pytest

from visalab.core import PStatus
from visalab.drivers import KeysightU2021XA
from visalab.sim import KeysightU2021XASim

from conftest import flush


@pytest.fixture
def pm(make):
    return make(KeysightU2021XA, KeysightU2021XASim)


def setp(drv, rec, key, value):
    drv.set_param(key, value); flush(drv)
    return rec.last_status(key)[0]


def test_connect_sets_units_and_feeds_without_reset(pm):
    drv, sim, rec = pm
    assert "*RST" not in sim.history
    assert all(u == "DBM" for n, u in sim.units.items() if n <= 3)
    assert sim.feeds[2] == "POW:PEAK ON SWEEP1" and sim.feeds[3] == "POW:PTAV ON SWEEP1"
    assert rec.values["detector"] == "NORMAL" and rec.values["trig.source"] == "IMM"
    assert rec.values["meas.continuous"] is True and sim.cfg["cont"] is True   # свободный счёт


def test_free_run_average_power(pm):
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=100e-6, period=1e-3)          # скважность 10 -> -10 dBm
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(-10.0, abs=0.01)
    assert rec.values["meas.peak"] == pytest.approx(0.0, abs=0.01)


def test_level_trigger_gated_measurement(pm):
    """Главный сценарий: запуск по фронту импульса, строб внутри импульса."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=5.0, width=200e-6, period=1e-3, off_dbm=-60)
    for k, v in (("trig.level", -10.0), ("gate.start", 20e-6), ("gate.length", 100e-6), ("trig.source", "INT")):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    assert sim.cfg["cont"] is False                    # драйвер взводит по одному
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(5.0, abs=0.01)      # строб целиком в импульсе
    assert rec.values["meas.ptav"] == pytest.approx(0.0, abs=0.01)
    assert rec.values["meas.armed"] is True            # взведено снова (continuous)
    n_init = sim.history.count("INIT")
    drv.poll()
    assert sim.history.count("INIT") == n_init + 1     # каждое измерение - новый взвод


def test_no_trigger_means_waiting_not_error(pm):
    """CW-сигнал не даёт фронта: измерение ждёт, опрос не блокируется и не рвёт связь."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=None)
    setp(drv, rec, "trig.level", -10.0)
    setp(drv, rec, "trig.source", "INT")
    for _ in range(5):
        drv._on_poll()
    assert rec.values["meas.armed"] is True and rec.values["meas.power"] is None
    assert not rec.comm and drv.state.value == "connected"
    sim.set_signal(width=200e-6, period=1e-3)         # появились импульсы - пришёл запуск
    drv._on_poll()
    assert rec.values["meas.power"] == pytest.approx(0.0, abs=0.01)


def test_single_shot(pm):
    drv, sim, rec = pm
    setp(drv, rec, "meas.continuous", False)
    setp(drv, rec, "trig.source", "BUS")
    drv.run_action("single")
    drv.poll()
    assert rec.values["meas.armed"] is True and rec.values["meas.power"] is None
    drv.run_action("trigger")                         # *TRG
    drv.poll()
    assert rec.values["meas.armed"] is False and rec.values["meas.power"] is not None
    drv.poll()
    assert rec.values["meas.armed"] is False          # не взводится снова


def test_level_trigger_switches_detector_to_normal(pm):
    """INT при AVERage на приборе -221; драйвер прячет конфликт."""
    drv, sim, rec = pm
    setp(drv, rec, "detector", "AVERAGE")
    assert setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    assert rec.values["detector"] == "NORMAL" and not rec.errors
    assert setp(drv, rec, "detector", "AVERAGE") is PStatus.WRITTEN
    assert rec.values["trig.source"] == "IMM" and not rec.errors
    assert rec.values["meas.peak"] is None             # в AVERAGE пика нет


def test_offset_and_gate_partly_outside_pulse(pm):
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=100e-6, period=1e-3, off_dbm=-100)
    for k, v in (("offset", 20.0), ("offset.state", True), ("trig.level", -20.0),
                 ("gate.start", 50e-6), ("gate.length", 100e-6), ("trig.source", "INT")):
        setp(drv, rec, k, v)
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(20 + 10 * math.log10(0.5), abs=0.05)   # половина строба
    assert rec.values["meas.peak"] == pytest.approx(20.0, abs=0.01)


def test_param_change_rearms(pm):
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=None)            # нет запуска - висит взведённым
    setp(drv, rec, "trig.level", -10.0)
    setp(drv, rec, "trig.source", "INT")
    setp(drv, rec, "freq", 1e9)
    assert sim.history[-1] != "ABOR" and sim.measuring and rec.values["meas.armed"] is True


def test_zero_uses_long_timeout(pm):
    drv, sim, rec = pm
    drv.run_action("zero")
    assert sim.zeroed and drv.transport.timeout_ms == drv.TIMEOUT_MS
