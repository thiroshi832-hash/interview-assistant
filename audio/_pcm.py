"""
Helpers: float/int conversion, channel downmix, resampling.

Kept dependency-light: just numpy + scipy.signal.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly


def to_mono_16k_int16(raw: bytes, *, sample_rate: int, channels: int, sample_format: str, target_rate: int = 16000) -> bytes:
    """
    Convert a PCM buffer of arbitrary format/rate/channels to 16-bit mono `target_rate` Hz.

    `sample_format` is one of: 'int16', 'int32', 'float32'.
    """
    if sample_format == "int16":
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_format == "int32":
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sample_format == "float32":
        arr = np.frombuffer(raw, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported sample format: {sample_format}")

    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)

    if sample_rate != target_rate:
        # rational resampling — fast and high quality for speech
        from math import gcd
        g = gcd(sample_rate, target_rate)
        up = target_rate // g
        down = sample_rate // g
        arr = resample_poly(arr, up, down).astype(np.float32)

    arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
    return arr.tobytes()
