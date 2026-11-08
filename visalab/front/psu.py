"""Окно источников питания (профиль power_supply: ITECH IT6300 и любые другие).

Избранное - плитки источников, по три в ряд; в неполном ряду плитки растягиваются на всю ширину
(ряды - QHBoxLayout, не сетка). Плитка - один канал:
* поля V и I: пока поле не выбрано, в нём ИЗМЕРЕННОЕ значение, слева мелко - уставка;
  выбор поля = настройка: уставка уходит в подсказку, слева мелко - измерение; Enter - записать,
  Esc - отменить (как у всех полей окна);
* клик по плитке вне полей - включить / выключить выход; включённый выход - индикатор
  (рамка и толстая нижняя граница), как у остальных кнопок;
* в заголовке плитки - имя канала и режим (CV / CC, OVP - сработала защита).

Правила (вкладка RULES): строки вида ``IF CH2.I < 5m OR CH2 OFF THEN CH1 OFF AND BLOCK ELSE UNBLOCK``
(синтаксис и семантика - в rules.py). BLOCK запрещает включать выход (плитка показывает причину в
подсказке), OFF выключает. Enter в строке - применить и сохранить, ошибка - красная рамка и подсказка.
Правило по умолчанию - лабораторное: CH1 только когда CH2 и CH3 включены и потребляют ток.
Ниже плиток - OFF ALL. Вкладки: PROT (OVP, предел уставки по каналам), OUT (объединение), RULES.

``SourceGrid`` принимает любой набор каналов [(прокси, номер, подпись)], в том числе с нескольких
приборов: окно отдаёт ему каналы своего прибора.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QRectF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..core.params import ConnectionState
from ..core.proxy import InstrumentProxy
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow
from .params import PLabel, PLineEdit, PSButton, PanelLine, PanelTable, _repolish, paint_indicator
from .power_meter import ActionButton, LToggle
from .rules import Rule, RuleError, parse_rule, resolve
from .theme import UI_COLUMN_WIDTH_PX, render_qss

__all__ = ["PSUWindow", "RulesTable", "SourceTile", "SourceGrid", "SourceField", "saved_connection"]

CONNECTION_GROUP = "CONNECTION/PSU"
PER_ROW = 3
DEFAULT_RULES = [("IF CH2.I < 1m OR CH2 OFF OR CH3.I < 1m OR CH3 OFF THEN CH1 OFF AND BLOCK ELSE UNBLOCK", True)]


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


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


class RuleRow(PanelLine):
    """Строка правила: текст, вкл / выкл, удалить."""

    def __init__(self, text: str, on: bool, channels, changed, remove, parent=None):
        self.edit = QLineEdit(text)
        self.edit.setObjectName("cfg")
        self.edit.setMinimumWidth(80)
        self.edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.edit.setCursorPosition(0)                          # длинное правило - показать начало
        self.edit.setStyleSheet(render_qss('QLineEdit#cfg[wstate="error"] { border-color: $UI_COLOR_ERROR; }'))
        self.edit.setPlaceholderText("IF CH2.I < 5m THEN CH1 OFF AND BLOCK ELSE UNBLOCK")
        self.toggle = LToggle([("ON", True), ("OFF", False)], on)
        self.toggle.setFixedWidth(48)
        self.delete = QPushButton("×")
        self.delete.setObjectName("choice")
        self.delete.setFixedWidth(28)
        self.delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete.setToolTip("Delete rule")
        super().__init__("", self.edit, self.toggle, self.delete, parent=parent)
        self._name.hide()
        self.line_layout.setStretchFactor(self.edit, 1)
        self.channels, self.rule, self.error = channels, None, ""
        self._changed = changed
        self.edit.editingFinished.connect(self.commit)
        self.toggle.changed.connect(lambda _v: self._changed())
        self.delete.clicked.connect(lambda: remove(self))
        self.parse()

    def parse(self) -> None:
        text = self.edit.text().strip()
        try:
            self.rule, self.error = (parse_rule(text, self.channels) if text else None), ""
        except RuleError as e:
            self.rule, self.error = None, str(e)
        self.edit.setProperty("wstate", "error" if self.error else "")
        self.edit.setToolTip(self.error or "IF <cond> THEN <actions> [ELSE <actions>]; Enter applies")
        _repolish(self.edit)

    def commit(self) -> None:
        if self.edit.isModified() or self.rule is None:
            self.edit.setModified(False)
            self.parse()
            self._changed()

    @property
    def active(self) -> Rule | None:
        return self.rule if self.toggle.value is True else None


class RulesTable(PanelTable):
    """Список правил с кнопкой «+»; ``changed`` вызывается после любой правки."""

    def __init__(self, entries, channels, changed, parent=None):
        super().__init__(parent=parent)
        self.channels, self._changed = set(channels), changed
        self.rows: list[RuleRow] = []
        self.add_button = QPushButton("+")
        self.add_button.setObjectName("choice")
        self.add_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setToolTip("Add rule")
        self.add_button.clicked.connect(lambda: self.add_rule("", True, focus=True))
        self.table_layout.addWidget(self.add_button)
        for text, on in entries:
            self.add_rule(text, on, notify=False)

    def build(self, column_width: int | None = None) -> None:
        """Столбцов нет: ширину задаёт сама строка (текст растягивается)."""

    def add_rule(self, text: str = "", on: bool = True, focus: bool = False, notify: bool = True) -> RuleRow:
        row = RuleRow(text, on, self.channels, self._changed, self.remove_rule)
        self.rows.append(row)
        self.lines.append(row)
        self.table_layout.insertWidget(len(self.rows) - 1, row)
        if focus:
            row.edit.setFocus()
        if notify:
            self._changed()
        return row

    def remove_rule(self, row: RuleRow) -> None:
        self.rows.remove(row)
        self.lines.remove(row)
        self.table_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._changed()

    def rules(self) -> list[Rule]:
        return [r.active for r in self.rows if r.active is not None]

    def entries(self) -> list[tuple[str, bool]]:
        return [(r.edit.text().strip(), r.toggle.value is True) for r in self.rows if r.edit.text().strip()]


class PSUWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "it6300"
    PRESET_KEY = "PSU_PRESETS"             # пресетов нет

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Power supply", parent=None):
        super().__init__(proxy, settings, title, parent)
        for n in self.tiles:
            for k in ("output", "meas_v", "meas_i", "meas_p"):
                if proxy.supports(f"ch{n}.{k}"):
                    proxy.subscribe(f"ch{n}.{k}", lambda _v: self.apply_rules())
        proxy.state_changed.connect(lambda _s: self.apply_rules())
        self.apply_rules()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, s, pre = self.proxy, self.settings, self._prefix
        nch = proxy.channels
        self.grid = SourceGrid([(proxy, n, f"CH{n}") for n in range(1, nch + 1)
                                if proxy.supports(f"ch{n}.output")])
        self.tiles = {t.n: t for t in self.grid.tiles}
        for t in self.grid.tiles:
            self.saved_widgets += [t.fields["V"], t.fields["I"]]       # выходы после Init не включаем
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
        if out:
            self.page_specs.append(("OUT", (), PanelTable(*out)))
        raw = s.value(f"{pre}/RULES", None)
        try:
            saved = [(str(r["text"]), bool(r.get("on", True))) for r in json.loads(raw)] if raw else None
        except (ValueError, TypeError, KeyError, AttributeError):
            saved = None
        if saved is None:
            saved = DEFAULT_RULES if all(n in self.tiles for n in (1, 2, 3)) else []
        self.rules_table = RulesTable(saved, set(self.tiles), self._rules_changed)
        self.page_specs.append(("RULES", (), self.rules_table))

    def restore_rank(self, key: str) -> int:
        return 0 if key == "combine" else 2

    # ------------------------------------------------------------ правила
    def _rules_changed(self) -> None:
        self.settings.setValue(f"{self._prefix}/RULES", json.dumps(
            [{"text": t, "on": on} for t, on in self.rules_table.entries()], ensure_ascii=False))
        self.apply_rules()
        self._fit_height()

    def _get(self, n: int, q: str):
        p = self.proxy
        if not p.is_connected:
            return None
        return p.value(f"ch{n}.output" if q == "OUT" else f"ch{n}.meas_{q.lower()}")

    def apply_rules(self) -> None:
        """Применить правила: заблокировать плитки, выключить выходы. Без связи - всё заблокировано."""
        if not hasattr(self, "rules_table"):
            return
        off, on, blocked, why = resolve(self.rules_table.rules(), self._get)
        for n, tile in self.tiles.items():
            tile.set_locked(n in blocked or not self.proxy.is_connected, why.get(n, ""))
        if not self.proxy.is_connected:
            return
        for n in sorted(off):
            if n in self.tiles and self.proxy.value(f"ch{n}.output") is True:
                self.proxy.set(f"ch{n}.output", False)
        for n in sorted(on):
            if n in self.tiles and self.proxy.value(f"ch{n}.output") is False:
                self.proxy.set(f"ch{n}.output", True)

    def _on_state(self, state: ConnectionState) -> None:
        super()._on_state(state)
        self.apply_rules()
        for t in (self.grid.tiles if hasattr(self, "grid") else ()):
            t._update_tip()
