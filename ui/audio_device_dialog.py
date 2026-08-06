"""Audio device picker for same-laptop mode."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from audio.pyaudio_compat import get_loopback_devices, loopback_hint, loopback_footer_hint
from config import Config
from ui.style import STYLE


def _device_label(info: dict[str, Any]) -> str:
    name = str(info.get("name", "Unknown device"))
    index = int(info.get("index", -1))
    rate = int(float(info.get("defaultSampleRate", 0) or 0))
    if rate:
        return f"{name}  (#{index}, {rate} Hz)"
    return f"{name}  (#{index})"


class AudioDeviceDialog(QDialog):
    """Persist same-laptop mic and loopback device choices into Config."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._error: str = ""
        self._inputs: list[dict[str, Any]] = []
        self._loopbacks: list[dict[str, Any]] = []

        self.setWindowTitle("Audio devices")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Same-laptop audio devices")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(loopback_hint())
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.cb_mic = QComboBox()
        self.cb_loopback = QComboBox()

        self._load_devices()
        self._populate_combo(
            self.cb_mic,
            self._inputs,
            default_label="Default microphone",
            selected=cfg.mic_device_index,
        )
        self._populate_combo(
            self.cb_loopback,
            self._loopbacks,
            default_label="Default output loopback",
            selected=cfg.loopback_device_index,
        )

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Your microphone:", self.cb_mic)
        form.addRow("Interviewer audio:", self.cb_loopback)
        layout.addLayout(form)

        if self._error:
            err = QLabel(self._error)
            err.setObjectName("hint")
            err.setWordWrap(True)
            err.setStyleSheet("color: #f38ba8;")
            layout.addWidget(err)

        footer = QLabel(loopback_footer_hint())
        footer.setObjectName("hint")
        footer.setWordWrap(True)
        layout.addWidget(footer)

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

    def _load_devices(self) -> None:
        try:
            from audio.pyaudio_compat import pyaudio as pa_mod

            pa = pa_mod.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if int(info.get("maxInputChannels", 0) or 0) > 0:
                        self._inputs.append(info)
                self._loopbacks = get_loopback_devices(pa)
            finally:
                pa.terminate()
        except Exception as e:
            self._error = f"Could not list audio devices: {e}"

    def _populate_combo(
        self,
        combo: QComboBox,
        devices: list[dict[str, Any]],
        *,
        default_label: str,
        selected: int | None,
    ) -> None:
        combo.addItem(default_label, userData=None)
        for info in devices:
            index = int(info.get("index", -1))
            combo.addItem(_device_label(info), userData=index)
            if selected is not None and index == selected:
                combo.setCurrentIndex(combo.count() - 1)

    def _save(self) -> None:
        self.cfg.mic_device_index = self.cb_mic.currentData()
        self.cfg.loopback_device_index = self.cb_loopback.currentData()
        self.cfg.save()
        self.accept()
