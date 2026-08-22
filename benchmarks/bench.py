#!/usr/bin/env python3
"""Benchmark harness.  Runs one engine directory over the corpus and emits a
CSV: file,mode,engine,backend,orig_bytes,out_bytes,comp_ms_median,dec_ms_median,sha_ok

Every run SHA-256-verifies the round trip; a failed round trip aborts with a
non-zero exit code.  Timing = median over --runs (default 7) runs.

Usage:
  python benchmarks/bench.py --engine-dir . --label v4 [--runs 7]
      [--fmt auto|afc1|afc2] [--no-native] [--files ...]
"""
import argparse
import glob
import hashlib
import importlib
import os
import statistics
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--engine-dir", required=True)
ap.add_argument("--label", required=True)
ap.add_argument("--runs", type=int, default=7)
ap.add_argument("--fmt", default=None, help="container format override")
ap.add_argument("--no-native", action="store_true")
ap.add_argument("--modes", default="adaptive,baseline")
ap.add_argument("--files", nargs="*", default=None)
ap.add_argument("--opts", default=None,
                help="comma list of engine OPTS to force, e.g. dp=0,hdr2=1")
a = ap.parse_args()

if a.no_native:
    os.environ["AFC_NO_NATIVE"] = "1"

eng_dir = os.path.abspath(a.engine_dir)
sys.path.insert(0, eng_dir)
afc2 = importlib.import_module("afc2")
if a.opts and hasattr(afc2, "OPTS"):
    for kv in a.opts.split(","):
        k, v = kv.split("=")
        afc2.OPTS[k] = bool(int(v))

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
files = a.files or sorted(glob.glob(os.path.join(root, "benchmarks",
                                                 "corpus", "*")))
backend = "native" if afc2.NATIVE else "python"
print("file,mode,engine,backend,orig_bytes,out_bytes,"
      "comp_ms_median,dec_ms_median,sha_ok")
failed = False
for path in files:
    data = open(path, "rb").read()
    ref = hashlib.sha256(data).digest()
    for mode in a.modes.split(","):
        adaptive = mode == "adaptive"
        kw = {}
        if a.fmt:
            kw = {"fmt": a.fmt}
        ct, dt = [], []
        blob = out = b""
        for _ in range(a.runs):
            t0 = time.perf_counter()
            blob = afc2.compress_bytes(data, adaptive, **kw)
            ct.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            out = afc2.decompress_bytes(blob)
            dt.append((time.perf_counter() - t0) * 1000)
        ok = hashlib.sha256(out).digest() == ref
        failed |= not ok
        print(f"{os.path.basename(path)},{mode},{a.label},{backend},"
              f"{len(data)},{len(blob)},{statistics.median(ct):.2f},"
              f"{statistics.median(dt):.2f},{int(ok)}")
sys.exit(1 if failed else 0)
