#!/usr/bin/env python3
"""Reproducible AFC preset/backend experiment runner.

Each measured trial runs in a fresh child interpreter and emits a row even on
failure. Timing excludes file I/O and import time. Memory is the complete OS
resident working set and therefore includes native allocations.
"""

import argparse
import csv
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2  # noqa: E402


SUITES = {
    "developmental": [
        "benchmarks/corpus/server.log", "benchmarks/corpus/data.csv",
        "benchmarks/corpus/data.json", "benchmarks/corpus/records.bin",
        "benchmarks/corpus/prose_en.txt", "benchmarks/corpus/random.bin",
        "benchmarks/corpus/code_python.py.txt",
        "benchmarks/corpus/bytes256.bin",
    ],
    "canterbury": [
        "benchmarks/canterbury_official/alice29.txt",
        "benchmarks/canterbury_official/asyoulik.txt",
        "benchmarks/canterbury_official/cp.html",
        "benchmarks/canterbury_official/fields.c",
        "benchmarks/canterbury_official/grammar.lsp",
        "benchmarks/canterbury_official/kennedy.xls",
        "benchmarks/canterbury_official/lcet10.txt",
        "benchmarks/canterbury_official/plrabn12.txt",
        "benchmarks/canterbury_official/ptt5",
        "benchmarks/canterbury_official/sum",
        "benchmarks/canterbury_official/xargs.1",
    ],
    "silesia": [
        "benchmarks/silesia/dickens", "benchmarks/silesia/mozilla",
        "benchmarks/silesia/mr", "benchmarks/silesia/nci",
        "benchmarks/silesia/ooffice", "benchmarks/silesia/osdb",
        "benchmarks/silesia/reymont", "benchmarks/silesia/samba",
        "benchmarks/silesia/sao", "benchmarks/silesia/webster",
        "benchmarks/silesia/xml", "benchmarks/silesia/x-ray",
    ],
    "documents": [],
}


def _document_files():
    folder = os.path.join(ROOT, "benchmarks", "documents")
    if not os.path.isdir(folder):
        return []
    return [os.path.join("benchmarks", "documents", name)
            for name in sorted(os.listdir(folder))
            if name.lower().endswith((".pdf", ".docx"))]


def environment_record():
    native = getattr(afc2, "_native", None)
    library = getattr(native, "LIBRARY_PATH", "") if native else ""
    library_sha = ""
    if library and os.path.isfile(library):
        with open(library, "rb") as stream:
            library_sha = hashlib.sha256(stream.read()).hexdigest()
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "native_available": bool(afc2.NATIVE),
        "native_library": library,
        "native_library_sha256": library_sha,
        "git_commit": _git_commit(),
        "timing_policy": "fresh process per trial; file read before timer",
        "memory_policy": "OS resident working set sampled every 1 ms",
    }


def _git_commit():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    except Exception:
        return ""


def worker(path, preset, backend, warmups):
    import presets
    from bench_runtime import measure_peak

    with open(path, "rb") as stream:
        data = stream.read()
    source_sha = hashlib.sha256(data).hexdigest()
    options = presets.options_for(preset)
    for _ in range(warmups):
        warm = afc2.compress_bytes(data, options=options, backend=backend)
        if afc2.decompress_bytes(warm) != data:
            raise ValueError("warm-up round trip failed")

    started = time.perf_counter_ns()
    blob, baseline_kb, peak_kb = measure_peak(
        lambda: afc2.compress_bytes(data, options=options, backend=backend))
    compress_ns = time.perf_counter_ns() - started
    started = time.perf_counter_ns()
    restored = afc2.decompress_bytes(blob)
    decompress_ns = time.perf_counter_ns() - started
    restored_sha = hashlib.sha256(restored).hexdigest()
    return {
        "status": "ok",
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "preset": preset,
        "backend": backend,
        "backend_reported": "C++ native" if backend == "native" else "pure Python",
        "original_bytes": len(data),
        "compressed_bytes": len(blob),
        "container": blob[:4].decode("ascii", "replace"),
        "compression_ms": compress_ns / 1_000_000.0,
        "decompression_ms": decompress_ns / 1_000_000.0,
        "baseline_rss_kib": baseline_kb,
        "peak_rss_kib": peak_kb,
        "peak_delta_kib": max(0, peak_kb - baseline_kb),
        "source_sha256": source_sha,
        "restored_sha256": restored_sha,
        "byte_equal": restored == data,
        "sha256_equal": restored_sha == source_sha,
    }


def run_child(path, preset, backend, warmups, timeout):
    command = [sys.executable, __file__, "--worker", path, preset, backend,
               str(warmups)]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "file": os.path.basename(path),
                "path": os.path.abspath(path), "preset": preset,
                "backend": backend, "error": "trial exceeded %d s" % timeout}
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip().splitlines()
        return {"status": "failed", "file": os.path.basename(path),
                "path": os.path.abspath(path), "preset": preset,
                "backend": backend,
                "error": error[-1] if error else "child exit %d" % result.returncode}
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"status": "failed", "file": os.path.basename(path),
                "path": os.path.abspath(path), "preset": preset,
                "backend": backend, "error": "invalid child output: %s" % exc}


def write_csv(path, rows):
    if not rows:
        return
    fields = sorted({key for row in rows for key in row})
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summaries(rows):
    groups = {}
    for row in rows:
        key = (row.get("path"), row.get("preset"), row.get("backend"))
        groups.setdefault(key, []).append(row)
    out = []
    for (path, preset, backend), group in sorted(groups.items()):
        good = [row for row in group if row.get("status") == "ok"]
        base = {"file": os.path.basename(path or ""), "path": path,
                "preset": preset, "backend": backend,
                "trials_requested": len(group), "trials_ok": len(good)}
        if not good:
            base.update({"status": "failed", "error": group[0].get("error", "")})
            out.append(base)
            continue
        sizes = {row["compressed_bytes"] for row in good}
        base.update({
            "status": "ok" if len(good) == len(group) and len(sizes) == 1 else "invalid",
            "original_bytes": good[0]["original_bytes"],
            "compressed_bytes": good[0]["compressed_bytes"],
            "container": good[0]["container"],
            "ratio": good[0]["original_bytes"] / max(1, good[0]["compressed_bytes"]),
            "saved_percent": 100.0 * (1.0 - good[0]["compressed_bytes"] /
                                       max(1, good[0]["original_bytes"])),
            "median_compression_ms": statistics.median(
                row["compression_ms"] for row in good),
            "median_decompression_ms": statistics.median(
                row["decompression_ms"] for row in good),
            "max_peak_rss_kib": max(row["peak_rss_kib"] for row in good),
            "max_peak_delta_kib": max(row["peak_delta_kib"] for row in good),
            "deterministic_size": len(sizes) == 1,
            "all_byte_equal": all(row["byte_equal"] for row in good),
            "all_sha256_equal": all(row["sha256_equal"] for row in good),
        })
        out.append(base)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*")
    parser.add_argument("--suite", choices=tuple(SUITES) + ("all",))
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--presets", nargs="+", default=["fast", "balanced", "maximum"])
    parser.add_argument("--backends", nargs="+", default=["python", "native"])
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--out-prefix", default="benchmarks/experiment")
    parser.add_argument("--worker", nargs=4, metavar=("FILE", "PRESET", "BACKEND", "WARMUPS"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        path, preset, backend, warmups = args.worker
        print(json.dumps(worker(path, preset, backend, int(warmups)), sort_keys=True))
        return 0
    if args.runs < 1 or args.warmups < 0:
        parser.error("runs must be positive and warmups non-negative")

    files = list(args.files)
    SUITES["documents"] = _document_files()
    if args.suite:
        names = tuple(SUITES) if args.suite == "all" else (args.suite,)
        for name in names:
            files.extend(SUITES[name])
    files = list(dict.fromkeys(os.path.abspath(os.path.join(ROOT, p))
                               if not os.path.isabs(p) else p for p in files))
    missing = [path for path in files if not os.path.isfile(path)]
    if missing:
        for path in missing:
            print("MISSING", path)
        return 2
    if not files:
        parser.error("provide files or --suite")
    if "native" in args.backends and not afc2.NATIVE:
        print("native backend requested but unavailable")
        return 2

    rows = []
    for path in files:
        for preset in args.presets:
            for backend in args.backends:
                for trial in range(1, args.runs + 1):
                    row = run_child(path, preset, backend, args.warmups, args.timeout)
                    row["trial"] = trial
                    rows.append(row)
                    print("%-24s %-8s %-6s %3d/%-3d %s" % (
                        os.path.basename(path), preset, backend, trial, args.runs,
                        row.get("status")))
                    sys.stdout.flush()

    summary = summaries(rows)
    prefix = os.path.abspath(os.path.join(ROOT, args.out_prefix)
                             if not os.path.isabs(args.out_prefix)
                             else args.out_prefix)
    os.makedirs(os.path.dirname(prefix), exist_ok=True)
    write_csv(prefix + "_trials.csv", rows)
    write_csv(prefix + "_summary.csv", summary)
    with open(prefix + "_environment.json", "w", encoding="utf-8") as stream:
        json.dump(environment_record(), stream, indent=2, sort_keys=True)
    invalid = [row for row in summary if row.get("status") != "ok"
               or not row.get("all_byte_equal") or not row.get("all_sha256_equal")]
    print("wrote", prefix + "_{trials,summary,environment}")
    print("%d summary rows; %d invalid" % (len(summary), len(invalid)))
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
