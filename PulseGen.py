# Запускать из корневой папки проекта:
# pyinstaller --onedir --noconsole --paths . --name PGRemote --add-data "icons;icons" Instruments\PulseGen.py

import json
import math
import re
import sys
import time
from pathlib import Path

# При прямом запуске файла Python добавляет в sys.path только Instruments.
# Добавляем корень проекта, чтобы локальные модули импортировались так же,
# как при запуске через ``python -m Instruments.PulseGen``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication

from Shared import *
from InstrumentWidgets import Instrument
from Parameter import (
    PBGroup,
    PLabel,
    PNLabel,
    PLineEdit,
    PSButton,
    Panel,
    PanelLine,
    PanelTable,
    VBoxLayout,
)
from Settings import (
    REC_PG_CH1_DLY,
    REC_PG_CH1_HLEV,
    REC_PG_CH1_LLEV,
    REC_PG_CH1_LOAD,
    REC_PG_CH1_OUTP,
    REC_PG_CH1_PERI,
    REC_PG_CH1_WIDTH,
    REC_PG_CH1_WVTP,
    REC_PG_CH2_DLY,
    REC_PG_CH2_HLEV,
    REC_PG_CH2_LLEV,
    REC_PG_CH2_LOAD,
    REC_PG_CH2_OUTP,
    REC_PG_CH2_PERI,
    REC_PG_CH2_WIDTH,
    REC_PG_CH2_WVTP,
    settings,
)
from Signals import signals
from VISAClass import VISAClass, pv
from WidgetFabric import Button, QSize


CHANNELS = ("C1", "C2")
PULSE_PRESETS = ((400e-3, 30e-6),)
# SDG теряет команды при слишком быстрой последовательной отправке.
COMMAND_DELAY_SECONDS = 0.15
# Запись подтверждается чтением и при необходимости повторяется.
WRITE_ATTEMPTS = 3

BSWV = "BSWV"
OUTP = "OUTP"
EQPHASE = "EQPHASE"

WVTP = "WVTP"
PERI = "PERI"
WIDTH = "WIDTH"
HLEV = "HLEV"
LLEV = "LLEV"
DUTY = "DUTY"
DLY = "DLY"
LOAD = "LOAD"

BSWV_FIELDS = (WVTP, PERI, WIDTH, HLEV, LLEV, DUTY, DLY)
PHASE_AFFECTING_FIELDS = (WVTP, PERI, WIDTH, DUTY, DLY)
NUMBER_PATTERN = re.compile(
    r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)"
)

# Старые настройки используются как значения первого запуска. После первого
# сохранения Parameter будет работать уже со своими адресными ключами.
CHANNEL_DEFAULTS = (
    {
        "output": REC_PG_CH1_OUTP,
        "wave_type": REC_PG_CH1_WVTP,
        "period": float(REC_PG_CH1_PERI),
        "width": float(REC_PG_CH1_WIDTH),
        "high_level": float(REC_PG_CH1_HLEV),
        "low_level": float(REC_PG_CH1_LLEV),
        "delay": float(REC_PG_CH1_DLY),
        "load": REC_PG_CH1_LOAD,
    },
    {
        "output": REC_PG_CH2_OUTP,
        "wave_type": REC_PG_CH2_WVTP,
        "period": float(REC_PG_CH2_PERI),
        "width": float(REC_PG_CH2_WIDTH),
        "high_level": float(REC_PG_CH2_HLEV),
        "low_level": float(REC_PG_CH2_LLEV),
        "delay": float(REC_PG_CH2_DLY),
        "load": REC_PG_CH2_LOAD,
    },
)


def scpi_route(channel, subsystem, field=""):
    """Создаёт логический маршрут Parameter, например C1:BSWV PERI."""
    command = f"{channel}:{subsystem}"
    return f"{command} {field}" if field else command


def format_time(value):
    value = float(value)
    absolute_value = abs(value)
    for factor, unit in (
        (1.0, "s"),
        (1e-3, "ms"),
        (1e-6, "µs"),
        (1e-9, "ns"),
    ):
        if absolute_value >= factor:
            return f"{value / factor:.6g} {unit}"
    return f"{value * 1e12:.6g} ps"


class PulsePresetGroup(PBGroup):
    """Группа preset-кнопок, хранящих пары period/width."""

    preset_selected = pyqtSignal(float, float)
    add_requested = pyqtSignal()

    def __init__(self, settings_key, presets=PULSE_PRESETS):
        self._preset_settings_key = settings_key
        presets = self._load_presets(presets)
        is_empty = not presets
        states = {
            self._button_text(period, width): (float(period), float(width))
            for period, width in (presets or ((0.0, 0.0),))
        }
        super().__init__(states)

        # PBGroup требует хотя бы одно состояние при создании. Удаляем
        # временную кнопку, если пользователь ранее удалил все presets.
        if is_empty:
            self._remove_preset_button(0)

        self.add_button = Button("+")
        self.add_button.setFixedSize(QSize(30, 30))
        self.delete_button = Button("-")
        self.delete_button.setFixedSize(QSize(30, 30))
        add_delete_layout = VBoxLayout(margins=(0,0,0,0), spacing=8, alignments=af.AlignRight)
        add_delete_layout.addWidget(self.add_button)
        add_delete_layout.addWidget(self.delete_button)

        self.group_layout.setSpacing(5)
        self.group_layout.addStretch()
        self.group_layout.addLayout(add_delete_layout, stretch=0)

        self.add_button.clicked.connect(self.add_requested)
        self.delete_button.clicked.connect(self.remove_selected)

    @staticmethod
    def _button_text(period, width):
        return (
            f"Period: {format_time(period)}\n"
            f"Width: {format_time(width)}"
        )

    def _load_presets(self, defaults):
        raw_value = settings.value(self._preset_settings_key)
        if raw_value is None:
            return tuple(defaults)
        try:
            return tuple(
                (float(period), float(width))
                for period, width in json.loads(str(raw_value))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            signals.error_occurrence.emit(
                f"Preset recall error: {self._preset_settings_key}"
            )
            return tuple(defaults)

    def save_presets(self):
        settings.setValue(
            self._preset_settings_key,
            json.dumps(self._data, separators=(",", ":")),
        )

    def add_preset(self, period, width):
        preset = (float(period), float(width))
        for index, saved_preset in enumerate(self._data):
            if all(
                math.isclose(current, saved, rel_tol=1e-9, abs_tol=1e-15)
                for current, saved in zip(preset, saved_preset)
            ):
                self._group.button(index).setChecked(True)
                self._value = saved_preset
                self._written()
                return False

        index = len(self._data)
        name = self._button_text(*preset)
        button = Button(name)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._group.addButton(button, index)
        self.group_layout.insertWidget(
            self.group_layout.indexOf(self.add_button),
            button,
        )
        self._names += (name,)
        self._data += (preset,)
        button.setChecked(True)
        self._value = preset
        self._written()
        self.save_presets()
        return True

    def _remove_preset_button(self, index):
        button = self._group.button(index)
        if button is None:
            return False

        # После удаления сдвигаем ID, чтобы они снова совпадали с _data.
        for old_index in range(index + 1, len(self._data)):
            shifted_button = self._group.button(old_index)
            self._group.setId(shifted_button, old_index - 1)

        self._group.removeButton(button)
        self.group_layout.removeWidget(button)
        button.deleteLater()
        self._names = self._names[:index] + self._names[index + 1:]
        self._data = self._data[:index] + self._data[index + 1:]
        return True

    def remove_selected(self):
        index = self._group.checkedId()
        if index < 0 or not self._remove_preset_button(index):
            return False
        self._unknown()
        self.save_presets()
        return True

    def _change_value(self, index):
        period, width = self._data[index]
        self._value = (period, width)
        self._written()
        self.preset_selected.emit(period, width)


class Widget(Instrument):
    """Двухканальный интерфейс генератора на виджетах Parameter."""

    def __init__(self, address=PG_ADDRESS, **kwargs):
        super().__init__(address, **kwargs)
        self.setWindowTitle("Pulse generator")

        self._parameters = []
        self._saved_parameters = []
        self._parameter_defaults = {}
        self.channels = tuple(
            self._make_channel(channel, index)
            for index, channel in enumerate(CHANNELS)
        )

        self.presets = PulsePresetGroup(
            f"{self.address}PULSE_PRESETS".upper()
        )
        self.presets.preset_selected.connect(self._apply_preset)
        self.presets.add_requested.connect(self._add_preset)

        preset_table = PanelTable()
        preset_line = PanelLine("Presets")
        preset_line.add_widget(self.presets)
        preset_table.add_line(preset_line)
        self.preset_panel = Panel(preset_table)
        self.main_layout.addWidget(self.preset_panel)

        table = PanelTable()
        table.add_line(self._make_header_line())

        # Имя элемента в строке соответствует ключу в словаре каждого канала.
        rows = (
            ("Output", "output"),
            ("Type", "wave_type"),
            ("Duty", "duty"),
            ("Period", "period"),
            ("Width", "width"),
            ("High level", "high_level"),
            ("Low level", "low_level"),
            ("Delay", "delay"),
            ("Load", "load"),
        )
        for title, name in rows:
            line = PanelLine(title)
            line.add_widget(*(channel[name] for channel in self.channels))
            table.add_line(line)

        self.parameter_panel = Panel(table)
        self.parameter_panel.rewdith_wdgts()
        self.main_layout.addWidget(self.parameter_panel)

        self._pulse_generator = VISAObject(self.address)
        self.titleBar.raise_()

    def _add_preset(self):
        # Preset общий для каналов, поэтому сохраняем текущую пару CH1.
        period = self.channels[0]["period"].value
        width = self.channels[0]["width"].value
        if period is None or width is None:
            signals.error_occurrence.emit(
                "Preset add error: Period or Width is empty"
            )
            return
        self.presets.add_preset(period, width)

    def _apply_preset(self, period, width):
        # Один preset задаёт одинаковые временные параметры обоим каналам.
        # VISAObject последовательно выполнит команды и завершит их EQPHASE.
        for channel in self.channels:
            channel["period"].set_value(period)
            channel["period"].change_value()
            channel["width"].set_value(width)
            channel["width"].change_value()

    def _make_header_line(self):
        line = PanelLine("")
        headings = []
        for number in range(1, len(CHANNELS) + 1):
            heading = PLabel()
            heading.set_value(f"Channel {number}")
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            headings.append(heading)
        line.add_widget(*headings)
        return line

    def _make_channel(self, channel, channel_index):
        # DUTY только отображается: для PULSE прибор вычисляет его сам из
        # периода и ширины. Остальные элементы можно записывать в прибор.
        controls = {
            "output": PSButton({"Off": "OFF", "On": "ON"}),
            "wave_type": PSButton({"Pulse": "PULSE"}),
            "duty": PNLabel("%"),
            "period": PLineEdit("s"),
            "width": PLineEdit("s"),
            "high_level": PLineEdit("V"),
            "low_level": PLineEdit("V"),
            "delay": PLineEdit("s"),
            "load": PSButton({"50 Ohm": "50", "High Z": "HZ"}),
        }

        # BSWV и OUTP возвращают сразу несколько полей. Поле в маршруте нужно,
        # чтобы транспорт вернул каждому Parameter только его значение.
        routes = {
            "output": scpi_route(channel, OUTP),
            "wave_type": scpi_route(channel, BSWV, WVTP),
            "duty": scpi_route(channel, BSWV, DUTY),
            "period": scpi_route(channel, BSWV, PERI),
            "width": scpi_route(channel, BSWV, WIDTH),
            "high_level": scpi_route(channel, BSWV, HLEV),
            "low_level": scpi_route(channel, BSWV, LLEV),
            "delay": scpi_route(channel, BSWV, DLY),
            "load": scpi_route(channel, OUTP, LOAD),
        }

        for name, control in controls.items():
            control.signal_connect(self.address, routes[name])
            self._parameters.append(control)
            if name != "duty":
                self._saved_parameters.append(control)
                settings_key = f"{self.address}{routes[name]}".upper()
                self._parameter_defaults[control] = (
                    settings_key,
                    CHANNEL_DEFAULTS[channel_index][name],
                )

        for name in ("period", "width", "high_level", "low_level", "delay"):
            control = controls[name]
            control.editingFinished.connect(control.change_value)

        return controls

    def _on_finish_initial(self, address):
        super()._on_finish_initial(address)
        if address != self.address:
            return
        # После *RST восстанавливаем сохранённое значение. Если новый адресный
        # ключ ещё не создан, отправляем совместимое значение старой настройки.
        for parameter in self._saved_parameters:
            settings_key, default = self._parameter_defaults[parameter]
            value = settings.value(settings_key, default)

            # QSettings может вернуть сохранённое число как str, тогда как
            # PLineEdit форматирует только числовые значения.
            if isinstance(parameter, PLineEdit):
                try:
                    value = float(str(value).replace(",", "."))
                except (TypeError, ValueError):
                    signals.error_occurrence.emit(
                        f"Recall error: {settings_key}={value}"
                    )
                    continue

            parameter.set_value(value)
            parameter.change_value()

    def closeEvent(self, event):
        self.presets.save_presets()
        # Дочерние QWidget не получают closeEvent при закрытии родителя,
        # поэтому параметры сохраняются централизованно здесь.
        for parameter in self._saved_parameters:
            parameter.save()
        for parameter in self._parameters:
            parameter.signal_disconnect()

        visa_thread = self._pulse_generator._thread
        self._pulse_generator.stop_signal.emit()
        visa_thread.wait()
        return super().closeEvent(event)


class VISAObject(VISAClass):
    """Последовательный SDG-транспорт с паузой и проверкой записи."""

    command_delay = COMMAND_DELAY_SECONDS
    write_attempts = WRITE_ATTEMPTS

    def __init__(self, address, **kwargs):
        self._last_command_time = 0.0
        super().__init__(address, **kwargs)

    def _configure_signals(self):
        # VISAClass только формирует список сигналов. Подключаем его здесь,
        # иначе write/query/init/sync не попадут в рабочий VISA-поток.
        for signal, handler in self._signals:
            signal.connect(handler, Qt.ConnectionType.QueuedConnection)

    def _wait_command_delay(self):
        # Пауза отсчитывается от фактического завершения предыдущего I/O.
        elapsed = time.monotonic() - self._last_command_time
        remaining = self.command_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _device_write(self, command):
        self._wait_command_delay()
        try:
            self.me.write(command)
        finally:
            self._last_command_time = time.monotonic()

    def _device_query(self, command):
        self._wait_command_delay()
        try:
            return str(self.me.query(command))
        finally:
            self._last_command_time = time.monotonic()

    def write(self, scpi_command):
        with self.lock:
            if not self.status or self.me is None:
                return False
            try:
                self._device_write(scpi_command.strip())
                return True
            except pv.Error as error:
                signals.error_occurrence.emit(
                    f"Write {scpi_command} Error: {error}"
                )
                return False

    def query(self, scpi_command):
        command = scpi_command.strip().removesuffix("?")
        with self.lock:
            if not self.status or self.me is None:
                return None
            try:
                response = self._device_query(f"{command}?")
            except pv.Error as error:
                signals.error_occurrence.emit(
                    f"Query {command} Error: {error}"
                )
                return None
        # SDG возвращает заголовок вместе с данными: C1:BSWV <answer>.
        return self._strip_command_echo(command, response)

    @staticmethod
    def _header_patterns(command):
        # CHDR может быть настроен как на короткие, так и на длинные имена.
        headers = [command]
        upper_command = command.upper()
        if upper_command.endswith(":BSWV"):
            headers.append(command[:-4] + "BASIC_WAVE")
        elif upper_command.endswith(":OUTP"):
            headers.append(command[:-4] + "OUTPUT")
        return headers

    @classmethod
    def _strip_command_echo(cls, command, response):
        text = str(response).strip()
        for header in cls._header_patterns(command):
            pattern = r"^\s*" + r"\s*:\s*".join(
                re.escape(part) for part in header.split(":")
            )
            match = re.match(pattern + r"\??\s*", text, re.IGNORECASE)
            if match:
                return text[match.end():].lstrip(" ,")
        return text

    @staticmethod
    def _clean_value(value):
        # Убираем единицу измерения: 250HZ -> 250, 0.004S -> 0.004.
        value = str(value).strip()
        match = NUMBER_PATTERN.match(value)
        return match.group(1) if match else value

    @classmethod
    def _parse_snapshot(cls, base_command, payload):
        # BSWV состоит из пар FIELD,VALUE. У OUTP первый элемент — состояние,
        # после которого также идут пары LOAD,VALUE и PLRT,VALUE.
        tokens = [token.strip() for token in str(payload).split(",")]
        snapshot = {}

        if base_command.upper().endswith(f":{OUTP}"):
            if tokens and tokens[0]:
                snapshot[""] = cls._clean_value(tokens[0])
            start = 1
        elif base_command.upper().endswith(f":{BSWV}"):
            start = 0
        else:
            return snapshot

        for index in range(start, len(tokens) - 1, 2):
            field = tokens[index].upper()
            snapshot[field] = cls._clean_value(tokens[index + 1])
        return snapshot

    @staticmethod
    def _split_route(command):
        base_command, separator, field = command.strip().partition(" ")
        return base_command.upper(), field.strip().upper() if separator else ""

    @staticmethod
    def _write_command(base_command, field, value):
        if field:
            return f"{base_command} {field}, {value}"
        return f"{base_command} {value}"

    @staticmethod
    def _values_equal(expected, actual):
        # Числа сравниваются с допуском из-за форматирования прибора, состояния
        # и другие текстовые значения — без учёта регистра.
        try:
            return math.isclose(
                float(expected),
                float(actual),
                rel_tol=1e-6,
                abs_tol=1e-12,
            )
        except (TypeError, ValueError):
            return str(expected).strip().upper() == str(actual).strip().upper()

    def _emit_snapshot(self, base_command, snapshot):
        # Превращаем один составной ответ прибора в отдельные ответы Parameter.
        for field, value in snapshot.items():
            if field and field not in BSWV_FIELDS and field != LOAD:
                continue
            route = f"{base_command} {field}" if field else base_command
            signals.response.emit(self.address, route, str(value))

    def _read_snapshot(self, base_command):
        payload = self.query(base_command)
        if payload is None:
            return None
        return self._parse_snapshot(base_command, payload)

    def _equalize_phase(self):
        # EQPHASE не имеет query-формы, поэтому можно повторить команду только
        # при явной VISA-ошибке. Повторный вызов безопасен.
        for _attempt in range(1, self.write_attempts + 1):
            if self.write(EQPHASE):
                return True
        signals.error_occurrence.emit("EQPHASE failed")
        return False

    @pyqtSlot(str, str, str)
    def _on_write(self, address, command, value):
        if address != self.address:
            return None

        base_command, field = self._split_route(command)
        last_value = None

        with self.lock:
            # Успешный VISA write ещё не означает, что SDG применил значение.
            # Поэтому после каждой попытки читаем параметр и сравниваем ответ.
            for _attempt in range(1, self.write_attempts + 1):
                write_command = self._write_command(base_command, field, value)
                if not self.write(write_command):
                    continue

                snapshot = self._read_snapshot(base_command)
                if snapshot is None:
                    continue

                self._emit_snapshot(base_command, snapshot)
                last_value = snapshot.get(field)
                if last_value is not None and self._values_equal(value, last_value):
                    if (
                        base_command.endswith(f":{BSWV}")
                        and field in PHASE_AFFECTING_FIELDS
                    ):
                        self._equalize_phase()
                    return last_value

        signals.error_occurrence.emit(
            f"Write verification failed: {command}={value}, "
            f"readback={last_value}"
        )
        return None

    @pyqtSlot(str, str)
    def _on_query(self, address, command):
        if address != self.address:
            return None

        base_command, field = self._split_route(command)
        snapshot = self._read_snapshot(base_command)
        if snapshot is None:
            return None

        if field:
            value = snapshot.get(field)
            if value is not None:
                signals.response.emit(self.address, command, str(value))
            return value

        self._emit_snapshot(base_command, snapshot)
        return snapshot

    def sync_all_params(self):
        # Двух запросов на канал достаточно: каждый возвращает все свои поля.
        for channel in CHANNELS:
            for subsystem in (BSWV, OUTP):
                base_command = scpi_route(channel, subsystem)
                snapshot = self._read_snapshot(base_command)
                if snapshot is not None:
                    self._emit_snapshot(base_command, snapshot)
        super().sync_all_params()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Widget()
    window.show()
    sys.exit(app.exec())
