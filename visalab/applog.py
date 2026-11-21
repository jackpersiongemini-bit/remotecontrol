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
import os
import platform
import sys
import time
from pathlib import Path

__all__ = ["setup_logging", "close_logging", "app_data_dir", "default_ini_path", "default_log_path",
           "Throttle", "FORMAT", "APP_NAME"]

APP_NAME = "visalab"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 2_000_000
BACKUPS = 5


def app_data_dir(app_name: str = APP_NAME) -> Path:
    """Папка данных программы: ``%APPDATA%/visalab`` на Windows, Application Support на macOS, XDG на Linux."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform.startswith("win"):
        return Path.home() / "AppData" / "Roaming" / app_name
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / app_name


def default_ini_path(filename: str = "visalab_front.ini", app_name: str = APP_NAME) -> Path:
    """Файл настроек в папке данных программы (``%APPDATA%/visalab/visalab_front.ini``)."""
    return app_data_dir(app_name) / filename


def default_log_path(ini: str | Path) -> Path:
    """``logs/visalab.log`` рядом с ini окон (там же и calibrations/)."""
    return Path(ini).resolve().parent / "logs" / "visalab.log"


def _pyvisa_version() -> str:
    try:
        from importlib.metadata import version
        return version("pyvisa")
    except Exception:  # noqa: BLE001 - только для строки в журнале
        return "-"


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler, не падающий с PermissionError, если файл лога открыт другим процессом на Windows."""

    def doRollover(self) -> None:  # noqa: N802 - logging
        try:
            super().doRollover()
        except PermissionError:
            pass


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
    try:
        resolved = Path(path).resolve()
        for h in root.handlers:
            if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == resolved:
                return h
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handler = _SafeRotatingFileHandler(resolved, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    except (OSError, RuntimeError) as exc:
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
