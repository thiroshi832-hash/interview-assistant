#!/usr/bin/env python3
"""
Generate assets/aetherstack-icon.icns from assets/aetherstack-icon.png.

macOS only — requires the `sips` and `iconutil` command-line tools (bundled
with macOS). Run this once before building with PyInstaller:

    python3 scripts/make_icns.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_PNG = os.path.join(REPO_ROOT, "assets", "aetherstack-icon.png")
DST_ICNS = os.path.join(REPO_ROOT, "assets", "aetherstack-icon.icns")

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def main() -> int:
    if sys.platform != "darwin":
        print("This script requires macOS (uses sips + iconutil).")
        return 1

    if not os.path.exists(SRC_PNG):
        print(f"Source PNG not found: {SRC_PNG}")
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = os.path.join(tmpdir, "icon.iconset")
        os.makedirs(iconset)

        for size in SIZES:
            # Standard resolution
            out = os.path.join(iconset, f"icon_{size}x{size}.png")
            subprocess.run(
                ["sips", "-z", str(size), str(size), SRC_PNG, "--out", out],
                check=True, capture_output=True,
            )
            # Retina (@2x) — half the name, double the pixels
            if size <= 512:
                out_2x = os.path.join(iconset, f"icon_{size // 2}x{size // 2}@2x.png")
                # Only if size//2 is a valid iconset size
                if size // 2 in SIZES:
                    subprocess.run(
                        ["sips", "-z", str(size), str(size), SRC_PNG, "--out", out_2x],
                        check=True, capture_output=True,
                    )

        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", DST_ICNS],
            check=True,
        )

    print(f"Created: {DST_ICNS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
