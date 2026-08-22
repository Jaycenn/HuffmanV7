#!/bin/sh
# Rebuild the local Tailwind stylesheet (no CDN, works offline).
# Requires node+npx once; the generated CSS is committed so end users need neither.
set -e
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3 -c tailwind.config.js \
    -i tools/tailwind_input.css -o static/css/tailwind.css --minify
echo "built static/css/tailwind.css"
