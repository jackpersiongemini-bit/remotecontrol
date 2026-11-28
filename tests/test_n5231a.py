"""ВАЦ Keysight PNA / PNA-L: 4-портовое двоичное чтение комплексных S-параметров (SDATA + SENS:X?),
настройка стимула, мощности, CW, триггеров, калибровки, состояний, Touchstone и работа через Proxy / Instrument."""
import cmath
import math

import pytest

from visalab.coord import COORD, ActivityMonitor, Instrument
from visalab.core import ConnectionState, PStatus, Trace, VNATraceSpec, format_sparam_trace
from visalab.core.export import write_touchstone
from visalab.drivers import KeysightPNA, PNA_MODELS
from visalab.instruments import InstrumentProxy
from visalab.sim import KeysightPNASim
from visalab.transport import SimTransport, binary_block, parse_binary_block

from conftest import flush


@pytest.fixture
def vna(make):
    return make(KeysightPNA, KeysightPNASim)


def setp(drv, rec, key, value):
    drv.set_param(key, value)
    flush(drv)
    return rec.last_status(key)[0]


def _wait(qtbot, cond, timeout=4000):
    qtbot.waitUntil(cond, timeout=timeout)


# ============================================================ 1. Транспорт: двоичные блоки
def test_binary_block_float32_and_float64_roundtrip():
    vals = [1e9, 10.123456789012e9, -0.123456789012345, 0.0, 19.999999999e9]
    for be in (True, False):
        raw64 = binary_block(vals, datatype="d", big_endian=be).encode("latin-1")
        out64 = parse_binary_block(raw64, datatype="d", big_endian=be)
        assert out64 == pytest.approx(vals, rel=1e-15, abs=0.0)
        raw32 = binary_block([1.5, -2.25, 0.0], datatype="f", big_endian=be).encode("latin-1")
        assert parse_binary_block(raw32, datatype="f", big_endian=be) == pytest.approx([1.5, -2.25, 0.0], rel=1e-6)
    # неопределённая длина #0...
    indef = b"#0" + binary_block([1.25, -3.5], datatype="d", big_endian=True).encode("latin-1")[4:] + b"\n"
    assert parse_binary_block(indef, datatype="d", big_endian=True) == pytest.approx([1.25, -3.5])


# ============================================================ 2. Порты, двоичный формат, отсутствие дисплея
def test_connect_detects_4_ports_and_uses_binary_without_display_queries(vna):
    drv, sim, rec = vna
    assert drv.ports == 4
    assert rec.values["ports"] == 4
    assert sim.form_data == ("REAL", 64)
    assert sim.form_bord == "NORM"
    assert "*RST" not in sim.history
    assert rec.values["s.active"] == "S11"
    assert rec.values["s11.enabled"] is True
    assert rec.values["s41.enabled"] is False
    assert not rec.errors and not rec.comm

    drv.poll()
    # Никаких дисплейных запросов (шкалы, маркеры, лимиты, форматы, FDATA)
    forbidden = ("WNUM", "TNUM", "PDIV", "RLEV", "RPOS", "MARK", "LIM:", "FDATA", "MATH:", "FORM?")
    assert not any(any(tok in cmd.upper() for tok in forbidden) for cmd in sim.history)
    assert any("DATA? SDATA" in cmd.upper() for cmd in sim.history)
    assert any("SENS1:X?" in cmd.upper() for cmd in sim.history)


def test_2_port_vna_rejects_ports_3_and_4_cleanly(make):
    sim = KeysightPNASim(ports=2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim, driver_kw={"model": PNA_MODELS["N5231A"]})
    assert drv.ports == 2
    assert rec.values["ports"] == 2
    assert not rec.errors and not rec.comm  # _sync_all не слал SOUR1:POW3?/POW4? на 2-портовый прибор

    assert setp(drv, rec, "s41.enabled", True) is PStatus.INVALID
    assert setp(drv, rec, "s.active", "S11,S41") is PStatus.INVALID
    assert setp(drv, rec, "gen.port", 3) is PStatus.INVALID
    assert setp(drv, rec, "source.power_p3", -10.0) is PStatus.INVALID
    assert setp(drv, rec, "s21.enabled", True) is PStatus.WRITTEN
    assert rec.values["s.active"] == "S11,S21"
    assert not rec.errors


def test_creates_default_s11_when_catalog_empty(make):
    sim = KeysightPNASim(ports=4)
    sim.meas.clear()
    sim.windows[1].clear()
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert rec.values["s.active"] == "S11"
    assert "VL_CH1_S11" in sim.meas and sim.meas["VL_CH1_S11"]["s"] == "S11"
    assert "VL_CH1_S11" in sim.windows[1].values()  # тихо привязано к окну, чтобы PNA свипировал
    assert not rec.errors


def test_ascii_fallback_when_binary_rejected(make):
    sim = KeysightPNASim(ports=4)
    from visalab.sim.base import ESR_EXE, SimError

    def reject_real(a, g):
        if "REAL" in a.upper():
            raise SimError(-224, "Binary not supported", ESR_EXE)
        sim._form_set(a, g)

    sim._handlers = [(p, s, g) for p, s, g in sim._handlers if p != "FORMat[:DATA]"]
    sim.on("FORMat[:DATA]", reject_real, sim._form_get)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert drv._binary is False
    assert sim.form_data == ("ASC", 0)
    drv.poll()
    tr = rec.values["s11.trace"]
    assert isinstance(tr, Trace) and tr.is_complex and len(tr) == 201
    assert not rec.errors and not rec.comm


# ============================================================ 3. Чтение S11, S41 и других S-параметров
def test_read_s11_and_s41_complex_accuracy_and_lifecycle(vna):
    drv, sim, rec = vna
    for k, v in (("freq.start", 1e9), ("freq.stop", 3e9), ("sweep.points", 401)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k

    assert setp(drv, rec, "s41.enabled", True) is PStatus.WRITTEN
    assert rec.values["s.active"] == "S11,S41"
    assert "VL_CH1_S41" in sim.meas and sim.meas["VL_CH1_S41"]["s"] == "S41"
    assert "VL_CH1_S41" in sim.windows[1].values()

    drv.poll()
    t11 = rec.values["s11.trace"]
    t41 = rec.values["s41.trace"]
    assert isinstance(t11, Trace) and t11.is_complex and t11.name == "S11" and len(t11) == 401
    assert isinstance(t41, Trace) and t41.is_complex and t41.name == "S41" and len(t41) == 401
    assert rec.values["meas.count"] == 2
    assert rec.values["trace"] is t11
    assert rec.values.get("s21.trace") is None

    # Точность комплексных Re/Im (float64) относительно физики симулятора
    for sp, tr in (("S11", t11), ("S41", t41)):
        for idx in (0, 200, 400):
            expected = sim.s(sp, tr.x[idx])
            got = complex(tr.y[idx], tr.im[idx])
            assert got.real == pytest.approx(expected.real, rel=1e-12, abs=1e-15)
            assert got.imag == pytest.approx(expected.imag, rel=1e-12, abs=1e-15)

    # S41 физически отличается от S21 (другие потери/фаза)
    assert setp(drv, rec, "s21.enabled", True) is PStatus.WRITTEN
    drv.poll()
    t21 = rec.values["s21.trace"]
    assert abs(complex(t21.y[200], t21.im[200])) > abs(complex(t41.y[200], t41.im[200]))

    # Выключаем S41 -> трасса обнуляется и наше временное измерение удаляется (пользовательское CH1_S11_1 не трогаем)
    assert setp(drv, rec, "s41.enabled", False) is PStatus.WRITTEN
    assert rec.values["s41.trace"] is None
    assert "VL_CH1_S41" not in sim.meas
    assert "CH1_S11_1" in sim.meas
    assert not rec.errors and not rec.comm


def test_sparam_actions_set_all_none(vna):
    drv, sim, rec = vna
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))

    drv.run_action("sparam_set", ("S41", "S11", "S34"))
    assert actions[-1] == ("sparam_set", ["S11", "S34", "S41"])
    assert rec.values["s.active"] == "S11,S34,S41"
    drv.poll()
    assert rec.values["meas.count"] == 3
    assert all(rec.values[f"{sp.lower()}.trace"].is_complex for sp in ("S11", "S34", "S41"))

    drv.run_action("sparam_all")
    assert len(actions[-1][1]) == 16
    drv.poll()
    assert rec.values["meas.count"] == 16

    drv.run_action("sparam_none")
    assert actions[-1] == ("sparam_none", [])
    assert rec.values["s.active"] == ""
    assert rec.values["meas.count"] == 0
    assert rec.values["trace"] is None
    assert not rec.errors


# ============================================================ 4. Точная ось X (LIN, LOG, CW, POWER + кэш)
def test_exact_x_axis_for_lin_log_cw_power_and_cache(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "freq.start", 1e9) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.stop", 9e9) is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.points", 101) is PStatus.WRITTEN
    sim.history.clear()

    # LIN: SENS1:X? читается один раз и кэшируется между развёртками
    drv.poll()
    tr_lin = rec.values["s11.trace"]
    assert tr_lin.x_unit == "Hz" and tr_lin.uniform is True
    assert tr_lin.x[50] == pytest.approx(5e9)
    x_reads = sum(1 for c in sim.history if c.upper() == "SENS1:X?")
    assert x_reads == 1
    drv.poll()
    assert sum(1 for c in sim.history if c.upper() == "SENS1:X?") == 1  # из кэша!

    # LOG: геометрическая середина sqrt(1e9 * 9e9) = 3e9 Hz!
    assert setp(drv, rec, "sweep.type", "LOG") is PStatus.WRITTEN
    drv.poll()
    tr_log = rec.values["s11.trace"]
    assert tr_log.x_unit == "Hz" and tr_log.uniform is False
    assert tr_log.x[50] == pytest.approx(3e9, rel=1e-9)

    # CW: ось X — время 0..sweep.time в секундах
    assert setp(drv, rec, "sweep.type", "CW") is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.time", 0.5) is PStatus.WRITTEN
    drv.poll()
    tr_cw = rec.values["s11.trace"]
    assert tr_cw.x_unit == "s" and tr_cw.uniform is True
    assert tr_cw.x[0] == pytest.approx(0.0) and tr_cw.x[-1] == pytest.approx(0.5)

    # POWER: ось X — мощность в dBm
    assert setp(drv, rec, "sweep.type", "POWER") is PStatus.WRITTEN
    drv.poll()
    tr_pow = rec.values["s11.trace"]
    assert tr_pow.x_unit == "dBm" and tr_pow.uniform is False
    assert tr_pow.x[0] == pytest.approx(-20.0) and tr_pow.x[-1] == pytest.approx(0.0)

    # Изменение числа точек на передней панели прибора (в обход драйвера) автоматически обновляет ось X
    assert setp(drv, rec, "sweep.type", "LIN") is PStatus.WRITTEN
    drv.poll()
    assert len(rec.values["s11.trace"]) == 101
    sim.c["poin"] = 51
    drv.poll()
    assert len(rec.values["s11.trace"]) == 51
    assert not rec.errors


# ============================================================ 5. Стимул, усреднение (GRO), развёртки
def test_only_complete_sweeps_and_group_averaging_on_single(vna):
    drv, sim, rec = vna
    sim.hold_sweeps = True
    drv.poll()
    first = rec.values["s11.trace"]
    for _ in range(3):
        drv._on_poll()
    assert rec.values["s11.trace"] is first and rec.values["meas.armed"] is True
    sim.finish_sweep()
    drv.poll()
    assert rec.values["s11.trace"] is not first

    # В режиме SINGLE с включённым усреднением используется GRO + GRO:COUN
    assert setp(drv, rec, "avg.state", True) is PStatus.WRITTEN
    assert setp(drv, rec, "avg.count", 8) is PStatus.WRITTEN
    sim.history.clear()
    drv.run_action("single")
    assert "SENS1:SWE:GRO:COUN 8" in sim.history
    assert any("SWE:MODE GRO" in c for c in sim.history)
    assert rec.values["meas.armed"] is True
    sim.finish_sweep()
    drv.poll()
    assert rec.values["meas.armed"] is False
    assert not rec.errors


def test_ifbw_rounded_is_coerced(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "bw.if", 1234) is PStatus.COERCED
    assert rec.values["bw.if"] == 1000


# ============================================================ 6. 4-портовая мощность и CW-генератор
def test_4_port_decoupled_power_and_cw_generator(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "source.power_coupled", False) is PStatus.WRITTEN
    for p, lvl in ((1, -10.0), (2, -5.0), (3, 0.0), (4, -15.0)):
        assert setp(drv, rec, f"source.power_p{p}", lvl) is PStatus.WRITTEN
        assert rec.values[f"source.power_p{p}"] == pytest.approx(lvl)
    assert sim.c["pow_levels"] == {1: -10.0, 2: -5.0, 3: 0.0, 4: -15.0}

    # Включаем CW-генератор на порту 4
    assert setp(drv, rec, "gen.port", 4) is PStatus.WRITTEN
    assert setp(drv, rec, "gen.state", True) is PStatus.WRITTEN
    assert rec.values["gen.state"] is True and rec.values["gen.channel"] == 1
    assert sim.c["type"] == "CW"
    assert sim.c["pow_modes"] == {1: "OFF", 2: "OFF", 3: "OFF", 4: "ON"}
    assert rec.values["source.power"] == pytest.approx(-15.0)

    # Переключаем CW на порт 3
    assert setp(drv, rec, "gen.port", 3) is PStatus.WRITTEN
    assert sim.c["pow_modes"] == {1: "OFF", 2: "OFF", 3: "ON", 4: "OFF"}
    assert rec.values["source.power"] == pytest.approx(0.0)

    # Выключаем CW -> возвращаются прежние настройки и уровни всех 4 портов
    assert setp(drv, rec, "gen.state", False) is PStatus.WRITTEN
    assert sim.c["type"] == "LIN"
    assert sim.c["pow_modes"] == {1: "AUTO", 2: "AUTO", 3: "AUTO", 4: "AUTO"}
    assert sim.c["pow_levels"] == {1: -10.0, 2: -5.0, 3: 0.0, 4: -15.0}
    assert not rec.errors


def test_power_offset_calibration(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "source.power", 0.0) is PStatus.WRITTEN
    assert sim.c["pow"] == pytest.approx(0.0)
    assert setp(drv, rec, "power.offset", -18.5) is PStatus.WRITTEN
    assert setp(drv, rec, "power.offset_state", True) is PStatus.WRITTEN
    # включение поправки не меняет физический выход до новой записи
    assert sim.c["pow"] == pytest.approx(0.0)
    assert rec.values["source.power"] == pytest.approx(18.5)
    assert setp(drv, rec, "source.power", 5.0) is PStatus.WRITTEN
    assert sim.c["pow"] == pytest.approx(-13.5)
    assert rec.values["source.power"] == pytest.approx(5.0)


# ============================================================ 7. Touchstone 1..4 порта (snp_read + write_touchstone)
def test_snp_read_and_write_touchstone_1_2_4_ports(vna, tmp_path):
    drv, sim, rec = vna
    assert setp(drv, rec, "freq.start", 1e9) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.stop", 3e9) is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.points", 11) is PStatus.WRITTEN

    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))

    # По умолчанию на 4-портовом ВАЦ — все 4 порта (16 параметров)
    drv.run_action("snp_read")
    sp4 = actions[-1][1]
    assert len(sp4) == 16 and "S41" in sp4 and "S44" in sp4
    assert all(tr.is_complex and len(tr) == 11 for tr in sp4.values())
    s4p = write_touchstone(tmp_path / "dut.s4p", sp4, comments=["4-port test"])
    lines4 = [ln for ln in s4p.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith(("!", "#"))]
    assert len(lines4) == 11 * 4  # по 4 строки на частоту по стандарту Touchstone 1.x

    # 2-портовое подмножество портов 1 и 4 -> .s2p
    drv.run_action("snp_read", "1,4")
    sp14 = actions[-1][1]
    assert list(sp14) == ["S11", "S41", "S14", "S44"]
    s2p = write_touchstone(tmp_path / "p14.s2p", sp14)
    lines2 = [ln for ln in s2p.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith(("!", "#"))]
    assert len(lines2) == 11

    # 1 порт -> .s1p
    drv.run_action("snp_read", "4")
    sp1 = actions[-1][1]
    assert list(sp1) == ["S44"]
    s1p = write_touchstone(tmp_path / "p4.s1p", sp1)
    assert s1p.exists()
    assert not rec.errors


# ============================================================ 8. Калибровка (4-портовая guided и THRU S41)
def test_4_port_guided_and_thru_s41_calibration(vna):
    drv, sim, rec = vna
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))

    # Guided-калибровка всех 4 портов (механический набор: 4*3 OSL + 3 THRU = 15 шагов)
    drv.run_action(
        "cal_start",
        ("APC 3.5 female", "85052D",
         "APC 3.5 female", "85052D",
         "APC 3.5 female", "85052D",
         "APC 3.5 female", "85052D"),
    )
    steps, done = actions[-1][1]
    assert len(steps) == 15 and done == []
    for k in range(1, 16):
        drv.run_action("cal_measure", k)
        drv.poll()
    drv.run_action("cal_save", "CAL_4PORT")
    assert "CAL_4PORT" in actions[-1][1]
    assert rec.values["cal.set"] == "CAL_4PORT" and rec.values["cal.state"] is True

    # Response THRU калибровка напрямую для S41
    drv.run_action("cal_start_thru", "S41")
    steps, done = actions[-1][1]
    assert len(steps) == 1 and "S41" in steps[0]
    drv.run_action("cal_measure", 1)
    drv.poll()
    drv.run_action("cal_save", "CAL_THRU_41")
    assert "CAL_THRU_41" in actions[-1][1]
    assert rec.values["cal.set"] == "CAL_THRU_41"
    assert not rec.errors


# ============================================================ 9. Состояния (.csa) и Reset
def test_state_save_recall_and_reset(vna):
    drv, sim, rec = vna
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))

    assert setp(drv, rec, "freq.start", 1.5e9) is PStatus.WRITTEN
    drv.run_action("state_save", "bench_4p")
    drv.run_action("state_list")
    assert "bench_4p" in actions[-1][1]

    assert setp(drv, rec, "freq.start", 4.0e9) is PStatus.WRITTEN
    drv.run_action("state_recall", "bench_4p")
    assert rec.values["freq.start"] == pytest.approx(1.5e9)

    drv.run_action("reset")
    assert rec.values["s.active"] == "S11"
    assert not rec.errors


# ============================================================ 10. Proxy & Instrument (Слои 3 и 4)
def test_proxy_and_coord_instrument_sparam_api_and_sweep_signal(qtbot):
    sim = KeysightPNASim(ports=4)
    proxy = InstrumentProxy(KeysightPNA, SimTransport(sim))
    inst = Instrument(proxy)
    COORD.links.add(inst)
    mon = ActivityMonitor()
    sweeps: list[dict[str, Trace]] = []
    s41_updates: list[Trace | None] = []
    inst.sweep_updated.connect(sweeps.append)
    inst.subscribe_sparam("S41", s41_updates.append)

    try:
        inst.open()
        _wait(qtbot, lambda: inst.is_connected and inst.value("ports") == 4)
        assert inst.ports == 4
        assert inst.active_sparams() == ["S11"]

        inst.set_sparams(["S11", "S41"])
        _wait(qtbot, lambda: inst.active_sparams() == ["S11", "S41"] and inst.sparam_trace("S41") is not None)

        traces = inst.sparam_traces()
        assert set(traces) == {"S11", "S41"}
        assert traces["S11"].is_complex and traces["S41"].is_complex
        assert any("S41" in sw and "S11" in sw for sw in sweeps)
        assert any(isinstance(t, Trace) and t.name == "S41" for t in s41_updates)

        # ActivityMonitor видит 4-портовый сигнал S41 и отдаёт трассы
        assert mon.vna_has_signal(inst) is True
        labels = [lbl for lbl, _tr in mon.vna_traces(inst)]
        assert labels == ["1·S11", "1·S41"]

        # enable_sparam выключает S41
        inst.enable_sparam("S41", False)
        _wait(qtbot, lambda: inst.active_sparams() == ["S11"] and inst.sparam_trace("S41") is None)
        assert set(inst.sparam_traces()) == {"S11"}
    finally:
        mon.stop()
        COORD.links.remove(inst)
        inst.shutdown()


# ============================================================ 11. Каналы, power.start/stop и мульти-канальные развёртки
def test_channels_create_switch_delete_and_multi_channel_sweeps(vna):
    drv, sim, rec = vna
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))

    assert rec.values["channels.list"] == "1"
    assert rec.values["stim.channel"] == 1

    # Создаём канал 2
    drv.run_action("chan_create", 2)
    assert actions[-1] == ("chan_create", [1, 2])
    assert rec.values["channels.list"] == "1,2"
    assert rec.values["stim.channel"] == 2

    # Настраиваем CH2: 3..5 ГГц, 21 точка
    assert setp(drv, rec, "freq.start", 3e9) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.stop", 5e9) is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.points", 21) is PStatus.WRITTEN

    # Возвращаемся на CH1: 1..2 ГГц, 11 точек
    assert setp(drv, rec, "stim.channel", 1) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.start", 1e9) is PStatus.WRITTEN
    assert setp(drv, rec, "freq.stop", 2e9) is PStatus.WRITTEN
    assert setp(drv, rec, "sweep.points", 11) is PStatus.WRITTEN

    # Включаем чтение из обоих каналов одновременно через s.all_active
    emitted: list[tuple[int, str, Trace]] = []
    drv.param_updated.connect(
        lambda k, v: emitted.append((int(v.meta.get("channel", 1)), v.name, v))
        if k.endswith(".trace") and isinstance(v, Trace) else None
    )
    assert setp(drv, rec, "s.all_active", "1:S11,1:S41,2:S21") is PStatus.WRITTEN
    drv.poll()

    by_pair = {(ch, sp): tr for ch, sp, tr in emitted}
    assert (1, "S11") in by_pair and (1, "S41") in by_pair and (2, "S21") in by_pair
    assert len(by_pair[(1, "S11")]) == 11 and by_pair[(1, "S11")].x[0] == pytest.approx(1e9)
    assert len(by_pair[(1, "S41")]) == 11 and by_pair[(1, "S41")].x[-1] == pytest.approx(2e9)
    assert len(by_pair[(2, "S21")]) == 21 and by_pair[(2, "S21")].x[0] == pytest.approx(3e9)
    assert by_pair[(2, "S21")].x[-1] == pytest.approx(5e9)

    # Power sweep (power.start / power.stop) на CH1
    assert setp(drv, rec, "sweep.type", "POWER") is PStatus.WRITTEN
    assert setp(drv, rec, "power.start", -25.0) is PStatus.WRITTEN
    assert setp(drv, rec, "power.stop", 5.0) is PStatus.WRITTEN
    emitted.clear()
    drv.poll()
    p_tr = next(tr for ch, sp, tr in emitted if ch == 1 and sp == "S11")
    assert p_tr.x_unit == "dBm"
    assert p_tr.x[0] == pytest.approx(-25.0) and p_tr.x[-1] == pytest.approx(5.0)

    # Удаляем канал 2
    drv.run_action("chan_delete", 2)
    assert rec.values["channels.list"] == "1"
    assert "2:S21" not in rec.values["s.all_active"]

    # Последний оставшийся канал 1 не удаляется
    drv.run_action("chan_delete", 1)
    assert rec.values["channels.list"] == "1"
    assert not rec.errors


# ============================================================ 12. Пользовательские трассы на ПК (VNATraceSpec)
def test_vna_trace_spec_formatting_edel_and_smoothing():
    # Синтетическая трасса: |S21| = 0.5 (−6.02 dB), задержка кабеля tau = 2 нс -> S(f) = 0.5 * exp(-j 2pi f tau)
    tau = 2e-9
    xs = tuple(1e9 + i * 1e7 for i in range(101))  # 1..2 ГГц, шаг 10 МГц
    zs = [0.5 * cmath.exp(-2j * math.pi * f * tau) for f in xs]
    raw = Trace(
        x=xs,
        y=tuple(z.real for z in zs),
        im=tuple(z.imag for z in zs),
        x_unit="Hz",
        y_unit="",
        name="S21",
        meta={"channel": 1, "fmt": "SDATA"},
    )

    mlog = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "MLOG"))
    assert mlog.y_unit == "dB" and mlog.y[0] == pytest.approx(20.0 * math.log10(0.5), abs=1e-6)

    lin = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "LIN_MAG"))
    assert lin.y_unit == "" and lin.y[0] == pytest.approx(0.5, abs=1e-6)

    swr = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "SWR"))
    assert swr.y_unit == "" and swr.y[0] == pytest.approx((1.0 + 0.5) / (1.0 - 0.5), abs=1e-6)

    re_tr = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "REAL"))
    im_tr = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "IMAG"))
    assert re_tr.y[0] == pytest.approx(zs[0].real, abs=1e-6)
    assert im_tr.y[0] == pytest.approx(zs[0].imag, abs=1e-6)

    # Групповая задержка = -d(phi)/d(2*pi*f) = +2 нс
    gd = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "GDELAY"))
    assert gd.y_unit == "s" and gd.y[50] == pytest.approx(tau, rel=1e-4)

    # Компенсация электрической задержки EDEL = +2 нс выпрямляет фазу в 0°
    ph_comp = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "PHASE", edel_s=tau))
    assert ph_comp.y_unit == "deg"
    assert all(abs(v) < 1e-6 for v in ph_comp.y)

    # Развёрнутая фаза без EDEL монотонно убывает на -720° за 1 ГГц (2 нс * 1 ГГц = 2 периода)
    uph = format_sparam_trace(raw, VNATraceSpec("t1", 1, "S21", "UPHASE"))
    assert (uph.y[-1] - uph.y[0]) == pytest.approx(-720.0, abs=1e-3)

    # Сглаживание снижает разброс шума
    noisy = Trace(
        x=xs,
        y=tuple((0.5 + (0.1 if i % 2 else -0.1)) for i in range(len(xs))),
        im=tuple(0.0 for _ in xs),
        x_unit="Hz", y_unit="", name="S21", meta={"channel": 1, "fmt": "SDATA"},
    )
    raw_lin = format_sparam_trace(noisy, VNATraceSpec("t1", 1, "S21", "LIN_MAG", smooth=False))
    smo_lin = format_sparam_trace(noisy, VNATraceSpec("t1", 1, "S21", "LIN_MAG", smooth=True, smooth_aper=20.0))
    assert len(smo_lin) == len(raw_lin)
    assert (max(smo_lin.y[15:-15]) - min(smo_lin.y[15:-15])) < 0.05

    # Сериализация to_dict / from_dict
    spec = VNATraceSpec("t9", 2, "s41", "phase", enabled=False, edel_s=1.5e-9, smooth=True, smooth_aper=5.0)
    restored = VNATraceSpec.from_dict(spec.to_dict())
    assert restored == VNATraceSpec("t9", 2, "S41", "PHASE", False, 1.5e-9, True, 5.0)


def test_coord_instrument_vna_trace_specs_and_activity_monitor(qtbot):
    sim = KeysightPNASim(ports=4)
    proxy = InstrumentProxy(KeysightPNA, SimTransport(sim))
    inst = Instrument(proxy)
    COORD.links.add(inst)
    mon = ActivityMonitor()
    changes = []
    mon.changed.connect(lambda: changes.append(1))

    try:
        inst.open()
        _wait(qtbot, lambda: inst.is_connected and inst.value("ports") == 4)
        inst.action("chan_create", 2)
        _wait(qtbot, lambda: 2 in inst.channels_list())

        specs = [
            VNATraceSpec("t1", 1, "S41", "MLOG", enabled=True),
            VNATraceSpec("t2", 1, "S41", "PHASE", edel_s=1e-9, enabled=True),
            VNATraceSpec("t3", 2, "S21", "SWR", enabled=True),
            VNATraceSpec("t4", 1, "S11", "MLOG", enabled=False),
        ]
        inst.set_vna_trace_specs(specs)
        inst.set_channel_sparams([(1, "S41"), (2, "S21")])
        _wait(qtbot, lambda: (1, "S41") in inst.channel_traces() and (2, "S21") in inst.channel_traces())

        rendered = mon.vna_traces(inst)
        assert [lbl for lbl, _tr in rendered] == ["1·S41 MLOG", "1·S41 PHAS", "2·S21 SWR"]
        assert [tr.y_unit for _lbl, tr in rendered] == ["dB", "deg", ""]

        # Изменение PC-настройки (EDEL / выключение трассы) мгновенно обновляет ActivityMonitor
        before = len(changes)
        inst.set_vna_trace_specs([specs[0], specs[2]])
        assert len(changes) > before
        assert [lbl for lbl, _tr in mon.vna_traces(inst)] == ["1·S41 MLOG", "2·S21 SWR"]
    finally:
        mon.stop()
        COORD.links.remove(inst)
        inst.shutdown()
