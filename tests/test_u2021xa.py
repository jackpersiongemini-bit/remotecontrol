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
    assert sim.feeds[2] == "POW:PEAK" and sim.feeds[3] == "POW:PTAV"     # свободный счёт: без строба
    assert not rec.errors
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
    sim.set_signal(on_dbm=0.0, width=None)            # нет запуска - ждёт в программном LEVEL, не вешая датчик
    setp(drv, rec, "trig.level", -10.0)
    setp(drv, rec, "trig.source", "INT")
    setp(drv, rec, "freq", 1e9)
    assert sim.cfg["src"] == "IMM" and rec.values["meas.armed"] is True


def test_zero_uses_long_timeout(pm):
    drv, sim, rec = pm
    drv.run_action("zero")
    assert sim.zeroed and drv.transport.timeout_ms == drv.TIMEOUT_MS


def test_free_run_normal_mode_no_conflict(pm):
    """Ошибка с прибора: свободный счёт + NORMal давал -221 на FEED "... ON SWEEP1"."""
    drv, sim, rec = pm
    setp(drv, rec, "detector", "AVERAGE")
    assert setp(drv, rec, "detector", "NORMAL") is PStatus.WRITTEN
    assert not rec.errors and sim.feeds[1] == "POW:AVER" and sim.feeds[2] == "POW:PEAK"
    sim.set_signal(on_dbm=0.0, width=100e-6, period=1e-3)
    drv.poll()
    assert rec.values["meas.peak"] == pytest.approx(0.0, abs=0.01)


def test_gate_feeds_follow_trigger_source(pm):
    drv, sim, rec = pm
    setp(drv, rec, "trig.level", -10.0)
    setp(drv, rec, "trig.source", "INT")
    # Программный LEVEL не включает аппаратный ON SWEEP1 и держит датчик в IMM + TRAC:STAT ON
    assert sim.cfg["src"] == "IMM" and sim.cfg["trac"] is True
    assert sim.feeds[1] == "POW:AVER" and sim.feeds[3] == "POW:PTAV"
    setp(drv, rec, "trig.source", "IMM")
    assert sim.feeds[1] == "POW:AVER" and sim.cfg["trac"] is False and not rec.errors
    setp(drv, rec, "trig.source", "EXT")
    setp(drv, rec, "detector", "AVERAGE")              # EXT + AVER разрешено, строб у среднего остаётся
    assert sim.feeds[1] == "POW:AVER ON SWEEP1" and not rec.errors


def test_float_block_roundtrip():
    from visalab.transport import float_block, parse_float_block
    vals = [-10.0, 1.5, -120.25]
    assert parse_float_block(float_block(vals).encode("latin-1")) == vals


def test_time_domain_trace(pm):
    """Временная область: TRAC:STAT ON -> DET NORM, свободный счёт по одному измерению, трасса 1000 точек."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=100e-6, period=1e-3, off_dbm=-60)
    assert setp(drv, rec, "detector", "AVERAGE") is PStatus.WRITTEN
    assert setp(drv, rec, "trace.state", True) is PStatus.WRITTEN
    assert rec.values["detector"] == "NORMAL" and sim.cfg["det"] == "NORM"
    assert sim.cfg["cont"] is False                    # трасса только при INIT:CONT OFF
    assert setp(drv, rec, "trace.time", 1e-3) is PStatus.WRITTEN
    assert setp(drv, rec, "trace.offset", -100e-6) is PStatus.WRITTEN
    drv.poll()
    t = rec.values["trace"]
    assert len(t) == 1000 and t.x[0] == pytest.approx(-100e-6) and t.x[-1] == pytest.approx(900e-6)
    assert max(t.y) == pytest.approx(0.0, abs=0.01) and min(t.y) == pytest.approx(-60.0, abs=0.01)
    on = [x for x, y in zip(t.x, t.y) if y > -30]
    assert on[0] == pytest.approx(0.0, abs=2e-6) and on[-1] == pytest.approx(100e-6, abs=2e-6)
    assert setp(drv, rec, "detector", "AVERAGE") is PStatus.WRITTEN   # трасса выключается сама
    assert rec.values["trace.state"] is False and sim.cfg["trac"] is False
    assert not rec.errors


def test_level_mode_stops_free_run_and_disables_auto_level(pm):
    """Переход FREE RUN -> LEVEL переводит датчик в INIT:CONT OFF + IMM + TRAC:STAT ON без TRIG:SOUR INT / TRIG:LEV."""
    drv, sim, rec = pm
    del sim.history[:]
    assert setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    assert "INIT:CONT OFF" in sim.history
    assert "TRIG:SOUR INT" not in sim.history
    assert sim.cfg["src"] == "IMM" and sim.cfg["trac"] is True and not rec.errors
    del sim.history[:]
    assert setp(drv, rec, "trig.level", -12.0) is PStatus.WRITTEN
    assert not any(c.startswith("TRIG:LEV") for c in sim.history)
    assert rec.values["trig.level"] == pytest.approx(-12.0)
    assert rec.values["trig.level_auto"] is False and not rec.errors


def test_level_mode_rearms_after_readbacks_and_clears_stale_reading(pm):
    """Переход FREE -> LEVEL и смена порога сбрасывают старое показание и не оставляют устаревших данных."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=-10.0, width=None)           # CW: в FREE RUN даёт -10 dBm, в LEVEL фронта нет
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(-10.0, abs=0.05)

    assert setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    # Старое показание FREE RUN сброшено до прихода запуска
    assert rec.values["meas.power"] is None and rec.values["meas.armed"] is True
    assert not rec.errors

    # Появились импульсы -> сработал программный запуск по уровню
    sim.set_signal(on_dbm=0.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    assert setp(drv, rec, "trig.level", -10.0) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(0.0, abs=0.05)

    # Подняли порог выше импульса -> старый результат сброшен, датчик ждёт (но трасса всё равно видна!)
    assert setp(drv, rec, "trig.level", 10.0) is PStatus.WRITTEN
    assert rec.values["meas.power"] is None and rec.values["meas.armed"] is True
    drv.poll()
    assert rec.values["meas.power"] is None and rec.values["trace"] is not None


def test_level_auto_triggers_and_updates_detected_level(pm):
    """В режиме LEVEL AUTO датчик сам подбирает порог и после измерения обновляет trig.level в GUI."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=-10.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    assert setp(drv, rec, "trig.level_auto", True) is PStatus.WRITTEN
    assert setp(drv, rec, "gate.start", 20e-6) is PStatus.WRITTEN
    assert setp(drv, rec, "gate.length", 100e-6) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(-10.0, abs=0.05)
    assert rec.values["trig.level"] < -10.0 and rec.values["trig.level"] > -40.0
    assert not rec.errors


def test_level_negative_slope_triggers_on_falling_edge(pm):
    """Фронт NEG запускает строб от спада импульса (сразу после спада — уровень паузы)."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    for k, v in (("trig.level", -10.0), ("gate.start", 20e-6), ("gate.length", 50e-6), ("trig.source", "INT")):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(0.0, abs=0.05)
    assert setp(drv, rec, "trig.slope", "NEG") is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(-60.0, abs=0.5)


def test_batch_write_arms_once_and_resync_aborts_before_init(pm):
    """Пачка параметров в _pending для EXT взводит INIT один раз в конце; after_resync делает ABOR до INIT."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=0.0, width=None)
    del sim.history[:]
    drv.set_param("gate.start", 10e-6)
    drv.set_param("gate.length", 80e-6)
    drv.set_param("trig.source", "EXT")
    flush(drv)
    assert sim.history.count("INIT") == 1
    assert sim.history[-2:] == ["INIT", "*OPC"]

    del sim.history[:]
    drv.after_resync(cleared=True)
    assert sim.history.index("ABOR") < sim.history.index("INIT")
    assert not rec.errors


@pytest.mark.parametrize("hw_shifts", [False, True])
def test_trig_level_accounts_for_calibration_offset(make, hw_shifts):
    """Калибровка (CORR:GAIN2) сдвигает диапазон уровня запуска выше +20 дБм без обращения к аппаратному TRIG:LEV."""
    drv, sim, rec = make(KeysightU2021XA, KeysightU2021XASim,
                         sim=KeysightU2021XASim(trig_level_includes_offset=hw_shifts))
    # физически на датчике импульс +5 дБм; с калибровкой +40 дБ это +45 дБм (> +20 дБм!)
    sim.set_signal(on_dbm=5.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    assert setp(drv, rec, "offset", 40.0) is PStatus.WRITTEN
    assert setp(drv, rec, "offset.state", True) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.level", 35.0) is PStatus.WRITTEN
    assert rec.values["trig.level"] == pytest.approx(35.0)
    assert setp(drv, rec, "gate.start", 20e-6) is PStatus.WRITTEN
    assert setp(drv, rec, "gate.length", 100e-6) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(45.0, abs=0.05)
    assert rec.values["meas.peak"] == pytest.approx(45.0, abs=0.05)
    # выход за динамический диапазон [0..60] дБм (при поправке +40 дБ) отклоняется
    assert setp(drv, rec, "trig.level", 65.0) is PStatus.INVALID
    assert setp(drv, rec, "trig.level", -5.0) is PStatus.INVALID
    # отрицательная поправка (усиление 30 дБ -> offset -30 дБ): допустимы уровни ниже -40 дБм
    assert setp(drv, rec, "offset", -30.0) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.level", -55.0) is PStatus.WRITTEN
    assert rec.values["trig.level"] == pytest.approx(-55.0)
    assert not rec.errors


def test_software_level_10us_pulse(pm):
    """Программный LEVEL стабильно измеряет короткий импульс 10 мкс и выравнивает фронт трассы в t = 0."""
    drv, sim, rec = pm
    sim.set_signal(on_dbm=3.0, width=10e-6, period=1e-3, off_dbm=-65.0)
    for k, v in (("trig.level", -10.0), ("gate.start", 1e-6), ("gate.length", 7e-6), ("trig.source", "INT")):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["meas.power"] == pytest.approx(3.0, abs=0.05)
    assert rec.values["meas.peak"] == pytest.approx(3.0, abs=0.05)
    tr = rec.values["trace"]
    assert tr is not None and len(tr) == 1000
    assert tr.x[0] < 0.0 < tr.x[-1]
    assert not rec.errors
