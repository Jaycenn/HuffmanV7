#!/usr/bin/env python3
"""Baseline single-tier Huffman, for Objective 4.

This is the control the multi-level model is measured against: classic
byte-level Huffman with no n-gram tier, no word tier, no pattern dictionary
and no structural blocks. One frequency count over the 256 byte values, one
canonical tree, one code per symbol.

CONTAINER
---------
Sizes include the cost of the code table, because AFC's sizes include its
dictionary. Anything else would flatter this baseline. The layout is:

    8 bytes    original length
    256 bytes  canonical code length per byte value (0 = symbol unused)
    payload    the bit stream, padded to a byte boundary

WHY SIZES ARE COMPUTED, NOT ENCODED
-----------------------------------
For a canonical Huffman code the compressed length is fully determined by the
symbol frequencies and their code lengths: sum(freq[s] * bits[s]). That is an
identity, not an approximation, so `encoded_size` returns exactly what
`encode` would emit. Encoding 326 MB bit-by-bit in pure Python takes hours and
yields the same number. `verify()` asserts the two agree, and that encode /
decode round-trips, on every file it is given.
"""
import argparse
import collections
import csv
import heapq
import os
import sys
import time

HEADER_BYTES = 8 + 256
MAX_CODE_BITS = 32


def code_lengths(freq):
    """Canonical Huffman code lengths, limited to MAX_CODE_BITS."""
    symbols = [s for s in range(256) if freq[s]]
    if not symbols:
        return [0] * 256
    if len(symbols) == 1:
        lengths = [0] * 256
        lengths[symbols[0]] = 1          # one symbol still costs one bit
        return lengths

    work = dict(freq)
    while True:
        heap = [(work[s], s, s) for s in symbols]
        heapq.heapify(heap)
        depth = {s: 0 for s in symbols}
        members = {s: [s] for s in symbols}
        counter = 256
        while len(heap) > 1:
            w1, _t1, a = heapq.heappop(heap)
            w2, _t2, b = heapq.heappop(heap)
            merged = members.pop(a) + members.pop(b)
            for s in merged:
                depth[s] += 1
            members[counter] = merged
            heapq.heappush(heap, (w1 + w2, counter, counter))
            counter += 1
        if max(depth.values()) <= MAX_CODE_BITS:
            lengths = [0] * 256
            for s in symbols:
                lengths[s] = depth[s]
            return lengths
        # Flatten the distribution and retry; converges quickly and only ever
        # triggers on pathological inputs.
        work = {s: (work[s] + 1) // 2 + 1 for s in symbols}


def encoded_size(data):
    """Exact size the encoder below would produce, without encoding."""
    if not data:
        return HEADER_BYTES
    freq = collections.Counter(data)
    lengths = code_lengths(freq)
    bits = sum(freq[s] * lengths[s] for s in freq)
    return HEADER_BYTES + (bits + 7) // 8


def _canonical(lengths):
    """Map symbol -> (code, bits) in canonical order."""
    ordered = sorted((l, s) for s, l in enumerate(lengths) if l)
    codes = {}
    code = 0
    prev = ordered[0][0] if ordered else 0
    for length, symbol in ordered:
        code <<= (length - prev)
        codes[symbol] = (code, length)
        code += 1
        prev = length
    return codes


def encode(data):
    freq = collections.Counter(data)
    lengths = code_lengths(freq)
    out = bytearray(len(data).to_bytes(8, "big"))
    out += bytes(lengths)
    codes = _canonical(lengths)
    acc = 0
    nbits = 0
    for byte in data:
        code, length = codes[byte]
        acc = (acc << length) | code
        nbits += length
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def decode(blob):
    total = int.from_bytes(blob[:8], "big")
    if not total:
        return b""
    lengths = list(blob[8:HEADER_BYTES])
    lookup = {(c, l): s for s, (c, l) in _canonical(lengths).items()}
    out = bytearray()
    code = 0
    length = 0
    for byte in blob[HEADER_BYTES:]:
        for shift in range(7, -1, -1):
            code = (code << 1) | ((byte >> shift) & 1)
            length += 1
            hit = lookup.get((code, length))
            if hit is not None:
                out.append(hit)
                code = 0
                length = 0
                if len(out) == total:
                    return bytes(out)
    return bytes(out)


def verify(paths):
    """Prove encode/decode round-trips and that encoded_size is exact."""
    for path in paths:
        data = open(path, "rb").read()
        blob = encode(data)
        assert decode(blob) == data, "round trip failed on %s" % path
        assert len(blob) == encoded_size(data), "size mismatch on %s" % path
    print("verified %d files: round-trip exact, encoded_size exact"
          % len(paths))


def walk(path):
    if os.path.isfile(path):
        return [path]
    found = []
    for root, _dirs, names in os.walk(path):
        found.extend(os.path.join(root, n) for n in sorted(names))
    return sorted(found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", required=True,
                    metavar="NAME=PATH")
    ap.add_argument("--out", required=True)
    ap.add_argument("--verify-under", type=int, default=65536,
                    help="round-trip check every file smaller than this")
    args = ap.parse_args()

    jobs = []
    for spec in args.corpus:
        name, _, path = spec.partition("=")
        files = walk(path)
        if not files:
            sys.stderr.write("no files under %s\n" % path)
            return 2
        jobs.append((name, files))

    total = sum(len(f) for _n, f in jobs)
    done = 0
    checked = []
    started = time.time()

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["corpus", "file", "orig_bytes",
                         "single_tier_huffman_bytes"])
        for corpus, files in jobs:
            for path in files:
                data = open(path, "rb").read()
                size = encoded_size(data)
                if len(data) <= args.verify_under and len(checked) < 40:
                    blob = encode(data)
                    assert decode(blob) == data, "round trip failed: " + path
                    assert len(blob) == size, "size mismatch: " + path
                    checked.append(path)
                writer.writerow([corpus, os.path.basename(path), len(data), size])
                done += 1
                if done % 25 == 0 or done == total:
                    print("  %d/%d files  %.0fs elapsed"
                          % (done, total, time.time() - started), flush=True)

    print("round-trip verified on %d sampled files" % len(checked))
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
