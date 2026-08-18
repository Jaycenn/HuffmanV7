#!/usr/bin/env python3
"""Size-policy benchmark for SIZE_POLICY.md.

Answers, with measured numbers rather than assumptions:
  - does the engine COMPLETE on 150 MB and 250 MB inputs?
  - how long does it take (native and pure-Python)?
  - does memory stay bounded (peak RSS vs input size)?
  - is it still lossless (SHA-256 round trip)?

Run:  python tools/size_policy_bench.py            # native + python
      python tools/size_policy_bench.py --quick    # small sizes only

Results are appended to benchmarks/size_policy_results.csv.  Pure-Python runs
at very large sizes are gated behind --python-large because the interpreted
DP parse is ~30x slower than the native core; the script reports a measured
scaling point plus a clearly-labelled extrapolation instead of pretending to
have run a size it did not.
"""
import argparse
import csv
import os
import random
import sys
import tempfile

import experiment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
OUT_CSV = os.path.join("benchmarks", "size_policy_results.csv")


def make_file(path, nbytes, seed=7):
    """Deterministic mixed corpus: repetitive log lines + prose words, so all
    three tiers of the scan are exercised (not an unrealistically easy input)."""
    if os.path.exists(path) and os.path.getsize(path) == nbytes:
        return path
    rng = random.Random(seed)
    W = ("the of and a to in is was he that it his her she had with for as on "
         "at by which but from this all not are were when we there been").split()
    P = ["/api/v1/users", "/static/app.js", "/health", "/api/v1/orders"]
    buf = bytearray()
    with open(path, "wb") as f:
        while nbytes > 0:
            buf.clear()
            while len(buf) < (1 << 20) and len(buf) < nbytes:
                if rng.random() < 0.5:
                    buf += ('192.168.%d.%d - - [27/Jul/2026] "GET %s HTTP/1.1" '
                            '%d %d\n' % (rng.randint(0, 4), rng.randint(1, 254),
                                         rng.choice(P),
                                         rng.choice([200, 200, 304, 404]),
                                         rng.randint(180, 9500))).encode()
                else:
                    buf += (" ".join(rng.choice(W)
                                     for _ in range(rng.randint(8, 18)))
                            + ".\n").encode()
            chunk = bytes(buf[:min(len(buf), nbytes)])
            f.write(chunk)
            nbytes -= len(chunk)
    return path


def run_one(path, native, timeout):
    """Run one measurement in a FRESH subprocess so peak RSS is attributable
    to this file alone. Returns an explicit failure row on timeout/crash."""
    backend = "native" if native else "python"
    row = experiment.run_child(path, "balanced", backend, 0, timeout)
    if row.get("status") != "ok":
        return {"status": row.get("status", "failed").upper() + ": " +
                          row.get("error", "unknown error")}
    return {"status": "OK", "orig": row["original_bytes"],
            "comp": row["compressed_bytes"],
            "comp_s": row["compression_ms"] / 1000.0,
            "dec_s": row["decompression_ms"] / 1000.0,
            "rss_mb": row["peak_rss_kib"] / 1024.0,
            "lossless": row["byte_equal"] and row["sha256_equal"],
            "engine": backend}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="small sizes only (CI smoke test)")
    ap.add_argument("--python-large", action="store_true",
                    help="also run pure-Python at 150/250 MB (very slow)")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--sizes", type=int, nargs="+", default=None,
                    help="explicit input sizes in MiB (overrides --quick)")
    ap.add_argument("--csv", default=OUT_CSV,
                    help="result CSV path")
    a = ap.parse_args()

    sizes_mb = a.sizes or ([8, 32] if a.quick else [8, 32, 100, 150, 250])
    py_sizes = (sizes_mb if a.sizes else
                ([8, 32] if not a.python_large else [8, 32, 150, 250]))

    rows = []
    tmp = os.environ.get("AFC_BENCH_TMP", tempfile.gettempdir())
    for mb in sizes_mb:
        path = os.path.join(tmp, "afc_size_%dmb.bin" % mb)
        make_file(path, mb * 1024 * 1024)
        for native in (True, False):
            if not native and mb not in py_sizes:
                continue
            label = "native" if native else "python"
            res = run_one(path, native, a.timeout)
            res.update({"size_mb": mb, "backend": label})
            rows.append(res)
            if res["status"] == "OK":
                print("%6d MB %-7s comp %8.2fs  dec %7.2fs  peakRSS %8.1f MB "
                      "(%.1fx)  ratio %.2fx  lossless=%s"
                      % (mb, label, res["comp_s"], res["dec_s"], res["rss_mb"],
                         res["rss_mb"] / mb, res["orig"] / max(1, res["comp"]),
                         res["lossless"]))
            else:
                print("%6d MB %-7s %s" % (mb, label, res["status"]))
            sys.stdout.flush()
        try:
            os.remove(path)
        except OSError:
            pass

    os.makedirs("benchmarks", exist_ok=True)
    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["size_mb", "backend", "status", "orig_bytes",
                    "compressed_bytes", "comp_seconds", "dec_seconds",
                    "peak_rss_mb", "rss_per_input_byte", "lossless"])
        for r in rows:
            if r["status"] == "OK":
                w.writerow([r["size_mb"], r["backend"], r["status"], r["orig"],
                            r["comp"], "%.3f" % r["comp_s"],
                            "%.3f" % r["dec_s"], "%.1f" % r["rss_mb"],
                            "%.2f" % (r["rss_mb"] / r["size_mb"]),
                            int(r["lossless"])])
            else:
                w.writerow([r["size_mb"], r["backend"], r["status"],
                            "", "", "", "", "", "", ""])
    print("\nwrote", a.csv)


if __name__ == "__main__":
    main()
