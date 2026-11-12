"""Журнал программы в файл: что случилось с приборами и сценариями, пока никто не смотрел на окна.

``setup_logging(path)`` - консоль (как раньше) и файл ``logs/visalab.log`` рядом с ini: по 2 МБ, 5 старых
копий (``visalab.log.1`` ...). В файл попадает:

* подключение (с ``*IDN?``), обрыв и восстановление связи, переподключение;
* ошибки связи и программы, ошибки прибора (``SYST:ERR?``), ошибки записи параметров;
* сценарии координатора (ACAL): начало, итог, что не удалось вернуть;
* необработанные ошибки программы - с трассировкой.

Одинаковые сообщения подряд пишутся раз в минуту (ключ, который не читается при каждом опросе,
не вытесняет из файла всё остальное).
"""
from __future__ import annotations

import logging
import logging.handlers
import platform
import sys
import time
from pathlib import Path

__all__ = ["setup_logging", "close_logging", "default_log_path", "Throttle", "FORMAT"]

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 2_000_000
BACKUPS = 5


def default_log_path(ini: str | Path) -> Path:
    """``logs/visalab.log`` рядом с ini окон (там же и calibrations/)."""
    return Path(ini).resolve().parent / "logs" / "visalab.log"


def _pyvisa_version() -> str:
    try:
        from importlib.metadata import version
        return version("pyvisa")
    except Exception:  # noqa: BLE001 - только для строки в журнале
        return "-"


def setup_logging(path: str | Path | None, level: int = logging.INFO) -> logging.Handler | None:
    """Консоль + файл ``path`` (None - только консоль). Возвращает обработчик файла (закрыть -
    ``close_logging``) или None, если файл не открылся (нет прав и т.п.: программа работает и так)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=FORMAT)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    if not path:
        return None
    path = Path(path).resolve()
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path:
            return h
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                       encoding="utf-8")
    except OSError as exc:
        logging.getLogger("visalab").warning("журнал в файл не открыт (%s): %s", path, exc)
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("visalab").info("старт: %s | Python %s | %s | pyvisa %s", " ".join(sys.argv),
                                      platform.python_version(), platform.platform(), _pyvisa_version())
    return handler


def close_logging(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


class Throttle:
    """Не повторять одно и то же сообщение чаще раза в ``interval_s`` (по ключу)."""

    def __init__(self, interval_s: float = 60.0, clock=time.monotonic):
        self.interval_s = interval_s
        self.clock = clock
        self._last: dict[str, tuple[str, float]] = {}

    def __call__(self, key: str, message: str) -> bool:
        """True - писать (новое сообщение по ключу или прошло ``interval_s``)."""
        now = self.clock()
        last = self._last.get(key)
        if last is not None and last[0] == message and now - last[1] < self.interval_s:
            return False
        self._last[key] = (message, now)
        return True
