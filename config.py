"""
Configuration for AetherStack Interview Assistant.

Reads from environment variables first, then ~/.interview_assistant/config.json.
(Config directory name stays as `.interview_assistant` to preserve continuity
for existing installs — only the human-visible brand changed.)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path


CONFIG_DIR = Path.home() / ".interview_assistant"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    # ── Licensing / trial ─────────────────────────────────────────────────────
    # Timestamp of the first time the app launched (seconds since epoch).
    # Drives the 30-day trial counter. 0 means "this is the first run".
    first_run_at: float = 0.0
    # If non-empty AND valid, the trial gate is skipped permanently.
    license_key: str = ""

    # ── Provider ──────────────────────────────────────────────────────────────
    provider: str = "anthropic"              # "anthropic" or "openai"
    max_tokens: int = 2048

    # ── Anthropic ─────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    model: str = "claude-opus-4-7"          # switch to "claude-sonnet-4-6" for lower latency
    deep_model: str = "claude-opus-4-7"     # used by the "deeper answer" hotkey
    effort: str = "low"                      # low | medium | high | max — low keeps live answers fast

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"        # fast default for live answers
    openai_deep_model: str = "gpt-4o"        # used by the "deeper answer" hotkey

    # ── STT ───────────────────────────────────────────────────────────────────
    # "whispercpp" = pywhispercpp streaming (partials during speech, CPU-friendly)
    # "deepgram"   = cloud streaming (lowest latency, requires API key + internet)
    # (legacy "batch" / faster-whisper was removed in the slim-down — any saved
    #  config with "batch" is auto-migrated to "whispercpp" at startup.)
    stt_engine: str = "whispercpp"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"           # nova-3 is current best for english
    # base.en is ~3x faster than small.en on CPU with only a small accuracy
    # drop for native-English speech — good live-interview tradeoff. Drop to
    # tiny.en if you need maximum speed (5x faster than small.en) or bump to
    # small.en/medium.en if you have a fast CPU or a GPU.
    whisper_model: str = "base.en"           # tiny.en, base.en, small.en, medium.en, large-v3
    whisper_device: str = "cpu"              # "cpu" or "cuda" — auto-detect requires CUDA DLLs even on no-GPU machines
    whisper_compute: str = "int8"            # int8 (CPU), float16 (GPU), float32

    # ── Audio ─────────────────────────────────────────────────────────────────
    sample_rate: int = 16000
    chunk_ms: int = 30
    mic_device_index: int | None = None       # PyAudio input device index; None = default mic
    loopback_device_index: int | None = None  # PyAudio WASAPI loopback index; None = default output loopback

    # ── Network audio (helper-network mode) ───────────────────────────────────
    # When mode == MODE_HELPER_NETWORK, the receiver connects to the sender
    # app running on the interview computer over a plain WebSocket. Filled in
    # by the NetworkConnectDialog at startup and persisted for next launch.
    network_host: str = ""
    network_port: int = 8765

    # ── VAD ───────────────────────────────────────────────────────────────────
    vad_silence_ms: int = 800                # silence to close a speech segment
    vad_min_speech_ms: int = 500             # discard back-channel "yeah", "mhm"
    # Silero-vad output probability threshold for "this window is speech".
    # 0.5 is the model default and works well for direct-mic audio. Lower it
    # to ~0.35 if you're capturing acoustically (helper-laptop mode, talking
    # across a desk) and quiet speech gets missed.
    vad_threshold: float = 0.35

    # ── Diarization (single-mic mode) ─────────────────────────────────────────
    auto_label_min_utterances: int = 3       # turns to observe per cluster before locking labels

    # Optional pre-enrolled voice embedding. When present, helper-laptop mode
    # uses anchor-based speaker recognition (instant, no warmup) instead of
    # behavioural auto-labeling. List of 256 floats produced by VoiceEnroll.
    candidate_voice_embedding: list[float] = field(default_factory=list)

    # ── Question detection ────────────────────────────────────────────────────
    question_silence_ms: int = 1200          # interviewer-finished-talking heuristic (silence-net trigger)

    # ── Conversation context ──────────────────────────────────────────────────
    rolling_turns: int = 8                   # how many prior turns to send to Claude

    # ── UI ────────────────────────────────────────────────────────────────────
    answer_font_size: int = 16               # pixels; the A− / A+ buttons persist here
    transcript_collapsed: bool = False

    # ── Interview metadata (filled in by the UI before "Start") ───────────────
    job_title: str = ""
    job_description: str = ""
    # Personal context the resume doesn't cover — salary expectations, start date,
    # hobbies, work-style preferences, anything else the LLM should know about
    # the candidate. Fed into the system prompt alongside the resume.
    personal_context: str = ""

    def __post_init__(self) -> None:
        self._apply_env()

    def _apply_env(self) -> None:
        a_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if a_key:
            self.anthropic_api_key = a_key
        o_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if o_key:
            self.openai_api_key = o_key
        d_key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
        if d_key:
            self.deepgram_api_key = d_key

    # ── persistence ───────────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (json.JSONDecodeError, OSError):
                pass
        # Env vars always win over the config file
        cfg._apply_env()
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Don't persist a key to disk if it came from the environment
        data = asdict(self)
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            data["anthropic_api_key"] = ""
        if os.environ.get("OPENAI_API_KEY", "").strip():
            data["openai_api_key"] = ""
        if os.environ.get("DEEPGRAM_API_KEY", "").strip():
            data["deepgram_api_key"] = ""
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ── helpers ───────────────────────────────────────────────────────────────
    def active_api_key(self) -> str:
        return self.openai_api_key if self.provider == "openai" else self.anthropic_api_key
