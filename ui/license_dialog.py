"""
Trial-expired dialog. Asks for a license key. Cancel/invalid → caller exits.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from pipeline.license import TRIAL_DAYS, is_valid_license
from ui.style import STYLE


class LicenseDialog(QDialog):
    """
    After exec(): if accepted, `key` is the validated license string.
    If rejected (Quit / closed), the caller should exit the process.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("License key required")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        # Make the dialog non-closable via the X button — must click Quit.
        # (Users tend to close trial dialogs and expect a free pass.)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        self.key: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Your trial has expired")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        body = QLabel(
            f"This is a {TRIAL_DAYS}-day evaluation copy of AetherStack Interview Assistant. "
            f"To continue, enter your license key below. Without a valid key, "
            f"the app will exit."
        )
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)

        self.input = QLineEdit()
        self.input.setPlaceholderText("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
        self.input.textChanged.connect(self._clear_error)
        layout.addWidget(self.input)

        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: #f38ba8; font-weight: bold;")
        layout.addWidget(self.lbl_error)

        btn_row = QHBoxLayout()
        self.btn_quit = QPushButton("Quit")
        self.btn_quit.setObjectName("danger")
        self.btn_quit.clicked.connect(self.reject)
        self.btn_activate = QPushButton("Activate")
        self.btn_activate.setObjectName("primary")
        self.btn_activate.setDefault(True)
        self.btn_activate.clicked.connect(self._activate)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_quit)
        btn_row.addWidget(self.btn_activate)
        layout.addLayout(btn_row)

    def _clear_error(self):
        self.lbl_error.setText("")

    def _activate(self):
        candidate = self.input.text().strip()
        if not candidate:
            self.lbl_error.setText("Enter a license key.")
            return
        if not is_valid_license(candidate):
            self.lbl_error.setText("That license key is not valid.")
            return
        self.key = candidate
        self.accept()
