"""
Online speaker diarizer.

Takes finished utterances (PCM bytes + sample rate), produces a stable cluster ID
("spk_0", "spk_1", ...) per utterance. Uses Resemblyzer voice embeddings.

Light on dependencies: no Hugging Face auth, no online registration. Model is
~150 MB, runs on CPU.
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np


_SIM_THRESHOLD = 0.70           # clustering: same-speaker threshold
_ANCHOR_SIM_THRESHOLD = 0.62    # anchor mode: lower because the enrollment mic / room
                                # may differ slightly from the interview setup


class Diarizer:
    def __init__(self, candidate_anchor: Optional[np.ndarray] = None):
        """
        If `candidate_anchor` is provided (256-dim normalized embedding from
        VoiceEnroll), the diarizer runs in anchor mode and `assign_labeled()`
        returns "candidate" or "interviewer" directly — no clustering, no
        auto-labeling, no warmup.

        If not provided, `assign()` runs the original clustering and returns
        opaque cluster IDs (spk_0, spk_1, ...) for the AutoLabeler to label.
        """
        self._lock = threading.Lock()
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._encoder = None  # lazy-load — model takes a few seconds

        self._anchor: Optional[np.ndarray] = None
        if candidate_anchor is not None:
            a = np.asarray(candidate_anchor, dtype=np.float32)
            n = float(np.linalg.norm(a))
            if n > 0:
                self._anchor = a / n

    @property
    def has_anchor(self) -> bool:
        return self._anchor is not None

    def _ensure_encoder(self):
        if self._encoder is None:
            try:
                from resemblyzer import VoiceEncoder  # type: ignore
            except ImportError as e:
                # Slim build doesn't bundle resemblyzer + torch. Helper-mode
                # voice fingerprinting is unavailable; callers degrade to the
                # behavioural AutoLabeler instead.
                raise RuntimeError(
                    "Voice fingerprinting isn't available in this build "
                    "(resemblyzer not bundled). Helper-laptop mode will use "
                    "behavioural speaker detection instead."
                ) from e
            self._encoder = VoiceEncoder("cpu")

    def _embed(self, pcm_int16: bytes, sample_rate: int) -> Optional[np.ndarray]:
        """Compute a unit-norm embedding from a PCM clip. None if too short."""
        self._ensure_encoder()
        wav = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        if len(wav) < sample_rate // 2:
            return None
        try:
            raw = self._encoder.embed_utterance(wav)  # type: ignore[union-attr]
        except Exception:
            return None
        return raw / (np.linalg.norm(raw) + 1e-8)

    @property
    def anchor(self) -> Optional[np.ndarray]:
        """The unit-norm candidate anchor embedding, or None if not enrolled."""
        return self._anchor

    def embed(self, pcm_int16: bytes, sample_rate: int = 16000) -> Optional[np.ndarray]:
        """Public: unit-norm voice embedding for a clip, or None if too short."""
        return self._embed(pcm_int16, sample_rate)

    def anchor_similarity(self, pcm_int16: bytes, sample_rate: int = 16000) -> Optional[float]:
        """
        Cosine similarity of this clip's voice to the enrolled candidate anchor,
        in [-1, 1]. None if the clip is too short to embed. Callers compare
        similarities RELATIVELY (which speaker is closest to the anchor) rather
        than against a fixed threshold — a different speaker can still exceed
        any absolute cutoff, but the relative gap between speakers is reliable.
        """
        if self._anchor is None:
            raise RuntimeError("Diarizer.anchor_similarity() requires a candidate_anchor")
        emb = self._embed(pcm_int16, sample_rate)
        if emb is None:
            return None
        return float(np.dot(emb, self._anchor))

    def assign_labeled(self, pcm_int16: bytes, sample_rate: int = 16000) -> Optional[str]:
        """
        Anchor mode: directly classify as "candidate" or "interviewer" by
        cosine similarity to the stored candidate embedding.

        Returns None if the clip is too short to embed reliably.
        """
        if self._anchor is None:
            raise RuntimeError("Diarizer.assign_labeled() requires a candidate_anchor")
        emb = self._embed(pcm_int16, sample_rate)
        if emb is None:
            return None
        sim = float(np.dot(emb, self._anchor))
        return "candidate" if sim >= _ANCHOR_SIM_THRESHOLD else "interviewer"

    def assign(self, pcm_int16: bytes, sample_rate: int = 16000) -> Optional[str]:
        """
        Cluster mode: return an opaque cluster ID (spk_0, spk_1, ...).
        Use `assign_labeled()` instead when an anchor is configured.
        """
        emb = self._embed(pcm_int16, sample_rate)
        if emb is None:
            return None

        with self._lock:
            if not self._centroids:
                self._centroids.append(emb)
                self._counts.append(1)
                return "spk_0"

            sims = [float(np.dot(emb, c)) for c in self._centroids]
            best_idx = int(np.argmax(sims))
            if sims[best_idx] >= _SIM_THRESHOLD:
                # running-mean update
                n = self._counts[best_idx]
                self._centroids[best_idx] = (self._centroids[best_idx] * n + emb) / (n + 1)
                self._centroids[best_idx] /= np.linalg.norm(self._centroids[best_idx]) + 1e-8
                self._counts[best_idx] += 1
                return f"spk_{best_idx}"

            # new speaker, cap at 3 to avoid drift
            if len(self._centroids) >= 3:
                # fall back to nearest
                return f"spk_{best_idx}"
            self._centroids.append(emb)
            self._counts.append(1)
            return f"spk_{len(self._centroids) - 1}"

    def reset(self) -> None:
        with self._lock:
            self._centroids.clear()
            self._counts.clear()
