# PyInstaller spec for AetherStack Interview Assistant — onedir build.
#
# Build:
#   pyinstaller app.spec
#
# Output:
#   dist\interview-assistant\interview-assistant.exe   (+ runtime DLLs)
#
# Distribute by zipping the entire dist\interview-assistant\ folder. The .exe
# is a normal double-clickable Windows app; DLLs load from the folder it's in.
#
# We use onedir (not onefile) because heavy ML libs — torch, faster-whisper,
# ctranslate2 — crash in the PyInstaller --onefile bootloader on Windows
# (exit code 0xC0000005, access violation during temp extraction).

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [
    # Bundle the icon assets so the runtime can call setWindowIcon() inside
    # the .exe (see `paths.py:icon_path`).
    ("assets/aetherstack-icon.png", "assets"),
    ("assets/aetherstack-icon.ico", "assets"),
    # silero-vad ONNX file — loaded directly via onnxruntime (no silero-vad
    # pip package, no torch).
    ("assets/silero_vad.onnx", "assets"),
]
binaries = []
hiddenimports = []

# Reduced bundle list — slim build:
#   removed silero_vad      (replaced with direct onnxruntime + bundled .onnx)
#   removed faster_whisper  (whisper.cpp via pywhispercpp covers it)
#   removed ctranslate2     (transitive dep of faster_whisper; gone now)
# resemblyzer is back in (needed for voice enrollment fingerprinting).
# Net savings vs the fat build: ~340 MB.
for pkg in (
    "pywhispercpp",
    "deepgram",
    "resemblyzer",
    "librosa",
    "tokenizers",
    "onnxruntime",
    "pyaudiowpatch",
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
# scipy.signal imports its vendored array_api_compat at load time; without this
# the frozen app crashes with "No module named
# 'scipy._external.array_api_compat.numpy.fft'". Collect both possible vendored
# locations (older scipy: _lib, newer: _external); a missing one yields [].
hiddenimports += collect_submodules("scipy._lib.array_api_compat")
hiddenimports += collect_submodules("scipy._external.array_api_compat")


block_cipher = None


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Local hooks take precedence over pyinstaller-hooks-contrib. We override
    # the contrib `hook-webrtcvad.py` because it errors out on `webrtcvad-wheels`.
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Slim-build excludes — block faster-whisper / silero-vad-Python
        # paths only. We KEEP torch + librosa + numba because resemblyzer
        # (voice fingerprint enrollment) needs them.
        # NOTE: webrtcvad MUST be bundled (via webrtcvad-wheels) because
        # resemblyzer.audio imports it at module load time — excluding it
        # makes `from resemblyzer import VoiceEncoder` ImportError at runtime
        # which surfaces in the UI as "Voice enrollment isn't available".
        "faster_whisper", "ctranslate2", "av",
        "silero_vad",                              # we use the bundled .onnx directly
        "torchaudio", "torchvision",               # torch is needed, but not the extras
        "matplotlib", "PIL", "tkinter",
        "pytest", "IPython", "jupyter", "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# onedir build: thin .exe + binaries collected into dist/interview-assistant/
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
    console=False,                 # pure GUI — no console window
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
    name="aetherstack-interview-assistant",
)
