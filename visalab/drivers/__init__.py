"""Реестр драйверов: имя -> (класс драйвера, класс симулятора).

Чтобы добавить прибор, допишите строку в DRIVERS - GUI и run_gui.py подхватят его.
"""
from ..core.transport import PyVisaTransport, SimTransport, Transport
from ..sim.itech_it6300_sim import ItechIT6300Sim
from ..sim.siglent_sdg_sim import SiglentSDGSim
from .itech_it6300 import ItechIT6300
from .siglent_sdg import SiglentSDG

DRIVERS = {
    "sdg": (SiglentSDG, SiglentSDGSim),
    "it6300": (ItechIT6300, ItechIT6300Sim),
}


def make_transport(kind: str, address: str | None, backend: str = "", latency_s: float = 0.0) -> Transport:
    """address=None или "sim" -> симулятор, иначе pyvisa."""
    _, sim_cls = DRIVERS[kind]
    if address in (None, "", "sim"):
        return SimTransport(sim_cls(), address=f"SIM::{kind.upper()}::INSTR", latency_s=latency_s)
    return PyVisaTransport(address, backend=backend)


__all__ = ["DRIVERS", "make_transport", "SiglentSDG", "ItechIT6300"]
