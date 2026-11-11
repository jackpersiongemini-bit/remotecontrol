"""Готовые сценарии координатора.

* :class:`FrequencySweep` - генератор (signal_generator) перебирает частоты, ваттметр (power_meter)
  меряет мощность на каждой: частота датчика ставится вместе с частотой генератора, ваттметр
  на время сценария - в свободном счёте.
* :class:`AutoCalibration` (ACAL, кнопка ▶ на вкладке CAL окна ваттметра) - то же по START / STOP / PTS,
  затем подбор GAIN и таблицы поправки CSV (``fit_gain_table``): исправленное показание ваттметра
  равно уровню генератора, то есть тракт генератор -> датчик учтён.

В конце (и при остановке / ошибке) всё, что сценарий менял, возвращается как было: сначала выход
ВЧ генератора, потом настройки ваттметра, потом частоты.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.scpi import si_format
from .calibration import fit_gain_table, save_csv
from .scenario import MEASURE_TIMEOUT_S, Device, Run, Scenario, ScenarioError

__all__ = ["frequency_points", "FrequencySweep", "AutoCalibration", "AcalResult", "acal_file_name"]


def frequency_points(start: float, stop: float, points: int) -> list[float]:
    """Равномерная сетка частот, края включены."""
    points = int(points)
    if points < 2 or not float(stop) > float(start):
        raise ValueError("need START < STOP and PTS >= 2")
    step = (float(stop) - float(start)) / (points - 1)
    return [float(start) + i * step for i in range(points - 1)] + [float(stop)]


def _compact(freq: float) -> str:
    return si_format(freq, "Hz", 6).replace(" ", "").replace("Hz", "")


def acal_file_name(start: float, stop: float, points: int) -> str:
    """Имя таблицы ACAL: ``ACAL_1G-18G_101.csv`` (та же полоса и число точек - файл перезаписывается)."""
    return f"ACAL_{_compact(start)}-{_compact(stop)}_{int(points)}.csv"


class FrequencySweep(Scenario):
    """Генератор перебирает частоты, ваттметр меряет. Результат - [(Hz, dBm показания, dBm генератора)]."""

    NAME = "SWEEP"
    needs = ("signal_generator", "power_meter")
    #: пауза после смены частоты до замера, с (генератор и датчик установились)
    SETTLE_S = 0.3

    def __init__(self, freqs, settle_s: float | None = None, skip: int = 1, count: int = 1,
                 timeout_s: float = MEASURE_TIMEOUT_S):
        self.freqs = [float(f) for f in freqs]
        if not self.freqs:
            raise ValueError("no frequencies")
        self.settle_s = self.SETTLE_S if settle_s is None else float(settle_s)
        self.skip, self.count, self.timeout_s = int(skip), int(count), timeout_s
        self.points: list[tuple[float, float, float]] = []

    # ------------------------------------------------------------ шаги
    def steps(self, run: Run):
        gen, pm = run.device("signal_generator"), run.device("power_meter")
        gen.require("rf.frequency", "rf.power", "rf.output")
        pm.require("meas.power")
        for f in self.freqs:                     # вне диапазона модели - отказ до первой команды
            gen.check("rf.frequency", f)
            if pm.supports("freq"):
                pm.check("freq", f)
        if gen.value("rf.power") is None:
            raise ScenarioError(f"{gen.name}: level (rf.power) unknown - sync the generator")
        self.check(run, gen, pm)
        saved: list[tuple[Device, str, object]] = []      # что вернуть (в обратном порядке)
        self.points = []
        try:
            self.remember(saved, gen, "rf.frequency")
            if pm.supports("freq"):
                self.remember(saved, pm, "freq")
            yield from self.prepare(run, gen, pm, saved)
            n = len(self.freqs)
            for i, f in enumerate(self.freqs):
                run.report(i, n, si_format(f, "Hz", 6))
                sets = [gen.set("rf.frequency", f)]
                if pm.supports("freq"):
                    sets.append(pm.set("freq", f))           # поправка датчика на этой частоте
                yield sets
                if self.settle_s > 0:
                    yield run.sleep(self.settle_s)
                power = float((yield pm.measure("meas.power", self.skip, self.count, self.timeout_s)))
                level = float(gen.value("rf.power"))
                self.points.append((f, power, level))
                run.add_point({"freq": f, "power": power, "level": level, "index": i, "total": n})
            run.report(n, n, "done")
            result = self.finish(run, gen, pm)
        finally:
            yield from self.restore(run, saved)
        return result

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
        spec = dev.proxy.params[key]
        if old is not None and spec.same(spec.validate(value), old):
            return
        saved.append((dev, key, old))
        yield dev.set(key, value)

    @staticmethod
    def restore(run: Run, saved: list):
        """Вернуть всё в обратном порядке; ошибка одного ключа не мешает остальным."""
        while saved:
            dev, key, value = saved.pop()
            if value is None or not dev.connected:
                continue
            try:
                yield dev.set(key, value)
            except ScenarioError as exc:
                run.log(f"could not restore {dev.name} {key}: {exc}")


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
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                save_csv(self.path, table)
            except OSError as exc:
                raise ScenarioError(f"cannot save {self.path.name}: {exc}") from None
        run.log(f"done: GAIN {gain:+.2f} dB, {len(table)} points" + (f", DUTY {duty:+.2f} dB" if duty else "")
                + (f", table {self.path.stem}" if self.path is not None else ""))
        return AcalResult(gain=gain, table=table, response=response, duty_db=duty, path=self.path,
                          points=list(self.points))
