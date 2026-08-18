# CHANGES_v8.md — exact DOCX XML/token processing and AFC4

V8 continues the existing Hybrid-Huffman implementation. It does not replace
the Python reference algorithm, add a second AFC compressor, rebuild the web
application, or remove AFC1/AFC2/AFC3 behavior.

## 1. Current-branch audit and native status

The audit began on default branch `claude/hybrid-huffman-system-fuhn15` at
commit `9efcde7` before source changes.

The initial Windows checkout had no `afc_kernels.dll` and no `g++`, `clang++`
or `cl` on `PATH`. `tools/native_doctor.py` therefore correctly reported all
three presets as pure Python. With a portable x64 GCC available, the unchanged
loader built a 64-bit static DLL, loaded it, found `afc_compress_ex`, and
reported Fast/Balanced/Maximum as C++ native. This was an environment/build
availability issue, not an algorithm or preset-routing defect.

Measured on `benchmarks/large/dickens_surrogate.txt` (10,437,251 bytes), median
of three native runs; raw CSV: `benchmarks/v8_dickens_native.csv`:

| preset | backend | AFC bytes | saved | compression | decompression | SHA-256 |
|---|---|---:|---:|---:|---:|---|
| Fast | C++ native | 3,978,304 | 61.88% | 1,196.28 ms | 64.70 ms | match |
| Balanced | C++ native | 3,619,716 | 65.32% | 11,096.77 ms | 61.73 ms | match |
| Maximum | C++ native | 3,556,007 | 65.93% | 29,993.81 ms | 60.71 ms | match |

The old 138/335-second screenshots were genuine pure-Python timings, but they
do not describe this built current branch. No algorithm tuning was changed in
response to those old timings.

## 2. Baseline defects found

The untouched suite produced 215 passes and three failures. All three were
contradictory preset assertions: an older test still required Maximum to be
smaller than Balanced, while the newer code/docs and current measurement on
`cp.html` showed 11,232 versus 11,200 bytes. Tests now assert the actual preset
contract (more DP/merge search), not unsupported monotonic output sizes.

The audit also found that `docx_stored.docx` was generated with ZIP method 8,
compression level 0. Its 90.2% result was real, but the historical claim that
it was a method-0 STORED package was not. V8 documents the correction and adds
`docx_zip_stored.docx`, whose eight XML members genuinely use method 0.

## 3. PDF path retained and hardened

V7 already provided real PDF component processing:

* object inventory resolves `/Type /Page` and `/Contents` references;
* structural syntax and suitable unfiltered page/content streams are pooled;
* JPEG/JPX/Flate and high-entropy payloads are preserved verbatim;
* exact segment tiling guarantees byte reconstruction; and
* AFC3 is selected only after a final whole-file size comparison.

V8 retains that design. AFC3 decoding now additionally rejects empty segments,
length-total mismatches, truncated pooled components and trailing payload data.

## 4. DOCX member inventory

`containers.zip_components()` reads the ZIP central directory and local
headers without importing `zipfile` or a codec. It reports each member's real
name, method, flags, CRC-32, compressed/expanded sizes, and exact raw payload
range. Central-directory sizes also handle data-descriptor entries safely.

The router can now distinguish:

| member | treatment |
|---|---|
| ZIP structure, names, directories | pooled to Hybrid-Huffman |
| method-0 XML, including `word/document.xml` | pooled directly |
| viable method-8 XML | exact token transform, then Hybrid-Huffman (AFC4) |
| compact method-8 XML | preserved when exposure cannot compete |
| media / unsupported / encrypted entries | preserved verbatim |

ZIP64/multi-disk or malformed inventories conservatively fall back to the
safe legacy scan/whole-file path. Expansion and ratio budgets prevent ZIP-bomb
analysis; declining a component never affects losslessness.

## 5. Exact DEFLATE-token transform (not a compressor)

`deflate_tokens.py` parses raw member streams. It records the producer's exact
block headers, canonical-code symbols, literal runs, length/distance symbols,
extra bits, stored blocks and final padding. It also expands those tokens to
the original XML.

It does **not** search for matches, select a block type, build a replacement
tree, invoke zlib, or generate an optimised DEFLATE stream. `restore()` simply
serialises the recorded choices and verifies them against the XML, reproducing
the original raw member bytes bit-for-bit. The plain XML + recipe are the data
given to the existing Hybrid-Huffman engine.

All deflated XML members in the document corpus were parsed and restored
exactly, with expanded ZIP size and CRC-32 independently verified.

## 6. AFC4

```
"AFC4" | u8 mode(4) | varint original_length | varint segment_count
       | per segment: varint (original_length << 2 | kind)
         kind 0 opaque
         kind 1 direct pooled
         kind 2 transformed: varint XML length | varint recipe length
       | varint opaque_length + verbatim bytes
       | varint pooled_blob_length + ordinary AFC1/AFC2 container
```

For a transformed segment, the inner pooled source contains `XML | recipe`.
Reconstruction reads both, serialises the exact original member stream, and
concatenates it with the direct/opaque segments in original order.

AFC1 and AFC2 are untouched. AFC3 layout is untouched. `afc2.decompress_bytes`
dispatches AFC4 by magic before the native AFC1/AFC2 decoder; old decoders
reject AFC4 rather than misreading it. The Flask app, type sniffer, analysis
tree/attribution reader, download flow and separate Compress/Decompress pages
recognise and report AFC4.

## 7. Measured document results

Median of three native runs, 15 files, every result byte-equal and SHA-256
matched. Raw CSV: `benchmarks/v8_documents.csv`.

| file | original | plain AFC | current | change | current ms | format |
|---|---:|---:|---:|---:|---:|---|
| docx_images.docx | 205,171 | 204,953 | 204,595 | -0.17% | 31.5 | AFC3 |
| docx_stored.docx (level-0 method 8) | 184,151 | 18,025 | 17,996 | -0.16% | 226.8 | AFC4 |
| docx_text_and_images.docx | 128,247 | 128,158 | 127,802 | -0.28% | 19.3 | AFC3 |
| docx_text_large.docx | 16,412 | 16,420 | 16,147 | -1.66% | 20.4 | AFC3 |
| docx_zip_stored.docx (true method 0) | 184,825 | 19,400 | 19,400 | 0.00% | 101.3 | AFC2 |
| pdf_text_and_images.pdf | 235,584 | 174,052 | 169,826 | -2.43% | 274.3 | AFC3 |
| pdf_images_only.pdf | 255,512 | 245,834 | 244,079 | -0.71% | 33.9 | AFC3 |
| pdf_word_flate.pdf | 25,999 | 18,821 | 18,514 | -1.63% | 47.8 | AFC3 |

Totals: 1,928,299 original bytes; 1,061,080 plain AFC bytes; 1,052,853 current
bytes (**0.78% smaller than plain overall**). The current path was smaller on
8 files, unchanged on 7, and larger on 0.

The honest normal-DOCX result remains important. For the checked Word-style
files, deflated XML expanded from 3,147-15,490 bytes to 23,851-849,972 bytes.
That 9x-57x source cannot plausibly beat its producer stream with this engine,
so the viability gate preserves it. No improvement is claimed from processing
an inner component when the complete output does not win.

## 8. Verification

```
tests/test_app.py          250 passed, 0 failed
AFC_NO_NATIVE=1 tests      237 passed, 0 failed
tools/doc_bench.py         15/15 byte-exact, SHA-256 verified
tools/preset_bench.py      Dickens Fast/Balanced/Maximum native, 3/3 verified
tools/run_verification.py  60 corpus round trips plus Python, native, and
                           JavaScript decoders: ALL CHECKS PASSED
```

Coverage includes native/Python byte identity for all presets, exact ZIP
tiling, member naming, every parsed token recipe, stored/fixed/dynamic blocks,
ZIP CRC/size checks, forced and automatically selected AFC4, corrupt AFC3/AFC4
rejection, AFC1/AFC2 backward compatibility, old-decoder rejection, two full
compression cycles, web integrity reporting, and generic-file byte identity.

## 9. Files

Added: `deflate_tokens.py`, `CHANGES_v8.md`,
`benchmarks/documents/docx_zip_stored.docx`,
`benchmarks/v8_documents.csv`, `benchmarks/v8_dickens_native.csv`.

Modified: `containers.py`, `afc2.py`, `analysis.py`, `app.py`, `filetypes.py`,
`afcpak.py`, `config.py`,
`static/js/compress.js`, `static/js/decompress.js`,
`templates/decompress.html`, `tools/make_doc_corpus.py`,
`tools/doc_bench.py`, `tools/preset_bench.py`, `tools/run_verification.py`,
`tests/test_app.py`, README and scope/change notes.

Unchanged algorithm implementations: `afc.py`, `afc_native.cpp`,
`afc_engine.js`. The compression algorithm remains the project's
Multi-Level Frequency Analysis + Bit Cost Decision Engine + Hybrid Huffman.
