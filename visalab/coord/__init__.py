"""Слой координатора: приборы процесса (реестр) и то, что над ними (кто что измеряет).

UI <> координатор <> абстрактные приборы (профили) <> драйверы <> pyvisa. Координатор знает
приборы только по профилям и ключам.
"""
from .activity import Activity, ActivityMonitor, Source, Thresholds
from .registry import LINKS, ProxyLinks, profile_name

__all__ = ["LINKS", "ProxyLinks", "profile_name", "ActivityMonitor", "Activity", "Source", "Thresholds"]
