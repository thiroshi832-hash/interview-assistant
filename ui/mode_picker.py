"""Startup dialog: same-laptop / helper-laptop (acoustic) / helper-network."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ui.style import STYLE


MODE_SAME = "same"
MODE_HELPER = "helper"
MODE_HELPER_NETWORK = "helper_network"


class ModePicker(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AetherStack Interview Assistant — Choose mode")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        self.mode: str | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("How are you running this?")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        # ── Same laptop ──────────────────────────────────────────────────
        self.rb_same = QRadioButton("Same laptop")
        same_hint = QLabel(
            "This computer runs the interview AND the assistant. The assistant\n"
            "window must NOT be screen-shared. Cleanest audio quality.")
        same_hint.setObjectName("hint")
        same_hint.setWordWrap(True)

        # ── Helper laptop (acoustic) ─────────────────────────────────────
        self.rb_helper = QRadioButton("Helper laptop — acoustic")
        helper_hint = QLabel(
            "Helper laptop sits next to the interview computer and listens via\n"
            "its microphone. Nothing installed on the interview computer.\n"
            "Diarization can be flaky in noisy rooms.")
        helper_hint.setObjectName("hint")
        helper_hint.setWordWrap(True)

        # ── Helper laptop (network) ──────────────────────────────────────
        self.rb_helper_net = QRadioButton("Helper laptop — network (recommended)")
        helper_net_hint = QLabel(
            "Run AetherStack Sender on the interview computer. It streams the\n"
            "mic + system audio to this laptop over your LAN — two clean,\n"
            "perfectly-tagged streams, no acoustic diarization needed.")
        helper_net_hint.setObjectName("hint")
        helper_net_hint.setWordWrap(True)

        self.rb_helper_net.setChecked(True)

        for w in (self.rb_same, same_hint,
                  self.rb_helper, helper_hint,
                  self.rb_helper_net, helper_net_hint):
            layout.addWidget(w)

        layout.addStretch(1)

        ok = QPushButton("Continue")
        ok.setObjectName("primary")
        ok.clicked.connect(self._accept)
        layout.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)

    def _accept(self):
        if self.rb_same.isChecked():
            self.mode = MODE_SAME
        elif self.rb_helper.isChecked():
            self.mode = MODE_HELPER
        else:
            self.mode = MODE_HELPER_NETWORK
        self.accept()
