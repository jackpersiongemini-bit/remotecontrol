"""Окно анализатора спектра (E4446A, visalab/front/spectrum.py): настройки и пресеты.

    python run_spectrum.py                 # адрес из ini окна, иначе lab.json (e4446a), иначе симулятор
    python run_spectrum.py --sim           # симулятор
    python run_spectrum.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Пресеты - состояния в памяти самого анализатора (MMEM:STOR:STAT / LOAD:STAT). Трассы здесь нет.
"""
from __future__ import annotations

import sys

from visalab.front import SpectrumWindow
from visalab.front.launch import run_window


def main(argv=None) -> int:
    return run_window(SpectrumWindow, "e4446a", __doc__, argv)


if __name__ == "__main__":
    sys.exit(main())
