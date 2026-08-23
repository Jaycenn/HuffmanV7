#!/usr/bin/env python3
"""db_pg.py — PostgreSQL (Supabase) backend for db.py.

WHY THIS IS THIN.  db.py's SQL is deliberately written in the subset both
engines accept: COUNT(*) FILTER (WHERE ...), CURRENT_TIMESTAMP, ON CONFLICT
DO NOTHING, RETURNING id, TRUE/FALSE literals, and Python bools as parameters.
All of those run unchanged on SQLite 3.35+ and on PostgreSQL, so there is no
dialect rewriting here.  Only three things genuinely differ:

  1. The parameter marker.  db.py writes '?'; psycopg spells it '%s'.
  2. Transaction locking.  SQLite serialises writers with BEGIN IMMEDIATE;
     PostgreSQL uses MVCC, so the quota check takes a per-user advisory lock
     instead (see db.begin_exclusive).
  3. login_attempts.created_at is REAL epoch seconds on SQLite and TIMESTAMPTZ
     on PostgreSQL, per Appendix G.  db.py branches those three queries.

This module presents exactly the slice of the sqlite3 connection surface that
db.py uses: execute(), commit(), rollback(), close(), and rows that support
row["column"].  psycopg's dict_row already returns mappings, and db.py never
indexes a row by position (checked: 43 string-key accesses, 0 positional), so
cursors are handed back unwrapped.
"""
import config


class PostgresUnavailable(RuntimeError):
    """Raised when the Postgres backend is selected but cannot be used."""


def _translate(sql):
    """Rewrite SQLite parameter markers for psycopg.

    '?' outside a string literal becomes '%s'.  Every literal '%' is doubled,
    because psycopg scans the whole query for its own placeholder marker.
    Quote tracking keeps a '?' inside a SQL string literal intact; doubled
    quotes ('') toggle twice and therefore cancel, which is correct.
    """
    out = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _register_sqlite_shaped_types(conn):
    """Make PostgreSQL hand back the Python types SQLite hands back.

    Two differences would otherwise leak into the application:

      * SUM() over an integer column returns numeric, which psycopg loads as
        decimal.Decimal.  SQLite returns int, and db.history_stats multiplies
        it by a float -- Decimal * float raises TypeError.
      * TIMESTAMPTZ loads as datetime.  SQLite returns TEXT, and the templates
        slice it: {{ r['created_at'][:16] }} would raise on a datetime.

    So numeric is loaded as int when integral and float otherwise (exactly
    SQLite's SUM/AVG behaviour), and timestamps are loaded as the same
    'YYYY-MM-DD HH:MM:SS' string CURRENT_TIMESTAMP produces on SQLite.  The
    session runs in UTC so that text is unambiguous.
    """
    from psycopg.adapt import Loader

    class _NumericAsIntOrFloat(Loader):
        def load(self, data):
            text = bytes(data).decode()
            if any(c in text for c in ".eEnN"):      # 1.5, 1e3, NaN
                try:
                    return float(text)
                except ValueError:
                    return text
            try:
                return int(text)
            except ValueError:
                return text

    class _TimestampAsText(Loader):
        def load(self, data):
            # UTC session, so the rendered form is
            # 'YYYY-MM-DD HH:MM:SS[.ffffff][+00]'.  Trim to seconds to match
            # SQLite's CURRENT_TIMESTAMP exactly.
            return bytes(data).decode()[:19]

    conn.execute("SET TIME ZONE 'UTC'")
    conn.adapters.register_loader("numeric", _NumericAsIntOrFloat)
    conn.adapters.register_loader("timestamptz", _TimestampAsText)
    conn.adapters.register_loader("timestamp", _TimestampAsText)
    conn.commit()


class PgConnection:
    """sqlite3-shaped wrapper over a psycopg connection."""

    def __init__(self, dsn):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:                      # pragma: no cover
            raise PostgresUnavailable(
                "psycopg is not installed. Run:  pip install \"psycopg[binary]\""
            ) from exc
        if not dsn:
            raise PostgresUnavailable(
                "AFC_PG_DSN is empty. Set it in .env, or set "
                "AFC_DB_BACKEND=sqlite to use the local database.")
        self._conn = psycopg.connect(dsn, row_factory=dict_row,
                                     autocommit=False,
                                     connect_timeout=15)
        _register_sqlite_shaped_types(self._conn)

    # -- the surface db.py actually uses ------------------------------------

    def execute(self, sql, params=()):
        return self._conn.execute(_translate(sql), tuple(params))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:                               # pragma: no cover
            pass

    def cursor(self):
        return self._conn.cursor()

    # -- SQLite-only calls that must not reach PostgreSQL -------------------

    def executescript(self, _sql):                      # pragma: no cover
        raise PostgresUnavailable(
            "executescript() is SQLite-only. The PostgreSQL schema is applied "
            "once from schema_pg.sql, not from the application.")

    @property
    def raw(self):
        """The underlying psycopg connection, for advisory locks."""
        return self._conn


def connect(dsn=None):
    return PgConnection(dsn if dsn is not None else config.PG_DSN)
