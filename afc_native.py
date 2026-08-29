#!/usr/bin/env python3
"""ctypes bridge to afc_native.cpp.  Auto-loads a prebuilt afc_kernels.dll/.so,
attempts a one-time g++ build if only the source is present, and exposes
AVAILABLE=False (callers fall back to pure Python) when neither works.

v4: adds compress(data, adaptive, fmt) — the full pipeline in one native
call — and decompress() now reads AFC2 containers as well as legacy AFC1.
The v3 kernel wrappers (ngram_candidates, segment_ids, pack_bits) are kept
for backwards compatibility."""
import ctypes
import os
import subprocess
import sys
from array import array

_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB = None

_FMT_CODES = {"auto": 0, "afc1": 1, "afc2": 2}

# ---------------------------------------------------------------------------
# [v7] Loader diagnostics — WHY the native core is or is not in use
# ---------------------------------------------------------------------------
# The V6 loader swallowed every failure: `except OSError: pass`, a discarded
# g++ stderr, and a silent FileNotFoundError when g++ was not on PATH. The
# only visible symptom was the whole engine quietly running ~12x slower on the
# pure-Python path, with the UI reporting "pure Python" and no way to find out
# why. That is exactly the situation behind the 138-second dickens run.
#
# Every step now records its outcome in DIAGNOSTICS, and REASON carries a
# one-line explanation that /api/status and the Settings page surface to the
# user. Nothing here changes compression behaviour.

DIAGNOSTICS = []
REASON = ""
LIBRARY_PATH = ""
SEARCHED = []


def _note(step, ok, detail=""):
    DIAGNOSTICS.append({"step": step, "ok": bool(ok), "detail": str(detail)})
    return ok


def _python_bits():
    return 64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32


def _binary_bits(path):
    """Bitness of a .dll/.so, read from its header.

    A 32-bit MinGW DLL will not load into 64-bit Python — a very common
    Windows failure whose OSError message ("%1 is not a valid Win32
    application") does not say so plainly. Returns None if unrecognised.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head[:4] == b"\x7fELF":
            return 64 if head[4] == 2 else 32
        if head[:2] == b"MZ":
            off = int.from_bytes(head[0x3C:0x40], "little")
            if head[off:off + 4] == b"PE\x00\x00":
                machine = int.from_bytes(head[off + 4:off + 6], "little")
                return {0x014c: 32, 0x8664: 64, 0xAA64: 64}.get(machine)
    except Exception:
        return None
    return None


def _load():
    """Try each candidate library, recording exactly why any of them fails."""
    global _LIB, LIBRARY_PATH
    found_any = False
    for name in ("afc_kernels.dll", "afc_kernels.so", "libafc_kernels.so"):
        p = os.path.join(_DIR, name)
        SEARCHED.append(p)
        if not os.path.exists(p):
            continue
        found_any = True
        bits = _binary_bits(p)
        mine = _python_bits()
        if bits and bits != mine:
            _note("load %s" % name, False,
                  "architecture mismatch: library is %d-bit, this Python is "
                  "%d-bit. Rebuild with a matching toolchain (on Windows use "
                  "a 64-bit MinGW-w64 shell for 64-bit Python)." % (bits, mine))
            continue
        try:
            lib = ctypes.CDLL(p)
        except OSError as exc:
            hint = ""
            if os.name == "nt":
                hint = (" On Windows this usually means the MinGW runtime "
                        "DLLs are not on PATH — rebuild with -static.")
            _note("load %s" % name, False, "%s.%s" % (exc, hint))
            continue
        # require the v4 entry point; a stale v3-only binary is rejected so
        # the engine and the library can never disagree
        if not hasattr(lib, "afc_compress"):
            _note("load %s" % name, False,
                  "library predates AFC v4 (no afc_compress export); "
                  "delete it and let the engine rebuild.")
            continue
        _LIB = lib
        LIBRARY_PATH = p
        _note("load %s" % name, True, "loaded (%d-bit)" % (bits or mine))
        return True
    if not found_any:
        _note("locate library", False,
              "no afc_kernels.dll/.so beside afc_native.py")
    return False


def library_name():
    """The library filename this platform expects."""
    return "afc_kernels.dll" if os.name == "nt" else "afc_kernels.so"


def expected_path():
    return os.path.join(_DIR, library_name())


def _build_commands(src, out):
    """Candidate build commands, in preference order.

    V6 only ever tried `g++`. On Windows that is often absent even when a
    perfectly good compiler is installed — Visual Studio ships `cl.exe`, and
    LLVM ships `clang++`. Trying all three (and reporting each failure) is the
    difference between "native unavailable" and a working DLL.
    """
    if os.name == "nt":
        return [
            ("g++", ["g++", "-O3", "-std=c++17", "-shared", "-static",
                     "-pthread", src, "-o", out]),
            ("clang++", ["clang++", "-O3", "-std=c++17", "-shared",
                         src, "-o", out]),
            # MSVC: /LD builds a DLL. The AFC_API __declspec(dllexport)
            # annotations in afc_native.cpp are what make its exports visible
            # — without them cl.exe produces a DLL that exports nothing.
            ("cl", ["cl", "/nologo", "/LD", "/O2", "/EHsc", "/std:c++17",
                    src, "/Fe:" + out]),
        ]
    return [
        ("g++", ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", "-pthread",
                 src, "-o", out]),
        ("clang++", ["clang++", "-O3", "-std=c++17", "-shared", "-fPIC",
                     "-pthread", src, "-o", out]),
    ]


def _build():
    """Attempt a one-time build, recording each compiler's own error output."""
    src = os.path.join(_DIR, "afc_native.cpp")
    if not os.path.exists(src):
        return _note("build", False, "afc_native.cpp not found at %s" % src)
    out = expected_path()
    tried = []
    for name, cmd in _build_commands(src, out):
        try:
            r = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            tried.append("%s: not on PATH" % name)
            _note("build with %s" % name, False, "not on PATH")
            continue
        except Exception as exc:                   # pragma: no cover
            tried.append("%s: %s" % (name, exc))
            _note("build with %s" % name, False, str(exc))
            continue
        if r.returncode == 0:
            _note("build with %s" % name, True, " ".join(cmd))
            return _load()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if not err:
            err = (r.stdout or b"").decode("utf-8", "replace").strip()
        tried.append("%s: exited %d" % (name, r.returncode))
        _note("build with %s" % name, False,
              "exit %d: %s" % (r.returncode, err[-500:] or "(no output)"))

    return _note("build", False,
                 "no usable C++ compiler. Tried %s. Install one and re-run, "
                 "or place a prebuilt %s next to afc_native.py. Windows: "
                 "MSYS2 `pacman -S mingw-w64-x86_64-gcc` then build from the "
                 "MinGW 64-bit shell, or use the Visual Studio Developer "
                 "Command Prompt (cl.exe)."
                 % ("; ".join(tried), library_name()))


def _resolve():
    global REASON
    if os.environ.get("AFC_NO_NATIVE"):
        _note("environment", False,
              "AFC_NO_NATIVE is set, so the native core is disabled on "
              "purpose. Unset it to use the C++ backend.")
        REASON = "disabled by AFC_NO_NATIVE"
        return False
    if _load() or _build():
        REASON = "native library loaded from %s" % LIBRARY_PATH
        return True
    failures = [d for d in DIAGNOSTICS if not d["ok"]]
    REASON = failures[-1]["detail"] if failures else "unknown"
    return False


AVAILABLE = _resolve()


def smoke_test():
    """Actually CALL the native library and verify a round trip.

    Existing + loadable + exporting the symbol is still not proof that the
    native path works. This compresses and decompresses a tiny payload through
    the C++ core and compares the bytes, so `--diagnose` reports a real
    success rather than an inference.

    Returns (ok, detail).
    """
    if not AVAILABLE:
        return False, "native library not loaded"
    sample = b"hello hello hello AFC AFC AFC native smoke test 1234567890\n" * 8
    try:
        blob = compress(sample, True, "auto")
    except Exception as exc:
        return False, "native compress raised: %s" % exc
    try:
        back = decompress(blob)
    except Exception as exc:
        return False, "native decompress raised: %s" % exc
    if back != sample:
        return False, ("round trip mismatch: %d bytes in, %d out"
                       % (len(sample), len(back)))
    if TUNABLE:
        try:
            alt = compress(sample, True, "auto",
                           params={"dp": False, "dp_rounds": 1,
                                   "merge_rounds": 2, "min_freq": 5,
                                   "tune": True})
            if decompress(alt) != sample:
                return False, "afc_compress_ex round trip mismatch"
        except Exception as exc:
            return False, "afc_compress_ex raised: %s" % exc
    return True, ("%d bytes -> %d bytes -> %d bytes, byte-identical"
                  % (len(sample), len(blob), len(back)))


def diagnose():
    """The exact checklist a slow run needs answered. Returns an exit code."""
    import platform
    path = expected_path()
    exists = os.path.exists(path)
    bits = _binary_bits(path) if exists else None
    has_ex = bool(_LIB is not None and hasattr(_LIB, "afc_compress_ex"))
    has_c = bool(_LIB is not None and hasattr(_LIB, "afc_compress"))
    ok, detail = smoke_test()

    print("AFC NATIVE BACKEND DIAGNOSIS")
    print("=" * 60)
    print("Python architecture   : %d-bit" % _python_bits())
    print("Python version        : %s" % platform.python_version())
    print("Operating system      : %s (%s)" % (platform.system(),
                                               platform.machine()))
    print("Module directory      : %s" % _DIR)
    print("Working directory     : %s" % os.getcwd())
    print("Library expected path : %s" % path)
    print("Library exists        : %s" % ("YES" if exists else "NO"))
    print("Library architecture  : %s" % (("%d-bit" % bits) if bits
                                          else ("UNKNOWN" if exists else "n/a")))
    if exists and bits and bits != _python_bits():
        print("                        *** MISMATCH: %d-bit library cannot "
              "load into %d-bit Python ***" % (bits, _python_bits()))
    print("Library load          : %s" % ("SUCCESS" if _LIB is not None
                                          else "FAIL"))
    print("Loaded from           : %s" % (LIBRARY_PATH or "-"))
    print("Export afc_compress   : %s" % ("YES" if has_c else "NO"))
    print("Export afc_compress_ex: %s" % ("YES" if has_ex else
                                          "NO (pre-V7 library: Fast and "
                                          "Maximum will use Python)"))
    print("Native call test      : %s" % ("SUCCESS" if ok else "FAIL"))
    print("                        %s" % detail)
    print("AFC_NO_NATIVE set     : %s"
          % ("YES - native disabled on purpose"
             if os.environ.get("AFC_NO_NATIVE") else "no"))
    print()
    print("RESULT: %s" % ("C++ native backend is ACTIVE" if (AVAILABLE and ok)
                          else "PURE PYTHON - native backend unavailable"))
    if not (AVAILABLE and ok):
        print("Reason: %s" % REASON)
    print()
    print("Steps attempted:")
    for d in DIAGNOSTICS:
        print("  [%s] %s%s" % ("ok  " if d["ok"] else "FAIL", d["step"],
                               (" - " + d["detail"]) if d["detail"] else ""))
    print()
    print("Searched for a library at:")
    for p in SEARCHED:
        print("  %s%s" % (p, "" if os.path.exists(p) else "   (absent)"))
    if not (AVAILABLE and ok):
        print()
        print("BUILD IT MANUALLY")
        if os.name == "nt":
            print("  MinGW-w64 (from the *MinGW 64-bit* shell):")
            print("    g++ -O3 -std=c++17 -shared -static -pthread \\")
            print("        afc_native.cpp -o afc_kernels.dll")
            print("  Visual Studio (Developer Command Prompt for x64):")
            print("    cl /nologo /LD /O2 /EHsc /std:c++17 \\")
            print("       afc_native.cpp /Fe:afc_kernels.dll")
            print("  The toolchain bitness MUST match your Python.")
        else:
            print("    g++ -O3 -std=c++17 -shared -fPIC -pthread \\")
            print("        afc_native.cpp -o afc_kernels.so")
        print("  Then re-run: python -m afc_native --diagnose")
    return 0 if (AVAILABLE and ok) else 1


def report():
    """Human-readable explanation of the backend decision.

    Printed by tools/native_doctor.py and surfaced by /api/status, so a slow
    run is never a mystery."""
    lines = ["AFC native backend: %s" % ("AVAILABLE" if AVAILABLE
                                         else "NOT AVAILABLE")]
    lines.append("Python: %d-bit, %s" % (_python_bits(), sys.platform))
    if AVAILABLE:
        lines.append("Library: %s" % LIBRARY_PATH)
        lines.append("Tunable presets (afc_compress_ex): %s" % TUNABLE)
    else:
        lines.append("Reason: %s" % REASON)
        lines.append("Consequence: every preset runs on the pure-Python "
                     "reference path, which is roughly an order of magnitude "
                     "slower. Output is identical; only speed differs.")
    lines.append("")
    lines.append("Steps:")
    for d in DIAGNOSTICS:
        lines.append("  [%s] %s%s" % ("ok " if d["ok"] else "FAIL", d["step"],
                                      (" - " + d["detail"]) if d["detail"] else ""))
    if SEARCHED:
        lines.append("")
        lines.append("Searched:")
        for p in SEARCHED:
            lines.append("  %s%s" % (p, "" if os.path.exists(p) else "  (absent)"))
    return "\n".join(lines)

if AVAILABLE:
    _LIB.afc_free.argtypes = [ctypes.c_void_p]
    for fn in ("count_ngrams", "segment_ids", "afc_decompress"):
        getattr(_LIB, fn).argtypes = [
            ctypes.c_char_p, ctypes.c_uint32,
        ] + ([ctypes.c_uint32] if fn == "count_ngrams" else
             [ctypes.c_char_p, ctypes.c_uint32] if fn == "segment_ids"
             else []) + [
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
        getattr(_LIB, fn).restype = ctypes.c_int
    _LIB.pack_bits.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(ctypes.c_uint32)]
    _LIB.pack_bits.restype = ctypes.c_int
    _LIB.afc_compress.argtypes = [ctypes.c_char_p, ctypes.c_uint32,
                                  ctypes.c_int, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_void_p),
                                  ctypes.POINTER(ctypes.c_uint32)]
    _LIB.afc_compress.restype = ctypes.c_int

    # Extended entry point: same pipeline, caller-supplied tunables. Older
    # libraries built before V7 do not export it; TUNABLE stays False and
    # callers fall back to the pure-Python path for non-default presets
    # exactly as V6 did, so a stale .so degrades instead of misbehaving.
    TUNABLE = hasattr(_LIB, "afc_compress_ex")
    if TUNABLE:
        _LIB.afc_compress_ex.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
        _LIB.afc_compress_ex.restype = ctypes.c_int

    # [v9] Profile entry point: the same pipeline again, but the search-shape
    # tunables (scan window, n-gram ceiling, dictionary caps, block cap,
    # merges per round) are caller-supplied too. A library without it reports
    # PROFILES False and presets.py falls back to the single default profile,
    # so a stale .so still compresses correctly — just without the search.
    PROFILES = hasattr(_LIB, "afc_compress_v9")
    if PROFILES:
        _LIB.afc_compress_v9.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
        _LIB.afc_compress_v9.restype = ctypes.c_int
else:
    TUNABLE = False
    PROFILES = False


def _take(outp, outn):
    data = ctypes.string_at(outp, outn.value)
    _LIB.afc_free(outp)
    return data


# [v9] Positional layout of the flat int32 vector afc_compress_v9 reads. The
# first five entries are the V7 tunables; anything the caller leaves off keeps
# the engine default, which is how the ABI grows without a new export.
_V9_FIELDS = ("dp", "dp_rounds", "merge_rounds", "min_freq", "tune",
              "scan_window", "ngram_max", "max_initial_dict", "max_dict",
              "max_block", "merges_per_round")
_V9_DEFAULTS = {"dp": 1, "dp_rounds": 3, "merge_rounds": 6, "min_freq": 4,
                "tune": 1, "scan_window": 1 << 20, "ngram_max": 5,
                "max_initial_dict": 3072, "max_dict": 4096, "max_block": 128,
                "merges_per_round": 32}
_V9_ONLY = _V9_FIELDS[5:]


def compress(data: bytes, adaptive: bool = True, fmt: str = "auto",
             params: dict = None) -> bytes:
    """Full v4 pipeline in one native call (byte-identical to pure Python).

    `params`, when given, carries the preset-controlled tunables:

        {"dp": bool, "dp_rounds": int, "merge_rounds": int,
         "min_freq": int, "tune": bool}

    and, on a v9 library, the search-shape ones as well:

        {"scan_window": int, "ngram_max": int, "max_initial_dict": int,
         "max_dict": int, "max_block": int, "merges_per_round": int}

    They map 1:1 onto the matching ``afc2.EngineOptions`` fields, so a preset
    reaches the native core instead of being silently ignored. Omitting
    `params` uses the engine defaults and calls the original entry point, so
    nothing changes for existing callers.

    A caller that asks for a non-default search shape on a library that
    predates afc_compress_v9 gets a RuntimeError rather than output produced
    under the wrong parameters; presets.py treats that as "no profile search
    available" and falls back to the default profile.
    """
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    if params is None:
        rc = _LIB.afc_compress(data, len(data), 1 if adaptive else 0,
                               _FMT_CODES.get(fmt, 0),
                               ctypes.byref(outp), ctypes.byref(outn))
    else:
        shaped = any(name in params
                     and int(params[name]) != _V9_DEFAULTS[name]
                     for name in _V9_ONLY)
        if shaped and not PROFILES:
            raise RuntimeError("native library predates afc_compress_v9")
        if shaped:
            values = [int(params.get(name, _V9_DEFAULTS[name]))
                      for name in _V9_FIELDS]
            vec = (ctypes.c_int32 * len(values))(*values)
            rc = _LIB.afc_compress_v9(
                data, len(data), 1 if adaptive else 0,
                _FMT_CODES.get(fmt, 0), vec, len(values),
                ctypes.byref(outp), ctypes.byref(outn))
        else:
            if not TUNABLE:
                raise RuntimeError("native library predates afc_compress_ex")
            rc = _LIB.afc_compress_ex(
                data, len(data), 1 if adaptive else 0,
                _FMT_CODES.get(fmt, 0),
                1 if params.get("dp", True) else 0,
                int(params.get("dp_rounds", 3)),
                int(params.get("merge_rounds", 6)),
                int(params.get("min_freq", 4)),
                1 if params.get("tune", True) else 0,
                ctypes.byref(outp), ctypes.byref(outn))
    if rc != 0:
        raise RuntimeError(f"native compress failed (rc={rc})")
    return _take(outp, outn)


def decompress(blob: bytes) -> bytes:
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    rc = _LIB.afc_decompress(blob, len(blob),
                             ctypes.byref(outp), ctypes.byref(outn))
    if rc != 0:
        raise ValueError(f"not a valid AFC container (native rc={rc})")
    return _take(outp, outn)


# ---------------------------------------------------------------------------
# legacy v3 kernel wrappers (unchanged API)
# ---------------------------------------------------------------------------

def ngram_candidates(data: bytes, window: int) -> dict:
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    _LIB.count_ngrams(data, len(data), window,
                      ctypes.byref(outp), ctypes.byref(outn))
    buf = _take(outp, outn)
    count = int.from_bytes(buf[:4], "little")
    pos, out = 4, {}
    for _ in range(count):
        ln = buf[pos]; pos += 1
        pat = buf[pos:pos + ln]; pos += ln
        f = int.from_bytes(buf[pos:pos + 4], "little"); pos += 4
        out[pat] = f
    return out


def _pats_blob(plist):
    b = bytearray(len(plist).to_bytes(4, "little"))
    for p in plist:
        b.append(len(p))
        b += p
    return bytes(b)


def segment_ids(data: bytes, plist) -> array:
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    blob = _pats_blob(plist)
    result = _LIB.segment_ids(data, len(data), blob, len(blob),
                              ctypes.byref(outp), ctypes.byref(outn))
    if result != 0:
        raise ValueError("native segment_ids rejected the pattern buffer "
                         "(error %d)" % result)
    raw = ctypes.string_at(outp, outn.value * 4)
    _LIB.afc_free(outp)
    a = array("I")
    a.frombytes(raw)
    return a


def pack_bits(ids: array, codes: dict, max_id: int) -> bytes:
    carr = array("I", bytes(4 * (max_id + 1)))
    larr = bytearray(max_id + 1)
    for sid, (c, L) in codes.items():
        carr[sid] = c
        larr[sid] = L
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    _LIB.pack_bits(ids.buffer_info()[0], len(ids),
                   carr.buffer_info()[0], bytes(larr),
                   ctypes.byref(outp), ctypes.byref(outn))
    return _take(outp, outn)


# ---------------------------------------------------------------------------
# python -m afc_native --diagnose
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys
    if "--diagnose" in _sys.argv or "-d" in _sys.argv or len(_sys.argv) == 1:
        _sys.exit(diagnose())
    print(report())
    _sys.exit(0 if AVAILABLE else 1)
