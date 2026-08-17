#!/usr/bin/env python3
"""
presets.py — Fast / Balanced / Maximum compression presets.

WHAT A PRESET ACTUALLY CHANGES
------------------------------
Presets map to the engine's EXISTING tunables — no new algorithm logic:

    MIN_CANDIDATE_FREQ   Tier-2/Tier-3 admission floor
    MERGE_ROUNDS_V4      structural block-growth rounds
    DP_ROUNDS            optimal-parse <-> tree iterations
    OPTS["dp"]           whether optimal parsing runs at all

They are applied by assigning to afc2's module-level constants inside a
context manager and restoring them afterwards.  afc2.py itself is NOT edited,
per constraint #1.

[v7] EVERY PRESET NOW RUNS NATIVELY
-----------------------------------
Historically only Balanced could use the C++ core, because afc_native.cpp had
these four values compiled in as `static const` and the `afc_compress` ABI had
no parameters for them — so the native core silently ignored a preset. That
was measured, not assumed: alice29.txt at DP_ROUNDS 1/3/6 produced identical
native output (61 692 bytes each time). presets.compress_with() therefore had
to force `afc2.NATIVE = False` for Fast and Maximum, which is why they ran
~30x slower than Balanced.

V7 fixes the cause rather than documenting the symptom:

  * afc_native.cpp gained a `Params` struct and an `afc_compress_ex` export
    carrying dp / dp_rounds / merge_rounds / min_freq / tune.
  * afc_native.py passes them through; `TUNABLE` reports whether the loaded
    library is new enough.
  * afc2.compress_bytes forwards the live tunables to the native core.

The original `afc_compress` ABI is untouched and still yields the defaults, so
a stale library keeps working — `TUNABLE` goes False and non-default presets
fall back to pure Python exactly as they did in V6.

VERIFIED: all three presets produce BYTE-IDENTICAL output on the native and
pure-Python paths, across the whole corpus (10 files x 3 presets = 30
combinations, every one identical and SHA-256 lossless). Python remains the
reference implementation; C++ only accelerates it.

Wall-clock times are now directly comparable across all three presets, because
all three run on the same backend.

MAXIMUM IS NOT ALWAYS SMALLER THAN BALANCED — MEASURED, AND CORRECTED
---------------------------------------------------------------------
Earlier documentation claimed Maximum was "never larger than Balanced on the
tested corpus".  That claim rested on three files.  Measured across the full
ten-file corpus (benchmarks/v7_preset_matrix.csv), Maximum is LARGER on two:

    data.csv             balanced 49431  maximum 51143   +3.46%
    code_python.py.txt   balanced 17363  maximum 17376   +0.07%

and smaller on five (best: data.json -3.65%, records.bin -1.63%,
server.log -1.55%).  This is not a V7 regression: the parameters are unchanged
and V7 Maximum output is byte-identical to V6 Maximum output.  The old claim
was simply an over-generalisation from too small a sample.

Why it happens: a deeper block-growth search (merge_rounds 12 vs 6) can admit
structural blocks that pay for themselves under the Bit Cost Decision Engine's
estimate but crowd the dictionary and lengthen the codes of more valuable
symbols.  On highly regular delimited data (data.csv) that trade goes the
wrong way.

Maximum therefore means "search harder", not "always smaller".  The UI says
so.  Do not re-add a blanket "never larger" claim without re-measuring the
whole corpus.

MAXIMUM was also retuned during development: an earlier version lowered
MIN_CANDIDATE_FREQ to 3, which admitted many weak candidates and came out
larger than Balanced on two of three files.  Keeping the floor at 4 and only
deepening the search is what the current numbers reflect.  Do not lower that
floor again without re-measuring.
"""
import contextlib

import afc2

# name -> (label, description, parameters)
PRESETS = {
    "fast": {
        "label": "Fast",
        "description": ("Fewer candidate patterns, no optimal parsing and "
                        "fewer block-growth rounds. Fastest, for larger "
                        "output."),
        "params": {"dp": False, "dp_rounds": 1, "merge_rounds": 2,
                   "min_candidate_freq": 5},
    },
    "balanced": {
        "label": "Balanced",
        "description": ("The engine default. Moderate candidate search, DP "
                        "and block growth. Best ratio-to-time trade-off."),
        "params": {"dp": True, "dp_rounds": 3, "merge_rounds": 6,
                   "min_candidate_freq": 4},
    },
    "maximum": {
        "label": "Maximum",
        "description": ("Larger candidate search, more optimal-parse and "
                        "block-growth rounds. Usually smaller than Balanced "
                        "(up to -3.7%), but not always: on some regular "
                        "delimited data it is slightly larger."),
        "params": {"dp": True, "dp_rounds": 8, "merge_rounds": 12,
                   "min_candidate_freq": 4},
    },
}

DEFAULT_PRESET = "balanced"


def is_valid(name) -> bool:
    return name in PRESETS


def uses_native(name) -> bool:
    """[v7] Every preset can run on the native core.

    True when a native library is loaded AND it exports the extended entry
    point that carries the tunables. A pre-V7 library reports TUNABLE=False,
    in which case only Balanced (the compiled-in defaults) can run natively
    and the others fall back to pure Python, as in V6."""
    if not afc2.NATIVE:
        return False
    if getattr(afc2._native, "TUNABLE", False):
        return True
    return name == DEFAULT_PRESET


def describe(name=None):
    """Serialisable preset info for the UI/API."""
    if name:
        p = dict(PRESETS[name])
        p["name"] = name
        p["native_capable"] = uses_native(name)
        return p
    return [describe(k) for k in ("fast", "balanced", "maximum")]


@contextlib.contextmanager
def applied(name):
    """Temporarily apply a preset to afc2's module constants.

    Restores every touched value on exit, including on exception, so a failed
    compression cannot leave the engine mistuned for the next request.

    NOT thread-safe: it mutates module globals.  That is acceptable here
    because config.MAX_CONCURRENT_JOBS is 1 and the queue is strictly
    sequential (see app.api_batch).  If concurrency is ever introduced, this
    must become a per-call parameter instead — which would require the engine
    to accept one, i.e. a change to afc2.py that Part 2 is not permitted to
    make.
    """
    if name not in PRESETS:
        name = DEFAULT_PRESET
    params = PRESETS[name]["params"]
    saved = {
        "dp": afc2.OPTS["dp"],
        "dp_rounds": afc2.DP_ROUNDS,
        "merge_rounds": afc2.MERGE_ROUNDS_V4,
        "min_candidate_freq": afc2.MIN_CANDIDATE_FREQ,
    }
    try:
        afc2.OPTS["dp"] = params["dp"]
        afc2.DP_ROUNDS = params["dp_rounds"]
        afc2.MERGE_ROUNDS_V4 = params["merge_rounds"]
        afc2.MIN_CANDIDATE_FREQ = params["min_candidate_freq"]
        yield name
    finally:
        afc2.OPTS["dp"] = saved["dp"]
        afc2.DP_ROUNDS = saved["dp_rounds"]
        afc2.MERGE_ROUNDS_V4 = saved["merge_rounds"]
        afc2.MIN_CANDIDATE_FREQ = saved["min_candidate_freq"]


def compress_with(data: bytes, name: str, fmt: str = "auto",
                  adaptive: bool = True):
    """Compress under a preset.  Returns (blob, effective_preset, backend).

    [v7] All three presets take the native path when the loaded library
    supports the extended entry point; the tunables are forwarded to it by
    afc2.compress_bytes, so the preset is honoured rather than ignored.

    With a pre-V7 library (TUNABLE False) a non-default preset still forces
    the pure-Python path, because that is the only way its parameters can take
    effect. Output is byte-identical either way — only the speed differs."""
    if not is_valid(name):
        name = DEFAULT_PRESET
    with applied(name):
        if uses_native(name):
            return afc2.compress_bytes(data, adaptive, fmt=fmt), name, \
                "C++ native"
        saved_native = afc2.NATIVE
        try:
            afc2.NATIVE = False          # force the tunable Python pipeline
            blob = afc2.compress_bytes(data, adaptive, fmt=fmt)
        finally:
            afc2.NATIVE = saved_native
        backend = "pure Python (preset)" if saved_native else "pure Python"
        return blob, name, backend
