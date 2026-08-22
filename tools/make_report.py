#!/usr/bin/env python3
"""Regenerates the benchmark CSVs in benchmarks/:

  results_v3_native.csv / results_v3_python.csv   (BEFORE — frozen snapshot)
  results_v4_native.csv / results_v4_python.csv   (AFTER)
  results_before_after.csv                        (merged side-by-side)
  ablation_sizes.csv                              (one optimization at a time)
  reference_codecs.csv                            (gzip/bzip2/lzma — labeled
                                                   reference rows only)

Timings are medians over --runs runs (default 7).  Run from the project
root; takes a few minutes because the pure-Python engines are timed too.
"""
import argparse
import bz2
import csv
import glob
import gzip
import importlib
import lzma
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
BENCH = os.path.join(ROOT, "benchmarks", "bench.py")
CORPUS = sorted(glob.glob(os.path.join("benchmarks", "corpus", "*")))

ap = argparse.ArgumentParser()
ap.add_argument("--runs", type=int, default=7)
a = ap.parse_args()


def run_bench(engine_dir, label, out_csv, no_native=False):
    cmd = [sys.executable, BENCH, "--engine-dir", engine_dir,
           "--label", label, "--runs", str(a.runs)]
    if no_native:
        cmd.append("--no-native")
    with open(out_csv, "w") as f:
        subprocess.run(cmd, stdout=f, check=True)
    print("wrote", out_csv)


run_bench("benchmarks/v3_snapshot", "v3", "benchmarks/results_v3_native.csv")
run_bench("benchmarks/v3_snapshot", "v3", "benchmarks/results_v3_python.csv",
          no_native=True)
run_bench(".", "v4", "benchmarks/results_v4_native.csv")
run_bench(".", "v4", "benchmarks/results_v4_python.csv", no_native=True)

# ---- merged before/after -------------------------------------------------
rows = {}
for fn in ("results_v3_native.csv", "results_v3_python.csv",
           "results_v4_native.csv", "results_v4_python.csv"):
    with open(os.path.join("benchmarks", fn)) as f:
        for r in csv.DictReader(f):
            key = (r["file"], r["mode"], r["backend"])
            rows.setdefault(key, {})[r["engine"]] = r

with open("benchmarks/results_before_after.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "mode", "backend", "orig_bytes",
                "v3_bytes", "v4_bytes", "size_delta_pct",
                "v3_comp_ms", "v4_comp_ms",
                "v3_dec_ms", "v4_dec_ms"])
    for (file, mode, backend), d in sorted(rows.items()):
        if "v3" not in d or "v4" not in d:
            continue
        v3, v4 = d["v3"], d["v4"]
        s3, s4 = int(v3["out_bytes"]), int(v4["out_bytes"])
        w.writerow([file, mode, backend, v3["orig_bytes"], s3, s4,
                    f"{100.0 * (s4 - s3) / s3:+.2f}",
                    v3["comp_ms_median"], v4["comp_ms_median"],
                    v3["dec_ms_median"], v4["dec_ms_median"]])
print("wrote benchmarks/results_before_after.csv")

# ---- size ablation (one variable at a time, pure Python, adaptive) --------
os.environ["AFC_NO_NATIVE"] = "1"
sys.path.insert(0, ROOT)
afc2 = importlib.import_module("afc2")
VARIANTS = [
    ("v3-base", dict(dp=0, llhuff=0, hdr2=0, refund=0, tune=0)),
    ("llhuff_only", dict(dp=0, llhuff=1, hdr2=0, refund=0, tune=0)),
    ("dp_only", dict(dp=1, llhuff=0, hdr2=0, refund=0, tune=0)),
    ("hdr2_only", dict(dp=0, llhuff=0, hdr2=1, refund=0, tune=0)),
    ("refund_only", dict(dp=0, llhuff=0, hdr2=0, refund=1, tune=0)),
    ("tune_only", dict(dp=0, llhuff=0, hdr2=0, refund=0, tune=1)),
    ("v4_all", dict(dp=1, llhuff=1, hdr2=1, refund=1, tune=1)),
]
with open("benchmarks/ablation_sizes.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "orig_bytes"] + [v[0] for v in VARIANTS])
    table = {}
    for name, opts in VARIANTS:
        for k, v in opts.items():
            afc2.OPTS[k] = bool(v)
        for p in CORPUS:
            d = open(p, "rb").read()
            b = afc2.compress_bytes(d, True)
            assert afc2.decompress_bytes(b) == d
            table[(name, p)] = len(b)
    for k in afc2.OPTS:
        afc2.OPTS[k] = True
    for p in CORPUS:
        w.writerow([os.path.basename(p), os.path.getsize(p)] +
                   [table[(name, p)] for name, _ in VARIANTS])
print("wrote benchmarks/ablation_sizes.csv")

# ---- clearly-labeled reference rows ----------------------------------------
def write_reference(paths, out_csv):
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "orig_bytes", "afc_v4_bytes",
                    "gzip9_bytes_REFERENCE", "bzip2_bytes_REFERENCE",
                    "lzma_bytes_REFERENCE"])
        for p in paths:
            d = open(p, "rb").read()
            v4 = len(afc2.compress_bytes(d, True))
            w.writerow([os.path.basename(p), len(d), v4,
                        len(gzip.compress(d, 9)), len(bz2.compress(d, 9)),
                        len(lzma.compress(d, preset=6))])
    print("wrote", out_csv)


write_reference(CORPUS, "benchmarks/reference_codecs.csv")

# ---- real Canterbury / Silesia corpora, if present -------------------------
# These are third-party standard benchmark files (not generated); the
# headline results in CHANGES.md / README come from here.
for folder, tag in (("canterbury", "canterbury"), ("silesia", "silesia")):
    d = os.path.join("benchmarks", folder)
    if not os.path.isdir(d):
        continue
    real = sorted(glob.glob(os.path.join(d, "*")))
    real = [p for p in real if os.path.isfile(p)]
    if not real:
        continue
    # bench.py's --files lets us target just this folder:
    import subprocess as _sp
    for label, engine_dir, extra in (
            ("v3", "benchmarks/v3_snapshot", []),
            ("v4", ".", []),
            ("v4", ".", ["--no-native"])):
        suffix = "python" if "--no-native" in extra else "native"
        out = f"benchmarks/{tag}_{label}_{suffix}.csv"
        cmd = [sys.executable, BENCH, "--engine-dir", engine_dir,
               "--label", label, "--runs", str(a.runs),
               "--files"] + real + extra
        with open(out, "w") as fh:
            _sp.run(cmd, stdout=fh, check=True)
        print("wrote", out)
    write_reference(real, f"benchmarks/{tag}_reference_codecs.csv")


def med(xs):
    return statistics.median(xs)
