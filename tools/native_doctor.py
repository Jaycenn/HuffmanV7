#!/usr/bin/env python3
"""
tools/native_doctor.py — why is the engine on the pure-Python path?

Run this first whenever compression is unexpectedly slow:

    python tools/native_doctor.py

It prints exactly which library files were searched, which loaded or failed
and why, whether a build was attempted and what the compiler said, and the
resulting backend for each preset. Exits non-zero when the native core is
unavailable, so it can be used as a CI or setup check.

Background: a 9.72 MB text file takes roughly 11 s natively and roughly 138 s
on the pure-Python reference path. The output is byte-identical either way —
only speed differs — so a silent fallback looks like "the compressor is slow"
rather than "the native library did not load". This tool removes the guesswork.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc_native                                               # noqa: E402
import afc2                                                     # noqa: E402
import presets                                                  # noqa: E402


def main():
    print(afc_native.report())
    print()
    print("Engine view")
    print("  afc2.NATIVE          : %s" % afc2.NATIVE)
    print("  tunable presets      : %s" % getattr(afc_native, "TUNABLE", False))
    print()
    print("Backend per preset")
    for name in ("fast", "balanced", "maximum"):
        native = presets.uses_native(name)
        print("  %-9s -> %s" % (name, "C++ native" if native
                                else "pure Python"))
    print()

    if not afc_native.AVAILABLE:
        print("HOW TO FIX")
        print("  Linux/macOS:")
        print("    g++ -O3 -std=c++17 -shared -fPIC -pthread \\")
        print("        afc_native.cpp -o afc_kernels.so")
        print("  Windows (MinGW-w64, from the *MinGW 64-bit* shell):")
        print("    g++ -O3 -std=c++17 -shared -static -pthread \\")
        print("        afc_native.cpp -o afc_kernels.dll")
        print()
        print("  -static bundles libstdc++/libgcc/winpthread so the DLL loads")
        print("  without MinGW on PATH. The toolchain bitness must match your")
        print("  Python: a 32-bit DLL will not load into 64-bit Python.")
        return 1

    print("Native backend is active. Nothing to fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
