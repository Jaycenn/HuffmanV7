# CHANGES.md — Web application, Part 1

Scope of this document: the **web application** work (accounts, queue, batch,
archive, reports, size policy, responsive layout).

The engine-level changelog for AFC v4 (multi-tier scan, Bit Cost Decision
Engine, DP optimal parse, hybrid Huffman, AFC1/AFC2 containers) is preserved
separately in **`CHANGES_v4_engine.md`** — nothing in Part 1 modifies it.

**Read `SCOPE_NOTES.md` alongside this file if you are picking the project up
cold.** It explains the constraints and what Part 2 inherits.

---

## Part 3 — Compress and Decompress split into separate pages

A **UI/UX restructuring**. No engine file was touched, no compression algorithm
was added, and no existing feature was removed.

**Before:** one `/compress` page headed *"Compress / Decompress"* with a single
upload box that sniffed the first four bytes and decided for you what would
happen. A first-time user could not tell which operation they were about to
perform.

**After:** two top-level destinations, each naming its own operation.

| | Compress | Decompress |
|---|---|---|
| route | `/compress` | `/decompress` |
| heading | Compress Files | Decompress Files |
| description | Reduce the size of your files using Hybrid-Huffman compression. | Restore an AFC file to its original format and verify its integrity. |
| accepts | any normal file | `.afc` containers |
| refuses | `.afc` → links to Decompress | non-AFC → links to Compress |
| multi-file pane | Queue & batch, Folder → archive | Extract archive |
| engine entry point | `afc2.compress_bytes` | `afc2.decompress_bytes` |

### What was added

* `templates/decompress.html`, `static/js/decompress.js` — the new page.
* `templates/compress.html` — a new *Single file* pane matching the required
  workflow; the existing queue and archive panes are preserved verbatim, ids
  intact, so `static/js/queue.js` is unmodified.
* `static/js/compress.js` — single-file compress flow with real upload
  progress (XHR) and a client-side AFC guard that fires before any upload.
* `POST /api/decompress` — decompress-only endpoint. Reports container format
  and mode, a structural integrity check, SHA-256 verification, and the
  recovered original filename.
* `filetypes.py` — content-based type detection. **Compresses nothing.** It
  imports no compression library (asserted by an AST test, as with `afcpak`),
  and identifies ZIP-based packages such as DOCX by scanning for their literal
  member names rather than by opening an archive.
* `db.py` — three additive columns via the existing idempotent migration list:
  `sha256_original`, `sha256_container`, `detected_type`.

### Two decisions worth defending

**Filename recovery.** An AFC container stores
`magic | mode | original_length | payload` and carries no filename, so
`MyDocument.afc` cannot know it was a PDF. The extension is recovered by
sniffing the *restored bytes*. This reads output only — it does not change the
container format, which stays byte-compatible with every existing decoder.

**The SHA-256 line means something.** Absent a reference digest, no decoder can
prove restored bytes equal an original it never saw. So the page reports three
distinct verdicts and never fabricates the third:

| Verdict | When |
|---|---|
| ✓ MATCH | the digest recorded when this account compressed the file matches the restored bytes |
| ✗ MISMATCH | a digest is on record and does **not** match |
| — no reference on file | this container was not produced by this account; the restored file's own digest is shown instead |

Integrity is reported separately and is always available: the header declares
`original_length` independently of the payload, so a length disagreement means
a corrupt or doctored container.

### Verification

`tests/test_app.py` grew from 122 to **165 checks**, 0 failing. The 122
original checks are unchanged and still pass. The 43 new ones assert the split
itself: both pages exist with their own headings and descriptions, both appear
in the primary navigation on every page, the old combined heading is gone, each
page refuses the other's input, `/api/decompress` round-trips the corpus under
SHA-256, an unknown container reports *no reference* rather than a fake match, a
corrupt container returns a clean 400, `MyDocument.afc` is restored as
`MyDocument.pdf`, `filetypes.py` imports no codec and defines no compression
function, and `app.py` calls only the published engine API.

`tools/run_verification.py` still reports ALL CHECKS PASSED, and the engine
files remain byte-for-byte identical:

| Engine file | Changed? |
|---|---|
| `afc.py`, `afc2.py`, `afc_native.cpp`, `afc_native.py`, `afc_engine.js` | no — byte-identical |
| `afcpak.py`, `analysis.py`, `presets.py`, `config.py`, `schema.sql` | no — byte-identical |

Verified in a real browser at 1440 / 1024 / 768 / 375 px: a 107.3 KB PDF
compressed to 2.9 KB (36.96×, C++ native, VERIFIED), then restored through the
Decompress page as `MyDocument.pdf`, byte-identical, integrity VERIFIED,
SHA-256 MATCH. No horizontal overflow at any breakpoint.

---

## The one-line summary

Part 1 adds a multi-user local web application around the existing compressor.
**No file in the compression engine was modified.**

| Engine file | Changed in Part 1? |
|---|---|
| `afc.py` | ❌ untouched |
| `afc2.py` | ❌ untouched |
| `afc_native.cpp` / `afc_native.py` | ❌ untouched |
| `afc_engine.js` | ❌ untouched |

Everything below is new code *around* those files, calling only
`compress_bytes()` / `decompress_bytes()`.

---

## New files

| File | Purpose |
|---|---|
| `config.py` | every tunable, incl. the size-policy constants |
| `db.py` | SQLite access layer |
| `schema.sql` | 4 tables: users, compression_history, audit_log, login_attempts |
| `auth.py` | blueprint: login/logout/register/change-password + role gating |
| `admin.py` | blueprint: admin user list + audit log |
| `afcpak.py` | `.afcpak` archive container (manifest + AFC payloads) |
| `templates/base.html` + 10 page templates | responsive shell and pages |
| `static/js/queue.js` | drag-drop queue, batch table, archive flows |
| `static/css/tailwind.css` | locally-built Tailwind (no CDN) |
| `tools/size_policy_bench.py` | the 8→250 MB benchmark behind SIZE_POLICY.md |
| `tools/build_css.sh` | rebuilds the local stylesheet |
| `tests/test_app.py` | 49-check end-to-end suite |
| `SIZE_POLICY.md`, `SCOPE_NOTES.md` | the two decision documents |

`app.py` was rewritten as an app factory with blueprints; the previous
single-file dashboard behaviour is preserved (see "Existing features" below).

`templates/index.html` (the old single-page dashboard) was **removed** — its
functionality now lives in `dashboard.html` + `compress.html`, and no route
rendered it any more. It is in git history if you need the original markup.

---

## Feature-by-feature

### Accounts, roles, audit

* SQLite `users` table; passwords hashed with werkzeug PBKDF2-SHA256, never
  stored or logged in plaintext (asserted by a test).
* Session login via Flask's signed cookie session. No Flask-Login dependency —
  one small auditable module instead.
* **Seeded admin** (`admin` / `afc-admin`, see README) ships with
  `must_change_password = 1`; every route redirects to the change form until a
  new password is set, so the documented credential cannot survive first use.
* **Roles:** `user` sees only their own history; `admin` additionally gets
  `/admin/users` and `/admin/audit`. A logged-in non-admin hitting an admin
  route gets a **real 403**, not a redirect — tested explicitly, because a
  redirect would be indistinguishable from a session timeout.
* **Login rate limiting:** fixed-window counter keyed on (username, IP),
  `LOGIN_MAX_ATTEMPTS` per `LOGIN_WINDOW_SECONDS`. Expired rows are pruned on
  each check, so no cleanup job is needed. A locked-out user is refused *even
  with the correct password* — asserted by a test.
* **Audit log:** logins, failed logins, logouts, registrations, password
  changes, forbidden-route attempts, rate-limit blocks, and admin actions.
* Every compress/decompress is attributed to the logged-in user and written to
  `compression_history`.
* **Touches the engine:** nothing.

### 1. Drag-and-drop queue

* Multiple files by drop or picker; one row each with name, size, status
  (Queued → Compressing → Done/Error) and a progress bar.
* Queued files can be removed before the run starts; removal is disabled once
  processing begins.
* **Sequential by design.** `MAX_CONCURRENT_JOBS = 1`. The rationale is in
  code comments and is not arbitrary: the native core already threads
  internally, and the measured memory profile is ~16–19× the input size
  (SIZE_POLICY.md), so two concurrent 100 MB files would need ~3.8 GB. Running
  files in parallel multiplies peak RSS rather than saving wall-clock time.
* **Touches the engine:** nothing — one HTTP round trip per file.

### 2. Batch results table

* Name, original size, compressed size, ratio, saved %, engine, container,
  lossless status, per-row download.
* **Sortable** on every column (numeric via `data-v`, text otherwise).
* **Aggregate footer row:** total original, total compressed, overall ratio,
  overall saved %, and a failure count that includes both lossless failures
  and files that errored before producing a result.
* **Touches the engine:** nothing.

### 3. Folder / multi-file → one `.afcpak` archive

* Folder picker (`webkitdirectory`) or multi-file picker; `webkitRelativePath`
  preserves the folder structure inside the archive.
* Format: `AFCPAK01` magic, `uint32` manifest length, JSON manifest (path,
  original size, stored size, offset, container, SHA-256, verified flag), then
  the AFC payloads concatenated.
* Each member is compressed **independently** — no cross-file references.
* **No DEFLATE anywhere.** `afcpak.py` imports no compression library; this is
  verified by AST inspection in the test suite, plus a structural test that
  every member payload starts with an `AFC1`/`AFC2` magic.
* Matching extract flow reads the manifest, decompresses each member, verifies
  each stored SHA-256, and lists members for download.
* **Path traversal is rejected** on both write and read (`..`, absolute paths,
  drive letters, UNC), and `extract_to()` re-checks containment before writing
  — a hand-edited manifest still cannot escape the destination.
* **Touches the engine:** nothing — `afcpak` calls `compress_bytes` per member.

### 4. Report export

* **CSV** (`/report.csv`) and **PDF** (`/report.pdf`, a print-stylesheet page
  with a "Save as PDF" button).
* Contents: per-file table, aggregate totals, generation timestamp, app and
  engine version strings, and per-file lossless status.
* Both accept `?batch_id=…` so a single queue or archive run can be exported on
  its own; the batch results panel links to exactly that.
* PDF is print-based deliberately: no `reportlab`/`weasyprint`, keeping the
  dependency footprint at zero for an offline demo.
* **Touches the engine:** nothing — reads `compression_history`.

### 5. Size policy

* `MIN_FILE_SIZE`, `MAX_FILE_SIZE`, `MAX_BATCH_SIZE` in `config.py`,
  overridable by environment variable.
* Defaults are the **paper's tested ceiling** (100 MB / 500 MB), *not* the
  requested 200 MB / 1 GB. See SIZE_POLICY.md for the measured 150 MB and
  250 MB results and the exact Appendix C sentence to change.
* **The requested 1 MB minimum was not adopted** — every file in the thesis
  corpus is under 1 MB (largest ~414 KB), so that floor would make the app
  refuse the study's own test data. Default is 1 byte; the constant remains
  configurable.
* Enforced in one helper (`app.size_error`) used by every upload path, and
  published to the browser at `/api/config` so no limit is duplicated in JS.
* **Touches the engine:** nothing.

### 6. Responsive layout

* ≥1024 px: persistent sidebar. 768–1023 px: hamburger drawer + bottom nav.
  <768 px: same, tuned for thumb reach.
* Verified in Chromium at **1440 / 768 / 375 px**: correct element visibility
  per breakpoint, working drawer, and **no horizontal overflow** on any page.
* Reuses the existing HAU maroon/paper palette. No new UI library.

---

## Two problems found and fixed along the way

**Tailwind was loaded from a CDN.** The previous dashboard pulled
`cdn.tailwindcss.com` at runtime, so with no internet the page rendered
completely unstyled — and the inline `tailwind.config` assignment threw
`tailwind is not defined`, killing every script after it. This also made the
responsive requirement unverifiable (the first test run measured an unstyled
page: sidebar visible at every breakpoint, overflow on mobile).

Fixed by compiling Tailwind locally to `static/css/tailwind.css` (13 KB) and
dropping the CDN tag. The dashboard now renders correctly offline, which the
local/non-cloud delimitation arguably required anyway. Rebuild with
`sh tools/build_css.sh` after editing templates.

**The engine's memory profile was undocumented.** Measuring for the size
policy showed peak RSS is ~16× input natively and ~83× in pure Python. That
number is what makes the 200 MB request unsafe on the fallback path, and it is
now recorded in SIZE_POLICY.md and cited in the concurrency comment.

---

## Existing features: still working

The pre-Part-1 single-file behaviour is intact — single upload, AFC1/AFC2
auto-detection on `.afc` input, metrics, engine badge, container badge,
download, error handling — now behind login and with history recording. The
explicit Compress / Decompress / Auto-detect action control added previously
is preserved in the API (`action` form field; omitting it keeps magic-byte
auto-detection).

The CLI and the browser engine are unchanged:
`python afc2.py compress|decompress|verify|benchmark`, `--baseline`,
`--format {auto,afc1,afc2}`, and `AFC_WebApp.html`.

---

## Test coverage (`python tests/test_app.py` — 49 checks, all passing)

Auth: anonymous redirect, register, login, logout, wrong password → 401,
short/duplicate registration → 400, hashed-not-plaintext storage, rate limit
→ 429, rate limit blocks correct password, recovery after window clear,
forced password change on the seeded admin, forced change blocks other routes,
**non-admin → 403 on both admin routes**, admin access granted.

File data: single-file SHA-256 round trip across corpus files, batch endpoint
round trip, archive create/extract with per-member SHA-256 and preserved
folder paths, every member payload is an AFC container, AST proof of no
compression-library import, no `ZIP_DEFLATED` reference, path-traversal
rejection, tampered-manifest rejection.

Policy and reporting: empty file rejected, oversize file and oversize batch
rejected *through the config constants*, `/api/config` publishes real limits,
defaults match the Appendix C ceiling, history isolation between users, CSV
and PDF export, versions present in the report, all pages render, Settings
shows the configured maximum.

Browser-verified separately (Playwright, Chromium): queue rows and statuses,
remove-before-start, batch completion and aggregate row, column sorting,
archive create → download → extract with SHA-256 match, and the three
responsive breakpoints.

---
---

# Part 2 — Analytics & algorithm showcase

Appended to Part 1 above; both phases stay visible. Part 2 **extends** Part 1
and redesigns nothing. Engine files remain untouched:

| Engine file | Changed in Part 2? |
|---|---|
| `afc.py` | ❌ untouched (read via public helpers only) |
| `afc2.py` | ❌ untouched (module constants set at runtime, file not edited) |
| `afc_native.cpp` / `.py` | ❌ untouched |
| `afc_engine.js` | ❌ untouched |

## New files

| File | Purpose |
|---|---|
| `analysis.py` | READ-ONLY introspection: entropy, container parse, hybrid tree, savings attribution |
| `presets.py` | Fast / Balanced / Maximum mapped to existing engine tunables |
| `templates/analytics.html` | stat cards, ratio chart, file types, system status |
| `templates/compare.html` | side-by-side diff of two history entries |

## Two findings that shaped the implementation

**1. The engine exposes no stats API.** Every tier function in `afc2.py` is
private and returns nothing to callers, so Features 6–8 could not simply "read
existing stats". Instead `analysis.py` derives everything by reading: Tier-1
byte frequencies are recounted from the input, and the hybrid tree, dictionary
and per-symbol bit costs are parsed out of the produced container. This is
reading, which constraint #1 permits, and no engine file was modified.

**2. The native core ignores the Python tunables — measured, not assumed.**
Compressing `alice29.txt` natively at `DP_ROUNDS` = 1, 3 and 6 produced
**byte-identical output (61 692 B every time)**, because `afc_native.cpp`
compiles those constants in. Feature 9 as specified is therefore impossible on
the native path without editing the forbidden `.cpp`. Consequence, documented
in `presets.py` and surfaced in the UI: **Balanced runs natively; Fast and
Maximum force the pure-Python path**, which is the only way their parameters
take effect. Ratios are comparable across presets; wall-clock times are only
comparable within the same backend.

## Feature-by-feature

### 1. Full stat cards — *no engine change*
Files, average ratio, space saved, and **transfer time saved**. The last is a
derived estimate (`bytes_saved / ASSUMED_LINK_MBPS`) and the card says so in
the UI rather than presenting it as measured. Admins can switch to system-wide
via `?scope=system`; a non-admin passing the same parameter is silently
restricted to their own rows (tested).

### 2. AFC vs gzip vs single-tier Huffman over time — *no engine change*
New `gzip_bytes` / `huffman_bytes` columns are measured at compress time and
charted. **These are reference measurements only.** gzip never produces user
output; it is LZ77+Huffman and is measured purely for honest comparison. The
chart legend and body text say this, and where AFC is behind, the chart shows
it. References are skipped above `REFERENCE_CODEC_MAX_BYTES` (8 MB default) to
keep uploads responsive — those rows chart as a **gap, not a fabricated zero**.

### 3. File-type distribution — *no engine change*
Grouped by extension in SQL, with per-type totals and average ratio.

### 4. History search / filter / sort / paginate — *extends Part 1's table*
Part 1's static client-sorted table became a server-driven view
(`/api/history/search`) with filename search, date range, engine and preset
filters, whitelisted sort columns and pagination. The sort column is validated
against an allow-list, never interpolated — asserted with a SQL-injection
attempt in the tests.

### 5. Comparison view — *no engine change*
Pick any two history entries; diffs sizes, ratio, saved %, duration, engine,
container and preset, colouring the better value per metric. Rows are fetched
scoped to the owner, so one user cannot diff another's data (tested).

### 6. Pre-compression entropy estimate — *reads Tier-1 data only*
Shannon entropy over the byte histogram, shown the moment a file is selected,
before any compression. Reports bits/byte, the order-0 floor, distinct byte
count, a qualitative band and a top-byte histogram. Labelled an **estimate**:
it is a per-byte figure, and AFC routinely beats it because structural blocks
capture multi-byte structure a byte histogram cannot see. Verified to differ
across text (4.57), structured (4.59) and near-random (8.00) inputs.

### 7. Hybrid Huffman tree visualiser — *reads the container only*
Renders the actual tree from the produced container with **literal-byte leaves
in gold and structural-block leaves in maroon**, so the "hybrid" in the thesis
title is directly visible.

*A depth default of 6 was wrong and was fixed by measurement.* Literals earn
short codes (typically 4–8 bits) and blocks longer ones (9–14), so a shallow
cut rendered **11 literal leaves and zero block leaves** — the hybrid was
invisible. Depth 9 shows both (≈41 literal, ≈57 block on `alice29.txt`) and is
now the default, adjustable via `?depth=`. A code-length distribution chart was
added alongside, quantifying the split across the *whole* alphabet even where
the drawing is truncated. A test asserts the **drawn** tree contains both leaf
classes, not merely that the totals are non-zero.

### 8. Plain-language explainer — *reads the container only*
One sentence after compression, e.g. *"Saved 59.5%: 92% of the savings came
from 1052 structural blocks (averaging 4.7 bytes each), the rest from
single-byte Huffman codes."* Built from measured attribution: the container's
own bitstream is walked and bits are tallied per symbol class. A test asserts
the attribution **reconciles exactly** with the real coded stream. Raw-stored
files get an honest "stored raw" sentence instead of invented savings.

### 9. Compression presets — *sets existing constants, edits nothing*
Maps to `MIN_CANDIDATE_FREQ`, `MERGE_ROUNDS_V4`, `DP_ROUNDS` and `OPTS["dp"]`
through a context manager that restores every value afterwards (tested).

Measured on `cp.html`, logged by the test suite:

| Preset | Size | Time | Backend |
|---|---:|---:|---|
| Fast | 12 781 B | 87 ms | pure Python |
| Balanced | 10 869 B | 25 ms | C++ native |
| Maximum | 10 837 B | 741 ms | pure Python |

*Maximum was retuned during development.* The first attempt lowered
`MIN_CANDIDATE_FREQ` to 3, which admitted weak candidates and came out
**larger** than Balanced on two of three files (+0.17%, +2.95%). Keeping the
floor at 4 and only deepening the search makes Maximum never worse than
Balanced (up to −3.65% on `data.json`). Do not lower that floor again without
re-measuring.

### 10–13. Smaller additions
* **System status** — engine version, native vs pure-Python, uptime, DB name,
  cached results; supports the ISO 5055 maintainability criterion.
* **File preview** — text head or hex dump, chosen by sniffing the first 4 KB.
* **Batch notification** — a transient in-app toast when a queue finishes.
* **Dark mode** — class-based Tailwind variant, preference in `localStorage`
  only (never sent to the server), applied before first paint to avoid a flash.

## A bug this work exposed in Part 1's JavaScript

Part 1's file-input handler clears `input.value` after queueing (so the same
file can be re-picked). Part 2's entropy/preview handler was registered later
and therefore ran *after* that clear, always seeing zero files — the panel
stayed blank. Fixed with a **capture-phase listener on `document`**, which
fires before any listener bound to the target element. Browser-verified.

## Test coverage (`python tests/test_app.py` — 122 checks, all passing)

Part 1's 49 checks still pass unchanged. Part 2 adds, among others: analytics
totals reconciled row-by-row against `compression_history`; extension counts
matched against real filenames; gzip reference proven to be a real measurement
(differs from the AFC number); filter/pagination/sort including an injection
attempt; entropy across three file types with a band-difference assertion;
tree + attribution across three file types with exact bitstream
reconciliation; drawn-tree hybrid visibility; preset size/time/lossless
ordering with numbers printed to the log; constant restoration; preset
persistence; cross-user isolation on compare and analytics scope; status and
preview; and a regression block re-checking every Part 1 page and the archive
flow.

Browser-verified separately (Playwright/Chromium): stat cards, 3-series chart,
file-type table, entropy panel with histogram, preview, tree with both leaf
colours, explainer, attribution bar, completion toast, dark-mode persistence
across reload, and history filtering.
