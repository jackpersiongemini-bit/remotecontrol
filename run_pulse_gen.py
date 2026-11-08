"""Окно генератора импульсов Siglent SDG (visalab/front/pulse_gen.py).

    python run_pulse_gen.py                 # адрес из ini окна, иначе lab.json (sdg), иначе симулятор
    python run_pulse_gen.py --sim           # симулятор
    python run_pulse_gen.py --address 192.168.0.5
    python run_pulse_gen.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Драйвер, модель и адрес можно поменять в окне: клик по адресу -> APPLY (сохраняется в ini).
"""
from __future__ import annotations

import sys

from visalab.front import PulseGenWindow
from visalab.front.launch import run_window


def main(argv=None) -> int:
    return run_window(PulseGenWindow, "sdg", __doc__, argv)


if __name__ == "__main__":
    sys.exit(main())
