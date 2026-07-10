"""
AetherStack Interview Assistant — main entry point.

Wires the audio source → VAD → STT → (diarizer + auto-labeler) → transcript →
question detector → Claude → UI.
"""
from __future__ import annotations

import io
import itertools
import sys
import threading
import time
from collections import deque
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
from PySide6.QtCore import QObject, QSharedMemory, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from audio.auto_labeler import AutoLabeler
from audio.diarizer import Diarizer
from audio.dual_stream import DualStreamSource
from audio.network_source import NetworkAudioSource
from audio.single_mic import SingleMicSource
from audio.source import AudioSource
from config import Config, CONFIG_DIR
from llm_provider import make_client
from pipeline.context_summary import ContextSummarizer
from pipeline.echo_filter import is_echo
from pipeline.interview_health import compute_health
from pipeline.question_detector import QuestionDetector
from pipeline.stt_backend import STTEvent
from pipeline.stt_engines import make_stt_backend, effective_stt_engine
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

# Half-duplex echo guard (same-laptop mode). The mic can receive the
# interviewer's audio — acoustically (no headphones) or digitally through a
# virtual-audio mixer (e.g. SteelSeries Sonar), which headphones don't stop.
# That audio transcribes as "candidate". While the interviewer is (or was
# just) audibly speaking, suppress candidate transcription: in this app the
# candidate answers AFTER the interviewer finishes, so real answers fall
# outside this window and survive. This value covers the finalization-timing
# gap between the two independent Deepgram streams (~1s utterance_end).
INTERVIEWER_ECHO_GUARD_SEC = 1.5

# After we generate an answer, stay quiet for this long unless either
#   (a) the regex matches a clear new question, or
#   (b) the user hits the manual hotkey / Regenerate button.
# Prevents the silence net from firing while the candidate is mid-answer.
POST_ANSWER_COOLDOWN_SEC = 25.0

# Minimum words in a non-question interviewer turn before it may arm the
# silence-net trigger. Back-channel acknowledgements while the candidate is
# answering — "That's", "Got it.", "Right", or STT fragments of a murmur —
# must NOT schedule an answer: the timer fires with force=True and would
# replace the answer the candidate is still reading. Real statement-style
# prompts are longer, and real short questions ("Why?", "Could you share…")
# still trigger instantly via the '?' / question-opener path.
MIN_SILENCE_TRIGGER_WORDS = 5

# Bleed loud enough to beat the mic RMS gate arrives GARBLED — its word
# overlap with the interviewer's own transcription often lands just under the
# normal echo threshold ("overhead and query" heard as "or the heavy"). So
# when candidate audio temporally overlaps ACTIVE interviewer audio on the
# other channel, raise the bar: candidate speech must be substantial AND pass
# a looser fuzzy-echo check. Genuine mid-question barge-ins are rare; the
# candidate's real answer starts after the interviewer stops.
OVERLAP_MIN_WORDS = 8            # min words for candidate speech during overlap
OVERLAP_ECHO_THRESHOLD = 0.40    # looser is_echo threshold during overlap
CHANNEL_ACTIVE_RMS = 250.0       # chunk RMS above this = channel audibly active


class App(QObject):
    """Pipeline controller. Emits Qt signals on the main thread."""

    new_turn = Signal(str, str)        # legacy: speaker, text (final-only)
    turn_update = Signal(str, str, bool, bool)  # speaker, text, is_final, replaces_pending
    clear_answer = Signal()
    answer_chunk = Signal(str)
    # Internal: (generation, is_clear, text) from answer workers. Funnelled
    # through a main-thread slot that drops chunks from superseded generations
    # before re-emitting clear_answer / answer_chunk to the UI — see
    # _on_answer_evt for why the filtering must happen on the receiving thread.
    _answer_evt = Signal(int, bool, str)
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
        pass_through = (effective_stt_engine(cfg) == "deepgram")
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
        anchor = (
            np.asarray(cfg.candidate_voice_embedding, dtype=np.float32)
            if cfg.candidate_voice_embedding else None
        )
        try:
            if anchor is not None:
                # Enrolled voice → anchor diarizer in EVERY mode: it labels by
                # voice in helper-acoustic, and powers the voice bleed-filter in
                # same-laptop / helper-network (drop mic audio that's actually
                # the interviewer's voice). Channel-based labeling is unchanged.
                self.diarizer = Diarizer(candidate_anchor=anchor)
            elif mode == MODE_HELPER:
                self.diarizer = Diarizer(candidate_anchor=None)
        except Exception:
            # resemblyzer not importable (slim build). Degrade gracefully.
            self.diarizer = None
            anchor = None
        if mode == MODE_HELPER and anchor is None:
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
        # Helper-acoustic anchor mode: running voice-similarity-to-anchor per
        # Deepgram speaker tag. The tag closest to the enrolled recording is
        # the candidate (relative comparison — see _resolve_by_voice).
        self._tag_sim: dict[str, float] = {}

        # ── threading ─────────────────────────────────────────────────────
        self._answer_thread: threading.Thread | None = None
        self._answer_cancel = threading.Event()
        # Serializes appends to the Q&A log file (workers may overlap briefly).
        self._qa_log_lock = threading.Lock()
        # Per-channel rolling chunk loudness — see _on_speech_chunk.
        self._chunk_rms: dict[str, deque] = {}
        # Monotonic answer generation. next() on itertools.count is atomic in
        # CPython, so concurrent triggers can't mint duplicate generations.
        self._answer_gen_counter = itertools.count(1)
        self._answer_current_gen = 0   # touched ONLY in _on_answer_evt (main thread)
        self._answer_evt.connect(self._on_answer_evt)
        self._candidate_speaking_until = 0.0
        # Wall-clock time until which the interviewer counts as "recently
        # audible" — candidate audio in this window is suppressed as echo.
        self._interviewer_voice_until = 0.0
        self._stop = threading.Event()
        self._silence_timer: threading.Timer | None = None
        self._last_answer_started_at = 0.0
        # Did the candidate speak since the most recent generated answer?
        # If yes, we know they had their turn — cooldown can release.
        self._candidate_spoke_since_answer = True

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, resume: str, job_title: str, jd: str, personal_context: str = "") -> None:
        self.llm.set_context(resume, job_title, jd, personal_context)
        if effective_stt_engine(self.cfg) == "deepgram":
            self.status.emit("Connecting to Deepgram cloud — no local STT model needed.")
        else:
            self.status.emit(
                f"Loading STT ({effective_stt_engine(self.cfg)} / {self.cfg.whisper_model}) — this can take a few seconds…"
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

        # Session separator in the single Q&A log file, so consecutive
        # interviews are distinguishable.
        try:
            with self._qa_log_lock:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with open(CONFIG_DIR / "qa_log.txt", "a", encoding="utf-8") as f:
                    f.write(
                        f"===== session started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"(mode: {self.mode}) =====\n\n"
                    )
        except Exception:
            pass

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
            # 2) Don't fire while ANYONE is mid-utterance (the candidate may be
            #    speaking right now but no Utterance/final has been emitted yet).
            #    is_anyone_speaking() only works with local VAD; _recently_audible
            #    covers Deepgram pass-through (the default), where it stays False.
            if self.segmenter.is_anyone_speaking() or self._recently_audible():
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
        self._launch_answer(deep=deep, style_hint=style_hint)

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
        # Rolling per-channel loudness (~30 ms RMS per chunk, last ~8 s). Feeds
        # the partial bleed gate (which has no PCM of its own) and the
        # "is anyone speaking right now" check the silence-net trigger uses.
        # Single-mic (helper) chunks have speaker=None → bucket them under
        # "_mic" so that check still has a signal there.
        try:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size:
                rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
                key = speaker if speaker is not None else "_mic"
                self._chunk_rms.setdefault(key, deque(maxlen=256)).append((ts, rms))
        except Exception:
            pass
        self.stt.feed(pcm, speaker, ts)

    def _recent_channel_peak(self, speaker: str, ts_start: float, ts_end: float) -> float:
        """
        Loudest ~30 ms chunk RMS on this channel within [ts_start, ts_end]
        (falling back to the last 2 s of the channel if the window is empty —
        STT timestamps and chunk timestamps come from different clocks in some
        backends). 0.0 if the channel has no recorded audio yet.
        """
        dq = self._chunk_rms.get(speaker)
        if not dq:
            return 0.0
        samples = list(dq)
        vals = [r for (t, r) in samples if ts_start - 0.2 <= t <= ts_end + 0.2]
        if not vals:
            cutoff = samples[-1][0] - 2.0
            vals = [r for (t, r) in samples if t >= cutoff]
        return max(vals) if vals else 0.0

    def _recently_audible(self, within_sec: float = 0.4) -> bool:
        """
        True if ANY channel carried speech-level audio in the last `within_sec`
        seconds. This is the cross-engine "is someone talking right now" check:
        the VAD segmenter's is_anyone_speaking() only works in local-VAD mode
        and stays False in Deepgram pass-through (the default), so the
        silence-net trigger needs this to avoid answering over a live speaker.
        """
        now = time.time()
        for dq in list(self._chunk_rms.values()):
            try:
                for ts, rms in reversed(list(dq)):
                    if now - ts > within_sec:
                        break
                    if rms >= CHANNEL_ACTIVE_RMS:
                        return True
            except Exception:
                continue
        return False

    def _channel_active(self, speaker: str, ts_start: float, ts_end: float) -> bool:
        """
        True if this channel carried audible speech within the window. Level
        matters, not mere chunk presence: in Deepgram pass-through mode every
        chunk (including silence) flows through _on_speech_chunk.
        """
        dq = self._chunk_rms.get(speaker)
        if not dq:
            return False
        return any(
            ts_start - 0.3 <= t <= ts_end + 0.3 and r >= CHANNEL_ACTIVE_RMS
            for (t, r) in list(dq)
        )

    def _retract_candidate_partial(self) -> None:
        """
        Erase the candidate's in-progress transcript line after its utterance
        was judged interviewer bleed. Without this, a bleed partial that was
        displayed before the interviewer's own text arrived (the two STT
        connections race at every sentence start) stays behind as an orphan
        italic [CANDIDATE] line.
        """
        if self.transcript.retract_partial("candidate"):
            # Empty text + replaces_pending=True → the UI deletes the line.
            self.turn_update.emit("candidate", "", True, True)

    def _on_speech_close(self, speaker: Optional[str], ts: float) -> None:
        """Segmenter callback — VAD detected end-of-speech for this speaker."""
        self.stt.close_segment(speaker, ts)

    @staticmethod
    def _clip_peak_rms(pcm: bytes, sr: int) -> float:
        """
        Peak loudness of a clip: the 95th-percentile RMS across 100 ms windows.
        Robust to long silences padding the buffer (which would sink a plain
        mean) — real speech shows loud windows, faint bleed does not.
        """
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if arr.size == 0:
            return 0.0
        w = max(1, int(sr * 0.1))
        n = arr.size // w
        if n == 0:
            return float(np.sqrt(np.mean(arr * arr)))
        win = arr[: n * w].reshape(n, w)
        rms = np.sqrt(np.mean(win * win, axis=1))
        return float(np.percentile(rms, 95))

    def _resolve_by_voice(self, event: STTEvent, tag: Optional[str]) -> str:
        """
        Helper-acoustic speaker labeling by voice similarity to the enrolled
        candidate recording — purely RELATIVE, no absolute threshold (the same
        person's similarity varies too much across clips for any fixed cutoff
        to be reliable).

        Track each Deepgram speaker tag's running similarity to the anchor. The
        candidate is whichever tag is CLOSEST to the recording, among the
        speakers heard so far:
          - one voice heard  → it's the closest by default → candidate
          - two+ voices      → the closer one is candidate, the rest interviewer

        Partials / clips too short to embed keep the last known speaker. If the
        cold-start guess is wrong (e.g. the interviewer speaks first, before the
        candidate has been heard), it self-corrects once the candidate speaks;
        the Swap button is the manual override.
        """
        last = getattr(self, "_helper_last_speaker", "interviewer")
        if not event.is_final or not event.pcm:
            return last
        # Deepgram's single-channel diarization routinely collapses two live,
        # in-room voices into ONE tag (the "unique_speakers=['0']" debug line),
        # which forces every turn to the same label. So IGNORE `tag` and do our
        # OWN voice clustering on the finalized clip, then use the enrolled
        # candidate anchor to decide which cluster is the candidate: whichever
        # cluster's running similarity to the anchor is highest is the candidate;
        # the rest are the interviewer. Embed once and reuse for both.
        try:
            emb = self.diarizer.embed(event.pcm, self.cfg.sample_rate)
        except Exception:
            emb = None
        if emb is None:
            return last  # too short/quiet to embed — keep last speaker
        key = self.diarizer.assign_from_embedding(emb)
        sim = float(np.dot(emb, self.diarizer.anchor))
        prev = self._tag_sim.get(key)
        # EMA per cluster: stable, but adapts if a cluster's similarity drifts.
        self._tag_sim[key] = sim if prev is None else 0.6 * prev + 0.4 * sim
        candidate_key = max(self._tag_sim, key=self._tag_sim.get)
        return "candidate" if key == candidate_key else "interviewer"

    def _resolve_by_cluster(self, event: STTEvent, text: str) -> Optional[str]:
        """
        No-enrollment single-mic labeling: cluster the finalized clip locally
        (ignoring Deepgram's unreliable single-channel tag) and label the
        cluster behaviourally via the AutoLabeler (question-rate / length /
        first-to-speak). Returns None for partials or clips too short to embed
        so the caller keeps the last known speaker.
        """
        if not event.is_final or not event.pcm:
            return None
        try:
            cluster = self.diarizer.assign(event.pcm, self.cfg.sample_rate)
        except Exception:
            cluster = None
        if not cluster:
            return None
        return self.auto_labeler.observe(cluster, text, event.ts_end)

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

        # Diagnostic/status events (Deepgram diarize-debug, connection errors)
        # are for the UI status line only — route them there and DON'T store
        # them as transcript turns, or they'd be sent to the LLM as candidate
        # context and pollute answers.
        if event.speaker == "_status":
            self.status.emit(text)
            return

        # ── Resolve speaker ───────────────────────────────────────────────
        if event.speaker is not None:
            speaker = event.speaker
            if getattr(self, "_same_laptop_swap", False):
                speaker = "interviewer" if speaker == "candidate" else "candidate"
        else:
            # Helper-laptop mode — a single mic carries both voices. Deepgram's
            # server-side diarization is unreliable here: on one channel it
            # routinely collapses two in-room voices into ONE tag
            # ("unique_speakers=['0']"), which would force every turn to the
            # same label. So we IGNORE event.diarize_speaker and do our own
            # per-utterance voice work on the finalized clip:
            #   • enrolled   → cluster locally; the enrolled anchor picks which
            #                  cluster is the candidate       (_resolve_by_voice)
            #   • no enroll  → cluster locally; label clusters behaviourally
            #                  via the AutoLabeler          (_resolve_by_cluster)
            # Partials (no PCM) and clips too short to embed keep the last known
            # speaker. Cold-start mistakes self-correct as more speech arrives;
            # the Swap button is the manual override.
            speaker = None
            anchor_mode = self.diarizer is not None and self.diarizer.has_anchor
            if anchor_mode:
                speaker = self._resolve_by_voice(event, event.diarize_speaker)
            elif self.diarizer is not None and self.auto_labeler is not None:
                speaker = self._resolve_by_cluster(event, text)

            if speaker is None:
                speaker = getattr(self, "_helper_last_speaker", "interviewer")

            self._helper_last_speaker = speaker
            if getattr(self, "_same_laptop_swap", False):
                speaker = "interviewer" if speaker == "candidate" else "candidate"

        # Helper-acoustic + enrolled voice: partials carry no PCM, so they
        # can't be voice-fingerprinted. Showing a guessed-speaker partial that
        # the voice-matched final then relabels leaves duplicate lines under
        # both speakers. Only display voice-matched finals in this mode.
        if (event.speaker is None and not event.is_final
                and self.diarizer is not None and self.diarizer.has_anchor):
            return

        # Track interviewer voice activity (partials AND finals) for the echo
        # guard below.
        if speaker == "interviewer":
            self._interviewer_voice_until = time.time() + INTERVIEWER_ECHO_GUARD_SEC

        # ── Candidate/mic bleed suppression (channel modes) ───────────────
        # In same-laptop / helper-network the mic should carry only the
        # candidate, who speaks directly into it (loud). The interviewer's audio
        # leaks in acoustically at LOW volume — measured ~10-15x quieter than
        # direct speech (bleed rms < ~300 vs direct rms in the thousands). A
        # microphone noise gate drops that faint bleed while keeping real
        # speech. `event.speaker is not None` ⇒ channel mode (not helper).
        if speaker == "candidate" and event.speaker is not None:
            if event.is_final and event.pcm:
                if self._clip_peak_rms(event.pcm, self.cfg.sample_rate) < self.cfg.mic_gate_rms:
                    # Peak too faint to be the candidate → interviewer bleed.
                    # Also erase any partial of it already on screen.
                    self._retract_candidate_partial()
                    return
            if not event.is_final:
                # PARTIALS carry no PCM, so gate them on the channel's rolling
                # chunk loudness instead. This is the only guard that can catch
                # bleed at the START of an interviewer sentence: the text-based
                # filters below need the interviewer's own transcription, which
                # races ours through a separate STT connection and usually
                # hasn't arrived yet.
                peak = self._recent_channel_peak("candidate", event.ts_start, event.ts_end)
                if peak and peak < self.cfg.mic_gate_rms:
                    self._retract_candidate_partial()
                    return
            # Backup text-overlap echo filter (catches any louder echo).
            recent = self.transcript.snapshot()[-16:]
            if is_echo(text, recent, candidate_ts=event.ts_end):
                self._retract_candidate_partial()
                return
            if len(text.split()) < 5 and time.time() <= self._interviewer_voice_until:
                self._retract_candidate_partial()
                return
            # Candidate audio that temporally OVERLAPS audible interviewer
            # audio is almost always their voice bleeding into the mic — and
            # loud bleed transcribes garbled, sliding under the normal echo
            # threshold above. Demand length + pass a looser fuzzy-echo check.
            if self._channel_active("interviewer", event.ts_start, event.ts_end):
                if len(text.split()) < OVERLAP_MIN_WORDS or is_echo(
                    text, recent, candidate_ts=event.ts_end,
                    overlap_threshold=OVERLAP_ECHO_THRESHOLD,
                ):
                    self._retract_candidate_partial()
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
            if not in_cooldown and len(text.split()) >= MIN_SILENCE_TRIGGER_WORDS:
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
    def _on_answer_evt(self, gen: int, is_clear: bool, text: str) -> None:
        """
        Main-thread gatekeeper between answer workers and the UI. A cancelled
        worker can still have ONE chunk in flight (it passed its cancel check
        and was preempted before emitting), so emitter-side checks alone can't
        fully prevent a stale fragment landing in a newer answer. All deliveries
        serialize here on the main thread, where generation comparison is
        race-free: a clear from a newer generation advances the bar, and any
        chunk whose generation is below it is dropped.
        """
        if is_clear:
            if gen >= self._answer_current_gen:
                self._answer_current_gen = gen
                self.clear_answer.emit()
            return
        if gen == self._answer_current_gen:
            self.answer_chunk.emit(text)

    def _launch_answer(self, *, deep: bool = False, style_hint: str = "") -> None:
        # cancel anything already running
        if self._answer_thread and self._answer_thread.is_alive():
            self._answer_cancel.set()
            self._answer_thread.join(timeout=0.5)
        # Each worker gets its OWN cancel event, passed as an argument. The old
        # worker is usually blocked inside the LLM network stream for >0.5s, so
        # the join above times out with it still alive; if it then re-read
        # self._answer_cancel it would see this NEW (unset) event and keep
        # streaming — two workers interleaving chunks into the answer box,
        # producing word-salad. With its own event, the .set() above stops it
        # at its next chunk no matter when it wakes up. The generation filter
        # in _on_answer_evt catches the one chunk that can still be in flight.
        cancel = threading.Event()
        self._answer_cancel = cancel
        gen = next(self._answer_gen_counter)
        self._last_answer_started_at = time.time()
        self._candidate_spoke_since_answer = False
        self._answer_thread = threading.Thread(
            target=self._answer_worker,
            args=(gen, deep, style_hint, cancel),
            daemon=True,
        )
        self._answer_thread.start()

    def _answer_worker(self, gen: int, deep: bool, style_hint: str,
                       cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        turns = self.transcript.snapshot()

        # Don't call the LLM when there's genuinely nothing to answer. If turns
        # exist but NONE are the interviewer's (e.g. speaker attribution has
        # mislabeled everything as the candidate), the model would be handed a
        # transcript with no question and reply with a confusing "I haven't
        # heard anything to answer" — which then sits in the answer box. An
        # EMPTY transcript is allowed through: that's the intentional manual
        # "give me a self-intro" case.
        if turns and not any(t.speaker == "interviewer" for t in turns):
            self.status.emit("Waiting for an interviewer question…")
            return

        self._answer_evt.emit(gen, True, "")

        parts: list[str] = []   # everything shown to the user → Q&A log

        try:
            for chunk in self.llm.stream_answer(
                turns, deep=deep, style_hint=style_hint, summary=self._summarizer.summary,
            ):
                if cancel.is_set():
                    break
                self._answer_evt.emit(gen, False, chunk)
                parts.append(chunk)
        except Exception as e:
            if not cancel.is_set():
                self.status.emit(f"LLM error: {e}")
        finally:
            question = next(
                (t.text for t in reversed(turns) if t.speaker == "interviewer"), ""
            )
            self._log_qa(question, "".join(parts), cancelled=cancel.is_set())

    def _log_qa(self, question: str, answer: str, *, cancelled: bool) -> None:
        """
        Append one Q/A pair to the session-spanning log file
        (~/.interview_assistant/qa_log.txt). Best-effort — a full disk or
        locked file must never break the live answer path.
        """
        answer = answer.strip()
        if not answer:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        q = " ".join(question.split()) or "(manual trigger — no interviewer question)"
        note = " [cut off — superseded by a newer answer]" if cancelled else ""
        entry = f"[{stamp}]\nQ: {q}\nA{note}: {answer}\n\n"
        try:
            with self._qa_log_lock:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with open(CONFIG_DIR / "qa_log.txt", "a", encoding="utf-8") as f:
                    f.write(entry)
        except Exception:
            pass


def main() -> int:
    cfg = Config.load()
    qt_app = QApplication(sys.argv)

    # App-wide icon — shows in window titlebars and the Windows taskbar.
    try:
        qt_app.setWindowIcon(QIcon(icon_path("png")))
    except Exception:
        pass

    # Single-instance guard (same pattern as the sender). A second assistant
    # would fight the first for the mic / loopback devices and run a duplicate
    # STT + LLM session. Held in a module global for the process lifetime so
    # it isn't garbage-collected; Windows frees the shared-memory segment
    # automatically when the process dies, even on a crash.
    global _instance_lock
    _instance_lock = QSharedMemory("AetherStackAssistant-singleton")
    if not _instance_lock.create(1):
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("AetherStack Interview Assistant")
        box.setText(
            "AetherStack Interview Assistant is already running.\n\n"
            "Check the taskbar for the existing window."
        )
        box.exec()
        return 0

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
        ve = VoiceEnrollDialog(device_index=cfg.mic_device_index)
        ve.exec()  # user may Skip; either way we proceed
        if ve.embedding is not None:
            cfg.candidate_voice_embedding = [float(x) for x in ve.embedding]
            cfg.save()

    # 3) main window
    win = MainWindow(cfg, mode)
    win.show()

    controller: App | None = None

    # Wire the interview-view buttons ONCE. The interview view persists across
    # interviews, so wiring these inside on_ready would stack a duplicate
    # handler on every new interview (double answers, etc.). The lambdas read
    # whichever `controller` is current, and no-op when there isn't one.
    win.interview_view.answer_now.connect(lambda: controller and controller.force_answer())
    win.interview_view.style_request.connect(lambda hint: controller and controller.force_answer(style_hint=hint))
    win.interview_view.deep_request.connect(lambda: controller and controller.force_answer(deep=True))
    win.interview_view.swap_speakers.connect(lambda: controller and controller.swap_speakers())
    win.interview_view.stop_request.connect(lambda: controller and controller.stop())

    def end_with_eval():
        nonlocal controller
        if controller is None:
            return
        # Pause audio capture immediately so no new audio piles up while the
        # LLM evaluates.
        try:
            controller.segmenter.stop()
            controller.source.stop()
        except Exception:
            pass
        # Modal evaluation dialog; its worker thread calls the LLM with the
        # snapshotted transcript.
        ev_dlg = EvaluationDialog(
            evaluator=lambda: controller.llm.evaluate_interview(
                controller.transcript.snapshot_finalized()
            )
        )
        ev_dlg.exec()
        # Dialog closed → tear down the pipeline and return to the setup
        # screen so the user can review settings or start another interview.
        controller.stop()
        controller = None
        win.show_setup()
    win.interview_view.end_interview_request.connect(end_with_eval)

    def on_ready(resume: str, title: str, jd: str, personal_context: str = ""):
        nonlocal controller
        try:
            controller = App(mode=mode, cfg=cfg)
        except Exception as e:
            QMessageBox.critical(win, "Could not start", str(e))
            return

        # Fresh slate — clear any transcript/answer from a previous interview.
        win.interview_view.reset()

        # Wire signals → UI
        controller.turn_update.connect(win.interview_view.update_turn, Qt.ConnectionType.QueuedConnection)
        controller.clear_answer.connect(win.interview_view.clear_answer, Qt.ConnectionType.QueuedConnection)
        controller.answer_chunk.connect(win.interview_view.append_answer_chunk, Qt.ConnectionType.QueuedConnection)
        controller.status.connect(win.interview_view.set_status, Qt.ConnectionType.QueuedConnection)
        controller.health_update.connect(win.interview_view.set_health, Qt.ConnectionType.QueuedConnection)
        controller.mic_level.connect(win.interview_view.set_mic_level, Qt.ConnectionType.QueuedConnection)

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
