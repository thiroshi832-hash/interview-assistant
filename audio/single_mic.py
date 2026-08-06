"""
Helper-laptop audio source: a single microphone that hears both voices
(candidate + interviewer-from-speakers). Speaker is left as None; the
diarizer + auto-labeler decide who said what downstream.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from audio.pyaudio_compat import pyaudio

from audio.source import AudioChunk, AudioSource
from audio._pcm import to_mono_16k_int16
from config import Config


_FRAMES_PER_BUFFER = 1024


class SingleMicSource(AudioSource):
    """Single mic, mixed voices, speaker resolved downstream."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pa = pyaudio.PyAudio()

        mic_info = self._pa.get_default_input_device_info()
        rate = int(mic_info["defaultSampleRate"])
        channels = min(int(mic_info["maxInputChannels"]), 1) or 1
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=_FRAMES_PER_BUFFER,
            input_device_index=int(mic_info["index"]),
        )
        self._thread = threading.Thread(
            target=self._reader, args=(self._stream, rate, channels), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass

    def _reader(self, stream, rate: int, channels: int) -> None:
        target_rate = self.cfg.sample_rate
        while self._running:
            try:
                raw = stream.read(_FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception:
                break
            pcm = to_mono_16k_int16(
                raw, sample_rate=rate, channels=channels,
                sample_format="int16", target_rate=target_rate,
            )
            try:
                self.queue.put_nowait(AudioChunk(pcm=pcm, speaker=None, ts=time.time()))
            except Exception:
                pass
