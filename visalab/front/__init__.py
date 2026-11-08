"""Окна приборов в стиле Алексея (WindowClass.py / Parameter.py / PulseGen.py) поверх InstrumentProxy."""
from .pulse_gen import PulseGenWindow
from .rf_gen import RFGenWindow

__all__ = ["PulseGenWindow", "RFGenWindow"]
