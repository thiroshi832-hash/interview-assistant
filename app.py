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
from audio.network_source import NetworkAudioSource
from audio.single_mic import SingleMicSource
from audio.source import AudioSource
from config import Config
from llm_provider import make_client
from pipeline.context_summary import ContextSummarizer
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
from ui.mode_picker import MODE_HELPER, MODE_HELPER_NETWORK, MODE_SAME, ModePicker
from ui.model_loading_dialog import ModelLoadingDialog
from ui.network_connect_dialog import NetworkConnectDialog
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
    mic_level = Signal(int)                  # 0..100 mic input level

    def __init__(self, mode: str, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.mode = mode

        # ── audio path ────────────────────────────────────────────────────
        # Three sources:
        #   same             → DualStreamSource (local mic + WASAPI loopback)
        #   helper           → SingleMicSource  (single mic acoustically)
        #   helper_network   → NetworkAudioSource (WebSocket to remote sender;
        #                      two cleanly-tagged streams, no diarization)
        if mode == MODE_SAME:
            self.source: AudioSource = DualStreamSource(cfg)
        elif mode == MODE_HELPER_NETWORK:
            self.source = NetworkAudioSource(cfg, cfg.network_host, cfg.network_port)
        else:
            self.source = SingleMicSource(cfg)
        # The STT backend handles its own thread + transcription. The
        # Segmenter does VAD only, feeds chunks during speech, and signals
        # close on silence.
        self.stt = make_stt_backend(cfg)
        # Deepgram does its own VAD + endpointing — bypass our local VAD
        # for ANY mode when it's the chosen engine. In helper mode the
        # DeepgramBackend itself accumulates PCM into _pending_pcm and
        # attaches it to the final STTEvent, so the diarizer still gets a
        # complete clip to embed.
        # (Previously only `same-laptop + deepgram` got pass-through, which
        # meant helper-laptop + deepgram silently dropped quiet acoustic
        # audio that didn't clear the 0.5 VAD probability threshold.)
        pass_through = (cfg.stt_engine == "deepgram")
        self.segmenter = Segmenter(
            self.source, cfg,
            on_chunk=self._on_speech_chunk,
            on_close=self._on_speech_close,
            on_level=lambda lvl: self.mic_level.emit(lvl),
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
            try:
                self.diarizer = Diarizer(candidate_anchor=anchor)
            except Exception:
                # resemblyzer not bundled (slim build). Fall back to the
                # heuristic auto-labeler — works without voice fingerprint.
                self.diarizer = None
                anchor = None
            if anchor is None:
                self.auto_labeler = AutoLabeler(cfg.auto_label_min_utterances)

        # ── language pipeline ─────────────────────────────────────────────
        self.transcript = Transcript()
        self.qdetect = QuestionDetector()
        self.llm = make_client(cfg)
        # Running summary of turns older than the rolling window (see
        # pipeline/context_summary.py) — keeps answers consistent across a
        # long interview without resending the full transcript every time.
        self._summarizer = ContextSummarizer()
        self._summarizing = False

        # Subscribe to transcript updates so we emit `turn_update` whenever
        # the transcript changes (partial or final, replaced or new).
        self.transcript.subscribe(self._on_transcript_change)
        # Diarize-tag watchdog state — used to warn the user if Deepgram
        # diarization is on but never producing per-word speaker IDs.
        self._finals_seen_without_diarize: int = 0
        self._diarize_warning_emitted: bool = False

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

        # Watchdog — if no audio chunks have arrived 5 seconds in, warn loudly.
        # Catches Windows mic privacy blocks, wrong default device, etc.
        def _audio_watchdog():
            time.sleep(5.0)
            if self._stop.is_set():
                return
            if self.segmenter.first_chunk_at is None:
                self.status.emit(
                    "⚠ No audio detected — check (1) Windows Sound settings → "
                    "Input device, (2) Settings → Privacy → Microphone → "
                    "'Allow desktop apps to access your microphone', "
                    "(3) the mic isn't muted in the volume mixer."
                )
        threading.Thread(target=_audio_watchdog, daemon=True).start()

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
        invert + lock so it never re-flips by itself, AND flips the
        Deepgram-diarize-tag map.

        After a manual swap, the mapping is also LOCKED — future anchor
        re-evaluations on short clips won't overwrite the user's choice.
        """
        if self.auto_labeler is not None:
            self.auto_labeler.swap_and_lock()
        # If Deepgram diarization is producing tags, invert their mapping too.
        dg_map = getattr(self, "_dg_speaker_map", None)
        if dg_map:
            self._dg_speaker_map = {
                tag: ("candidate" if label == "interviewer" else "interviewer")
                for tag, label in dg_map.items()
            }
            # User has spoken — their decision is now authoritative.
            self._dg_map_user_locked = True
        # Same-laptop swap toggle stays for the case with no diarizer / no map.
        if self.auto_labeler is None and not dg_map:
            self._same_laptop_swap = not getattr(self, "_same_laptop_swap", False)
        self.status.emit("Labels swapped — future turns use the new mapping (locked).")

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
        else:
            # Helper-laptop mode — single mic stream. Two paths to a label:
            #
            #   Preferred: Deepgram's server-side diarization tags each word
            #   with a stable speaker ID. We map those tags to candidate /
            #   interviewer ONCE per session (via the anchor embedding on the
            #   first sufficient final), then reuse the mapping for every
            #   subsequent partial + final.
            #
            #   Fallback: no diarize tag → fall back to embedding-on-final
            #   (works for whisper.cpp, or Deepgram if diarization is off).
            speaker = None
            tag = event.diarize_speaker

            if tag is not None:
                # Lazy-init the diarize-tag → label map.
                if not hasattr(self, "_dg_speaker_map"):
                    self._dg_speaker_map: dict[str, str] = {}

                user_locked = getattr(self, "_dg_map_user_locked", False)

                if tag in self._dg_speaker_map:
                    speaker = self._dg_speaker_map[tag]
                elif user_locked:
                    # The user manually swapped — they want the existing mapping
                    # preserved. A new tag here is the OTHER role.
                    other = {"candidate", "interviewer"} - set(self._dg_speaker_map.values())
                    speaker = other.pop() if other else "interviewer"
                    self._dg_speaker_map[tag] = speaker
                elif event.is_final and event.pcm and self.diarizer is not None and self.diarizer.has_anchor:
                    # First time we see this tag with a usable final. ONLY lock
                    # the mapping if the audio is long enough to embed reliably
                    # — resemblyzer is unreliable on <1.5s clips. Until then we
                    # use a tentative label but keep re-trying on each final.
                    pcm_bytes = len(event.pcm)
                    audio_seconds = pcm_bytes / (2 * self.cfg.sample_rate)  # int16 mono
                    MIN_LOCK_SECONDS = 1.5
                    try:
                        label = self.diarizer.assign_labeled(event.pcm, self.cfg.sample_rate)
                    except Exception:
                        label = None
                    if label and audio_seconds >= MIN_LOCK_SECONDS:
                        self._dg_speaker_map[tag] = label
                        speaker = label
                        self.status.emit(
                            f"Deepgram speaker '{tag}' identified as {label} "
                            f"(via voice anchor, {audio_seconds:.1f}s of audio)."
                        )
                    elif label:
                        # Tentative — show the label but don't lock.
                        speaker = label
                        self.status.emit(
                            f"Speaker '{tag}' tentative '{label}' "
                            f"({audio_seconds:.1f}s — need ≥{MIN_LOCK_SECONDS}s to lock). "
                            f"Click Swap if wrong; speak more to confirm."
                        )
                # If we have ≥2 known tags and this is a new one, infer it's
                # the OTHER role (someone different from the ones we know).
                if speaker is None and self._dg_speaker_map:
                    known_labels = set(self._dg_speaker_map.values())
                    if "candidate" in known_labels and tag not in self._dg_speaker_map:
                        self._dg_speaker_map[tag] = "interviewer"
                        speaker = "interviewer"
                        self.status.emit(f"Deepgram speaker '{tag}' assumed interviewer.")
                    elif "interviewer" in known_labels and tag not in self._dg_speaker_map:
                        self._dg_speaker_map[tag] = "candidate"
                        speaker = "candidate"
                        self.status.emit(f"Deepgram speaker '{tag}' assumed candidate.")
                # If no anchor exists AND this is our first tag ever, default
                # the first speaker we hear to "interviewer" (typical opening).
                if speaker is None and not self._dg_speaker_map and event.is_final:
                    self._dg_speaker_map[tag] = "interviewer"
                    speaker = "interviewer"
                    self.status.emit(
                        f"Deepgram speaker '{tag}' defaulted to interviewer "
                        f"(no voice enrollment — click Swap if wrong)."
                    )

            # Watchdog: if Deepgram finals keep coming with no speaker tag at
            # all, diarization isn't actually running. Warn the user once.
            if event.is_final and tag is None and self.cfg.stt_engine == "deepgram":
                self._finals_seen_without_diarize += 1
                if (self._finals_seen_without_diarize >= 3
                        and not self._diarize_warning_emitted):
                    self._diarize_warning_emitted = True
                    self.status.emit(
                        "⚠ Deepgram isn't tagging words with speaker IDs — "
                        "diarization may not be supported by the selected model "
                        f"({self.cfg.deepgram_model}) or your account tier. "
                        "Labels will use a best-effort default; use Swap if wrong."
                    )

            # Path 2 (fallback): no diarize tag, or still nothing decided.
            if speaker is None:
                speaker = getattr(self, "_helper_last_speaker", "interviewer")
                if event.is_final and event.pcm and self.diarizer is not None:
                    try:
                        if self.diarizer.has_anchor:
                            label = self.diarizer.assign_labeled(event.pcm, self.cfg.sample_rate)
                        else:
                            assert self.auto_labeler is not None
                            cluster = self.diarizer.assign(event.pcm, self.cfg.sample_rate)
                            label = (self.auto_labeler.observe(cluster, text, event.ts_end)
                                     if cluster else None)
                    except Exception:
                        label = None
                    if label:
                        speaker = label

            self._helper_last_speaker = speaker
            if getattr(self, "_same_laptop_swap", False):
                speaker = "interviewer" if speaker == "candidate" else "candidate"

        # ── Echo filter (finals only) ────────────────────────────────────
        # Compare against snapshot() (includes the interviewer's IN-PROGRESS
        # partial), not snapshot_finalized(). During a long interviewer turn
        # the mic's acoustic echo of the tail often finalizes before the
        # interviewer turn commits (the mic + loopback Deepgram streams commit
        # independently), so the matching interviewer turn isn't finalized yet.
        # The in-progress partial's ts is recent, so it passes the lag gate.
        if event.is_final and speaker == "candidate":
            if is_echo(
                text, self.transcript.snapshot()[-16:],
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

        self._maybe_update_summary()

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

    # ── running context summary ─────────────────────────────────────────────
    def _maybe_update_summary(self) -> None:
        """
        Kick a background summary update once enough finalized turns have
        aged out of the rolling window (`cfg.rolling_turns`) to be worth
        folding in. Batched (cfg.summary_fold_batch) and async so it never
        adds latency to a live answer — see pipeline/context_summary.py.
        """
        if self._summarizing:
            return
        finals = self.transcript.snapshot_finalized()
        if not self._summarizer.should_update(finals, self.cfg.rolling_turns, self.cfg.summary_fold_batch):
            return
        pending = self._summarizer.pending_turns(finals, self.cfg.rolling_turns)
        self._summarizing = True
        threading.Thread(target=self._summary_worker, args=(pending,), daemon=True).start()

    def _summary_worker(self, pending) -> None:
        try:
            updated = self.llm.summarize(self._summarizer.summary, pending)
            self._summarizer.apply_update(pending, updated)
        except Exception:
            # Non-critical — those turns stay pending and get retried once
            # more turns age in past them.
            pass
        finally:
            self._summarizing = False

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
            for chunk in self.llm.stream_answer(
                turns, deep=deep, style_hint=style_hint, summary=self._summarizer.summary,
            ):
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

    # 1a) helper-network mode: ask for the sender's host:port
    if mode == MODE_HELPER_NETWORK:
        nc = NetworkConnectDialog(cfg)
        if nc.exec() != nc.DialogCode.Accepted:
            return 0
        # Values are persisted by the dialog itself; cfg.network_host/port now set.

    # 2) voice enrollment — onboarding step, helper-laptop (acoustic) only.
    # In same-laptop AND helper-network modes, speakers are known by stream/tag;
    # no enrollment needed. We only show the dialog if the user hasn't
    # enrolled before.
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
        controller.mic_level.connect(win.interview_view.set_mic_level, Qt.ConnectionType.QueuedConnection)

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
        # Resemblyzer (voice fingerprint) is only used by acoustic helper mode.
        # Network mode has tagged streams, so no fingerprinting needed.
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
