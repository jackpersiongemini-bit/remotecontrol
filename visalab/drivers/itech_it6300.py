"""Драйвер трёхканальных источников ITECH IT6300 (IT6322/6332/6333 A/B/C).

По "IT6300 Programming Guide V1.1":
* Канальные команды (VOLT, CURR, CHAN:OUTP, VOLT:PROT...) действуют на ТЕКУЩИЙ
  канал, который выбирается ``INST:NSEL n``. Канал выбирается перед каждой
  канальной операцией (не кэшируется: его могли переключить с панели).
* Измерения всех каналов одной командой: ``MEAS:VOLT:ALL?``, ``MEAS:CURR:ALL?``.
* Режим CV/CC и срабатывание OVP: ``STAT:QUES:INST:ISUMn:COND?`` (бит0 CV,
  бит1 CC, бит9 OV) - без переключения канала.
* Очередь ошибок ``SYST:ERR?`` -> ``<код>,"<текст>"``.
* ``SYST:REM`` при подключении (нужно для RS232/VCP, безвредно для USBTMC),
  ``SYST:LOC`` при отключении.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .base import VISAInstrument
from ..core.params import Action, Kind, Param
from ..core.units import parse_number
from ..profiles import POWER_SUPPLY

__all__ = ["ItechIT6300", "IT6300Model", "IT6300_MODELS"]


@dataclass(frozen=True)
class IT6300Model:
    name: str
    #: (Umax, Imax) по каналам CH1..CH3
    ratings: tuple[tuple[float, float], ...]


#: Номиналы по каналам. ВНИМАНИЕ: проверить по паспорту своего прибора.
IT6300_MODELS: dict[str, IT6300Model] = {
    "IT6322": IT6300Model("IT6322", ((30.0, 3.0), (30.0, 3.0), (5.0, 3.0))),
    "IT6332": IT6300Model("IT6332", ((30.0, 6.0), (30.0, 6.0), (5.0, 3.0))),
    "IT6333": IT6300Model("IT6333", ((60.0, 3.0), (60.0, 3.0), (5.0, 3.0))),
}
DEFAULT_MODEL = IT6300_MODELS["IT6322"]

COND_CV, COND_CC, COND_OV = 0x001, 0x002, 0x200
COMBINE = ("NONE", "SER", "PAR", "TRAC")


class ItechIT6300(VISAInstrument):
    NAME = "ITECH IT6300"
    PROFILE = POWER_SUPPLY
    POLL_INTERVAL_MS = 500

    def __init__(self, transport, model: IT6300Model | None = None, parent=None,
                 command_delay_s: float | None = None):
        self.model = model or DEFAULT_MODEL
        super().__init__(transport, parent, command_delay_s)

    @property
    def channels(self) -> int:
        return len(self.model.ratings)

    # ------------------------------------------------------------ описание
    def build_params(self):
        params: list[Param] = []
        for n, (umax, imax) in enumerate(self.model.ratings, start=1):
            sec = f"Каналы/CH{n}"
            k = f"ch{n}"
            params += [
                Param(f"{k}.output", "Выход", Kind.BOOL, section=sec, channel=n,
                      query="CHAN:OUTP?", write="CHAN:OUTP {value}", also_read=("output_all",)),
                Param(f"{k}.voltage", "Уставка U", Kind.FLOAT, "V", section=sec, channel=n,
                      query="VOLT?", write="VOLT {value}", minimum=0.0, maximum=umax, tolerance=1e-3),
                Param(f"{k}.current", "Уставка I", Kind.FLOAT, "A", section=sec, channel=n,
                      query="CURR?", write="CURR {value}", minimum=0.0, maximum=imax, tolerance=1e-3),
                Param(f"{k}.meas_v", "Измер. U", Kind.FLOAT, "V", section=sec, group="MEAS", poll=True),
                Param(f"{k}.meas_i", "Измер. I", Kind.FLOAT, "A", section=sec, group="MEAS", poll=True),
                Param(f"{k}.meas_p", "Измер. P", Kind.FLOAT, "W", section=sec, group="MEAS", poll=True),
                Param(f"{k}.mode", "Режим", Kind.ENUM, section=sec, group="COND", poll=True,
                      choices=("CV", "CC", "OFF")),
                Param(f"{k}.ovp_tripped", "OVP сработала", Kind.BOOL, section=sec, group="COND", poll=True),
                Param(f"{k}.ovp_state", "OVP вкл.", Kind.BOOL, section=f"Защита/CH{n}", channel=n,
                      query="VOLT:PROT:STAT?", write="VOLT:PROT:STAT {value}"),
                Param(f"{k}.ovp_level", "Порог OVP", Kind.FLOAT, "V", section=f"Защита/CH{n}", channel=n,
                      query="VOLT:PROT?", write="VOLT:PROT {value}", minimum=0.0, maximum=umax * 1.1,
                      tolerance=1e-3),
                Param(f"{k}.vlimit", "Предел U", Kind.FLOAT, "V", section=f"Защита/CH{n}", channel=n,
                      query="VOLT:LIMIT?", write="VOLT:LIMIT {value}", minimum=0.0, maximum=umax,
                      tolerance=1e-3, also_read=(f"{k}.voltage",)),
            ]
        outs = tuple(f"ch{n}.output" for n in range(1, len(self.model.ratings) + 1))
        params += [
            Param("output_all", "Все выходы", Kind.BOOL, section="Общие/Выход",
                  query="OUTP:STAT:ALL?", write="OUTP {value}", also_read=outs),
            Param("combine", "CH1+CH2", Kind.ENUM, section="Общие/Выход", choices=COMBINE,
                  labels=("Раздельно", "Последоват.", "Параллельно", "Слежение"),
                  write="<custom>", query="<custom>",
                  also_read=("ch1.voltage", "ch2.voltage", "ch1.current", "ch2.current")),
        ]
        return params

    def build_actions(self):
        return [
            Action("all_off", "Выключить все выходы"),
            Action("clear_ovp", "Сбросить OVP"),
            Action("reset", "Сброс *RST", confirm="Сброс выключит выходы и вернёт уставки по умолчанию. Продолжить?"),
            Action("x.beep", "Звуковой сигнал"),   # расширение ITECH
        ]

    # --------------------------------------------------------------- хуки
    def on_connected(self) -> None:
        self.write("SYST:REM")

    def on_disconnecting(self) -> None:
        self.write("SYST:LOC")

    def select_channel(self, channel: int) -> None:
        self.write(f"INST:NSEL {channel}")

    def write_param(self, spec: Param, value: Any) -> None:
        if spec.key == "combine":
            # Режимы нельзя переключать напрямую: сначала снять текущий.
            for cmd in ("OUTP:SER 0", "OUTP:PAR 0", "OUTP:TRAC 0"):
                self.write(cmd)
            if value != "NONE":
                self.write(f"OUTP:{value} 1")
            return
        super().write_param(spec, value)

    def read_param_raw(self, spec: Param) -> Any:
        if spec.key == "combine":
            for mode in ("SER", "PAR", "TRAC"):
                if self.query_int(f"OUTP:{mode}?"):
                    return mode
            return "NONE"
        return super().read_param_raw(spec)

    def read_group(self, group: str) -> dict[str, Any]:
        n_ch = len(self.model.ratings)
        out: dict[str, Any] = {}
        if group == "MEAS":
            volts = _floats(self.query("MEAS:VOLT:ALL?"))
            amps = _floats(self.query("MEAS:CURR:ALL?"))
            for n in range(1, n_ch + 1):
                if n <= len(volts) and n <= len(amps):
                    v, i = volts[n - 1], amps[n - 1]
                    out.update({f"ch{n}.meas_v": v, f"ch{n}.meas_i": i, f"ch{n}.meas_p": v * i})
        elif group == "COND":
            for n in range(1, n_ch + 1):
                cond = self.query_int(f"STAT:QUES:INST:ISUM{n}:COND?")
                out[f"ch{n}.mode"] = "CC" if cond & COND_CC else "CV" if cond & COND_CV else "OFF"
                out[f"ch{n}.ovp_tripped"] = bool(cond & COND_OV)
        else:
            raise NotImplementedError(group)
        return out

    def run_custom_action(self, name: str, args: tuple) -> Any:
        if name == "all_off":
            self.write("OUTP 0")
            self._read_keys(["output_all", *(f"ch{n}.output" for n in range(1, len(self.model.ratings) + 1))])
            return None
        if name == "clear_ovp":
            channels = args or range(1, len(self.model.ratings) + 1)
            for n in channels:
                self.select_channel(int(n))
                self.write("VOLT:PROT:CLE")
            self._read_keys([f"ch{n}.ovp_tripped" for n in channels])
            return None
        if name == "x.beep":
            self.write("SYST:BEEP")
            return None
        return super().run_custom_action(name, args)


def _floats(text: str) -> list[float]:
    return [parse_number(t) for t in re.split(r"[,\s]+", text.strip()) if t]
