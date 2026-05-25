"""
Background worker that records the candidate's voice from the default mic for
a fixed duration and produces a 256-dim Resemblyzer embedding.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np
import pyaudiowpatch as pyaudio  # type: ignore

from audio._pcm import to_mono_16k_int16


_FRAMES_PER_BUFFER = 1024


class VoiceEnroll(threading.Thread):
    """
    Records audio from the default input device, converts to 16 kHz mono int16,
    and (on completion) computes a normalized voice embedding.

    Callbacks:
      on_tick(remaining_sec: float)            — fired ~10x/s while recording
      on_done(embedding_or_None: np.ndarray | None) — fired once, on the worker
        thread; emb=None means "recording failed / too short / cancelled"
    """

    def __init__(
        self,
        duration_sec: float = 12.0,
        on_tick: Optional[Callable[[float], None]] = None,
        on_done: Optional[Callable[[Optional[np.ndarray]], None]] = None,
    ):
        super().__init__(daemon=True)
        self.duration_sec = float(duration_sec)
        self.on_tick = on_tick
        self.on_done = on_done
        self._stop = threading.Event()
        self._finish_early = threading.Event()

    def cancel(self) -> None:
        """Stop and DISCARD any captured audio (treated as failure)."""
        self._stop.set()

    def finish_early(self) -> None:
        """Stop now but USE the captured audio (treated as success if long enough)."""
        self._finish_early.set()
        self._stop.set()

    def run(self) -> None:
        emb: Optional[np.ndarray] = None
        pa = None
        stream = None
        try:
            pa = pyaudio.PyAudio()
            mic_info = pa.get_default_input_device_info()
            rate = int(mic_info["defaultSampleRate"])
            channels = min(int(mic_info["maxInputChannels"]), 1) or 1
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                frames_per_buffer=_FRAMES_PER_BUFFER,
                input_device_index=int(mic_info["index"]),
            )

            buffer = bytearray()
            t_start = time.time()
            last_tick = 0.0
            while not self._stop.is_set():
                elapsed = time.time() - t_start
                remaining = self.duration_sec - elapsed
                if remaining <= 0:
                    break
                try:
                    raw = stream.read(_FRAMES_PER_BUFFER, exception_on_overflow=False)
                except Exception:
                    break
                pcm = to_mono_16k_int16(
                    raw, sample_rate=rate, channels=channels,
                    sample_format="int16", target_rate=16000,
                )
                buffer.extend(pcm)
                now = time.time()
                if now - last_tick >= 0.1 and self.on_tick:
                    last_tick = now
                    try:
                        self.on_tick(max(0.0, remaining))
                    except Exception:
                        pass

            # Treat a "finish_early" stop as success if we got enough audio,
            # but a plain `cancel()` always discards.
            cancelled_outright = self._stop.is_set() and not self._finish_early.is_set()
            if cancelled_outright or len(buffer) < 16000 * 3 * 2:
                # need at least 3 s of audio for a decent embedding (16k * 3 * 2 bytes)
                emb = None
            else:
                wav = np.frombuffer(bytes(buffer), dtype=np.int16).astype(np.float32) / 32768.0
                try:
                    from resemblyzer import VoiceEncoder  # type: ignore
                    encoder = VoiceEncoder("cpu", verbose=False)
                    raw_emb = encoder.embed_utterance(wav)
                    norm = np.linalg.norm(raw_emb) + 1e-8
                    emb = (raw_emb / norm).astype(np.float32)
                except ImportError:
                    # Slim build: voice enrollment isn't available.
                    emb = None
                except Exception:
                    emb = None
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if pa is not None:
                    pa.terminate()
            except Exception:
                pass

            if self.on_done:
                try:
                    self.on_done(emb)
                except Exception:
                    pass
