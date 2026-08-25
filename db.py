#!/usr/bin/env python3
"""
db.py — SQLite access layer for the AFC web app.

Design notes for whoever picks this up (Part 2):
  * Plain sqlite3 + hand-written SQL.  No ORM on purpose: the logical schema is
    small, the same query layer also targets PostgreSQL, and an ORM would add a
    heavy dependency without improving the compression study.
  * Connections are per-request, stored on flask.g, closed by teardown.
  * Every query returns sqlite3.Row, so callers use row["column"].
  * Writes go through helpers here, never raw SQL in a route — if Part 2 adds
    analytics, add a query function here and call it from the blueprint.
  * SQLite lives at config.DATABASE_PATH for development/tests. The hosted
    instance selects PostgreSQL through AFC_DB_BACKEND; see SCOPE_NOTES.md.
"""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

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
    (tests, CLI) should use this and close it themselves.

    An explicit path always means SQLite: the test suite and reset_db() pass
    one, and they must keep working on the local file regardless of which
    backend the deployed app is configured for.
    """
    if path is None and config.using_postgres():
        import db_pg
        return db_pg.connect()
    conn = sqlite3.connect(path or config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps readers from blocking the writer; harmless for a local file.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def begin_exclusive(conn, user_id=None):
    """Start a write transaction that other writers cannot interleave with.

    SQLite takes its single writer lock with BEGIN IMMEDIATE.  PostgreSQL is
    MVCC and has no such lock, so the caller's intent -- one reservation per
    user at a time -- is expressed with a per-user transaction-scoped advisory
    lock, released automatically at COMMIT or ROLLBACK.
    """
    if config.using_postgres():
        if user_id is not None:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (int(user_id),))
        return
    conn.execute("BEGIN IMMEDIATE")


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
    if path is None and config.using_postgres():
        # schema_pg.sql is applied once, out of band (Supabase SQL editor or
        # psql).  The application never issues DDL against the hosted database.
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES ('session_epoch', ?)"
                " ON CONFLICT DO NOTHING", (uuid.uuid4().hex,))
            conn.commit()
            if seed_admin:
                _seed_admin(conn)
        finally:
            conn.close()
        return
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
    # Archive member rows are children of their durable AFCPAK summary.  The
    # self-reference lets deletion/retention remove the complete logical run.
    ("parent_history_id", "INTEGER REFERENCES compression_history(id)"
                          " ON DELETE CASCADE"),
]


def _migrate(conn):
    have = {r["name"] for r in
            conn.execute("PRAGMA table_info(compression_history)")}
    for col, decl in _MIGRATIONS:
        if col not in have:
            conn.execute("ALTER TABLE compression_history ADD COLUMN %s %s"
                         % (col, decl))
    # Explicit additive migration for databases created before durable result
    # storage existed.  CREATE IF NOT EXISTS keeps repeated startups harmless.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stored_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL UNIQUE
                REFERENCES compression_history (id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            storage_key TEXT NOT NULL UNIQUE,
            download_name TEXT NOT NULL,
            mimetype TEXT NOT NULL DEFAULT 'application/octet-stream',
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            sha256 TEXT NOT NULL,
            integrity_status TEXT NOT NULL DEFAULT 'verified',
            last_verified_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_user_created
            ON stored_artifacts (user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_history_parent
            ON compression_history (parent_history_id);
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES"
        " ('session_epoch', ?) ON CONFLICT DO NOTHING", (uuid.uuid4().hex,))
    # Backfill pre-parent archive members narrowly: real member rows had no
    # durable artifact of their own.  Normal batch outputs do, so a caller-
    # supplied batch-id collision is not adopted by the archive summary.
    conn.execute("""
        UPDATE compression_history AS child
        SET parent_history_id = (
            SELECT summary.id FROM compression_history AS summary
            WHERE summary.user_id = child.user_id
              AND summary.batch_id = child.batch_id
              AND summary.container_format = 'AFCPAK'
            ORDER BY summary.id DESC LIMIT 1)
        WHERE child.parent_history_id IS NULL
          AND child.operation = 'compress'
          AND child.batch_id IS NOT NULL
          AND child.container_format <> 'AFCPAK'
          AND NOT EXISTS (
              SELECT 1 FROM stored_artifacts a WHERE a.history_id = child.id)
          AND EXISTS (
              SELECT 1 FROM compression_history AS summary
              WHERE summary.user_id = child.user_id
                AND summary.batch_id = child.batch_id
                AND summary.container_format = 'AFCPAK')
    """)
    conn.commit()


def reset_db(path=None):
    """DESTRUCTIVE: delete the database file and recreate it from schema.sql.
    README documents this as the supported reset procedure."""
    target = path or config.DATABASE_PATH
    # The supported reset operation wipes the private result store as well as
    # SQLite.  Only opaque keys inside the configured store are touched.
    if os.path.abspath(target) == os.path.abspath(config.DATABASE_PATH):
        try:
            import artifact_store
            for storage_key in artifact_store.list_keys():
                artifact_store.delete(storage_key)
            artifact_store.remove_stale_temporary_files(min_age_seconds=0)
        except (OSError, ValueError) as exc:
            # Never destroy ownership metadata while a user blob remains.  A
            # reset failure is explicit and leaves the database available for
            # recovery/retry.
            raise RuntimeError(
                "Reset aborted: private stored files could not be removed.") \
                from exc
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(target + suffix)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                "Reset could not remove the database file %s." %
                (target + suffix)) from exc
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
        " must_change_password) VALUES (?, ?, ?, 'admin', TRUE)",
        (config.DEFAULT_ADMIN_USERNAME, config.DEFAULT_ADMIN_EMAIL,
         generate_password_hash(config.DEFAULT_ADMIN_PASSWORD)))
    conn.commit()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def get_user_by_id(uid):
    return get_db().execute("SELECT * FROM users WHERE id = ?",
                             (uid,)).fetchone()


def session_epoch():
    row = get_db().execute(
        "SELECT value FROM app_meta WHERE key = 'session_epoch'").fetchone()
    return row["value"] if row else ""


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
        " must_change_password) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (username, email, generate_password_hash(password), role,
         bool(must_change_password)))
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def set_password(user_id, password):
    from werkzeug.security import generate_password_hash
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ?,"
                 " must_change_password = FALSE"
                 " WHERE id = ?", (generate_password_hash(password), user_id))
    conn.commit()


def touch_last_login(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP"
                 " WHERE id = ?", (user_id,))
    conn.commit()


def set_user_active(user_id, active):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?",
                 (bool(active), user_id))
    conn.commit()


def list_users():
    """Admin user-list view.  Includes per-user aggregates so the template does
    not need a second query per row."""
    return get_db().execute("""
        SELECT u.id, u.username, u.email, u.role, u.is_active, u.created_at,
               u.last_login_at,
               COUNT(h.id)                        AS file_count,
               COALESCE(SUM(h.original_bytes), 0) AS total_original,
               COALESCE(SUM(h.compressed_bytes), 0) AS total_compressed,
               COALESCE((SELECT SUM(a.byte_size) FROM stored_artifacts a
                         WHERE a.user_id = u.id), 0) AS stored_bytes
        FROM users u
        LEFT JOIN compression_history h
               ON h.user_id = u.id AND h.operation = 'compress'
              AND h.parent_history_id IS NULL
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
                sha256_container="", detected_type="",
                parent_history_id=None):
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
        " sha256_original, sha256_container, detected_type, parent_history_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " RETURNING id",
        (user_id, filename, operation, orig, comp, ratio, saved, engine,
         container_format, bool(lossless_verified), float(duration_ms),
         batch_id, int(gzip_bytes), int(huffman_bytes), float(entropy_bits),
         float(block_share_pct), preset, sha256_original or "",
         sha256_container or "", detected_type or "", parent_history_id))
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


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
    sql = """SELECT h.*,
                    a.id AS artifact_id,
                    a.download_name AS artifact_name,
                    a.byte_size AS artifact_bytes,
                    a.integrity_status AS artifact_integrity,
                    a.last_verified_at AS artifact_last_verified,
                    CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS artifact_available
             FROM compression_history h
             LEFT JOIN stored_artifacts a ON a.history_id = h.id
             WHERE h.user_id = ?"""
    args = [user_id]
    if batch_id:
        sql += " AND h.batch_id = ?"
        args.append(batch_id)
    sql += " ORDER BY h.created_at DESC, h.id DESC LIMIT ? OFFSET ?"
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
               COUNT(*) FILTER (WHERE lossless_verified) AS lossless_count
        FROM compression_history h
        WHERE h.user_id = ? AND h.operation = 'compress'
          AND h.parent_history_id IS NULL
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


def delete_history_group(row_id, user_id, connection=None):
    """Delete a result; archive children cascade through parent_history_id."""
    conn = connection or get_db()
    cur = conn.execute(
        "DELETE FROM compression_history WHERE id = ? AND user_id = ?",
        (row_id, user_id))
    if connection is None:
        conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# durable compressed artifacts
# ---------------------------------------------------------------------------

class StorageQuotaExceeded(RuntimeError):
    """Raised inside the serialized quota reservation."""

    def __init__(self, used, limit):
        super().__init__("stored artifact quota exceeded")
        self.used = int(used)
        self.limit = int(limit)


@contextmanager
def storage_reservation(user_id, new_bytes, limit):
    """Serialize quota check + metadata commit across threads/processes.

    BEGIN IMMEDIATE takes SQLite's writer lock before calculating usage.  The
    caller writes the opaque file and inserts metadata while this transaction
    is open, so two requests cannot both reserve the same remaining capacity.
    """
    conn = connect()
    try:
        begin_exclusive(conn, user_id)
        row = conn.execute(
            "SELECT COALESCE(SUM(byte_size), 0) AS n FROM stored_artifacts"
            " WHERE user_id = ?", (user_id,)).fetchone()
        used = int(row["n"])
        if int(new_bytes) > int(limit) - used:
            raise StorageQuotaExceeded(used, limit)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_stored_artifact(history_id, user_id, storage_key, download_name,
                        mimetype, byte_size, sha256, connection=None):
    conn = connection or get_db()
    cur = conn.execute(
        "INSERT INTO stored_artifacts (history_id, user_id, storage_key,"
        " download_name, mimetype, byte_size, sha256, last_verified_at)"
        " VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP) RETURNING id",
        (history_id, user_id, storage_key, download_name, mimetype,
         int(byte_size), sha256))
    new_id = cur.fetchone()["id"]
    if connection is None:
        conn.commit()
    return new_id


def stored_bytes_for_user(user_id):
    row = get_db().execute(
        "SELECT COALESCE(SUM(byte_size), 0) AS n FROM stored_artifacts"
        " WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["n"])


def get_stored_artifact(history_id):
    """Resolve by history id; the route performs owner-only authorization."""
    return get_db().execute("""
        SELECT a.*, h.filename, h.container_format, h.sha256_container,
               h.batch_id
        FROM stored_artifacts a
        JOIN compression_history h ON h.id = a.history_id
        WHERE a.history_id = ?
    """, (history_id,)).fetchone()


def set_artifact_integrity(history_id, status):
    conn = get_db()
    conn.execute(
        "UPDATE stored_artifacts SET integrity_status = ?,"
        " last_verified_at = CURRENT_TIMESTAMP WHERE history_id = ?",
        (status, history_id))
    conn.commit()


def all_stored_artifacts(connection=None):
    """Every artifact row, for the startup reconciliation sweep.

    Takes an explicit connection because the sweep runs during create_app(),
    outside any request context, where get_db() has no flask.g to cache on.
    """
    conn = connection or get_db()
    return conn.execute(
        "SELECT history_id, storage_key, user_id FROM stored_artifacts"
        " ORDER BY history_id").fetchall()


def expired_artifacts(days, connection=None):
    """Artifacts older than the retention window.

    SQLite does relative dates with datetime('now', '-N days'); PostgreSQL
    uses an interval.  The comparison is otherwise identical.
    """
    conn = connection or get_db()
    days = int(days)
    if config.using_postgres():
        return conn.execute(
            "SELECT history_id, storage_key, user_id FROM stored_artifacts"
            " WHERE created_at < now() - make_interval(days => ?)",
            (days,)).fetchall()
    return conn.execute(
        "SELECT history_id, storage_key, user_id FROM stored_artifacts"
        " WHERE created_at < datetime('now', ?)",
        ("-%d days" % days,)).fetchall()


def mark_artifact_missing(history_id, connection=None):
    """Record that a durable file backing this row is no longer present."""
    conn = connection or get_db()
    conn.execute(
        "UPDATE stored_artifacts SET integrity_status = 'missing',"
        " last_verified_at = CURRENT_TIMESTAMP WHERE history_id = ?",
        (history_id,))


def list_user_artifacts(user_id):
    return get_db().execute(
        "SELECT * FROM stored_artifacts WHERE user_id = ? ORDER BY id",
        (user_id,)).fetchall()


@contextmanager
def user_deletion_lock(user_id):
    """Serialize artifact enumeration and account deletion with new stores."""
    conn = connect()
    try:
        begin_exclusive(conn, user_id)
        artifacts = conn.execute(
            "SELECT * FROM stored_artifacts WHERE user_id = ? ORDER BY id",
            (user_id,)).fetchall()
        yield conn, artifacts
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_user(user_id, connection=None):
    """Delete an account, its files, and identifying security-log fields.

    Audit event types and timestamps remain useful for security review, but a
    withdrawal/account-deletion request must not leave the former username or
    IP address behind.  Admin target details are scrubbed too.
    """
    conn = connection or get_db()
    target = conn.execute(
        "SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        return 0
    username = target["username"]
    marker = "target=" + username
    conn.execute(
        "UPDATE audit_log SET username = '', ip_address = '',"
        " detail = replace(detail, ?, 'target=[deleted]')"
        " WHERE user_id = ? OR username = ? OR instr(detail, ?) > 0",
        (marker, user_id, username, marker))
    conn.execute("DELETE FROM login_attempts WHERE username = ?", (username,))
    cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if connection is None:
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
    if config.using_postgres():
        # created_at is TIMESTAMPTZ with DEFAULT now(); let the server stamp it
        # so the window is measured against database time, not app-server time.
        conn.execute("INSERT INTO login_attempts (username, ip_address)"
                     " VALUES (?,?)", (username, ip_address or ""))
    else:
        conn.execute(
            "INSERT INTO login_attempts (username, ip_address, created_at)"
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
    window = config.LOGIN_WINDOW_SECONDS
    if config.using_postgres():
        conn.execute("DELETE FROM login_attempts"
                     " WHERE created_at < now() - make_interval(secs => ?)",
                     (window,))
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE username = ?"
            " AND ip_address = ?"
            " AND created_at >= now() - make_interval(secs => ?)",
            (username, ip_address or "", window)).fetchone()
    else:
        cutoff = time.time() - window
        conn.execute("DELETE FROM login_attempts WHERE created_at < ?",
                     (cutoff,))
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
               COUNT(*) FILTER (WHERE lossless_verified) AS lossless_count,
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

    sql = ["""SELECT h.*,
                    a.id AS artifact_id,
                    a.download_name AS artifact_name,
                    a.byte_size AS artifact_bytes,
                    a.integrity_status AS artifact_integrity,
                    a.last_verified_at AS artifact_last_verified,
                    CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS artifact_available
             FROM compression_history h
             LEFT JOIN stored_artifacts a ON a.history_id = h.id
             WHERE h.user_id = ?"""]
    args = [user_id]
    if q:
        sql.append("AND h.filename LIKE ?")
        args.append("%" + q + "%")
    if date_from:
        sql.append("AND date(h.created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        sql.append("AND date(h.created_at) <= date(?)")
        args.append(date_to)
    if engine:
        sql.append("AND h.engine = ?")
        args.append(engine)
    if preset:
        sql.append("AND h.preset = ?")
        args.append(preset)

    count_sql = "SELECT COUNT(*) AS n FROM (" + " ".join(sql) + ")"
    total = get_db().execute(count_sql, args).fetchone()["n"]

    sql.append("ORDER BY h.%s %s, h.id DESC LIMIT ? OFFSET ?" %
               (sort, direction))
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
