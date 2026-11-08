"""Окно генератора импульсов (Siglent SDG) - по PulseGen.py Алексея.

Заголовок: ON (связь), Sync, Init (*RST + восстановление сохранённого), адрес, индикаторы записи.
Ниже - пресеты PERI/WID (общие для обоих каналов, «+» берёт текущие значения CH1, «−» удаляет
выбранный) и таблица параметров по каналам. Каждая запись проверяется драйвером чтением обратно,
EQPHASE после изменений, влияющих на фазу, драйвер отправляет сам.

Окно знает только InstrumentProxy и ключи профиля function_generator.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from ..core.params import ConnectionState
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from .params import PBGroup, PLabel, PLineEdit, PNLabel, PSButton, Panel, PanelLine, PanelTable, WriteTracker
from .theme import OFF, ON, UI_COLUMN_WIDTH_PX, WINDOW_BAR_HEIGHT
from .window import (AddressLabel, CustomExpandButton, CustomInitButton, CustomOnButton, CustomSyncButton,
                     ErrorIndicator, StatusIndicator, Window, short_address)

__all__ = ["PulseGenWindow", "PULSE_PRESETS", "ROWS"]

PULSE_PRESETS = ((400e-3, 30e-6),)

#: параметры канала: (подпись, ключ без "chN.", фабрика виджета, сохранять в ini).
#: OUT показывается наверху (всегда), остальное - в сворачиваемой таблице.
ROWS = (
    ("OUT", "output", lambda p, k: PSButton(p, k, {f"{k[:3].upper()} OFF": False, f"{k[:3].upper()} ON": True}),
     True),
    ("TYPE", "wave", lambda p, k: PSButton(p, k, {"PULSE": "PULSE"}), True),
    ("DUTY", "duty", lambda p, k: PNLabel(p, k, "%"), False),
    ("PERI", "period", lambda p, k: PLineEdit(p, k, "s"), True),
    ("WID", "width", lambda p, k: PLineEdit(p, k, "s"), True),
    ("HLVL", "high", lambda p, k: PLineEdit(p, k, "V"), True),
    ("LLVL", "low", lambda p, k: PLineEdit(p, k, "V"), True),
    ("DLY", "delay", lambda p, k: PLineEdit(p, k, "s"), True),
    ("LOAD", "load", lambda p, k: PSButton(p, k, {"50 Ω": "50", "HiZ": "HIZ"}), True),
)


def _fmt(value: float) -> str:
    return si_format(value, "s", 4)


class PresetGroup(PBGroup):
    """Пары (period, width) в QSettings (JSON). Чип в две строки: период / ширина."""

    def __init__(self, settings: QSettings, key: str, defaults=PULSE_PRESETS, columns: int = 3, parent=None):
        super().__init__(columns=columns, parent=parent)
        self.settings, self.key = settings, key
        self.data: list[tuple[float, float]] = []
        for preset in self._load(defaults):
            self._append(preset)

    def _load(self, defaults):
        raw = self.settings.value(self.key)
        if raw is None:
            return [tuple(map(float, p)) for p in defaults]
        try:
            return [(float(a), float(b)) for a, b in json.loads(str(raw))]
        except (TypeError, ValueError):
            return [tuple(map(float, p)) for p in defaults]

    def save(self) -> None:
        self.settings.setValue(self.key, json.dumps(self.data, separators=(",", ":")))

    def _append(self, preset):
        b = self.add_button(f"{_fmt(preset[0])}\n{_fmt(preset[1])}")
        b.setToolTip(f"PERI {_fmt(preset[0])}\nWID {_fmt(preset[1])}")
        self.data.append(preset)

    def find(self, period: float, width: float) -> int:
        for i, (p, w) in enumerate(self.data):
            if math.isclose(p, period, rel_tol=1e-9) and math.isclose(w, width, rel_tol=1e-9):
                return i
        return -1

    def add(self, period: float, width: float) -> bool:
        i = self.find(period, width)
        if i < 0:
            self._append((float(period), float(width)))
            i = len(self.data) - 1
            self.save()
        self.set_checked(i)
        return True

    def remove_selected(self) -> bool:
        i = self.checked_index()
        if i < 0:
            return False
        self.remove_button(i)
        del self.data[i]
        self.save()
        return True


class PulseGenWindow(Window):
    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Pulse generator", parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        self.setWindowTitle(title)
        self._prefix = re.sub(r"\W+", "_", proxy.address).strip("_").upper() or "SDG"
        self._resetting = False
        nch = min(2, proxy.channels)
        self.channels: list[dict[str, Any]] = []

        # ---------------------------------------------------------- заголовок
        bar = self.titleBar
        self.on_button = CustomOnButton()
        self.on_button.setToolTip("Connect / disconnect")
        self.on_button.set_status(OFF)
        self.on_button.clicked.connect(self._toggle_connection)
        self.sync_button = CustomSyncButton()
        self.sync_button.setToolTip("Read all parameters")
        self.sync_button.clicked.connect(self._sync)
        self.init_button = CustomInitButton()
        self.init_button.setToolTip("Reset (*RST) and restore saved values")
        self.init_button.clicked.connect(self._init)
        self.address = AddressLabel(short_address(proxy.address))
        self.address.setToolTip(proxy.address)
        self.status_indicator = StatusIndicator()
        self.error_indicator = ErrorIndicator()
        for i, w in enumerate((self.on_button, self.sync_button, self.init_button, self.address,
                               self.status_indicator, self.error_indicator)):
            bar.insert_widget(w, i)
        bar.title_label.hide()                    # заголовок - в панели задач; в окне место занимает адрес
        self.expand_button = CustomExpandButton()
        self.expand_button.setToolTip("Show / hide all parameters")
        self.expand_button.clicked.connect(lambda: self.set_expanded(not self.expand_button.expanded))
        bar.insert_control_widget(self.expand_button)

        # ---------------------------------------------------------- виджеты каналов
        for n in range(1, nch + 1):
            self.channels.append({name: make(proxy, f"ch{n}.{name}") for _t, name, make, _s in ROWS
                                  if proxy.supports(f"ch{n}.{name}")})

        # ---------------------------------------------------------- верх: выходы и пресеты
        outputs = [ch["output"] for ch in self.channels if "output" in ch]
        self.presets = PresetGroup(settings, f"{self._prefix}/PULSE_PRESETS")
        self.presets.selected.connect(self._apply_preset)
        self.add_button = self._tool("+", "Add preset from CH1 PERI / WID", self._add_preset)
        self.remove_button = self._tool("−", "Remove selected preset", self.presets.remove_selected)
        tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        tl.addWidget(self.add_button)
        tl.addWidget(self.remove_button)
        tl.addStretch()
        top = PanelTable(PanelLine("OUT", *outputs), PanelLine("PRST", self.presets, tools))
        self.preset_panel = Panel(top)
        for w in outputs:
            w.setMinimumWidth(UI_COLUMN_WIDTH_PX)
        top.lines[1].line_layout.setAlignment(top.lines[1]._name, Qt.AlignmentFlag.AlignTop)

        # ---------------------------------------------------------- таблица (сворачивается)
        heads = []
        for n in range(1, nch + 1):
            h = PLabel(text=f"CH{n}")
            h.setObjectName("heading")
            heads.append(h)
        lines = [PanelLine("", *heads)]
        for title_, name, _m, _s in ROWS:
            if name != "output" and all(name in ch for ch in self.channels):
                lines.append(PanelLine(title_, *(ch[name] for ch in self.channels)))
        self.parameter_panel = Panel(PanelTable(*lines), column_width=UI_COLUMN_WIDTH_PX)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.preset_panel)
        lay.addWidget(self.parameter_panel)
        lay.addStretch()
        self._layout = lay

        # ---------------------------------------------------------- состояние
        keys = [w.key for ch in self.channels for w in ch.values()]
        names = {k: k.upper().replace(".", " ") for k in keys}
        self.tracker = WriteTracker(proxy, keys, names, self)
        self.tracker.changed.connect(self.status_indicator.set_items)
        self.tracker.changed.connect(self.error_indicator.set_items)
        proxy.state_changed.connect(self._on_state)
        proxy.sync_done.connect(self._on_sync_done)
        proxy.action_done.connect(self._on_action_done)
        proxy.param_updated.connect(self._on_param)
        proxy.comm_error.connect(self._on_comm_error)
        self._on_state(proxy.state)

        self.address.setMinimumWidth(self.address.sizeHint().width())
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), lay.sizeHint().width()))
        self.recall_size(QSize(self.minimumWidth(), 0))
        self.set_expanded(settings.value(f"{self._prefix}/EXPANDED", False, type=bool))

    # ------------------------------------------------------------ вид
    def set_expanded(self, on: bool) -> None:
        """Полный вид - с таблицей всех параметров; обычный - только выходы и пресеты."""
        self.expand_button.set_expanded(on)
        self.parameter_panel.setVisible(on)
        self.settings.setValue(f"{self._prefix}/EXPANDED", bool(on))
        self._layout.activate()
        self.setFixedHeight(self._layout.sizeHint().height())

    @staticmethod
    def _tool(text, tip, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------ сохранение
    def _settings_key(self, key: str) -> str:
        return f"{self._prefix}/{key.upper()}"

    def _saved_widgets(self):
        saved = {name for _t, name, _m, s in ROWS if s}
        return [w for ch in self.channels for name, w in ch.items() if name in saved]

    def _on_param(self, key: str, value: Any) -> None:
        if self._resetting:              # после *RST прибор отдаёт заводские значения - не сохранять
            return
        if value is not None and any(w.key == key for w in self._saved_widgets()):
            self.settings.setValue(self._settings_key(key), value)

    def restore(self) -> None:
        """Отправить сохранённые значения (только отличающиеся от текущих)."""
        widgets = self._saved_widgets()
        # форма (от неё зависят PERI/WID/DLY) -> нагрузка (пересчитывает уровни, поэтому до HLVL/LLVL)
        # -> остальное -> выходы последними
        rank = {"wave": 0, "load": 1, "output": 3}
        for w in sorted(widgets, key=lambda w: rank.get(w.key.split(".", 1)[1], 2)):
            raw = self.settings.value(self._settings_key(w.key))
            if raw is None:
                continue
            try:
                value = w.spec.validate(str(raw).replace(",", ".") if isinstance(raw, str) else raw)
            except (TypeError, ValueError):
                continue
            w.set_value(value)

    # ------------------------------------------------------------ кнопки
    def _toggle_connection(self) -> None:
        if self.proxy.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            self.proxy.close()
        else:
            self.proxy.open()

    def _sync(self) -> None:
        if self.proxy.is_connected:
            self.sync_button.set_status("process")
            self.proxy.sync()

    def _init(self) -> None:
        if self.proxy.is_connected:
            self.init_button.set_status("process")
            self._resetting = True
            self.proxy.action("reset")

    def _add_preset(self) -> None:
        ch1 = self.channels[0]
        period, width = ch1["period"].value, ch1["width"].value
        if period is not None and width is not None:
            self.presets.add(period, width)

    def _apply_preset(self, index: int) -> None:
        period, width = self.presets.data[index]
        for ch in self.channels:
            cur = ch["period"].value
            pairs = [(ch["period"], period), (ch["width"], width)]
            if cur is not None and period < cur:
                pairs.reverse()              # уменьшаем: сначала WID, чтобы WID < PERI
            for w, v in pairs:
                w.set_value(v)

    # ------------------------------------------------------------ события
    def _on_state(self, state: ConnectionState) -> None:
        status = {ConnectionState.CONNECTED: ON, ConnectionState.CONNECTING: "process",
                  ConnectionState.ERROR: "error"}.get(state, OFF)
        self.on_button.set_status(status)
        on = state is ConnectionState.CONNECTED
        self.sync_button.setEnabled(on)
        self.init_button.setEnabled(on)
        if not on:
            self._resetting = False
            self.sync_button.set_status("ok")
            self.init_button.set_status("ok")

    def _on_comm_error(self, _msg: str) -> None:
        self._resetting = False
        for b in (self.sync_button, self.init_button):
            if b.status() == "process":
                b.set_status("ok")

    def _on_sync_done(self) -> None:
        self.sync_button.set_status("ok")

    def _on_action_done(self, name: str, _result) -> None:
        if name == "reset":
            self.init_button.set_status("ok")
            self._resetting = False
            self.restore()

    def closeEvent(self, event):  # noqa: N802 - Qt
        self.presets.save()
        self.settings.sync()
        super().closeEvent(event)

