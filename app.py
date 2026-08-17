#!/usr/bin/env python3
"""
app.py — AFC Thesis Dashboard, Flask application (Parts 1 + 2).

MAP OF THE APPLICATION (read this first if you are picking it up cold)
----------------------------------------------------------------------
  config.py    every tunable, incl. the file-size policy constants
  db.py        SQLite access layer (users, compression_history, audit_log)
  auth.py      blueprint: /login /logout /register /change-password
  admin.py     blueprint: /admin/users /admin/audit  (role-gated, 403)
  afcpak.py    .afcpak multi-file archive (manifest + AFC payloads, no DEFLATE)
  app.py       this file: app factory, page routes, and the file-processing API

  Engine modules (afc.py / afc2.py / afc_native.*) are READ-ONLY to this layer.
  Nothing here changes compression behaviour; the app only calls
  afc2.compress_bytes / afc2.decompress_bytes and reports what comes back.

SEPARATE COMPRESS AND DECOMPRESS PAGES
--------------------------------------
  The two operations are two destinations in the primary navigation, never a
  mode switch inside one upload box:

      GET /compress    "Compress Files"    normal file  -> .afc
      GET /decompress  "Decompress Files"  .afc         -> original file

  This is a UI/UX split ONLY. Both pages call the SAME engine entry points
  (afc2.compress_bytes / afc2.decompress_bytes) through the same helpers used
  before the split; there is no second engine, no duplicated pipeline and no
  format-specific compressor. `filetypes.py` names a byte stream so the
  Decompress page can hand `MyDocument.afc` back as `MyDocument.pdf` — it
  compresses nothing.

ROUTES AVAILABLE TO BUILD ON IN PART 2
--------------------------------------
  GET  /                     -> dashboard (stat cards + recent files)
  GET  /compress             -> Compress Files (single + queue + archive)
  GET  /decompress           -> Decompress Files (single + extract archive)
  POST /api/decompress       -> .afc -> original, with SHA-256 verification
  GET  /files                -> history table for the current user
  GET  /settings             -> account settings
  GET  /admin/users          -> admin only
  GET  /admin/audit          -> admin only
  GET  /api/config           -> size limits + versions (UI reads limits here)
  POST /api/compress         -> single file; returns metrics + download token
  POST /api/batch            -> one file of a batch (client drives the queue)
  POST /api/archive/create   -> many files -> one .afcpak
  POST /api/archive/extract  -> .afcpak -> manifest + per-file download tokens
  GET  /api/history          -> JSON history (Part 2 analytics can use this)
  GET  /api/stats            -> JSON aggregates (Part 2 analytics)
  GET  /download/<token>     -> fetch a produced artefact
  GET  /report.csv|/report.pdf -> exports for the current run or history

PART 2 ROUTES (analytics + algorithm showcase)
----------------------------------------------
  GET  /analytics                 -> stat cards, charts, file-type breakdown
  GET  /compare                   -> side-by-side diff of two history entries
  GET  /api/analytics/summary     -> full stats (?scope=system for admins)
  GET  /api/analytics/extensions  -> file-type distribution
  GET  /api/analytics/timeseries  -> AFC vs gzip vs Huffman ratios over time
  GET  /api/history/search        -> filtered/sorted/paginated history
  POST /api/entropy               -> pre-compression compressibility estimate
  POST /api/preview               -> text head / hex view before compressing
  GET  /api/tree/<token>          -> hybrid Huffman tree of a produced file
  GET  /api/presets               -> Fast/Balanced/Maximum descriptions
  GET  /api/status                -> engine mode, versions, uptime

PART 2 DESIGN NOTES
-------------------
  * analysis.py does all container/entropy introspection READ-ONLY; the
    engine still exposes no stats API and was not modified.
  * presets.py maps Fast/Balanced/Maximum onto afc2's existing tunables.
    MEASURED CAVEAT: the C++ core compiles those constants in and ignores the
    Python values, so non-default presets force the pure-Python path. See the
    presets.py docstring before changing this.
  * gzip appears ONLY as a reference measurement for the comparison chart. It
    never produces user output.
"""
import csv
import hashlib
import sys
import io
import os
import time
import uuid
from datetime import timedelta

from flask import (Blueprint, Flask, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)

import afc          # engine: baseline control + decoder   (READ ONLY)
import afc2 as engine  # engine: adaptive pipeline          (READ ONLY)
import afcpak
import analysis     # Part 2: read-only introspection of inputs/containers
import auth
import config
import db
import filetypes   # content sniffing so restored files regain their real name
import presets     # Part 2: Fast/Balanced/Maximum tunable presets

APP_STARTED_AT = time.time()

main = Blueprint("main", __name__)

# In-memory artefact store: token -> (download_name, bytes, mimetype).
# Deliberately NOT persisted — produced files live only for the session that
# made them, matching the local/non-cloud delimitation (SCOPE_NOTES.md).
RESULTS = {}
MAX_KEEP = 60


def _stash(name, blob, mimetype="application/octet-stream"):
    token = uuid.uuid4().hex
    RESULTS[token] = (name, blob, mimetype)
    while len(RESULTS) > MAX_KEEP:
        RESULTS.pop(next(iter(RESULTS)))
    return token


def engine_name():
    return "C++ native" if engine.NATIVE else "pure Python"


# ---------------------------------------------------------------------------
# size policy — one place, used by every upload path
# ---------------------------------------------------------------------------

def size_error(nbytes, total_batch=None):
    """Return a human error string if the size policy rejects this input.

    Every number in the message comes from config.py (constraint #5) — do not
    inline literals here."""
    if nbytes <= 0:
        return "That file is empty (0 bytes)."
    if nbytes < config.MIN_FILE_SIZE:
        return ("File is %s; the minimum accepted size is %s."
                % (human(nbytes), human(config.MIN_FILE_SIZE)))
    if nbytes > config.MAX_FILE_SIZE:
        return ("File is %s; the maximum accepted size is %s. This ceiling is "
                "the size the engine is documented as tested to (see "
                "SIZE_POLICY.md)." % (human(nbytes), human(config.MAX_FILE_SIZE)))
    if total_batch is not None and total_batch > config.MAX_BATCH_SIZE:
        return ("Batch totals %s; the maximum accepted batch is %s."
                % (human(total_batch), human(config.MAX_BATCH_SIZE)))
    return None


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GB" % n


# ---------------------------------------------------------------------------
# page routes
# ---------------------------------------------------------------------------

@main.route("/")
@auth.login_required
def dashboard():
    stats = db.history_stats(g.user["id"])
    recent = db.list_history(g.user["id"], limit=10)
    return render_template("dashboard.html", stats=stats, recent=recent)


@main.route("/compress")
@auth.login_required
def compress_page():
    """Compress Files — normal file in, .afc out. Compression only."""
    return render_template("compress.html")


@main.route("/decompress")
@auth.login_required
def decompress_page():
    """Decompress Files — .afc in, original file out. Decompression only.

    A separate destination in the primary navigation rather than a mode inside
    the compress box, so a first-time user never has to work out which
    operation an ambiguous upload will perform."""
    return render_template("decompress.html")


@main.route("/files")
@auth.login_required
def files_page():
    rows = db.list_history(g.user["id"], limit=500)
    return render_template("files.html", rows=rows,
                           stats=db.history_stats(g.user["id"]))


@main.route("/settings")
@auth.login_required
def settings_page():
    return render_template("settings.html")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@main.get("/api/config")
def api_config():
    """The client reads every limit from here so no cap is duplicated in JS."""
    return jsonify(config.public_dict())


@main.get("/api/history")
@auth.login_required
def api_history():
    rows = db.list_history(g.user["id"], limit=int(request.args.get("limit", 500)))
    return jsonify([dict(r) for r in rows])


@main.get("/api/stats")
@auth.login_required
def api_stats():
    return jsonify(db.history_stats(g.user["id"]))


def _reference_sizes(data):
    """Measure gzip and single-tier Huffman sizes for the dashboard charts.

    REFERENCE ONLY.  gzip is never used to produce user output; it is measured
    so the thesis can show an honest comparison. Skipped above
    config.REFERENCE_CODEC_MAX_BYTES so large uploads stay responsive — the
    columns then stay 0 and the chart shows a gap rather than a made-up value.
    """
    if len(data) > config.REFERENCE_CODEC_MAX_BYTES:
        return 0, 0
    try:
        import gzip as _gzip
        gz = len(_gzip.compress(data, 6))
    except Exception:
        gz = 0
    try:
        hz = len(afc.compress_bytes(data, False))   # engine's own baseline
    except Exception:
        hz = 0
    return gz, hz


def _process_one(data, filename, fmt, adaptive, batch_id=None,
                 preset=presets.DEFAULT_PRESET):
    """Compress OR decompress one payload and record it. Returns a result dict.

    This is the single code path used by /api/compress and /api/batch so the
    two can never drift apart."""
    is_container = data[:4] in (b"AFC1", b"AFC2")
    t0 = time.perf_counter()
    if is_container:
        restored = engine.decompress_bytes(data)
        elapsed = (time.perf_counter() - t0) * 1000
        # Recover the real name by sniffing the restored bytes, so a queued
        # `report.afc` comes back as `report.pdf` and not as `report`.
        kind = filetypes.sniff(restored)
        name = filetypes.restored_name(filename, restored)
        token = _stash(name, restored, mimetype=kind["mime"])
        db.add_history(g.user["id"], filename, len(restored), len(data),
                       engine_name(), data[:4].decode("ascii", "replace"),
                       lossless_verified=True, operation="decompress",
                       duration_ms=elapsed, batch_id=batch_id,
                       sha256_original=hashlib.sha256(restored).hexdigest(),
                       sha256_container=hashlib.sha256(data).hexdigest(),
                       detected_type=kind["label"])
        return {"kind": "decompress", "name": filename,
                "output_name": name,
                "original": len(data), "restored": len(restored),
                "container": data[:4].decode("ascii", "replace"),
                "engine": engine_name(), "ms": round(elapsed, 1),
                "lossless": True, "token": token,
                "detected": kind["label"]}

    blob, used_preset, backend = presets.compress_with(data, preset, fmt=fmt,
                                                       adaptive=adaptive)
    elapsed = (time.perf_counter() - t0) * 1000
    # SHA-256 round trip — constraint #3. Never reported optimistically.
    sha_original = hashlib.sha256(data).hexdigest()
    lossless = (hashlib.sha256(engine.decompress_bytes(blob)).hexdigest()
                == sha_original)
    sha_container = hashlib.sha256(blob).hexdigest()
    kind = filetypes.sniff(data)
    # `report.pdf` -> `report.pdf.afc`, so the original extension survives in
    # the name as well as in the sniffable bytes. Decompress restores either
    # way; carrying it here just makes the round trip obvious to the user.
    token = _stash(filename + ".afc", blob)

    # Read-only analytics derived from the input and the produced container.
    gz, hz = _reference_sizes(data)
    try:
        ent = analysis.entropy_report(
            data, sample_limit=config.ENTROPY_SAMPLE_BYTES)
        entropy_bits = ent["entropy_bits_per_byte"]
    except Exception:
        entropy_bits = 0.0
    try:
        attr = analysis.attribution_report(blob)
        block_share = 0.0 if attr.get("raw") else attr["block"]["share_pct"]
        explain = analysis.explain(blob, len(data))
    except Exception:
        block_share, explain = 0.0, ""

    db.add_history(g.user["id"], filename, len(data), len(blob), backend,
                   blob[:4].decode("ascii", "replace"), lossless_verified=lossless,
                   operation="compress", duration_ms=elapsed, batch_id=batch_id,
                   gzip_bytes=gz, huffman_bytes=hz, entropy_bits=entropy_bits,
                   block_share_pct=block_share, preset=used_preset,
                   sha256_original=sha_original, sha256_container=sha_container,
                   detected_type=kind["label"])
    return {"kind": "compress", "name": filename,
            "output_name": filename + ".afc",
            "original": len(data), "compressed": len(blob),
            "ratio": round(len(data) / len(blob), 3) if blob else 0,
            "saved": round(100.0 * (1 - len(blob) / len(data)), 2) if data else 0,
            "container": blob[:4].decode("ascii", "replace"),
            "engine": backend, "ms": round(elapsed, 1),
            "lossless": lossless, "token": token,
            "preset": used_preset, "explain": explain,
            "entropy_bits": entropy_bits, "block_share_pct": block_share,
            "gzip_bytes": gz, "huffman_bytes": hz,
            "detected": kind["label"], "family": kind["family"],
            "container_aware": kind["container_aware"],
            "sha256_original": sha_original, "sha256_container": sha_container}


@main.post("/api/compress")
@auth.login_required
def api_compress():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No file selected. Choose a file first."), 400
    data = f.read()
    err = size_error(len(data))
    if err:
        return jsonify(error=err), 400
    fmt = request.form.get("format", "auto")
    if fmt not in ("auto", "afc1", "afc2"):
        fmt = "auto"
    adaptive = request.form.get("mode", "adaptive") != "baseline"
    preset = request.form.get("preset", presets.DEFAULT_PRESET)
    action = request.form.get("action", "auto")
    is_container = data[:4] in (b"AFC1", b"AFC2")
    if action == "decompress" and not is_container:
        return jsonify(error="Decompress was selected, but this file is not an "
                             "AFC container."), 400
    try:
        if action == "compress" and is_container:
            # explicit nested compression
            blob = engine.compress_bytes(data, adaptive, fmt=fmt)
            lossless = engine.decompress_bytes(blob) == data
            token = _stash(f.filename + ".afc", blob)
            db.add_history(g.user["id"], f.filename, len(data), len(blob),
                           engine_name(), blob[:4].decode("ascii", "replace"),
                           lossless_verified=lossless, operation="compress")
            return jsonify(kind="compress", name=f.filename,
                           original=len(data), compressed=len(blob),
                           ratio=round(len(data) / len(blob), 3),
                           saved=round(100.0 * (1 - len(blob) / len(data)), 2),
                           container=blob[:4].decode("ascii", "replace"),
                           engine=engine_name(), lossless=lossless, token=token)
        result = _process_one(data, f.filename, fmt, adaptive, preset=preset)
    except Exception as exc:                       # never 500 to the user
        return jsonify(error="Processing failed: %s" % exc), 500

    if result["kind"] == "compress":
        # single-file view also shows the baseline control for the byte ruler
        try:
            result["baseline"] = len(afc.compress_bytes(data, False))
        except Exception:
            result["baseline"] = result["compressed"]
    return jsonify(**result)


@main.post("/api/batch")
@auth.login_required
def api_batch():
    """One file of a queued batch.

    The QUEUE IS DRIVEN BY THE CLIENT, one request per file, sequentially
    (config.MAX_CONCURRENT_JOBS == 1).  Rationale: the native core already
    threads internally, and the DP parse peaks near 19x the input size
    (measured — SIZE_POLICY.md), so running files concurrently multiplies peak
    RSS rather than saving wall-clock time.  Raising the cap means accounting
    for that multiplier."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No file in this batch slot."), 400
    data = f.read()
    batch_total = request.form.get("batch_total_bytes", type=int)
    err = size_error(len(data), total_batch=batch_total)
    if err:
        return jsonify(error=err, name=f.filename), 400
    fmt = request.form.get("format", "auto")
    if fmt not in ("auto", "afc1", "afc2"):
        fmt = "auto"
    adaptive = request.form.get("mode", "adaptive") != "baseline"
    batch_id = request.form.get("batch_id") or None
    preset = request.form.get("preset", presets.DEFAULT_PRESET)
    try:
        return jsonify(**_process_one(data, f.filename, fmt, adaptive,
                                      batch_id=batch_id, preset=preset))
    except Exception as exc:
        return jsonify(error="Processing failed: %s" % exc,
                       name=f.filename), 500


_MODE_NAMES = {afc.MODE_RAW: "raw stored (incompressible input)",
               afc.MODE_BASELINE: "baseline single-tier Huffman",
               afc.MODE_ADAPTIVE: "adaptive hybrid Huffman"}


@main.post("/api/decompress")
@auth.login_required
def api_decompress():
    """Decompress-only endpoint backing the Decompress page.

    Calls exactly the same engine entry point the combined page always used
    (engine.decompress_bytes). What it adds is reporting, not decoding:
    container format/mode detection, a structural integrity check, SHA-256
    verification against the digest recorded when the container was made, and
    the original filename recovered by sniffing the restored bytes.

    Refuses anything that is not an AFC container and says which page to use
    instead — the two workflows never silently swap.
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No file selected. Choose an .afc file first."), 400
    data = f.read()
    err = size_error(len(data))
    if err:
        return jsonify(error=err), 400

    if data[:4] not in (b"AFC1", b"AFC2"):
        info = filetypes.sniff(data)
        if afcpak.is_archive(data):
            return jsonify(
                error="That is a %s archive, not a single AFC container. Use "
                      "‘Extract archive’ below." % config.ARCHIVE_EXT,
                detected=info["label"], wrong_tool=True), 400
        return jsonify(
            error="This is not an AFC container — it looks like %s. The "
                  "Decompress page only accepts .afc files; use the Compress "
                  "page to turn this into one." % info["label"],
            detected=info["label"], wrong_page=True), 400

    # Container identity, read before decoding so a corrupt payload still
    # reports what it claimed to be.
    container = data[:4].decode("ascii", "replace")
    declared = None
    mode_name = ""
    try:
        meta = analysis.parse_container(data)
        declared = meta.get("original_size")
        mode_name = _MODE_NAMES.get(meta.get("mode"), "")
    except Exception:
        pass

    t0 = time.perf_counter()
    try:
        restored = engine.decompress_bytes(data)
    except Exception as exc:
        return jsonify(error="This AFC file could not be decoded: %s. It may "
                             "be truncated or corrupt." % exc,
                       container=container), 400
    elapsed = (time.perf_counter() - t0) * 1000

    # Integrity: the header declares the original length independently of the
    # payload, so a mismatch means a corrupt or doctored container.
    size_ok = (declared is None) or (len(restored) == declared)

    sha_restored = hashlib.sha256(restored).hexdigest()
    sha_container = hashlib.sha256(data).hexdigest()

    # SHA-256 verification against a REAL reference. We only claim a match
    # when this app recorded the original's digest at compress time; with no
    # record we say so rather than inventing a verdict.
    ref = db.find_by_container_sha(g.user["id"], sha_container)
    if ref is not None and ref["sha256_original"]:
        sha_match = (sha_restored == ref["sha256_original"])
        sha_status = "match" if sha_match else "mismatch"
        sha_note = ("Matches the SHA-256 recorded when this file was "
                    "compressed here." if sha_match else
                    "DOES NOT match the digest recorded at compression time.")
    else:
        sha_match = None
        sha_status = "no_reference"
        sha_note = ("No digest on record for this container — it was not "
                    "compressed by this account. The restored file's own "
                    "SHA-256 is shown above; compare it with your source.")

    kind = filetypes.sniff(restored)
    out_name = filetypes.restored_name(f.filename, restored)
    token = _stash(out_name, restored, mimetype=kind["mime"])

    verified = bool(size_ok) and (sha_match is not False)
    db.add_history(g.user["id"], f.filename, len(restored), len(data),
                   engine_name(), container, lossless_verified=verified,
                   operation="decompress", duration_ms=elapsed,
                   sha256_original=sha_restored, sha256_container=sha_container,
                   detected_type=kind["label"])

    return jsonify(
        kind="decompress",
        afc_name=f.filename, afc_bytes=len(data),
        restored_name=out_name, restored_bytes=len(restored),
        container=container, container_mode=mode_name,
        declared_bytes=declared,
        ms=round(elapsed, 2), engine=engine_name(),
        integrity_ok=bool(size_ok),
        integrity_note=(
            "Restored %d bytes, exactly the length declared in the %s header."
            % (len(restored), container) if size_ok else
            "Length mismatch: the %s header declares %s bytes but decoding "
            "produced %d." % (container, declared, len(restored))),
        sha256_restored=sha_restored, sha256_container=sha_container,
        sha256_status=sha_status, sha256_match=sha_match, sha256_note=sha_note,
        detected=kind["label"], family=kind["family"],
        container_aware=kind["container_aware"],
        token=token)


@main.post("/api/archive/create")
@auth.login_required
def api_archive_create():
    """Many files (or a picked folder) -> one .afcpak.

    Each member is compressed independently by the engine; afcpak only writes
    a manifest and concatenates the AFC payloads (no DEFLATE, no cross-file
    references)."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files selected for the archive."), 400
    payload = []
    total = 0
    for f in files:
        data = f.read()
        if not data:
            continue                                  # skip empty/folder stubs
        total += len(data)
        err = size_error(len(data), total_batch=total)
        if err:
            return jsonify(error="%s: %s" % (f.filename, err)), 400
        # webkitdirectory sends the relative path in the filename field
        payload.append((f.filename, data))
    if not payload:
        return jsonify(error="Every selected entry was empty."), 400

    fmt = request.form.get("format", "auto")
    if fmt not in ("auto", "afc1", "afc2"):
        fmt = "auto"
    name = (request.form.get("archive_name") or "archive").strip() or "archive"
    try:
        blob, entries = afcpak.pack(payload, fmt=fmt)
    except afcpak.ArchiveError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error="Archive creation failed: %s" % exc), 500

    batch_id = db.new_batch_id()
    for ent in entries:
        db.add_history(g.user["id"], ent["path"], ent["original_bytes"],
                       ent["stored_bytes"], engine_name(), ent["container"],
                       lossless_verified=ent["lossless_verified"],
                       operation="compress", batch_id=batch_id)
    token = _stash(name + config.ARCHIVE_EXT, blob)
    return jsonify(kind="archive", name=name + config.ARCHIVE_EXT,
                   files=len(entries), original=total, compressed=len(blob),
                   ratio=round(total / len(blob), 3) if blob else 0,
                   saved=round(100.0 * (1 - len(blob) / total), 2) if total else 0,
                   engine=engine_name(), batch_id=batch_id, token=token,
                   entries=entries)


@main.post("/api/archive/extract")
@auth.login_required
def api_archive_extract():
    """Read a .afcpak and return the manifest plus a download token per member,
    so the browser can rebuild the folder structure."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No archive selected."), 400
    blob = f.read()
    if not afcpak.is_archive(blob):
        return jsonify(error="That file is not a %s archive."
                             % config.ARCHIVE_EXT), 400
    try:
        members = afcpak.unpack(blob, verify=True)
    except afcpak.ArchiveError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error="Extraction failed: %s" % exc), 500

    batch_id = db.new_batch_id()
    out = []
    for m in members:
        token = _stash(os.path.basename(m["path"]), m["data"])
        db.add_history(g.user["id"], m["path"], m["original_bytes"], 0,
                       engine_name(), m["container"],
                       lossless_verified=bool(m["sha256_ok"]),
                       operation="decompress", batch_id=batch_id)
        out.append({"path": m["path"], "bytes": m["original_bytes"],
                    "sha256_ok": m["sha256_ok"], "token": token})
    return jsonify(kind="extract", files=len(out), batch_id=batch_id,
                   members=out)


# ===========================================================================
# PART 2 — analytics, algorithm showcase, and small additions
# ===========================================================================
# Everything here reads the LOCAL SQLite database or performs read-only
# analysis of a container. No engine file is modified (constraint #1) and no
# new datastore is introduced (constraint #4).

def _admin_scope():
    """Admins may request system-wide analytics with ?scope=system.
    Anyone else is silently restricted to their own rows — the scope parameter
    can never be used to widen access."""
    want_system = request.args.get("scope") == "system"
    if want_system and g.user["role"] == "admin":
        return None, True
    return g.user["id"], False


@main.route("/analytics")
@auth.login_required
def analytics_page():
    return render_template("analytics.html",
                           presets=presets.describe(),
                           is_admin=(g.user["role"] == "admin"))


@main.route("/compare")
@auth.login_required
def compare_page():
    """Feature 5 — side-by-side diff of two history entries."""
    rows = db.list_history(g.user["id"], limit=200)
    a = request.args.get("a", type=int)
    b = request.args.get("b", type=int)
    left = db.get_history_row(a, g.user["id"]) if a else None
    right = db.get_history_row(b, g.user["id"]) if b else None
    diff = None
    if left is not None and right is not None:
        def delta(x, y):
            return {"left": x, "right": y, "abs": y - x,
                    "pct": (100.0 * (y - x) / x) if x else 0.0}
        diff = {
            "original_bytes": delta(left["original_bytes"], right["original_bytes"]),
            "compressed_bytes": delta(left["compressed_bytes"], right["compressed_bytes"]),
            "ratio": delta(left["ratio"], right["ratio"]),
            "space_saved_pct": delta(left["space_saved_pct"], right["space_saved_pct"]),
            "duration_ms": delta(left["duration_ms"], right["duration_ms"]),
        }
    return render_template("compare.html", rows=rows, left=left, right=right,
                           diff=diff)


@main.get("/api/analytics/summary")
@auth.login_required
def api_analytics_summary():
    """Feature 1 — full stat cards. Time saved is a derived ESTIMATE from an
    assumed link speed, and is labelled as such in the UI."""
    uid, system = _admin_scope()
    stats = db.analytics_stats(uid)
    mbps = config.ASSUMED_LINK_MBPS
    seconds = (stats["total_saved"] * 8.0) / (mbps * 1_000_000) if mbps else 0.0
    stats["transfer_seconds_saved"] = round(seconds, 2)
    stats["assumed_link_mbps"] = mbps
    stats["scope"] = "system" if system else "user"
    stats["processing_seconds"] = round(stats.get("total_ms", 0) / 1000.0, 2)
    return jsonify(stats)


@main.get("/api/analytics/extensions")
@auth.login_required
def api_analytics_extensions():
    """Feature 3 — file-type distribution."""
    uid, _ = _admin_scope()
    return jsonify(db.analytics_by_extension(uid))


@main.get("/api/analytics/timeseries")
@auth.login_required
def api_analytics_timeseries():
    """Feature 2 — AFC vs gzip vs single-tier Huffman ratios over time.

    gzip and Huffman are REFERENCE MEASUREMENTS shown for context. They are
    not part of the compression pipeline and the UI does not claim AFC beats
    them; where AFC is behind, the chart shows that."""
    uid, _ = _admin_scope()
    return jsonify(db.analytics_timeseries(
        uid, limit=int(request.args.get("limit", 60))))


@main.get("/api/history/search")
@auth.login_required
def api_history_search():
    """Feature 4 — search / filter / sort / paginate the history table."""
    page = max(1, request.args.get("page", 1, type=int))
    # floor of 1: a caller asking for a small page must get one.
    per = min(100, max(1, request.args.get("per_page", 15, type=int)))
    rows, total = db.search_history(
        g.user["id"],
        q=request.args.get("q", "").strip(),
        date_from=request.args.get("from", "").strip(),
        date_to=request.args.get("to", "").strip(),
        engine=request.args.get("engine", "").strip(),
        preset=request.args.get("preset", "").strip(),
        limit=per, offset=(page - 1) * per,
        sort=request.args.get("sort", "created_at"),
        direction=request.args.get("dir", "desc"))
    return jsonify({
        "rows": [dict(r) for r in rows], "total": total, "page": page,
        "per_page": per, "pages": max(1, (total + per - 1) // per),
        "engines": db.distinct_engines(g.user["id"]),
    })


@main.post("/api/entropy")
@auth.login_required
def api_entropy():
    """Feature 6 — pre-compression compressibility estimate.

    Shannon entropy over the Tier-1 byte histogram, computed by reading the
    uploaded bytes. Read-only: no engine state is touched and nothing is
    stored. Called the moment a file is selected, before any compression."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No file supplied."), 400
    data = f.read(config.ENTROPY_SAMPLE_BYTES)
    if not data:
        return jsonify(error="That file is empty."), 400
    rep = analysis.entropy_report(data,
                                  sample_limit=config.ENTROPY_SAMPLE_BYTES)
    rep["filename"] = f.filename
    return jsonify(rep)


@main.post("/api/preview")
@auth.login_required
def api_preview():
    """Feature 11 — text head or hex view of a file before compressing."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="No file supplied."), 400
    head = f.read(65536)
    if not head:
        return jsonify(error="That file is empty."), 400
    # Automatic type detection, so the Compress page can name the file and
    # flag packaged formats (PDF/DOCX/XLSX/PPTX/ODF) whose internals are
    # handled for the user. Detection only labels — it never changes the
    # pipeline the bytes go through.
    kind = filetypes.sniff(head)
    # binary if it has NULs or a high share of non-text bytes in the head
    sample = head[:4096]
    textish = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126)
    is_binary = (b"\x00" in sample) or (textish / len(sample) < 0.85)
    if is_binary:
        lines = []
        for off in range(0, min(len(sample), 512), 16):
            chunk = sample[off:off + 16]
            hexs = " ".join("%02x" % b for b in chunk)
            asc = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
            lines.append("%08x  %-47s  %s" % (off, hexs, asc))
        return jsonify(kind="hex", filename=f.filename, lines=lines,
                       detected=kind["label"], family=kind["family"],
                       container_aware=kind["container_aware"])
    try:
        text = sample.decode("utf-8", "replace")
    except Exception:
        text = ""
    return jsonify(kind="text", filename=f.filename,
                   lines=text.splitlines()[:40],
                   detected=kind["label"], family=kind["family"],
                   container_aware=kind["container_aware"])


@main.get("/api/tree/<token>")
@auth.login_required
def api_tree(token):
    """Feature 7 — the actual hybrid Huffman tree for a produced container.

    Reads the container the user just made (held in memory for this session)
    and returns it with literal-byte and structural-block leaves tagged, so
    the UI can colour them differently."""
    item = RESULTS.get(token)
    if item is None:
        return jsonify(error="That result has expired. Compress the file "
                             "again to inspect its tree."), 404
    _, blob, _ = item
    try:
        depth = min(12, max(4, request.args.get("depth", 9, type=int)))
        tree = analysis.tree_report(blob, max_depth=depth)
        attr = analysis.attribution_report(blob)
    except Exception as exc:
        return jsonify(error="Could not read that container: %s" % exc), 400
    return jsonify(tree=tree, attribution=attr,
                   explain=analysis.explain(blob))


@main.get("/api/presets")
@auth.login_required
def api_presets():
    """Feature 9 — preset descriptions, including the honest note that only
    Balanced can use the native core."""
    return jsonify(presets=presets.describe(),
                   default=presets.DEFAULT_PRESET,
                   native_available=bool(engine.NATIVE))


@main.get("/api/status")
@auth.login_required
def api_status():
    """Feature 10 — system status panel (ISO 5055 maintainability evidence)."""
    up = time.time() - APP_STARTED_AT
    return jsonify(
        app_version=config.APP_VERSION,
        engine_version=config.ENGINE_VERSION,
        backend="C++ native" if engine.NATIVE else "pure Python",
        native_available=bool(engine.NATIVE),
        uptime_seconds=round(up, 1),
        uptime_human=_uptime_human(up),
        python_version=sys.version.split()[0],
        database=os.path.basename(config.DATABASE_PATH),
        archive_format=config.ARCHIVE_EXT,
        max_file_size=config.MAX_FILE_SIZE,
        max_batch_size=config.MAX_BATCH_SIZE,
        results_cached=len(RESULTS),
    )


def _uptime_human(sec):
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm %ds" % (m, s)
    return "%ds" % s


# ---------------------------------------------------------------------------
# downloads and reports
# ---------------------------------------------------------------------------

@main.get("/download/<token>")
@auth.login_required
def download(token):
    item = RESULTS.get(token)
    if item is None:
        abort(404)
    name, blob, mime = item
    return send_file(io.BytesIO(blob), as_attachment=True,
                     download_name=name, mimetype=mime)


def _report_rows(batch_id=None):
    rows = db.list_history(g.user["id"], limit=1000, batch_id=batch_id)
    return [r for r in rows if r["operation"] == "compress"]


@main.get("/report.csv")
@auth.login_required
def report_csv():
    rows = _report_rows(request.args.get("batch_id"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["AFC Compression Report"])
    w.writerow(["generated_utc", time.strftime("%Y-%m-%d %H:%M:%SZ",
                                               time.gmtime())])
    w.writerow(["app_version", config.APP_VERSION,
                "engine_version", config.ENGINE_VERSION])
    w.writerow(["user", g.user["username"]])
    w.writerow([])
    w.writerow(["filename", "original_bytes", "compressed_bytes", "ratio",
                "space_saved_pct", "engine", "container", "lossless_verified",
                "duration_ms", "created_at"])
    to = tc = 0
    for r in rows:
        to += r["original_bytes"]
        tc += r["compressed_bytes"]
        w.writerow([r["filename"], r["original_bytes"], r["compressed_bytes"],
                    "%.3f" % r["ratio"], "%.2f" % r["space_saved_pct"],
                    r["engine"], r["container_format"],
                    "yes" if r["lossless_verified"] else "NO",
                    "%.1f" % r["duration_ms"], r["created_at"]])
    w.writerow([])
    w.writerow(["TOTAL", to, tc,
                "%.3f" % (to / tc) if tc else "0",
                "%.2f" % (100.0 * (1 - tc / to)) if to else "0",
                "", "", "%d/%d verified" % (
                    sum(1 for r in rows if r["lossless_verified"]), len(rows)),
                "", ""])
    data = buf.getvalue().encode("utf-8")
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name="afc_report.csv", mimetype="text/csv")


@main.get("/report.pdf")
@auth.login_required
def report_pdf():
    """Print-stylesheet PDF: returns an HTML page that auto-opens the browser
    print dialog ("Save as PDF").  This keeps the dependency footprint at zero
    — no reportlab/weasyprint — which matters for a local, offline demo."""
    rows = _report_rows(request.args.get("batch_id"))
    to = sum(r["original_bytes"] for r in rows)
    tc = sum(r["compressed_bytes"] for r in rows)
    return render_template(
        "report.html", rows=rows, total_original=to, total_compressed=tc,
        overall_ratio=(to / tc) if tc else 0,
        saved_pct=(100.0 * (1 - tc / to)) if to else 0,
        verified=sum(1 for r in rows if r["lossless_verified"]),
        generated=time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()))


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app(db_path=None, testing=False):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=config.SESSION_LIFETIME_MINUTES)
    app.config["TESTING"] = testing
    # Local single-user demo app: a stable dev key is fine, but allow override.
    app.secret_key = os.environ.get("AFC_SECRET_KEY", "afc-local-dev-secret")
    if db_path:
        config.DATABASE_PATH = db_path

    db.init_db(config.DATABASE_PATH)
    app.teardown_appcontext(db.close_db)

    app.register_blueprint(auth.bp)
    app.register_blueprint(main)
    import admin
    app.register_blueprint(admin.bp)

    @app.before_request
    def _load_user():
        g.user = auth.current_user()
        # Force the password change before anything else is reachable.
        if g.user is not None and g.user["must_change_password"]:
            allowed = {"auth.change_password", "auth.logout", "static",
                       "main.api_config"}
            if request.endpoint not in allowed:
                return redirect(url_for("auth.change_password"))

    @app.context_processor
    def _inject():
        # Templates render limits from here — never a literal in the HTML.
        return {"cfg": config, "human": human, "current_user": g.get("user")}

    @app.errorhandler(401)
    def _401(_):
        if request.path.startswith("/api/"):
            return jsonify(error="Authentication required."), 401
        return redirect(url_for("auth.login"))

    @app.errorhandler(403)
    def _403(_):
        if request.path.startswith("/api/"):
            return jsonify(error="Forbidden: your role cannot access this."), 403
        return render_template("error.html", code=403,
                               message="Forbidden — your account role does not "
                                       "have access to that page."), 403

    @app.errorhandler(404)
    def _404(_):
        if request.path.startswith("/api/") or request.path.startswith("/download/"):
            return jsonify(error="Not found, or that download has expired."), 404
        return render_template("error.html", code=404,
                               message="Page not found."), 404

    @app.errorhandler(413)
    def _413(_):
        return jsonify(error="Upload exceeds the %s limit."
                             % human(config.MAX_CONTENT_LENGTH)), 413

    return app


app = create_app()

if __name__ == "__main__":
    import threading
    import webbrowser
    threading.Timer(1.2,
                    lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
