"""Pre-interview setup: load resume, enter job title + JD."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from config import Config
from pipeline.license import days_remaining, is_valid_license
from resume_loader import load_resume
from ui.api_key_dialog import ApiKeyDialog
from ui.audio_device_dialog import AudioDeviceDialog
from ui.stt_settings_dialog import SttSettingsDialog
from ui.voice_enroll_dialog import VoiceEnrollDialog


class SetupView(QWidget):
    """Emits `ready(resume_text, job_title, job_description)` when the user clicks Start."""

    ready = Signal(str, str, str, str)   # resume, job_title, job_description, personal_context

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._resume_text: str = ""
        self._resume_filename: str = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        # ── trial / license status (only shown while on a trial) ──
        if not (cfg.license_key and is_valid_license(cfg.license_key)):
            days = days_remaining(cfg.first_run_at)
            self.lbl_trial = QLabel(
                f"Trial: {days} day{'s' if days != 1 else ''} left  ·  "
                "Enter a license key after expiry to keep using the app."
            )
            self.lbl_trial.setObjectName("hint")
            self.lbl_trial.setStyleSheet(
                "color: #f9e2af;" if days <= 7 else "color: #a6adc8;"
            )
            outer.addWidget(self.lbl_trial)

        # ── provider row ──
        provider_row = QHBoxLayout()
        self.lbl_provider = QLabel()
        self.lbl_provider.setObjectName("hint")
        self.btn_provider = QPushButton("Change API…")
        self.btn_provider.clicked.connect(self._change_provider)
        provider_row.addWidget(self.lbl_provider, stretch=1)
        provider_row.addWidget(self.btn_provider)
        outer.addLayout(provider_row)
        self._refresh_provider_label()

        # ── voice enrollment row ──
        voice_row = QHBoxLayout()
        self.lbl_voice = QLabel()
        self.lbl_voice.setObjectName("hint")
        self.btn_voice = QPushButton("Re-record voice…")
        self.btn_voice.clicked.connect(self._reenroll_voice)
        voice_row.addWidget(self.lbl_voice, stretch=1)
        voice_row.addWidget(self.btn_voice)
        outer.addLayout(voice_row)
        self._refresh_voice_label()

        # ── STT settings row ──
        stt_row = QHBoxLayout()
        self.lbl_stt = QLabel()
        self.lbl_stt.setObjectName("hint")
        self.btn_stt = QPushButton("STT settings…")
        self.btn_stt.clicked.connect(self._stt_settings)
        stt_row.addWidget(self.lbl_stt, stretch=1)
        stt_row.addWidget(self.btn_stt)
        outer.addLayout(stt_row)
        self._refresh_stt_label()

        # ── same-laptop audio device settings ──
        audio_row = QHBoxLayout()
        self.lbl_audio = QLabel()
        self.lbl_audio.setObjectName("hint")
        self.btn_audio = QPushButton("Audio devices…")
        self.btn_audio.clicked.connect(self._audio_settings)
        audio_row.addWidget(self.lbl_audio, stretch=1)
        audio_row.addWidget(self.btn_audio)
        outer.addLayout(audio_row)
        self._refresh_audio_label()

        # ── resume ──
        resume_box = QGroupBox("Resume")
        resume_layout = QVBoxLayout(resume_box)
        row = QHBoxLayout()
        self.btn_load = QPushButton("Load resume (PDF, DOCX, TXT)…")
        self.btn_load.clicked.connect(self._load_resume)
        self.lbl_status = QLabel("No resume loaded.")
        self.lbl_status.setObjectName("hint")
        row.addWidget(self.btn_load)
        row.addWidget(self.lbl_status, stretch=1)
        resume_layout.addLayout(row)

        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setPlaceholderText("Parsed resume text will appear here. You can edit it.")
        self.txt_preview.setMinimumHeight(180)
        resume_layout.addWidget(self.txt_preview)

        outer.addWidget(resume_box)

        # Restore the resume from the last session so the user isn't forced
        # to re-upload it every launch.
        if cfg.resume_text:
            self._resume_text = cfg.resume_text
            self._resume_filename = cfg.resume_filename
            self.txt_preview.setPlainText(cfg.resume_text)
            self.lbl_status.setText(
                f"Restored: {cfg.resume_filename or 'previous resume'}  ({len(cfg.resume_text)} chars)"
            )

        # ── job ──
        job_box = QGroupBox("Role")
        form = QFormLayout(job_box)
        self.in_title = QLineEdit()
        self.in_title.setPlaceholderText("e.g. Senior Backend Engineer")
        if cfg.job_title:
            self.in_title.setText(cfg.job_title)
        self.in_jd = QPlainTextEdit()
        self.in_jd.setPlaceholderText("Paste the job description (optional but recommended).")
        self.in_jd.setMinimumHeight(120)
        if cfg.job_description:
            self.in_jd.setPlainText(cfg.job_description)
        form.addRow("Job title:", self.in_title)
        form.addRow("Description:", self.in_jd)
        outer.addWidget(job_box)

        # ── personal context ──
        personal_box = QGroupBox("Personal context (optional)")
        personal_layout = QVBoxLayout(personal_box)
        personal_hint = QLabel(
            "Things the resume doesn't say. The LLM uses this for questions about salary, "
            "start date, hobbies, work-style preferences, etc."
        )
        personal_hint.setObjectName("hint")
        personal_hint.setWordWrap(True)
        personal_layout.addWidget(personal_hint)
        self.in_personal = QPlainTextEdit()
        self.in_personal.setPlaceholderText(
            "e.g.\n"
            "- Salary expectations: $180-220k base\n"
            "- Available to start: 2 weeks notice\n"
            "- Work style: prefer async, focused blocks, hybrid OK\n"
            "- Outside work: open-source ML projects, chess, hiking\n"
            "- Anything else not in the resume the LLM should know"
        )
        self.in_personal.setMinimumHeight(110)
        if cfg.personal_context:
            self.in_personal.setPlainText(cfg.personal_context)
        personal_layout.addWidget(self.in_personal)
        outer.addWidget(personal_box)

        # ── start ──
        self.btn_start = QPushButton("Start interview")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self._start)
        outer.addWidget(self.btn_start)

    def _load_resume(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load resume", "", "Documents (*.pdf *.docx *.txt *.md)"
        )
        if not path:
            return
        try:
            text = load_resume(path)
        except Exception as e:
            QMessageBox.warning(self, "Could not load resume", str(e))
            return
        self._resume_text = text
        self._resume_filename = Path(path).name
        self.txt_preview.setPlainText(text)
        self.lbl_status.setText(f"Loaded: {Path(path).name}  ({len(text)} chars)")

    def _start(self):
        resume = self.txt_preview.toPlainText().strip()
        if not resume:
            QMessageBox.warning(self, "Need a resume", "Load a resume before starting.")
            return
        personal = self.in_personal.toPlainText().strip()
        # Persist so the setup screen restores this session's resume/role/
        # personal context next launch instead of starting blank.
        self.cfg.resume_text = resume
        self.cfg.resume_filename = self._resume_filename
        self.cfg.job_title = self.in_title.text().strip()
        self.cfg.job_description = self.in_jd.toPlainText().strip()
        self.cfg.personal_context = personal
        self.cfg.save()
        self.ready.emit(
            resume,
            self.in_title.text().strip(),
            self.in_jd.toPlainText().strip(),
            personal,
        )

    # ── provider switching ──────────────────────────────────────────────
    def _refresh_provider_label(self) -> None:
        if self.cfg.provider == "openai":
            model = self.cfg.openai_model
            name = f"OpenAI ({model})"
        else:
            model = self.cfg.model
            name = f"Anthropic ({model})"
        has_key = "✓" if self.cfg.active_api_key() else "✗ no key"
        self.lbl_provider.setText(f"LLM: {name}  ·  {has_key}")

    def _refresh_voice_label(self) -> None:
        if self.cfg.candidate_voice_embedding:
            self.lbl_voice.setText(
                "Voice: enrolled (helper-laptop mode uses anchor-based recognition)"
            )
        else:
            self.lbl_voice.setText(
                "Voice: not enrolled — helper-laptop mode falls back to auto-detect"
            )

    def _refresh_stt_label(self) -> None:
        device = "GPU (CUDA)" if self.cfg.whisper_device == "cuda" else "CPU"
        engine_map = {
            "batch": "faster-whisper (batch)",
            "whispercpp": "whisper.cpp (streaming)",
            "deepgram": "Deepgram (cloud)",
        }
        engine = engine_map.get(self.cfg.stt_engine, self.cfg.stt_engine)
        self.lbl_stt.setText(
            f"Speech-to-text: {engine}  ·  {self.cfg.whisper_model}  ·  {device}"
        )

    def _refresh_audio_label(self) -> None:
        mic = (
            f"mic #{self.cfg.mic_device_index}"
            if self.cfg.mic_device_index is not None
            else "default mic"
        )
        loopback = (
            f"loopback #{self.cfg.loopback_device_index}"
            if self.cfg.loopback_device_index is not None
            else "default output loopback"
        )
        self.lbl_audio.setText(f"Same-laptop audio: {mic}  ·  interviewer: {loopback}")

    def _stt_settings(self) -> None:
        dlg = SttSettingsDialog(self.cfg)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._refresh_stt_label()

    def _audio_settings(self) -> None:
        dlg = AudioDeviceDialog(self.cfg)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._refresh_audio_label()

    def _reenroll_voice(self) -> None:
        dlg = VoiceEnrollDialog()
        if dlg.exec() != dlg.DialogCode.Accepted or dlg.embedding is None:
            return
        self.cfg.candidate_voice_embedding = [float(x) for x in dlg.embedding]
        self.cfg.save()
        self._refresh_voice_label()

    def _change_provider(self) -> None:
        dlg = ApiKeyDialog(
            current_provider=self.cfg.provider,
            anthropic_key=self.cfg.anthropic_api_key,
            openai_key=self.cfg.openai_api_key,
        )
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.api_key:
            return
        self.cfg.provider = dlg.provider
        if dlg.provider == "openai":
            self.cfg.openai_api_key = dlg.api_key
        else:
            self.cfg.anthropic_api_key = dlg.api_key
        self.cfg.save()
        self._refresh_provider_label()
