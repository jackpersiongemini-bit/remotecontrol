"""Стандартные профили классов приборов.

Это и есть внешний интерфейс фонового слоя. GUI и сценарии пишутся только
против этих ключей, поэтому любой прибор класса взаимозаменяем.

Соглашения
----------
* Единицы - СИ без префиксов: Hz, V, A, W, s, dBm, %, deg, rad. Префиксы (k, M, G)
  только в отображении.
* Логические значения - bool (True/False), а не "ON"/"1".
* ENUM - стандартные токены, перечисленные здесь. Перевод в токены прибора делает
  драйвер (``Param.enum_map``).
* Канальные ключи ``ch{n}.*`` нумеруются с 1.
* Что не входит в профиль - расширение производителя ``x.*``.

Профили анализатора спектра, ваттметра и ВАЦ будут добавлены вместе с их драйверами
(им нужен ещё стандартный тип данных для трасс).
"""
from __future__ import annotations

from .core.params import Kind
from .core.profile import ActionSlot, Profile, Slot

__all__ = ["POWER_SUPPLY", "FUNCTION_GENERATOR", "SIGNAL_GENERATOR", "PROFILES"]

F, I, B, E, S = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM, Kind.STR
SOURCES = ("INT", "EXT")
TRIG_SOURCES = ("INT", "EXT", "MAN")

# ---------------------------------------------------------------- источник питания
POWER_SUPPLY = Profile(
    "power_supply", "Источник питания",
    slots=(
        Slot("ch{n}.output", B, required=True, doc="выход канала"),
        Slot("ch{n}.voltage", F, "V", required=True, doc="уставка напряжения"),
        Slot("ch{n}.current", F, "A", required=True, doc="уставка (ограничение) тока"),
        Slot("ch{n}.meas_v", F, "V", required=True, readonly=True, doc="измеренное напряжение"),
        Slot("ch{n}.meas_i", F, "A", required=True, readonly=True, doc="измеренный ток"),
        Slot("ch{n}.meas_p", F, "W", readonly=True, doc="измеренная мощность"),
        Slot("ch{n}.mode", E, readonly=True, choices=("CV", "CC", "OFF"), doc="режим стабилизации"),
        Slot("ch{n}.ovp_state", B, doc="защита от перенапряжения включена"),
        Slot("ch{n}.ovp_level", F, "V", doc="порог OVP"),
        Slot("ch{n}.ovp_tripped", B, readonly=True, doc="OVP сработала"),
        Slot("ch{n}.vlimit", F, "V", doc="программный предел уставки напряжения"),
        Slot("output_all", B, doc="все выходы разом"),
        Slot("combine", E, choices=("NONE", "SER", "PAR", "TRAC"),
             doc="объединение CH1+CH2: раздельно / последовательно / параллельно / слежение"),
    ),
    actions=(
        ActionSlot("all_off", required=True, doc="выключить все выходы"),
        ActionSlot("reset", doc="*RST"),
        ActionSlot("clear_ovp", doc="снять срабатывание OVP (аргументы: номера каналов)"),
    ),
)

# ------------------------------------------------------- функциональный генератор
FUNCTION_GENERATOR = Profile(
    "function_generator", "Генератор сигналов (НЧ, функциональный)",
    slots=(
        Slot("ch{n}.output", B, required=True),
        Slot("ch{n}.wave", E, required=True,
             choices=("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC")),
        Slot("ch{n}.frequency", F, "Hz", required=True),
        Slot("ch{n}.amplitude", F, "Vpp", required=True),
        Slot("ch{n}.offset", F, "V", required=True),
        Slot("ch{n}.high", F, "V"),
        Slot("ch{n}.low", F, "V"),
        Slot("ch{n}.phase", F, "deg"),
        Slot("ch{n}.duty", F, "%"),
        Slot("ch{n}.symmetry", F, "%"),
        Slot("ch{n}.width", F, "s"),
        Slot("ch{n}.load", E, choices=("50", "HIZ"), doc="нагрузка, на которую пересчитывается амплитуда"),
        Slot("ch{n}.polarity", E, choices=("NORM", "INV")),
        Slot("ch{n}.sync", B, doc="синхровыход"),
        Slot("ch{n}.mod_state", B),
        Slot("ch{n}.mod_type", E, choices=("AM", "FM", "PM", "FSK", "ASK", "PSK", "DSBAM", "PWM")),
        Slot("ch{n}.mod_source", E, choices=SOURCES),
        Slot("ch{n}.mod_shape", E, choices=("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE", "ARB")),
        Slot("ch{n}.mod_freq", F, "Hz"),
        Slot("ch{n}.mod_depth", F, "%"),
        Slot("ch{n}.mod_dev", F, "", doc="девиация: Hz для FM, deg для PM"),
        Slot("ch{n}.sweep_state", B),
        Slot("ch{n}.sweep_time", F, "s"),
        Slot("ch{n}.sweep_start", F, "Hz"),
        Slot("ch{n}.sweep_stop", F, "Hz"),
        Slot("ch{n}.sweep_mode", E, choices=("LINE", "LOG")),
        Slot("ch{n}.sweep_dir", E, choices=("UP", "DOWN")),
        Slot("ch{n}.sweep_trig", E, choices=TRIG_SOURCES),
        Slot("ch{n}.burst_state", B),
        Slot("ch{n}.burst_mode", E, choices=("NCYC", "GATE")),
        Slot("ch{n}.burst_period", F, "s"),
        Slot("ch{n}.burst_cycles", S, doc="число циклов или INF"),
        Slot("ch{n}.burst_trig", E, choices=TRIG_SOURCES),
        Slot("ch{n}.burst_delay", F, "s"),
    ),
    actions=(
        ActionSlot("all_off", required=True),
        ActionSlot("reset"),
        ActionSlot("trigger_{n}", doc="ручной триггер свипа/пакета канала"),
    ),
)

# ------------------------------------------------------------- СВЧ-генератор
SIGNAL_GENERATOR = Profile(
    "signal_generator", "Генератор СВЧ (CW + аналоговая модуляция)",
    slots=(
        Slot("rf.output", B, required=True, doc="выход ВЧ"),
        Slot("rf.frequency", F, "Hz", required=True, doc="частота CW"),
        Slot("rf.power", F, "dBm", required=True, doc="уровень"),
        Slot("rf.alc", B, doc="АРУ мощности (ALC)"),
        Slot("mod.output", B, doc="общий выключатель модуляции"),
        Slot("am.state", B), Slot("am.depth", F, "%"), Slot("am.source", E, choices=SOURCES),
        Slot("am.freq", F, "Hz", doc="частота внутреннего модулирующего сигнала"),
        Slot("fm.state", B), Slot("fm.deviation", F, "Hz"), Slot("fm.source", E, choices=SOURCES),
        Slot("fm.freq", F, "Hz"),
        Slot("pm.state", B), Slot("pm.deviation", F, "rad"), Slot("pm.source", E, choices=SOURCES),
        Slot("pm.freq", F, "Hz"),
        Slot("pulse.state", B), Slot("pulse.source", E, choices=SOURCES),
        Slot("pulse.period", F, "s"), Slot("pulse.width", F, "s"),
        Slot("ref.source", E, readonly=None, choices=SOURCES, doc="опорный генератор 10 МГц"),
    ),
    actions=(
        ActionSlot("all_off", required=True, doc="выключить ВЧ-выход"),
        ActionSlot("reset"),
    ),
)

PROFILES = {p.name: p for p in (POWER_SUPPLY, FUNCTION_GENERATOR, SIGNAL_GENERATOR)}
