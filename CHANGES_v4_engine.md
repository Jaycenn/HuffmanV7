# AFC v4 — Change Log with Thesis Mapping

Every change below is mapped to the thesis terminology: **multi-tier scan**
(Tier-1 byte frequencies → Tier-2 n-gram scan → Tier-3 word tokens), **Bit
Cost Decision Engine**, **structural blocks**, and the **hybrid Huffman
tree**.

Nothing in this release adds a forbidden mechanism. The entropy stage is
still **static canonical Huffman only** (now length-limited via
package-merge). There is no arithmetic/range/ANS coding, no LZ77/LZ78/LZW/
LZSS, no offset or distance back-reference of any kind, no BWT/MTF, no PPM or
context mixing, and no machine-learning model. The multi-level frequency
dictionary plus the Bit Cost Decision Engine remains the core mechanism —
every optimization below either feeds it better numbers or spends its output
more efficiently.

---

## Measurement environment

| Item | Value |
|---|---|
| CPU | 4 cores, Linux 6.18.5 (container) |
| Compiler | g++ (Ubuntu 13.3.0) `-O3 -std=c++17 -pthread` |
| Python | CPython 3.11.15 |
| Node | v22.22.2 (browser-engine cross-checks) |
| Timing | median of **7** runs per file per mode (`benchmarks/bench.py --runs 7`) |
| Corpus | 10 files, 1.07 MB total, regenerated deterministically by `tools/make_corpus.py` |

"v3" throughout = the frozen pre-upgrade engine in `benchmarks/v3_snapshot/`,
which reproduces the original pipeline (greedy segmentation, unlimited-depth
Huffman, AFC1-only container, v3 kernels). All numbers come from
`benchmarks/results_*.csv`; nothing here is estimated.

---

## 1. Size changes

### 1a. AFC2 compacted container — *hybrid Huffman tree serialization*

**What changed.** A second container format was added. The **hybrid Huffman
tree** is written far more tightly than in AFC1:

* symbol ids are delta-coded (`id[k] − id[k−1] − 1`) then varint-packed —
  in a dense mixed alphabet (literals 0–255 plus patterns 256+i) almost every
  delta collapses to a single byte;
* code lengths are packed **4 bits each** (`length − 1`, high nibble first)
  instead of one byte per symbol — exactly half the table;
* **structural block** dictionary entries are emitted through the literal
  Huffman codes of the same canonical tree rather than as raw bytes, with a
  per-file flag byte so the encoder keeps whichever is actually smaller.

AFC1 remains the default-compatible output and is still read by every
decoder (Python, C++, JavaScript). `--format {auto,afc1,afc2}` selects the
output; `auto` emits AFC1 unless AFC2 is *strictly* smaller, so the format
switch can never cost bytes.

**Measured (isolated, `hdr2_only` column of `benchmarks/ablation_sizes.csv`):**
−1.0 % to −9.3 % per file, e.g. `universe.sql` 2578 → 2338 B (−9.3 %),
`data.csv` 53576 → 52497 B (−2.0 %), `bytes256.bin` 322 → 309 B (−4.0 %).
**Kept.**

### 1b. Optimal parsing — *segmentation over real costs*

**What changed.** Greedy longest-match segmentation is now only the *seed*
parse. The final segmentation is a dynamic-programming shortest path over the
symbol lattice, where each edge weighs the symbol's **actual Huffman code
length** (a symbol with no code yet costs a fixed 18-bit penalty). Parse and
tree are then iterated: parse → rebuild hybrid tree → re-parse, **3 rounds**.

This is *not* a back-reference scheme: the DP only chooses among dictionary
entries that the multi-tier scan and the Bit Cost Decision Engine already
admitted. It changes *which admitted symbol covers each position*, nothing
else. Greedy is provably suboptimal here — taking the longest match can force
an expensive rare symbol where two cheap frequent ones would cost fewer bits.

**Measured (isolated, `dp_only`):** the single largest win. `data.json`
29006 → 17410 B (−40.0 %), `prose_en.txt` 28229 → 21502 B (−23.8 %),
`server.log` 51678 → 41181 B (−20.3 %), `universe.sql` 2578 → 2181 B
(−15.4 %). Round sweep (total corpus bytes): 1 round 264162, 2 rounds
262073, **3 rounds 261286**, so 3 was chosen. **Kept.**

### 1c. Length-limited canonical Huffman — *hybrid Huffman tree*

**What changed.** Tree construction moved from unlimited-depth Huffman to
deterministic **package-merge** with a 16-bit limit, in all three
implementations.

**Measured (isolated, `llhuff_only`): 0 bytes changed on every corpus file.**
This is an honest negative result *for compression on this corpus* — the
natural Huffman depth never exceeded 16 bits, so the limit never bound.

**Kept anyway, for two non-size reasons:** it is the precondition for the
4-bit code-length nibbles in 1a (a length > 16 would not fit), and it caps
decoder table size so the byte-at-a-time LUT decoder (2h) is always
constructible. Its cost is zero bytes and it removes a pathological-input
failure mode.

### 1d. Per-file tuning — *multi-tier scan admission floor*

**What changed.** For files below 64 KB the engine now compresses **twice** —
once at the v3 admission floor `MIN_CANDIDATE_FREQ = 4`, once at 3 — and
keeps the smaller container. Ties keep the v3 setting, so tuning cannot
regress.

**Failed direction, corrected.** The first implementation simply *lowered*
the floor to 3 for small files instead of trying both. That regressed
`bytes256.bin` from 322 B to 1031 B (+220 %): with 769 near-tied candidates
admitted, the greedy parse never merged any adjacent pair, **structural block
growth** stalled, and the strict final audit then dropped every entry, leaving
a literal-only stream. Measuring one variable at a time caught it. The
trial-both-and-keep-smaller form is what ships.

**Measured (isolated, `tune_only`):** small and narrow — `universe.sql`
2578 → 2515 B (−2.4 %), all other files unchanged. **Kept** (it is monotone
by construction, and it costs time only on files under 64 KB, where time is
already negligible).

### 1e. Dictionary-refund accounting — *structural block growth*

**What changed.** When adjacent-pair merging creates a **structural block**
that makes one of its children unused, the child's dictionary cost
`8 × (len + 3)` is refunded into the merge's Bit Cost Decision Engine score.
The old scoring charged for the new entry but never credited the retired one,
so profitable merges were rejected. Merge rounds were also raised 3 → 6,
which only pays off once refunds are counted.

**Measured (isolated, `refund_only`):** small — `code_python.py.txt`
20953 → 20844 B (−0.5 %), `data.csv` 53576 → 53542 B, others unchanged.
**Kept** (never negative, and it interacts positively with 1b: deeper blocks
give the DP parse more material).

### 1f. Strict-final-audit pricing fix — *Bit Cost Decision Engine*

**What changed (not behind a toggle).** The strict final audit re-prices every
pattern with its **actual** post-segmentation count, per the thesis rule. The
v3 audit priced the "spelled out" side using counts taken from the *segmented
id stream*, in which a pattern's constituent bytes are frequently absent —
so those bytes were charged the 18-bit no-code penalty, the spelled-out side
was inflated, and patterns that were in fact unprofitable survived the audit.
The audit now prices the spelled-out side with the **Tier-1 literal
estimates**, i.e. the same estimator the admission stage used, so admission
and audit are consistent.

**Measured (v3 → ablation `v3-base`, all other v4 options off):**
`data.json` 34071 → 29006 B (−14.9 %), `prose_en.txt` 32462 → 28229 B
(−13.0 %), `universe.sql` 2810 → 2578 B (−8.3 %), `code_python.py.txt`
22194 → 20953 B (−5.6 %). **Two files got slightly worse in isolation:**
`data.csv` 53386 → 53576 B (+0.4 %) and `records.bin` 53151 → 53207 B
(+0.1 %) — a stricter audit drops entries that the greedy parse happened to
use well. Both are more than recovered by the full v4 stack (`data.csv`
49431 B, `records.bin` 51526 B, i.e. −7.4 % and −3.1 % against v3), so the
shipped build regresses on neither. **Kept.**

### Net size result (adaptive mode, `benchmarks/results_before_after.csv`)

| file | orig | v3 | v4 | delta |
|---|---:|---:|---:|---:|
| bytes256.bin | 1 024 | 322 | **278** | −13.7 % |
| code_python.py.txt | 41 181 | 22 194 | **17 363** | −21.8 % |
| data.csv | 179 858 | 53 386 | **49 431** | −7.4 % |
| data.json | 151 159 | 34 071 | **16 704** | −51.0 % |
| prose_en.txt | 67 426 | 32 462 | **20 928** | −35.5 % |
| random.bin | 65 536 | 65 544 | 65 544 | ±0 (raw fallback) |
| records.bin | 131 072 | 53 151 | **51 526** | −3.1 % |
| server.log | 424 437 | 52 421 | **37 432** | −28.6 % |
| tiny.txt | 32 | 38 | 38 | ±0 (raw fallback) |
| universe.sql | 3 973 | 2 810 | **2 042** | −27.3 % |

Baseline mode also shrinks slightly (−0.0 % to −1.3 %) from length-limited
trees and the AFC2 header. **No corpus file, in either mode, in any container
format, is larger under v4 than under v3.**

---

## 2. Speed changes

### 2f. Whole pipeline in one native core

**What changed.** v3 called into C++ for three isolated kernels (n-gram
count, greedy segmentation, bit packing) and did candidate scoring, block
growth, the audit, tree building and container assembly in Python — paying a
ctypes crossing and a full Python-object materialization of the id stream at
every boundary. v4 exposes a single `afc_compress()` entry point that runs
**multi-tier scan → Bit Cost Decision Engine → greedy seed parse → structural
block growth → strict final audit → DP optimal parse → hybrid Huffman →
container emit** entirely in C++, for both modes and all three formats.

The pure-Python engine remains a complete, independent implementation and is
selected automatically when no library can be loaded (or forced with
`AFC_NO_NATIVE=1`).

**Measured (native, adaptive):** `data.csv` 160.0 → 97.2 ms (−39 %),
`records.bin` 111.7 → 74.6 ms (−33 %), `random.bin` 59.5 → 40.5 ms (−32 %),
`server.log` 198.8 → 165.4 ms (−17 %) — while simultaneously producing the
smaller outputs above, i.e. the DP parse is paid for and still nets a win.
**Baseline mode is where the crossing dominated:** `code_python.py.txt`
11.2 → 0.57 ms (**19.7×**), `data.csv` 44.6 → 1.32 ms (**33.8×**),
`server.log` 113.7 → 2.86 ms (**39.7×**). **Kept.**

Two native slowdowns, both on trivially small inputs where fixed setup now
dominates: `tiny.txt` 0.15 → 0.48 ms (32 B) and, before the threshold in 2g
was added, `bytes256.bin`. Absolute cost is well under a millisecond.

### 2g. Threaded multi-tier scan

**What changed.** The Tier-2 n-gram scan runs one thread per length (2, 3, 4,
5) into **separate** maps, so results never depend on thread interleaving —
output stays byte-deterministic. Files under 64 KB additionally run their two
tuning trials (1d) on parallel threads.

**Failed direction, corrected.** Threading unconditionally made small files
*slower* — spawn cost exceeded the scan. `bytes256.bin` sat at 5.98 ms.
A 16 KB threshold (serial below it) brought it to **3.55 ms**, now faster
than v3's 3.99 ms. Threshold shipped.

Also in the scan: n-grams of length ≤ 8 are hashed as packed `uint64` keys
instead of heap strings, and segmentation lookups use `string_view` into the
input buffer instead of constructing a `std::string` per probe. Same counts,
no allocation.

### 2h. Table-driven multi-bit canonical decoding

**What changed.** The decoder built a single-level lookup table indexed by the
next `maxlen` bits, yielding `(symbol, bit-length)` in one step, replacing the
bit-by-bit canonical walk. Guaranteed constructible because of 1c. Legacy
AFC1 files whose code lengths exceed 16 bits still decode through the old walk,
so **no existing file becomes unreadable**.

**Measured (native decode):** `prose_en.txt` 0.62 → 0.30 ms (−52 %),
`data.json` 0.64 → 0.35 ms (−45 %), `server.log` 1.15 → 0.65 ms (−43 %),
baseline `data.csv` 2.01 → 1.43 ms (−29 %). Pure-Python decode also improves
(`server.log` 24.2 → 17.5 ms). **Kept.**

### 2i. WebAssembly build for the browser app

**What changed.** `afc_native.cpp` compiles to WebAssembly via
`tools/build_wasm.sh` (`-DAFC_NO_THREADS`, single-threaded because a
`file://` page cannot supply the COOP/COEP headers SharedArrayBuffer needs).
`AFC_WebApp.html` probes for `afc_core.wasm` at startup and falls back to the
JavaScript engine when it is absent; the badge names whichever is live.

**Not measured — reported honestly.** The Emscripten SDK is not installed in
this environment, so **no wasm timing is claimed**. What *is* verified here is
that the source compiles cleanly in its `-DAFC_NO_THREADS` configuration
(`g++ -DAFC_NO_THREADS -fsyntax-only`, clean), that the loader path is
present, and that the JavaScript fallback is byte-exact (below). The `emcc`
line is documented in the README for a machine with the SDK.

---

## 3. Cross-implementation equivalence

All three implementations produce **identical container bytes** for identical
input, and each decodes the others' output:

* **60 encode combinations** (10 files × {adaptive, baseline} × {auto, afc1,
  afc2}): C++ core output is byte-identical to pure-Python output — 0
  mismatches.
* The **JavaScript engine** (`afc_engine.js`) reproduces all 60 containers
  byte-for-byte and decodes every Python/C++ container — 0 mismatches,
  0 decode failures.
* Every container decodes correctly through the Python decoder, the native
  decoder, and the JavaScript decoder.

Determinism is enforced by construction, not by luck: package-merge uses a
two-pointer merge with a documented leaf-before-package tie rule, candidate
ranking sorts by `(gain desc, pattern bytes asc)`, merge ranking by
`(gain desc, merged bytes asc, left id, right id)`, and the threaded scan
writes to disjoint maps.

---

## 4. Losslessness

`tools/run_verification.py` — run on the native engine **and** with
`AFC_NO_NATIVE=1`; both report ALL CHECKS PASSED:

* 60 SHA-256-verified round trips per engine;
* native ↔ pure-Python container byte-identity;
* cross-decoding Python ↔ native ↔ JavaScript;
* edge cases: empty file, 1 byte, 64 KB of one repeated byte, full 256-byte
  alphabet, a 1.5 MB input exceeding the 1 MB scan window, and an `.afc`
  container fed back in as ordinary data.

The raw-storage fallback is intact: incompressible input never inflates by
more than the container header (`random.bin` 65 536 → 65 544 B, +8 B).

---

## 5. Reference codecs (context only — not part of the thesis claim)

`benchmarks/reference_codecs.csv` records gzip -9, bzip2 -9 and LZMA sizes.
These are **general-purpose LZ/BWT codecs listed for orientation only**; they
are not an AFC configuration and are outside the thesis mechanism. AFC v4
happens to beat all three on `prose_en.txt` (20 928 vs 21 340 LZMA / 23 363
gzip), on `bytes256.bin` and on `tiny.txt`, and is close to gzip on
`server.log`, `data.json` and `data.csv`; the LZ-family codecs remain ahead on
`code_python.py.txt`, `records.bin` and `universe.sql`. That gap is expected —
they may use back-references, which AFC by design may not.

---

## 5a. Real Canterbury Corpus results

The synthetic corpus above is what the harness generates locally. To validate
against a recognized standard, the five **Canterbury Corpus** files supplied
for this study — `alice29.txt`, `asyoulik.txt`, `cp.html`, `fields.c`,
`grammar.lsp` — were added under `benchmarks/canterbury/` and run through the
identical harness. Their byte sizes match the canonical Canterbury values
exactly (152089 / 125179 / 24603 / 11150 / 3721). All 30 combinations
(5 files × 2 modes × 3 formats) pass SHA-256 round trips, native↔Python
byte-identity, and Python↔native↔JavaScript cross-decoding.

**v4 vs v3 (adaptive, native, median of 7 runs — `benchmarks/canterbury_before_after.csv`):**

| file | orig | v3 | v4 | size | v4 decode |
|---|---:|---:|---:|---:|---:|
| alice29.txt | 152 089 | 77 407 | **61 692** | −20.3 % | 1.40 ms |
| asyoulik.txt | 125 179 | 66 774 | **54 741** | −18.0 % | 1.09 ms |
| cp.html | 24 603 | 14 864 | **10 869** | −26.9 % | 0.24 ms |
| fields.c | 11 150 | 7 455 | **5 330** | −28.5 % | 0.10 ms |
| grammar.lsp | 3 721 | 2 350 | **1 927** | −18.0 % | 0.05 ms |

v4 is smaller than v3 on **every** Canterbury file, in both modes (baseline
gains are small, −0.04 % to −1.6 %, as expected — no dictionary there). Note
`fields.c` under v3 adaptive (7455 B) was actually *worse* than v3 baseline
(7215 B) — a symptom of the audit-pricing bug in §1f; v4 adaptive (5330 B)
fixes that and beats both.

**AFC v4 vs reference codecs (`benchmarks/canterbury_reference_codecs.csv`) —
these are LZ/BWT codecs listed for orientation only, outside the thesis
mechanism:**

| file | afc v4 | gzip -9 | bzip2 -9 | lzma |
|---|---:|---:|---:|---:|
| alice29.txt | 61 692 | 54 182 | 43 202 | 48 492 |
| asyoulik.txt | 54 741 | 48 790 | 39 569 | 44 536 |
| cp.html | 10 869 | 7 952 | 7 624 | 7 644 |
| fields.c | 5 330 | 3 127 | 3 039 | 3 028 |
| grammar.lsp | 1 927 | 1 234 | 1 283 | 1 292 |
| **total** | **134 559** | 115 285 | 94 717 | 104 992 |

**Honest bottom line on Canterbury:** AFC v4 reaches **2.35×** overall but is
**16.7 % larger than gzip -9, 42.1 % larger than bzip2, 28.2 % larger than
lzma** on this text-and-code corpus. That gap is structural, not a tuning
miss: these files are dominated by repeated *phrases at varying offsets*
(natural-language text, source code), which back-reference codecs capture and
AFC — restricted to a frequency dictionary with no offset/distance references
— cannot. The v4 work closes roughly a third of the v3→gzip gap (v3 total was
188 850 B, +63.8 % over gzip; v4 is +16.7 %) but does not overtake the
LZ family here, and no honest configuration of a back-reference-free codec
would. Where AFC's mechanism fits the data better — the highly templated
`server.log` and `data.json` in the synthetic set, or `prose_en.txt` where it
beats gzip and lzma — it is competitive; on free-form prose and C source it
is not, and this section states that plainly rather than cherry-picking.

Silesia was requested too but could not be fetched: this environment's proxy
uses an allowlist (package registries and GitHub only), so
`sun.aei.polsl.pl`, `corpus.canterbury.ac.nz`, and every other outside host
return 403. Drop Silesia files into `benchmarks/silesia/` and re-run
`tools/make_report.py` — the harness already handles that folder and will emit
`silesia_*` CSVs with the same round-trip guarantees.

---

## 6. Compatibility

* Python API unchanged: `compress_bytes(data, adaptive, fmt=...)`,
  `decompress_bytes(blob)`.
* CLI unchanged, plus `--format`: `compress`, `decompress`, `verify`,
  `benchmark`, `--baseline`.
* Pure-Python fallback when no native library is available.
* Legacy AFC1 files — including those written by v3, with code lengths above
  16 bits — decode in all three implementations.
* Web dashboard keeps every v3 feature (compress, decompress, verify/lossless
  badge, metrics, byte ruler, engine badge, download, error handling, 8 MB
  demo threshold, 64 MB cap) and adds a container selector and container
  badge. A v3 template bug that blanked the engine badge on compress (a later
  write overwrote the timing line) was fixed.

### Decompression made explicit in both web apps

Decompression worked in v3 and v4 but was **invisible**: it only ever
triggered by sniffing the container magic, the run button always read "Run
multi-tier compression", and the sole hint was a line of small grey text.
Users reasonably concluded the feature was missing. Both apps now expose it
directly, with no engine change:

* a three-way **Action** control — *Auto-detect* (previous behaviour, still
  the default), *Compress*, *Decompress*;
* the first 4 bytes of the chosen file are read client-side, so the page
  states what will happen before you run it and relabels the button to
  **Decompress this file**;
* forcing *Decompress* on a non-container gives a specific error instead of
  silently compressing; forcing *Compress* on an `.afc` file nests it (the
  engine already handled this — it is the `afc_as_data` edge case);
* encoder-only controls (container format, adaptive/baseline mode) hide while
  decoding, and the progress line describes decoding rather than tier scanning;
* `POST /compress` accepts an `action` field (`auto`|`compress`|`decompress`);
  omitting it reproduces the old magic-detection behaviour, so existing
  clients are unaffected.

Verified end-to-end in Chromium (Playwright) against both apps: compress →
download → re-upload → explicit decompress returns byte-identical data, and
the error path reports correctly. A pre-existing crash was also fixed — the
dashboard's inline `tailwind.config` threw `tailwind is not defined` whenever
the Tailwind CDN was unreachable, which killed the page's scripts; it is now
guarded, so an offline dashboard renders unstyled but stays fully functional.
