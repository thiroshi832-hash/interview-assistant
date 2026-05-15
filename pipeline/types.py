"""Shared lightweight types."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Turn:
    speaker: str           # "interviewer" or "candidate"
    text: str
    ts: float = 0.0        # audio end time (NOT STT-finish time)
    is_final: bool = True  # False for in-progress streaming partials
