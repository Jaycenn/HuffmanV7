#!/usr/bin/env python3
"""One table: the baseline, this engine, and the reference codecs.

    python benchmarks/compare_all.py \\
        benchmarks/govdocs1_reference_codecs.csv \\
        benchmarks/govdocs1_baseline_huffman.csv

The first file supplies AFC and the gzip/bzip2/lzma baselines; the second
supplies single-tier Huffman. Files missing from either are skipped, and the
count of skipped rows is reported rather than silently absorbed.
"""
import collections
import csv
import sys

COLUMNS = [("base", "1-tier Huffman"), ("afc", "AFC"), ("gzip", "gzip -9"),
           ("bzip2", "bzip2 -9"), ("lzma", "lzma -9")]


def main(ref_path, base_path):
    base = {}
    for row in csv.DictReader(open(base_path, newline="", encoding="utf-8")):
        base[(row["corpus"], row["file"])] = int(row["single_tier_huffman_bytes"])

    groups = collections.defaultdict(lambda: collections.defaultdict(int))
    counts = collections.Counter()
    skipped = 0

    for row in csv.DictReader(open(ref_path, newline="", encoding="utf-8")):
        key = (row["corpus"], row["file"])
        if not row["afc_bytes"] or key not in base:
            skipped += 1
            continue
        corpus = row["corpus"]
        counts[corpus] += 1
        g = groups[corpus]
        g["orig"] += int(row["orig_bytes"])
        g["base"] += base[key]
        g["afc"] += int(row["afc_bytes"])
        g["gzip"] += int(row["gzip9_bytes_REFERENCE"])
        g["bzip2"] += int(row["bzip2_bytes_REFERENCE"])
        g["lzma"] += int(row["lzma_bytes_REFERENCE"])

    head = "%-16s %6s %14s" % ("corpus", "files", "original")
    for _k, label in COLUMNS:
        head += "%16s" % label
    print(head)
    print("-" * len(head))

    grand = collections.defaultdict(int)
    for corpus in sorted(groups):
        g = groups[corpus]
        line = "%-16s %6d %14s" % (corpus, counts[corpus], format(g["orig"], ","))
        for key, _label in COLUMNS:
            line += "%15.2f%%" % ((1 - g[key] / g["orig"]) * 100)
        print(line)
        for key in ("orig", "base", "afc", "gzip", "bzip2", "lzma"):
            grand[key] += g[key]

    print("-" * len(head))
    line = "%-16s %6d %14s" % ("TOTAL", sum(counts.values()),
                               format(grand["orig"], ","))
    for key, _label in COLUMNS:
        line += "%15.2f%%" % ((1 - grand[key] / grand["orig"]) * 100)
    print(line)

    print("\nObjective 4 — improvement over the single-tier baseline:")
    for corpus in sorted(groups):
        g = groups[corpus]
        print("  %-16s AFC output is %6.2f%% smaller than single-tier Huffman"
              % (corpus, (1 - g["afc"] / g["base"]) * 100))
    print("  %-16s AFC output is %6.2f%% smaller than single-tier Huffman"
          % ("OVERALL", (1 - grand["afc"] / grand["base"]) * 100))

    if skipped:
        print("\nskipped %d rows with no matching baseline or AFC size" % skipped)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: python compare_all.py <reference_codecs.csv> "
            "<baseline_huffman.csv>")
    main(sys.argv[1], sys.argv[2])
