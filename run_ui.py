"""Новый фронтенд (демо, без приборов).

    python run_ui.py                 # источник, генератор, СВЧ-генератор и окно макросов
    python run_ui.py psu             # одно окно
    python run_ui.py psu --compact   # сразу компактная версия

Окна на qframelesswindow (pip install PyQt6-Frameless-Window). Бэкенд не подключён:
значения принимаются как есть, измерения источника - фальшивые.
Выбор параметров для таблицы (☆) запоминается между запусками.
"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from visalab.ui import InstrumentWindow, MacroWindow
from visalab.ui.demo import SPECS, SUBTITLES, demo_model


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("windows", nargs="*", choices=[*SPECS, "macros", []], default=[])
    ap.add_argument("--compact", action="store_true", help="open compact views")
    ap.add_argument("--no-connect", action="store_true", help="do not connect the demo on start")
    args = ap.parse_args(argv)
    names = args.windows or [*SPECS, "macros"]

    app = QApplication(sys.argv[:1])
    store = QSettings("visalab", "ui")
    windows = []
    for i, name in enumerate(names):
        if name == "macros":
            w = MacroWindow()
        else:
            model = demo_model(name)
            w = InstrumentWindow(model, SUBTITLES[name], settings=store,
                                 mode="compact" if args.compact else None)
            if not args.no_connect:
                model.connect_requested.emit(True)
        w.move(40 + i * 50, 40 + i * 50)
        w.show()
        windows.append(w)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
