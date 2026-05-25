"""
AetherStack Sender — companion app for helper-laptop mode.

Runs on the interview computer. Captures mic + WASAPI loopback, streams the
two cleanly-tagged audio streams over a single WebSocket to the receiver
(AetherStack Interview Assistant running on the helper laptop).

Wire format (binary frames):
    byte 0       : speaker tag — 0x01 = candidate (mic), 0x02 = interviewer (loopback)
    bytes 1..end : 16 kHz mono int16 PCM
"""
