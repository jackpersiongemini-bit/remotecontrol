"""Главное окно лаборатории (visalab/front/main_window.py): что сейчас измеряется.

    python run_main.py                 # все приборы из lab.json (адреса из ini окон, иначе lab.json)
    python run_main.py --sim           # все на симуляторах; генератор подаёт сигнал на SA и ваттметр
    python run_main.py --only pna,e4446a,u2021xa,e8267d
    python run_main.py --hide-windows  # окна приборов скрыты, открываются кликом по плитке

Открываются главное окно и окна всех приборов. Плитка прибора: класс и состояние, индикатор - прибор
измеряет или выход включён; клик - поднять окно прибора.
Панели ниже - приёмники, которые сейчас измеряют (ВАЦ - трейсы, ваттметр - точки, анализатор - спектр).
"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtCore import QTimer

from visalab.coord import ActivityMonitor
from visalab.front import PowerMeterWindow, PSUWindow, PulseGenWindow, RFGenWindow, SpectrumWindow, VNAWindow
from visalab.front.launch import run_windows
from visalab.front.main_window import MainWindow

#: запись lab.json -> окно; порядок - порядок плиток
WINDOWS = {"pna": VNAWindow, "e4446a": SpectrumWindow, "u2021xa": PowerMeterWindow, "e8267d": RFGenWindow,
           "sdg": PulseGenWindow, "psu": PSUWindow}


def _sim(proxy):
    return getattr(getattr(proxy._worker, "transport", None), "sim", None)


def setup(opened, args, settings):
    win = MainWindow(settings, ActivityMonitor())
    win.quit_on_close = True
    win.show()
    keep = {"main": win}
    if args.sim:                                       # стенд: генератор -> анализатор и ваттметр
        from visalab.coord import profile_name
        from visalab.sim.bench import SimBench
        by = {}
        for cur in opened:
            sim = _sim(cur["proxy"])
            if sim is not None:
                by.setdefault(profile_name(cur["proxy"]), []).append(sim)
        for sa in by.get("spectrum_analyzer", []):     # обзор вокруг частоты генератора по умолчанию
            sa.c.update(center=1e9, span=100e6)
            sa._couple()
        bench = SimBench(by.get("signal_generator", []), by.get("spectrum_analyzer", []), by.get("power_meter", []))
        timer = QTimer(win)
        timer.setInterval(200)
        timer.timeout.connect(bench.step)
        timer.start()
        keep["bench"] = bench
    return keep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="записи lab.json через запятую")
    ap.add_argument("--hide-windows", action="store_true", help="окна приборов скрыты (открываются по плитке)")
    known = ap.parse_known_args(argv)[0]
    keys = [k.strip() for k in known.only.split(",") if k.strip()] or list(WINDOWS)
    unknown = [k for k in keys if k not in WINDOWS]
    if unknown:
        ap.error(f"неизвестные приборы: {', '.join(unknown)} (есть: {', '.join(WINDOWS)})")
    specs = [(WINDOWS[k], k) for k in keys]
    return run_windows(specs, __doc__, argv, parser=ap, show=not known.hide_windows, setup=setup)


if __name__ == "__main__":
    sys.exit(main())
