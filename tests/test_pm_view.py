"""Тесты графика ваттметра (кривая на каждую мощность, подписи макс/мин), таблицы с линиями и CSV-точек."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front.main_window import MainWindow
from visalab.front.params import Panel
from visalab.front.pm_view import (
    PMPointsDialog,
    PowerCurvePlot,
    filter_pm_matrix,
    load_pm_points_csv,
    parse_freq_hz,
    parse_power_dbm,
    save_pm_points_csv,
)
from visalab.instruments import InstrumentProxy
from visalab.sim.bench import SimBench
from visalab.transport import SimTransport


def test_csv_load_save_and_parsing(tmp_path):
    assert parse_freq_hz("2.4 GHz") == pytest.approx(2.4e9)
    assert parse_freq_hz("500M") == pytest.approx(500e6)
    assert parse_freq_hz("bad") is None
    assert parse_power_dbm("-10 dBm") == pytest.approx(-10.0)
    assert parse_power_dbm("") is None and parse_power_dbm("*") is None

    csv_file = tmp_path / "plan.csv"
    csv_file.write_text(
        "# Target points\n"
        "freq;power\n"
        "1 GHz;-10\n"
        "1G;0 dBm\n"
        "2 GHz;-10;0\n"
        "3 GHz;\n",
        encoding="utf-8",
    )
    pts = load_pm_points_csv(csv_file)
    assert pts == [
        (1e9, -10.0),
        (1e9, 0.0),
        (2e9, -10.0),
        (2e9, 0.0),
        (3e9, None),
    ]

    out = save_pm_points_csv(tmp_path / "saved.csv", pts)
    assert load_pm_points_csv(out) == pts


def test_filter_pm_matrix_csv_plan():
    raw_cols = [-20.0, -10.0, 0.0]
    raw_rows = [
        (1e9, {-20.0: -30.0, -10.0: -20.0, 0.0: -10.0}),
        (2e9, {-10.0: -21.0, 0.0: -11.0}),
        (3e9, {-10.0: -22.0}),
    ]
    target = [(1e9, -10.0), (1e9, 0.0), (2e9, -10.0), (4e9, -10.0), (4e9, 0.0)]
    cols, rows, planned = filter_pm_matrix(raw_cols, raw_rows, target)

    # -20 dBm и 3 GHz отфильтрованы; 4 GHz добавлен как запланированный (пустые значения)
    assert cols == [-10.0, 0.0]
    assert [f for f, _v in rows] == [1e9, 2e9, 4e9]
    assert rows[0][1] == {-10.0: -20.0, 0.0: -10.0}
    assert rows[1][1] == {-10.0: -21.0}                # (2e9, 0.0) не был в target
    assert rows[2][1] == {}                            # 4e9 ещё не измерен
    assert (4e9, -10.0) in planned and (4e9, 0.0) in planned and (2e9, 0.0) not in planned


def test_power_curve_plot_curves_and_minmax_labels(qtbot):
    plot = PowerCurvePlot(unit="dBm")
    qtbot.addWidget(plot)
    plot.resize(520, 280)
    plot.show()

    cols = [-20.0, -10.0, 0.0]
    rows = [
        (1e9, {-20.0: -30.0, -10.0: -20.0, 0.0: -10.0}),
        (2e9, {-20.0: -30.5, -10.0: -20.5, 0.0: -10.5}),
        (3e9, {-20.0: -31.0, -10.0: -21.0, 0.0: -11.0}),
    ]
    plot.set_matrix(cols, rows, unit="dBm")
    plot.grab()

    assert len(plot._curves) == 3
    above = [item for item in plot.point_labels if item[0] == "above"]
    below = [item for item in plot.point_labels if item[0] == "below"]
    assert len(above) == 3 and len(below) == 3
    # Максимальная кривая (0 dBm -> -10..-11 dBm): подпись строго ВЫШЕ точки (lbl_y < pt_y)
    for _pl, text, _px, pt_y, lbl_y in above:
        assert lbl_y < pt_y
        assert text in ("-10.0 dBm", "-10.5 dBm", "-11.0 dBm")
    # Минимальная кривая (-20 dBm -> -30..-31 dBm): подпись строго НИЖЕ точки (lbl_y > pt_y)
    for _pl, text, _px, pt_y, lbl_y in below:
        assert lbl_y > pt_y
        assert text in ("-30.0 dBm", "-30.5 dBm", "-31.0 dBm")

    # Переключение единиц в W и dB корректно перестраивает график и подписи
    plot.set_unit("W")
    plot.grab()
    assert any("W" in lbl[1] for lbl in plot.point_labels)
    plot.set_unit("dB")
    plot.grab()
    assert any("dB" in lbl[1] for lbl in plot.point_labels)


def test_pm_points_dialog_edit_grid_and_save(qtbot, tmp_path):
    dlg = PMPointsDialog([], parent=None)
    qtbot.addWidget(dlg)

    # Генерация сетки 3 частоты × 2 мощности = 6 точек
    dlg.gen_f1.setText("1G")
    dlg.gen_f2.setText("3G")
    dlg.gen_fn.setText("3")
    dlg.gen_p1.setText("-10")
    dlg.gen_p2.setText("0")
    dlg.gen_pn.setText("2")
    dlg.gen_btn.click()
    assert dlg.table.rowCount() == 6

    # Редактирование ячейки и добавление строки
    dlg.add_row()
    assert dlg.table.rowCount() == 7
    dlg.table.item(6, 0).setText("4.5 GHz")
    dlg.table.item(6, 1).setText("-5")
    pts = dlg.collect_points()
    assert (4.5e9, -5.0) in pts and len(pts) == 7

    # Сохранение в CSV
    target_path = tmp_path / "edited.csv"
    dlg.csv_path = str(target_path)
    saved = dlg.save_file()
    assert saved == target_path and load_pm_points_csv(target_path) == pts


def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return Instrument(InstrumentProxy(drv, SimTransport(sim_cls())))


def test_main_window_pm_graph_table_toggles_and_csv_mode(qtbot, tmp_path):
    # В таблице ваттметра включены горизонтальные линии между строками
    assert "border-bottom:" in Panel.STYLE_SHEET.split("pmTable::item")[1].splitlines()[0]

    links = ProxyLinks()
    gen, pm = _proxy("e8267d"), _proxy("u2021xa")
    bench = SimBench([sim_of(gen)], [], [sim_of(pm)], loss_db=10.0, slope_db_per_ghz=0.5)
    for p in (gen, pm):
        links.add(p)
        with qtbot.waitSignal(p.sync_done, timeout=10000):
            p.open()
    try:
        ini = tmp_path / "pm_view.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        mon = ActivityMonitor(links)
        win = MainWindow(settings, mon)
        qtbot.addWidget(win)
        win.resize(980, 520)
        win.show()
        panel = win.panels[pm]

        def measure(freq_hz: float, p_in: float):
            gen.set("rf.frequency", freq_hz)
            gen.set("rf.power", p_in)
            gen.set("rf.output", True)
            pm.set("freq", freq_hz)
            expected = p_in - (10.0 + 0.5 * (freq_hz / 1e9))
            qtbot.waitUntil(lambda: gen.value("rf.output") is True
                            and gen.value("rf.frequency") == pytest.approx(freq_hz)
                            and gen.value("rf.power") == pytest.approx(p_in)
                            and pm.value("freq") == pytest.approx(freq_hz), timeout=5000)
            bench.step()
            qtbot.waitUntil(lambda: pm.value("meas.power") is not None
                            and abs(pm.value("meas.power") - expected) < 0.3, timeout=5000)
            qtbot.waitUntil(lambda: any(abs(f - freq_hz) < 1.0 and p_in in vals
                                        and abs(vals[p_in] - expected) < 0.3
                                        for f, vals in panel.power_table.rows), timeout=5000)

        for f in (1e9, 2e9, 3e9):
            measure(f, -10.0)
            measure(f, 0.0)

        # По умолчанию видны и график ваттметра, и таблица
        assert panel.pm_plot.isVisibleTo(panel) and panel.power_table.isVisibleTo(panel)
        assert len(panel.pm_plot._curves) == 2

        # Переключение PLOT / TBL
        panel.pm_table_button.click()
        assert panel.pm_plot.isVisibleTo(panel) and not panel.power_table.isVisibleTo(panel)
        panel.pm_plot_button.click()                   # нельзя скрыть оба -> таблица включается сама
        assert not panel.pm_plot.isVisibleTo(panel) and panel.power_table.isVisibleTo(panel)
        panel.pm_plot_button.click()
        assert panel.pm_plot.isVisibleTo(panel) and panel.power_table.isVisibleTo(panel)

        # Загрузка CSV-плана (1 GHz, 2 GHz и ещё не измеренный 4 GHz) -> 3 GHz скрыт, 4 GHz показан как "---"
        csv_path = tmp_path / "targets.csv"
        save_pm_points_csv(csv_path, [(1e9, -10.0), (1e9, 0.0), (2e9, -10.0), (2e9, 0.0), (4e9, -10.0), (4e9, 0.0)])
        panel.load_pm_csv(csv_path, enable=True)
        assert panel.pm_csv_enabled and panel.pm_csv_button.isChecked()
        assert [f for f, _v in panel.power_table.rows] == [1e9, 2e9, 4e9]
        assert panel.power_table.item(2, 1).text() == "---"
        assert len(panel.pm_plot._curves) == 2
        assert all(len(pts) == 2 for _cp, _lbl, pts in panel.pm_plot._curves)

        # Выключаем режим CSV -> 3 GHz возвращается, 4 GHz пропадает
        panel.pm_csv_button.click()
        assert not panel.pm_csv_enabled
        assert [f for f, _v in panel.power_table.rows] == [1e9, 2e9, 3e9]

        # Включаем CSV снова, сохраняем и проверяем восстановление в новом окне
        panel.pm_csv_button.click()
        settings.sync()
        win2 = MainWindow(QSettings(str(ini), QSettings.Format.IniFormat), mon)
        qtbot.addWidget(win2)
        p2 = win2.panels[pm]
        assert p2.pm_csv_enabled and p2.pm_csv_path == str(csv_path)
        assert [f for f, _v in p2.power_table.rows] == [1e9, 2e9, 4e9]
    finally:
        for p in (gen, pm):
            p.shutdown()
