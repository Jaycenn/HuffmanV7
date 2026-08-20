#!/usr/bin/env python3
"""Reproducible v9 size/speed audit over the corpus stored IN THIS REPOSITORY.

The AFC 1.3 Canterbury/Silesia summaries are a one-trial-per-file audit run on
the full official corpora, which are not redistributed here (see .gitignore).
This harness measures only files the repository actually carries, so anyone
can re-run it and get the same table:

    python benchmarks/v9_repo_corpus.py > benchmarks/v9_repo_corpus.csv

Every row SHA-256-verifies the round trip; a mismatch aborts with a non-zero
exit code rather than reporting a size for output that is not lossless.
Timing is the median of --runs runs and is machine-dependent; the byte counts
are not.
"""
import argparse
import csv
import glob
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402
import presets                                                  # noqa: E402

GROUPS = (
    ("canterbury", "canterbury/*"),
    ("corpus", "corpus/*"),
    ("documents", "documents/*"),
    ("large", "large/*"),
)


def collect():
    out = []
    for group, pattern in GROUPS:
        for path in sorted(glob.glob(os.path.join(HERE, pattern))):
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                out.append((group, path))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--presets", default="fast,balanced,maximum")
    args = ap.parse_args()

    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["group", "file", "preset", "backend", "container",
                     "original_bytes", "compressed_bytes", "ratio",
                     "saved_percent", "byte_equal", "sha256_equal",
                     "median_compression_ms", "median_decompression_ms"])
    failed = 0
    for group, path in collect():
        data = open(path, "rb").read()
        digest = hashlib.sha256(data).hexdigest()
        for preset in args.presets.split(","):
            ct, dt, blob, out = [], [], b"", b""
            for _ in range(args.runs):
                t0 = time.perf_counter()
                blob, _used, backend = presets.compress_with(data, preset,
                                                             fmt="auto")
                ct.append((time.perf_counter() - t0) * 1000.0)
                t0 = time.perf_counter()
                out = afc2.decompress_bytes(blob)
                dt.append((time.perf_counter() - t0) * 1000.0)
            byte_equal = out == data
            sha_equal = hashlib.sha256(out).hexdigest() == digest
            if not (byte_equal and sha_equal):
                failed += 1
            ct.sort()
            dt.sort()
            writer.writerow([
                group, os.path.basename(path), preset, backend,
                blob[:4].decode("ascii", "replace"), len(data), len(blob),
                round(len(data) / max(1, len(blob)), 6),
                round(100.0 * (1.0 - len(blob) / max(1, len(data))), 4),
                byte_equal, sha_equal,
                round(ct[len(ct) // 2], 4), round(dt[len(dt) // 2], 4)])
    if failed:
        sys.stderr.write("%d round trip(s) FAILED\n" % failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
