#!/usr/bin/env python3
"""Prove the app can reach Supabase before any of db.py is rewritten.

Reads credentials from .env (never from the command line, so nothing lands in
shell history).  Checks the database, the schema that schema_pg.sql created,
and a real round trip through object storage.

    python supabase_check.py
"""
import os
import sys

EXPECTED = {
    "users": 9,
    "app_meta": 2,
    "compression_history": 23,
    "stored_artifacts": 11,
    "audit_log": 7,
    "login_attempts": 4,
}
INDEXES = 9


def load_env(path=".env"):
    """Minimal .env reader so the check has no import-order surprises."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need(name):
    value = os.environ.get(name, "")
    if not value:
        print("  MISSING  %s is not set in .env" % name)
    return value


PLACEHOLDERS = ("YOUR-REF", "YOUR-PASSWORD", "[YOUR", "YOUR-PROJECT")


def check_database(dsn):
    print("\nDATABASE")
    left = [p for p in PLACEHOLDERS if p in dsn]
    if left:
        print("  PLACEHOLDER NOT REPLACED: %s" % ", ".join(left))
        print("  AFC_PG_DSN still contains template text rather than your own")
        print("  project details. Copy the URI from the Connect panel in the")
        print("  Supabase dashboard, then replace only [YOUR-PASSWORD].")
        print("  Your project ref is the subdomain in SUPABASE_URL.")
        return False
    try:
        import psycopg
    except ImportError:
        print("  psycopg is not installed:  pip install \"psycopg[binary]\"")
        return False
    try:
        with psycopg.connect(dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                print("  connected:", cur.fetchone()[0].split(",")[0])

                cur.execute("""
                    SELECT table_name, count(*)
                    FROM information_schema.columns
                    WHERE table_schema='public'
                    GROUP BY table_name ORDER BY table_name""")
                found = dict(cur.fetchall())
                ok = True
                for table, cols in sorted(EXPECTED.items()):
                    got = found.get(table)
                    if got is None:
                        print("  MISSING TABLE  %s" % table); ok = False
                    elif got != cols:
                        print("  COLUMN COUNT   %-20s expected %d, found %d"
                              % (table, cols, got)); ok = False
                    else:
                        print("  ok  %-22s %2d columns" % (table, got))
                extra = sorted(set(found) - set(EXPECTED))
                if extra:
                    print("  note: extra tables present:", ", ".join(extra))

                cur.execute("SELECT count(*) FROM pg_indexes"
                            " WHERE schemaname='public' AND indexname LIKE 'idx_%'")
                n = cur.fetchone()[0]
                print("  %s  %d of %d expected indexes"
                      % ("ok " if n == INDEXES else "WARN", n, INDEXES))
                ok = ok and n == INDEXES

                cur.execute("""
                    SELECT c.relname, c.relrowsecurity,
                           (SELECT count(*) FROM pg_policy p
                            WHERE p.polrelid = c.oid)
                    FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
                    WHERE ns.nspname='public' AND c.relkind='r'
                    ORDER BY c.relname""")
                for name, rls, npol in cur.fetchall():
                    if name not in EXPECTED:
                        continue
                    if not rls:
                        print("  RLS OFF  %-20s publicly reachable via the "
                              "REST API" % name); ok = False
                    else:
                        print("  ok  %-22s RLS on, %d policies" % (name, npol))
        return ok
    except Exception as exc:                                # noqa: BLE001
        print("  FAILED:", exc)
        print("  If this is a network or DNS error, try the connection pooler")
        print("  string from Project Settings -> Database instead of the")
        print("  direct one; the direct host is IPv6-only on some projects.")
        return False


def check_storage(url, key, bucket):
    print("\nOBJECT STORAGE")
    try:
        from supabase import create_client
    except ImportError:
        print("  supabase is not installed:  pip install supabase")
        return False
    try:
        client = create_client(url, key)
        buckets = client.storage.list_buckets()
        names = [getattr(b, "name", None) or b.get("name") for b in buckets]
        if bucket not in names:
            print("  MISSING BUCKET  %r not found. Buckets: %s"
                  % (bucket, ", ".join(str(n) for n in names) or "(none)"))
            near = [n for n in names
                    if str(n).lower() == str(bucket).lower()]
            if near:
                print("  Bucket names are case-sensitive. Set "
                      "SUPABASE_BUCKET=%s in .env, or recreate the bucket "
                      "as %r." % (near[0], bucket))
            return False
        print("  ok  bucket %r exists" % bucket)
        for b in buckets:
            name = getattr(b, "name", None) or b.get("name")
            if name != bucket:
                continue
            public = getattr(b, "public", None)
            if public is None and isinstance(b, dict):
                public = b.get("public")
            if public:
                print("  PUBLIC BUCKET   %r is public; every stored result "
                      "would be readable by URL" % bucket)
                return False
            print("  ok  bucket is private")

        import hashlib
        import uuid
        key_name = "healthcheck-" + uuid.uuid4().hex
        payload = b"afc round trip " + uuid.uuid4().bytes
        digest = hashlib.sha256(payload).hexdigest()
        store = client.storage.from_(bucket)
        store.upload(key_name, payload,
                     {"content-type": "application/octet-stream"})
        got = store.download(key_name)
        store.remove([key_name])
        if got != payload or hashlib.sha256(got).hexdigest() != digest:
            print("  ROUND TRIP FAILED  downloaded bytes did not match")
            return False
        print("  ok  upload, download and delete round-tripped %d bytes, "
              "SHA-256 matched" % len(payload))
        return True
    except Exception as exc:                                # noqa: BLE001
        print("  FAILED:", exc)
        return False


def main():
    load_env()
    print("SUPABASE CONNECTION CHECK")
    dsn = need("AFC_PG_DSN")
    url = need("SUPABASE_URL")
    key = need("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.environ.get("SUPABASE_BUCKET", "artifacts")
    if not (dsn and url and key):
        print("\nFill in .env first. See .env.example.")
        return 2
    db_ok = check_database(dsn)
    st_ok = check_storage(url, key, bucket)
    print()
    if db_ok and st_ok:
        print("ALL CHECKS PASSED — safe to start the db.py backend switch.")
        return 0
    print("SOMETHING IS NOT READY — fix the items above before Phase 4.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
