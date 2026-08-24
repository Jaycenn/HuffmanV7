#!/usr/bin/env python3
"""Fail unless the native core loaded and the whole preset ladder is reachable.

Run as a build step, not at runtime.  afc_native.py deliberately degrades to
pure Python when the C++ core is unavailable -- correct for a laptop with no
compiler, wrong for a deployment, where the fallback is ~12x slower with
byte-identical output and nothing on screen says so.  A participant would
experience that as "the compressor is slow" and rate it on the questionnaire.

Exits non-zero, with the loader's own explanation, so a deploy fails loudly
instead of quietly serving a different engine than the one the thesis measured.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import afc_native                                              # noqa: E402
import presets                                                 # noqa: E402

# Rungs each ladder must offer.  presets.ladder_for() is what decides which
# profile wins, so a short ladder is not a slow build -- it is a different
# experiment.
EXPECTED = {"fast": 2, "balanced": 6, "maximum": 12}


def main():
    if not afc_native.AVAILABLE:
        sys.stderr.write("native core did not load: %s\n" % afc_native.REASON)
        for step in afc_native.DIAGNOSTICS:
            sys.stderr.write("  [%s] %s: %s\n" % (
                "ok" if step["ok"] else "--", step["step"], step["detail"]))
        return 1

    print("native core: %s" % afc_native.LIBRARY_PATH)
    bad = []
    for name, want in EXPECTED.items():
        got = len(presets.ladder_for(name))
        print("  ladder %-9s %2d profile(s)%s"
              % (name, got, "" if got == want else "  EXPECTED %d" % want))
        if got != want:
            bad.append(name)
    if bad:
        sys.stderr.write(
            "ladder is degraded for: %s -- the profile search would not match "
            "the one the thesis measured.\n" % ", ".join(sorted(bad)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
