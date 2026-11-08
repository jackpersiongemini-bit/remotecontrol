"""Запуск окна прибора: адрес из аргументов / ini окна / lab.json, пересоздание по APPLY."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from ..core import InstrumentProxy
from ..drivers import DRIVERS, make_transport
from .instrument_window import saved_connection
from .models import all_models

__all__ = ["run_window"]

ROOT = Path(__file__).resolve().parents[2]


def _lab(path: str, key: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    entry = data.get(key, {})
    return entry if isinstance(entry, dict) else {}


def run_window(window_cls, lab_key: str, doc: str = "", argv=None) -> int:
    """``window_cls`` - наследник InstrumentWindow; ``lab_key`` - запись прибора в lab.json."""
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", metavar="ADDR")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--lab", default=str(ROOT / "lab.json"))
    ap.add_argument("--ini", default=str(ROOT / "visalab_front.ini"))
    ap.add_argument("--backend", default="")
    ap.add_argument("--latency", type=float, default=0.005, help="задержка симулятора, с")
    ap.add_argument("--no-connect", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = QApplication.instance() or QApplication(sys.argv[:1])
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    lab = _lab(args.lab, lab_key)
    saved = saved_connection(settings, window_cls.CONNECTION_GROUP)     # выбранное в окне (адрес -> APPLY)
    kind, model, delay = saved["driver"] or window_cls.DEFAULT_KIND, saved["model"] or None, None
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
        win = window_cls(proxy, settings)
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
