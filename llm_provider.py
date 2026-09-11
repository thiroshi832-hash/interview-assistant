"""
LLM provider interface + factory.

Both clients (Anthropic Claude, OpenAI) implement the same minimal interface
used by the app: set_context() once, then stream_answer() per question.
"""
from __future__ import annotations

from typing import Iterator, Protocol, Sequence, runtime_checkable

from config import Config
from pipeline.evaluation import InterviewEvaluation
from pipeline.types import Turn


PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"


@runtime_checkable
class LLMClient(Protocol):
    def set_context(
        self,
        resume_text: str,
        job_title: str = "",
        job_description: str = "",
        personal_context: str = "",
    ) -> None: ...
    def warmup(self) -> None: ...
    def evaluate_interview(self, turns: Sequence[Turn]) -> InterviewEvaluation: ...
    def summarize(self, prior_summary: str, new_turns: Sequence[Turn]) -> str: ...
    def stream_answer(
        self,
        turns: Sequence[Turn],
        *,
        deep: bool = False,
        style_hint: str = "",
        summary: str = "",
    ) -> Iterator[str]: ...


def make_client(cfg: Config) -> LLMClient:
    """Return the client matching cfg.provider."""
    if cfg.provider == PROVIDER_OPENAI:
        from openai_client import OpenAIClient
        return OpenAIClient(cfg)
    # default + explicit "anthropic"
    from claude_client import ClaudeClient
    return ClaudeClient(cfg)
