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
]
binaries = []
hiddenimports = []

for pkg in (
    "silero_vad",
    "faster_whisper",
    "pywhispercpp",
    "deepgram",
    "resemblyzer",
    "librosa",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "pyaudiowpatch",
    "openai",
    "anthropic",
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
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "webrtcvad",       # resemblyzer dep we deliberately skipped
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
