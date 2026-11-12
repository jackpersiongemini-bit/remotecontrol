"""Слой 4: координатор. Этот модуль - входной порт слоя (им пользуется слой 5, UI).

UI <> координатор <> абстрактные приборы (профили) <> драйверы <> pyvisa. Координатор знает
приборы только по профилям и ключам:

* ``COORD`` (``Coordinator``) - приборы процесса: ``open_instrument`` / ``close_instrument`` / ``close_all``,
  поиск по профилю (``find``), каталог драйверов (``kinds``, ``models``...), сценарии измерений над
  несколькими приборами (``start``; готовые - ``FrequencySweep``, ``AutoCalibration`` ACAL);
* ``Instrument`` - прибор, с которым работает окно: ключи и действия профиля, значения, статусы, сигналы;
  пока прибор ведёт сценарий, запросы окна, которые меняют прибор, в него не уходят;
* ``LINKS`` - реестр приборов процесса (его ведёт ``COORD``);
* ``ActivityMonitor`` - кто что сейчас измеряет (главное окно);
* ``calibration`` - таблицы поправки по частоте (CSV) и подбор GAIN.

Выходной порт слоя - порт абстрактных приборов (:mod:`visalab.instruments`).
"""
from .activity import Activity, ActivityMonitor, Source, Thresholds
from .calibration import fit_gain_table, interpolate, load_csv, save_csv
from .coordinator import COORD, Coordinator
from .instrument import Instrument
from .registry import LINKS, ProxyLinks, profile_name
from .scenario import Device, Run, Scenario, ScenarioError, Stopped
from .scenarios import AcalResult, AutoCalibration, FrequencySweep, acal_file_name, frequency_points

__all__ = ["COORD", "Coordinator", "Instrument", "LINKS", "ProxyLinks", "profile_name", "ActivityMonitor", "Activity",
           "Source", "Thresholds", "Scenario", "Run", "Device", "ScenarioError", "Stopped",
           "FrequencySweep", "AutoCalibration", "AcalResult", "acal_file_name", "frequency_points",
           "load_csv", "save_csv", "interpolate", "fit_gain_table"]
