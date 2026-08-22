#!/usr/bin/env python3
"""Size/lossless audit over corpora that are NOT redistributed with this
repository (Canterbury, Silesia, GovDocs1).

The corpora are large and separately licensed, so they are downloaded by the
operator and pointed at by path:

    python benchmarks/external_corpus.py \
        --corpus canterbury=/path/to/cantrbry \
        --corpus silesia=/path/to/silesia \
        --corpus govdocs1=/path/to/govdocs1/files \
        --out benchmarks/external_corpus.csv

Every row SHA-256-verifies the round trip. A file whose round trip is not
byte-exact is reported and makes the run exit non-zero; a size is never
published for output that is not provably lossless.

Rows are written and flushed as they complete, so a long run can be inspected
(or resumed from) while it is still going. Timings are machine-dependent;
the byte counts are not.

SEARCH-PROFILE GUARD
--------------------
A preset is a LADDER of search profiles, and the ladder can only be walked if
the loaded native library exports the per-call profile entry point. When it
does not, presets.ladder_for() correctly falls back to the default shape alone
-- Balanced searches two profiles instead of six, Maximum three instead of
twelve -- and the engine still reports "C++ native", because the native core is
genuinely in use. A corpus measured that way understates the engine by several
percent and nothing in the output says so.

That happened once: a shipped Windows library predated the entry point, and the
resulting audit was published before the discrepancy was traced. So this
harness refuses to run when the loaded backend cannot walk the ladder, and
writes the capability it observed next to the CSV. --allow-degraded overrides
the refusal for the deliberate case (measuring the fallback path on purpose)
and stamps DEGRADED into the sidecar so the result cannot be mistaken later for
a full-depth run.
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402
import presets                                                  # noqa: E402

FIELDS = ["corpus", "file", "preset", "backend", "container",
          "original_bytes", "compressed_bytes", "ratio", "saved_percent",
          "byte_equal", "sha256_equal", "compression_ms", "decompression_ms"]


def backend_capability():
    """What the loaded backend can actually do, as plain data.

    ``profiles`` is the fact that matters: False means every preset ladder
    collapses to its default shape, so the measurement would describe a weaker
    engine than the one under study.
    """
    native = bool(getattr(afc2, "NATIVE", False))
    lib = getattr(afc2, "_native", None)
    return {
        "native": native,
        "tunable": bool(getattr(lib, "TUNABLE", False)) if native else False,
        "profiles": bool(presets.profiles_available()),
        "library": str(getattr(lib, "LIBRARY_PATH", "") or "") if native else "",
        "reason": str(getattr(lib, "REASON", "") or "") if native else "",
        "ladder_rungs_balanced": len(presets.ladder_for("balanced", 1 << 20)),
        "ladder_rungs_maximum": len(presets.ladder_for("maximum", 1 << 20)),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


REFUSAL = """REFUSING TO MEASURE: the loaded backend cannot walk the preset ladder.
  native   %s
  tunable  %s
  profiles %s   <- required
  library  %s
  balanced would search %d profile(s), maximum %d

The engine would still report the native backend while searching a fraction of
what the presets define, and the corpus totals would understate the engine by
several percent with nothing in the CSV to show it.

Fix: rebuild the native library from afc_native.cpp so it exports the per-call
profile entry point, or delete the prebuilt library and let afc_native.py build
one. Pass --allow-degraded only if measuring the fallback path is the point of
the run.
"""


def _refuse(cap):
    sys.stderr.write(REFUSAL % (
        cap["native"], cap["tunable"], cap["profiles"],
        cap["library"] or "(none)",
        cap["ladder_rungs_balanced"], cap["ladder_rungs_maximum"]))


def walk(path):
    if os.path.isfile(path):
        return [path]
    out = []
    for base, _dirs, names in os.walk(path):
        for name in sorted(names):
            full = os.path.join(base, name)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                out.append(full)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", required=True,
                    metavar="NAME=PATH")
    ap.add_argument("--presets", default="fast,balanced,maximum")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="skip files larger than this (0 = no limit)")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="measure even when the backend cannot walk "
                         "the preset ladder; marks the run DEGRADED")
    args = ap.parse_args()

    cap = backend_capability()
    if not cap["profiles"] and not args.allow_degraded:
        _refuse(cap)
        return 3
    cap["degraded"] = not cap["profiles"]
    sys.stderr.write(
        "backend: native=%s profiles=%s  balanced searches %d "
        "profile(s), maximum %d%s\n"
        % (cap["native"], cap["profiles"], cap["ladder_rungs_balanced"],
           cap["ladder_rungs_maximum"],
           "   *** DEGRADED ***" if cap["degraded"] else ""))

    jobs = []
    for spec in args.corpus:
        name, _, path = spec.partition("=")
        files = walk(path)
        if not files:
            sys.stderr.write("no files under %s\n" % path)
            return 2
        jobs.append((name, files))

    presets_wanted = args.presets.split(",")
    total_files = sum(len(f) for _n, f in jobs)
    done = 0
    failures = []
    started = time.time()

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        for corpus, files in jobs:
            for path in files:
                size = os.path.getsize(path)
                if args.max_bytes and size > args.max_bytes:
                    done += 1
                    continue
                data = open(path, "rb").read()
                digest = hashlib.sha256(data).hexdigest()
                for preset in presets_wanted:
                    t0 = time.perf_counter()
                    blob, _used, backend = presets.compress_with(
                        data, preset, fmt="auto")
                    cms = (time.perf_counter() - t0) * 1000.0
                    t0 = time.perf_counter()
                    out = afc2.decompress_bytes(blob)
                    dms = (time.perf_counter() - t0) * 1000.0
                    byte_equal = out == data
                    sha_equal = hashlib.sha256(out).hexdigest() == digest
                    if not (byte_equal and sha_equal):
                        failures.append((corpus, path, preset))
                    writer.writerow([
                        corpus, os.path.basename(path), preset, backend,
                        blob[:4].decode("ascii", "replace"), len(data),
                        len(blob), round(len(data) / max(1, len(blob)), 6),
                        round(100.0 * (1.0 - len(blob) / max(1, len(data))), 4),
                        byte_equal, sha_equal, round(cms, 4), round(dms, 4)])
                    del out
                del data
                handle.flush()
                done += 1
                if done % 25 == 0 or done == total_files:
                    sys.stderr.write(
                        "  %d/%d files  %.0fs elapsed\n"
                        % (done, total_files, time.time() - started))
                    sys.stderr.flush()

    if failures:
        sys.stderr.write("ROUND TRIP FAILED on %d (file, preset):\n"
                         % len(failures))
        for corpus, path, preset in failures[:20]:
            sys.stderr.write("  %s %s %s\n" % (corpus, path, preset))
        return 1
    sys.stderr.write("all round trips byte-exact and SHA-256 verified\n")
    side = os.path.splitext(args.out)[0] + "_backend.json"
    cap["files"] = total_files
    cap["presets"] = presets_wanted
    cap["elapsed_s"] = round(time.time() - started, 1)
    with open(side, "w", encoding="utf-8") as meta:
        json.dump(cap, meta, indent=2, sort_keys=True)
        meta.write("\n")
    sys.stderr.write("wrote %s\n" % side)
    if cap["degraded"]:
        sys.stderr.write(
            "WARNING: this run is DEGRADED. The ladder collapsed to its "
            "default shape;\nthese figures are not the full-depth engine.\n")
    _summarize(args.out, presets_wanted)
    return 0


def _summarize(path, presets_wanted):
    """Report how often a preset wins, not only the corpus total.

    A corpus total is dominated by whichever files are largest, so a preset
    that helps substantially on many files can look inert in the aggregate.
    Reporting the per-file win rate and the largest individual gains is the
    honest way to describe what a preset actually does for a user, whose file
    is one file rather than a corpus."""
    import csv as _csv
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(_csv.DictReader(handle))
    by = {}
    for r in rows:
        by.setdefault(r["preset"], {})[(r["corpus"], r["file"])] = \
            (int(r["original_bytes"]), int(r["compressed_bytes"]))
    order = [p for p in ("fast", "balanced", "maximum") if p in by]
    out = sys.stderr
    for lower, higher in zip(order, order[1:]):
        a, b = by[lower], by[higher]
        keys = [k for k in a if k in b]
        wins = [(a[k][1] - b[k][1], k) for k in keys if b[k][1] < a[k][1]]
        ties = sum(1 for k in keys if b[k][1] == a[k][1])
        worse = sum(1 for k in keys if b[k][1] > a[k][1])
        total = sum(a[k][1] for k in keys)
        saved = sum(d for d, _ in wins)
        out.write("\n%s vs %s\n" % (higher.upper(), lower))
        out.write("  smaller on %d of %d files (%.1f%%), identical on %d, "
                  "larger on %d\n" % (len(wins), len(keys),
                                      100.0 * len(wins) / max(1, len(keys)),
                                      ties, worse))
        out.write("  bytes saved: %s (%.2f%% of %s output)\n"
                  % (format(saved, ","), 100.0 * saved / max(1, total), lower))
        for d, k in sorted(wins, reverse=True)[:5]:
            pct = 100.0 * d / a[k][1]
            out.write("    %-11s %-16s %10s bytes  %5.2f%%\n"
                      % (k[0], k[1][:16], format(d, ","), pct))


if __name__ == "__main__":
    sys.exit(main())