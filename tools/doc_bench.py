#!/usr/bin/env python3
"""
tools/doc_bench.py — PDF / DOCX container-aware benchmark.

Produces the two tables the V7 brief asks for:

  §43  per file: original, V6 AFC (plain whole-file), V7 AFC (container-aware),
       old time, new time, backend, and the SHA-256 verification result;
  §41  per file: the component-level breakdown — how many bytes were pooled
       into the Hybrid-Huffman input versus preserved verbatim, and what those
       components were.

The headline number is always the FINAL AFC SIZE OF THE WHOLE FILE. Component
figures are shown to explain that number, never to replace it.

Every row is verified: the produced container is expanded and compared against
the original bytes, and the SHA-256 digests are compared. A file that fails is
printed as FAILED and makes the tool exit non-zero — failures are never
dropped from the table.

Usage:
    python tools/doc_bench.py                    # benchmarks/documents/*
    python tools/doc_bench.py FILE [FILE ...]
    python tools/doc_bench.py --runs 3 --csv benchmarks/v7_documents.csv
"""
import argparse
import glob
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402
import containers                                               # noqa: E402


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def timed(fn, runs):
    ts = []
    out = None
    for _ in range(runs):
        t0 = time.perf_counter()
        out = fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return out, median(ts)


def bench_one(path, runs):
    data = open(path, "rb").read()
    name = os.path.basename(path)

    plain, t_plain = timed(
        lambda: afc2.compress_bytes(data, True, fmt="auto",
                                    container_aware=False), runs)
    v7, t_v7 = timed(
        lambda: afc2.compress_bytes(data, True, fmt="auto"), runs)

    restored, t_dec = timed(lambda: afc2.decompress_bytes(v7), runs)
    sha_in = hashlib.sha256(data).hexdigest()
    sha_out = hashlib.sha256(restored).hexdigest()
    lossless = (restored == data) and (sha_in == sha_out)

    stats = containers.describe_plan(data) or {
        "segments": 0, "pooled_bytes": 0, "opaque_bytes": 0,
        "opaque_fraction": 0.0, "components": {}}

    # Object-level inventory (§14): how many components were analysed, how
    # many were routed to Hybrid-Huffman, how many were preserved verbatim.
    inventory = []
    if data[:5] == b"%PDF-":
        try:
            inventory = containers.pdf_components(data)
        except Exception:
            inventory = []
    segs = containers.plan(data) or []
    opaque_ranges = [(s.start, s.end) for s in segs
                     if s.kind == containers.OPAQUE]

    def _is_preserved(c):
        return any(a <= c["stream_start"] and c["stream_end"] <= b
                   for a, b in opaque_ranges)

    streams = [c for c in inventory if c["stream_start"] >= 0]
    preserved = [c for c in streams if _is_preserved(c)]
    compressed = [c for c in streams if not _is_preserved(c)]
    stats["components_analyzed"] = len(inventory)
    stats["streams_analyzed"] = len(streams)
    stats["streams_compressed"] = len(compressed)
    stats["streams_preserved"] = len(preserved)
    stats["compressed_stream_bytes"] = sum(c["length"] for c in compressed)
    stats["preserved_stream_bytes"] = sum(c["length"] for c in preserved)
    kinds = {}
    for c in streams:
        k = kinds.setdefault(c["kind"], {"n": 0, "bytes": 0, "preserved": 0})
        k["n"] += 1
        k["bytes"] += c["length"]
        if _is_preserved(c):
            k["preserved"] += 1
    stats["stream_kinds"] = kinds

    return {
        "file": name, "orig": len(data),
        "v6_bytes": len(plain), "v7_bytes": len(v7),
        "v6_ms": t_plain, "v7_ms": t_v7, "dec_ms": t_dec,
        "container": v7[:4].decode("ascii", "replace"),
        "backend": "C++ native" if afc2.NATIVE else "pure Python",
        "segments": stats["segments"],
        "pooled_bytes": stats["pooled_bytes"],
        "opaque_bytes": stats["opaque_bytes"],
        "opaque_pct": 100.0 * stats["opaque_fraction"],
        "components": stats["components"],
        "components_analyzed": stats.get("components_analyzed", 0),
        "streams_analyzed": stats.get("streams_analyzed", 0),
        "streams_compressed": stats.get("streams_compressed", 0),
        "streams_preserved": stats.get("streams_preserved", 0),
        "compressed_stream_bytes": stats.get("compressed_stream_bytes", 0),
        "preserved_stream_bytes": stats.get("preserved_stream_bytes", 0),
        "stream_kinds": stats.get("stream_kinds", {}),
        "sha_in": sha_in, "sha_out": sha_out, "lossless": lossless,
        "v7_saved_pct": 100.0 * (1 - len(v7) / len(data)) if data else 0.0,
        "v7_vs_v6_pct": (100.0 * (len(v7) - len(plain)) / len(plain)
                         if plain else 0.0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()

    files = a.files or sorted(glob.glob(
        os.path.join(ROOT, "benchmarks", "documents", "*")))
    if not files:
        print("No documents found. Run: python tools/make_doc_corpus.py")
        return 1

    print("backend: %s | container-aware: %s | runs: %d (median)\n"
          % ("C++ native" if afc2.NATIVE else "pure Python",
             afc2.CONTAINER_AWARE, a.runs))

    rows = [bench_one(p, a.runs) for p in files]

    # ---- §43 whole-file comparison ----------------------------------------
    print("=" * 108)
    print("WHOLE-FILE RESULT (the headline number)")
    print("=" * 108)
    print("%-28s %9s %10s %10s %8s %9s %9s %8s %-11s %s"
          % ("file", "original", "V6 AFC", "V7 AFC", "V7 vs V6",
             "V6 ms", "V7 ms", "saved%", "container", "SHA-256"))
    for r in rows:
        print("%-28s %9d %10d %10d %7.2f%% %9.1f %9.1f %7.2f%% %-11s %s"
              % (r["file"], r["orig"], r["v6_bytes"], r["v7_bytes"],
                 r["v7_vs_v6_pct"], r["v6_ms"], r["v7_ms"],
                 r["v7_saved_pct"], r["container"],
                 "MATCH" if r["lossless"] else "*** FAILED ***"))

    to = sum(r["orig"] for r in rows)
    t6 = sum(r["v6_bytes"] for r in rows)
    t7 = sum(r["v7_bytes"] for r in rows)
    print("-" * 108)
    print("%-28s %9d %10d %10d %7.2f%%" % ("TOTAL", to, t6, t7,
                                           100.0 * (t7 - t6) / t6 if t6 else 0))
    worse = [r for r in rows if r["v7_bytes"] > r["v6_bytes"]]
    better = [r for r in rows if r["v7_bytes"] < r["v6_bytes"]]
    print("V7 smaller on %d file(s), larger on %d, unchanged on %d."
          % (len(better), len(worse), len(rows) - len(better) - len(worse)))

    # ---- §41 component-level breakdown ------------------------------------
    print()
    print("=" * 108)
    print("COMPONENT BREAKDOWN (explains the number above; never replaces it)")
    print("=" * 108)
    print("%-28s %6s %11s %11s %8s  %s"
          % ("file", "segs", "pooled B", "opaque B", "opaque%", "components"))
    for r in rows:
        comps = ", ".join(
            "%s x%d (%d B)" % (k, v["count"], v["bytes"])
            for k, v in sorted(r["components"].items(),
                               key=lambda kv: -kv[1]["bytes"])[:4])
        print("%-28s %6d %11d %11d %7.1f%%  %s"
              % (r["file"], r["segments"], r["pooled_bytes"],
                 r["opaque_bytes"], r["opaque_pct"], comps or "-"))

    # ---- §14 object-level component inventory (PDF) -----------------------
    pdfs = [r for r in rows if r["components_analyzed"]]
    if pdfs:
        print()
        print("=" * 108)
        print("PDF OBJECT INVENTORY — components analysed / compressed / preserved")
        print("=" * 108)
        print("%-28s %7s %8s %10s %10s %12s %12s"
              % ("file", "objects", "streams", "compressed", "preserved",
                 "comp bytes", "presv bytes"))
        for r in pdfs:
            print("%-28s %7d %8d %10d %10d %12d %12d"
                  % (r["file"], r["components_analyzed"], r["streams_analyzed"],
                     r["streams_compressed"], r["streams_preserved"],
                     r["compressed_stream_bytes"], r["preserved_stream_bytes"]))
        print()
        print("%-28s %s" % ("file", "stream kinds (n, bytes, preserved)"))
        for r in pdfs:
            k = ", ".join("%s x%d/%dB/%dpres" % (name, v["n"], v["bytes"],
                                                 v["preserved"])
                          for name, v in sorted(r["stream_kinds"].items()))
            print("%-28s %s" % (r["file"], k or "-"))

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as f:
            cols = ["file", "orig", "v6_bytes", "v7_bytes", "v7_vs_v6_pct",
                    "v7_saved_pct", "v6_ms", "v7_ms", "dec_ms", "container",
                    "backend", "segments", "pooled_bytes", "opaque_bytes",
                    "opaque_pct", "components_analyzed", "streams_analyzed",
                    "streams_compressed", "streams_preserved",
                    "compressed_stream_bytes", "preserved_stream_bytes",
                    "sha_in", "sha_out", "lossless"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print("\nwrote %s" % a.csv)

    bad = [r for r in rows if not r["lossless"]]
    print()
    if bad:
        print("%d FILE(S) FAILED LOSSLESS VERIFICATION:" % len(bad))
        for r in bad:
            print("  %s  %s != %s" % (r["file"], r["sha_in"], r["sha_out"]))
        return 1
    print("All %d documents reconstructed byte-for-byte "
          "(SHA-256 verified)." % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
