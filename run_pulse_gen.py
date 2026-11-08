"""Окно генератора импульсов Siglent SDG (visalab/front/pulse_gen.py).

    python run_pulse_gen.py                 # адрес из lab.json (sdg), иначе симулятор
    python run_pulse_gen.py --sim           # симулятор
    python run_pulse_gen.py --address 192.168.0.5
    python run_pulse_gen.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Драйвер, модель и адрес можно поменять в окне: клик по адресу -> APPLY (сохраняется в ini).
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
from visalab.drivers import DRIVERS, make_transport
from visalab.front import PulseGenWindow
from visalab.front.models import all_models
from visalab.front.pulse_gen import saved_connection

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

    app = QApplication.instance() or QApplication(sys.argv[:1])
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    lab = _load_lab(args.lab).get("sdg", {})
    saved = saved_connection(settings)            # выбранное в окне (клик по адресу -> APPLY)
    kind, model, delay = saved["driver"] or "sdg", saved["model"] or None, None
    if args.sim:
        address = "sim"
    elif args.address:
        address = args.address
    elif saved["address"]:
        address = saved["address"]
    else:
        address = lab.get("address", "sim")
        model, delay = model or lab.get("model"), lab.get("delay")
    current: dict = {}

    def build(kind: str, address: str, model: str | None, pos=None) -> None:
        driver_cls, _ = DRIVERS[kind]
        proxy = InstrumentProxy(driver_cls, make_transport(kind, address, args.backend, args.latency),
                                model=all_models(settings, kind).get(model) if model else None,
                                command_delay_s=None if delay is None else float(delay))
        win = PulseGenWindow(proxy, settings)
        win.reconnect_requested.connect(reconnect)
        if pos is not None:
            win.move(pos)
        win.show()
        current.update(proxy=proxy, win=win)
        if not args.no_connect:
            proxy.open()

    def reconnect(kind: str, address: str, model: str) -> None:
        old_proxy, old_win = current["proxy"], current["win"]
        pos = old_win.pos()
        old_win.hide()
        old_proxy.shutdown()
        old_win.deleteLater()
        build(kind, address, model or None, pos)

    build(kind, address, model, None)
    app.aboutToQuit.connect(lambda: current["proxy"].shutdown())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
