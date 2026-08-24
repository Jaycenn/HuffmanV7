#!/usr/bin/env python3
"""storage_supabase.py — Supabase object storage backend for artifact_store.py.

Only the I/O lives here.  Key generation, key validation and the SHA-256
verify-on-read stay in artifact_store.py, so the integrity guarantee is the
same sentence of code whichever backend is in use -- a stored file that does
not match its recorded digest and length is rejected either way.

Object keys are the same opaque 32-hex identifiers used on disk, which is what
Appendix G means by "the storage_key column becomes the object key": the column
and its UNIQUE constraint are unchanged by the move.
"""
import config


class StorageUnavailable(RuntimeError):
    """Raised when the Supabase backend is selected but cannot be used."""


_client = None


def _bucket():
    """Cache one client per process; create_client opens a session."""
    global _client
    if _client is None:
        try:
            from supabase import create_client
        except ImportError as exc:                      # pragma: no cover
            raise StorageUnavailable(
                "supabase is not installed. Run:  pip install supabase"
            ) from exc
        if not (config.SUPABASE_URL and config.SUPABASE_SERVICE_ROLE_KEY):
            raise StorageUnavailable(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in "
                ".env, or set AFC_STORAGE_BACKEND=local to use the disk store.")
        _client = create_client(config.SUPABASE_URL,
                                config.SUPABASE_SERVICE_ROLE_KEY)
    return _client.storage.from_(config.SUPABASE_BUCKET)


def ensure_ready():
    """Confirm the bucket exists and is private.  Called once at startup.

    A public bucket would hand every user's compressed output to anyone with
    the URL, contradicting the access-control commitment in the Ethical
    Consideration, so it is refused rather than warned about.
    """
    _bucket()                       # forces client creation and validation
    buckets = _client.storage.list_buckets()
    for b in buckets:
        name = getattr(b, "name", None) or (
            b.get("name") if isinstance(b, dict) else None)
        if name != config.SUPABASE_BUCKET:
            continue
        public = getattr(b, "public", None)
        if public is None and isinstance(b, dict):
            public = b.get("public")
        if public:
            raise StorageUnavailable(
                "Bucket %r is public. Stored results must not be readable by "
                "URL; set it to private in the Supabase dashboard."
                % config.SUPABASE_BUCKET)
        return config.SUPABASE_BUCKET
    raise StorageUnavailable(
        "Bucket %r does not exist. Create it (private) in the Supabase "
        "dashboard, or correct SUPABASE_BUCKET in .env."
        % config.SUPABASE_BUCKET)


def put(key, blob):
    """Upload one object.  A single request, so there is no partial state."""
    _bucket().upload(key, blob,
                     {"content-type": "application/octet-stream"})


def get(key):
    """Download one object as bytes.  Raises on a missing key."""
    return _bucket().download(key)


def exists(key):
    try:
        return bool(_bucket().exists(key))
    except Exception:                                   # pragma: no cover
        return False


def delete(key):
    """Remove one object.  Returns False when it was not there."""
    try:
        removed = _bucket().remove([key])
    except Exception:
        return False
    return bool(removed)


def list_keys(key_pattern):
    """Every object whose name is a valid opaque artifact key.

    The listing is paged; Supabase caps a page at 100 by default, so this
    walks until a short page comes back.
    """
    found = []
    offset = 0
    page = 100
    while True:
        batch = _bucket().list("", {"limit": page, "offset": offset})
        if not batch:
            break
        for item in batch:
            name = getattr(item, "name", None) or (
                item.get("name") if isinstance(item, dict) else None)
            if name and key_pattern.fullmatch(name):
                found.append(name)
        if len(batch) < page:
            break
        offset += page
    return found
