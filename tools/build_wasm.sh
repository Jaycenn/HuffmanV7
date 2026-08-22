#!/bin/sh
# Build the WebAssembly core for AFC_WebApp.html from afc_native.cpp.
# Requires the Emscripten SDK (https://emscripten.org) on PATH.
#
# The wasm build is single-threaded (-DAFC_NO_THREADS): browser threads
# would require SharedArrayBuffer + COOP/COEP headers, which a standalone
# file:// page cannot provide.  Output bytes are identical either way.
#
# Result: afc_core.wasm next to AFC_WebApp.html.  The page loads it
# automatically when present and silently falls back to afc_engine.js
# (byte-identical) when absent.
set -e
cd "$(dirname "$0")/.."
emcc -O3 -std=c++17 -DAFC_NO_THREADS \
  -s STANDALONE_WASM=1 -s ALLOW_MEMORY_GROWTH=1 --no-entry \
  -s EXPORTED_FUNCTIONS='["_afc_compress","_afc_decompress","_afc_free","_malloc","_free"]' \
  afc_native.cpp -o afc_core.wasm
echo "built afc_core.wasm"
