"""Тесты разделителей ``GraphSplitter`` и раскладки ``GraphsWindow``."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QWidget

from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front import GraphsWindow
from visalab.front.split_layout import GraphSplitter, PaneLeaf, PaneSplit, build_preset_tree
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


def test_graphs_window_splitter_and_vna_live_traces(qtbot, tmp_path):
    drv_vna, sim_vna = DRIVERS["pna"]
    drv_sa, sim_sa = DRIVERS["e4446a"]
    vna = Instrument(InstrumentProxy(drv_vna, SimTransport(sim_vna())))
    sa = Instrument(InstrumentProxy(drv_sa, SimTransport(sim_sa())))
    links = ProxyLinks()
    links.add(vna)
    links.add(sa)
    with qtbot.waitSignal(vna.sync_done, timeout=10000):
        vna.open()
    with qtbot.waitSignal(sa.sync_done, timeout=10000):
        sa.open()
    try:
        vna.set("s21.enabled", True)
        ini = tmp_path / "graphs_split.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        mon = ActivityMonitor(links)
        win = GraphsWindow(settings, mon)
        qtbot.addWidget(win)
        win.resize(1000, 700)
        win.show()
        qtbot.waitExposed(win)

        qtbot.waitUntil(lambda: len(win.card("VNA").plot.series) >= 2, timeout=10000)
        assert win.main_card.graph_name == "VNA"
        assert "SA" in win.side_cards

        win.splitter.apply_weights([0.65, 0.35])
        win._save_split()
        settings.sync()
        win.cleanup()
        win.close()

        win2 = GraphsWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
        qtbot.addWidget(win2)
        win2.resize(1000, 700)
        win2.show()
        qtbot.waitExposed(win2)
        try:
            w = win2.splitter.current_weights()
            assert w is not None and w[0] == pytest.approx(0.65, abs=0.05)
        finally:
            win2.cleanup()
            win2.close()
        mon.stop()
    finally:
        vna.shutdown()
        sa.shutdown()
