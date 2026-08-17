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

IMPORTANT MEASURED LIMITATION — READ BEFORE CHANGING
----------------------------------------------------
The C++ native core has these values compiled in as constants
(afc_native.cpp).  It therefore IGNORES the Python tunables completely.  This
was measured, not assumed: compressing alice29.txt through the native path at
DP_ROUNDS = 1, 3 and 6 produced byte-identical output (61 692 bytes each
time).  Tuning the native core would mean editing afc_native.cpp, which the
constraints forbid.

Consequence: a non-default preset MUST run on the pure-Python path, which is
roughly 30x slower than the native core.  So:

  * BALANCED is the engine default.  Its output is byte-identical on the
    native and pure-Python paths (verified in Part 1), so it runs natively and
    fast.
  * FAST and MAXIMUM force the pure-Python path, because that is the only way
    their parameters can take effect.

That makes wall-clock time NOT comparable across backends: "Fast" is faster
than "Maximum" on the same (Python) path, but both are slower than "Balanced"
running natively.  The UI says this plainly rather than implying Fast is the
quickest route to a compressed file.  Ratio comparisons ARE apples-to-apples,
because ratio depends only on the parameters, not the backend.

Measured on the Canterbury/corpus files (pure-Python path, so the three are
directly comparable):

    fields.c   fast 6052 B / 40 ms | balanced 5330 B / 127 ms | maximum 5330 B / ~250 ms
    cp.html    fast 12781 B / 85 ms | balanced 10869 B / 337 ms | maximum 10837 B / ~700 ms
    data.json                        | balanced 16704 B        | maximum 16095 B (-3.65%)

MAXIMUM was retuned during development: an earlier version lowered
MIN_CANDIDATE_FREQ to 3, which admitted many weak candidates and came out
*larger* than Balanced on two of three files (+0.17% and +2.95%).  Keeping the
floor at 4 and only deepening the search makes Maximum never worse than
Balanced.  Do not lower that floor again without re-measuring.
"""
import contextlib

import afc2

# name -> (label, description, parameters)
PRESETS = {
    "fast": {
        "label": "Fast",
        "description": ("Skips optimal parsing and does fewer block-growth "
                        "rounds. Roughly 3x quicker than Balanced on the same "
                        "path, for about 12-18% larger output."),
        "params": {"dp": False, "dp_rounds": 1, "merge_rounds": 2,
                   "min_candidate_freq": 5},
    },
    "balanced": {
        "label": "Balanced",
        "description": ("The engine default, and the only preset that can use "
                        "the C++ native core. Best ratio-to-time trade-off."),
        "params": {"dp": True, "dp_rounds": 3, "merge_rounds": 6,
                   "min_candidate_freq": 4},
    },
    "maximum": {
        "label": "Maximum",
        "description": ("Deeper optimal-parse and block-growth search. Up to "
                        "~3.6% smaller than Balanced on structured data, at "
                        "roughly 2x the time. Never larger than Balanced on "
                        "the tested corpus."),
        "params": {"dp": True, "dp_rounds": 8, "merge_rounds": 12,
                   "min_candidate_freq": 4},
    },
}

DEFAULT_PRESET = "balanced"


def is_valid(name) -> bool:
    return name in PRESETS


def uses_native(name) -> bool:
    """Only Balanced can run on the native core (see module docstring)."""
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

    Non-default presets force the pure-Python path because the native core
    cannot honour them (see module docstring).  The forcing is done with the
    documented AFC_NO_NATIVE switch via afc2.NATIVE, restored afterwards."""
    if not is_valid(name):
        name = DEFAULT_PRESET
    with applied(name):
        if uses_native(name):
            backend = "C++ native" if afc2.NATIVE else "pure Python"
            return afc2.compress_bytes(data, adaptive, fmt=fmt), name, backend
        saved_native = afc2.NATIVE
        try:
            afc2.NATIVE = False          # force the tunable Python pipeline
            blob = afc2.compress_bytes(data, adaptive, fmt=fmt)
        finally:
            afc2.NATIVE = saved_native
        return blob, name, "pure Python (preset)"
