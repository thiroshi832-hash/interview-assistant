"""
Same-laptop audio source: captures the mic and the system loopback in parallel,
tags chunks with their known speaker.

On Windows uses pyaudiowpatch (WASAPI loopback). On macOS uses standard pyaudio
with a virtual audio device (e.g. BlackHole) for system audio capture.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from audio.pyaudio_compat import pyaudio, get_loopback_devices, get_default_loopback, no_loopback_error
from audio.source import AudioChunk, AudioSource
from audio._pcm import to_mono_16k_int16
from config import Config


_FRAMES_PER_BUFFER = 1024  # ~21 ms @ 48 kHz; ~64 ms @ 16 kHz — fine for VAD upstream


class DualStreamSource(AudioSource):
    """Mic → 'candidate', WASAPI loopback → 'interviewer'."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._pa: Optional[pyaudio.PyAudio] = None
        self._mic_stream = None
        self._loop_stream = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pa = pyaudio.PyAudio()

        # ── mic stream ────────────────────────────────────────────────────
        mic_info = self._get_input_device(self.cfg.mic_device_index)
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
        t1 = threading.Thread(
            target=self._reader,
            args=(self._mic_stream, mic_rate, mic_channels, "int16", "candidate"),
            daemon=True,
        )
        t1.start()
        self._threads.append(t1)

        # ── loopback stream ───────────────────────────────────────────────
        loopback = self._get_loopback_device(self.cfg.loopback_device_index)
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
        t2 = threading.Thread(
            target=self._reader,
            args=(self._loop_stream, loop_rate, loop_channels, "int16", "interviewer"),
            daemon=True,
        )
        t2.start()
        self._threads.append(t2)

    def stop(self) -> None:
        self._running = False
        for stream in (self._mic_stream, self._loop_stream):
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass

    # ── internals ────────────────────────────────────────────────────────
    def _get_input_device(self, index: int | None):
        """Return the configured input device, falling back to the default mic."""
        assert self._pa is not None
        if index is not None:
            try:
                info = self._pa.get_device_info_by_index(int(index))
                if int(info.get("maxInputChannels", 0) or 0) > 0:
                    return info
            except Exception:
                pass
        return self._pa.get_default_input_device_info()

    def _get_loopback_device(self, index: int | None):
        """Return the configured loopback device, falling back to platform default."""
        assert self._pa is not None
        if index is not None:
            try:
                for info in get_loopback_devices(self._pa):
                    if int(info.get("index", -1)) == int(index):
                        return info
            except Exception:
                pass
        return get_default_loopback(self._pa)

    def _reader(self, stream, rate: int, channels: int, fmt: str, speaker: str) -> None:
        target_rate = self.cfg.sample_rate
        while self._running:
            try:
                raw = stream.read(_FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception:
                break
            pcm = to_mono_16k_int16(
                raw, sample_rate=rate, channels=channels,
                sample_format=fmt, target_rate=target_rate,
            )
            try:
                self.queue.put_nowait(AudioChunk(pcm=pcm, speaker=speaker, ts=time.time()))
            except Exception:
                pass  # drop on overflow rather than block
