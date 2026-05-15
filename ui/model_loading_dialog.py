"""
Modal dialog shown while STT / VAD / speaker-embedding models load (or download
on first use). Tells the user what's happening, shows download progress for the
big Whisper model, and exits when everything's ready.

For Deepgram + same-laptop mode there's basically nothing to load (no STT model,
no speaker encoder) — only silero-vad which loads in ~100 ms. We skip showing
the dialog entirely in that case so it doesn't flicker.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
)

from config import Config
from pipeline.model_preloader import (
    ModelPreloader, whisper_is_cached, whispercpp_is_cached,
)
from ui.style import STYLE


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


class ModelLoadingDialog(QDialog):
    """
    After exec(): `ok` is True if every model loaded, False if cancelled or
    if any model raised. `error` carries the message in the failure case.
    """

    _status = Signal(str, str)   # step, message
    _bytes_progress = Signal(int)
    _finished = Signal(bool, str)

    def __init__(self, cfg: Config, *, need_resemblyzer: bool, llm_warmup=None):
        super().__init__()
        self.setWindowTitle("Loading models")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(640)
        self.setModal(True)

        self.ok = False
        self.error = ""

        self._cfg = cfg
        self._need_resemblyzer = need_resemblyzer
        self._preloader = ModelPreloader(
            cfg, need_resemblyzer=need_resemblyzer, llm_warmup=llm_warmup,
        )
        self._worker: threading.Thread | None = None
        # Decide if there's actually anything big enough to be worth showing a
        # progress dialog for. With LLM warmup, almost always yes — warmup is
        # a ~1-2 s network round-trip and the user wants visual feedback.
        engine = (cfg.stt_engine or "batch").lower()
        is_cached = (
            engine == "deepgram"
            or (engine == "whispercpp" and whispercpp_is_cached(cfg.whisper_model))
            or (engine == "batch" and whisper_is_cached(cfg.whisper_model))
        )
        self._is_downloading = (engine != "deepgram" and not is_cached)
        # Fast-path only if there's literally nothing to do: Deepgram (no STT
        # model), no resemblyzer (same-laptop mode), AND no LLM warmup.
        self._fast_path = (
            engine == "deepgram"
            and not need_resemblyzer
            and llm_warmup is None
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Preparing for the interview")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        self.lbl_step = QLabel("Starting...")
        self.lbl_step.setWordWrap(True)
        layout.addWidget(self.lbl_step)

        # Indeterminate progress bar; switches to determinate-feeling display
        # via the bytes-downloaded label below
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)   # indeterminate animation
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(14)
        layout.addWidget(self.progress)

        self.lbl_bytes = QLabel("")
        self.lbl_bytes.setObjectName("hint")
        layout.addWidget(self.lbl_bytes)

        hint = QLabel(
            "Models are cached at %USERPROFILE%\\.cache\\huggingface — first run "
            "downloads them; subsequent runs reuse the cache."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

        self._status.connect(self._on_status)
        self._bytes_progress.connect(self._on_bytes)
        self._finished.connect(self._on_finished)

        # If everything is already cached / cloud, run the load synchronously
        # without ever showing the dialog window — avoids a flicker.
        QTimer.singleShot(0, self._kickoff)

    def exec(self):  # type: ignore[override]
        # Fast path: nothing big to load. Run preload in a worker, return as
        # soon as it's done, without ever showing the dialog window.
        if self._fast_path:
            self._kickoff()
            if self._worker is not None:
                self._worker.join(timeout=5.0)
            if self._preloader.error:
                self.ok = False
                self.error = self._preloader.error
                return QDialog.DialogCode.Rejected
            self.ok = True
            return QDialog.DialogCode.Accepted
        return super().exec()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _kickoff(self):
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        self._preloader.run(
            on_status=lambda step, msg: self._status.emit(step, msg),
            on_bytes=lambda n: self._bytes_progress.emit(n),
        )
        if self._preloader.cancel.is_set():
            self._finished.emit(False, "Cancelled.")
        elif self._preloader.error:
            self._finished.emit(False, self._preloader.error)
        else:
            self._finished.emit(True, "")

    def _cancel(self):
        self._preloader.cancel.set()
        self.btn_cancel.setText("Cancelling...")
        self.btn_cancel.setEnabled(False)

    # ── signal handlers (main thread) ─────────────────────────────────────
    @Slot(str, str)
    def _on_status(self, step: str, message: str):
        self.lbl_step.setText(message)
        if step == "stt" and self._is_downloading:
            # Whisper download — bytes label will populate from progress signal
            self.lbl_bytes.setText("Starting download...")
        else:
            self.lbl_bytes.setText("")

    @Slot(int)
    def _on_bytes(self, n: int):
        if n > 0:
            self.lbl_bytes.setText(f"Downloaded {_mb(n)} so far...")

    @Slot(bool, str)
    def _on_finished(self, ok: bool, error: str):
        self.ok = ok
        self.error = error
        if ok:
            self.accept()
        else:
            self.reject()
