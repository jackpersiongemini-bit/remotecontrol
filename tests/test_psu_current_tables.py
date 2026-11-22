"""Тесты таблиц токов ИП по частотам и выходным мощностям генератора в главном окне."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QAbstractButton, QLabel, QLineEdit

from conftest import sim_of
from visalab.coord import ActivityMonitor, Instrument, ProxyLinks
from visalab.drivers import DRIVERS
from visalab.front.main_window import CurrentPanel, MainWindow
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport


def _proxy(kind: str, addr: str | None = None) -> Instrument:
    drv, sim_cls = DRIVERS[kind]
    tr = SimTransport(sim_cls(), address=addr or f"SIM::{kind.upper()}::INSTR")
    return Instrument(InstrumentProxy(drv, tr))


@pytest.fixture
def bench_psu(qtbot):
    links = ProxyLinks()
    psu = _proxy("it6300", "SIM::PSU1::INSTR")
    gen1 = _proxy("e8267d", "SIM::GEN1::INSTR")
    gen2 = _proxy("e8267d", "SIM::GEN2::INSTR")
    pm = _proxy("u2021xa", "SIM::PM1::INSTR")
    proxies = (psu, gen1, gen2, pm)
    try:
        # Разные нагрузки на каналах ИП, чтобы токи CH1 и CH2 явно отличались
        psu_sim = sim_of(psu)
        psu_sim.chs[0].load_ohm = 10.0   # 5 В / 10 Ом = 0.5 A
        psu_sim.chs[1].load_ohm = 20.0   # 5 В / 20 Ом = 0.25 A
        for p in proxies:
            links.add(p)
            p.set_poll_interval(50)
            with qtbot.waitSignal(p.sync_done, timeout=10000):
                p.open()
    except Exception:
        for p in proxies:
            p.shutdown()
        raise
    yield links, psu, gen1, gen2, pm
    for p in proxies:
        p.shutdown()


def test_psu_current_tables_matrix_selection_rf_choice_and_persistence(qtbot, bench_psu, tmp_path):
    links, psu, gen1, gen2, pm = bench_psu
    psu_sim = sim_of(psu)
    mon = ActivityMonitor(links)
    settings = QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat)
    mw = MainWindow(settings, mon)
    qtbot.addWidget(mw)
    mw.show()
    qtbot.waitExposed(mw)

    try:
        # Кнопка CURR видна (есть ИП), по умолчанию таблицы токов не включены
        assert mw.current_button.isVisible()
        assert mw.current_button.text() == "CURR"
        assert len(mw.current_panels) == 3  # IT6300: CH1, CH2, CH3
        assert not any(isinstance(p, CurrentPanel) for p in mw.visible_panels())

        cp1 = mw.current_panels[(psu, 1)]
        cp2 = mw.current_panels[(psu, 2)]
        cp3 = mw.current_panels[(psu, 3)]

        # Включаем CH1 и CH2 — для каждого источника своя отдельная таблица
        mw.set_current_panel_enabled(cp1.panel_key, True)
        mw.set_current_panel_enabled(cp2.panel_key, True)
        assert mw.current_button.text() == "CURR (2)"
        assert mw.current_button.isChecked()
        assert cp1 in mw.visible_panels() and cp2 in mw.visible_panels() and cp3 not in mw.visible_panels()
        assert mw.current_controls[cp1.panel_key].isChecked()
        assert mw.current_controls[cp2.panel_key].isChecked()
        assert not mw.current_controls[cp3.panel_key].isChecked()

        # Включаем выходы CH1 и CH2 на ИП (CH3 выключен) и генератор GEN1
        psu.set("ch1.voltage", 5.0)
        psu.set("ch1.current", 2.0)
        psu.set("ch1.output", True)
        psu.set("ch2.voltage", 5.0)
        psu.set("ch2.current", 2.0)
        psu.set("ch2.output", True)
        gen1.set("rf.frequency", 1e9)
        gen1.set("rf.power", -10.0)
        gen1.set("rf.output", True)

        qtbot.waitUntil(
            lambda: mon.psu_matrix(psu, 1)[0] == [-10.0] and len(mon.psu_matrix(psu, 1)[1]) == 1,
            timeout=5000,
        )
        # Меняем мощность и частоту генератора + нагрузку CH1
        psu_sim.chs[0].load_ohm = 5.0  # 1.0 A
        gen1.set("rf.power", 0.0)
        qtbot.waitUntil(lambda: mon.psu_matrix(psu, 1)[0] == [-10.0, 0.0], timeout=5000)

        gen1.set("rf.frequency", 2e9)
        qtbot.waitUntil(lambda: len(mon.psu_matrix(psu, 1)[1]) == 2, timeout=5000)

        # Обе таблицы заполнились своими токами независимо, а выключенный CH3 пуст
        cols1, rows1 = mon.psu_matrix(psu, 1)
        cols2, rows2 = mon.psu_matrix(psu, 2)
        assert cols1 == [-10.0, 0.0] and [f for f, _ in rows1] == [1e9, 2e9]
        assert rows1[0][1][-10.0] == pytest.approx(0.5, abs=0.02)
        assert rows1[0][1][0.0] == pytest.approx(1.0, abs=0.02)
        assert rows2[0][1][-10.0] == pytest.approx(0.25, abs=0.02)
        assert mon.psu_matrix(psu, 3) == ([], [])

        # Проверяем отображение в CurrentTable и переключение единиц (mA) во встроенных настройках
        qtbot.waitUntil(lambda: cp1.current_table.rowCount() == 2 and cp1.current_table.columnCount() == 3, timeout=5000)
        cp1.set_unit("mA")
        assert cp1.unit_buttons["mA"].isChecked()
        assert "500.00 mA" == cp1.current_table.item(0, 1).text()
        assert "1000.00 mA" == cp1.current_table.item(0, 2).text()

        # Выбор конкретного RF-генератора GEN2 для таблицы CH2
        gen2.set("rf.frequency", 3.5e9)
        gen2.set("rf.power", -5.0)
        gen2.set("rf.output", True)
        qtbot.waitUntil(lambda: gen2.value("rf.output") is True, timeout=5000)
        cp2.set_rf_source(gen2)
        assert cp2.rf_button.text() == "RF: GEN2"
        qtbot.waitUntil(
            lambda: any(abs(f - 3.5e9) < 1.0 for f, _ in mon.psu_matrix(psu, 2)[1]),
            timeout=5000,
        )

        # Очистка таблицы CH1 не трогает CH2
        cp1.clear_button.click()
        assert mon.psu_matrix(psu, 1) == ([], [])
        assert len(mon.psu_matrix(psu, 2)[1]) >= 1

        # Все видимые элементы управления имеют подсказки (совместимость с test_help)
        for btn in mw.findChildren(QAbstractButton):
            if btn.isVisible():
                assert btn.toolTip(), f"Missing tooltip on {btn.text()!r} ({type(btn).__name__})"

        # Закрытие × на панели CH1 выключает её и обновляет счётчик CURR и галочку в SET
        cp1.close_button.click()
        assert cp1 not in mw.visible_panels()
        assert mw.current_button.text() == "CURR (1)"
        assert not mw.current_controls[cp1.panel_key].isChecked()

        # Сохранение выбора, единиц и RF-источника в QSettings
        mw.cleanup()
        mw.close()
        mw2 = MainWindow(settings, ActivityMonitor(links))
        qtbot.addWidget(mw2)
        try:
            cp1_new = mw2.current_panels[(psu, 1)]
            cp2_new = mw2.current_panels[(psu, 2)]
            assert cp1_new not in mw2.visible_panels()
            assert cp2_new in mw2.visible_panels()
            assert cp1_new.current_table.unit == "mA"
            assert cp2_new.rf_button.text() == "RF: GEN2"
            assert mw2.current_button.text() == "CURR (1)"
        finally:
            mw2.cleanup()
            mw2.close()
    finally:
        if not mw.isHidden():
            mw.cleanup()
            mw.close()
        mon.stop()
