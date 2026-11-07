"""Реестр драйверов: имя -> (класс драйвера, класс симулятора).

Чтобы добавить прибор, допишите строку в DRIVERS - GUI и run_gui.py подхватят его.
Класс прибора (профиль) задаётся в самом драйвере: ``Driver.PROFILE``.
"""
from ..core.transport import PyVisaTransport, SimTransport, Transport
from ..sim.itech_it6300_sim import ItechIT6300Sim
from ..sim.rf_generator_sim import KeysightE8267DSim, RSSMF100ASim
from ..sim.siglent_sdg_sim import SiglentSDGSim
from .itech_it6300 import IT6300_MODELS, ItechIT6300
from .keysight_e8267d import E8267D_MODELS, KeysightE8267D
from .rs_smf100a import SMF100A_MODELS, RSSMF100A
from .siglent_sdg import SDG_MODELS, SiglentSDG

DRIVERS = {
    "sdg": (SiglentSDG, SiglentSDGSim),
    "it6300": (ItechIT6300, ItechIT6300Sim),
    "e8267d": (KeysightE8267D, KeysightE8267DSim),
    "smf100a": (RSSMF100A, RSSMF100ASim),
}

#: модели (пределы проверки) по типу драйвера
MODELS = {"sdg": SDG_MODELS, "it6300": IT6300_MODELS, "e8267d": E8267D_MODELS, "smf100a": SMF100A_MODELS}


def make_transport(kind: str, address: str | None, backend: str = "", latency_s: float = 0.0) -> Transport:
    """address=None или "sim" -> симулятор, иначе pyvisa."""
    _, sim_cls = DRIVERS[kind]
    if address in (None, "", "sim"):
        return SimTransport(sim_cls(), address=f"SIM::{kind.upper()}::INSTR", latency_s=latency_s)
    return PyVisaTransport(address, backend=backend)


__all__ = ["DRIVERS", "MODELS", "make_transport", "SiglentSDG", "ItechIT6300", "KeysightE8267D", "RSSMF100A"]
