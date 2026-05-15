"""
Speech-to-text wrapper around faster-whisper.

Takes complete utterances (raw PCM int16) and returns transcribed text. Runs on
CPU by default; switch `whisper_compute` to "float16" in config for GPU.
"""
from __future__ import annotations

import threading

import numpy as np

from config import Config


class STT:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore
            # device="auto" would try CUDA first and crash with "cublas64_12.dll
            # not found" on machines without CUDA installed — pin to whatever
            # the user configured (default "cpu"; set "cuda" if you have CUDA).
            try:
                self._model = WhisperModel(
                    self.cfg.whisper_model,
                    device=self.cfg.whisper_device,
                    compute_type=self.cfg.whisper_compute,
                )
            except (RuntimeError, ValueError) as e:
                # Common case on Windows: user picked GPU + float16 but cuDNN
                # isn't installed. Fall back to int8 (works on CUDA without
                # cuDNN, still way faster than CPU).
                if self.cfg.whisper_device == "cuda" and self.cfg.whisper_compute != "int8":
                    self.cfg.whisper_compute = "int8"
                    self.cfg.save()
                    self._model = WhisperModel(
                        self.cfg.whisper_model,
                        device="cuda",
                        compute_type="int8",
                    )
                else:
                    raise

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        self._ensure_model()
        wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(wav) < sample_rate // 2:
            return ""
        with self._lock:
            segments, _ = self._model.transcribe(  # type: ignore[union-attr]
                wav,
                language="en",
                vad_filter=False,                    # we already VAD'd
                beam_size=1,                          # fast path; bump to 5 for accuracy
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            parts = [s.text for s in segments]
        return " ".join(parts).strip()
