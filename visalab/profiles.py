"""Стандартные профили классов приборов.

Это и есть внешний интерфейс фонового слоя. GUI и сценарии пишутся только
против этих ключей, поэтому любой прибор класса взаимозаменяем.

Соглашения
----------
* Единицы - СИ без префиксов: Hz, V, A, W, s, dBm, dB, %, deg, rad. Префиксы (k, M, G)
  только в отображении.
* Логические значения - bool (True/False), а не "ON"/"1".
* ENUM - стандартные токены, перечисленные здесь. Перевод в токены прибора делает
  драйвер (``Param.enum_map``).
* Канальные ключи ``ch{n}.*`` нумеруются с 1.
* Что не входит в профиль - расширение производителя ``x.*``.

* Трассы - ``visalab.core.trace.Trace`` (ось X, ось Y, единицы, метаданные), ключ вида Kind.TRACE.
"""
from __future__ import annotations

from .core.params import Kind
from .core.profile import ActionSlot, Profile, Slot

__all__ = ["POWER_SUPPLY", "FUNCTION_GENERATOR", "SIGNAL_GENERATOR", "POWER_METER", "SPECTRUM_ANALYZER", "NETWORK_ANALYZER", "PROFILES"]

F, I, B, E, S, T = Kind.FLOAT, Kind.INT, Kind.BOOL, Kind.ENUM, Kind.STR, Kind.TRACE
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
        Slot("ch{n}.period", F, "s", doc="период = 1/частота"),
        Slot("ch{n}.amplitude", F, "Vpp", required=True),
        Slot("ch{n}.offset", F, "V", required=True),
        Slot("ch{n}.high", F, "V"),
        Slot("ch{n}.low", F, "V"),
        Slot("ch{n}.phase", F, "deg"),
        Slot("ch{n}.duty", F, "%"),
        Slot("ch{n}.symmetry", F, "%"),
        Slot("ch{n}.width", F, "s"),
        Slot("ch{n}.delay", F, "s", doc="задержка импульса"),
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
        ActionSlot("align_phase", doc="выровнять фазы каналов"),
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

# ------------------------------------------------------------------ ваттметр
POWER_METER = Profile(
    "power_meter", "Измеритель мощности (ваттметр / датчик)",
    doc="""Результаты всегда в dBm (отношение пик/среднее - в dB), независимо от настроек прибора.

    Цикл измерения:
    * trig.source = IMM и meas.continuous = True - свободный счёт, результат обновляется при опросе;
    * любой другой источник запуска (INT - по уровню сигнала, EXT, BUS) - драйвер взводит
      однократное измерение, ждёт его завершения без блокирующих запросов (meas.armed = True),
      публикует результат и, если meas.continuous, взводит снова;
    * действие single - одно измерение по текущему запуску, abort - отменить ожидание.
    """,
    slots=(
        Slot("meas.power", F, "dBm", required=True, readonly=True,
             doc="основной результат: средняя мощность (в стробе, если detector = NORMAL)"),
        Slot("meas.peak", F, "dBm", readonly=True, doc="пиковая мощность в стробе"),
        Slot("meas.ptav", F, "dB", readonly=True, doc="пик/среднее в стробе"),
        Slot("meas.continuous", B, required=True, doc="повторять измерения автоматически"),
        Slot("meas.armed", B, readonly=True, doc="измерение взведено, ждём запуск"),
        Slot("freq", F, "Hz", required=True, doc="частота сигнала для поправки датчика"),
        Slot("avg.state", B, doc="усреднение включено"),
        Slot("avg.count", I, doc="число усреднений"),
        Slot("avg.auto", B, doc="число усреднений выбирается прибором"),
        Slot("offset", F, "dB", doc="поправка (аттенюатор/ответвитель) в dB"),
        Slot("offset.state", B),
        Slot("detector", E, choices=("AVERAGE", "NORMAL"),
             doc="AVERAGE - только средняя мощность; NORMAL - пик/среднее, строб, запуск"),
        Slot("video_bw", E, choices=("OFF", "LOW", "MED", "HIGH")),
        Slot("trig.source", E, choices=("IMM", "INT", "EXT", "BUS", "HOLD"),
             doc="IMM - свободный счёт, INT - по уровню входного сигнала"),
        Slot("trig.level", F, "dBm"),
        Slot("trig.level_auto", B),
        Slot("trig.slope", E, choices=("POS", "NEG")),
        Slot("trig.delay", F, "s"),
        Slot("trig.holdoff", F, "s"),
        Slot("gate.start", F, "s", doc="начало строба от момента запуска"),
        Slot("gate.length", F, "s", doc="длительность строба"),
        Slot("trace", T, "dBm", readonly=True,
             doc="мощность во времени (x - с от задержанного запуска), после каждого измерения при trace.state"),
        Slot("trace.state", B, doc="режим временной области: снимать трассу (нужен detector = NORMAL)"),
        Slot("trace.time", F, "s", doc="длительность трассы"),
        Slot("trace.offset", F, "s", doc="начало трассы от задержанного запуска"),
    ),
    actions=(
        ActionSlot("zero", required=True, doc="обнуление датчика (вход без сигнала!)"),
        ActionSlot("calibrate", doc="внутренняя калибровка"),
        ActionSlot("single", doc="одно измерение по текущему источнику запуска"),
        ActionSlot("abort", doc="отменить ожидание запуска"),
        ActionSlot("trigger", doc="программный запуск (trig.source = BUS)"),
        ActionSlot("reset"),
    ),
)

# ---------------------------------------------------------- анализатор спектра
SPECTRUM_ANALYZER = Profile(
    "spectrum_analyzer", "Анализатор спектра",
    doc="""Трасса ``trace`` - Trace(x в Hz, y в dBm), публикуется после КАЖДОЙ завершённой развёртки
    (драйвер не читает недорисованную трассу). meas.continuous - развёртки подряд,
    действие single - одна развёртка. Маркер перечитывается вместе с трассой.""",
    slots=(
        Slot("trace", T, "dBm", required=True, readonly=True, doc="последняя завершённая развёртка"),
        Slot("meas.continuous", B, required=True),
        Slot("meas.armed", B, readonly=True, doc="развёртка идёт, ждём окончания"),
        Slot("freq.center", F, "Hz", required=True),
        Slot("freq.span", F, "Hz", required=True),
        Slot("freq.start", F, "Hz"),
        Slot("freq.stop", F, "Hz"),
        Slot("bw.res", F, "Hz", required=True, doc="RBW"),
        Slot("bw.res_auto", B),
        Slot("bw.video", F, "Hz", doc="VBW"),
        Slot("bw.video_auto", B),
        Slot("ref_level", F, "dBm", required=True),
        Slot("scale_div", F, "dB", doc="дБ на деление"),
        Slot("atten", F, "dB", doc="входной аттенюатор"),
        Slot("atten_auto", B),
        Slot("sweep.time", F, "s"),
        Slot("sweep.time_auto", B),
        Slot("sweep.points", I),
        Slot("detector", E, choices=("NORMAL", "PEAK", "NEG_PEAK", "SAMPLE", "AVERAGE", "RMS", "QPEAK")),
        Slot("avg.state", B),
        Slot("avg.count", I),
        Slot("ref.source", E, readonly=None, choices=("INT", "EXT")),
        Slot("marker.state", B),
        Slot("marker.freq", F, "Hz"),
        Slot("marker.level", F, "dBm", readonly=True),
    ),
    actions=(
        ActionSlot("single", doc="одна развёртка"),
        ActionSlot("abort", doc="прервать развёртку"),
        ActionSlot("marker_peak", doc="маркер на максимум"),
        ActionSlot("reset"),
    ),
)

# ------------------------------------------------------------------------ ВАЦ
NETWORK_ANALYZER = Profile(
    "network_analyzer", "Векторный анализатор цепей",
    doc="""Трасса ``trace`` - Trace(x в Hz, y в единицах формата: MLOG - dB, PHASE/UPHASE - deg,
    GDELAY - s, SWR/LIN_MAG/REAL/IMAG - без единиц), публикуется после каждой ЗАВЕРШЁННОЙ развёртки.
    Поэтому у ключа ``trace`` единица в профиле пустая, а настоящая - в ``Trace.y_unit``.
    Драйвер работает с одним измерением (S-параметром) первого канала.""",
    slots=(
        Slot("trace", T, "", required=True, readonly=True),
        Slot("meas.continuous", B, required=True),
        Slot("meas.armed", B, readonly=True),
        Slot("sparam", E, required=True, choices=("S11", "S21", "S12", "S22")),
        Slot("format", E, required=True,
             choices=("MLOG", "PHASE", "UPHASE", "GDELAY", "SWR", "LIN_MAG", "REAL", "IMAG")),
        Slot("freq.start", F, "Hz", required=True),
        Slot("freq.stop", F, "Hz", required=True),
        Slot("freq.center", F, "Hz"),
        Slot("freq.span", F, "Hz"),
        Slot("sweep.points", I, required=True),
        Slot("sweep.time", F, "s"),
        Slot("sweep.time_auto", B),
        Slot("bw.if", F, "Hz", required=True, doc="полоса ПЧ"),
        Slot("source.power", F, "dBm", required=True),
        Slot("rf.output", B, doc="мощность источника включена"),
        Slot("avg.state", B),
        Slot("avg.count", I),
        Slot("cal.state", B, readonly=None, doc="коррекция (калибровка) включена"),
        Slot("marker.state", B),
        Slot("marker.freq", F, "Hz"),
        Slot("marker.value", F, "", readonly=True, doc="значение трассы в маркере, единицы - как у трассы"),
    ),
    actions=(
        ActionSlot("single", doc="одна развёртка"),
        ActionSlot("abort"),
        ActionSlot("marker_peak", doc="маркер на максимум"),
        ActionSlot("marker_min", doc="маркер на минимум"),
        ActionSlot("reset"),
    ),
)

PROFILES = {p.name: p for p in (POWER_SUPPLY, FUNCTION_GENERATOR, SIGNAL_GENERATOR, POWER_METER,
                                SPECTRUM_ANALYZER, NETWORK_ANALYZER)}
