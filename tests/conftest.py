import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from visalab.core import SimTransport  # noqa: E402


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
