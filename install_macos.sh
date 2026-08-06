#!/usr/bin/env bash
# Install AetherStack Interview Assistant on macOS.
# Usage:  chmod +x install_macos.sh && ./install_macos.sh
#
# Prerequisites:
#   - Python 3.10+ (brew install python@3.12)
#   - PortAudio (brew install portaudio)
#   - For system audio loopback: BlackHole (brew install blackhole-2ch)
#
# Creates a virtual environment (.venv) in the project directory, installs
# all dependencies there, and installs PyInstaller for building.

set -euo pipefail

VENV_DIR=".venv"

echo "[1/5] Checking prerequisites..."
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install via: brew install python@3.12"
    exit 1
fi
if ! brew list portaudio &>/dev/null 2>&1; then
    echo "Warning: portaudio not found. Installing via Homebrew..."
    brew install portaudio
fi

echo "[2/5] Creating virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

echo "[3/5] Installing main dependencies + PyInstaller..."
pip install --disable-pip-version-check -r requirements_macos.txt
pip install --disable-pip-version-check pyinstaller

echo "[4/5] Installing resemblyzer without its webrtcvad dep..."
pip install --disable-pip-version-check --no-deps resemblyzer

echo "[5/5] Verifying imports..."
python -c "import resemblyzer, librosa, pyaudio, anthropic, PySide6; print('All deps importable.')"

echo ""
echo "============================================================"
echo " Done! Virtual environment created at: $VENV_DIR"
echo "============================================================"
echo ""
echo "Activate the venv before running or building:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Run from source:"
echo '  export ANTHROPIC_API_KEY="sk-ant-..."'
echo "  python app.py"
echo ""
echo "Build .app bundle:"
echo "  python scripts/make_icns.py"
echo "  pyinstaller app_macos.spec --clean --noconfirm"
echo ""
echo "For system audio capture (interviewer's voice from Zoom/Teams/Meet):"
echo "  1. Install BlackHole: brew install blackhole-2ch"
echo "  2. Open Audio MIDI Setup → create Multi-Output Device"
echo "     (combine your speakers + BlackHole 2ch)"
echo "  3. Set Multi-Output Device as system output"
echo "  4. In the app, select 'BlackHole 2ch' as the loopback device"
