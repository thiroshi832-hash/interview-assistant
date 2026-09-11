"""
Running summary as a SAFETY VALVE for the full-transcript memory model.

The whole conversation is sent verbatim on every answer (a full interview fits
the model's context, and the resume/role prefix is prompt-cached). We only
compress when the verbatim transcript grows past a token budget: the OLDEST
turns are folded into a short running summary via one cheap LLM call, always
leaving at least `min_verbatim` recent turns verbatim. For most interviews the
valve never fires and nothing is compressed — the model sees the exact words.

Turns are matched by `ts` rather than list index/count, so eviction from the
transcript's bounded deque (see pipeline/transcript.py, max_turns=200) can
never cause a turn to be skipped or re-folded.
"""
from __future__ import annotations

from typing import Sequence

from pipeline.types import Turn


SUMMARY_MAX_WORDS = 250


def _est_tokens(turns: Sequence[Turn]) -> int:
    """Cheap token estimate (~4 chars/token) — avoids a tokenizer dependency."""
    return sum(len(t.text) for t in turns) // 4

UPDATE_PROMPT = """You maintain a running memory of an ongoing job interview for an answer-assistant that only sees the last few exchanges verbatim. Your summary is how it stays consistent with what's already been said.

Existing summary so far:
{prior_summary}

New exchanges to fold in (oldest first):
{new_turns}

Write the updated summary in at most {max_words} words. Preserve: topics/questions already covered, and any specific facts, numbers, names, or stories the candidate already committed to, so future answers don't contradict them. Plain prose, no bullet points, no preamble, no meta-commentary about the summary itself."""


def format_turns(turns: Sequence[Turn]) -> str:
    lines = []
    for t in turns:
        label = "INTERVIEWER" if t.speaker == "interviewer" else "CANDIDATE"
        lines.append(f"[{label}] {t.text.strip()}")
    return "\n".join(lines)


def build_update_prompt(prior_summary: str, new_turns: Sequence[Turn]) -> str:
    """Standalone so the LLM clients can build the prompt without depending
    on a ContextSummarizer instance."""
    return UPDATE_PROMPT.format(
        prior_summary=prior_summary or "(none yet — this is the first update)",
        new_turns=format_turns(new_turns),
        max_words=SUMMARY_MAX_WORDS,
    )


class ContextSummarizer:
    """Owns the running summary text plus a `ts` cursor of what's folded in."""

    def __init__(self) -> None:
        self.summary: str = ""
        self._folded_up_to_ts: float = 0.0

    def unfolded(self, turns: Sequence[Turn]) -> list[Turn]:
        """Turns to send VERBATIM: everything not yet folded into the summary.
        The cursor only advances over old FINALIZED turns, so recent partials
        (large ts) are always included — pass the full snapshot here."""
        return [t for t in turns if t.ts > self._folded_up_to_ts]

    def turns_to_fold(
        self, finalized_turns: Sequence[Turn], budget_tokens: int, min_verbatim: int
    ) -> list[Turn]:
        """
        The oldest not-yet-folded finalized turns to compress NOW so the
        verbatim remainder fits `budget_tokens`, while always leaving at least
        `min_verbatim` recent turns verbatim. Empty list = under budget, do
        nothing (the common case).
        """
        unfolded = [t for t in finalized_turns if t.ts > self._folded_up_to_ts]
        remaining = list(unfolded)
        to_fold: list[Turn] = []
        while len(remaining) > min_verbatim and _est_tokens(remaining) > budget_tokens:
            to_fold.append(remaining.pop(0))
        return to_fold

    def should_update(
        self, finalized_turns: Sequence[Turn], budget_tokens: int, min_verbatim: int
    ) -> bool:
        """True when the verbatim transcript is over budget and there are old
        turns that can be folded to bring it back down."""
        return bool(self.turns_to_fold(finalized_turns, budget_tokens, min_verbatim))

    def apply_update(self, folded_turns: Sequence[Turn], updated_summary: str) -> None:
        """Call after a successful summarize() call — commits the new summary
        text and advances the fold cursor past `folded_turns` (so they drop out
        of the verbatim set exactly as they enter the summary — no gap)."""
        self.summary = updated_summary.strip()
        if folded_turns:
            self._folded_up_to_ts = max(t.ts for t in folded_turns)

    def reset(self) -> None:
        """New interview session — clear all state."""
        self.summary = ""
        self._folded_up_to_ts = 0.0
