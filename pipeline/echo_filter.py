"""
Drop candidate utterances that are echoes of recent interviewer turns.

Common scenario (same-laptop mode without headphones):
    - The interviewer's voice plays through the candidate's speakers.
    - The candidate's mic picks that audio up.
    - STT produces a "candidate" turn that is nearly identical to the
      recent "interviewer" turn.

Acoustic echo happens IMMEDIATELY — the mic captures the speaker output in
real time. So if a candidate turn's audio ENDED within ~`max_echo_lag_sec` of
the interviewer's audio ending, AND there is high word overlap, it's echo.

If the candidate is *reading the suggested answer aloud* (which contains
question words by nature), the audio ends much later. The time gate keeps
us from dropping that legitimate speech.

Pass `candidate_ts` = the audio-end time of the candidate utterance
(NOT time.time()), and make sure interviewer turns also carry their audio-end
time. Otherwise STT delay confounds the lag check.
"""
from __future__ import annotations

import re
import time
from typing import Sequence

from pipeline.types import Turn


_WORD = re.compile(r"\w+")
_MIN_WORDS = 5         # ignore short utterances ("yes", "right", "okay")


def _word_set(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def is_echo(
    candidate_text: str,
    recent_turns: Sequence[Turn],
    *,
    candidate_ts: float | None = None,
    max_echo_lag_sec: float = 3.0,
    overlap_threshold: float = 0.65,
) -> bool:
    """
    True iff `candidate_text` looks like acoustic echo of an interviewer turn
    whose audio ended within `max_echo_lag_sec` of the candidate's audio.

    `candidate_ts` is the candidate audio's end time. If not provided we fall
    back to time.time() — useful for tests, but in the live pipeline always
    pass utt.ts_end so STT latency doesn't confound the lag check.
    """
    cand_words = _word_set(candidate_text)
    if len(cand_words) < _MIN_WORDS:
        return False

    ref = time.time() if candidate_ts is None else candidate_ts
    # Only interviewer turns whose audio ended within `max_echo_lag_sec` of
    # the candidate's audio end — true acoustic echo lives in this window.
    interviewer_text_parts = [
        t.text for t in recent_turns
        if t.speaker == "interviewer" and 0 <= ref - t.ts <= max_echo_lag_sec
    ]
    if not interviewer_text_parts:
        return False

    int_words = _word_set(" ".join(interviewer_text_parts))
    if not int_words:
        return False

    intersection = cand_words & int_words
    smaller = min(len(cand_words), len(int_words))
    overlap = len(intersection) / max(smaller, 1)
    return overlap >= overlap_threshold
