"""Общее окно прибора в стиле PulseGen.py Алексея.

Заголовок: связь (ползунок), Sync, Init (*RST + восстановление сохранённого), адрес (клик -
настройки подключения), индикатор ошибок (клик - журнал), развернуть / свернуть.
Ниже - «избранное» (выходы, пресеты, главные поля) и сворачиваемые вкладки с таблицами
параметров. На месте таблицы могут открываться журнал ошибок и настройки подключения
(выход - клик по вкладке).

Конкретное окно (генератор импульсов, СВЧ генератор...) наследует ``InstrumentWindow`` и
задаёт в ``build()`` виджеты, строки избранного и вкладки. Окно знает только InstrumentProxy
и ключи профиля прибора.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from ..core.params import ConnectionState, PStatus
from ..core.proxy import InstrumentProxy
from ..drivers import DRIVERS
from .models import ModelWindow, all_models, model_class
from .params import PBGroup, PChoice, PTabBar, Panel, PanelLine, PanelTable, WriteTracker
from .theme import OFF, ON, UI_COLUMN_WIDTH_PX, WINDOW_BAR_HEIGHT
from .window import (AddressLabel, CustomExpandButton, CustomInitButton, CustomSyncButton, ErrorIndicator, Switch,
                     Window, short_address)

__all__ = ["InstrumentWindow", "PresetGroup", "HScrollArea", "saved_connection", "PRESET_HEIGHT", "PRESET_GAP",
           "LINKS", "ProxyLinks"]

PRESET_HEIGHT = 56          # высота чипа пресета; «+» и «−» вместе (с зазором) - такой же высоты
PRESET_GAP = 4


class ProxyLinks(QObject):
    """Прокси открытых в этом процессе окон: окно может брать значения другого прибора
    (ваттметр - скважность у ГСС) по имени профиля, не зная драйвера."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._proxies: list[InstrumentProxy] = []

    def add(self, proxy: InstrumentProxy) -> None:
        if proxy not in self._proxies:
            self._proxies.append(proxy)
            self.changed.emit()

    def remove(self, proxy: InstrumentProxy) -> None:
        if proxy in self._proxies:
            self._proxies.remove(proxy)
            try:
                self.changed.emit()
            except RuntimeError:                 # выход из программы: реестр уже удалён
                pass

    def find(self, profile: str) -> InstrumentProxy | None:
        """Последний открытый прибор с этим профилем (``function_generator``...)."""
        for proxy in reversed(self._proxies):
            prof = getattr(getattr(proxy, "driver_cls", None), "PROFILE", None)
            if prof is not None and prof.name == profile:
                return proxy
        return None


#: общий для процесса реестр приборов окон
LINKS = ProxyLinks()


def saved_connection(settings: QSettings, group: str) -> dict[str, str]:
    """Сохранённые в окне драйвер / адрес / модель (пустые строки - не задано)."""
    return {k: str(settings.value(f"{group}/{k.upper()}", "") or "") for k in ("driver", "address", "model")}


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)
    return a == b


class PresetGroup(PBGroup):
    """Пресеты - кортежи значений в QSettings (JSON); чип - несколько строк текста.

    ``normalize(*values) -> tuple`` приводит значения (и подставляет умолчания),
    ``describe(preset) -> (текст чипа, подсказка)``.

    Порядок на экране за сеанс не меняется (иначе ← → переставляли бы соседей). Использование
    запоминается, и в ini пресеты пишутся по последнему использованию - новый порядок виден
    после перезапуска.
    """

    def __init__(self, settings: QSettings, key: str, defaults, normalize: Callable[..., tuple],
                 describe: Callable[[tuple], tuple[str, str]], parent=None):
        super().__init__(parent=parent)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.settings, self.key = settings, key
        self.normalize, self.describe = normalize, describe
        self.data: list[tuple] = []
        self._recent: list[tuple] = []          # использованные за сеанс, последний - первый
        for preset in self._load(defaults):
            self._append(preset)

    def _load(self, defaults):
        raw = self.settings.value(self.key)
        try:
            items = defaults if raw is None else json.loads(str(raw))
            return [self.normalize(*p) for p in items]
        except (TypeError, ValueError):
            return [self.normalize(*p) for p in defaults]

    def saved_order(self) -> list[tuple]:
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
        for b, preset in zip(self.buttons(), self.data):
            text, tip = self.describe(preset)
            b.setText(text)
            b.setToolTip(tip)
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

    def find(self, *values) -> int:
        target = self.normalize(*values)
        for i, preset in enumerate(self.data):
            if all(_same(a, b) for a, b in zip(preset, target)):
                return i
        return -1

    def add(self, *values) -> bool:
        i = self.find(*values)
        if i < 0:
            self._append(self.normalize(*values))
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


class InstrumentWindow(Window):
    """База окна прибора. Наследник задаёт атрибуты класса и ``build()``."""

    #: APPLY в настройках подключения: (driver, address, model) - запускающий код пересоздаёт прокси
    reconnect_requested = pyqtSignal(str, str, str)

    CONNECTION_GROUP = "CONNECTION/INSTRUMENT"
    DEFAULT_KIND = ""
    PRESET_KEY = "PRESETS"
    PRESET_DEFAULTS: tuple = ()
    PRESET_ADD_TIP = "Add preset from current values"

    # ------------------------------------------------------------ точки расширения
    def build(self) -> None:  # pragma: no cover - абстрактный
        """Создать виджеты: ``self.saved_widgets`` (сохраняются в ini), ``self.top_lines``
        (строки избранного; строку пресетов даёт ``preset_line()``) и ``self.page_specs``
        [(название вкладки, ключи «режим включён», PanelTable)]."""
        raise NotImplementedError

    def preset_normalize(self, *values) -> tuple:  # pragma: no cover
        raise NotImplementedError

    def preset_describe(self, preset: tuple) -> tuple[str, str]:  # pragma: no cover
        raise NotImplementedError

    def preset_from_current(self) -> tuple | None:  # pragma: no cover
        raise NotImplementedError

    def apply_preset_values(self, preset: tuple) -> None:  # pragma: no cover
        raise NotImplementedError

    def create_presets(self, key: str) -> PresetGroup:
        return PresetGroup(self.settings, key, self.PRESET_DEFAULTS, self.preset_normalize, self.preset_describe)

    def restore_rank(self, key: str) -> int:
        """Порядок восстановления после Init: меньше - раньше (по умолчанию 2)."""
        return 2

    # ------------------------------------------------------------ сборка
    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Instrument", parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        self.setWindowTitle(title)
        self._prefix = re.sub(r"\W+", "_", proxy.address).strip("_").upper() or self.DEFAULT_KIND.upper()
        self._resetting = False
        self._expanded = False
        self._overlay: str | None = None        # вместо таблицы: "errors" / "connection"
        self.saved_widgets: list = []
        self.top_lines: list[PanelLine] = []
        self.page_specs: list[tuple[str, tuple[str, ...], PanelTable]] = []

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

        # ---------------------------------------------------------- пресеты и содержимое окна
        self.presets = self.create_presets(f"{self._prefix}/{self.PRESET_KEY}")
        self.presets.selected.connect(self._apply_preset)
        self.preset_scroll = HScrollArea(self.presets)
        self.build()

        top = PanelTable(*self.top_lines)
        self.preset_panel = Panel(top)
        top.build()

        self.tabs = PTabBar()
        self.pages: list[PanelTable] = []
        self._page_states: list[tuple[str, ...]] = []
        for title_, states, table in self.page_specs:
            self.pages.append(table)
            self._page_states.append(tuple(states))
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
        keys = list(dict.fromkeys(w.key for w in self.bound_widgets()))
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
        LINKS.add(proxy)
        self.destroyed.connect(lambda *_a, p=proxy: LINKS.remove(p))

        self.address.setMinimumWidth(self.address.sizeHint().width())
        self.setMinimumWidth(max(self.titleBar.sizeHint().width(), lay.minimumSize().width()))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.recall_size(QSize(self.minimumWidth(), 0))
        self.set_page(settings.value(f"{self._prefix}/TAB", 0, type=int), fit=False)
        self.set_expanded(settings.value(f"{self._prefix}/EXPANDED", False, type=bool))
        self._update_tabs()

    def bound_widgets(self) -> list:
        """Все виджеты, привязанные к ключам (для журнала ошибок)."""
        found = []
        for w in self.findChildren(QWidget):
            if hasattr(w, "key") and hasattr(w, "proxy") and getattr(w, "proxy", None) is self.proxy:
                found.append(w)
        return found

    def preset_line(self, name: str = "PRST") -> PanelLine:
        """Строка пресетов: одна строка чипов с прокруткой и «+» / «−» столбиком."""
        half = (PRESET_HEIGHT - PRESET_GAP) // 2
        self.add_button = self._tool("+", self.PRESET_ADD_TIP, self._add_preset, half)
        self.remove_button = self._tool("−", "Remove selected preset", self.presets.remove_selected, half)
        tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(PRESET_GAP)
        tl.addWidget(self.add_button)
        tl.addWidget(self.remove_button)
        line = PanelLine(name, self.preset_scroll, tools)
        line.line_layout.setAlignment(line._name, Qt.AlignmentFlag.AlignTop)
        return line

    # ------------------------------------------------------------ вид
    def set_expanded(self, on: bool) -> None:
        """Полный вид - вкладки с таблицами; обычный - избранное и полоса вкладок-индикаторов."""
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
        """Клик по вкладке: из журнала / подключения - обратно к таблице; в свёрнутом окне - развернуть."""
        if index < 0:
            return
        if self._overlay:
            self._overlay = None
            self.set_page(index, fit=self._expanded)
        if not self._expanded:
            self.set_page(index, fit=False)
            self.set_expanded(True)

    def _show_overlay(self, name: str) -> None:
        self._overlay = name
        if self._expanded:
            self._show_pages()
            self._fit_height()
        else:
            self.set_expanded(True)

    def _close_overlay(self) -> None:
        self._overlay = None
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
        """Показать вкладку таблицы."""
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
        """Индикатор на вкладке, если её режим включён."""
        for i, states in enumerate(self._page_states):
            self.tabs.set_on(i, any(self.proxy.value(k) is True for k in states))

    @staticmethod
    def _tool(text, tip, slot, height=None) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        if height:
            b.setProperty("half", True)
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

    def _log(self, name: str, detail: str) -> None:
        detail = (detail or "error").strip().splitlines()[0] if detail else "error"
        self.error_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {name}: {detail}")

    def _log_status(self, key: str, status: PStatus, message: str) -> None:
        if key in self.tracker.keys and status in (PStatus.ERROR, PStatus.INVALID):
            self._log(self.tracker.names.get(key, key), message)

    def show_errors(self) -> None:
        """Клик по индикатору ошибок: вместо таблицы - журнал (выход - вкладка или CLEAR)."""
        self._show_overlay("errors")

    def clear_errors(self) -> None:
        self.error_log.clear()
        self.tracker.clear_errors()
        self._close_overlay()

    # ------------------------------------------------------------ подключение
    def _driver_kind(self) -> str:
        for kind, (cls, _sim) in DRIVERS.items():
            if cls is getattr(self.proxy, "driver_cls", None):
                return kind
        return self.DEFAULT_KIND

    def _build_connection_page(self) -> QWidget:
        kind = self._driver_kind()
        profile = DRIVERS[kind][0].PROFILE
        saved = saved_connection(self.settings, self.CONNECTION_GROUP)
        drivers = [(k.upper(), k) for k, (cls, _s) in DRIVERS.items() if cls.PROFILE is profile]
        self.driver_choice = PChoice(drivers, kind)
        self.driver_choice.setToolTip("Driver (instrument family)")
        self.model_choice = PChoice([("AUTO", "")])
        self.model_choice.setToolTip("Model (limits); AUTO - from *IDN?")
        self.new_model_button = self._tool("+", "Create a model: limits and functions (based on the selected one)",
                                           self.new_model)
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
        table = PanelTable(PanelLine("DRV", self.driver_choice),
                           PanelLine("MDL", self.model_choice, self.new_model_button),
                           PanelLine("ADDR", self.address_edit), PanelLine("", self.apply_button))
        table.lines[-1].line_layout.insertStretch(1)
        table.hide()
        return table

    def _fill_models(self, kind, value: str = "") -> None:
        models = [("AUTO", "")] + [(m, m) for m in all_models(self.settings, kind)]
        self.model_choice.set_choices(models, value)
        self.new_model_button.setEnabled(model_class(kind) is not None)

    def new_model(self) -> None:
        """Окно создания модели; после SAVE модель появляется в списке MDL и выбирается."""
        kind = self.driver_choice.value or self._driver_kind()
        template = all_models(self.settings, kind).get(self.model_choice.value or "")
        self.model_window = ModelWindow(self.settings, kind, template)
        self.model_window.saved.connect(lambda name: self._fill_models(kind, name))
        self.model_window.move(self.pos() + self.rect().center() - self.model_window.rect().center())
        self.model_window.show()

    def show_connection(self) -> None:
        """Клик по адресу: вместо таблицы - драйвер / модель / адрес (выход - вкладка или APPLY)."""
        self._show_overlay("connection")

    def apply_connection(self) -> None:
        driver = self.driver_choice.value or self._driver_kind()
        address = self.address_edit.text().strip() or "sim"
        model = self.model_choice.value or ""
        for k, v in (("DRIVER", driver), ("ADDRESS", address), ("MODEL", model)):
            self.settings.setValue(f"{self.CONNECTION_GROUP}/{k}", v)
        self.settings.sync()
        self._close_overlay()
        self.reconnect_requested.emit(driver, address, model)

    # ------------------------------------------------------------ пресеты
    def _add_preset(self) -> None:
        preset = self.preset_from_current()
        if preset is None:
            return
        self.presets.add(*preset)
        i = self.presets.checked_index()
        if i >= 0:
            self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[i], 0, 0)

    def _apply_preset(self, index: int) -> None:
        preset = self.presets.data[index]
        self.presets.touch(index)
        self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[index], 0, 0)
        self.setFocus()                       # чтобы ← → сразу переключали пресеты
        self.apply_preset_values(preset)

    # ------------------------------------------------------------ сохранение
    def _settings_key(self, key: str) -> str:
        return f"{self._prefix}/{key.upper()}"

    def _on_param(self, key: str, value: Any) -> None:
        if any(key in states for states in self._page_states):
            self._update_tabs()
        self.on_param(key, value)
        if self._resetting:              # после *RST прибор отдаёт заводские значения - не сохранять
            return
        if value is not None and any(w.key == key for w in self.saved_widgets):
            self.settings.setValue(self._settings_key(key), value)

    def on_param(self, key: str, value: Any) -> None:
        """Для наследника: реакция на новое значение параметра."""

    def restore(self) -> None:
        """Отправить сохранённые значения (только отличающиеся от текущих), в порядке restore_rank."""
        for w in sorted(self.saved_widgets, key=lambda w: self.restore_rank(w.key)):
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
