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
from array import array

_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB = None

_FMT_CODES = {"auto": 0, "afc1": 1, "afc2": 2}


def _load():
    global _LIB
    for name in ("afc_kernels.dll", "afc_kernels.so", "libafc_kernels.so"):
        p = os.path.join(_DIR, name)
        if os.path.exists(p):
            try:
                lib = ctypes.CDLL(p)
                # require the v4 entry points; stale v3-only binaries force
                # a rebuild so engine and library never disagree
                if not hasattr(lib, "afc_compress"):
                    continue
                _LIB = lib
                return True
            except OSError:
                pass
    return False


def _build():
    src = os.path.join(_DIR, "afc_native.cpp")
    if not os.path.exists(src):
        return False
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
        return r.returncode == 0 and _load()
    except FileNotFoundError:
        return False


AVAILABLE = _load() or _build()

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


def _take(outp, outn):
    data = ctypes.string_at(outp, outn.value)
    _LIB.afc_free(outp)
    return data


def compress(data: bytes, adaptive: bool = True, fmt: str = "auto") -> bytes:
    """Full v4 pipeline in one native call (byte-identical to pure Python)."""
    outp, outn = ctypes.c_void_p(), ctypes.c_uint32()
    rc = _LIB.afc_compress(data, len(data), 1 if adaptive else 0,
                           _FMT_CODES.get(fmt, 0),
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
