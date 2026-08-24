#!/usr/bin/env bash
# Build step for the hosted deployment.  Run once per deploy, before the
# process in Procfile starts.
#
# Two things happen here, in this order:
#
#   1. Python dependencies are installed from requirements.txt.
#   2. The C++ core is compiled to afc_kernels.so.
#
# Step 2 is the one that matters.  afc_native.py can compile the core lazily on
# first use, but on a hosted instance that would put a ~10 s compile inside the
# first participant's first request, and it would fail silently -- falling back
# to the pure-Python path, roughly 12x slower -- on any host whose runtime
# image has no compiler.  Building here makes the native path a deploy-time
# guarantee rather than a runtime hope: if g++ is missing, the deploy fails
# loudly instead of quietly serving a slower engine than the one measured in
# the thesis.
set -euo pipefail

python -m pip install -r requirements.txt

echo "--- building the native core ---"
g++ -O3 -std=c++17 -shared -fPIC -pthread afc_native.cpp -o afc_kernels.so

# Prove the library loads and that the full profile ladder is reachable.  A
# degraded ladder would change which preset wins, so it must not reach the
# participants.
python tools/verify_native.py

echo "--- build complete ---"
