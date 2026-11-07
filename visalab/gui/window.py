"""Универсальное окно прибора, которое строится из описания параметров драйвера.

``section = "Вкладка/Группа"`` -> вкладка QTabWidget, внутри группы QGroupBox
(до трёх в ряд). Окно общается с прибором только через InstrumentProxy (сигналы).
"""
from __future__ import annotations

import time
from collections import OrderedDict

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QDockWidget, QFormLayout, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QVBoxLayout,
                             QWidget)

from ..core.params import ConnectionState, Kind
from ..core.proxy import InstrumentProxy
from .widgets import STYLE, make_widget

_LED = {
    ConnectionState.DISCONNECTED: ("#9e9e9e", "Отключён"),
    ConnectionState.CONNECTING: ("#c9a227", "Подключение…"),
    ConnectionState.CONNECTED: ("#2e7d32", "Подключён"),
    ConnectionState.ERROR: ("#d64545", "Ошибка связи"),
}


class InstrumentWindow(QMainWindow):
    GROUP_COLUMNS = 3

    def __init__(self, proxy: InstrumentProxy, title: str | None = None, parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.setWindowTitle(f"{title or proxy.name} — {proxy.address}")
        self.setStyleSheet(STYLE)

        central = QWidget()
        root = QVBoxLayout(central)
        root.addLayout(self._build_header())
        self.tabs = self._build_tabs()
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._build_console()

        proxy.state_changed.connect(self._on_state)
        proxy.identified.connect(lambda idn: self.idn_label.setText(idn))
        proxy.instrument_error.connect(lambda c, m: self._log(f"ОШИБКА ПРИБОРА {c}: {m}", error=True))
        proxy.comm_error.connect(lambda m: self._log(f"ОШИБКА: {m}", error=True))
        proxy.scpi_log.connect(lambda d, t: self._log(f"{d} {t}"))
        proxy.raw_response.connect(lambda q, a: self._log(f"{q} → {a}"))
        proxy.param_status.connect(self._on_param_status)
        self._on_state(proxy.state)

    # ---------------------------------------------------------------- шапка
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.led = QLabel()
        self.led.setObjectName("led")
        self.state_label = QLabel()
        self.connect_btn = QPushButton("Подключить")
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.sync_btn = QPushButton("Синхронизировать")
        self.sync_btn.setToolTip("Перечитать все параметры из прибора")
        self.sync_btn.clicked.connect(self.proxy.sync)
        self.idn_label = QLabel("")
        self.idn_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.idn_label.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.idn_label.setFont(f)
        row.addWidget(self.led)
        row.addWidget(self.state_label)
        row.addWidget(self.connect_btn)
        row.addWidget(self.sync_btn)
        self.action_buttons = []
        for action in self.proxy.actions.values():
            btn = QPushButton(action.label)
            btn.clicked.connect(lambda _=False, a=action: self._run_action(a))
            row.addWidget(btn)
            self.action_buttons.append(btn)
        row.addStretch(1)
        row.addWidget(self.idn_label)
        return row

    # --------------------------------------------------------------- вкладки
    def _build_tabs(self) -> QTabWidget:
        layout: "OrderedDict[str, OrderedDict[str, list]]" = OrderedDict()
        for spec in self.proxy.params.values():
            tab, _, group = (spec.section or "Параметры/Общие").partition("/")
            layout.setdefault(tab, OrderedDict()).setdefault(group or "Общие", []).append(spec)

        tabs = QTabWidget()
        for tab_name, groups in layout.items():
            page = QWidget()
            grid = QGridLayout(page)
            for i, (group_name, specs) in enumerate(groups.items()):
                box = QGroupBox(group_name)
                form = QFormLayout(box)
                form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
                for spec in specs:
                    w = make_widget(self.proxy, spec)
                    if spec.kind is Kind.TRACE:      # график - на всю ширину группы
                        form.addRow(w)
                        continue
                    w.setMinimumWidth(130)
                    form.addRow(spec.label + (f", {spec.unit}" if spec.unit and spec.readonly is False else ""), w)
                grid.addWidget(box, i // self.GROUP_COLUMNS, i % self.GROUP_COLUMNS,
                               alignment=Qt.AlignmentFlag.AlignTop)
            grid.setRowStretch(grid.rowCount(), 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tabs.addTab(scroll, tab_name)
        return tabs

    # --------------------------------------------------------------- консоль
    def _build_console(self) -> None:
        dock = QDockWidget("Журнал SCPI", self)
        dock.setObjectName("console")
        w = QWidget()
        lay = QVBoxLayout(w)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont("monospace", 9))
        lay.addWidget(self.log_view)
        row = QHBoxLayout()
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText("SCPI-команда, например *IDN?")
        self.cmd_edit.returnPressed.connect(self._send_raw)
        send = QPushButton("Отправить")
        send.clicked.connect(self._send_raw)
        self.poll_log = QCheckBox("Показывать опрос")
        self.poll_log.setToolTip("Логировать трафик периодического опроса (много строк)")
        self.poll_log.toggled.connect(self.proxy.set_log_polling)
        row.addWidget(self.cmd_edit, 1)
        row.addWidget(send)
        row.addWidget(self.poll_log)
        lay.addLayout(row)
        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    # ------------------------------------------------------------ обработчики
    def _toggle_connection(self) -> None:
        if self.proxy.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            self.proxy.close()
        else:
            self.proxy.open()

    def _run_action(self, action) -> None:
        if action.confirm and QMessageBox.question(self, action.label, action.confirm) \
                != QMessageBox.StandardButton.Yes:
            return
        self.proxy.action(action.name)

    def _send_raw(self) -> None:
        cmd = self.cmd_edit.text().strip()
        if not cmd:
            return
        (self.proxy.query_raw if cmd.endswith("?") or "?" in cmd.split()[0] else self.proxy.write_raw)(cmd)
        self.cmd_edit.clear()

    def _on_state(self, state: ConnectionState) -> None:
        color, text = _LED[state]
        self.led.setStyleSheet(f"background: {color};")
        self.state_label.setText(text)
        connected = state is ConnectionState.CONNECTED
        self.connect_btn.setText("Отключить" if state in (ConnectionState.CONNECTED,
                                                           ConnectionState.CONNECTING) else "Подключить")
        self.tabs.setEnabled(connected)
        self.sync_btn.setEnabled(connected)
        for b in self.action_buttons:
            b.setEnabled(connected)
        self.statusBar().showMessage(text, 5000)

    def _on_param_status(self, key: str, status, message: str) -> None:
        if message:
            self._log(f"{key}: {status.value} — {message}", error=status.value in ("error", "invalid"))

    def _log(self, text: str, error: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {text}")
        if error:
            self.statusBar().showMessage(text, 8000)

    def closeEvent(self, event) -> None:
        self.proxy.shutdown()
        super().closeEvent(event)
