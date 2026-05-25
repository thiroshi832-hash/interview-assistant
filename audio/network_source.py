"""
NetworkAudioSource — receives mic + WASAPI-loopback audio over a WebSocket
from the AetherStack sender running on a different machine.

Wire format (binary WebSocket frames):
  byte 0       : speaker tag — 0x01 = candidate (mic), 0x02 = interviewer (loopback)
  bytes 1..end : 16 kHz mono int16 PCM

This gives helper-laptop mode the same advantages as same-laptop mode:
- Two cleanly-separated streams
- Per-stream speaker is KNOWN (no diarization, no embedding, no swap needed)
- No echo from acoustic capture
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from audio.source import AudioChunk, AudioSource
from config import Config


_TAG_CANDIDATE = 0x01
_TAG_INTERVIEWER = 0x02


class NetworkAudioSource(AudioSource):
    """Connects to `ws://host:port` and pushes received chunks into source.queue."""

    def __init__(self, cfg: Config, host: str, port: int):
        super().__init__()
        self.cfg = cfg
        self.host = host
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_evt.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    # ── async machinery ────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        try:
            asyncio.run(self._connect_and_pump())
        except Exception:
            pass

    async def _connect_and_pump(self) -> None:
        self._loop = asyncio.get_event_loop()
        import websockets  # type: ignore
        url = f"ws://{self.host}:{self.port}"
        # Auto-reconnect loop — try forever (or until stop). 2 s backoff.
        while not self._stop_evt.is_set():
            try:
                async with websockets.connect(
                    url, max_size=None, ping_interval=20, ping_timeout=15,
                ) as ws:
                    await self._pump(ws)
            except Exception:
                if self._stop_evt.is_set():
                    return
                await asyncio.sleep(2.0)

    async def _pump(self, ws) -> None:
        while not self._stop_evt.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return
            if not isinstance(msg, (bytes, bytearray)) or len(msg) < 2:
                continue
            tag = msg[0]
            pcm = bytes(msg[1:])
            if tag == _TAG_CANDIDATE:
                speaker = "candidate"
            elif tag == _TAG_INTERVIEWER:
                speaker = "interviewer"
            else:
                continue
            try:
                self.queue.put_nowait(
                    AudioChunk(pcm=pcm, speaker=speaker, ts=time.time())
                )
            except Exception:
                pass  # queue full — drop
