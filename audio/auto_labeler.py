"""
Map raw cluster IDs (spk_0, spk_1, ...) onto semantic labels ("candidate" /
"interviewer") based on behavior, with no enrollment.

Strategy: collect short stats per cluster — fraction of utterances ending in '?',
average length, first-to-speak — and once the stats are stable, label the cluster
with the LOWEST "asks-questions" score as the candidate; everyone else is an
interviewer. Until the labels are locked, utterances are emitted as 'interviewer'
to be safe (the question detector will only fire on real questions anyway).
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from dataclasses import dataclass


_QUESTION_LIKE = re.compile(
    r"(\?$|^(tell me|walk me|describe|explain|how |why |what |can you|"
    r"could you|would you|have you|are you|give me an example|when did|where did)\b)",
    re.IGNORECASE,
)


@dataclass
class _ClusterStats:
    utterances: int = 0
    questions: int = 0
    total_chars: int = 0
    first_ts: float = float("inf")


class AutoLabeler:
    def __init__(self, min_utterances_per_cluster: int = 3):
        self._lock = threading.Lock()
        self._stats: dict[str, _ClusterStats] = defaultdict(_ClusterStats)
        self._labels: dict[str, str] = {}
        self._min_utterances = min_utterances_per_cluster
        # When True, the user has manually pinned the mapping — stop auto-relabeling.
        self._locked = False

    def observe(self, cluster_id: str, text: str, ts: float) -> str:
        """
        Record an utterance and return the current best-guess label for it.

        Returns "interviewer" or "candidate".
        """
        with self._lock:
            s = self._stats[cluster_id]
            s.utterances += 1
            if _QUESTION_LIKE.search(text):
                s.questions += 1
            s.total_chars += len(text)
            if ts < s.first_ts:
                s.first_ts = ts

            # Re-evaluate labels if we have enough data — unless the user has pinned.
            if not self._locked and self._can_label():
                self._relabel()

            return self._labels.get(cluster_id, "interviewer")

    def labels(self) -> dict[str, str]:
        with self._lock:
            return dict(self._labels)

    def swap_and_lock(self) -> dict[str, str]:
        """
        Invert the current candidate/interviewer mapping and freeze it.
        Future utterances use the inverted labels; the heuristic stops second-guessing.
        Returns the new label mapping.

        Even if labels haven't been assigned yet (too few utterances), this
        bootstraps the mapping from the most-spoken vs. least-spoken cluster.
        """
        with self._lock:
            if not self._labels:
                # Cold start: pick most-spoken cluster as candidate, others as interviewer.
                # (User is asking us to swap from the default "everyone is interviewer",
                # so this sets up an explicit candidate.)
                if not self._stats:
                    return {}
                most = max(self._stats.items(), key=lambda kv: kv[1].utterances)[0]
                self._labels = {
                    cid: ("candidate" if cid == most else "interviewer")
                    for cid in self._stats
                }
            else:
                self._labels = {
                    cid: ("candidate" if lbl == "interviewer" else "interviewer")
                    for cid, lbl in self._labels.items()
                }
            self._locked = True
            return dict(self._labels)

    @property
    def locked(self) -> bool:
        return self._locked

    # ── internals ────────────────────────────────────────────────────────
    def _can_label(self) -> bool:
        if len(self._stats) < 2:
            return False
        # Require at least N utterances on the *most-spoken* cluster, plus some
        # data on the others — avoids locking labels on a 1-vs-1 sample.
        counts = sorted((s.utterances for s in self._stats.values()), reverse=True)
        return counts[0] >= self._min_utterances and counts[1] >= max(1, self._min_utterances - 1)

    def _relabel(self) -> None:
        # Score each cluster: higher = more like an interviewer.
        # Inputs:
        #   q_rate  : questions / utterances  (interviewers ↑)
        #   avg_len : total_chars / utterances (candidates ↑ → invert)
        #   first   : earliest timestamp (interviewers usually open → ↓ = ↑ score)
        scored = []
        first_times = [s.first_ts for s in self._stats.values()]
        earliest = min(first_times)
        latest = max(first_times)
        span = max(latest - earliest, 1e-6)

        for cid, s in self._stats.items():
            q_rate = s.questions / s.utterances
            avg_len = s.total_chars / s.utterances
            len_inv = 1.0 - min(avg_len / 400.0, 1.0)     # 0..1, candidate-long → low
            first_score = 1.0 - (s.first_ts - earliest) / span  # earliest → 1.0

            interviewer_score = 0.6 * q_rate + 0.25 * len_inv + 0.15 * first_score
            scored.append((cid, interviewer_score))

        # Sort descending. The lowest interviewer-score is the candidate.
        scored.sort(key=lambda x: x[1], reverse=True)
        candidate_cid = scored[-1][0]

        self._labels = {
            cid: ("candidate" if cid == candidate_cid else "interviewer")
            for cid, _ in scored
        }
