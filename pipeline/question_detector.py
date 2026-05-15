"""
Decide when an interviewer turn warrants generating an answer.

Triggers in priority order:
  1. Manual hotkey override (force=True).
  2. The turn ends with '?'.
  3. The turn matches a behavioral / technical question opener.
  4. The interviewer has been silent for `question_silence_ms` after a non-question
     statement (handled by the caller passing `silence_after=True`).

Suppression:
  - Candidate is currently speaking → never fire.
  - Same interviewer turn was already answered (deduped by turn ts).
"""
from __future__ import annotations

import re

from pipeline.types import Turn


# Match a question opener at the start of the text OR after a short preamble
# ("Great, let's dive in. background, how would you..."). The leading group
# requires a sentence/clause boundary in front of the opener so we don't match
# "what" mid-sentence in a long monologue.
_QUESTION_OPENERS = re.compile(
    r"(?:^|[.!?,;]\s+|\b(?:so|and|now|given that|with that)[,]?\s+)"
    r"(tell me|walk me|describe|explain|"
    r"how (?:do|did|would|have|has|can|could|are|is|were|might|about)|"
    r"why (?:do|did|would|are|is|were|might)|"
    r"what (?:do|did|would|are|is|was|were|happens|happened|makes|brings|drove|kind|sort|about|if)|"
    r"which (?:of|approach|do|would|did)|"
    r"can you|could you|would you|have you|are you|do you|did you|"
    r"give (?:me )?an example|when (?:did|have|would)|where did|in what)\b",
    re.IGNORECASE,
)


class QuestionDetector:
    def __init__(self):
        self._last_answered_ts: float = 0.0

    def should_answer(
        self,
        turn: Turn,
        *,
        candidate_speaking: bool = False,
        silence_after: bool = False,
        force: bool = False,
    ) -> bool:
        if force:
            self._last_answered_ts = turn.ts
            return True
        if candidate_speaking:
            return False
        if turn.speaker != "interviewer":
            return False
        if turn.ts <= self._last_answered_ts:
            return False

        text = turn.text.strip()
        is_question = text.endswith("?") or bool(_QUESTION_OPENERS.search(text))

        if is_question or silence_after:
            self._last_answered_ts = turn.ts
            return True
        return False
