"""Тесты разделителей ``GraphSplitter`` и внутренней раскладки графиков ВАЦ (``PLOT_LAYOUT_MODES``)."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QWidget

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front.main_window import MainWindow
from visalab.front.split_layout import PLOT_LAYOUT_MODES, GraphSplitter, PaneLeaf, PaneSplit, build_preset_tree
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport


def test_preset_trees_for_vna_plots():
    t_col = build_preset_tree("1 COL", ["0", "1", "2"])
    assert isinstance(t_col, PaneSplit) and t_col.orientation == "V"
    assert [c.key for c in t_col.children] == ["0", "1", "2"]

    t_row = build_preset_tree("ROW", ["0", "1", "2"])
    assert isinstance(t_row, PaneSplit) and t_row.orientation == "H"

    t_2c = build_preset_tree("2 COL", ["0", "1", "2"])
    assert isinstance(t_2c, PaneSplit) and t_2c.orientation == "V"
    assert isinstance(t_2c.children[0], PaneSplit) and t_2c.children[0].orientation == "H"
    assert isinstance(t_2c.children[1], PaneLeaf) and t_2c.children[1].key == "2"

    assert build_preset_tree("ROW", ["0"]) == PaneLeaf("0")
    assert build_preset_tree("ROW", []) is None


def test_graph_splitter_weights_and_equalize(qtbot):
    sp = GraphSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(sp)
    sp.resize(600, 300)
    sp.addWidget(QWidget())
    sp.addWidget(QWidget())
    sp.show()
    qtbot.waitExposed(sp)

    sp.apply_weights([0.7, 0.3])
    w = sp.current_weights()
    assert w is not None and w[0] == pytest.approx(0.7, abs=0.05)

    sp.equalize()
    w2 = sp.current_weights()
    assert w2 is not None and w2[0] == pytest.approx(0.5, abs=0.05)


def test_vna_panel_split_traces_and_internal_plot_layout(qtbot, tmp_path):
    drv, sim_cls = DRIVERS["pna"]
    vna = Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))
    links = ProxyLinks()
    links.add(vna)
    with qtbot.waitSignal(vna.sync_done, timeout=10000):
        vna.open()
    try:
        sim = sim_of(vna)
        sim.meas["CH1_S21_2"] = {"s": "S21", "fmt": "MLOG", "ch": 1, "pdiv": 10.0, "rlev": 0.0, "rpos": 5.0}
        sim.meas["CH1_S22_3"] = {"s": "S22", "fmt": "MLOG", "ch": 1, "pdiv": 10.0, "rlev": 0.0, "rpos": 5.0}
        sim.windows[1][2] = "CH1_S21_2"
        sim.windows[1][3] = "CH1_S22_3"
        vna.set("m2.channel", 1)
        vna.set("m2.name", "CH1_S21_2")
        vna.set("m2.enabled", True)
        vna.set("m3.channel", 1)
        vna.set("m3.name", "CH1_S22_3")
        vna.set("m3.enabled", True)

        ini = tmp_path / "vna_split.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        win = MainWindow(settings, ActivityMonitor(links))
        qtbot.addWidget(win)
        win.resize(1000, 800)
        win.show()
        panel = win.panels[vna]
        qtbot.waitUntil(lambda: len(win.monitor.vna_traces(vna)) == 3, timeout=10000)
        qtbot.waitUntil(lambda: len(panel.plots[0].traces) == 3, timeout=5000)

        assert len([p for p in panel.plots if p.isVisibleTo(panel)]) == 1
        assert panel.split_traces_cb is not None and not panel.split_traces_cb.isChecked()
        assert tuple(panel.plot_layout_buttons) == PLOT_LAYOUT_MODES

        # Включаем Split each trace во вкладке SET -> 3 отдельных графика
        panel.tabs.setCurrentIndex(1)
        panel.split_traces_cb.click()
        panel.tabs.setCurrentIndex(0)
        assert len([p for p in panel.plots if p.isVisibleTo(panel)]) == 3
        assert all(len(panel.plots[i].traces) == 1 for i in range(3))
        assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Vertical

        # Переключаем внутреннюю раскладку: 2 COL и ROW
        panel.plot_layout_buttons["2 COL"].click()
        assert panel.plot_layout_mode == "2 COL"
        assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Vertical
        assert isinstance(panel._plot_root.widget(0), GraphSplitter)
        assert panel._plot_root.widget(0).orientation() == Qt.Orientation.Horizontal

        panel.plot_layout_buttons["ROW"].click()
        assert panel.plot_layout_mode == "ROW"
        assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Horizontal

        # Настройки сохраняются и восстанавливаются в новом окне
        settings.sync()
        win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
        qtbot.addWidget(win2)
        try:
            panel2 = win2.panels[vna]
            assert panel2.split_traces_cb.isChecked()
            assert panel2.plot_layout_mode == "ROW"
            assert len([p for p in panel2.plots if p.isVisibleTo(panel2)]) == 3
        finally:
            win2.cleanup()
            win2.close()
        win.cleanup()
        win.close()
    finally:
        vna.shutdown()
