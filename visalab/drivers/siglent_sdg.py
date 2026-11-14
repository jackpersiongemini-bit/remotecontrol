"""Драйвер генераторов Siglent SDG (по "SDG Series Programming Guide").

Особенности протокола, учтённые здесь:
* Команды канальные с префиксом ``C1:``/``C2:`` - выбор канала не нужен.
* Один запрос возвращает весь блок параметров с заголовком:
  ``C1:BSWV WVTP,SINE,FRQ,100HZ,PERI,0.01S,AMP,2V,...`` -> группы BSWV/OUTP/MDWV/SWWV/BTWV.
* В ответах числа идут с единицами (``100HZ``, ``2.4e-07S``).
* В MDWV/SWWV/BTWV есть вложенные префиксы (``AM,FRQ,...``, ``CARR,WVTP,...``).
* Очереди ``SYST:ERR?`` в мануале нет: ошибки берём из *ESR? и, где есть, ``CMR?``.
* Модуляция, свип и пакет взаимоисключающие - после включения одного
  перечитываются остальные.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, Kind, Param, PStatus
from ..core.units import parse_bool, parse_number
from ..profiles import FUNCTION_GENERATOR

__all__ = ["SiglentSDG", "SDGModel", "SDG_MODELS", "parse_siglent_response"]


@dataclass(frozen=True)
class SDGModel:
    name: str
    channels: int = 2
    max_freq: float = 120e6      # Гц, синус
    max_amp_hiz: float = 20.0    # Vpp на High-Z
    has_cmr: bool = False        # поддержка CMR? (по таблице мануала)


#: Профили моделей: выбираются явно (run_gui.py --sdg-model). Без выбора - DEFAULT
#: (проверка диапазона мягкая, окончательное слово за прибором: обратное чтение
#: покажет округление).
SDG_MODELS: dict[str, SDGModel] = {
    "SDG1032X": SDGModel("SDG1032X", 2, 30e6),
    "SDG1062X": SDGModel("SDG1062X", 2, 60e6),
    "SDG2042X": SDGModel("SDG2042X", 2, 40e6),
    "SDG2082X": SDGModel("SDG2082X", 2, 80e6),
    "SDG2122X": SDGModel("SDG2122X", 2, 120e6),
    "SDG1025": SDGModel("SDG1025", 2, 25e6, has_cmr=True),
    "SDG1050": SDGModel("SDG1050", 2, 50e6, has_cmr=True),
    "SDG5162": SDGModel("SDG5162", 2, 160e6, has_cmr=True),
    "SDG6022X": SDGModel("SDG6022X", 2, 200e6),
}
DEFAULT_MODEL = SDGModel("SDG (неизвестная модель)", 2, 500e6)

WAVES = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC")
MOD_TYPES = ("AM", "FM", "PM", "FSK", "ASK", "PSK", "DSBAM", "PWM")
_PREFIXES = set(MOD_TYPES) | {"CARR"}

CMR_MESSAGES = {
    1: "Unrecognized command/query header", 2: "Invalid character", 3: "Invalid separator",
    4: "Missing parameter", 5: "Unrecognized keyword", 6: "String error",
    7: "Parameter can't allowed", 8: "Command String Too Long", 9: "Query cannot allowed",
    10: "Missing Query mask", 11: "Invalid parameter", 12: "Parameter syntax error",
    13: "Filename too long", 14: "Directory not exist",
}

_HDR_RE = re.compile(r"^\s*(?:C\d\s*:\s*)?[A-Z_]+\s+", re.I)


def parse_siglent_response(response: str) -> tuple[list[str], dict[str, str], list[str]]:
    """Разбор ответа Siglent.

    Возвращает (ведущие токены без ключа, словарь пар, встреченные префиксы).
    ``C1:MDWV STATE,ON,AM,FRQ,100HZ,CARR,WVTP,SINE`` ->
    ``([], {"STATE": "ON", "AM.FRQ": "100HZ", "CARR.WVTP": "SINE"}, ["AM", "CARR"])``.
    ``C1:OUTP ON,LOAD,HZ`` -> ``(["ON"], {"LOAD": "HZ"}, [])``.
    """
    body = _HDR_RE.sub("", response.strip(), count=1)
    tokens = [t.strip() for t in body.split(",")] if body else []
    lead: list[str] = []
    pairs: dict[str, str] = {}
    prefixes: list[str] = []
    prefix = ""
    i = 0
    # OUTP/SYNC начинаются со значения без ключа (ON/OFF)
    while i < len(tokens) and tokens[i].upper() in ("ON", "OFF"):
        lead.append(tokens[i].upper())
        i += 1
    while i < len(tokens):
        tok = tokens[i].upper()
        if tok in _PREFIXES:
            prefix = tok
            prefixes.append(tok)
            i += 1
            continue
        if i + 1 >= len(tokens):
            lead.append(tok)
            break
        pairs[f"{prefix}.{tok}" if prefix else tok] = tokens[i + 1]
        i += 2
    return lead, pairs, prefixes


class SiglentSDG(VISAInstrument):
    NAME = "Siglent SDG"
    PROFILE = FUNCTION_GENERATOR
    STRIP_RESPONSE_HEADER = True   # "*ESR 1", "C1:SYNC OFF"
    #: SDG по USB может терять команды, идущие подряд без паузы. 50 мс - стартовое
    #: значение, подбирается на приборе (run_gui.py --sdg-delay).
    COMMAND_DELAY_S = 0.05
    ERROR_QUERY = None           # SYST:ERR? в этом мануале нет

    # (ключ-суффикс, подпись, тип, единица, код в BSWV, extra)
    _BSWV = (
        ("wave", "Форма", Kind.ENUM, "", "WVTP", {"choices": WAVES}),
        ("frequency", "Частота", Kind.FLOAT, "Hz", "FRQ", {"minimum": 1e-6}),
        ("period", "Период", Kind.FLOAT, "s", "PERI", {"minimum": 0.0}),
        ("amplitude", "Амплитуда", Kind.FLOAT, "Vpp", "AMP", {"minimum": 0.0}),
        ("offset", "Смещение", Kind.FLOAT, "V", "OFST", {}),
        ("high", "Верхний уровень", Kind.FLOAT, "V", "HLEV", {}),
        ("low", "Нижний уровень", Kind.FLOAT, "V", "LLEV", {}),
        ("phase", "Фаза", Kind.FLOAT, "deg", "PHSE", {"minimum": 0.0, "maximum": 360.0}),
        ("duty", "Скважность", Kind.FLOAT, "%", "DUTY", {"minimum": 0.0, "maximum": 100.0}),
        ("symmetry", "Симметрия", Kind.FLOAT, "%", "SYM", {"minimum": 0.0, "maximum": 100.0}),
        ("width", "Длительность имп.", Kind.FLOAT, "s", "WIDTH", {"minimum": 0.0}),
        ("delay", "Задержка имп.", Kind.FLOAT, "s", "DLY", {}),
    )
    #: после изменения этих полей BSWV выравнивать фазы каналов (EQPHASE), как в PulseGen
    PHASE_FIELDS = ("WVTP", "PERI", "FRQ", "WIDTH", "DUTY", "DLY")
    AUTO_EQPHASE = True
    _SWWV = (
        ("sweep_time", "Время", Kind.FLOAT, "s", "TIME", {"minimum": 0.0}),
        ("sweep_start", "Старт", Kind.FLOAT, "Hz", "START", {"minimum": 0.0}),
        ("sweep_stop", "Стоп", Kind.FLOAT, "Hz", "STOP", {"minimum": 0.0}),
        ("sweep_mode", "Закон", Kind.ENUM, "", "SWMD", {"choices": ("LINE", "LOG")}),
        ("sweep_dir", "Направление", Kind.ENUM, "", "DIR", {"choices": ("UP", "DOWN")}),
        ("sweep_trig", "Триггер", Kind.ENUM, "", "TRSR", {"choices": ("INT", "EXT", "MAN")}),
    )
    _BTWV = (
        ("burst_mode", "Режим", Kind.ENUM, "", "GATE_NCYC", {"choices": ("NCYC", "GATE")}),
        ("burst_period", "Период", Kind.FLOAT, "s", "PRD", {"minimum": 0.0}),
        ("burst_cycles", "Циклов (INF)", Kind.STR, "", "TIME", {}),
        ("burst_trig", "Триггер", Kind.ENUM, "", "TRSR", {"choices": ("INT", "EXT", "MAN")}),
        ("burst_delay", "Задержка", Kind.FLOAT, "s", "DLAY", {"minimum": 0.0}),
    )

    def __init__(self, transport, model: SDGModel | None = None, parent=None,
                 command_delay_s: float | None = None):
        self.model = model or DEFAULT_MODEL
        super().__init__(transport, parent, command_delay_s)

    @property
    def channels(self) -> int:
        return self.model.channels

    # ------------------------------------------------------------ описание
    def build_params(self):
        params: list[Param] = []
        m = self.model
        for ch in range(1, m.channels + 1):
            c = f"C{ch}"          # префикс SCPI
            k = f"ch{ch}"         # префикс ключа профиля
            tab = f"CH{ch}"
            for suffix, label, kind, unit, code, extra in self._BSWV:
                if suffix == "frequency":
                    extra = {**extra, "maximum": m.max_freq, "also_read": (f"{k}.period",)}
                elif suffix == "period":
                    extra = {**extra, "also_read": (f"{k}.frequency", f"{k}.duty")}
                elif suffix == "width":
                    extra = {**extra, "also_read": (f"{k}.duty",)}
                params.append(Param(
                    f"{k}.{suffix}", label, kind, unit, section=f"{tab}/Сигнал",
                    group=f"{c}:BSWV", write=f"{c}:BSWV {code},{{value}}", meta={"code": code}, **extra))
            outp_keys = (f"{k}.amplitude", f"{k}.high", f"{k}.low", f"{k}.offset")
            params += [
                Param(f"{k}.output", "Выход", Kind.BOOL, section=f"{tab}/Выход", group=f"{c}:OUTP",
                      write=f"{c}:OUTP {{value}}", bool_tokens=("ON", "OFF")),
                Param(f"{k}.load", "Нагрузка", Kind.ENUM, section=f"{tab}/Выход", group=f"{c}:OUTP",
                      write=f"{c}:OUTP LOAD,{{value}}", choices=("50", "HIZ"), labels=("50 Ом", "High-Z"),
                      enum_map=(("HIZ", "HZ"),), also_read=outp_keys),
                Param(f"{k}.polarity", "Полярность", Kind.ENUM, section=f"{tab}/Выход", group=f"{c}:OUTP",
                      write=f"{c}:OUTP PLRT,{{value}}", choices=("NORM", "INV"), labels=("Норм.", "Инв."),
                      enum_map=(("NORM", "NOR"), ("INV", "INVT"))),
                Param(f"{k}.sync", "Синхровыход", Kind.BOOL, section=f"{tab}/Выход",
                      query=f"{c}:SYNC?", write=f"{c}:SYNC {{value}}", bool_tokens=("ON", "OFF")),
            ]
            # взаимоисключающие режимы: после переключения перечитать остальные
            states = (f"{k}.mod_state", f"{k}.sweep_state", f"{k}.burst_state")
            mod_group = f"{c}:MDWV"
            params += [
                Param(f"{k}.mod_state", "Модуляция", Kind.BOOL, section=f"{tab}/Модуляция", group=mod_group,
                      write=f"{c}:MDWV STATE,{{value}}", bool_tokens=("ON", "OFF"),
                      also_read=tuple(k for k in states if not k.endswith("mod_state"))),
                Param(f"{k}.mod_type", "Тип", Kind.ENUM, section=f"{tab}/Модуляция", group=mod_group,
                      write=f"{c}:MDWV {{value}}", choices=MOD_TYPES),
                Param(f"{k}.mod_source", "Источник", Kind.ENUM, section=f"{tab}/Модуляция", group=mod_group,
                      write=f"{c}:MDWV {{type}},SRC,{{value}}", choices=("INT", "EXT")),
                Param(f"{k}.mod_shape", "Форма мод.", Kind.ENUM, section=f"{tab}/Модуляция", group=mod_group,
                      write=f"{c}:MDWV {{type}},MDSP,{{value}}",
                      choices=("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE", "ARB")),
                Param(f"{k}.mod_freq", "Частота мод.", Kind.FLOAT, "Hz", section=f"{tab}/Модуляция",
                      group=mod_group, write=f"{c}:MDWV {{type}},FRQ,{{value}}", minimum=0.0),
                Param(f"{k}.mod_depth", "Глубина AM", Kind.FLOAT, "%", section=f"{tab}/Модуляция",
                      group=mod_group, write=f"{c}:MDWV AM,DEPTH,{{value}}", minimum=0.0, maximum=120.0),
                Param(f"{k}.mod_dev", "Девиация FM/PM", Kind.FLOAT, "", section=f"{tab}/Модуляция",
                      group=mod_group, write=f"{c}:MDWV {{type}},DEVI,{{value}}", minimum=0.0),
                Param(f"{k}.sweep_state", "Свип", Kind.BOOL, section=f"{tab}/Свип", group=f"{c}:SWWV",
                      write=f"{c}:SWWV STATE,{{value}}", bool_tokens=("ON", "OFF"),
                      also_read=tuple(k for k in states if not k.endswith("sweep_state"))),
            ]
            for suffix, label, kind, unit, code, extra in self._SWWV:
                params.append(Param(f"{k}.{suffix}", label, kind, unit, section=f"{tab}/Свип",
                                    group=f"{c}:SWWV", write=f"{c}:SWWV {code},{{value}}",
                                    meta={"code": code}, **extra))
            params.append(Param(f"{k}.burst_state", "Пакет", Kind.BOOL, section=f"{tab}/Пакет",
                                group=f"{c}:BTWV", write=f"{c}:BTWV STATE,{{value}}", bool_tokens=("ON", "OFF"),
                                also_read=tuple(k for k in states if not k.endswith("burst_state"))))
            for suffix, label, kind, unit, code, extra in self._BTWV:
                params.append(Param(f"{k}.{suffix}", label, kind, unit, section=f"{tab}/Пакет",
                                    group=f"{c}:BTWV", write=f"{c}:BTWV {code},{{value}}",
                                    meta={"code": code}, **extra))
        return params

    def build_actions(self):
        acts = [Action("reset", "Сброс *RST", confirm="Сбросить генератор к заводским настройкам?"),
                Action("all_off", "Выключить все выходы")]
        for ch in range(1, self.model.channels + 1):
            acts.append(Action(f"trigger_{ch}", f"Ручной триггер CH{ch}"))
        if self.model.channels >= 2:
            acts.append(Action("x.copy_1_to_2", "Копировать CH1 → CH2"))   # расширение Siglent
            acts.append(Action("align_phase", "Выровнять фазы (EQPHASE)"))
        return acts

    # --------------------------------------------------------------- хуки
    def write_param(self, spec: Param, value: Any) -> None:
        if "{type}" in spec.write:
            ch = spec.key.split(".")[0]
            mod_type = self._values.get(f"{ch}.mod_type") or "AM"
            template = spec.write
            if mod_type in ("FSK", "ASK", "PSK"):   # у манипуляций частота называется KFRQ
                template = template.replace(",FRQ,", ",KFRQ,")
            cmd = template.format(type=mod_type, value=spec.to_scpi(value))
        else:
            cmd = spec.write.format(value=spec.to_scpi(value))
        self.write(cmd)
        if (self.AUTO_EQPHASE and self.model.channels >= 2 and spec.group and spec.group.endswith(":BSWV")
                and spec.meta.get("code") in self.PHASE_FIELDS):
            self.write("EQPHASE")

    def read_param_raw(self, spec: Param) -> Any:
        # SYNC: "C1:SYNC ON" (на части моделей "C1:SYNC ON,TYPE,CH1")
        lead, _, _ = parse_siglent_response(self.query(spec.query))
        if not lead:
            raise ValueError("пустой ответ")
        return spec.from_scpi(lead[0])

    def read_group(self, group: str) -> dict[str, Any]:
        ch, cmd = group.split(":")           # "C1", "BSWV"
        ch = f"ch{ch[1:]}"                   # -> префикс ключа профиля
        lead, pairs, prefixes = parse_siglent_response(self.query(f"{group}?"))
        out: dict[str, Any] = {}

        def put(key: str, raw: str | None) -> None:
            if raw is None:
                return
            spec = self._params.get(key)
            if spec is None:
                return
            try:
                out[key] = spec.from_scpi(raw)
            except ValueError:
                self.param_status.emit(key, PStatus.ERROR, f"ответ не распознан: {raw!r}")

        if cmd == "BSWV":
            for suffix, *_rest in self._BSWV:
                put(f"{ch}.{suffix}", pairs.get(_rest[3]))
        elif cmd == "OUTP":
            if lead:
                out[f"{ch}.output"] = lead[0] == "ON"
            put(f"{ch}.load", pairs.get("LOAD"))
            put(f"{ch}.polarity", pairs.get("PLRT"))
        elif cmd == "MDWV":
            state = pairs.get("STATE", "OFF")
            out[f"{ch}.mod_state"] = parse_bool(state)
            mod_type = next((p for p in prefixes if p in MOD_TYPES), None)
            if mod_type:
                out[f"{ch}.mod_type"] = mod_type
                put(f"{ch}.mod_source", pairs.get(f"{mod_type}.SRC"))
                put(f"{ch}.mod_shape", (pairs.get(f"{mod_type}.MDSP") or "").replace(" ", "") or None)
                put(f"{ch}.mod_freq", pairs.get(f"{mod_type}.FRQ") or pairs.get(f"{mod_type}.KFRQ"))
                put(f"{ch}.mod_depth", pairs.get("AM.DEPTH"))
                put(f"{ch}.mod_dev", pairs.get(f"{mod_type}.DEVI"))
            elif f"{ch}.mod_type" in self._values:
                out[f"{ch}.mod_type"] = self._values[f"{ch}.mod_type"]  # тип известен с прошлого раза
        elif cmd in ("SWWV", "BTWV"):
            prefix = "sweep" if cmd == "SWWV" else "burst"
            out[f"{ch}.{prefix}_state"] = parse_bool(pairs.get("STATE", "OFF"))
            table = self._SWWV if cmd == "SWWV" else self._BTWV
            for suffix, *_rest in table:
                put(f"{ch}.{suffix}", pairs.get(_rest[3]))
        else:
            raise NotImplementedError(group)
        return out

    def read_error_queue(self) -> list[tuple[int, str]]:
        if not self.model.has_cmr:
            return []      # базовый класс расшифрует биты *ESR?
        code = int(parse_number(self.query("CMR?").split()[-1]))   # "CMR 0"
        return [(code, CMR_MESSAGES.get(code, f"CMR {code}"))] if code else []

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "all_off":
            for ch in range(1, self.model.channels + 1):
                self.write(f"C{ch}:OUTP OFF")
            self._read_keys([f"ch{ch}.output" for ch in range(1, self.model.channels + 1)])
            return None
        if name.startswith("trigger_"):
            ch = int(name.rsplit("_", 1)[1])
            self._read_keys([f"ch{ch}.burst_state", f"ch{ch}.sweep_state"])
            if self._values.get(f"ch{ch}.burst_state"):
                self.write(f"C{ch}:BTWV MTRIG")
            elif self._values.get(f"ch{ch}.sweep_state"):
                self.write(f"C{ch}:SWWV MTRIG")
            else:
                raise ValueError(f"CH{ch}: ни свип, ни пакет не включены")
            return None
        if name == "align_phase":
            self.write("EQPHASE")
            return None
        if name == "x.copy_1_to_2":
            self.write("PACP C2,C1")
            self._sync_all()
            return None
        return super().run_custom_action(name, args)

