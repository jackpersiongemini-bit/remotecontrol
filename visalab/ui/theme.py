"""Общая тема окон (тёмная, «приборная»)."""

ACCENT = "#e0a020"

STYLE = f"""
QWidget {{ background: #161b22; color: #d6dbe1; font-size: 10pt; }}
QWidget#frameRoot {{ background: #161b22; border: 1px solid #30363d; }}
QToolTip {{ background: #222a33; color: #d6dbe1; border: 1px solid #30363d; }}

/* заголовок */
QWidget#titleBar {{ background: #0f1318; }}
QLabel#titleText {{ background: transparent; font-weight: 600; padding-left: 6px; }}
QLabel#titleSub {{ background: transparent; color: #7a828e; padding-left: 8px; }}
QLabel#led {{ border-radius: 6px; min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px; }}
QFrame#modeSwitch {{ background: transparent; }}
QFrame#modeSwitch QToolButton {{ background: #222a33; border: 1px solid #30363d; padding: 2px 10px; color: #9aa4af; }}
QFrame#modeSwitch QToolButton:checked {{ background: {ACCENT}; color: #111; border-color: {ACCENT}; font-weight: 600; }}

/* верхняя панель */
QFrame#actionBar {{ background: #1c222b; border-bottom: 1px solid #30363d; }}
QFrame#actionBar QPushButton {{ min-height: 30px; }}
QFrame#readout {{ background: #0f1318; border: 1px solid #30363d; border-radius: 6px; min-width: 150px; }}
QLabel#readoutCaption {{ background: transparent; color: #7a828e; font-size: 8.5pt; }}
QLabel#readoutValue {{ background: transparent; color: {ACCENT}; font-family: monospace; font-size: 17pt; }}

/* кнопки */
QPushButton {{ background: #222a33; border: 1px solid #30363d; border-radius: 5px; padding: 4px 12px; }}
QPushButton:hover {{ border-color: #4b5562; }}
QPushButton:disabled {{ color: #59616b; }}
QPushButton[role="primary"] {{ background: #2b3440; font-weight: 600; }}
QPushButton[role="toggle"]:checked, QPushButton[role="output"]:checked {{ background: #238636; border-color: #2ea043; color: white; font-weight: 600; }}
QPushButton[role="output"] {{ min-width: 110px; }}

/* поля */
QLineEdit, QComboBox {{ background: #0f1318; border: 1px solid #30363d; border-radius: 4px; padding: 3px 6px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: #59616b; }}
QLineEdit[invalid="true"], QComboBox[invalid="true"] {{ border-color: #f85149; }}
QLabel[role="value"] {{ font-family: monospace; }}
QToolButton#pin {{ background: transparent; border: none; color: #59616b; font-size: 13pt; padding: 0 2px; }}
QToolButton#pin:checked {{ color: {ACCENT}; }}

/* таблица */
QTableWidget#paramTable {{ background: #161b22; alternate-background-color: #1a2028; border: 1px solid #30363d; }}
QHeaderView::section {{ background: #1c222b; color: #9aa4af; border: none; border-bottom: 1px solid #30363d; padding: 4px 6px; }}
QLabel#placeholder {{ background: transparent; color: #59616b; }}

/* полный экран */
QTabWidget::pane {{ border: 1px solid #30363d; top: -1px; }}
QTabBar::tab {{ background: #1c222b; border: 1px solid #30363d; padding: 5px 14px; margin-right: 2px; color: #9aa4af; }}
QTabBar::tab:selected {{ background: #161b22; color: #d6dbe1; border-bottom-color: #161b22; }}
QGroupBox {{ border: 1px solid #30363d; border-radius: 6px; margin-top: 12px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT}; }}
QSplitter::handle {{ background: #30363d; }}
QStatusBar, QLabel#statusLine {{ background: #0f1318; color: #7a828e; padding: 2px 8px; }}
"""
