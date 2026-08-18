#!/usr/bin/env python3
"""
afcpak.py — the `.afcpak` multi-file archive container.

WHAT THIS IS
------------
A NEUTRAL package format.  It performs NO compression of its own.  Every file
inside is compressed independently by the existing AFC engine
(afc2.compress_bytes) and the resulting AFC1-AFC4 payloads are stored back to
back.  This module only writes a manifest and concatenates bytes.

WHY IT IS BUILT THIS WAY (constraint #1 / #2 of the brief)
----------------------------------------------------------
  * No cross-file back-references.  Each member is compressed in isolation, so
    the archive introduces no LZ-style matching across file boundaries.  A
    "solid" archive would require exactly the offset/back-reference mechanism
    the thesis forbids.
  * NO DEFLATE.  This module never imports zipfile/gzip/zlib/bz2/lzma.  If you
    are tempted to swap this for zipfile, you must pass ZIP_STORED explicitly —
    zipfile's convenience paths and many examples use ZIP_DEFLATED, which is
    LZ77 + Huffman and would silently violate the Huffman-only constraint.
    There is a test (tests/test_app.py::test_archive_has_no_deflate) that fails
    if a DEFLATE stream header ever appears in an archive.
  * Losslessness is verified per member, not per archive: pack() SHA-256s each
    member's round trip before writing it.

BINARY LAYOUT
-------------
    magic        8 bytes   b"AFCPAK01"
    manifest_len 4 bytes   little-endian uint32
    manifest     manifest_len bytes, UTF-8 JSON
    payloads     concatenated AFC containers, in manifest order

Manifest JSON:
    {
      "version": 1,
      "created_utc": "2026-07-28T01:23:45Z",
      "engine": "AFC v4",
      "app_version": "...",
      "entries": [
        {"path": "docs/a.txt",      # original relative path (folder structure)
         "original_bytes": 1234,
         "stored_bytes": 567,       # size of the AFC payload
         "offset": 0,               # offset within the payload region
         "container": "AFC2",       # AFC1 | AFC2 | AFC3 | AFC4
         "sha256": "…",             # of the ORIGINAL bytes
         "lossless_verified": true}
      ]
    }

Path safety: entry paths are sanitised on both write and read.  Absolute
paths, drive letters, and any ".." component are rejected, so extracting an
archive can never escape the destination directory (zip-slip).
"""
import datetime
import hashlib
import io
import json
import os
import posixpath
import struct

import afc2
import config

MAGIC = config.ARCHIVE_MAGIC
_HDR = struct.Struct("<I")


class ArchiveError(ValueError):
    """Raised for malformed archives and unsafe member paths."""


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------

def safe_member_path(raw):
    """Normalise a member path to a relative POSIX path with no traversal.

    Rejects: absolute paths, Windows drive letters, UNC paths, and any '..'
    component.  Returns the cleaned path.  Raises ArchiveError if unsafe."""
    if raw is None:
        raise ArchiveError("archive entry has no path")
    p = str(raw).replace("\\", "/").strip()
    if not p:
        raise ArchiveError("archive entry has an empty path")
    if p.startswith("/") or p.startswith("//"):
        raise ArchiveError("unsafe absolute path in archive: %r" % raw)
    if len(p) >= 2 and p[1] == ":":
        raise ArchiveError("unsafe drive-qualified path in archive: %r" % raw)
    parts = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ArchiveError("unsafe '..' component in archive path: %r" % raw)
        parts.append(part)
    if not parts:
        raise ArchiveError("archive entry resolves to an empty path: %r" % raw)
    return posixpath.join(*parts)


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------

def pack(files, fmt="auto", adaptive=True, verify=True, progress=None):
    """Build an .afcpak archive.

    files:    iterable of (path, data_bytes)
    fmt:      container format passed straight to the engine ('auto'|'afc1'|'afc2')
    verify:   SHA-256 round-trip each member before storing it (default on;
              turning it off is only for benchmarking, never for user data)
    progress: optional callable(index, total, path) for UI feedback

    Returns (archive_bytes, entries) where entries is the manifest list, so the
    caller can write per-file rows into compression_history without re-parsing.
    """
    items = list(files)
    payloads = []
    entries = []
    offset = 0
    for i, (path, data) in enumerate(items):
        if progress:
            progress(i, len(items), path)
        rel = safe_member_path(path)
        blob = afc2.compress_bytes(data, adaptive, fmt=fmt)
        ok = False
        if verify:
            # Per-member losslessness proof (constraint #3).  A member that
            # fails is a hard error — we never ship a silently lossy archive.
            ok = afc2.decompress_bytes(blob) == data
            if not ok:
                raise ArchiveError(
                    "lossless verification FAILED for %r — archive not written"
                    % rel)
        entries.append({
            "path": rel,
            "original_bytes": len(data),
            "stored_bytes": len(blob),
            "offset": offset,
            "container": blob[:4].decode("ascii", "replace"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "lossless_verified": bool(ok),
        })
        payloads.append(blob)
        offset += len(blob)

    manifest = {
        "version": 1,
        "created_utc": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": config.ENGINE_VERSION,
        "app_version": config.APP_VERSION,
        "entries": entries,
    }
    mjson = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    out = io.BytesIO()
    out.write(MAGIC)
    out.write(_HDR.pack(len(mjson)))
    out.write(mjson)
    for blob in payloads:
        out.write(blob)
    return out.getvalue(), entries


# ---------------------------------------------------------------------------
# read / unpack
# ---------------------------------------------------------------------------

def is_archive(blob):
    return len(blob) >= len(MAGIC) and blob[:len(MAGIC)] == MAGIC


def read_manifest(blob):
    """Parse and return (manifest_dict, payload_region_offset)."""
    if not is_archive(blob):
        raise ArchiveError("not an %s archive (bad magic)" % config.ARCHIVE_EXT)
    pos = len(MAGIC)
    if len(blob) < pos + _HDR.size:
        raise ArchiveError("truncated archive header")
    (mlen,) = _HDR.unpack_from(blob, pos)
    pos += _HDR.size
    if len(blob) < pos + mlen:
        raise ArchiveError("truncated archive manifest")
    try:
        manifest = json.loads(blob[pos:pos + mlen].decode("utf-8"))
    except Exception as exc:
        raise ArchiveError("unreadable archive manifest: %s" % exc)
    if not isinstance(manifest, dict) or "entries" not in manifest:
        raise ArchiveError("archive manifest has no entries")
    return manifest, pos + mlen


def unpack(blob, verify=True, progress=None):
    """Decompress every member.

    Returns list of dicts: {path, data, original_bytes, sha256_ok, container}.
    Paths are re-sanitised on read, so a hand-edited archive still cannot
    escape the extraction directory.
    """
    manifest, base = read_manifest(blob)
    out = []
    entries = manifest["entries"]
    for i, ent in enumerate(entries):
        rel = safe_member_path(ent.get("path"))
        if progress:
            progress(i, len(entries), rel)
        off = base + int(ent.get("offset", 0))
        end = off + int(ent.get("stored_bytes", 0))
        if off < base or end > len(blob) or end < off:
            raise ArchiveError("archive entry %r has an out-of-range extent"
                               % rel)
        data = afc2.decompress_bytes(blob[off:end])
        sha_ok = None
        if verify and ent.get("sha256"):
            sha_ok = hashlib.sha256(data).hexdigest() == ent["sha256"]
            if not sha_ok:
                raise ArchiveError(
                    "SHA-256 mismatch extracting %r — archive is corrupt" % rel)
        out.append({"path": rel, "data": data,
                    "original_bytes": len(data),
                    "sha256_ok": sha_ok,
                    "container": ent.get("container", "")})
    return out


def extract_to(blob, dest_dir, verify=True):
    """Write every member back to disk under dest_dir, recreating the original
    folder structure.  Returns the list of absolute paths written."""
    dest_root = os.path.abspath(dest_dir)
    written = []
    for member in unpack(blob, verify=verify):
        target = os.path.abspath(os.path.join(dest_root, member["path"]))
        # belt-and-braces: even after sanitising, confirm containment
        if not (target == dest_root or
                target.startswith(dest_root + os.sep)):
            raise ArchiveError("refusing to write outside destination: %r"
                               % member["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(member["data"])
        written.append(target)
    return written
