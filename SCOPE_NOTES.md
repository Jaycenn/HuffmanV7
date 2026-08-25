# SCOPE_NOTES.md — how Part 1 stays inside the thesis scope

Written for two audiences: the engineer picking this up for **Part 2**, and the
**adviser/panel** asking whether the added features broke the study's stated
boundaries. Every claim below is enforced by a test in `tests/test_app.py`.

---

## 1. The compression algorithm was not touched

`afc.py`, `afc2.py`, `afc_native.cpp`, `afc_native.py`, `afc_engine.js` were
**byte-for-byte unchanged** in Part 1. The web layer only calls the published
API:

```python
afc2.compress_bytes(data, adaptive, fmt=...)   # -> AFC1/AFC2/AFC3/AFC4/AFC6
afc2.decompress_bytes(blob)                    # -> original bytes
```

Nothing in `app.py`, `db.py`, `auth.py`, `admin.py`, or `afcpak.py` reaches
into engine internals, changes a constant, or post-processes a container.
`git diff` on those five engine files should be empty for this part.

**Later performance work does edit `afc_native.cpp`, and states its own
proof.** The v9 optimisation replaced the search structures inside the native
core — an Aho-Corasick automaton over the structural dictionary instead of
per-length hash probing, open-addressed frequency counters, and an early exit
once the optimal parse stops moving. None of it changes the compression model:
the tier scans, the Bit Cost Decision Engine, block growth, the length-limited
canonical Huffman coder and the container layouts are all as before. The claim
that matters to the study is therefore not "the file was not edited" but **the
emitted container is identical byte for byte**, which is asserted directly:
`test_container_bytes_are_pinned` fixes the SHA-256 of the produced container
for a seven-file corpus at all three presets, and
`test_preset_backend_byte_identity` keeps the pure-Python reference and the
native core in agreement.

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

## 4. Where persistence lives, and what the Delimitations say about it

This section used to argue that a local SQLite file satisfied a "local,
non-cloud" delimitation. That is no longer the study's position and the
manuscript no longer claims it.

**The deployed system runs in Azure and persists to Supabase.** Participant
uploads are processed by the Flask application in Azure Container Apps.
Account credentials, processing history, the audit log and completed compressed
results are held in Supabase managed PostgreSQL and object storage. The
Delimitations and Ethical Consideration disclose both providers before consent
is given rather than describing the system as local-only.

**SQLite remains the development store.** Appendix C says so, and the code
keeps both: `AFC_DB_BACKEND` selects `sqlite` or `supabase`,
`AFC_STORAGE_BACKEND` selects `local` or `supabase`, and both default to the
local option. The 549-check automated suite always runs against SQLite, so it
needs no network and no credentials. Appendix G documents the type mapping;
the logical model is identical and only the physical types differ.

**The engine itself still makes no network call.** This is the distinction
worth keeping from the old text. Compression and decompression happen entirely
inside the application server process — the multi-tier scan, the Bit Cost
Decision Engine and the container writers have no database or network
dependency and cannot reach Supabase. What moved to a hosted service is the
persistence layer around the engine, not the engine.

**Only produced compressed output is persisted, on either backend.** Completed
`.afc` and `.afcpak` results are stored under opaque server-generated
identifiers: a file outside `static/` when the local backend is selected, one
logical object in the private bucket when Supabase is. Large logical objects
are transparently split into 40 MiB internal parts to remain under the Free
plan's per-object cap. Uploaded originals, decompressed originals and extracted
members are never written to either backend — they live in the owner-bound,
60-entry `app.RESULTS` cache until downloaded, evicted, or the process stops.
That property is unchanged by the move, and it is the stronger one to state.

**What the move costs, stated rather than glossed.** There is now a remote copy
to delete as well as a local one, and account details and stored results sit on
infrastructure the researchers do not operate. The Ethical Consideration tells
participants this rather than leaving them to infer it.

Access is authenticated and owner-scoped; administrators may also retrieve a
stored result. A different user receives 404 so the row's existence is not
disclosed. SHA-256 and byte length are checked again before every durable
download, on both backends — the verification is the same code either way, and
a stored object that does not match its recorded digest and length is refused.
Row-level security is enabled on all six PostgreSQL tables with no policies, so
the only route to the data is through the application and the auto-generated
REST API reaches nothing. `RESULT_RETENTION_DAYS=0` keeps results until owner
deletion; a positive value enables age cleanup. `MAX_STORED_BYTES_PER_USER`
defaults to 2 GiB; the Azure configuration currently sets 150 MiB and refuses
a new result instead of evicting an older one. The
Files page provides Download and Delete controls, and deleting an account
removes its stored blobs before the database row is removed. Security event
types and timestamps remain, but the deleted account's username, IP address,
login-attempt rows, and identifying admin-target text are removed.

## 5. Password hashing is not file encryption

The thesis excludes encryption of compressed output. Part 1 does not encrypt
any file data — there is no cipher anywhere in the file path.

Account passwords are a separate concern and *are* hashed, with
`werkzeug.security.generate_password_hash`, whose current default is scrypt.
The algorithm and its parameters are recorded in the stored string, so
`check_password_hash` verifies an older pbkdf2 hash and a newer scrypt one
alike. That is credential
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

Not implemented, per the brief: two-factor auth, broad personal-data export,
multi-language support, webhooks, scheduled or watched-folder
automation, and any form of file encryption.

---

## 8. Part 2 history and current scope

Part 2 originally added analytics and algorithm-showcase features without
modifying engine files. The analytics page and `/api/analytics/*` views were
later removed as a scope reduction. Processing history, reports, previews,
entropy inspection, and the algorithm evidence remain.

* **The engine has no stats API.** Features 6-8 could not read engine
  internals because every tier function is private. `analysis.py` therefore
  derives entropy from the input bytes and the tree/attribution from the
  produced container -- reading only, which constraint #1 allows.
* **Historical Part 2 limitation, resolved in V7:** the native core originally
  ignored Python tunables. V7 added the tunable native ABI without changing the
  algorithm; Fast, Balanced, and Maximum now run natively when the library is
  available and are checked byte-for-byte against the Python reference. See
  §11 for the measured current behavior.

Legacy history rows can still carry gzip and single-tier-Huffman reference
sizes from the removed analytics view. Neither reference ever produces user
output or participates in an AFC container. Removing their now-unused upload
measurement is listed as a proposed cleanup rather than silently changing the
recording path in this UI/storage change.

## 9. What Part 2 inherited (and what a Part 3 would)

**Schema** (`schema.sql`): `users`, `compression_history`, `stored_artifacts`,
`audit_log`, `login_attempts`, and `app_meta`. `stored_artifacts` is additive
and linked by foreign keys; `app_meta.session_epoch` invalidates signed sessions
after a destructive reset. Existing history rows and databases migrate in place.

**Routes already available** (see the map at the top of `app.py`):
`/api/history` and `/api/stats` return JSON for the logged-in user, supporting
the cross-session Files view and report evidence without cloud storage.

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
4. **DOCX is not compressed with ZIP/DEFLATE.** For suitable XML members, AFC4
   reversibly parses the *existing* DEFLATE token stream into source XML plus
   an exact reconstruction recipe. The XML goes through Hybrid Huffman; the
   recipe rebuilds the original compressed bytes exactly. Unsupported,
   high-entropy, and media members are preserved verbatim. No inflate/re-deflate
   compressor is introduced, and producer-compressed DOCX files may therefore
   yield little additional reduction.
5. **Byte-exactness is structural, not hopeful.** The segment plan must
   exactly tile the input — validated before anything is written — so
   reconstruction is a concatenation and cannot drift even if the PDF/ZIP
   parser is wrong about a file. Every document in the corpus is SHA-256
   verified, including two full compress/decompress cycles.
6. **Old containers still work.** AFC1/AFC2 are unchanged and dispatch on
   magic before anything else; the old decoder raises on AFC3 rather than
   misreading it. AFC3 is only emitted when it is smaller than the plain
   container.

Nothing in §7 was un-excluded: still no two-factor auth, broad personal-data
export, multi-language support, webhooks, scheduled automation, or file
encryption. Files and account administration now provide scoped deletion of
locally stored compressed results.

## 10. The Compress/Decompress page split (Part 3) is UI only

The dashboard now has two pages, `/compress` and `/decompress`, instead of one
combined upload box. For the panel, the constraint-relevant facts are:

1. **The page split changed no engine file.** `afc.py`, `afc2.py`, `afc_native.cpp`,
   `afc_native.py` and `afc_engine.js` are byte-for-byte identical, as are
   `afcpak.py`, `analysis.py`, and `presets.py`. The later ByteSize persistence
   layer changes only web/config/database files, not compression behavior.
   Both pages call `afc2.compress_bytes` / `afc2.decompress_bytes` — the same
   entry points the combined page used. A test parses `app.py`'s AST and fails
   if it ever calls anything else on an engine module.
2. **No second compressor was introduced.** There is exactly one pipeline. The
   new `filetypes.py` only *names* byte streams so a restored file regains its
   extension; a test asserts it defines no function whose name contains
   "compress", and a second test asserts it imports no compression library —
   the same AST argument used for `afcpak.py` in §2, and for the same reason.
3. **The UI page split itself added no format-specific processing.** That was
   true of this earlier change only. The current AFC4/AFC6 paths described in
   §§12–13 do process suitable DOCX/PDF components, while still routing their
   bytes through Hybrid-Huffman and preserving exact reconstruction.
4. **The UI page split did not change the then-current container format.** The
   later component-aware work introduced explicitly versioned AFC4/AFC6 paths;
   the backward-compatible AFC1/AFC2 decoders remain available.
5. **SHA-256 verification is never fabricated.** The Decompress page can only
   claim a match against a digest this account actually recorded at compression
   time. With no record it says "no reference on file" and shows the restored
   file's own digest. Integrity — restored length versus the length declared in
   the header — is reported separately and is always available. A test asserts
   that a foreign container yields *no reference*, not a green check.

Nothing in §7 was broadly un-excluded: there is still no two-factor auth,
personal-data export, self-service account-erasure workflow, multi-language
support, webhooks, scheduled automation, or file encryption. The implemented
scope is narrower: owners can delete stored compressed artifacts, and an
administrator can delete an account together with its owned artifacts.

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
