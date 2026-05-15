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
            if existing is not None and existing in self._turns:
                # Replace in-place
                idx = list(self._turns).index(existing)
                # deque doesn't support direct assignment; rebuild
                new_deque: Deque[Turn] = deque(maxlen=self._turns.maxlen)
                for i, t in enumerate(self._turns):
                    new_deque.append(turn if i == idx else t)
                self._turns = new_deque
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
            # If there's a pending partial but the final is empty, drop it
            with self._lock:
                pending = self._pending.pop(speaker, None)
                if pending is not None and pending in self._turns:
                    new_deque: Deque[Turn] = deque(maxlen=self._turns.maxlen)
                    for t in self._turns:
                        if t is not pending:
                            new_deque.append(t)
                    self._turns = new_deque
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
            if existing is not None and existing in self._turns:
                idx = list(self._turns).index(existing)
                new_deque: Deque[Turn] = deque(maxlen=self._turns.maxlen)
                for i, t in enumerate(self._turns):
                    new_deque.append(turn if i == idx else t)
                self._turns = new_deque
                replaced = True
            else:
                self._turns.append(turn)

        for fn in list(self._listeners):
            try:
                fn(turn, replaced)
            except Exception:
                pass
        return turn

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
