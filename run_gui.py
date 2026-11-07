"""Запуск окон приборов.

    python run_gui.py                                   # генератор SDG и источник на симуляторах
    python run_gui.py --sdg USB0::0xF4EC::0x1102::SDG2XXXX::INSTR --psu sim
    python run_gui.py --e8267d sim --smf100a sim        # СВЧ-генераторы на симуляторах
    python run_gui.py --sdg 192.168.0.3                 # по LAN: = TCPIP0::192.168.0.3::INSTR
    python run_gui.py --e8267d GPIB0::19::INSTR --model e8267d=E8267D-544
    python run_gui.py --delay sdg=0.1                   # пауза между командами, с
    python run_gui.py --list                            # показать ресурсы VISA

Каждый прибор - своё окно, свой поток, общение только сигналами Qt.
Если ни один прибор не указан, открываются SDG и источник на симуляторах.
"""
from __future__ import annotations

import argparse
import logging
import sys

from PyQt6.QtWidgets import QApplication

from visalab.core import InstrumentProxy
from visalab.drivers import DRIVERS, MODELS, make_transport
from visalab.gui.window import InstrumentWindow

#: ключ командной строки -> тип драйвера, заголовок окна, модель по умолчанию
DEVICES = {
    "sdg": ("sdg", "Генератор Siglent SDG", None),
    "psu": ("it6300", "Источник питания ITECH IT6300", "IT6322"),
    "e8267d": ("e8267d", "СВЧ-генератор Keysight E8267D", None),
    "smf100a": ("smf100a", "СВЧ-генератор R&S SMF100A", None),
    "u2021xa": ("u2021xa", "Датчик мощности Keysight U2021XA", None),
}


def _pairs(items: list[str], what: str) -> dict[str, str]:
    out = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or name not in DEVICES:
            raise SystemExit(f"--{what}: ожидается ПРИБОР=ЗНАЧЕНИЕ, прибор из {sorted(DEVICES)}: {item!r}")
        out[name] = value
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, (kind, title, _) in DEVICES.items():
        ap.add_argument(f"--{name}", metavar="АДРЕС", help=f"{title}: VISA-адрес, IP (192.168.0.3 или 192.168.0.3:5025) или 'sim'")
    ap.add_argument("--model", action="append", default=[], metavar="ПРИБОР=МОДЕЛЬ",
                    help="модель (пределы проверки): " + "; ".join(
                        f"{n}: {', '.join(sorted(MODELS[k]))}" for n, (k, _, _) in DEVICES.items()))
    ap.add_argument("--sdg-model", help="то же, что --model sdg=МОДЕЛЬ")
    ap.add_argument("--psu-model", help="то же, что --model psu=МОДЕЛЬ")
    ap.add_argument("--delay", action="append", default=[], metavar="ПРИБОР=СЕК",
                    help="пауза между командами прибору (по умолчанию: sdg 0.05, остальные 0)")
    ap.add_argument("--sdg-delay", type=float, help="то же, что --delay sdg=СЕК")
    ap.add_argument("--psu-delay", type=float, help="то же, что --delay psu=СЕК")
    ap.add_argument("--only", choices=sorted(DEVICES), help="открыть только этот прибор")
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

    models = _pairs(args.model, "model")
    if args.sdg_model:
        models["sdg"] = args.sdg_model
    if args.psu_model:
        models["psu"] = args.psu_model
    delays = {k: float(v) for k, v in _pairs(args.delay, "delay").items()}
    if args.sdg_delay is not None:
        delays["sdg"] = args.sdg_delay
    if args.psu_delay is not None:
        delays["psu"] = args.psu_delay

    addresses = {n: getattr(args, n) for n in DEVICES if getattr(args, n) is not None}
    if not addresses:
        addresses = {"sdg": "sim", "psu": "sim"}
    if args.only:
        addresses = {args.only: addresses.get(args.only, "sim")}

    app = QApplication(sys.argv if argv is None else ["run_gui", *argv])
    windows = []
    for i, (name, address) in enumerate(addresses.items()):
        kind, title, default_model = DEVICES[name]
        driver_cls, _ = DRIVERS[kind]
        model_name = models.get(name, default_model)
        if model_name is not None and model_name not in MODELS[kind]:
            raise SystemExit(f"{name}: неизвестная модель {model_name!r}, есть {sorted(MODELS[kind])}")
        proxy = InstrumentProxy(driver_cls, make_transport(kind, address, args.backend, args.latency),
                                model=MODELS[kind].get(model_name) if model_name else None,
                                command_delay_s=delays.get(name))
        win = InstrumentWindow(proxy, title)
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
