"""Окно СВЧ генератора (E8267D / SMF100A, visalab/front/rf_gen.py).

    python run_rf_gen.py                    # адрес из ini окна, иначе lab.json (e8267d), иначе симулятор
    python run_rf_gen.py --sim              # симулятор
    python run_rf_gen.py --address 192.168.0.3
    python run_rf_gen.py --ini my.ini       # файл настроек (по умолчанию visalab_front.ini)

Драйвер (E8267D / SMF100A), модель и адрес - в окне: клик по адресу -> APPLY (сохраняется в ini).
"""
from __future__ import annotations

import sys

from visalab.front import RFGenWindow
from visalab.front.launch import run_window


def main(argv=None) -> int:
    return run_window(RFGenWindow, "e8267d", __doc__, argv)


if __name__ == "__main__":
    sys.exit(main())
