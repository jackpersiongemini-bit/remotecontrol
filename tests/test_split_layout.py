"""Тесты системы разделения экрана графиков: дерево сплитов, пресеты, перетаскивание, фокус и VNA sub-plots."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, QRectF, QSettings, Qt
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QWidget

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front.main_window import _PANEL_MIME, MainWindow
from visalab.front.split_layout import (
    CUSTOM_LAYOUT_MODE,
    LAYOUT_MODES,
    DropZoneOverlay,
    GraphSplitter,
    PaneLeaf,
    PaneSplit,
    build_preset_tree,
    detect_drop_zone,
    dock_panel,
    drop_zone_rect,
    equalize_tree,
    layout_preset_icon,
    leaf_order,
    normalize_tree,
    project_tree,
    sync_weights_into_tree,
    tree_from_dict,
    tree_structure_sig,
    tree_to_dict,
)
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


# ------------------------------------------------------------------ чистая модель дерева
def test_preset_trees_shapes_and_weights():
    assert build_preset_tree("1 COL", []) is None
    assert build_preset_tree("ROW", ["A"]) == PaneLeaf("A")

    t_col = build_preset_tree("1 COL", ["A", "B", "C"])
    assert isinstance(t_col, PaneSplit) and t_col.orientation == "V"
    assert leaf_order(t_col) == ["A", "B", "C"] and sum(t_col.weights) == pytest.approx(1.0)

    t_row = build_preset_tree("ROW", ["A", "B", "C"])
    assert isinstance(t_row, PaneSplit) and t_row.orientation == "H"
    assert leaf_order(t_row) == ["A", "B", "C"]

    t_2col = build_preset_tree("2 COL", ["A", "B", "C"])
    assert isinstance(t_2col, PaneSplit) and t_2col.orientation == "V" and len(t_2col.children) == 2
    assert isinstance(t_2col.children[0], PaneSplit) and t_2col.children[0].orientation == "H"
    assert leaf_order(t_2col.children[0]) == ["A", "B"]
    assert t_2col.children[1] == PaneLeaf("C")

    t_main_l = build_preset_tree("MAIN L", ["A", "B", "C"])
    assert isinstance(t_main_l, PaneSplit) and t_main_l.orientation == "H"
    assert t_main_l.children[0] == PaneLeaf("A")
    assert isinstance(t_main_l.children[1], PaneSplit) and t_main_l.children[1].orientation == "V"
    assert leaf_order(t_main_l.children[1]) == ["B", "C"]
    assert t_main_l.weights[0] > t_main_l.weights[1]

    t_main_t = build_preset_tree("MAIN T", ["A", "B", "C"])
    assert isinstance(t_main_t, PaneSplit) and t_main_t.orientation == "V"
    assert t_main_t.children[0] == PaneLeaf("A")
    assert isinstance(t_main_t.children[1], PaneSplit) and t_main_t.children[1].orientation == "H"
    assert leaf_order(t_main_t.children[1]) == ["B", "C"]


def test_dock_panel_all_five_zones_and_normalization():
    base = build_preset_tree("1 COL", ["A", "B", "C"])
    # Справа от A -> H(A, C) сверху, B снизу
    t = dock_panel(base, "C", "A", "right")
    assert isinstance(t, PaneSplit) and t.orientation == "V"
    assert isinstance(t.children[0], PaneSplit) and t.children[0].orientation == "H"
    assert leaf_order(t.children[0]) == ["A", "C"]
    assert t.children[1] == PaneLeaf("B")

    # Слева от C -> сглаживание вложенного H: H(B, A, C) -> т.к. B перенесли влево от C: H(A, B, C)
    t2 = dock_panel(t, "B", "C", "left")
    assert isinstance(t2, PaneSplit) and t2.orientation == "H"
    assert leaf_order(t2) == ["A", "B", "C"]

    # Снизу от C -> H(A, B, V(C, ...)) после переноса A вниз от C -> H(B, V(C, A))
    t3 = dock_panel(t2, "A", "C", "bottom")
    assert isinstance(t3, PaneSplit) and t3.orientation == "H"
    assert t3.children[0] == PaneLeaf("B")
    assert isinstance(t3.children[1], PaneSplit) and t3.children[1].orientation == "V"
    assert leaf_order(t3.children[1]) == ["C", "A"]

    # Сверху от B -> H(V(A, B), C)
    t4 = dock_panel(t3, "A", "B", "top")
    assert isinstance(t4, PaneSplit) and t4.orientation == "H"
    assert isinstance(t4.children[0], PaneSplit) and t4.children[0].orientation == "V"
    assert leaf_order(t4.children[0]) == ["A", "B"]
    assert t4.children[1] == PaneLeaf("C")

    # Центр -> обмен местами без поломки структуры
    t5 = dock_panel(t4, "A", "C", "center")
    assert tree_structure_sig(t5) == ("H", (("V", (("L", "C"), ("L", "B"))), ("L", "A")))

    # Сброс на себя - без изменений
    assert dock_panel(t5, "A", "A", "left") == t5


def test_project_tree_preserves_hidden_slot_and_syncs_weights():
    full = PaneSplit("H", [PaneLeaf("VNA"), PaneSplit("V", [PaneLeaf("SA"), PaneLeaf("PM")], [0.4, 0.6])], [0.6, 0.4])
    # SA временно скрыт -> экран показывает H(VNA, PM) с сохранённой пропорцией 60/40
    proj = project_tree(full, ["VNA", "PM"])
    assert isinstance(proj, PaneSplit) and proj.orientation == "H"
    assert leaf_order(proj) == ["VNA", "PM"]
    assert proj.weights == pytest.approx([0.6, 0.4])

    # Пользователь перетащил делитель на 75/25 пока SA скрыт -> синхронизируем веса без потери слота SA
    proj.weights = [0.75, 0.25]
    synced = sync_weights_into_tree(full, proj)
    assert isinstance(synced, PaneSplit) and synced.weights == pytest.approx([0.75, 0.25])
    assert leaf_order(synced) == ["VNA", "SA", "PM"]
    assert isinstance(synced.children[1], PaneSplit) and synced.children[1].weights == pytest.approx([0.4, 0.6])

    # SA вернулся + добавился новый прибор PM2
    restored = project_tree(synced, ["VNA", "SA", "PM", "PM2"])
    assert leaf_order(restored) == ["VNA", "SA", "PM", "PM2"]


def test_tree_serialization_and_equalize():
    tree = PaneSplit("H", [PaneLeaf("VNA"), PaneSplit("V", [PaneLeaf("SA"), PaneLeaf("PM")], [0.25, 0.75])], [0.7, 0.3])
    restored = tree_from_dict(tree_to_dict(tree))
    assert restored is not None
    assert tree_structure_sig(restored) == tree_structure_sig(tree)
    assert restored.weights == pytest.approx([0.7, 0.3])
    assert restored.children[1].weights == pytest.approx([0.25, 0.75])

    eq = equalize_tree(restored)
    assert eq.weights == pytest.approx([0.5, 0.5])
    assert eq.children[1].weights == pytest.approx([0.5, 0.5])

    assert tree_from_dict(None) is None
    assert tree_from_dict({"d": "X", "c": []}) is None
    assert normalize_tree(PaneSplit("H", [PaneLeaf("ONLY")], [1.0])) == PaneLeaf("ONLY")


def test_drop_zone_detection_and_overlay_painting(qtbot):
    r = QRectF(0, 0, 400, 300)
    assert detect_drop_zone(r, QPointF(20, 150)) == "left"
    assert detect_drop_zone(r, QPointF(380, 150)) == "right"
    assert detect_drop_zone(r, QPointF(200, 20)) == "top"
    assert detect_drop_zone(r, QPointF(200, 280)) == "bottom"
    assert detect_drop_zone(r, QPointF(200, 150)) == "center"

    assert drop_zone_rect(r, "left").width() == pytest.approx(drop_zone_rect(r, "right").width())
    assert drop_zone_rect(r, "top").height() == pytest.approx(drop_zone_rect(r, "bottom").height())

    host = QWidget()
    host.resize(320, 240)
    qtbot.addWidget(host)
    ov = DropZoneOverlay(host)
    for zone in ("left", "right", "top", "bottom", "center"):
        ov.set_zone(zone)
        assert ov.isVisibleTo(host)
        ov.grab()
    ov.set_zone(None)
    assert ov.isHidden()

    for mode in (*LAYOUT_MODES, CUSTOM_LAYOUT_MODE):
        assert not layout_preset_icon(mode).isNull()


def test_graph_splitter_double_click_equalizes(qtbot):
    sp = GraphSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(sp)
    a, b = QWidget(), QWidget()
    sp.addWidget(a)
    sp.addWidget(b)
    sp.resize(400, 200)
    sp.show()
    sp.apply_weights([0.75, 0.25])
    qtbot.wait(50)
    w_before = sp.current_weights()
    assert w_before is not None and w_before[0] > 0.65

    with qtbot.waitSignal(sp.equalized, timeout=1000):
        qtbot.mouseDClick(sp.handle(1), Qt.MouseButton.LeftButton)
    w_after = sp.current_weights()
    assert w_after is not None and w_after[0] == pytest.approx(0.5, abs=0.05)


# ------------------------------------------------------------------ интеграция с MainWindow
def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


@pytest.fixture
def split_lab(qtbot):
    links = ProxyLinks()
    proxies = {k: _proxy(k) for k in ("pna", "e4446a", "u2021xa", "e8267d")}
    sa_sim = sim_of(proxies["e4446a"])
    sa_sim.c.update(center=1e9, span=100e6)
    sa_sim._couple()
    bench = SimBench([sim_of(proxies["e8267d"])], [sa_sim], [sim_of(proxies["u2021xa"])])
    bench.step()
    for p in proxies.values():
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    yield links, proxies
    for p in proxies.values():
        p.shutdown()


def test_main_window_split_presets_main_l_and_main_t(qtbot, split_lab, tmp_path):
    links, px = split_lab
    ini = tmp_path / "presets.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.show()
    win.all_button.setChecked(True)
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)

    # Цикл пресетов: 1 COL -> 2 COL -> ROW -> MAIN L -> MAIN T -> 1 COL
    seen = [win.layout_mode]
    for _ in range(len(LAYOUT_MODES)):
        win.layout_button.click()
        seen.append(win.layout_mode)
    assert seen == ["1 COL", "2 COL", "ROW", "MAIN L", "MAIN T", "1 COL"]

    # MAIN L: корневой горизонтальный сплиттер, слева VNA, справа вертикальный [SA, PM]
    win.set_layout_mode("MAIN L")
    assert isinstance(win._root_widget, GraphSplitter)
    assert win._root_widget.orientation() == Qt.Orientation.Horizontal
    assert win._root_widget.widget(0) is win.panels[px["pna"]]
    right = win._root_widget.widget(1)
    assert isinstance(right, GraphSplitter) and right.orientation() == Qt.Orientation.Vertical
    assert right.widget(0) is win.panels[px["e4446a"]] and right.widget(1) is win.panels[px["u2021xa"]]

    # MAIN T: корневой вертикальный сплиттер, сверху VNA, снизу горизонтальный [SA, PM]
    win.set_layout_mode("MAIN T")
    assert isinstance(win._root_widget, GraphSplitter)
    assert win._root_widget.orientation() == Qt.Orientation.Vertical
    assert win._root_widget.widget(0) is win.panels[px["pna"]]
    bottom = win._root_widget.widget(1)
    assert isinstance(bottom, GraphSplitter) and bottom.orientation() == Qt.Orientation.Horizontal
    assert bottom.widget(0) is win.panels[px["e4446a"]] and bottom.widget(1) is win.panels[px["u2021xa"]]

    # Перезапуск восстанавливает MAIN T
    settings.sync()
    win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
    qtbot.addWidget(win2)
    assert win2.layout_mode == "MAIN T" and win2.layout_button.text() == "MAIN T"


def test_main_window_custom_split_docking_hide_restore_and_persist(qtbot, split_lab, tmp_path):
    links, px = split_lab
    ini = tmp_path / "custom_split.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.resize(1000, 760)
    win.show()
    win.all_button.setChecked(True)
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)

    vna_k = win._panel_key(px["pna"])
    sa_k = win._panel_key(px["e4446a"])
    pm_k = win._panel_key(px["u2021xa"])

    # Реальное dropEvent: перетаскиваем PM на правый край SA -> режим SPLIT
    sa_panel = win.panels[px["e4446a"]]
    mime = QMimeData()
    mime.setData(_PANEL_MIME, pm_k.encode("utf-8"))
    right_edge = QPointF(sa_panel.width() - 8, sa_panel.height() / 2.0)
    ev = QDropEvent(right_edge, Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    sa_panel.dropEvent(ev)

    assert win.layout_mode == CUSTOM_LAYOUT_MODE and win.layout_button.text() == CUSTOM_LAYOUT_MODE
    assert isinstance(win._root_widget, GraphSplitter) and win._root_widget.orientation() == Qt.Orientation.Vertical
    assert win._root_widget.widget(0) is win.panels[px["pna"]]
    row = win._root_widget.widget(1)
    assert isinstance(row, GraphSplitter) and row.orientation() == Qt.Orientation.Horizontal
    assert row.widget(0) is win.panels[px["e4446a"]] and row.widget(1) is win.panels[px["u2021xa"]]

    # Скрываем SA и возвращаем обратно -> SA возвращается в своё место слева от PM
    win.hide_panel(px["e4446a"])
    assert [p.title.text() for p in win.visible_panels()] == ["VNA", "PM"]
    win.show_panel(sa_k)
    assert [p.title.text() for p in win.visible_panels()] == ["VNA", "SA", "PM"]
    row2 = win._root_widget.widget(1)
    assert isinstance(row2, GraphSplitter) and row2.widget(0) is win.panels[px["e4446a"]]

    # Перезапуск восстанавливает дерево SPLIT
    settings.sync()
    win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
    qtbot.addWidget(win2)
    win2.show()
    assert win2.layout_mode == CUSTOM_LAYOUT_MODE
    assert isinstance(win2._root_widget, GraphSplitter)
    assert win2._root_widget.orientation() == Qt.Orientation.Vertical
    assert tree_structure_sig(win2._active_tree) == ("V", (("L", vna_k), ("H", (("L", sa_k), ("L", pm_k)))))

    # Сброс разделения -> обратно к 1 COL
    win2.reset_split_layout()
    assert win2.layout_mode == "1 COL" and win2._custom_tree is None


def test_main_window_maximize_and_equalize(qtbot, split_lab, tmp_path):
    links, px = split_lab
    settings = QSettings(str(tmp_path / "max.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings, ActivityMonitor(links))
    qtbot.addWidget(win)
    win.resize(960, 760)
    win.show()
    win.all_button.setChecked(True)
    qtbot.waitUntil(lambda: len(win.visible_panels()) == 3, timeout=10000)

    win.set_layout_mode("MAIN L")
    assert win._splitters
    # Выравнивание делителей: MAIN L имел 58/42 -> стал 50/50
    win.equalize_splits()
    root_node = win._splitters[0][1]
    assert root_node.weights == pytest.approx([0.5, 0.5])

    # Разворачиваем SA на весь экран кнопкой ⤢
    sa_panel = win.panels[px["e4446a"]]
    sa_panel.maximize_button.click()
    assert win.visible_panels() == [sa_panel]
    assert sa_panel.maximize_button.text() == "⤡"
    assert win._root_widget is sa_panel

    # Выход по Esc (вызов _exit_maximize) возвращает MAIN L
    win._exit_maximize()
    assert len(win.visible_panels()) == 3
    assert sa_panel.maximize_button.text() == "⤢"
    assert win.layout_mode == "MAIN L"

    # Двойной клик по заголовку снова разворачивает, а скрытие развёрнутой панели снимает фокус
    qtbot.mouseDClick(sa_panel, Qt.MouseButton.LeftButton, pos=QPoint(80, 14))
    assert win.visible_panels() == [sa_panel]
    win.hide_panel(px["e4446a"])
    assert win._maximized_key is None
    assert len(win.visible_panels()) == 2


def test_vna_panel_split_traces_and_internal_plot_layout(qtbot, split_lab, tmp_path):
    links, px = split_lab
    vna = px["pna"]
    sim = sim_of(vna)
    # 3 трейса с одинаковой шкалой (MLOG, 10 dB/div) -> по умолчанию все на одном графике
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

    # По умолчанию: 1 график с 3 трассами, кнопка SPLIT видна, кнопка раскладки скрыта
    assert len([p for p in panel.plots if p.isVisibleTo(panel)]) == 1
    assert panel.split_traces_button.isVisibleTo(panel) and not panel.split_traces_button.isChecked()
    assert not panel.plot_layout_button.isVisibleTo(panel)

    # Включаем SPLIT -> 3 отдельных графика с делителями, появляется кнопка раскладки графиков
    panel.split_traces_button.click()
    assert len([p for p in panel.plots if p.isVisibleTo(panel)]) == 3
    assert all(len(panel.plots[i].traces) == 1 for i in range(3))
    assert panel.plot_layout_button.isVisibleTo(panel) and panel.plot_layout_mode == "1 COL"
    assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Vertical

    # Переключаем внутреннюю раскладку графиков: 1 COL -> 2 COL -> ROW
    panel.plot_layout_button.click()
    assert panel.plot_layout_mode == "2 COL"
    assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Vertical
    assert isinstance(panel._plot_root.widget(0), GraphSplitter)
    assert panel._plot_root.widget(0).orientation() == Qt.Orientation.Horizontal

    panel.plot_layout_button.click()
    assert panel.plot_layout_mode == "ROW"
    assert isinstance(panel._plot_root, GraphSplitter) and panel._plot_root.orientation() == Qt.Orientation.Horizontal

    # Настройки SPLIT и ROW сохраняются и восстанавливаются в новом окне
    settings.sync()
    win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), ActivityMonitor(links))
    qtbot.addWidget(win2)
    panel2 = win2.panels[vna]
    assert panel2.split_traces_button.isChecked()
    assert panel2.plot_layout_mode == "ROW"
    assert len([p for p in panel2.plots if p.isVisibleTo(panel2)]) == 3
