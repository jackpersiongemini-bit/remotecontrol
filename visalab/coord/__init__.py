"""Слой координатора: всё, что работает с несколькими приборами сразу.

UI <> координатор <> абстрактные приборы (профили) <> драйверы <> pyvisa. Координатор знает
приборы только по профилям и ключам:

* ``LINKS`` - реестр приборов процесса (окна регистрируют свои прокси), поиск по профилю;
* ``ActivityMonitor`` - кто что сейчас измеряет (главное окно);
* ``COORD`` (``Coordinator``) - сценарии измерений над несколькими приборами (``scenario``,
  готовые - в ``scenarios``: перебор частот генератора с ваттметром, автокалибровка ACAL);
* ``calibration`` - таблицы поправки по частоте (CSV) и подбор GAIN.
"""
from .activity import Activity, ActivityMonitor, Source, Thresholds
from .calibration import fit_gain_table, interpolate, load_csv, save_csv
from .coordinator import COORD, Coordinator
from .registry import LINKS, ProxyLinks, profile_name
from .scenario import Device, Run, Scenario, ScenarioError, Stopped
from .scenarios import AcalResult, AutoCalibration, FrequencySweep, acal_file_name, frequency_points

__all__ = ["LINKS", "ProxyLinks", "profile_name", "ActivityMonitor", "Activity", "Source", "Thresholds",
           "COORD", "Coordinator", "Scenario", "Run", "Device", "ScenarioError", "Stopped",
           "FrequencySweep", "AutoCalibration", "AcalResult", "acal_file_name", "frequency_points",
           "load_csv", "save_csv", "interpolate", "fit_gain_table"]
