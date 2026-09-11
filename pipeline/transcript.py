"""
Thread-safe rolling transcript of speaker-labelled utterances, with support
for in-progress (partial) updates from streaming STT backends.

The audio pipeline calls:
    update_partial(speaker, text, ts)  — fired many times during speech
    commit(speaker, text, ts)          — fired once when STT is sure

The UI subscribes via subscribe(fn) — fn(turn, was_partial_update: bool).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

from pipeline.types import Turn


class Transcript:
    def __init__(self, max_turns: int = 200):
        self._turns: Deque[Turn] = deque(maxlen=max_turns)
        self._lock = threading.Lock()
        # fn(turn: Turn, replaced_partial: bool)
        self._listeners: list[Callable[[Turn, bool], None]] = []
        # Per-speaker in-progress turn (the last non-final turn for that
        # speaker, which gets replaced on each new partial).
        self._pending: dict[str, Turn] = {}

    # ── writes ─────────────────────────────────────────────────────────────
    def add(self, speaker: str, text: str, ts: float | None = None) -> Optional[Turn]:
        """Legacy / batch path — adds a finalized turn directly."""
        return self.commit(speaker, text, ts)

    def update_partial(self, speaker: str, text: str, ts: float | None = None) -> Optional[Turn]:
        """
        Insert or replace an in-progress turn for this speaker. Returns the
        Turn (or None if text is empty). Listeners are notified with
        replaced_partial=True if this replaces an earlier partial from the
        same speaker, False if it's the first partial of a new utterance.
        """
        text = text.strip()
        if not text:
            return None
        turn = Turn(
            speaker=speaker,
            text=text,
            ts=ts if ts is not None else time.time(),
            is_final=False,
        )
        replaced = False
        with self._lock:
            existing = self._pending.get(speaker)
            # Only replace in place if this speaker's pending turn is still the
            # MOST RECENT turn. If turns from someone else arrived after it, the
            # conversation moved on and that pending is stale — appending a new
            # turn keeps the transcript in chronological order. (Replacing a
            # stale pending in place resurrects it out of order at its old
            # position, e.g. a never-finalized bleed fragment getting
            # overwritten by the candidate's later real speech.)
            if existing is not None and self._turns and self._turns[-1] is existing:
                self._turns[-1] = turn
                replaced = True
            else:
                self._turns.append(turn)
            self._pending[speaker] = turn

        for fn in list(self._listeners):
            try:
                fn(turn, replaced)
            except Exception:
                pass
        return turn

    def commit(self, speaker: str, text: str, ts: float | None = None) -> Optional[Turn]:
        """
        Finalize the in-progress turn for this speaker (or create a new final
        turn if there was no pending one — covers the batch backend's path).
        """
        text = text.strip()
        if not text:
            # Empty final cancels the in-progress turn — but only if it's still
            # the active tail. A stale pending (conversation moved past it)
            # stays frozen where it chronologically belongs.
            with self._lock:
                pending = self._pending.pop(speaker, None)
                if pending is not None and self._turns and self._turns[-1] is pending:
                    self._turns.pop()
            return None
        turn = Turn(
            speaker=speaker,
            text=text,
            ts=ts if ts is not None else time.time(),
            is_final=True,
        )
        replaced = False
        with self._lock:
            existing = self._pending.pop(speaker, None)
            # Same rule as update_partial: finalize in place only if the
            # pending is still the most recent turn; otherwise append so a
            # stale pending isn't resurrected out of order.
            if existing is not None and self._turns and self._turns[-1] is existing:
                self._turns[-1] = turn
                replaced = True
            else:
                self._turns.append(turn)

        for fn in list(self._listeners):
            try:
                fn(turn, replaced)
            except Exception:
                pass
        return turn

    def retract_partial(self, speaker: str) -> bool:
        """
        Drop this speaker's in-progress turn outright (utterance judged to be
        bleed/echo after its partial was already shown). Unlike update/commit,
        this removes the pending turn WHEREVER it sits — in the common bleed
        race the interviewer's own final lands after the bleed partial, so the
        partial is no longer the tail, yet it must still be erased. Returns
        True if a pending turn was removed (caller should then tell the UI to
        erase the displayed line).
        """
        with self._lock:
            pending = self._pending.pop(speaker, None)
            if pending is None:
                return False
            for i in range(len(self._turns) - 1, -1, -1):
                if self._turns[i] is pending:
                    del self._turns[i]
                    return True
        return False

    # ── reads ──────────────────────────────────────────────────────────────
    def snapshot(self) -> list[Turn]:
        """All turns including partials."""
        with self._lock:
            return list(self._turns)

    def snapshot_finalized(self) -> list[Turn]:
        """Only committed turns — feed this to the LLM."""
        with self._lock:
            return [t for t in self._turns if t.is_final]

    def last_interviewer_turn(self) -> Turn | None:
        with self._lock:
            for turn in reversed(self._turns):
                if turn.speaker == "interviewer" and turn.is_final:
                    return turn
        return None

    def subscribe(self, fn: Callable[[Turn, bool], None]) -> None:
        """fn(turn, replaced_partial). Called on every partial or final."""
        self._listeners.append(fn)
