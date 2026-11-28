"""Окно векторного анализатора цепей (2 и 4 порта: Keysight PNA / PNA-L, профиль ``network_analyzer``).

Прибор отдаёт только сырые комплексные ``SDATA`` нужных S-параметров (``S11..S44``) — дисплей
анализатора не читается и не настраивается. Пользовательские трассы (канал, S-параметр, формат
``MLOG / PHASE / UPHASE / GDELAY / SWR / LIN_MAG / REAL / IMAG``, электрическая задержка ``EDEL``
и сглаживание ``SMO``) настраиваются и считаются локально на ПК.

Избранное:
* ``POW`` — мощность источника (``PowerSpin``), ``RF ON/OFF`` и ``PRST``;
* ``CW``  — режим генератора (``CW``, выходной порт ``P1..P4``, частота CW).

Закреплённая шапка настроек (без сворачивания, всегда видна сверху настроек):
* ``CH``  — каналы прибора (чипы ``C1``, ``C2``, ..., ``+`` создать канал, ``−`` удалить);
* ``TRC`` — пользовательские трассы (чипы с форматом и ``канал·S-параметр``, индикатор снизу —
  трасса включена, ``+`` новая трасса, ``−`` удалить выбранную).

Разделы настроек:
* ``TRAC`` — настройки выбранной пользовательской трассы (``CH``, ``READ``, ``SPAR``, ``FMT``,
  ``EDEL``, ``SMO``, ``ALL / NONE``);
* ``CH``   — настройки выбранного канала (частоты, точки, полоса ПЧ, время, тип развёртки,
  развёртка мощности ``PSTR / PSTP``, усреднение);
* ``PWR``  — мощность канала, ``RF``, связь портов ``COUP``, индивидуальная мощность ``P1..P4``,
  пресеты мощности и калибровка источника (``SourceCal``);
* ``TRIG`` — запуск развёрток (источник, ``TRIG``, область, режим канала, фронт/тип, задержка,
  ``CONT / SINGLE``, ``SWEEP``);
* ``CAL``  — cal set'ы канала, ``STIM``, ``CORR``, ``INT`` и мастер калибровки ``CALIBRATE`` (1..4 порта);
* ``SAVE`` — состояния ``.csa`` на диске прибора + экспорт на ПК (``DATA SAVE`` CSV и ``SNP SAVE`` Touchstone).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, QSize, Qt, QTimer
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..coord import Instrument
from ..core.export import write_touchstone, write_traces_csv
from ..core.params import Kind
from ..core.trace import Trace
from ..core.units import si_format, si_parse
from ..core.vna_traces import VNA_FORMAT_LABELS, VNA_FORMATS, VNATraceSpec, format_sparam_trace
from . import instrument_window as _iw
from .instrument_window import (PRESET_GAP, HScrollArea, InstrumentWindow, PowerSpin, PresetGroup,
                                parse_power_list)
from .params import IndicatorButton, PBGroup, PLineEdit, PSButton, PSpinBox, PSpinEdit, Panel, PanelLine, PanelTable
from .power_meter import CONFIRM_MS, ActionButton
from .source_cal import SourceCal
from .spectrum import StatePresets
from .theme import UI_COLUMN_WIDTH_PX, WINDOW_BAR_HEIGHT
from .widgets import CommitLineEdit
from .window import Window

__all__ = [
    "VNAWindow", "PowerSpin", "CWButton", "ChannelBar", "OwnTraceEditor", "MenuButton",
    "CalSets", "CalWizard", "CalWindow", "parse_power_list", "saved_connection",
]

CONNECTION_GROUP = "CONNECTION/VNA"
ONOFF = [("OFF", False), ("ON", True)]
AUTO = [("MAN", False), ("AUTO", True)]
TRACE_CHIP_HEIGHT = 44            # чипы своих трасс в избранном: две строки (формат / канал·S-параметр)
CHAN_CHIP_HEIGHT = 28             # чипы каналов в избранном
CAL_WINDOW_WIDTH = 440            # окно калибровки: полные названия наборов и шагов
MAX_PORTS = 4

#: раздел CH (настройки выбранного канала): строки из групп ``((подпись, ключ, второй ключ или None), ...)``
CHANNEL_ROWS = (
    (("STAR", "freq.start", None), ("STOP", "freq.stop", None)),
    (("CENT", "freq.center", None), ("SPAN", "freq.span", None)),
    (("PTS", "sweep.points", None), ("IFBW", "bw.if", None)),
    (("TIME", "sweep.time", "sweep.time_auto"), ("TYPE", "sweep.type", None)),
    (("PSTR", "power.start", None), ("PSTP", "power.stop", None)),
    (("AVG", "avg.state", "avg.count"),),
)
#: раздел TRIG: строки из групп ``((подпись, ключ, кнопки или None, подсказка), ...)``
TRIG_ROWS = (
    (("SRC", "trig.source", [("INT", "IMM"), ("EXT", "EXT"), ("MAN", "MAN")],
      "Trigger source: internal, external input (rear BNC), manual (TRIG button)"),
     ("SCOP", "trig.scope", [("ALL", "ALL"), ("CH", "CURRENT")],
      "One trigger sweeps all channels in turn, or only the current one")),
    (("MODE", "trig.mode", [("CHAN", "CHANNEL"), ("SWP", "SWEEP"), ("PNT", "POINT"), ("TRAC", "TRACE")],
      "What one trigger does on the active channel: the whole channel, one sweep, one point, one trace"),
     ("SLOP", "trig.slope", [("POS", "POS"), ("NEG", "NEG")], "External trigger edge / level polarity"),
     ("TYPE", "trig.type", [("EDGE", "EDGE"), ("LEVEL", "LEVEL")], "External trigger by edge or by level")),
    (("DLY", "trig.delay", None, "Delay after the external trigger, s"),),
)
NOT_SAVED = ("freq.center", "freq.span", "cal.state")
RESTORE_RANK = {"freq.start": 0, "freq.stop": 0, "sweep.type": 1, "freq.cw": 1, "rf.output": 5, "trig.source": 6}


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _auto(proxy: Instrument, key: str):
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, AUTO if key.endswith("_auto") else ONOFF)
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    if spec.kind in (Kind.FLOAT, Kind.INT):
        return PSpinEdit(proxy, key)
    return PLineEdit(proxy, key)


def _sparams_for_ports(ports: int) -> tuple[str, ...]:
    n = max(1, min(MAX_PORTS, int(ports or 2)))
    return tuple(f"S{r}{c}" for r in range(1, n + 1) for c in range(1, n + 1))


class CWButton(IndicatorButton):
    """CW — режим генератора (``gen.state``): активный канал в CW, остальные стоят; повторный клик
    возвращает настройки канала, как были до включения."""

    def __init__(self, proxy: Instrument, parent=None):
        super().__init__("CW", parent)
        self.proxy = proxy
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Generator mode: the active channel goes to CW, other channels stop.\n"
                        "Click again to restore everything as it was")
        self._on: bool | None = None
        self.clicked.connect(self.toggle)
        proxy.subscribe("gen.state", self._on_state)

    def indicated(self) -> bool:
        return bool(self._on)

    def _on_state(self, value) -> None:
        self._on = value
        self.update()

    def toggle(self) -> None:
        if not self.proxy.is_connected or self._on is None:
            return
        self.proxy.set("gen.state", not self._on)


class MenuButton(IndicatorButton):
    """Выбор из списка без привязки к ключу: клик — меню; ``on_pick(значение)`` — только выбор пользователя."""

    def __init__(self, choices, on_pick, tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tip)
        self.choices, self.on_pick, self.value = tuple(choices), on_pick, None
        self.clicked.connect(self._menu)

    def show_value(self, value) -> None:
        self.value = value
        self.setText(next((n for n, d in self.choices if d == value), "" if value is None else str(value)))

    def _menu(self) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        for name, data in self.choices:
            if name is None:
                menu.addSeparator()
                continue
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(data == self.value)
            act.triggered.connect(lambda _=False, d=data: self.on_pick(d))
        menu.setMinimumWidth(self.width())
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))


# ============================================================ управление каналами
class _ChannelChip(IndicatorButton):
    def __init__(self, ch: int):
        super().__init__(f"C{ch}")
        self.channel = ch
        self.setObjectName("chip")
        self.setProperty("tight", True)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(CHAN_CHIP_HEIGHT)
        self.setToolTip(f"Switch active VNA channel to CH{ch} (stimulus, power, trigger mode, calibration)")

    def sizeHint(self):  # noqa: N802 - Qt
        return QSize(super().sizeHint().width(), CHAN_CHIP_HEIGHT)

    def minimumSizeHint(self):  # noqa: N802 - Qt
        return self.sizeHint()


class _ChannelChips(PBGroup):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.channels: list[int] = []

    def add_button(self, name: str) -> QPushButton:
        raise NotImplementedError

    def set_channels(self, channels: list[int]) -> None:
        self.channels = list(channels)
        while self._buttons:
            self.remove_button(0)
        for ch in self.channels:
            b = _ChannelChip(ch)
            self._group.addButton(b, len(self._buttons))
            self._buttons.append(b)
        self._relayout()


class ChannelBar(QWidget):
    """Чипы каналов прибора (``C1``, ``C2``, ...) + кнопки ``+`` (создать) / ``−`` (удалить).

    Выбранный чип задаёт ``stim.channel`` (настройки раздела ``CH``, ``PWR``, ``TRIG.MODE``, ``CAL``).
    """

    def __init__(self, proxy: Instrument, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.chips = _ChannelChips()
        self.chips.selected.connect(self._clicked)
        self.scroll = HScrollArea(self.chips, CHAN_CHIP_HEIGHT)
        self.delete_button: QPushButton | None = None
        self.add_button: QPushButton | None = None
        self._armed = QTimer(self)
        self._armed.setSingleShot(True)
        self._armed.timeout.connect(lambda: self.delete_button and self.delete_button.setText("−"))
        proxy.subscribe("channels.list", lambda _v: self._refresh())
        proxy.subscribe("stim.channel", lambda _v: self._refresh())
        proxy.subscribe("gen.state", lambda _v: self._refresh())
        proxy.state_changed.connect(lambda _s: self._refresh())
        proxy.action_done.connect(self._on_action)
        self._refresh()

    def _on_action(self, name: str, result) -> None:
        if sip.isdeleted(self):
            return
        if name in ("chan_list", "chan_create", "chan_delete", "state_recall"):
            self._refresh()

    def channels(self) -> list[int]:
        return self.proxy.channels_list() if hasattr(self.proxy, "channels_list") else [1]

    def current_channel(self) -> int:
        v = self.proxy.value("stim.channel")
        return int(v) if isinstance(v, int) and v >= 1 else (self.channels()[0] if self.channels() else 1)

    def _refresh(self) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.chips):
            return
        chs = self.channels()
        if chs != self.chips.channels:
            self.chips.set_channels(chs)
            self.chips.updateGeometry()
        cur = self.current_channel()
        on = self.proxy.is_connected
        gen = self.proxy.value("gen.state") is True
        self.chips.set_checked(chs.index(cur) if cur in chs else -1)
        for b in self.chips.buttons():
            b.setEnabled(on and (not gen or b.channel == cur))
            b.update()
        if cur in chs:
            self.scroll.ensureWidgetVisible(self.chips.buttons()[chs.index(cur)], 0, 0)
        if self.add_button is not None and not sip.isdeleted(self.add_button):
            self.add_button.setEnabled(on and not gen)
        if self.delete_button is not None and not sip.isdeleted(self.delete_button):
            self.delete_button.setEnabled(on and not gen and len(chs) > 1)

    def _clicked(self, index: int) -> None:
        if 0 <= index < len(self.chips.channels) and self.proxy.is_connected:
            ch = self.chips.channels[index]
            if ch != self.proxy.value("stim.channel"):
                self.proxy.set("stim.channel", ch)

    def create_channel(self) -> None:
        if self.proxy.is_connected and "chan_create" in self.proxy.actions:
            self.proxy.action("chan_create")

    def delete_channel(self) -> bool:
        chs = self.channels()
        if not self.proxy.is_connected or len(chs) <= 1 or "chan_delete" not in self.proxy.actions:
            return False
        b = self.delete_button
        if b is not None and not self._armed.isActive():
            b.setText("?")
            self._armed.start(CONFIRM_MS)
            return False
        self._armed.stop()
        if b is not None:
            b.setText("−")
        self.proxy.action("chan_delete", self.current_channel())
        return True


# ============================================================ пользовательские трассы на ПК
class _OwnTraceChip(IndicatorButton):
    """Чип пользовательской трассы: выбор — инверсия, полоска снизу — трасса включена (READ)."""

    def __init__(self, owner: "OwnTraceEditor", trace_id: str):
        super().__init__()
        self.owner, self.trace_id = owner, trace_id
        self.setObjectName("chip")
        self.setProperty("tight", True)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(TRACE_CHIP_HEIGHT)

    def sizeHint(self):  # noqa: N802 - Qt
        return QSize(super().sizeHint().width(), TRACE_CHIP_HEIGHT)

    def minimumSizeHint(self):  # noqa: N802 - Qt
        return self.sizeHint()

    def indicated(self) -> bool:
        spec = self.owner.spec_by_id(self.trace_id)
        return not self.isChecked() and spec is not None and spec.enabled


class _OwnTraceChips(PBGroup):
    def __init__(self, owner: "OwnTraceEditor", parent=None):
        self.owner = owner
        super().__init__(parent=parent)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

    def add_button(self, name: str) -> QPushButton:
        raise NotImplementedError

    def set_ids(self, ids: list[str]) -> None:
        while self._buttons:
            self.remove_button(0)
        for tid in ids:
            b = _OwnTraceChip(self.owner, tid)
            self._group.addButton(b, len(self._buttons))
            self._buttons.append(b)
        self._relayout()


class _OwnReadButton(IndicatorButton):
    def __init__(self, owner: "OwnTraceEditor"):
        super().__init__("READ")
        self.owner = owner
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Enable reading and plotting this trace")

    def indicated(self) -> bool:
        spec = self.owner.current_spec()
        return spec is not None and spec.enabled


class _OwnSmoothButton(IndicatorButton):
    def __init__(self, owner: "OwnTraceEditor"):
        super().__init__("OFF")
        self.owner = owner
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("PC-side trace smoothing on / off (computed locally from raw SDATA)")

    def indicated(self) -> bool:
        spec = self.owner.current_spec()
        return spec is not None and spec.smooth


class OwnTraceEditor(QWidget):
    """Управление собственными (ПК) трассами ВАЦ.

    Каждая трасса задаёт ``(channel, sparam, fmt, enabled, edel_s, smooth, smooth_aper)``.
    При любом изменении:
    * список сохраняется в ``QSettings``;
    * ``proxy.set_vna_trace_specs(traces)`` мгновенно пересчитывает и перерисовывает графики;
    * ``proxy.set_channel_sparams(...)`` синхронизирует набор уникальных ``(channel, sparam)`` на приборе.
    """

    def __init__(self, proxy: Instrument, settings: QSettings, prefix: str, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.settings = settings
        self.settings_key = f"{prefix}/OWN_TRACES"
        self.traces: list[VNATraceSpec] = self._load()
        self.current_id: str | None = self.traces[0].id if self.traces else None
        self._next_num = self._compute_next_num()
        self._pending_new_ch_trace: str | None = None

        self.chips = _OwnTraceChips(self)
        self.chips.selected.connect(self._chip_clicked)
        self.scroll = HScrollArea(self.chips, TRACE_CHIP_HEIGHT)

        self.channel = MenuButton([], self._channel_picked, "VNA channel for this trace (pick NEW to create a channel)")
        self.read = _OwnReadButton(self)
        self.read.clicked.connect(self.toggle_read)
        self.sparam = MenuButton([], self._sparam_picked, "S-parameter measured on the selected channel")
        self.format = MenuButton(VNA_FORMAT_LABELS, self._format_picked,
                                 "Display format computed locally on PC from complex SDATA")

        self.edel_edit = CommitLineEdit()
        self.edel_edit.setToolTip("PC-side electrical delay in seconds (e.g. 1.5ns, 250ps, 0): removes cable phase slope")
        self.edel_edit.committed.connect(self._edel_committed)

        self.smooth_btn = _OwnSmoothButton(self)
        self.smooth_btn.clicked.connect(self._toggle_smooth)

        self.aper_edit = CommitLineEdit()
        self.aper_edit.setToolTip("PC-side smoothing aperture, % of trace points (0.5..25 %)")
        self.aper_edit.committed.connect(self._aper_committed)

        self.all_btn = QPushButton("ALL")
        self.all_btn.setObjectName("choice")
        self.all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_btn.setToolTip("Enable all own traces")
        self.all_btn.clicked.connect(self.enable_all)

        self.none_btn = QPushButton("NONE")
        self.none_btn.setObjectName("choice")
        self.none_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.none_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.none_btn.setToolTip("Disable all own traces")
        self.none_btn.clicked.connect(self.disable_all)

        self.editors = (self.channel, self.read, self.sparam, self.format,
                        self.edel_edit, self.smooth_btn, self.aper_edit)

        proxy.subscribe("ports", lambda _v: self._on_hw_changed())
        proxy.subscribe("channels.list", lambda _v: self._on_hw_changed())
        proxy.subscribe("stim.channel", lambda _v: self._show_current())
        proxy.subscribe("gen.state", lambda _v: self._show_current())
        proxy.sync_done.connect(self._on_sync_done)
        proxy.action_done.connect(self._on_action)

        self._rebuild_chips()
        self._publish_specs(sync_hw=False)

    # -------------------------------------------------------- хранение
    def _load(self) -> list[VNATraceSpec]:
        raw = str(self.settings.value(self.settings_key, "", type=str) or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    out = [VNATraceSpec.from_dict(item, f"t{i}") for i, item in enumerate(data, 1)
                           if isinstance(item, dict)]
                    if out:
                        return out
            except (TypeError, ValueError):
                pass
        return [VNATraceSpec("t1", channel=1, sparam="S11", fmt="MLOG", enabled=True)]

    def _save(self) -> None:
        self.settings.setValue(self.settings_key, json.dumps([t.to_dict() for t in self.traces]))

    def _compute_next_num(self) -> int:
        nums = [int(m.group(1)) for t in self.traces for m in [re.match(r"^t(\d+)$", t.id)] if m]
        return max(nums, default=0) + 1

    # -------------------------------------------------------- выбор / правка
    def spec_by_id(self, trace_id: str | None) -> VNATraceSpec | None:
        if trace_id is None:
            return None
        return next((t for t in self.traces if t.id == trace_id), None)

    def current_spec(self) -> VNATraceSpec | None:
        return self.spec_by_id(self.current_id)

    def _replace_current(self, **changes) -> None:
        if self.current_id is None:
            return
        changed = False
        for i, t in enumerate(self.traces):
            if t.id == self.current_id:
                new_t = replace(t, **changes)
                if new_t != t:
                    self.traces[i] = new_t
                    changed = True
                break
        if changed:
            hw_changed = any(k in changes for k in ("channel", "sparam", "enabled"))
            self._save()
            self._show_current()
            self._publish_specs(sync_hw=hw_changed)

    def _ports(self) -> int:
        return max(1, min(MAX_PORTS, int(getattr(self.proxy, "ports", 2) or 2)))

    def _channels(self) -> list[int]:
        chs = self.proxy.channels_list() if hasattr(self.proxy, "channels_list") else [1]
        for t in self.traces:
            if t.channel not in chs:
                chs = sorted(set(chs) | {t.channel})
        return chs or [1]

    def _sync_hardware(self) -> None:
        if not self.proxy.is_connected or not self.proxy.supports("s.all_active"):
            return
        ports = self._ports()
        valid = set(_sparams_for_ports(ports))
        pairs = [(t.channel, t.sparam) for t in self.traces if t.enabled and t.sparam in valid]
        self.proxy.set_channel_sparams(pairs)

    def _publish_specs(self, *, sync_hw: bool = True) -> None:
        if hasattr(self.proxy, "set_vna_trace_specs"):
            self.proxy.set_vna_trace_specs(self.traces)
        if sync_hw:
            self._sync_hardware()

    def _on_sync_done(self) -> None:
        if sip.isdeleted(self):
            return
        self._on_hw_changed()
        self._sync_hardware()

    def _on_hw_changed(self) -> None:
        if sip.isdeleted(self):
            return
        # если удалили канал на приборе — подкорректируем меню; трассы на других каналах остаются видимы
        self._show_current()

    def _on_action(self, name: str, result) -> None:
        if sip.isdeleted(self):
            return
        if name == "chan_create" and self._pending_new_ch_trace is not None:
            tid = self._pending_new_ch_trace
            self._pending_new_ch_trace = None
            new_ch = self.proxy.value("stim.channel")
            if isinstance(new_ch, int):
                for i, t in enumerate(self.traces):
                    if t.id == tid:
                        self.traces[i] = replace(t, channel=new_ch)
                        break
                self._save()
                self._show_current()
                self._publish_specs(sync_hw=True)
        elif name == "chan_delete" and isinstance(result, (list, tuple)) and result:
            valid_chs = {int(c) for c in result}
            fallback = min(valid_chs)
            changed = False
            for i, t in enumerate(self.traces):
                if t.channel not in valid_chs:
                    self.traces[i] = replace(t, channel=fallback)
                    changed = True
            if changed:
                self._save()
                self._show_current()
                self._publish_specs(sync_hw=True)

    # -------------------------------------------------------- действия пользователя
    def create(self) -> None:
        """``+`` : добавить новую пользовательскую трассу на текущем канале."""
        cur_ch = self.proxy.value("stim.channel")
        ch = int(cur_ch) if isinstance(cur_ch, int) and cur_ch >= 1 else (
            self.current_spec().channel if self.current_spec() else 1
        )
        ports = self._ports()
        used = {t.sparam for t in self.traces if t.channel == ch}
        preferred = ("S21", "S11", "S41", "S12", "S22", "S31", "S42", "S32", *_sparams_for_ports(ports))
        valid = set(_sparams_for_ports(ports))
        sp = next((s for s in preferred if s in valid and s not in used), "S21" if "S21" in valid else "S11")
        tid = f"t{self._next_num}"
        self._next_num += 1
        self.traces.append(VNATraceSpec(tid, channel=ch, sparam=sp, fmt="MLOG", enabled=True))
        self.current_id = tid
        self._save()
        self._rebuild_chips()
        self._publish_specs(sync_hw=True)

    def delete(self) -> bool:
        """``−`` : удалить выбранную пользовательскую трассу."""
        if self.current_id is None:
            return False
        idx = next((i for i, t in enumerate(self.traces) if t.id == self.current_id), -1)
        if idx < 0:
            return False
        self.traces.pop(idx)
        if self.traces:
            self.current_id = self.traces[min(idx, len(self.traces) - 1)].id
        else:
            self.current_id = None
        self._save()
        self._rebuild_chips()
        self._publish_specs(sync_hw=True)
        return True

    def toggle_read(self) -> None:
        spec = self.current_spec()
        if spec is not None:
            self._replace_current(enabled=not spec.enabled)

    def enable_all(self) -> None:
        if not self.traces:
            return
        self.traces = [replace(t, enabled=True) for t in self.traces]
        self._save()
        self._show_current()
        self._publish_specs(sync_hw=True)

    def disable_all(self) -> None:
        if not self.traces:
            return
        self.traces = [replace(t, enabled=False) for t in self.traces]
        self._save()
        self._show_current()
        self._publish_specs(sync_hw=True)

    def _chip_clicked(self, index: int) -> None:
        if 0 <= index < len(self.traces):
            self.current_id = self.traces[index].id
            self._show_current()
            spec = self.current_spec()
            if (spec is not None and self.proxy.is_connected
                    and self.proxy.value("gen.state") is not True
                    and spec.channel in self.proxy.channels_list()
                    and self.proxy.value("stim.channel") != spec.channel):
                self.proxy.set("stim.channel", spec.channel)

    def _channel_picked(self, value) -> None:
        spec = self.current_spec()
        if spec is None:
            return
        ch = int(value)
        existing = self.proxy.channels_list() if hasattr(self.proxy, "channels_list") else [1]
        if ch not in existing and self.proxy.is_connected and "chan_create" in self.proxy.actions:
            self._pending_new_ch_trace = spec.id
            self.proxy.action("chan_create", ch)
            return
        self._replace_current(channel=ch)
        if self.proxy.is_connected and self.proxy.value("gen.state") is not True:
            self.proxy.set("stim.channel", ch)

    def _sparam_picked(self, value) -> None:
        self._replace_current(sparam=str(value).upper())

    def _format_picked(self, value) -> None:
        self._replace_current(fmt=str(value).upper())

    def _edel_committed(self, text: str | None = None) -> None:
        spec = self.current_spec()
        if spec is None:
            return
        raw = (self.edel_edit.text() if text is None else text).strip()
        if not raw:
            val = 0.0
        else:
            try:
                val = float(si_parse(raw, "s"))
            except ValueError:
                self._show_current()
                return
        val = max(-10.0, min(10.0, val))
        self._replace_current(edel_s=val)

    def _toggle_smooth(self) -> None:
        spec = self.current_spec()
        if spec is not None:
            self._replace_current(smooth=not spec.smooth)

    def _aper_committed(self, text: str | None = None) -> None:
        spec = self.current_spec()
        if spec is None:
            return
        raw = (self.aper_edit.text() if text is None else text).strip().rstrip("%").replace(",", ".").strip()
        try:
            val = float(raw)
        except ValueError:
            self._show_current()
            return
        val = max(0.5, min(25.0, val))
        self._replace_current(smooth_aper=val)

    # -------------------------------------------------------- отрисовка
    def _rebuild_chips(self) -> None:
        self.chips.set_ids([t.id for t in self.traces])
        self.chips.updateGeometry()
        self._show_current()

    def _show_current(self) -> None:
        if sip.isdeleted(self):
            return
        gen = self.proxy.value("gen.state") is True
        cur_ch = self.proxy.value("stim.channel")
        ports = self._ports()
        valid_sparams = _sparams_for_ports(ports)
        for b, t in zip(self.chips.buttons(), self.traces):
            b.setText(f"{t.short_format()}\n{t.channel}·{t.sparam}")
            extras = []
            if t.edel_s:
                extras.append(f"EDEL {si_format(t.edel_s, 's', 4)}")
            if t.smooth:
                extras.append(f"SMO {t.smooth_aper:g}%")
            extra_txt = (" · " + ", ".join(extras)) if extras else ""
            b.setToolTip(f"Own trace: CH{t.channel} {t.sparam} {t.fmt}{extra_txt}\n"
                         f"Click to edit · bottom bar means READ is ON")
            b.setEnabled((not gen or t.channel == cur_ch) and t.sparam in valid_sparams)
            b.update()
        ids = [t.id for t in self.traces]
        idx = ids.index(self.current_id) if self.current_id in ids else -1
        self.chips.set_checked(idx)
        if idx >= 0:
            self.scroll.ensureWidgetVisible(self.chips.buttons()[idx], 0, 0)

        spec = self.current_spec()
        for w in self.editors:
            w.setEnabled(spec is not None)
        if spec is None:
            for w in (self.channel, self.sparam, self.format):
                w.show_value(None)
            self.edel_edit.setText("")
            self.smooth_btn.setText("OFF")
            self.aper_edit.setText("")
            self.read.update()
            self.smooth_btn.update()
            return

        chs = self._channels()
        new_ch = max(chs) + 1
        self.channel.choices = tuple((f"C{c}", c) for c in chs) + ((f"C{new_ch} NEW", new_ch),)
        self.channel.show_value(spec.channel)
        self.sparam.choices = tuple((s, s) for s in valid_sparams)
        self.sparam.show_value(spec.sparam)
        self.format.show_value(spec.fmt)
        if not self.edel_edit.hasFocus():
            self.edel_edit.setText("0 s" if spec.edel_s == 0.0 else si_format(spec.edel_s, "s", 4))
        self.smooth_btn.setText("ON" if spec.smooth else "OFF")
        if not self.aper_edit.hasFocus():
            self.aper_edit.setText(f"{spec.smooth_aper:g} %")
        self.read.update()
        self.smooth_btn.update()


# ============================================================ калибровка (1..4 порта)
class CalSets(QWidget):
    """Cal set'ы в памяти анализатора — чипы (NONE + имена, по два в ряд)."""

    NONE = "NONE"

    def __init__(self, proxy: Instrument, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.names: list[str] = []
        self.chips = PBGroup(columns=2)
        self.chips.selected.connect(self._clicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chips)
        self._armed = QTimer(self)
        self._armed.setSingleShot(True)
        self._armed.timeout.connect(lambda: self.delete_button and self.delete_button.setText("−"))
        self.delete_button: QPushButton | None = None
        self.relayout = None
        proxy.action_done.connect(self._on_action)
        proxy.subscribe("cal.set", lambda _v: self._show())
        proxy.subscribe("gen.state", lambda _v: self._show())
        self.set_names([])

    def refresh(self) -> None:
        if self.proxy.is_connected and "cal_list" in self.proxy.actions:
            self.proxy.action("cal_list")

    def set_names(self, names: list[str]) -> None:
        self.names = list(names)
        while self.chips.buttons():
            self.chips.remove_button(0)
        for text in (self.NONE, *self.names):
            b = self.chips.add_button(text)
            b.setToolTip("Remove the cal set from the channel (correction off)" if text == self.NONE
                         else f"Apply cal set {text!r} to the active channel")
        self._show()
        if self.relayout is not None:
            self.relayout()

    def _show(self) -> None:
        cur = self.proxy.value("cal.set")
        keys = ["", *self.names]
        self.chips.set_checked(keys.index(cur) if cur in keys else -1)
        gen = self.proxy.value("gen.state") is True
        for b in self.chips.buttons():
            b.setEnabled(not gen)

    def _clicked(self, index: int) -> None:
        name = "" if index == 0 else self.names[index - 1]
        if self.proxy.value("cal.set") != name:
            self.proxy.set("cal.set", name)
        self._show()

    def delete(self) -> bool:
        name = self.proxy.value("cal.set")
        b = self.delete_button
        if not name or name not in self.names:
            return False
        if b is not None and not self._armed.isActive():
            b.setText("?")
            self._armed.start(CONFIRM_MS)
            return False
        self._armed.stop()
        if b is not None:
            b.setText("−")
        self.proxy.action("cal_delete", name)
        return True

    def _on_action(self, name: str, result) -> None:
        if name in ("cal_list", "cal_delete", "cal_save") and isinstance(result, (list, tuple)):
            self.set_names([str(n) for n in result])


def short_conn(name: str) -> str:
    """Короткая подпись разъёма: «Type N (50) female» -> «N F», «APC 3.5 male» -> «3.5 M»."""
    words = [w for w in re.sub(r"\(\d+\)", "", name).split() if w.lower() not in ("type", "apc")]
    return " ".join({"female": "F", "male": "M"}.get(w.lower(), w) for w in words) or name


def short_step(text: str, conns=()) -> str:
    """Короткий шаг: «Connect APC 3.5 female OPEN to port 1» -> «3.5 F OPEN to P1»."""
    for c in sorted(conns, key=len, reverse=True):
        text = text.replace(c, short_conn(c))
    text = re.sub(r"^\s*connect\s+", "", text, flags=re.I)
    return re.sub(r"\bport\s*(\d+)", r"P\1", text, flags=re.I)


class _StepButton(IndicatorButton):
    def __init__(self, text: str, step: int):
        super().__init__(text)
        self.step, self.done = step, False
        self.setObjectName("choice")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def indicated(self) -> bool:
        return self.done


class CalWizard(QWidget):
    """Калибровка активного канала (guided 1..4 порта или THRU для любого Sij передачи)."""

    TYPES = ("FULL", "THRU")
    OFF = "OFF"

    def __init__(self, proxy: Instrument, settings: QSettings, prefix: str, parent=None):
        super().__init__(parent)
        self.proxy, self.settings, self.prefix = proxy, settings, prefix
        self.conns: list[str] = []
        self.kits: dict[str, list[str]] = {}
        self._kits_pending: list[str] = []
        self.port_conn = {p: str(settings.value(f"{prefix}/CAL_P{p}_CONN", self.OFF if p > 2 else "") or "")
                          for p in range(1, MAX_PORTS + 1)}
        self.port_kit = {p: str(settings.value(f"{prefix}/CAL_P{p}_KIT", "") or "")
                         for p in range(1, MAX_PORTS + 1)}
        self.steps: list[str] = []
        self.done: set[int] = set()
        kind = str(settings.value(f"{prefix}/CAL_TYPE", "FULL") or "FULL")
        self.kind = kind if kind in self.TYPES else "FULL"
        self.type_button = self._button(self.kind, "Calibration type: FULL - guided with the cal kits (P1..P4),\n"
                                                   "THRU - transmission response of the selected Sij trace (i != j)",
                                        self.next_type)
        self.trace_label = QLabel("")
        self.trace_label.setObjectName("lineName")
        self.trace_spec = None                             # окно: () -> VNATraceSpec | None
        self.port_lines: dict[int, QWidget] = {}
        self.conn_buttons = {p: MenuButton([], lambda v, p=p: self.set_conn(p, v), f"Port {p} connector")
                             for p in range(1, MAX_PORTS + 1)}
        self.kit_buttons = {p: MenuButton([], lambda v, p=p: self.set_kit(p, v), f"Port {p} cal kit")
                            for p in range(1, MAX_PORTS + 1)}
        self.start_button = self._button("START", "Start the calibration of the active channel", self.start)
        self.abort_button = self._button("ABORT", "Stop the calibration, sweeps go on", self.abort)
        self.steps_box = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_box)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(4)
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Name of the new cal set (letters, digits, _ and -)")
        self.save_button = self._button("SAVE", "Save the calibration to the cal set NAME and apply it", self.save)
        proxy.action_done.connect(self._on_action)
        proxy.comm_error.connect(self._on_comm_error)
        proxy.refused.connect(self._on_refused)
        proxy.subscribe("cal.busy", self._on_busy)
        proxy.subscribe("gen.state", lambda _v: self._show())
        proxy.subscribe("ports", lambda _v: self._show())
        self.run_lines: list[QWidget] = []
        self.relayout = None
        proxy.state_changed.connect(self._on_state)
        self._show()

    @staticmethod
    def _button(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("choice")
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _ports(self) -> int:
        return max(1, min(MAX_PORTS, int(getattr(self.proxy, "ports", 2) or 2)))

    def refresh(self) -> None:
        if self.proxy.is_connected and "cal_conns" in self.proxy.actions:
            self.proxy.action("cal_conns")

    def _ask_kits(self, conn: str) -> None:
        if conn and conn != self.OFF and conn not in self.kits and conn not in self._kits_pending:
            self._kits_pending.append(conn)
            self.proxy.action("cal_kits", conn)

    def set_conn(self, port: int, conn: str) -> None:
        self.port_conn[port] = conn
        self.settings.setValue(f"{self.prefix}/CAL_P{port}_CONN", conn)
        self._ask_kits(conn)
        self._show()

    def set_kit(self, port: int, kit: str) -> None:
        self.port_kit[port] = kit
        self.settings.setValue(f"{self.prefix}/CAL_P{port}_KIT", kit)
        self._show()

    def conn(self, port: int) -> str:
        c = self.port_conn[port]
        if c == self.OFF or c in self.conns:
            return c
        if port > 2:
            return self.OFF
        return self.conn(1) if port == 2 and self.conns else (self.conns[0] if self.conns else "")

    def kit(self, port: int) -> str:
        kits = self.kits.get(self.conn(port), [])
        return self.port_kit[port] if self.port_kit[port] in kits else (kits[0] if kits else "")

    def next_type(self) -> None:
        self.kind = self.TYPES[(self.TYPES.index(self.kind) + 1) % len(self.TYPES)]
        self.settings.setValue(f"{self.prefix}/CAL_TYPE", self.kind)
        self._show()
        if self.relayout is not None:
            self.relayout()

    def thru_sparam(self) -> str | None:
        spec = self.trace_spec() if self.trace_spec is not None else None
        if spec is not None and len(spec.sparam) == 3 and spec.sparam[1] != spec.sparam[2]:
            return spec.sparam
        return None

    def start(self) -> None:
        if self.kind == "THRU":
            sp = self.thru_sparam()
            if sp:
                self.proxy.action("cal_start_thru", sp)
            else:
                self.proxy.action("cal_start_thru")
            return
        args = []
        for p in range(1, self._ports() + 1):
            c = self.conn(p)
            args += ["", ""] if c in ("", self.OFF) else [c, self.kit(p)]
        self.proxy.action("cal_start", *args)

    def measure(self, step: int) -> None:
        self.proxy.action("cal_measure", step)

    def abort(self) -> None:
        self.proxy.action("cal_abort")

    def save(self) -> None:
        name = self.name_edit.text().strip() or self.default_name()
        self.name_edit.setText(name)
        self.proxy.action("cal_save", name)

    def default_name(self) -> str:
        return f"CAL_CH{self.proxy.value('stim.channel') or 1}_{time.strftime('%Y%m%d_%H%M')}"

    def _on_state(self, _state) -> None:
        if sip.isdeleted(self):
            return
        if not self.proxy.is_connected:
            self._kits_pending.clear()
        self._show()

    def _on_comm_error(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        self._kits_pending.clear()

    def _on_refused(self, op: str, _reason: str) -> None:
        if sip.isdeleted(self):
            return
        if op == "cal_kits" and self._kits_pending:
            self._kits_pending.pop(0)

    def _on_busy(self, busy) -> None:
        if sip.isdeleted(self):
            return
        if busy is False and self.steps:
            self.proxy.action("cal_status")
        self._show()

    def _on_action(self, name: str, result) -> None:
        if sip.isdeleted(self):
            return
        if name == "cal_conns" and isinstance(result, (list, tuple)):
            self.conns = [str(c) for c in result]
            for p in range(1, self._ports() + 1):
                self._ask_kits(self.conn(p))
        elif name == "cal_kits" and isinstance(result, (list, tuple)) and self._kits_pending:
            self.kits[self._kits_pending.pop(0)] = [str(k) for k in result]
        elif name in ("cal_start", "cal_start_thru", "cal_status") and isinstance(result, (list, tuple)) \
                and len(result) == 2:
            steps, done = result
            if name != "cal_status" or list(steps) != self.steps:
                self.steps = [str(t) for t in steps]
                self._make_steps()
                self.name_edit.setText(self.default_name())
            self.done = {int(k) for k in done}
        elif name in ("cal_save", "cal_abort"):
            self.steps, self.done = [], set()
            self._make_steps()
        else:
            return
        self._show()
        if name != "cal_status" and self.relayout is not None:
            self.relayout()

    def _make_steps(self) -> None:
        while self.steps_layout.count():
            w = self.steps_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for k, text in enumerate(self.steps, 1):
            b = _StepButton(f"{k}. {short_step(text, self.conns)}", k)
            b.setToolTip(f"{text}\nConnect the standard, then click to measure it")
            b.clicked.connect(lambda _=False, k=k: self.measure(k))
            self.steps_layout.addWidget(b)

    def step_buttons(self) -> list[_StepButton]:
        return [self.steps_layout.itemAt(i).widget() for i in range(self.steps_layout.count())]

    def _show(self) -> None:
        if sip.isdeleted(self):
            return
        on = self.proxy.is_connected
        running = bool(self.steps)
        busy = self.proxy.value("cal.busy") is True
        gen = self.proxy.value("gen.state") is True
        n_ports = self._ports()
        thru = self.kind == "THRU"
        any_port_ready = False
        for p in range(1, MAX_PORTS + 1):
            choices = [(short_conn(c), c) for c in self.conns] + ([(self.OFF, self.OFF)] if p > 1 else [])
            self.conn_buttons[p].choices = tuple(choices)
            c = self.conn(p)
            self.conn_buttons[p].show_value(c)
            self.conn_buttons[p].setToolTip(f"Port {p} connector: {c or '?'}")
            kits = self.kits.get(c, [])
            self.kit_buttons[p].choices = tuple((k, k) for k in kits)
            self.kit_buttons[p].show_value(self.kit(p) or None)
            self.kit_buttons[p].setVisible(c != self.OFF)
            for b in (self.conn_buttons[p], self.kit_buttons[p]):
                b.setEnabled(on and not running)
            line = self.port_lines.get(p)
            if line is not None:
                line.setVisible(not thru and p <= n_ports)
            if p <= n_ports and c not in ("", self.OFF) and bool(self.kit(p)):
                any_port_ready = True
        self.type_button.setText(self.kind)
        self.type_button.setEnabled(on and not running)
        spec = self.trace_spec() if self.trace_spec is not None else None
        sp = self.thru_sparam()
        self.trace_label.setVisible(thru)
        self.trace_label.setText("" if spec is None else f"{spec.channel}·{spec.sparam}" + ("" if sp else " ✕"))
        self.trace_label.setToolTip("Selected trace; THRU needs a transmission parameter (e.g. S21, S41)")
        ready = bool(sp) if thru else any_port_ready
        self.start_button.setEnabled(on and not running and not gen and ready)
        self.abort_button.setEnabled(on and running and not busy)
        for b in self.step_buttons():
            b.done = b.step in self.done
            b.setEnabled(on and not busy)
            b.update()
        complete = running and len(self.done) == len(self.steps)
        self.save_button.setEnabled(on and complete and not busy)
        for line in self.run_lines:
            line.setVisible(running)


class _CalOpenButton(IndicatorButton):
    def __init__(self, wizard: CalWizard):
        super().__init__("CALIBRATE")
        self.wizard = wizard
        self.setObjectName("choice")
        self.setToolTip("Open the calibration window for the active channel")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        wizard.proxy.action_done.connect(lambda *_a: self.update())

    def indicated(self) -> bool:
        return bool(self.wizard.steps)


class CalWindow(Window):
    """Отдельное окно калибровки (guided 1..4 порта или THRU) активного канала."""

    def __init__(self, proxy: Instrument, settings: QSettings, prefix: str, parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        wz = self.wizard = CalWizard(proxy, settings, prefix)
        wz.run_lines = [PanelLine("STEP", wz.steps_box), PanelLine("NAME", wz.name_edit, wz.save_button)]
        port_lines = [PanelLine(f"P{p}", wz.conn_buttons[p], wz.kit_buttons[p]) for p in range(1, MAX_PORTS + 1)]
        wz.port_lines = {p: ln for p, ln in enumerate(port_lines, 1)}
        self.table = PanelTable(PanelLine("TYPE", wz.type_button, wz.trace_label), *port_lines,
                                PanelLine("RUN", wz.start_button, wz.abort_button), *wz.run_lines)
        panel = Panel(self.table)
        self.table.build(UI_COLUMN_WIDTH_PX)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        self._outer.addWidget(panel)
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), self._outer.minimumSize().width()))
        self.recall_size(QSize(max(self.minimumWidth(), CAL_WINDOW_WIDTH), 0))
        wz.relayout = self.fit_height
        proxy.subscribe("stim.channel", lambda _v: self._title())
        self._title()
        wz._show()
        self.fit_height()

    def _title(self) -> None:
        self.setWindowTitle(f"CAL CH{self.proxy.value('stim.channel') or 1}")

    def fit_height(self, again: bool = True) -> None:
        with self.fitting():
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest.value)
            for w in self.findChildren(QWidget):
                if w.layout() is not None:
                    w.layout().invalidate()
            self._outer.invalidate()
            self._outer.activate()
            self.fit_to(self._outer)
        if again:
            QTimer.singleShot(0, self._fit_again)

    def _fit_again(self) -> None:
        if not sip.isdeleted(self):
            self.fit_height(False)

    def open(self) -> None:
        self.wizard.refresh()
        self.wizard._show()
        self._title()
        self.show()
        self.raise_()
        self.activateWindow()
        self.fit_height()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if self.wizard.steps and self.proxy.is_connected:
            self.proxy.action("cal_abort")
        super().closeEvent(event)


# ============================================================ главное окно VNA
class VNAWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "pna"
    PRESET_KEY = "VNA_STATES"
    PRESET_DEFAULTS = ()
    PRESET_ADD_TIP = "Save the current state in the analyzer memory"
    PRESET_HELP = ("States saved on the analyzer disk (folder in the VNA line): click a chip - recall it. "
                   "+ saves the current state, − deletes the selected one from the analyzer.")

    def __init__(self, proxy: Instrument, settings: QSettings, title: str = "Network analyzer", parent=None):
        self._select_after_list: str | None = None
        super().__init__(proxy, settings, title, parent)
        if getattr(self, "_ch_tools", None) is not None:
            ch_btns = self._ch_tools.findChildren(QPushButton)
            self._ch_tools.setMinimumWidth(0)
            self._ch_tools.setFixedWidth(
                sum(b.sizeHint().width() for b in ch_btns) + PRESET_GAP * max(0, len(ch_btns) - 1)
            )
        for tools in (getattr(self, "_trc_tools", None),
                      getattr(self, "_cal_tools", None), getattr(self, "_pwr_tools", None),
                      getattr(self, "add_button", None) and self.add_button.parentWidget()):
            if tools is not None:
                tools.setMinimumWidth(0)
                tools.setFixedWidth(max(b.sizeHint().width() for b in tools.findChildren(QPushButton)))
        proxy.action_done.connect(self._on_action)
        proxy.sync_done.connect(self.refresh_lists)
        proxy.subscribe("ports", lambda _v: self._sync_port_widgets())
        if hasattr(self, "source_cal"):
            proxy.sync_done.connect(self.source_cal.apply_correction)
            proxy.action_done.connect(lambda name, _r: name == "reset" and self.source_cal.apply_correction())
        self._sync_port_widgets()
        if proxy.is_connected:
            self.refresh_lists()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, sup, pre = self.proxy, self.proxy.supports, self._prefix
        self.widgets: dict[str, Any] = {}
        self._port_power_lines: dict[int, PanelLine] = {}
        pinned: list[PanelLine] = []
        top: list[PanelLine] = []

        # CH (несворачиваемая шапка настроек): выбор / создание / удаление канала прибора
        if sup("stim.channel"):
            cb = self.channel_bar = ChannelBar(proxy)
            ch_tools = self._ch_tools = QWidget()
            chl = QHBoxLayout(ch_tools)
            chl.setContentsMargins(0, 0, 0, 0)
            chl.setSpacing(PRESET_GAP)
            cb.add_button = self._tool("+", "Create a new VNA channel", cb.create_channel, CHAN_CHIP_HEIGHT)
            cb.delete_button = self._tool("−", "Delete the active VNA channel (click twice)",
                                          cb.delete_channel, CHAN_CHIP_HEIGHT)
            chl.addWidget(cb.add_button)
            chl.addWidget(cb.delete_button)
            pinned.append(PanelLine("CH", cb.scroll, ch_tools).set_help(
                "VNA channels: click a chip to select the active channel (CH, PWR, TRIG mode, CAL). "
                "+ creates a new channel, − deletes the active channel."))

        # TRC (несворачиваемая шапка настроек): пользовательские трассы на ПК (формат, EDEL, сглаживание из сырых SDATA)
        if sup("s.active"):
            te = self.trace_editor = OwnTraceEditor(proxy, self.settings, pre)
            tools = self._trc_tools = QWidget()
            tl = QVBoxLayout(tools)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(PRESET_GAP)
            half = (TRACE_CHIP_HEIGHT - PRESET_GAP) // 2
            for text, tip, slot in (("+", "Add a new PC-side trace on the active channel", te.create),
                                    ("−", "Delete the selected trace", te.delete)):
                tl.addWidget(self._tool(text, tip, slot, half))
            pinned.append(PanelLine("TRC", te.scroll, tools).set_help(
                "Own PC-side traces computed from raw complex SDATA: click a chip to configure it in Traces. "
                "Bottom bar — trace is enabled (READ). + add trace, − delete selected."))

            b = self.data_button = QPushButton("SAVE")
            b.setObjectName("choice")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Save all enabled traces (last sweep) to a CSV file; folder and name - SAVE section")
            b.clicked.connect(self.save_data)

        self.settings_header_lines = pinned

        # POW (избранное)
        spin, prst = self.power_widgets("source.power", "Preset mode: the wheel and arrows step the power "
                                                        "through the PWR list")
        pow_ws: list[QWidget] = []
        if sup("rf.output"):
            rf = PSButton(proxy, "rf.output", [("RF OFF", False), ("RF ON", True)])
            self.widgets["rf.output"] = rf
            pow_ws.append(rf)
        pow_ws.append(prst)
        pow_ws.append(spin)
        top.append(PanelLine("POW", *pow_ws))

        # CW (избранное)
        if sup("gen.state") and sup("freq.cw"):
            self.cw_button = CWButton(proxy)
            self.cw_spin = PSpinBox(proxy, "freq.cw", ("k", "M", "G"))
            self.widgets["freq.cw"] = self.cw_spin
            cw_ws: list[QWidget] = [self.cw_button]
            if sup("gen.port"):
                self.port_button = PSButton(proxy, "gen.port", [(f"P{p}", p) for p in range(1, MAX_PORTS + 1)])
                self.widgets["gen.port"] = self.port_button
                cw_ws.append(self.port_button)
            cw_ws.append(self.cw_spin)
            top.append(PanelLine("CW", *cw_ws))

        self.top_lines = top
        self.saved_widgets += [self.power_spin, *(w for k, w in self.widgets.items() if k not in NOT_SAVED)]

        # 1. TRAC (Traces): настройки выбранной пользовательской трассы на ПК
        if hasattr(self, "trace_editor"):
            te = self.trace_editor
            trac = [
                PanelLine("CH", te.channel, te.read),
                (PanelLine("SPAR", te.sparam), PanelLine("FMT", te.format)),
                (PanelLine("EDEL", te.edel_edit), PanelLine("SMO", te.smooth_btn, te.aper_edit)),
                PanelLine("SET", te.all_btn, te.none_btn),
            ]
            self.page_specs.append(("TRAC", (), PanelTable(*trac)))

        # 2. CH (Channel): все настройки выбранного канала (стимул, развёртка, усреднение)
        ch_lines = []
        for group in CHANNEL_ROWS:
            row = []
            for name, key, extra in group:
                if not sup(key):
                    continue
                w_main = _auto(proxy, key)
                ws = [w_main]
                if extra and sup(extra):
                    w_extra = _auto(proxy, extra)
                    if isinstance(w_extra, PSButton) and not isinstance(w_main, PSButton):
                        ws.insert(0, w_extra)
                    else:
                        ws.append(w_extra)
                for w in ws:
                    self.widgets[w.key] = w
                    if w.key not in NOT_SAVED:
                        self.saved_widgets.append(w)
                row.append(PanelLine(name, *ws))
            if row:
                ch_lines.append(tuple(row))
        if ch_lines:
            self.page_specs.append(("CH", ("avg.state",), PanelTable(*ch_lines)))

        # 3. PWR (Power): общая и попортовая мощность P1..P4, RF, пресеты, калибровка источника
        prst_line = self.power_levels_line("source.power")
        pwr_top = [PanelLine("POW", _auto(proxy, "source.power"))]
        if sup("rf.output"):
            pwr_top.append(PanelLine("RF", PSButton(proxy, "rf.output", ONOFF)))
        pwr: list = [tuple(pwr_top)]
        if sup("source.power_coupled"):
            coup = PSButton(proxy, "source.power_coupled", ONOFF)
            self.widgets["source.power_coupled"] = coup
            self.saved_widgets.append(coup)
            pwr.append(PanelLine("COUP", coup))
        for pair in ((1, 2), (3, 4)):
            row = []
            for p in pair:
                key = f"source.power_p{p}"
                if sup(key):
                    w = _auto(proxy, key)
                    self.widgets[key] = w
                    ln = PanelLine(f"P{p}", w)
                    self._port_power_lines[p] = ln
                    row.append(ln)
            if row:
                pwr.append(tuple(row))
        pwr.append(prst_line)
        if sup("power.offset"):
            self.source_cal = SourceCal(self, proxy, self.settings, pre, "network_analyzer", self._source_cal_freq)
            if self.source_cal.offset_label is not None:
                self.widgets["power.offset"] = self.source_cal.offset_label
            pwr.extend(self.source_cal.lines())
        pwr_ind = tuple(k for k in ("rf.output", "power.offset_state") if sup(k))
        self.page_specs.append(("PWR", pwr_ind, PanelTable(*pwr)))

        # 4. TRIG (Trigger)
        trig = []
        for group in TRIG_ROWS:
            row = []
            for name, key, choices, tip in group:
                if not sup(key):
                    continue
                w = PSButton(proxy, key, choices) if choices else _auto(proxy, key)
                w.setToolTip(tip)
                self.widgets[key] = w
                self.saved_widgets.append(w)
                ws = [w]
                if key == "trig.source" and "trigger" in proxy.actions:
                    ws.append(ActionButton(proxy, "trigger", "TRIG", "Manual trigger (SRC = MAN)"))
                row.append(PanelLine(name, *ws))
            if row:
                trig.append(tuple(row))
        if sup("meas.continuous"):
            cont = PSButton(proxy, "meas.continuous", [("CONT", True), ("SINGLE", False)])
            self.widgets["meas.continuous"] = cont
            mode = [cont] + ([ActionButton(proxy, "single", "SWEEP", "Single sweep")] if "single" in proxy.actions
                             else [])
            run_line = PanelLine("RUN", *mode)
            if trig and len(trig[-1]) == 1 and trig[-1][0].name == "DLY":
                trig[-1] = (*trig[-1], run_line)
            else:
                trig.append(run_line)
        if trig:
            self.page_specs.append(("TRIG", (), PanelTable(*trig)))

        # 5. CAL (Calibration): cal set'ы канала + мастер калибровки 1..4 порта
        if sup("cal.set") and "cal_list" in proxy.actions:
            cs = self.cal_sets = CalSets(proxy)
            tools = QWidget()
            tl = QVBoxLayout(tools)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(PRESET_GAP)
            half = (_iw.PRESET_HEIGHT - PRESET_GAP) // 2
            tl.addWidget(self._tool("↻", "Re-read the cal sets of the analyzer", cs.refresh, half))
            if "cal_delete" in proxy.actions:
                cs.delete_button = self._tool("−", "Delete the cal set of this channel from the analyzer "
                                                   "(click twice)", cs.delete, half)
                tl.addWidget(cs.delete_button)
            tl.addStretch(1)
            self._cal_tools = tools
            cal = [PanelLine("CSET", cs, tools).set_help(
                "Cal sets on the analyzer: click one - apply it to the channel. ↻ re-reads the list, "
                "− deletes the cal set of this channel.")]
            cal_flags = []
            for name, key, choices, tip in (
                    ("STIM", "cal.stim", [("CAL", True), ("KEEP", False)],
                     "Applying a cal set: CAL - take its stimulus too (start, stop, points, IFBW, power),\n"
                     "KEEP - keep the channel stimulus (interpolated if INT is on)"),
                    ("CORR", "cal.state", [("OFF", False), ("ON", True)], "Error correction on the active channel"),
                    ("INT", "cal.interp", ONOFF, "Interpolate the calibration when the stimulus differs")):
                if sup(key):
                    w = PSButton(proxy, key, choices)
                    w.setToolTip(tip)
                    self.widgets.setdefault(key, w)
                    cal_flags.append(PanelLine(name, w))
            if cal_flags:
                cal.append(tuple(cal_flags))
            if "cal_start" in proxy.actions:
                self.cal_window = CalWindow(proxy, self.settings, pre)
                self.cal_wizard = self.cal_window.wizard
                if hasattr(self, "trace_editor"):
                    self.cal_wizard.trace_spec = self.trace_editor.current_spec
                    self.trace_editor.chips.selected.connect(lambda _i: self.cal_wizard._show())
                b = self.cal_button = _CalOpenButton(self.cal_wizard)
                b.clicked.connect(self.open_cal_window)
                cal.append(PanelLine("NEW", b))
            cs.relayout = self._fit_height
            self.page_specs.append(("CAL", ("cal.state",), PanelTable(*cal)))

        # 6. SAVE (Save / Recall): состояния .csa + CSV и Touchstone .s2p/.s4p
        save = []
        if "state_list" in proxy.actions:
            if sup("state.dir"):
                vdir = PLineEdit(proxy, "state.dir")
                vdir.setToolTip("Folder on the analyzer disk with the state files (*.csa), e.g.\n"
                                "C:\\Users\\Public\\Documents\\Network Analyzer")
                self.widgets["state.dir"] = vdir
                ws = [vdir]
                if "state_dirs" in proxy.actions:
                    self.vdir_button = self._tool("…", "Choose the folder on the analyzer", self._ask_dirs)
                    self.vdir_button.setFixedWidth(TRACE_CHIP_HEIGHT)
                    ws.append(self.vdir_button)
                save.append(PanelLine("VNA", *ws))
                proxy.subscribe("state.dir", lambda _v: self.proxy.is_connected and self.proxy.action("state_list"))
            save.append(self.preset_line("STAT"))
        if hasattr(self, "data_button"):
            self.dir_edit = QLineEdit(str(self.data_dir()))
            self.dir_edit.setToolTip("Folder on this PC for DATA SAVE and Touchstone files")
            _save_dir = lambda: self.settings.setValue(f"{pre}/DATA_DIR", self.dir_edit.text().strip())
            self.dir_edit.returnPressed.connect(_save_dir)
            self.dir_edit.editingFinished.connect(_save_dir)
            browse = self._tool("…", "Choose the folder", self._browse_dir)
            browse.setFixedWidth(TRACE_CHIP_HEIGHT)
            self.name_edit = QLineEdit(str(self.settings.value(f"{pre}/DATA_NAME", "")))
            self.name_edit.setPlaceholderText("file name prefix, e.g. DUT")
            self.name_edit.setToolTip("Prefix of the file name; date and time are added: <prefix>_YYYYMMDD_HHMMSS.csv")
            _save_name = lambda: self.settings.setValue(f"{pre}/DATA_NAME", self.name_edit.text().strip())
            self.name_edit.returnPressed.connect(_save_name)
            self.name_edit.editingFinished.connect(_save_name)
            save += [PanelLine("PC", self.dir_edit, browse), PanelLine("NAME", self.name_edit)]
            export_row = [PanelLine("DATA", self.data_button)]
            if "snp_read" in proxy.actions:
                b = self.s2p_button = QPushButton("SAVE")
                b.setObjectName("choice")
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip("Save full S-parameter matrix of the active channel to a Touchstone .s2p/.s4p file\n"
                             "(one fresh sweep; folder and name - PC / NAME)")
                b.clicked.connect(self.save_snp)
                for sig in (proxy.comm_error, proxy.instrument_error, proxy.refused):
                    sig.connect(lambda *_a: (not sip.isdeleted(self)) and self.s2p_button.text() == "…"
                                and self._flash("ERROR", self.s2p_button))
                proxy.state_changed.connect(self._on_s2p_state)
                b.setEnabled(proxy.is_connected)
                export_row.append(PanelLine("SNP", b))
            save.append(tuple(export_row))
        if save:
            self.page_specs.append(("SAVE", (), PanelTable(*save)))

    def _sync_port_widgets(self) -> None:
        if sip.isdeleted(self):
            return
        n_ports = max(1, min(MAX_PORTS, int(getattr(self.proxy, "ports", 2) or 2)))
        if hasattr(self, "port_button") and not sip.isdeleted(self.port_button):
            pairs = [(f"P{p}", p) for p in range(1, n_ports + 1)]
            self.port_button._names = tuple(n for n, _ in pairs)
            self.port_button._data = tuple(d for _, d in pairs)
            cur = self.proxy.value("gen.port")
            if cur is not None:
                self.port_button.show_value(cur)
        for p, ln in self._port_power_lines.items():
            if not sip.isdeleted(ln):
                ln.setVisible(p <= n_ports)

    def open_cal_window(self) -> None:
        w = self.cal_window
        if not w.isVisible() and not w._pos_restored:
            w.move(self.pos() + QPoint(self.width() + 8, 0))
        w.open()

    def _on_s2p_state(self, _s) -> None:
        if sip.isdeleted(self) or not hasattr(self, "s2p_button"):
            return
        if not self.proxy.is_connected and self.s2p_button.text() == "…":
            self.s2p_button.setText("SAVE")
        self.s2p_button.setEnabled(self.proxy.is_connected and self.s2p_button.text() != "…")

    def _source_cal_freq(self) -> float | None:
        p = self.proxy
        if p.value("gen.state") is True or p.value("sweep.type") == "CW":
            f = p.value("freq.cw")
            if isinstance(f, (int, float)):
                return float(f)
        c = p.value("freq.center")
        if isinstance(c, (int, float)):
            return float(c)
        lo, hi = p.value("freq.start"), p.value("freq.stop")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            return (float(lo) + float(hi)) / 2.0
        return None

    def on_param(self, key: str, value: Any) -> None:
        if hasattr(self, "source_cal") and key in ("freq.cw", "freq.center", "freq.start", "freq.stop",
                                                   "sweep.type", "gen.state"):
            freq = self._source_cal_freq()
            if freq != getattr(self, "_last_cal_freq", object()):
                self._last_cal_freq = freq
                self.source_cal.apply_correction()

    def restore(self) -> None:
        if hasattr(self, "source_cal"):
            self.source_cal.apply_correction()
        super().restore()

    def cleanup(self) -> None:
        if hasattr(self, "source_cal"):
            self.source_cal.cleanup()
        cw = getattr(self, "cal_window", None)
        if cw is not None and not sip.isdeleted(cw):
            cw.close()
            cw.deleteLater()
            self.cal_window = None
        super().cleanup()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if getattr(self, "cal_window", None) is not None and not sip.isdeleted(self.cal_window):
            self.cal_window.close()
        super().closeEvent(event)

    # ------------------------------------------------------------ папка состояний на анализаторе
    _dirs_asked = False

    def _ask_dirs(self) -> None:
        if self.proxy.is_connected:
            self._dirs_asked = True
            self.proxy.action("state_dirs")

    @staticmethod
    def dir_choices(path: str, subdirs: list[str]) -> list[tuple[str, str]]:
        path = path.rstrip("\\")
        out = []
        if "\\" in path:
            parent = path.rsplit("\\", 1)[0]
            out.append(("..", parent + "\\" if parent.endswith(":") else parent))
        base = path + "\\"
        return out + [(d, base + d) for d in subdirs]

    def _dirs_menu(self, path: str, subdirs: list[str]) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setObjectName("psmenu")
        title = menu.addAction(path)
        title.setEnabled(False)
        menu.addSeparator()
        choices = self.dir_choices(path, subdirs)
        for text, full in choices:
            menu.addAction(text).triggered.connect(lambda _=False, f=full: self.proxy.set("state.dir", f))
        if not choices:
            menu.addAction("(no folders)").setEnabled(False)
        b = self.vdir_button
        menu.popup(b.mapToGlobal(QPoint(b.width() - menu.sizeHint().width(), b.height())))
        self._dirs_popup = menu

    # ------------------------------------------------------------ экспорт данных (CSV / Touchstone)
    def data_dir(self) -> Path:
        saved = str(self.settings.value(f"{self._prefix}/DATA_DIR", "") or "").strip()
        return Path(saved) if saved else Path(self.settings.fileName()).resolve().parent / "vna_data"

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for VNA data", str(self.data_dir()))
        if d:
            self.dir_edit.setText(d)
            self.settings.setValue(f"{self._prefix}/DATA_DIR", d)

    def read_traces(self) -> list[tuple[str, Trace]]:
        """Включённые пользовательские трассы (отформатированные с учётом EDEL / SMO)."""
        out: list[tuple[str, Trace]] = []
        if hasattr(self, "trace_editor"):
            for spec in self.trace_editor.traces:
                if not spec.enabled:
                    continue
                raw = self.proxy.channel_trace(spec.channel, spec.sparam)
                if raw is None or len(raw) == 0:
                    continue
                out.append((spec.display_label(), format_sparam_trace(raw, spec)))
            return out
        for sp, tr in self.proxy.sparam_traces().items():
            out.append((sp, tr))
        return out

    def save_data(self) -> Path | None:
        traces = self.read_traces()
        if not traces:
            self._flash("NO DATA")
            return None
        path = self._data_path("", "csv")
        comments = [f"{self.windowTitle() or 'VNA'}, saved {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for label, t in traces:
            m = t.meta
            extra = ", ".join(f"{k} {m[k]}" for k in ("bw.if", "source.power", "avg.state", "avg.count", "cal.state")
                              if m.get(k) is not None)
            comments.append(f"{label}: channel {m.get('channel')}, {len(t)} points"
                            + (f", {extra}" if extra else ""))
        try:
            write_traces_csv(path, [t for _l, t in traces], [lbl for lbl, _t in traces], comments)
        except OSError as e:
            self._flash("ERROR")
            self.data_button.setToolTip(f"Not saved: {e}")
            return None
        self._flash("SAVED")
        self.data_button.setToolTip(f"Last file: {path}")
        return path

    def _flash(self, text: str, b: QPushButton | None = None) -> None:
        b = b or self.data_button
        timer = getattr(b, "_flash_timer", None)
        if timer is None:
            timer = b._flash_timer = QTimer(b)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: b.setText("SAVE"))
        b.setText(text)
        timer.start(1500)

    def _data_path(self, tag: str, ext: str) -> Path:
        name = (getattr(self, "name_edit", None) and self.name_edit.text().strip()) or ""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = re.sub(r"[^\w.-]+", "_", "_".join(p for p in (name, tag, stamp) if p))
        folder = Path(self.dir_edit.text().strip()) if getattr(self, "dir_edit", None) and \
            self.dir_edit.text().strip() else self.data_dir()
        path, k = folder / f"{base}.{ext}", 2
        while path.exists():
            path, k = folder / f"{base}-{k}.{ext}", k + 1
        return path

    def save_snp(self) -> None:
        """SNP SAVE: прочитать полную матрицу S-параметров канала -> ``.s2p`` / ``.s4p``."""
        if self.proxy.is_connected and self.s2p_button.text() != "…":
            self.s2p_button.setText("…")
            self.s2p_button.setEnabled(False)
            self.proxy.action("snp_read")

    save_s2p = save_snp

    def _write_snp(self, sparams: dict) -> Path | None:
        t = next(iter(sparams.values()))
        m = t.meta
        n_ports = int(round(len(sparams) ** 0.5)) or 2
        ext = f"s{n_ports}p"
        path = self._data_path(f"CH{m.get('channel', '')}", ext)
        comments = [f"{self.windowTitle() or 'VNA'}, saved {time.strftime('%Y-%m-%d %H:%M:%S')}",
                    f"channel {m.get('channel')}, {len(t)} points, cal set {m.get('cal.set') or '-'}, "
                    f"correction {'ON' if m.get('cal.state') else 'OFF'}, IFBW {m.get('bw.if')}, "
                    f"power {m.get('source.power')} dBm"]
        try:
            write_touchstone(path, sparams, comments)
        except (OSError, ValueError) as e:
            self._flash("ERROR", self.s2p_button)
            self.s2p_button.setToolTip(f"Not saved: {e}")
            return None
        self._flash("SAVED", self.s2p_button)
        self.s2p_button.setToolTip(f"Last file: {path}")
        return path

    def restore_rank(self, key: str) -> int:
        if key.endswith(("_auto", "_state")) or key == "avg.count":
            return 3
        return RESTORE_RANK.get(key, 2)

    # ------------------------------------------------------------ состояния в приборе (SAVE)
    def create_presets(self, key: str) -> PresetGroup:
        return StatePresets(self.settings, key, self._delete_state)

    def refresh_lists(self) -> None:
        if hasattr(self, "cal_wizard"):
            self.cal_wizard.refresh()
        for name in ("state_list", "cal_list", "chan_list"):
            if name in self.proxy.actions:
                self.proxy.action(name)

    def state_name(self) -> str | None:
        if self.proxy.value("sweep.type") == "CW":
            f = self.proxy.value("freq.cw")
            parts = ["CW", f]
        else:
            parts = [self.proxy.value("freq.start"), self.proxy.value("freq.stop")]
        if any(v is None for v in parts):
            return None
        def text(v):
            if isinstance(v, str):
                return v
            s = si_format(v, "Hz", 4).replace(" ", "").replace(".", "p").replace("µ", "u")
            return re.sub(r"[^A-Za-z0-9_\-]", "", s)
        base = "_".join(text(v) for v in parts)[:28] or "STATE"
        name, n = None, 1
        while name is None or name in self.presets.names():
            suffix = "" if n == 1 else f"-{n}"
            name = f"{base[:32 - len(suffix)]}{suffix}"
            n += 1
        return name

    def _add_preset(self) -> None:
        name = self.state_name()
        if name is not None:
            self._select_after_list = name
            self.proxy.action("state_save", name)
            self.refresh_lists()

    def _delete_state(self, name: str) -> None:
        self.proxy.action("state_delete", name)
        self.refresh_lists()

    def apply_preset_values(self, preset: tuple) -> None:
        self.proxy.action("state_recall", preset[0])

    def _on_action(self, name: str, result) -> None:
        if sip.isdeleted(self):
            return
        if name == "snp_read" and hasattr(self, "s2p_button"):
            self.s2p_button.setEnabled(self.proxy.is_connected)
            if isinstance(result, dict) and result:
                self._write_snp(result)
            elif self.s2p_button.text() == "…":
                self._flash("ERROR", self.s2p_button)
        elif name == "state_dirs" and self._dirs_asked and isinstance(result, (list, tuple)) and len(result) == 2:
            self._dirs_asked = False
            self._dirs_menu(str(result[0]), [str(d) for d in result[1]])
        elif name == "state_list" and isinstance(result, (list, tuple)):
            self.presets.set_names([str(n) for n in result], self._select_after_list)
            self._select_after_list = None
        elif name == "state_recall":
            if "chan_list" in self.proxy.actions:
                self.proxy.action("chan_list")
            if hasattr(self, "trace_editor"):
                self.trace_editor._sync_hardware()
