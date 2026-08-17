#!/usr/bin/env python3
"""
filetypes.py — content-based file type detection for the Compress and
Decompress pages.

WHY THIS EXISTS
---------------
The Compress page must accept a normal file (PDF, DOCX, TXT, CSV, JSON, SQL,
source code, binary) and process it automatically — the user never extracts
PDF page streams, DOCX XML or package components by hand.  The Decompress page
must rebuild the original file and give it back under its real name.

Because an AFC container stores only `magic | mode | original_length | payload`
and nothing else, the original filename extension is NOT carried inside it.
This module recovers it by sniffing the RESTORED BYTES, which is why
`MyDocument.afc` can be handed back as `MyDocument.pdf`.

SCOPE — READ BEFORE EDITING
---------------------------
This module performs NO compression and NO decompression.  It only looks at
bytes and names them.  It deliberately imports no compression library (no
zipfile/zlib/gzip/bz2/lzma/tarfile): ZIP-based formats such as DOCX are
identified by scanning for their literal package markers in the raw bytes, not
by opening the archive.  That keeps the Huffman-only constraint of
SCOPE_NOTES.md structurally intact — see §2 there for why importing a codec
here would be a problem even if it were never used to produce output.
"""
import json
import re

# Families are used by the UI to group what it shows; they are descriptive
# only and never change how a file is processed.
DOCUMENT = "document"
TEXT = "text"
DATA = "data"
CODE = "code"
IMAGE = "image"
ARCHIVE = "archive"
MEDIA = "media"
BINARY = "binary"
AFC = "afc"

_UNKNOWN = {"ext": "", "label": "Unknown / raw binary", "family": BINARY,
            "mime": "application/octet-stream", "container_aware": False}


def _hit(ext, label, family, mime, container_aware=False):
    return {"ext": ext, "label": label, "family": family, "mime": mime,
            "container_aware": container_aware}


# Signatures checked at offset 0, longest-first so PK\x03\x04 cannot shadow a
# more specific match. Each entry: (magic_bytes, result).
_MAGIC = [
    (b"%PDF-", _hit(".pdf", "PDF document", DOCUMENT, "application/pdf", True)),
    (b"AFCPAK01", _hit(".afcpak", "AFC archive", AFC,
                       "application/octet-stream")),
    (b"AFC1", _hit(".afc", "AFC container (AFC1)", AFC,
                   "application/octet-stream")),
    (b"AFC2", _hit(".afc", "AFC container (AFC2)", AFC,
                   "application/octet-stream")),
    (b"\x89PNG\r\n\x1a\n", _hit(".png", "PNG image", IMAGE, "image/png")),
    (b"\xff\xd8\xff", _hit(".jpg", "JPEG image", IMAGE, "image/jpeg")),
    (b"GIF87a", _hit(".gif", "GIF image", IMAGE, "image/gif")),
    (b"GIF89a", _hit(".gif", "GIF image", IMAGE, "image/gif")),
    (b"BM", _hit(".bmp", "Bitmap image", IMAGE, "image/bmp")),
    (b"\x1f\x8b", _hit(".gz", "gzip stream", ARCHIVE, "application/gzip")),
    (b"\x7fELF", _hit("", "ELF executable", BINARY,
                      "application/octet-stream")),
    (b"SQLite format 3\x00", _hit(".sqlite3", "SQLite database", DATA,
                                  "application/vnd.sqlite3")),
    (b"{\\rtf", _hit(".rtf", "Rich Text document", DOCUMENT,
                     "application/rtf")),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
     _hit(".doc", "Legacy Microsoft Office document", DOCUMENT,
          "application/msword", True)),
    (b"OggS", _hit(".ogg", "Ogg media", MEDIA, "audio/ogg")),
    (b"fLaC", _hit(".flac", "FLAC audio", MEDIA, "audio/flac")),
    (b"ID3", _hit(".mp3", "MP3 audio", MEDIA, "audio/mpeg")),
    (b"%!PS", _hit(".ps", "PostScript document", DOCUMENT,
                   "application/postscript")),
]

# ZIP-based OOXML/ODF packages. The value is the marker that must appear in the
# package's central directory / local headers, which are plain bytes in the
# stream — no archive library needed to spot them.
_ZIP_PACKAGES = [
    (b"word/document.xml",
     _hit(".docx", "Word document (DOCX)", DOCUMENT,
          "application/vnd.openxmlformats-officedocument"
          ".wordprocessingml.document", True)),
    (b"xl/workbook.xml",
     _hit(".xlsx", "Excel workbook (XLSX)", DOCUMENT,
          "application/vnd.openxmlformats-officedocument"
          ".spreadsheetml.sheet", True)),
    (b"ppt/presentation.xml",
     _hit(".pptx", "PowerPoint presentation (PPTX)", DOCUMENT,
          "application/vnd.openxmlformats-officedocument"
          ".presentationml.presentation", True)),
    (b"opendocument.text",
     _hit(".odt", "OpenDocument text", DOCUMENT,
          "application/vnd.oasis.opendocument.text", True)),
    (b"opendocument.spreadsheet",
     _hit(".ods", "OpenDocument spreadsheet", DOCUMENT,
          "application/vnd.oasis.opendocument.spreadsheet", True)),
]

_SQL_RE = re.compile(
    rb"\b(CREATE\s+TABLE|INSERT\s+INTO|SELECT\s+.+\s+FROM|ALTER\s+TABLE|"
    rb"DROP\s+TABLE|BEGIN\s+TRANSACTION)\b", re.I)


def _looks_textual(head: bytes) -> bool:
    """True when the head is dominated by printable ASCII/UTF-8 text."""
    if not head:
        return False
    if b"\x00" in head:
        return False
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b <= 126)
    return (printable / len(head)) >= 0.90


def _classify_text(head: bytes, sample: bytes) -> dict:
    """Name a textual payload. Ordered most-specific first."""
    stripped = sample.lstrip()
    lowered = stripped[:512].lower()

    if stripped[:1] in (b"{", b"["):
        try:
            json.loads(sample.decode("utf-8", "strict"))
            return _hit(".json", "JSON data", DATA, "application/json")
        except Exception:
            # `sample` is capped, so a large JSON file always fails the strict
            # parse above on a mid-value truncation. Fall back to structure:
            # an object/array opener plus quoted keys is JSON in practice.
            if b'":' in stripped[:4096] or b'": ' in stripped[:4096]:
                return _hit(".json", "JSON data", DATA, "application/json")

    if lowered.startswith(b"<?xml"):
        return _hit(".xml", "XML document", DATA, "application/xml")
    # Real-world HTML often omits <!doctype> and <html> (Canterbury's cp.html
    # opens on <head>), so accept any of the structural tags near the start.
    if stripped[:1] == b"<" and any(
            t in lowered for t in (b"<!doctype html", b"<html", b"<head",
                                   b"<body", b"<title", b"<div", b"<meta")):
        return _hit(".html", "HTML document", TEXT, "text/html")
    if _SQL_RE.search(sample[:8192]):
        return _hit(".sql", "SQL script", DATA, "application/sql")

    # CSV/TSV: several lines carrying a consistent, non-zero delimiter count.
    lines = [ln for ln in sample.split(b"\n")[:20] if ln.strip()]
    if len(lines) >= 2:
        for delim, ext, label in ((b",", ".csv", "CSV data"),
                                  (b"\t", ".tsv", "TSV data")):
            counts = [ln.count(delim) for ln in lines]
            if counts[0] >= 1 and len(set(counts)) == 1:
                return _hit(ext, label, DATA, "text/csv")

    # Source code — cheap keyword vote, only to label the file for the user.
    code_markers = (
        (b"def ", b"import ", b"class "), ".py", "Python source",
    )
    if sum(1 for m in code_markers[0] if m in sample[:8192]) >= 2:
        return _hit(".py", "Python source", CODE, "text/x-python")
    if b"#include" in sample[:4096] or b"int main(" in sample[:8192]:
        return _hit(".c", "C/C++ source", CODE, "text/x-c")
    if (b"function " in sample[:8192] and b"var " in sample[:8192]) or \
            b"=>" in sample[:2048] and b"const " in sample[:4096]:
        return _hit(".js", "JavaScript source", CODE, "text/javascript")

    return _hit(".txt", "Plain text", TEXT, "text/plain")


def sniff(data: bytes) -> dict:
    """Identify a byte stream.

    Returns {ext, label, family, mime, container_aware}. `container_aware` is
    True for packaged formats (PDF, DOCX, XLSX, PPTX, ODF) whose internal
    components the app handles automatically — the user supplies the whole
    file and gets the whole file back.

    Never raises: an unrecognised stream is reported as raw binary.
    """
    if not data:
        return dict(_UNKNOWN)
    head = data[:4096]

    for magic, result in _MAGIC:
        if data.startswith(magic):
            return dict(result)

    # ZIP container: decide which package it is from its member names.
    if data[:2] == b"PK" and data[2:4] in (b"\x03\x04", b"\x05\x06",
                                           b"\x07\x08"):
        window = data[:65536]
        for marker, result in _ZIP_PACKAGES:
            if marker in window:
                return dict(result)
        return _hit(".zip", "ZIP archive", ARCHIVE, "application/zip")

    # MP4/MOV family carries 'ftyp' at offset 4.
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return _hit(".mp4", "MP4 media", MEDIA, "video/mp4")

    if _looks_textual(head):
        return _classify_text(head, data[:65536])

    return dict(_UNKNOWN)


def restored_name(afc_filename: str, restored: bytes) -> str:
    """Best original filename for bytes recovered from `afc_filename`.

    Rules, in order:
      1. `report.pdf.afc` already names the original -> `report.pdf`.
      2. `report.afc` -> strip `.afc`, then append the sniffed extension.
      3. no `.afc` suffix at all -> append the sniffed extension to the stem.

    The sniffed extension is only appended when the stem does not already end
    in it, so `notes.txt.afc` never becomes `notes.txt.txt`.
    """
    name = (afc_filename or "file").strip() or "file"
    stem = name[:-4] if name.lower().endswith(".afc") else name

    info = sniff(restored)
    ext = info["ext"]

    root, dot, existing = stem.rpartition(".")
    if dot and existing and len(existing) <= 8 and "/" not in existing:
        # The stem already carries an extension (case 1) — keep it.
        return stem
    if ext and not stem.lower().endswith(ext):
        return stem + ext
    return stem


def describe(data: bytes) -> dict:
    """sniff() plus a `detected` display string, for JSON responses."""
    info = sniff(data)
    info["detected"] = info["label"]
    return info
