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

They are represented by an immutable ``afc2.EngineOptions`` value and passed
into each compression call. This keeps concurrent requests isolated. A legacy
context manager remains only for compatibility with older benchmark scripts.

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

[v9] A PRESET IS NOW A SEARCH, AND A COSTLIER PRESET IS NEVER LARGER
-------------------------------------------------------------------
Until v9 a preset could only search DEEPER — more DP rounds, more block-growth
rounds — because the rest of the engine's shape (the Tier-2 n-gram ceiling,
the scan window, the dictionary and block caps, the merges admitted per round)
was compiled in as constants.  Deeper is not reliably smaller.  Measured over
the corpus, Maximum came out LARGER than Balanced on several files, because a
deeper block-growth search admits structural blocks that pay for themselves
under the Bit Cost Decision Engine's estimate but crowd the dictionary and
lengthen the codes of more valuable symbols.

Those tunables are per-call from v9 on, and measurement says no single setting
wins everywhere.  Against the default profile, over the corpus:

    a 16k dictionary        -8.4% on a 10 MB text, but +2.8% on alice29
                            and +10.3% on regular delimited CSV
    a 6-byte n-gram scan   -18.3% on a stored-DOCX, -5.7% on a server log,
                            but +27.6% on the JSON sample
    an 8-byte n-gram scan  -14.8% on multipage PDF text, -6.6% on prose,
                            but +45.5% on that same JSON

So the presets stop guessing.  Each one compresses the file under a LADDER of
profiles and keeps the smallest container; ties keep the earliest profile in
the ladder, so the result is deterministic.  The ladders are nested —

    fast     ⊂ balanced ⊂ maximum

— which makes the size ordering a property of the construction rather than a
claim about a corpus: Maximum considers everything Balanced considered, and
Balanced considers everything Fast considered, so neither can come out larger.
`test_preset_size_is_monotonic` asserts it.

The profiles run concurrently (ctypes releases the GIL for the duration of a
native call), with the worker count bounded by the input size so a large file
does not multiply peak memory.  Cost, over the corpus: Balanced runs five
profiles for roughly 1.5x the single-profile wall time, Maximum runs nine for
roughly 2.5x — both still far below what the same presets cost before the v9
search-structure work.

If the loaded native library predates `afc_compress_v9` it cannot honour a
reshaped scan.  Rather than silently dropping to the ~30x slower pure-Python
path for eight of the nine profiles, the search degrades to the single default
profile: same output as V7, no search.  `afc_native.PROFILES` reports which.
"""
import contextlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import afc2

# name -> (label, description, parameters)
PRESETS = {
    "fast": {
        "label": "Fast",
        "description": ("Fewer candidate patterns, no optimal parsing and "
                        "fewer block-growth rounds. One search profile. "
                        "Fastest, for larger output."),
        "params": {"dp": False, "dp_rounds": 1, "merge_rounds": 2,
                   "min_candidate_freq": 5},
    },
    "balanced": {
        "label": "Balanced",
        "description": ("The engine default. Moderate candidate search, DP "
                        "and block growth, tried under five search profiles "
                        "with the smallest result kept. Never larger than "
                        "Fast. Best ratio-to-time trade-off."),
        "params": {"dp": True, "dp_rounds": 3, "merge_rounds": 6,
                   "min_candidate_freq": 4},
    },
    "maximum": {
        "label": "Maximum",
        "description": ("Everything Balanced tries, then again at the "
                        "deepest settings: nine search profiles, smallest "
                        "result kept. Never larger than Balanced, and the "
                        "slowest."),
        "params": {"dp": True, "dp_rounds": 8, "merge_rounds": 12,
                   "min_candidate_freq": 4},
    },
}

DEFAULT_PRESET = "balanced"

# [v9] A search profile reshapes WHERE the engine looks for structure. Every
# field maps onto an afc2.EngineOptions tunable; none of them affects decoding,
# because the dictionary and the code lengths are written into the container.
SEARCH_PROFILES = {
    "default": {},
    "wide-dict": {"max_initial_dict": 12288, "max_dict": 16384},
    "ngram6": {"ngram_max": 6},
    "ngram8": {"ngram_max": 8},
}

# The ladders are deliberately nested: each preset's list starts with the whole
# of the cheaper preset's list. That is what makes "a costlier preset is never
# larger" true by construction rather than by measurement.
_SHAPES = ("default", "wide-dict", "ngram6", "ngram8")

# Above this the ladder drops the wider n-gram scans. That is not a guess: on
# multi-megabyte text they LOSE (ngram6 and ngram8 are both about +6% on the
# 10 MB sample) while the wider dictionary is the whole win (-8.4%), so the
# trimmed ladder gives the same bytes for roughly half the work. The trim is
# applied to every preset alike, so the ladders stay nested and the size
# ordering still holds.
_LARGE_INPUT = 8 * 1024 * 1024
_LARGE_SHAPES = ("default", "wide-dict")


def _shapes_for(nbytes):
    return _LARGE_SHAPES if nbytes >= _LARGE_INPUT else _SHAPES


def _ladder(name, shapes):
    rungs = [("fast", "default")]
    # [v10] Fast gets the wider n-gram scan as a second rung. Measured on 14
    # Govdocs1 PDFs (3.7 MB of pooled transformable text), ngram8 is smaller on
    # 13 of them and 5.74% smaller overall; on the repo corpus it is 3.84%
    # smaller but LOSES on data.csv (-2.1%) and records.bin (-4.4%), so it is
    # added as a rung to choose between rather than as a new default. Guarded
    # on membership so the >=8 MB ladder, where ngram8 is a measured loss, is
    # untouched; the nesting fast <= balanced <= maximum is preserved because
    # both richer ladders start from this same list.
    if "ngram8" in shapes:
        rungs.append(("fast", "ngram8"))
    if name in ("balanced", "maximum"):
        rungs += [("balanced", shape) for shape in shapes]
    if name == "maximum":
        rungs += [("maximum", shape) for shape in shapes]
    return rungs

# Peak memory scales with input size times the number of concurrent jobs: the
# DP cost/back/span arrays alone are ~14 bytes per input byte. Cap the
# in-flight TOTAL rather than the job count, so the ladder parallelises freely
# on ordinary uploads and collapses to one job at a time as the input
# approaches config.MAX_FILE_SIZE, where a single job already dominates memory.
# AFC_PROFILE_MEMORY_BUDGET overrides it on a memory-constrained host.
_INFLIGHT_BUDGET = int(os.environ.get("AFC_PROFILE_MEMORY_BUDGET",
                                      1024 * 1024 * 1024))
_BYTES_PER_INPUT_BYTE = 16

_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    """One process-wide pool: the ladder runs on every upload, and building a
    fresh ThreadPoolExecutor per call showed up as real time on small files."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                try:
                    workers = os.cpu_count() or 1
                except Exception:
                    workers = 1
                _POOL = ThreadPoolExecutor(
                    max_workers=max(4, min(workers * 2, 16)),
                    thread_name_prefix="afc-profile")
    return _POOL


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


def options_for(name, shape="default"):
    """Return the immutable engine configuration for a preset and profile."""
    if not is_valid(name):
        name = DEFAULT_PRESET
    params = PRESETS[name]["params"]
    return afc2.EngineOptions(
        dp=params["dp"],
        dp_rounds=params["dp_rounds"],
        merge_rounds_v4=params["merge_rounds"],
        min_candidate_freq=params["min_candidate_freq"],
        **SEARCH_PROFILES.get(shape, {}),
    )


def profiles_available() -> bool:
    """True when a reshaped scan can actually be run at native speed."""
    if not afc2.NATIVE:
        return True          # pure Python honours every profile, just slowly
    return bool(getattr(afc2._native, "PROFILES", False))


def ladder_for(name, nbytes=0):
    """The (preset, profile) pairs a named preset searches, in order."""
    if not is_valid(name):
        name = DEFAULT_PRESET
    ladder = _ladder(name, _shapes_for(nbytes))
    if not profiles_available():
        # No v9 library: keep only the profiles the loaded core can run at
        # native speed. The search shrinks; nothing is computed under the
        # wrong parameters.
        ladder = [(p, sh) for (p, sh) in ladder if sh == "default"]
    return ladder


def _worker_count(nbytes, jobs):
    if jobs <= 1:
        return 1
    try:
        cpus = os.cpu_count() or 1
    except Exception:
        cpus = 1
    budget = max(1, _INFLIGHT_BUDGET // max(1, nbytes * _BYTES_PER_INPUT_BYTE))
    return max(1, min(jobs, cpus, budget))


@contextlib.contextmanager
def applied(name):
    """Legacy context manager for third-party scripts.

    Production compression no longer uses this mutation-based path; it is
    retained so old benchmark extensions do not break. New callers should use
    :func:`options_for` and pass the result to ``afc2.compress_bytes``.
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


def _run_one(data, preset, shape, fmt, adaptive, native):
    options = options_for(preset, shape)
    return afc2.compress_bytes(data, adaptive, fmt=fmt, options=options,
                               backend="native" if native else "python")


def compress_with(data: bytes, name: str, fmt: str = "auto",
                  adaptive: bool = True):
    """Compress under a preset.  Returns (blob, effective_preset, backend).

    [v9] The preset names a LADDER of search profiles rather than a single
    parameter set. Every profile in the ladder compresses the input, and the
    smallest container wins; on a tie the earliest profile in the ladder wins,
    so the result is deterministic and reproducible. Because each ladder
    contains the whole of the cheaper preset's ladder, a costlier preset can
    never produce a larger container.

    [v7] All three presets take the native path when the loaded library
    supports the extended entry point; the tunables are forwarded to it by
    afc2.compress_bytes, so the preset is honoured rather than ignored.

    With a pre-V7 library (TUNABLE False) a non-default preset still forces
    the pure-Python path, because that is the only way its parameters can take
    effect. Output is byte-identical either way — only the speed differs."""
    if not is_valid(name):
        name = DEFAULT_PRESET
    native = uses_native(name)
    backend = ("C++ native" if native else
               ("pure Python (preset)" if afc2.NATIVE else "pure Python"))
    ladder = ladder_for(name, len(data))
    if len(ladder) == 1:
        preset, shape = ladder[0]
        return _run_one(data, preset, shape, fmt, adaptive, native), name, \
            backend

    results = [None] * len(ladder)
    workers = _worker_count(len(data), len(ladder))
    if workers <= 1:
        for i, (preset, shape) in enumerate(ladder):
            results[i] = _run_one(data, preset, shape, fmt, adaptive, native)
    elif workers >= len(ladder):
        # Memory budget allows the whole ladder at once: no barriers.
        pool = _pool()
        futures = {
            pool.submit(_run_one, data, preset, shape, fmt, adaptive,
                        native): i
            for i, (preset, shape) in enumerate(ladder)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    else:
        # Large input: run in batches so peak memory stays inside the budget.
        pool = _pool()
        for start in range(0, len(ladder), workers):
            futures = {
                pool.submit(_run_one, data, ladder[i][0], ladder[i][1], fmt,
                            adaptive, native): i
                for i in range(start, min(start + workers, len(ladder)))}
            for future in as_completed(futures):
                results[futures[future]] = future.result()

    # min() keeps the first minimum it meets, so ladder order breaks ties.
    best = min(results, key=len)
    return best, name, backend
