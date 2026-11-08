"""Окна приборов в стиле Алексея (WindowClass.py / Parameter.py / PulseGen.py) поверх InstrumentProxy."""
from .power_meter import PowerMeterWindow
from .psu import PSUWindow
from .pulse_gen import PulseGenWindow
from .rf_gen import RFGenWindow

__all__ = ["PowerMeterWindow", "PSUWindow", "PulseGenWindow", "RFGenWindow"]
