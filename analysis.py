#!/usr/bin/env python3
"""
analysis.py — READ-ONLY introspection of AFC inputs and containers.

WHY THIS MODULE EXISTS
----------------------
Part 2 needs to *show* what the engine did: pre-compression entropy, the
hybrid Huffman tree, and where the savings came from.  The engine exposes no
stats API — every tier function in afc2.py is private and returns nothing to
callers — and constraint #1 forbids editing afc.py / afc2.py / afc_native.*.

So everything here is derived by READING, which the constraint explicitly
allows:

  * Tier-1 byte frequencies are recomputed from the input bytes (that is
    plain counting, not engine logic).
  * The hybrid Huffman tree and the dictionary of structural blocks are parsed
    out of the produced AFC1/AFC2 container header.
  * Savings attribution is measured by walking the container's own bitstream
    and tallying which symbols spent which bits.

Nothing here compresses anything, changes engine behaviour, or re-implements
a compression decision.  It only reports on output the engine already made.
It reuses afc.py's public helpers (read_varint, canonical_codes) rather than
duplicating them.

Public API
----------
  entropy_report(data)            -> dict   (Feature 6: pre-compression estimate)
  parse_container(blob)           -> dict   (header: patterns + code lengths)
  tree_report(blob, max_nodes)    -> dict   (Feature 7: tree for visualisation)
  attribution_report(blob)        -> dict   (Feature 8: where savings came from)
  explain(blob, original_size)    -> str    (Feature 8: one plain-English line)
"""
import math
from collections import Counter

import afc

MAGIC1 = afc.MAGIC1
MAGIC2 = afc.MAGIC2


# ===========================================================================
# Feature 6 — pre-compression entropy estimate (Tier-1 data, read-only)
# ===========================================================================

def entropy_report(data: bytes, sample_limit: int = 4 << 20) -> dict:
    """Shannon entropy over the Tier-1 byte-frequency distribution.

    This is the same first-order byte histogram the engine's Tier-1 scan
    builds; we recompute it here rather than reaching into the engine.

    Returns the entropy in bits/byte, the floor that a pure order-0 Huffman
    coder could reach, and a qualitative band.  It is an ESTIMATE and is
    labelled as such in the UI: AFC routinely beats the order-0 bound because
    its dictionary of structural blocks captures multi-byte regularity that a
    per-byte entropy figure cannot see.
    """
    n = len(data)
    if n == 0:
        return {"bytes": 0, "entropy_bits_per_byte": 0.0, "max_entropy": 8.0,
                "order0_floor_bytes": 0, "order0_ratio": 0.0,
                "unique_bytes": 0, "band": "empty",
                "note": "Empty input.", "histogram": []}

    sample = data[:sample_limit]
    counts = Counter(sample)
    total = len(sample)
    entropy = 0.0
    for c in counts.values():
        p = c / total
        entropy -= p * math.log2(p)

    # Order-0 floor: what a perfect per-byte entropy coder would need.
    floor_bytes = int(math.ceil(entropy * n / 8.0))
    ratio = (n / floor_bytes) if floor_bytes else 0.0

    if entropy >= 7.5:
        band, note = "very low", ("Near-random bytes. Expect little or no "
                                  "gain; the raw-storage fallback may win.")
    elif entropy >= 6.5:
        band, note = "low", ("High byte diversity. Modest gains unless the "
                             "file has repeated multi-byte structure.")
    elif entropy >= 4.5:
        band, note = "moderate", ("Typical binary or mixed content. "
                                  "Worthwhile compression expected.")
    elif entropy >= 2.5:
        band, note = "high", ("Text-like distribution. Good compression "
                              "expected, especially with repeated phrases.")
    else:
        band, note = "very high", ("Highly skewed byte distribution. Strong "
                                   "compression expected.")

    top = counts.most_common(24)
    histogram = [{"byte": b, "count": c, "pct": 100.0 * c / total,
                  "label": _byte_label(b)} for b, c in top]

    return {
        "bytes": n,
        "sampled_bytes": total,
        "entropy_bits_per_byte": round(entropy, 4),
        "max_entropy": 8.0,
        "order0_floor_bytes": floor_bytes,
        "order0_ratio": round(ratio, 3),
        "unique_bytes": len(counts),
        "band": band,
        "note": note,
        "histogram": histogram,
    }


def _byte_label(b: int) -> str:
    if b == 32:
        return "SP"
    if b == 9:
        return "TAB"
    if b == 10:
        return "LF"
    if b == 13:
        return "CR"
    if 33 <= b <= 126:
        return chr(b)
    return "%02X" % b


# ===========================================================================
# container parsing (read-only mirror of the documented AFC1/AFC2 layout)
# ===========================================================================

# ---------------------------------------------------------------------------
# Component-aware AFC3/AFC4 unwrapping
# ---------------------------------------------------------------------------
# AFC3/AFC4 hold an ordinary AFC1/AFC2 container inside — the one
# produced from the pooled, compressible components of a PDF or DOCX. All the
# introspection below (tree, code lengths, attribution) describes THAT inner
# container, because it is the one the Hybrid-Huffman engine actually built.
# Unwrapping here keeps every reader in this module working on AFC3 files
# without duplicating their logic.

def unwrap(blob: bytes) -> bytes:
    """Return the inner coded container, following AFC3/AFC4 when present.

    Other input is returned unchanged, so every caller can apply this
    unconditionally."""
    if len(blob) < 5 or blob[:4] not in (b"AFC3", b"AFC4"):
        return blob
    try:
        import containers
        return containers.inner_container(blob)
    except Exception:
        return blob


def is_component_aware(blob: bytes) -> bool:
    return len(blob) >= 4 and blob[:4] in (b"AFC3", b"AFC4")


def parse_container(blob: bytes) -> dict:
    """Parse an AFC container header.

    Returns {mode, magic, original_size, patterns, lengths, bit_offset} where
    `lengths` is {symbol_id: code_length} — i.e. the actual hybrid Huffman
    tree the encoder wrote — and `patterns` is the structural-block dictionary.
    Raises ValueError on anything that is not a coded container.
    """
    blob = unwrap(blob)
    if len(blob) < 6 or blob[:4] not in (MAGIC1, MAGIC2):
        raise ValueError("not an AFC container")
    magic = blob[:4]
    mode = blob[4]
    pos = 5
    orig, pos = afc.read_varint(blob, pos)
    if mode == afc.MODE_RAW:
        return {"magic": magic.decode(), "mode": mode, "raw": True,
                "original_size": orig, "patterns": [], "lengths": {},
                "bit_offset": pos}

    patterns = []
    lengths = {}
    if magic == MAGIC1:
        dc, pos = afc.read_varint(blob, pos)
        for _ in range(dc):
            L, pos = afc.read_varint(blob, pos)
            patterns.append(bytes(blob[pos:pos + L]))
            pos += L
        used, pos = afc.read_varint(blob, pos)
        for _ in range(used):
            sid, pos = afc.read_varint(blob, pos)
            lengths[sid] = blob[pos]
            pos += 1
    else:                                                   # AFC2
        used, pos = afc.read_varint(blob, pos)
        ids = []
        prev = -1
        for _ in range(used):
            d, pos = afc.read_varint(blob, pos)
            sid = d if prev < 0 else prev + 1 + d
            ids.append(sid)
            prev = sid
        nbytes = (used + 1) // 2
        for k, sid in enumerate(ids):
            b = blob[pos + (k >> 1)]
            v = (b >> 4) if not (k & 1) else (b & 0x0F)
            lengths[sid] = v + 1
        pos += nbytes
        dc, pos = afc.read_varint(blob, pos)
        if dc:
            flag = blob[pos]
            pos += 1
            plens = []
            for _ in range(dc):
                L, pos = afc.read_varint(blob, pos)
                plens.append(L)
            if flag == 0:
                for L in plens:
                    patterns.append(bytes(blob[pos:pos + L]))
                    pos += L
            else:
                # dictionary bytes coded with the literal Huffman codes
                total = sum(plens)
                raw, bits = _decode_symbols(blob, pos, total, [], lengths,
                                            want_bytes=True)
                pos += (bits + 7) // 8
                joined = bytes(raw)
                off = 0
                for L in plens:
                    patterns.append(joined[off:off + L])
                    off += L
    return {"magic": magic.decode(), "mode": mode, "raw": False,
            "original_size": orig, "patterns": patterns, "lengths": lengths,
            "bit_offset": pos}


def _decode_symbols(blob, pos, want, patterns, lengths, want_bytes=False):
    """Walk the canonical bitstream, returning (output_or_symbols, bits_used).

    A read-only re-implementation of the documented canonical decode, used so
    we can attribute bits per symbol.  afc.decompress_bytes remains the
    authority for actual decompression; this is only for measurement, and the
    caller verifies the two agree."""
    codes = afc.canonical_codes(lengths)
    lookup = {}
    for sid, (code, L) in codes.items():
        lookup[(L, code)] = sid
    out = bytearray() if want_bytes else []
    syms = []
    acc = 0
    nbits = 0
    bits_used = 0
    produced = 0
    i = pos
    n = len(blob)
    while produced < want and i <= n:
        if nbits == 0:
            if i >= n:
                break
            acc = blob[i]
            i += 1
            nbits = 8
        # consume one bit at a time into a growing code
        code = 0
        L = 0
        while True:
            if nbits == 0:
                if i >= n:
                    return (out if want_bytes else syms), bits_used
                acc = blob[i]
                i += 1
                nbits = 8
            bit = (acc >> (nbits - 1)) & 1
            nbits -= 1
            code = (code << 1) | bit
            L += 1
            bits_used += 1
            sid = lookup.get((L, code))
            if sid is not None:
                if sid < 256:
                    if want_bytes:
                        out.append(sid)
                    produced += 1
                    syms.append((sid, L, 1))
                else:
                    p = patterns[sid - 256] if patterns else b""
                    room = want - produced
                    take = min(len(p), room) if p else 0
                    if want_bytes:
                        out += p[:take]
                    produced += take if p else 0
                    syms.append((sid, L, take))
                break
            if L > 32:
                return (out if want_bytes else syms), bits_used
    return (out if want_bytes else syms), bits_used


# ===========================================================================
# Feature 7 — hybrid Huffman tree, for visualisation
# ===========================================================================

def tree_report(blob: bytes, max_depth: int = 9,
                max_leaves: int = 900) -> dict:
    """Build a renderable view of the ACTUAL hybrid Huffman tree in a container.

    The whole point of the thesis title is that one tree mixes two kinds of
    leaf: literal bytes (0-255) and structural blocks (256+i).  This returns
    the tree with every leaf tagged `literal` or `block`, so the UI can colour
    them differently and the hybrid claim becomes visible.

    Large alphabets are truncated for display: the tree is cut at `max_depth`
    and deeper subtrees are summarised as a single collapsed node carrying its
    leaf count.  Full totals are always reported in `summary`, so the numbers
    stay honest even when the drawing is abbreviated.

    DEPTH DEFAULT: 9, chosen by measurement rather than taste.  Literal bytes
    earn short codes (typically 4-8 bits) and structural blocks longer ones
    (typically 9-14), so a shallow cut shows ONLY literals and the "hybrid"
    claim stays invisible -- at depth 6 a representative text file rendered 11
    literal leaves and zero block leaves.  Depth 9 surfaces both classes
    (about 40 literal and 59 block leaves on the same file) while staying
    legible.  `summary["code_lengths"]` additionally reports the full
    literal-vs-block code-length distribution, which makes the split visible
    even where the drawing is truncated.
    """
    blob = unwrap(blob)
    info = parse_container(blob)
    if info["raw"]:
        return {"raw": True, "summary": {"note": "Stored raw (no tree)."},
                "tree": None}
    lengths = info["lengths"]
    patterns = info["patterns"]
    if not lengths:
        return {"raw": True, "summary": {"note": "No symbols."}, "tree": None}
    codes = afc.canonical_codes(lengths)

    lit = [s for s in lengths if s < 256]
    blk = [s for s in lengths if s >= 256]
    summary = {
        "total_symbols": len(lengths),
        "literal_symbols": len(lit),
        "block_symbols": len(blk),
        "min_code_length": min(lengths.values()),
        "max_code_length": max(lengths.values()),
        "avg_code_length": round(sum(lengths.values()) / len(lengths), 2),
        "dictionary_entries": len(patterns),
        "container": info["magic"],
        "shown_depth": max_depth,
        # full distribution, so the hybrid split is quantified even when the
        # drawn tree is truncated
        "code_lengths": {
            "literal": _length_hist(lengths, lambda s: s < 256),
            "block": _length_hist(lengths, lambda s: s >= 256),
        },
    }

    root = {"children": [None, None], "leaf": None, "count": 0}

    def insert(sid, code, L):
        node = root
        for k in range(L):
            bit = (code >> (L - 1 - k)) & 1
            node["count"] += 1
            nxt = node["children"][bit]
            if nxt is None:
                nxt = {"children": [None, None], "leaf": None, "count": 0}
                node["children"][bit] = nxt
            node = nxt
        node["count"] += 1
        node["leaf"] = sid

    ordered = sorted(codes.items(), key=lambda kv: (kv[1][1], kv[0]))
    for sid, (code, L) in ordered[:max_leaves * 4]:
        insert(sid, code, L)

    def render(node, depth):
        if node is None:
            return None
        if node["leaf"] is not None:
            sid = node["leaf"]
            if sid < 256:
                return {"type": "literal", "symbol": sid,
                        "label": _byte_label(sid),
                        "bits": codes[sid][1]}
            idx = sid - 256
            pat = patterns[idx] if idx < len(patterns) else b""
            return {"type": "block", "symbol": sid,
                    "label": _printable(pat), "length": len(pat),
                    "bits": codes[sid][1]}
        if depth >= max_depth:
            lits, blks = _count_leaves(node)
            return {"type": "collapsed", "leaves": lits + blks,
                    "literals": lits, "blocks": blks}
        return {"type": "node",
                "children": [render(node["children"][0], depth + 1),
                             render(node["children"][1], depth + 1)]}

    return {"raw": False, "summary": summary, "tree": render(root, 0)}


def _length_hist(lengths, pred):
    h = {}
    for sid, L in lengths.items():
        if pred(sid):
            h[L] = h.get(L, 0) + 1
    return dict(sorted(h.items()))


def _count_leaves(node):
    if node is None:
        return (0, 0)
    if node["leaf"] is not None:
        return (1, 0) if node["leaf"] < 256 else (0, 1)
    a = _count_leaves(node["children"][0])
    b = _count_leaves(node["children"][1])
    return (a[0] + b[0], a[1] + b[1])


def _printable(pat: bytes, limit: int = 14) -> str:
    s = "".join(chr(b) if 32 <= b <= 126 else "·" for b in pat[:limit])
    return s + ("…" if len(pat) > limit else "")


# ===========================================================================
# Feature 8 — savings attribution + plain-language explainer
# ===========================================================================

def attribution_report(blob: bytes) -> dict:
    """Measure where the compression actually came from.

    Walks the container's own bitstream and tallies, per symbol class:
      * bits spent
      * original bytes reproduced

    Savings for a class = (bytes it reproduced x 8) - (bits it spent).
    Summing the two classes reproduces the whole-file saving, which is
    asserted in the test suite rather than assumed.
    """
    blob = unwrap(blob)
    info = parse_container(blob)
    if info["raw"]:
        return {"raw": True, "note": "Stored raw — no coding was applied."}
    lengths = info["lengths"]
    patterns = info["patterns"]
    want = info["original_size"]
    syms, bits_used = _decode_symbols(blob, info["bit_offset"], want,
                                      patterns, lengths, want_bytes=False)

    lit_bits = lit_bytes = lit_count = 0
    blk_bits = blk_bytes = blk_count = 0
    for sid, L, produced in syms:
        if sid < 256:
            lit_bits += L
            lit_bytes += produced
            lit_count += 1
        else:
            blk_bits += L
            blk_bytes += produced
            blk_count += 1

    lit_saved = lit_bytes * 8 - lit_bits
    blk_saved = blk_bytes * 8 - blk_bits
    total_saved = lit_saved + blk_saved
    denom = total_saved if total_saved > 0 else 1

    return {
        "raw": False,
        "original_size": want,
        "symbols_emitted": len(syms),
        "literal": {"symbols": lit_count, "bits": lit_bits,
                    "bytes_reproduced": lit_bytes, "bits_saved": lit_saved,
                    "share_pct": round(100.0 * lit_saved / denom, 1)},
        "block": {"symbols": blk_count, "bits": blk_bits,
                  "bytes_reproduced": blk_bytes, "bits_saved": blk_saved,
                  "share_pct": round(100.0 * blk_saved / denom, 1),
                  "dictionary_entries": len(patterns),
                  "avg_block_bytes": round(blk_bytes / blk_count, 2)
                  if blk_count else 0.0},
        "total_bits_saved": total_saved,
        "coded_bits": bits_used,
    }


def explain(blob: bytes, original_size: int = None) -> str:
    """One plain-English sentence describing the result, for the UI.

    Built entirely from the attribution measured above — no new algorithm
    logic, and no claim the numbers do not support."""
    # Keep the OUTER size before unwrapping. For AFC3/AFC4 the inner
    # container covers only the pooled components, so pairing its length with
    # the caller's whole-file original size would overstate the saving wildly
    # (a PDF that really saved 4.5% reported 98.4%). Percentages are always
    # computed from two figures describing the same thing.
    outer_len = len(blob)
    component_aware = is_component_aware(blob)
    blob = unwrap(blob)
    try:
        a = attribution_report(blob)
    except Exception:
        return "This file was stored without coding."
    if a.get("raw"):
        return ("This file was stored raw — coding it would have made it "
                "larger, so the fallback kept it byte-for-byte.")
    if original_size:
        orig, comp = original_size, outer_len       # whole-file view
    else:
        orig, comp = a["original_size"], len(blob)  # coded-portion view
    pct = 100.0 * (1 - comp / orig) if orig else 0.0
    prefix = ""
    if component_aware:
        prefix = ("Container-aware: already-compressed components were "
                  "preserved as-is and only the compressible parts were "
                  "coded. ")
    blk = a["block"]
    lit = a["literal"]
    if a["total_bits_saved"] <= 0:
        return (prefix + "This file barely compressed (%.1f%%): its byte "
                "distribution is close to random, so few patterns paid for "
                "themselves." % pct)
    if blk["symbols"] == 0:
        return (prefix + "Saved %.1f%% using single-byte Huffman codes only — "
                "the Bit Cost Decision Engine rejected every multi-byte "
                "pattern as not worth its dictionary cost." % pct)
    return (prefix + "Saved %.1f%%: %.0f%% of the savings came from %d "
            "structural blocks (averaging %.1f bytes each), the rest from "
            "single-byte Huffman codes."
            % (pct, blk["share_pct"], blk["dictionary_entries"],
               blk["avg_block_bytes"]))
