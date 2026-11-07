"""Реестр драйверов: имя -> (класс драйвера, класс симулятора).

Чтобы добавить прибор, допишите строку в DRIVERS - GUI и run_gui.py подхватят его.
Класс прибора (профиль) задаётся в самом драйвере: ``Driver.PROFILE``.
"""
import re

from ..core.transport import PyVisaTransport, SimTransport, Transport
from ..sim.agilent_e4446a_sim import AgilentPSASim
from ..sim.itech_it6300_sim import ItechIT6300Sim
from ..sim.keysight_u2021xa_sim import KeysightU2021XASim
from ..sim.rf_generator_sim import KeysightE8267DSim, RSSMF100ASim
from ..sim.siglent_sdg_sim import SiglentSDGSim
from .agilent_e4446a import PSA_MODELS, AgilentPSA
from .itech_it6300 import IT6300_MODELS, ItechIT6300
from .keysight_e8267d import E8267D_MODELS, KeysightE8267D
from .keysight_u2021xa import KeysightU2021XA
from .rs_smf100a import SMF100A_MODELS, RSSMF100A
from .siglent_sdg import SDG_MODELS, SiglentSDG

DRIVERS = {
    "sdg": (SiglentSDG, SiglentSDGSim),
    "it6300": (ItechIT6300, ItechIT6300Sim),
    "e8267d": (KeysightE8267D, KeysightE8267DSim),
    "smf100a": (RSSMF100A, RSSMF100ASim),
    "u2021xa": (KeysightU2021XA, KeysightU2021XASim),
    "e4446a": (AgilentPSA, AgilentPSASim),
}

#: модели (пределы проверки) по типу драйвера
MODELS = {"sdg": SDG_MODELS, "it6300": IT6300_MODELS, "e8267d": E8267D_MODELS, "smf100a": SMF100A_MODELS, "u2021xa": {}, "e4446a": PSA_MODELS}


_IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?$")


def normalize_address(address: str) -> str:
    """Короткая запись LAN-адреса -> VISA-ресурс.

    ``192.168.0.3``      -> ``TCPIP0::192.168.0.3::INSTR``         (VXI-11, есть serial poll)
    ``192.168.0.3:5025`` -> ``TCPIP0::192.168.0.3::5025::SOCKET``  (сырой сокет, статус через *STB?)
    Всё остальное (USB0::..., GPIB0::..., TCPIP0::...) возвращается как есть.
    """
    m = _IP_RE.match(address.strip())
    if not m:
        return address
    ip, port = m.groups()
    return f"TCPIP0::{ip}::{port}::SOCKET" if port else f"TCPIP0::{ip}::INSTR"


def make_transport(kind: str, address: str | None, backend: str = "", latency_s: float = 0.0) -> Transport:
    """address=None или "sim" -> симулятор, иначе pyvisa."""
    _, sim_cls = DRIVERS[kind]
    if address in (None, "", "sim"):
        return SimTransport(sim_cls(), address=f"SIM::{kind.upper()}::INSTR", latency_s=latency_s)
    return PyVisaTransport(normalize_address(address), backend=backend)


__all__ = ["DRIVERS", "MODELS", "make_transport", "normalize_address", "SiglentSDG", "ItechIT6300", "KeysightE8267D", "RSSMF100A", "KeysightU2021XA", "AgilentPSA"]
