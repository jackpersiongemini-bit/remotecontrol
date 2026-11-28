"""Общее окно прибора в стиле PulseGen.py Алексея.

Заголовок: связь (ползунок), индикатор ошибок (клик - журнал), Sync, Init (*RST + восстановление
сохранённого), что это за прибор - VNA, PM... (двойной клик - настройки подключения), развернуть / свернуть.
Высота окна - по содержимому; окно можно растянуть, графики и журнал растут вместе с ним.
Ниже - «избранное» (выходы, пресеты, главные поля) и сворачиваемые вкладки с таблицами
параметров. На месте таблицы могут открываться журнал ошибок и настройки подключения
(выход - клик по вкладке).

Конкретное окно (генератор импульсов, СВЧ генератор...) наследует ``InstrumentWindow`` и
задаёт в ``build()`` виджеты, строки избранного и вкладки. Окно знает только координатор:
прибор (``proxy`` - :class:`visalab.coord.Instrument`, его выдаёт ``COORD.open_instrument``), ключи профиля
прибора и каталог драйверов для страницы подключения. Пока прибор ведёт сценарий координатора (ACAL),
запись из окна в прибор не уходит - отказ виден как ошибка ключа и строка BUSY в журнале окна.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..coord import COORD, Instrument
from ..core.params import ConnectionState, PStatus
from ..core.units import si_format, si_parse
from .favorites import FavoritesManager, FavoritesPopup
from .models import ModelWindow, all_models, model_class
from .params import (IndicatorButton, PBGroup, PChoice, PSpinBox, PSpinEdit, PTabBar, Panel, PanelLine, PanelTable,
                     WriteTracker)
from .theme import OFF, ON, UI_COLUMN_WIDTH_PX, UI_CONTROL_HEIGHT_PX, WINDOW_BAR_HEIGHT
from .window import (AddressLabel, CustomExpandButton, CustomFavButton, CustomInitButton,
                     CustomSyncButton, ErrorIndicator, Switch, Window, WriteState, mark_growing)

__all__ = ["InstrumentWindow", "PresetGroup", "PowerLevels", "PowerSpin", "HScrollArea", "saved_connection", "saved_delay",
           "saved_poll_pause", "parse_power_list",
           "PRESET_HEIGHT", "PRESET_GAP", "DEFAULT_POWER_LIST", "WINDOW_EVENTS", "window_for", "all_instrument_windows"]

PRESET_SETTLE_S = 5.0      # после клика по пресету: столько ждать, пока прибор примет все значения
COMPACT_PADDING_PX = 9     # кнопка у спинбокса: поля по бокам от подписи
PRESET_HEIGHT = 56          # высота чипа пресета; «+» и «−» вместе (с зазором) - такой же высоты
PRESET_GAP = 4
DEFAULT_POWER_LIST = (-20.0, -10.0, -5.0, 0.0)


def _fmt_level(value: float) -> str:
    """Форматирование уровня мощности для чипа и ini без потери дробных знаков и без «-0»."""
    text = f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def parse_power_list(text: str | list | tuple) -> list[float]:
    """``"-20, -10.5 0;5"`` -> [-20.0, -10.5, 0.0, 5.0] (по возрастанию, без повторов). ValueError при мусоре."""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text)
    out = set()
    for t in re.split(r"[\s,;]+", str(text).strip()):
        if t:
            v = float(t)
            if not math.isfinite(v):
                raise ValueError(f"неконечное число: {t!r}")
            out.add(round(v, 4))
    return sorted(out)




def saved_connection(settings: QSettings, group: str) -> dict[str, str]:
    """Сохранённые в окне драйвер / адрес / модель (пустые строки - не задано)."""
    return {k: str(settings.value(f"{group}/{k.upper()}", "") or "") for k in ("driver", "address", "model")}


def saved_delay(settings: QSettings, group: str) -> float | None:
    """Сохранённая на странице адреса пауза между командами, с (None - не задана: как у драйвера)."""
    v = settings.value(f"{group}/DELAY", "")
    try:
        return None if v in (None, "") else max(0.0, float(v))
    except (TypeError, ValueError):
        return None


def saved_poll_pause(settings: QSettings, group: str) -> float | None:
    """Сохранённая на странице настроек пауза между считываниями в непрерывном режиме, с
    (None - не задана: как у драйвера)."""
    v = settings.value(f"{group}/POLL_PAUSE", "")
    try:
        return None if v in (None, "") else max(0.05, float(v))
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-12)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


class _PresetChip(QPushButton):
    """Чип пресета во всю высоту строки PRST (QSS `min-height: 0` у `#chip` не сжимает его)."""

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt
        hint = super().sizeHint()
        return QSize(hint.width(), max(hint.height(), PRESET_HEIGHT))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt
        hint = super().minimumSizeHint()
        return QSize(hint.width(), max(hint.height(), PRESET_HEIGHT))


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
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.settings, self.key = settings, key
        self.normalize, self.describe = normalize, describe
        self.data: list[tuple] = []
        self._recent: list[tuple] = []          # использованные за сеанс, последний - первый
        for preset in self._load(defaults):
            self._append(preset)

    def _make_button(self, name: str) -> QPushButton:
        return _PresetChip(name)

    def _load(self, defaults):
        raw = self.settings.value(self.key) if self.key else None
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
        if not self.key:                        # окно без пресетов (PRESET_KEY = None)
            return
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
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
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

    def _find_exact(self, target: tuple) -> int:
        for i, preset in enumerate(self.data):
            if _same(preset, target):
                return i
        return -1

    def find(self, *values) -> int:
        return self._find_exact(self.normalize(*values))

    def match(self, *values) -> int:
        """Какой пресет совпадает с текущими значениями прибора (для подсветки чипа)."""
        return self.find(*values)

    def add(self, *values) -> bool:
        target = self.normalize(*values)
        i = self._find_exact(target)
        if i < 0:
            self._append(target)
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


class PowerSpin(PSpinBox):
    """Мощность: колесо / стрелки - разряд под курсором, а в режиме пресетов - соседнее значение списка."""

    def __init__(self, proxy: Instrument, key: str, parent=None):
        super().__init__(proxy, key, parent=parent)
        self.preset_mode = False
        self.levels: list[float] = list(DEFAULT_POWER_LIST)

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt
        if not self.preset_mode or not self.levels:
            super().stepBy(steps)
            return
        if steps == 0:
            return
        cur = self._value if isinstance(self._value, (int, float)) else self.value()
        tol = PowerLevels.TOL
        if steps > 0:
            above = [v for v in self.levels if v > cur + tol]
            target = above[min(steps, len(above)) - 1] if above else self.levels[-1]
        else:
            below = [v for v in self.levels if v < cur - tol]
            target = below[-min(-steps, len(below))] if below else self.levels[0]
        try:
            target = self.spec.validate(target)
        except ValueError:
            return
        self._value = target
        self.show_value(target, force=True)
        self.set_value(target)


class PowerLevels(QWidget):
    """Пресеты мощности: чипы уровней по возрастанию, клик ставит мощность ``key``; выбран чип,
    равный текущей мощности. ``add_current()`` - текущая мощность, ``remove_selected()`` - выбранный
    (один уровень остаётся всегда). ``on_change(levels)`` - после изменения списка."""

    COLUMNS = 4
    TOL = 5e-4

    def __init__(self, proxy: Instrument, key: str, levels, on_change: Callable | None = None, parent=None):
        super().__init__(parent)
        self.proxy, self.key, self.on_change = proxy, key, on_change
        self._levels: list[float] = []
        self.chips = PBGroup(columns=self.COLUMNS)
        self.chips.selected.connect(self._clicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chips)
        proxy.subscribe(key, lambda _v: self._show())
        proxy.state_changed.connect(lambda _s: self._show())
        self.set_levels(levels)

    @property
    def levels(self) -> list[float]:
        return list(self._levels)

    def set_levels(self, levels) -> None:
        vals = sorted(round(float(v), 4) for v in levels if math.isfinite(float(v)))
        uniq: list[float] = []
        for v in vals:
            if not uniq or abs(v - uniq[-1]) > self.TOL:
                uniq.append(v)
        self._levels = uniq
        while self.chips.buttons():
            self.chips.remove_button(0)
        for v in self._levels:
            txt = _fmt_level(v)
            b = self.chips.add_button(txt)
            b.setToolTip(f"Set the power to {txt} dBm")
        self._show()

    def _show(self) -> None:
        if sip.isdeleted(self):
            return
        on = self.proxy.is_connected
        cur = self.proxy.value(self.key)
        i = next((k for k, v in enumerate(self._levels)
                  if isinstance(cur, (int, float)) and abs(v - cur) <= self.TOL), -1) if on else -1
        self.chips.set_checked(i)
        for b in self.chips.buttons():
            b.setEnabled(on)

    def _clicked(self, index: int) -> None:
        if self.proxy.is_connected and 0 <= index < len(self._levels):
            self.proxy.set(self.key, self._levels[index])
        self._show()

    def step(self, steps: int) -> None:
        """Соседний уровень (← →): от выбранного, а если текущая мощность не из списка - ближайший."""
        if not self.proxy.is_connected or not self._levels:
            return
        cur = self.proxy.value(self.key)
        if not isinstance(cur, (int, float)):
            target = self._levels[0]
        elif steps > 0:
            above = [v for v in self._levels if v > cur + self.TOL]
            target = above[min(steps, len(above)) - 1] if above else self._levels[-1]
        else:
            below = [v for v in self._levels if v < cur - self.TOL]
            target = below[-min(-steps, len(below))] if below else self._levels[0]
        self.proxy.set(self.key, target)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.levels)

    def add_current(self) -> bool:
        cur = self.proxy.value(self.key)
        if not isinstance(cur, (int, float)) or not math.isfinite(cur) \
                or any(abs(v - cur) <= self.TOL for v in self._levels):
            return False
        self.set_levels([*self._levels, cur])
        self._changed()
        return True

    def remove_selected(self) -> bool:
        i = self.chips.checked_index()
        if i < 0 or len(self._levels) <= 1:
            return False
        self.set_levels([v for k, v in enumerate(self._levels) if k != i])
        self._changed()
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


def compact_button(b: QPushButton) -> QPushButton:
    """Кнопка у спинбокса (PRST, RF ON, CW...): ширина по самой длинной подписи, а не столбец
    (Алексей 2026-11-14: «почему кнопки занимают столько же, сколько спинбокс»)."""
    b.ensurePolished()
    font = QFont(b.font())
    font.setBold(True)                        # выбранная - полужирная: ширина не прыгает
    fm = QFontMetrics(font)
    texts = list(getattr(b, "_names", ())) or [b.text()]
    width = max(fm.horizontalAdvance(t) for t in texts) + 2 * COMPACT_PADDING_PX
    b.setFixedWidth(max(width, UI_CONTROL_HEIGHT_PX + 12))
    return b


class LogView(QPlainTextEdit):
    """Журнал ошибок окна: обычно 10 строк, растёт вместе с окном."""

    def sizeHint(self):  # noqa: N802 - Qt
        return QSize(UI_COLUMN_WIDTH_PX * 4, 10 * self.fontMetrics().lineSpacing() + 12)


class WindowEvents(QObject):
    """Окна приборов процесса: ``changed(прибор)`` - окно создано, показано, скрыто или у него изменились
    ошибки (плитки главного окна: «окно открыто» и «ошибка»)."""

    changed = pyqtSignal(object)


WINDOW_EVENTS = WindowEvents()

_INSTRUMENT_WINDOWS: list["InstrumentWindow"] = []


def _proxy_alive(proxy) -> bool:
    if proxy is None or sip.isdeleted(proxy) or getattr(proxy, "_shut_down", False):
        return False
    inner = getattr(proxy, "_proxy", None)
    if inner is not None and (sip.isdeleted(inner) or getattr(inner, "_shut_down", False)):
        return False
    return True


def all_instrument_windows() -> list["InstrumentWindow"]:
    """Все живые окна приборов в процессе (и плавающие, и прикреплённые к доку)."""
    _INSTRUMENT_WINDOWS[:] = [
        w for w in _INSTRUMENT_WINDOWS
        if not sip.isdeleted(w) and _proxy_alive(w.proxy)
    ]
    return list(_INSTRUMENT_WINDOWS)


def window_for(proxy) -> "InstrumentWindow | None":
    """Окно прибора в этом процессе (может быть скрыто или прикреплено к доку) или None."""
    for w in all_instrument_windows():
        if w.proxy is proxy:
            return w
    for w in QApplication.topLevelWidgets():
        if isinstance(w, InstrumentWindow) and not sip.isdeleted(w) and w.proxy is proxy:
            return w
    return None


class InstrumentWindow(Window):
    """База окна прибора. Наследник задаёт атрибуты класса и ``build()``."""

    #: APPLY в настройках подключения: (driver, address, model) - запускающий код пересоздаёт прокси
    reconnect_requested = pyqtSignal(str, str, str)

    CONNECTION_GROUP = "CONNECTION/INSTRUMENT"
    DEFAULT_KIND = ""
    PRESET_KEY = "PRESETS"
    #: пресеты - значения прибора (SDG): горит тот, что совпадает с прибором (``preset_from_current``)
    PRESET_TRACKS_VALUES = False
    _preset_pending_until = 0.0
    PRESET_DEFAULTS: tuple = ()
    PRESET_ADD_TIP = "Add preset from current values"
    #: подсказка строки пресетов
    PRESET_HELP = ("Presets: click a chip - apply it to the instrument. The chip that matches the instrument "
                   "now is selected. + saves the current values, − removes the selected chip.")

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
    def __init__(self, proxy: Instrument, settings: QSettings, title: str = "Instrument", parent=None):
        super().__init__(settings, parent)
        self.proxy = proxy
        self.setWindowTitle(title)
        self._prefix = re.sub(r"\W+", "_", proxy.address).strip("_").upper() or self.DEFAULT_KIND.upper()
        self._resetting = False
        self._saved: dict[str, Any] = {}        # что уже записано в ini (ключ -> значение)
        self._expanded = False
        self._overlay: str | None = None        # вместо таблицы: "errors" / "connection"
        self._fav_popup: FavoritesPopup | None = None
        self.saved_widgets: list = []
        self.top_lines: list[PanelLine] = []
        self.settings_header_lines: list[PanelLine] = []
        self.settings_header_table: PanelTable | None = None
        self.page_specs: list[tuple[str, tuple[str, ...], PanelTable]] = []
        _INSTRUMENT_WINDOWS.append(self)

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
        # имя драйвера прибора занимает всё место между левыми и правыми кнопками заголовка (не кликабелен)
        self.address = AddressLabel(proxy.name or COORD.display_name(proxy))
        self._update_name()
        self.error_indicator = ErrorIndicator()
        # ход записи виден по цвету рамки самого поля; в заголовке остаётся только индикатор ошибок -
        # рядом с переключателем связи (Алексей)
        for i, w in enumerate((self.on_button, self.error_indicator, self.sync_button, self.init_button)):
            bar.insert_widget(w, i)
        bar.insert_widget(self.address, 4, stretch=1)
        bar.hBoxLayout.setAlignment(self.on_button, Qt.AlignmentFlag.AlignVCenter)   # он ниже кнопок
        bar.title_label.hide()                    # заголовок - в панели задач; в окне место занимает имя драйвера
        self.expand_button = CustomExpandButton()
        self.expand_button.setToolTip("Show / hide all parameters")
        self.expand_button.clicked.connect(lambda: self.set_expanded(not self.expand_button.expanded))
        bar.insert_control_widget(self.expand_button)

        # ---------------------------------------------------------- пресеты и содержимое окна
        self.presets = self.create_presets(f"{self._prefix}/{self.PRESET_KEY}" if self.PRESET_KEY else None)
        self.presets.selected.connect(self._apply_preset)
        self.preset_scroll = HScrollArea(self.presets)
        self.build()

        top = PanelTable(*self.top_lines)
        self.preset_panel = Panel(top)
        top.build()
        for line in self.top_lines:                   # кнопки рядом со спинбоксом - по тексту, не шире него
            if any(isinstance(w, (PSpinBox, PSpinEdit)) for w in line.widgets):
                for w in line.widgets:
                    if isinstance(w, QPushButton):
                        compact_button(w)

        self.tabs = PTabBar()
        self.pages: list[PanelTable] = []
        self._page_states: list[tuple[str, ...]] = []
        for title_, states, table in self.page_specs:
            self.pages.append(table)
            self._page_states.append(tuple(states))
            self.tabs.addTab(title_)
        # отдельная вкладка настроек подключения (драйвер, модель, адрес, пауза)
        self.connection_page = self._build_connection_page()
        self._settings_tab_index = len(self.pages)
        self.pages.append(self.connection_page)
        self._page_states.append(())
        self.tabs.addTab("SET")
        self.tabs.setTabToolTip(self._settings_tab_index, "Connection settings: driver, model, address, command delay")
        self.tabs.currentChanged.connect(self.set_page)
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        if self.settings_header_lines:
            self.settings_header_table = PanelTable(*self.settings_header_lines)
            self.settings_header_table.build()
            bl.addWidget(self.settings_header_table)
        self.fav_button = CustomFavButton()
        self.fav_button.setToolTip("Configure favorites (choose what is shown in the top panel)")
        self.fav_button.clicked.connect(self.show_favorites_editor)
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(4)
        tabs_row.addWidget(self.tabs, 1)
        tabs_row.addWidget(self.fav_button)
        bl.addLayout(tabs_row)
        for page in self.pages:
            bl.addWidget(page)
        self.error_page = self._build_error_page()
        bl.addWidget(self.error_page)
        self.parameter_panel = Panel(body)
        label_w = 0
        for page in self.pages:
            page.build(UI_COLUMN_WIDTH_PX)
            label_w = max([label_w, *(ln.label_hint() for ln in page.first_column_lines())])
        for page in self.pages:
            for ln in page.first_column_lines():
                ln.set_label_width(label_w)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, WINDOW_BAR_HEIGHT + 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.preset_panel)
        lay.addWidget(self.parameter_panel, 1)    # растянули окно - панель растёт (график в ней - вместе с ней)
        self._layout = lay

        # ---------------------------------------------------------- сохранённое состояние виджетов (шаг, размерность, значение)
        self._primary_bound_widgets = list(self.bound_widgets())
        for w in self._primary_bound_widgets:
            if hasattr(w, "attach_settings"):
                w.attach_settings(settings, self._prefix)
            if isinstance(w, (PSpinBox, PSpinEdit)):
                if w.step_setting is None:
                    raw = settings.value(self._step_key(w.key), None)
                    try:
                        if raw not in (None, ""):
                            w.set_step(float(raw), emit=False)
                    except (TypeError, ValueError):
                        pass
                w.step_changed.connect(lambda step, k=w.key: self._save_step(k, step))

        # ---------------------------------------------------------- настраиваемое избранное
        self.favorites = FavoritesManager(self, settings, self._prefix)
        self.favorites.discover()
        self.favorites.attach_to_panel()
        self.favorites.load()

        # ---------------------------------------------------------- состояние
        keys = list(dict.fromkeys(w.key for w in self.bound_widgets()))
        names = {k: k.upper().replace(".", " ") for k in keys}
        names["instrument"] = "INSTRUMENT"
        self.tracker = WriteTracker(proxy, keys, names, self)
        self.tracker.changed.connect(self.error_indicator.set_items)
        self.tracker.changed.connect(lambda *_a: WINDOW_EVENTS.changed.emit(self.proxy))
        self.error_indicator.clicked.connect(self.show_errors)
        COORD.links.changed.connect(self._update_name)       # второй прибор того же класса - GEN2
        proxy.identified.connect(lambda _idn: self._update_name())
        proxy.param_status.connect(self._log_status)
        proxy.instrument_error.connect(self._log_instrument_error)
        proxy.state_changed.connect(self._on_state)
        proxy.sync_done.connect(self._on_sync_done)
        proxy.action_done.connect(self._on_action_done)
        proxy.param_updated.connect(self._on_param)
        proxy.comm_error.connect(self._on_comm_error)
        proxy.reconnect_pending.connect(self._on_reconnect)
        proxy.refused.connect(self._on_refused)
        self._on_state(proxy.state)

        self.setMinimumWidth(max(self.titleBar.minimumSizeHint().width(), lay.minimumSize().width()))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.recall_size(QSize(self.minimumWidth(), 0))
        self.set_page(settings.value(f"{self._prefix}/TAB", 0, type=int), fit=False)
        self.set_expanded(settings.value(f"{self._prefix}/EXPANDED", False, type=bool))
        saved_poll = saved_poll_pause(settings, self.CONNECTION_GROUP)
        if saved_poll is not None:
            proxy.set_read_pause(saved_poll)
        self._update_tabs()
        WINDOW_EVENTS.changed.emit(proxy)

    def cleanup(self) -> None:
        """Сохранить состояние виджетов, отключить глобальные сигналы и закрыть дочерние окна."""
        for w in getattr(self, "_primary_bound_widgets", self.bound_widgets()):
            if not sip.isdeleted(w) and hasattr(w, "save_state"):
                w.save_state()
        self.settings.sync()
        if self in _INSTRUMENT_WINDOWS:
            _INSTRUMENT_WINDOWS.remove(self)
        if getattr(self, "_fav_popup", None) is not None and not sip.isdeleted(self._fav_popup):
            self._fav_popup.close()
            self._fav_popup.deleteLater()
            self._fav_popup = None
        if hasattr(self, "favorites"):
            self.favorites.cleanup()
        mw = getattr(self, "model_window", None)
        if mw is not None and not sip.isdeleted(mw):
            mw.close()
            mw.deleteLater()
            self.model_window = None
        try:
            COORD.links.changed.disconnect(self._update_name)
        except (TypeError, RuntimeError):
            pass

    # ------------------------------------------------------------ избранное и док
    def show_favorites_editor(self) -> FavoritesPopup:
        """Открыть всплывающую панель настройки избранного под кнопкой ★."""
        prev = self._fav_popup
        if prev is not None and not sip.isdeleted(prev):
            if prev.isVisible():
                prev.close()
                return prev
            prev.deleteLater()
        popup = FavoritesPopup(self.favorites, self)
        self._fav_popup = popup
        popup.show_under(self.fav_button)
        return popup

    def set_favorite_enabled(self, item_id: str, enabled: bool) -> None:
        self.favorites.set_enabled(item_id, enabled)

    def reset_favorites(self) -> None:
        self.favorites.reset()

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
        line.set_help(self.PRESET_HELP)
        return line

    def power_levels_line(self, key: str, name: str = "PRST") -> PanelLine:
        """Строка пресетов мощности ``key``: чипы ``self.power_levels`` и «+» / «−» столбиком справа.
        Уровни - в ini (POWER_LIST); после правки вызывается ``levels_changed(levels)``."""
        pl = self.power_levels = PowerLevels(self.proxy, key, self._load_levels(), self._levels_edited)
        tools = self._pwr_tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(PRESET_GAP)
        half = (PRESET_HEIGHT - PRESET_GAP) // 2
        tl.addWidget(self._tool("+", "Add the current power to the presets", pl.add_current, half))
        tl.addWidget(self._tool("−", "Remove the selected power preset", pl.remove_selected, half))
        tl.addStretch(1)
        return PanelLine(name, pl, tools).set_help(
            "Power presets: click a level - set the power. + adds the current power, − removes the selected level.")

    def _load_levels(self) -> list[float]:
        raw = self.settings.value(f"{self._prefix}/POWER_LIST", "")
        try:
            return parse_power_list(raw) or list(DEFAULT_POWER_LIST)
        except ValueError:
            return list(DEFAULT_POWER_LIST)

    def _levels_edited(self, levels: list[float]) -> None:
        self.settings.setValue(f"{self._prefix}/POWER_LIST", ", ".join(_fmt_level(v) for v in levels))
        self.levels_changed(levels)

    def levels_changed(self, levels: list[float]) -> None:
        """Список пресетов мощности изменён: режим PRST спинбокса ходит по новому списку."""
        if getattr(self, "power_spin", None) is not None:
            self.power_spin.levels = list(levels)

    def power_widgets(self, key: str, tip: str) -> tuple[PowerSpin, IndicatorButton]:
        """Мощность ``key`` для избранного: спинбокс ``self.power_spin`` и кнопка ``self.prst_button`` -
        режим пресетов (колесо / стрелки ходят по списку ``power_levels_line``, уровни - POWER_LIST)."""
        spin = self.power_spin = PowerSpin(self.proxy, key)
        spin.levels = self._load_levels()
        b = self.prst_button = IndicatorButton("PRST")
        b.setObjectName("choice")
        b.setCheckable(True)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.toggled.connect(self._set_preset_mode)
        b.setChecked(self.settings.value(f"{self._prefix}/POWER_PRESET_MODE", False, type=bool))
        return spin, b

    def _set_preset_mode(self, on: bool) -> None:
        self.power_spin.preset_mode = bool(on)
        self.settings.setValue(f"{self._prefix}/POWER_PRESET_MODE", bool(on))

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
        WINDOW_EVENTS.changed.emit(self.proxy)

    def hideEvent(self, e):  # noqa: N802 - Qt
        super().hideEvent(e)
        WINDOW_EVENTS.changed.emit(self.proxy)

    def _show_pages(self) -> None:
        if getattr(self, "_hosted_in_sections", False):
            return
        show_body = self._expanded
        overlay = self._overlay if show_body else None
        for i, page in enumerate(self.pages):
            page.setVisible(show_body and overlay is None and i == self.tabs.currentIndex())
        self.error_page.setVisible(overlay == "errors")
        self.tabs.show_selection = show_body and overlay is None
        self.tabs.update()

    def _on_tab_clicked(self, index: int) -> None:
        """Клик по вкладке: из журнала - обратно к таблице; в свёрнутом окне - развернуть;
        повторный клик по уже открытой вкладке - свернуть."""
        if index < 0:
            return
        if self._overlay:
            self._overlay = None
            self.set_page(index, fit=self._expanded)
            if not self._expanded:
                self.set_expanded(True)
            return
        if not self._expanded:
            self.set_page(index, fit=False)
            self.set_expanded(True)
            return
        if index == self.tabs.currentIndex():
            self.set_expanded(False)

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

    def view_key(self) -> str:
        """Вид окна для запомненной высоты: свёрнуто, вкладка, журнал, подключение."""
        if not getattr(self, "_expanded", False):
            return "collapsed"
        return getattr(self, "_overlay", None) or f"tab{self.tabs.currentIndex()}"

    def _grows(self, panel: QWidget) -> bool:
        return any(w.property("grows") and w.isVisibleTo(self) for w in panel.findChildren(QWidget))

    def _fit_height(self, again: bool = True) -> None:
        """Высота окна - по содержимому (вложенные раскладки пересчитываются не сразу). Окно можно
        растянуть: лишняя высота - графику / журналу, а если их нет - панели параметров."""
        if getattr(self, "_hosted_in_sections", False):
            return
        with self.fitting():
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest.value)
            for w in self.findChildren(QWidget):          # вложенные раскладки хранят старые размеры
                if w.layout() is not None:
                    w.layout().invalidate()
            # лишняя высота: панелям с графиком / журналом (в избранном - TRAC ON), обеим - по их высоте
            # (Qt выравнивает размеры по stretch); нет графиков - панели параметров
            top, params = self._grows(self.preset_panel), self._grows(self.parameter_panel)
            self._layout.setStretchFactor(self.preset_panel, self.preset_panel.sizeHint().height() if top else 0)
            self._layout.setStretchFactor(self.parameter_panel,
                                          self.parameter_panel.sizeHint().height() if params else int(not top))
            self._layout.invalidate()
            self._layout.activate()
            self.fit_to(self._layout, top or params)
        if again:
            QTimer.singleShot(0, self._fit_height_again)   # связанный метод: не сработает после удаления окна

    def _fit_height_again(self) -> None:
        if not sip.isdeleted(self):
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
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.presets.data and self.proxy.is_connected:
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
        title.setToolTip("Errors of this window: failed writes, instrument errors, lost connection. "
                         "The red indicator in the title bar stays on until CLEAR.")
        head.addWidget(title)
        head.addStretch()
        self.clear_button = self._tool("CLEAR", "Clear the error log and return to parameters", self.clear_errors)
        self.clear_button.setProperty("wide", True)
        head.addWidget(self.clear_button)
        lay.addLayout(head)
        self.error_log = LogView()
        self.error_log.setObjectName("errorLog")
        self.error_log.setReadOnly(True)
        self.error_log.setPlaceholderText("No errors")
        self.error_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.error_log.setMinimumHeight(6 * self.error_log.fontMetrics().lineSpacing() + 12)
        mark_growing(self.error_log)              # 10 строк; растянули окно - журнал выше
        lay.addWidget(self.error_log)
        page.hide()
        return page

    def _log(self, name: str, detail: str) -> None:
        if sip.isdeleted(self):
            return
        lines = (detail or "").strip().splitlines()
        line = lines[0] if lines else "error"
        self.error_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {name}: {line}")

    def _log_status(self, key: str, status: PStatus, message: str) -> None:
        if key in self.tracker.keys and status in (PStatus.ERROR, PStatus.INVALID):
            self._log(self.tracker.names.get(key, key), message)

    def _log_instrument_error(self, _code: int, message: str) -> None:
        """Ошибка прибора не по ключу окна (EQPHASE, опрос, команда из консоли): в журнал и в индикатор
        ошибок. Ошибку записи ключа («... [ch1.period]») журнал уже показал строкой этого ключа."""
        context = message[message.rfind("[") + 1:-1] if message.endswith("]") and "[" in message else ""
        if context not in self.tracker.keys:
            self._log("INSTRUMENT", message)
            self.tracker.note_error("instrument", message)

    def _step_key(self, key: str) -> str:
        return f"{self._prefix}/STEP/{key.upper()}"

    def _save_step(self, key: str, step) -> None:
        if step:
            self.settings.setValue(self._step_key(key), float(step))
        else:
            self.settings.remove(self._step_key(key))

    def has_errors(self) -> bool:
        """Индикатор ошибок горит (до CLEAR в журнале) - плитка главного окна красная."""
        return self.error_indicator.state is WriteState.ERROR

    def _update_name(self) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self.proxy):
            return
        name = COORD.display_name(self.proxy)
        driver = (self.proxy.name or self._driver_kind().upper() or name).strip()
        idn = (getattr(self.proxy, "idn", "") or "").strip()
        self.address.setText(driver)
        self.setWindowTitle(driver)
        lines = [f"{name}: {driver}"]
        if idn and idn != driver:
            lines.append(idn)
        lines.append(self.proxy.address)
        self.address.setToolTip("\n".join(lines))

    def show_errors(self) -> None:
        """Клик по индикатору ошибок: вместо таблицы - журнал (выход - вкладка или CLEAR)."""
        cb = getattr(self, "_hosted_errors_cb", None)
        if cb is not None:
            cb(True)
            return
        self._show_overlay("errors")

    def clear_errors(self) -> None:
        self.error_log.clear()
        self.tracker.clear_errors()
        cb = getattr(self, "_hosted_errors_cb", None)
        if cb is not None:
            cb(False)
            return
        self._close_overlay()

    # ------------------------------------------------------------ подключение
    def _driver_kind(self) -> str:
        return self.proxy.kind or self.DEFAULT_KIND

    def _build_connection_page(self) -> QWidget:
        kind = self._driver_kind()
        saved = saved_connection(self.settings, self.CONNECTION_GROUP)
        drivers = [(k.upper(), k) for k in COORD.kinds(self.proxy.profile.name)]
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
        self.delay_edit = QLineEdit()
        self.delay_edit.setObjectName("cfg")
        self.delay_edit.setPlaceholderText("driver default")
        self.delay_edit.setToolTip("Pause between commands to the instrument (e.g. 50m = 50 ms); "
                                   "Enter or focus out applies at once, empty = driver default")
        self.delay_edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._show_delay()
        self.delay_edit.returnPressed.connect(self.apply_delay)
        self.delay_edit.editingFinished.connect(self.apply_delay)
        self.poll_edit = QLineEdit()
        self.poll_edit.setObjectName("cfg")
        self.poll_edit.setPlaceholderText("driver default")
        self.poll_edit.setToolTip("Pause between readings in continuous mode (e.g. 500m = 500 ms, 2 = 2 s); "
                                  "triggered and level modes read on instrument completion signal. "
                                  "Enter or focus out applies at once, empty = driver default")
        self.poll_edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._show_poll_pause()
        self.poll_edit.returnPressed.connect(self.apply_poll_pause)
        self.poll_edit.editingFinished.connect(self.apply_poll_pause)
        self.apply_button = self._tool("APPLY", "Save and reconnect with these settings", self.apply_connection)
        self.apply_button.setProperty("wide", True)
        apply_line = PanelLine("", self.apply_button)
        apply_line._name.hide()
        table = PanelTable(
            (PanelLine("DRV", self.driver_choice), PanelLine("MDL", self.model_choice, self.new_model_button)),
            PanelLine("ADDR", self.address_edit),
            (PanelLine("DLY", self.delay_edit), PanelLine("POLL", self.poll_edit)),
            apply_line,
        )
        table.hide()
        return table

    def _fill_models(self, kind, value: str = "") -> None:
        models = [("AUTO", "")] + [(m, m) for m in all_models(self.settings, kind)]
        self.model_choice.set_choices(models, value)
        self.new_model_button.setEnabled(model_class(kind) is not None)
        if hasattr(self, "poll_edit"):
            self._show_poll_pause()

    def new_model(self) -> None:
        """Окно создания модели; после SAVE модель появляется в списке MDL и выбирается."""
        prev = getattr(self, "model_window", None)
        if prev is not None and not sip.isdeleted(prev):
            prev.close()
            prev.deleteLater()
        kind = self.driver_choice.value or self._driver_kind()
        template = all_models(self.settings, kind).get(self.model_choice.value or "")
        self.model_window = ModelWindow(self.settings, kind, template)
        self.model_window.saved.connect(lambda name, k=kind: (not sip.isdeleted(self)) and self._fill_models(k, name))
        if not self.model_window._pos_restored:
            self.model_window.move(self.pos() + self.rect().center() - self.model_window.rect().center())
        self.model_window.show()

    def show_connection(self) -> None:
        """Открыть вкладку настроек подключения (SET)."""
        self._overlay = None
        if self._expanded:
            self.set_page(self._settings_tab_index, fit=True)
        else:
            self.set_page(self._settings_tab_index, fit=False)
            self.set_expanded(True)

    def _show_delay(self) -> None:
        saved = saved_delay(self.settings, self.CONNECTION_GROUP)
        self.delay_edit.setText("" if saved is None else si_format(saved, "s", 4))
        # пустое поле - пауза драйвера: показать какая (у SDG 150 мс), чтобы старое значение в ini было видно
        try:
            default = COORD.default_delay(self._driver_kind())
        except KeyError:                  # драйвер не из каталога - подсказка без числа
            default = 0.0
        self.delay_edit.setPlaceholderText(f"default {si_format(default, 's', 4)}" if default else "driver default")

    def apply_delay(self) -> bool:
        """DLY: сохранить в ini и сразу применить к прибору. False - не число (поле возвращается)."""
        text = self.delay_edit.text().strip()
        if not text:
            self.settings.remove(f"{self.CONNECTION_GROUP}/DELAY")
            try:
                default = COORD.default_delay(self._driver_kind())
            except KeyError:
                default = 0.0
            self.proxy.set_command_delay(default)
            return True
        try:
            value = si_parse(text, "s")
            if not math.isfinite(value) or value < 0 or value > 60.0:
                raise ValueError("out of range")
        except ValueError:
            self._show_delay()
            return False
        self.settings.setValue(f"{self.CONNECTION_GROUP}/DELAY", value)
        self.proxy.set_command_delay(value)
        self._show_delay()
        return True

    def _show_poll_pause(self) -> None:
        saved = saved_poll_pause(self.settings, self.CONNECTION_GROUP)
        self.poll_edit.setText("" if saved is None else si_format(saved, "s", 4))
        try:
            default = COORD.default_poll_pause(self._driver_kind())
        except KeyError:
            default = 0.5
        self.poll_edit.setPlaceholderText(f"default {si_format(default, 's', 4)}" if default else "driver default")

    def apply_poll_pause(self) -> bool:
        """POLL: пауза между считываниями в непрерывном режиме; сохранить в ini и сразу применить к прибору."""
        text = self.poll_edit.text().strip()
        if not text:
            self.settings.remove(f"{self.CONNECTION_GROUP}/POLL_PAUSE")
            try:
                default = COORD.default_poll_pause(self._driver_kind())
            except KeyError:
                default = 0.5
            self.proxy.set_read_pause(default)
            return True
        try:
            value = si_parse(text, "s")
            if not math.isfinite(value) or value < 0.05 or value > 3600.0:
                raise ValueError("out of range")
        except ValueError:
            self._show_poll_pause()
            return False
        self.settings.setValue(f"{self.CONNECTION_GROUP}/POLL_PAUSE", value)
        self.proxy.set_read_pause(value)
        self._show_poll_pause()
        return True

    def apply_connection(self) -> None:
        self.apply_delay()
        self.apply_poll_pause()
        driver = self.driver_choice.value or self._driver_kind()
        address = self.address_edit.text().strip() or "sim"
        model = self.model_choice.value or ""
        for k, v in (("DRIVER", driver), ("ADDRESS", address), ("MODEL", model)):
            self.settings.setValue(f"{self.CONNECTION_GROUP}/{k}", v)
        self.settings.sync()
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
        if self.PRESET_TRACKS_VALUES:         # пока значения пишутся - чип горит; потом - по прибору
            self._preset_pending_until = time.monotonic() + PRESET_SETTLE_S
            QTimer.singleShot(int(PRESET_SETTLE_S * 1000) + 50, self._sync_preset)
        self.apply_preset_values(preset)

    def _sync_preset(self) -> None:
        """Горит пресет, совпадающий со значениями прибора; поменяли ширину руками - не горит ни один
        (Алексей 2026-11-14: «пресет горит, а я уже поменял ширину»)."""
        if sip.isdeleted(self) or not self.PRESET_TRACKS_VALUES or not self.presets.data:
            return
        cur = self.preset_from_current()
        i = self.presets.match(*cur) if cur is not None else -1
        if i < 0 and time.monotonic() < self._preset_pending_until:
            return                            # пресет ещё пишется: значения приходят по одному
        if i != self.presets.checked_index():
            self.presets.set_checked(i)

    # ------------------------------------------------------------ сохранение
    def _settings_key(self, key: str) -> str:
        return f"{self._prefix}/{key.upper()}"

    def _on_param(self, key: str, value: Any) -> None:
        if any(key in states for states in self._page_states):
            self._update_tabs()
        self.on_param(key, value)
        if self.PRESET_TRACKS_VALUES:
            self._sync_preset()
        if self._resetting:              # после *RST прибор отдаёт заводские значения - не сохранять
            return
        # то же значение не писать: ini переписывается целиком при каждом setValue, а приборы публикуют
        # некоторые ключи постоянно (ВАЦ - ось трасс после каждой развёртки)
        if value is not None and any(w.key == key for w in self.saved_widgets) and self._saved.get(key) != value:
            self._saved[key] = value
            self.settings.setValue(self._settings_key(key), value)

    def on_param(self, key: str, value: Any) -> None:
        """Для наследника: реакция на новое значение параметра."""

    def restore(self) -> None:
        """Отправить сохранённые значения в порядке restore_rank (неизменённые пропустит драйвер)."""
        for w in sorted(self.saved_widgets, key=lambda w: self.restore_rank(w.key)):
            raw = self.settings.value(self._settings_key(w.key))
            if raw is None:
                continue
            try:
                value = w.spec.validate(str(raw).replace(",", ".") if isinstance(raw, str) else raw)
            except (TypeError, ValueError, OverflowError):
                continue
            w.set_value(value)

    # ------------------------------------------------------------ кнопки
    def _toggle_connection(self) -> None:
        if self.proxy.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING) or self.proxy.reconnecting:
            self.proxy.close()          # в том числе - перестать переподключаться
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
        if sip.isdeleted(self):
            return
        status = {ConnectionState.CONNECTED: ON, ConnectionState.CONNECTING: "process",
                  ConnectionState.ERROR: "retry" if self.proxy.reconnecting else "error"}.get(state, OFF)
        self.on_button.set_status(status)
        self.on_button.setToolTip("No connection: reconnecting by itself when the instrument answers. "
                                  "Click - stop" if status == "retry" else "Connect / disconnect")
        on = state is ConnectionState.CONNECTED
        if not on:
            self._resetting = False
            self.sync_button.set_status("ok")
            self.init_button.set_status("ok")
        self.sync_button.setEnabled(on)
        self.init_button.setEnabled(on)
        if on:
            self._update_name()

    def _on_reconnect(self, _seconds: float) -> None:
        if not sip.isdeleted(self):
            self._on_state(self.proxy.state)

    def _on_comm_error(self, msg: str) -> None:
        if sip.isdeleted(self):
            return
        self._log("CONNECTION", msg)
        self._release_buttons()

    def _on_refused(self, msg: str) -> None:
        """Прибор ведёт сценарий координатора: запрос окна не отправлен."""
        if sip.isdeleted(self):
            return
        self._log("BUSY", msg)
        self._release_buttons()

    def _release_buttons(self) -> None:
        self._resetting = False
        for b in (self.sync_button, self.init_button):
            if b.status() == "process":
                b.set_status("ok")
                b.setEnabled(self.proxy.is_connected)

    def _on_sync_done(self) -> None:
        if not sip.isdeleted(self):
            self.sync_button.set_status("ok")
            self.sync_button.setEnabled(self.proxy.is_connected)

    def _on_action_done(self, name: str, _result) -> None:
        if sip.isdeleted(self):
            return
        if name == "reset":
            self.init_button.set_status("ok")
            self.init_button.setEnabled(self.proxy.is_connected)
            self._resetting = False
            self.restore()

    def closeEvent(self, event):  # noqa: N802 - Qt
        if self._fav_popup is not None and not sip.isdeleted(self._fav_popup):
            self._fav_popup.close()
        mw = getattr(self, "model_window", None)
        if mw is not None and not sip.isdeleted(mw):
            mw.close()
        self.presets.save()
        self.settings.sync()
        super().closeEvent(event)
