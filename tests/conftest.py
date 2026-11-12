import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from visalab.coord import COORD, LINKS  # noqa: E402
from visalab.transport import SimTransport  # noqa: E402


def attach(proxy):
    """Абстрактный прибор (слой 3) -> прибор координатора, как его получает окно (в реестре COORD)."""
    return COORD.attach(proxy)


def l3(inst):
    """Абстрактный прибор под прибором координатора (тестам: поток, драйвер, симулятор)."""
    return getattr(inst, "_proxy", inst)


def worker(inst):
    """Драйвер прибора (живёт в потоке прибора)."""
    return l3(inst)._worker


def sim_of(inst):
    """Симулятор под прибором."""
    return worker(inst).transport.sim


@pytest.fixture(autouse=True)
def _own_registry_entries():
    """Тест не оставляет своих приборов в реестре координатора процесса."""
    before = LINKS.proxies()
    yield
    for inst in LINKS.proxies():
        if inst not in before:
            LINKS.remove(inst)


class Recorder:
    """Собирает все сигналы драйвера/прокси в списки."""

    def __init__(self, obj):
        self.values, self.statuses, self.errors, self.comm, self.states = {}, [], [], [], []
        obj.param_updated.connect(lambda k, v: self.values.__setitem__(k, v))
        obj.param_status.connect(lambda k, s, m: self.statuses.append((k, s, m)))
        obj.instrument_error.connect(lambda c, m: self.errors.append((c, m)))
        obj.comm_error.connect(self.comm.append)
        obj.state_changed.connect(self.states.append)

    def last_status(self, key):
        for k, s, m in reversed(self.statuses):
            if k == key:
                return s, m
        return None, None


@pytest.fixture
def make(qapp):
    """make(DriverCls, SimCls, **transport_kw) -> (driver, sim, recorder); уже подключён."""
    created = []

    def factory(driver_cls, sim_cls, connect=True, sim=None, driver_kw=None, **kw):
        sim = sim or sim_cls()
        drv = driver_cls(SimTransport(sim, **kw), **(driver_kw or {}))
        rec = Recorder(drv)
        if connect:
            drv.open_connection()
        created.append(drv)
        return drv, sim, rec

    yield factory
    for d in created:
        d.close_connection()


def flush(drv):
    """set_param откладывает запись в цикл событий - выполнить сразу."""
    drv._flush_pending()
