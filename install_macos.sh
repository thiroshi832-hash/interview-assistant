#!/usr/bin/env bash
# Install AetherStack Interview Assistant on macOS.
# Usage:  chmod +x install_macos.sh && ./install_macos.sh
#
# Prerequisites:
#   - Python 3.10+ (brew install python@3.12)
#   - PortAudio (brew install portaudio)
#   - For system audio loopback: BlackHole (brew install blackhole-2ch)
#
# This handles the webrtcvad build (needs Xcode CLT) and resemblyzer's
# pinned dependency on webrtcvad.

set -euo pipefail

echo "[1/4] Checking prerequisites..."
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install via: brew install python@3.12"
    exit 1
fi
if ! brew list portaudio &>/dev/null 2>&1; then
    echo "Warning: portaudio not found. Installing via Homebrew..."
    brew install portaudio
fi

echo "[2/4] Installing main dependencies..."
python3 -m pip install --disable-pip-version-check -r requirements_macos.txt

echo "[3/4] Installing resemblyzer without its webrtcvad dep..."
python3 -m pip install --disable-pip-version-check --no-deps resemblyzer

echo "[4/4] Verifying imports..."
python3 -c "import resemblyzer, librosa, pyaudio, anthropic, PySide6; print('All deps importable.')"

echo ""
echo "Done. Set ANTHROPIC_API_KEY then run:"
echo '  export ANTHROPIC_API_KEY="sk-ant-..."'
echo "  python3 app.py"
echo ""
echo "For system audio capture (interviewer's voice from Zoom/Teams/Meet):"
echo "  1. Install BlackHole: brew install blackhole-2ch"
echo "  2. Open Audio MIDI Setup → create Multi-Output Device"
echo "     (combine your speakers + BlackHole 2ch)"
echo "  3. Set Multi-Output Device as system output"
echo "  4. In the app, select 'BlackHole 2ch' as the loopback device"
