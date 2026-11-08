"""Окно ваттметра (U2021XA, visalab/front/power_meter.py).

    python run_power_meter.py                 # адрес из ini окна, иначе lab.json (u2021xa), иначе симулятор
    python run_power_meter.py --sim           # симулятор
    python run_power_meter.py --with-sdg      # и окно ГСС: DUTY = SDG1 / SDG2 берёт скважность с него
    python run_power_meter.py --ini my.ini    # файл настроек (по умолчанию visalab_front.ini)

Поправка (CAL - GAIN + DUTY) пишется в прибор как offset, поэтому все показания уже исправлены.
"""
from __future__ import annotations

import argparse
import sys

from visalab.front import PowerMeterWindow, PulseGenWindow
from visalab.front.launch import run_windows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-sdg", action="store_true", help="открыть и окно ГСС (скважность для DUTY)")
    with_sdg = ap.parse_known_args(argv)[0].with_sdg
    specs = [(PowerMeterWindow, "u2021xa")] + ([(PulseGenWindow, "sdg")] if with_sdg else [])
    return run_windows(specs, __doc__, argv, parser=ap)


if __name__ == "__main__":
    sys.exit(main())
