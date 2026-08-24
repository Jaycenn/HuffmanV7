#!/usr/bin/env python3
"""Tier and gate ablation: what does each component of the model actually earn?

The whole-system comparison in Appendix F shows that the Adaptive System beats
single-tier Huffman.  It cannot show WHICH part earns that, because every part
is present in every measurement.  Objectives 1 and 2 name the multi-tier scanner
and the Bit Cost Decision Engine specifically, so each needs a number of its own.

This runs the same corpus under four builds and reports the difference:

    full        the complete system
    no-tier3    Tier 3 (whole-word structural tokens) disabled
    no-tier23   Tiers 2 and 3 disabled -- byte-level Huffman only
    no-gate     the Bit Cost Decision Engine replaced by an unconditional
                frequency threshold.  _bit_cost_gain is patched module-wide,
                so this disables the cost-based profitability decision at
                all three sites that call it: candidate admission
                (afc2.py:210), merge scoring during structural block growth
                (afc2.py:336), and the final audit that drops unprofitable
                entries (afc2.py:402).  It is not an admission-only ablation.

Reading the result:

    Tier 3 earns    no-tier3  - full
    Tier 2 earns    no-tier23 - no-tier3
    the Engine earns  no-gate - full

These are measured against different baselines and answer different
counterfactuals, so they are reported independently and are never summed.
Tiers 2 and 3 together (no-tier23 - full) is the complete difference between
the full system and byte-level Huffman coding alone.

A NEGATIVE figure for the Decision Engine would mean admitting everything
produced smaller output, which would falsify the claim the Engine exists to
support.  That is the point of measuring it rather than asserting it.

    python benchmarks/tier_ablation.py
    python benchmarks/tier_ablation.py --corpus text=benchmarks/canterbury \
        --max-bytes 400000 --out benchmarks/tier_ablation.csv

BACKEND.  Every configuration runs on the pure-Python reference path.  The
ablations are patches to the Python model functions; the native core cannot be
reconfigured this way, so measuring the full build natively and the ablated
builds in Python would compare two different implementations.  Python is slower
but it is the same code for all four columns, which is what makes the
difference attributable.  Sizes are identical to the native path -- the test
suite asserts that -- so only the wall clock differs.

CONFIGURATION.  All four builds call afc2.compress_bytes with the engine's
default EngineOptions.  They do NOT run a preset search ladder, so the "full"
column is the default-options build and is NOT the Balanced-preset figure
reported for the same files elsewhere in the study.  That is deliberate: the
four columns must differ only in the ablated component.  Quote the differences
between columns, not the "full" total, as a preset result.

Every configuration round-trips and is SHA-256 verified before it is counted.
"""
import argparse
import contextlib
import csv
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afc2                                                     # noqa: E402


@contextlib.contextmanager
def _patched(**repl):
    """Temporarily replace named afc2 functions, restoring them afterwards."""
    saved = {name: getattr(afc2, name) for name in repl}
    try:
        for name, fn in repl.items():
            setattr(afc2, name, fn)
        yield
    finally:
        for name, fn in saved.items():
            setattr(afc2, name, fn)


def _no_words(data, min_freq):
    """Tier 3 disabled: no lexical tokens are ever proposed."""
    return {}


def _no_ngrams(data, min_freq, options=None):
    """Tier 2 disabled: no multi-byte sequences are ever proposed."""
    return {}


def _always_admit(pat, freq, lit_bits, sym_bits):
    """Decision Engine disabled: every cost test returns a constant gain.

    This replaces afc2._bit_cost_gain module-wide, so all three call sites see
    it.  _select_candidates keeps a candidate when the gain is > 0, so
    admission depends on min_candidate_freq alone; merge scoring during
    structural block growth loses its cost ranking; and the final audit, which
    drops an entry when the gain is <= 0, can no longer drop anything.  The
    build therefore disables the Decision Engine entirely, not admission alone.
    """
    return 1


CONFIGS = [
    ("full", "the complete system", {}),
    ("no-tier3", "Tier 3 disabled", {"_tier3_words": _no_words}),
    ("no-tier23", "Tiers 2 and 3 disabled",
     {"_tier3_words": _no_words, "_tier2_ngrams": _no_ngrams}),
    ("no-gate", "Bit Cost Decision Engine replaced by a frequency threshold",
     {"_bit_cost_gain": _always_admit}),
]


def walk(path):
    if os.path.isfile(path):
        return [path]
    found = []
    for base, _dirs, names in os.walk(path):
        found.extend(os.path.join(base, n) for n in sorted(names))
    return sorted(found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", metavar="NAME=PATH",
                    help="repeatable; defaults to the repository corpus")
    ap.add_argument("--max-bytes", type=int, default=1_500_000,
                    help="skip files larger than this (the Python path is slow)")
    ap.add_argument("--min-bytes", type=int, default=256)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    specs = args.corpus or ["corpus=benchmarks/corpus",
                            "canterbury=benchmarks/canterbury"]
    jobs = []
    for spec in specs:
        name, _, path = spec.partition("=")
        files = [f for f in walk(os.path.join(ROOT, path))
                 if args.min_bytes <= os.path.getsize(f) <= args.max_bytes]
        if not files:
            sys.stderr.write("no eligible files under %s\n" % path)
            return 2
        jobs.append((name, files))

    total_files = sum(len(f) for _n, f in jobs)
    print("tier and gate ablation")
    print("  backend      pure Python, engine default options "
          "(no preset ladder)")
    print("  files        %d, %d bytes to %d bytes"
          % (total_files, args.min_bytes, args.max_bytes))
    print("  builds       %s" % ", ".join(n for n, _d, _p in CONFIGS))
    print()

    rows = []
    started = time.time()
    done = 0
    for corpus, files in jobs:
        for path in files:
            data = open(path, "rb").read()
            digest = hashlib.sha256(data).hexdigest()
            sizes = {}
            for name, _desc, repl in CONFIGS:
                with _patched(**repl):
                    blob = afc2.compress_bytes(data, backend="python")
                    back = afc2.decompress_bytes(blob)
                if back != data or hashlib.sha256(back).hexdigest() != digest:
                    sys.exit("round trip FAILED: %s under %s" % (path, name))
                sizes[name] = len(blob)
            rows.append((corpus, os.path.basename(path), len(data), sizes))
            done += 1
            print("  %3d/%d  %-26s %8d -> full %7d  no-t3 %7d  no-t23 %7d  "
                  "no-gate %7d"
                  % (done, total_files, os.path.basename(path)[:25], len(data),
                     sizes["full"], sizes["no-tier3"], sizes["no-tier23"],
                     sizes["no-gate"]), flush=True)

    orig = sum(r[2] for r in rows)
    tot = {n: sum(r[3][n] for r in rows) for n, _d, _p in CONFIGS}

    print()
    print("%-12s %14s %9s   %s" % ("build", "AFC bytes", "saved", "description"))
    print("-" * 78)
    for name, desc, _p in CONFIGS:
        print("%-12s %14s %8.2f%%   %s"
              % (name, format(tot[name], ","), (1 - tot[name] / orig) * 100, desc))
    print("-" * 78)
    print("%-12s %14s" % ("original", format(orig, ",")))

    def share(bigger, smaller):
        d = tot[bigger] - tot[smaller]
        return d, (d / tot[bigger] * 100) if tot[bigger] else 0.0

    t3, t3p = share("no-tier3", "full")
    t2, t2p = share("no-tier23", "no-tier3")
    gate, gatep = share("no-gate", "full")
    both, bothp = share("no-tier23", "full")

    print()
    print("component contributions, as bytes removed from the build without it")
    print("  Tier 3 (word tokens)      %+12s   %6.2f%% of that build's output"
          % (format(-t3, ","), t3p))
    print("  Tier 2 (n-grams)          %+12s   %6.2f%%" % (format(-t2, ","), t2p))
    print("  Tiers 2 and 3 together    %+12s   %6.2f%%" % (format(-both, ","), bothp))
    print("  Bit Cost Decision Engine  %+12s   %6.2f%%" % (format(-gate, ","), gatep))
    print()
    if gate > 0:
        print("  The Decision Engine pays for itself: admitting every candidate")
        print("  above the frequency floor produced %s more bytes."
              % format(gate, ","))
    else:
        print("  NOTE: the Decision Engine did NOT pay for itself on this "
              "corpus (%s bytes)." % format(gate, ","))
        print("  That is a real result and belongs in the write-up as one.")
    print()
    print("  %d files, %d configurations, all byte-exact and SHA-256 verified"
          % (len(rows), len(rows) * len(CONFIGS)))
    print("  elapsed %.0fs" % (time.time() - started))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            w = csv.writer(handle, lineterminator="\n")
            w.writerow(["corpus", "file", "orig_bytes"]
                       + [n + "_bytes" for n, _d, _p in CONFIGS])
            for corpus, fname, n, sizes in rows:
                w.writerow([corpus, fname, n]
                           + [sizes[c] for c, _d, _p in CONFIGS])
        print("  wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())