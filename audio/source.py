"""
AudioSource interface — both dual-stream (same-laptop) and single-mic
(helper-laptop) modes implement this.

Producers push (speaker_or_none, pcm_bytes) onto a queue; consumers
(VAD + STT) read from it. `speaker` is None for single-mic mode where
identity is decided later by the diarizer.
"""
from __future__ import annotations

import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioChunk:
    pcm: bytes              # 16-bit PCM, mono, sample_rate from config
    speaker: Optional[str]  # "candidate" / "interviewer" / None (decide later)
    ts: float


class AudioSource(ABC):
    """Base class. Implementations push AudioChunk onto `queue` from a worker thread."""

    def __init__(self):
        self.queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=1024)
        self._running = False

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    def running(self) -> bool:
        return self._running
