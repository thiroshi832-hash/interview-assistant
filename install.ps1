# Install AetherStack Interview Assistant on Windows.
# Usage:  .\install.ps1
#
# This handles the webrtcvad-needs-MSVC problem: resemblyzer is installed
# without its deps (since it never reaches the webrtcvad code path), and we
# supply librosa + scikit-learn separately.

$ErrorActionPreference = "Stop"

Write-Host "[1/3] Installing main dependencies..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check -r requirements.txt

Write-Host "[2/3] Installing resemblyzer without its webrtcvad dep..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check --no-deps resemblyzer

Write-Host "[3/3] Verifying imports..." -ForegroundColor Cyan
# faster_whisper / silero_vad are NOT dependencies of the slim build (whisper.cpp
# via pywhispercpp covers STT; VAD hits the bundled .onnx directly via onnxruntime)
# — don't check for them here, they were intentionally dropped from requirements.txt.
python -c "import resemblyzer, librosa, onnxruntime, pyaudiowpatch, anthropic, PySide6; print('All deps importable.')"

Write-Host ""
Write-Host "Done. Set ANTHROPIC_API_KEY then run:" -ForegroundColor Green
Write-Host '  $env:ANTHROPIC_API_KEY = "sk-ant-..."'
Write-Host "  python app.py"
