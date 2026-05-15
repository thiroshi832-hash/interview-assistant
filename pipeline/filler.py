"""
Short opener sentences emitted to the answer panel the instant a question is
detected — buys the candidate a few seconds to speak while the real LLM answer
is still being generated.

Three categories so the picks don't all feel similar:
  • SHORT  — one to three words. Use when the answer probably arrives fast.
  • MEDIUM — one short sentence. Most common.
  • LONG   — a full setup sentence. Use when the LLM is slow (Whisper/Claude).

The picker:
  • ~25 % of the time emits no opener at all (clean start)
  • Dedupes the last 8 picks across categories
  • Weights MEDIUM most, then LONG, then SHORT
"""
from __future__ import annotations

import random
from collections import deque


# Short — natural micro-fillers a real person says before they start speaking.
_SHORT: tuple[str, ...] = (
    "Sure.",
    "Yeah.",
    "Right.",
    "Okay.",
    "Mhm.",
    "Got it.",
    "Yeah, sure.",
    "Okay, yeah.",
    "Right, so.",
)

# Medium — single sentence, varied structure.
_MEDIUM: tuple[str, ...] = (
    "Hmm, let me think.",
    "Yeah, good one.",
    "So — the way I'd frame this is.",
    "Right, so off the top of my head.",
    "Okay, so a quick story.",
    "Let me think about that for a sec.",
    "Honestly, the first thing that comes to mind is.",
    "Good question — quick context first.",
    "Yeah, the way I usually think about this is.",
    "I'll give you the short version.",
    "Quick story on that.",
    "Sure, let me back up a second.",
    "So this actually came up at my last role.",
    "I'll start with the punchline.",
    "Yeah, I've thought about this one before.",
    "Right, okay, let me set the scene.",
    "Sure — the short answer first.",
    "Hmm, interesting framing. So.",
)

# Long — full setup sentences. Use when latency is likely higher.
_LONG: tuple[str, ...] = (
    "Yeah, that's a good one — let me walk you through how I'd actually approach it.",
    "Right, so the way I usually think about a problem like this is to start from the constraints.",
    "Honestly, the way this played out for me was, we had a similar situation, and what we did was.",
    "Okay, let me think about this for a second — there's a specific story I want to tell.",
    "Sure, so the short version is, but let me also give you the why behind it.",
    "Hmm, that's actually a fun one because we ran into exactly this at my last role.",
    "Let me think about how to structure this — I want to give you the actual numbers, not just the headline.",
    "Yeah, so I'll start with what I did first, and then I can walk back into the tradeoffs.",
)


_recent: deque[str] = deque(maxlen=8)
_SKIP_PROB = 0.25       # how often we emit nothing — cleaner start sometimes


def pick_opener() -> str:
    """
    Return a filler opener, or empty string for "no opener this time".

    Tuned so consecutive answers feel varied in length and form.
    """
    if random.random() < _SKIP_PROB:
        return ""

    # Weighted choice across categories
    category = random.choices(
        [_SHORT, _MEDIUM, _LONG],
        weights=[15, 60, 25],
        k=1,
    )[0]

    candidates = [o for o in category if o not in _recent]
    if not candidates:
        candidates = list(category)
    choice = random.choice(candidates)
    _recent.append(choice)
    return choice
