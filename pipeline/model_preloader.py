"""
Pre-load STT / VAD / speaker-embedding models before the interview starts so
the first audio segment doesn't sit in a queue while gigabytes download.

Reports progress to a callback so the UI can show a download status view.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from config import Config


# Per-status callback signature: fn(step_name, message)
StatusFn = Callable[[str, str], None]
# Per-byte callback signature: fn(bytes_downloaded)
BytesFn = Callable[[int], None]


_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def _hf_repo_size(repo_dir_name: str) -> int:
    """Sum the bytes of all files in a HuggingFace cache subdir, or 0 if absent."""
    p = _HF_CACHE / repo_dir_name
    if not p.exists():
        return 0
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _whisper_cache_dir(model_id: str) -> str:
    return f"models--Systran--faster-whisper-{model_id}"


def _whispercpp_cache_dir() -> str:
    # pywhispercpp pulls ggml-*.bin files from this single HF repo.
    return "models--ggerganov--whisper.cpp"


def whisper_is_cached(model_id: str) -> bool:
    """Heuristic: if the model dir exists and contains at least 1 MB, it's downloaded."""
    return _hf_repo_size(_whisper_cache_dir(model_id)) > 1 * 1024 * 1024


def whispercpp_is_cached(model_id: str) -> bool:
    # The cache dir contains ALL whisper.cpp models, so size alone is a coarse signal.
    # Better: look for a file containing the model_id (e.g. "ggml-base.en.bin").
    p = _HF_CACHE / _whispercpp_cache_dir()
    if not p.exists():
        return False
    needle = model_id.replace("ggml-", "")
    try:
        for f in p.rglob("*.bin"):
            if needle in f.name and f.stat().st_size > 1024 * 1024:
                return True
    except Exception:
        pass
    return False


class ModelPreloader:
    """
    Initializes models in a background worker. Emits status updates and (when
    downloading Whisper) byte counts for progress display.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        need_resemblyzer: bool,
        llm_warmup: Callable[[], None] | None = None,
    ):
        self.cfg = cfg
        self.need_resemblyzer = need_resemblyzer
        self.cancel = threading.Event()
        self.error: str | None = None
        # If provided, called after STT/VAD/resemblyzer are loaded. Used to
        # pre-warm the LLM provider's HTTPS connection + prompt cache so the
        # first interview answer streams as fast as subsequent ones.
        self.llm_warmup = llm_warmup

    def _preload_local_whisper(self, engine: str, on_status: StatusFn, on_bytes: BytesFn) -> None:
        # After the slim-down only whisper.cpp is on-device. "batch" is auto-
        # migrated to "whispercpp" by the engine factory.
        model_id = self.cfg.whisper_model
        cached = whispercpp_is_cached(model_id)
        cache_dir = _whispercpp_cache_dir()

        if cached:
            on_status("stt", f"Loading whisper.cpp model ({model_id}) from cache...")
        else:
            on_status(
                "stt",
                f"Downloading whisper.cpp model ({model_id})... "
                f"One-time download, happens in the background.",
            )
        poll_stop = threading.Event()

        def poll():
            while not poll_stop.is_set() and not self.cancel.is_set():
                on_bytes(_hf_repo_size(cache_dir))
                time.sleep(0.4)

        poll_thread = threading.Thread(target=poll, daemon=True)
        if not cached:
            poll_thread.start()

        try:
            from pywhispercpp.model import Model  # type: ignore
            Model(
                model_id,
                print_realtime=False,
                print_progress=False,
                # `None` -> pywhispercpp redirects to a real os.devnull file
                # (has a valid OS file descriptor). An in-memory io.StringIO()
                # doesn't, and current pywhispercpp does an OS-level fd dup2
                # onto it (it only checks hasattr(stream, "fileno"), which is
                # True for StringIO even though calling it raises), crashing
                # with io.UnsupportedOperation: fileno.
                redirect_whispercpp_logs_to=None,
            )
        except Exception as e:
            self.error = f"Could not load STT model: {e}"
            return
        finally:
            poll_stop.set()

    def run(self, on_status: StatusFn, on_bytes: BytesFn) -> None:
        # 1) STT model (engine-specific). Deepgram needs nothing pre-loaded.
        engine = (self.cfg.stt_engine or "batch").lower()
        if engine == "deepgram":
            on_status("stt", "Using Deepgram cloud — no local model to load.")
        else:
            self._preload_local_whisper(engine, on_status, on_bytes)
            if self.error or self.cancel.is_set():
                return

        # 2) ONNX-runtime silero-vad — bundled file, no download.
        on_status("vad", "Loading voice activity detector...")
        try:
            from pipeline.onnx_vad import OnnxVAD
            OnnxVAD()
        except Exception as e:
            self.error = f"Could not load VAD: {e}"
            return

        if self.cancel.is_set():
            return

        # 3) Resemblyzer voice encoder (only if helper mode might use it)
        if self.need_resemblyzer:
            on_status("voice", "Loading speaker recognition model...")
            try:
                from resemblyzer import VoiceEncoder
                VoiceEncoder("cpu", verbose=False)
            except Exception as e:
                self.error = f"Could not load voice encoder: {e}"
                return

        if self.cancel.is_set():
            return

        # 4) LLM warm-up (optional) — populates the prompt cache so the FIRST
        # interview answer doesn't pay the cache-miss penalty (~2-4 s slower).
        if self.llm_warmup is not None:
            on_status("llm", "Warming up the LLM — first answer ~2 s faster…")
            try:
                self.llm_warmup()
            except Exception:
                # Warmup is best-effort. A failure here just means the first
                # real answer pays the cache-write cost. Don't fail the load.
                pass

        on_status("done", "All models loaded.")
