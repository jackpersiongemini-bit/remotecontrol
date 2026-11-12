"""Запуск окна прибора: адрес из аргументов / ini окна / lab.json, пересоздание по APPLY.

Прибор окна открывает и закрывает координатор (``COORD.open_instrument`` / ``close_instrument``) - окно
получает готовый прибор координатора и о драйверах и транспорте не знает.
Журнал программы - в ``logs/visalab.log`` рядом с ini (``--log``), см. :mod:`visalab.applog`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from ..applog import close_logging, default_log_path, setup_logging
from ..coord import COORD
from .instrument_window import saved_connection, saved_delay
from .models import all_models

__all__ = ["run_window", "run_windows", "remember_windows", "connection_for", "lab_entry"]

ROOT = Path(__file__).resolve().parents[2]
#: при выходе - сколько ждать, пока идущий сценарий вернёт приборы
QUIT_SCENARIO_WAIT_MS = 5000


def lab_entry(path: str, key: str) -> dict:
    """Запись прибора в lab.json ({address, model, delay}) или {}."""
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    entry = data.get(key, {})
    return entry if isinstance(entry, dict) else {}


def run_window(window_cls, lab_key: str, doc: str = "", argv=None) -> int:
    """``window_cls`` - наследник InstrumentWindow; ``lab_key`` - запись прибора в lab.json."""
    return run_windows([(window_cls, lab_key)], doc, argv)


def _log_exception(kind, value, tb) -> None:
    """Ошибка в слоте Qt: в журнал, а не аварийный выход всей программы (так делает PyQt6 по умолчанию)."""
    logging.getLogger("visalab").error("необработанная ошибка", exc_info=(kind, value, tb))


def run_windows(specs, doc: str = "", argv=None, parser=None, show: bool | None = True, setup=None) -> int:
    """Несколько окон в одном процессе (окна видят приборы друг друга через координатор).

    ``specs`` - [(window_cls, lab_key)]; ``--address`` относится к первому окну. ``parser`` -
    готовый ArgumentParser (если обёртке нужны свои флаги). ``show=False`` - окна приборов создаются
    скрытыми (их показывает главное окно); ``show=None`` - как были при выходе (ini WINDOWS/<прибор>/VISIBLE,
    сначала скрыты). ``setup(opened, args, settings)`` - вызывается после
    создания окон (главное окно, стенд симуляторов); что вернёт - живёт до выхода."""
    ap = parser or argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", metavar="ADDR")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--lab", default=str(ROOT / "lab.json"))
    ap.add_argument("--ini", default=str(ROOT / "visalab_front.ini"))
    ap.add_argument("--backend", default="")
    ap.add_argument("--latency", type=float, default=0.005, help="задержка симулятора, с")
    ap.add_argument("--no-connect", action="store_true")
    ap.add_argument("--log", metavar="FILE", help="журнал программы (по умолчанию logs/visalab.log рядом с ini)")
    args, _rest = ap.parse_known_args(argv)
    log_file = setup_logging(args.log or default_log_path(args.ini))

    sys.excepthook = _log_exception
    app = QApplication.instance() or QApplication(sys.argv[:1])
    settings = QSettings(args.ini, QSettings.Format.IniFormat)
    opened: list[dict] = []
    for n, (window_cls, lab_key) in enumerate(specs):
        visible = settings.value(f"WINDOWS/{lab_key}/VISIBLE", False, type=bool) if show is None else show
        opened.append(_open(window_cls, lab_key, args, settings, args.address if n == 0 else None, visible))
        opened[-1]["lab_key"] = lab_key
    keep = setup(opened, args, settings) if setup else None  # noqa: F841 - держим ссылку до выхода

    app.aboutToQuit.connect(lambda: _quit(opened))
    try:
        return app.exec()
    finally:
        logging.getLogger("visalab").info("выход")
        close_logging(log_file)


def _quit(opened: list[dict]) -> None:
    """Выход: сначала сценарии координатора возвращают приборы (генератор - ВЧ как было...), потом
    приборы закрываются параллельно."""
    COORD.stop_all(wait_ms=QUIT_SCENARIO_WAIT_MS)
    COORD.close_all([cur["proxy"] for cur in opened])


def remember_windows(opened: list[dict], settings: QSettings) -> None:
    """Запомнить, какие окна приборов открыты (вызывать ДО выхода: quit сам закрывает окна)."""
    for cur in opened:
        settings.setValue(f"WINDOWS/{cur['lab_key']}/VISIBLE", bool(cur["win"].isVisible()))
    settings.sync()


def connection_for(window_cls, lab_key: str, lab_path: str, settings: QSettings, sim: bool = False,
                   cli_address: str | None = None) -> dict:
    """Как подключать прибор окна: {kind, address, model, delay}. Адрес - из аргумента, иначе выбранный в окне
    (ini, APPLY), иначе lab.json; ``sim`` - симулятор."""
    lab = lab_entry(lab_path, lab_key)
    saved = saved_connection(settings, window_cls.CONNECTION_GROUP)     # выбранное в окне (адрес -> APPLY)
    kind, model = saved["driver"] or window_cls.DEFAULT_KIND, saved["model"] or None
    delay = saved_delay(settings, window_cls.CONNECTION_GROUP)          # DLY на странице адреса
    if delay is None:
        delay = lab.get("delay")
    if sim:
        address = "sim"
    elif cli_address:
        address = cli_address
    elif saved["address"]:
        address = saved["address"]
    else:
        address = lab.get("address", "sim")
        model = model or lab.get("model")
    return {"kind": kind, "address": address, "model": model, "delay": delay}


def _open(window_cls, lab_key: str, args, settings: QSettings, cli_address: str | None,
          show: bool = True) -> dict:
    """Открыть прибор (через координатор) и окно; по APPLY в окне - пересоздать. Возвращает {proxy, win}
    (обновляется; ``proxy`` - прибор координатора)."""
    conn = connection_for(window_cls, lab_key, args.lab, settings, args.sim, cli_address)
    kind, address, model, delay = conn["kind"], conn["address"], conn["model"], conn["delay"]
    current: dict = {}

    def build(kind: str, address: str, model: str | None, pos=None, visible: bool = True) -> None:
        proxy = COORD.open_instrument(kind, address, model=all_models(settings, kind).get(model) if model else None,
                                      delay=delay, backend=args.backend, latency=args.latency)
        win = window_cls(proxy, settings)
        win.reconnect_requested.connect(reconnect)
        if pos is not None:
            win.move(pos)
        if visible:
            win.show()
        current.update(proxy=proxy, win=win)
        if not args.no_connect:
            proxy.open()

    def reconnect(kind: str, address: str, model: str) -> None:
        old_proxy, old_win = current["proxy"], current["win"]
        pos, visible = old_win.pos(), old_win.isVisible()
        old_win.hide()
        COORD.close_instrument(old_proxy)
        old_win.deleteLater()
        build(kind, address, model or None, pos, visible)

    build(kind, address, model, None, show)
    return current
