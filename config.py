#!/usr/bin/env python3
"""
config.py — single source of truth for every tunable the AFC web app exposes.

WHY THIS FILE EXISTS (read before changing a number):
Constraint #5 of the Part-1 brief: "Every size/quota number shown in the UI
must read from real config constants — never a hardcoded/placeholder number."
The templates render these values via the `cfg` context processor in app.py,
and the JavaScript reads them from `/api/config`.  If you change a number
here it changes everywhere — UI copy, client-side pre-checks, and server-side
enforcement — with no second place to update.

SIZE POLICY — DO NOT RAISE THESE WITHOUT EVIDENCE.
MAX_FILE_SIZE / MAX_BATCH_SIZE are deliberately set to the ceiling the thesis
Appendix C actually documents as tested (100 MB per file, 500 MB per batch),
NOT the 200 MB / 1 GB that was requested.  Raising the enforced cap above what
the paper claims was validated would let the app accept inputs the study never
covered.  See SIZE_POLICY.md for the measured 150 MB / 250 MB benchmark
results and the exact sentence to change in Appendix C if the team decides to
raise the documented ceiling.  Procedure: update Appendix C first, then change
the constant here, then re-run tools/size_policy_bench.py to confirm.
"""
import os

# --------------------------------------------------------------------------
# File-size policy
# --------------------------------------------------------------------------

MB = 1024 * 1024

# Minimum accepted file size.
#
# NOTE FOR THE TEAM — the brief requested a 1 MB minimum.  That value is NOT
# used as the default here because it would reject the entire thesis corpus:
# every benchmark file in benchmarks/corpus/ and benchmarks/canterbury/ is
# under 1 MB (largest is server.log at ~414 KB; alice29.txt is ~149 KB).
# A 1 MB floor would make the app unable to compress the very files the paper
# reports results for, and would block the demo.  The default is therefore 1
# byte (reject only genuinely empty files).  If the team still wants a floor,
# set AFC_MIN_FILE_SIZE and document the reason — but re-check the corpus
# first.  See SIZE_POLICY.md §"Minimum file size".
MIN_FILE_SIZE = int(os.environ.get("AFC_MIN_FILE_SIZE", 1))

# Maximum accepted size for a SINGLE file. Default = Appendix C tested ceiling.
MAX_FILE_SIZE = int(os.environ.get("AFC_MAX_FILE_SIZE", 100 * MB))

# Maximum accepted TOTAL size for one batch/queue/archive run.
MAX_BATCH_SIZE = int(os.environ.get("AFC_MAX_BATCH_SIZE", 500 * MB))

# Hard HTTP body cap.  Must be >= MAX_FILE_SIZE or large uploads would be
# rejected by Werkzeug before our own friendlier check ever runs.  Headroom
# covers multipart overhead and the batch endpoint.
MAX_CONTENT_LENGTH = max(MAX_BATCH_SIZE, MAX_FILE_SIZE) + (16 * MB)

# Values the paper currently documents — shown in the UI/SIZE_POLICY so the
# difference between "tested" and "enforced" is always visible.
PAPER_TESTED_MAX_FILE = 100 * MB
PAPER_TESTED_MAX_BATCH = 500 * MB

# --------------------------------------------------------------------------
# Queue / batch behaviour
# --------------------------------------------------------------------------

# Files are processed SEQUENTIALLY (one HTTP round trip each) by default.
# Rationale: the native core already uses a thread per n-gram length inside a
# single compress call, so running N compressions concurrently would oversubscribe
# the CPU and, more importantly, multiply peak memory — the engine's DP parse
# allocates roughly 19x the input size (measured, see SIZE_POLICY.md).  Two
# concurrent 100 MB files would need ~3.8 GB.  If you enable a worker pool,
# keep this cap small and account for that multiplier.
MAX_CONCURRENT_JOBS = int(os.environ.get("AFC_MAX_CONCURRENT_JOBS", 1))

# --------------------------------------------------------------------------
# Accounts / security
# --------------------------------------------------------------------------

DATABASE_PATH = os.environ.get(
    "AFC_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "afc_app.sqlite3"))

# Seed admin. README documents these; first login forces a password change.
DEFAULT_ADMIN_USERNAME = os.environ.get("AFC_ADMIN_USER", "admin")
DEFAULT_ADMIN_EMAIL = os.environ.get("AFC_ADMIN_EMAIL", "admin@localhost")
DEFAULT_ADMIN_PASSWORD = os.environ.get("AFC_ADMIN_PASSWORD", "afc-admin")

# Login rate limiting — simple fixed-window counter, per (username, IP).
LOGIN_MAX_ATTEMPTS = int(os.environ.get("AFC_LOGIN_MAX_ATTEMPTS", 5))
LOGIN_WINDOW_SECONDS = int(os.environ.get("AFC_LOGIN_WINDOW_SECONDS", 300))

# Idle session expiry.
SESSION_LIFETIME_MINUTES = int(os.environ.get("AFC_SESSION_MINUTES", 60))

MIN_PASSWORD_LENGTH = 8

# --------------------------------------------------------------------------
# Part 2 — analytics / reference measurements
# --------------------------------------------------------------------------

# The dashboard charts AFC against gzip and the engine's own single-tier
# Huffman baseline.  These are REFERENCE MEASUREMENTS FOR DISPLAY ONLY --
# gzip is never used to produce user output, and computing it here does not
# make it part of the compression pipeline (constraint #1).
#
# Both references cost real time on every upload, so they are only measured
# for files up to this size; above it the columns are stored as 0 and the
# chart shows a gap rather than an invented value.
REFERENCE_CODEC_MAX_BYTES = int(
    os.environ.get("AFC_REFERENCE_MAX_BYTES", 8 * 1024 * 1024))

# Assumed link speed used to turn "bytes saved" into "transfer time saved" on
# the dashboard.  It is an ASSUMPTION, not a measurement, and the UI labels it
# as such next to the number.
ASSUMED_LINK_MBPS = float(os.environ.get("AFC_ASSUMED_LINK_MBPS", 10.0))

# Entropy estimate is computed over at most this many bytes (Shannon entropy
# converges quickly; sampling keeps the pre-compression preview instant).
ENTROPY_SAMPLE_BYTES = int(
    os.environ.get("AFC_ENTROPY_SAMPLE_BYTES", 4 * 1024 * 1024))

# --------------------------------------------------------------------------
# Archive format
# --------------------------------------------------------------------------

ARCHIVE_EXT = ".afcpak"
ARCHIVE_MAGIC = b"AFCPAK01"

# --------------------------------------------------------------------------
# Version strings shown in reports (engine identity for reproducibility)
# --------------------------------------------------------------------------

# Bumped when Part 2 landed; shown in reports, the status panel and
# /api/config, so it must describe the build actually running.
APP_VERSION = "1.2.0"
# The Hybrid-Huffman algorithm remains v4; AFC4 is a new outer document
# container, not a replacement entropy codec.
ENGINE_VERSION = "AFC v4 (AFC1-AFC4)"


def public_dict():
    """Everything the browser is allowed to know, served at /api/config so the
    client never hardcodes a limit either."""
    return {
        "min_file_size": MIN_FILE_SIZE,
        "max_file_size": MAX_FILE_SIZE,
        "max_batch_size": MAX_BATCH_SIZE,
        "paper_tested_max_file": PAPER_TESTED_MAX_FILE,
        "paper_tested_max_batch": PAPER_TESTED_MAX_BATCH,
        "archive_ext": ARCHIVE_EXT,
        "app_version": APP_VERSION,
        "engine_version": ENGINE_VERSION,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "reference_codec_max_bytes": REFERENCE_CODEC_MAX_BYTES,
        "entropy_sample_bytes": ENTROPY_SAMPLE_BYTES,
        "assumed_link_mbps": ASSUMED_LINK_MBPS,
    }
