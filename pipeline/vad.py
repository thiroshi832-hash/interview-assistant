"""
VAD + segmenter.

Eats raw PCM chunks from an AudioSource and emits *complete utterances* —
contiguous spans of speech bounded by silence. Tracks a separate buffer per
known speaker label (for dual-stream mode) or a single buffer when the speaker
is unknown (single-mic mode).

Uses silero-vad in ONNX mode — no torch required for the VAD step itself.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from queue import Empty
from typing import Callable, Optional

import numpy as np

from audio.source import AudioChunk, AudioSource
from config import Config


@dataclass
class Utterance:
    pcm: bytes              # int16 mono @ cfg.sample_rate
    speaker: Optional[str]  # None if unknown (helper-mode)
    ts_start: float
    ts_end: float
    sample_rate: int


@dataclass
class _SpeakerBuffer:
    pcm_chunks: list[bytes] = field(default_factory=list)
    last_speech_ts: float = 0.0
    speech_started_ts: float = 0.0
    in_speech: bool = False
    samples_since_speech: int = 0


_VAD_WINDOW_SAMPLES = 512   # silero-vad's native chunk size @ 16 kHz


class Segmenter(threading.Thread):
    """
    Background thread. Pulls AudioChunks off `source.queue`, runs VAD, and calls
    `on_utterance(Utterance)` for each complete utterance.
    """

    def __init__(
        self,
        source: AudioSource,
        cfg: Config,
        on_utterance: Optional[Callable[[Utterance], None]] = None,
        on_chunk: Optional[Callable[[Optional[str], bytes, float], None]] = None,
        on_close: Optional[Callable[[Optional[str], float], None]] = None,
        on_level: Optional[Callable[[int], None]] = None,
        pass_through: bool = False,
    ):
        """
        Callbacks (fire any combination):
          on_utterance(Utterance)         — batch mode; full PCM clip at segment close
          on_chunk(speaker, pcm, ts)      — streaming mode; each PCM chunk during speech
          on_close(speaker, ts)           — streaming mode; VAD detected end-of-speech
          on_level(int 0..100)            — overall mic input level, throttled to ~10 Hz

        `pass_through=True` disables local VAD entirely: every audio chunk
        flows to `on_chunk` regardless of speech/silence, and `on_close` /
        `on_utterance` never fire. This is the right mode for backends that
        do their own VAD + endpointing (e.g. Deepgram cloud) — local VAD
        gating would otherwise cut the first ~200 ms off every utterance.
        """
        super().__init__(daemon=True)
        self.source = source
        self.cfg = cfg
        self.on_utterance = on_utterance
        self.on_chunk = on_chunk
        self.on_close = on_close
        self.on_level = on_level
        self.pass_through = pass_through
        self._stop = threading.Event()
        self._buffers: dict[str, _SpeakerBuffer] = {}
        self._vad = None
        # carry-over raw int16 per speaker key so we can chunk to exactly 512 samples
        self._carry: dict[str, np.ndarray] = {}
        # Level-meter throttling — only fire on_level ~10 times per second
        self._last_level_emit: float = 0.0
        # Watchdog — track time of the first audio chunk we ever process
        self.first_chunk_at: Optional[float] = None

    def _ensure_vad(self):
        if self._vad is None:
            from pipeline.onnx_vad import OnnxVAD
            self._vad = OnnxVAD()

    def stop(self) -> None:
        self._stop.set()

    def is_anyone_speaking(self) -> bool:
        """True if any per-speaker buffer is currently inside a speech segment."""
        return any(buf.in_speech for buf in self._buffers.values())

    def run(self) -> None:
        if not self.pass_through:
            self._ensure_vad()
        while not self._stop.is_set():
            try:
                chunk: AudioChunk = self.source.queue.get(timeout=0.1)
            except Empty:
                if not self.pass_through:
                    # also check for stale buffers that timed out into silence
                    self._flush_idle()
                continue

            # Record first-chunk-arrival time + emit level meter (every mode).
            if self.first_chunk_at is None:
                self.first_chunk_at = time.time()
            self._emit_level(chunk)

            if self.pass_through:
                # Forward raw audio with no VAD gating — engine handles VAD itself.
                if self.on_chunk is not None and chunk.pcm:
                    try:
                        self.on_chunk(chunk.speaker, chunk.pcm, chunk.ts)
                    except Exception:
                        pass
                continue
            self._process(chunk)

    def _emit_level(self, chunk: AudioChunk) -> None:
        """Compute RMS of this PCM chunk and emit on_level at ~10 Hz."""
        if self.on_level is None or not chunk.pcm:
            return
        now = time.time()
        if now - self._last_level_emit < 0.1:
            return
        try:
            arr = np.frombuffer(chunk.pcm, dtype=np.int16).astype(np.float32)
            if len(arr) == 0:
                return
            rms = float(np.sqrt(np.mean(arr * arr)))
            # Map RMS to 0..100. Empirically: ~50 = quiet, ~2000 = normal speech,
            # ~8000 = loud. log scale gives a meter that reads well across that range.
            import math
            level = int(min(100, max(0, 20 * math.log10(max(rms, 1.0) / 32.0))))
            self.on_level(level)
            self._last_level_emit = now
        except Exception:
            pass

    # ── per-chunk handling ───────────────────────────────────────────────
    def _process(self, chunk: AudioChunk) -> None:
        key = chunk.speaker or "_unknown"
        buf = self._buffers.setdefault(key, _SpeakerBuffer())
        sr = self.cfg.sample_rate

        # Build a window-aligned int16 array from carry-over + this chunk
        new = np.frombuffer(chunk.pcm, dtype=np.int16)
        prev = self._carry.get(key)
        arr = np.concatenate([prev, new]) if prev is not None and len(prev) else new

        # Process whole 512-sample windows
        n_windows = len(arr) // _VAD_WINDOW_SAMPLES
        consumed = n_windows * _VAD_WINDOW_SAMPLES
        windows = arr[:consumed].reshape(-1, _VAD_WINDOW_SAMPLES)
        self._carry[key] = arr[consumed:]

        for w in windows:
            self._step_vad(buf, w, key, sr, ts=chunk.ts)

        # If buf is mid-utterance, retain the audio for batch mode and push
        # the same bytes to the streaming hook (if any).
        if buf.in_speech and consumed > 0:
            consumed_bytes = arr[:consumed].tobytes()
            buf.pcm_chunks.append(consumed_bytes)
            if self.on_chunk is not None:
                speaker = None if key == "_unknown" else key
                try:
                    self.on_chunk(speaker, consumed_bytes, chunk.ts)
                except Exception:
                    pass

    def _step_vad(self, buf: _SpeakerBuffer, window: np.ndarray, key: str, sr: int, ts: float) -> None:
        # OnnxVAD takes int16 numpy directly — no torch round-trip.
        prob = self._vad(window, sr)
        is_speech = prob > self.cfg.vad_threshold

        if is_speech:
            if not buf.in_speech:
                buf.in_speech = True
                buf.speech_started_ts = ts
                buf.pcm_chunks = []  # reset
            buf.last_speech_ts = ts
            buf.samples_since_speech = 0
        else:
            if buf.in_speech:
                buf.samples_since_speech += _VAD_WINDOW_SAMPLES
                silence_ms = buf.samples_since_speech * 1000 / sr
                if silence_ms >= self.cfg.vad_silence_ms:
                    self._emit(buf, key, sr)

    def _emit(self, buf: _SpeakerBuffer, key: str, sr: int) -> None:
        pcm = b"".join(buf.pcm_chunks)
        duration_ms = len(pcm) * 1000 / (sr * 2)  # 2 bytes per sample
        speaker = None if key == "_unknown" else key
        if duration_ms >= self.cfg.vad_min_speech_ms:
            if self.on_utterance is not None:
                utt = Utterance(
                    pcm=pcm, speaker=speaker,
                    ts_start=buf.speech_started_ts,
                    ts_end=buf.last_speech_ts,
                    sample_rate=sr,
                )
                try:
                    self.on_utterance(utt)
                except Exception:
                    pass
            if self.on_close is not None:
                try:
                    self.on_close(speaker, buf.last_speech_ts)
                except Exception:
                    pass
        # reset
        buf.pcm_chunks = []
        buf.in_speech = False
        buf.samples_since_speech = 0

    def _flush_idle(self) -> None:
        """If a buffer has been mid-speech but no new audio arrived, time it out."""
        now = time.time()
        sr = self.cfg.sample_rate
        for key, buf in self._buffers.items():
            if not buf.in_speech:
                continue
            if (now - buf.last_speech_ts) * 1000 >= self.cfg.vad_silence_ms:
                self._emit(buf, key, sr)
