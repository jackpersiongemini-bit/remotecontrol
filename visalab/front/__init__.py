"""Интерфейс лаборатории: окно графиков (``GraphsWindow``), окно инструментов (``InstrumentsWindow``)
и окна приборов поверх координатора (``Instrument``)."""
from .favorites import FavoritesManager, FavoritesPopup
from .graphs_window import GraphCard, GraphsWindow
from .instruments_window import InstrumentCard, InstrumentsWindow
from .plot_widget import PlotLimit, PlotMarker, PlotSeries, PlotSettingsWindow, PlotWidget
from .power_meter import PowerMeterWindow
from .psu import PSUWindow
from .pulse_gen import PulseGenWindow
from .rf_gen import RFGenWindow
from .spectrum import SpectrumWindow
from .vna import VNAWindow
from .widgets import ScenarioChip, SincEmblem

__all__ = [
    "GraphsWindow",
    "GraphCard",
    "InstrumentsWindow",
    "InstrumentCard",
    "PlotWidget",
    "PlotSeries",
    "PlotMarker",
    "PlotLimit",
    "PlotSettingsWindow",
    "SincEmblem",
    "ScenarioChip",
    "PowerMeterWindow",
    "PSUWindow",
    "PulseGenWindow",
    "RFGenWindow",
    "SpectrumWindow",
    "VNAWindow",
    "FavoritesManager",
    "FavoritesPopup",
]
