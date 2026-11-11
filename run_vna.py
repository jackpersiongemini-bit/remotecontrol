"""Окно векторного анализатора цепей (N5231A, visalab/front/vna.py): настройки, мощность, CW, состояния.

    python run_vna.py                 # адрес из ini окна, иначе lab.json (pna), иначе симулятор
    python run_vna.py --sim           # симулятор
    python run_vna.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Трасс здесь нет. Состояния (вкладка SAVE) хранятся в памяти самого анализатора (MMEM:STOR / LOAD *.csa).
"""
from __future__ import annotations

import sys

from visalab.front import VNAWindow
from visalab.front.launch import run_window


def main(argv=None) -> int:
    return run_window(VNAWindow, "pna", __doc__, argv)


if __name__ == "__main__":
    sys.exit(main())
