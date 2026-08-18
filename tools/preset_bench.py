#!/usr/bin/env python3
"""
tools/preset_bench.py — the 6-way preset x backend benchmark.

Measures, for every file given:

    Python Fast / Balanced / Maximum
    C++    Fast / Balanced / Maximum

reporting compressed size, ratio, % reduction, median compress and decompress
time, peak RSS (measured in a forked child so native C++ allocations count)
and the backend actually used. Every row is verified with a
SHA-256 round trip; a row that fails verification is reported as FAILED rather
than dropped.

Usage:
    python tools/preset_bench.py                       # default corpus
    python tools/preset_bench.py FILE [FILE ...]
    python tools/preset_bench.py --runs 5 --csv out.csv
    python tools/preset_bench.py --native-only benchmarks/large/dickens_surrogate.txt
"""
import argparse
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402
import presets                                                  # noqa: E402
import experiment                                               # noqa: E402

DEFAULT_FILES = [
    "benchmarks/corpus/data.json",
    "benchmarks/corpus/prose_en.txt",
    "benchmarks/corpus/server.log",
    "benchmarks/corpus/data.csv",
    "benchmarks/corpus/code_python.py.txt",
    "benchmarks/corpus/records.bin",
    "benchmarks/corpus/random.bin",
    "benchmarks/canterbury/alice29.txt",
    "benchmarks/canterbury/cp.html",
    "benchmarks/canterbury/fields.c",
]


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def measure(path, data, preset, force_python, runs):
    """Compress+decompress `runs` times under one preset/backend combination."""
    ct, dt = [], []
    blob = b""
    ok = True
    engine = "python" if force_python else "native"
    options = presets.options_for(preset)
    source_sha = hashlib.sha256(data).digest()
    for _ in range(runs):
        t0 = time.perf_counter()
        blob = afc2.compress_bytes(data, True, fmt="auto", options=options,
                                   backend=engine)
        ct.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        out = afc2.decompress_bytes(blob)
        dt.append((time.perf_counter() - t0) * 1000)
        if out != data or hashlib.sha256(out).digest() != source_sha:
            ok = False
    peak = _peak_rss_kb(path, preset, force_python)
    backend = "pure Python" if force_python else "C++ native"
    return {"bytes": len(blob), "comp_ms": median(ct), "dec_ms": median(dt),
            "peak_kb": peak, "ok": ok, "backend": backend,
            "container": blob[:4].decode("ascii", "replace")}


def _peak_rss_kb(path, preset, force_python):
    """Peak additional OS RSS caused by one fresh-process compression."""
    backend = "python" if force_python else "native"
    row = experiment.run_child(os.path.abspath(path), preset, backend, 0, 3600)
    return float(row.get("peak_delta_kib", -1.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", default=None)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--csv", default=None)
    backend = ap.add_mutually_exclusive_group()
    backend.add_argument("--native-only", action="store_true")
    backend.add_argument("--python-only", action="store_true")
    a = ap.parse_args()

    files = a.files or [os.path.join(ROOT, f) for f in DEFAULT_FILES]
    print("native library available: %s" % afc2.NATIVE)
    print("runs per measurement: %d (median reported)\n" % a.runs)

    if a.native_only and not afc2.NATIVE:
        print("--native-only requested, but the native library is unavailable")
        return 1
    force_modes = [False] if a.native_only else (
        [True] if a.python_only else [True, False])

    rows = []
    for path in files:
        data = open(path, "rb").read()
        name = os.path.basename(path)
        print("=" * 92)
        print("%s — %d bytes" % (name, len(data)))
        print("%-9s %-13s %10s %8s %9s %11s %11s %10s %s"
              % ("preset", "backend", "bytes", "ratio", "saved%",
                 "comp ms", "dec ms", "peak RSS KB", "ok"))
        for preset in ("fast", "balanced", "maximum"):
            for force_py in force_modes:
                if force_py is False and not afc2.NATIVE:
                    continue
                r = measure(path, data, preset, force_py, a.runs)
                ratio = len(data) / r["bytes"] if r["bytes"] else 0
                saved = 100.0 * (1 - r["bytes"] / len(data)) if data else 0
                print("%-9s %-13s %10d %8.3f %9.2f %11.2f %11.2f %10.1f %s"
                      % (preset, r["backend"], r["bytes"], ratio, saved,
                         r["comp_ms"], r["dec_ms"], r["peak_kb"],
                         "OK" if r["ok"] else "FAILED"))
                rows.append({"file": name, "orig": len(data), "preset": preset,
                             "backend": r["backend"], "bytes": r["bytes"],
                             "ratio": round(ratio, 4),
                             "saved_pct": round(saved, 2),
                             "comp_ms": round(r["comp_ms"], 3),
                             "dec_ms": round(r["dec_ms"], 3),
                             "peak_kb": round(r["peak_kb"], 1),
                             "container": r["container"],
                             "lossless": r["ok"]})
        print()

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("wrote %s (%d rows)" % (a.csv, len(rows)))

    bad = [r for r in rows if not r["lossless"]]
    if bad:
        print("\n%d ROW(S) FAILED SHA-256 VERIFICATION" % len(bad))
        return 1
    print("\nAll %d measured rows verified lossless (SHA-256)." % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
