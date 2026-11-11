"""Окно ВАЦ на симуляторе N5231A: избранное, режим пресетов мощности, CW, выбор измерений, состояния."""
import pytest

from visalab.front.power_meter import ActionButton
from PyQt6.QtCore import QSettings, QSize

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
    assert [ln.name for ln in win.preset_panel.table.lines] == ["TRC", "MEM", "POW", "CW", "CORR"]
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["TRAC", "SCAL", "MARK", "STIM", "TRIG", "PWR", "CAL", "SAVE"]
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
    sim.c["cset"] = "CAL_FULL"                                 # у канала есть калибровка: CORR можно включить
    win.cw_spin.set_value(2.5e9)
    win.widgets["rf.output"].set_value(False)
    _wait(qtbot, lambda: sim.c["cw"] == pytest.approx(2.5e9) and sim.c["outp"] is False)
    win.cw_button.click()
    _wait(qtbot, lambda: proxy.value("gen.state") is True and sim.c["type"] == "CW")
    assert win.cw_button.indicated() and proxy.value("sweep.type") == "CW"
    assert [b.isEnabled() for b in win.trace_editor.chips.buttons()] == [True, True, False]   # канал 2 стоит
    win.widgets["rf.output"].set_value(True)
    win.widgets["cal.state"].set_value(True)
    _wait(qtbot, lambda: sim.c["corr"] is True and sim.c["outp"] is True)
    win.widgets["cal.state"].next_value()                      # CORR OFF
    _wait(qtbot, lambda: sim.c["corr"] is False)
    win.cw_button.click()                                      # всё как было до генератора
    _wait(qtbot, lambda: sim.c["type"] == "LIN" and proxy.value("gen.state") is False)
    assert sim.c["outp"] is False and sim.c["cw"] == pytest.approx(2.5e9)
    assert all(b.isEnabled() for b in win.trace_editor.chips.buttons())


def _chips(te):
    return [b.text() for b in te.chips.buttons()]


def test_trace_chips_read_and_mem_math(qtbot, vna):
    proxy, win, _s, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    assert _chips(te) == ["MLOG\n1·S11", "MLOG\n1·S21", "MLOG\n2·S12"]
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
    _wait(qtbot, lambda: te.sparam.text() == "S12" and _chips(te)[1] == "PHAS\n1·S12")
    assert [n for n, _v in te.channel.choices] == ["C1", "C2", "C3 NEW"]
    te.channel.on_pick(3)                                      # перенос в новый канал 3 - выбран перенесённый
    _wait(qtbot, lambda: te.current == (3, "CH3_S12_1") and "USER_S21" not in sim.meas)
    assert sim.meas["CH3_S12_1"]["fmt"] == "PHAS" and te.channel.text() == "C3"
    te.create()                                                # + : S21 на канале выбранного (3) - выбран новый
    _wait(qtbot, lambda: te.current == (3, "CH3_S21_1") and len(te.chips.buttons()) == 4)
    assert te.chips.checked_index() == 3
    te.chips.buttons()[1].click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2)
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
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    _wait(qtbot, lambda: sum(b.width() for b in te.chips.buttons()) + 8 >= te.scroll.viewport().width())
    assert all(b.height() == 44 for b in te.chips.buttons())
    first = te.chips.buttons()[0]
    assert first.isChecked() and te.is_read(1, "CH1_S11_1") and not first.indicated()   # выбран - без индикатора
    te.chips.buttons()[1].click()
    assert first.indicated()                                   # не выбран и читается - индикатор


def test_plus_minus_at_right_edge(qtbot, vna):
    proxy, win, _s, _sim = vna
    win.show()
    win.set_expanded(True)
    for page, tools, width in ((0, win._trc_tools, 40), (6, win._cal_tools, 40),
                                (7, win.add_button.parentWidget(), 40)):   # TRC, CAL и SAVE
        win.set_page(page)
        qtbot.wait(50)
        line = tools.parentWidget()
        assert line.width() - tools.geometry().right() <= 2
        assert tools.width() <= width


def test_trace_selects_channel(qtbot, vna):
    proxy, win, _s, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    assert proxy.value("stim.channel") == 1 and te.chips.checked_index() == 0
    assert all(b.sizeHint().height() == 44 for b in te.chips.buttons())
    te.chips.buttons()[2].click()                              # трейс канала 2 -> канал окна 2
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2 and proxy.value("sweep.points") == 101)
    win.widgets["sweep.points"].set_value(21)
    _wait(qtbot, lambda: sim.stim(2)["poin"] == 21)
    assert sim.c["poin"] == 201 and te.chips.checked_index() == 2
    te.chips.buttons()[1].click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 1)
    te.chips.buttons()[2].click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2)
    te.channel.on_pick(3)                                      # перенос в новый канал 3 - канал окна следом
    _wait(qtbot, lambda: proxy.value("stim.channel") == 3 and te.current[0] == 3)
    sim.meas["CH1_S21_2"] = {"s": "S21", "fmt": "PHAS", "ch": 1}
    proxy.action("meas_table")                                 # формат - в подписи каждого чипа
    _wait(qtbot, lambda: "PHAS\n1·S21" in _chips(te) and "MLOG\n1·S21" in _chips(te))


def test_trig_tab(qtbot, vna):
    proxy, win, _s, sim = vna
    page = win.pages[[win.tabs.tabText(i) for i in range(win.tabs.count())].index("TRIG")]
    assert [ln.name for ln in page.lines] == ["SRC", "SCOP", "MODE", "SLOP", "TYPE", "DLY", "RUN"]
    assert "MODE" not in [ln.name for ln in win.pages[3].lines]               # CONT/SINGLE ушло из STIM
    win.widgets["trig.source"].set_value("MAN")
    win.widgets["trig.mode"].set_value("POINT")
    _wait(qtbot, lambda: sim.trig["sour"] == "MAN" and sim.c["tmode"] == "POIN")
    done = sim.sweeps_done
    trig = next(w for ln in page.lines for w in ln.widgets if getattr(w, "name", None) == "trigger")
    for _ in range(sim.c["poin"]):
        trig.click()
    _wait(qtbot, lambda: sim.sweeps_done > done)


def test_data_save_csv(qtbot, vna, tmp_path):
    proxy, win, settings, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    assert [ln.name for ln in win.pages[7].lines] == ["VNA", "STAT", "PC", "NAME", "DATA", "S2P"]
    te.chips.buttons()[2].click()                              # трейс канала 2 (другая ось) - тоже читать
    te.read.click()
    te.format.on_pick("POLAR")                                 # комплексный формат - два столбца
    _wait(qtbot, lambda: len(win.read_traces()) == 2 and win.read_traces()[1][1].is_complex)
    out = tmp_path / "data"
    win.dir_edit.setText(str(out))
    win.dir_edit.editingFinished.emit()
    win.name_edit.setText("DUT 1")
    win.name_edit.editingFinished.emit()
    assert settings.value(f"{win._prefix}/DATA_DIR") == str(out)
    path = win.save_data()
    assert path is not None and path.parent == out and path.name.startswith("DUT_1_") and path.suffix == ".csv"
    assert win.data_button.text() == "SAVED"
    lines = path.read_text(encoding="utf-8").splitlines()
    head = next(ln for ln in lines if not ln.startswith("#"))
    assert head == '"Freq 1, Hz","MLOG 1·S11, dB","Freq 2, Hz",POL 2·S12 Re,POL 2·S12 Im'
    rows = [ln.split(",") for ln in lines[lines.index(head) + 1:]]
    assert len(rows) == 201 and float(rows[0][0]) == pytest.approx(10e6) and float(rows[0][2]) == pytest.approx(1e9)
    assert rows[100][2] != "" and rows[101][2:] == ["", "", ""]                # канал 2 короче: 101 точка
    assert sum(ln.startswith("#") for ln in lines) == 3
    assert win.save_data() != path                              # повтор: новое имя, старый файл не затёрт
    for b in te.chips.buttons()[::2]:                          # ничего не читается - файла нет
        b.click()
        if te.read.indicated():
            te.read.click()
    _wait(qtbot, lambda: not win.read_traces())
    assert win.save_data() is None and win.data_button.text() == "NO DATA"


def test_states_folder_on_analyzer(qtbot, vna):
    from visalab.sim.keysight_n5231a_sim import DEFAULT_DIR
    proxy, win, _s, sim = vna
    import copy
    sim.dirs[DEFAULT_DIR + "\\Lab"]["old.csa"] = copy.deepcopy((sim.c, sim.meas, sim.other, sim.marker))
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR and win.presets.names() == [])
    win.widgets["state.dir"].set_value(DEFAULT_DIR + "\\Lab")              # другая папка - другой список
    _wait(qtbot, lambda: sim.cdir.endswith("Lab") and win.presets.names() == ["old"])
    win._add_preset()                                          # сохраняется в выбранную папку
    _wait(qtbot, lambda: len(win.presets.names()) == 2)
    assert len(sim.dirs[DEFAULT_DIR + "\\Lab"]) == 2 and not sim.dirs[DEFAULT_DIR]
    win.widgets["state.dir"].set_value("C:\\nope")                         # нет такой папки - остаётся прежняя
    _wait(qtbot, lambda: proxy.value("state.dir", None) is not None and sim.cdir.endswith("Lab"))
    assert win.presets.names() and len(win.presets.names()) == 2


def test_states_folder_picked_from_menu(qtbot, vna):
    from visalab.sim.keysight_n5231a_sim import DEFAULT_DIR
    from visalab.front.vna import VNAWindow
    proxy, win, _s, sim = vna
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR)
    win.vdir_button.click()                                    # меню подпапок текущей папки
    _wait(qtbot, lambda: getattr(win, "_dirs_popup", None) is not None)
    items = [a.text() for a in win._dirs_popup.actions() if not a.isSeparator()]
    assert items == [DEFAULT_DIR, "..", "Lab"]
    next(a for a in win._dirs_popup.actions() if a.text() == "Lab").trigger()
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR + "\\Lab" and sim.cdir.endswith("Lab"))
    win._dirs_popup.close()
    win._dirs_popup = None
    win.vdir_button.click()
    _wait(qtbot, lambda: getattr(win, "_dirs_popup", None) is not None)
    next(a for a in win._dirs_popup.actions() if a.text() == "..").trigger()        # наверх
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR)
    assert VNAWindow.dir_choices("C:\\Users", ["A"]) == [("..", "C:\\"), ("A", "C:\\Users\\A")]
    assert VNAWindow.dir_choices("C:\\", []) == []


def test_scal_tab_follows_selected_trace(qtbot, vna):
    proxy, win, _s, sim = vna
    sim.windows[1][2] = "USER_S21"                             # показан на экране - у него есть масштаб
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    assert [ln.name for ln in win.pages[1].lines] == ["AUTO", "DIV", "REF", "RPOS", "EDEL", "SMO"]
    _wait(qtbot, lambda: proxy.value("trace.sel") == "1:CH1_S11_1" and proxy.value("scale.div") == 10.0)
    te.chips.buttons()[1].click()                              # другой трейс - его масштаб
    _wait(qtbot, lambda: proxy.value("trace.sel") == "1:USER_S21")
    win.widgets["scale.div"].set_value(2.0)
    win.widgets["smooth.state"].set_value(True)
    _wait(qtbot, lambda: sim.meas["USER_S21"].get("pdiv") == 2.0 and sim.meas["USER_S21"].get("smo") is True)
    assert "pdiv" not in sim.meas["CH1_S11_1"]
    te.chips.buttons()[0].click()
    _wait(qtbot, lambda: proxy.value("trace.sel") == "1:CH1_S11_1" and proxy.value("scale.div") == 10.0
          and proxy.value("smooth.state") is False)
    proxy.action("autoscale")
    _wait(qtbot, lambda: sim.meas["CH1_S11_1"].get("pdiv") is not None and proxy.value("scale.div") != 10.0)
    te.chips.buttons()[2].click()                              # не показан ни в одном окне - масштаба нет
    _wait(qtbot, lambda: proxy.value("trace.sel") == "2:CH2_S12" and proxy.value("scale.div") is None)
    assert proxy.is_connected


def test_cal_tab_cal_sets(qtbot, vna):
    proxy, win, _s, sim = vna
    cs, te = win.cal_sets, win.trace_editor
    _wait(qtbot, lambda: [b.text() for b in cs.chips.buttons()] == ["NONE", "CAL_1-3G", "CAL_FULL"])
    assert [ln.name for ln in win.pages[6].lines] == ["CSET", "STIM", "CORR", "INT", "NEW"]
    assert cs.chips.checked_index() == 0                       # у канала 1 калибровки нет
    cs.chips.buttons()[1].click()                              # cal set со стимулом
    _wait(qtbot, lambda: sim.c["cset"] == "CAL_1-3G" and proxy.value("cal.set") == "CAL_1-3G")
    assert sim.c["corr"] and sim.c["start"] == 1e9 and cs.chips.checked_index() == 1
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    te.chips.buttons()[2].click()                              # трейс канала 2: его калибровка
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2 and cs.chips.checked_index() == 0)
    win.widgets["cal.stim"].set_value(False)                   # KEEP: стимул канала остаётся
    _wait(qtbot, lambda: proxy.value("cal.stim") is False)
    cs.chips.buttons()[2].click()
    _wait(qtbot, lambda: sim.stim(2)["cset"] == "CAL_FULL" and cs.chips.checked_index() == 2)
    assert sim.stim(2)["start"] == 1e9 and sim.stim(2)["poin"] == 101
    cs.chips.buttons()[0].click()                              # NONE - снять
    _wait(qtbot, lambda: sim.stim(2)["cset"] == "" and proxy.value("cal.set") == "")
    te.chips.buttons()[0].click()                              # канал 1: удалить его cal set (два клика)
    _wait(qtbot, lambda: proxy.value("cal.set") == "CAL_1-3G")
    cs.delete_button.click()
    assert cs.delete_button.text() == "?" and "CAL_1-3G" in sim.csets
    cs.delete_button.click()
    _wait(qtbot, lambda: [b.text() for b in cs.chips.buttons()] == ["NONE", "CAL_FULL"])
    assert "CAL_1-3G" not in sim.csets and cs.chips.checked_index() == 0 and cs.delete_button.text() == "−"


def test_cal_tab_guided_calibration(qtbot, vna):
    proxy, win, settings, sim = vna
    wz, cw = win.cal_wizard, win.cal_window
    win.show()
    win.set_expanded(True)
    win.set_page(6)
    assert not cw.isVisible()
    win.cal_button.click()                                     # отдельное окно калибровки
    assert cw.isVisible() and cw.windowTitle() == "CAL CH1"
    assert [ln.name for ln in cw.table.lines] == ["TYPE", "P1", "P2", "RUN", "STEP", "NAME"]
    if wz.kind != "FULL":
        wz.next_type()
    _wait(qtbot, lambda: wz.kit(1) == "Ideal 3.5mm" and wz.kit(2) == "Ideal 3.5mm")   # идеальный - по умолчанию
    assert wz.conn_buttons[1].text() == "3.5 F" and not wz.run_lines[0].isVisible()
    wz.set_conn(2, "Type N (50) male")
    _wait(qtbot, lambda: wz.kit(2) == "Ideal Type N")
    assert settings.value(f"{win._prefix}/CAL_P2_CONN") == "Type N (50) male"
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 7)
    qtbot.wait(50)
    h = cw.height()
    assert win.cal_button.indicated() and wz.run_lines[0].isVisible() and not wz.save_button.isEnabled() and not wz.start_button.isEnabled()
    assert wz.step_buttons()[0].text() == "1. 3.5 F OPEN to P1"
    for k in range(7):
        _wait(qtbot, lambda: wz.step_buttons()[k].isEnabled())
        wz.step_buttons()[k].click()
        _wait(qtbot, lambda: wz.step_buttons()[k].indicated())
    _wait(qtbot, lambda: wz.save_button.isEnabled())
    wz.name_edit.setText("DUT_CAL")
    wz.save_button.click()
    _wait(qtbot, lambda: sim.c["cset"] == "DUT_CAL" and not wz.steps)
    _wait(qtbot, lambda: "DUT_CAL" in [b.text() for b in win.cal_sets.chips.buttons()]
          and proxy.value("cal.set") == "DUT_CAL")
    assert not wz.run_lines[0].isVisible() and wz.start_button.isEnabled()
    wz.set_conn(2, wz.OFF)                                     # однопортовая, прервать
    assert not wz.kit_buttons[2].isVisible()
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 3 and cw.height() < h)   # окно - по шагам
    cw.close()                                                 # закрыть посреди калибровки - прервать
    _wait(qtbot, lambda: not wz.steps and proxy.value("meas.armed") is True)
    assert not win.cal_button.indicated()


def test_save_s2p_of_selected_channel(qtbot, vna, tmp_path):
    proxy, win, _s, sim = vna
    te = win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    te.chips.buttons()[2].click()                              # канал 2
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2)
    win.dir_edit.setText(str(tmp_path))
    win.name_edit.setText("DUT")
    win.s2p_button.click()
    _wait(qtbot, lambda: win.s2p_button.text() == "SAVED")
    files = list(tmp_path.glob("DUT_CH2_*.s2p"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    data = [ln for ln in lines if ln and ln[0] not in "!#"]
    assert "# HZ S RI R 50" in lines and len(data) == sim.stim(2)["poin"] and len(data[0].split()) == 9
    assert float(data[0].split()[0]) == pytest.approx(sim.stim(2)["start"])


def test_cal_window_thru_response(qtbot, vna):
    proxy, win, _s, sim = vna
    wz, cw, te = win.cal_wizard, win.cal_window, win.trace_editor
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    win.show()
    win.open_cal_window()
    if wz.kind != "THRU":
        wz.type_button.click()
    assert wz.kind == "THRU" and not wz.port_lines[0].isVisible() and wz.trace_label.isVisible()
    assert te.current == (1, "CH1_S11_1") and not wz.start_button.isEnabled()   # S11 - не перемычка
    te.chips.buttons()[1].click()                              # 1·S21
    _wait(qtbot, lambda: proxy.value("trace.sel") == "1:USER_S21" and wz.start_button.isEnabled())
    assert wz.trace_label.text() == "1·S21"
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 1)
    assert wz.step_buttons()[0].text().startswith("1. THRU between P1 and P2")
    wz.step_buttons()[0].click()
    _wait(qtbot, lambda: wz.save_button.isEnabled())
    wz.name_edit.setText("THRU_CAL")
    wz.save_button.click()
    _wait(qtbot, lambda: sim.c["cset"] == "THRU_CAL" and not wz.steps)
    wz.type_button.click()                                     # обратно FULL
    assert wz.kind == "FULL" and wz.port_lines[0].isVisible()


def test_mark_tab_markers_of_selected_trace(qtbot, vna):
    proxy, win, _s, sim = vna
    te, mp = win.trace_editor, win.marker_panel
    assert [ln.name for ln in win.pages[2].lines] == ["MKR", "STAT", "SRCH", "ALL"]
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    te.chips.buttons()[1].click()                              # 1·S21
    _wait(qtbot, lambda: proxy.value("trace.sel") == "1:USER_S21")
    assert mp.chips.checked_index() == 0 and mp.value.text() == "OFF"
    next(b for b in win.findChildren(ActionButton) if b.name == "mark_peak").click()
    _wait(qtbot, lambda: 1 in mp.table and proxy.value("mark.state") is True)
    assert proxy.value("mark.x") == pytest.approx(2e9, rel=5e-2) and "dB" in mp.value.text()   # шаг 67 МГц
    assert mp.value.parentWidget() is win.widgets["mark.x"].parentWidget()           # STAT · X · Y одной строкой
    mp.chips.buttons()[2].click()                              # M3: поставить на 1.5 ГГц
    _wait(qtbot, lambda: proxy.value("mark.n") == 3 and proxy.value("mark.state") is False)
    assert mp.chips.buttons()[0].indicated() and not mp.chips.buttons()[2].indicated()
    win.widgets["mark.x"].set_value(1.5e9)
    _wait(qtbot, lambda: sorted(mp.table) == [1, 3])
    assert sim.meas["USER_S21"]["marks"][3] == {"on": True, "x": 1.5e9}
    win.show()
    win.set_expanded(True)
    win.set_page(2)
    qtbot.wait(50)
    h = win.height()
    for n in (2, *range(4, 11)):                               # 10 маркеров: окно не растёт
        proxy.set("mark.n", n)
        _wait(qtbot, lambda: proxy.value("mark.n") == n)
        proxy.set("mark.x", 1e9 + n * 1e8)
        _wait(qtbot, lambda: n in mp.table)
    _wait(qtbot, lambda: len(mp.table) == 10)
    qtbot.wait(50)
    assert win.height() == h and all(b.indicated() for b in mp.chips.buttons()[:9])
    next(b for b in win.findChildren(ActionButton) if b.name == "mark_aoff").click()
    _wait(qtbot, lambda: not mp.table and not any(b.indicated() for b in mp.chips.buttons()))
