"""Окно генератора импульсов Siglent SDG (visalab/front/pulse_gen.py).

    python run_pulse_gen.py                 # адрес из lab.json (sdg), иначе симулятор
    python run_pulse_gen.py --sim           # симулятор
    python run_pulse_gen.py --address 192.168.0.5
    python run_pulse_gen.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from run_gui import _load_lab
from visalab.core import InstrumentProxy
from visalab.drivers import DRIVERS, MODELS, make_transport
from visalab.front import PulseGenWindow

HERE = Path(__file__).resolve().parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", metavar="ADDR")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--lab", default=str(HERE / "lab.json"))
    ap.add_argument("--ini", default=str(HERE / "visalab_front.ini"))
    ap.add_argument("--backend", default="")
    ap.add_argument("--latency", type=float, default=0.005, help="задержка симулятора, с")
    ap.add_argument("--no-connect", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    lab = _load_lab(args.lab).get("sdg", {})
    address, model, delay = args.address, None, None
    if args.sim:
        address = "sim"
    elif address is None:
        address = lab.get("address", "sim")
        model, delay = lab.get("model"), lab.get("delay")

    app = QApplication(sys.argv[:1])
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    driver_cls, _ = DRIVERS["sdg"]
    proxy = InstrumentProxy(driver_cls, make_transport("sdg", address, args.backend, args.latency),
                            model=MODELS["sdg"].get(model) if model else None,
                            command_delay_s=None if delay is None else float(delay))
    win = PulseGenWindow(proxy, settings)
    win.show()
    if not args.no_connect:
        proxy.open()
    app.aboutToQuit.connect(proxy.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
