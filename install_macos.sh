#!/usr/bin/env bash
# Install AetherStack Interview Assistant on macOS.
# Usage:  chmod +x install_macos.sh && ./install_macos.sh
#
# Prerequisites:
#   - Python 3.9–3.12 (PySide6 does not yet support 3.13+)
#   - PortAudio (brew install portaudio)
#   - For system audio loopback: BlackHole (brew install blackhole-2ch)
#
# Creates a virtual environment (.venv) in the project directory, installs
# all dependencies there, and installs PyInstaller for building.

set -euo pipefail

VENV_DIR=".venv"

# ── Find a compatible Python (3.9–3.12) ──────────────────────────────────
# PySide6 requires Python <3.13. Homebrew's default `python3` may be 3.13+,
# so we look for an explicit 3.12 or 3.11 first.
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3.9; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # Fall back to python3 and check version
    if ! command -v python3 &>/dev/null; then
        echo "Error: python3 not found. Install via: brew install python@3.12"
        exit 1
    fi
    PY_VER=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_VER" -ge 13 ]; then
        echo "Error: Python 3.$PY_VER detected, but PySide6 requires Python <3.13."
        echo "Install Python 3.12:  brew install python@3.12"
        echo "Then re-run this script."
        exit 1
    fi
    PYTHON="python3"
fi

echo "Using: $PYTHON ($($PYTHON --version))"

echo "[1/5] Checking prerequisites..."
if ! brew list portaudio &>/dev/null 2>&1; then
    echo "portaudio not found. Installing via Homebrew..."
    brew install portaudio
fi

echo "[2/5] Creating virtual environment..."
if [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR"
fi
# On some macOS + Homebrew Python combos, `python -m venv` gets killed by
# the system (Killed: 9) during the ensurepip step. Work around by creating
# the venv without pip, then bootstrapping pip via get-pip.py.
if ! "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
    echo "  Standard venv failed — trying --without-pip workaround..."
    "$PYTHON" -m venv --without-pip "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/_get_pip.py
    python /tmp/_get_pip.py --quiet
    rm -f /tmp/_get_pip.py
else
    source "$VENV_DIR/bin/activate"
fi

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
