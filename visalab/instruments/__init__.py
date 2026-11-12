"""Слой 3: абстрактные приборы. Этот модуль - входной порт слоя (им пользуется слой 4, координатор).

Порт:

* :func:`open_instrument` - абстрактный прибор (:class:`InstrumentProxy`) по типу драйвера и адресу;
* :class:`InstrumentProxy` - прибор глазами координатора: ключи и действия профиля класса приборов,
  значения, статусы записи, состояние связи, сигналы Qt;
* каталог драйверов (транзитом из слоя 2): :func:`kinds`, :func:`profile_of`, :func:`default_delay`,
  :func:`models`, :func:`model_class`, :func:`list_resources`.

Выходной порт слоя - порт драйверов (:mod:`visalab.drivers`): каталог и контракт драйвера.
"""
from __future__ import annotations

from .. import drivers as _drivers
from .proxy import InstrumentProxy

__all__ = ["InstrumentProxy", "open_instrument", "kinds", "profile_of", "default_delay", "models", "model_class",
           "list_resources"]


def open_instrument(kind: str, address: str | None, model=None, delay: float | None = None, backend: str = "",
                    latency: float = 0.0) -> InstrumentProxy:
    """Абстрактный прибор по типу драйвера (``e8267d``...) и адресу (VISA, IP, ``sim``). Ещё не подключён:
    подключение - ``open()``. ``model`` - имя встроенной модели, своя модель или None (по *IDN?)."""
    driver = _drivers.create_driver(kind, address, model=model, delay=delay, backend=backend, latency=latency)
    return InstrumentProxy(driver, kind=kind)


def kinds(profile: str | None = None) -> list[str]:
    """Типы драйверов - все или одного класса приборов (``signal_generator``...)."""
    return _drivers.kinds(profile)


def profile_of(kind: str):
    """Профиль класса приборов драйвера."""
    return _drivers.profile_of(kind)


def default_delay(kind: str) -> float:
    """Пауза между командами по умолчанию для драйвера, с."""
    return _drivers.default_delay(kind)


def models(kind: str) -> dict:
    """Встроенные модели драйвера: {имя: модель}."""
    return _drivers.models(kind)


def model_class(kind: str):
    """Класс модели драйвера (форма своей модели) или None."""
    return _drivers.model_class(kind)


def list_resources(backend: str = "") -> list[str]:
    """Какие приборы видит VISA."""
    return _drivers.list_resources(backend)
