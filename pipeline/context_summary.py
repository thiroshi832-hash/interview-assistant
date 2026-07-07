"""
Running summary of interview turns that have aged out of the rolling
transcript window sent to the LLM on every answer.

`rolling_turns` (see config.py) keeps each answer prompt cheap and fast, but
on its own it means the model has zero memory of anything before the last
few exchanges. This fills that gap without paying for the full verbatim
transcript on every call: once enough finalized turns have fallen out of the
window, they're folded into a short running summary via one cheap LLM call,
and the summary — not the raw turns — carries that older context forward.

Turns are matched by `ts` rather than list index/count, so eviction from the
transcript's bounded deque (see pipeline/transcript.py, max_turns=200) can
never cause a turn to be skipped or re-folded.
"""
from __future__ import annotations

from typing import Sequence

from pipeline.types import Turn


SUMMARY_MAX_WORDS = 150

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

    def pending_turns(self, finalized_turns: Sequence[Turn], keep_last: int) -> list[Turn]:
        """
        Finalized turns old enough to fall outside the rolling window
        (`keep_last`) that haven't been folded into the summary yet.
        """
        cutoff = max(0, len(finalized_turns) - keep_last)
        older = finalized_turns[:cutoff]
        return [t for t in older if t.ts > self._folded_up_to_ts]

    def should_update(self, finalized_turns: Sequence[Turn], keep_last: int, batch_size: int) -> bool:
        """True once at least `batch_size` turns are waiting to be folded in —
        batches the (paid) summarization call instead of firing on every turn."""
        return len(self.pending_turns(finalized_turns, keep_last)) >= batch_size

    def apply_update(self, new_turns: Sequence[Turn], updated_summary: str) -> None:
        """Call after a successful summarize() call — commits the new summary
        text and advances the fold cursor past `new_turns`."""
        self.summary = updated_summary.strip()
        if new_turns:
            self._folded_up_to_ts = max(t.ts for t in new_turns)

    def reset(self) -> None:
        """New interview session — clear all state."""
        self.summary = ""
        self._folded_up_to_ts = 0.0
