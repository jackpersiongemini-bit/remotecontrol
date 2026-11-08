"""Окно источников питания (профиль power_supply: ITECH IT6300 и любые другие).

Избранное - плитки источников, по три в ряд; в неполном ряду плитки растягиваются на всю ширину
(ряды - QHBoxLayout, не сетка). Плитка - один канал:
* поля V и I: пока поле не выбрано, в нём ИЗМЕРЕННОЕ значение, слева мелко - уставка;
  выбор поля = настройка: уставка уходит в подсказку, слева мелко - измерение; Enter - записать,
  Esc - отменить (как у всех полей окна);
* клик по плитке вне полей - включить / выключить выход; включённый выход - индикатор
  (рамка и толстая нижняя граница), как у остальных кнопок;
* в заголовке плитки - имя канала и режим (CV / CC, OVP - сработала защита).

Блокировка CH1 (правило лаборатории): CH1 включается, только если CH2 и CH3 потребляют ток не
меньше порога; вкладка OUT, строка ILCK: ON - блокировка действует, OFF - обход. Выключить можно всегда.
Ниже плиток - OFF ALL. Вкладки: PROT (OVP, предел уставки по каналам), OUT (объединение каналов).

``SourceGrid`` принимает любой набор каналов [(прокси, номер, подпись)], в том числе с нескольких
приборов: окно отдаёт ему каналы своего прибора.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QRectF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..core.params import ConnectionState
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow
from .params import PLabel, PLineEdit, PSButton, PanelLine, PanelTable, _repolish, paint_indicator
from .power_meter import ActionButton, LToggle
from .theme import UI_COLUMN_WIDTH_PX, render_qss

__all__ = ["PSUWindow", "SourceTile", "SourceGrid", "SourceField", "saved_connection"]

CONNECTION_GROUP = "CONNECTION/PSU"
PER_ROW = 3
GATED, GATE_BY = 1, (2, 3)              # CH1 - только при потреблении CH2 и CH3
GATE_MIN_A = 1e-3                       # порог «потребляет ток», А (ini: <prefix>/GATE_MIN_A)


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _fmt(value, unit: str) -> str:
    return "---" if not isinstance(value, (int, float)) else si_format(value, unit, 5)


class SourceField(PLineEdit):
    """Поле уставки, которое в покое показывает измерение.

    Привязано к ключу уставки (запись, проверка, цвет рамки - как у PLineEdit); ``meas_key`` -
    измеренное значение для показа. ``side`` - мелкий текст внутри поля, снизу слева, в тех же
    единицах, что и основной: уставка в покое, измерение во время настройки."""

    side: QLabel | None = None

    def __init__(self, proxy: InstrumentProxy, key: str, meas_key: str, parent=None):
        self.meas_key, self._meas = meas_key, None
        super().__init__(proxy, key, digits=5, parent=parent)
        self.side = QLabel("", self)
        self.side.setObjectName("side")
        self.side.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        f = self.side.font()
        f.setPointSizeF(max(6.0, f.pointSizeF() * 0.75))
        self.side.setFont(f)
        self.setMinimumHeight(46)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)   # не захватывать фокус при показе окна
        proxy.subscribe(meas_key, self._on_meas)
        self._place_side()

    def _side_text(self, value) -> str:
        if not isinstance(value, (int, float)):
            return "---"
        return f"{value / self._factor():.{self.digits}g}"

    def _place_unit(self) -> None:
        """Единица - сверху слева, мелкий текст - под ней; число справа на всю высоту."""
        b = self.unit_button
        if b is None:
            return
        b.ensurePolished()
        w = max(b.sizeHint().width(), 26)
        b.setGeometry(2, 2, w, max(int(self.height() * 0.5) - 2, 0))
        self.setTextMargins(w, 0, 0, 0)

    def _set_side(self, value) -> None:
        if self.side is not None:
            self.side.setText(self._side_text(value))
            self._place_side()

    def _place_side(self) -> None:
        if self.side is None:
            return
        self.side.adjustSize()
        self.side.move(6, self.height() - self.side.height() - 2)

    def resizeEvent(self, e):  # noqa: N802 - Qt
        super().resizeEvent(e)
        self._place_side()

    @property
    def measured(self):
        return self._meas

    def _on_meas(self, value: Any) -> None:
        self._meas = value
        self.show_value(self._value)

    def show_value(self, value, force: bool = False):
        if self._editing() and not force:          # настройка: уставка - в подсказке, слева - измерение
            self.setPlaceholderText("" if value is None else f"{value / self._factor():.{self.digits}g}")
            self._set_side(self._meas)
            return
        shown = self._meas if self._meas is not None else None
        self._auto_prefix(shown)
        self._update_unit()
        self.setText("" if shown is None else f"{shown / self._factor():.{self.digits}g}")
        self.setModified(False)
        self._set_side(value)

    def focusInEvent(self, e):  # noqa: N802 - Qt
        QLineEdit.focusInEvent(self, e)
        self.clear()
        self.setModified(False)
        self.show_value(self._value)               # подсказка - уставка, слева - измерение


class SourceTile(QFrame):
    """Плитка канала. Ведёт себя как привязанный к ``chN.output`` виджет (key / proxy / spec /
    set_value), поэтому журнал ошибок и цвет рамки при записи работают как у кнопок."""

    STYLE = render_qss("""
        SourceTile { background-color: $UI_COLOR_BACKGROUND; border: ${UI_BORDER_WIDTH_PX}px solid $UI_COLOR_BACKGROUND;
            border-radius: ${UI_BORDER_RADIUS_PX}px; }
        SourceTile:hover { border-color: $UI_COLOR_BORDER; }
        SourceTile[wstate="process"] { border-color: $UI_COLOR_PROCESS; }
        SourceTile[wstate="error"] { border-color: $UI_COLOR_ERROR; }
        SourceTile QLabel#side { color: $UI_COLOR_BORDER_LIGHT; }
        SourceTile QLabel { background: transparent; color: $UI_COLOR_BORDER_LIGHT; padding: 0; }
        SourceTile QLabel#name { color: $UI_COLOR_TEXT; font-weight: $UI_FONT_WEIGHT_SEMIBOLD; }
        SourceTile QLabel#mode[mode="CC"] { color: $UI_COLOR_PROCESS; }
        SourceTile QLabel#mode[mode="OVP"] { color: $UI_COLOR_ERROR; font-weight: $UI_FONT_WEIGHT_SEMIBOLD; }
        SourceTile PLineEdit { background-color: $UI_COLOR_PANEL; border-color: $UI_COLOR_PANEL; }
    """)

    def __init__(self, proxy: InstrumentProxy, n: int, title: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE)
        self.proxy, self.n = proxy, n
        self.key = f"ch{n}.output"
        self.spec = proxy.params[self.key]
        self._value: bool | None = None
        self.locked = False                         # блокировка (CH1 без потребления CH2/CH3)
        self.lock_reason = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(150)

        self.name = QLabel(title or f"CH{n}")
        self.name.setObjectName("name")
        self.mode = QLabel("")
        self.mode.setObjectName("mode")
        self.head = QHBoxLayout()
        self.head.setContentsMargins(0, 0, 0, 0)
        self.head.setSpacing(6)
        self.head.addWidget(self.name)
        self.head.addStretch(1)
        self.head.addWidget(self.mode)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 10)
        lay.setSpacing(4)
        lay.addLayout(self.head)
        self.fields: dict[str, SourceField] = {}
        for q, set_key, meas_key in (("V", "voltage", "meas_v"), ("I", "current", "meas_i")):
            f = SourceField(proxy, f"ch{n}.{set_key}", f"ch{n}.{meas_key}")
            f.setToolTip(f"CH{n} {q}: measured; small text - setpoint. Click to set")
            self.fields[q] = f
            lay.addWidget(f)

        proxy.subscribe(self.key, self._on_value, self._on_status)
        for k in (f"ch{n}.mode", f"ch{n}.ovp_tripped"):
            if proxy.supports(k):
                proxy.subscribe(k, lambda _v: self._update_mode())
        proxy.state_changed.connect(lambda _s: self._update_tip())
        self._update_tip()

    # ------------------------------------------------------------ как привязанный виджет
    @property
    def value(self):
        return self._value

    def set_value(self, value, force: bool = False) -> bool:
        if not force and self._value is not None and bool(value) == bool(self._value):
            return False
        self.proxy.set(self.key, bool(value))
        return True

    def _on_value(self, value) -> None:
        self._value = value
        self._update_mode()
        self.update()

    def _on_status(self, status, message: str) -> None:
        from .params import _WSTATE
        self.setProperty("wstate", _WSTATE.get(status, ""))
        _repolish(self)

    # ------------------------------------------------------------ вид
    def is_on(self) -> bool:
        return self._value is True

    def _update_mode(self) -> None:
        p, n = self.proxy, self.n
        if p.value(f"ch{n}.ovp_tripped") is True:
            mode = "OVP"
        elif self.is_on():
            mode = p.value(f"ch{n}.mode") or ""
            mode = "" if mode == "OFF" else mode
        else:
            mode = "OFF"
        self.mode.setText(mode)
        if self.mode.property("mode") != mode:
            self.mode.setProperty("mode", mode)
            _repolish(self.mode)
        self._update_tip()

    def set_locked(self, locked: bool, reason: str = "") -> None:
        self.locked, self.lock_reason = locked, reason
        self._update_tip()

    def _update_tip(self) -> None:
        if not self.proxy.is_connected:
            tip = "Not connected"
        elif self.locked and not self.is_on():
            tip = f"Locked: {self.lock_reason}"
        else:
            tip = f"Click to switch CH{self.n} output {'OFF' if self.is_on() else 'ON'}"
        self.setToolTip(tip)

    def paintEvent(self, e):  # noqa: N802 - Qt
        super().paintEvent(e)
        if self.is_on():
            p = QPainter(self)
            paint_indicator(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

    def mouseReleaseEvent(self, e):  # noqa: N802 - Qt
        """Клик по плитке вне полей - выход вкл / выкл (выключить можно всегда)."""
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.toggle()
        super().mouseReleaseEvent(e)

    def toggle(self) -> bool:
        if not self.proxy.is_connected or self._value is None:
            return False
        if not self.is_on() and self.locked:
            return False
        return self.set_value(not self.is_on(), force=True)


class SourceGrid(QWidget):
    """Плитки по PER_ROW в ряд; неполный ряд растягивается на всю ширину."""

    def __init__(self, channels, parent=None):
        super().__init__(parent)
        self.tiles: list[SourceTile] = [SourceTile(p, n, title) for p, n, title in channels]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.rows: list[QHBoxLayout] = []
        for i in range(0, len(self.tiles), PER_ROW):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            for t in self.tiles[i:i + PER_ROW]:
                row.addWidget(t, 1)
            lay.addLayout(row)
            self.rows.append(row)


class PSUWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "it6300"
    PRESET_KEY = "PSU_PRESETS"             # пресетов нет

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Power supply", parent=None):
        super().__init__(proxy, settings, title, parent)
        for n in (GATED, *GATE_BY):
            for k in (f"ch{n}.meas_i", f"ch{n}.output"):
                if proxy.supports(k):
                    proxy.subscribe(k, lambda _v: self.update_gate())
        proxy.state_changed.connect(lambda _s: self.update_gate())
        self.update_gate()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, s, pre = self.proxy, self.settings, self._prefix
        nch = proxy.channels
        self.grid = SourceGrid([(proxy, n, f"CH{n}") for n in range(1, nch + 1)
                                if proxy.supports(f"ch{n}.output")])
        self.tiles = {t.n: t for t in self.grid.tiles}
        for t in self.grid.tiles:
            self.saved_widgets += [t.fields["V"], t.fields["I"]]       # выходы после Init не включаем
        # блокировка CH1: замок в плитке, закрыт - блокировка действует, открыт - обход
        self.gate_lock = None
        if GATED in self.tiles and all(n in self.tiles for n in GATE_BY):
            self.gate_lock = LToggle([("ON", True), ("OFF", False)],
                                     not s.value(f"{pre}/GATE_BYPASS", False, type=bool))
            self.gate_lock.changed.connect(self._gate_toggled)
        self.gate_min_a = s.value(f"{pre}/GATE_MIN_A", GATE_MIN_A, type=float)
        grid_line = PanelLine("", self.grid)
        grid_line._name.hide()
        self.top_lines = [grid_line]
        if "all_off" in proxy.actions:
            self.all_off = ActionButton(proxy, "all_off", "OFF ALL", "Switch all outputs off")
            line = PanelLine("", self.all_off)
            line._name.hide()
            self.top_lines.append(line)

        # PROT: защита по каналам (столбцы CH1..CHn)
        chans = sorted(self.tiles)
        heads = []
        for n in chans:
            h = PLabel(text=f"CH{n}")
            h.setObjectName("heading")
            heads.append(h)
        rows = [PanelLine("", *heads)]
        onoff = [("OFF", False), ("ON", True)]
        for title, key, make in (("OVP", "ovp_state", lambda p, k: PSButton(p, k, onoff)),
                                 ("OVPL", "ovp_level", lambda p, k: PLineEdit(p, k)),
                                 ("VLIM", "vlimit", lambda p, k: PLineEdit(p, k))):
            ws = [make(proxy, f"ch{n}.{key}") for n in chans if proxy.supports(f"ch{n}.{key}")]
            if len(ws) == len(chans):
                self.saved_widgets += ws
                rows.append(PanelLine(title, *ws))
        if "clear_ovp" in proxy.actions:
            rows.append(PanelLine("TRIP", ActionButton(proxy, "clear_ovp", "CLEAR", "Clear OVP trip on all channels")))
        self.page_specs.append(("PROT", tuple(f"ch{n}.ovp_state" for n in chans
                                              if proxy.supports(f"ch{n}.ovp_state")), PanelTable(*rows)))
        # OUT: объединение каналов
        out = []
        if proxy.supports("combine"):
            w = PSButton(proxy, "combine", [(c, c) for c in proxy.params["combine"].choices])
            self.saved_widgets.append(w)
            out.append(PanelLine("COMB", w))
        if self.gate_lock is not None:
            out.append(PanelLine("ILCK", self.gate_lock))
        if out:
            self.page_specs.append(("OUT", (), PanelTable(*out)))

    def restore_rank(self, key: str) -> int:
        return 0 if key == "combine" else 2

    # ------------------------------------------------------------ блокировка CH1
    def _gate_toggled(self, locked: bool) -> None:
        self.settings.setValue(f"{self._prefix}/GATE_BYPASS", not locked)
        self.update_gate()

    def gate_open(self) -> bool:
        if self.gate_lock is None or self.gate_lock.value is not True:
            return True
        p = self.proxy
        return all(p.value(f"ch{n}.output") is True and (p.value(f"ch{n}.meas_i") or 0.0) >= self.gate_min_a
                   for n in GATE_BY)

    def update_gate(self) -> None:
        if self.gate_lock is None:
            return
        tile = self.tiles[GATED]
        self.gate_lock.setToolTip("Interlock ON: CH1 needs " + " and ".join(f"CH{n}" for n in GATE_BY)
                                  + f" drawing ≥ {si_format(self.gate_min_a, 'A', 3)}. Click to bypass"
                                  if self.gate_lock.value is True else "Interlock bypassed. Click to enable")
        tile.set_locked(not self.gate_open(),
                        " and ".join(f"CH{n}" for n in GATE_BY) + f" must draw ≥ {si_format(self.gate_min_a, 'A', 3)}")

    def _on_state(self, state: ConnectionState) -> None:
        super()._on_state(state)
        for t in (self.grid.tiles if hasattr(self, "grid") else ()):
            t._update_tip()
