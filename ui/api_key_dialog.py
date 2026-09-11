"""First-launch dialog: pick provider and paste its API key. Saved to disk."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QVBoxLayout,
)

from ui.style import STYLE


PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"


class ApiKeyDialog(QDialog):
    """
    Pick an LLM provider and paste its API key.

    Exposes after exec():
      .provider  → "anthropic" | "openai"
      .api_key   → the entered key
    """

    def __init__(self, *, current_provider: str = PROVIDER_ANTHROPIC,
                 anthropic_key: str = "", openai_key: str = ""):
        super().__init__()
        self.setWindowTitle("LLM provider")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(620)

        self.provider: str = current_provider
        self.api_key: str = ""
        self._anthropic_key = anthropic_key
        self._openai_key = openai_key

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Choose your LLM provider")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        # ── provider radios ─────────────────────────────────────────
        self.group = QButtonGroup(self)
        self.rb_anthropic = QRadioButton("Anthropic — Claude (recommended for quality)")
        self.rb_openai = QRadioButton("OpenAI — GPT (good speed/cost balance)")
        self.group.addButton(self.rb_anthropic, 0)
        self.group.addButton(self.rb_openai, 1)
        layout.addWidget(self.rb_anthropic)
        layout.addWidget(self.rb_openai)
        (self.rb_openai if current_provider == PROVIDER_OPENAI else self.rb_anthropic).setChecked(True)

        # ── key field + hint ────────────────────────────────────────
        self.lbl = QLabel("")
        layout.addWidget(self.lbl)
        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.input)

        self.hint = QLabel("")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        # ── buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

        # ── wire up provider switching ──────────────────────────────
        self.rb_anthropic.toggled.connect(self._refresh)
        self.rb_openai.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        if self.rb_openai.isChecked():
            self.lbl.setText("Paste your OpenAI API key")
            self.input.setPlaceholderText("sk-...")
            self.input.setText(self._openai_key)
            self.hint.setText(
                "Get one at https://platform.openai.com/api-keys.\n"
                "Saved to %USERPROFILE%\\.interview_assistant\\config.json — only on this machine."
            )
        else:
            self.lbl.setText("Paste your Anthropic API key")
            self.input.setPlaceholderText("sk-ant-...")
            self.input.setText(self._anthropic_key)
            self.hint.setText(
                "Get one at https://console.anthropic.com/settings/keys.\n"
                "Saved to %USERPROFILE%\\.interview_assistant\\config.json — only on this machine."
            )

    def _save(self):
        key = self.input.text().strip()
        if not key:
            return
        self.provider = PROVIDER_OPENAI if self.rb_openai.isChecked() else PROVIDER_ANTHROPIC
        self.api_key = key
        # remember whatever the user just typed in case they flip provider later
        if self.provider == PROVIDER_OPENAI:
            self._openai_key = key
        else:
            self._anthropic_key = key
        self.accept()
