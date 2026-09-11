"""
Onboarding dialog: candidate reads a fixed prompt while the app records ~12 s,
then saves a 256-dim voice embedding for speaker recognition during the
interview.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextEdit, QVBoxLayout,
)

from ui.style import STYLE


# Fixed prompt — short and varied phonemes. ~38 words, ~15 s at natural pace,
# comfortably fits in the 18 s recording window.
PROMPT_TEXT = (
    "Hello, I'm setting up the interview assistant. I enjoy solving hard problems "
    "and learning new skills. The quick brown fox jumps over the lazy dog. "
    "Five boxing wizards jump quickly. Okay, that should be enough — I'm done."
)

DURATION_SEC = 18.0


class VoiceEnrollDialog(QDialog):
    """
    After exec(): `embedding` is set to a numpy array on success, or None
    if the user skipped or the recording failed.
    """

    _tick = Signal(float)
    _done = Signal(object)  # np.ndarray | None

    def __init__(self, device_index: Optional[int] = None):
        super().__init__()
        self.setWindowTitle("Voice setup")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(720)

        self.embedding: Optional[np.ndarray] = None
        self._recorder = None
        # Record enrollment from the SAME mic the interview uses (None =
        # system default) so the fingerprint matches what's captured live.
        self._device_index = device_index

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Teach the app your voice")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "Click Start, then read the paragraph below at a natural pace. Recording "
            "lasts about 18 seconds, so don't rush — there's room to breathe between "
            "sentences. You only do this once, unless you change microphones."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.prompt = QTextEdit()
        self.prompt.setReadOnly(True)
        self.prompt.setPlainText(PROMPT_TEXT)
        self.prompt.setStyleSheet("font-size: 14px;")
        self.prompt.setMinimumHeight(180)
        layout.addWidget(self.prompt)

        self.progress = QProgressBar()
        self.progress.setRange(0, int(DURATION_SEC * 10))
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Ready when you are")
        layout.addWidget(self.progress)

        btn_row = QHBoxLayout()
        self.btn_skip = QPushButton("Skip — use auto-detection")
        self.btn_skip.clicked.connect(self.reject)
        self.btn_record = QPushButton("Start recording")
        self.btn_record.setObjectName("primary")
        self.btn_record.setDefault(True)
        self.btn_record.clicked.connect(self._start)
        # "I'm done" — finishes the recording early once you've read the prompt.
        self.btn_stop_early = QPushButton("I'm done")
        self.btn_stop_early.setEnabled(False)
        self.btn_stop_early.clicked.connect(self._stop_early)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_skip)
        btn_row.addWidget(self.btn_stop_early)
        btn_row.addWidget(self.btn_record)
        layout.addLayout(btn_row)

        self._tick.connect(self._on_tick)
        self._done.connect(self._on_done)

    def _stop_early(self):
        if self._recorder is not None:
            try:
                self._recorder.finish_early()
            except Exception:
                pass
            self.btn_stop_early.setEnabled(False)
            self.progress.setFormat("Wrapping up...")

    def _start(self):
        # Pre-flight check: in the slim build, resemblyzer isn't bundled.
        # Recording would succeed but embedding would silently fail. Better
        # to tell the user clearly before they read 18 s of text for nothing.
        try:
            import resemblyzer  # noqa: F401
        except ImportError:
            self.progress.setFormat(
                "Voice enrollment isn't available in this build — Skip to continue."
            )
            self.btn_record.setEnabled(False)
            return
        self.btn_record.setEnabled(False)
        self.btn_skip.setEnabled(False)
        self.btn_stop_early.setEnabled(True)
        self.progress.setFormat("Recording... start reading now")
        # Lazy import to avoid pulling pyaudio at app start
        from audio.voice_enroll import VoiceEnroll
        self._recorder = VoiceEnroll(
            duration_sec=DURATION_SEC,
            on_tick=lambda remaining: self._tick.emit(remaining),
            on_done=lambda emb: self._done.emit(emb),
            device_index=self._device_index,
        )
        self._recorder.start()

    @Slot(float)
    def _on_tick(self, remaining: float):
        used = DURATION_SEC - remaining
        self.progress.setValue(int(used * 10))
        self.progress.setFormat(f"Recording — {remaining:.1f} s remaining")

    @Slot(object)
    def _on_done(self, emb):
        if emb is None:
            self.progress.setFormat("Recording failed — try again")
            self.progress.setValue(0)
            self.btn_record.setEnabled(True)
            self.btn_skip.setEnabled(True)
            self.btn_record.setText("Try again")
            return
        self.embedding = emb
        self.progress.setValue(int(DURATION_SEC * 10))
        self.progress.setFormat("Done — your voice fingerprint is saved")
        QTimer.singleShot(700, self.accept)
