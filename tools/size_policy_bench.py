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
import hashlib
import os
import random
import resource
import subprocess
import sys
import time

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


CHILD = r'''
import hashlib, os, resource, sys, time
sys.path.insert(0, sys.argv[1])
import afc2
path = sys.argv[2]
data = open(path, "rb").read()
ref = hashlib.sha256(data).digest()
t0 = time.perf_counter(); blob = afc2.compress_bytes(data, True)
ct = time.perf_counter() - t0
t0 = time.perf_counter(); out = afc2.decompress_bytes(blob)
dt = time.perf_counter() - t0
ok = hashlib.sha256(out).digest() == ref
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
print("RESULT %d %d %.3f %.3f %.1f %d %s" % (
    len(data), len(blob), ct, dt, rss, int(ok),
    "native" if afc2.NATIVE else "python"))
'''


def run_one(path, native, timeout):
    """Run one measurement in a FRESH subprocess so peak RSS is attributable
    to this file alone.  Returns a dict or None on timeout/crash."""
    env = dict(os.environ)
    if not native:
        env["AFC_NO_NATIVE"] = "1"
    else:
        env.pop("AFC_NO_NATIVE", None)
    try:
        r = subprocess.run([sys.executable, "-c", CHILD, ROOT, path],
                           capture_output=True, text=True, env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT (>%ds)" % timeout}
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return {"status": "FAILED: " + (tail[-1] if tail else
                                        "rc=%d" % r.returncode)}
    for line in r.stdout.splitlines():
        if line.startswith("RESULT "):
            _, o, c, ct, dt, rss, ok, eng = line.split()
            return {"status": "OK", "orig": int(o), "comp": int(c),
                    "comp_s": float(ct), "dec_s": float(dt),
                    "rss_mb": float(rss), "lossless": bool(int(ok)),
                    "engine": eng}
    return {"status": "FAILED: no RESULT line"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="small sizes only (CI smoke test)")
    ap.add_argument("--python-large", action="store_true",
                    help="also run pure-Python at 150/250 MB (very slow)")
    ap.add_argument("--timeout", type=int, default=3600)
    a = ap.parse_args()

    sizes_mb = [8, 32] if a.quick else [8, 32, 100, 150, 250]
    py_sizes = [8, 32] if not a.python_large else [8, 32, 150, 250]

    rows = []
    tmp = os.environ.get("AFC_BENCH_TMP", "/tmp")
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
    with open(OUT_CSV, "w", newline="") as f:
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
    print("\nwrote", OUT_CSV)


if __name__ == "__main__":
    main()
