"""
Heuristic interview-health signal.

Real-time, no extra LLM call. Computes a 0-100 "engagement" score from
observable signals:
  + candidate answers in a healthy length range (~30-150 words)
  - candidate answers are very short (< 15 words) — looks unprepared
  - candidate answers are very long (> 300 words) — rambling
  - many consecutive interviewer turns with no candidate response (silence)
  + reasonably balanced turn-taking (interviewer : candidate word count)

The score is an APPROXIMATE engagement signal, NOT a verdict on whether
you'll get the job — surface it accordingly in the UI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from pipeline.types import Turn


_WORD = re.compile(r"\w+")


@dataclass
class Health:
    score: int          # 0..100
    label: str          # one-word state: low / fair / good / strong
    note: str           # brief reason / tip


def _wc(text: str) -> int:
    return len(_WORD.findall(text))


def compute_health(turns: Sequence[Turn]) -> Health:
    """Score the conversation so far. Returns Health with score + label + note."""

    if not turns:
        return Health(score=70, label="waiting", note="Waiting for the interview to begin.")

    # Look at the most recent ~10 turns to keep this responsive.
    recent = list(turns)[-10:]
    cand_turns = [t for t in recent if t.speaker == "candidate"]
    int_turns = [t for t in recent if t.speaker == "interviewer"]

    if not cand_turns:
        return Health(
            score=55,
            label="listening",
            note="The interviewer has spoken — you haven't responded yet.",
        )

    cand_wcs = [_wc(t.text) for t in cand_turns]
    avg_cand = sum(cand_wcs) / len(cand_wcs)
    avg_int = sum(_wc(t.text) for t in int_turns) / max(len(int_turns), 1)

    score = 70
    notes: list[str] = []

    # Answer length tier
    if avg_cand < 15:
        score -= 20
        notes.append("Answers are very short — try to elaborate.")
    elif avg_cand > 300:
        score -= 12
        notes.append("Answers running long — consider tightening up.")
    elif 30 <= avg_cand <= 180:
        score += 12
        notes.append("Good answer length.")

    # Turn-taking balance — interviewer should not dominate dramatically
    if avg_int > avg_cand * 2.5 and len(int_turns) >= 2:
        score -= 10
        notes.append("Interviewer is doing most of the talking.")

    # Consecutive interviewer turns without a candidate response = silence
    consecutive_int = 0
    max_consecutive = 0
    for t in recent:
        if t.speaker == "interviewer":
            consecutive_int += 1
            max_consecutive = max(max_consecutive, consecutive_int)
        else:
            consecutive_int = 0
    if max_consecutive >= 3:
        score -= 8
        notes.append("Several interviewer turns in a row without your response.")

    # Engagement: more candidate turns = more back-and-forth
    if len(cand_turns) >= 5:
        score += 6

    score = max(0, min(100, score))

    if score >= 75:
        label = "strong"
    elif score >= 55:
        label = "good"
    elif score >= 35:
        label = "fair"
    else:
        label = "low"

    note = "  ".join(notes) if notes else "Steady engagement."
    return Health(score=score, label=label, note=note)
