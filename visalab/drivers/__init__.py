"""Слой 2: драйверы. Этот модуль - входной порт слоя (то, чем пользуется слой 3, абстрактные приборы).

Порт:

* каталог: :func:`kinds` (типы драйверов, все или одного класса приборов), :func:`profile_of`,
  :func:`default_delay`, :func:`models`, :func:`model_class`, :func:`kind_of`;
* :func:`create_driver` - драйвер по типу и адресу, вместе с транспортом; :func:`list_resources` - что видит VISA;
* контракт драйвера - :class:`~visalab.drivers.base.VISAInstrument` (сигналы и слоты, которыми с ним
  говорит слой 3) и профиль класса приборов (``Driver.PROFILE``, :mod:`visalab.profiles`).

Выходной порт слоя - транспорт (:mod:`visalab.transport`): драйвер пишет и читает SCPI только через него.

Чтобы добавить прибор, допишите строку в DRIVERS (класс драйвера, класс симулятора) - окна и
``run_gui.py`` подхватят его через каталог.
"""
from __future__ import annotations

import re

from .. import transport as _transport
from ..core.profile import Profile
from ..sim import (BENCH, AgilentPSASim, ItechIT6300Sim, KeysightE8267DSim, KeysightPNASim, KeysightU2021XASim,
                   RSSMF100ASim, SiglentSDGSim)
from ..transport import PyVisaTransport, SimTransport, Transport
from .agilent_e4446a import PSA_MODELS, AgilentPSA
from .base import ESR_BITS, InstrumentError, VISAInstrument
from .itech_it6300 import IT6300_MODELS, ItechIT6300
from .keysight_e8267d import E8267D_MODELS, KeysightE8267D
from .keysight_n5231a import PNA_MODELS, KeysightPNA
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
    "pna": (KeysightPNA, KeysightPNASim),
}

#: модели (пределы проверки) по типу драйвера
MODELS = {"sdg": SDG_MODELS, "it6300": IT6300_MODELS, "e8267d": E8267D_MODELS, "smf100a": SMF100A_MODELS, "u2021xa": {}, "e4446a": PSA_MODELS, "pna": PNA_MODELS}


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
    """address=None или "sim" -> симулятор (встаёт на стенд симуляторов процесса, пока подключён), иначе pyvisa."""
    _, sim_cls = DRIVERS[kind]
    if address in (None, "", "sim"):
        return SimTransport(sim_cls(), address=f"SIM::{kind.upper()}::INSTR", latency_s=latency_s, bench=BENCH)
    return PyVisaTransport(normalize_address(address), backend=backend)


def list_resources(backend: str = "") -> list[str]:
    """Какие приборы видит VISA (``backend``: "" - системная библиотека, "@py" - pyvisa-py)."""
    return _transport.list_resources(backend)


# ------------------------------------------------------------------ порт слоя: каталог
def kinds(profile: str | None = None) -> list[str]:
    """Типы драйверов (``e8267d``, ``smf100a``...) в порядке реестра; ``profile`` - только этого класса
    приборов (``signal_generator``...)."""
    return [k for k, (cls, _sim) in DRIVERS.items() if profile is None or cls.PROFILE.name == profile]


def profile_of(kind: str) -> Profile:
    """Профиль класса приборов, который реализует драйвер."""
    return DRIVERS[kind][0].PROFILE


def kind_of(driver) -> str | None:
    """Тип драйвера по его классу или экземпляру (None - драйвер не из реестра)."""
    cls = driver if isinstance(driver, type) else type(driver)
    return next((k for k, (c, _sim) in DRIVERS.items() if c is cls), None)


def default_delay(kind: str) -> float:
    """Пауза между командами по умолчанию для драйвера, с."""
    return float(getattr(DRIVERS[kind][0], "COMMAND_DELAY_S", 0.0) or 0.0)


def models(kind: str) -> dict:
    """Встроенные модели драйвера (пределы и функции): {имя: модель}."""
    return dict(MODELS.get(kind) or {})


def model_class(kind: str):
    """Класс модели драйвера (frozen dataclass) - по нему строится форма своей модели; None - моделей нет."""
    built_in = MODELS.get(kind) or {}
    return type(next(iter(built_in.values()))) if built_in else None


def create_driver(kind: str, address: str | None, model=None, delay: float | None = None, backend: str = "",
                  latency: float = 0.0) -> VISAInstrument:
    """Драйвер прибора вместе с его транспортом (ещё не подключён).

    ``address`` - VISA-строка, IP, IP:порт или ``sim`` (симулятор); ``model`` - имя встроенной модели,
    готовая модель (своя, из окна) или None (по *IDN?); ``delay`` - пауза между командами, с (None - по
    умолчанию драйвера)."""
    driver_cls, _sim = DRIVERS[kind]
    if isinstance(model, str):
        model = models(kind).get(model)
    return driver_cls(make_transport(kind, address, backend, latency), model=model,
                      command_delay_s=None if delay is None else float(delay))


__all__ = ["DRIVERS", "MODELS", "make_transport", "normalize_address", "kinds", "profile_of", "kind_of",
           "default_delay", "models", "model_class", "create_driver", "list_resources", "VISAInstrument",
           "InstrumentError", "ESR_BITS",
           "SiglentSDG", "ItechIT6300", "KeysightE8267D", "RSSMF100A", "KeysightU2021XA", "AgilentPSA", "KeysightPNA"]
