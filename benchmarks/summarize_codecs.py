#!/usr/bin/env python3
"""Print a head-to-head table from a reference_codecs.py run.

    python benchmarks/summarize_codecs.py benchmarks/govdocs1_reference_codecs.csv
"""
import collections
import csv
import sys

CODECS = [("afc_bytes", "AFC"),
          ("gzip9_bytes_REFERENCE", "gzip -9"),
          ("bzip2_bytes_REFERENCE", "bzip2 -9"),
          ("lzma_bytes_REFERENCE", "lzma -9")]


def main(path):
    groups = collections.defaultdict(lambda: collections.defaultdict(int))
    counts = collections.Counter()
    wins = collections.Counter()

    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            corpus = row["corpus"]
            if not row["afc_bytes"]:
                continue
            counts[corpus] += 1
            groups[corpus]["orig"] += int(row["orig_bytes"])
            sizes = {}
            for key, label in CODECS:
                value = int(row[key])
                groups[corpus][label] += value
                sizes[label] = value
            wins[(corpus, min(sizes, key=sizes.get))] += 1

    labels = [label for _k, label in CODECS]
    head = "%-16s %6s %13s" % ("corpus", "files", "original")
    for label in labels:
        head += "%12s" % label
    print(head)
    print("-" * len(head))

    grand = collections.defaultdict(int)
    for corpus in sorted(groups):
        g = groups[corpus]
        line = "%-16s %6d %13s" % (corpus, counts[corpus], format(g["orig"], ","))
        for label in labels:
            line += "%11.2f%%" % ((1 - g[label] / g["orig"]) * 100)
        print(line)
        grand["orig"] += g["orig"]
        for label in labels:
            grand[label] += g[label]

    print("-" * len(head))
    line = "%-16s %6d %13s" % ("TOTAL", sum(counts.values()),
                               format(grand["orig"], ","))
    for label in labels:
        line += "%11.2f%%" % ((1 - grand[label] / grand["orig"]) * 100)
    print(line)

    print("\nsmallest output, per file:")
    for corpus in sorted(groups):
        parts = ["%s %d" % (label, wins[(corpus, label)])
                 for label in labels if wins[(corpus, label)]]
        print("  %-16s %s" % (corpus, "   ".join(parts)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python summarize_codecs.py <reference_codecs.csv>")
    main(sys.argv[1])
