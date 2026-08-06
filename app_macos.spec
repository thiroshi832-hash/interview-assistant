# PyInstaller spec for AetherStack Interview Assistant — macOS .app bundle.
#
# Build:
#   pyinstaller app_macos.spec --clean --noconfirm
#
# Output:
#   dist/AetherStack Interview Assistant.app
#
# We use onedir (not onefile) because heavy ML libs crash in PyInstaller's
# onefile bootloader. The .app bundle wraps the onedir output.

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [
    ("assets/aetherstack-icon.png", "assets"),
    ("assets/aetherstack-icon.icns", "assets"),
    # silero-vad ONNX file — loaded directly via onnxruntime.
    ("assets/silero_vad.onnx", "assets"),
]
binaries = []
hiddenimports = []

for pkg in (
    "pywhispercpp",
    "deepgram",
    "resemblyzer",
    "librosa",
    "tokenizers",
    "onnxruntime",
    "pyaudio",
    "openai",
    "anthropic",
    "websockets",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("scipy.signal")
hiddenimports += collect_submodules("scipy.special")
hiddenimports += collect_submodules("librosa")


block_cipher = None


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "faster_whisper", "ctranslate2", "av",
        "silero_vad",
        "torchaudio", "torchvision",
        "matplotlib", "PIL", "tkinter",
        "pytest", "IPython", "jupyter", "notebook",
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
    name="aetherstack-interview-assistant",
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
    name="aetherstack-interview-assistant",
)

app = BUNDLE(
    coll,
    name="AetherStack Interview Assistant.app",
    icon="assets/aetherstack-icon.icns",
    bundle_identifier="com.aetherstack.interview-assistant",
    info_plist={
        "CFBundleDisplayName": "AetherStack Interview Assistant",
        "CFBundleShortVersionString": "1.0.0",
        "NSMicrophoneUsageDescription": "AetherStack needs microphone access to capture interview audio.",
        "NSHighResolutionCapable": True,
    },
)
