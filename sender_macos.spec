# PyInstaller spec for AetherStack Sender — macOS .app bundle.
#
# Build:
#   pyinstaller sender_macos.spec --clean --noconfirm
#
# Output:
#   dist/AetherStack Sender.app
#
# This is the companion app that runs on the interview computer in
# helper-network mode. Captures mic + virtual audio device loopback and
# streams tagged PCM to the helper laptop via WebSocket.

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [
    ("assets/aetherstack-icon.png", "assets"),
    ("assets/aetherstack-icon.icns", "assets"),
]
binaries = []
hiddenimports = []

for pkg in ("PySide6", "shiboken6", "pyaudio", "websockets"):
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
        # macOS does not use pyaudiowpatch
        "pyaudiowpatch",
    ],
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
    icon="assets/aetherstack-icon.icns",
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

app = BUNDLE(
    coll,
    name="AetherStack Sender.app",
    icon="assets/aetherstack-icon.icns",
    bundle_identifier="com.aetherstack.sender",
    info_plist={
        "CFBundleDisplayName": "AetherStack Sender",
        "CFBundleShortVersionString": "1.0.0",
        "NSMicrophoneUsageDescription": "AetherStack Sender needs microphone access to capture and stream interview audio.",
        "NSHighResolutionCapable": True,
    },
)
