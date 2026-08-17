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


def _build():
    """Attempt a one-time build, recording the compiler's own error output."""
    src = os.path.join(_DIR, "afc_native.cpp")
    if not os.path.exists(src):
        return _note("build", False, "afc_native.cpp not found")
    if os.name == "nt":
        out = os.path.join(_DIR, "afc_kernels.dll")
        cmd = ["g++", "-O3", "-std=c++17", "-shared", "-static", "-pthread",
               src, "-o", out]
    else:
        out = os.path.join(_DIR, "afc_kernels.so")
        cmd = ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", "-pthread",
               src, "-o", out]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        return _note("build", False,
                     "g++ is not on PATH, so the native library cannot be "
                     "built automatically. Install a C++ toolchain (Windows: "
                     "MSYS2 `pacman -S mingw-w64-x86_64-gcc`, then build from "
                     "the MinGW 64-bit shell), or ship a prebuilt "
                     "afc_kernels.dll next to afc_native.py.")
    except Exception as exc:                       # pragma: no cover
        return _note("build", False, "build failed to start: %s" % exc)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return _note("build", False,
                     "g++ exited %d: %s" % (r.returncode, err[-400:] or "(no output)"))
    _note("build", True, " ".join(cmd))
    return _load()


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
else:
    TUNABLE = False


def _take(outp, outn):
    data = ctypes.string_at(outp, outn.value)
    _LIB.afc_free(outp)
    return data


def compress(data: bytes, adaptive: bool = True, fmt: str = "auto",
             params: dict = None) -> bytes:
    """Full v4 pipeline in one native call (byte-identical to pure Python).

    `params`, when given, carries the four preset-controlled tunables:

        {"dp": bool, "dp_rounds": int, "merge_rounds": int,
         "min_freq": int, "tune": bool}

    They map 1:1 onto afc2.OPTS["dp"], afc2.DP_ROUNDS, afc2.MERGE_ROUNDS_V4
    and afc2.MIN_CANDIDATE_FREQ, so a preset reaches the native core instead
    of being silently ignored. Omitting `params` uses the engine defaults and
    calls the original entry point, so nothing changes for existing callers.
    """
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    if params is None:
        rc = _LIB.afc_compress(data, len(data), 1 if adaptive else 0,
                               _FMT_CODES.get(fmt, 0),
                               ctypes.byref(outp), ctypes.byref(outn))
    else:
        if not TUNABLE:
            raise RuntimeError("native library predates afc_compress_ex")
        rc = _LIB.afc_compress_ex(
            data, len(data), 1 if adaptive else 0, _FMT_CODES.get(fmt, 0),
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
    _LIB.segment_ids(data, len(data), blob, len(blob),
                     ctypes.byref(outp), ctypes.byref(outn))
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
