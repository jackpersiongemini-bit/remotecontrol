"""Окно источников питания (IT6300, visalab/front/psu.py).

    python run_psu.py                 # адрес из ini окна, иначе lab.json (psu), иначе симулятор
    python run_psu.py --sim           # симулятор
    python run_psu.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Плитка = канал: клик по полю V / I - уставка (Enter), клик по плитке вне полей - выход вкл / выкл.
CH1 включается, только если CH2 и CH3 потребляют ток (замок в плитке CH1 - обход).
"""
from __future__ import annotations

import sys

from visalab.front import PSUWindow
from visalab.front.launch import run_window


def main(argv=None) -> int:
    return run_window(PSUWindow, "psu", __doc__, argv)


if __name__ == "__main__":
    sys.exit(main())
