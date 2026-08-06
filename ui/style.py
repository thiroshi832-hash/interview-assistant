"""Shared stylesheet — Catppuccin-inspired dark theme, easy on the eyes."""
import sys

_FONT_FAMILY = (
    '"Segoe UI"' if sys.platform == "win32"
    else '".AppleSystemUIFont", "Helvetica Neue"' if sys.platform == "darwin"
    else '"Ubuntu", "Noto Sans", sans-serif'
)

STYLE = (
    "* { font-family: " + _FONT_FAMILY + "; font-size: 13px; }\n"
    """
QMainWindow, QDialog, QWidget { background: #1e1e2e; color: #cdd6f4; }

QGroupBox {
    border: 1px solid #45475a; border-radius: 6px;
    margin-top: 14px; padding-top: 6px;
    color: #89b4fa; font-weight: bold;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }

QLineEdit, QTextEdit, QPlainTextEdit {
    background: #313244; border: 1px solid #45475a; border-radius: 4px;
    padding: 5px 8px; color: #cdd6f4;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus { border-color: #89b4fa; }

QPushButton {
    background: #45475a; color: #cdd6f4; border: 1px solid #585b70;
    border-radius: 4px; padding: 6px 14px;
}
QPushButton:hover { background: #585b70; }
QPushButton:pressed { background: #313244; }
QPushButton:disabled { color: #6c7086; }

QPushButton#primary {
    background: #89b4fa; color: #1e1e2e; font-weight: bold; border: 0;
}
QPushButton#primary:hover { background: #b4befe; }
QPushButton#primary:disabled { background: #45475a; color: #6c7086; }

QPushButton#danger {
    background: #f38ba8; color: #1e1e2e; font-weight: bold; border: 0;
}
QPushButton#danger:hover { background: #fab387; }

QRadioButton { color: #cdd6f4; padding: 6px; }
QLabel { color: #cdd6f4; }
QLabel#hint { color: #a6adc8; font-size: 11px; }
QLabel#answer { font-size: 16px; line-height: 1.6; }

QScrollBar:vertical { background: #1e1e2e; width: 10px; }
QScrollBar::handle:vertical { background: #45475a; border-radius: 5px; min-height: 20px; }
"""
)
