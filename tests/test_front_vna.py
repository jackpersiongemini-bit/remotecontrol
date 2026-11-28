"""Окно ВАЦ на симуляторе N5231A (4 порта): каналы (CH), пользовательские трассы на ПК (TRC/TRAC),
настройки канала (CH), мощность P1..P4 и пресеты (PWR), CW-генератор, триггеры, калибровка, состояния и экспорт."""
import copy

import pytest
from PyQt6.QtCore import QSettings

from conftest import attach
from visalab.drivers import DRIVERS
from visalab.front import VNAWindow
from visalab.front.power_meter import ActionButton
from visalab.front.vna import parse_power_list
from visalab.instruments import InstrumentProxy
from visalab.sim import KeysightPNASim
from visalab.transport import SimTransport


def _open(qtbot, tmp_path, sim=None, ini_name="vna.ini"):
    drv, sim_cls = DRIVERS["pna"]
    sim = sim or sim_cls(ports=4)
    proxy = attach(InstrumentProxy(drv, SimTransport(sim)))
    settings = QSettings(str(tmp_path / ini_name), QSettings.Format.IniFormat)
    win = VNAWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    return proxy, win, settings, sim


@pytest.fixture
def vna(qtbot, tmp_path):
    sim = KeysightPNASim(ports=4)
    proxy, win, settings, sim = _open(qtbot, tmp_path, sim)
    yield proxy, win, settings, sim
    proxy.shutdown()


def _wait(qtbot, cond):
    qtbot.waitUntil(cond, timeout=5000)


def _chips(widget):
    return [b.text() for b in widget.chips.buttons()]


def _page_by_name(win, title):
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    return win.pages[names.index(title)]


# ============================================================ 1. Компоновка окна
def test_layout(qtbot, vna):
    proxy, win, _s, _sim = vna
    assert [ln.name for ln in win.preset_panel.table.lines] == ["POW", "CW"]
    assert win.settings_header_table is not None
    assert [ln.name for ln in win.settings_header_table.lines] == ["CH", "TRC"]
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == [
        "TRAC", "CH", "PWR", "TRIG", "CAL", "SAVE", "SET",
    ]
    keys = {k for p in win.pages for ln in p.lines for w in ln.widgets for k in [getattr(w, "key", None)] if k}
    assert not any(k == "trace" or k.endswith((".trace", ".sdata")) for k in keys)
    assert not any(k.startswith(("scale.", "mark.", "m1.")) for k in keys)
    assert [ln.name for ln in _page_by_name(win, "TRAC").lines] == ["CH", "SPAR", "FMT", "EDEL", "SMO", "SET"]
    assert [ln.name for ln in _page_by_name(win, "CH").lines] == [
        "STAR", "STOP", "CENT", "SPAN", "PTS", "IFBW", "TIME", "TYPE", "PSTR", "PSTP", "AVG",
    ]


# ============================================================ 2. Каналы: выбор, создание, настройки, удаление
def test_channel_bar_create_switch_settings_and_delete(qtbot, vna):
    proxy, win, _s, sim = vna
    cb = win.channel_bar
    _wait(qtbot, lambda: _chips(cb) == ["C1"] and cb.chips.checked_index() == 0)

    # Создаём канал 2 кнопкой «+»
    cb.add_button.click()
    _wait(qtbot, lambda: _chips(cb) == ["C1", "C2"] and proxy.value("stim.channel") == 2)
    assert cb.chips.checked_index() == 1

    # Настраиваем канал 2 отдельно от канала 1
    win.widgets["freq.start"].set_value(2e9)
    win.widgets["freq.stop"].set_value(4e9)
    win.widgets["sweep.points"].set_value(51)
    win.widgets["bw.if"].set_value(5000.0)
    win.widgets["avg.state"].set_value(True)
    win.widgets["avg.count"].set_value(4)
    win.widgets["sweep.type"].set_value("POWER")
    win.widgets["power.start"].set_value(-20.0)
    win.widgets["power.stop"].set_value(0.0)
    _wait(qtbot, lambda: (
        sim.stim(2)["start"] == pytest.approx(2e9)
        and sim.stim(2)["stop"] == pytest.approx(4e9)
        and sim.stim(2)["poin"] == 51
        and sim.stim(2)["aver"] is True
        and sim.stim(2)["coun"] == 4
        and sim.stim(2)["type"] == "POW"
        and sim.stim(2)["pow_start"] == pytest.approx(-20.0)
        and sim.stim(2)["pow_stop"] == pytest.approx(0.0)
    ))

    # Переключаемся на C1: настройки CH1 не затронуты
    cb.chips.buttons()[0].click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 1 and proxy.value("sweep.points") == 201)
    assert sim.c["start"] == pytest.approx(10e6) and sim.c["type"] == "LIN"

    # Удаляем C2 (два клика по «−»)
    cb.chips.buttons()[1].click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2)
    cb.delete_button.click()
    assert cb.delete_button.text() == "?" and 2 in proxy.channels_list()
    cb.delete_button.click()
    _wait(qtbot, lambda: _chips(cb) == ["C1"] and proxy.value("stim.channel") == 1)
    assert cb.delete_button.text() == "−"


# ============================================================ 3. Пользовательские трассы (TRC / TRAC)
def test_own_traces_create_edit_multichannel_and_persist(qtbot, tmp_path):
    sim = KeysightPNASim(ports=4)
    proxy, win, settings, sim = _open(qtbot, tmp_path, sim, ini_name="persist.ini")
    try:
        te = win.trace_editor
        _wait(qtbot, lambda: _chips(te) == ["MLOG\n1·S11"] and proxy.value("s.all_active") == "1:S11")
        assert te.read.indicated() and te.sparam.text() == "S11" and te.format.text() == "MLOG"

        # Добавляем вторую трассу, настраиваем S41 / PHASE / EDEL / SMO
        te.create()
        _wait(qtbot, lambda: len(te.chips.buttons()) == 2)
        assert te.chips.checked_index() == 1
        te.sparam.on_pick("S41")
        te.format.on_pick("PHASE")
        te.edel_edit.setText("2n")
        te.edel_edit.returnPressed.emit()
        te.smooth_btn.click()
        te.aper_edit.setText("5")
        te.aper_edit.returnPressed.emit()

        _wait(qtbot, lambda: _chips(te) == ["MLOG\n1·S11", "PHAS\n1·S41"]
              and proxy.value("s.all_active") == "1:S11,1:S41")
        spec = te.current_spec()
        assert spec is not None
        assert spec.sparam == "S41" and spec.fmt == "PHASE"
        assert spec.edel_s == pytest.approx(2e-9)
        assert spec.smooth is True and spec.smooth_aper == pytest.approx(5.0)
        assert len(proxy.vna_trace_specs()) == 2

        # Переносим вторую трассу в новый канал C2 через меню CH
        assert ("C2 NEW", 2) in te.channel.choices
        te.channel.on_pick(2)
        _wait(qtbot, lambda: (
            2 in proxy.channels_list()
            and te.current_spec().channel == 2
            and proxy.value("stim.channel") == 2
            and proxy.value("s.all_active") == "1:S11,2:S41"
        ))
        assert _chips(te) == ["MLOG\n1·S11", "PHAS\n2·S41"]
        _wait(qtbot, lambda: (1, "S11") in proxy.channel_traces() and (2, "S41") in proxy.channel_traces())

        # NONE / ALL / READ
        te.none_btn.click()
        _wait(qtbot, lambda: proxy.value("s.all_active") == "" and not te.read.indicated())
        te.all_btn.click()
        _wait(qtbot, lambda: proxy.value("s.all_active") == "1:S11,2:S41" and te.read.indicated())
        te.read.click()
        _wait(qtbot, lambda: proxy.value("s.all_active") == "1:S11" and not te.read.indicated())
        te.read.click()
        _wait(qtbot, lambda: proxy.value("s.all_active") == "1:S11,2:S41")
    finally:
        proxy.shutdown()

    # Повторное открытие окна с тем же ini восстанавливает список трасс и их PC-настройки
    sim2 = KeysightPNASim(ports=4)
    proxy2, win2, _s2, _sim2 = _open(qtbot, tmp_path, sim2, ini_name="persist.ini")
    try:
        te2 = win2.trace_editor
        assert _chips(te2) == ["MLOG\n1·S11", "PHAS\n2·S41"]
        restored = te2.traces[1]
        assert restored.channel == 2 and restored.sparam == "S41" and restored.fmt == "PHASE"
        assert restored.edel_s == pytest.approx(2e-9) and restored.smooth is True
        # Удаляем выбранную трассу
        te2.chips.buttons()[1].click()
        assert te2.delete()
        assert _chips(te2) == ["MLOG\n1·S11"]
    finally:
        proxy2.shutdown()


# ============================================================ 4. Мощность: пресеты и 4-портовая мощность
def test_power_preset_mode_and_4_port_power(qtbot, vna):
    proxy, win, settings, sim = vna
    spin, pl = win.power_spin, win.power_levels
    pwr_page = _page_by_name(win, "PWR")
    assert [ln.name for ln in pwr_page.lines] == [
        "POW", "RF", "COUP", "P1", "P2", "P3", "P4", "PRST", "", "TABL", "OFFS", "ACAL", "GAIN",
    ]
    assert [b.text() for b in pl.chips.buttons()] == ["-20", "-10", "-5", "0"]
    pl.chips.buttons()[2].click()
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-5.0) and pl.chips.checked_index() == 2)
    assert pl.remove_selected() and spin.levels == [-20.0, -10.0, 0.0]
    spin.set_value(5.0)
    _wait(qtbot, lambda: proxy.value("source.power") == pytest.approx(5.0))
    assert pl.add_current() and not pl.add_current()
    assert spin.levels == [-20.0, -10.0, 0.0, 5.0] and pl.chips.checked_index() == 3
    assert settings.value(f"{win._prefix}/POWER_LIST") == "-20, -10, 0, 5"
    spin.set_value(-7.0)
    _wait(qtbot, lambda: proxy.value("source.power") == pytest.approx(-7.0) and pl.chips.checked_index() == -1)
    win.prst_button.setChecked(True)
    assert settings.value(f"{win._prefix}/POWER_PRESET_MODE", type=bool) is True
    spin.stepBy(1)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(0.0))
    spin.stepBy(1)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(5.0))
    spin.stepBy(1)
    spin.stepBy(-2)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-10.0))
    win.prst_button.setChecked(False)
    spin.stepBy(1)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-9.0))
    assert parse_power_list("") == []

    # Раздельная мощность портов 1..4
    win.widgets["source.power_coupled"].set_value(False)
    _wait(qtbot, lambda: sim.c["pow_coup"] is False)
    win.widgets["source.power_p4"].set_value(-14.0)
    _wait(qtbot, lambda: sim.c["pow_levels"][4] == pytest.approx(-14.0))


def test_2_port_vna_hides_ports_3_and_4(qtbot, tmp_path):
    sim = KeysightPNASim(ports=2)
    proxy, win, _s, _sim = _open(qtbot, tmp_path, sim)
    try:
        _wait(qtbot, lambda: proxy.value("ports") == 2)
        assert win.port_button._data == (1, 2)
        assert not win._port_power_lines[3].isVisible()
        assert not win._port_power_lines[4].isVisible()
        assert len(win.trace_editor.sparam.choices) == 4
    finally:
        proxy.shutdown()


# ============================================================ 5. CW-генератор на 4 портах
def test_cw_generator_and_correction(qtbot, vna):
    proxy, win, _s, sim = vna
    sim.c["cset"] = "CAL_FULL"
    win.cw_spin.set_value(2.5e9)
    win.widgets["rf.output"].set_value(False)
    _wait(qtbot, lambda: sim.c["cw"] == pytest.approx(2.5e9) and sim.c["outp"] is False)
    win.cw_button.click()
    _wait(qtbot, lambda: proxy.value("gen.state") is True and sim.c["type"] == "CW")
    assert win.cw_button.indicated() and proxy.value("sweep.type") == "CW"
    win.widgets["rf.output"].set_value(True)
    win.widgets["cal.state"].set_value(True)
    _wait(qtbot, lambda: sim.c["corr"] is True and sim.c["outp"] is True)
    win.widgets["cal.state"].next_value()
    _wait(qtbot, lambda: sim.c["corr"] is False)
    win.cw_button.click()
    _wait(qtbot, lambda: sim.c["type"] == "LIN" and proxy.value("gen.state") is False)
    assert sim.c["outp"] is False and sim.c["cw"] == pytest.approx(2.5e9)


def test_cw_generator_4_port_button(qtbot, vna):
    proxy, win, _s, sim = vna
    assert win.port_button.text() == "P1" and win.port_button._data == (1, 2, 3, 4)
    win.power_spin.set_value(-8.0)
    _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-8.0))
    win.cw_button.click()
    _wait(qtbot, lambda: proxy.value("gen.state") is True and sim.c["pow_modes"][1] == "ON")
    win.port_button.set_value(2)
    _wait(qtbot, lambda: proxy.value("gen.port") == 2 and sim.c["pow_modes"][2] == "ON")
    win.port_button.set_value(3)
    _wait(qtbot, lambda: proxy.value("gen.port") == 3 and sim.c["pow_modes"][3] == "ON")
    win.port_button.set_value(4)
    _wait(qtbot, lambda: proxy.value("gen.port") == 4 and sim.c["pow_modes"][4] == "ON")
    assert win.port_button.text() == "P4" and sim.c["pow_modes"][1] == "OFF"
    win.power_spin.set_value(2.0)
    _wait(qtbot, lambda: sim.c["pow_levels"][4] == pytest.approx(2.0))
    assert sim.c["pow_levels"][1] == pytest.approx(-8.0)
    win.cw_button.click()
    _wait(qtbot, lambda: proxy.value("gen.state") is False and sim.c["pow_modes"][4] == "AUTO")


# ============================================================ 6. Внешний вид чипов и кнопок +/−
def test_trace_chips_fill_width_and_selected_has_no_indicator(qtbot, vna):
    proxy, win, _s, _sim = vna
    win.show()
    win.set_expanded(True)
    te = win.trace_editor
    te.create()
    te.create()
    _wait(qtbot, lambda: len(te.chips.buttons()) == 3)
    _wait(qtbot, lambda: sum(b.width() for b in te.chips.buttons()) + 8 >= te.scroll.viewport().width())
    assert all(b.height() == 44 for b in te.chips.buttons())
    te.chips.buttons()[0].click()
    first = te.chips.buttons()[0]
    assert first.isChecked() and te.traces[0].enabled and not first.indicated()
    te.chips.buttons()[1].click()
    assert first.indicated()


def test_plus_minus_at_right_edge(qtbot, vna):
    proxy, win, _s, _sim = vna
    win.show()
    win.set_expanded(True)
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    checks = (
        (0, win._ch_tools, 80),
        (0, win._trc_tools, 40),
        (names.index("PWR"), win._pwr_tools, 40),
        (names.index("CAL"), win._cal_tools, 40),
        (names.index("SAVE"), win.add_button.parentWidget(), 40),
    )
    for page, tools, width in checks:
        win.set_page(page)
        qtbot.wait(50)
        line = tools.parentWidget()
        assert line.width() - tools.geometry().right() <= 2
        assert tools.width() <= width


# ============================================================ 7. Триггеры
def test_trig_tab(qtbot, vna):
    proxy, win, _s, sim = vna
    page = _page_by_name(win, "TRIG")
    assert [ln.name for ln in page.lines] == ["SRC", "SCOP", "MODE", "SLOP", "TYPE", "DLY", "RUN"]
    assert "MODE" not in [ln.name for ln in _page_by_name(win, "CH").lines]
    win.widgets["trig.source"].set_value("MAN")
    win.widgets["trig.mode"].set_value("POINT")
    _wait(qtbot, lambda: sim.trig["sour"] == "MAN" and sim.c["tmode"] == "POIN")
    done = sim.sweeps_done
    trig = next(w for ln in page.lines for w in ln.widgets if getattr(w, "name", None) == "trigger")
    for _ in range(sim.c["poin"]):
        trig.click()
    _wait(qtbot, lambda: sim.sweeps_done > done)


# ============================================================ 8. Экспорт CSV и Touchstone .s4p
def test_data_save_csv_multi_channel(qtbot, vna, tmp_path):
    proxy, win, settings, sim = vna
    te = win.trace_editor
    assert [ln.name for ln in _page_by_name(win, "SAVE").lines] == ["VNA", "STAT", "PC", "NAME", "DATA", "SNP"]

    # Создаём 2-ю трассу на канале 2 (101 точка) в формате PHASE
    te.create()
    te.sparam.on_pick("S41")
    te.format.on_pick("PHASE")
    te.channel.on_pick(2)
    _wait(qtbot, lambda: len(win.read_traces()) == 2 and sim.stim(2)["poin"] == 101)

    out = tmp_path / "data"
    win.dir_edit.setText(str(out))
    win.dir_edit.returnPressed.emit()
    win.name_edit.setText("DUT 1")
    win.name_edit.returnPressed.emit()
    assert settings.value(f"{win._prefix}/DATA_DIR") == str(out)

    path = win.save_data()
    assert path is not None and path.parent == out and path.name.startswith("DUT_1_") and path.suffix == ".csv"
    assert win.data_button.text() == "SAVED"
    lines = path.read_text(encoding="utf-8").splitlines()
    head = next(ln for ln in lines if not ln.startswith("#"))
    assert head == '"Freq 1, Hz","1·S11 MLOG, dB","Freq 2, Hz","2·S41 PHAS, deg"'
    rows = [ln.split(",") for ln in lines[lines.index(head) + 1:]]
    assert len(rows) == 201 and float(rows[0][0]) == pytest.approx(10e6) and float(rows[0][2]) == pytest.approx(1e9)
    assert rows[100][2] != "" and rows[101][2:] == ["", ""]
    assert sum(ln.startswith("#") for ln in lines) == 3
    assert win.save_data() != path

    # Выключаем все трассы -> NO DATA
    te.disable_all()
    _wait(qtbot, lambda: not win.read_traces())
    assert win.save_data() is None and win.data_button.text() == "NO DATA"


def test_save_s4p_of_selected_channel(qtbot, vna, tmp_path):
    proxy, win, _s, sim = vna
    win.channel_bar.add_button.click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2)
    win.dir_edit.setText(str(tmp_path))
    win.name_edit.setText("DUT")
    win.s2p_button.click()
    _wait(qtbot, lambda: win.s2p_button.text() == "SAVED")
    files = list(tmp_path.glob("DUT_CH2_*.s4p"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    data = [ln for ln in lines if ln and ln[0] not in "!#"]
    assert "# HZ S RI R 50" in lines and len(data) == sim.stim(2)["poin"] * 4
    assert float(data[0].split()[0]) == pytest.approx(sim.stim(2)["start"])


# ============================================================ 9. Состояния на диске ВАЦ
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


def test_states_folder_on_analyzer(qtbot, vna):
    from visalab.sim.keysight_n5231a_sim import DEFAULT_DIR
    proxy, win, _s, sim = vna
    sim.dirs[DEFAULT_DIR + "\\Lab"]["old.csa"] = copy.deepcopy((sim.c, sim.meas, sim.other))
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR and win.presets.names() == [])
    win.widgets["state.dir"].set_value(DEFAULT_DIR + "\\Lab")
    _wait(qtbot, lambda: sim.cdir.endswith("Lab") and win.presets.names() == ["old"])
    win._add_preset()
    _wait(qtbot, lambda: len(win.presets.names()) == 2)
    assert len(sim.dirs[DEFAULT_DIR + "\\Lab"]) == 2 and not sim.dirs[DEFAULT_DIR]
    win.widgets["state.dir"].set_value("C:\\nope")
    _wait(qtbot, lambda: proxy.value("state.dir", None) is not None and sim.cdir.endswith("Lab"))
    assert win.presets.names() and len(win.presets.names()) == 2


def test_states_folder_picked_from_menu(qtbot, vna):
    from visalab.sim.keysight_n5231a_sim import DEFAULT_DIR
    proxy, win, _s, sim = vna
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR)
    win.vdir_button.click()
    _wait(qtbot, lambda: getattr(win, "_dirs_popup", None) is not None)
    items = [a.text() for a in win._dirs_popup.actions() if not a.isSeparator()]
    assert items == [DEFAULT_DIR, "..", "Lab"]
    next(a for a in win._dirs_popup.actions() if a.text() == "Lab").trigger()
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR + "\\Lab" and sim.cdir.endswith("Lab"))
    win._dirs_popup.close()
    win._dirs_popup = None
    win.vdir_button.click()
    _wait(qtbot, lambda: getattr(win, "_dirs_popup", None) is not None)
    next(a for a in win._dirs_popup.actions() if a.text() == "..").trigger()
    _wait(qtbot, lambda: proxy.value("state.dir") == DEFAULT_DIR)
    assert VNAWindow.dir_choices("C:\\Users", ["A"]) == [("..", "C:\\"), ("A", "C:\\Users\\A")]
    assert VNAWindow.dir_choices("C:\\", []) == []


# ============================================================ 10. Калибровка (Cal sets, 4-портовая Guided, THRU S41)
def test_cal_tab_cal_sets(qtbot, vna):
    proxy, win, _s, sim = vna
    cs, cb = win.cal_sets, win.channel_bar
    _wait(qtbot, lambda: [b.text() for b in cs.chips.buttons()] == ["NONE", "CAL_1-3G", "CAL_FULL"])
    assert [ln.name for ln in _page_by_name(win, "CAL").lines] == ["CSET", "STIM", "CORR", "INT", "NEW"]
    assert cs.chips.checked_index() == 0
    cs.chips.buttons()[1].click()
    _wait(qtbot, lambda: sim.c["cset"] == "CAL_1-3G" and proxy.value("cal.set") == "CAL_1-3G")
    assert sim.c["corr"] and sim.c["start"] == 1e9 and cs.chips.checked_index() == 1

    # Создаём канал 2 и применяем CAL_FULL без смены стимула (KEEP)
    cb.add_button.click()
    _wait(qtbot, lambda: proxy.value("stim.channel") == 2 and cs.chips.checked_index() == 0)
    win.widgets["cal.stim"].set_value(False)
    _wait(qtbot, lambda: proxy.value("cal.stim") is False)
    cs.chips.buttons()[2].click()
    _wait(qtbot, lambda: sim.stim(2)["cset"] == "CAL_FULL" and cs.chips.checked_index() == 2)
    assert sim.stim(2)["start"] == 1e9 and sim.stim(2)["poin"] == 101
    cs.chips.buttons()[0].click()
    _wait(qtbot, lambda: sim.stim(2)["cset"] == "" and proxy.value("cal.set") == "")

    # Возвращаемся на канал 1 и удаляем CAL_1-3G (два клика)
    cb.chips.buttons()[0].click()
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
    win.set_page([win.tabs.tabText(i) for i in range(win.tabs.count())].index("CAL"))
    assert not cw.isVisible()
    win.cal_button.click()
    assert cw.isVisible() and cw.windowTitle() == "CAL CH1"
    assert [ln.name for ln in cw.table.lines] == ["TYPE", "P1", "P2", "P3", "P4", "RUN", "STEP", "NAME"]
    if wz.kind != "FULL":
        wz.next_type()
    _wait(qtbot, lambda: wz.kit(1) == "Ideal 3.5mm" and wz.kit(2) == "Ideal 3.5mm")
    assert wz.conn_buttons[1].text() == "3.5 F" and not wz.run_lines[0].isVisible()

    # Отключаем P3 и P4 -> 2-портовая калибровка (7 шагов)
    wz.set_conn(3, wz.OFF)
    wz.set_conn(4, wz.OFF)
    wz.set_conn(2, "Type N (50) male")
    _wait(qtbot, lambda: wz.kit(2) == "Ideal Type N")
    assert settings.value(f"{win._prefix}/CAL_P2_CONN") == "Type N (50) male"
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 7)
    qtbot.wait(50)
    h = cw.height()
    assert win.cal_button.indicated() and wz.run_lines[0].isVisible()
    assert not wz.save_button.isEnabled() and not wz.start_button.isEnabled()
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

    # Однопортовая калибровка + отмена при закрытии окна
    wz.set_conn(2, wz.OFF)
    assert not wz.kit_buttons[2].isVisible()
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 3 and cw.height() < h)
    cw.close()
    _wait(qtbot, lambda: not wz.steps and proxy.value("meas.armed") is True)
    assert not win.cal_button.indicated()


def test_cal_window_thru_response_s41(qtbot, vna):
    proxy, win, _s, sim = vna
    wz, cw, te = win.cal_wizard, win.cal_window, win.trace_editor
    win.show()
    win.open_cal_window()
    if wz.kind != "THRU":
        wz.type_button.click()
    assert wz.kind == "THRU" and not wz.port_lines[1].isVisible() and wz.trace_label.isVisible()
    assert te.current_spec().sparam == "S11" and not wz.start_button.isEnabled()

    # Переключаем выбранную трассу на S41 -> THRU между P1 и P4
    te.sparam.on_pick("S41")
    _wait(qtbot, lambda: wz.start_button.isEnabled() and wz.trace_label.text() == "1·S41")
    wz.start_button.click()
    _wait(qtbot, lambda: len(wz.step_buttons()) == 1)
    assert "P1" in wz.step_buttons()[0].text() and "P4" in wz.step_buttons()[0].text()
    wz.step_buttons()[0].click()
    _wait(qtbot, lambda: wz.save_button.isEnabled())
    wz.name_edit.setText("THRU_41")
    wz.save_button.click()
    _wait(qtbot, lambda: sim.c["cset"] == "THRU_41" and not wz.steps)
    wz.type_button.click()
    assert wz.kind == "FULL" and wz.port_lines[1].isVisible()


# ============================================================ 11. Ini и калибровка источника
def test_sweeps_do_not_rewrite_ini(qtbot, vna):
    proxy, win, settings, sim = vna
    writes, sweeps = [], []
    real = settings.setValue
    settings.setValue = lambda k, v: (writes.append(k), real(k, v))
    proxy.param_updated.connect(lambda k, _v: sweeps.append(k) if k == "s11.trace" else None)
    _wait(qtbot, lambda: len(sweeps) >= 5)
    assert not [k for k in writes if k.endswith(("FREQ.START", "FREQ.STOP", "SWEEP.TYPE"))], writes
    win.widgets["freq.start"].set_value(2e9)
    _wait(qtbot, lambda: any(k.endswith("FREQ.START") for k in writes))


def test_source_calibration_on_pwr_tab(qtbot, tmp_path):
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir()
    (cal_dir / "vna_cable.csv").write_text("freq_hz,db\n1e9,-2.0\n3e9,2.0\n", encoding="utf-8")
    proxy, win, _settings, sim = _open(qtbot, tmp_path)
    try:
        proxy.set("freq.cw", 1e9)
        proxy.set("gen.state", True)
        _wait(qtbot, lambda: proxy.value("gen.state") is True and proxy.value("freq.cw") == pytest.approx(1e9))
        sc = win.source_cal
        sc.refresh_tables()
        sc.cal_choice.set_value("vna_cable.csv")
        sc.gain.set_value(-10.0)
        sc.gain_on.set_value(True)
        _wait(qtbot, lambda: proxy.value("power.offset") == pytest.approx(8.0) and proxy.value("power.offset_state") is True)
        proxy.set("source.power", -10.0)
        _wait(qtbot, lambda: sim.c["pow"] == pytest.approx(-2.0) and proxy.value("source.power") == pytest.approx(-10.0))
        proxy.set("freq.cw", 3e9)
        _wait(qtbot, lambda: proxy.value("power.offset") == pytest.approx(12.0) and sim.c["pow"] == pytest.approx(2.0))
        assert proxy.value("source.power") == pytest.approx(-10.0)
    finally:
        win.cleanup()
        proxy.shutdown()


# ============================================================ 12. Интеграция с InstrumentsWindow и GraphsWindow
def test_vna_inside_instruments_and_graphs_window(qtbot, tmp_path):
    from visalab.coord import COORD, ActivityMonitor
    from visalab.front import GraphsWindow, InstrumentsWindow

    sim = KeysightPNASim(ports=4)
    proxy, win, settings, _sim = _open(qtbot, tmp_path, sim, ini_name="integ.ini")
    COORD.links.add(proxy)
    mon = ActivityMonitor()
    iw = InstrumentsWindow(settings, instruments=("VNA",))
    gw = GraphsWindow(settings, mon, instruments_window=iw)
    qtbot.addWidget(iw)
    qtbot.addWidget(gw)
    try:
        iw.attach_instrument("VNA", win)
        iw.show()
        iw.open_settings("VNA")
        view = iw.settings_views["VNA"]
        assert list(view.sections) == ["TRAC", "CH", "PWR", "TRIG", "CAL", "SAVE", "SET"]
        assert view.pinned_section is not None and view.pinned_section.isVisible()
        # CH и TRC не в избранном, а в закреплённой шапке настроек и не сворачиваются по collapse_all
        assert [ln.name for ln in win.preset_panel.table.lines] == ["POW", "CW"]
        view.collapse_all()
        assert view.pinned_section.isVisible()
        view.expand_all()

        # Добавляем вторую трассу (2·S41 SWR) и проверяем, что GraphsWindow получает обе с верными единицами
        te = win.trace_editor
        te.create()
        te.sparam.on_pick("S41")
        te.format.on_pick("SWR")
        te.channel.on_pick(2)
        _wait(qtbot, lambda: len(mon.vna_traces(proxy)) == 2)

        traces = mon.vna_traces(proxy)
        assert [lbl for lbl, _tr in traces] == ["1·S11 MLOG", "2·S41 SWR"]
        assert [tr.y_unit for _lbl, tr in traces] == ["dB", ""]
    finally:
        mon.stop()
        COORD.links.remove(proxy)
        gw.close()
        iw.close()
        win.cleanup()
        proxy.shutdown()
