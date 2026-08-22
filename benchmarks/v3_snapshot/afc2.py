#!/usr/bin/env python3
"""
afc2.py — AFC adaptive engine (v3 pipeline).

Pipeline (thesis terminology):
  Tier-1  byte-frequency scan
  Tier-2  n-gram scan (2..5 bytes, windowed)
  Tier-3  word-token scan
  Bit Cost Decision Engine:
      accept a pattern only if
      freq * (bits_spelled_out - bits_as_one_symbol) > 8 * (pattern_len + 3)
  greedy longest-match segmentation
  engine-gated structural block growth (adjacent-pair merging,
      blocks <= 128 bytes, dictionary cap 4096)
  strict final audit with actual counts
  canonical hybrid Huffman over mixed symbols (literals 0..255,
      patterns 256+i) -> AFC1 container, raw-storage fallback

Pure-Python throughout; afc_native.py kernels are used when available and
produce byte-identical output.
"""

import hashlib
import os
import sys
import time
from collections import Counter

import afc
from afc import (MODE_ADAPTIVE, est_code_len, huffman_lengths,
                 canonical_codes, pack_ids, emit_afc1, emit_raw)

try:
    import afc_native as _native
    NATIVE = _native.AVAILABLE and not os.environ.get("AFC_NO_NATIVE")
except Exception:
    _native = None
    NATIVE = False

# ---- v3 engine constants -------------------------------------------------
SCAN_WINDOW = 1 << 20        # Tier-2 n-gram scan window
NGRAM_MIN, NGRAM_MAX = 2, 5  # Tier-2 range
MIN_CANDIDATE_FREQ = 4       # Tier-2/Tier-3 admission floor
WORD_MIN, WORD_MAX = 3, 24   # Tier-3 token lengths
MAX_INITIAL_DICT = 3072      # room left below the 4096 cap for block growth
MAX_DICT = 4096              # structural dictionary cap
MAX_BLOCK = 128              # structural block growth byte cap
MERGE_ROUNDS = 3             # adjacent-pair merge rounds
MERGES_PER_ROUND = 32        # accepted merges per round

_WORD_CHARS = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


# --------------------------------------------------------------------------
# Tier scans
# --------------------------------------------------------------------------

def _tier2_ngrams(data: bytes) -> dict:
    """n-gram frequencies (lengths 2..5) over the scan window, freq >= 4."""
    win = data[:SCAN_WINDOW]
    if NATIVE:
        return _native.ngram_candidates(bytes(win), SCAN_WINDOW)
    out = {}
    n = len(win)
    for L in range(NGRAM_MIN, NGRAM_MAX + 1):
        if L > n:
            break
        cnt = Counter(bytes(win[i:i + L]) for i in range(n - L + 1))
        for pat, f in cnt.items():
            if f >= MIN_CANDIDATE_FREQ:
                out[pat] = f
    return out


def _tier3_words(data: bytes) -> dict:
    """Word tokens: maximal [A-Za-z0-9_] runs of length 3..24, freq >= 4."""
    import re
    cnt = Counter(m.group(0) for m in
                  re.finditer(rb"[0-9A-Z_a-z]{3,}", data)
                  if len(m.group(0)) <= WORD_MAX)
    return {w: f for w, f in cnt.items() if f >= MIN_CANDIDATE_FREQ}


# --------------------------------------------------------------------------
# Bit Cost Decision Engine
# --------------------------------------------------------------------------

def _bit_cost_gain(pat: bytes, freq: int, lit_bits, sym_bits: int) -> int:
    """Net bit gain per thesis rule; positive means the pattern pays for
    itself: freq*(bits_spelled_out - bits_as_one_symbol) - 8*(len+3)."""
    spelled = 0
    for b in pat:
        spelled += lit_bits[b]
    return freq * (spelled - sym_bits) - 8 * (len(pat) + 3)


def _select_candidates(data: bytes) -> list:
    total = len(data)
    tier1 = [0] * 256
    for b in data:
        tier1[b] += 1
    lit_bits = [est_code_len(f, total) for f in tier1]
    cands = _tier2_ngrams(data)
    for w, f in _tier3_words(data).items():
        if w not in cands:
            cands[w] = f
    scored = []
    for pat, f in cands.items():
        gain = _bit_cost_gain(pat, f, lit_bits, est_code_len(f, total))
        if gain > 0:
            scored.append((gain, pat))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [pat for _, pat in scored[:MAX_INITIAL_DICT]]


# --------------------------------------------------------------------------
# greedy longest-match segmentation
# --------------------------------------------------------------------------

def _segment_greedy(data: bytes, patterns: list) -> list:
    if NATIVE:
        return list(_native.segment_ids(bytes(data), patterns))
    pset = {p: i for i, p in enumerate(patterns)}
    by_first = {}
    for p in patterns:
        by_first.setdefault(p[0], set()).add(len(p))
    lens_of = {b: sorted(s, reverse=True) for b, s in by_first.items()}
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


# --------------------------------------------------------------------------
# engine-gated structural block growth (adjacent-pair merging)
# --------------------------------------------------------------------------

def _grow_blocks(ids: list, patterns: list, total_syms: int) -> tuple:
    """Merge frequent adjacent symbol pairs into larger structural blocks,
    each merge gated by the Bit Cost Decision Engine.  Returns (ids,
    patterns)."""
    def expand(sid):
        return bytes([sid]) if sid < 256 else patterns[sid - 256]

    for _ in range(MERGE_ROUNDS):
        if len(patterns) >= MAX_DICT:
            break
        total = len(ids)
        if total < 2:
            break
        pairs = Counter(zip(ids, ids[1:]))
        lit_counts = Counter(ids)
        lit_bits = [est_code_len(lit_counts.get(b, 0), total)
                    for b in range(256)]
        accepted = []
        for (a, b), f in pairs.items():
            if f < MIN_CANDIDATE_FREQ:
                continue
            merged = expand(a) + expand(b)
            if len(merged) > MAX_BLOCK:
                continue
            gain = _bit_cost_gain(merged, f, lit_bits,
                                  est_code_len(f, total))
            if gain > 0:
                accepted.append((gain, merged, a, b))
        if not accepted:
            break
        accepted.sort(key=lambda t: (-t[0], t[1]))
        room = MAX_DICT - len(patterns)
        chosen = {}
        pat_index = {p: i for i, p in enumerate(patterns)}
        for gain, merged, a, b in accepted[:MERGES_PER_ROUND]:
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


# --------------------------------------------------------------------------
# strict final audit with actual counts
# --------------------------------------------------------------------------

def _final_audit(ids: list, patterns: list) -> tuple:
    """Re-check every pattern against the Bit Cost Decision Engine using the
    ACTUAL post-segmentation counts; expand losers back to literals, drop
    unused entries, remap ids densely."""
    counts = Counter(ids)
    total = len(ids)
    lit_bits = [est_code_len(counts.get(b, 0), total) for b in range(256)]
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


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def compress_bytes(data: bytes, adaptive: bool = True,
                   fmt: str = "afc1") -> bytes:
    if not adaptive:
        return afc.compress_bytes(data, False, fmt=fmt)
    if len(data) == 0:
        return emit_raw(data)
    patterns = _select_candidates(data)
    ids = _segment_greedy(data, patterns)
    ids, patterns = _grow_blocks(ids, patterns, len(ids))
    ids, patterns = _final_audit(ids, patterns)
    lengths = huffman_lengths(Counter(ids))
    codes = canonical_codes(lengths)
    if NATIVE and max(L for _, L in codes.values()) <= 32:
        from array import array
        bitstream = _native.pack_bits(array("I", ids), codes,
                                      max(codes) if codes else 0)
    else:
        bitstream = pack_ids(ids, codes)
    blob = emit_afc1(MODE_ADAPTIVE, len(data), patterns, lengths, bitstream)
    raw = emit_raw(data)
    return raw if len(raw) <= len(blob) else blob


def decompress_bytes(blob: bytes) -> bytes:
    if NATIVE and blob[:4] == afc.MAGIC1:
        return _native.decompress(bytes(blob))
    return afc.decompress_bytes(blob)


# --------------------------------------------------------------------------
# CLI: compress / decompress / verify / benchmark
# --------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="AFC adaptive engine (v3)")
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
            s.add_argument("--format", choices=("afc1",), default="afc1")
    a = ap.parse_args()

    if a.cmd == "compress":
        data = open(a.infile, "rb").read()
        blob = compress_bytes(data, not a.baseline, fmt=a.format)
        open(a.outfile, "wb").write(blob)
        print(f"{len(data)} -> {len(blob)} bytes "
              f"({100.0 * (1 - len(blob) / max(1, len(data))):.2f}% saved)")
    elif a.cmd == "decompress":
        blob = open(a.infile, "rb").read()
        out = decompress_bytes(blob)
        open(a.outfile, "wb").write(out)
        print(f"{len(blob)} -> {len(out)} bytes")
    elif a.cmd == "verify":
        data = open(a.infile, "rb").read()
        ref = hashlib.sha256(data).hexdigest()
        ok = True
        for label, adaptive in (("adaptive", True), ("baseline", False)):
            blob = compress_bytes(data, adaptive)
            good = hashlib.sha256(decompress_bytes(blob)).hexdigest() == ref
            ok &= good
            print(f"{label:9s} {len(data):>9} -> {len(blob):>9}  "
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
                blob = b""
                for _ in range(a.runs):
                    t0 = time.perf_counter()
                    blob = compress_bytes(data, adaptive)
                    ct.append((time.perf_counter() - t0) * 1000)
                    t0 = time.perf_counter()
                    out = decompress_bytes(blob)
                    dt.append((time.perf_counter() - t0) * 1000)
                assert out == data, f"round-trip failed on {path}"
                ct.sort()
                dt.sort()
                print(f"{path},{label},{len(data)},{len(blob)},"
                      f"{ct[len(ct) // 2]:.2f},{dt[len(dt) // 2]:.2f}")


if __name__ == "__main__":
    _cli()
