"""Dark application theme - QSS stylesheet + accent management (Claude-clay by default)."""

from __future__ import annotations

ACCENTS = {
    "Claude Clay": "#d97757",
    "Signal Blue": "#6ea8fe",
    "Mint": "#3fd68f",
    "Amber": "#ffb454",
    "Violet": "#b18cff",
}
DEFAULT_ACCENT = "Claude Clay"

_BASE = {
    "bg": "#15130f", "panel": "#1c1915", "panel2": "#26221c", "border": "#34302a", "text": "#e6e0d6", "muted": "#9a9284",
    "field": "#211e18", "console": "#100e0b", "blue": "#6ea8fe", "amber": "#ffb454", "red": "#ff5d5d", "green": "#3fd68f",
    "onaccent": "#1b0f0a",
}


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def _lighten(hex_color: str, factor: float = 0.18) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(int(r + (255 - r) * factor), int(g + (255 - g) * factor), int(b + (255 - b) * factor))


_QSS = """
QWidget { background-color: $bg; color: $text;
          font-family: "Segoe UI", "Inter", "Helvetica Neue", system-ui, sans-serif; font-size: 13px; }
QMainWindow, QDialog { background-color: $bg; }
QLabel { background: transparent; }
#Sidebar { background-color: $panel; border-right: 1px solid $border; }
#LogoLabel { font-size: 21px; font-weight: 800; letter-spacing: 1px; }
#LogoSub { color: $muted; font-size: 10px; letter-spacing: 2px; }
#SideVersion { color: $muted; font-size: 10px; }

QToolButton[nav="true"] { background: transparent; border: none; border-left: 3px solid transparent; border-radius: 7px;
                          padding: 11px 12px; text-align: left; color: $muted; font-size: 13px; }
QToolButton[nav="true"]:hover { background-color: $panel2; color: $text; }
QToolButton[nav="true"]:checked { background-color: $panel2; color: $text; border-left: 3px solid $accent; font-weight: 600; }
QToolButton[nav="true"][locked="true"] { color: #6d675c; }

QPushButton { background-color: $panel2; border: 1px solid $border; border-radius: 8px; padding: 8px 16px; color: $text; font-weight: 600; }
QPushButton:hover { border-color: $accent; }
QPushButton:pressed { background-color: $accent_dim; }
QPushButton:disabled { color: $muted; background-color: $panel; border-color: $border; }
QPushButton[primary="true"] { background-color: $accent; color: $onaccent; border: none; font-weight: 700; }
QPushButton[primary="true"]:hover { background-color: $accent_hi; }
QPushButton[primary="true"]:disabled { background-color: $panel2; color: $muted; }
QPushButton[danger="true"] { background: transparent; color: $red; border: 1px solid $red; }
QPushButton[danger="true"]:hover { background-color: rgba(255, 93, 93, 0.12); }
QPushButton[mini="true"] { padding: 3px 9px; border-radius: 6px; font-weight: 700; }
QPushButton[mode="audit"] { border: 1px solid $red; color: $text; }
QPushButton[mode="live"] { border: 1px solid $green; color: $text; }
QPushButton[mode="audit"]:disabled, QPushButton[mode="live"]:disabled { border-color: $border; }

QLineEdit, QSpinBox, QComboBox { background-color: $field; border: 1px solid $border; border-radius: 7px; padding: 7px 10px; color: $text;
                                 selection-background-color: $accent; selection-color: $onaccent; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $accent; }
QLineEdit:read-only { color: $muted; }
QLineEdit[mono="true"], QPlainTextEdit[mono="true"] { font-family: "Cascadia Code", "Consolas", "Menlo", monospace; font-size: 12px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background-color: $panel2; border: 1px solid $border; color: $text;
                              selection-background-color: $accent_dim; selection-color: $accent; }

QCheckBox, QRadioButton { spacing: 9px; background: transparent; padding: 3px 0; }
QCheckBox:disabled, QRadioButton:disabled { color: #6d675c; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid $border; background-color: $panel2; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background-color: $accent; border-color: $accent; }
QCheckBox::indicator:checked:hover, QRadioButton::indicator:checked:hover { background-color: $accent_hi; }

QProgressBar { background-color: $panel2; border: none; border-radius: 7px; height: 15px; text-align: center; color: $text; font-size: 10px; font-weight: 600; }
QProgressBar::chunk { background-color: $accent; border-radius: 7px; }

QPlainTextEdit[console="true"], QPlainTextEdit[preview="true"] { background-color: $console; border: 1px solid $border; border-radius: 8px;
    font-family: "Cascadia Code", "Consolas", "Menlo", monospace; font-size: 12px; }

QFrame[card="true"] { background-color: $panel; border: 1px solid $border; border-radius: 10px; }
#CardValue { font-size: 22px; font-weight: 800; color: $accent; }
#CardCaption { color: $muted; font-size: 10px; letter-spacing: 1px; }
#BigReadout { font-size: 30px; font-weight: 800; color: $accent; }

QLabel[chip="green"] { background-color: $accent_dim; color: $accent; border-radius: 5px; padding: 3px 10px; font-weight: 700; }
QLabel[chip="blue"] { background-color: rgba(110, 168, 254, 0.15); color: $blue; border-radius: 5px; padding: 3px 10px; font-weight: 700; }
QLabel[chip="amber"] { background-color: rgba(255, 180, 84, 0.15); color: $amber; border-radius: 5px; padding: 3px 10px; font-weight: 700; }
QLabel[chip="red"] { background-color: rgba(255, 93, 93, 0.15); color: $red; border-radius: 5px; padding: 3px 10px; font-weight: 700; }
QLabel[h1="true"] { font-size: 22px; font-weight: 800; }
QLabel[h2="true"] { font-size: 15px; font-weight: 700; }
QLabel[muted="true"] { color: $muted; }
QLabel[banner="amber"] { background-color: rgba(255, 180, 84, 0.10); border: 1px solid rgba(255, 180, 84, 0.45); border-radius: 8px; padding: 9px 12px; color: $amber; }

#LockOverlay { background-color: rgba(21, 19, 15, 0.94); }
#LockCard { background-color: $panel; border: 1px solid $border; border-radius: 14px; }

QListWidget, QTableWidget { background-color: $panel; border: 1px solid $border; border-radius: 8px; outline: none; gridline-color: $border; }
QListWidget::item { padding: 7px 9px; border-bottom: 1px solid $border; }
QListWidget::item:selected { background-color: $accent_dim; color: $accent; }
QHeaderView::section { background-color: $panel2; color: $muted; border: none; border-bottom: 1px solid $border; padding: 6px 8px; font-weight: 700; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 3px; }
QScrollBar::handle:vertical { background-color: #45403a; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background-color: #574f47; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 3px; }
QScrollBar::handle:horizontal { background-color: #45403a; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QStatusBar { background-color: $panel; border-top: 1px solid $border; color: $muted; }
QStatusBar::item { border: none; }
QToolTip { background-color: $panel2; color: $text; border: 1px solid $accent; border-radius: 8px; padding: 9px 12px; font-size: 12px; max-width: 340px; }
QMessageBox { background-color: $panel; }
QSplitter::handle { background-color: $border; }
QTextBrowser { background-color: $panel; border: 1px solid $border; border-radius: 10px; padding: 14px; }
"""


def palette(accent_name: str = DEFAULT_ACCENT) -> dict:
    accent = ACCENTS.get(accent_name, ACCENTS[DEFAULT_ACCENT])
    p = dict(_BASE)
    p.update(accent=accent, accent_hi=_lighten(accent), accent_dim=_rgba(accent, 0.16))
    return p


def build_qss(accent_name: str = DEFAULT_ACCENT) -> str:
    p = palette(accent_name)
    qss = _QSS
    for key in sorted(p, key=len, reverse=True):          # longest first: $panel2 before $panel
        qss = qss.replace("$" + key, p[key])
    return qss


def apply(app, accent_name: str = DEFAULT_ACCENT) -> None:
    app.setStyleSheet(build_qss(accent_name))
