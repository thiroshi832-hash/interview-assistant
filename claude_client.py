"""
Claude client. Streams interview answers using the candidate's resume as cached context.

The resume + role description live in the system prompt with cache_control set,
so we pay the ~1.25x write cost only on the first request of the session and
~0.1x on every subsequent answer.
"""
from __future__ import annotations

import json
from typing import Iterator, Sequence

import anthropic

from config import Config
from pipeline.evaluation import (
    EVALUATION_SCHEMA, EVALUATION_USER_PROMPT, InterviewEvaluation,
    format_transcript,
)
from pipeline.types import Turn


SYSTEM_RULES = """You are answering interview questions on behalf of the candidate, in their voice, in real time during a live interview. The answer is going to be READ ALOUD by the candidate. It must sound like a real engineer thinking out loud, NOT like a blog post or a memorized pitch.

SOUND HUMAN. Use the rhythms of natural speech:
- Mix sentence lengths. Some short. Some longer with a couple of clauses.
- Use contractions everywhere: "we'd", "I've", "it's", "didn't" — never "we would" / "I have" / "did not".
- Light hedging is fine when honest: "I think", "kind of", "basically", "around 5,000", "if I remember right".
- Soft mid-sentence corrections feel real, sparingly: "we used Redis — well, Redis and then later Postgres."
- Talk in the FIRST SENTENCE before backing into context. Lead with the answer, then the example.
- Use "so", "yeah", "right" as natural connectives at most once per answer — not as openers.

AVOID THESE TELLS that make answers sound AI-generated:
- "Great question", "Happy to discuss", "I'd love to share"
- "I hope that helps", "Does that answer your question?"
- Corporate filler: "leverage", "synergize", "robust", "best-in-class", "scalable solutions", "deep dive"
- Formulaic structure: "Firstly... Secondly... Thirdly..." (say "First off..." / "And then..." / "The other piece...")
- Bullet-point list structure inside a spoken answer
- Excessive hedging: "I would perhaps suggest that..." — just say it
- Perfectly polished STAR with explicit S/T/A/R labels — compress it into how someone would actually narrate the story

LENGTH — keep it short, running long sounds unprepared:
- Clarifying questions: 1 sentence.
- Behavioral questions: 2-3 sentences. One compressed STAR beat (situation → action → result, skip "task").
- Technical / system-design questions: 3-5 sentences. Lead with the most important point. The interviewer will follow up if they want detail.
- Expand only when explicitly asked (style override like "more technical, include specifics").

ACCURACY:
- Never invent employer names, project names, dates, or metrics that aren't in the resume.
- If the question covers something the resume doesn't, say so briefly ("I haven't worked on X directly") and give a credible take in one or two sentences. Don't fake expertise.

CONTINUATION:
- If the candidate has already started speaking this turn, continue from where they left off — don't restart.

OUTPUT:
- ONLY the words the candidate would say. No commentary, no stage directions, no quoted sections, no labels."""


class ClaudeClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it as an environment variable "
                "or save it via the settings dialog."
            )
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._resume_text: str = ""

    # ── one-time per interview ───────────────────────────────────────────────
    def set_context(
        self,
        resume_text: str,
        job_title: str = "",
        job_description: str = "",
        personal_context: str = "",
    ) -> None:
        self._resume_text = resume_text.strip()
        self.cfg.job_title = job_title
        self.cfg.job_description = job_description
        self.cfg.personal_context = personal_context

    # ── one-time warmup (populate prompt cache + establish HTTPS) ──────────
    def warmup(self) -> None:
        """
        Make a tiny single-token call so:
          - The HTTPS connection to api.anthropic.com is established
          - The cached system block (resume + role + personal_context) is
            written to Anthropic's 5-minute prompt cache
        Subsequent live-interview answers then start streaming ~50 % faster.
        Failures are swallowed — the first real answer will just pay the
        cache-write cost itself instead.
        """
        if not self._resume_text:
            return
        try:
            self._client.messages.create(
                model=self.cfg.model,
                max_tokens=8,
                system=self._build_system_blocks(),
                messages=[{"role": "user", "content": "Ready?"}],
                output_config={"effort": "low"},
            )
        except Exception:
            pass

    # ── post-interview evaluation (called when user clicks "End interview") ──
    def evaluate_interview(self, turns: Sequence[Turn]) -> InterviewEvaluation:
        """
        Send the full transcript to Claude and ask for a structured hireability
        verdict. Uses the deep model (Opus 4.7) for a more careful assessment.
        Returns an InterviewEvaluation with .error set if the call fails.
        """
        if not self._resume_text:
            return InterviewEvaluation(
                score=0, verdict="fail", summary="",
                error="No resume context — cannot evaluate.",
            )
        transcript = format_transcript(turns)
        if not transcript.strip():
            return InterviewEvaluation(
                score=0, verdict="fail", summary="",
                error="No transcript to evaluate.",
            )
        try:
            response = self._client.messages.create(
                model=self.cfg.deep_model,
                max_tokens=2048,
                system=self._build_system_blocks(),
                messages=[{
                    "role": "user",
                    "content": EVALUATION_USER_PROMPT.format(transcript=transcript),
                }],
                output_config={
                    "format": {"type": "json_schema", "schema": EVALUATION_SCHEMA},
                    "effort": "high",
                },
            )
            text = next(
                (b.text for b in response.content if getattr(b, "type", None) == "text"),
                "",
            )
            data = json.loads(text)
            return InterviewEvaluation(
                score=int(data.get("score", 0)),
                verdict=str(data.get("verdict", "fail")),
                summary=str(data.get("summary", "")),
                strengths=list(data.get("strengths", []) or []),
                concerns=list(data.get("concerns", []) or []),
                specific_moments=list(data.get("specific_moments", []) or []),
            )
        except Exception as e:
            return InterviewEvaluation(
                score=0, verdict="fail", summary="",
                error=f"Evaluation failed: {e}",
            )

    # ── per-question ─────────────────────────────────────────────────────────
    def stream_answer(
        self,
        turns: Sequence[Turn],
        *,
        deep: bool = False,
        style_hint: str = "",
    ) -> Iterator[str]:
        """
        Yield text chunks of the answer as Claude streams it.

        `turns` is the rolling speaker-labelled transcript. The latest interviewer
        turn is the one being answered.

        `deep=True` swaps to the deep-mode model (Opus 4.7 by default).
        `style_hint` is appended to nudge length/tone, e.g. "shorter" or "more technical".
        """
        if not self._resume_text:
            raise RuntimeError("Call set_context() with the resume before requesting an answer.")

        system = self._build_system_blocks()
        user_message = self._build_user_message(turns, style_hint=style_hint)
        model = self.cfg.deep_model if deep else self.cfg.model

        with self._client.messages.stream(
            model=model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_config={"effort": self.cfg.effort},
        ) as stream:
            for text in stream.text_stream:
                yield text

    # ── internals ────────────────────────────────────────────────────────────
    def _build_system_blocks(self) -> list[dict]:
        # Render order is tools → system → messages. Stable content first, then
        # the cached resume+role block, then nothing volatile in system.
        role_block = (
            f"<target_role>\n{self.cfg.job_title or '(not specified)'}\n\n"
            f"Job description:\n{self.cfg.job_description or '(not specified)'}\n"
            f"</target_role>"
        )
        resume_block = f"<candidate_resume>\n{self._resume_text}\n</candidate_resume>"
        personal_block = ""
        if self.cfg.personal_context.strip():
            personal_block = (
                f"\n\n<personal_context>\n"
                f"Information about the candidate that is NOT in the resume. Use this "
                f"to answer questions about salary, start date, hobbies, work style, "
                f"availability, location, etc. Treat these as the candidate's actual "
                f"preferences — do not contradict them.\n\n"
                f"{self.cfg.personal_context.strip()}\n"
                f"</personal_context>"
            )

        return [
            {"type": "text", "text": SYSTEM_RULES},
            {
                "type": "text",
                "text": f"{role_block}\n\n{resume_block}{personal_block}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def _build_user_message(self, turns: Sequence[Turn], *, style_hint: str = "") -> str:
        if not turns:
            return "Provide a brief self-introduction in the candidate's voice based on the resume."

        # Rolling transcript, oldest first
        lines = []
        for t in turns[-self.cfg.rolling_turns:]:
            label = "INTERVIEWER" if t.speaker == "interviewer" else "CANDIDATE"
            lines.append(f"[{label}] {t.text.strip()}")

        transcript = "\n".join(lines)
        hint = f"\n\nStyle override for this answer: {style_hint}." if style_hint else ""

        return (
            f"Conversation so far:\n\n{transcript}\n\n"
            "Answer the LAST interviewer turn above. If the candidate has already started "
            "speaking in response, continue from where they left off; otherwise produce "
            f"the full answer.{hint}"
        )
