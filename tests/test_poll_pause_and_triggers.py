"""Тесты настройки паузы непрерывного считывания (POLL) и строгого ожидания сигнала окончания
измерения от прибора в триггерных и уровневых режимах (VNA, SA, PM, PSU)."""
from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QSettings

from conftest import flush, worker
from visalab.coord import Instrument
from visalab.core import PStatus
from visalab.core.units import si_format
from visalab.drivers import AgilentPSA, KeysightPNA, KeysightU2021XA, default_poll_pause
from visalab.front import PowerMeterWindow, SpectrumWindow, VNAWindow
from visalab.front.instrument_window import saved_poll_pause
from visalab.front.launch import connection_for
from visalab.instruments import InstrumentProxy
from visalab.sim import AgilentPSASim, KeysightPNASim, KeysightU2021XASim
from visalab.transport import SimTransport


def _setp(drv, rec, key, value):
    drv.set_param(key, value)
    flush(drv)
    return rec.last_status(key)[0]


# ------------------------------------------------------------------ 1. VNA (Keysight PNA)
def test_pna_continuous_pause_and_strict_triggered_completion(make):
    drv, sim, rec = make(KeysightPNA, KeysightPNASim)
    assert not drv.is_triggered_mode()
    assert drv._timer.isSingleShot()

    # Пауза непрерывного считывания 2.5 с применяется в режиме IMM
    drv.set_read_pause(2.5)
    assert drv._effective_poll_ms() == 2500
    assert drv._timer.interval() == 2500
    assert drv._cont_fallback_s() == pytest.approx(2.5)

    # Переключение в внешний триггер EXT:
    # 1) таймер опроса сигнала готовности не замедляется паузой непрерывного чтения (200 мс вместо 2500 мс)
    # 2) даже при истёкшем времени (_last_cont_read = 0) данные НЕ выдаются без сигнала от прибора
    assert _setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN
    assert drv.is_triggered_mode()
    assert drv._effective_poll_ms() == KeysightPNA.POLL_INTERVAL_MS
    assert drv._timer.interval() == KeysightPNA.POLL_INTERVAL_MS

    emitted: list = []
    drv.param_updated.connect(lambda k, v: emitted.append(v) if k == "s11.trace" else None)

    drv._last_cont_read = 0.0
    for _ in range(5):
        drv.poll()
    assert emitted == []  # без триггера — ни одного ложного чтения

    # Пришёл внешний триггер -> прибор закончил развёртку (STAT:OPER:EVEN бит 4) -> данные выданы
    sim.trigger()
    drv.poll()
    assert len(emitted) == 1

    # Снова нет триггера — снова тишина даже при _last_cont_read = 0
    drv._last_cont_read = 0.0
    for _ in range(5):
        drv.poll()
    assert len(emitted) == 1

    # Ручной запуск MAN: тоже строго по сигналу прибора
    assert _setp(drv, rec, "trig.source", "MAN") is PStatus.WRITTEN
    drv._last_cont_read = 0.0
    for _ in range(4):
        drv.poll()
    assert len(emitted) == 1
    drv.run_action("trigger")
    drv.poll()
    assert len(emitted) == 2

    # Возврат в IMM возвращает паузу 2.5 с
    assert _setp(drv, rec, "trig.source", "IMM") is PStatus.WRITTEN
    assert not drv.is_triggered_mode()
    assert drv._effective_poll_ms() == 2500
    assert drv._timer.interval() == 2500


def test_pna_triggered_without_oper_uses_opc_never_timer(make):
    """Если STAT:OPER:EVEN? не поддерживается прошивкой, в триггерном режиме PNA ждёт *OPC, а не таймер."""
    drv, sim, rec = make(KeysightPNA, KeysightPNASim)
    drv._oper_supported = False
    assert _setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN

    emitted: list = []
    drv.param_updated.connect(lambda k, v: emitted.append(v) if k == "s11.trace" else None)

    drv._last_cont_read = 0.0
    for _ in range(5):
        drv.poll()
    assert emitted == []
    assert drv._opc_pending is True

    sim.trigger()
    drv.poll()
    assert len(emitted) == 1
    assert drv._opc_pending is True  # сразу взведён на следующий триггер

    # Без второго триггера снова не отдаёт
    drv._last_cont_read = 0.0
    for _ in range(3):
        drv.poll()
    assert len(emitted) == 1


def test_pna_connecting_already_in_ext_does_not_emit_pretrigger_trace(make):
    sim = KeysightPNASim()
    sim.trig["sour"] = "EXT"
    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert rec.values["trig.source"] == "EXT"
    assert rec.values.get("s11.trace") is None
    drv._last_cont_read = 0.0
    for _ in range(3):
        drv.poll()
    assert rec.values.get("s11.trace") is None
    sim.trigger()
    drv.poll()
    assert rec.values.get("s11.trace") is not None


def test_pna_real_hardware_int_trig_and_zero_oper_reads_traces(make):
    """На реальном PNA-L N5231A TRIG:SOUR? может вернуть 'INT'/'INTERNAL', а STAT:OPER:EVEN? всегда '0':
    непрерывное чтение трасс и триггерные EXT/MAN (*OPC) должны работать без зависаний."""
    sim = KeysightPNASim()
    sim.on("TRIGger[:SEQuence]:SOURce",
           sim._tenum("sour", {"IMM": "IMM", "IMMEDIATE": "IMM", "INT": "IMM", "INTERNAL": "IMM",
                               "EXT": "EXT", "EXTERNAL": "EXT", "MAN": "MAN", "MANUAL": "MAN"}),
           lambda a, g: {"IMM": "INT", "EXT": "EXT", "MAN": "MAN"}[sim.trig["sour"]])
    sim.on("STATus:OPERation[:EVENt]", get=lambda a, g: "0")

    drv, sim, rec = make(KeysightPNA, KeysightPNASim, sim=sim)
    assert rec.values["trig.source"] == "IMM"
    assert not drv.is_triggered_mode()
    first = rec.values.get("s11.trace")
    assert first is not None and not rec.errors

    # Повторный опрос в CONT читает новую трассу через фоллбэк даже при STAT:OPER:EVEN? == 0
    drv._last_cont_read = 0.0
    drv.poll()
    assert rec.values["s11.trace"] is not first and not rec.errors

    # В EXT / MAN при STAT:OPER:EVEN? == 0 (но _oper_supported == True!) ждём *OPC, а не застреваем на бите 4
    assert drv._oper_supported is True
    assert _setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN
    assert drv.is_triggered_mode()
    emitted: list = []
    drv.param_updated.connect(lambda k, v: emitted.append(v) if k == "s11.trace" else None)
    drv._last_cont_read = 0.0
    for _ in range(4):
        drv.poll()
    assert emitted == []
    sim.trigger()
    drv.poll()
    assert len(emitted) == 1

    assert _setp(drv, rec, "trig.source", "MAN") is PStatus.WRITTEN
    for _ in range(3):
        drv.poll()
    assert len(emitted) == 1
    drv.run_action("trigger")
    drv.poll()
    assert len(emitted) == 2

    # Отсутствующий / None trig.source не должен блокировать непрерывное чтение
    drv._values["trig.source"] = None
    assert not drv._is_triggered()


# ------------------------------------------------------------------ 2. Ваттметр (Keysight U2021XA)
def test_pm_continuous_pause_and_level_trigger_fast_opc(make):
    drv, sim, rec = make(KeysightU2021XA, KeysightU2021XASim)
    drv.set_read_pause(1.5)
    assert not drv.is_triggered_mode()
    assert drv._effective_poll_ms() == 1500
    assert drv._timer.interval() == 1500

    # Запуск по уровню (INT) с непрерывным сигналом (нет фронта -> нет срабатывания)
    sim.set_signal(on_dbm=0.0, width=None)
    assert _setp(drv, rec, "trig.level", -10.0) is PStatus.WRITTEN
    assert _setp(drv, rec, "trig.source", "INT") is PStatus.WRITTEN
    assert drv.is_triggered_mode()
    assert drv._effective_poll_ms() == KeysightU2021XA.POLL_INTERVAL_MS
    assert drv._timer.interval() == KeysightU2021XA.POLL_INTERVAL_MS

    readings: list = []
    drv.param_updated.connect(lambda k, v: readings.append(v) if k == "meas.power" and v is not None else None)
    for _ in range(5):
        drv.poll()
    assert readings == []

    # Появился импульс -> фронт через уровень -> *OPC -> данные выданы
    sim.set_signal(width=200e-6, period=1e-3)
    drv.poll()
    assert len(readings) == 1 and readings[0] == pytest.approx(0.0, abs=0.05)

    # Внешний триггер EXT: тоже только по *OPC
    assert _setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN
    for _ in range(4):
        drv.poll()
    assert len(readings) == 1
    sim.fire_external()
    drv.poll()
    assert len(readings) == 2

    # Возврат в IMM возвращает паузу 1.5 с
    assert _setp(drv, rec, "trig.source", "IMM") is PStatus.WRITTEN
    assert not drv.is_triggered_mode()
    assert drv._effective_poll_ms() == 1500


# ------------------------------------------------------------------ 3. Анализатор спектра (Agilent PSA)
def test_sa_continuous_pause_and_video_ext_trigger_opc(make):
    drv, sim, rec = make(AgilentPSA, AgilentPSASim)
    drv.set_read_pause(3.0)
    assert not drv.is_triggered_mode()
    assert drv._effective_poll_ms() == 3000
    assert drv._timer.interval() == 3000

    # Видеозапуск по уровню (VIDEO): быстрый опрос *OPC, данные строго после триггера
    assert _setp(drv, rec, "trig.source", "VIDEO") is PStatus.WRITTEN
    assert drv.is_triggered_mode()
    assert drv._effective_poll_ms() == AgilentPSA.POLL_INTERVAL_MS
    assert drv._timer.interval() == AgilentPSA.POLL_INTERVAL_MS

    traces: list = []
    drv.param_updated.connect(lambda k, v: traces.append(v) if k == "trace" and v is not None else None)
    for _ in range(5):
        drv.poll()
    assert traces == []

    sim.fire_trigger()
    drv.poll()
    assert len(traces) == 1

    # Внешний триггер EXT
    assert _setp(drv, rec, "trig.source", "EXT") is PStatus.WRITTEN
    for _ in range(4):
        drv.poll()
    assert len(traces) == 1
    sim.fire_trigger()
    drv.poll()
    assert len(traces) == 2

    # Возврат в FREE восстанавливает паузу 3.0 с
    assert _setp(drv, rec, "trig.source", "FREE") is PStatus.WRITTEN
    assert not drv.is_triggered_mode()
    assert drv._effective_poll_ms() == 3000


# ------------------------------------------------------------------ 4. Реальный поток + таймер
def test_threaded_read_pause_does_not_delay_triggered_mode(qtbot):
    sim = KeysightU2021XASim()
    sim.set_signal(on_dbm=-5.0, width=200e-6, period=1e-3, off_dbm=-60.0)
    proxy = Instrument(InstrumentProxy(KeysightU2021XA, SimTransport(sim)))
    try:
        with qtbot.waitSignal(proxy.sync_done, timeout=5000):
            proxy.open()
        # Ставим большую паузу непрерывного чтения (5 с)
        proxy.set_read_pause(5.0)
        qtbot.waitUntil(lambda: worker(proxy).read_pause_s == pytest.approx(5.0), timeout=3000)

        # Переходим в внешний триггер EXT: триггер должен прийти быстро (< 1.5 с), а не ждать 5 с!
        proxy.set("trig.source", "EXT")
        qtbot.waitUntil(lambda: proxy.value("trig.source") == "EXT", timeout=3000)
        readings: list = []
        proxy.param_updated.connect(lambda k, v: readings.append(v) if k == "meas.power" and v is not None else None)

        t0 = time.monotonic()
        sim.fire_external()
        qtbot.waitUntil(lambda: len(readings) >= 1, timeout=2000)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5
    finally:
        proxy.shutdown()


# ------------------------------------------------------------------ 5. UI: поле POLL на вкладке SET и launch.py
def test_poll_pause_ui_and_persistence(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "poll.ini"), QSettings.Format.IniFormat)
    proxy = Instrument(InstrumentProxy(KeysightU2021XA, SimTransport(KeysightU2021XASim())))
    win = PowerMeterWindow(proxy, settings)
    qtbot.addWidget(win)
    try:
        names = [ln.name for ln in win.connection_page.lines]
        assert "POLL" in names
        assert win.poll_edit.text() == ""
        assert win.poll_edit.placeholderText() == f"default {si_format(default_poll_pause('u2021xa'), 's', 4)}"

        # Задать 750 мс
        win.poll_edit.setText("750m")
        assert win.apply_poll_pause()
        assert saved_poll_pause(settings, win.CONNECTION_GROUP) == pytest.approx(0.75)
        assert win.poll_edit.text() == "750 ms"
        qtbot.waitUntil(lambda: worker(proxy).read_pause_s == pytest.approx(0.75), timeout=3000)

        # Некорректный ввод (< 50 мс или мусор) откатывается
        win.poll_edit.setText("10m")
        assert not win.apply_poll_pause() and win.poll_edit.text() == "750 ms"
        win.poll_edit.setText("junk")
        assert not win.apply_poll_pause() and win.poll_edit.text() == "750 ms"

        # connection_for подхватывает сохранённую паузу
        conn = connection_for(PowerMeterWindow, "u2021xa", str(tmp_path / "no_lab.json"), settings, sim=True)
        assert conn["poll_pause"] == pytest.approx(0.75)

        # Пустое значение сбрасывает к умолчанию драйвера
        win.poll_edit.setText("")
        assert win.apply_poll_pause()
        assert saved_poll_pause(settings, win.CONNECTION_GROUP) is None
        qtbot.waitUntil(lambda: worker(proxy).read_pause_s == pytest.approx(default_poll_pause("u2021xa")), timeout=3000)
    finally:
        win.cleanup()
        win.close()
        proxy.shutdown()
