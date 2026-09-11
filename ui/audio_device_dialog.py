"""Audio device picker for same-laptop mode."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from config import Config
from ui.style import STYLE


def _device_label(info: dict[str, Any], host_api_name: str = "") -> str:
    name = str(info.get("name", "Unknown device"))
    index = int(info.get("index", -1))
    rate = int(float(info.get("defaultSampleRate", 0) or 0))
    # Windows exposes the same physical device once per host API (MME,
    # DirectSound, WASAPI, WDM-KS) — identical-looking entries that can
    # behave very differently (some flood non-real-time garbage instead of
    # pacing real audio). Show the API so users can tell them apart, and
    # prefer WASAPI when duplicates exist (see _populate_combo).
    api_suffix = f", {host_api_name}" if host_api_name else ""
    if rate:
        return f"{name}  (#{index}, {rate} Hz{api_suffix})"
    return f"{name}  (#{index}{api_suffix})"


class AudioDeviceDialog(QDialog):
    """
    Pick audio input devices into Config.

    `single_device=True` (helper-laptop acoustic mode): one microphone hears
    both speakers, so only the mic picker is shown. `single_device=False`
    (same-laptop): mic + system loopback pickers.
    """

    def __init__(self, cfg: Config, single_device: bool = False):
        super().__init__()
        self.cfg = cfg
        self._single = single_device
        self._error: str = ""
        self._inputs: list[dict[str, Any]] = []
        self._loopbacks: list[dict[str, Any]] = []

        self.setWindowTitle("Audio device" if single_device else "Audio devices")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Microphone" if single_device else "Same-laptop audio devices")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        if single_device:
            hint_text = (
                "Pick the single microphone that hears the room — both you and the "
                "interviewer. The app tells you apart by your enrolled voice, so only "
                "one input is needed. If the same mic is listed more than once, prefer "
                "the \"Windows WASAPI\" entry."
            )
        else:
            hint_text = (
                "Pick the microphone for your voice and the Windows loopback device "
                "that carries the interviewer's audio. If the same mic is listed more "
                "than once, prefer the \"Windows WASAPI\" entry — some virtual-audio "
                "setups expose broken duplicates under other APIs."
            )
        hint = QLabel(hint_text)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.cb_mic = QComboBox()
        self.cb_loopback = QComboBox() if not single_device else None

        self._load_devices()
        self._populate_combo(
            self.cb_mic,
            self._inputs,
            default_label="Default microphone",
            selected=cfg.mic_device_index,
        )

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Microphone:" if single_device else "Your microphone:", self.cb_mic)
        if not single_device:
            self._populate_combo(
                self.cb_loopback,
                self._loopbacks,
                default_label="Default output loopback",
                selected=cfg.loopback_device_index,
            )
            form.addRow("Interviewer audio:", self.cb_loopback)
        layout.addLayout(form)

        if self._error:
            err = QLabel(self._error)
            err.setObjectName("hint")
            err.setWordWrap(True)
            err.setStyleSheet("color: #f38ba8;")
            layout.addWidget(err)

        if not single_device:
            footer = QLabel(
                "Tip: if Zoom/Teams/Meet is routed to a specific speaker in Windows "
                "Volume Mixer, choose that speaker's loopback here."
            )
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
            import pyaudiowpatch as pyaudio  # type: ignore

            pa = pyaudio.PyAudio()
            try:
                host_api_names: dict[int, str] = {}

                def host_api_name(host_api_index: int) -> str:
                    if host_api_index not in host_api_names:
                        try:
                            host_api_names[host_api_index] = str(
                                pa.get_host_api_info_by_index(host_api_index).get("name", "")
                            )
                        except Exception:
                            host_api_names[host_api_index] = ""
                    return host_api_names[host_api_index]

                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if int(info.get("maxInputChannels", 0) or 0) > 0:
                        info["_host_api_name"] = host_api_name(int(info.get("hostApi", -1)))
                        self._inputs.append(info)
                # The same physical mic is often exposed once per Windows audio
                # host API (MME, DirectSound, WASAPI, WDM-KS) under an
                # identical-looking name. Some of those duplicates don't
                # actually pace audio in real time. WASAPI is the modern,
                # reliable one — list it first so it's the natural pick.
                self._inputs.sort(key=lambda info: info.get("_host_api_name") != "Windows WASAPI")
                try:
                    self._loopbacks = list(pa.get_loopback_device_info_generator())  # type: ignore[attr-defined]
                except Exception:
                    self._loopbacks = []
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
            combo.addItem(_device_label(info, info.get("_host_api_name", "")), userData=index)
            if selected is not None and index == selected:
                combo.setCurrentIndex(combo.count() - 1)

    def _save(self) -> None:
        self.cfg.mic_device_index = self.cb_mic.currentData()
        if not self._single and self.cb_loopback is not None:
            self.cfg.loopback_device_index = self.cb_loopback.currentData()
        self.cfg.save()
        self.accept()
