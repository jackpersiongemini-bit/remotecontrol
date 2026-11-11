"""Связь с реальным прибором: то, чего симулятор сам по себе не делает.

* Прибор ответил позже таймаута: ответ остаётся в буфере, и без восстановления каждый следующий
  запрос читал бы ответ на предыдущий (``SimTransport.late`` моделирует это).
* На запрос, которого у модели нет, прибор не отвечает вовсе (-113): это не обрыв связи.
* Закрытие программы: потоки приборов закрываются параллельно и без ``terminate()``.
* Таймаут посреди записи: ключ получает ERROR (а не вечное WRITING), очередь записей не теряется.
"""
import pytest

from visalab.core import ConnectionState, InstrumentProxy, PStatus, SimTransport, TransportTimeout
from visalab.drivers import AgilentPSA, KeysightE8267D, KeysightPNA
from visalab.sim import AgilentPSASim, KeysightE8267DSim, KeysightPNASim

from conftest import flush


def test_late_answer_does_not_shift_following_reads(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    drv.transport.late = {":FREQ?"}
    drv.read_param("rf.frequency")                      # таймаут, ответ «опоздал»
    assert drv.state is ConnectionState.CONNECTED
    assert rec.last_status("rf.frequency")[0] is PStatus.ERROR
    drv.read_param("rf.power")                          # без восстановления сюда пришла бы частота
    power = rec.values["rf.power"]
    drv.read_param("rf.frequency")
    assert rec.values["rf.frequency"] > 1e6 and power < 100      # частота - частота, мощность - мощность
    assert "rf.frequency" not in drv._no_answer        # ошибки команды не было: ключ читается дальше


def test_console_query_without_answer_keeps_connection(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    drv.raw_query(":FREQ 2e9")                          # не запрос: ответа не будет
    assert drv.state is ConnectionState.CONNECTED and rec.comm   # таймаут в журнале, связь есть
    drv.read_param("rf.frequency")
    assert rec.values["rf.frequency"] == pytest.approx(2e9)


def test_dead_instrument_still_disconnects(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)

    def silent():
        raise TransportTimeout("таймаут")
    drv.transport.read = silent                          # прибор выключили: не отвечает ни на что
    drv.read_param("rf.frequency")
    assert drv.state is ConnectionState.ERROR


def test_poll_timeouts_reach_limit_then_disconnect(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    for _ in range(drv.MAX_POLL_FAILURES - 1):
        drv.transport.late = {"*STB?", ":OUTP?", ":FREQ?", ":POW?"}
        drv._on_poll()
        assert drv.state is ConnectionState.CONNECTED
    drv._poll_failures = 0
    drv._on_poll()                                     # удачный опрос сбрасывает счётчик
    assert drv.state is ConnectionState.CONNECTED and drv._poll_failures == 0


def test_spectrum_sweeps_continue_after_trace_timeout(make):
    drv, sim, rec = make(AgilentPSA, AgilentPSASim)
    drv.poll()
    first = rec.values["trace"]
    drv.transport.late = {"TRAC:DATA?"}
    drv._on_poll()                                     # трасса не пришла вовремя
    assert drv.state is ConnectionState.CONNECTED and rec.values["meas.armed"] is True   # развёртки идут
    drv._on_poll()
    assert rec.values["trace"] is not first and len(rec.values["trace"]) == len(first)


def test_vna_sweeps_continue_after_trace_timeout(make):
    drv, sim, rec = make(KeysightPNA, KeysightPNASim)
    drv.poll()
    first = rec.values["m1.trace"]
    drv.transport.late = {"CALC1:DATA? FDATA"}
    drv._on_poll()
    assert drv.state is ConnectionState.CONNECTED and rec.values["meas.armed"] is True
    drv._on_poll()
    assert rec.values["m1.trace"] is not first and len(rec.values["m1.trace"]) == len(first)


def test_device_clear_rearms_opc(make):
    """Device clear (IEEE 488.2) отменяет ожидание *OPC: после него *OPC посылается снова."""
    drv, sim, rec = make(AgilentPSA, AgilentPSASim)
    assert drv._opc_pending
    n = sim.history.count("*OPC")
    drv.after_resync(cleared=True)
    assert sim.history.count("*OPC") == n + 1


def test_shutdown_all_closes_in_parallel(qapp):
    proxies = [InstrumentProxy(KeysightE8267D, SimTransport(KeysightE8267DSim())) for _ in range(3)]
    for p in proxies:
        p.open()
    assert all(p.shutdown_wait_ms() >= 3000 for p in proxies)
    InstrumentProxy.shutdown_all(proxies)
    assert all(not p._thread.isRunning() for p in proxies)
    assert all(p.state is ConnectionState.DISCONNECTED for p in proxies)


def test_pyvisa_transport_decodes_any_byte_and_discards_late(monkeypatch):
    """pyvisa по умолчанию декодирует ascii: байт > 127 давал UnicodeDecodeError мимо обработки ошибок связи."""
    import pyvisa
    from visalab.core import PyVisaTransport, TransportError

    class Res:
        interface_type = None
        resource_class = "INSTR"
        timeout = 0
        encoding = "ascii"
        pending = [b"late answer\n", b"another\n"]

        def read(self):
            raise UnicodeDecodeError("ascii", b"\xb5", 0, 1, "bad")

        def read_raw(self):
            if not Res.pending:
                raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
            return Res.pending.pop(0)

        def close(self):
            pass

    class RM:
        def open_resource(self, address, **kw):
            return Res()

    monkeypatch.setattr(pyvisa, "ResourceManager", lambda backend="": RM())
    t = PyVisaTransport("TCPIP0::1.1.1.1::INSTR")
    t.open()
    assert t._res.encoding == "latin-1"
    with pytest.raises(TransportError):
        t.read()
    t.discard_input()
    assert Res.pending == [] and t.timeout_ms == 2000 and t._res.timeout == 2000


def test_write_timeout_reports_error_status(make):
    """Таймаут посреди записи (связь восстановлена): ключ получает ERROR, а не вечное WRITING,
    и следующие записи из очереди выполняются."""
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)
    drv.transport.late = {":POW?"}
    drv.set_param("rf.power", -3.0)
    drv.set_param("rf.frequency", 2e9)
    flush(drv)
    assert drv.state is ConnectionState.CONNECTED
    assert rec.last_status("rf.power")[0] is PStatus.ERROR
    assert rec.last_status("rf.frequency")[0] is PStatus.WRITTEN


def test_write_lost_connection_reports_error_for_queued(make):
    drv, sim, rec = make(KeysightE8267D, KeysightE8267DSim)

    def dead(*_a):
        raise TransportTimeout("таймаут")
    drv.transport.read = dead
    drv.set_param("rf.power", -3.0)
    drv.set_param("rf.frequency", 2e9)
    flush(drv)
    assert drv.state is ConnectionState.ERROR
    assert ("rf.power", PStatus.ERROR) in [(k, st) for k, st, _m in rec.statuses]
    assert rec.last_status("rf.power")[0] is not PStatus.WRITING          # не «пишется» навсегда
    assert rec.last_status("rf.frequency")[0] is PStatus.ERROR             # из очереди - не отправлена
