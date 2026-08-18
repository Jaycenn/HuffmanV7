# Adaptive File Compression (AFC) — 1.3

**This archive is the complete project: compression engine + web application
(Parts 1 and 2 combined).** Part 2 extended Part 1 in place, so there is one
codebase, one database schema and one test suite — nothing needs merging.

* Engine work (multi-tier scan, Bit Cost Decision Engine, DP optimal parse,
  hybrid Huffman, AFC1/AFC2 containers) — `CHANGES_v4_engine.md`
* Web app Part 1 (accounts, queue, batch, `.afcpak` archives, reports, size
  policy, responsive layout) — `CHANGES.md`, first half
* Web app Part 2 (analytics, entropy estimate, hybrid-tree visualiser,
  explainer, presets, dark mode) — `CHANGES.md`, second half
* V7: native acceleration for every preset, and container-aware PDF/DOCX —
  `CHANGES_v7.md`
* V8: exact DOCX member inventory, reversible XML/token processing and AFC4 —
  `CHANGES_v8.md`
* 1.3: immutable preset calls, reproducible corpus tooling, AFC5 integrity,
  exact PDF Flate recipes (AFC6), and the light-first UI — `CHANGES_1_3.md`
* Scope/constraint compliance — `SCOPE_NOTES.md`
* File-size limits and the measured evidence behind them — `SIZE_POLICY.md`

Verify everything with:

```bash
python tests/test_app.py          # 277 checks: web app, native presets,
                                  # AFC1-AFC6, documents, integrity
python tools/native_doctor.py     # WHY is the backend Python or C++?
python tools/preset_bench.py      # Python vs C++ across all three presets
python tools/doc_bench.py         # PDF/DOCX container-aware results
python tools/run_verification.py  # engine round trips
```


Multi-Level Frequency Analysis & Hybrid Huffman.

AFC compresses by building a per-file dictionary of frequent patterns through
a three-tier scan, admitting each pattern only when a **Bit Cost Decision
Engine** proves it pays for itself, growing accepted patterns into larger
**structural blocks**, auditing the whole dictionary against actual counts,
and coding the resulting mixed symbol stream (literals `0–255`, patterns
`256+i`) with one **canonical hybrid Huffman tree**.

The entropy stage is static canonical Huffman only. There is no arithmetic /
range / ANS coding, no LZ77/78/W/SS or any offset-based back-reference, no
BWT/MTF, no PPM or context mixing, and no ML model.

Three implementations ship here and produce **byte-identical output**:
pure Python (reference), C++ (`afc_native.cpp`, loaded via ctypes or compiled
to WebAssembly), and JavaScript (`afc_engine.js`, for the standalone browser
app).

---

# Web application (Part 1) — accounts, queue, batch, archives, reports

The Flask dashboard is now a multi-user local web app. **The compression
engine is unchanged** — see `SCOPE_NOTES.md`. Engine-level docs continue
below this section.

## Setup

```bash
pip install flask
python app.py                     # opens http://127.0.0.1:5000
```

The SQLite database (`afc_app.sqlite3`) is created automatically next to
`app.py` on first run, along with the seeded admin.

### Default admin credentials

| Username | Password |
|---|---|
| `admin` | `afc-admin` |

**You will be forced to change this password at first login** — every route
redirects to the change form until you do. Override the seed before first run
with `AFC_ADMIN_USER` / `AFC_ADMIN_PASSWORD` / `AFC_ADMIN_EMAIL`.

Regular accounts self-register at `/register` and get the `user` role.

### Resetting the database

```bash
python -c "import db; db.reset_db()"     # DESTRUCTIVE: wipes users + history
```

Deleting `afc_app.sqlite3` (plus any `-wal`/`-shm` files) does the same. There
is no remote copy — see `SCOPE_NOTES.md` §4.

### Rebuilding the stylesheet

Tailwind is compiled locally to `static/css/tailwind.css`, so the app needs
**no internet**. After editing any template:

```bash
sh tools/build_css.sh        # needs node/npx once; output is committed
```

## Compress and Decompress are separate pages

The two operations are two destinations in the primary navigation. There is no
combined "compress or decompress?" upload box, so a first-time user picks the
page that names the operation they want:

```text
Dashboard
Analytics
History
Compare
────────────────
Compress      normal file → .afc
Decompress    .afc → original file
────────────────
Settings
```

**Compress** (`/compress`) — *"Reduce the size of your files using
Hybrid-Huffman compression."* Drag-and-drop or pick a file; the type is
detected automatically and shown before you commit. Press **Compress File** and
the result reports the original name and size, the `.afc` name and size, space
saved, compression ratio, compression time, backend used and the SHA-256
verification status, with a download button for the `.afc`.

**Decompress** (`/decompress`) — *"Restore an AFC file to its original format
and verify its integrity."* Drop a `.afc` and press **Decompress File**. The
result reports the AFC name and size, the restored name and size, decompression
time, the detected container format and mode, an integrity check and a SHA-256
result, with a download button for the restored original.

Each page refuses the other's input rather than silently switching operation: a
`.afc` dropped on Compress, or a normal file dropped on Decompress, gets a
message and a link to the correct page.

**Restoring the original filename.** An AFC container stores
`magic | mode | original_length | payload` and no filename, so the extension is
recovered by sniffing the restored bytes (`filetypes.py`). That is why
`MyDocument.afc` comes back as `MyDocument.pdf`. Packaged formats — PDF, DOCX,
XLSX, PPTX, ODF — are handled whole and automatically; you never extract PDF
page streams or DOCX package parts yourself.

> **This split is UI/UX only.** Both pages call the same
> `afc2.compress_bytes` / `afc2.decompress_bytes` entry points the combined
> page always used. There is no second engine, no duplicated pipeline and no
> format-specific compressor — `filetypes.py` only *names* bytes, and a test
> asserts it defines no compression function and imports no codec.

### Multi-file features

**Queue and batch** (`/compress` → *Queue & batch*). Drop or pick multiple
files. Each row shows name, size, status and progress; queued files can be
removed with the *remove* link before you press **Start queue**. Files are
processed one at a time. When the run finishes, a results table appears with
per-file metrics — click any column heading to sort — plus a totals row and
per-file download links.

**Folder → archive** (`/compress` → *Folder → archive*). Pick a folder or a
set of files, name the archive, and press **Create archive** to get one
`.afcpak`. Each file inside is compressed independently and SHA-256 verified
before it is written; folder structure is preserved.

**Extract archive** (`/decompress` → *Extract archive*). Pick a `.afcpak`;
every member is decompressed, its stored SHA-256 re-checked, and listed with
its original path for download. (Extraction lives on the Decompress page
because it is a decompression operation; archives are *created* on Compress.)

**Reports.** *Download CSV* / *Download PDF* on the dashboard, the Files page,
or a finished batch. The PDF button opens a print-styled page — use your
browser's "Save as PDF". Batch buttons export only that run
(`?batch_id=…`); the dashboard buttons export your whole history.

**Files & History** (`/files`). Every file you have processed, sortable, with
export buttons.

**Admin** (admin role only): `/admin/users` lists accounts with per-user
totals and lets you enable/disable an account; `/admin/audit` shows logins,
failed logins and admin actions. Non-admins receive a 403.

## Analytics & algorithm showcase (Part 2)

**Analytics** (`/analytics`) — stat cards (files, average ratio, space saved,
estimated transfer time saved), a ratio-over-time chart comparing AFC against
gzip and single-tier Huffman, a file-type breakdown, and a system status panel.
Admins get a *system-wide* checkbox; everyone else sees only their own rows.

> The gzip and Huffman lines are **reference measurements shown for context**.
> gzip is an LZ77+Huffman codec and is never used to produce output here. Where
> AFC is behind, the chart shows it. References are measured only for files up
> to `REFERENCE_CODEC_MAX_BYTES` (8 MB); larger files leave a gap in the chart
> rather than a fabricated value.

**Compare** (`/compare`) — pick two history entries and diff sizes, ratio,
saved %, duration, engine, container and preset side by side.

**Files & History** (`/files`) — filename search, date range, engine and preset
filters, sortable columns and pagination, all server-side.

**Before you compress** — selecting a file immediately shows an **estimated
compressibility** (Shannon entropy over the byte histogram, with a top-byte
chart) and a **preview** (text head, or hex dump for binaries). The entropy
figure is an *estimate*: it is per-byte, and AFC usually beats it because
structural blocks capture multi-byte structure a byte histogram cannot see.

**After you compress** — a **plain-language explainer** ("Saved 59.5%: 92% of
the savings came from 1052 structural blocks…"), a **hybrid Huffman tree**
with literal-byte leaves in gold and structural-block leaves in maroon, a
code-length distribution, and a bar showing where the savings came from. All
of it is measured by reading the produced container; the tree depth is
adjustable with `?depth=` on `/api/tree/<token>` (4–12, default 9).

### Compression presets

| Preset | Effect | Backend |
|---|---|---|
| **Fast** | fewer candidates, no optimal parsing, fewer growth rounds | **C++ native** |
| **Balanced** *(default)* | engine defaults, best ratio-to-time trade | **C++ native** |
| **Maximum** | larger candidate search, more DP and growth rounds — usually smaller, but see the note | **C++ native** |

> **Maximum is not always smaller than Balanced.** Measured over the full
> corpus it wins on five files (best −3.65%) and loses on two (`data.csv`
> +3.46%, `code_python.py.txt` +0.07%). Earlier documentation claimed it was
> never larger; that was generalised from three files and has been corrected.
> A deeper block-growth search can admit blocks that pass the Bit Cost
> Decision Engine's estimate but crowd the dictionary. Details in `presets.py`.

> **[v7] All three presets now run natively.** The C++ core used to compile its
> tuning constants in and ignore the Python values, so Fast and Maximum were
> forced onto the pure-Python path. `afc_native.cpp` gained an
> `afc_compress_ex` entry point that carries the four tunables, and
> `afc2.compress_bytes` forwards them. Python remains the reference: all three
> presets produce **byte-identical** output on both backends (verified over 10
> files x 3 presets). Wall-clock times are now directly comparable across
> presets. See `CHANGES_v7.md` §1-3 for the root cause and the measurements.

### PDF and DOCX component processing

The user uploads a normal `.pdf` or `.docx`; extraction and routing are fully
automatic.

* **PDF:** object/page-content inventory identifies stream payloads. PDF
  syntax and suitable unfiltered content are pooled into one Hybrid-Huffman
  call; JPEG/JPX and high-entropy payloads stay verbatim. Suitable textual
  `/FlateDecode` page/content streams can be exposed as expanded source plus
  an exact zlib/DEFLATE-token recipe in **AFC6**. No stream is re-deflated.
  For large PDFs, a bounded early viability probe declines AFC6 when sampled
  expanded source plus its exact recipe already exceeds the encoded streams
  by more than 4x; the unchanged AFC3/plain candidates still process every
  original byte. This prevents token analysis for a demonstrably losing
  candidate without changing the codec.
* **DOCX/OOXML:** the ZIP central directory identifies members by their real
  names and methods, including `word/document.xml`. STORED XML is directly
  pooled. For suitable method-8 XML, `deflate_tokens.py` parses the producer's
  existing blocks/tokens into plain XML plus an exact reconstruction recipe;
  both go through Hybrid-Huffman in **AFC4**. It does not search for DEFLATE
  matches or build a second compressed stream.
* **Global guard:** AFC3/AFC4/AFC6 is emitted only when the complete wrapper is
  smaller than the unchanged whole-file AFC1/AFC2 result. Ordinary Word XML
  is often already very compact, so it is correctly preserved when exposing
  it would lose. This is a measured limitation, not hidden as an improvement.

All wrappers reconstruct the original PDF/DOCX bytes exactly; the suite and
document benchmark compare bytes and SHA-256, including two-cycle tests and
old AFC1/AFC2 decoding.

### Light-first interface and dark mode

The 1.3 interface defaults to a brighter high-contrast canvas, clearer cards,
larger upload targets and visible keyboard focus. Dark mode remains available
from the sidebar. The preference lives in `localStorage` only and is never sent
to the server, consistent with the local/non-cloud delimitation.


## File size limits

Read from `config.py` and shown on the Settings page — nothing is hardcoded in
the UI.

| Constant | Default | Env override |
|---|---|---|
| `MIN_FILE_SIZE` | 1 byte | `AFC_MIN_FILE_SIZE` |
| `MAX_FILE_SIZE` | 100 MB | `AFC_MAX_FILE_SIZE` |
| `MAX_BATCH_SIZE` | 500 MB | `AFC_MAX_BATCH_SIZE` |

Defaults match the ceiling the thesis Appendix C documents as tested. **Read
`SIZE_POLICY.md` before raising them** — it has measured 150 MB and 250 MB
results, the memory profile, and the exact sentence to update in the paper.
Note the requested 1 MB minimum was deliberately not adopted: it would reject
every file in the thesis corpus.

## Running the tests

```bash
python tests/test_app.py      # 277 checks: auth/web, native equivalence,
                              # AFC1-AFC6, PDF/DOCX, byte equality + SHA-256
python tools/run_verification.py          # engine round trips (unchanged)
python tools/size_policy_bench.py --quick # size/memory smoke test
```

## Privacy

Everything is local: the app binds to `127.0.0.1`, makes no outbound requests,
and stores only *metadata* in SQLite. Compressed output and extracted files
live in memory for the session and are never written to disk by the server.

---

## Quick start (command line engine)

```bash
python afc2.py compress  input.txt out.afc      # adaptive, smallest container
python afc2.py compress  input.txt out.afc --baseline        # single-tier control
python afc2.py compress  input.txt out.afc --format afc1     # legacy container
python afc2.py decompress out.afc restored.txt
python afc2.py verify    input.txt              # SHA-256 round trip, all modes
python afc2.py benchmark benchmarks/corpus/* --runs 7
```

The C++ core is used automatically when a library is present (or buildable);
otherwise the pure-Python engine runs. Force pure Python with
`AFC_NO_NATIVE=1`.

Python API:

```python
import afc2
blob = afc2.compress_bytes(data, adaptive=True, fmt="auto")   # auto|afc1|afc2
data = afc2.decompress_bytes(blob)
afc2.NATIVE   # True when the C++ core is active
```

---

## Files

| File | Role |
|---|---|
| `afc.py` | container I/O (AFC1 + AFC2), canonical/package-merge Huffman, universal decoder, v1 baseline engine |
| `afc2.py` | v4 adaptive engine: multi-tier scan, Bit Cost Decision Engine, block growth, audit, DP optimal parse, CLI |
| `afc_native.cpp` | C++ core — full pipeline both directions, plus the legacy v3 kernels |
| `afc_native.py` | ctypes bridge; auto-loads or auto-builds the library |
| `afc_engine.js` | JavaScript engine, byte-compatible with the above |
| `AFC_WebApp.html` | standalone browser app (WASM core if present, JS otherwise) |
| `app.py` | Flask app factory + page/API routes (map at top of file) |
| `config.py` | all tunables incl. size policy — the only place limits live |
| `db.py`, `schema.sql` | local SQLite: users, history, audit, login attempts |
| `auth.py`, `admin.py` | auth blueprint (login/roles) and admin blueprint |
| `afcpak.py` | `.afcpak` archive container (manifest + AFC payloads, no DEFLATE) |
| `templates/`, `static/` | responsive dashboard; `compress.html`/`decompress.html` are the two separate workflow pages, with `compress.js`/`decompress.js` driving them and `queue.js` the multi-file panes |
| `filetypes.py` | content-based type detection; recovers the original extension on decompress. Compresses nothing |
| `analysis.py` | read-only entropy / container / tree / attribution analysis |
| `presets.py` | Fast / Balanced / Maximum tunable presets |
| `tests/test_app.py` | 277-check end-to-end suite (native presets, AFC1-AFC6, web, integrity and document paths) |
| `tools/native_doctor.py` | [v7] diagnoses why the native core is or is not loaded |
| `containers.py` | PDF/OOXML inventory, exact tiling, AFC3/AFC4/AFC6 routing and whole-file size guards |
| `deflate_tokens.py` | Reversible parser/serializer for existing DOCX member tokens; makes XML available to Hybrid-Huffman without adding a compressor |
| `tools/` | corpus generator, verification suite, size benchmark, CSS + WASM builds |
| `benchmarks/` | corpus, Canterbury files, harness, v3 snapshot, result CSVs |
| `CHANGES.md` | web app (Part 1) changelog |
| `CHANGES_v4_engine.md` | engine changelog, mapped to thesis terminology |
| `SCOPE_NOTES.md` | constraint compliance + what Part 2 inherits |
| `SIZE_POLICY.md` | measured size/memory limits and the Appendix C sentence |

---

## Build

The native library is **optional** — everything works in pure Python without
it. A verified, static 64-bit Windows build now ships as `afc_kernels.dll`, so
the normal Windows x64/Python x64 checkout uses C++ immediately even when no
compiler is installed. Its provenance, exports, dependencies and SHA-256 are
recorded in `NATIVE_WINDOWS_X64.md`.

On other platforms, or when the prebuilt artifact does not match Python's
architecture, `afc_native.py` tries to build the library once automatically
with `g++`, `clang++` or MSVC and otherwise falls back to the Python reference.
Run `python -m afc_native --diagnose` to verify the backend actually loaded.

### Linux / macOS

```bash
g++ -O3 -std=c++17 -shared -fPIC -pthread afc_native.cpp -o afc_kernels.so
```

### Windows (MinGW-w64)

```bat
g++ -O3 -std=c++17 -shared -static -pthread afc_native.cpp -o afc_kernels.dll
```

`-static` bundles the libstdc++/libgcc/winpthread runtimes so the DLL loads
without MinGW on PATH. Use a 64-bit MinGW-w64 toolchain matching your Python
build (a 32-bit DLL will not load into 64-bit Python). From MSYS2:
`pacman -S mingw-w64-x86_64-gcc`, then build from the *MinGW 64-bit* shell.

The bridge searches for `afc_kernels.dll`, `afc_kernels.so`, then
`libafc_kernels.so` beside `afc_native.py`, and requires the v4
`afc_compress` export — a stale v3-only binary is ignored and rebuilt, so the
engine and the library can never disagree.

### WebAssembly (for `AFC_WebApp.html`)

Requires the [Emscripten SDK](https://emscripten.org) on PATH:

```bash
sh tools/build_wasm.sh
```

which runs:

```bash
emcc -O3 -std=c++17 -DAFC_NO_THREADS \
  -s STANDALONE_WASM=1 -s ALLOW_MEMORY_GROWTH=1 --no-entry \
  -s EXPORTED_FUNCTIONS='["_afc_compress","_afc_decompress","_afc_free","_malloc","_free"]' \
  afc_native.cpp -o afc_core.wasm
```

`-DAFC_NO_THREADS` builds the single-threaded configuration — a `file://`
page cannot send the COOP/COEP headers that SharedArrayBuffer requires.
Output bytes are identical either way.

Place `afc_core.wasm` next to `AFC_WebApp.html`. The page loads it when
present and falls back to `afc_engine.js` when absent; the footer badge names
the active engine. **Note:** wasm is fetched over HTTP, so serve the folder
(`python -m http.server`) if you want the wasm path — opening the file
directly still works via the JavaScript engine.

---

## Web dashboard

```bash
pip install flask
python app.py            # opens http://127.0.0.1:5000
```

Both web apps compress and decompress, but they expose it differently:

* **The Flask dashboard has two separate pages** — `/compress` and
  `/decompress` — each a top-level navigation entry. See *"Compress and
  Decompress are separate pages"* above. Neither page asks you to choose an
  action; the page you are on *is* the action, and each refuses the other's
  input with a link to the right page.
* **The standalone `AFC_WebApp.html` keeps its single-page Action control**
  (auto-detect / compress / decompress). It is one self-contained file with no
  server and no upload, so a single page is the right shape for it. It is
  unchanged by the dashboard split.

Dashboard results show original/restored sizes, space saved, ratio,
compression or decompression time, a SHA-verified lossless badge, the engine
badge (C++ native / pure Python), the container badge (AFC1-AFC6), and a
download link.

> **Offline note:** the dashboard's Tailwind build is local
> (`static/css/tailwind.css`, rebuilt with `sh tools/build_css.sh`), so it
> renders correctly with no internet. `AFC_WebApp.html` has no CDN dependency
> either. Neither app makes an outbound request.

---

## Verification and benchmarks

```bash
python tools/make_corpus.py            # regenerate the deterministic corpus
python tools/run_verification.py       # full suite (native)
AFC_NO_NATIVE=1 python tools/run_verification.py    # same suite, pure Python
python tools/make_report.py --runs 7   # regenerate every CSV
```

The verification suite covers 60 SHA-256 round trips per engine (10 files ×
2 modes × 3 formats), native ↔ Python byte-identity, cross-decoding
Python ↔ native ↔ JavaScript, and edge cases (empty, 1 byte, 64 KB of one
byte, full 256-byte alphabet, input larger than the 1 MB scan window, and an
`.afc` file re-fed as data). Both engines report **ALL CHECKS PASSED**.

Result files in `benchmarks/`:

| CSV | Contents |
|---|---|
| `results_before_after.csv` | v3 vs v4, sizes and median ms, both backends |
| `results_v3_*.csv`, `results_v4_*.csv` | raw per-engine runs |
| `ablation_sizes.csv` | each optimization measured in isolation |
| `reference_codecs.csv` | gzip/bzip2/LZMA — **reference rows only**, not AFC configurations |

### Headline results (adaptive mode, C++ core, median of 7 runs)

| file | orig | v3 | v4 | size | v3 comp | v4 comp | v3 dec | v4 dec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| data.json | 151 159 | 34 071 | **16 704** | −51.0 % | 78.3 ms | 59.9 ms | 0.64 ms | 0.35 ms |
| prose_en.txt | 67 426 | 32 462 | **20 928** | −35.5 % | 56.9 ms | 43.9 ms | 0.62 ms | 0.30 ms |
| server.log | 424 437 | 52 421 | **37 432** | −28.6 % | 198.8 ms | 165.4 ms | 1.15 ms | 0.65 ms |
| universe.sql | 3 973 | 2 810 | **2 042** | −27.3 % | 5.5 ms | 4.6 ms | 0.07 ms | 0.04 ms |
| code_python.py.txt | 41 181 | 22 194 | **17 363** | −21.8 % | 52.9 ms | 48.0 ms | 0.50 ms | 0.31 ms |
| bytes256.bin | 1 024 | 322 | **278** | −13.7 % | 4.0 ms | 3.6 ms | 0.01 ms | 0.03 ms |
| data.csv | 179 858 | 53 386 | **49 431** | −7.4 % | 160.0 ms | 97.2 ms | 1.05 ms | 0.73 ms |
| records.bin | 131 072 | 53 151 | **51 526** | −3.1 % | 111.7 ms | 74.6 ms | 0.92 ms | 0.53 ms |
| random.bin | 65 536 | 65 544 | 65 544 | ±0 | 59.5 ms | 40.5 ms | 0.02 ms | 0.03 ms |
| tiny.txt | 32 | 38 | 38 | ±0 | 0.15 ms | 0.48 ms | 0.00 ms | 0.01 ms |

Baseline mode gains most in speed, since the whole pipeline moved native:
`server.log` 113.7 → 2.86 ms (**39.7×**), `data.csv` 44.6 → 1.32 ms
(**33.8×**), `code_python.py.txt` 11.2 → 0.57 ms (**19.7×**).

**No corpus file is larger under v4 than under v3, in either mode, in any
container format.**

### Real Canterbury Corpus (adaptive, native, median of 7 runs)

The five standard Canterbury files ship under `benchmarks/canterbury/` and run
through the same harness (all 30 combinations pass SHA-256 round trips and
Python↔native↔JS cross-decode). Full data in
`benchmarks/canterbury_before_after.csv` and
`benchmarks/canterbury_reference_codecs.csv`.

| file | orig | v3 | v4 | size | gzip -9 | bzip2 | lzma |
|---|---:|---:|---:|---:|---:|---:|---:|
| alice29.txt | 152 089 | 77 407 | **61 692** | −20.3 % | 54 182 | 43 202 | 48 492 |
| asyoulik.txt | 125 179 | 66 774 | **54 741** | −18.0 % | 48 790 | 39 569 | 44 536 |
| cp.html | 24 603 | 14 864 | **10 869** | −26.9 % | 7 952 | 7 624 | 7 644 |
| fields.c | 11 150 | 7 455 | **5 330** | −28.5 % | 3 127 | 3 039 | 3 028 |
| grammar.lsp | 3 721 | 2 350 | **1 927** | −18.0 % | 1 234 | 1 283 | 1 292 |

v4 beats v3 on every file (18–28 % smaller). **But be clear about the
reference codecs:** on this text/code corpus AFC v4 is ~2.35× overall and
remains **larger than gzip/bzip2/lzma** (total 134 559 B vs gzip 115 285) —
because these files are full of repeated phrases at varying offsets, which
back-reference codecs exploit and AFC, by design, does not. The v4 work closes
about a third of the v3→gzip gap but does not overtake the LZ family here.
AFC's mechanism wins where data is highly templated (the synthetic
`server.log`, `data.json`, and `prose_en.txt` where it beats gzip/lzma); on
free-form prose and C source it does not, and the numbers above are reported
without cherry-picking.

Silesia was requested but is unreachable from this environment (proxy
allowlist blocks all non-registry hosts). Drop Silesia files into
`benchmarks/silesia/` and re-run `tools/make_report.py`; the harness already
targets that folder and emits `silesia_*` CSVs with the same guarantees.

### Known trade-off — reported, not hidden

The **pure-Python adaptive** path is slower than v3 (e.g. `server.log`
628 → 2410 ms), because the DP optimal parse and its 3 parse↔tree iterations
are expensive in interpreted code. It buys exactly the same size reductions,
and the native path absorbs the cost and still ends up faster than v3. Pure
Python remains the correctness reference and the no-toolchain fallback; use
the native core for throughput. Pure-Python *decode* is faster than v3
everywhere (`server.log` 24.2 → 17.5 ms), and pure-Python baseline mode is
unchanged.

---

## Container formats

Both start `magic | mode u8 | varint original_length`; `mode 0` means raw
stored bytes (the fallback that guarantees incompressible input never
inflates beyond the header).

**AFC1** (legacy, unchanged): dictionary as varint-length + raw bytes, then
the code table as varint symbol id + 1 byte length per symbol, then the
bitstream.

**AFC2** (v4): symbol ids delta+varint coded, code lengths packed 4 bits each,
dictionary entries optionally coded with the literal Huffman codes (flag byte
picks whichever is smaller), then the bitstream. Selected by `--format afc2`,
or by `--format auto` when it is strictly smaller.

**AFC3** (component-aware): a segment manifest, verbatim opaque bytes, and one
ordinary AFC1/AFC2 inner container for pooled PDF/OOXML structure.

**AFC4** (DOCX XML component-aware): extends the outer manifest with exact
transformed-member records. Plain XML and its reversible token recipe live in
the ordinary AFC1/AFC2 inner Hybrid-Huffman container.

**AFC5** (self-verifying envelope): stores original length and SHA-256, payload
length and SHA-256, and a safe original basename around an AFC payload. This
makes verification self-contained instead of relying on database history.

**AFC6** (PDF Flate component-aware): uses transformed records for eligible
RFC-1950 zlib-wrapped page/content streams. Expanded source plus the producer's
exact zlib/DEFLATE-token recipe goes through Hybrid-Huffman; images, fonts and
unsupported/high-entropy streams remain byte-for-byte raw.

`afc2.decompress_bytes` and the Flask app read AFC1-AFC6. The legacy
`afc.py`, JavaScript and native core remain AFC1/AFC2 decoders; they reject the
new outer magics cleanly instead of misinterpreting them.
