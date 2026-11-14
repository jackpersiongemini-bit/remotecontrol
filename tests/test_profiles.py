"""Контракт «чёрного ящика»: каждый драйвер соответствует профилю своего класса."""
import pytest

from visalab.core import Kind, Param, ProfileError
from visalab.transport import SimTransport
from visalab.drivers.base import VISAInstrument
from visalab.core.params import Action
from visalab.drivers import DRIVERS
from visalab.profiles import PROFILES, SIGNAL_GENERATOR

from conftest import flush


@pytest.mark.parametrize("kind", sorted(DRIVERS))
def test_driver_conforms_to_profile(make, kind):
    drv_cls, sim_cls = DRIVERS[kind]
    assert drv_cls.PROFILE is not None and drv_cls.PROFILE.name in PROFILES
    drv, sim, rec = make(drv_cls, sim_cls)
    assert drv.PROFILE.problems(drv.params, drv.actions, drv.channels) == []
    required = drv.PROFILE.keys(drv.channels, required_only=True)
    assert set(required) <= drv.capabilities
    drv.poll()                                             # измерения появляются при опросе
    for key in required:                                   # обязательное читается
        assert rec.values.get(key) is not None, key
    assert not rec.errors and not rec.comm


@pytest.mark.parametrize("kind", sorted(DRIVERS))
def test_all_off_is_universal(make, kind):
    """Действие all_off одинаково для любого источника сигнала/питания."""
    drv, sim, rec = make(*DRIVERS[kind])
    if "all_off" not in drv.PROFILE.expand_actions(drv.channels):
        pytest.skip("у класса нет выходов")
    outputs = [k for k in drv.capabilities if k.endswith(".output") and not k.startswith("mod.")]
    for k in outputs:
        drv.set_param(k, True); flush(drv)
    assert all(rec.values[k] is True for k in outputs)
    drv.run_action("all_off")
    assert all(rec.values[k] is False for k in outputs)


def _driver(params, actions=(), profile=SIGNAL_GENERATOR):
    class Bad(VISAInstrument):
        NAME = "Bad"
        PROFILE = profile

        def build_params(self):
            return params

        def build_actions(self):
            return actions
    return Bad


GOOD = [Param("rf.output", "o", Kind.BOOL, write="O {value}", query="O?"),
        Param("rf.frequency", "f", Kind.FLOAT, "Hz", write="F {value}", query="F?"),
        Param("rf.power", "p", Kind.FLOAT, "dBm", write="P {value}", query="P?")]
ALL_OFF = [Action("all_off", "off")]


def test_good_minimal_driver_is_accepted(qapp):
    _driver(GOOD, ALL_OFF)(SimTransport(None))


@pytest.mark.parametrize("bad, why", [
    (Param("rf.freq", "f", Kind.FLOAT, "Hz", write="F {value}"), "нет в профиле"),
    (Param("rf.frequency", "f", Kind.FLOAT, "GHz", write="F {value}"), "единица"),
    (Param("rf.frequency", "f", Kind.STR, "Hz", write="F {value}"), "тип"),
    (Param("rf.frequency", "f", Kind.FLOAT, "Hz", query="F?"), "только чтение"),
    (Param("am.source", "s", Kind.ENUM, write="S {value}", choices=("INT", "EXT1")), "токены"),
])
def test_profile_violations_are_rejected(qapp, bad, why):
    params = [p for p in GOOD if p.key != bad.key] + [bad]
    with pytest.raises(ProfileError, match=why):
        _driver(params, ALL_OFF)(SimTransport(None))


def test_missing_required_and_vendor_extensions(qapp):
    with pytest.raises(ProfileError, match="rf.power: обязательный"):
        _driver(GOOD[:2], ALL_OFF)(SimTransport(None))
    with pytest.raises(ProfileError, match="all_off"):
        _driver(GOOD, [])(SimTransport(None))
    ext = Param("x.vendor_knob", "v", Kind.INT, write="V {value}")   # расширение - можно
    _driver([*GOOD, ext], [*ALL_OFF, Action("x.self_test", "t")])(SimTransport(None))
