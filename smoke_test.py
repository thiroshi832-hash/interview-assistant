"""
Smoke test for the offline components: transcript, question detector, auto labeler.

Run with:  python smoke_test.py
Exit code 0 = all good.
"""
from __future__ import annotations

import sys

from audio.auto_labeler import AutoLabeler
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
