#!/usr/bin/env python3
"""Private on-disk storage for produced AFC containers.

Only compressed outputs are written here.  Uploads and restored originals stay
in memory.  Storage keys are server-generated opaque identifiers, never user
filenames, and every read re-checks the recorded SHA-256 digest.
"""
import hashlib
import os
import re
import time
import uuid

import config


_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


class ArtifactIntegrityError(IOError):
    """Raised when a stored artifact is missing, truncated, or changed."""


def ensure_dir():
    root = os.path.realpath(os.path.abspath(config.RESULT_STORAGE_DIR))
    static_root = os.path.realpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static"))
    try:
        inside_static = os.path.commonpath((root, static_root)) == static_root
    except ValueError:  # different Windows drives cannot contain one another
        inside_static = False
    if inside_static:
        raise ValueError(
            "AFC_RESULT_STORAGE_DIR must be outside the public static folder.")
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # Windows applies its ACL instead of POSIX mode bits
        pass
    return root


def _path(storage_key):
    if not _KEY_RE.fullmatch(str(storage_key or "")):
        raise ValueError("Invalid artifact storage key.")
    root = os.path.realpath(ensure_dir())
    path = os.path.realpath(os.path.join(root, storage_key))
    if os.path.dirname(path) != root:
        raise ValueError("Artifact path escaped the storage directory.")
    return path


def write(blob):
    """Atomically persist bytes and return ``(opaque_key, sha256_hex)``."""
    key = uuid.uuid4().hex
    target = _path(key)
    temporary = target + ".tmp-" + uuid.uuid4().hex
    digest = hashlib.sha256(blob).hexdigest()
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return key, digest


def read_verified(storage_key, expected_sha256, expected_size):
    """Read one artifact and reject any on-disk integrity mismatch."""
    try:
        with open(_path(storage_key), "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        raise ArtifactIntegrityError("Stored file is unavailable.") from exc
    digest = hashlib.sha256(blob).hexdigest()
    if len(blob) != int(expected_size) or digest != expected_sha256:
        raise ArtifactIntegrityError(
            "Stored file failed its SHA-256 integrity check.")
    return blob


def exists(storage_key):
    return os.path.isfile(_path(storage_key))


def delete(storage_key):
    """Remove an artifact if present.  Invalid keys are always rejected."""
    target = _path(storage_key)
    try:
        os.remove(target)
    except FileNotFoundError:
        return False
    return True


def list_keys():
    """Return only complete opaque artifact files, never arbitrary paths."""
    try:
        names = os.listdir(ensure_dir())
    except OSError:
        return []
    return [name for name in names if _KEY_RE.fullmatch(name)]


def remove_stale_temporary_files(min_age_seconds=3600):
    """Remove incomplete atomic-write files left by a terminated process."""
    root = ensure_dir()
    removed = 0
    for name in os.listdir(root):
        if not re.fullmatch(r"[0-9a-f]{32}\.tmp-[0-9a-f]{32}", name):
            continue
        path = os.path.join(root, name)
        try:
            if time.time() - os.path.getmtime(path) < min_age_seconds:
                continue
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
    return removed
