# CHANGES_v7.md — native acceleration for every preset, container-aware PDF/DOCX

V7 adds the two engine features V6 was missing. It does not rebuild the web
application, redesign the UI, or replace the Hybrid-Huffman algorithm.

* **Python remains the reference implementation.** C++ accelerates the same
  algorithm and is verified against Python on every preset.
* **No compression algorithm other than Hybrid/canonical Huffman was
  introduced.** No DEFLATE, ZIP, LZ, arithmetic, range, ANS, BWT, PPM or ML.
  Asserted structurally by AST tests, not by inspection.

---

## 0. The `dickens` performance bug — why Balanced ran on pure Python

**Reported:** a 9.72 MB file, preset Balanced, 3.61 MB out (62.82%), **138.23 s**,
backend "pure Python".

**Reproduced.** Silesia is unreachable from this environment, so the test used a
byte-for-byte size match (10 192 446 B) built from the repository's own English
text. On the pure-Python path it took **128.23 s** — close enough to 138 s to
confirm the same code path and the same scenario.

**The cause is not preset routing.** Balanced was already the one preset V6
allowed on the native core, and `presets.uses_native("balanced")` returns True.
The backend said "pure Python" for the only remaining reason: `afc_native.AVAILABLE`
was False, so `afc2.NATIVE` was False and *every* preset ran interpreted.

**The real defect is that the loader never said so.** V6's `afc_native.py`
swallowed every failure:

```python
except OSError:
    pass                       # wrong architecture, missing runtime DLL — silent
...
if not hasattr(lib, "afc_compress"):
    continue                   # stale binary — silent
...
r = subprocess.run(cmd, capture_output=True)
return r.returncode == 0 and _load()   # g++ stderr discarded
except FileNotFoundError:
    return False               # g++ not on PATH — silent
```

Any of these produced an engine that was ~12x slower with byte-identical
output. The user sees "the compressor is slow", not "the native library did not
load". On Windows the likely chain is: no `afc_kernels.dll` is shipped →
auto-build tries `g++` → not on PATH → `FileNotFoundError` → silent fallback.

**Fix.** Every step now records its outcome. `afc_native.DIAGNOSTICS`,
`REASON`, `LIBRARY_PATH` and `report()` explain the decision; the loader also
reads the PE/ELF header and refuses an architecture-mismatched library with a
message naming the mismatch (a 32-bit MinGW DLL silently failing to load into
64-bit Python is a classic Windows case whose OSError text does not say so).
`g++` stderr is captured and reported.

Surfaced in three places so it cannot stay hidden:

* `python tools/native_doctor.py` — full diagnosis plus the exact build command;
* `GET /api/status` — `native_available`, `native_reason`, `native_library`,
  `native_diagnostics`, `preset_backends`;
* the **Settings page**, which states the active backend, the reason, and the
  per-preset backend.

Verified by simulation: with no library and no `g++`, the loader reports
*"g++ is not on PATH, so the native library cannot be built automatically…"*;
with a 32-bit DLL and 64-bit Python it reports the architecture mismatch,
skips it, and rebuilds a working library rather than failing.

**Result on the same file, preset Balanced:**

| metric | pure Python | C++ native | factor |
|---|---:|---:|---:|
| compressed bytes | 3 574 764 | 3 574 764 | **identical** |
| space saved | 64.93% | 64.93% | — |
| ratio | 2.85x | 2.85x | — |
| compression time | **128.23 s** | **11.10 s** | **11.6x faster** |
| decompression time | 2.32 s | 0.07 s | 31.9x faster |
| peak RSS | 828.8 MB | 212.6 MB | 3.9x less |
| SHA-256 lossless | yes | yes | — |

The compression result is preserved exactly — the 62.82%-class ratio is not
traded away for the speed-up.

## 1. Root cause of the Fast/Maximum Python fallback

Two mechanisms, one underlying cause.

**The cause:** `afc_native.cpp` declared the four preset-controlled tunables as
file-scope constants —

```cpp
static const int MIN_CANDIDATE_FREQ = 4;   // line 52
static const int MERGE_ROUNDS_V4    = 6;   // line 57
static const int DP_ROUNDS          = 3;   // line 59
```

— and the exported ABI had no parameters for them:

```c
int afc_compress(const uint8_t* data, uint32_t n, int adaptive, int fmt,
                 void** out, uint32_t* outn);
```

So the native core physically could not honour a preset. The V6 docs verified
this by measurement: alice29.txt at `DP_ROUNDS` 1, 3 and 6 produced
byte-identical native output (61 692 B each time).

**How that surfaced:**

1. `afc2.compress_bytes` gated the native path on `all(OPTS.values())`. Fast
   sets `OPTS["dp"] = False`, so Fast fell to Python on that check alone.
2. Maximum leaves every `OPTS` flag True, so it would have gone native and
   *silently produced Balanced output*. To prevent that,
   `presets.compress_with` explicitly forced `afc2.NATIVE = False` for any
   non-default preset.

Interestingly, `compress_core` in C++ **already took `min_freq` and `rounds`
as arguments** — the small-file tuning trial calls it twice with different
floors. Only `DP_ROUNDS` and the dp on/off switch were unreachable. The fix
was therefore much smaller than the V6 documentation implied.

## 2. C++ changes

`afc_native.cpp`:

* Added a `Params` struct (`dp`, `dp_rounds`, `merge_rounds`, `min_freq`,
  `tune`) whose defaults reproduce the previous compiled-in behaviour exactly.
* `compress_core` takes `const Params&` and uses `P.dp_rounds`.
* **The DP branch now mirrors Python precisely.** In `afc2._compress_core`,
  when `OPTS["dp"]` is false the DP loop *and the second `final_audit`* are
  both skipped. The C++ version ran `final_audit` unconditionally. Guarding
  only the loop would have changed Fast's output; both statements are inside
  the branch.
* `afc_compress_impl` carries the params through the small-file tuning trial,
  which now uses `P.min_freq` / `P.min_freq - 1` and respects `P.tune`.
* New export `afc_compress_ex(data, n, adaptive, fmt, dp, dp_rounds,
  merge_rounds, min_freq, tune, out, outn)`.
* **`afc_compress` keeps its original signature and behaviour** and simply
  calls the impl with default `Params`. A pre-V7 caller or binary is
  unaffected.

`afc_native.py`: exposes `TUNABLE` (whether the loaded library exports
`afc_compress_ex`) and an optional `params` argument on `compress()`.

`afc2.py`: `_native_params()` forwards the live tunables;
`_NATIVE_FIXED_OPTS` records the three toggles the native core still has
hard-wired on (`llhuff`, `hdr2`, `refund`) — with any of those disabled the
run still uses Python.

`presets.py`: `uses_native()` returns True for all three presets when the
loaded library is new enough, and falls back to V6 behaviour when it is not.

**Graceful degradation:** with a stale `.so`, `TUNABLE` is False, Fast and
Maximum return to the pure-Python path, and output is unchanged. A missing
feature never becomes a wrong result.

## 3. Verified equivalence

All three presets produce **byte-identical containers** on both backends, over
10 corpus files x 3 presets = 30 combinations, each also SHA-256 round-tripped:

| file | preset | Python | C++ | identical |
|---|---|---:|---:|---|
| data.json | fast / balanced / maximum | 31104 / 16704 / 16095 | 31104 / 16704 / 16095 | yes |
| prose_en.txt | fast / balanced / maximum | 27265 / 20928 / 20894 | same | yes |
| fields.c | fast / balanced / maximum | 6052 / 5330 / 5330 | same | yes |
| cp.html | fast / balanced / maximum | 12781 / 10869 / 10837 | same | yes |
| records.bin | fast / balanced / maximum | 51651 / 51526 / 50685 | same | yes |
| random.bin | all three | 65544 | 65544 | yes |

Presets stay distinct: Fast is always larger than Balanced, and Maximum
searches harder. Maximum is *usually* but **not always** smaller than
Balanced — see §7.3 for the two files where it is not, and the correction to
the earlier documentation that claimed otherwise.

## 4. Container-aware PDF and DOCX

New module `containers.py`. The user still uploads `MyDocument.pdf` and clicks
Compress; everything below is internal.

### The design decision that makes it safe

The analysis produces an ordered list of segments that **exactly tile** the
original byte range — no gaps, no overlaps, first starts at 0, last ends at
`len(data)`. `_validate_tiling()` asserts this before anything is written.

Reconstruction is then a concatenation of those segments, so it is byte-exact
**by construction**. It does not depend on the PDF or ZIP parser being
semantically correct: a parser that misunderstands a file produces a worse
*ratio*, never a wrong *result*. Anything unrecognised stays inside an opaque
segment. That is what allows a losslessness claim on arbitrary real-world
documents without implementing the full PDF or ZIP specification.

### Classification

| Component | Treatment |
|---|---|
| PDF object syntax, dictionaries, xref, trailer, unfiltered content streams | pooled → Hybrid-Huffman |
| PDF `/DCTDecode` (JPEG), `/JPXDecode`, `/FlateDecode`, `/CCITTFaxDecode` payloads | preserved verbatim |
| ZIP local headers, filenames, extra fields, central directory, EOCD | pooled → Hybrid-Huffman |
| ZIP STORED member payloads (raw XML) | pooled → Hybrid-Huffman |
| ZIP DEFLATE member payloads, media | preserved verbatim |

Classification is by cheap entropy probe rather than by trusting the declared
filter, so a mislabelled stream cannot cause expensive work on random data.
All pooled segments are concatenated into **one** engine call, so a pattern
recurring across several page streams is discovered once and shared.

### What is deliberately NOT done

**DOCX members are never inflated and re-deflated.** That would (a) introduce
DEFLATE as a second stage, which the brief forbids, and (b) risk byte-
exactness, because re-deflating is not guaranteed to reproduce the original
bytes. Deflated payloads are therefore preserved verbatim.

**This bounds what is achievable on a Word-generated DOCX, and the numbers
below show it honestly.** The XML inside such a file is already deflate-
compressed and cannot be reached losslessly. Where the XML *is* reachable —
`docx_stored.docx`, a package written with STORED members — the existing
engine compresses it by **90.2%** (184 151 → 18 025 B), which is the evidence
that the engine exploits that redundancy when it can get to it.

### AFC3

```
"AFC3" | u8 mode(3) | varint original_length | varint segment_count
       | segment_count x varint (length << 1 | kind)     kind: 0 opaque, 1 pooled
       | varint opaque_length   + raw bytes
       | varint pooled_length   + an ordinary AFC1/AFC2 container
```

AFC1 and AFC2 are untouched; every existing decoder still reads them.
`afc2.decompress_bytes` dispatches on magic **before** anything else, so an old
container is never reinterpreted under the new format, and the old decoder
raises on AFC3 rather than misreading it.

AFC3 is emitted **only when it is smaller than the plain whole-file
container** (§27). It is never chosen because one component shrank.

## 5. Two bugs found by testing, and fixed

Both were found by driving the real UI in a browser, not by unit tests:

1. **False integrity failure.** The Decompress page read the declared length
   via `analysis.parse_container`, which transparently unwraps AFC3 to the
   inner container — whose declared length covers only the pooled components.
   Comparing that against the whole restored file reported `✗ FAILED` on files
   that were byte-perfect. Fixed with `containers.header_info()`, which reads
   the AFC3 header's own whole-file length.
2. **Wildly overstated saving.** `analysis.explain()` unwrapped the blob and
   then paired the *inner* container size with the *whole-file* original size,
   reporting "Saved 98.4%" for a PDF that actually saved 4.47%. Fixed by
   keeping the outer length and only ever comparing two figures that describe
   the same thing.

Both now have regression tests.

## 6. Performance work

* **Fast path for incompressible data (§29).** A 256-byte printable-character
  probe answers the common case before the 256-bucket entropy scan runs. On a
  388 KB text PDF with hundreds of uncompressed content streams the entropy
  scans alone had cost ~83 ms to discover there was nothing opaque to find.
* **Entropy histogram** uses 256 C-level `bytes.count()` scans instead of a
  per-byte Python loop (~0.21 ms → negligible per segment; a PDF with 81
  stream segments paid that 81 times).
* **Viability gate.** The container path is only attempted when it could
  plausibly pay for itself (≥ 8 KB opaque, ≥ 512 B pooled, ≥ 25% opaque).
  Without it, a small DOCX paid for a second full compression to discover a
  0.00% gain — measured at 1.6x the time for no benefit.
* **Whole-file comparison skip** for large, ≥ 90% opaque files, where the
  comparison pass would burn time on data already proven incompressible.

An earlier version of the size guard used a 0.35 opaque threshold and shipped
containers *larger* than V6's on three files (docx_tables 5933 vs 5821). The
threshold is now 0.90 plus a 64 KB floor, and a test asserts V7 is never larger
than V6 on any document.

## 7. Measured results

All numbers below are real measurements on this machine, median of 3 runs, every
row SHA-256 verified. Raw data: `benchmarks/v7_preset_matrix.csv` (60 rows) and
`benchmarks/v7_documents.csv` (14 rows).

### 7.1 Speed — C++ native vs pure Python, all three presets

Compression, median ms. **Overall: 41.0 s → 4.4 s, a 9.3x speed-up**; per-row
median 10.6x, range 2.9x–23.7x.

| file | preset | Python ms | C++ ms | speed-up |
|---|---|---:|---:|---:|
| server.log | fast | 844.4 | 35.6 | **23.7x** |
| code_python.py.txt | maximum | 2563.9 | 168.0 | 15.3x |
| code_python.py.txt | balanced | 1010.7 | 69.7 | 14.5x |
| data.json | fast | 284.1 | 20.7 | 13.7x |
| cp.html | balanced | 503.9 | 37.9 | 13.3x |
| server.log | balanced | 2963.9 | 256.6 | 11.6x |
| data.csv | balanced | 1489.4 | 138.4 | 10.8x |
| data.json | balanced | 984.5 | 92.5 | 10.6x |
| alice29.txt | balanced | 2286.9 | 279.3 | 8.2x |
| records.bin | balanced | 833.1 | 106.1 | 7.9x |
| random.bin | fast | 165.1 | 57.5 | 2.9x |

The V6 situation for comparison: Fast and Maximum could only ever run the
Python column. Those rows are now the C++ column.

### 7.2 Memory — peak RSS attributable to one compression

| file | preset | Python KB | C++ KB | reduction |
|---|---|---:|---:|---:|
| server.log | balanced | 30 080 | 9 016 | **3.34x** |
| server.log | maximum | 30 188 | 9 016 | 3.35x |
| data.csv | maximum | 16 308 | 5 384 | 3.03x |
| data.csv | balanced | 15 812 | 5 396 | 2.93x |
| data.json | balanced | 11 532 | 4 224 | 2.73x |
| prose_en.txt | maximum | 6 940 | 2 936 | 2.36x |
| alice29.txt | maximum | 15 912 | 7 108 | 2.24x |
| records.bin | balanced | 13 272 | 9 668 | 1.37x |
| fields.c | fast | 1 588 | 1 904 | 0.83x |

On the smallest files (fields.c 11 KB, cp.html 24 KB) the native path's peak is
slightly *higher* — the allocation is dominated by fixed-size working buffers
rather than by the input. Reported rather than trimmed from the table.

### 7.3 Compression ratio by preset (native; identical on both backends)

| file | fast | balanced | maximum | max vs balanced |
|---|---:|---:|---:|---:|
| data.json | 31 104 | 16 704 | 16 095 | **−3.65%** |
| records.bin | 51 651 | 51 526 | 50 685 | −1.63% |
| server.log | 55 576 | 37 432 | 36 851 | −1.55% |
| cp.html | 12 781 | 10 869 | 10 837 | −0.29% |
| prose_en.txt | 27 265 | 20 928 | 20 894 | −0.16% |
| alice29.txt | 69 688 | 60 836 | 60 833 | −0.00% |
| fields.c | 6 052 | 5 330 | 5 330 | 0.00% |
| random.bin | 65 544 | 65 544 | 65 544 | 0.00% |
| code_python.py.txt | 20 061 | 17 363 | 17 376 | **+0.07%** |
| data.csv | 52 362 | 49 431 | 51 143 | **+3.46%** |

**Reported, not hidden:** Maximum is *larger* than Balanced on two of ten
files. Earlier documentation claimed it was "never larger than Balanced on the
tested corpus"; that generalised from three files and has been corrected in
`presets.py` and the README. This is not a V7 regression — the parameters are
unchanged and V7 Maximum output is byte-identical to V6 Maximum output.

### 7.4 PDF / DOCX — whole-file result

The headline number is always the final AFC size of the whole file.

| file | original | V6 AFC | V7 AFC | V7 vs V6 | V6 ms | V7 ms | saved |
|---|---:|---:|---:|---:|---:|---:|---:|
| pdf_text_and_images.pdf | 235 584 | 174 052 | **169 826** | **−2.43%** | 384 | 483 | 27.91% |
| pdf_word_flate.pdf | 25 999 | 18 821 | **18 514** | −1.63% | 44 | 70 | 28.79% |
| pdf_images_only.pdf | 255 512 | 245 834 | **244 079** | −0.71% | 487 | **41** | 4.47% |
| pdf_flate_and_images.pdf | 181 168 | 175 009 | **174 086** | −0.53% | 331 | **27** | 3.91% |
| docx_text_and_images.docx | 128 247 | 128 158 | **128 053** | −0.08% | 194 | **21** | 0.15% |
| docx_images.docx | 205 171 | 204 953 | **204 808** | −0.07% | 348 | **26** | 0.18% |
| docx_text_large.docx | 16 412 | 16 420 | 16 418 | −0.01% | 29 | 38 | −0.04% |
| docx_stored.docx | 184 151 | 18 025 | 18 025 | 0.00% | 161 | 170 | **90.21%** |
| pdf_text_large.pdf | 388 414 | 30 872 | 30 872 | 0.00% | 468 | 461 | **92.05%** |
| pdf_text_multipage.pdf | 97 270 | 10 891 | 10 891 | 0.00% | 104 | 99 | 88.80% |
| pdf_text_small.pdf | 5 210 | 1 500 | 1 500 | 0.00% | 9 | 9 | 71.21% |
| docx_tables.docx | 5 960 | 5 821 | 5 821 | 0.00% | 17 | 17 | 2.33% |
| docx_text_multipage.docx | 6 593 | 6 568 | 6 568 | 0.00% | 11 | 14 | 0.38% |
| docx_text_small.docx | 4 069 | 4 026 | 4 026 | 0.00% | 9 | 11 | 1.06% |
| **TOTAL** | 1 739 760 | 1 040 950 | **1 033 487** | **−0.72%** | | | |

V7 is smaller on 7 files, larger on **0**, unchanged on 7. All 14 reconstruct
byte-for-byte.

**Speed:** media-heavy documents get dramatically faster because the opaque
regions skip candidate discovery, block growth and the DP parse entirely —
`pdf_images_only` 487 → 41 ms (11.8x), `docx_images` 348 → 26 ms (13.4x). The
two mid-opacity PDFs are ~1.2–1.6x *slower*, because both the container path
and the plain path are compressed so the smaller can be chosen; that time buys
the −2.43% and −1.63%. Text-only PDFs are unchanged within noise.

### 7.5 Component breakdown

| file | segments | pooled → Hybrid-Huffman | preserved verbatim | components |
|---|---:|---:|---:|---|
| pdf_images_only.pdf | 13 | 15 512 | 240 000 | /DCTDecode x6, pdf-structure x7 |
| pdf_text_and_images.pdf | 9 | 75 584 | 160 000 | /DCTDecode x4, pdf-structure x5 |
| pdf_word_flate.pdf | 81 | 10 424 | 15 575 | /FlateDecode x40, pdf-structure x41 |
| pdf_flate_and_images.pdf | 69 | 9 478 | 171 690 | /DCTDecode x4, /FlateDecode x30 |
| pdf_text_large.pdf | 1 | 388 414 | 0 | pdf-structure (nothing opaque) |
| docx_images.docx | 17 | 2 581 | 202 590 | zip-stored x5, zip-deflate x3, zip-header x8 |
| docx_text_and_images.docx | 13 | 2 328 | 125 919 | zip-stored x3, zip-deflate x3 |
| docx_stored.docx | 1 | 184 151 | 0 | zip-structure (all reachable XML) |

### 7.6 The honest DOCX result

A Word-generated DOCX gains **0.00–0.18%**, because every XML part inside it is
already deflate-compressed and cannot be reached without inflating and
re-deflating — which would introduce DEFLATE as a second stage and could not
guarantee byte-exact reconstruction.

The engine is not the limitation. `docx_stored.docx` is the same content in a
package whose members are STORED, so the XML *is* reachable, and Hybrid-Huffman
compresses it **90.2%** (184 151 → 18 025 B). That is the measurement behind
the brief's ~80% figure, and it confirms the redundancy is there — it is simply
locked behind DEFLATE in a normal Word file.

PDFs are different, and that is where container-awareness pays: PDF structural
syntax is stored in the clear, so pooling it away from the JPEG/Flate payloads
produces real gains (up to −2.43%) and large speed-ups.

## 8. Files

**Added:** `containers.py`, `CHANGES_v7.md`, `tools/native_doctor.py`,
`tools/preset_bench.py`,
`tools/doc_bench.py`, `tools/make_doc_corpus.py`, `benchmarks/documents/*`,
`benchmarks/v7_preset_matrix.csv`, `benchmarks/v7_documents.csv`.

**Modified:** `afc_native.cpp` (params + new export), `afc_native.py` (bridge),
`afc2.py` (tunable forwarding, container dispatch, AFC3 decode), `presets.py`
(all presets native), `analysis.py` (AFC3 unwrapping, explainer fix), `app.py`
(AFC3 recognition, integrity fix), `filetypes.py`, `static/js/compress.js`,
`static/js/decompress.js`, `templates/decompress.html`, `tests/test_app.py`.

**Preserved unchanged:** `afc.py`, `afc_engine.js`, `afcpak.py`, `config.py`,
`schema.sql`, `db.py`, `auth.py`, `admin.py`, every template except the one
line above, the dashboard, analytics, history, compare, settings, the separate
Compress/Decompress pages, authentication, the audit log and every existing
API endpoint.

**Note on scope:** V6's `SCOPE_NOTES.md` §1 claimed the engine files were
byte-for-byte unchanged. That was true for Parts 1-2. V7 changes `afc2.py` and
`afc_native.cpp` deliberately, because the brief requires extending the native
interface. `afc.py` (containers, canonical Huffman, the universal decoder) and
`afc_engine.js` remain untouched, and the algorithm itself is unchanged — only
the plumbing that carries parameters into it.

## 8.5 PDF object inventory

`containers.pdf_components()` walks `N G obj … endobj` records, reads each
object dictionary, resolves `/Type /Page` → `/Contents N 0 R`, and names each
stream: **page-content, image, font, metadata, objstm, content-stream**, along
with its declared filter. Measured on the corpus:

| file | objects | streams | compressed | preserved | stream kinds |
|---|---:|---:|---:|---:|---|
| pdf_text_large.pdf | 324 | 160 | 160 | 0 | page-content x160 |
| pdf_text_and_images.pdf | 68 | 34 | 30 | 4 | page-content x30, image x4 (all preserved) |
| pdf_images_only.pdf | 22 | 12 | 6 | 6 | image x6 preserved, page-content x6 compressed |
| pdf_word_flate.pdf | 84 | 40 | 0 | **40** | page-content x40, all /FlateDecode → preserved |
| pdf_flate_and_images.pdf | 68 | 34 | 0 | 34 | image x4 + /FlateDecode page-content x30 |

`pdf_word_flate.pdf` is the filter-awareness case (§6): every page stream is
already `/FlateDecode`-compressed, so all 40 are preserved rather than fed to
Hybrid-Huffman a second time. Classification is still decided by the entropy
probe rather than by trusting the declared filter, so a mislabelled stream
cannot send incompressible data into the expensive path.

## 9. Verification

```
tests/test_app.py          214 passed, 0 failed   (V6 had 165; 49 new)
tools/run_verification.py  ALL CHECKS PASSED      (60 round trips, cross-decode)
tools/doc_bench.py         14/14 byte-exact, SHA-256 verified
tools/preset_bench.py      60/60 rows verified lossless
```

New V7 assertions include: byte-identity across backends for every preset,
preset distinctness, Python↔C++ cross-decoding, exact segment tiling on every
document, byte-exact PDF/DOCX reconstruction, AFC3 never larger than plain,
two-cycle reconstruction, AFC1/AFC2 backward compatibility, old decoders
rejecting AFC3, corrupt-container safety, and `containers.py` importing no
codec and defining no compressor.
