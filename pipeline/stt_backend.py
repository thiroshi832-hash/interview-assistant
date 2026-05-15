"""
STT backend interface — same shape for all three engines:
    - BatchBackend       (wraps faster-whisper; emits only finals)
    - WhisperCppBackend  (pywhispercpp; emits partials + finals)
    - DeepgramBackend    (cloud; emits partials + finals, lowest latency) — added in Feature 3

Audio comes in as chunks tagged with speaker (or None for helper-mode).
Events come out tagged with `is_final`. Downstream code (echo filter, question
detector, health scorer) only acts on finals — partials are purely cosmetic
for the live transcript view.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class STTEvent:
    """
    A transcription event from any backend.

    `is_final = False` means "best guess so far — may change before final".
    `is_final = True`  means "this won't change; safe to act on (LLM, etc.)".

    `speaker` is None when the source itself doesn't know who's talking
    (helper-laptop single-mic). Downstream resolves it via the diarizer.
    """
    text: str
    speaker: Optional[str]
    ts_start: float
    ts_end: float
    is_final: bool
    pcm: Optional[bytes] = None     # only set on finals; used for speaker ID


class STTBackend(ABC):
    """
    Streaming-shaped STT contract. Even the batch backend implements this —
    it just emits a single `is_final=True` event per utterance, no partials.

    Lifecycle:
        backend.start(on_event)         # register the consumer callback
        backend.feed(chunk)             # called many times per second
        backend.close_segment(speaker)  # called when VAD detects end-of-speech
        backend.stop()                  # tear down
    """

    @abstractmethod
    def start(self, on_event: Callable[[STTEvent], None]) -> None: ...

    @abstractmethod
    def feed(self, pcm: bytes, speaker: Optional[str], ts: float) -> None: ...

    @abstractmethod
    def close_segment(self, speaker: Optional[str], ts: float) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...
