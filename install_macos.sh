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
# PySide6 requires Python <3.13. Homebrew's python3.12 can get Killed:9 on
# older macOS, so we prefer the python.org framework build first.
_try_python() {
    # Verify the candidate actually runs (Homebrew Python gets SIGKILL'd on
    # older macOS). Redirect stderr so "Killed" noise doesn't confuse users.
    "$1" -c "import sys; sys.exit(0)" 2>/dev/null
}

PYTHON=""
# 1) python.org framework builds (most reliable on older macOS)
for ver in 3.12 3.11 3.10 3.9; do
    fw="/Library/Frameworks/Python.framework/Versions/$ver/bin/python$ver"
    if [ -x "$fw" ] && _try_python "$fw"; then
        PYTHON="$fw"
        break
    fi
done
# 2) PATH-based candidates (Homebrew, pyenv, etc.)
if [ -z "$PYTHON" ]; then
    for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$candidate" &>/dev/null && _try_python "$candidate"; then
            PY_VER=$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo "0")
            if [ "$PY_VER" -lt 13 ]; then
                PYTHON="$candidate"
                break
            fi
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "Error: No working Python 3.9–3.12 found."
    echo "Install from https://www.python.org/downloads/ (the macOS universal installer)"
    echo "or via: brew install python@3.12"
    exit 1
fi

echo "Using: $PYTHON ($($PYTHON --version))"

echo "[1/5] Checking prerequisites..."
if ! brew list portaudio &>/dev/null 2>&1; then
    echo "portaudio not found. Installing via Homebrew..."
    brew install portaudio
fi
# scipy needs clang >= 15 and gfortran to build from source when no
# pre-built wheel exists for this macOS version.
if ! brew list llvm &>/dev/null 2>&1; then
    echo "llvm not found. Installing via Homebrew (needed to compile scipy)..."
    brew install llvm
fi
if ! command -v gfortran &>/dev/null; then
    echo "gfortran not found. Installing gcc via Homebrew (needed to compile scipy)..."
    brew install gcc
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
# Use Homebrew LLVM if the system clang is too old for scipy's build.
LLVM_PREFIX="$(brew --prefix llvm 2>/dev/null || true)"
if [ -n "$LLVM_PREFIX" ] && [ -d "$LLVM_PREFIX/bin" ]; then
    export CC="$LLVM_PREFIX/bin/clang"
    export CXX="$LLVM_PREFIX/bin/clang++"
    echo "  Using LLVM clang: $CC"
fi
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
