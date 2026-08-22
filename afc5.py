#!/usr/bin/env python3
"""AFC5 self-verifying outer envelope.

AFC5 does not add a compression algorithm. It wraps one existing AFC1-AFC4/AFC6
payload with the original length, SHA-256, payload SHA-256, and a safe original
basename. AFC3/AFC4/AFC6 carry their component manifests internally.
"""

import hashlib
import os

MAGIC = b"AFC5"
VERSION = 1
FLAG_NAME = 1
INNER_MAGICS = (b"AFC1", b"AFC2", b"AFC3", b"AFC4", b"AFC6")
MAX_NAME_BYTES = 1024
MAX_VARINT_BYTES = 10


def _put_varint(out, value):
    if value < 0:
        raise ValueError("negative AFC5 varint")
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return


def _get_varint(blob, pos):
    value = 0
    shift = 0
    for _ in range(MAX_VARINT_BYTES):
        if pos >= len(blob):
            raise ValueError("truncated AFC5 varint")
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
        shift += 7
    raise ValueError("oversized AFC5 varint")


def safe_name(filename):
    """Return a path-free, control-free UTF-8 basename for the envelope."""
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    name = "".join(char for char in name if ord(char) >= 32 and char not in "\x7f")
    if not name:
        return ""
    raw = name.encode("utf-8")[:MAX_NAME_BYTES]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def wrap(original, payload, filename=""):
    if payload[:4] not in INNER_MAGICS:
        raise ValueError("AFC5 payload must be AFC1-AFC4 or AFC6")
    name = safe_name(filename)
    encoded_name = name.encode("utf-8")
    flags = FLAG_NAME if encoded_name else 0
    out = bytearray(MAGIC)
    out.extend((VERSION, flags))
    _put_varint(out, len(original))
    _put_varint(out, len(payload))
    _put_varint(out, len(encoded_name))
    out.extend(hashlib.sha256(original).digest())
    out.extend(hashlib.sha256(payload).digest())
    out.extend(encoded_name)
    out.extend(payload)
    return bytes(out)


def parse(blob, verify_payload=True):
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise ValueError("not an AFC5 container")
    version = blob[4]
    flags = blob[5]
    if version != VERSION:
        raise ValueError("unsupported AFC5 version %d" % version)
    if flags & ~FLAG_NAME:
        raise ValueError("unknown AFC5 flags")
    pos = 6
    original_length, pos = _get_varint(blob, pos)
    payload_length, pos = _get_varint(blob, pos)
    name_length, pos = _get_varint(blob, pos)
    if name_length > MAX_NAME_BYTES:
        raise ValueError("unreasonable AFC5 filename length")
    if pos + 64 + name_length > len(blob):
        raise ValueError("truncated AFC5 header")
    original_sha256 = bytes(blob[pos:pos + 32]).hex()
    pos += 32
    payload_sha256 = bytes(blob[pos:pos + 32]).hex()
    pos += 32
    raw_name = bytes(blob[pos:pos + name_length])
    pos += name_length
    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid AFC5 filename encoding") from exc
    if name != safe_name(name):
        raise ValueError("unsafe AFC5 filename")
    if bool(flags & FLAG_NAME) != bool(name):
        raise ValueError("AFC5 filename flag mismatch")
    if payload_length > len(blob) - pos or pos + payload_length != len(blob):
        raise ValueError("AFC5 payload length mismatch")
    payload = bytes(blob[pos:])
    if payload[:4] not in INNER_MAGICS:
        raise ValueError("invalid AFC5 inner container")
    if verify_payload and hashlib.sha256(payload).hexdigest() != payload_sha256:
        raise ValueError("AFC5 payload SHA-256 mismatch")
    return {
        "magic": "AFC5", "version": version, "flags": flags,
        "original_length": original_length, "original_sha256": original_sha256,
        "payload_length": payload_length, "payload_sha256": payload_sha256,
        "payload_magic": payload[:4].decode("ascii"),
        "original_name": name, "payload": payload,
        "header_bytes": pos,
    }


def unwrap(blob, verify_payload=True):
    return parse(blob, verify_payload=verify_payload)["payload"]


def decompress(blob, decompress_fn):
    info = parse(blob, verify_payload=True)
    restored = decompress_fn(info["payload"])
    if len(restored) != info["original_length"]:
        raise ValueError("AFC5 restored length mismatch")
    if hashlib.sha256(restored).hexdigest() != info["original_sha256"]:
        raise ValueError("AFC5 original SHA-256 mismatch")
    return restored
