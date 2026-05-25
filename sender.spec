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
# PySide6 MUST be in this list — without it PyInstaller misses the Qt
# platform plugins (platforms\qwindows.dll, etc.), and a windowed-mode
# build will silently fail to create any window at runtime ("the app
# shows a taskbar icon but no main window").
for pkg in ("PySide6", "shiboken6", "pyaudiowpatch", "websockets"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# scipy.signal for the resampler in audio/_pcm.py
hiddenimports += collect_submodules("scipy.signal")
hiddenimports += collect_submodules("scipy.special")


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
