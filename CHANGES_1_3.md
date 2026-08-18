# AFC 1.3 — verified native execution, integrity, PDF Flate, and evaluation

This release continues the project's Multi-Level Frequency Analysis, Bit Cost
Decision Engine, structural blocks, and canonical Hybrid-Huffman coding. It
does not add or substitute another AFC compression algorithm.

## Current-branch audit before modification

The audit began on branch `claude/hybrid-huffman-system-fuhn15` at commit
`6df43df`. Claims from earlier screenshots and documentation were re-tested.

* `tools/native_doctor.py` loaded the packaged Windows x64
  `afc_kernels.dll`, found the tunable `afc_compress_ex` export, and reported
  Fast, Balanced, and Maximum as C++ native.
* The untouched suite measured 250 passes; with `AFC_NO_NATIVE=1`, 237 passes.
* `tools/run_verification.py` verified 60 corpus round trips, Python/native
  byte identity, cross-decoding, the JavaScript decoder, and edge cases.
* A single-run 10,437,251-byte Dickens surrogate audit measured:

| preset | backend | bytes | saved | compression |
|---|---|---:|---:|---:|
| Fast | C++ native | 3,978,304 | 61.88% | 1.201 s |
| Balanced | C++ native | 3,619,716 | 65.32% | 10.873 s |
| Maximum | C++ native | 3,556,007 | 65.93% | 28.574 s |

These are current-branch measurements, not the old 138/335-second
pure-Python screenshots. Algorithm tuning was not changed in response to old
timings.

## Immutable preset execution

`afc2.EngineOptions` is a frozen per-call configuration. `presets.compress_with`
passes one immutable option object and an explicit `auto`, `native`, or
`python` backend into the engine. Production requests no longer mutate global
DP/tree/search settings. Concurrent preset tests verify isolation, lossless
decoding, and byte-identical Python/C++ output for every preset.

## AFC5 self-verifying envelope

AFC5 wraps one AFC1/AFC2/AFC3/AFC4/AFC6 payload with:

* original byte length and SHA-256;
* payload length and SHA-256; and
* a UTF-8, path-free original basename.

The payload hash is checked before decoding; reconstructed length and SHA-256
are checked afterward. Legacy AFC1-AFC4 files still decode. The web app now
creates AFC5 by default, so verification does not depend on the local history
database.

## AFC6 exact PDF Flate components

PDF `/FlateDecode` commonly stores RFC-1950 zlib-wrapped DEFLATE. AFC6 is a
new explicit component format rather than silently changing AFC4 semantics.
For eligible textual page/content, metadata, or object streams:

1. the parser reads the existing zlib header, DEFLATE tokens and Adler-32;
2. it exposes the expanded source and records the producer's exact choices;
3. source and recipe are pooled through the existing Hybrid-Huffman engine;
4. restoration serialises the original stream bit-for-bit; and
5. the complete PDF bytes and SHA-256 are verified before selection.

JPEG/JPX, fonts, images, unsupported streams, malformed inputs and excessive
expansions stay verbatim. AFC6 is selected only if its complete result beats
AFC3 and whole-file AFC. On the current fixtures, forced AFC6 reconstruction
was exact, but AFC3 remained smaller and therefore won automatic selection.
This is reported as a measured outcome, not an improvement claim.

## DOCX path retained and verified

AFC4 continues to discover normal OOXML members from the ZIP central
directory, including `word/document.xml`. Viable method-8 XML uses the exact
raw-DEFLATE token recipe; media and compact/high-entropy components remain raw.
The user uploads one normal `.docx`, and decompression rebuilds the exact
original ZIP package.

## Reproducible evaluation tooling

* `benchmarks/corpus_manifest.json` pins all 11 official Canterbury and all 12
  Silesia files by byte size and cryptographic checksum.
* `tools/corpus_manifest.py` downloads/verifies those source corpora.
* `tools/experiment.py` runs fresh processes per trial, records raw trials,
  medians, environment metadata, exact bytes/SHA-256 and platform RSS.
* `tools/analyze_experiment.py` provides paired Wilcoxon signed-rank tests,
  Holm correction and rank-biserial effects without treating repeat trials as
  independent files.
* `tools/bench_runtime.py` reports real RSS on Windows, Linux and macOS/BSD;
  the old Windows `-1` placeholder is removed.

## Measured document regression

One current native audit run over 15 generated PDF/DOCX fixtures produced:

* 1,928,299 original bytes;
* 1,061,080 bytes through plain whole-file AFC;
* 1,052,853 bytes through automatic component routing (0.78% smaller);
* 8 files smaller, 7 equal, 0 larger; and
* 15/15 exact byte equality and SHA-256 matches.

The final regression suite after these changes measured **277 passed, 0
failed** with native loading enabled and **264 passed, 0 failed** with
`AFC_NO_NATIVE=1`. These counts describe the current branch at the time of
this change; future edits must rerun the suite rather than copying the numbers
as assumptions.

## Official Canterbury and Silesia audit

The pinned manifests were verified before the run. A one-trial-per-file native
audit then exercised Fast, Balanced and Maximum on every file: 33 Canterbury
and 36 Silesia configurations, **69/69 valid**, all byte-equal and SHA-256
equal. These timings describe a correctness/performance audit, not a repeated
timing study.

| suite / preset | source bytes | AFC bytes | saved | summed compression | max RSS |
|---|---:|---:|---:|---:|---:|
| Canterbury Fast | 2,810,784 | 929,546 | 66.93% | 0.486 s | 57,644 KiB |
| Canterbury Balanced | 2,810,784 | 826,154 | 70.61% | 3.190 s | 57,996 KiB |
| Canterbury Maximum | 2,810,784 | 817,940 | 70.90% | 8.452 s | 56,512 KiB |
| Silesia Fast | 211,938,580 | 99,141,167 | 53.22% | 26.452 s | 812,264 KiB |
| Silesia Balanced | 211,938,580 | 91,315,157 | 56.91% | 223.911 s | 928,424 KiB |
| Silesia Maximum | 211,938,580 | 88,430,131 | 58.28% | 698.280 s | 992,976 KiB |

Raw per-trial CSV, file-level summaries, environment metadata and paired size
statistics are stored under `benchmarks/afc_1_3_*`.

## UI

The Flask application still has distinct Compress and Decompress pages. A
local `static/css/app.css` layer adds a light-first, higher-contrast layout,
clearer card hierarchy, larger drop zones, keyboard focus indicators and
responsive adjustments. Dark mode remains optional. No CDN is required.
