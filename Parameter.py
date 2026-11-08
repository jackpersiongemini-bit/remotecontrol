from WidgetFabric import VBoxLayout, HBoxLayout, Button
from Shared import *
from Constants import *
from Signals import signals

from enum import Enum, auto
from PyQt6.QtCore import pyqtSignal, Qt, QEvent
from PyQt6.QtWidgets import (
    QLabel, 
    QLineEdit, 
    QWidget, 
    QFrame,
    QButtonGroup
)



def make_plabel(address, command):
    wdgt = PLabel()
    wdgt.signal_connect(address, command)
    return wdgt

def make_plineedit(unit, address, command):
    wdgt = PLineEdit(unit)
    wdgt.signal_connect(address, command)
    return wdgt

def make_pnlabel(unit, address, command):
    wdgt = PNLabel(unit)
    wdgt.signal_connect(address, command)
    return wdgt

def make_psbutton(states, address, command):
    wdgt = PSButton(states)
    wdgt.signal_connect(address, command)
    return wdgt

def make_pbgroup(states, address, command):
    wdgt = PBGroup(states)
    wdgt.signal_connect(address, command)
    return wdgt

def panel_line(name, *wdgts):
    wdgt = PanelLine(name)
    wdgt.add_widget(*wdgts)
    return wdgt

def make_table(*lines):
    table = PanelTable()
    table.add_line(*lines)
    return table

def make_panel(*lines):
    table = make_table(*lines)
    panel = Panel(table)
    return panel


class PStatus(Enum):
    UNKNOWN     = auto()
    WRITING     = auto()
    WRITTEN     = auto()
    UNWRITTEN   = auto()
    INCORRECT   = auto()


class Parameter:
    def __init__(self):
        self._status = PStatus.UNKNOWN
        self._value = None
        self._pending_value = None

        self._address = None
        self._command = None

    @property
    def status(self):
        return self._status

    @property
    def value(self):
        return self._value

    @property
    def pending_value(self):
        return self._pending_value

    def signal_connect(self, address, command):
        if address is None or command is None:
            signals.error_occurrence.emit(f'{self} Connect Error')
            return
        self.signal_disconnect()
        self._address = address
        self._command = command
        signals.response.connect(self._on_response)

    def signal_disconnect(self):
        self._address = None
        self._command = None
        try:
            signals.response.disconnect(self._on_response)
        except:
            pass

    def recall(self):
        if self._address and self._command:
            value = settings.value(f'{self._address}{self._command}'.upper())
            if value is not None: 
                self.set_value(value)
                self.change_value()
        else:
            signals.error_occurrence.emit(f'[{self}] Recall error: have not address or command')

    def save(self):
        if self._address and self._command and self._value is not None:
            settings.setValue(f'{self._address}{self._command}'.upper(), self._value)

    def _written(self):
        self._status = PStatus.WRITTEN
        self._pending_value = None

    def _unwritten(self):
        self._status = PStatus.UNWRITTEN
        signals.error_occurrence.emit(f"{self}: unwritten")

    def _writing(self):
        self._pending_value = self._value
        self._status = PStatus.WRITING

    def _incorrect(self):
        self._status = PStatus.INCORRECT
        signals.error_occurrence.emit(f"{self}: incorrect")

    def _unknown(self):
        self._status = PStatus.UNKNOWN
        self._value = None
        self._pending_value = None

    def set_value(self, value):
        if self.pending_value is None:
            self._written()
        elif self.pending_value == value:
            self._written()
        else:
            self._unwritten()
        self._value = value

    def change_value(self):
        self._writing()
        if self._address is not None and self._command is not None:
            signals.write.emit(self._address, self._command, str(self._value))

    def clear(self):
        self._unknown()

    def _on_response(self, address, command, answer):
        if address == self._address and command == self._command:
            self.set_value(answer)

class PLabel(QLabel, Parameter):
    def __init__(self, **kwargs):
        QLabel.__init__(self, **kwargs)
        Parameter.__init__(self)
        self.clear()

    def set_value(self, value):
        if value is None:
            self.clear()
            return

        self.setText(str(value))
        Parameter.set_value(self, value)

    def clear(self):
        QLabel.clear(self)
        Parameter.clear(self)
        
class PLineEdit(QLineEdit, Parameter):
    def __init__(self, unit='', **kwargs):
        QLineEdit.__init__(self, **kwargs)
        Parameter.__init__(self)

        self._unit = unit
        self._is_not_prefix = self._unit in NOT_PREFIX_UNITS
        self._separator = " " if self._unit not in NOT_SEPARATOR_UNITS else ""
        self.clear()

    def set_value(self, value):
        self._convert_value(value)
        return Parameter.set_value(self, value)

    def _on_response(self, address, command, answer):
        if address != self._address or command != self._command:
            return

        try:
            value = float(str(answer).replace(",", "."))
        except (TypeError, ValueError):
            self._incorrect()
            return

        self.set_value(value)

    def _convert_value(self, value):
        if not self._is_not_prefix:
            power_value, converted_value = value_to_prefix(value)
        else:
            power_value, converted_value = 0, value
        self.setText(
            f"{converted_value:.6g}"
            f"{self._separator}"
            f"{prefix_dictionary[power_value]}"
            f"{self._unit}"
        )

    def change_value(self):
        raw_text = self.text().strip().replace(',', '.')
        if self._unit and raw_text.endswith(self._unit):
            raw_text = raw_text[:-len(self._unit)].rstrip()
        last_symbol = raw_text[-1:]
        try:
            if not self._is_not_prefix and last_symbol in invert_prefix_dictionary.keys():
                power_value = invert_prefix_dictionary.get(last_symbol)
                converted_value = float(raw_text[:-1])
                self._value = converted_value * 1000 ** (power_value)
            else:
                self._value = float(raw_text)
        except ValueError:
            self._incorrect()
            return
        self._convert_value(self._value)
        return Parameter.change_value(self)

    def clear(self):
        QLineEdit.clear(self)
        Parameter.clear(self)

    def close(self):
        Parameter.save(self)
        QLineEdit.close(self)

class PNLabel(PLineEdit):
    def __init__(self, unit='', **kwargs):
        super().__init__(unit, **kwargs)
        self.setReadOnly(True)

class PSButton(Button, Parameter):
    def __init__(self, states:dict):
        Button.__init__(self)
        Parameter.__init__(self)
        if not states:
            raise ValueError(f"{self}: PSButton requires at least one state")
        
        self._states = states # name: data

        self._names = tuple(self._states.keys())
        self._data = tuple(self._states.values())

        self._index = -1
        self._len = len(self._names)
        self.clear()

        self.clicked.connect(self.next_value)

    def next_value(self):
        self._index = (self._index + 1) % self._len
        self._value = self._data[self._index]
        self._change_text()
        self.change_value()

    def _change_text(self):
        if self._index == -1:
            self.setText("NKNW")
            return
        self.setText(self._names[self._index])

    def set_value(self, value):
        if value is None:
            self._incorrect()
            return False

        value_text = str(value)

        matches = [
            (ndx, data)
            for ndx, data in enumerate(self._data)
            if data is not None and str(data) in value_text
        ]

        if not matches:
            self._incorrect()
            return False

        ndx, data = max(
            matches,
            key=lambda item: len(str(item[1])),
        )

        self._index = ndx
        self._change_text()
        Parameter.set_value(self, data)
        return True

    def clear(self):
        self._index = -1
        self._change_text()
        Parameter.clear(self)

    def close(self):
        Parameter.save(self)
        QLineEdit.close(self)

class PBGroup(QWidget, Parameter):
    def __init__(self, states: dict):
        QWidget.__init__(self)
        Parameter.__init__(self)

        if not states:
            raise ValueError(
                f"{self}: PBGroup requires at least one state"
            )

        self._names = tuple(states.keys())
        self._data = tuple(states.values())

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self.group_layout = HBoxLayout(
            self,
            margins=(0, 0, 0, 0),
            spacing=0,
        )

        for index, name in enumerate(self._names):
            button = Button(name)
            button.setCheckable(True)

            self._group.addButton(button, index)
            self.group_layout.addWidget(button)

        self._group.idClicked.connect(self._change_value)

    def _change_value(self, index):
        self._value = self._data[index]
        Parameter.change_value(self)

    def set_value(self, value):
        if value is None:
            self._incorrect()
            return False

        value_text = str(value)

        matches = [
            (index, data)
            for index, data in enumerate(self._data)
            if data is not None and str(data) in value_text
        ]

        if not matches:
            self._incorrect()
            return False

        index, data = max(matches, key=lambda item: len(str(item[1])),)

        self._group.button(index).setChecked(True)
        Parameter.set_value(self, data)
        return True

    def clear(self):
        self._group.setExclusive(False)

        for button in self._group.buttons():
            button.setChecked(False)

        self._group.setExclusive(True)
        Parameter.clear(self)

    def close(self):
        Parameter.save(self)
        return QWidget.close(self)


class PanelLine(QWidget):
    def __init__(self, name):
        super().__init__()
        self._name = QLabel(name)
        self._name.setAlignment(af.AlignLeft| af.AlignVCenter)
        self._widgets = []
        self.line_layout = HBoxLayout(self,
                                      margins=(0,0,0,0),
                                      spacing=8)
        self.line_layout.addWidget(self._name)

    def add_widget(self, *wdgts):
        for wdgt in wdgts:
            self._widgets.append(wdgt)

    def build(self):
        for wdgt in self._widgets:
            self.line_layout.addWidget(wdgt)

    @property
    def name(self):
        return self._name.text()

    # Методы размеров
    def set_label_width(self, width):
        self._name.setFixedWidth(width)

    def set_wdgts_width(self, width):
        for wdgt in self._widgets:
            wdgt.setFixedWidth(width)

    @property
    def label_size_hint_width(self):
        self._name.ensurePolished()
        return self._name.sizeHint().width()

    @property
    def wdgts_size_hint_width(self):
        widths = []

        for widget in self._widgets:
            widget.ensurePolished()
            widths.append(widget.sizeHint().width())

        return widths

class PanelTable(QWidget):
    def __init__(self):
        super().__init__()
        self._lines = []
        self.table_layout = VBoxLayout(self,
                                       margins=(0,0,0,0),
                                       spacing=4)
        self.table_layout.setAlignment(af.AlignTop)

    def add_line(self, *lines:PanelLine):
        for line in lines:
            self._lines.append(line)

    # build необходимо вызывать только в классе с STYLE_SHEET
    def build(self):
        for line in self._lines:
            line.build()
            self.table_layout.addWidget(line)
        self._rewidth_labels()

    def _rewidth_labels(self):
        width = max([line.label_size_hint_width for line in self._lines])
        for line in self._lines:
            line.set_label_width(width)

    def rewidth_wdgts(self):
        width = max(max(line.wdgts_size_hint_width) for line in self._lines)
        for line in self._lines:
            line.set_wdgts_width(width)

class Panel(QFrame):
    STYLE_SHEET = render_qss("""
        Panel, Panel QWidget {
            border: none;
            background-color: $UI_COLOR_PANEL;
            font-family: $UI_MONOSPACE_FONT_FAMILY;
            font-size: ${UI_FONT_SIZE_PT}pt;
            color: $UI_COLOR_TEXT_MUTED;
            border-radius: ${UI_BORDER_RADIUS_PX}px;
        }
        Panel PLineEdit {
            background-color: $UI_COLOR_BACKGROUND;
        }
        Panel PLabel,
        Panel PNLabel {
            background-color: transparent;
            padding: 4px 1px;
        }
        Panel PSButton,
        Panel PLineEdit
        {
            padding: 4px 1px;
        }
        Panel PSButton:hover {
            background-color: $UI_COLOR_TEXT_MUTED;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT_INVERSE;
        }
        Panel PBGroup {
            background-color: transparent;
        }
        Panel PBGroup Button {
            background-color: $UI_COLOR_BACKGROUND;
            padding: 4px 8px;
        }
        Panel PBGroup Button:hover {
            background-color: $UI_COLOR_BORDER;
        }
        Panel PBGroup Button:checked {
            background-color: $UI_COLOR_TEXT_MUTED;
            font-weight: $UI_FONT_WEIGHT_SEMIBOLD;
            color: $UI_COLOR_TEXT_INVERSE;
        }
    """)

    def __init__(self, panel: PanelTable):
        super().__init__()
        self.setStyleSheet(self.STYLE_SHEET)
        self._panel = panel
        self.panel_layout = VBoxLayout(self, 
                                       margins=(8,8,8,8),
                                       spacing=0)
        self.panel_layout.addWidget(self._panel)
        self._panel.build()

    def rewdith_wdgts(self):
        self._panel.rewidth_wdgts()

