"""
Direct ONNX-runtime wrapper around silero-vad.

Replaces the silero-vad pip package (which depends on torch — 320 MB).
We load the ONNX model file directly and run inference via onnxruntime,
which is already a transitive dependency we need anyway.

The model expects:
  input: float32 audio, shape (batch=1, samples=512) at 16 kHz
  state: float32 LSTM state, shape (2, 1, 128) — persist between calls
  sr:    int64 sample rate scalar (16000)
Returns:
  output: probability tensor of shape (1, 1)
  stateN: updated state tensor
"""
from __future__ import annotations

import numpy as np
import onnxruntime as ort

from paths import asset


class OnnxVAD:
    """Stateful silero-vad. One instance per concurrent speaker stream."""

    def __init__(self):
        # Lightweight session — single-threaded since we already process
        # per-speaker streams on separate consumer threads upstream.
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3   # silence info/warning prints
        self._session = ort.InferenceSession(
            asset("silero_vad.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.reset()

    def reset(self) -> None:
        # LSTM hidden state — persist across windows so the model has memory.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, window_int16: np.ndarray, sample_rate: int) -> float:
        """
        Run one 512-sample window through silero-vad.
        `window_int16` must be int16 PCM, shape (512,), 16 kHz.
        Returns the speech probability in [0, 1].
        """
        # Normalize to float32 [-1, 1], add batch dim
        wav = (window_int16.astype(np.float32) / 32768.0)[np.newaxis, :]
        out, new_state = self._session.run(
            None,
            {
                "input": wav,
                "state": self._state,
                "sr": np.array(sample_rate, dtype=np.int64),
            },
        )
        self._state = new_state
        return float(out[0][0])
