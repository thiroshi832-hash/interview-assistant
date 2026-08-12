"""
Cross-platform PyAudio compatibility layer.

On Windows, imports pyaudiowpatch (PyAudio fork with WASAPI loopback support).
On macOS/Linux, imports standard pyaudio. System-audio loopback on macOS
requires a virtual audio device (e.g. BlackHole) configured as an input —
the app sees it as a normal input device, no special API needed.
"""
from __future__ import annotations

import sys
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    import pyaudiowpatch as pyaudio  # type: ignore
else:
    import pyaudio  # type: ignore


paInt16 = pyaudio.paInt16
PyAudio = pyaudio.PyAudio


def get_loopback_devices(pa: pyaudio.PyAudio) -> list[dict[str, Any]]:
    """Return loopback devices. On Windows, uses WASAPI loopback enumeration.
    On macOS, returns all input devices (the user picks their virtual audio
    device, e.g. BlackHole, as the loopback source)."""
    if IS_WINDOWS:
        try:
            return list(pa.get_loopback_device_info_generator())  # type: ignore[attr-defined]
        except Exception:
            return []
    # macOS / Linux: return all input devices — the user selects the virtual
    # audio device (BlackHole, Soundflower, etc.) from this list.
    devices = []
    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
            if int(info.get("maxInputChannels", 0) or 0) > 0:
                devices.append(info)
        except Exception:
            continue
    return devices


# Input devices whose name contains one of these is a virtual audio device
# carrying system output — the macOS stand-in for WASAPI loopback.
_VIRTUAL_DEVICE_HINTS = (
    "blackhole", "soundflower", "loopback audio", "vb-cable", "multi-output",
)


def get_default_loopback(pa: pyaudio.PyAudio) -> dict[str, Any] | None:
    """Return the default loopback device. On Windows, uses the WASAPI default
    loopback. On macOS, auto-detects an installed virtual audio device
    (BlackHole, Soundflower, …); returns None if none is installed."""
    if IS_WINDOWS:
        try:
            return pa.get_default_wasapi_loopback()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            default_out = pa.get_default_output_device_info()
            name = default_out["name"]
            for d in pa.get_loopback_device_info_generator():  # type: ignore[attr-defined]
                if name in d["name"]:
                    return d
        except Exception:
            pass
        return None
    # macOS: no WASAPI equivalent. Pick a virtual audio device by name if the
    # user installed one, so the common BlackHole setup works out of the box.
    for info in get_loopback_devices(pa):
        name = str(info.get("name", "")).lower()
        if any(hint in name for hint in _VIRTUAL_DEVICE_HINTS):
            return info
    return None


def loopback_hint() -> str:
    """Platform-specific hint text for the loopback device picker."""
    if IS_WINDOWS:
        return (
            "Pick the microphone for your voice and the Windows loopback device "
            "that carries the interviewer's audio."
        )
    return (
        "Pick the microphone for your voice and the virtual audio device "
        "(e.g. BlackHole) that carries the interviewer's audio. "
        "See https://github.com/ExistentialAudio/BlackHole for setup."
    )


def loopback_footer_hint() -> str:
    """Platform-specific footer hint for the audio device dialog."""
    if IS_WINDOWS:
        return (
            "Tip: if Zoom/Teams/Meet is routed to a specific speaker in Windows "
            "Volume Mixer, choose that speaker's loopback here."
        )
    return (
        "Tip: create a Multi-Output Device in Audio MIDI Setup that combines "
        "your speakers + BlackHole, then select BlackHole as the loopback input here."
    )


def no_loopback_error() -> str:
    """Platform-specific error when no loopback device is found."""
    if IS_WINDOWS:
        return (
            "No WASAPI loopback device found. Make sure you're on Windows and "
            "system audio is not muted."
        )
    return (
        "No loopback device configured. Install a virtual audio device like "
        "BlackHole and select it in Audio Devices settings."
    )


def audio_watchdog_message() -> str:
    """Platform-specific message when no audio is detected after startup."""
    if IS_WINDOWS:
        return (
            "⚠ No audio detected — check (1) Windows Sound settings → "
            "Input device, (2) Settings → Privacy → Microphone → "
            "'Allow desktop apps to access your microphone', "
            "(3) the mic isn't muted in the volume mixer."
        )
    return (
        "⚠ No audio detected — check (1) System Settings → Sound → "
        "Input device, (2) System Settings → Privacy & Security → "
        "Microphone → allow this app, "
        "(3) the mic isn't muted."
    )


def tray_not_available_message() -> str:
    """Platform-specific message when system tray is unavailable."""
    if IS_WINDOWS:
        return (
            "System tray is not available on this system. "
            "Enable tray icons in Windows settings and try again."
        )
    return (
        "Menu bar is not available on this system."
    )
