"""Проверка стенда одной командой: приборы подключаются так же, как в окнах (адреса - из ini окон, иначе
lab.json), читаются все параметры, несколько секунд идёт опрос. Итог - отчёт: на что прибор не ответил,
чего у модели нет, что не разобрано, ошибки в очереди прибора (SYST:ERR?), время подключения и чтения.

    python run_check.py                  # все приборы главного окна
    python run_check.py e4446a pna       # только эти
    python run_check.py --poll 20        # опрашивать 20 с (по умолчанию 5)
    python run_check.py --sim            # на симуляторах (проверить саму проверку)

В приборы пишется только то же, что при любом подключении (*CLS, *ESE). Отчёт - в консоль и в файл
logs/check_<дата_время>.txt рядом с ini: его можно прислать целиком. Код выхода 0 - все приборы
подключились и не потеряли связь.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QSettings

from run_main import WINDOWS
from visalab.applog import FORMAT, close_logging, default_log_path, setup_logging
from visalab.check import format_report, run_check
from visalab.core import InstrumentProxy
from visalab.drivers import DRIVERS, make_transport
from visalab.front.launch import ROOT, connection_for
from visalab.front.models import all_models


def _quiet_console() -> None:
    """В консоли - только отчёт; подробности - в журнале (logs/visalab.log). Вывод в файл на Windows идёт в
    кодировке системы (cp1251): знак, которого в ней нет, - замена, а не ошибка (отчёт целиком - в файле).
    Вызывать до ``setup_logging``: строка «старт» - уже только в журнал."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=FORMAT)
    for h in root.handlers:
        if type(h) is logging.StreamHandler:
            h.setLevel(logging.ERROR)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("only", nargs="*", metavar="ПРИБОР", help=f"записи lab.json: {', '.join(WINDOWS)}")
    ap.add_argument("--sim", action="store_true", help="симуляторы вместо приборов")
    ap.add_argument("--lab", default=str(ROOT / "lab.json"))
    ap.add_argument("--ini", default=str(ROOT / "visalab_front.ini"))
    ap.add_argument("--backend", default="", help="библиотека VISA для pyvisa (\"\" - системная, @py - pyvisa-py)")
    ap.add_argument("--poll", type=float, default=5.0, help="сколько опрашивать после чтения всех параметров, с")
    ap.add_argument("--timeout", type=float, default=60.0, help="сколько ждать подключения и чтения, с")
    ap.add_argument("--out", metavar="FILE", help="отчёт (по умолчанию logs/check_<дата_время>.txt рядом с ini)")
    ap.add_argument("--log", metavar="FILE", help="журнал программы (по умолчанию logs/visalab.log рядом с ini)")
    args = ap.parse_args(argv)
    keys = args.only or list(WINDOWS)
    unknown = [k for k in keys if k not in WINDOWS]
    if unknown:
        ap.error(f"неизвестные приборы: {', '.join(unknown)} (есть: {', '.join(WINDOWS)})")

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])  # noqa: F841 - цикл событий потоков
    _quiet_console()
    log_file = setup_logging(args.log or default_log_path(args.ini))
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    targets = []
    for key in keys:
        conn = connection_for(WINDOWS[key], key, args.lab, settings, sim=args.sim)
        kind = conn["kind"]
        proxy = InstrumentProxy(DRIVERS[kind][0], make_transport(kind, conn["address"], args.backend),
                                model=all_models(settings, kind).get(conn["model"]) if conn["model"] else None,
                                command_delay_s=None if conn["delay"] is None else float(conn["delay"]))
        targets.append((key, proxy))
    try:
        results = run_check(targets, poll_s=args.poll, timeout_s=args.timeout)
    finally:
        InstrumentProxy.shutdown_all(p for _k, p in targets)
    head = [f"Проверка приборов {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "python run_check.py " + " ".join(sys.argv[1:] if argv is None else argv),
            f"ini: {Path(args.ini).resolve()}",
            f"lab: {Path(args.lab).resolve()}"]
    text = format_report(results, head)
    out = Path(args.out) if args.out else default_log_path(args.ini).parent / f"check_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Отчёт: {out}")
    logging.getLogger("visalab").info("проверка стенда: %s", out)
    close_logging(log_file)
    return 0 if all(r.status != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
