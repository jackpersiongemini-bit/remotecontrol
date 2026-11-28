"""Окно ваттметра на симуляторе U2021XA."""
import json
import math

import pytest
from PyQt6.QtCore import QSettings

from conftest import attach, sim_of
from visalab.instruments import InstrumentProxy
from visalab.transport import SimTransport
from visalab.drivers import DRIVERS
from visalab.front import PowerMeterWindow, PulseGenWindow
from visalab.coord import LINKS
from visalab.front.power_meter import interpolate, load_csv, save_csv


def _proxy(kind):
    drv, sim_cls = DRIVERS[kind]
    return attach(InstrumentProxy(drv, SimTransport(sim_cls())))


@pytest.fixture
def pm(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "pm.ini"), QSettings.Format.IniFormat)
    proxy = _proxy("u2021xa")
    win = PowerMeterWindow(proxy, settings)
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    yield proxy, win, settings
    proxy.shutdown()


def _wait(qtbot, proxy, key, pred):
    qtbot.waitUntil(lambda: pred(proxy.value(key)), timeout=5000)


def test_interpolate():
    t = [(1e9, 1.0), (3e9, 3.0)]
    assert interpolate(t, 2e9) == pytest.approx(2.0)
    assert interpolate(t, 0.5e9) == 1.0 and interpolate(t, 9e9) == 3.0
    assert interpolate([], 2e9) == 0.0


def test_layout_and_readout(qtbot, pm):
    proxy, win, _s = pm
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["MAIN", "TRIG", "CAL", "TIME", "SWP", "SET"]
    avg = next(ln for ln in win.pages[0].lines if ln.name == "AVG")       # усреднение - одной строкой (button line)
    assert [w.key for w in avg.widgets] == ["avg.state", "avg.auto", "avg.count"]
    assert not any(ln.name in ("ACNT", "AAUT") for ln in win.pages[0].lines)
    # Настройки запуска и строба (Gate) - на отдельной вкладке TRIG
    trig_names = [ln.name for ln in win.pages[1].lines]
    for expected in ("SRC", "LVL", "SLOP", "DLY", "HOLD", "GSTR", "GLEN", "RUN"):
        assert expected in trig_names
    assert len(win.preset_panel.table.lines) == 1 and win.preset_panel.table.lines[0].widgets == [win.readout, win.plot]
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None, timeout=5000)
    qtbot.waitUntil(lambda: win.readout.main.text() == f"{proxy.value('meas.power'):.2f}", timeout=5000)
    assert win.readout.sec_button.text().endswith("W")
    win.readout.sec_button.click()
    assert win.readout.sec_button.text().startswith("PEAK")
    assert win.readout.trig_button.text().startswith("FREE")
    # клик по основной мощности: dBm <-> W (малое поле показывает другую единицу)
    from PyQt6.QtCore import Qt
    r = win.readout
    r.sec_button.click()
    r.sec_button.click()                                     # снова W
    qtbot.mouseClick(r.main, Qt.MouseButton.LeftButton)
    assert r.main_unit == "W" and r.unit.text().endswith("W") and r.sec_button.text().endswith("dBm")
    assert _s.value(f"{win._prefix}/MAIN_UNIT") == "W"
    qtbot.mouseClick(r.unit, Qt.MouseButton.LeftButton)
    assert r.main_unit == "dBm" and r.unit.text() == "dBm"


def test_trigger_toggle_from_readout(qtbot, pm):
    proxy, win, _s = pm
    _wait(qtbot, proxy, "trig.source", lambda v: v == "IMM")
    assert not win.tabs.is_on(1)
    assert not win.widgets["trig.slope"].isEnabled()               # в FREE RUN фронт недоступен (-221)
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "INT")
    qtbot.waitUntil(lambda: win.readout.trig_button.text().startswith("LEVEL"), timeout=5000)
    qtbot.waitUntil(lambda: win.tabs.is_on(1), timeout=5000)       # вкладка TRIG светится при активном запуске
    assert win.widgets["trig.slope"].isEnabled()
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "EXT")       # внешний запуск: ждём импульс на TRIG IN
    qtbot.waitUntil(lambda: win.readout.trig_button.text() == "EXT", timeout=5000)
    sim = sim_of(proxy)
    before = proxy.value("meas.power")
    sim.set_signal(on_dbm=-3.0, width=None)
    sim.fire_external()
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None
                    and proxy.value("meas.power") == pytest.approx(-3.0, abs=0.05), timeout=5000)
    assert before is None or before != pytest.approx(-3.0, abs=0.05)
    win.readout.trig_button.click()
    _wait(qtbot, proxy, "trig.source", lambda v: v == "IMM")
    qtbot.waitUntil(lambda: not win.tabs.is_on(1), timeout=5000)


def test_correction_written_as_offset(qtbot, pm):
    proxy, win, settings = pm
    proxy.set("freq", 2e9)
    _wait(qtbot, proxy, "freq", lambda v: v == pytest.approx(2e9))
    win.cal_dir.mkdir(exist_ok=True)
    save_csv(win.cal_dir / "cable.csv", [(1e9, 1.0), (3e9, 3.0)])
    win.cal_choice.click()                            # список перечитывает папку
    win.cal_choice.set_value("cable.csv")
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(2.0, abs=1e-3))
    _wait(qtbot, proxy, "offset.state", lambda v: v is True)
    win.gain_on.set_value(True)
    win.gain.set_value(10.0)
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(-8.0, abs=1e-3))
    win.duty_src.set_value("MAN")
    win.duty.set_value(10.0)                          # 10 % -> +10 dB в FREE RUN
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(2.0, abs=1e-3))
    assert win.readout.cal_button.text() == "CAL +2.00 dB"
    # В LEVEL и EXT поправка 10·lg(100/DUTY) не прибавляется, чтобы не удваивать скважность
    proxy.set("trig.source", "INT")
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(-8.0, abs=1e-3))
    assert win.readout.cal_button.text() == "CAL -8.00 dB"
    proxy.set("trig.source", "IMM")
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(2.0, abs=1e-3))
    assert settings.value(f"{win._prefix}/CAL_FILE") == "cable.csv"
    assert win.cal_plot.trace is not None and win.cal_plot.marker() == pytest.approx(2e9)
    proxy.set("freq", 3e9)                            # частота датчика -> новая точка таблицы
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(3.0, abs=1e-3))
    win.cal_choice.set_value("")                      # NONE - таблица выключена
    win.gain_on.set_value(False)
    win.duty_src.set_value("OFF")
    _wait(qtbot, proxy, "offset.state", lambda v: v is False)


def test_csv_and_acal_ui(qtbot, pm, tmp_path):
    proxy, win, _s = pm
    path = tmp_path / "t.csv"
    path.write_text("freq;dB\n2e9;1.5\n1e9;0.5\nbad line\n", encoding="utf-8")
    assert load_csv(path) == [(1e9, 0.5), (2e9, 1.5)]
    win.cal_plot.set_points(load_csv(path))
    lo, hi = win.cal_plot.y_range()
    assert lo <= 0.5 and hi >= 1.5 and hi - lo <= 3
    win.cal_plot.resize(300, 150)
    win.cal_plot.grab()
    got = []
    win.acal_requested.connect(lambda *a: got.append(a))
    win.acal_run.click()                                 # поля пустые - только запись в журнал
    assert got == [] and "ACAL" in win.error_log.toPlainText()
    for w, text in ((win.acal_start, "1"), (win.acal_stop, "18"), (win.acal_points, "101")):
        w.setText(text)
        w.setModified(True)
        w._commit()
    win.acal_run.click()
    assert got == [(pytest.approx(1e9), pytest.approx(18e9), 101)]

def test_duty_from_pulse_generator(qtbot, pm, tmp_path):
    proxy, win, _s = pm
    win.duty_src.set_value("CH1")
    assert win.duty_value() is None and win.duty.placeholderText() == "no SDG"
    sdg = _proxy("sdg")
    gen = PulseGenWindow(sdg, QSettings(str(tmp_path / "sdg.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(gen)
    try:
        assert LINKS.find("function_generator") is sdg
        with qtbot.waitSignal(sdg.sync_done, timeout=5000):
            sdg.open()
        sdg.set("ch1.wave", "PULSE")
        sdg.set("ch1.duty", 10.0)
        _wait(qtbot, sdg, "ch1.duty", lambda v: v is not None and v == pytest.approx(10.0, abs=0.5))
        duty = sdg.value("ch1.duty")
        assert duty and win.duty_value() == pytest.approx(duty)
        _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(10 * math.log10(100 / duty), abs=1e-3))
        assert win.duty.isReadOnly()
    finally:
        sdg.shutdown()


def test_zero_needs_confirmation(qtbot, pm):
    proxy, win, _s = pm
    calls = []
    proxy.action_done.connect(lambda name, _r: calls.append(name))
    win.zero_button.click()
    assert win.zero_button.text() == "SURE?" and calls == []
    win.zero_button.click()
    qtbot.waitUntil(lambda: "zero" in calls, timeout=10000)
    assert win.zero_button.text() == "ZERO"


def test_time_domain_trace(qtbot, pm):
    proxy, win, _s = pm
    proxy.set("detector", "AVERAGE")
    _wait(qtbot, proxy, "detector", lambda v: v == "AVERAGE")
    proxy.set("trace.state", True)                      # сам переводит датчик в NORMAL
    _wait(qtbot, proxy, "trace.state", lambda v: v is True)
    _wait(qtbot, proxy, "detector", lambda v: v == "NORMAL")
    proxy.set("trace.time", 2e-3)
    _wait(qtbot, proxy, "trace.time", lambda v: v == pytest.approx(2e-3))
    qtbot.waitUntil(lambda: win.plot.trace is not None and win.plot.trace.x[-1] == pytest.approx(2e-3),
                    timeout=5000)
    t = win.plot.trace
    assert len(t) == 1000 and t.x_unit == "s"
    assert max(t.y) == pytest.approx(-10.0, abs=0.01)    # импульс симулятора -10 dBm
    qtbot.waitUntil(lambda: win.tabs.is_on(3), timeout=5000)
    assert not win.plot.isHidden() and win.readout.isHidden()          # график вместо индикатора
    win.plot.resize(400, 200)
    win.plot.grab()                                     # отрисовка без ошибок
    proxy.set("detector", "AVERAGE")                    # трасса только в NORMAL - выключается
    _wait(qtbot, proxy, "trace.state", lambda v: v is False)
    qtbot.waitUntil(lambda: win.plot.trace is None and not win.tabs.is_on(3), timeout=5000)
    assert win.plot.isHidden() and not win.readout.isHidden()

def test_pm_launcher_with_sdg(qtbot, tmp_path, monkeypatch):
    import run_power_meter
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    made = []
    for cls in (run_power_meter.PowerMeterWindow, run_power_meter.PulseGenWindow):
        orig = cls.__init__

        def track(self, *a, _orig=orig, **k):
            _orig(self, *a, **k)
            made.append(self)
        monkeypatch.setattr(cls, "__init__", track)
    assert run_power_meter.main(["--sim", "--with-sdg", "--ini", str(tmp_path / "x.ini"), "--no-connect"]) == 0
    assert [type(w).__name__ for w in made] == ["PowerMeterWindow", "PulseGenWindow"]
    for w in made:
        w.proxy.shutdown()


def test_time_plot_grows_with_window(qtbot, pm):
    proxy, win, _s = pm
    win.show()
    qtbot.waitExposed(win)
    proxy.set("trace.state", True)
    _wait(qtbot, proxy, "trace.state", lambda v: v is True)
    qtbot.waitUntil(lambda: not win.plot.isHidden(), timeout=5000)
    qtbot.wait(20)
    assert win.minimumHeight() <= win.height() < win.maximumHeight()      # окно растягивается
    h = win.plot.height()
    win.resize(win.width(), win.height() + 120)
    qtbot.wait(20)
    assert win.plot.height() == h + 120                         # лишняя высота - графику
    win.set_expanded(True)
    win.set_page(2)                                             # CAL: два графика делят высоту
    qtbot.wait(20)
    cal = win.cal_plot.height()
    win.resize(win.width(), win.height() + 100)
    qtbot.wait(20)
    assert win.cal_plot.height() > cal and win.plot.height() > h


def test_time_scale_units_and_det_on_main(qtbot, pm):
    """DET - на MAIN, не в TIME; шкала TIME: AUTO / MAN, dBm / W, REF и DIV (Алексей 2026-11-14)."""
    proxy, win, settings = pm
    names = lambda page: [ln.name for ln in win.pages[page].lines]          # noqa: E731
    assert "DET" in names(0) and "DET" not in names(3)
    assert names(3)[-3:] == ["SCAL", "REF", "DIV"]
    proxy.set("trace.state", True)
    _wait(qtbot, proxy, "trace.state", lambda v: v is True)
    qtbot.waitUntil(lambda: win.plot.trace is not None, timeout=5000)
    lo, hi = win.plot.y_range()                                            # импульс -10 dBm: верх -10, 10 dB/дел
    assert win.plot.auto and hi == -10.0 and lo == -70.0
    assert win.scale_ref.value == hi and win.scale_div.value == 10.0      # в AUTO поля - текущая шкала
    win.scale_div.set_value(5.0)                                           # ввели DIV - шкала ручная
    assert not win.plot.auto and win.plot.y_range() == (hi - 30.0, hi) and win.scale_auto.value is False
    win.scale_ref.set_value(-5.0)
    assert win.plot.y_range() == (-35.0, -5.0)
    assert settings.value(f"{win._prefix}/TIME_REF_DBM", type=float) == -5.0
    win.scale_unit.set_value("W")                                          # линейная шкала в ваттах
    win.scale_auto.set_value(True)
    assert win.plot.y_unit() == "W" and max(win.plot.ys()) == pytest.approx(1e-4, rel=1e-3)   # -10 dBm = 100 uW
    lo, hi = win.plot.y_range()
    assert lo == 0.0 and hi >= 1e-4 and win.scale_ref.unit == "mW"
    win.plot.resize(400, 200)
    win.plot.grab()
    from visalab.front import PowerMeterWindow
    win2 = PowerMeterWindow(proxy, settings)                               # из ini
    qtbot.addWidget(win2)
    assert win2.scale_unit.value == "W" and win2.scale_auto.value is True


def test_follow_button_shows_source(qtbot, tmp_path):
    """FREQ на MAIN: кнопка слежения за генератором (вкл по умолчанию, ini), видно за кем."""
    from visalab.coord import COORD
    settings = QSettings(str(tmp_path / "f.ini"), QSettings.Format.IniFormat)
    gen, pm = COORD.open_instrument("e8267d", "sim"), COORD.open_instrument("u2021xa", "sim")
    try:
        win = PowerMeterWindow(pm, settings)
        qtbot.addWidget(win)
        for p in (gen, pm):
            with qtbot.waitSignal(p.sync_done, timeout=5000):
                p.open()
        freq_line = next(ln for ln in win.pages[0].lines if ln.name == "FREQ")
        assert win.follow_button in freq_line.widgets and win.follow_button.isChecked()
        gen.set("rf.frequency", 2.2e9)
        _wait(qtbot, pm, "freq", lambda v: v == pytest.approx(2.2e9))
        qtbot.waitUntil(lambda: win.follow_button.text().startswith("GEN"), timeout=2000)
        assert "←" not in win.follow_button.text()
        win.follow_button.setChecked(False)
        assert settings.value(f"{win._prefix}/FREQ_FOLLOW", type=bool) is False
        assert win.follow_button.text() == "FOLW" and not COORD.follow.enabled(pm)
    finally:
        COORD.close_all([gen, pm])


def test_trigger_level_above_20dbm_with_cal_offset(qtbot, pm):
    """С калибровкой +40 дБ поле LVL принимает уровни выше +20 дБм и запуск по уровню срабатывает."""
    proxy, win, _s = pm
    sim = sim_of(proxy)
    sim.set_signal(on_dbm=5.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    win.cal_dir.mkdir(exist_ok=True)
    save_csv(win.cal_dir / "att40.csv", [(1e6, 40.0), (18e9, 40.0)])
    win.cal_choice.click()
    win.cal_choice.set_value("att40.csv")
    _wait(qtbot, proxy, "offset", lambda v: v == pytest.approx(40.0, abs=1e-3))
    _wait(qtbot, proxy, "offset.state", lambda v: v is True)
    lvl = win.widgets["trig.level"]
    lvl.setValue(35.0)
    lvl.commit(force=True)
    _wait(qtbot, proxy, "trig.level", lambda v: v == pytest.approx(35.0, abs=1e-3))
    proxy.set("gate.start", 20e-6)
    proxy.set("gate.length", 100e-6)
    proxy.set("trig.source", "INT")
    _wait(qtbot, proxy, "trig.source", lambda v: v == "INT")
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None
                    and proxy.value("meas.power") == pytest.approx(45.0, abs=0.1), timeout=5000)
    assert "LEVEL 35 dBm" in win.readout.trig_button.text()


def test_level_wait_indicator_and_bus_button_gating(qtbot, pm):
    """В LEVEL без запуска индикатор показывает --- и WAIT; кнопка TRIG (*TRG) активна только в BUS."""
    proxy, win, _s = pm
    sim = sim_of(proxy)
    assert not win.trig_bus_button.isEnabled()
    sim.set_signal(on_dbm=-10.0, width=None)           # CW -> нет фронта для LEVEL
    proxy.set("trig.level", -15.0)
    proxy.set("trig.source", "INT")
    _wait(qtbot, proxy, "trig.source", lambda v: v == "INT")
    qtbot.waitUntil(lambda: proxy.value("meas.armed") is True and proxy.value("meas.power") is None, timeout=5000)
    qtbot.waitUntil(lambda: "WAIT" in win.readout.trig_button.text() and win.readout.main.text() == "---", timeout=5000)
    assert not win.trig_bus_button.isEnabled()

    # Появились импульсы -> измерение прошло, WAIT пропало
    sim.set_signal(on_dbm=-5.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    qtbot.waitUntil(lambda: proxy.value("meas.power") is not None, timeout=5000)
    qtbot.waitUntil(lambda: "WAIT" not in win.readout.trig_button.text() and win.readout.main.text() != "---", timeout=5000)

    # В режиме BUS кнопка TRIG становится активной
    proxy.set("trig.source", "BUS")
    _wait(qtbot, proxy, "trig.source", lambda v: v == "BUS")
    qtbot.waitUntil(lambda: win.trig_bus_button.isEnabled(), timeout=5000)


def test_restore_cal_table_with_freq_and_high_trig_level(qtbot, pm):
    """restore() применяет CAL(f) для сохранённой частоты до trig.level, чтобы уровень > +20 дБм не отклонялся."""
    proxy, win, settings = pm
    win.cal_dir.mkdir(exist_ok=True)
    save_csv(win.cal_dir / "slope.csv", [(50e6, 0.0), (10e9, 40.0)])
    win.cal_choice.click()
    win.cal_choice.set_value("slope.csv")
    # На 50 МГц поправка 0 дБ, на 10 ГГц поправка +40 дБ; сохраняем частоту 10 ГГц и trig.level = +35 дБм
    settings.setValue(win._settings_key("freq"), 10e9)
    settings.setValue(win._settings_key("trig.level_auto"), False)
    settings.setValue(win._settings_key("trig.level"), 35.0)
    settings.setValue(win._settings_key("trig.source"), "INT")
    win.restore()
    _wait(qtbot, proxy, "freq", lambda v: v is not None and v == pytest.approx(10e9))
    _wait(qtbot, proxy, "offset", lambda v: v is not None and v == pytest.approx(40.0, abs=1e-2))
    _wait(qtbot, proxy, "trig.level", lambda v: v is not None and v == pytest.approx(35.0, abs=1e-2))
    _wait(qtbot, proxy, "trig.source", lambda v: v == "INT")
