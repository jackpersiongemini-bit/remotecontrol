"""Окна приборов в стиле Алексея (WindowClass.py / Parameter.py / PulseGen.py) поверх прибора координатора (Instrument)."""
from .power_meter import PowerMeterWindow
from .psu import PSUWindow
from .pulse_gen import PulseGenWindow
from .rf_gen import RFGenWindow
from .spectrum import SpectrumWindow
from .vna import VNAWindow

__all__ = ["PowerMeterWindow", "PSUWindow", "PulseGenWindow", "RFGenWindow", "SpectrumWindow", "VNAWindow"]
