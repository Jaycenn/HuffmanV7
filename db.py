#!/usr/bin/env python3
"""
db.py — SQLite access layer for the AFC web app.

Design notes for whoever picks this up (Part 2):
  * Plain sqlite3 + hand-written SQL.  No ORM on purpose: the schema is five
    tables, the app ships to a student laptop, and an ORM would be a new heavy
    dependency for no benefit.
  * Connections are per-request, stored on flask.g, closed by teardown.
  * Every query returns sqlite3.Row, so callers use row["column"].
  * Writes go through helpers here, never raw SQL in a route — if Part 2 adds
    analytics, add a query function here and call it from the blueprint.
  * The DB file lives next to the app (config.DATABASE_PATH). It is LOCAL.
    See SCOPE_NOTES.md for the non-cloud justification.
"""
import os
import sqlite3
import time
import uuid

import config

try:
    from flask import g
except ImportError:                                    # tests may import bare
    g = None


# ---------------------------------------------------------------------------
# connection handling
# ---------------------------------------------------------------------------

def connect(path=None):
    """Open a new connection with sane defaults.  Callers outside a request
    (tests, CLI) should use this and close it themselves."""
    conn = sqlite3.connect(path or config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps readers from blocking the writer; harmless for a local file.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    """Per-request connection cached on flask.g."""
    if g is None:
        return connect()
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    if g is None:
        return
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# schema management
# ---------------------------------------------------------------------------

def init_db(path=None, seed_admin=True):
    """Create tables if missing and seed the default admin.  Idempotent."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "schema.sql"), encoding="utf-8") as f:
        ddl = f.read()
    conn = connect(path)
    try:
        conn.executescript(ddl)
        conn.commit()
        _migrate(conn)
        if seed_admin:
            _seed_admin(conn)
    finally:
        conn.close()


# Part 2 added columns to compression_history.  Existing Part 1 databases are
# upgraded in place here rather than requiring a reset -- ALTER TABLE ADD
# COLUMN is cheap and SQLite tolerates it online.  Add future columns to this
# list; it is idempotent.
_MIGRATIONS = [
    ("gzip_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("huffman_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("entropy_bits", "REAL NOT NULL DEFAULT 0"),
    ("block_share_pct", "REAL NOT NULL DEFAULT 0"),
    ("preset", "TEXT NOT NULL DEFAULT ''"),
    # Separate Compress/Decompress pages: the Decompress page proves integrity
    # by comparing the restored bytes against the digest recorded when the
    # container was made. Both are hex SHA-256, '' when not applicable.
    ("sha256_original", "TEXT NOT NULL DEFAULT ''"),
    ("sha256_container", "TEXT NOT NULL DEFAULT ''"),
    ("detected_type", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(conn):
    have = {r["name"] for r in
            conn.execute("PRAGMA table_info(compression_history)")}
    for col, decl in _MIGRATIONS:
        if col not in have:
            conn.execute("ALTER TABLE compression_history ADD COLUMN %s %s"
                         % (col, decl))
    conn.commit()


def reset_db(path=None):
    """DESTRUCTIVE: delete the database file and recreate it from schema.sql.
    README documents this as the supported reset procedure."""
    target = path or config.DATABASE_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(target + suffix)
        except OSError:
            pass
    init_db(target)


def _seed_admin(conn):
    """Create the default admin exactly once.  must_change_password=1 forces a
    change at first login (see auth.py), so the documented default credential
    cannot survive real use."""
    from werkzeug.security import generate_password_hash
    row = conn.execute("SELECT id FROM users WHERE username = ?",
                       (config.DEFAULT_ADMIN_USERNAME,)).fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO users (username, email, password_hash, role,"
        " must_change_password) VALUES (?, ?, ?, 'admin', 1)",
        (config.DEFAULT_ADMIN_USERNAME, config.DEFAULT_ADMIN_EMAIL,
         generate_password_hash(config.DEFAULT_ADMIN_PASSWORD)))
    conn.commit()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def get_user_by_id(uid):
    return get_db().execute("SELECT * FROM users WHERE id = ?",
                            (uid,)).fetchone()


def get_user_by_username(username):
    return get_db().execute("SELECT * FROM users WHERE username = ?",
                            (username,)).fetchone()


def get_user_by_email(email):
    return get_db().execute("SELECT * FROM users WHERE email = ?",
                            (email,)).fetchone()


def create_user(username, email, password, role="user",
                must_change_password=0):
    from werkzeug.security import generate_password_hash
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash, role,"
        " must_change_password) VALUES (?, ?, ?, ?, ?)",
        (username, email, generate_password_hash(password), role,
         int(must_change_password)))
    conn.commit()
    return cur.lastrowid


def set_password(user_id, password):
    from werkzeug.security import generate_password_hash
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0"
                 " WHERE id = ?", (generate_password_hash(password), user_id))
    conn.commit()


def touch_last_login(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET last_login_at = datetime('now')"
                 " WHERE id = ?", (user_id,))
    conn.commit()


def set_user_active(user_id, active):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?",
                 (1 if active else 0, user_id))
    conn.commit()


def list_users():
    """Admin user-list view.  Includes per-user aggregates so the template does
    not need a second query per row."""
    return get_db().execute("""
        SELECT u.id, u.username, u.email, u.role, u.is_active, u.created_at,
               u.last_login_at,
               COUNT(h.id)                        AS file_count,
               COALESCE(SUM(h.original_bytes), 0) AS total_original,
               COALESCE(SUM(h.compressed_bytes), 0) AS total_compressed
        FROM users u
        LEFT JOIN compression_history h
               ON h.user_id = u.id AND h.operation = 'compress'
        GROUP BY u.id
        ORDER BY u.created_at
    """).fetchall()


# ---------------------------------------------------------------------------
# compression history
# ---------------------------------------------------------------------------

def new_batch_id():
    return uuid.uuid4().hex


def add_history(user_id, filename, original_bytes, compressed_bytes,
                engine, container_format="", lossless_verified=False,
                operation="compress", duration_ms=0.0, batch_id=None,
                gzip_bytes=0, huffman_bytes=0, entropy_bits=0.0,
                block_share_pct=0.0, preset="", sha256_original="",
                sha256_container="", detected_type=""):
    """Record one processed file.  ratio and space_saved_pct are derived here
    so every caller reports them identically."""
    orig = max(0, int(original_bytes))
    comp = max(0, int(compressed_bytes))
    ratio = (orig / comp) if comp else 0.0
    saved = (100.0 * (1 - comp / orig)) if orig else 0.0
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO compression_history (user_id, filename, operation,"
        " original_bytes, compressed_bytes, ratio, space_saved_pct, engine,"
        " container_format, lossless_verified, duration_ms, batch_id,"
        " gzip_bytes, huffman_bytes, entropy_bits, block_share_pct, preset,"
        " sha256_original, sha256_container, detected_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, filename, operation, orig, comp, ratio, saved, engine,
         container_format, 1 if lossless_verified else 0, float(duration_ms),
         batch_id, int(gzip_bytes), int(huffman_bytes), float(entropy_bits),
         float(block_share_pct), preset, sha256_original or "",
         sha256_container or "", detected_type or ""))
    conn.commit()
    return cur.lastrowid


def find_by_container_sha(user_id, digest):
    """Most recent compress row whose PRODUCED container has this SHA-256.

    Used by the Decompress page to recover the original file's digest so the
    restored bytes can be checked against a real reference instead of an
    assumption. Scoped to the owner: one user's containers never resolve
    against another's history. Returns None when there is no record — the
    caller must then report 'no reference on file', never a fake match.
    """
    if not digest:
        return None
    return get_db().execute(
        "SELECT * FROM compression_history WHERE user_id = ?"
        " AND sha256_container = ? AND operation = 'compress'"
        " ORDER BY id DESC LIMIT 1", (user_id, digest)).fetchone()


def list_history(user_id, limit=200, offset=0, batch_id=None):
    sql = "SELECT * FROM compression_history WHERE user_id = ?"
    args = [user_id]
    if batch_id:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return get_db().execute(sql, args).fetchall()


def history_stats(user_id):
    """Stat-card aggregates for the dashboard.  Part 2 builds its charts on
    top of this — extend here rather than querying from a template."""
    row = get_db().execute("""
        SELECT COUNT(*)                            AS files,
               COALESCE(SUM(original_bytes), 0)    AS total_original,
               COALESCE(SUM(compressed_bytes), 0)  AS total_compressed,
               COALESCE(AVG(ratio), 0)             AS avg_ratio,
               COALESCE(SUM(lossless_verified), 0) AS lossless_count
        FROM compression_history
        WHERE user_id = ? AND operation = 'compress'
    """, (user_id,)).fetchone()
    d = dict(row)
    d["total_saved"] = d["total_original"] - d["total_compressed"]
    d["overall_ratio"] = (d["total_original"] / d["total_compressed"]
                          if d["total_compressed"] else 0.0)
    d["saved_pct"] = (100.0 * d["total_saved"] / d["total_original"]
                      if d["total_original"] else 0.0)
    return d


def delete_history_row(row_id, user_id):
    """Scoped by user_id so one user can never delete another's row."""
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM compression_history WHERE id = ? AND user_id = ?",
        (row_id, user_id))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------

def audit(event, user_id=None, username="", detail="", ip_address=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (user_id, username, event, detail, ip_address)"
        " VALUES (?,?,?,?,?)",
        (user_id, username or "", event, detail, ip_address or ""))
    conn.commit()


def list_audit(limit=200):
    return get_db().execute(
        "SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,)).fetchall()


# ---------------------------------------------------------------------------
# login rate limiting (fixed window)
# ---------------------------------------------------------------------------

def record_login_failure(username, ip_address=""):
    conn = get_db()
    conn.execute("INSERT INTO login_attempts (username, ip_address, created_at)"
                 " VALUES (?,?,?)", (username, ip_address or "", time.time()))
    conn.commit()


def clear_login_failures(username, ip_address=""):
    conn = get_db()
    conn.execute("DELETE FROM login_attempts WHERE username = ?"
                 " AND ip_address = ?", (username, ip_address or ""))
    conn.commit()


def login_attempts_in_window(username, ip_address=""):
    """Count failures inside the window, pruning expired rows as we go so the
    table cannot grow without bound (no scheduled cleanup job needed)."""
    conn = get_db()
    cutoff = time.time() - config.LOGIN_WINDOW_SECONDS
    conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (cutoff,))
    conn.commit()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE username = ?"
        " AND ip_address = ? AND created_at >= ?",
        (username, ip_address or "", cutoff)).fetchone()
    return row["n"]


def is_rate_limited(username, ip_address=""):
    return login_attempts_in_window(username, ip_address) >= \
        config.LOGIN_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Part 2 — analytics queries
# ---------------------------------------------------------------------------
# All of these read the LOCAL compression_history table only (constraint #4:
# no new datastore, no external service).  `user_id=None` means system-wide
# and is reserved for admins — callers must enforce that; see app.py.

def _scope(user_id):
    """Returns (sql_fragment, args) restricting to a user, or system-wide."""
    if user_id is None:
        return "", []
    return " AND user_id = ?", [user_id]


def analytics_stats(user_id=None):
    """Full stat cards: files, ratio, bytes saved, and TIME SAVED.

    'Time saved' is transfer time avoided: bytes_saved / assumed link speed.
    The assumption is surfaced in the UI so the number is never mistaken for
    a measured quantity."""
    where, args = _scope(user_id)
    row = get_db().execute("""
        SELECT COUNT(*)                            AS files,
               COALESCE(SUM(original_bytes), 0)    AS total_original,
               COALESCE(SUM(compressed_bytes), 0)  AS total_compressed,
               COALESCE(AVG(ratio), 0)             AS avg_ratio,
               COALESCE(SUM(lossless_verified), 0) AS lossless_count,
               COALESCE(SUM(duration_ms), 0)       AS total_ms,
               COALESCE(AVG(entropy_bits), 0)      AS avg_entropy,
               COALESCE(SUM(gzip_bytes), 0)        AS total_gzip,
               COALESCE(SUM(huffman_bytes), 0)     AS total_huffman
        FROM compression_history
        WHERE operation = 'compress'""" + where, args).fetchone()
    d = dict(row)
    d["total_saved"] = d["total_original"] - d["total_compressed"]
    d["overall_ratio"] = (d["total_original"] / d["total_compressed"]
                          if d["total_compressed"] else 0.0)
    d["saved_pct"] = (100.0 * d["total_saved"] / d["total_original"]
                      if d["total_original"] else 0.0)
    return d


def analytics_by_extension(user_id=None, limit=12):
    """File-type distribution (Feature 3), grouped by lowercase extension."""
    where, args = _scope(user_id)
    rows = get_db().execute("""
        SELECT LOWER(
                 CASE WHEN instr(filename, '.') > 0
                      THEN replace(filename, rtrim(filename, replace(filename,
                           '.', '')), '')
                      ELSE '' END)                 AS ext,
               COUNT(*)                            AS files,
               COALESCE(SUM(original_bytes), 0)    AS total_original,
               COALESCE(SUM(compressed_bytes), 0)  AS total_compressed,
               COALESCE(AVG(ratio), 0)             AS avg_ratio
        FROM compression_history
        WHERE operation = 'compress'""" + where + """
        GROUP BY ext ORDER BY files DESC, ext LIMIT ?""",
        args + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ext"] = d["ext"] or "(none)"
        d["saved"] = d["total_original"] - d["total_compressed"]
        out.append(d)
    return out


def analytics_timeseries(user_id=None, limit=60):
    """Ratio-over-time for AFC vs the gzip / plain-Huffman REFERENCE columns
    (Feature 2).  Rows with no reference measurement (0) are returned as None
    so the chart leaves a gap instead of drawing a fake 0."""
    where, args = _scope(user_id)
    rows = get_db().execute("""
        SELECT id, filename, created_at, original_bytes, compressed_bytes,
               ratio, gzip_bytes, huffman_bytes
        FROM compression_history
        WHERE operation = 'compress'""" + where + """
        ORDER BY id DESC LIMIT ?""", args + [limit]).fetchall()
    out = []
    for r in reversed(rows):
        orig = r["original_bytes"]
        out.append({
            "id": r["id"], "filename": r["filename"],
            "created_at": r["created_at"], "original_bytes": orig,
            "afc_ratio": round(r["ratio"], 3),
            "gzip_ratio": round(orig / r["gzip_bytes"], 3)
                          if r["gzip_bytes"] else None,
            "huffman_ratio": round(orig / r["huffman_bytes"], 3)
                             if r["huffman_bytes"] else None,
        })
    return out


def search_history(user_id, q="", date_from="", date_to="", engine="",
                   preset="", limit=25, offset=0, sort="created_at",
                   direction="desc"):
    """Feature 4: filtered, sorted, paginated history.

    Sort column and direction are whitelisted, never interpolated from raw
    user input."""
    allowed_sort = {"created_at", "filename", "original_bytes",
                    "compressed_bytes", "ratio", "space_saved_pct",
                    "duration_ms"}
    if sort not in allowed_sort:
        sort = "created_at"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"

    sql = ["SELECT * FROM compression_history WHERE user_id = ?"]
    args = [user_id]
    if q:
        sql.append("AND filename LIKE ?")
        args.append("%" + q + "%")
    if date_from:
        sql.append("AND date(created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        sql.append("AND date(created_at) <= date(?)")
        args.append(date_to)
    if engine:
        sql.append("AND engine = ?")
        args.append(engine)
    if preset:
        sql.append("AND preset = ?")
        args.append(preset)

    count_sql = "SELECT COUNT(*) AS n FROM (" + " ".join(sql) + ")"
    total = get_db().execute(count_sql, args).fetchone()["n"]

    sql.append("ORDER BY %s %s, id DESC LIMIT ? OFFSET ?" % (sort, direction))
    rows = get_db().execute(" ".join(sql), args + [limit, offset]).fetchall()
    return rows, total


def get_history_row(row_id, user_id):
    """Single row, scoped to the owner (used by the comparison view)."""
    return get_db().execute(
        "SELECT * FROM compression_history WHERE id = ? AND user_id = ?",
        (row_id, user_id)).fetchone()


def distinct_engines(user_id):
    return [r["engine"] for r in get_db().execute(
        "SELECT DISTINCT engine FROM compression_history WHERE user_id = ?"
        " AND engine <> '' ORDER BY engine", (user_id,)).fetchall()]
