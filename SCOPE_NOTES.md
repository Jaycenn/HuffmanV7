# SCOPE_NOTES.md — how Part 1 stays inside the thesis scope

Written for two audiences: the engineer picking this up for **Part 2**, and the
**adviser/panel** asking whether the added features broke the study's stated
boundaries. Every claim below is enforced by a test in `tests/test_app.py`.

---

## 1. The compression algorithm was not touched

`afc.py`, `afc2.py`, `afc_native.cpp`, `afc_native.py`, `afc_engine.js` are
**byte-for-byte unchanged** in Part 1. The web layer only calls the published
API:

```python
afc2.compress_bytes(data, adaptive, fmt=...)   # -> AFC1/AFC2/AFC3/AFC4/AFC6
afc2.decompress_bytes(blob)                    # -> original bytes
```

Nothing in `app.py`, `db.py`, `auth.py`, `admin.py`, or `afcpak.py` reaches
into engine internals, changes a constant, or post-processes a container.
`git diff` on those five engine files should be empty for this part.

**Entropy coding is still Huffman-only.** No arithmetic/range/ANS coding, no
LZ77/78/LZW/LZSS or any offset back-reference, no BWT/MTF, no PPM, no ML. The
new features add packaging and bookkeeping, not a second compressor.

## 2. Why the archive format cannot smuggle in a second codec

`.afcpak` is a **neutral container**: an 8-byte magic, a JSON manifest, and the
per-file AFC payloads concatenated. It performs no compression of its own.

* **No cross-file references.** Each member goes through the engine
  independently, so no LZ-style matching happens across file boundaries. A
  "solid" archive would need exactly the back-reference mechanism the thesis
  forbids, so it is not offered.
* **No DEFLATE, structurally.** `afcpak.py` imports no compression library at
  all — no `zipfile`, `zlib`, `gzip`, `bz2`, `lzma`, `tarfile`. This is
  asserted by parsing the module's AST in
  `test_archive_no_deflate`, not by grepping (a grep false-positives on this
  very paragraph). A second test asserts every member payload begins with an
  `AFC1`/`AFC2`/`AFC3`/`AFC4`/`AFC6` magic, which would fail if a foreign codec were
  introduced.
* **If someone later switches to `zipfile`**, it must be `ZIP_STORED`.
  `ZIP_DEFLATED` is LZ77 + Huffman and would silently violate the constraint.
  The AST test is there to catch that change in review.

## 3. Losslessness is proven, never assumed

Every processed file gets a **SHA-256 round trip** at processing time, and the
result is what gets stored in `compression_history.lossless_verified`. The
field is never set optimistically. Inside archives, `afcpak.pack()` verifies
each member *before* writing it and raises rather than emitting a
silently-lossy archive; `afcpak.unpack()` re-checks each member's stored digest
on extraction. The raw-storage fallback in the engine is untouched, so
incompressible input still cannot inflate beyond the container header.

## 4. Why a local SQLite file still counts as "local, non-cloud"

The thesis Delimitations exclude cloud storage and remote services. A local
SQLite database does not cross that line, for reasons the team can state
plainly to an adviser:

1. **It is a file on the same machine**, sitting next to `app.py`
   (`afc_app.sqlite3`). It is not a server, not a service, and not a network
   endpoint. SQLite is an embedded library — there is no database process to
   connect to, no port, no credentials.
2. **No data leaves the computer.** The app binds to `127.0.0.1`, has no
   outbound calls, no telemetry, no analytics beacons, and (since Part 1) not
   even a CDN request — Tailwind is compiled to `static/css/tailwind.css` and
   served locally, so the dashboard works with the network cable unplugged.
3. **Deleting the file deletes the data.** `python -c "import db;
   db.reset_db()"` returns the system to a clean state. There is no remote
   copy to also delete.
4. **The alternative is worse for the study.** Keeping accounts and history in
   memory only would make Part 2's analytics impossible to demonstrate across
   sessions, and writing them to a cloud service is what the delimitation
   actually excludes.

In short: the delimitation rules out *cloud* persistence, not *persistence*.
A single-file embedded database is the most local persistence available.

**Produced files are deliberately NOT persisted.** Compressed output,
archives, and extracted members live in an in-memory dict (`app.RESULTS`,
capped at 60 entries) and disappear when the process stops. Only *metadata*
(sizes, ratio, engine, verification status) is written to SQLite. The app
never stores user file contents on disk.

## 5. Password hashing is not file encryption

The thesis excludes encryption of compressed output. Part 1 does not encrypt
any file data — there is no cipher anywhere in the file path.

Account passwords are a separate concern and *are* hashed, with
`werkzeug.security.generate_password_hash` (PBKDF2-SHA256). That is credential
storage hygiene, not a compression feature, and it does not make the output
"password-protected": an `.afc` or `.afcpak` produced by this app can be
decompressed by the CLI or the browser engine with no credential at all. These
two things are easy to conflate in a defence; keep them separate.

## 6. Size limits reflect what the paper actually validated

`MAX_FILE_SIZE`/`MAX_BATCH_SIZE` default to the ceiling Appendix C documents
(100 MB / 500 MB) rather than the larger numbers that were requested, so the
app cannot accept inputs the paper never claimed to have tested. The measured
behaviour at 150 MB and 250 MB, and the exact sentence to change if the team
raises the documented ceiling, are in **SIZE_POLICY.md**. Every limit shown in
the UI is read from `config.py` (via `/api/config` and the `cfg` template
context) — there are no hardcoded sizes in templates or JavaScript, and a test
asserts the Settings page renders the configured value.

## 7. Explicitly excluded, and still excluded

Not implemented, per the brief: two-factor auth, data export / right-to-delete
flows, multi-language support, webhooks, scheduled or watched-folder
automation, and any form of file encryption.

---

## 8. Part 2 status (completed)

Part 2 is built. It added analytics, the algorithm-showcase features and the
small additions, **without modifying any engine file**. Two constraint-relevant
findings from that work:

* **The engine has no stats API.** Features 6-8 could not read engine
  internals because every tier function is private. `analysis.py` therefore
  derives entropy from the input bytes and the tree/attribution from the
  produced container -- reading only, which constraint #1 allows.
* **The native core ignores the Python tunables** (measured: byte-identical
  output at DP_ROUNDS 1/3/6). Presets other than Balanced therefore run on the
  pure-Python path. This is documented in `presets.py` and stated in the UI so
  nobody reads "Fast" as the quickest route to a compressed file.

`gzip` now appears in the codebase as a **reference measurement for the
comparison chart only** (`app._reference_sizes`). It never produces user
output, and the UI labels it as a reference. This does not introduce a second
compression mechanism -- the archive path still refuses every compression
library, as the AST test enforces.

## 9. What Part 2 inherited (and what a Part 3 would)

**Schema** (`schema.sql`): `users`, `compression_history`, `audit_log`,
`login_attempts`. The history table already carries everything an analytics
view needs — `ratio`, `space_saved_pct`, `engine`, `container_format`,
`lossless_verified`, `duration_ms`, `created_at`, and a `batch_id` that groups
one queue/archive run. Add analytics queries to `db.py`, not to templates.

**Routes already available** (see the map at the top of `app.py`):
`/api/history` and `/api/stats` return JSON for the logged-in user, which is
enough to build charts without a new backend aggregation layer.

## 11. V7 — what changed in the engine, and what did not

V7 is the first part that deliberately edits engine files. §1 above ("the
compression algorithm was not touched") described Parts 1-2 and remains true
of them; it is superseded for V7 by this section.

**Changed:** `afc2.py` and `afc_native.cpp`, to carry preset parameters into
the native core. **Unchanged:** `afc.py` (containers, canonical/package-merge
Huffman, the universal decoder) and `afc_engine.js`.

The constraint-relevant facts for a panel:

1. **The algorithm is the same.** No stage was added, removed or reordered.
   What changed is that four values which were compiled into the C++ core are
   now passed in as arguments, so a preset reaches it instead of being
   silently ignored. Proof: all three presets produce **byte-identical**
   containers on the native and pure-Python paths across the corpus (30
   combinations), and Python is still the reference both are checked against.
2. **The entropy stage is still Huffman-only.** No arithmetic/range/ANS, no
   LZ77/78/LZW/LZSS or offset back-references, no BWT/MTF, no PPM, no ML.
3. **Container-aware processing adds no compressor.** `containers.py` decides
   *where* the existing engine is applied. It imports no compression library
   and defines no function that compresses — both asserted by AST tests, the
   same argument used for `afcpak.py` in §2. Every byte it compresses goes
   through `afc2.compress_bytes`; every byte it does not is copied verbatim.
4. **DOCX is not solved with ZIP/DEFLATE.** Deflated members are never
   inflated and re-deflated — that would introduce DEFLATE as a second stage
   and could not guarantee byte-exact reconstruction. They are preserved
   verbatim instead. The consequence is reported rather than hidden: a
   Word-generated DOCX gains little, because its XML is already compressed and
   cannot be reached losslessly. Where the XML *is* reachable (a STORED
   package) the engine compresses it by 90.2%.
5. **Byte-exactness is structural, not hopeful.** The segment plan must
   exactly tile the input — validated before anything is written — so
   reconstruction is a concatenation and cannot drift even if the PDF/ZIP
   parser is wrong about a file. Every document in the corpus is SHA-256
   verified, including two full compress/decompress cycles.
6. **Old containers still work.** AFC1/AFC2 are unchanged and dispatch on
   magic before anything else; the old decoder raises on AFC3 rather than
   misreading it. AFC3 is only emitted when it is smaller than the plain
   container.

Nothing in §7 was un-excluded: still no two-factor auth, no export /
right-to-delete flows, no multi-language support, no webhooks, no scheduled
automation, and no file encryption.

## 10. The Compress/Decompress page split (Part 3) is UI only

The dashboard now has two pages, `/compress` and `/decompress`, instead of one
combined upload box. For the panel, the constraint-relevant facts are:

1. **No engine file changed.** `afc.py`, `afc2.py`, `afc_native.cpp`,
   `afc_native.py` and `afc_engine.js` are byte-for-byte identical, as are
   `afcpak.py`, `analysis.py`, `presets.py`, `config.py` and `schema.sql`.
   Both pages call `afc2.compress_bytes` / `afc2.decompress_bytes` — the same
   entry points the combined page used. A test parses `app.py`'s AST and fails
   if it ever calls anything else on an engine module.
2. **No second compressor was introduced.** There is exactly one pipeline. The
   new `filetypes.py` only *names* byte streams so a restored file regains its
   extension; a test asserts it defines no function whose name contains
   "compress", and a second test asserts it imports no compression library —
   the same AST argument used for `afcpak.py` in §2, and for the same reason.
3. **No format-specific processing was added.** "Container-aware" here means
   the user hands over a whole PDF/DOCX and gets a whole PDF/DOCX back: the
   file is compressed as one byte stream, exactly as before. Nothing extracts
   PDF page streams or DOCX package parts, because doing so would be a
   second, format-specific algorithm — which the brief forbids.
4. **The container format is unchanged.** Filename recovery reads the restored
   *output*; nothing new is written into AFC1 or AFC2, so every existing
   decoder (Python, C++, JavaScript, WASM) still reads these files.
5. **SHA-256 verification is never fabricated.** The Decompress page can only
   claim a match against a digest this account actually recorded at compression
   time. With no record it says "no reference on file" and shows the restored
   file's own digest. Integrity — restored length versus the length declared in
   the header — is reported separately and is always available. A test asserts
   that a foreign container yields *no reference*, not a green check.

Nothing in §7 was un-excluded: still no two-factor auth, no export/right-to-
delete flows, no multi-language support, no webhooks, no scheduled automation,
and no file encryption.

**Conventions worth keeping:**
* every new admin route gets `@auth.role_required("admin")` — it returns a
  real 403, which the tests assert;
* every new limit goes in `config.py` and `public_dict()`, never in a template;
* every feature that touches file data gets a SHA-256 round-trip test;
* rebuild `static/css/tailwind.css` with `sh tools/build_css.sh` after editing
  templates, or new utility classes will not exist in the local stylesheet.

## 12. V8 — exact DOCX XML/token processing

V8 corrects the V7 limitation without adding another compressor.

1. `containers.zip_components()` reads ZIP central-directory metadata and
   names real OOXML members such as `word/document.xml`. It imports neither
   `zipfile` nor a compression library.
2. STORED XML bytes are pooled directly into the existing Hybrid-Huffman
   engine. Method-8 XML is considered only when its expanded size is viable.
3. `deflate_tokens.py` is a reversible parser/serializer, not an encoder: it
   records the producer's existing block headers, Huffman symbols, match
   lengths/distances and extra bits. It performs no match search, tree choice,
   or compression-library call. The resulting plain XML + exact recipe are
   the bytes compressed by Hybrid-Huffman.
4. AFC4 is a new outer wrapper, explicitly distinguished from AFC1/AFC2/AFC3.
   AFC1/AFC2 are unchanged; AFC3 decoding is unchanged and hardened against
   truncated/trailing component data. Old decoders reject AFC4 cleanly.
5. The global guard compares the complete AFC4 candidate against the normal
   whole-file result. Already-compact Word XML remains verbatim when the
   transform would be larger. No ratio improvement is claimed for those files.
6. Exactness is checked at three levels: DEFLATE recipe bytes, ZIP member
   size/CRC, and complete-file byte equality plus SHA-256. The web flow still
   accepts one normal `.docx` and returns the exact original package.

## 13. AFC 1.3 — self-verification and PDF Flate components

1. AFC5 is a metadata/integrity envelope, not a codec. It stores the original
   length/SHA-256, inner payload length/SHA-256 and safe basename around an AFC
   payload. Legacy containers remain readable.
2. AFC6 explicitly versions PDF zlib recipes; it does not reuse AFC4's raw ZIP
   DEFLATE semantics. Old decoders reject the distinct magic cleanly.
3. `deflate_tokens.transform_zlib()` parses an existing RFC-1950 stream and
   retains its header, Adler-32 and exact DEFLATE token recipe. It imports no
   compression library, searches for no matches and chooses no replacement
   tree. `restore_zlib()` reproduces the original stream bytes.
4. Only suitable textual PDF page/content, metadata or object streams are
   considered. Images, fonts, unsupported filters, malformed inputs and
   excessive expansions stay opaque and verbatim.
5. AFC6 must beat the complete AFC3 and whole-file AFC results and must pass a
   full byte comparison before automatic selection. A losing component trial
   is reported as such and cannot inflate the user's output.
6. Preset selection uses frozen per-call `EngineOptions`; concurrent web
   requests never share mutable DP/search tuning.
