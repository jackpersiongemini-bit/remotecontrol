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
    assert rec.values["ch1.frequency"] == 1000.0
    assert rec.values["ch1.wave"] == "SINE"
    assert rec.values["ch1.duty"] is None          # у синуса нет скважности
    assert rec.values["ch2.load"] == "HIZ"           # стандартный токен профиля (у SDG "HZ")


def test_set_frequency(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.frequency", 2.5e6)
    flush(drv)
    assert sim.ch[1]["BSWV"]["FRQ"] == 2.5e6
    assert "C1:BSWV FRQ,2500000" in sim.history
    assert rec.last_status("ch1.frequency")[0] is PStatus.WRITTEN
    assert rec.values["ch1.frequency"] == 2.5e6


def test_writes_are_coalesced(sdg):
    drv, sim, rec = sdg
    for f in (1e3, 2e3, 3e3, 4e3):
        drv.set_param("ch1.frequency", f)
    flush(drv)
    assert [h for h in sim.history if h.startswith("C1:BSWV FRQ")] == ["C1:BSWV FRQ,4000"]


def test_out_of_model_range_is_rejected_before_sending(sdg):
    drv, sim, rec = sdg
    n = len(sim.history)
    drv.set_param("ch1.frequency", 100e6)          # > 40 МГц у SDG2042X
    flush(drv)
    assert rec.last_status("ch1.frequency")[0] is PStatus.INVALID
    assert not any("FRQ" in h for h in sim.history[n:])


def test_coerced_amplitude_on_50_ohm(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.load", "50")
    flush(drv)
    drv.set_param("ch1.amplitude", 15)             # прибор ограничит до 10 Vpp
    flush(drv)
    status, msg = rec.last_status("ch1.amplitude")
    assert status is PStatus.COERCED and rec.values["ch1.amplitude"] == 10.0


def test_modes_are_exclusive(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.mod_state", True)
    flush(drv)
    assert rec.values["ch1.mod_state"] is True and rec.values["ch1.mod_type"] == "AM"
    drv.set_param("ch1.sweep_state", True)
    flush(drv)
    assert rec.values["ch1.sweep_state"] is True
    assert rec.values["ch1.mod_state"] is False    # перечитано через also_read


def test_mod_freq_uses_current_type(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.mod_state", True); flush(drv)
    drv.set_param("ch1.mod_type", "FM"); flush(drv)
    drv.set_param("ch1.mod_freq", 250); flush(drv)
    assert "C1:MDWV FM,FRQ,250" in sim.history
    assert rec.values["ch1.mod_freq"] == 250


def test_burst_infinite(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch2.burst_state", True); flush(drv)
    drv.set_param("ch2.burst_cycles", "INF"); flush(drv)
    assert rec.values["ch2.burst_cycles"] == "INF"
    assert rec.last_status("ch2.burst_cycles")[0] is PStatus.WRITTEN


def test_instrument_error_from_esr(sdg):
    drv, sim, rec = sdg
    drv.raw_write("C1:BSWV FOO,1")               # EXE
    drv.raw_write("BLAH 1")                      # CME
    codes = [m for _, m in rec.errors]
    assert any("исполнения" in m for m in codes) and any("команды" in m for m in codes)


def test_error_param_marks_status(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.wave", "NOISE"); flush(drv)
    drv.set_param("ch1.sweep_state", True); flush(drv)   # свип с шумом запрещён
    assert rec.last_status("ch1.sweep_state")[0] is PStatus.ERROR
    assert rec.values["ch1.sweep_state"] is False


def test_trigger_without_mode_reports(sdg):
    drv, sim, rec = sdg
    drv.run_action("trigger_1", ())
    assert rec.comm and "свип" in rec.comm[-1]
    assert drv.state is ConnectionState.CONNECTED


def test_output_and_all_off(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.output", True); flush(drv)
    assert sim.ch[1]["OUTP"]["STATE"] == "ON" and rec.values["ch1.output"] is True
    drv.run_action("all_off", ())
    assert rec.values["ch1.output"] is False


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


def test_pulse_period_delay_and_eqphase(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.wave", "PULSE")
    drv.set_param("ch1.period", 0.4)
    drv.set_param("ch1.width", 30e-6)
    drv.set_param("ch1.delay", 1e-3)
    flush(drv)
    assert rec.values["ch1.period"] == pytest.approx(0.4)
    assert rec.values["ch1.frequency"] == pytest.approx(2.5)          # перечитана вместе с периодом
    assert rec.values["ch1.width"] == pytest.approx(30e-6)
    assert rec.values["ch1.delay"] == pytest.approx(1e-3)
    assert getattr(sim, "eqphase_count", 0) >= 4                      # после каждого фазового поля
    n = sim.eqphase_count
    drv.set_param("ch1.amplitude", 2.0)                               # амплитуда фазу не трогает
    flush(drv)
    assert sim.eqphase_count == n
    drv.run_action("align_phase", ())
    assert sim.eqphase_count == n + 1


class _LossySim(SiglentSDGSim):
    """Прибор теряет первую запись периода (команда пришла, а значение прежнее, ошибки нет)."""
    lose = 1

    def _bswv_set(self, args, g):
        if self.lose and "PERI" in args.upper():
            self.lose -= 1
            return None
        return super()._bswv_set(args, g)


def test_eqphase_goes_after_confirmed_write(sdg):
    """Как PulseGen.py: запись -> проверка ошибок -> обратное чтение -> только потом EQPHASE."""
    drv, sim, rec = sdg
    drv.set_param("ch1.wave", "PULSE"); flush(drv)
    start = len(sim.history)
    drv.set_param("ch1.period", 0.4); flush(drv)
    seq = sim.history[start:]
    write = next(i for i, u in enumerate(seq) if u.startswith("C1:BSWV PERI"))
    readback = next(i for i, u in enumerate(seq) if u == "C1:BSWV?" and i > write)
    eq = seq.index("EQPHASE")
    assert write < readback < eq and seq[eq + 1] == "*ESR?"            # у EQPHASE своя проверка ошибок
    assert rec.last_status("ch1.period")[0] is PStatus.WRITTEN


def test_rejected_eqphase_is_not_a_period_error(make):
    """Прибор не знает EQPHASE: ошибка - у EQPHASE (один раз), период записан; после
    переподключения EQPHASE пробуется снова."""
    drv, sim, rec = make(SiglentSDG, SiglentSDGSim, sim=SiglentSDGSim(eqphase=False),
                         driver_kw={"model": SDG_MODELS["SDG2042X"]})
    drv.set_param("ch1.wave", "PULSE"); flush(drv)
    assert rec.last_status("ch1.wave")[0] is PStatus.WRITTEN
    assert any("[EQPHASE]" in m for _, m in rec.errors)
    assert not any("[ch1." in m for _, m in rec.errors)
    assert any("EQPHASE" in m and "не принимает" in m for _, m in rec.errors)
    errors = len(rec.errors)
    drv.set_param("ch1.period", 0.4); flush(drv)
    drv.set_param("ch1.width", 1e-4); flush(drv)
    assert rec.last_status("ch1.period")[0] is PStatus.WRITTEN
    assert rec.last_status("ch1.width")[0] is PStatus.WRITTEN
    assert sim.history.count("EQPHASE") == 1 and len(rec.errors) == errors
    drv.close_connection()
    drv.open_connection()
    drv.set_param("ch1.period", 0.5); flush(drv)
    assert sim.history.count("EQPHASE") == 2
    assert rec.last_status("ch1.period")[0] is PStatus.WRITTEN


def test_lost_write_is_sent_again(make):
    drv, sim, rec = make(SiglentSDG, _LossySim, driver_kw={"model": SDG_MODELS["SDG2042X"]})
    drv.set_param("ch1.period", 0.4); flush(drv)
    assert sum(u.startswith("C1:BSWV PERI") for u in sim.history) == 2     # потерянная запись - ещё раз
    assert rec.values["ch1.period"] == pytest.approx(0.4)
    assert rec.last_status("ch1.period")[0] is PStatus.WRITTEN and not rec.errors


def test_failed_or_coerced_write_is_not_repeated(sdg):
    drv, sim, rec = sdg
    drv.set_param("ch1.load", "50"); flush(drv)
    drv.set_param("ch1.amplitude", 15.0); flush(drv)                       # 50 Ом: прибор ограничит до 10 В
    assert sum(u.startswith("C1:BSWV AMP") for u in sim.history) == 1
    assert rec.last_status("ch1.amplitude")[0] is PStatus.COERCED
    drv.set_param("ch1.wave", "NOISE"); flush(drv)
    drv.set_param("ch1.sweep_state", True); flush(drv)                     # ошибка прибора - не повторять
    assert sum(u.startswith("C1:SWWV STATE") for u in sim.history) == 1
    assert rec.last_status("ch1.sweep_state")[0] is PStatus.ERROR


@pytest.mark.real_pacing
def test_default_pause_before_every_exchange(make):
    """150 мс от конца прошлого обмена до каждой команды, запроса и опроса статуса (serial poll)."""
    import time
    drv, sim, rec = make(SiglentSDG, SiglentSDGSim, driver_kw={"model": SDG_MODELS["SDG2042X"]})
    assert drv.command_delay_s == pytest.approx(0.15)
    t = drv.transport
    spans, depth = [], [0]

    def timed(fn):
        def call(*a, **kw):                 # query транспорта сам зовёт write - считать только внешний вызов
            depth[0] += 1
            t0 = time.monotonic()
            try:
                return fn(*a, **kw)
            finally:
                depth[0] -= 1
                if not depth[0]:
                    spans.append((t0, time.monotonic(), fn.__name__))
        return call

    for name in ("write", "query", "read_stb"):
        setattr(t, name, timed(getattr(t, name)))
    drv.set_param("ch1.period", 2e-3); flush(drv)
    drv._on_poll()
    names = [n for *_, n in spans]
    assert "read_stb" in names and "write" in names and "query" in names
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
    assert min(gaps) >= 0.149, list(zip(names[1:], gaps))
