"""Готовые сценарии координатора.

* :class:`FrequencySweep` - генератор (signal_generator) перебирает частоты, ваттметр (power_meter)
  меряет мощность на каждой: частота датчика ставится вместе с частотой генератора, ваттметр
  на время сценария - в свободном счёте.
* :class:`AutoCalibration` (ACAL, кнопка ▶ на вкладке CAL окна ваттметра) - то же по START / STOP / PTS,
  затем подбор GAIN и таблицы поправки CSV (``fit_gain_table``): исправленное показание ваттметра
  равно уровню генератора, то есть тракт генератор -> датчик учтён.

В конце (и при остановке / ошибке) всё, что сценарий менял, возвращается как было: сначала выход
ВЧ генератора, потом настройки ваттметра, потом частоты. Прибор, с которым пропала связь, вернуть
нельзя - выход, который включил сценарий, координатор выключит, когда прибор снова подключится.

На каждой точке: показание берётся, когда устоялось (датчик ещё усредняет значения до смены частоты);
нет сигнала (ниже ``MIN_SIGNAL_DBM``) - отказ с понятной причиной; частоту или выход генератора
поменяли руками во время сценария - отказ (точки были бы не на тех частотах).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable

from ..core.trace import Trace
from ..core.units import si_format
from .calibration import fit_gain_table, save_csv
from .scenario import MEASURE_TIMEOUT_S, Device, Run, Scenario, ScenarioError, Stopped

__all__ = ["frequency_points", "sweep_points", "FrequencySweep", "AutoCalibration", "SourceAutoCalibration",
           "AcalResult", "acal_file_name", "SweepPoint", "SweepResult", "PowerMeterSweep", "SpectrumSweep"]


def frequency_points(start: float, stop: float, points: int) -> list[float]:
    """Равномерная сетка частот, края включены."""
    points = int(points)
    start_f, stop_f = float(start), float(stop)
    if points < 2 or not (math.isfinite(start_f) and math.isfinite(stop_f)) or not stop_f > start_f:
        raise ValueError("need START < STOP and PTS >= 2")
    step = (stop_f - start_f) / (points - 1)
    return [start_f + i * step for i in range(points - 1)] + [stop_f]


def sweep_points(start: float, stop: float, points: int, *, log: bool = False) -> list[float]:
    """Сетка точек для sweep-режимов (линейная или логарифмическая, допускает убывание)."""
    points = int(points)
    start_f, stop_f = float(start), float(stop)
    if not (math.isfinite(start_f) and math.isfinite(stop_f)):
        raise ValueError("START and STOP must be finite")
    if points == 1 and start_f == stop_f:
        return [start_f]
    if points < 2 or start_f == stop_f:
        raise ValueError("need START != STOP and PTS >= 2")
    if log:
        if start_f <= 0.0 or stop_f <= 0.0:
            raise ValueError("LOG sweep requires START > 0 and STOP > 0")
        ratio = stop_f / start_f
        return [start_f * (ratio ** (i / (points - 1))) for i in range(points - 1)] + [stop_f]
    step = (stop_f - start_f) / (points - 1)
    return [start_f + i * step for i in range(points - 1)] + [stop_f]


def _compact(freq: float) -> str:
    return si_format(freq, "Hz", 6).replace(" ", "").replace("Hz", "")


def acal_file_name(start: float, stop: float, points: int, prefix: str = "ACAL") -> str:
    """Имя таблицы ACAL: ``ACAL_1G-18G_101.csv`` (та же полоса и число точек - файл перезаписывается)."""
    return f"{prefix}_{_compact(start)}-{_compact(stop)}_{int(points)}.csv"


class FrequencySweep(Scenario):
    """Генератор перебирает частоты, ваттметр меряет. Результат - [(Hz, dBm показания, dBm генератора)]."""

    NAME = "SWEEP"
    needs = ("signal_generator", "power_meter")
    source_profile = "signal_generator"
    freq_key = "rf.frequency"
    power_key = "rf.power"
    #: пауза после смены частоты до замера, с (генератор и датчик установились)
    SETTLE_S = 0.3
    #: показание устоялось: два подряд различаются не больше, dB
    STABLE_DB = 0.05
    #: показаний на точку самое большее (не устоялось - среднее двух последних и строка в журнале)
    MAX_READINGS = 6
    #: ниже - сигнала на датчике нет (кабель, выход генератора), dBm
    MIN_SIGNAL_DBM = -60.0

    def __init__(self, freqs, settle_s: float | None = None, skip: int = 1, count: int = 1,
                 timeout_s: float = MEASURE_TIMEOUT_S, stable_db: float | None = None,
                 max_readings: int | None = None, min_signal_dbm: float | None = None):
        self.freqs = [float(f) for f in freqs]
        if not self.freqs:
            raise ValueError("no frequencies")
        self.settle_s = self.SETTLE_S if settle_s is None else float(settle_s)
        self.skip, self.count, self.timeout_s = int(skip), int(count), timeout_s
        self.stable_db = self.STABLE_DB if stable_db is None else float(stable_db)
        self.max_readings = self.MAX_READINGS if max_readings is None else max(1, int(max_readings))
        self.min_signal_dbm = self.MIN_SIGNAL_DBM if min_signal_dbm is None else float(min_signal_dbm)
        self.points: list[tuple[float, float, float]] = []

    # ------------------------------------------------------------ шаги
    def steps(self, run: Run):
        gen, pm = run.device(self.source_profile), run.device("power_meter")
        fk, pk = self.freq_key, self.power_key
        gen.require(fk, pk, "rf.output")
        pm.require("meas.power")
        for f in self.freqs:                     # вне диапазона модели - отказ до первой команды
            gen.check(fk, f)
            if pm.supports("freq"):
                pm.check("freq", f)
        if gen.value(pk) is None:
            raise ScenarioError(f"{gen.name}: level ({pk}) unknown - sync the source")
        self.check(run, gen, pm)
        saved: list[tuple[Device, str, object]] = []      # что вернуть (в обратном порядке)
        self.points = []
        try:
            self.remember(saved, gen, fk)
            if pm.supports("freq"):
                self.remember(saved, pm, "freq")
            yield from self.prepare(run, gen, pm, saved)
            n = len(self.freqs)
            for i, f in enumerate(self.freqs):
                run.report(i, n, si_format(f, "Hz", 6))
                sets = [gen.set(fk, f)]
                if pm.supports("freq"):
                    sets.append(pm.set("freq", f))           # поправка датчика на этой частоте
                # что приборы подтвердили (могли округлить) - с этим и сверять, не поменял ли кто
                confirmed = list(zip(((gen, fk), (pm, "freq")), (yield sets)))
                if self.settle_s > 0:
                    yield run.sleep(self.settle_s)
                power = yield from self.reading(run, pm, f)
                if not gen.connected:
                    raise ScenarioError(f"{gen.name}: connection lost during {self.NAME}")
                raw_level = gen.value(pk)
                if raw_level is None:
                    raise ScenarioError(f"{gen.name}: level ({pk}) lost during {self.NAME}")
                level = float(raw_level)
                self.check_point(gen, pm, f, power, level, confirmed)
                self.points.append((f, power, level))
                run.add_point({"freq": f, "power": power, "level": level, "index": i, "total": n})
            run.report(n, n, "done")
            result = self.finish(run, gen, pm)
        finally:
            yield from self.restore(run, saved)
        return result

    def reading(self, run: Run, pm: Device, f: float):
        """Показание на точке, когда устоялось: после смены частоты датчик ещё усредняет значения
        до неё - ждать, пока два подряд совпадут (``STABLE_DB``), но не больше ``MAX_READINGS``."""
        prev = float((yield pm.measure("meas.power", self.skip, self.count, self.timeout_s)))
        if self.max_readings <= 1:
            return prev
        cur = prev
        for _ in range(self.max_readings - 1):
            cur = float((yield pm.measure("meas.power", 0, self.count, self.timeout_s)))
            if abs(cur - prev) <= self.stable_db:
                return (cur + prev) / 2
            prev_, prev = prev, cur
        run.log(f"{si_format(f, 'Hz', 6)}: reading not settled in {self.max_readings} "
                f"({prev_:+.2f} / {cur:+.2f} dBm), mean taken")
        return (cur + prev_) / 2

    def check_point(self, gen: Device, pm: Device, f: float, power: float, level: float,
                    confirmed=()) -> None:
        """Сигнал на датчике есть; выход генератора включён и частоты те, что приборы подтвердили на этой
        точке (``confirmed`` - [((прибор, ключ), значение)]): никто не поменял их руками во время сценария."""
        if not math.isfinite(power) or power < self.min_signal_dbm:
            raise ScenarioError(f"no signal at {pm.name}: {power:.1f} dBm at {si_format(f, 'Hz', 6)} "
                                f"({gen.name} {level:+.1f} dBm) - check the cable and the generator output")
        if not gen.connected:
            raise ScenarioError(f"{gen.name}: connection lost during {self.NAME}")
        if gen.value("rf.output") is False:
            raise ScenarioError(f"{gen.name} RF output was switched off during {self.NAME}")
        for (dev, key), expected in confirmed:
            actual = dev.value(key)
            if actual is None or expected is None:
                continue
            if not dev.spec(key).same(expected, actual):
                raise ScenarioError(f"{dev.name} {key} changed during {self.NAME}: "
                                    f"{si_format(actual, 'Hz', 6)} instead of {si_format(expected, 'Hz', 6)}")

    # ------------------------------------------------------------ хуки
    def check(self, run: Run, gen: Device, pm: Device) -> None:
        """Проверки до первой команды (ScenarioError - не начинать)."""

    def prepare(self, run: Run, gen: Device, pm: Device, saved: list):
        """Ваттметр - свободный счёт (показания приходят при опросе), генератор - ВЧ включён."""
        if pm.supports("trig.source"):
            yield from self.change(saved, pm, "trig.source", "IMM")
        yield from self.change(saved, pm, "meas.continuous", True)
        yield from self.change(saved, gen, "rf.output", True)

    def finish(self, run: Run, gen: Device, pm: Device):
        return list(self.points)

    # ------------------------------------------------------------ установка с возвратом
    @staticmethod
    def remember(saved: list, dev: Device, key: str) -> None:
        saved.append((dev, key, dev.value(key)))

    def change(self, saved: list, dev: Device, key: str, value):
        """Поставить значение на время сценария (если оно другое) и запомнить, что вернуть."""
        old = dev.value(key)
        if old is None and dev.connected and dev.supports(key):
            try:
                old = yield dev.read(key)
            except ScenarioError:
                old = False if key.endswith("output") and value is True else None
        spec = dev.spec(key)
        if old is not None and spec.same(spec.validate(value), old):
            return
        saved.append((dev, key, old))
        yield dev.set(key, value)

    @staticmethod
    def restore(run: Run, saved: list):
        """Вернуть всё в обратном порядке; ошибка одного ключа не мешает остальным. С прибором нет связи:
        выход, который включил сценарий, выключится, когда прибор снова подключится (координатор);
        остальное (частоты...) так не возвращается - после переподключения это было бы неожиданно."""
        lost: dict[str, list[str]] = {}

        def later(dev: Device, key: str, value) -> None:
            if value is False and key.endswith("output"):
                run.defer_restore(dev, key, value)
                lost.setdefault(dev.name, []).append(f"{key} OFF when it reconnects")
            else:
                lost.setdefault(dev.name, []).append(f"{key} not returned")

        while saved:
            dev, key, value = saved.pop()
            if value is None:
                continue
            if not dev.connected:
                later(dev, key, value)
                continue
            try:
                yield dev.set(key, value)
            except Stopped:
                # Безопасность: не бросать выключение RF при повторной остановке в середине restore
                if value is False and key.endswith("output"):
                    if dev.connected:
                        dev.proxy.set(dev.k(key), False)
                    else:
                        later(dev, key, value)
                else:
                    run.log(f"restore interrupted before {dev.name} {key}")
            except ScenarioError as exc:
                if dev.connected:
                    run.log(f"could not restore {dev.name} {key}: {exc}")
                else:
                    later(dev, key, value)
        for name, items in lost.items():
            run.log(f"{name}: no connection - " + ", ".join(items))


@dataclass
class AcalResult:
    gain: float                                   # dB, средний коэффициент передачи тракта
    table: list[tuple[float, float]]              # CAL(f), dB: неравномерность (GAIN − R)
    response: list[tuple[float, float]]           # R(f), dB: показание + DUTY − уровень генератора
    duty_db: float = 0.0                          # учтённая поправка скважности (генератор с импульсной модуляцией)
    path: Path | None = None                      # куда записана таблица
    points: list = field(default_factory=list)    # (Hz, dBm показания, dBm генератора)


class AutoCalibration(FrequencySweep):
    """ACAL: перебор START..STOP по PTS точкам, ваттметр без поправки (offset выключен на время),
    затем GAIN = среднее R, таблица CAL(f) = GAIN − R(f) в CSV ``path``.

    ``duty_db`` - поправка скважности из окна (10·lg(100 / DUTY)); учитывается, только если у
    генератора включена импульсная модуляция (тогда ваттметр видит среднюю мощность импульсов)."""

    NAME = "ACAL"

    def __init__(self, start: float, stop: float, points: int, path=None, duty_db: float = 0.0, **kw):
        super().__init__(frequency_points(start, stop, points), **kw)
        self.start, self.stop, self.npoints = float(start), float(stop), int(points)
        self.path = Path(path) if path else None
        self.duty_db = float(duty_db or 0.0)
        self.pulsed = False

    def check(self, run: Run, gen: Device, pm: Device) -> None:
        self.pulsed = gen.value("pulse.state") is True and gen.value("mod.output") is not False

    def prepare(self, run: Run, gen: Device, pm: Device, saved: list):
        # «сырые» показания: поправку окна (offset) на время калибровки выключить
        if pm.supports("offset.state"):
            yield from self.change(saved, pm, "offset.state", False)
        elif pm.supports("offset"):
            yield from self.change(saved, pm, "offset", 0.0)
        yield from super().prepare(run, gen, pm, saved)

    def finish(self, run: Run, gen: Device, pm: Device) -> AcalResult:
        duty = self.duty_db if self.pulsed else 0.0
        response = [(f, p + duty - level) for f, p, level in self.points]
        gain, table = fit_gain_table(response)
        spread = max(d for _f, d in table) - min(d for _f, d in table) if table else 0.0
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                save_csv(self.path, table)
            except OSError as exc:
                raise ScenarioError(f"cannot save {self.path.name}: {exc}") from None
        run.log(f"done: GAIN {gain:+.2f} dB, flatness {spread:.2f} dB p-p, {len(table)} points"
                + (f", DUTY {duty:+.2f} dB" if duty else "") + (f", table {self.path.stem}" if self.path is not None else ""))
        return AcalResult(gain=gain, table=table, response=response, duty_db=duty, path=self.path,
                          points=list(self.points))


class SourceAutoCalibration(AutoCalibration):
    """ACAL источника СВЧ (генератор ``signal_generator`` или ВАЦ ``network_analyzer`` в CW) по ваттметру.

    На время измерения выключается поправка источника (``power.offset_state``), а ваттметр выступает
    эталоном в опорной плоскости (его собственная поправка не сбрасывается). Для ВАЦ на время
    сценария включается режим генератора (``gen.state``). Результат - GAIN и таблица CAL(f) CSV:
    поправка источника ``CAL(f) − GAIN`` компенсирует потери / усиление выходного тракта."""

    NAME = "SRC ACAL"

    def __init__(self, start: float, stop: float, points: int, source_profile: str = "signal_generator",
                 path=None, duty_db: float = 0.0, **kw):
        self.source_profile = source_profile
        self.needs = (source_profile, "power_meter")
        if source_profile == "network_analyzer":
            self.freq_key = "freq.cw"
            self.power_key = "source.power"
        else:
            self.freq_key = "rf.frequency"
            self.power_key = "rf.power"
        super().__init__(start, stop, points, path=path, duty_db=duty_db, **kw)

    def check(self, run: Run, gen: Device, pm: Device) -> None:
        if self.source_profile == "signal_generator":
            self.pulsed = gen.value("pulse.state") is True and gen.value("mod.output") is not False
        else:
            self.pulsed = False

    def prepare(self, run: Run, gen: Device, pm: Device, saved: list):
        if gen.supports("power.offset_state"):
            yield from self.change(saved, gen, "power.offset_state", False)
        if self.source_profile == "network_analyzer" and gen.supports("gen.state"):
            yield from self.change(saved, gen, "gen.state", True)
        yield from FrequencySweep.prepare(self, run, gen, pm, saved)


# ===================================================================== Sweep-режимы (ваттметр и спектр)
@dataclass(frozen=True)
class SweepPoint:
    """Одна измеренная точка sweep-сценария."""

    freq: float                         # Hz
    src_power: float                    # dBm (уровень источника)
    power: float                        # dBm (измеренная мощность)
    gain: float                         # dB (power - src_power)
    floor: float | None = None          # dBm (шумовая дорожка спектра, если есть)
    peak_freq: float | None = None      # Hz (частота пика спектра, если есть)


@dataclass
class SweepResult:
    """Результат sweep-сценария: точки, трассы для графиков/CSV и краткая сводка."""

    mode: str                           # "FREQ" | "POW" | "2D"
    receiver: str                       # "power_meter" | "spectrum_analyzer"
    points: list[SweepPoint] = field(default_factory=list)

    def traces(self, view: str = "POUT") -> list[tuple[str, Trace]]:
        """Построить ``[(подпись, Trace)]`` для отображения (``view`` = ``POUT`` или ``GAIN``)."""
        if not self.points:
            return []
        use_gain = (view == "GAIN")
        y_unit = "dB" if use_gain else "dBm"
        y_name = "Gain" if use_gain else "Power"

        def val(pt: SweepPoint) -> float:
            return pt.gain if use_gain else pt.power

        if self.mode == "POW":
            pairs = sorted((pt.src_power, val(pt)) for pt in self.points if math.isfinite(val(pt)))
            if not pairs:
                return []
            xs, ys = zip(*pairs)
            f0 = self.points[0].freq
            label = f"{y_name} @ {si_format(f0, 'Hz', 4)}"
            return [(label, Trace(tuple(xs), tuple(ys), x_unit="dBm", y_unit=y_unit, name=label, uniform=False))]

        if self.mode == "2D":
            groups: dict[float, list[tuple[float, float]]] = {}
            for pt in self.points:
                v = val(pt)
                if math.isfinite(v):
                    pk = round(pt.src_power, 3)
                    groups.setdefault(pk, []).append((pt.freq, v))
            out: list[tuple[str, Trace]] = []
            for pk, pairs in groups.items():
                pairs.sort(key=lambda xy: xy[0])
                xs, ys = zip(*pairs)
                label = f"{pk:g} dBm"
                out.append((label, Trace(tuple(xs), tuple(ys), x_unit="Hz", y_unit=y_unit, name=label, uniform=False)))
            return out

        # FREQ
        pairs = sorted((pt.freq, val(pt)) for pt in self.points if math.isfinite(val(pt)))
        if not pairs:
            return []
        xs, ys = zip(*pairs)
        p0 = self.points[0].src_power
        label = f"{y_name} ({p0:g} dBm)"
        return [(label, Trace(tuple(xs), tuple(ys), x_unit="Hz", y_unit=y_unit, name=label, uniform=False))]

    def p1db(self) -> tuple[float, float, float] | None:
        """Для режима ``POW``: точка 1 дБ компрессии ``(P_in_1dB, P_out_1dB, G0)`` или None."""
        if self.mode != "POW" or len(self.points) < 3:
            return None
        pts = sorted((pt for pt in self.points if math.isfinite(pt.gain) and math.isfinite(pt.src_power)),
                     key=lambda pt: pt.src_power)
        if len(pts) < 3:
            return None
        g0 = (pts[0].gain + pts[1].gain) / 2.0 if abs(pts[0].gain - pts[1].gain) <= 0.2 else pts[0].gain
        target = g0 - 1.0
        for i in range(1, len(pts)):
            prev, cur = pts[i - 1], pts[i]
            if cur.gain <= target and prev.gain > target and prev.gain != cur.gain:
                t = (prev.gain - target) / (prev.gain - cur.gain)
                pin = prev.src_power + t * (cur.src_power - prev.src_power)
                return pin, pin + target, g0
        return None

    def summary(self, view: str = "POUT") -> str:
        """Краткая строка сводки для UI и журнала."""
        if not self.points:
            return "NO DATA"
        use_gain = (view == "GAIN")
        unit = "dB" if use_gain else "dBm"
        vals = [pt.gain if use_gain else pt.power for pt in self.points if math.isfinite(pt.gain if use_gain else pt.power)]
        if not vals:
            return "NO DATA"
        lo, hi = min(vals), max(vals)
        if self.mode == "POW":
            comp = self.p1db()
            base = f"{len(vals)} pts  MIN {lo:+.2f} / MAX {hi:+.2f} {unit}"
            if comp is not None:
                return f"{base}  P1dB {comp[0]:+.1f} in / {comp[1]:+.1f} out dBm"
            return base
        if self.mode == "2D":
            n_curves = len({round(pt.src_power, 3) for pt in self.points})
            return f"{n_curves} lvl · {len(vals)} pts  MIN {lo:+.2f} / MAX {hi:+.2f} {unit}"
        return f"{len(vals)} pts  MIN {lo:+.2f} / MAX {hi:+.2f} {unit}  Δ {hi - lo:.2f} dB"


class _CoordinatedSweep(Scenario):
    """Общая база для sweep-сценариев «источник СВЧ (генератор / ВАЦ в CW) + приёмник (PM / SA)»."""

    receiver_profile: str = ""
    SETTLE_S = 0.2

    def __init__(self, mode: str = "FREQ", *, freqs=(), powers=(), settle_s: float | None = None,
                 repeat: bool = False, timeout_s: float = MEASURE_TIMEOUT_S):
        mode = str(mode).upper()
        if mode not in ("FREQ", "POW", "2D"):
            raise ValueError(f"unknown sweep mode: {mode!r}")
        self.mode = mode
        self.freqs = [float(f) for f in freqs]
        self.powers = [float(p) for p in powers]
        if mode in ("FREQ", "2D") and not self.freqs:
            raise ValueError("need frequency points for FREQ / 2D sweep")
        if mode in ("POW", "2D") and not self.powers:
            raise ValueError("need power points for POW / 2D sweep")
        self.settle_s = self.SETTLE_S if settle_s is None else max(0.0, float(settle_s))
        self.repeat = bool(repeat)
        self.timeout_s = float(timeout_s)
        self.points: list[SweepPoint] = []

    def _schedule(self) -> list[tuple[float | None, float | None]]:
        if self.mode == "FREQ":
            return [(f, None) for f in self.freqs]
        if self.mode == "POW":
            return [(None, p) for p in self.powers]
        return [(f, p) for p in self.powers for f in self.freqs]

    def _validate_devices(self, gen: Device, rx: Device) -> None:
        gen.require("rf.frequency", "rf.power", "rf.output")
        for f in self.freqs:
            gen.check("rf.frequency", f)
        for p in self.powers:
            gen.check("rf.power", p)
        if self.mode == "FREQ" and gen.value("rf.power") is None:
            raise ScenarioError(f"{gen.name}: level (rf.power) unknown - sync the source")
        if self.mode == "POW" and gen.value("rf.frequency") is None:
            raise ScenarioError(f"{gen.name}: frequency (rf.frequency) unknown - sync the source")

    def _prepare_receiver(self, run: Run, gen: Device, rx: Device, saved: list):
        raise NotImplementedError

    def _measure_point(self, run: Run, gen: Device, rx: Device, target_f: float | None,
                       target_p: float | None, first: bool):
        raise NotImplementedError

    def steps(self, run: Run):
        gen = run.device("signal_generator")
        rx = run.device(self.receiver_profile)
        self._validate_devices(gen, rx)
        schedule = self._schedule()
        saved: list[tuple[Device, str, object]] = []
        self.points = []
        try:
            FrequencySweep.remember(saved, gen, "rf.frequency")
            FrequencySweep.remember(saved, gen, "rf.power")
            yield from self._prepare_receiver(run, gen, rx, saved)
            yield from FrequencySweep.change(self, saved, gen, "rf.output", True)
            n = len(schedule)
            cycle = 0
            while True:
                if cycle > 0:
                    self.points = []
                for i, (f, p) in enumerate(schedule):
                    if not gen.connected:
                        raise ScenarioError(f"{gen.name}: connection lost during {self.NAME}")
                    if gen.value("rf.output") is False:
                        raise ScenarioError(f"{gen.name} RF output was switched off during {self.NAME}")
                    label = si_format(f, "Hz", 5) if f is not None else f"{p:g} dBm"
                    if f is not None and p is not None:
                        label = f"{p:g} dBm · {si_format(f, 'Hz', 4)}"
                    run.report(i, n, label)
                    pt = yield from self._measure_point(run, gen, rx, f, p, first=(i == 0 and cycle == 0))
                    self.points.append(pt)
                    run.add_point({
                        "freq": pt.freq, "src_power": pt.src_power, "power": pt.power,
                        "gain": pt.gain, "floor": pt.floor, "peak_freq": pt.peak_freq,
                        "index": i, "total": n, "cycle": cycle,
                    })
                run.report(n, n, "done" if not self.repeat else f"cycle {cycle + 1}")
                if not self.repeat:
                    break
                cycle += 1
            res = SweepResult(mode=self.mode, receiver=self.receiver_profile, points=list(self.points))
            run.log(f"{self.mode} done: {res.summary('POUT')}")
            return res
        finally:
            yield from FrequencySweep.restore(run, saved)


class PowerMeterSweep(_CoordinatedSweep):
    """Sweep-режим ваттметра: перебор частоты (FREQ), мощности источника (POW) или 2D (FREQ × POW).

    В отличие от ACAL сохраняет действующую калибровку ваттметра (и при наличии ``offset_fn`` обновляет
    табличную поправку CAL(f) на каждой частоте) и не обрывает измерение на слабом сигнале.
    """

    NAME = "PM SWP"
    needs = ("signal_generator", "power_meter")
    receiver_profile = "power_meter"
    SETTLE_S = 0.2
    STABLE_DB = 0.05
    MAX_READINGS = 4

    def __init__(self, mode: str = "FREQ", *, freqs=(), powers=(), settle_s: float | None = None,
                 skip: int = 1, count: int = 1, stable_db: float | None = None,
                 max_readings: int | None = None, repeat: bool = False,
                 offset_fn: Callable[[float], float | None] | None = None,
                 timeout_s: float = MEASURE_TIMEOUT_S):
        super().__init__(mode, freqs=freqs, powers=powers, settle_s=settle_s, repeat=repeat, timeout_s=timeout_s)
        self.skip = max(0, int(skip))
        self.count = max(1, int(count))
        self.stable_db = self.STABLE_DB if stable_db is None else float(stable_db)
        self.max_readings = self.MAX_READINGS if max_readings is None else max(1, int(max_readings))
        self.offset_fn = offset_fn

    def _validate_devices(self, gen: Device, rx: Device) -> None:
        super()._validate_devices(gen, rx)
        rx.require("meas.power")
        if rx.supports("freq"):
            for f in self.freqs:
                rx.check("freq", f)

    def _prepare_receiver(self, run: Run, gen: Device, pm: Device, saved: list):
        if pm.supports("freq"):
            FrequencySweep.remember(saved, pm, "freq")
        if self.offset_fn is not None:
            if pm.supports("offset"):
                FrequencySweep.remember(saved, pm, "offset")
            if pm.supports("offset.state"):
                FrequencySweep.remember(saved, pm, "offset.state")
        if pm.supports("trig.source"):
            yield from FrequencySweep.change(self, saved, pm, "trig.source", "IMM")
        yield from FrequencySweep.change(self, saved, pm, "meas.continuous", True)

    def _read_settled(self, run: Run, pm: Device):
        prev = float((yield pm.measure("meas.power", self.skip, self.count, self.timeout_s)))
        if self.max_readings <= 1:
            return prev
        cur = prev
        prev_ = prev
        for _ in range(self.max_readings - 1):
            cur = float((yield pm.measure("meas.power", 0, self.count, self.timeout_s)))
            if abs(cur - prev) <= self.stable_db:
                return (cur + prev) / 2.0
            prev_, prev = prev, cur
        return (cur + prev_) / 2.0

    def _measure_point(self, run: Run, gen: Device, pm: Device, target_f: float | None,
                       target_p: float | None, first: bool):
        sets = []
        actual_f = target_f if target_f is not None else float(gen.value("rf.frequency"))
        if target_f is not None:
            sets.append(gen.set("rf.frequency", target_f))
            if pm.supports("freq"):
                sets.append(pm.set("freq", target_f))
        elif first and pm.supports("freq"):
            sets.append(pm.set("freq", actual_f))
        if target_p is not None:
            sets.append(gen.set("rf.power", target_p))
        if self.offset_fn is not None and (target_f is not None or first):
            off = self.offset_fn(actual_f)
            if off is not None:
                if pm.supports("offset"):
                    sets.append(pm.set("offset", off))
                if pm.supports("offset.state"):
                    sets.append(pm.set("offset.state", abs(off) > 5e-4))
        if sets:
            yield sets
        if self.settle_s > 0:
            yield run.sleep(self.settle_s)
        power = yield from self._read_settled(run, pm)
        src_p = float(target_p if target_p is not None else gen.value("rf.power"))
        return SweepPoint(freq=actual_f, src_power=src_p, power=power, gain=power - src_p)


class SpectrumSweep(_CoordinatedSweep):
    """Sweep-режим анализатора спектра: перебор частоты (FREQ), мощности (POW) или 2D (FREQ × POW).

    В режиме слежения (``track_center=True``) центр анализатора перестраивается синхронно с генератором,
    на каждой точке выполняется свежая однократная развёртка (``single``) и берётся пик (или среднее в
    нулевой полосе ``track_span=0``) и шумовая дорожка.
    """

    NAME = "SA SWP"
    needs = ("signal_generator", "spectrum_analyzer")
    receiver_profile = "spectrum_analyzer"
    SETTLE_S = 0.05

    def __init__(self, mode: str = "FREQ", *, freqs=(), powers=(), settle_s: float | None = None,
                 track_center: bool = True, track_span: float | None = None,
                 repeat: bool = False, timeout_s: float = MEASURE_TIMEOUT_S):
        super().__init__(mode, freqs=freqs, powers=powers, settle_s=settle_s, repeat=repeat, timeout_s=timeout_s)
        self.track_center = bool(track_center)
        self.track_span = None if track_span is None else float(track_span)

    def _validate_devices(self, gen: Device, sa: Device) -> None:
        super()._validate_devices(gen, sa)
        sa.require("trace", "freq.center")
        if self.track_center:
            for f in self.freqs:
                sa.check("freq.center", f)
        if self.track_span is not None:
            sa.check("freq.span", self.track_span)

    def _prepare_receiver(self, run: Run, gen: Device, sa: Device, saved: list):
        FrequencySweep.remember(saved, sa, "freq.center")
        if sa.supports("freq.span"):
            FrequencySweep.remember(saved, sa, "freq.span")
        if sa.supports("meas.continuous"):
            yield from FrequencySweep.change(self, saved, sa, "meas.continuous", False)
        if sa.supports("trace.mode"):
            yield from FrequencySweep.change(self, saved, sa, "trace.mode", "WRITE")
        if self.track_span is not None and sa.supports("freq.span"):
            yield from FrequencySweep.change(self, saved, sa, "freq.span", self.track_span)

    def _measure_point(self, run: Run, gen: Device, sa: Device, target_f: float | None,
                       target_p: float | None, first: bool):
        actual_f = target_f if target_f is not None else float(gen.value("rf.frequency"))
        sets = []
        if target_f is not None:
            sets.append(gen.set("rf.frequency", target_f))
            if self.track_center:
                sets.append(sa.set("freq.center", target_f))
        elif first and self.track_center:
            sets.append(sa.set("freq.center", actual_f))
        if target_p is not None:
            sets.append(gen.set("rf.power", target_p))
        if sets:
            yield sets
        if self.settle_s > 0:
            yield run.sleep(self.settle_s)
        # Сначала подписываемся на trace, одновременно запускаем single - без гонки на быстром симуляторе
        tr, _act = yield (
            sa.measure("trace", skip=0, count=1, timeout_s=self.timeout_s),
            sa.action("single", timeout_s=self.timeout_s),
        )
        if not isinstance(tr, Trace) or len(tr) == 0:
            raise ScenarioError(f"{sa.name}: empty trace during {self.NAME}")
        finite = [float(y) for y in tr.y if math.isfinite(y)]
        if not finite:
            raise ScenarioError(f"{sa.name}: non-finite trace during {self.NAME}")
        if tr.x_unit == "s":
            power = sum(finite) / len(finite)
            floor = None
            peak_freq = actual_f
        else:
            peak_freq, power = tr.peak()
            finite_sorted = sorted(finite)
            floor = finite_sorted[len(finite_sorted) // 2]
        src_p = float(target_p if target_p is not None else gen.value("rf.power"))
        return SweepPoint(freq=actual_f, src_power=src_p, power=float(power),
                          gain=float(power) - src_p, floor=floor, peak_freq=peak_freq)
