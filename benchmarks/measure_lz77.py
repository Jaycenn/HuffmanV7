#!/usr/bin/env python3
"""Measure LZ77 + Hybrid Huffman against the engine alone, and against gzip.

    python benchmarks/measure_lz77.py --corpus repo=benchmarks/corpus
    python benchmarks/measure_lz77.py \\
        --corpus csv=benchmarks/govdocs1/csv \\
        --corpus log=benchmarks/govdocs1/log \\
        --max-bytes 2000000 --out benchmarks/lz77_experiment.csv

Four columns per file:

    AFC          the engine as it is today
    LZ77+AFC     LZ77 transform, then the same engine on the result
    gzip -9      the reference; LZ77 + Huffman, decades of tuning
    single-tier  order-0 Huffman, the baseline from Objective 4

Every LZ77 result is round-trip checked before it is counted, so a number
here is a number you can quote.

SPEED: the LZ77 pass is pure Python and runs at roughly 1-2 MB/s. Use
--max-bytes to cap file size; the default skips anything over 2 MB, which
keeps a run over the repo corpus to a couple of minutes.
"""
import argparse
import csv
import hashlib
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import afc2                                             # noqa: E402
from lz77_transform import lz77_encode, lz77_decode     # noqa: E402

try:
    from baseline_huffman import encoded_size as single_tier_size
except ImportError:                                     # optional column
    single_tier_size = None


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
    ap.add_argument("--preset", default="balanced")
    ap.add_argument("--max-bytes", type=int, default=2_000_000,
                    help="skip files larger than this (LZ77 here is slow)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    jobs = []
    for spec in args.corpus:
        name, _, path = spec.partition("=")
        files = walk(path)
        if not files:
            sys.stderr.write("no files under %s\n" % path)
            return 2
        jobs.append((name, files))

    rows = []
    started = time.time()
    print("%-26s %10s %10s %10s %10s %10s"
          % ("file", "original", "AFC", "LZ77+AFC", "gzip -9", "1-tier"))
    print("-" * 80)

    for corpus, files in jobs:
        for path in files:
            data = open(path, "rb").read()
            if len(data) < 1024 or len(data) > args.max_bytes:
                continue

            plain = afc2.compress_bytes(data)
            token = lz77_encode(data)
            if lz77_decode(token) != data:
                sys.exit("LZ77 round trip failed on " + path)
            combo = afc2.compress_bytes(token)
            gz = zlib.compress(data, 9)
            base = single_tier_size(data) if single_tier_size else 0

            rows.append((corpus, os.path.basename(path), len(data),
                         len(plain), len(combo), len(gz), base))
            print("%-26s %10d %10d %10d %10d %10d"
                  % (os.path.basename(path)[:25], len(data), len(plain),
                     len(combo), len(gz), base))

    if not rows:
        print("no files matched (try raising --max-bytes)")
        return 1

    o = sum(r[2] for r in rows)
    a = sum(r[3] for r in rows)
    c = sum(r[4] for r in rows)
    g = sum(r[5] for r in rows)
    b = sum(r[6] for r in rows)
    wins = sum(1 for r in rows if r[4] < r[3])

    print("-" * 80)
    print("%-26s %10d %10d %10d %10d %10d" % ("TOTAL", o, a, c, g, b))
    print()
    print("  saved:   AFC %.2f%%   LZ77+AFC %.2f%%   gzip %.2f%%%s"
          % ((1 - a / o) * 100, (1 - c / o) * 100, (1 - g / o) * 100,
             ("   1-tier %.2f%%" % ((1 - b / o) * 100)) if b else ""))
    print("  LZ77+AFC vs AFC alone:  %+.2f pt   (smaller on %d of %d files)"
          % ((1 - c / o) * 100 - (1 - a / o) * 100, wins, len(rows)))
    print("  LZ77+AFC vs gzip -9:    %+.2f pt"
          % ((1 - c / o) * 100 - (1 - g / o) * 100))
    print("  elapsed %.0fs" % (time.time() - started))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            w = csv.writer(handle, lineterminator="\n")
            w.writerow(["corpus", "file", "orig_bytes", "afc_bytes",
                        "lz77_afc_bytes", "gzip9_bytes", "single_tier_bytes"])
            w.writerows(rows)
        print("  wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
