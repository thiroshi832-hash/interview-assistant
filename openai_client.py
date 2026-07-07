"""
OpenAI client — same interface as ClaudeClient, streams interview answers
using the candidate's resume as the system prompt.

Uses the official `openai` SDK natively. OpenAI's automatic prompt caching
kicks in for long system prompts (the resume), so we keep the resume at the
top of the system message and the rolling transcript in the user turn.
"""
from __future__ import annotations

import json
from typing import Iterator, Sequence

from openai import OpenAI

from config import Config
from pipeline.context_summary import build_update_prompt
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

LENGTH — SHORT is the default. Long answers sound rehearsed and eat the interviewer's time:
- Clarifying questions: 1 sentence.
- Behavioral questions: 2 sentences — one compressed STAR beat (situation → action → result).
- Technical / system-design questions: 2-3 sentences. Lead with the single most important point, then stop — the interviewer will follow up if they want more.
- One idea per answer. Don't stack three points where one lands. If the answer is complete in a sentence, give one sentence — never pad to fill space.
- Plain, everyday words over fancy ones; short sentences over long ones.
- Expand only when explicitly asked (style override like "more technical, include specifics").

ACCURACY:
- Never invent employer names, project names, dates, or metrics that aren't in the resume.
- If the question covers something the resume doesn't, say so briefly ("I haven't worked on X directly") and give a credible take in one or two sentences. Don't fake expertise.

CONTINUATION:
- If the candidate has already started speaking this turn, continue from where they left off — don't restart.

OUTPUT:
- ONLY the words the candidate would say. No commentary, no stage directions, no quoted sections, no labels."""


class OpenAIClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Paste it in the API key dialog on launch, "
                "or set the OPENAI_API_KEY environment variable."
            )
        self._client = OpenAI(api_key=cfg.openai_api_key)
        self._resume_text: str = ""

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

    def evaluate_interview(self, turns: Sequence[Turn]) -> InterviewEvaluation:
        """Same shape as ClaudeClient.evaluate_interview — uses OpenAI's
        json_schema response_format."""
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
            response = self._client.chat.completions.create(
                model=self.cfg.openai_deep_model,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": self._build_system()},
                    {"role": "user", "content": EVALUATION_USER_PROMPT.format(transcript=transcript)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "interview_evaluation",
                        "schema": EVALUATION_SCHEMA,
                        "strict": True,
                    },
                },
                temperature=0.3,
            )
            text = response.choices[0].message.content or "{}"
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

    def warmup(self) -> None:
        """
        Tiny one-shot call to establish the HTTPS connection and prime
        OpenAI's automatic prompt cache (which kicks in for prompts ≥1024
        tokens). Subsequent answers start streaming ~30-50 % faster.
        """
        if not self._resume_text:
            return
        try:
            self._client.chat.completions.create(
                model=self.cfg.openai_model,
                max_tokens=8,
                messages=[
                    {"role": "system", "content": self._build_system()},
                    {"role": "user", "content": "Ready?"},
                ],
                temperature=0.0,
            )
        except Exception:
            pass

    # ── running context summary (see pipeline/context_summary.py) ─────────────
    def summarize(self, prior_summary: str, new_turns: Sequence[Turn]) -> str:
        """Same contract as ClaudeClient.summarize() — see there for why."""
        response = self._client.chat.completions.create(
            model=self.cfg.openai_model,
            max_tokens=300,
            messages=[{"role": "user", "content": build_update_prompt(prior_summary, new_turns)}],
            temperature=0.3,
        )
        return (response.choices[0].message.content or "").strip()

    def stream_answer(
        self,
        turns: Sequence[Turn],
        *,
        deep: bool = False,
        style_hint: str = "",
        summary: str = "",
    ) -> Iterator[str]:
        if not self._resume_text:
            raise RuntimeError("Call set_context() with the resume before requesting an answer.")

        system = self._build_system()
        user = self._build_user(turns, style_hint=style_hint, summary=summary)
        model = self.cfg.openai_deep_model if deep else self.cfg.openai_model

        stream = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
            max_tokens=self.cfg.max_tokens,
            # 0.75 gives noticeably more natural phrasing variety than the
            # earlier 0.4 (which produced the repeated "Great question / I'd
            # love to share" patterns). Still well below 1.0 — doesn't risk
            # rambling or off-topic answers.
            temperature=0.75,
        )
        for event in stream:
            try:
                delta = event.choices[0].delta.content
            except (AttributeError, IndexError):
                delta = None
            if delta:
                yield delta

    # ── internals ───────────────────────────────────────────────────────
    def _build_system(self) -> str:
        role_block = (
            f"<target_role>\n{self.cfg.job_title or '(not specified)'}\n\n"
            f"Job description:\n{self.cfg.job_description or '(not specified)'}\n"
            f"</target_role>"
        )
        resume_block = f"<candidate_resume>\n{self._resume_text}\n</candidate_resume>"
        personal_block = ""
        if self.cfg.personal_context.strip():
            personal_block = (
                "\n\n<personal_context>\n"
                "Information about the candidate that is NOT in the resume. Use this "
                "to answer questions about salary, start date, hobbies, work style, "
                "availability, location, etc. Treat these as the candidate's actual "
                "preferences — do not contradict them.\n\n"
                f"{self.cfg.personal_context.strip()}\n"
                "</personal_context>"
            )
        return f"{SYSTEM_RULES}\n\n{role_block}\n\n{resume_block}{personal_block}"

    def _build_user(self, turns: Sequence[Turn], *, style_hint: str = "", summary: str = "") -> str:
        if not turns:
            return "Provide a brief self-introduction in the candidate's voice based on the resume."

        lines = []
        for t in turns[-self.cfg.rolling_turns:]:
            label = "INTERVIEWER" if t.speaker == "interviewer" else "CANDIDATE"
            lines.append(f"[{label}] {t.text.strip()}")
        transcript = "\n".join(lines)
        hint = f"\n\nStyle override for this answer: {style_hint}." if style_hint else ""
        summary_block = (
            f"Summary of the interview before the excerpt below:\n{summary}\n\n" if summary else ""
        )

        return (
            f"{summary_block}"
            f"Conversation so far:\n\n{transcript}\n\n"
            "Answer the LAST interviewer turn above. If the candidate has already started "
            "speaking in response, continue from where they left off; otherwise produce "
            f"the full answer.{hint}"
        )
