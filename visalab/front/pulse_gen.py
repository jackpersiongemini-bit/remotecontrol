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
import time
from typing import Any

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from ..core.params import ConnectionState, Kind, PStatus
from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from ..drivers import DRIVERS, MODELS
from .params import PBGroup, PChoice, PLabel, PTabBar, PLineEdit, PNLabel, PSButton, Panel, PanelLine, PanelTable, WriteTracker
from .theme import OFF, ON, UI_COLUMN_WIDTH_PX, UI_CONTROL_HEIGHT_PX, WINDOW_BAR_HEIGHT
from .window import (AddressLabel, CustomExpandButton, CustomInitButton, CustomSyncButton, Switch,
                     ErrorIndicator, Window, short_address)

__all__ = ["PulseGenWindow", "PULSE_PRESETS", "ROWS", "saved_connection"]

PULSE_PRESETS = ((400e-3, 30e-6, "PULSE"),)       # (PERI, WID, форма)

def _auto(proxy: InstrumentProxy, key: str):
    """Виджет по типу параметра: BOOL - OFF/ON, ENUM - список, число/строка - поле."""
    spec = proxy.params[key]
    if spec.kind is Kind.BOOL:
        return PSButton(proxy, key, [("OFF", False), ("ON", True)])
    if spec.kind is Kind.ENUM:
        return PSButton(proxy, key, [(c, c) for c in spec.choices])
    return PLineEdit(proxy, key)


#: строка: (подпись, ключ без "chN.", фабрика виджета, сохранять в ini)
OUTPUT_ROW = ("OUT", "output",
              lambda p, k: PSButton(p, k, {f"{k[:3].upper()} OFF": False, f"{k[:3].upper()} ON": True}), True)

#: вкладки развёрнутой таблицы: (название, ключ включения режима или None, строки)
PAGES = (
    ("MAIN", None, (
        ("TYPE", "wave", _auto, True),
        ("DUTY", "duty", lambda p, k: PNLabel(p, k, "%"), False),
        ("PERI", "period", _auto, True),
        ("WID", "width", _auto, True),
        ("HLVL", "high", _auto, True),
        ("LLVL", "low", _auto, True),
        ("DLY", "delay", _auto, True),
        ("LOAD", "load", lambda p, k: PSButton(p, k, {"50 Ω": "50", "HiZ": "HIZ"}), True),
    )),
    ("MOD", "mod_state", (
        ("STAT", "mod_state", _auto, True),
        ("TYPE", "mod_type", _auto, True),
        ("SRC", "mod_source", _auto, True),
        ("SHP", "mod_shape", _auto, True),
        ("FRQ", "mod_freq", _auto, True),
        ("DEPT", "mod_depth", _auto, True),
        ("DEVI", "mod_dev", _auto, True),
    )),
    ("SWP", "sweep_state", (
        ("STAT", "sweep_state", _auto, True),
        ("TIME", "sweep_time", _auto, True),
        ("STRT", "sweep_start", _auto, True),
        ("STOP", "sweep_stop", _auto, True),
        ("MODE", "sweep_mode", _auto, True),
        ("DIR", "sweep_dir", _auto, True),
        ("TRIG", "sweep_trig", _auto, True),
    )),
    ("BRST", "burst_state", (
        ("STAT", "burst_state", _auto, True),
        ("MODE", "burst_mode", _auto, True),
        ("PER", "burst_period", _auto, True),
        ("CYC", "burst_cycles", _auto, True),
        ("TRIG", "burst_trig", _auto, True),
        ("DLY", "burst_delay", _auto, True),
    )),
)
ROWS = (OUTPUT_ROW,) + tuple(row for _t, _s, rows in PAGES for row in rows)
#: порядок восстановления: форма -> нагрузка (до уровней) -> остальное -> включение режимов -> выходы
RESTORE_RANK = {"wave": 0, "load": 1, "mod_state": 3, "sweep_state": 3, "burst_state": 3, "output": 4}


def _fmt(value: float) -> str:
    return si_format(value, "s", 4)


def _preset(period, width, wave="PULSE") -> tuple[float, float, str]:
    return float(period), float(width), str(wave).upper()


class PresetGroup(PBGroup):
    """Пресеты (period, width, wave) в QSettings (JSON). Чип в три строки: форма / период / ширина.

    Порядок на экране за сеанс не меняется (иначе ← → переставляли бы соседей). Использование
    запоминается, и в ini пресеты пишутся по последнему использованию - новый порядок виден
    после перезапуска.
    """

    def __init__(self, settings: QSettings, key: str, defaults=PULSE_PRESETS, parent=None):
        super().__init__(parent=parent)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.settings, self.key = settings, key
        self.data: list[tuple[float, float, str]] = []
        self._recent: list[tuple[float, float, str]] = []      # использованные за сеанс, последний - первый
        for preset in self._load(defaults):
            self._append(preset)

    def _load(self, defaults):
        raw = self.settings.value(self.key)
        try:
            items = defaults if raw is None else json.loads(str(raw))
            return [_preset(*p) for p in items]
        except (TypeError, ValueError):
            return [_preset(*p) for p in defaults]

    def saved_order(self) -> list[tuple[float, float, str]]:
        """Порядок для ini: использованные за сеанс (последний - первый), затем прочие как были."""
        recent = [p for p in self._recent if p in self.data]
        return recent + [p for p in self.data if p not in recent]

    def save(self) -> None:
        self.settings.setValue(self.key, json.dumps(self.saved_order(), separators=(",", ":")))

    def _append(self, preset):
        self.add_button("")
        self.data.append(preset)
        self._refresh()

    def _refresh(self) -> None:
        for b, (period, width, wave) in zip(self.buttons(), self.data):
            b.setText(f"{wave}\n{_fmt(period)}\n{_fmt(width)}")
            b.setToolTip(f"TYPE {wave}\nPERI {_fmt(period)}\nWID {_fmt(width)}")
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setFixedHeight(PRESET_HEIGHT)

    def touch(self, index: int) -> None:
        """Отметить пресет использованным и выделить (место на экране не меняется)."""
        if 0 <= index < len(self.data):
            preset = self.data[index]
            if preset in self._recent:
                self._recent.remove(preset)
            self._recent.insert(0, preset)
            self.set_checked(index)
            self.save()

    def find(self, period: float, width: float, wave: str = "PULSE") -> int:
        for i, (p, w, t) in enumerate(self.data):
            if (math.isclose(p, period, rel_tol=1e-9) and math.isclose(w, width, rel_tol=1e-9)
                    and t == str(wave).upper()):
                return i
        return -1

    def add(self, period: float, width: float, wave: str = "PULSE") -> bool:
        i = self.find(period, width, wave)
        if i < 0:
            self._append(_preset(period, width, wave))
            i = len(self.data) - 1
        self.touch(i)
        return True

    def remove_selected(self) -> bool:
        i = self.checked_index()
        if i < 0:
            return False
        self.remove_button(i)
        del self.data[i]
        self.save()
        return True


PRESET_HEIGHT = 56          # высота чипа пресета; «+» и «−» вместе (с зазором) - такой же высоты
PRESET_GAP = 4


class HScrollArea(QScrollArea):
    """Одна строка с прокруткой вбок без полосы, листается колесом мыши."""

    def __init__(self, widget: QWidget, height: int = PRESET_HEIGHT, parent=None):
        super().__init__(parent)
        self._height = height
        self.setObjectName("hscroll")
        self.setWidget(widget)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)
        self.setMinimumWidth(1)

    def sizeHint(self):  # noqa: N802 - Qt
        return QSize(1, self._height)

    def wheelEvent(self, e):  # noqa: N802 - Qt
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - (e.angleDelta().y() or e.angleDelta().x()))
        e.accept()


#: группа настроек подключения окна в ini (драйвер, адрес, модель)
CONNECTION_GROUP = "CONNECTION/PULSE_GEN"


def saved_connection(settings: QSettings) -> dict[str, str]:
    """Сохранённые в окне драйвер / адрес / модель (пустые строки - не задано)."""
    return {k: str(settings.value(f"{CONNECTION_GROUP}/{k.upper()}", "") or "")
            for k in ("driver", "address", "model")}


class PulseGenWindow(Window):
    #: APPLY в настройках подключения: (driver, address, model) - запускающий код пересоздаёт прокси
    reconnect_requested = pyqtSignal(str, str, str)

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Pulse generator", parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        self.setWindowTitle(title)
        self._prefix = re.sub(r"\W+", "_", proxy.address).strip("_").upper() or "SDG"
        self._resetting = False
        self._expanded = False
        nch = min(2, proxy.channels)
        self.channels: list[dict[str, Any]] = []

        # ---------------------------------------------------------- заголовок
        bar = self.titleBar
        self.on_button = Switch()
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
        self.error_indicator = ErrorIndicator()
        # ход записи виден по цвету рамки самого поля; в заголовке остаётся только индикатор ошибок
        for i, w in enumerate((self.on_button, self.sync_button, self.init_button, self.address,
                               self.error_indicator)):
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
        self.preset_scroll = HScrollArea(self.presets)
        half = (PRESET_HEIGHT - PRESET_GAP) // 2
        self.add_button = self._tool("+", "Add preset from CH1 TYPE / PERI / WID", self._add_preset, half)
        self.remove_button = self._tool("−", "Remove selected preset", self.presets.remove_selected, half)
        tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(PRESET_GAP)
        tl.addWidget(self.add_button)
        tl.addWidget(self.remove_button)
        top = PanelTable(PanelLine("OUT", *outputs), PanelLine("PRST", self.preset_scroll, tools))
        self.preset_panel = Panel(top)
        for w in outputs:
            w.setMinimumWidth(UI_COLUMN_WIDTH_PX)
        top.lines[1].line_layout.setAlignment(top.lines[1]._name, Qt.AlignmentFlag.AlignTop)

        # ---------------------------------------------------------- вкладки и таблицы (сворачиваются)
        self.tabs = PTabBar()
        self._overlay: str | None = None        # вместо таблицы: "errors" / "connection"
        self.pages: list[PanelTable] = []
        self._page_states: list[str | None] = []
        for title_, state, rows in PAGES:
            present = [r for r in rows if all(r[1] in ch for ch in self.channels)]
            if not present:
                continue
            heads = []
            for n in range(1, nch + 1):
                h = PLabel(text=f"CH{n}")
                h.setObjectName("heading")
                heads.append(h)
            lines = [PanelLine("", *heads)]
            lines += [PanelLine(t, *(ch[name] for ch in self.channels)) for t, name, _m, _s in present]
            self.pages.append(PanelTable(*lines))
            self._page_states.append(state)
            self.tabs.addTab(title_)
        self.tabs.currentChanged.connect(self.set_page)
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        bl.addWidget(self.tabs)
        for page in self.pages:
            bl.addWidget(page)
        self.error_page = self._build_error_page()
        bl.addWidget(self.error_page)
        self.connection_page = self._build_connection_page()
        bl.addWidget(self.connection_page)
        self.parameter_panel = Panel(body)
        label_w = 0
        for page in self.pages:
            page.build(UI_COLUMN_WIDTH_PX)
            label_w = max(label_w, page.lines[0]._name.width(), *(ln.label_hint() for ln in page.lines))
        for page in (*self.pages, self.connection_page):
            for ln in page.lines:
                ln.set_label_width(label_w)

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
        self.tracker.changed.connect(self.error_indicator.set_items)
        self.error_indicator.clicked.connect(self.show_errors)
        self.address.clicked.connect(self.show_connection)
        proxy.param_status.connect(self._log_status)
        proxy.state_changed.connect(self._on_state)
        proxy.sync_done.connect(self._on_sync_done)
        proxy.action_done.connect(self._on_action_done)
        proxy.param_updated.connect(self._on_param)
        proxy.comm_error.connect(self._on_comm_error)
        self._on_state(proxy.state)

        self.address.setMinimumWidth(self.address.sizeHint().width())
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), lay.minimumSize().width()))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.recall_size(QSize(self.minimumWidth(), 0))
        self.set_page(settings.value(f"{self._prefix}/TAB", 0, type=int), fit=False)
        self.set_expanded(settings.value(f"{self._prefix}/EXPANDED", False, type=bool))
        self._update_tabs()

    # ------------------------------------------------------------ вид
    def set_expanded(self, on: bool) -> None:
        """Полный вид - вкладки с таблицами; обычный - выходы, пресеты и полоса вкладок-индикаторов."""
        self._expanded = bool(on)
        self.expand_button.set_expanded(on)
        if not on:
            self._overlay = None
        self._show_pages()
        self.settings.setValue(f"{self._prefix}/EXPANDED", bool(on))
        self._fit_height()

    def showEvent(self, e):  # noqa: N802 - Qt
        super().showEvent(e)
        self._fit_height()                # до показа раскладки считаются неточно

    def _show_pages(self) -> None:
        overlay = self._overlay if self._expanded else None
        for i, page in enumerate(self.pages):
            page.setVisible(self._expanded and overlay is None and i == self.tabs.currentIndex())
        self.error_page.setVisible(overlay == "errors")
        self.connection_page.setVisible(overlay == "connection")
        self.tabs.show_selection = self._expanded and overlay is None
        self.tabs.update()

    def _on_tab_clicked(self, index: int) -> None:
        """Клик по вкладке: из журнала ошибок - обратно к таблице; в свёрнутом окне - развернуть на ней."""
        if index < 0:
            return
        if self._overlay:                         # журнал / подключение -> обратно к таблице
            self._overlay = None
            self.set_page(index, fit=self._expanded)
        if not self._expanded:
            self.set_page(index, fit=False)
            self.set_expanded(True)

    # ------------------------------------------------------------ журнал ошибок
    def _build_error_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("ERRORS")
        title.setObjectName("lineName")
        head.addWidget(title)
        head.addStretch()
        self.clear_button = self._tool("CLEAR", "Clear the error log and return to parameters", self.clear_errors)
        self.clear_button.setProperty("wide", True)
        head.addWidget(self.clear_button)
        lay.addLayout(head)
        self.error_log = QPlainTextEdit()
        self.error_log.setObjectName("errorLog")
        self.error_log.setReadOnly(True)
        self.error_log.setPlaceholderText("No errors")
        self.error_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.error_log.setFixedHeight(10 * self.error_log.fontMetrics().lineSpacing() + 12)
        lay.addWidget(self.error_log)
        page.hide()
        return page

    # ------------------------------------------------------------ подключение
    def _driver_kind(self) -> str:
        for kind, (cls, _sim) in DRIVERS.items():
            if cls is getattr(self.proxy, "driver_cls", None):
                return kind
        return "sdg"

    def _build_connection_page(self) -> QWidget:
        kind = self._driver_kind()
        profile = DRIVERS[kind][0].PROFILE
        saved = saved_connection(self.settings)
        drivers = [(f"{k.upper()}", k) for k, (cls, _s) in DRIVERS.items() if cls.PROFILE is profile]
        self.driver_choice = PChoice(drivers, kind)
        self.driver_choice.setToolTip("Driver (instrument family)")
        self.model_choice = PChoice([("AUTO", "")])
        self.model_choice.setToolTip("Model (limits); AUTO - from *IDN?")
        self.driver_choice.changed.connect(self._fill_models)
        self._fill_models(kind, saved["model"])
        address = "sim" if self.proxy.address.upper().startswith("SIM::") else self.proxy.address
        self.address_edit = QLineEdit(saved["address"] or address)
        self.address_edit.setObjectName("cfg")
        self.address_edit.setPlaceholderText("192.168.0.5 / USB0::... / sim")
        self.address_edit.setToolTip("VISA address: IP, IP:port, full VISA string, or sim")
        self.address_edit.returnPressed.connect(self.apply_connection)
        self.apply_button = self._tool("APPLY", "Save and reconnect with these settings", self.apply_connection)
        self.apply_button.setProperty("wide", True)
        table = PanelTable(PanelLine("DRV", self.driver_choice), PanelLine("MDL", self.model_choice),
                           PanelLine("ADDR", self.address_edit), PanelLine("", self.apply_button))
        table.lines[-1].line_layout.insertStretch(1)
        table.hide()
        return table

    def _fill_models(self, kind, value: str = "") -> None:
        models = [("AUTO", "")] + [(m, m) for m in MODELS.get(kind, {})]
        self.model_choice.set_choices(models, value)

    def show_connection(self) -> None:
        """Клик по адресу: вместо таблицы - драйвер / модель / адрес (выход - вкладка или APPLY)."""
        self._show_overlay("connection")

    def apply_connection(self) -> None:
        driver = self.driver_choice.value or self._driver_kind()
        address = self.address_edit.text().strip() or "sim"
        model = self.model_choice.value or ""
        for k, v in (("DRIVER", driver), ("ADDRESS", address), ("MODEL", model)):
            self.settings.setValue(f"{CONNECTION_GROUP}/{k}", v)
        self.settings.sync()
        self._overlay = None
        self._show_pages()
        self._fit_height()
        self.reconnect_requested.emit(driver, address, model)

    def _log(self, name: str, detail: str) -> None:
        detail = (detail or "error").strip().splitlines()[0] if detail else "error"
        self.error_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {name}: {detail}")

    def _log_status(self, key: str, status: PStatus, message: str) -> None:
        if key in self.tracker.keys and status in (PStatus.ERROR, PStatus.INVALID):
            self._log(self.tracker.names.get(key, key), message)

    def show_errors(self) -> None:
        """Клик по индикатору ошибок: вместо таблицы - журнал (выход - вкладка или CLEAR)."""
        self._show_overlay("errors")

    def _show_overlay(self, name: str) -> None:
        self._overlay = name
        if self._expanded:
            self._show_pages()
            self._fit_height()
        else:
            self.set_expanded(True)

    def clear_errors(self) -> None:
        self.error_log.clear()
        self.tracker.clear_errors()
        self._overlay: str | None = None        # вместо таблицы: "errors" / "connection"
        self._show_pages()
        self._fit_height()

    def _fit_height(self, again: bool = True) -> None:
        """Высота окна - по содержимому (вложенные раскладки пересчитываются не сразу)."""
        for w in (self.parameter_panel, self.parameter_panel.table, self.preset_panel):
            if w.layout():
                w.layout().invalidate()
        self._layout.invalidate()
        self._layout.activate()
        self.setFixedHeight(self._layout.sizeHint().height())
        if again:
            QTimer.singleShot(0, self._fit_height_again)   # связанный метод: не сработает после удаления окна

    def _fit_height_again(self) -> None:
        self._fit_height(False)

    def set_page(self, index: int, fit: bool = True) -> None:
        """Показать вкладку таблицы (MAIN / MOD / SWP / BRST)."""
        index = index if 0 <= index < len(self.pages) else 0
        if self.tabs.currentIndex() != index:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(index)
            self.tabs.blockSignals(False)
        self._show_pages()
        self.settings.setValue(f"{self._prefix}/TAB", index)
        if fit:
            self._fit_height()

    def _update_tabs(self) -> None:
        """Вкладка режима инвертирована, если режим включён хотя бы на одном канале."""
        for i, state in enumerate(self._page_states):
            on = bool(state) and any(self.proxy.value(f"ch{n}.{state}") is True
                                     for n in range(1, len(self.channels) + 1))
            self.tabs.set_on(i, on)

    @staticmethod
    def _tool(text, tip, slot, height=None) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        if height:
            b.setProperty("half", True)
        if height:
            b.setFixedHeight(height)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def keyPressEvent(self, e):  # noqa: N802 - Qt
        """← → - применить соседний пресет."""
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.presets.data:
            i = self.presets.checked_index()
            step = -1 if e.key() == Qt.Key.Key_Left else 1
            j = 0 if i < 0 else min(max(i + step, 0), len(self.presets.data) - 1)
            if j != i:
                self._apply_preset(j)
            e.accept()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------ сохранение
    def _settings_key(self, key: str) -> str:
        return f"{self._prefix}/{key.upper()}"

    def _saved_widgets(self):
        saved = {name for _t, name, _m, s in ROWS if s}
        return [w for ch in self.channels for name, w in ch.items() if name in saved]

    def _on_param(self, key: str, value: Any) -> None:
        if key.endswith("_state"):
            self._update_tabs()
        if self._resetting:              # после *RST прибор отдаёт заводские значения - не сохранять
            return
        if value is not None and any(w.key == key for w in self._saved_widgets()):
            self.settings.setValue(self._settings_key(key), value)

    def restore(self) -> None:
        """Отправить сохранённые значения (только отличающиеся от текущих)."""
        widgets = self._saved_widgets()
        # форма (от неё зависят PERI/WID/DLY) -> нагрузка (пересчитывает уровни, поэтому до HLVL/LLVL)
        # -> остальное -> включение режимов -> выходы последними
        for w in sorted(widgets, key=lambda w: RESTORE_RANK.get(w.key.split(".", 1)[1], 2)):
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
        wave = ch1["wave"].value if "wave" in ch1 else None
        if period is not None and width is not None:
            self.presets.add(period, width, wave or "PULSE")
            i = self.presets.checked_index()
            if i >= 0:
                self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[i], 0, 0)

    def _apply_preset(self, index: int) -> None:
        period, width, wave = self.presets.data[index]
        self.presets.touch(index)
        self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[index], 0, 0)
        self.setFocus()                       # чтобы ← → сразу переключали пресеты
        for ch in self.channels:
            if "wave" in ch:
                ch["wave"].set_value(wave)    # форма - первой: от неё зависят PERI/WID
            cur = ch["period"].value
            pairs = [(ch["period"], period)]
            if wave == "PULSE":               # ширина есть только у импульса
                pairs.append((ch["width"], width))
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

    def _on_comm_error(self, msg: str) -> None:
        self._log("CONNECTION", msg)
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

