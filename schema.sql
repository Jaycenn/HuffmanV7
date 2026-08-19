-- schema.sql — AFC web app, Part 1.
--
-- Local SQLite only.  See SCOPE_NOTES.md for why a local SQLite file still
-- satisfies the thesis "local / non-cloud" delimitation.
--
-- Apply with:  python -c "import db; db.init_db()"
-- or reset with:  python -c "import db; db.reset_db()"   (DESTRUCTIVE)

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
-- password_hash: werkzeug.security.generate_password_hash (PBKDF2-SHA256).
--   Plaintext passwords are never stored or logged.
--   NOTE: this is ACCOUNT password hashing only.  It is NOT file encryption —
--   the thesis Delimitations exclude encrypting compressed output, and nothing
--   in this app encrypts file data.  See SCOPE_NOTES.md.
-- must_change_password: set on the seeded admin so first login forces a change.
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE,
    email                TEXT    NOT NULL UNIQUE,
    password_hash        TEXT    NOT NULL,
    role                 TEXT    NOT NULL DEFAULT 'user'
                                 CHECK (role IN ('admin', 'user')),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);

-- Changes on every destructive database reset.  Signed sessions carry this
-- value so an old numeric user id can never authenticate as a new post-reset
-- account that happens to receive the same id.
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- compression_history
-- ---------------------------------------------------------------------------
-- One row per file processed (single, batch, or inside an archive).
-- operation:        'compress' | 'decompress'
-- container_format: 'AFC1' | 'AFC2' | '' (decompress input / raw)
-- engine:           'C++ native' | 'pure Python'
-- lossless_verified: 1 only when a SHA-256 round trip was actually run and
--                    matched.  Never set optimistically.
-- batch_id:         groups rows produced by one queue/archive run (NULL for
--                    single-file runs).  Part 2 can GROUP BY this for
--                    per-batch analytics.
CREATE TABLE IF NOT EXISTS compression_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    filename          TEXT    NOT NULL,
    operation         TEXT    NOT NULL DEFAULT 'compress'
                              CHECK (operation IN ('compress', 'decompress')),
    original_bytes    INTEGER NOT NULL,
    compressed_bytes  INTEGER NOT NULL,
    ratio             REAL    NOT NULL,
    space_saved_pct   REAL    NOT NULL,
    engine            TEXT    NOT NULL,
    container_format  TEXT    NOT NULL DEFAULT '',
    lossless_verified INTEGER NOT NULL DEFAULT 0,
    duration_ms       REAL    NOT NULL DEFAULT 0,
    batch_id          TEXT,
    parent_history_id INTEGER REFERENCES compression_history (id)
                              ON DELETE CASCADE,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_history_user    ON compression_history (user_id);
CREATE INDEX IF NOT EXISTS idx_history_created ON compression_history (created_at);
CREATE INDEX IF NOT EXISTS idx_history_batch   ON compression_history (batch_id);

-- ---------------------------------------------------------------------------
-- stored_artifacts
-- ---------------------------------------------------------------------------
-- Only produced compressed AFC/AFCPAK bytes are durable.  User uploads and
-- decompressed originals are intentionally absent from this table and disk.
-- storage_key is an opaque server-generated filename in RESULT_STORAGE_DIR.
CREATE TABLE IF NOT EXISTS stored_artifacts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id       INTEGER NOT NULL UNIQUE
                             REFERENCES compression_history (id) ON DELETE CASCADE,
    user_id          INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    storage_key      TEXT    NOT NULL UNIQUE,
    download_name    TEXT    NOT NULL,
    mimetype         TEXT    NOT NULL DEFAULT 'application/octet-stream',
    byte_size        INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256           TEXT    NOT NULL,
    integrity_status TEXT    NOT NULL DEFAULT 'verified',
    last_verified_at TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_user_created
    ON stored_artifacts (user_id, created_at);

-- ---------------------------------------------------------------------------
-- audit_log
-- ---------------------------------------------------------------------------
-- Security-relevant events: login, login_failed, logout, password_change,
-- admin_view_users, admin_toggle_active, rate_limited.
-- user_id is NULL when the event has no valid user (e.g. bad username).
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    username   TEXT,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_event   ON audit_log (event);

-- ---------------------------------------------------------------------------
-- login_attempts  (fixed-window rate limiting)
-- ---------------------------------------------------------------------------
-- One row per failed attempt; rows older than LOGIN_WINDOW_SECONDS are pruned
-- on each check, so the table stays small without a scheduled job.
CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL          -- unix epoch seconds
);

CREATE INDEX IF NOT EXISTS idx_attempts_lookup
    ON login_attempts (username, ip_address, created_at);
