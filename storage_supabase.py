#!/usr/bin/env python3
"""storage_supabase.py — Supabase object storage backend for artifact_store.py.

Only the I/O lives here.  Key generation, key validation and the SHA-256
verify-on-read stay in artifact_store.py, so the integrity guarantee is the
same sentence of code whichever backend is in use -- a stored file that does
not match its recorded digest and length is rejected either way.

The database continues to store one opaque 32-hex logical key per artifact.
Small results use that key directly.  Large results are split into internal
part objects and the logical key contains a small manifest.  This keeps a
100 MiB application result below Supabase Free's 50 MB per-object ceiling
without exposing the storage layout to routes, database rows, or users.
"""
import hashlib
import json
import re

import config


class StorageUnavailable(RuntimeError):
    """Raised when the Supabase backend is selected but cannot be used."""


_client = None

# Supabase Free currently caps one object at 50 MB.  Forty binary MiB remains
# safely below that limit while keeping a near-100 MiB AFC result to three
# requests.  This is an implementation detail, not the application's upload
# limit: artifact_store still treats the result as one verified byte string.
_CHUNK_BYTES = 40 * 1024 * 1024
_MANIFEST_MAGIC = b"AFC-SUPABASE-CHUNKS\x00"
_MANIFEST_VERSION = 1
_MAX_MANIFEST_PARTS = 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _part_key(key, index):
    return "%s.part.%06d" % (key, index)


def _encode_manifest(total_size, parts):
    body = {
        "version": _MANIFEST_VERSION,
        "size": total_size,
        "parts": parts,
    }
    return _MANIFEST_MAGIC + json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode("ascii")


def _decode_manifest(blob):
    """Return validated manifest metadata, or None for a legacy object."""
    if not blob.startswith(_MANIFEST_MAGIC):
        return None
    try:
        body = json.loads(blob[len(_MANIFEST_MAGIC):].decode("ascii"))
        parts = body["parts"]
        if body["version"] != _MANIFEST_VERSION or not isinstance(parts, list):
            raise ValueError
        if not 1 < len(parts) <= _MAX_MANIFEST_PARTS:
            raise ValueError
        total = 0
        for part in parts:
            size = part["size"]
            digest = part["sha256"]
            if not isinstance(size, int) or size < 0:
                raise ValueError
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError
            total += size
        if not isinstance(body["size"], int) or body["size"] != total:
            raise ValueError
        return body
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise IOError("Stored chunk manifest is invalid.")


def put(key, blob):
    """Upload one logical artifact, rolling back incomplete chunk sets."""
    bucket = _bucket()
    options = {"content-type": "application/octet-stream"}
    if len(blob) <= _CHUNK_BYTES:
        bucket.upload(key, blob, options)
        return

    parts = []
    uploaded = []
    try:
        for index, offset in enumerate(range(0, len(blob), _CHUNK_BYTES)):
            part = blob[offset:offset + _CHUNK_BYTES]
            part_name = _part_key(key, index)
            bucket.upload(part_name, part, options)
            uploaded.append(part_name)
            parts.append({
                "size": len(part),
                "sha256": hashlib.sha256(part).hexdigest(),
            })
        # Publish the logical key last.  list_keys()/exists() therefore never
        # report an artifact whose parts are still being uploaded.
        bucket.upload(key, _encode_manifest(len(blob), parts),
                      {"content-type": "application/octet-stream"})
    except Exception:
        if uploaded:
            try:
                bucket.remove(uploaded)
            except Exception:
                pass
        raise


def get(key):
    """Download one logical artifact, including legacy single objects."""
    bucket = _bucket()
    head = bucket.download(key)
    manifest = _decode_manifest(head)
    if manifest is None:
        return head
    out = bytearray()
    for index, expected in enumerate(manifest["parts"]):
        part = bucket.download(_part_key(key, index))
        if len(part) != expected["size"] or \
                hashlib.sha256(part).hexdigest() != expected["sha256"]:
            raise IOError("Stored artifact chunk failed its integrity check.")
        out.extend(part)
    if len(out) != manifest["size"]:
        raise IOError("Stored artifact chunk set is incomplete.")
    return bytes(out)


def exists(key):
    try:
        return bool(_bucket().exists(key))
    except Exception:                                   # pragma: no cover
        return False


def delete(key):
    """Remove one logical artifact and all of its internal parts."""
    bucket = _bucket()
    try:
        head = bucket.download(key)
    except Exception:
        return False
    manifest = _decode_manifest(head)
    names = [key]
    if manifest is not None:
        names.extend(_part_key(key, index)
                     for index in range(len(manifest["parts"])))
    try:
        removed = bucket.remove(names)
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
            # Internal ``.part.`` objects never match the opaque logical-key
            # pattern and therefore cannot become phantom database artifacts.
            if name and key_pattern.fullmatch(name):
                found.append(name)
        if len(batch) < page:
            break
        offset += page
    return found
