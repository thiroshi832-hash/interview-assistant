"""
Local PyInstaller hook for webrtcvad.

PyInstaller's contrib hook for `webrtcvad` errors out on `webrtcvad-wheels`
(a precompiled fork that ships as `webrtcvad.py` + `_webrtcvad.*.pyd`
TOP-LEVEL rather than as a package). This local hook ships the two files
directly and avoids the contrib hook entirely.
"""
import os
import sys
import sysconfig
import glob

from PyInstaller.utils.hooks import collect_dynamic_libs

# Find webrtcvad.py + the compiled _webrtcvad.*.pyd in site-packages.
# Pyinstaller normally handles a top-level .py module automatically (via the
# import graph), but only if no module-level error blocks it. Since the
# contrib hook errors out before that, we re-collect explicitly.
binaries = collect_dynamic_libs("webrtcvad")

# Add _webrtcvad.*.pyd as a binary if collect_dynamic_libs missed it.
site_dir = sysconfig.get_paths()["purelib"]
for pyd in glob.glob(os.path.join(site_dir, "_webrtcvad*.pyd")):
    binaries.append((pyd, "."))

# The Python wrapper is a top-level module — datas, not binaries.
datas = []
wrapper_py = os.path.join(site_dir, "webrtcvad.py")
if os.path.exists(wrapper_py):
    datas.append((wrapper_py, "."))

hiddenimports = ["webrtcvad", "_webrtcvad"]
