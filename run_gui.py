"""Запуск окон приборов.

    python run_gui.py                         # оба прибора на симуляторах
    python run_gui.py --sdg USB0::0xF4EC::0x1102::SDG2XXXX::INSTR --psu sim
    python run_gui.py --psu USB0::0x2EC7::0x6300::800000000000000001::INSTR --only psu
    python run_gui.py --list                  # показать ресурсы VISA

Каждый прибор - своё окно, свой поток, общение только сигналами Qt.
"""
from __future__ import annotations

import argparse
import logging
import sys

from PyQt6.QtWidgets import QApplication

from visalab.core import InstrumentProxy
from visalab.drivers import DRIVERS, make_transport
from visalab.gui.window import InstrumentWindow

TITLES = {"sdg": "Генератор Siglent SDG", "it6300": "Источник питания ITECH IT6300"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdg", default="sim", help="VISA-адрес генератора или 'sim'")
    ap.add_argument("--psu", default="sim", help="VISA-адрес источника или 'sim'")
    ap.add_argument("--only", choices=("sdg", "psu"), help="открыть только один прибор")
    ap.add_argument("--backend", default="", help="бэкенд pyvisa: '' (NI/Keysight VISA) или '@py'")
    ap.add_argument("--latency", type=float, default=0.005, help="задержка симулятора, с")
    ap.add_argument("--no-connect", action="store_true", help="не подключаться при запуске")
    ap.add_argument("--list", action="store_true", help="вывести список ресурсов VISA и выйти")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.list:
        import pyvisa
        print("\n".join(pyvisa.ResourceManager(args.backend).list_resources()) or "ресурсов не найдено")
        return 0

    app = QApplication(sys.argv if argv is None else ["run_gui", *argv])
    wanted = [("sdg", args.sdg), ("it6300", args.psu)]
    if args.only:
        wanted = [w for w in wanted if (w[0] == "sdg") == (args.only == "sdg")]

    windows = []
    for i, (kind, address) in enumerate(wanted):
        driver_cls, _ = DRIVERS[kind]
        proxy = InstrumentProxy(driver_cls, make_transport(kind, address, args.backend, args.latency))
        win = InstrumentWindow(proxy, TITLES[kind])
        win.resize(1000, 720)
        win.move(40 + i * 60, 40 + i * 60)
        win.show()
        if not args.no_connect:
            proxy.open()
        windows.append(win)
    app.aboutToQuit.connect(lambda: [w.proxy.shutdown() for w in windows])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
