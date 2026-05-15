"""Startup dialog: same-laptop vs helper-laptop."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ui.style import STYLE


MODE_SAME = "same"
MODE_HELPER = "helper"


class ModePicker(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AetherStack Interview Assistant — Choose mode")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(520)
        self.mode: str | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("How are you running this?")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        self.rb_same = QRadioButton("Same laptop")
        same_hint = QLabel(
            "This computer runs the interview AND the assistant. The assistant\n"
            "window must NOT be screen-shared. Cleanest audio quality.")
        same_hint.setObjectName("hint")
        same_hint.setWordWrap(True)

        self.rb_helper = QRadioButton("Helper laptop (recommended)")
        helper_hint = QLabel(
            "This computer sits next to the interview computer and listens via its\n"
            "microphone. Nothing is installed on the interview computer.")
        helper_hint.setObjectName("hint")
        helper_hint.setWordWrap(True)

        self.rb_helper.setChecked(True)

        for w in (self.rb_same, same_hint, self.rb_helper, helper_hint):
            layout.addWidget(w)

        layout.addStretch(1)

        ok = QPushButton("Continue")
        ok.setObjectName("primary")
        ok.clicked.connect(self._accept)
        layout.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)

    def _accept(self):
        self.mode = MODE_SAME if self.rb_same.isChecked() else MODE_HELPER
        self.accept()
