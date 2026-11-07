"""Временный фронтенд: все приборы в одном окне (visalab/quick.py).

    python run_quick.py                       # приборы из lab.json: SDG, источник, СВЧ-генератор, ваттметр
    python run_quick.py --sim                 # все четыре на симуляторах
    python run_quick.py --psu sim --sdg lab   # выборочно: адрес, 'lab' (из lab.json) или 'sim'
    python run_quick.py --rf smf100a=192.168.0.3   # СВЧ-генератор другой модели
    python run_quick.py --ini my.ini          # другой файл настроек

Настройки (пресеты SDG, обход блокировки CH1, положение окна) - в ini-файле QSettings,
по умолчанию visalab_quick.ini рядом со скриптом.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from run_gui import DEVICES, _load_lab
from visalab.core import InstrumentProxy
from visalab.drivers import DRIVERS, MODELS, make_transport
from visalab.quick import WINDOWS, LabWindow

HERE = Path(__file__).resolve().parent
#: ключ командной строки -> имя прибора в DEVICES / lab.json
ROLES = {"sdg": "sdg", "psu": "psu", "rf": None, "pm": "u2021xa"}
RF_NAMES = ("e8267d", "smf100a")


def _resolve_rf(arg: str | None, lab: dict) -> tuple[str, str | None]:
    """--rf [модель=]адрес -> (имя прибора, адрес)."""
    if arg and "=" in arg:
        name, _, addr = arg.partition("=")
        if name not in RF_NAMES:
            raise SystemExit(f"--rf: модель из {RF_NAMES}, а не {name!r}")
        return name, addr
    name = next((n for n in RF_NAMES if n in lab), "e8267d")
    return name, arg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdg", metavar="ADDR")
    ap.add_argument("--psu", metavar="ADDR")
    ap.add_argument("--rf", metavar="[MODEL=]ADDR", help="СВЧ-генератор, модель e8267d или smf100a")
    ap.add_argument("--pm", metavar="ADDR", help="датчик мощности U2021XA")
    ap.add_argument("--sim", action="store_true", help="все приборы на симуляторах")
    ap.add_argument("--lab", default=str(HERE / "lab.json"))
    ap.add_argument("--ini", default=str(HERE / "visalab_quick.ini"))
    ap.add_argument("--backend", default="")
    ap.add_argument("--latency", type=float, default=0.005, help="задержка симулятора, с")
    ap.add_argument("--no-connect", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    lab = _load_lab(args.lab)
    rf_name, rf_addr = _resolve_rf(args.rf, lab)
    wanted = {"sdg": args.sdg, "psu": args.psu, rf_name: rf_addr, "u2021xa": args.pm}
    if args.sim:
        wanted = {k: "sim" for k in wanted}
    elif all(v is None for v in wanted.values()):
        wanted = {k: ("lab" if k in lab else "sim") for k in wanted}
    else:
        wanted = {k: v for k, v in wanted.items() if v is not None}

    app = QApplication(sys.argv[:1])
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    panels = []
    for name, address in wanted.items():
        kind, _title, model_name = DEVICES[name]
        delay = None
        if address == "lab":
            if name not in lab:
                raise SystemExit(f"{name}: нет адреса в {args.lab}")
            address = lab[name]["address"]
            model_name = lab[name].get("model", model_name)
            delay = lab[name].get("delay")
        driver_cls, _ = DRIVERS[kind]
        proxy = InstrumentProxy(driver_cls, make_transport(kind, address, args.backend, args.latency),
                                model=MODELS[kind].get(model_name) if model_name else None,
                                command_delay_s=None if delay is None else float(delay))
        panels.append(WINDOWS[kind](proxy, title=_english(kind), settings=settings))
    win = LabWindow(panels, settings)
    win.show()
    if not args.no_connect:
        for p in panels:
            p.proxy.open()
    app.aboutToQuit.connect(lambda: [p.proxy.shutdown() for p in panels])
    return app.exec()


def _english(kind: str) -> str:
    return {"sdg": "Function Generator (SDG)", "it6300": "Power Supply (IT6300)",
            "e8267d": "RF Generator (E8267D)", "smf100a": "RF Generator (SMF100A)",
            "u2021xa": "Power Meter (U2021XA)"}[kind]


if __name__ == "__main__":
    sys.exit(main())
