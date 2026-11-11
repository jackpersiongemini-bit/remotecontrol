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


def _meas(s, fmt="MLOG", ch=1):
    return {"s": s, "fmt": fmt, "ch": ch}


def test_only_selected_measurements_are_read(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21", "PHAS")
    sim.meas["USER_S12"] = _meas("S12", "MLIN")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    drv.poll()
    # по умолчанию слоты - измерения канала 1 по порядку, читается только m1
    assert [rec.values[f"m{n}.name"] for n in (1, 2, 3, 4)] == ["CH1_S11_1", "USER_S21", "USER_S12", None]
    assert [rec.values[f"m{n}.enabled"] for n in (1, 2, 3)] == [True, False, False]
    assert rec.values["meas.count"] == 1 and rec.values["m1.trace"] is not None
    assert rec.values["m2.trace"] is None and rec.values["m2.sdata"] is None
    assert not any("USER_S21" in c and "SEL" in c for c in sim.history[-12:])      # лишнее не запрашивалось
    assert setp(drv, rec, "m3.enabled", True) is PStatus.WRITTEN                    # пользователь включил m3
    drv.poll()
    assert rec.values["meas.count"] == 2 and rec.values["m3.trace"].y_unit == "" and rec.values["m2.trace"] is None
    assert setp(drv, rec, "m1.enabled", False) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["m1.trace"] is None and rec.values["trace"] is None and rec.values["m3.trace"] is not None


def test_all_measurements_formatted_and_complex(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21", "PHAS")
    sim.meas["USER_S12"] = _meas("S12", "MLIN")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert setp(drv, rec, "meas.complex", True) is PStatus.WRITTEN               # SDATA - по запросу
    for k, v in (("freq.start", 1e9), ("freq.stop", 3e9), ("sweep.points", 201)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    drv.run_action("meas_all")
    drv.poll()
    assert rec.values["meas.count"] == 3 and not rec.errors and not rec.comm
    assert [rec.values[f"m{n}.sparam"] for n in (1, 2, 3)] == ["S11", "S21", "S12"]
    assert [rec.values[f"m{n}.format"] for n in (1, 2, 3)] == ["MLOG", "PHASE", "LIN_MAG"]
    assert rec.values["m4.trace"] is None and rec.values["m4.sdata"] is None
    assert [rec.values[f"m{n}.trace"].y_unit for n in (1, 2, 3)] == ["dB", "deg", ""]
    assert all(len(rec.values[f"m{n}.trace"]) == 201 for n in (1, 2, 3))
    assert rec.values["trace"] is rec.values["m1.trace"]
    s21 = rec.values["m2.sdata"]                                    # комплексные: Re в y, Im в im
    assert s21.is_complex and len(s21.im) == 201 and s21.meta["format"] == "SDATA"
    z = s21.complex_y()
    assert math.degrees(__import__("cmath").phase(z[100])) == pytest.approx(rec.values["m2.trace"].y[100], abs=1e-6)
    assert 20 * math.log10(abs(rec.values["m3.sdata"].complex_y()[100])) == pytest.approx(
        20 * math.log10(rec.values["m3.trace"].y[100]), abs=1e-6)
    drv.run_action("meas_none")
    drv.poll()
    assert rec.values["meas.count"] == 0 and rec.values["m1.trace"] is None


def test_other_channels(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    sim.stim(2).update(start=2e9, stop=4e9, poin=51)                # у канала 2 свой стимул
    sim.meas["CH2_S11"] = _meas("S11", "PHAS", 2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert setp(drv, rec, "meas.complex", True) is PStatus.WRITTEN               # SDATA - по запросу
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("meas_catalog")
    assert actions[-1] == ("meas_catalog", [(1, "CH1_S11_1", "S11"), (2, "CH2_S21", "S21"), (2, "CH2_S11", "S11")])
    # слот m2 -> канал 2, измерение CH2_S21
    assert setp(drv, rec, "m2.channel", 2) is PStatus.WRITTEN
    assert setp(drv, rec, "m2.name", "CH2_S21") is PStatus.WRITTEN
    assert rec.values["m2.sparam"] == "S21" and rec.values["m2.format"] == "MLOG"
    assert setp(drv, rec, "m2.enabled", True) is PStatus.WRITTEN
    drv.poll()
    t1, t2 = rec.values["m1.trace"], rec.values["m2.trace"]
    assert len(t2) == 51 and t2.x[0] == pytest.approx(2e9) and t2.x[-1] == pytest.approx(4e9)     # ось канала 2
    assert t2.meta["channel"] == 2 and t2.name == "S21" and len(t1) == 201 and t1.meta["channel"] == 1
    assert "SENS2:SWE:MODE SING" in sim.history and "CALC2:PAR:SEL 'CH2_S21'" in sim.history
    assert len(rec.values["m2.sdata"]) == 51 and not rec.errors and not rec.comm
    # формат и S-параметр меняются у измерения своего канала
    assert setp(drv, rec, "m2.format", "PHASE") is PStatus.WRITTEN and sim.meas["CH2_S21"]["fmt"] == "PHAS"
    assert sim.meas["CH2_S11"]["fmt"] == "PHAS" and sim.meas["CH1_S11_1"]["fmt"] == "MLOG"
    # выключили m1: канал 1 больше не разворачивается
    assert setp(drv, rec, "m1.enabled", False) is PStatus.WRITTEN
    del sim.history[:]
    drv.poll()
    assert "SENS1:SWE:MODE SING" not in sim.history and "SENS2:SWE:MODE SING" in sim.history
    # meas_all берёт измерения всех каналов
    drv.run_action("meas_all")
    drv.poll()
    assert rec.values["meas.count"] == 3 and [rec.values[f"m{n}.channel"] for n in (1, 2, 3)] == [1, 2, 2]


def test_slot_errors_and_deleted_measurement(make):
    sim = KeysightPNASim()
    sim.meas["B"] = _meas("S21")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert setp(drv, rec, "m2.format", "PHASE") is PStatus.WRITTEN
    assert sim.meas["B"]["fmt"] == "PHAS" and sim.meas["CH1_S11_1"]["fmt"] == "MLOG"       # первое не тронуто
    assert setp(drv, rec, "m2.sparam", "S12") is PStatus.WRITTEN and sim.meas["B"]["s"] == "S12"
    assert setp(drv, rec, "m5.format", "PHASE") is PStatus.ERROR                           # слот не назначен
    assert setp(drv, rec, "m2.name", "NO_SUCH") is PStatus.ERROR                           # нет такого измерения
    assert rec.values["m2.name"] == "B" and not rec.comm
    drv.run_action("meas_all")
    drv.poll()
    assert rec.values["meas.count"] == 2 and rec.values["m2.trace"].y_unit == "deg"
    del sim.meas["B"]                                                                      # удалили на приборе
    drv.poll()
    assert rec.values["meas.count"] == 1 and rec.values["m2.trace"] is None and not rec.comm
    assert setp(drv, rec, "meas.complex", False) is PStatus.WRITTEN
    drv.poll()
    assert rec.values["m1.sdata"] is None and rec.values["m1.trace"] is not None


def test_memory_and_math(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert setp(drv, rec, "meas.complex", True) is PStatus.WRITTEN               # SDATA - по запросу
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("meas_all")
    assert setp(drv, rec, "m2.math", "DIV") is not PStatus.WRITTEN      # памяти ещё нет - прибор отказал
    assert rec.values["m2.math"] == "NORM" and "math" not in sim.meas["USER_S21"]
    drv.run_action("mem_store")                                         # все включённые слоты
    assert actions[-1] == ("mem_store", [1, 2]) and sim.meas["USER_S21"]["mem"] is not None
    assert setp(drv, rec, "m2.math", "DIV") is PStatus.WRITTEN and sim.meas["USER_S21"]["math"] == "DIV"
    drv.poll()
    t = rec.values["m2.trace"]
    assert max(abs(v) for v in t.y) < 1e-6 and t.meta["math"] == "DIV"   # данные / та же память = 0 dB
    assert rec.values["m1.trace"].meta["math"] == "NORM"
    assert rec.values["m2.sdata"].meta["math"] == "NORM" and abs(rec.values["m2.sdata"].complex_y()[100]) < 1
    sim.dut["il_db"] = 3.0                                              # DUT изменился: видно отличие от памяти
    drv.poll()
    assert rec.values["m2.trace"].y[100] == pytest.approx(-2.0, abs=0.01)
    assert setp(drv, rec, "m2.math", "NORM") is PStatus.WRITTEN
    drv.run_action("mem_store", 1)
    assert actions[-1] == ("mem_store", [1])
    drv.run_action("mem_store", 5)                                      # не назначен - отказ, в прибор не ушло
    assert len(rec.comm) == 1 and "m5" in rec.comm[0] and actions[-1] == ("mem_store", [1])


def test_state_save_recall(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("state_list")
    assert actions[-1] == ("state_list", [])
    assert setp(drv, rec, "freq.start", 1.5e9) is PStatus.WRITTEN
    drv.run_action("state_save", "filt_a")
    assert 'MMEM:STOR "filt_a.csa"' in sim.history and rec.values["meas.armed"] is True
    sim.files["notes.txt"] = ()
    drv.run_action("state_list")
    assert actions[-1] == ("state_list", ["filt_a"])                    # только имена состояний
    # на приборе всё поменяли: другое измерение, другая частота
    assert setp(drv, rec, "freq.start", 1e9) is PStatus.WRITTEN
    del sim.meas["USER_S21"]
    sim.meas["OTHER"] = _meas("S12")
    drv.run_action("state_recall", "filt_a")
    assert rec.values["freq.start"] == pytest.approx(1.5e9)
    assert rec.values["m2.name"] == "USER_S21" and rec.values["m1.enabled"] and not rec.values["m2.enabled"]
    drv.poll()
    assert rec.values["m1.trace"].x[0] == pytest.approx(1.5e9) and not rec.errors and not rec.comm
    drv.run_action("state_recall", "bad name")                          # недопустимое имя в прибор не уходит
    assert "bad name" not in " ".join(sim.history)
    drv.run_action("state_recall", "nope")
    assert any("state_recall" in m for _, m in rec.errors)              # нет файла - ошибка прибора
    drv.run_action("state_delete", "filt_a")
    assert "filt_a.csa" not in sim.files


def test_cw_mode_as_generator(vna):
    drv, sim, rec = vna
    assert rec.values["sweep.type"] == "LIN"
    for k, v in (("sweep.type", "CW"), ("freq.cw", 2e9), ("source.power", 0.0), ("rf.output", True),
                 ("cal.state", False)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    assert sim.c["type"] == "CW" and sim.c["cw"] == 2e9 and sim.c["outp"] and not sim.c["corr"]
    drv.poll()
    t = rec.values["m1.trace"]
    assert t.x_unit == "s" and t.x[0] == 0 and t.x[-1] == pytest.approx(rec.values["sweep.time"])
    assert max(t.y) - min(t.y) < 1e-9                                     # одна частота во всех точках
    assert setp(drv, rec, "sweep.type", "LIN") is PStatus.WRITTEN
    drv.poll()
    assert rec.values["m1.trace"].x_unit == "Hz" and not rec.errors and not rec.comm


def test_measurement_table_create_config_delete(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21", "PHAS")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("meas_table")
    assert actions[-1] == ("meas_table", [(1, "CH1_S11_1", "S11", "MLOG", "NORM"), (1, "USER_S21", "S21", "PHASE", "NORM")])
    # настройка измерения, которое не читается (не в слоте... m2 занят им по умолчанию - тоже обновится)
    drv.run_action("meas_config", (1, "USER_S21", "format", "SWR"))
    assert sim.meas["USER_S21"]["fmt"] == "SWR" and rec.values["m2.format"] == "SWR"
    drv.run_action("meas_config", (1, "USER_S21", "sparam", "S12"))
    assert sim.meas["USER_S21"]["s"] == "S12" and actions[-1][1][1][2] == "S12"
    # создать на канале 2 (новый канал) и на канале 1
    drv.run_action("meas_create", (2, "S21"))
    drv.run_action("meas_create", (1, "S21"))
    table = actions[-1][1]
    assert (2, "CH2_S21_1", "S21", "MLOG", "NORM") in table and (1, "CH1_S21_1", "S21", "MLOG", "NORM") in table
    assert sim.windows[2] == {1: "CH2_S21_1"} and sim.windows[1] == {1: "CH1_S11_1", 2: "CH1_S21_1"}
    assert not rec.errors and not rec.comm
    # удалить измерение, которое читается в слоте: слот освобождается
    drv.run_action("meas_delete", (1, "CH1_S11_1"))
    assert "CH1_S11_1" not in sim.meas and rec.values["m1.name"] is None and rec.values["m1.enabled"] is False
    drv.poll()
    assert rec.values["meas.count"] == 0 and not rec.errors and not rec.comm
    drv.run_action("meas_config", (1, "NOPE", "format", "SWR"))
    drv.run_action("meas_create", (1, "S99"))
    assert len(rec.comm) == 2


def test_measurement_move_to_other_channel(make):
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = _meas("S21", "PHAS")
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    assert setp(drv, rec, "m2.enabled", True) is PStatus.WRITTEN          # USER_S21 читается в m2
    drv.run_action("meas_move", (1, "USER_S21", 3))
    assert "USER_S21" not in sim.meas and sim.meas["CH3_S21_1"] == {"s": "S21", "fmt": "PHAS", "ch": 3}
    assert actions[-1][1] == [(1, "CH1_S11_1", "S11", "MLOG", "NORM"), (3, "CH3_S21_1", "S21", "PHASE", "NORM")]
    assert rec.values["m2.channel"] == 3 and rec.values["m2.name"] == "CH3_S21_1" and rec.values["m2.enabled"]
    drv.poll()
    assert rec.values["m2.trace"].meta["channel"] == 3 and not rec.errors and not rec.comm


def test_complex_formats_and_sdata_off_by_default(vna):
    drv, sim, rec = vna
    drv.poll()
    assert rec.values["meas.complex"] is False and rec.values["m1.sdata"] is None
    assert not any("SDATA" in c for c in sim.history)                       # лишнего не читаем
    for fmt in ("POLAR", "SMITH"):
        assert setp(drv, rec, "m1.format", fmt) is PStatus.WRITTEN
        drv.poll()
        t = rec.values["m1.trace"]
        assert t.is_complex and len(t.im) == len(t) == 201 and t.meta["format"] == fmt and t.y_unit == ""
    z = t.complex_y()
    assert abs(z[0]) == pytest.approx(1.0, abs=0.05)                         # S11 вне полосы ~ 1
    drv.run_action("marker_peak")
    assert not rec.errors and not rec.comm


def test_stimulus_of_selected_channel(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert rec.values["stim.channel"] == 1 and rec.values["freq.start"] == pytest.approx(10e6)
    assert setp(drv, rec, "stim.channel", 2) is PStatus.WRITTEN
    assert rec.values["freq.start"] == pytest.approx(1e9) and rec.values["sweep.points"] == 101   # канал 2 перечитан
    for k, v in (("freq.start", 3e9), ("freq.stop", 4e9), ("sweep.points", 11), ("bw.if", 1e3), ("avg.state", True),
                 ("source.power", -20.0), ("sweep.type", "CW"), ("freq.cw", 3.5e9)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    st2 = sim.stim(2)
    assert (st2["start"], st2["stop"], st2["poin"], st2["bwid"], st2["aver"], st2["pow"], st2["type"], st2["cw"]) == \
        (3e9, 4e9, 11, 1e3, True, -20.0, "CW", 3.5e9)
    assert (sim.c["start"], sim.c["poin"], sim.c["pow"], sim.c["type"]) == (10e6, 201, -5.0, "LIN")   # канал 1 не тронут
    assert "SENS2:FREQ:STAR 3000000000" in sim.history and "SOUR2:POW1 -20" in sim.history
    assert setp(drv, rec, "stim.channel", 7) is PStatus.ERROR                      # нет такого канала
    assert setp(drv, rec, "stim.channel", 1) is PStatus.WRITTEN
    assert rec.values["freq.start"] == pytest.approx(10e6) and rec.values["sweep.type"] == "LIN"
    assert not rec.comm


def test_generator_mode_stops_other_channels_and_restores(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    drv.run_action("meas_all")
    drv.poll()
    assert rec.values["m2.trace"] is not None and rec.values["gen.state"] is False
    assert setp(drv, rec, "stim.channel", 2) is PStatus.WRITTEN
    for k, v in (("sweep.type", "LOG"), ("source.power", -15.0), ("rf.output", False), ("avg.state", True),
                 ("bw.if", 10e3)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    before = dict(sim.stim(2))
    assert setp(drv, rec, "gen.state", True) is PStatus.WRITTEN
    assert sim.stim(2)["type"] == "CW" and rec.values["sweep.type"] == "CW" and rec.values["gen.channel"] == 2
    # в генераторе меняем что угодно
    for k, v in (("freq.cw", 3e9), ("source.power", 5.0), ("rf.output", True), ("avg.state", False),
                 ("bw.if", 1e3), ("sweep.points", 3)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN, k
    assert setp(drv, rec, "stim.channel", 1) is PStatus.ERROR            # канал закреплён
    del sim.history[:]
    drv.poll()
    assert "SENS1:SWE:MODE SING" not in sim.history and "SENS2:SWE:MODE SING" in sim.history
    assert rec.values["m1.trace"] is None and rec.values["m2.trace"].x_unit == "s"   # канал 1 стоит
    # выключили - всё как было, частота CW осталась
    assert setp(drv, rec, "gen.state", False) is PStatus.WRITTEN
    after = sim.stim(2)
    for k in ("type", "start", "stop", "poin", "bwid", "pow", "aver", "coun", "swt_auto", "corr"):
        assert after[k] == before[k], k
    assert after["cw"] == 3e9 and sim.c["outp"] is False
    assert rec.values["sweep.type"] == "LOG" and rec.values["source.power"] == pytest.approx(-15.0)
    assert rec.values["gen.channel"] is None and rec.values["stim.channel"] == 2
    del sim.history[:]
    drv.poll()
    assert "SENS1:SWE:MODE SING" in sim.history and rec.values["m1.trace"] is not None
    assert not rec.comm


def test_generator_mode_restored_on_disconnect(vna):
    drv, sim, rec = vna
    assert setp(drv, rec, "gen.state", True) is PStatus.WRITTEN and sim.c["type"] == "CW"
    drv.close_connection()
    assert sim.c["type"] == "LIN"


def test_generator_mode_keeps_sweep_modes_of_other_channels(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)           # не читается, на приборе CONT
    sim.meas["CH3_S11"] = _meas("S11", "MLOG", 3)
    sim.stim(3)["mode"] = "HOLD"                             # пользователь остановил канал 3
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert sim.stim(2)["mode"] == "CONT"
    assert setp(drv, rec, "gen.state", True) is PStatus.WRITTEN
    assert (sim.stim(2)["mode"], sim.stim(3)["mode"], sim.c["type"]) == ("HOLD", "HOLD", "CW")
    del sim.history[:]
    drv.poll()
    assert not any(c.startswith(("SENS2:SWE:MODE", "SENS3:SWE:MODE")) for c in sim.history)
    assert setp(drv, rec, "gen.state", False) is PStatus.WRITTEN
    assert (sim.stim(2)["mode"], sim.stim(3)["mode"], sim.c["type"]) == ("CONT", "HOLD", "LIN")
    assert not rec.comm


def test_trigger_settings_external_and_manual(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    v = rec.values
    assert (v["trig.source"], v["trig.scope"], v["trig.mode"], v["trig.slope"], v["trig.type"], v["trig.delay"]) == \
        ("IMM", "ALL", "CHANNEL", "POS", "EDGE", 0.0)
    for k, val in (("trig.slope", "NEG"), ("trig.type", "LEVEL"), ("trig.delay", 0.01), ("trig.scope", "CURRENT")):
        assert setp(drv, rec, k, val) is PStatus.WRITTEN, k
    assert sim.trig == {"sour": "IMM", "scop": "CURR", "slop": "NEG", "type": "LEV", "del": 0.01}
    # режим запуска - у канала CH
    assert setp(drv, rec, "stim.channel", 2) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.mode", "POINT") is PStatus.WRITTEN
    assert sim.stim(2)["tmode"] == "POIN" and sim.c["tmode"] == "CHAN"
    assert setp(drv, rec, "stim.channel", 1) is PStatus.WRITTEN and v["trig.mode"] == "CHANNEL"
    # внешний: развёртка ждёт импульса на входе
    assert setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN and sim.trig["sour"] == "EXT"
    done = sim.sweeps_done
    for _ in range(3):
        drv.poll()
    assert sim.sweeps_done == done and v["meas.armed"] is True and not rec.comm     # ждём, без таймаута
    sim.trigger()
    drv.poll()
    assert sim.sweeps_done == done + 1 and v["m1.trace"] is not None
    # ручной + по точке: каждый TRIG - одна точка
    assert setp(drv, rec, "sweep.points", 3) is PStatus.WRITTEN
    assert setp(drv, rec, "trig.mode", "POINT") is PStatus.WRITTEN
    assert setp(drv, rec, "trig.source", "MAN") is PStatus.WRITTEN
    done = sim.sweeps_done
    for i in range(3):
        drv.poll()
        assert sim.sweeps_done == done
        drv.run_action("trigger")
    assert "INIT1:IMM" in sim.history
    drv.poll()
    assert sim.sweeps_done == done + 1 and len(v["m1.trace"]) == 3 and not rec.comm


def test_scale_and_processing_of_selected_trace(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "PHAS", 2)
    sim.windows[2] = {1: "CH2_S21"}
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    v = rec.values
    assert v["trace.sel"] == "1:CH1_S11_1" and v["scale.div"] == 10.0 and v["scale.rpos"] == 5.0
    assert v["smooth.state"] is False and v["scale.edel"] == 0.0
    assert setp(drv, rec, "trace.sel", "2:CH2_S21") is PStatus.WRITTEN
    for k, val in (("scale.div", 45.0), ("scale.ref", -90.0), ("scale.rpos", 8.0)):
        assert setp(drv, rec, k, val) is PStatus.WRITTEN, k
    m = sim.meas["CH2_S21"]
    assert (m["pdiv"], m["rlev"], m["rpos"]) == (45.0, -90.0, 8.0) and "pdiv" not in sim.meas["CH1_S11_1"]
    assert "DISP:WIND2:TRAC1:Y:PDIV 45" in sim.history
    drv.run_action("autoscale")
    assert m["pdiv"] != 45.0 and v["scale.div"] == m["pdiv"] and v["scale.rpos"] == 5.0
    # задержка и сглаживание меняют читаемые данные
    assert setp(drv, rec, "m2.channel", 2) is PStatus.WRITTEN and setp(drv, rec, "m2.name", "CH2_S21") is PStatus.WRITTEN
    assert setp(drv, rec, "m2.enabled", True) is PStatus.WRITTEN
    drv.poll()
    raw = list(v["m2.trace"].y)
    assert setp(drv, rec, "scale.edel", 1e-9) is PStatus.WRITTEN and m["edel"] == 1e-9
    drv.poll()
    assert list(v["m2.trace"].y) != raw
    assert setp(drv, rec, "scale.edel", 0.0) is PStatus.WRITTEN
    assert setp(drv, rec, "smooth.aper", 10.0) is PStatus.WRITTEN
    assert setp(drv, rec, "smooth.state", True) is PStatus.WRITTEN and m["smo"] and m["aper"] == 10.0
    drv.poll()
    sm = list(v["m2.trace"].y)
    assert sm != raw and max(sm) - min(sm) < max(raw) - min(raw)          # сглаженная - ровнее
    assert setp(drv, rec, "smooth.aper", 50.0) is not PStatus.WRITTEN      # больше 25 % нельзя
    assert setp(drv, rec, "trace.sel", "2:NOPE") is PStatus.ERROR
    assert not rec.comm


def test_cal_set_per_channel(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    v = rec.values
    assert v["cal.set"] == "" and v["cal.state"] is False and v["cal.stim"] is True and v["cal.interp"] is True
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("cal_list")
    assert actions[-1] == ("cal_list", ["CAL_1-3G", "CAL_FULL"])
    assert setp(drv, rec, "cal.state", True) is not PStatus.WRITTEN and v["cal.state"] is False   # без cal set
    # cal set со стимулом: канал 1 берёт частоты / точки / полосу / мощность калибровки
    assert setp(drv, rec, "cal.set", "CAL_1-3G") is PStatus.WRITTEN
    assert v["cal.set"] == "CAL_1-3G" and v["cal.state"] is True
    assert (v["freq.start"], v["freq.stop"], v["sweep.points"], v["bw.if"]) == (1e9, 3e9, 201, 1e3)
    # канал 2: без стимула, обзор внутри калибровки - интерполяция
    assert setp(drv, rec, "stim.channel", 2) is PStatus.WRITTEN and v["cal.set"] == ""
    assert setp(drv, rec, "cal.stim", False) is PStatus.WRITTEN
    assert setp(drv, rec, "cal.set", "CAL_FULL") is PStatus.WRITTEN
    assert v["cal.set"] == "CAL_FULL" and v["cal.state"] is True and v["freq.start"] == pytest.approx(1e9)
    assert 'SENS2:CORR:CSET:ACT "CAL_FULL",0' in sim.history
    assert setp(drv, rec, "cal.set", "NOPE") is PStatus.ERROR
    assert setp(drv, rec, "cal.set", "") is PStatus.WRITTEN               # снять
    assert v["cal.set"] == "" and v["cal.state"] is False and sim.stim(2)["cset"] == ""
    assert sim.c["cset"] == "CAL_1-3G"                                     # канал 1 не тронут
    # удаление cal set снимает его с канала
    assert setp(drv, rec, "stim.channel", 1) is PStatus.WRITTEN and v["cal.set"] == "CAL_1-3G"
    drv.run_action("cal_delete", "CAL_1-3G")
    assert actions[-1] == ("cal_delete", ["CAL_FULL"])
    assert v["cal.set"] == "" and v["cal.state"] is False
    assert not rec.comm


def test_guided_calibration(make):
    drv, sim, rec = make(KeysightPNA, KeysightPNASim)
    v = rec.values
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("cal_conns")
    assert actions[-1][1] == ["APC 3.5 female", "APC 3.5 male", "Type N (50) female", "Type N (50) male"]
    drv.run_action("cal_kits", "APC 3.5 female")
    assert actions[-1][1][0] == "Ideal 3.5mm"                  # идеальный набор - первым
    with pytest.raises(ValueError):
        drv.run_custom_action("cal_measure", (1,))             # калибровка не начата
    drv.run_action("cal_start", ("APC 3.5 female", "Ideal 3.5mm", "Type N (50) male", "Ideal Type N"))
    steps, done = actions[-1][1]
    assert len(steps) == 7 and steps[0] == "Connect APC 3.5 female OPEN to port 1" and done == []
    assert "THRU" in steps[-1] and not drv._armed               # развёртки стоят
    with pytest.raises(ValueError):
        drv.run_custom_action("cal_save", ("MYCAL",))           # не все стандарты
    for k in range(1, 8):
        drv.run_action("cal_measure", k)
        assert v["cal.busy"] is True
        drv.poll()
        assert v["cal.busy"] is False
    drv.run_action("cal_status")
    assert actions[-1][1] == (steps, list(range(1, 8)))
    drv.run_action("cal_save", "MYCAL")
    assert "MYCAL" in actions[-1][1] and sim.c["cset"] == "MYCAL" and sim.c["corr"]
    assert v["cal.set"] == "MYCAL" and v["cal.state"] is True and drv._armed      # развёртки снова идут
    # ECal на одном порту: один шаг; прервать
    drv.run_action("cal_start", ("APC 3.5 male", "N4691-60004 ECal", "", ""))
    assert actions[-1][1][0] == ["Connect ECal module to port 1"]
    drv.run_action("cal_abort")
    assert drv._cal is None and drv._armed and not rec.comm


def test_snp_read_for_touchstone(make, tmp_path):
    from visalab.core.export import write_touchstone
    drv, sim, rec = make(KeysightPNA, KeysightPNASim)
    for k, v in (("freq.start", 1e9), ("freq.stop", 3e9), ("sweep.points", 11)):
        assert setp(drv, rec, k, v) is PStatus.WRITTEN
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    drv.run_action("snp_read", "1,2")
    sp = actions[-1][1]
    assert list(sp) == ["S11", "S21", "S12", "S22"] and sim.snp_form == "RI"
    s21 = sp["S21"]
    assert s21.is_complex and len(s21) == 11 and s21.x[0] == 1e9 and s21.x[-1] == 3e9
    assert s21.complex_y()[5] == pytest.approx(sim.s("S21", 2e9))
    path = write_touchstone(tmp_path / "dut.s2p", sp, ["test"])
    lines = path.read_text().splitlines()
    assert lines[0] == "! test" and "# HZ S RI R 50" in lines and len(lines) == 3 + 11
    assert len(lines[-1].split()) == 9
    drv.run_action("snp_read", "2")
    assert list(actions[-1][1]) == ["S22"]
    assert drv._armed and not rec.comm


def test_thru_response_calibration(make):
    sim = KeysightPNASim()
    sim.meas["CH1_S21"] = _meas("S21", "MLOG", 1)
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    with pytest.raises(ValueError):
        drv.run_custom_action("cal_start_thru", ())          # выбран S11 - не перемычка
    assert setp(drv, rec, "trace.sel", "1:CH1_S21") is PStatus.WRITTEN
    drv.run_action("cal_start_thru")
    steps, done = actions[-1][1]
    assert steps == ["Connect THRU between port 1 and port 2 (S21 CH1_S21)"] and done == [] and not drv._armed
    drv.run_action("cal_measure", 1)
    drv.poll()
    assert rec.values["cal.busy"] is False
    drv.run_action("cal_save", "THRU_CAL")
    assert "THRU_CAL" in actions[-1][1] and sim.c["cset"] == "THRU_CAL" and sim.c["corr"]
    assert "SENS1:CORR:COLL:METH TRAN1" in sim.history and drv._armed and not rec.comm


def test_markers_of_selected_trace(make):
    sim = KeysightPNASim()
    sim.meas["CH2_S21"] = _meas("S21", "MLOG", 2)
    sim.windows[2] = {1: "CH2_S21"}
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    v = rec.values
    assert v["mark.n"] == 1 and v["mark.state"] is False and v["mark.y"] is None
    actions = []
    drv.action_done.connect(lambda n, r: actions.append((n, r)))
    assert setp(drv, rec, "trace.sel", "2:CH2_S21") is PStatus.WRITTEN
    drv.run_action("mark_peak")                                 # M1 на максимум S21 канала 2 (1..2 ГГц)
    assert v["mark.state"] is True and v["mark.x"] == pytest.approx(2e9, rel=1e-2)
    assert v["mark.y"] == pytest.approx(-1.0, abs=0.1) and actions[-1][1][0][0] == 1
    assert setp(drv, rec, "mark.n", 3) is PStatus.WRITTEN and v["mark.state"] is False
    assert setp(drv, rec, "mark.x", 1.5e9) is PStatus.WRITTEN and v["mark.state"] is True
    assert sim.meas["CH2_S21"]["marks"][3]["x"] == 1.5e9 and not any(m["on"] for m in sim.meas["CH1_S11_1"].get("marks", {}).values())
    drv.run_action("mark_table")
    assert [n for n, _x, _y in actions[-1][1]] == [1, 3]
    assert setp(drv, rec, "mark.state", False) is PStatus.WRITTEN and v["mark.y"] is None
    assert setp(drv, rec, "trace.sel", "1:CH1_S11_1") is PStatus.WRITTEN and v["mark.state"] is False  # свои маркеры
    assert setp(drv, rec, "trace.sel", "2:CH2_S21") is PStatus.WRITTEN
    drv.run_action("mark_aoff")
    assert not any(m["on"] for m in sim.meas["CH2_S21"]["marks"].values()) and actions[-1] == ("mark_aoff", [])
    assert setp(drv, rec, "mark.n", 11) is not PStatus.WRITTEN
    assert not rec.comm


def test_serial_poll_not_supported_falls_back_to_stb_query(qapp):
    """VI_ERROR_NSUP_OPER на read_stb (LAN): дальше статус через *STB?, без ошибки связи."""
    from visalab.core import SimTransport
    from visalab.core.transport import TransportNotSupported
    from visalab.drivers import DRIVERS
    drv_cls, sim_cls = DRIVERS["pna"]

    class NoPoll(SimTransport):
        def read_stb(self):
            raise TransportNotSupported("The given session or object reference does not support this operation")

    t = NoPoll(sim_cls())
    drv = drv_cls(t)
    comm = []
    drv.comm_error.connect(comm.append)
    drv.open_connection()
    drv._on_poll()
    drv._on_poll()
    assert not comm and t.supports_serial_poll is False
    drv.close_connection()


def test_stimulus_changed_on_the_pna_screen(vna):
    """Стимул поменяли на экране PNA: ось трассы и ключи стимула - по прибору, без SYNC."""
    drv, sim, rec = vna
    drv.poll()
    assert rec.values["trace"].x[-1] == pytest.approx(13.5e9)
    sim.stim(1).update(start=1e9, stop=3e9, poin=201)          # не через драйвер
    drv.poll()
    drv.poll()
    t = rec.values["trace"]
    assert t.x[0] == pytest.approx(1e9) and t.x[-1] == pytest.approx(3e9)
    assert rec.values["freq.start"] == pytest.approx(1e9) and rec.values["freq.stop"] == pytest.approx(3e9)
    assert rec.values["freq.center"] == pytest.approx(2e9) and rec.values["freq.span"] == pytest.approx(2e9)
    assert not rec.errors and not rec.comm
