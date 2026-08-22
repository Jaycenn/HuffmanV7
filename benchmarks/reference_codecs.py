#!/usr/bin/env python3
"""Measure the standard reference codecs on a corpus, for comparison only.

WHY THIS FILE MAY IMPORT COMPRESSION LIBRARIES
----------------------------------------------
gzip / bzip2 / lzma appear here as the *baselines being compared against*,
never in an AFC code path. This is a measurement script that reads a corpus
and writes a CSV; nothing in it is imported by the engine, the container
layer, or the web app. The structural no-codec guarantee is asserted against
afcpak.py and filetypes.py (test_archive_no_deflate, test_filetypes_imports_
no_codec), and this script is not in that set — exactly as the existing
benchmarks/reference_codecs.csv baseline was produced.

USAGE
-----
    python benchmarks/reference_codecs.py \\
        --corpus govdocs1-csv=benchmarks/govdocs1/csv \\
        --corpus govdocs1-log=benchmarks/govdocs1/log \\
        --corpus govdocs1-pdf=benchmarks/govdocs1/pdf \\
        --afc benchmarks/govdocs1_corpus.csv \\
        --out benchmarks/govdocs1_reference_codecs.csv

--afc is optional. When given, the AFC sizes are read from that file rather
than recompressed, so the comparison uses exactly the numbers already
published in your results and the run stays cheap.
"""
import argparse
import bz2
import csv
import lzma
import os
import sys
import time
import zlib

FIELDS = ["corpus", "file", "orig_bytes", "afc_bytes",
          "gzip9_bytes_REFERENCE", "bzip2_bytes_REFERENCE",
          "lzma_bytes_REFERENCE"]


def walk(path):
    if os.path.isfile(path):
        return [path]
    found = []
    for root, _dirs, names in os.walk(path):
        found.extend(os.path.join(root, n) for n in sorted(names))
    return sorted(found)


def afc_sizes(path, preset="balanced"):
    """Map (corpus, filename) -> compressed size from an external_corpus run."""
    sizes = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if preset and row.get("preset") not in (None, preset):
                continue
            sizes[(row["corpus"], row["file"])] = int(row["compressed_bytes"])
    return sizes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", required=True,
                    metavar="NAME=PATH")
    ap.add_argument("--afc", help="an external_corpus.py CSV to read AFC sizes from")
    ap.add_argument("--preset", default="balanced")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    known = afc_sizes(args.afc, args.preset) if args.afc else {}
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
    started = time.time()
    missing = 0

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        for corpus, files in jobs:
            for path in files:
                data = open(path, "rb").read()
                name = os.path.basename(path)
                afc = known.get((corpus, name), "")
                if args.afc and afc == "":
                    missing += 1
                writer.writerow([
                    corpus, name, len(data), afc,
                    len(zlib.compress(data, 9)),
                    len(bz2.compress(data, 9)),
                    len(lzma.compress(data, preset=9)),
                ])
                done += 1
                if done % 25 == 0 or done == total:
                    print("  %d/%d files  %.0fs elapsed"
                          % (done, total, time.time() - started), flush=True)

    if missing:
        print("note: %d files had no AFC row in %s (preset %s)"
              % (missing, args.afc, args.preset))
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
