# PyInstaller spec for AetherStack Sender — onedir build.
#
# Build:
#   pyinstaller sender.spec
#
# Output:
#   dist\aetherstack-sender\aetherstack-sender.exe   (+ runtime DLLs)
#
# This is the tiny companion app that runs on the INTERVIEW computer in
# helper-network mode. It captures mic + WASAPI loopback and streams the
# tagged PCM to the helper laptop via WebSocket. No STT, no LLM, no torch.
# Bundle target: ~40 MB.

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [
    ("assets/aetherstack-icon.png", "assets"),
    ("assets/aetherstack-icon.ico", "assets"),
]
binaries = []
hiddenimports = []

# Only what the sender actually uses.
# NOTE: do NOT collect_all("PySide6") — that force-pulls EVERY Qt module
# (QtWebEngine, Qt3D, QtCharts, QtMultimedia, QtQml…), and the QtWebEngine
# hook crashes the build (FileNotFoundError writing qt.conf). The sender only
# imports QtCore/QtGui/QtWidgets; PyInstaller's standard PySide6 hook collects
# those plus the platform plugins (qwindows.dll) automatically — same as
# app.spec does. Heavy unused Qt modules are excluded below for good measure.
for pkg in ("pyaudiowpatch", "websockets"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ── MS Visual C++ runtime DLLs ────────────────────────────────────────────
# Qt6Core.dll depends on MSVCP140.dll. PyInstaller classifies MSVCP140.dll as a
# "system" DLL and skips bundling it unless some collected package forces it in.
# In app.spec that happens by accident (torch/sklearn ship their own copies); in
# this slim sender build every such package is excluded, so MSVCP140.dll is left
# out entirely. The .exe then runs on any machine that already has the VC++
# redistributable (or a Python install) — because MSVCP140.dll sits in
# System32 there — but on a CLEAN machine the loader can't resolve it and the
# process dies before Python even starts (no crash log, no dialog). Fix: copy
# the VC runtime DLLs that ship inside the PySide6 wheel into the bundle root
# AND next to the Qt DLLs, matching app.spec's (accidentally-working) layout.
import os as _os
import PySide6 as _pyside6
_pyside6_dir = _os.path.dirname(_pyside6.__file__)
for _dll in (
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll", "vcomp140.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
):
    _src = _os.path.join(_pyside6_dir, _dll)
    if _os.path.exists(_src):
        binaries.append((_src, "."))
        binaries.append((_src, "PySide6"))

# scipy.signal for the resampler in audio/_pcm.py
hiddenimports += collect_submodules("scipy.signal")
hiddenimports += collect_submodules("scipy.special")
# scipy.signal imports its vendored array_api_compat at load time; PyInstaller's
# scipy hook doesn't fully collect it, so the frozen app crashes with e.g.
# "No module named 'scipy._external.array_api_compat.numpy.fft'". Collect both
# possible vendored locations (older scipy: _lib, newer: _external); a missing
# one yields [] harmlessly.
hiddenimports += collect_submodules("scipy._lib.array_api_compat")
hiddenimports += collect_submodules("scipy._external.array_api_compat")


block_cipher = None


a = Analysis(
    ["sender/sender_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Everything the main app needs but the sender does not.
        "anthropic", "openai", "tokenizers",
        "deepgram", "deepgram_sdk",
        "pywhispercpp", "faster_whisper", "ctranslate2",
        "resemblyzer", "librosa", "numba", "torch", "torchaudio", "torchvision",
        "silero_vad", "onnxruntime",
        "webrtcvad", "_webrtcvad",
        "matplotlib", "PIL", "tkinter",
        "pytest", "IPython", "jupyter", "notebook",
        "sklearn", "scikit-learn",
        "pypdf", "docx", "python-docx",
        # Heavy Qt modules the sender never uses. Excluding them keeps the
        # bundle small and guarantees the crashing QtWebEngine hook never runs.
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
        "PySide6.QtGraphsWidgets", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtLocation", "PySide6.QtSql", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="aetherstack-sender",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/aetherstack-icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="aetherstack-sender",
)
