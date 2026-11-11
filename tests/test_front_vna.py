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
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["TRAC", "STIM", "PWR", "SAVE"]
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


def _chips(te):
    return [b.text() for b in te.chips.buttons()]


def test_trace_chips_read_and_mem_math(qtbot, vna):
    proxy, win, _s, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    assert _chips(te) == ["C1 S11\nMLOG", "C1 S21\nMLOG", "C2 S12\nMLOG"]
    assert te.current == (1, "CH1_S11_1") and te.chips.checked_index() == 0       # выбран первый
    assert not te.chips.buttons()[0].indicated() and not te.chips.buttons()[2].indicated()   # выбран / не читается
    te.chips.buttons()[2].click()                              # выбрать трейс канала 2
    assert te.current == (2, "CH2_S12") and te.channel.text() == "C2" and not te.read.indicated()
    te.read.click()                                            # READ: свободный слот
    _wait(qtbot, lambda: te.is_read(2, "CH2_S12") and te.read.indicated())
    n = te.slot_of(2, "CH2_S12")
    _wait(qtbot, lambda: proxy.value(f"m{n}.trace") is not None)
    te.chips.buttons()[0].click()
    te.read.click()                                            # m1 больше не читается
    _wait(qtbot, lambda: proxy.value("m1.enabled") is False)
    assert win.math_button.slots() == [n]
    proxy.action("mem_store")
    _wait(qtbot, lambda: sim.meas["CH2_S12"].get("mem") is not None)
    assert sim.meas["CH1_S11_1"].get("mem") is None            # только читаемые
    win.math_button.set_value("DIV")
    te.chips.buttons()[2].click()
    _wait(qtbot, lambda: win.math_button.text() == "D/M" and te.math.text() == "D/M")
    assert sim.meas["CH2_S12"]["math"] == "DIV"


def test_trace_edit_create_move_delete(qtbot, vna):
    proxy, win, _s, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    te.chips.buttons()[1].click()
    te.format.on_pick("PHASE")                                 # трейс, который не читается
    _wait(qtbot, lambda: sim.meas["USER_S21"]["fmt"] == "PHAS" and te.format.text() == "PHAS")
    te.sparam.on_pick("S12")
    _wait(qtbot, lambda: te.sparam.text() == "S12" and _chips(te)[1] == "C1 S12\nPHAS")
    assert [n for n, _v in te.channel.choices] == ["C1", "C2", "C3 NEW"]
    te.channel.on_pick(3)                                      # перенос в новый канал 3 - выбран перенесённый
    _wait(qtbot, lambda: te.current == (3, "CH3_S12_1") and "USER_S21" not in sim.meas)
    assert sim.meas["CH3_S12_1"]["fmt"] == "PHAS" and te.channel.text() == "C3"
    te.create()                                                # + : S21 на канале выбранного (3) - выбран новый
    _wait(qtbot, lambda: te.current == (3, "CH3_S21_1") and len(te.chips.buttons()) == 4)
    assert te.chips.checked_index() == 3
    te.chips.buttons()[1].click()
    assert te.delete()                                         # − : удалить CH2_S12
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3 and "CH2_S12" not in sim.meas)
    assert te.current == (1, "CH1_S11_1")


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


def test_trace_chips_fill_width_and_selected_has_no_indicator(qtbot, vna):
    proxy, win, _s, _sim = vna
    win.show()
    win.set_expanded(True)
    win.set_page(0)
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    _wait(qtbot, lambda: sum(b.width() for b in te.chips.buttons()) + 8 >= te.scroll.viewport().width())
    assert all(b.height() == win.add_button.height() * 2 + 4 or b.height() == 56 for b in te.chips.buttons())
    first = te.chips.buttons()[0]
    assert first.isChecked() and te.is_read(1, "CH1_S11_1") and not first.indicated()   # выбран - без индикатора
    te.chips.buttons()[1].click()
    assert first.indicated()                                   # не выбран и читается - индикатор


def test_plus_minus_at_right_edge(qtbot, vna):
    proxy, win, _s, _sim = vna
    win.show()
    win.set_expanded(True)
    for page in (0, 3):                                        # TRAC и SAVE
        win.set_page(page)
        qtbot.wait(50)
        tools = win._trc_tools if page == 0 else win.add_button.parentWidget()
        line = tools.parentWidget()
        assert line.width() - tools.geometry().right() <= 2
        assert tools.width() <= 40


def test_stim_channel_chips(qtbot, vna):
    proxy, win, _s, sim = vna
    sc = win.stim_channels
    _wait(qtbot, lambda: sc.channels == [1, 2])
    assert [b.text() for b in sc.buttons()] == ["C1", "C2"] and sc.checked_index() == 0
    assert all(b.sizeHint().height() == 56 for b in sc.buttons())
    sc.buttons()[1].click()                                    # STIM -> канал 2
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2 and proxy.value("sweep.points") == 101)
    win.widgets["sweep.points"].set_value(21)
    _wait(qtbot, lambda: sim.stim(2)["poin"] == 21)
    assert sim.c["poin"] == 201 and sc.checked_index() == 1
    win.trace_editor.chips.buttons()[2].click()                # перенос трейса канала 2 в новый канал 3
    win.trace_editor.channel.on_pick(3)
    _wait(qtbot, lambda: sc.channels == [1, 2, 3] or sc.channels == [1, 3])
