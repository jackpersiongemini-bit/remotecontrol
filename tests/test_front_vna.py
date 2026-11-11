"""Окно ВАЦ на симуляторе N5231A: избранное, режим пресетов мощности, CW, выбор измерений, состояния."""
import pytest
from PyQt6.QtCore import QSettings

from visalab.core import InstrumentProxy, SimTransport
from visalab.drivers import DRIVERS
from visalab.front import VNAWindow
from visalab.front.vna import parse_power_list


def _open(qtbot, tmp_path, sim=None):
    drv, sim_cls = DRIVERS["pna"]
    sim = sim or sim_cls()
    proxy = InstrumentProxy(drv, SimTransport(sim))
    settings = QSettings(str(tmp_path / "vna.ini"), QSettings.Format.IniFormat)
    win = VNAWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    return proxy, win, settings, sim


@pytest.fixture
def vna(qtbot, tmp_path):
    from visalab.sim import KeysightPNASim
    sim = KeysightPNASim()
    sim.meas["USER_S21"] = {"s": "S21", "fmt": "MLOG", "ch": 1}
    sim.meas["CH2_S12"] = {"s": "S12", "fmt": "MLOG", "ch": 2}
    proxy, win, settings, sim = _open(qtbot, tmp_path, sim)
    yield proxy, win, settings, sim
    proxy.shutdown()


def _wait(qtbot, cond):
    qtbot.waitUntil(cond, timeout=5000)


def test_layout(qtbot, vna):
    proxy, win, _s, _sim = vna
    assert [ln.name for ln in win.preset_panel.table.lines] == ["MEM", "MATH", "POW", "CW", "CORR"]
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["STIM", "PWR", "MEAS", "SAVE"]
    keys = {k for p in win.pages for ln in p.lines for w in ln.widgets for k in [getattr(w, "key", None)] if k}
    assert not any(k == "trace" or k.endswith((".trace", ".sdata")) for k in keys)      # вывода трасс нет


def test_power_preset_mode(qtbot, vna):
    proxy, win, settings, sim = vna
    spin = win.power_spin
    spin.set_value(-7.0)
    _wait(qtbot, lambda: proxy.value("source.power") == pytest.approx(-7.0))
    win.levels_edit.setText("0; -10, 5 -20 x")
    win._levels_edited()                                       # мусор - список не меняется
    assert spin.levels == [-20.0, -10.0, -5.0, 0.0]
    win.levels_edit.setText("0; -10, 5 -20")
    win._levels_edited()
    assert spin.levels == [-20.0, -10.0, 0.0, 5.0] and win.levels_edit.text() == "-20, -10, 0, 5"
    win.prst_button.setChecked(True)
    assert settings.value(f"{win._prefix}/POWER_PRESET_MODE", type=bool) is True
    spin.stepBy(1)                                             # -7 -> ближайший уровень выше: 0
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(0.0))
    spin.stepBy(1)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(5.0))
    spin.stepBy(1)                                             # выше списка не уходит
    spin.stepBy(-2)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-10.0))
    win.prst_button.setChecked(False)
    spin.stepBy(1)                                             # обычный режим: шаг 1 dB
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-9.0))
    assert parse_power_list("") == []


def test_cw_generator_and_correction(qtbot, vna):
    proxy, win, _s, sim = vna
    win.cw_spin.set_value(2.5e9)
    _wait(qtbot, lambda: sim.c["cw"] == pytest.approx(2.5e9))
    win.cw_button.click()
    _wait(qtbot, lambda: proxy.value("sweep.type") == "CW" and sim.c["type"] == "CW")
    assert win.cw_button.indicated()
    win.widgets["rf.output"].set_value(True)
    win.widgets["cal.state"].set_value(True)
    _wait(qtbot, lambda: sim.c["corr"] is True and sim.c["outp"] is True)
    win.widgets["cal.state"].next_value()                      # CORR OFF
    _wait(qtbot, lambda: sim.c["corr"] is False)
    win.cw_button.click()                                      # обратно к прежней развёртке
    _wait(qtbot, lambda: sim.c["type"] == "LIN")


def test_choose_measurements_and_mem_math(qtbot, vna):
    proxy, win, _s, sim = vna
    ml = win.meas_list
    _wait(qtbot, lambda: len(ml.buttons) == 3)
    assert [b.text() for b in ml.buttons] == ["C1 S11", "C1 S21", "C2 S12"]
    assert ml.is_read(1, "CH1_S11_1") and not ml.is_read(2, "CH2_S12")
    ml.toggle(2, "CH2_S12")                                    # канал 2: свободный слот
    _wait(qtbot, lambda: ml.is_read(2, "CH2_S12"))
    n = ml.slot_of(2, "CH2_S12")
    _wait(qtbot, lambda: proxy.value(f"m{n}.trace") is not None)
    ml.toggle(1, "CH1_S11_1")                                  # m1 больше не читается
    _wait(qtbot, lambda: proxy.value("m1.enabled") is False)
    assert win.math_button.slots() == [n]
    proxy.action("mem_store")
    _wait(qtbot, lambda: sim.meas["CH2_S12"].get("mem") is not None)
    assert sim.meas["CH1_S11_1"].get("mem") is None            # только читаемые
    win.math_button.set_value("DIV")
    _wait(qtbot, lambda: win.math_button.text() == "D/M" and win.math_button.indicated())
    assert sim.meas["CH2_S12"]["math"] == "DIV"


def test_states_tab(qtbot, vna):
    proxy, win, settings, sim = vna
    win.widgets["freq.start"].set_value(1e9)
    win.widgets["freq.stop"].set_value(3e9)
    _wait(qtbot, lambda: proxy.value("freq.stop") == pytest.approx(3e9))
    win._add_preset()
    _wait(qtbot, lambda: win.presets.names() == ["1GHz_3GHz"])
    assert "1GHz_3GHz.csa" in sim.files
    win.widgets["freq.start"].set_value(2e9)
    _wait(qtbot, lambda: proxy.value("freq.start") == pytest.approx(2e9))
    win._apply_preset(0)
    _wait(qtbot, lambda: proxy.value("freq.start") == pytest.approx(1e9))
    win.presets.set_checked(0)
    win.presets.remove_selected()
    _wait(qtbot, lambda: win.presets.names() == [] and not sim.files)
