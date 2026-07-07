"""
Smoke test for the offline components: transcript, question detector, auto labeler.

Run with:  python smoke_test.py
Exit code 0 = all good.
"""
from __future__ import annotations

import sys

from audio.auto_labeler import AutoLabeler
from pipeline.context_summary import ContextSummarizer
from pipeline.echo_filter import is_echo
from pipeline.interview_health import compute_health
from pipeline.license import (
    TRIAL_DAYS, days_remaining, is_valid_license, make_license_key, trial_expired,
)
from pipeline.question_detector import QuestionDetector
from pipeline.transcript import Transcript
from pipeline.types import Turn


def test_transcript_basic() -> None:
    t = Transcript()
    a = t.add("interviewer", "Walk me through your background.")
    b = t.add("candidate", "Sure, I'm a backend engineer with 8 years.")
    assert a is not None and b is not None
    snap = t.snapshot()
    assert len(snap) == 2
    assert t.last_interviewer_turn().text.startswith("Walk me")


def test_transcript_ignores_empty() -> None:
    t = Transcript()
    assert t.add("interviewer", "   ") is None
    assert t.snapshot() == []


def test_transcript_stale_partial_does_not_reorder() -> None:
    # A candidate partial that never finalizes (e.g. an interviewer-bleed
    # blip) must not be resurrected out of order when the candidate later
    # really speaks. The real speech should append at the END, after the
    # interviewer turn that arrived in between — not overwrite the stale
    # partial at its old (earlier) position.
    t = Transcript()
    t.update_partial("candidate", "Continuing", ts=1.0)          # stray, never finalizes
    t.commit("interviewer", "Continuing from the safeguards, can you...", ts=2.0)
    t.update_partial("candidate", "So for a follow-up strategy", ts=3.0)   # real speech

    texts = [(s.speaker, s.text) for s in t.snapshot()]
    # Real candidate speech is last, in chronological order.
    assert texts[-1] == ("candidate", "So for a follow-up strategy"), texts
    # The interviewer turn precedes the candidate's real speech.
    intv_idx = texts.index(("interviewer", "Continuing from the safeguards, can you..."))
    cand_idx = len(texts) - 1
    assert intv_idx < cand_idx, texts


def test_transcript_active_partial_still_replaces_in_place() -> None:
    # Normal case: consecutive partials from the same active speaker replace
    # in place (no duplicate lines) as long as nothing else interleaves.
    t = Transcript()
    t.update_partial("candidate", "So for", ts=1.0)
    t.update_partial("candidate", "So for a follow-up", ts=1.5)
    t.commit("candidate", "So for a follow-up strategy.", ts=2.0)
    texts = [(s.speaker, s.text) for s in t.snapshot()]
    assert texts == [("candidate", "So for a follow-up strategy.")], texts


def test_question_detector_question_mark() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="What's your strongest skill?", ts=1.0)
    assert qd.should_answer(turn) is True


def test_question_detector_opener() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="Tell me about a hard bug you fixed.", ts=1.0)
    assert qd.should_answer(turn) is True


def test_question_detector_statement_no_fire() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="Got it, thanks for that context.", ts=1.0)
    assert qd.should_answer(turn) is False


def test_question_detector_skips_candidate() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="candidate", text="Should I tell you about the team?", ts=1.0)
    assert qd.should_answer(turn) is False


def test_question_detector_dedup() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="How did you solve it?", ts=1.0)
    assert qd.should_answer(turn) is True
    assert qd.should_answer(turn) is False  # same ts → already answered


def test_question_detector_force() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="some statement", ts=1.0)
    assert qd.should_answer(turn, force=True) is True


def test_question_detector_candidate_speaking_blocks() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="What's your favourite editor?", ts=1.0)
    assert qd.should_answer(turn, candidate_speaking=True) is False


def test_question_detector_matches_opener_after_preamble() -> None:
    # Real-world case: Whisper drops the '?' and the second segment starts
    # with "background, how would you design...". The detector must still fire.
    qd = QuestionDetector()
    turn = Turn(
        speaker="interviewer",
        text="background, how would you design a system to maintain low latency "
             "and high reliability during sudden usage spikes",
        ts=1.0,
    )
    assert qd.should_answer(turn) is True


def test_question_detector_silence_after_fires_for_statement() -> None:
    qd = QuestionDetector()
    turn = Turn(speaker="interviewer", text="Got it, thanks.", ts=1.0)
    # Without silence, a plain statement doesn't trigger.
    assert qd.should_answer(turn) is False
    # But the silence safety net does.
    assert qd.should_answer(turn, silence_after=True) is True


def test_auto_labeler_picks_candidate_by_question_rate() -> None:
    al = AutoLabeler(min_utterances_per_cluster=2)
    # spk_0 asks questions → interviewer
    al.observe("spk_0", "Tell me about your last project.", ts=1.0)
    al.observe("spk_0", "Why did you choose that database?", ts=3.0)
    al.observe("spk_0", "What were the tradeoffs?", ts=5.0)
    # spk_1 answers → candidate
    al.observe("spk_1", "Sure. We built a real-time analytics service.", ts=2.0)
    al.observe("spk_1", "We picked ClickHouse for the throughput.", ts=4.0)

    labels = al.labels()
    assert labels.get("spk_0") == "interviewer", labels
    assert labels.get("spk_1") == "candidate", labels


def test_auto_labeler_swap_and_lock_inverts_and_freezes() -> None:
    al = AutoLabeler(min_utterances_per_cluster=2)
    al.observe("spk_0", "Tell me about your last project.", ts=1.0)
    al.observe("spk_0", "Why ClickHouse?", ts=3.0)
    al.observe("spk_1", "We built a real-time analytics service.", ts=2.0)
    al.observe("spk_1", "Throughput was the main reason.", ts=4.0)

    before = al.labels()
    assert before["spk_0"] == "interviewer"
    assert before["spk_1"] == "candidate"

    al.swap_and_lock()
    after = al.labels()
    assert after["spk_0"] == "candidate"
    assert after["spk_1"] == "interviewer"
    assert al.locked is True

    # Even if the model would normally re-flip, the lock keeps the user's pick.
    for _ in range(5):
        al.observe("spk_0", "Why ClickHouse?", ts=10.0)
    assert al.labels() == after


def test_echo_filter_drops_near_duplicate_within_lag_window() -> None:
    # Real acoustic echo: mic captures the speaker output ~0.1s after the
    # interviewer's audio ends. Same words, same time. Should drop.
    interviewer = Turn(
        speaker="interviewer",
        text="background, how would you design a system to maintain low latency "
             "and high reliability during sudden usage spikes such as a pandemic "
             "driven surge while also respecting compliance requirements.",
        ts=110.0,
    )
    echo_text = (
        "Given that background, how would you design a system to maintain low "
        "latency and high reliability during sudden usage spikes."
    )
    assert is_echo(echo_text, [interviewer], candidate_ts=110.5) is True


def test_echo_filter_keeps_candidate_reading_answer_aloud() -> None:
    # The bug the user hit on small.en: candidate reads the suggested answer
    # aloud, which legitimately echoes question words. But it happens SEVERAL
    # seconds after the interviewer finished — well outside the echo window.
    interviewer = Turn(
        speaker="interviewer",
        text="tell me about scaling distributed systems",
        ts=100.0,
    )
    # Candidate started reading the answer 4s after interviewer stopped;
    # finished reading at ts 12s — total lag from interviewer end: 12s.
    candidate_reading = (
        "Sure. When I think about scaling distributed systems, the key "
        "consideration is partitioning and load balancing strategies."
    )
    assert is_echo(candidate_reading, [interviewer], candidate_ts=112.0) is False


def test_echo_filter_keeps_genuine_candidate_speech() -> None:
    interviewer = Turn(
        speaker="interviewer",
        text="Tell me about a hard system you designed.",
        ts=100.0,
    )
    candidate_real = (
        "Sure. The hardest one was a real-time analytics pipeline using ClickHouse and Kafka — "
        "we hit ten million events per second during Black Friday."
    )
    assert is_echo(candidate_real, [interviewer], candidate_ts=102.0) is False


def test_echo_filter_ignores_short_utterances() -> None:
    interviewer = Turn(speaker="interviewer", text="Right, yeah, makes sense.", ts=100.0)
    short = "Yeah, right."
    assert is_echo(short, [interviewer], candidate_ts=100.5) is False


def test_echo_filter_respects_lag_window() -> None:
    # Same words but >3s after the interviewer ended — not echo.
    old = Turn(speaker="interviewer", text="Tell me about your background", ts=100.0)
    same_text = "Tell me about your background you mentioned in the intro"
    assert is_echo(same_text, [old], candidate_ts=110.0) is False


def test_echo_filter_catches_echo_during_in_progress_interviewer_turn() -> None:
    # Long interviewer turn still in progress (not yet committed): its text
    # lives in snapshot() as a partial but NOT in snapshot_finalized(). The
    # mic's acoustic echo of the tail finalizes before the interviewer turn
    # commits. The filter must catch it — which only works when it compares
    # against the full snapshot (the app.py fix), not finalized turns only.
    t = Transcript()
    t.update_partial(
        "interviewer",
        "tell me about a time you had to scale a distributed system under heavy load",
        ts=100.0,
    )
    echo_text = "scale a distributed system under heavy load"
    # Finalized-only (the old behavior) can't see the in-progress turn:
    assert is_echo(echo_text, t.snapshot_finalized(), candidate_ts=100.3) is False
    # Full snapshot (the fix) correctly flags the echo:
    assert is_echo(echo_text, t.snapshot(), candidate_ts=100.3) is True


def test_health_no_turns() -> None:
    h = compute_health([])
    assert h.score == 70 and h.label == "waiting"


def test_health_strong_for_normal_length_answer() -> None:
    turns = [
        Turn(speaker="interviewer", text="Tell me about a system you built.", ts=1.0),
        Turn(
            speaker="candidate",
            text=("Sure. We built a real-time analytics pipeline on ClickHouse, handling "
                  "50 million events per day with sub-100ms p99 query latency. The cost "
                  "was about a third of Snowflake at our scale."),
            ts=2.0,
        ),
    ]
    h = compute_health(turns)
    assert h.score >= 75, h
    assert h.label == "strong"


def test_health_drops_for_one_word_answers() -> None:
    turns = [
        Turn(speaker="interviewer", text="Walk me through your background.", ts=1.0),
        Turn(speaker="candidate", text="Uh, yeah.", ts=2.0),
    ]
    h = compute_health(turns)
    assert h.score < 70, h


def test_license_valid_key_accepts_dashes_and_case() -> None:
    key = make_license_key()
    assert is_valid_license(key)
    assert is_valid_license(key.lower())
    assert is_valid_license(key.replace("-", ""))
    assert is_valid_license(f"  {key}  ")


def test_license_rejects_garbage() -> None:
    assert not is_valid_license("")
    assert not is_valid_license("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
    assert not is_valid_license("not a key")
    # Off by one char
    key = make_license_key().replace("-", "")
    tampered = key[:-1] + ("0" if key[-1] != "0" else "1")
    assert not is_valid_license(tampered)


def test_trial_countdown() -> None:
    import time
    now = time.time()
    assert days_remaining(0) == TRIAL_DAYS
    assert days_remaining(now, now=now) == TRIAL_DAYS
    assert days_remaining(now - 5 * 86400, now=now) == TRIAL_DAYS - 5
    assert days_remaining(now - TRIAL_DAYS * 86400, now=now) == 0
    assert days_remaining(now - 100 * 86400, now=now) == 0
    assert not trial_expired(now - 5 * 86400, now=now)
    assert trial_expired(now - (TRIAL_DAYS + 1) * 86400, now=now)


def _turns(n: int, start_ts: float = 1.0) -> list[Turn]:
    out = []
    for i in range(n):
        speaker = "interviewer" if i % 2 == 0 else "candidate"
        out.append(Turn(speaker=speaker, text=f"turn {i}", ts=start_ts + i))
    return out


def test_context_summary_no_pending_within_window() -> None:
    cs = ContextSummarizer()
    turns = _turns(5)
    assert cs.pending_turns(turns, keep_last=8) == []
    assert cs.should_update(turns, keep_last=8, batch_size=6) is False


def test_context_summary_pending_only_turns_older_than_window() -> None:
    cs = ContextSummarizer()
    turns = _turns(12)  # 4 older than an 8-turn window
    pending = cs.pending_turns(turns, keep_last=8)
    assert [t.text for t in pending] == ["turn 0", "turn 1", "turn 2", "turn 3"]


def test_context_summary_should_update_respects_batch_size() -> None:
    cs = ContextSummarizer()
    turns = _turns(12)  # 4 aged out
    assert cs.should_update(turns, keep_last=8, batch_size=6) is False
    turns = _turns(14)  # 6 aged out
    assert cs.should_update(turns, keep_last=8, batch_size=6) is True


def test_context_summary_apply_update_advances_cursor() -> None:
    cs = ContextSummarizer()
    turns = _turns(14)
    pending = cs.pending_turns(turns, keep_last=8)
    assert len(pending) == 6
    cs.apply_update(pending, "The candidate discussed X and Y.")
    assert cs.summary == "The candidate discussed X and Y."
    # Those 6 turns are folded now — same snapshot yields nothing new.
    assert cs.pending_turns(turns, keep_last=8) == []


def test_context_summary_folded_turns_survive_growth_and_dont_repeat() -> None:
    cs = ContextSummarizer()
    turns = _turns(14)
    first_batch = cs.pending_turns(turns, keep_last=8)
    cs.apply_update(first_batch, "summary so far")

    # More turns arrive; only the newly-aged-out ones should be pending —
    # the already-folded ones must not reappear even though list indices shift.
    grown = _turns(20)
    pending = cs.pending_turns(grown, keep_last=8)
    assert [t.text for t in pending] == ["turn 6", "turn 7", "turn 8", "turn 9", "turn 10", "turn 11"]


def test_context_summary_reset_clears_state() -> None:
    cs = ContextSummarizer()
    turns = _turns(14)
    cs.apply_update(cs.pending_turns(turns, keep_last=8), "summary")
    cs.reset()
    assert cs.summary == ""
    assert cs.pending_turns(turns, keep_last=8) == cs.pending_turns(turns, keep_last=8)
    assert len(cs.pending_turns(turns, keep_last=8)) == 6


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK    {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
