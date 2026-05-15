"""
AetherStack Interview Assistant — main entry point.

Wires the audio source → VAD → STT → (diarizer + auto-labeler) → transcript →
question detector → Claude → UI.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from typing import Optional

# ── PyInstaller "windowed" mode (--noconsole) sets sys.stdout/stderr to None.
# Any library that tries to write to them (pywhispercpp logs, faster-whisper
# warnings, etc.) crashes with "'NoneType' object has no attribute 'write'".
# Replace with discarding buffers BEFORE anything else imports.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from audio.auto_labeler import AutoLabeler
from audio.diarizer import Diarizer
from audio.dual_stream import DualStreamSource
from audio.single_mic import SingleMicSource
from audio.source import AudioSource
from config import Config
from llm_provider import make_client
from pipeline.echo_filter import is_echo
from pipeline.filler import pick_opener
from pipeline.interview_health import compute_health
from pipeline.question_detector import QuestionDetector
from pipeline.stt_backend import STTEvent
from pipeline.stt_engines import make_stt_backend
from pipeline.transcript import Transcript
from pipeline.vad import Segmenter, Utterance
from paths import icon_path
from pipeline.license import is_valid_license, trial_expired
from ui.evaluation_dialog import EvaluationDialog
from ui.api_key_dialog import ApiKeyDialog
from ui.license_dialog import LicenseDialog
from ui.main_window import MainWindow
from ui.mode_picker import MODE_HELPER, MODE_SAME, ModePicker
from ui.model_loading_dialog import ModelLoadingDialog
from ui.voice_enroll_dialog import VoiceEnrollDialog


# How long to wait after the candidate's last detected speech before we
# consider them "done" and start firing automatic answers again. Set to
# something longer than typical thinking-pause-mid-answer.
CANDIDATE_GRACE_SEC = 4.0

# After we generate an answer, stay quiet for this long unless either
#   (a) the regex matches a clear new question, or
#   (b) the user hits the manual hotkey / Regenerate button.
# Prevents the silence net from firing while the candidate is mid-answer.
POST_ANSWER_COOLDOWN_SEC = 25.0


class App(QObject):
    """Pipeline controller. Emits Qt signals on the main thread."""

    new_turn = Signal(str, str)        # legacy: speaker, text (final-only)
    turn_update = Signal(str, str, bool, bool)  # speaker, text, is_final, replaces_pending
    clear_answer = Signal()
    answer_chunk = Signal(str)
    status = Signal(str)
    health_update = Signal(int, str, str)   # score, label, note

    def __init__(self, mode: str, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.mode = mode

        # ── audio path ────────────────────────────────────────────────────
        self.source: AudioSource = (
            DualStreamSource(cfg) if mode == MODE_SAME else SingleMicSource(cfg)
        )
        # The STT backend handles its own thread + transcription. The
        # Segmenter does VAD only, feeds chunks during speech, and signals
        # close on silence.
        self.stt = make_stt_backend(cfg)
        # Deepgram does its own VAD + endpointing. In same-laptop mode we
        # bypass our VAD entirely (it would otherwise cut the first ~200ms
        # off each utterance). Helper-laptop mode still needs local VAD so
        # the diarizer can embed complete clips.
        pass_through = (cfg.stt_engine == "deepgram" and mode == MODE_SAME)
        self.segmenter = Segmenter(
            self.source, cfg,
            on_chunk=self._on_speech_chunk,
            on_close=self._on_speech_close,
            pass_through=pass_through,
        )

        # ── speaker resolution (helper mode only) ─────────────────────────
        # If the candidate enrolled their voice, run the diarizer in anchor
        # mode: it labels each utterance directly as candidate/interviewer
        # by similarity to the stored embedding — no clustering, no warmup,
        # no AutoLabeler needed.
        self.diarizer = None
        self.auto_labeler = None
        if mode == MODE_HELPER:
            anchor = (
                np.asarray(cfg.candidate_voice_embedding, dtype=np.float32)
                if cfg.candidate_voice_embedding else None
            )
            self.diarizer = Diarizer(candidate_anchor=anchor)
            if anchor is None:
                self.auto_labeler = AutoLabeler(cfg.auto_label_min_utterances)

        # ── language pipeline ─────────────────────────────────────────────
        self.transcript = Transcript()
        self.qdetect = QuestionDetector()
        self.llm = make_client(cfg)

        # Subscribe to transcript updates so we emit `turn_update` whenever
        # the transcript changes (partial or final, replaced or new).
        self.transcript.subscribe(self._on_transcript_change)

        # ── threading ─────────────────────────────────────────────────────
        self._answer_thread: threading.Thread | None = None
        self._answer_cancel = threading.Event()
        self._candidate_speaking_until = 0.0
        self._stop = threading.Event()
        self._silence_timer: threading.Timer | None = None
        self._last_answer_started_at = 0.0
        # Did the candidate speak since the most recent generated answer?
        # If yes, we know they had their turn — cooldown can release.
        self._candidate_spoke_since_answer = True

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, resume: str, job_title: str, jd: str, personal_context: str = "") -> None:
        self.llm.set_context(resume, job_title, jd, personal_context)
        if self.cfg.stt_engine == "deepgram":
            self.status.emit("Connecting to Deepgram cloud — no local STT model needed.")
        else:
            self.status.emit(
                f"Loading STT ({self.cfg.stt_engine} / {self.cfg.whisper_model}) — this can take a few seconds…"
            )

        # Start the STT backend. If the configured engine fails (e.g. Deepgram
        # without internet, missing API key), fall back to whisper.cpp →
        # faster-whisper batch in order.
        try:
            self.stt.start(on_event=self._on_stt_event)
        except Exception as e:
            original = self.cfg.stt_engine
            self.status.emit(f"STT engine '{original}' failed ({e}); falling back…")
            for fallback in ("whispercpp", "batch"):
                if fallback == original:
                    continue
                try:
                    self.cfg.stt_engine = fallback
                    self.stt = make_stt_backend(self.cfg)
                    self.stt.start(on_event=self._on_stt_event)
                    self.status.emit(f"Now using STT engine: {fallback}")
                    break
                except Exception:
                    continue
            else:
                self.status.emit("All STT engines failed to start.")
                raise

        try:
            self.source.start()
        except Exception as e:
            self.status.emit(f"Audio start failed: {e}")
            raise
        self.segmenter.start()
        self.status.emit(f"Listening — mode: {self.mode}")

    def stop(self) -> None:
        self._stop.set()
        self._answer_cancel.set()
        self._cancel_silence_timer()
        try:
            self.segmenter.stop()
        except Exception:
            pass
        try:
            self.source.stop()
        except Exception:
            pass
        try:
            self.stt.stop()
        except Exception:
            pass

    # ── silence-based trigger ────────────────────────────────────────────
    def _arm_silence_timer(self, turn) -> None:
        """
        Fire `_launch_answer` after `question_silence_ms` of inactivity unless
        another turn arrives first. Replaces any prior pending timer.
        """
        self._cancel_silence_timer()
        delay = self.cfg.question_silence_ms / 1000.0

        def fire():
            # 1) Don't talk over the candidate if they're inside the grace
            #    window after their last utterance — they're still answering.
            if time.time() < self._candidate_speaking_until:
                self._arm_silence_timer(turn)
                return
            # 2) Don't fire while ANYONE is mid-utterance (the candidate may
            #    be speaking right now but no Utterance has been emitted yet
            #    because their VAD segment hasn't closed).
            if self.segmenter.is_anyone_speaking():
                self._arm_silence_timer(turn)
                return
            # 3) Cooldown: if we just generated an answer and the candidate
            #    hasn't spoken since, stay quiet — they're probably mid-answer
            #    or about to start.
            in_cooldown = (
                not self._candidate_spoke_since_answer
                and time.time() - self._last_answer_started_at < POST_ANSWER_COOLDOWN_SEC
            )
            if in_cooldown:
                return
            if self.qdetect.should_answer(turn, force=True):
                self._launch_answer()

        self._silence_timer = threading.Timer(delay, fire)
        self._silence_timer.daemon = True
        self._silence_timer.start()

    def _cancel_silence_timer(self) -> None:
        if self._silence_timer is not None:
            try:
                self._silence_timer.cancel()
            except Exception:
                pass
            self._silence_timer = None

    # ── manual triggers (from UI buttons / hotkey) ───────────────────────
    def force_answer(self, *, deep: bool = False, style_hint: str = "") -> None:
        # Manual triggers skip the filler — the user wants a direct answer.
        self._launch_answer(deep=deep, style_hint=style_hint, with_filler=False)

    def swap_speakers(self) -> None:
        """
        User-triggered: invert candidate/interviewer labels and lock them.
        Same-laptop mode (known speakers per stream): just flips the static
        mapping for new utterances. Helper mode: tells the auto-labeler to
        invert + lock so it never re-flips by itself.
        """
        if self.auto_labeler is not None:
            self.auto_labeler.swap_and_lock()
        else:
            # same-laptop mode: flip the source-tagged labels going forward
            self._same_laptop_swap = not getattr(self, "_same_laptop_swap", False)
        self.status.emit("Labels swapped — future turns use the new mapping.")

    # ── audio → text path ────────────────────────────────────────────────
    def _on_speech_chunk(self, speaker: Optional[str], pcm: bytes, ts: float) -> None:
        """Segmenter callback — fired for every PCM chunk during active speech."""
        self.stt.feed(pcm, speaker, ts)

    def _on_speech_close(self, speaker: Optional[str], ts: float) -> None:
        """Segmenter callback — VAD detected end-of-speech for this speaker."""
        self.stt.close_segment(speaker, ts)

    def _on_stt_event(self, event: STTEvent) -> None:
        """
        STT backend callback. Fires from the backend's worker thread for
        every partial AND final transcription. We resolve speaker, run
        echo filter (finals only), update transcript, then run downstream
        triggers (question detection / answer launch — finals only too).
        """
        text = event.text.strip()
        if not text:
            return

        # ── Resolve speaker ───────────────────────────────────────────────
        if event.speaker is not None:
            speaker = event.speaker
            if getattr(self, "_same_laptop_swap", False):
                speaker = "interviewer" if speaker == "candidate" else "candidate"
        elif event.is_final and event.pcm:
            # Helper mode + final: full PCM available, run diarizer.
            assert self.diarizer is not None
            if self.diarizer.has_anchor:
                label = self.diarizer.assign_labeled(event.pcm, self.cfg.sample_rate)
                if label is None:
                    return
                speaker = label
            else:
                assert self.auto_labeler is not None
                cluster = self.diarizer.assign(event.pcm, self.cfg.sample_rate)
                if cluster is None:
                    return
                speaker = self.auto_labeler.observe(cluster, text, event.ts_end)
        else:
            # Partial in helper-laptop mode — can't classify without the
            # full PCM. Skip partials in this mode; user sees the final.
            return

        # ── Echo filter (finals only) ────────────────────────────────────
        if event.is_final and speaker == "candidate":
            if is_echo(
                text, self.transcript.snapshot_finalized()[-12:],
                candidate_ts=event.ts_end,
            ):
                return

        # ── Update transcript (partial or final) ─────────────────────────
        if event.is_final:
            turn = self.transcript.commit(speaker, text, ts=event.ts_end)
        else:
            turn = self.transcript.update_partial(speaker, text, ts=event.ts_end)
        if turn is None:
            return
        # The transcript fires `_on_transcript_change` which emits turn_update
        # to the UI — no need to emit anything else here.

        # ── Downstream stages only run on finals ─────────────────────────
        if not event.is_final:
            return

        self.new_turn.emit(speaker, text)   # legacy signal kept for compatibility

        try:
            h = compute_health(self.transcript.snapshot_finalized())
            self.health_update.emit(h.score, h.label, h.note)
        except Exception:
            pass

        if speaker == "candidate":
            self._candidate_speaking_until = event.ts_end + CANDIDATE_GRACE_SEC
            self._candidate_spoke_since_answer = True
            self._cancel_silence_timer()
            return  # never answer the candidate

        candidate_speaking = time.time() < self._candidate_speaking_until
        if self.qdetect.should_answer(turn, candidate_speaking=candidate_speaking):
            self._cancel_silence_timer()
            self._launch_answer()
        else:
            in_cooldown = (
                not self._candidate_spoke_since_answer
                and time.time() - self._last_answer_started_at < POST_ANSWER_COOLDOWN_SEC
            )
            if not in_cooldown:
                self._arm_silence_timer(turn)

    def _on_transcript_change(self, turn, replaced_partial: bool) -> None:
        """Transcript listener — forward partial+final updates to the UI."""
        self.turn_update.emit(turn.speaker, turn.text, turn.is_final, replaced_partial)

    # ── Claude streaming ─────────────────────────────────────────────────
    def _launch_answer(self, *, deep: bool = False, style_hint: str = "", with_filler: bool = True) -> None:
        # cancel anything already running
        if self._answer_thread and self._answer_thread.is_alive():
            self._answer_cancel.set()
            self._answer_thread.join(timeout=0.5)
        self._answer_cancel = threading.Event()
        self._last_answer_started_at = time.time()
        self._candidate_spoke_since_answer = False
        self._answer_thread = threading.Thread(
            target=self._answer_worker,
            args=(deep, style_hint, with_filler),
            daemon=True,
        )
        self._answer_thread.start()

    def _answer_worker(self, deep: bool, style_hint: str, with_filler: bool) -> None:
        self.clear_answer.emit()

        # Show an opener immediately so the candidate has something to say
        # while the LLM is still generating. Only on automatic triggers — manual
        # Regenerate / Shorter / Deeper should respond directly.
        # pick_opener() may return "" — clean start, no filler this turn.
        if with_filler:
            opener = pick_opener()
            if opener:
                self.answer_chunk.emit(opener + "\n\n")

        turns = self.transcript.snapshot()
        try:
            for chunk in self.llm.stream_answer(turns, deep=deep, style_hint=style_hint):
                if self._answer_cancel.is_set():
                    return
                self.answer_chunk.emit(chunk)
        except Exception as e:
            self.status.emit(f"LLM error: {e}")


def main() -> int:
    cfg = Config.load()
    qt_app = QApplication(sys.argv)

    # App-wide icon — shows in window titlebars and the Windows taskbar.
    try:
        qt_app.setWindowIcon(QIcon(icon_path("png")))
    except Exception:
        pass

    # ── Trial / license gate ───────────────────────────────────────────────
    # Stamp the first run so the 30-day countdown can start.
    if cfg.first_run_at <= 0:
        cfg.first_run_at = time.time()
        cfg.save()
    # If trial has expired and we don't already have a valid license, ask
    # for one. Cancel / invalid → exit cleanly.
    have_valid_key = bool(cfg.license_key) and is_valid_license(cfg.license_key)
    if not have_valid_key and trial_expired(cfg.first_run_at):
        dlg = LicenseDialog()
        if dlg.exec() != dlg.DialogCode.Accepted or not is_valid_license(dlg.key):
            return 1
        cfg.license_key = dlg.key
        cfg.save()

    # First-launch: ask for provider + API key if we don't have one saved.
    # Result is persisted in ~/.interview_assistant/config.json — only done once.
    # (We avoid QMessageBox.critical(None, ...) here because that segfaults
    # inside PyInstaller-bundled PySide6 apps on Windows.)
    if not cfg.active_api_key():
        dlg = ApiKeyDialog(
            current_provider=cfg.provider,
            anthropic_key=cfg.anthropic_api_key,
            openai_key=cfg.openai_api_key,
        )
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.api_key:
            return 0
        cfg.provider = dlg.provider
        if dlg.provider == "openai":
            cfg.openai_api_key = dlg.api_key
        else:
            cfg.anthropic_api_key = dlg.api_key
        cfg.save()

    # 1) pick mode
    picker = ModePicker()
    if picker.exec() != picker.DialogCode.Accepted or picker.mode is None:
        return 0
    mode = picker.mode

    # 2) voice enrollment — onboarding step, helper-laptop mode only.
    # In same-laptop mode, speakers are known by audio stream; no enrollment
    # needed. We only show the dialog if the user hasn't enrolled before.
    if mode == MODE_HELPER and not cfg.candidate_voice_embedding:
        ve = VoiceEnrollDialog()
        ve.exec()  # user may Skip; either way we proceed
        if ve.embedding is not None:
            cfg.candidate_voice_embedding = [float(x) for x in ve.embedding]
            cfg.save()

    # 3) main window
    win = MainWindow(cfg)
    win.show()

    controller: App | None = None

    def on_ready(resume: str, title: str, jd: str, personal_context: str = ""):
        nonlocal controller
        try:
            controller = App(mode=mode, cfg=cfg)
        except Exception as e:
            QMessageBox.critical(win, "Could not start", str(e))
            return

        # Wire signals → UI
        controller.turn_update.connect(win.interview_view.update_turn, Qt.ConnectionType.QueuedConnection)
        controller.clear_answer.connect(win.interview_view.clear_answer, Qt.ConnectionType.QueuedConnection)
        controller.answer_chunk.connect(win.interview_view.append_answer_chunk, Qt.ConnectionType.QueuedConnection)
        controller.status.connect(win.interview_view.set_status, Qt.ConnectionType.QueuedConnection)
        controller.health_update.connect(win.interview_view.set_health, Qt.ConnectionType.QueuedConnection)

        # Wire UI buttons → controller
        win.interview_view.answer_now.connect(lambda: controller and controller.force_answer())
        win.interview_view.style_request.connect(lambda hint: controller and controller.force_answer(style_hint=hint))
        win.interview_view.deep_request.connect(lambda: controller and controller.force_answer(deep=True))
        win.interview_view.swap_speakers.connect(lambda: controller and controller.swap_speakers())
        win.interview_view.stop_request.connect(lambda: controller and controller.stop())

        def end_with_eval():
            if controller is None:
                return
            # Pause audio capture immediately so no new audio piles up while
            # the LLM evaluates.
            try:
                controller.segmenter.stop()
                controller.source.stop()
            except Exception:
                pass
            # Open the modal evaluation dialog; the worker thread inside it
            # calls the LLM with the snapshotted transcript.
            ev_dlg = EvaluationDialog(
                evaluator=lambda: controller.llm.evaluate_interview(
                    controller.transcript.snapshot_finalized()
                )
            )
            ev_dlg.exec()
            # User closed the dialog → tear down the rest of the pipeline.
            controller.stop()
        win.interview_view.end_interview_request.connect(end_with_eval)

        # Inject the LLM context BEFORE preloading so the preloader's warmup
        # step caches the right prefix (resume + role + personal_context).
        controller.llm.set_context(resume, title, jd, personal_context)

        # Preload heavy models BEFORE starting audio capture so the first
        # interviewer utterance doesn't sit in a queue while gigabytes
        # download silently. Also pre-warms the LLM HTTPS connection + cache
        # so the first answer streams as fast as the rest.
        need_resemblyzer = (mode == MODE_HELPER)
        loading = ModelLoadingDialog(
            cfg,
            need_resemblyzer=need_resemblyzer,
            llm_warmup=controller.llm.warmup,
        )
        loading.exec()
        if not loading.ok:
            if loading.error:
                QMessageBox.critical(win, "Could not load models", loading.error)
            return

        win.show_interview()
        try:
            controller.start(resume, title, jd, personal_context)
        except Exception as e:
            QMessageBox.critical(win, "Could not start audio", str(e))

    win.setup_view.ready.connect(on_ready)

    rc = qt_app.exec()
    if controller is not None:
        controller.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
