#!/usr/bin/env python3
"""Size/lossless audit over corpora that are NOT redistributed with this
repository (Canterbury, Silesia, GovDocs1).

The corpora are large and separately licensed, so they are downloaded by the
operator and pointed at by path:

    python benchmarks/external_corpus.py \
        --corpus canterbury=/path/to/cantrbry \
        --corpus silesia=/path/to/silesia \
        --corpus govdocs1=/path/to/govdocs1/files \
        --out benchmarks/external_corpus.csv

Every row SHA-256-verifies the round trip. A file whose round trip is not
byte-exact is reported and makes the run exit non-zero; a size is never
published for output that is not provably lossless.

Rows are written and flushed as they complete, so a long run can be inspected
(or resumed from) while it is still going. Timings are machine-dependent;
the byte counts are not.
"""
import argparse
import csv
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402
import presets                                                  # noqa: E402

FIELDS = ["corpus", "file", "preset", "backend", "container",
          "original_bytes", "compressed_bytes", "ratio", "saved_percent",
          "byte_equal", "sha256_equal", "compression_ms", "decompression_ms"]


def walk(path):
    if os.path.isfile(path):
        return [path]
    out = []
    for base, _dirs, names in os.walk(path):
        for name in sorted(names):
            full = os.path.join(base, name)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                out.append(full)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", required=True,
                    metavar="NAME=PATH")
    ap.add_argument("--presets", default="fast,balanced,maximum")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="skip files larger than this (0 = no limit)")
    args = ap.parse_args()

    jobs = []
    for spec in args.corpus:
        name, _, path = spec.partition("=")
        files = walk(path)
        if not files:
            sys.stderr.write("no files under %s\n" % path)
            return 2
        jobs.append((name, files))

    presets_wanted = args.presets.split(",")
    total_files = sum(len(f) for _n, f in jobs)
    done = 0
    failures = []
    started = time.time()

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        for corpus, files in jobs:
            for path in files:
                size = os.path.getsize(path)
                if args.max_bytes and size > args.max_bytes:
                    done += 1
                    continue
                data = open(path, "rb").read()
                digest = hashlib.sha256(data).hexdigest()
                for preset in presets_wanted:
                    t0 = time.perf_counter()
                    blob, _used, backend = presets.compress_with(
                        data, preset, fmt="auto")
                    cms = (time.perf_counter() - t0) * 1000.0
                    t0 = time.perf_counter()
                    out = afc2.decompress_bytes(blob)
                    dms = (time.perf_counter() - t0) * 1000.0
                    byte_equal = out == data
                    sha_equal = hashlib.sha256(out).hexdigest() == digest
                    if not (byte_equal and sha_equal):
                        failures.append((corpus, path, preset))
                    writer.writerow([
                        corpus, os.path.basename(path), preset, backend,
                        blob[:4].decode("ascii", "replace"), len(data),
                        len(blob), round(len(data) / max(1, len(blob)), 6),
                        round(100.0 * (1.0 - len(blob) / max(1, len(data))), 4),
                        byte_equal, sha_equal, round(cms, 4), round(dms, 4)])
                    del out
                del data
                handle.flush()
                done += 1
                if done % 25 == 0 or done == total_files:
                    sys.stderr.write(
                        "  %d/%d files  %.0fs elapsed\n"
                        % (done, total_files, time.time() - started))
                    sys.stderr.flush()

    if failures:
        sys.stderr.write("ROUND TRIP FAILED on %d (file, preset):\n"
                         % len(failures))
        for corpus, path, preset in failures[:20]:
            sys.stderr.write("  %s %s %s\n" % (corpus, path, preset))
        return 1
    sys.stderr.write("all round trips byte-exact and SHA-256 verified\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
