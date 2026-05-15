"""
Resource-path helper. Works both in source-tree runs and in PyInstaller bundles.

PyInstaller's onedir build extracts data files alongside the .exe in `_internal/`
(or the .exe folder root, depending on version). `sys._MEIPASS` is set in the
bundle and points at that base path. In source, we fall back to the repo root.
"""
from __future__ import annotations

import os
import sys


def _base_path() -> str:
    # PyInstaller bundles set _MEIPASS at runtime
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    # Source tree: this file lives at <repo>/paths.py
    return os.path.dirname(os.path.abspath(__file__))


def asset(*parts: str) -> str:
    """Return an absolute path to a file under the bundled `assets/` directory."""
    return os.path.join(_base_path(), "assets", *parts)


def icon_path(ext: str = "png") -> str:
    """Path to the app icon. `ext` is 'png' (Qt) or 'ico' (Windows .exe)."""
    return asset(f"aetherstack-icon.{ext}")
