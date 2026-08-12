"""
Fail the build if any bundled Mach-O binary requires a newer macOS than we
target.

A wheel's platform tag (macosx_11_0_universal2) is a claim by the publisher,
not a guarantee — pip will also happily resolve a newer release than the one
we pre-downloaded. Either way the breakage only shows up as a dyld crash on
the user's machine:

    Symbol not found: __ZNSt3__13pmr25monotonic_buffer_resource11do_allocateEmm
    (which was built for Mac OS X 14.0)

The authoritative value is LC_BUILD_VERSION.minos in the Mach-O header. This
script walks a built .app and reports every binary whose minos exceeds the
target, so CI catches it instead of the user.

Usage:
    python scripts/check_macos_min_version.py <path> [--max-version 11.0]
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

FAT_MAGICS = {0xCAFEBABE, 0xCAFEBABF}
MH_MAGIC_64, MH_CIGAM_64 = 0xFEEDFACF, 0xCFFAEDFE
MH_MAGIC_32, MH_CIGAM_32 = 0xFEEDFACE, 0xCEFAEDFE
THIN_MAGICS = {MH_MAGIC_64, MH_CIGAM_64, MH_MAGIC_32, MH_CIGAM_32}

LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32

CPU_NAMES = {0x01000007: "x86_64", 0x0100000C: "arm64"}


def _decode(v: int) -> tuple[int, int, int]:
    return ((v >> 16) & 0xFFFF, (v >> 8) & 0xFF, v & 0xFF)


def _fmt(v: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in v)


def _slice_minos(buf: bytes, off: int):
    """Return (arch, minos) for one thin Mach-O slice, or None."""
    if off + 32 > len(buf):
        return None
    magic_be = struct.unpack_from(">I", buf, off)[0]
    if magic_be in (MH_MAGIC_64, MH_MAGIC_32):
        end = ">"
    elif magic_be in (MH_CIGAM_64, MH_CIGAM_32):
        end = "<"
    else:
        return None
    magic = struct.unpack_from(end + "I", buf, off)[0]
    is64 = magic in (MH_MAGIC_64, MH_CIGAM_64)
    cputype = struct.unpack_from(end + "I", buf, off + 4)[0]
    ncmds = struct.unpack_from(end + "I", buf, off + 16)[0]

    pos = off + (32 if is64 else 28)
    minos = None
    for _ in range(ncmds):
        if pos + 8 > len(buf):
            break
        cmd, cmdsize = struct.unpack_from(end + "II", buf, pos)
        if cmd == LC_BUILD_VERSION:
            minos = _decode(struct.unpack_from(end + "I", buf, pos + 12)[0])
            break
        if cmd == LC_VERSION_MIN_MACOSX:
            minos = _decode(struct.unpack_from(end + "I", buf, pos + 8)[0])
            break
        if cmdsize == 0:
            break
        pos += cmdsize
    return CPU_NAMES.get(cputype, hex(cputype)), minos


def inspect(data: bytes):
    """Yield (arch, minos) for every slice of a thin or fat Mach-O file."""
    if len(data) < 8:
        return
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in FAT_MAGICS:
        nfat = struct.unpack_from(">I", data, 4)[0]
        is64 = magic == 0xCAFEBABF
        entry = 32 if is64 else 20
        for i in range(nfat):
            base = 8 + i * entry
            if base + entry > len(data):
                break
            fmt = ">Q" if is64 else ">I"
            offset = struct.unpack_from(fmt, data, base + 8)[0]
            r = _slice_minos(data, offset)
            if r:
                yield r
    elif magic in THIN_MAGICS or struct.unpack_from("<I", data, 0)[0] in THIN_MAGICS:
        r = _slice_minos(data, 0)
        if r:
            yield r


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    if len(head) < 4:
        return False
    return (
        struct.unpack(">I", head)[0] in FAT_MAGICS | THIN_MAGICS
        or struct.unpack("<I", head)[0] in THIN_MAGICS
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="Directory to scan (e.g. dist/)")
    ap.add_argument("--max-version", default="11.0",
                    help="Highest acceptable minos, e.g. 11.0")
    args = ap.parse_args()

    parts = [int(p) for p in args.max_version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    limit = tuple(parts[:3])

    root = Path(args.path)
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    scanned = 0
    bad: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink() or not is_macho(p):
            continue
        scanned += 1
        for arch, minos in inspect(p.read_bytes()):
            if minos is not None and minos > limit:
                bad.append(f"  {p.relative_to(root)}\n      {arch}: minos={_fmt(minos)}")

    print(f"Scanned {scanned} Mach-O file(s) under {root}; "
          f"target macOS {_fmt(limit)}")
    if bad:
        print(f"\nFAIL — {len(bad)} binary slice(s) require a newer macOS:\n")
        print("\n".join(bad))
        print("\nThese will crash with a dyld 'Symbol not found' error on "
              f"macOS {_fmt(limit)}.\nPin the offending package to an older "
              "release, or build it from source.")
        return 1

    print("OK — every bundled binary runs on macOS " + _fmt(limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
