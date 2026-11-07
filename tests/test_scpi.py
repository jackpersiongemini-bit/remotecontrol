import pytest

from visalab.core.scpi import (compile_pattern, match_header, parse_bool, parse_number,
                               si_format, si_parse, split_header, split_units, format_number)


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
