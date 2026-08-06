"""
Local PyInstaller hook for webrtcvad.

PyInstaller's contrib hook for `webrtcvad` errors out on `webrtcvad-wheels`
(a precompiled fork that ships as `webrtcvad.py` + `_webrtcvad.*.pyd` /
`_webrtcvad.*.so` TOP-LEVEL rather than as a package). This local hook ships
the two files directly and avoids the contrib hook entirely.
"""
import os
import sys
import sysconfig
import glob

from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("webrtcvad")

site_dir = sysconfig.get_paths()["purelib"]
# Windows: .pyd extension; macOS/Linux: .so extension
ext = "pyd" if sys.platform == "win32" else "so"
for lib in glob.glob(os.path.join(site_dir, f"_webrtcvad*.{ext}")):
    binaries.append((lib, "."))

datas = []
wrapper_py = os.path.join(site_dir, "webrtcvad.py")
if os.path.exists(wrapper_py):
    datas.append((wrapper_py, "."))

hiddenimports = ["webrtcvad", "_webrtcvad"]
