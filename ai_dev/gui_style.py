"""One visual vocabulary for every wizard page and dialog."""

from pathlib import Path
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def asset(name):
    for base in (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)),
        Path(sys.prefix) / "share/megaprog",
    ):
        path = base / "assets" / name
        if path.is_file():
            return path
    return Path(__file__).resolve().parent.parent / "assets" / name


PALETTES = {
    "dark": dict(
        bg="#101d26",
        panel="#192c36",
        field="#12232d",
        text="#edf5f5",
        muted="#a9bdc3",
        line="#35505b",
        accent="#80dfc3",
        ink="#102c2b",
        hover="#25414a",
        danger="#ffa5a5",
        warning="#f3d08a",
    ),
    "light": dict(
        bg="#f2f6f7",
        panel="#ffffff",
        field="#f7fafb",
        text="#173039",
        muted="#536a73",
        line="#bccfd4",
        accent="#087d70",
        ink="#ffffff",
        hover="#e5f2ef",
        danger="#ac303b",
        warning="#84570b",
    ),
}


def resolved_theme(choice):
    if choice in PALETTES:
        return choice
    return (
        "dark"
        if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        else "light"
    )


def apply_theme(app, choice):
    name = resolved_theme(choice)
    c = PALETTES[name]
    palette = QPalette()
    for role, value in [
        (QPalette.ColorRole.Window, c["bg"]),
        (QPalette.ColorRole.WindowText, c["text"]),
        (QPalette.ColorRole.Base, c["field"]),
        (QPalette.ColorRole.AlternateBase, c["panel"]),
        (QPalette.ColorRole.Text, c["text"]),
        (QPalette.ColorRole.Button, c["panel"]),
        (QPalette.ColorRole.ButtonText, c["text"]),
        (QPalette.ColorRole.Highlight, c["accent"]),
        (QPalette.ColorRole.HighlightedText, c["ink"]),
        (QPalette.ColorRole.ToolTipBase, c["panel"]),
        (QPalette.ColorRole.ToolTipText, c["text"]),
        (QPalette.ColorRole.Link, c["accent"]),
    ]:
        palette.setColor(role, QColor(value))
    app.setPalette(palette)
    arrow = asset("chevron-" + name + ".svg").as_posix()
    check = asset("check-" + name + ".svg").as_posix()
    app.setStyleSheet("""
QWidget { color: %(text)s; }
QMainWindow, QDialog, QWidget#shell { background: %(bg)s; }
QLabel { background: transparent; }
QLabel#brand { font-size: 21px; font-weight: 650; }
QLabel#eyebrow { color: %(muted)s; font-size: 12px; }
QLabel#heading { font-size: 29px; font-weight: 650; }
QLabel#muted, QLabel[muted="true"] { color: %(muted)s; }
QFrame#card { background: %(panel)s; border: 1px solid %(line)s; border-radius: 16px; }
QLabel#cardTitle { font-size: 16px; font-weight: 600; }
QLabel#notice { background: %(hover)s; border-radius: 10px; padding: 12px; }
QLabel#warning { color: %(warning)s; }
QLabel#error { color: %(danger)s; }
QPushButton { background: %(panel)s; border: 1px solid %(line)s; border-radius: 10px; padding: 9px 16px; min-height: 20px; }
QPushButton:hover { background: %(hover)s; }
QPushButton:pressed { border-color: %(accent)s; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextBrowser:focus { border: 2px solid %(accent)s; }
QPushButton[primary="true"] { background: %(accent)s; color: %(ink)s; border-color: %(accent)s; font-weight: 600; }
QPushButton[danger="true"] { color: %(danger)s; }
QPushButton:disabled { color: %(muted)s; background: %(bg)s; border-color: %(line)s; }
QPushButton#step { border: 0; background: transparent; padding: 10px 6px; color: %(muted)s; border-radius: 8px; }
QPushButton#step[active="true"] { background: %(hover)s; color: %(accent)s; font-weight: 600; }
QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser { background: %(field)s; border: 1px solid %(line)s; border-radius: 10px; padding: 10px; selection-background-color: %(accent)s; selection-color: %(ink)s; }
QComboBox { padding-right: 30px; min-height: 20px; }
QComboBox::drop-down { border: 0; width: 28px; }
QComboBox::down-arrow { image: url("%(arrow)s"); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background: %(panel)s; color: %(text)s; selection-background-color: %(hover)s; selection-color: %(text)s; border: 1px solid %(line)s; padding: 6px; }
QCheckBox { spacing: 10px; }
QCheckBox::indicator { width: 20px; height: 20px; border: 1px solid %(line)s; border-radius: 5px; background: %(field)s; }
QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; image: url("%(check)s"); }
QCheckBox::indicator:focus { border: 2px solid %(accent)s; }
QScrollArea { border: 0; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 0; }
QScrollBar::handle:vertical { background: %(line)s; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: %(muted)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: %(line)s; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QProgressBar { background: %(field)s; border: 0; border-radius: 5px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 5px; }
QToolTip { color: %(text)s; background: %(panel)s; border: 1px solid %(line)s; padding: 6px; }
QMenu { background: %(panel)s; border: 1px solid %(line)s; padding: 6px; }
QMenu::item { padding: 8px 20px; border-radius: 5px; }
QMenu::item:selected { background: %(hover)s; }
QAbstractItemView { background: %(field)s; color: %(text)s; selection-background-color: %(hover)s; selection-color: %(text)s; }
""" % dict(c, arrow=arrow, check=check))
    return name
