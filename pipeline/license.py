"""
Trial + license management.

30-day trial begins on first launch (stored in config.json as `first_run_at`).
After expiry the user must enter a valid license key or the app exits.

The secret here ships in the binary — a determined user can extract it and
generate keys themselves. This is a casual trial-lock, not crypto-grade DRM.
Adequate for "share with friends, ask people to pay if they keep using it".

To generate a new valid key, run:
    python -m pipeline.license
"""
from __future__ import annotations

import hmac
import hashlib
import re
import time


# Secret used to derive license keys. Trivially extractable from the .exe.
_LICENSE_SECRET = b"aetherstack-interview-assistant-v1-license-secret"
_LICENSE_PAYLOAD = b"aetherstack-interview-assistant"

TRIAL_DAYS = 30


# ── trial accounting ─────────────────────────────────────────────────────────

def days_remaining(first_run_at: float, now: float | None = None) -> int:
    """How many trial days are left. Capped at 0."""
    if first_run_at <= 0:
        return TRIAL_DAYS
    now = now if now is not None else time.time()
    elapsed_days = (now - first_run_at) / 86400.0
    return max(0, TRIAL_DAYS - int(elapsed_days))


def trial_expired(first_run_at: float, now: float | None = None) -> bool:
    return days_remaining(first_run_at, now) <= 0


# ── license key validation ───────────────────────────────────────────────────

def _normalize(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", key).upper()


def _expected_key_raw() -> str:
    """The valid 24-char key — derived deterministically from the secret."""
    return hmac.new(
        _LICENSE_SECRET, _LICENSE_PAYLOAD, hashlib.sha256,
    ).hexdigest()[:24].upper()


def make_license_key() -> str:
    """Generate a pretty-formatted license key. Use offline to mint keys."""
    raw = _expected_key_raw()
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def is_valid_license(key: str) -> bool:
    """Check whether `key` matches the embedded license. Whitespace + dashes ignored."""
    norm = _normalize(key)
    expected = _expected_key_raw()
    if len(norm) != len(expected):
        return False
    return hmac.compare_digest(norm, expected)


if __name__ == "__main__":
    print("Valid license key:")
    print(" ", make_license_key())
