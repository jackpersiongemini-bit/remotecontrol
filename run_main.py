"""Главное окно лаборатории (visalab/front/main_window.py): что сейчас измеряется.

    python run_main.py                 # все приборы из lab.json (адреса из ini окон, иначе lab.json)
    python run_main.py --sim           # все на симуляторах; генератор подаёт сигнал на SA и ваттметр (стенд симуляторов)
    python run_main.py --only pna,e4446a,u2021xa,e8267d
    python run_main.py --show-windows  # показать все окна приборов (иначе - как были при выходе)

Плитка прибора: класс и состояние, индикатор - прибор измеряет или выход включён; клик - окно прибора. Окна приборов
открываются такими, какими были при выходе (скрытые - скрытыми). Идущий сценарий координатора (ACAL из окна
ваттметра) - фишка «ACAL n/N» в той же полосе; при выходе сценарий сначала возвращает приборы.
Панели ниже - приёмники, которые сейчас измеряют (ВАЦ - трейсы, ваттметр - точки, анализатор - спектр).
Прибор без связи переподключается сам (плитка RETRY). Журнал программы - logs/visalab.log рядом с ini;
проверка всех приборов стенда с отчётом - run_check.py.
"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtWidgets import QApplication

from visalab.coord import ActivityMonitor, profile_name
from visalab.front import PowerMeterWindow, PSUWindow, PulseGenWindow, RFGenWindow, SpectrumWindow, VNAWindow
from visalab.front.launch import remember_windows, run_windows
from visalab.front.main_window import MainWindow

#: запись lab.json -> окно; порядок - порядок плиток
WINDOWS = {"pna": VNAWindow, "e4446a": SpectrumWindow, "u2021xa": PowerMeterWindow, "e8267d": RFGenWindow,
           "sdg": PulseGenWindow, "psu": PSUWindow}


def _after_sync(proxy, fn) -> None:
    """Один раз: fn(proxy) после первой синхронизации прибора (подключён и прочитан)."""
    def run():
        proxy.sync_done.disconnect(run)
        fn(proxy)
    proxy.sync_done.connect(run)


def setup(opened, args, settings):
    win = MainWindow(settings, ActivityMonitor())
    win.quit_on_close = True
    QApplication.instance().aboutToQuit.connect(win.monitor.stop)    # до остановки приборов
    win.before_quit.append(lambda: remember_windows(opened, settings))
    win.show()
    if args.sim:
        # Генератор, анализатор и ваттметр на симуляторах стоят на стенде симуляторов сами (visalab/sim/bench.py:
        # тракт 10 dB + 0.2 dB/ГГц - ACAL ваттметра даёт GAIN и наклон таблицы). Здесь - только обзор анализатора
        # вокруг частоты генератора, ключами профиля, когда анализатор подключится.
        for cur in opened:
            if profile_name(cur["proxy"]) == "spectrum_analyzer":
                _after_sync(cur["proxy"], lambda p: (p.set("freq.center", 1e9), p.set("freq.span", 100e6)))
    return {"main": win}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="записи lab.json через запятую")
    ap.add_argument("--show-windows", action="store_true", help="показать все окна приборов")
    known = ap.parse_known_args(argv)[0]
    keys = [k.strip() for k in known.only.split(",") if k.strip()] or list(WINDOWS)
    unknown = [k for k in keys if k not in WINDOWS]
    if unknown:
        ap.error(f"неизвестные приборы: {', '.join(unknown)} (есть: {', '.join(WINDOWS)})")
    specs = [(WINDOWS[k], k) for k in keys]
    return run_windows(specs, __doc__, argv, parser=ap, show=True if known.show_windows else None, setup=setup)


if __name__ == "__main__":
    sys.exit(main())
