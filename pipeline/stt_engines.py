"""
Concrete STT backends.

BatchBackend     — wraps faster-whisper; one final event per utterance.
WhisperCppBackend — whisper.cpp via pywhispercpp; partials every ~600 ms
                    plus a final when the speech segment closes.

Both backends do their transcription work on their OWN worker thread,
so `feed()` and `close_segment()` are non-blocking — the Segmenter never
gets backed up by slow CPU inference.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

from config import Config
from pipeline.stt_backend import STTBackend, STTEvent


# ── BatchBackend ─────────────────────────────────────────────────────────────


class BatchBackend(STTBackend):
    """
    Existing path. Buffers per-speaker, transcribes the whole clip with
    faster-whisper on close_segment, emits a single final.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._model_lock = threading.Lock()
        self._buffers: dict[Optional[str], bytearray] = {}
        self._segment_start: dict[Optional[str], float] = {}
        self._on_event: Optional[Callable[[STTEvent], None]] = None
        self._jobs: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore
            try:
                self._model = WhisperModel(
                    self.cfg.whisper_model,
                    device=self.cfg.whisper_device,
                    compute_type=self.cfg.whisper_compute,
                )
            except (RuntimeError, ValueError):
                if self.cfg.whisper_device == "cuda" and self.cfg.whisper_compute != "int8":
                    self.cfg.whisper_compute = "int8"
                    self.cfg.save()
                    self._model = WhisperModel(
                        self.cfg.whisper_model, device="cuda", compute_type="int8",
                    )
                else:
                    raise

    def start(self, on_event):
        self._on_event = on_event
        self._ensure_model()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def feed(self, pcm: bytes, speaker, ts: float) -> None:
        buf = self._buffers.setdefault(speaker, bytearray())
        if not buf:
            self._segment_start[speaker] = ts
        buf.extend(pcm)

    def close_segment(self, speaker, ts: float) -> None:
        buf = self._buffers.pop(speaker, None)
        ts_start = self._segment_start.pop(speaker, ts)
        if not buf:
            return
        self._jobs.put((speaker, ts_start, ts, bytes(buf), True))

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                speaker, ts_start, ts_end, pcm, is_final = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            text = self._transcribe(pcm)
            if text and self._on_event:
                self._on_event(STTEvent(
                    text=text, speaker=speaker,
                    ts_start=ts_start, ts_end=ts_end,
                    is_final=is_final, pcm=pcm if is_final else None,
                ))

    def _transcribe(self, pcm: bytes) -> str:
        wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(wav) < 16000 // 2:
            return ""
        with self._model_lock:
            segments, _ = self._model.transcribe(  # type: ignore[union-attr]
                wav, language="en", vad_filter=False, beam_size=1,
                condition_on_previous_text=False, without_timestamps=True,
            )
            return " ".join(s.text for s in segments).strip()

    def stop(self) -> None:
        self._stop_event.set()
        self._buffers.clear()


# ── WhisperCppBackend ────────────────────────────────────────────────────────


_PARTIAL_INTERVAL_SEC = 0.6      # how often to emit a partial during speech
_MIN_BUFFER_SEC = 1.2            # don't transcribe until we have at least this much


class WhisperCppBackend(STTBackend):
    """
    Streaming via whisper.cpp (pywhispercpp).

    pywhispercpp doesn't expose a native streaming API, so we approximate
    it: every ~600 ms during speech, transcribe the growing audio buffer
    and emit a partial. On segment close, transcribe once more and emit a
    final. Worker thread handles transcription; feed() never blocks.

    Trade: occasional partial back-correction (the words may change
    slightly before final). Same behaviour Live Caption has.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._model_lock = threading.Lock()
        self._buffers: dict[Optional[str], bytearray] = {}
        self._segment_start: dict[Optional[str], float] = {}
        self._last_partial_at: dict[Optional[str], float] = {}
        self._on_event: Optional[Callable[[STTEvent], None]] = None
        # Per-job tuple: (speaker, ts_start, ts_end, pcm, is_final, generation)
        self._jobs: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        # Increments on every new partial job; the worker drops jobs whose
        # generation is stale (a newer partial for the same speaker is queued).
        self._gen_counter: dict[Optional[str], int] = {}

    def _ensure_model(self):
        if self._model is None:
            from pywhispercpp.model import Model  # type: ignore
            # pywhispercpp later calls `.write()` on `redirect_whispercpp_logs_to`
            # — `False` and `None` both break. Give it a real discarding buffer.
            import io as _io
            self._model = Model(
                self.cfg.whisper_model,
                print_realtime=False,
                print_progress=False,
                redirect_whispercpp_logs_to=_io.StringIO(),
            )

    def start(self, on_event):
        self._on_event = on_event
        self._ensure_model()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def feed(self, pcm: bytes, speaker, ts: float) -> None:
        buf = self._buffers.setdefault(speaker, bytearray())
        if not buf:
            self._segment_start[speaker] = ts
            self._last_partial_at[speaker] = ts
        buf.extend(pcm)

        secs_buffered = len(buf) / (16000 * 2)
        secs_since_partial = ts - self._last_partial_at.get(speaker, 0.0)
        if secs_buffered >= _MIN_BUFFER_SEC and secs_since_partial >= _PARTIAL_INTERVAL_SEC:
            self._gen_counter[speaker] = self._gen_counter.get(speaker, 0) + 1
            gen = self._gen_counter[speaker]
            self._jobs.put((
                speaker,
                self._segment_start[speaker],
                ts,
                bytes(buf),
                False,   # is_final
                gen,
            ))
            self._last_partial_at[speaker] = ts

    def close_segment(self, speaker, ts: float) -> None:
        buf = self._buffers.pop(speaker, None)
        ts_start = self._segment_start.pop(speaker, ts)
        self._last_partial_at.pop(speaker, None)
        if not buf:
            return
        # Bump generation so any pending partials for this speaker are skipped.
        self._gen_counter[speaker] = self._gen_counter.get(speaker, 0) + 1
        gen = self._gen_counter[speaker]
        self._jobs.put((speaker, ts_start, ts, bytes(buf), True, gen))

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                speaker, ts_start, ts_end, pcm, is_final, gen = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            # Skip partials that have been superseded by a newer job for the
            # same speaker. Finals always run.
            if not is_final and gen < self._gen_counter.get(speaker, 0):
                continue
            text = self._transcribe(pcm)
            if text and self._on_event:
                self._on_event(STTEvent(
                    text=text, speaker=speaker,
                    ts_start=ts_start, ts_end=ts_end,
                    is_final=is_final, pcm=pcm if is_final else None,
                ))

    def _transcribe(self, pcm: bytes) -> str:
        wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(wav) < 16000 // 2:
            return ""
        with self._model_lock:
            try:
                segments = self._model.transcribe(wav, language="en", n_threads=4)
            except Exception:
                return ""
        return " ".join(s.text for s in segments).strip()

    def stop(self) -> None:
        self._stop_event.set()
        self._buffers.clear()


# ── Factory ──────────────────────────────────────────────────────────────────


def make_stt_backend(cfg: Config) -> STTBackend:
    engine = (cfg.stt_engine or "batch").lower()
    if engine in ("deepgram", "cloud"):
        from pipeline.deepgram_stt import DeepgramBackend
        return DeepgramBackend(cfg)
    if engine in ("whispercpp", "whisper.cpp", "streaming", "whispercpp_streaming"):
        return WhisperCppBackend(cfg)
    return BatchBackend(cfg)
