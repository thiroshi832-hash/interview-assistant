"""
Post-interview evaluation. Takes the full transcript and asks the LLM for a
hireability verdict on a 0-100 scale plus structured reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from pipeline.types import Turn


VERDICT_TIERS = (
    (85, "strong_hire", "Strong hire"),
    (70, "hire", "Hire"),
    (50, "maybe", "Maybe — borderline"),
    (30, "lean_fail", "Lean fail"),
    (0,  "fail", "Fail"),
)


@dataclass
class InterviewEvaluation:
    score: int                      # 0..100
    verdict: str                    # one of: strong_hire / hire / maybe / lean_fail / fail
    summary: str                    # 1-2 sentence overall assessment
    strengths: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    specific_moments: list[dict] = field(default_factory=list)
    # Filled by caller if LLM call fails
    error: str = ""

    @classmethod
    def from_score(cls, score: int) -> str:
        """Map a raw 0-100 score → verdict tier label."""
        for threshold, tier, _label in VERDICT_TIERS:
            if score >= threshold:
                return tier
        return "fail"

    @classmethod
    def label_for_verdict(cls, verdict: str) -> str:
        for _t, tier, label in VERDICT_TIERS:
            if tier == verdict:
                return label
        return verdict


def format_transcript(turns: Sequence[Turn]) -> str:
    """Render the finalized transcript into a single block of speaker-labelled text."""
    lines = []
    for t in turns:
        if not t.is_final:
            continue
        if t.speaker == "interviewer":
            lines.append(f"INTERVIEWER: {t.text.strip()}")
        elif t.speaker == "candidate":
            lines.append(f"CANDIDATE: {t.text.strip()}")
    return "\n\n".join(lines)


# JSON schema enforced via structured outputs.
EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "0-100 hireability score. 85+ strong hire, 70+ hire, 50+ maybe, 30+ lean fail, below 30 fail.",
        },
        "verdict": {
            "type": "string",
            "enum": ["strong_hire", "hire", "maybe", "lean_fail", "fail"],
        },
        "summary": {
            "type": "string",
            "description": "1-2 sentence overall assessment, written as if briefing the hiring manager.",
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 bullets — the candidate's strongest observed traits / answers.",
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-5 bullets — gaps, red flags, or weak areas.",
        },
        "specific_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string", "description": "A short quote from the transcript."},
                    "comment": {"type": "string", "description": "What this moment showed about the candidate."},
                },
                "required": ["quote", "comment"],
                "additionalProperties": False,
            },
            "description": "2-4 notable moments worth quoting (good or bad).",
        },
    },
    "required": ["score", "verdict", "summary", "strengths", "concerns", "specific_moments"],
    "additionalProperties": False,
}


EVALUATION_USER_PROMPT = """You are an experienced engineering interviewer assessing a candidate's performance based on the FULL transcript below.

Evaluate against the candidate's own resume and the target role's job description (both in the system prompt). Score on a 0-100 hireability scale:
- 85-100  Strong hire — sharp, specific, deep answers; clear culture/values fit
- 70-84   Hire — solid answers, minor weaknesses
- 50-69   Maybe — mixed performance; needs follow-up rounds
- 30-49   Lean fail — significant gaps in skills or communication
- 0-29    Fail — major problems

Consider:
- Technical depth, correctness, and concrete examples
- Communication clarity and structure (STAR for behavioral, narrative for technical)
- Use of specific numbers, names, and outcomes from the resume
- Red flags (vagueness, contradiction, evasion, off-topic answers)
- Cultural / values fit signals
- Whether the candidate's stated personal context aligns with the role

DO NOT be a pushover. Most candidates score 50-70. Score 85+ only for genuinely exceptional performance.

Output ONLY valid JSON matching the schema. No prose outside the JSON.

INTERVIEW TRANSCRIPT:
─────────────────────────────────────────────────────
{transcript}
─────────────────────────────────────────────────────"""
