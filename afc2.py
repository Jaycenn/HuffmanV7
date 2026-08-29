#!/usr/bin/env python3
"""
afc2.py — AFC adaptive engine, v4.

Pipeline (thesis terminology) — the v3 stages are all still here; v4 changes
are marked [v4]:

  Tier-1  byte-frequency scan
  Tier-2  n-gram scan (2..5 bytes, windowed)
  Tier-3  word-token scan
  Bit Cost Decision Engine:
      accept a pattern only if
      freq * (bits_spelled_out - bits_as_one_symbol) > 8 * (pattern_len + 3)
  greedy longest-match segmentation (seed parse)
  engine-gated structural block growth (adjacent-pair merging,
      blocks <= 128 bytes, dictionary cap 4096)
      [v4] merge scoring adds dictionary-refund accounting: retiring a child
      entry that the merge makes unused refunds its 8*(len+3) dictionary cost
  strict final audit with actual counts
  [v4] optimal parsing: dynamic-programming shortest path over the ACTUAL
      code lengths, iterated parse <-> tree (2 rounds), replacing the greedy
      parse as the final segmentation
  [v4] hybrid Huffman tree is now length-limited (<=16 bits) via
      deterministic package-merge — still static canonical Huffman
  containers: AFC1 (legacy, byte-compatible) and [v4] AFC2 (compacted
      header: delta+varint symbol ids, 4-bit packed code lengths, dictionary
      entries coded with the literal Huffman codes); --format auto keeps
      AFC1 unless AFC2 is strictly smaller; raw-storage fallback always on

Engine toggles in OPTS allow each v4 optimization to be measured in
isolation (used by benchmarks/bench.py --opts).  All decisions are
integer-only and deterministic; the pure-Python path and the native path
(afc_native.cpp) produce byte-identical containers.
"""

import hashlib
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass

import afc
from afc import (MODE_ADAPTIVE, NOCODE_COST, est_code_len, huffman_lengths,
                 package_merge_lengths, canonical_codes, pack_ids,
                 finish_container, emit_raw)

_LOGGER = logging.getLogger(__name__)

try:
    import afc_native as _native
    NATIVE = _native.AVAILABLE and not os.environ.get("AFC_NO_NATIVE")
except Exception:
    _native = None
    NATIVE = False

# ---- engine constants ------------------------------------------------------
SCAN_WINDOW = 1 << 20        # Tier-2 n-gram scan window
NGRAM_MIN, NGRAM_MAX = 2, 5  # Tier-2 range
MIN_CANDIDATE_FREQ = 4       # Tier-2/Tier-3 admission floor (v3 value)
WORD_MIN, WORD_MAX = 3, 24   # Tier-3 token lengths
MAX_INITIAL_DICT = 3072      # room left below the 4096 cap for block growth
MAX_DICT = 4096              # structural dictionary cap
MAX_BLOCK = 128              # structural block growth byte cap
MERGE_ROUNDS = 3             # v3 adjacent-pair merge rounds
MERGE_ROUNDS_V4 = 6          # [v4] deeper growth when refund accounting is on
MERGES_PER_ROUND = 32        # accepted merges per round
DP_ROUNDS = 3                # [v4] optimal-parse <-> tree iterations
TUNE_SMALL = 65536           # [v4] files below this use MIN_CANDIDATE_FREQ-1

# [v4] optimization toggles (all measured individually; see CHANGES.md)
OPTS = {
    "dp": True,       # (b) DP shortest-path parse, iterated with the tree
    "llhuff": True,   # (c) length-limited canonical Huffman (package-merge)
    "hdr2": True,     # (a) AFC2 compacted container, picked only if smaller
    "refund": True,   # (e) block-growth dictionary-refund scoring
    "tune": True,     # (d) per-file parameter tuning
}


@dataclass(frozen=True)
class EngineOptions:
    """Immutable per-call configuration for the reference/native engine.

    The module globals above remain as a compatibility surface for the
    historical benchmark scripts, but production callers pass this object.
    That prevents one request's preset from changing another request while
    preserving the exact legacy defaults and encoded bytes.
    """

    dp: bool = True
    llhuff: bool = True
    hdr2: bool = True
    refund: bool = True
    tune: bool = True
    dp_rounds: int = DP_ROUNDS
    merge_rounds_v4: int = MERGE_ROUNDS_V4
    min_candidate_freq: int = MIN_CANDIDATE_FREQ
    # [v9] Search-shape tunables. These were module constants, which meant a
    # preset could only search DEEPER, never DIFFERENTLY — and measurement
    # showed no single setting is best for every file: a wider n-gram scan
    # wins on prose and PDF text, a larger dictionary wins on multi-megabyte
    # text, and both lose badly on regular delimited data. Making them
    # per-call is what lets presets.py try several profiles and keep the
    # smallest container. None of them affects decoding: the dictionary and
    # the code lengths are written into the container explicitly.
    scan_window: int = SCAN_WINDOW
    ngram_max: int = NGRAM_MAX
    max_initial_dict: int = MAX_INITIAL_DICT
    max_dict: int = MAX_DICT
    max_block: int = MAX_BLOCK
    merges_per_round: int = MERGES_PER_ROUND

    def __post_init__(self):
        if self.dp_rounds < 0:
            raise ValueError("dp_rounds must be non-negative")
        if self.merge_rounds_v4 < 0:
            raise ValueError("merge_rounds_v4 must be non-negative")
        if self.min_candidate_freq < 2:
            raise ValueError("min_candidate_freq must be at least 2")
        if self.scan_window < 1024:
            raise ValueError("scan_window must be at least 1024")
        # n-gram keys pack into a uint64 on the native side; 8 is the ceiling.
        if not (NGRAM_MIN <= self.ngram_max <= 8):
            raise ValueError("ngram_max must be between %d and 8" % NGRAM_MIN)
        if self.max_dict < 256:
            raise ValueError("max_dict must be at least 256")
        if not (0 < self.max_initial_dict <= self.max_dict):
            raise ValueError("max_initial_dict must be in (0, max_dict]")
        if not (2 <= self.max_block <= 65535):
            raise ValueError("max_block must be between 2 and 65535")
        if self.merges_per_round < 1:
            raise ValueError("merges_per_round must be at least 1")


DEFAULT_OPTIONS = EngineOptions()


def current_options() -> EngineOptions:
    """Snapshot the legacy module settings into an immutable option set."""
    return EngineOptions(
        dp=bool(OPTS["dp"]), llhuff=bool(OPTS["llhuff"]),
        hdr2=bool(OPTS["hdr2"]), refund=bool(OPTS["refund"]),
        tune=bool(OPTS["tune"]), dp_rounds=int(DP_ROUNDS),
        merge_rounds_v4=int(MERGE_ROUNDS_V4),
        min_candidate_freq=int(MIN_CANDIDATE_FREQ),
        scan_window=int(SCAN_WINDOW), ngram_max=int(NGRAM_MAX),
        max_initial_dict=int(MAX_INITIAL_DICT), max_dict=int(MAX_DICT),
        max_block=int(MAX_BLOCK), merges_per_round=int(MERGES_PER_ROUND),
    )


# -----------------------------------------------------------------------------
# Tier scans
# -----------------------------------------------------------------------------

def _tier2_ngrams(data: bytes, min_freq: int,
                  options: "EngineOptions" = None) -> dict:
    options = DEFAULT_OPTIONS if options is None else options
    win = data[:options.scan_window]
    out = {}
    n = len(win)
    for L in range(NGRAM_MIN, options.ngram_max + 1):
        if L > n:
            break
        cnt = Counter(bytes(win[i:i + L]) for i in range(n - L + 1))
        for pat, f in cnt.items():
            if f >= min_freq:
                out[pat] = f
    return out


def _tier3_words(data: bytes, min_freq: int) -> dict:
    import re
    cnt = Counter(m.group(0) for m in
                  re.finditer(rb"[0-9A-Z_a-z]{3,}", data)
                  if len(m.group(0)) <= WORD_MAX)
    return {w: f for w, f in cnt.items()
            if f >= min_freq and len(w) >= WORD_MIN}


# -----------------------------------------------------------------------------
# Bit Cost Decision Engine
# -----------------------------------------------------------------------------

def _bit_cost_gain(pat: bytes, freq: int, lit_bits, sym_bits: int) -> int:
    """freq*(bits_spelled_out - bits_as_one_symbol) - 8*(len+3); accept > 0."""
    spelled = 0
    for b in pat:
        spelled += lit_bits[b]
    return freq * (spelled - sym_bits) - 8 * (len(pat) + 3)


def _tier1_lit_bits(data: bytes) -> list:
    total = len(data)
    tier1 = [0] * 256
    for b in data:
        tier1[b] += 1
    return [est_code_len(f, total) for f in tier1]


def _select_candidates(data: bytes, min_freq: int, lit_bits: list,
                       options: "EngineOptions" = None) -> list:
    options = DEFAULT_OPTIONS if options is None else options
    total = len(data)
    cands = _tier2_ngrams(data, min_freq, options)
    for w, f in _tier3_words(data, min_freq).items():
        if w not in cands:
            cands[w] = f
    scored = []
    for pat, f in cands.items():
        gain = _bit_cost_gain(pat, f, lit_bits, est_code_len(f, total))
        if gain > 0:
            scored.append((gain, pat))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [pat for _, pat in scored[:options.max_initial_dict]]


# -----------------------------------------------------------------------------
# segmentation: greedy seed parse + [v4] DP optimal parse
# -----------------------------------------------------------------------------

def _pattern_index(patterns):
    pset = {p: i for i, p in enumerate(patterns)}
    lens_of = {}
    for p in patterns:
        lens_of.setdefault(p[0], set()).add(len(p))
    return pset, lens_of


def _segment_greedy(data: bytes, patterns: list) -> list:
    pset, lens_raw = _pattern_index(patterns)
    lens_of = {b: sorted(s, reverse=True) for b, s in lens_raw.items()}
    ids = []
    push = ids.append
    n = len(data)
    i = 0
    mv = memoryview(data)
    while i < n:
        b = data[i]
        ls = lens_of.get(b)
        if ls:
            for L in ls:
                if i + L <= n:
                    idx = pset.get(bytes(mv[i:i + L]))
                    if idx is not None:
                        push(256 + idx)
                        i += L
                        break
            else:
                push(b)
                i += 1
        else:
            push(b)
            i += 1
    return ids


def _segment_optimal(data: bytes, patterns: list, lengths: dict) -> list:
    """[v4] shortest-path segmentation over actual code lengths.  Symbols
    without a code cost NOCODE_COST bits.  Ties resolve to the literal /
    shortest pattern (strict < updates only, literal tried first, pattern
    lengths ascending)."""
    n = len(data)
    pset, lens_raw = _pattern_index(patterns)
    lens_of = {b: sorted(s) for b, s in lens_raw.items()}
    litcost = [lengths.get(b, NOCODE_COST) for b in range(256)]
    patcost = [lengths.get(256 + i, NOCODE_COST)
               for i in range(len(patterns))]
    INF = 1 << 60
    cost = [INF] * (n + 1)
    cost[0] = 0
    back = [0] * (n + 1)       # symbol id that reaches this position
    blen = [0] * (n + 1)       # its span in bytes
    mv = memoryview(data)
    for i in range(n):
        c = cost[i]
        if c >= INF:
            continue
        b = data[i]
        nc = c + litcost[b]
        if nc < cost[i + 1]:
            cost[i + 1] = nc
            back[i + 1] = b
            blen[i + 1] = 1
        ls = lens_of.get(b)
        if ls:
            for L in ls:
                j = i + L
                if j > n:
                    break
                idx = pset.get(bytes(mv[i:j]))
                if idx is None:
                    continue
                nc = c + patcost[idx]
                if nc < cost[j]:
                    cost[j] = nc
                    back[j] = 256 + idx
                    blen[j] = L
    ids = []
    pos = n
    while pos > 0:
        ids.append(back[pos])
        pos -= blen[pos]
    ids.reverse()
    return ids


# -----------------------------------------------------------------------------
# engine-gated structural block growth (adjacent-pair merging)
# -----------------------------------------------------------------------------

def _grow_blocks(ids: list, patterns: list, rounds: int,
                 refund: bool, min_freq: int = MIN_CANDIDATE_FREQ,
                 options: "EngineOptions" = None) -> tuple:
    options = DEFAULT_OPTIONS if options is None else options

    def expand(sid):
        return bytes([sid]) if sid < 256 else patterns[sid - 256]

    for _ in range(rounds):
        if len(patterns) >= options.max_dict:
            break
        total = len(ids)
        if total < 2:
            break
        pairs = Counter(zip(ids, ids[1:]))
        sym_counts = Counter(ids)
        lit_bits = [est_code_len(sym_counts.get(b, 0), total)
                    for b in range(256)]
        accepted = []
        for (a, b), f in pairs.items():
            if f < min_freq:
                continue
            merged = expand(a) + expand(b)
            if len(merged) > options.max_block:
                continue
            gain = _bit_cost_gain(merged, f, lit_bits,
                                  est_code_len(f, total))
            if refund:
                # [v4] dictionary-refund accounting: if the merge would leave
                # a child pattern entry unused, its dictionary cost comes back
                for child in (a, b):
                    if child >= 256 and sym_counts[child] == f and a != b:
                        gain += 8 * (len(patterns[child - 256]) + 3)
            if gain > 0:
                accepted.append((gain, merged, a, b))
        if not accepted:
            break
        # key includes (a, b): two different pairs can concatenate to the
        # same bytes with equal gain, and native/JS ports must sort likewise
        accepted.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
        room = options.max_dict - len(patterns)
        chosen = {}
        pat_index = {p: i for i, p in enumerate(patterns)}
        for gain, merged, a, b in accepted[:options.merges_per_round]:
            if (a, b) in chosen:
                continue
            if merged in pat_index:
                chosen[(a, b)] = 256 + pat_index[merged]
            elif room > 0:
                patterns.append(merged)
                pat_index[merged] = len(patterns) - 1
                chosen[(a, b)] = 256 + len(patterns) - 1
                room -= 1
        if not chosen:
            break
        out = []
        push = out.append
        i = 0
        n = len(ids)
        while i < n:
            if i + 1 < n:
                nid = chosen.get((ids[i], ids[i + 1]))
                if nid is not None:
                    push(nid)
                    i += 2
                    continue
            push(ids[i])
            i += 1
        ids = out
    return ids, patterns


# -----------------------------------------------------------------------------
# strict final audit with actual counts
# -----------------------------------------------------------------------------

def _final_audit(ids: list, patterns: list, lit_bits: list) -> tuple:
    """Strict final audit with actual counts.  Pattern frequencies are the
    ACTUAL post-segmentation counts; the spelled-out side is priced with the
    Tier-1 literal estimates (the same estimator the admission stage uses),
    so patterns whose bytes are currently absent from the id stream are not
    inflated to the NOCODE penalty."""
    counts = Counter(ids)
    total = len(ids)
    drop = set()
    for idx, pat in enumerate(patterns):
        sid = 256 + idx
        f = counts.get(sid, 0)
        if f == 0:
            drop.add(sid)
            continue
        gain = _bit_cost_gain(pat, f, lit_bits, est_code_len(f, total))
        if gain <= 0:
            drop.add(sid)
    if drop:
        out = []
        push = out.append
        for sid in ids:
            if sid in drop and sid >= 256:
                if counts.get(sid, 0):
                    out.extend(patterns[sid - 256])
                continue
            push(sid)
        ids = out
        counts = Counter(ids)
    keep = [i for i in range(len(patterns)) if (256 + i) in counts]
    remap = {256 + old: 256 + new for new, old in enumerate(keep)}
    patterns = [patterns[i] for i in keep]
    if remap:
        ids = [remap.get(sid, sid) for sid in ids]
    return ids, patterns


# -----------------------------------------------------------------------------
# top level
# -----------------------------------------------------------------------------

def _default_fmt(options: EngineOptions) -> str:
    return "auto" if options.hdr2 else "afc1"


def _build_lengths(counts: Counter, options: EngineOptions) -> dict:
    if options.llhuff:
        return package_merge_lengths(counts, afc.MAX_CODE_LEN)
    return huffman_lengths(counts)


# [v7] Optimization toggles the native core has compiled in as always-on. It
# has no way to express these being FALSE, so a run with any of them disabled
# must still use the pure-Python reference path. Everything else — the four
# preset-controlled tunables — is now passed through to the native core via
# afc_compress_ex, which is what makes Fast and Maximum native-capable.
_NATIVE_FIXED_OPTS = ("llhuff", "hdr2", "refund")


def _native_params(options: EngineOptions) -> dict:
    """The current tunables, in the shape afc_native.compress() expects.

    Mirrors exactly what the pure-Python path below computes, so the two
    backends stay byte-identical under every preset."""
    return {
        "dp": options.dp,
        "dp_rounds": options.dp_rounds,
        "merge_rounds": (options.merge_rounds_v4
                         if (options.tune and options.refund)
                         else MERGE_ROUNDS),
        "min_freq": options.min_candidate_freq,
        "tune": options.tune,
        "scan_window": options.scan_window,
        "ngram_max": options.ngram_max,
        "max_initial_dict": options.max_initial_dict,
        "max_dict": options.max_dict,
        "max_block": options.max_block,
        "merges_per_round": options.merges_per_round,
    }


# [v7] Container-aware processing for structured formats (PDF, DOCX/OOXML).
# When on, a PDF or ZIP-packaged file is analysed so that already-compressed
# regions (JPEG, deflate payloads) are preserved verbatim and only the
# structural/textual material is fed to the Hybrid-Huffman pipeline. It is a
# routing decision, not a second compressor — see containers.py. Set False to
# force the plain whole-file path (used by the benchmarks for A/B numbers).
CONTAINER_AWARE = True

_CONTAINER_MAGIC = (b"%PDF-", b"PK\x03\x04")


def _looks_like_container(data: bytes) -> bool:
    return data[:5] == _CONTAINER_MAGIC[0] or data[:4] == _CONTAINER_MAGIC[1]


def compress_bytes(data: bytes, adaptive: bool = True,
                   fmt: str = None, container_aware: bool = None,
                   options: EngineOptions = None,
                   backend: str = "auto") -> bytes:
    """Compress bytes with an immutable per-call engine configuration.

    ``backend`` is ``auto`` (prefer native), ``native`` (require it), or
    ``python`` (reference implementation). Existing callers that omit both
    new arguments retain the historical module-global behavior.
    """
    options = current_options() if options is None else options
    if not isinstance(options, EngineOptions):
        raise TypeError("options must be an EngineOptions instance")
    if backend not in ("auto", "native", "python"):
        raise ValueError("backend must be auto, native, or python")
    if fmt is None:
        fmt = _default_fmt(options)

    # [v7] Structured containers get their components routed first. The
    # callback below is the PLAIN path (container_aware=False), which is what
    # actually compresses the pooled buffer — the engine is unchanged and is
    # still the only thing doing compression. Falling through on None keeps
    # every existing behaviour intact.
    use_ca = CONTAINER_AWARE if container_aware is None else container_aware
    if adaptive and use_ca and len(data) > 0 and _looks_like_container(data):
        try:
            import containers
            blob, _info = containers.compress_container(
                data, fmt=fmt,
                compress_fn=lambda d, a=True, fmt=fmt: compress_bytes(
                    d, a, fmt=fmt, container_aware=False, options=options,
                    backend=backend))
            if blob is not None:
                return blob
        except Exception:
            _LOGGER.warning(
                "Container-aware analysis failed; falling back to the "
                "plain compression path.",
                exc_info=True,
            )

    native_ok = (backend != "python" and NATIVE
                 and hasattr(_native, "compress")
                 and fmt in ("auto", "afc1", "afc2"))
    if backend == "native" and not native_ok:
        raise RuntimeError("native backend requested but unavailable")
    if not adaptive:
        if native_ok and len(data) > 0:
            return _native.compress(bytes(data), False, fmt)
        return afc.compress_bytes(data, False, fmt=fmt)
    if len(data) == 0:
        return emit_raw(data, afc.MAGIC2 if fmt == "afc2" else afc.MAGIC1)

    fixed_enabled = (options.llhuff and options.hdr2 and options.refund)
    if native_ok and fixed_enabled:
        if getattr(_native, "TUNABLE", False):
            # [v7] any preset, natively. [v9] a search profile that reshapes
            # the scan needs afc_compress_v9; an older library raises rather
            # than quietly ignoring the shape, and we fall through to the
            # pure-Python reference, which can always honour it.
            try:
                return _native.compress(bytes(data), True, fmt,
                                        params=_native_params(options))
            except RuntimeError:
                if backend == "native":
                    raise
        elif (options == DEFAULT_OPTIONS):
            # library predates afc_compress_ex: only the compiled-in defaults
            # can be honoured, so anything else falls through to Python.
            return _native.compress(bytes(data), True, fmt)
    if backend == "native":
        raise RuntimeError("native backend cannot represent these options")

    rounds = options.merge_rounds_v4 if (options.tune and options.refund) \
        else MERGE_ROUNDS
    lit_bits = _tier1_lit_bits(data)
    blob = _compress_core(data, fmt, options.min_candidate_freq, rounds,
                          lit_bits, options)
    if options.tune and len(data) < TUNE_SMALL:
        # [v4] per-file tuning by trial: small files also try the lower
        # admission floor; the smaller container wins (ties keep the v3
        # setting, so tuning can never regress).
        alt = _compress_core(data, fmt, options.min_candidate_freq - 1,
                             rounds, lit_bits, options)
        if len(alt) < len(blob):
            blob = alt
    return blob


def _compress_core(data: bytes, fmt: str, min_freq: int, rounds: int,
                   lit_bits: list, options: EngineOptions) -> bytes:
    patterns = _select_candidates(data, min_freq, lit_bits, options)
    ids = _segment_greedy(data, patterns)
    ids, patterns = _grow_blocks(ids, patterns, rounds, options.refund,
                                 min_freq, options)
    ids, patterns = _final_audit(ids, patterns, lit_bits)

    if options.dp:
        # [v4] optimal parsing: iterate DP parse <-> tree over actual lengths
        lengths = _build_lengths(Counter(ids), options)
        for _ in range(options.dp_rounds):
            ids = _segment_optimal(data, patterns, lengths)
            lengths = _build_lengths(Counter(ids), options)
        ids, patterns = _final_audit(ids, patterns, lit_bits)

    lengths = _build_lengths(Counter(ids), options)
    codes = canonical_codes(lengths)
    bitstream = pack_ids(ids, codes)
    return finish_container(MODE_ADAPTIVE, data, patterns, lengths,
                            bitstream, fmt)


def decompress_bytes(blob: bytes) -> bytes:
    # [v7] Dispatch on the container version BEFORE anything else, so an old
    # container is never reinterpreted under a newer format and vice versa.
    # AFC1/AFC2 keep their exact existing decode path. AFC3 is the direct
    # component wrapper; AFC4 is the explicitly versioned DOCX XML/token
    # wrapper; AFC6 is the PDF zlib/token wrapper. Dispatch happens before the
    # native decoder sees any component magic.
    if blob[:4] == b"AFC5":
        import afc5
        return afc5.decompress(blob, decompress_fn=decompress_bytes)
    if blob[:4] == b"AFC3":
        import containers
        return containers.decompress_afc3(blob, decompress_fn=decompress_bytes)
    if blob[:4] == b"AFC4":
        import containers
        return containers.decompress_afc4(blob, decompress_fn=decompress_bytes)
    if blob[:4] == b"AFC6":
        import containers
        return containers.decompress_afc6(blob, decompress_fn=decompress_bytes)
    if NATIVE:
        try:
            return _native.decompress(bytes(blob))
        except Exception:
            if blob[:4] not in (afc.MAGIC1, afc.MAGIC2):
                raise
            # native lib without AFC2 support etc. — fall through to Python
    return afc.decompress_bytes(blob)


# -----------------------------------------------------------------------------
# CLI: compress / decompress / verify / benchmark
# -----------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="AFC adaptive engine (v4)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("compress", "decompress", "verify", "benchmark"):
        s = sub.add_parser(c)
        if c == "benchmark":
            s.add_argument("files", nargs="+")
            s.add_argument("--runs", type=int, default=7)
        else:
            s.add_argument("infile")
            if c != "verify":
                s.add_argument("outfile")
        if c in ("compress", "verify", "benchmark"):
            s.add_argument("--baseline", action="store_true")
        if c == "compress":
            s.add_argument("--format", choices=("auto", "afc1", "afc2"),
                           default=None)
    a = ap.parse_args()

    if a.cmd == "compress":
        data = open(a.infile, "rb").read()
        blob = compress_bytes(data, not a.baseline, fmt=a.format)
        open(a.outfile, "wb").write(blob)
        print(f"{len(data)} -> {len(blob)} bytes "
              f"({100.0 * (1 - len(blob) / max(1, len(data))):.2f}% saved, "
              f"{blob[:4].decode('ascii')} container, "
              f"{'native' if NATIVE else 'python'} engine)")
    elif a.cmd == "decompress":
        blob = open(a.infile, "rb").read()
        out = decompress_bytes(blob)
        open(a.outfile, "wb").write(out)
        print(f"{len(blob)} -> {len(out)} bytes")
    elif a.cmd == "verify":
        data = open(a.infile, "rb").read()
        ref = hashlib.sha256(data).hexdigest()
        ok = True
        runs = [("adaptive/auto", True, None),
                ("adaptive/afc1", True, "afc1"),
                ("adaptive/afc2", True, "afc2"),
                ("baseline", False, None)]
        for label, adaptive, fmt in runs:
            blob = compress_bytes(data, adaptive, fmt=fmt)
            good = hashlib.sha256(decompress_bytes(blob)).hexdigest() == ref
            ok &= good
            print(f"{label:14s} {len(data):>9} -> {len(blob):>9}  "
                  f"{'LOSSLESS OK' if good else 'ROUND-TRIP FAILED'}")
        sys.exit(0 if ok else 1)
    else:  # benchmark
        print("file,mode,orig_bytes,afc_bytes,comp_ms_median,dec_ms_median")
        for path in a.files:
            data = open(path, "rb").read()
            for label, adaptive in (("adaptive", True), ("baseline", False)):
                if a.baseline and not adaptive:
                    continue
                ct, dt = [], []
                blob = out = b""
                for _ in range(a.runs):
                    t0 = time.perf_counter()
                    blob = compress_bytes(data, adaptive)
                    ct.append((time.perf_counter() - t0) * 1000)
                    t0 = time.perf_counter()
                    out = decompress_bytes(blob)
                    dt.append((time.perf_counter() - t0) * 1000)
                if out != data:
                    raise RuntimeError(f"round-trip failed on {path}")
                ct.sort()
                dt.sort()
                print(f"{path},{label},{len(data)},{len(blob)},"
                      f"{ct[len(ct) // 2]:.2f},{dt[len(dt) // 2]:.2f}")


if __name__ == "__main__":
    _cli()
