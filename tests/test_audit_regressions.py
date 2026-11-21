"""Регрессионные тесты по результатам ревизии всех слоёв проекта."""
from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest
from PyQt6.QtCore import QRectF, QSettings

from visalab.coord import COORD, LINKS
from visalab.coord.activity import ActivityMonitor, spectrum_peak
from visalab.coord.calibration import fit_gain_table, interpolate, load_csv
from visalab.coord.follow import FrequencyFollow
from visalab.coord.scenario import ActionStep, AllStep, Device, ScenarioError, SleepStep
from visalab.core.export import write_touchstone, write_traces_csv
from visalab.core.params import Kind, Param
from visalab.core.trace import Trace
from visalab.core.units import parse_number, si_format, si_parse
from visalab.drivers import KeysightE8267D, KeysightU2021XA, RSSMF100A
from visalab.front.live_plot import decimate_trace_polygon, nice_range
from visalab.front.params import PSpinBox
from visalab.front.power_meter import PowerMeterWindow, PowerReadout
from visalab.front.rules import parse_rule
from visalab.instruments import InstrumentProxy
from visalab.sim import KeysightE8267DSim, KeysightU2021XASim, RSSMF100ASim
from visalab.transport import SimTransport, TransportError, lan_endpoint, parse_float_block
from visalab.transport.scpi import compile_pattern

from conftest import attach, l3


# ============================================================ Слой 1: Core / Transport
def test_parse_number_uppercase_scpi_units_and_non_finite():
    assert parse_number("50MV") == pytest.approx(0.05)
    assert parse_number("30MA") == pytest.approx(0.03)
    assert parse_number("10MS") == pytest.approx(0.01)
    assert parse_number("100US") == pytest.approx(1e-4)
    assert parse_number("50NS") == pytest.approx(5e-8)
    assert parse_number("10PS") == pytest.approx(1e-11)
    assert parse_number("10MHZ") == pytest.approx(1e7)
    assert parse_number("2MOHM") == pytest.approx(2e6)
    with pytest.raises(ValueError):
        parse_number("nan")
    with pytest.raises(ValueError):
        parse_number("inf")
    with pytest.raises(ValueError):
        si_parse("nan", "Hz")
    with pytest.raises(ValueError):
        si_parse("inf", "Hz")


def test_si_format_non_finite_and_no_prefix_units():
    assert si_format(math.nan, "Hz") == "nan Hz"
    assert si_format(math.inf, "dBm") == "inf dBm"
    assert si_format(-0.5, "dBm") == "-0.5 dBm"
    assert si_format(0.05, "dB") == "0.05 dB"
    assert si_format(0.05, "deg") == "0.05 deg"
    assert si_format(0.5, "div") == "0.5 div"


def test_param_validate_non_finite_and_str_none():
    pf = Param("f", "F", kind=Kind.FLOAT, minimum=0.0, maximum=10.0)
    pi = Param("i", "I", kind=Kind.INT, minimum=1, maximum=100)
    ps = Param("s", "S", kind=Kind.STR)
    pe = Param("e", "E", kind=Kind.ENUM, choices=("INT", "EXT"), enum_map=(("INT", "INT"), ("INT", "INTERNAL")))
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            pf.validate(bad)
        with pytest.raises(ValueError):
            pi.validate(bad)
    with pytest.raises(ValueError):
        ps.validate(None)
    assert pe.to_scpi("int") == "INT"


def test_trace_peak_ignores_leading_nan_and_rejects_empty():
    tr = Trace((1.0, 2.0, 3.0), (math.nan, -5.0, -2.0))
    assert tr.peak() == (3.0, -2.0)
    with pytest.raises(ValueError):
        Trace((), ()).peak()
    with pytest.raises(ValueError):
        Trace((1.0, 2.0), (math.nan, math.nan)).peak()


def test_export_touchstone_s22_multiline_and_csv_validation(tmp_path: Path):
    f = (1e9, 2e9)
    tr = Trace(f, (0.0, -3.0), x_unit="Hz", y_unit="dB", im=(0.0, 0.0))
    s1p = tmp_path / "p2.s1p"
    write_touchstone(s1p, {"S22": tr}, comments=["line1\nline2"])
    text = s1p.read_text(encoding="utf-8")
    assert "! line1\n! line2\n" in text
    assert "# HZ S RI R 50" in text

    with pytest.raises(ValueError):
        write_touchstone(tmp_path / "bad_z0.s1p", {"S11": tr}, z0=0.0)
    with pytest.raises(ValueError):
        write_touchstone(tmp_path / "bad_unit.s1p", {"S11": Trace(f, (0.0, 0.0), x_unit="s")})
    with pytest.raises(ValueError):
        write_traces_csv(tmp_path / "bad_csv.csv", [tr], ["a", "b"])


def test_parse_float_block_errors_and_indefinite():
    with pytest.raises(TransportError):
        parse_float_block(b"#X1234")
    with pytest.raises(TransportError):
        parse_float_block(b"#2ab1234")
    payload = struct.pack(">2f", 1.5, -2.5)
    assert parse_float_block(b"#0" + payload + b"\n") == pytest.approx([1.5, -2.5])


def test_lan_endpoint_ipv6_and_port_bounds():
    assert lan_endpoint("TCPIP0::[::1]::5025::SOCKET") == ("::1", 5025)
    assert lan_endpoint("TCPIP0::192.168.1.10::99999::SOCKET") is None
    assert lan_endpoint("TCPIP0::192.168.1.10::0::SOCKET") is None


def test_scpi_compile_pattern_preserves_trailing_digits():
    rx = compile_pattern("SOURce1:FREQuency:CW")
    assert rx.match("SOUR1:FREQ:CW") is not None
    assert rx.match("SOURCE1:FREQUENCY:CW") is not None
    assert rx.match("SOUR2:FREQ:CW") is None


# ============================================================ Слой 2: Proxy / Drivers
def test_proxy_unsubscribe_and_broken_callback(qtbot):
    proxy = InstrumentProxy(RSSMF100A, SimTransport(RSSMF100ASim()))
    seen = []

    def bad(_v):
        raise RuntimeError("boom")

    def good(v):
        seen.append(v)

    try:
        proxy.subscribe("rf.freq", bad)
        proxy.subscribe("rf.freq", good)
        proxy._on_value("rf.freq", 1e9)
        assert seen == [1e9]
        proxy.unsubscribe("rf.freq", good)
        proxy._on_value("rf.freq", 2e9)
        assert seen == [1e9]
    finally:
        proxy.shutdown()


def test_u2021xa_filters_scpi_sentinel(make):
    drv, sim, _rec = make(KeysightU2021XA, KeysightU2021XASim)
    sim.power_dbm = 9.91e37
    assert drv._fetch() is None


def test_rf_generator_enum_maps_read_long_write_short(qapp):
    smf = RSSMF100A(SimTransport(RSSMF100ASim()))
    psg = KeysightE8267D(SimTransport(KeysightE8267DSim()))
    assert smf.params["ref.source"].to_scpi("EXT") == "EXT"
    assert smf.params["ref.source"].from_scpi("EXTERNAL") == "EXT"
    assert smf.params["ref.source"].from_scpi("INT1") == "INT"
    assert psg.params["pulse.source"].to_scpi("EXT") == "EXT"
    assert psg.params["pulse.source"].from_scpi("EXTERNAL") == "EXT"


# ============================================================ Слой 3: Coordinator
def test_calibration_bom_duplicates_and_nan(tmp_path: Path):
    p = tmp_path / "cal.csv"
    p.write_text("\ufeff1e9,1.0\n1e9,2.0\n2e9,4.0\n", encoding="utf-8")
    pts = load_csv(p)
    assert pts == [(1e9, 1.5), (2e9, 4.0)]
    assert interpolate(pts, math.nan) == 0.0
    assert interpolate([(1e9, 1.0), (1e9, 3.0)], 1e9) == pytest.approx(2.0)
    gain, table = fit_gain_table([(1e9, 1.0), (1e9, 3.0), (2e9, 5.0)])
    assert len(table) == 2 and math.isfinite(gain)

    bad = tmp_path / "nan.csv"
    bad.write_text("1e9,nan\n", encoding="utf-8")
    assert load_csv(bad) == []


def test_scenario_steps_validation_and_action_comm_error(qtbot):
    with pytest.raises(ScenarioError):
        SleepStep(math.nan)
    with pytest.raises(ScenarioError):
        AllStep([object()])  # type: ignore[list-item]

    proxy = InstrumentProxy(RSSMF100A, SimTransport(RSSMF100ASim()))
    inst = attach(proxy)
    try:
        proxy.open()
        qtbot.waitUntil(lambda: proxy.is_connected, timeout=3000)
        dev = Device(inst, "gen")
        step = ActionStep(dev, "reset")
        failed = []
        step.start(lambda _v: None, failed.append)
        proxy.comm_error.emit("link lost")
        assert failed and "link lost" in failed[0]
    finally:
        l3(inst).shutdown()


def test_follow_clears_link_when_source_removed(qtbot):
    pm = attach(InstrumentProxy(KeysightU2021XA, SimTransport(KeysightU2021XASim())))
    gen = attach(InstrumentProxy(RSSMF100A, SimTransport(RSSMF100ASim())))
    follow = FrequencyFollow(COORD)
    events = []
    follow.changed.connect(events.append)
    try:
        follow._sources[pm] = gen
        follow._last = gen
        assert follow.source(pm) is gen
        LINKS.remove(gen)
        assert follow.source(pm) is None
        assert pm in events
    finally:
        follow.stop()
        l3(pm).shutdown()
        l3(gen).shutdown()


def test_activity_empty_trace_and_cache_purge(qtbot):
    has_peak, *_ = spectrum_peak(Trace((), ()), 10.0)
    assert has_peak is False
    pm = attach(InstrumentProxy(KeysightU2021XA, SimTransport(KeysightU2021XASim())))
    mon = ActivityMonitor(LINKS)
    try:
        mon._mag_cache[(pm, "trace.data")] = (object(), (1.0,))
        LINKS.remove(pm)
        assert not any(k[0] is pm for k in mon._mag_cache)
    finally:
        l3(pm).shutdown()


# ============================================================ Слой 4: Frontend UI
def test_spinbox_digit_step_negative_sign(qtbot):
    proxy = attach(InstrumentProxy(RSSMF100A, SimTransport(RSSMF100ASim())))
    try:
        sp = PSpinBox(proxy, "rf.power")
        qtbot.addWidget(sp)
        sp.setValue(-10.5)
        sp.hasFocus = lambda: True  # type: ignore[method-assign]
        le = sp.lineEdit()
        le.setCursorPosition(0)
        assert sp._digit_step() == pytest.approx(10.0)
        le.setCursorPosition(1)
        assert sp._digit_step() == pytest.approx(10.0)
    finally:
        l3(proxy).shutdown()


def test_power_readout_handles_nan_and_sentinel(qtbot):
    ro = PowerReadout()
    qtbot.addWidget(ro)
    ro.update_values(power=math.nan)
    assert ro.main.text() == "---"
    ro.update_values(power=9.91e37)
    assert ro.main.text() == "---"


def test_power_meter_close_reopen_keeps_links(qtbot, tmp_path: Path):
    pm = attach(InstrumentProxy(KeysightU2021XA, SimTransport(KeysightU2021XASim())))
    gen = attach(InstrumentProxy(RSSMF100A, SimTransport(RSSMF100ASim())))
    settings = QSettings(str(tmp_path / "pm.ini"), QSettings.Format.IniFormat)
    win = PowerMeterWindow(pm, settings, "PM")
    qtbot.addWidget(win)
    try:
        win.show()
        win.close()
        win.show()
        COORD.follow._sources[pm] = gen
        COORD.follow.changed.emit(pm)
        assert win.follow_button.text().startswith("← ")
        COORD.follow._sources.pop(pm, None)
        COORD.follow.changed.emit(pm)
        assert win.follow_button.text() == "FOLW"
    finally:
        win.cleanup()
        l3(pm).shutdown()
        l3(gen).shutdown()


def test_rules_non_finite_fails_safe():
    rule = parse_rule("IF CH2.I < 5m THEN CH1 OFF", channels=(1, 2))
    assert rule.holds(lambda _ch, _q: math.nan) is True
    assert rule.holds(lambda _ch, _q: math.inf) is True
    assert rule.holds(lambda _ch, _q: 0.1) is False


def test_live_plot_non_finite_guards():
    lo, hi, step = nice_range(math.nan, math.nan, 6)
    assert math.isfinite(lo) and math.isfinite(hi) and math.isfinite(step) and lo < hi and step > 0
    tr = Trace((1.0, 2.0, 3.0), (math.nan, 0.0, math.inf))
    poly = decimate_trace_polygon(tr, 1.0, 3.0, -10.0, 10.0, QRectF(0.0, 0.0, 100.0, 100.0))
    assert len(poly) == 1
