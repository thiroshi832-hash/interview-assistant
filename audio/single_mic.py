"""
Helper-laptop audio source: a single microphone that hears both voices
(candidate + interviewer-from-speakers). Speaker is left as None; the
diarizer + auto-labeler decide who said what downstream.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import pyaudiowpatch as pyaudio  # type: ignore

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

        # Use the mic the user selected (Audio devices…), not just the Windows
        # default — the default is often a virtual-audio device that carries
        # system playback rather than the room mic that hears the candidate.
        mic_info = self._get_input_device(self.cfg.mic_device_index)
        rate = int(mic_info["defaultSampleRate"])
        # Open at the device's NATIVE channel count and downmix to mono in
        # software (to_mono_16k_int16 handles channels > 1). Forcing PortAudio
        # to mono on a natively-stereo virtual device produces garbled audio.
        channels = int(mic_info["maxInputChannels"]) or 1
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

    def _get_input_device(self, index: int | None):
        """Configured input device, falling back to the system default mic."""
        assert self._pa is not None
        if index is not None:
            try:
                info = self._pa.get_device_info_by_index(int(index))
                if int(info.get("maxInputChannels", 0) or 0) > 0:
                    return info
            except Exception:
                pass
        return self._pa.get_default_input_device_info()

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
