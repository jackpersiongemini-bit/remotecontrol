"""Главный запуск лаборатории: окно графиков (``GraphsWindow``) и окно инструментов (``InstrumentsWindow``).

    python run_main.py                 # все приборы из lab.json (адреса из ini окон, иначе lab.json)
    python run_main.py --sim           # все на симуляторах; генератор подаёт сигнал на SA и ваттметр
    python run_main.py --only pna,e4446a,u2021xa,e8267d

При старте открываются два независимых окна:
* ``GraphsWindow`` — главный график слева и док-лента остальных графиков (VNA, SA, PM, PSU CH1..CHn)
  справа с единым ``PlotWidget``, локальными маркерами, памятью, математикой, пределами и анализом;
  для каналов PSU доступно переключение ``TIME`` (V/I/P по времени) и ``FREQ`` (ток от частоты).
* ``InstrumentsWindow`` — карточки приборов с избранным слева и сворачиваемыми разделами настроек
  справа, глобальным поиском и кнопкой ``Sync All``.

Закрытие ``GraphsWindow`` завершает программу (сначала останавливаются сценарии координатора и
приборы возвращаются в исходное состояние). Закрытие ``InstrumentsWindow`` скрывает его; вернуть
можно кнопкой ``INST`` в заголовке ``GraphsWindow``.
"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from visalab.coord import ActivityMonitor, profile_name
from visalab.front import (GraphsWindow, InstrumentsWindow, PowerMeterWindow, PSUWindow, PulseGenWindow, RFGenWindow,
                           SpectrumWindow, VNAWindow)
from visalab.front.launch import run_windows

#: запись lab.json -> класс окна прибора; порядок - порядок карточек
WINDOWS = {
    "pna": VNAWindow,
    "e4446a": SpectrumWindow,
    "u2021xa": PowerMeterWindow,
    "e8267d": RFGenWindow,
    "sdg": PulseGenWindow,
    "psu": PSUWindow,
}
#: запись lab.json -> имя слота в InstrumentsWindow
SLOTS = {
    "pna": "VNA",
    "e4446a": "SA",
    "u2021xa": "PM",
    "e8267d": "GEN",
    "sdg": "PULSE",
    "psu": "PSU",
}


def _after_sync(proxy, fn) -> None:
    """Один раз: fn(proxy) после первой синхронизации прибора (подключён и прочитан)."""
    def run():
        proxy.sync_done.disconnect(run)
        fn(proxy)
    proxy.sync_done.connect(run)


def setup(opened, args, settings):
    monitor = ActivityMonitor()
    slots = tuple(SLOTS[cur["lab_key"]] for cur in opened)

    inst_win = InstrumentsWindow(settings, instruments=slots)
    inst_win.hide_on_close = True
    for cur in opened:
        slot = SLOTS[cur["lab_key"]]
        inst_win.attach_instrument(slot, cur["win"])
        cur["on_reconnected"] = (lambda new_win, s=slot: inst_win.attach_instrument(s, new_win))

    graphs_win = GraphsWindow(settings, monitor, instruments_window=inst_win)
    inst_win.bind_graphs_window(graphs_win)
    graphs_win.quit_on_close = True
    QApplication.instance().aboutToQuit.connect(monitor.stop)    # до остановки приборов

    # На первом запуске (пока позиции не сохранены) разносим два окна так, чтобы оба были видны
    screen = QGuiApplication.primaryScreen()
    if screen is not None and not graphs_win._pos_restored and not inst_win._pos_restored:
        avail = screen.availableGeometry()
        gap = 12
        if avail.width() >= graphs_win.minimumWidth() + inst_win.minimumWidth() + 3 * gap:
            gw_w = int((avail.width() - 3 * gap) * 0.56)
            iw_w = avail.width() - 3 * gap - gw_w
            h = max(480, avail.height() - 2 * gap)
            graphs_win.resize(gw_w, h)
            inst_win.resize(iw_w, h)
            graphs_win.move(QPoint(avail.left() + gap, avail.top() + gap))
            inst_win.move(QPoint(avail.left() + 2 * gap + gw_w, avail.top() + gap))
        else:
            gw_w = min(max(graphs_win.minimumWidth(), int(avail.width() * 0.78)), avail.width() - 24)
            gw_h = min(max(graphs_win.minimumHeight(), int(avail.height() * 0.84)), avail.height() - 24)
            iw_w = min(max(inst_win.minimumWidth(), int(avail.width() * 0.74)), avail.width() - 24)
            iw_h = min(max(inst_win.minimumHeight(), int(avail.height() * 0.82)), avail.height() - 24)
            graphs_win.resize(gw_w, gw_h)
            inst_win.resize(iw_w, iw_h)
            graphs_win.move(QPoint(avail.left() + gap, avail.top() + gap))
            inst_win.move(QPoint(avail.right() - iw_w - gap, avail.bottom() - iw_h - gap))

    inst_win.show()
    graphs_win.show_window()
    graphs_win.raise_()
    graphs_win.activateWindow()

    if args.sim:
        # Генератор, анализатор и ваттметр на симуляторах стоят на стенде симуляторов сами (visalab/sim/bench.py).
        # Здесь — только обзор анализатора вокруг частоты генератора, когда анализатор подключится.
        for cur in opened:
            if profile_name(cur["proxy"]) == "spectrum_analyzer":
                _after_sync(cur["proxy"], lambda p: (p.set("freq.center", 1e9), p.set("freq.span", 100e6)))
    return {"graphs": graphs_win, "instruments": inst_win, "monitor": monitor}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="записи lab.json через запятую")
    known = ap.parse_known_args(argv)[0]
    keys = [k.strip() for k in known.only.split(",") if k.strip()] or list(WINDOWS)
    unknown = [k for k in keys if k not in WINDOWS]
    if unknown:
        ap.error(f"неизвестные приборы: {', '.join(unknown)} (есть: {', '.join(WINDOWS)})")
    specs = [(WINDOWS[k], k) for k in keys]
    return run_windows(specs, __doc__, argv, parser=ap, show=False, setup=setup)


if __name__ == "__main__":
    sys.exit(main())
