"""
Audio capture + WebSocket broadcast for the AetherStack Sender.

Architecture:
- Two background capture threads (mic + WASAPI loopback), same as the
  receiver's same-laptop DualStreamSource. Each one downmixes to 16 kHz
  mono int16, prepends the speaker-tag byte, and pushes onto an asyncio
  Queue.
- One asyncio task drains the queue and broadcasts each frame to every
  connected WebSocket client.
- One asyncio websocket server task accepts new clients.

The Qt UI calls `start(port, mic_idx, loopback_idx)` / `stop()` on the
main thread; everything else runs in a worker thread that owns its own
asyncio event loop.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

import numpy as np
from audio.pyaudio_compat import pyaudio, get_loopback_devices, get_default_loopback, no_loopback_error
from audio._pcm import to_mono_16k_int16


_TAG_CANDIDATE = 0x01
_TAG_INTERVIEWER = 0x02
_FRAMES_PER_BUFFER = 1024
_TARGET_RATE = 16000

# Cap the broadcast queue so a slow / dropped client doesn't pile up
# audio forever. 200 frames @ 64 ms ≈ 13 s — plenty of headroom for a
# brief network blip, but bounded.
_BROADCAST_QUEUE_MAX = 200


class SenderStreamer:
    """
    Captures mic + loopback, runs a WebSocket server, broadcasts framed PCM
    to every connected client.

    Public callbacks (all fired from the worker thread — UI should marshal
    onto the Qt main thread with QueuedConnection or a Signal):
        on_status(str)              — human-readable status line
        on_client_count(int)        — # of currently connected clients
        on_level(side, int 0..100)  — RMS level for "mic" or "loopback"
    """

    def __init__(
        self,
        on_status: Optional[Callable[[str], None]] = None,
        on_client_count: Optional[Callable[[int], None]] = None,
        on_level: Optional[Callable[[str, int], None]] = None,
    ):
        self.on_status = on_status or (lambda _msg: None)
        self.on_client_count = on_client_count or (lambda _n: None)
        self.on_level = on_level or (lambda _side, _lvl: None)

        self._running = False
        self._port: int = 8765
        self._mic_idx: Optional[int] = None
        self._loop_idx: Optional[int] = None

        self._pa: Optional[pyaudio.PyAudio] = None
        self._mic_stream = None
        self._loop_stream = None
        self._cap_threads: list[threading.Thread] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._broadcast_queue: Optional[asyncio.Queue] = None
        self._clients: set = set()
        self._server = None
        self._stop_evt = threading.Event()
        self._last_level_emit: dict[str, float] = {"mic": 0.0, "loopback": 0.0}

    # ── public API ───────────────────────────────────────────────────────
    def start(self, port: int, mic_idx: Optional[int], loopback_idx: Optional[int]) -> None:
        if self._running:
            return
        self._port = int(port)
        self._mic_idx = mic_idx
        self._loop_idx = loopback_idx
        self._stop_evt.clear()
        self._running = True

        # asyncio loop in a worker thread
        ready = threading.Event()

        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._broadcast_queue = asyncio.Queue(maxsize=_BROADCAST_QUEUE_MAX)
            try:
                self._loop.run_until_complete(self._async_main(ready))
            except Exception as e:
                self.on_status(f"Server crashed: {e}")
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True)
        self._loop_thread.start()
        # Block briefly so the audio threads only start once the WS server
        # is listening — otherwise the first ~second of frames vanishes.
        ready.wait(timeout=5.0)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_evt.set()
        # Stop capture first (no more frames produced).
        for stream in (self._mic_stream, self._loop_stream):
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
        self._mic_stream = None
        self._loop_stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        # Stop the asyncio loop (closes server + drops clients).
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        self._cap_threads = []
        self.on_client_count(0)
        self.on_status("Stopped.")

    # ── async main ───────────────────────────────────────────────────────
    async def _async_main(self, ready: threading.Event) -> None:
        import websockets  # type: ignore

        async def handler(ws, *_path_args):
            # websockets >=11 calls the handler with just `ws`; older
            # versions also pass `path`. Accept both via *_path_args.
            self._clients.add(ws)
            self.on_client_count(len(self._clients))
            self.on_status(f"Client connected ({ws.remote_address[0]}).")
            try:
                # We only send — drop anything the client sends.
                async for _ in ws:
                    pass
            except Exception:
                pass
            finally:
                self._clients.discard(ws)
                self.on_client_count(len(self._clients))
                self.on_status("Client disconnected.")

        try:
            self._server = await websockets.serve(
                handler, host="0.0.0.0", port=self._port,
                max_size=None, ping_interval=20, ping_timeout=15,
            )
        except Exception as e:
            self.on_status(f"Could not bind port {self._port}: {e}")
            ready.set()
            return

        # Now start audio capture (so we don't drop the first frames).
        try:
            self._start_audio()
        except Exception as e:
            self.on_status(f"Audio start failed: {e}")
            ready.set()
            return

        self.on_status(f"Listening on 0.0.0.0:{self._port} — share this port with the helper laptop.")
        ready.set()

        # Drain the broadcast queue forever.
        try:
            await self._broadcast_loop()
        finally:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass

    async def _broadcast_loop(self) -> None:
        assert self._broadcast_queue is not None
        while not self._stop_evt.is_set():
            try:
                frame = await asyncio.wait_for(self._broadcast_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not self._clients:
                continue
            # Snapshot clients so failed sends don't mutate during iteration.
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send(frame)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
            if dead:
                self.on_client_count(len(self._clients))

    # ── audio capture ────────────────────────────────────────────────────
    def _start_audio(self) -> None:
        self._pa = pyaudio.PyAudio()

        # Mic stream → tag 0x01 (candidate)
        mic_info = self._get_input_device(self._mic_idx)
        mic_rate = int(mic_info["defaultSampleRate"])
        mic_channels = min(int(mic_info["maxInputChannels"]), 1) or 1
        self._mic_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=mic_channels,
            rate=mic_rate,
            input=True,
            frames_per_buffer=_FRAMES_PER_BUFFER,
            input_device_index=int(mic_info["index"]),
        )
        t_mic = threading.Thread(
            target=self._capture_reader,
            args=(self._mic_stream, mic_rate, mic_channels, "int16",
                  _TAG_CANDIDATE, "mic"),
            daemon=True,
        )
        t_mic.start()
        self._cap_threads.append(t_mic)

        # Loopback stream → tag 0x02 (interviewer)
        loopback = self._get_loopback_device(self._loop_idx)
        if loopback is None:
            raise RuntimeError(no_loopback_error())
        loop_rate = int(loopback["defaultSampleRate"])
        loop_channels = int(loopback["maxInputChannels"]) or 2
        self._loop_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=loop_channels,
            rate=loop_rate,
            input=True,
            frames_per_buffer=_FRAMES_PER_BUFFER,
            input_device_index=int(loopback["index"]),
        )
        t_loop = threading.Thread(
            target=self._capture_reader,
            args=(self._loop_stream, loop_rate, loop_channels, "int16",
                  _TAG_INTERVIEWER, "loopback"),
            daemon=True,
        )
        t_loop.start()
        self._cap_threads.append(t_loop)

    def _capture_reader(self, stream, rate: int, channels: int, fmt: str,
                        tag: int, side: str) -> None:
        prefix = bytes([tag])
        while self._running and not self._stop_evt.is_set():
            try:
                raw = stream.read(_FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception:
                break
            try:
                pcm = to_mono_16k_int16(
                    raw, sample_rate=rate, channels=channels,
                    sample_format=fmt, target_rate=_TARGET_RATE,
                )
            except Exception:
                continue
            self._emit_level(side, pcm)
            self._enqueue(prefix + pcm)

    def _enqueue(self, frame: bytes) -> None:
        """Threadsafe push onto the asyncio broadcast queue."""
        loop = self._loop
        q = self._broadcast_queue
        if loop is None or q is None:
            return
        try:
            loop.call_soon_threadsafe(self._try_put_nowait, q, frame)
        except RuntimeError:
            pass  # loop closing

    @staticmethod
    def _try_put_nowait(q: asyncio.Queue, frame: bytes) -> None:
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            # drop oldest, push newest — keeps us near-real-time under back-pressure
            try:
                q.get_nowait()
                q.put_nowait(frame)
            except Exception:
                pass

    def _emit_level(self, side: str, pcm: bytes) -> None:
        now = time.time()
        if now - self._last_level_emit.get(side, 0.0) < 0.1:
            return
        try:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            if len(arr) == 0:
                return
            rms = float(np.sqrt(np.mean(arr * arr)))
            import math
            level = int(min(100, max(0, 20 * math.log10(max(rms, 1.0) / 32.0))))
            self.on_level(side, level)
            self._last_level_emit[side] = now
        except Exception:
            pass

    # ── device discovery ─────────────────────────────────────────────────
    def _get_input_device(self, index: Optional[int]):
        assert self._pa is not None
        if index is not None:
            try:
                info = self._pa.get_device_info_by_index(int(index))
                if int(info.get("maxInputChannels", 0) or 0) > 0:
                    return info
            except Exception:
                pass
        return self._pa.get_default_input_device_info()

    def _get_loopback_device(self, index: Optional[int]):
        assert self._pa is not None
        if index is not None:
            try:
                for info in get_loopback_devices(self._pa):
                    if int(info.get("index", -1)) == int(index):
                        return info
            except Exception:
                pass
        return get_default_loopback(self._pa)

    # ── device enumeration helpers (used by the UI) ──────────────────────
    @staticmethod
    def list_devices() -> tuple[list[dict], list[dict]]:
        """
        Returns (input_devices, loopback_devices). Each item is a dict with
        keys `index`, `name`, `channels`, `rate` — safe to JSON-serialize
        and display in a combobox.
        """
        pa = pyaudio.PyAudio()
        inputs: list[dict] = []
        loopbacks: list[dict] = []
        try:
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                ch_in = int(info.get("maxInputChannels", 0) or 0)
                if ch_in > 0 and not info.get("isLoopbackDevice", False):
                    inputs.append({
                        "index": int(info["index"]),
                        "name": str(info["name"]),
                        "channels": ch_in,
                        "rate": int(info["defaultSampleRate"]),
                    })
            for info in get_loopback_devices(pa):
                loopbacks.append({
                    "index": int(info["index"]),
                    "name": str(info["name"]),
                    "channels": int(info.get("maxInputChannels", 2) or 2),
                    "rate": int(info["defaultSampleRate"]),
                })
        finally:
            try:
                pa.terminate()
            except Exception:
                pass
        return inputs, loopbacks

    @staticmethod
    def local_ips() -> list[str]:
        """
        Return all non-loopback IPv4 addresses on this machine, so the user
        knows what address to type into the helper laptop.
        """
        import socket
        ips: list[str] = []
        try:
            hostname = socket.gethostname()
            for entry in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
                ip = entry[4][0]
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        # Sort: 192.168.* and 10.* first (most likely LAN addresses)
        def rank(ip: str) -> int:
            if ip.startswith("192.168."): return 0
            if ip.startswith("10."): return 1
            if ip.startswith("172."): return 2
            return 3
        ips.sort(key=rank)
        return ips
