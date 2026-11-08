"""Окно анализатора спектра (E4446A и любой прибор профиля spectrum_analyzer): только настройки.

Трасса и вся измерительная информация выводятся отдельным виджетом (его ещё нет), здесь -
избранное и настройки. В избранном одни пресеты, и это состояния в памяти САМОГО анализатора:
``+`` сохраняет текущее состояние (имя - из центра и обзора, например 2p4GHz_100MHz), чип
вызывает его, ``-`` удаляет из прибора. Список читается из прибора при каждой синхронизации,
поэтому пресеты, сохранённые на приборе раньше или с другого компьютера, появляются сами.
В ini лежит только порядок чипов (последние использованные - первыми).

Вкладки: FREQ (центр, обзор, старт, стоп), BW (RBW, VBW), AMPL (опорный уровень, масштаб,
аттенюатор, детектор), SWP (время, точки, усреднение, опора, непрерывно / одна развёртка).
Окно знает только InstrumentProxy и ключи профиля spectrum_analyzer.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings

from ..core.proxy import InstrumentProxy
from ..core.scpi import si_format
from . import instrument_window as _iw
from .instrument_window import InstrumentWindow, PresetGroup
from .params import PLineEdit, PSButton, PanelLine, PanelTable
from .power_meter import ActionButton

__all__ = ["SpectrumWindow", "StatePresets", "saved_connection"]

CONNECTION_GROUP = "CONNECTION/SPECTRUM"
AUTO = [("MAN", False), ("AUTO", True)]
ONOFF = [("OFF", False), ("ON", True)]

#: вкладки: (название, строки); строка - (подпись, ключ значения, ключ «авто» или None)
PAGES = (
    ("FREQ", (("CENT", "freq.center", None), ("SPAN", "freq.span", None),
              ("STAR", "freq.start", None), ("STOP", "freq.stop", None))),
    ("BW", (("RBW", "bw.res", "bw.res_auto"), ("VBW", "bw.video", "bw.video_auto"))),
    ("AMPL", (("RLEV", "ref_level", None), ("SDIV", "scale_div", None), ("ATT", "atten", "atten_auto"),
              ("DET", "detector", None))),
    ("SWP", (("TIME", "sweep.time", "sweep.time_auto"), ("PTS", "sweep.points", None),
             ("AVG", "avg.state", "avg.count"), ("REF", "ref.source", None))),
)
NOT_SAVED = ("freq.start", "freq.stop")          # связаны с центром и обзором - после Init не восстанавливаем


def saved_connection(settings: QSettings) -> dict[str, str]:
    return _iw.saved_connection(settings, CONNECTION_GROUP)


def _preset(name) -> tuple[str]:
    return (str(name),)


def _describe(preset: tuple) -> tuple[str, str]:
    return preset[0], f"{preset[0]}\nstate in the analyzer memory"


class StatePresets(PresetGroup):
    """Пресеты - имена состояний в памяти прибора. Удаление идёт в прибор (``on_remove``),
    а из списка пропадает, когда прибор перечислит файлы заново."""

    def __init__(self, settings, key, on_remove, parent=None):
        super().__init__(settings, key, (), _preset, _describe, parent)
        self.on_remove = on_remove

    def names(self) -> list[str]:
        return [p[0] for p in self.data]

    def selected_name(self) -> str | None:
        i = self.checked_index()
        return self.data[i][0] if 0 <= i < len(self.data) else None

    def remove_selected(self) -> bool:
        name = self.selected_name()
        if name is None:
            return False
        self.on_remove(name)
        return True

    def set_names(self, names: list[str], select: str | None = None) -> None:
        """Список из прибора: порядок - как в ini (последние использованные первыми), новые - по алфавиту."""
        keep = select or self.selected_name()
        known = self.names()
        order = [n for n in known if n in names] + sorted(n for n in names if n not in known)
        if order != known:
            while self.data:
                self.remove_button(0)
                del self.data[0]
            for n in order:
                self._append((n,))
        self.set_checked(order.index(keep) if keep in order else -1)
        self.save()


class SpectrumWindow(InstrumentWindow):
    CONNECTION_GROUP = CONNECTION_GROUP
    DEFAULT_KIND = "e4446a"
    PRESET_KEY = "SA_PRESETS"
    PRESET_DEFAULTS = ()
    PRESET_ADD_TIP = "Save the current state in the analyzer memory"

    def __init__(self, proxy: InstrumentProxy, settings: QSettings, title: str = "Spectrum analyzer", parent=None):
        self._select_after_list: str | None = None
        super().__init__(proxy, settings, title, parent)
        proxy.action_done.connect(self._on_action)
        proxy.sync_done.connect(self.refresh_states)
        if proxy.is_connected:
            self.refresh_states()

    # ------------------------------------------------------------ сборка
    def build(self) -> None:
        proxy, sup = self.proxy, self.proxy.supports
        self.top_lines = [self.preset_line()]
        self.widgets: dict[str, Any] = {}

        def make(key):
            spec = proxy.params[key]
            from ..core.params import Kind
            if spec.kind is Kind.BOOL:
                return PSButton(proxy, key, ONOFF)
            if spec.kind is Kind.ENUM:
                return PSButton(proxy, key, [(c, c) for c in spec.choices])
            return PLineEdit(proxy, key)

        for title_, rows in PAGES:
            lines = []
            for name, key, extra in rows:
                if not sup(key):
                    continue
                w = make(key)
                ws = [w]
                if extra and sup(extra):
                    ws.append(PSButton(proxy, extra, AUTO) if proxy.params[extra].kind.name == "BOOL"
                              else make(extra))
                for x, k in zip(ws, (key, extra)):
                    self.widgets[k] = x
                    if k not in NOT_SAVED:
                        self.saved_widgets.append(x)
                lines.append(PanelLine(name, *ws))
            if title_ == "SWP":
                if sup("meas.continuous"):
                    cont = PSButton(proxy, "meas.continuous", [("CONT", True), ("SINGLE", False)])
                    self.widgets["meas.continuous"] = cont
                    extra_ws = [cont]
                    if "single" in proxy.actions:
                        extra_ws.append(ActionButton(proxy, "single", "SWEEP", "Single sweep"))
                    lines.append(PanelLine("MODE", *extra_ws))
            if lines:
                self.page_specs.append((title_, (), PanelTable(*lines)))

    def restore_rank(self, key: str) -> int:
        return 1 if key.endswith("_auto") else 0

    # ------------------------------------------------------------ пресеты = состояния в приборе
    def create_presets(self, key: str) -> PresetGroup:
        return StatePresets(self.settings, key, self._delete_state)

    def refresh_states(self) -> None:
        if "state_list" in self.proxy.actions:
            self.proxy.action("state_list")

    def state_name(self) -> str | None:
        c, s = self.proxy.value("freq.center"), self.proxy.value("freq.span")
        if c is None or s is None:
            return None
        text = lambda v: si_format(v, "Hz", 4).replace(" ", "").replace(".", "p")
        base, name, n = f"{text(c)}_{text(s)}", None, 1
        while name is None or name in self.presets.names():
            name = base if n == 1 else f"{base}-{n}"
            n += 1
        return name

    def _add_preset(self) -> None:
        name = self.state_name()
        if name is not None:
            self._select_after_list = name
            self.proxy.action("state_save", name)
            self.refresh_states()

    def _delete_state(self, name: str) -> None:
        self.proxy.action("state_delete", name)
        self.refresh_states()

    def apply_preset_values(self, preset: tuple) -> None:
        self.proxy.action("state_recall", preset[0])

    def _on_action(self, name: str, result) -> None:
        if name == "state_list" and isinstance(result, (list, tuple)):
            self.presets.set_names([str(n) for n in result], self._select_after_list)
            self._select_after_list = None
            if self.presets.data:
                i = self.presets.checked_index()
                if i >= 0:
                    self.preset_scroll.ensureWidgetVisible(self.presets.buttons()[i], 0, 0)
