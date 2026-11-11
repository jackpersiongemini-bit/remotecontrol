import pytest

from visalab.core.scpi import (compile_pattern, match_header, parse_bool, parse_number,
                               si_format, si_parse, split_header, split_units, format_number,
                               strip_response_header)


@pytest.mark.parametrize("text,value", [
    ("100HZ", 100.0), ("0.01S", 0.01), ("2.4e-07S", 2.4e-7), ("30mA", 0.03), ("3A", 3.0),
    (" 4V ", 4.0), ("-1V", -1.0), ("1e+07HZ", 1e7), ("1.414Vrms", 1.414), ("10MHZ", 1e7), ("1.5 kHz", 1500.0),
    ("+12.500", 12.5),
])
def test_parse_number(text, value):
    assert parse_number(text) == pytest.approx(value)


@pytest.mark.parametrize("bad", ["", "ON", "abc", "12 parsecs"])
def test_parse_number_bad(bad):
    with pytest.raises(ValueError):
        parse_number(bad)


def test_bool_and_format():
    assert parse_bool("ON") and parse_bool("1") and not parse_bool(" off ")
    assert format_number(1000.0) == "1000" and format_number(1.5e-7) == "1.5e-07"


@pytest.mark.parametrize("pattern,header,ok", [
    ("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]", "VOLT", True),
    ("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]", "SOUR:VOLTAGE:LEV", True),
    ("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]", "VOLTA", False),
    ("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]", "VOLT:PROT", False),
    ("OUTPut[:STATe][:ALL]", "OUTP:STAT:ALL", True),
    ("STATus:QUEStionable:INSTrument:ISUMmary#:CONDition", "stat:ques:inst:isum2:cond", True),
    ("C#:BSWV", "C1:BSWV", True),
    ("*IDN", "*IDN", True),
])
def test_patterns(pattern, header, ok):
    assert bool(match_header(pattern, header)) is ok


def test_capture():
    assert compile_pattern("C#:BSWV").match("C2:BSWV").groups() == ("2",)


def test_split():
    assert split_units('DISP:TEXT "a;b";*OPC?') == ['DISP:TEXT "a;b"', "*OPC?"]
    assert split_header("C1: BSWV FRQ,10") == ("C1:BSWV", "FRQ,10", False)
    assert split_header("CURR? MAX") == ("CURR", "MAX", True)


def test_si():
    assert si_format(1500, "Hz") == "1.5 kHz"
    assert si_format(0.0025, "V") == "2.5 mV"
    assert si_parse("1,5 kHz", "Hz") == pytest.approx(1500)
    assert si_parse("20m", "V") == pytest.approx(0.02)
    assert si_parse("3") == 3


@pytest.mark.parametrize("raw,clean", [
    ("*ESR 1", "1"), ("*OPC 1", "1"), ("CMR 0", "0"), ("C1:SYNC OFF", "OFF"),
    ("C1: OUTP ON,LOAD,HZ", "ON,LOAD,HZ"), ("C2:BSWV WVTP,SINE,FRQ,100HZ", "WVTP,SINE,FRQ,100HZ"),
    ("1", "1"), ("Siglent Technologies,SDG2042X,X,1", "Siglent Technologies,SDG2042X,X,1"),
])
def test_strip_response_header(raw, clean):
    assert strip_response_header(raw) == clean


def test_lan_address_shortcut():
    from visalab.drivers import make_transport, normalize_address
    assert normalize_address("192.168.0.3") == "TCPIP0::192.168.0.3::INSTR"
    assert normalize_address("192.168.0.3:5025") == "TCPIP0::192.168.0.3::5025::SOCKET"
    assert normalize_address("USB0::0xF4EC::0x1102::X::INSTR") == "USB0::0xF4EC::0x1102::X::INSTR"
    assert make_transport("sdg", "192.168.0.3").address == "TCPIP0::192.168.0.3::INSTR"


def test_pyvisa_transport_close_keeps_shared_resource_manager(monkeypatch):
    """ResourceManager pyvisa общий на процесс: закрытие одного прибора не должно рвать остальные."""
    import pyvisa
    from visalab.core import PyVisaTransport

    class Res:
        interface_type = None
        resource_class = "INSTR"
        closed = False
        timeout = 0

        def close(self):
            self.closed = True

    class RM:
        closed = False

        def open_resource(self, address, **kw):
            return Res()

        def close(self):
            RM.closed = True

    shared = RM()
    monkeypatch.setattr(pyvisa, "ResourceManager", lambda backend="": shared)
    a, b = PyVisaTransport("TCPIP0::1.1.1.1::INSTR"), PyVisaTransport("TCPIP0::1.1.1.2::INSTR")
    a.open()
    b.open()
    res_b = b._res
    a.close()
    assert not RM.closed and not res_b.closed and b.is_open
