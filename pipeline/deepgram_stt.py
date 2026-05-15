"""
Deepgram cloud streaming STT backend.

Lowest-latency option in the app: words appear during speech (~200-300 ms
partial latency). Requires a Deepgram API key and an internet connection.

Per-speaker WebSocket connections: in same-laptop mode (DualStreamSource)
we open two — one for the mic and one for the loopback — so each stream
stays cleanly separated. In helper-laptop mode (SingleMicSource) there's
one connection and the diarizer assigns labels post-final.

Built against `deepgram-sdk` v7 (very different from v3):
  - DeepgramClient(api_key="...")            # keyword-only
  - client.listen.v1.connect(...) -> ctx-mgr yielding V1SocketClient
  - socket.send_media(bytes)
  - socket.send_finalize()                   # force partial -> final
  - socket.send_close_stream()
  - socket.on(EventType.MESSAGE, callback)
  - socket.start_listening()                  # blocks; runs in its own thread

Each speaker stream therefore needs its own worker thread holding the `with`
block open.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from config import Config
from pipeline.stt_backend import STTBackend, STTEvent


class _SpeakerStream:
    """One Deepgram WebSocket dedicated to one speaker (mic OR loopback OR None)."""

    def __init__(
        self,
        client,
        speaker: Optional[str],
        model: str,
        endpointing_ms: int,
        on_event: Callable[[STTEvent], None],
        get_pcm_buffer: Callable[[Optional[str]], bytes],
        clear_pcm_buffer: Callable[[Optional[str]], None],
    ):
        self._client = client
        self._speaker = speaker
        self._model = model
        self._endpointing_ms = endpointing_ms
        self._on_event = on_event
        self._get_pcm = get_pcm_buffer
        self._clear_pcm = clear_pcm_buffer
        self._socket = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._open_error: Optional[Exception] = None
        self._ts_start: float = time.time()
        # Deepgram emits per-segment text, not cumulative. When `is_final=True,
        # speech_final=False` fires mid-utterance (e.g. after a phrase break),
        # the segment is locked in. We accumulate those locked segments so the
        # transcript line keeps growing across the utterance, and only emit
        # OUR final when `speech_final=True` arrives.
        self._committed_segments: str = ""

    def open(self, timeout: float = 8.0) -> bool:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._ready.wait(timeout=timeout)
        return self._socket is not None

    def _on_message(self, msg) -> None:
        # msg type can be ListenV1Results / Metadata / UtteranceEnd / SpeechStarted
        try:
            from deepgram.listen.v1.types.listen_v1results import ListenV1Results
            from deepgram.listen.v1.types.listen_v1utterance_end import ListenV1UtteranceEnd
        except Exception:
            return

        # UtteranceEnd: explicit "speaker is done" signal. More reliable than
        # speech_final flag — fires `utterance_end_ms` after the last word.
        if isinstance(msg, ListenV1UtteranceEnd):
            self._flush_as_final()
            return

        if not isinstance(msg, ListenV1Results):
            return
        try:
            alt = msg.channel.alternatives[0]
            text = (alt.transcript or "").strip()
            # Two different "final" flags in Deepgram:
            #   is_final     = this transcript SEGMENT won't change (phrase boundary)
            #   speech_final = the speaker has actually stopped talking
            speech_final = bool(getattr(msg, "speech_final", False))
            phrase_final = bool(getattr(msg, "is_final", False))
        except Exception:
            return

        # Build the cumulative text for the current utterance:
        # already-committed segments + current segment's text.
        cumulative = (self._committed_segments + " " + text).strip()
        if not cumulative:
            return

        if speech_final:
            # Speaker stopped — append and flush the cumulative text as a final.
            if text:
                self._committed_segments = (
                    self._committed_segments + " " + text
                ).strip()
            self._flush_as_final()
        elif phrase_final:
            # Segment locked in but speaker continues. Accumulate, then emit
            # the cumulative text as a partial so the UI shows growing text.
            if text:
                self._committed_segments = (
                    self._committed_segments + " " + text
                ).strip()
            self._on_event(STTEvent(
                text=self._committed_segments, speaker=self._speaker,
                ts_start=self._ts_start, ts_end=time.time(),
                is_final=False, pcm=None,
            ))
        else:
            # Pure interim partial — show committed + current in-progress text.
            self._on_event(STTEvent(
                text=cumulative, speaker=self._speaker,
                ts_start=self._ts_start, ts_end=time.time(),
                is_final=False, pcm=None,
            ))

    def _flush_as_final(self) -> None:
        """
        Emit the accumulated committed segments as a single final and reset.
        Called from either speech_final=True or UtteranceEnd, whichever
        arrives first.
        """
        text = self._committed_segments.strip()
        if not text:
            return
        pcm = self._get_pcm(self._speaker) or None
        self._clear_pcm(self._speaker)
        self._on_event(STTEvent(
            text=text, speaker=self._speaker,
            ts_start=self._ts_start, ts_end=time.time(),
            is_final=True, pcm=pcm,
        ))
        self._committed_segments = ""
        self._ts_start = time.time()

    def _on_error(self, err) -> None:
        # Best-effort: just notify the controller via a fake "_status" event
        try:
            self._on_event(STTEvent(
                text=f"[deepgram error] {err}", speaker="_status",
                ts_start=time.time(), ts_end=time.time(),
                is_final=True, pcm=None,
            ))
        except Exception:
            pass

    def _run(self) -> None:
        from deepgram.core.events import EventType
        try:
            # `endpointing` triggers `is_final=True` after this many ms of
            # silence (phrase boundary). Default 10 ms is way too eager — set
            # to ~300 ms so phrase-finals fire on real pauses, not breaths.
            # `utterance_end_ms` MUST be set (default off) for Deepgram to
            # ever fire `speech_final=True` or `UtteranceEnd` — that's the
            # signal we use to trigger Claude. Match it to our local
            # `vad_silence_ms` so the perceived "speaker is done" boundary
            # matches the other engines.
            ctx = self._client.listen.v1.connect(
                model=self._model,
                encoding="linear16",
                sample_rate=16000,
                channels=1,
                interim_results=True,
                smart_format=True,
                language="en-US",
                punctuate=True,
                vad_events=False,
                endpointing=300,
                utterance_end_ms=max(self._endpointing_ms, 1000),
            )
            with ctx as socket:
                self._socket = socket
                socket.on(EventType.MESSAGE, self._on_message)
                socket.on(EventType.ERROR, self._on_error)
                self._ready.set()
                # Blocks until the socket closes / our stop drains it.
                socket.start_listening()
        except Exception as e:
            self._open_error = e
        finally:
            self._socket = None
            self._ready.set()

    def send(self, pcm: bytes) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            sock.send_media(pcm)
        except Exception:
            # connection dropped — clear and let next feed reopen
            self._socket = None

    def finalize(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            sock.send_finalize()
        except Exception:
            pass

    def close(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.send_close_stream()
            except Exception:
                pass


class DeepgramBackend(STTBackend):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.deepgram_api_key:
            raise RuntimeError(
                "Deepgram API key not set. Paste it in STT settings or set "
                "DEEPGRAM_API_KEY in your environment."
            )
        self._client = None
        self._streams: dict[Optional[str], _SpeakerStream] = {}
        self._pending_pcm: dict[Optional[str], bytearray] = {}
        self._lock = threading.Lock()
        self._on_event: Optional[Callable[[STTEvent], None]] = None
        self._stopped = False

    def start(self, on_event):
        self._on_event = on_event
        from deepgram import DeepgramClient  # type: ignore
        # v7: api_key is keyword-only.
        self._client = DeepgramClient(api_key=self.cfg.deepgram_api_key)

    # ── per-stream helpers ──────────────────────────────────────────────────
    def _get_pcm(self, speaker: Optional[str]) -> bytes:
        return bytes(self._pending_pcm.get(speaker, bytearray()))

    def _clear_pcm(self, speaker: Optional[str]) -> None:
        self._pending_pcm.pop(speaker, None)

    def _get_stream(self, speaker: Optional[str]) -> Optional[_SpeakerStream]:
        if speaker in self._streams:
            s = self._streams[speaker]
            if s._socket is not None:
                return s
            # dead — recreate
            self._streams.pop(speaker, None)
        if self._stopped:
            return None
        with self._lock:
            if speaker in self._streams:
                return self._streams[speaker]
            assert self._client is not None and self._on_event is not None
            s = _SpeakerStream(
                self._client,
                speaker=speaker,
                model=self.cfg.deepgram_model,
                endpointing_ms=self.cfg.vad_silence_ms,
                on_event=self._on_event,
                get_pcm_buffer=self._get_pcm,
                clear_pcm_buffer=self._clear_pcm,
            )
            if not s.open():
                return None
            self._streams[speaker] = s
            return s

    # ── STTBackend interface ───────────────────────────────────────────────
    def feed(self, pcm: bytes, speaker, ts: float) -> None:
        if self._stopped:
            return
        # Retain audio (used as the final's `pcm` for downstream speaker ID).
        self._pending_pcm.setdefault(speaker, bytearray()).extend(pcm)
        stream = self._get_stream(speaker)
        if stream is None:
            return
        stream.send(pcm)

    def close_segment(self, speaker, ts: float) -> None:
        stream = self._streams.get(speaker)
        if stream is None:
            return
        stream.finalize()

    def stop(self) -> None:
        self._stopped = True
        for stream in list(self._streams.values()):
            try:
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        self._pending_pcm.clear()
