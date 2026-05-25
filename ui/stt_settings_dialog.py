"""
Speech-to-text settings: pick the Whisper model and run device (CPU vs GPU).
Auto-detects CUDA availability and disables the GPU option if not usable.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from config import Config
from ui.style import STYLE


# (label shown in UI, model id, approx download size, approx speed on CPU)
MODELS = [
    ("Tiny  (fastest, ~5× real-time, ~39 MB)",   "tiny.en",   "39 MB"),
    ("Base  (fast, ~2× real-time, ~74 MB)",      "base.en",   "74 MB"),
    ("Small (balanced, ~0.5× real-time, ~244 MB)", "small.en", "244 MB"),
    ("Medium (accurate, slow on CPU, ~769 MB)",  "medium.en", "769 MB"),
    ("Large v3 (best quality, GPU recommended, ~1.5 GB)", "large-v3", "1.5 GB"),
]


def _is_cuda_available() -> bool:
    # onnxruntime exposes available execution providers — if CUDA is in there,
    # the user has cuDNN + CUDA installed. Avoids pulling in ctranslate2 just
    # for a probe.
    try:
        import onnxruntime as ort  # type: ignore
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


class SttSettingsDialog(QDialog):
    """
    After exec(), the dialog mutates the passed-in Config in-place if accepted.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._cuda = _is_cuda_available()

        self.setWindowTitle("Speech-to-text settings")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Speech-to-text settings")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "Bigger model = better accuracy, slower transcription. NVIDIA GPU "
            "with CUDA 12 + cuDNN cuts transcription latency to near-instant."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        # ── engine dropdown ─────────────────────────────────────────────
        self.cb_engine = QComboBox()
        self.cb_engine.addItem("whisper.cpp (streaming — on-device, words during speech)", userData="whispercpp")
        self.cb_engine.addItem("Deepgram (cloud streaming — lowest latency, needs API key)", userData="deepgram")
        # Migrate legacy saved value
        current = cfg.stt_engine if cfg.stt_engine != "batch" else "whispercpp"
        for i in range(self.cb_engine.count()):
            if self.cb_engine.itemData(i) == current:
                self.cb_engine.setCurrentIndex(i)
                break
        self.cb_engine.currentIndexChanged.connect(self._on_engine_change)
        form.addRow("Engine:", self.cb_engine)

        # ── Deepgram API key (visible only when Deepgram is selected) ───
        self.in_deepgram_key = QLineEdit()
        self.in_deepgram_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.in_deepgram_key.setPlaceholderText("dg_... (get at console.deepgram.com)")
        self.in_deepgram_key.setText(cfg.deepgram_api_key)
        self.lbl_deepgram_key = QLabel("Deepgram API key:")
        form.addRow(self.lbl_deepgram_key, self.in_deepgram_key)

        # ── model dropdown ──────────────────────────────────────────────
        self.cb_model = QComboBox()
        for label, model_id, _size in MODELS:
            self.cb_model.addItem(label, userData=model_id)
        # Select current
        for i in range(self.cb_model.count()):
            if self.cb_model.itemData(i) == cfg.whisper_model:
                self.cb_model.setCurrentIndex(i)
                break
        form.addRow("Model:", self.cb_model)

        # ── device dropdown ─────────────────────────────────────────────
        self.cb_device = QComboBox()
        self.cb_device.addItem("CPU (works everywhere)", userData="cpu")
        gpu_label = ("NVIDIA GPU (CUDA — fastest)"
                     if self._cuda
                     else "NVIDIA GPU — not detected on this machine")
        self.cb_device.addItem(gpu_label, userData="cuda")
        if not self._cuda:
            # disable the GPU option
            from PySide6.QtCore import QSize
            model = self.cb_device.model()
            item = model.item(1)
            if item is not None:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

        # Select current
        for i in range(self.cb_device.count()):
            if self.cb_device.itemData(i) == cfg.whisper_device:
                self.cb_device.setCurrentIndex(i)
                break
        form.addRow("Run on:", self.cb_device)

        layout.addLayout(form)

        # ── footer ──
        footer = QLabel(
            f"CUDA detected on this machine: {'yes' if self._cuda else 'no'}.\n"
            "Model files download to %USERPROFILE%\\.cache\\huggingface\\ on first use."
        )
        footer.setObjectName("hint")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        # ── buttons ──
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

        # Sync the Deepgram field visibility with the current engine choice
        self._on_engine_change()

    def _on_engine_change(self):
        is_dg = self.cb_engine.currentData() == "deepgram"
        self.lbl_deepgram_key.setVisible(is_dg)
        self.in_deepgram_key.setVisible(is_dg)

    def _save(self):
        self.cfg.stt_engine = self.cb_engine.currentData()
        self.cfg.whisper_model = self.cb_model.currentData()
        self.cfg.whisper_device = self.cb_device.currentData()
        # Save Deepgram key whenever the field has content, even if user is
        # currently viewing a different engine — so toggling engines later
        # keeps the key around.
        dg_key = self.in_deepgram_key.text().strip()
        if dg_key:
            self.cfg.deepgram_api_key = dg_key
        # GPU default is `int8` (universal — works without cuDNN). For users
        # with cuDNN installed, `float16` is faster — they can set it manually
        # in config.json.
        if self.cfg.whisper_device == "cuda":
            # Don't downgrade if the user already picked a fancier compute type
            if self.cfg.whisper_compute not in ("int8", "int8_float16", "float16", "float32"):
                self.cfg.whisper_compute = "int8"
        else:
            self.cfg.whisper_compute = "int8"
        self.cfg.save()
        self.accept()
