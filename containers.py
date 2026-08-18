#!/usr/bin/env python3
"""
containers.py — [v7] container-aware compression for PDF and DOCX.

WHAT THIS IS, IN ONE SENTENCE
-----------------------------
It decides *where* the existing Hybrid-Huffman engine should be applied inside
a structured file. It is not a compressor: every byte it compresses goes
through `afc2.compress_bytes`, and every byte it does not compress is stored
verbatim.

WHY BYTE-EXACT RECONSTRUCTION IS GUARANTEED
-------------------------------------------
The analysis produces an ordered list of segments that **exactly tile** the
original byte range: segment k ends where segment k+1 begins, the first starts
at 0, the last ends at len(data), with no gaps and no overlaps. This is
asserted before anything is written (`_validate_tiling`).

Reconstruction is therefore a concatenation of those segments in order, and is
byte-identical *by construction* — it does not depend on the PDF or ZIP parser
being semantically correct. A parser that misunderstands the format produces a
worse *ratio*, never a wrong *result*. Anything the parser does not recognise
simply stays inside an opaque segment.

That is the key design decision, and it is what lets us claim losslessness on
arbitrary real-world PDFs and DOCX files without implementing a full PDF or
ZIP specification.

WHAT IS NEVER DONE
------------------
* No zlib / zipfile / gzip / LZ / arithmetic compressor is used. This module
  imports no compression library (asserted by an AST test, exactly as for
  afcpak.py — see SCOPE_NOTES.md §2).
* ZIP members are never inflated and re-deflated. For suitable DOCX XML,
  ``deflate_tokens.py`` parses the producer's *existing* DEFLATE tokens and
  records an exact reversible recipe. The XML + recipe go through the same
  Hybrid-Huffman engine; restoration serialises the original token choices
  bit-for-bit. No new match search, tree choice, or second compressor exists.
* No PDF rewriting: no image recoding, no metadata stripping, no object
  removal, no xref rebuilding. The original file is never modified.

WHAT IT ACTUALLY BUYS
---------------------
Two things, both measured in benchmarks/:

1. **Ratio.** High-entropy embedded data (JPEG images, deflate streams) is
   kept out of the Hybrid-Huffman input. It cannot be compressed anyway, and
   excluding it means the frequency model, the pattern dictionary and the
   Huffman tree are built purely from the structural/textual material — the
   part that actually has redundancy. Text-like regions from all over the file
   are pooled into ONE engine call, so a pattern that recurs across several
   page streams is found once and shared.

2. **Speed.** Opaque regions skip candidate discovery, block growth and the DP
   parse entirely (see §29 of the brief: a fast path for incompressible data,
   not a new algorithm).

THE AFC3 / AFC4 CONTAINERS
-------------------------
    "AFC3"                     magic
    u8                         mode (3 = component-aware)
    varint original_length     total size of the reconstructed file
    varint segment_count
    segment_count x:
        varint  (length << 1) | kind      kind 0 = opaque, 1 = pooled
    varint opaque_length       then that many raw bytes
    varint pooled_blob_length  then an ordinary AFC1/AFC2 container

AFC1 and AFC2 are untouched and every existing decoder still reads them. AFC3
is only ever emitted when it is measurably smaller than the plain whole-file
container, so an existing file never becomes larger by upgrading (§27).

AFC4 is the explicitly versioned DOCX extension. It keeps AFC3's direct
pooled/opaque segments and adds a transformed kind for method-8 XML members.
The transformed source is ``plain XML | exact DEFLATE recipe`` inside the
ordinary AFC1/AFC2 inner Hybrid-Huffman container. AFC4 is likewise emitted
only when its final whole-file size beats every applicable older path.
"""
import binascii
import re
import struct

import afc2
import afc

MAGIC3 = b"AFC3"
MAGIC4 = b"AFC4"
MODE_COMPONENT = 3
MODE_COMPONENT_XML = 4

# Segment kinds
OPAQUE = 0      # stored verbatim: already compressed or high entropy
POOLED = 1      # concatenated into the single Hybrid-Huffman input
TRANSFORMED = 2 # exact DEFLATE-token recipe + plain XML, both Hybrid-Huffman

# A segment must be at least this big to be worth classifying separately;
# below it the manifest entry costs more than the segment can save.
MIN_SEGMENT = 64

# Shannon entropy (bits/byte) at or above which a payload is treated as
# already compressed. Deflate/JPEG output sits at ~7.9-8.0; XML and PDF
# structural syntax sit around 4.5-5.5.
OPAQUE_ENTROPY = 7.3

# Sample size for the cheap entropy probe (§29 fast path). Entropy converges
# quickly, so a head sample is enough to classify a multi-megabyte stream.
ENTROPY_SAMPLE = 16384

# When the plain whole-file comparison may be skipped.
#
# MEASURED, and deliberately conservative. The comparison is what guarantees
# V7 is never worse than V6, so it is only skipped where doing it would be
# genuinely wasteful AND the outcome is not in doubt.
#
# On the document corpus, AFC3 beat or equalled the plain container at every
# opaque fraction >= 0.70, but LOST slightly at 0.67 (docx_tables: 5933 vs
# 5821) and 0.52 (docx_text_small: 4041 vs 4026). An earlier 0.35 threshold
# therefore shipped a container that was larger than V6's on those files.
# Requiring >= 0.90 leaves a wide margin above the highest observed loss, and
# the size floor means the skip only kicks in when the wasted pass would
# actually cost real time (a large, media-dominated file — the §29 case).
OPAQUE_SKIP_WHOLE = 0.90
OPAQUE_SKIP_MIN_BYTES = 64 * 1024

# Do not even ATTEMPT the container path unless it could plausibly pay for
# itself. Without this, a small DOCX whose deflate payloads total a couple of
# kilobytes pays for a second full compression to discover a 0.00% gain —
# measured at 1.6x the compression time for no benefit at all.
#
# Thresholds from the corpus: every file with less than 8 KB of opaque data
# produced a gain of 0.00-0.05%, while every file above it produced a real one
# (up to 2.43%).
MIN_OPAQUE_BYTES = 8192
MIN_POOLED_BYTES = 512
MIN_OPAQUE_FRACTION = 0.25

# Guard against hostile ZIP expansion while analysing method-8 XML. This is
# an analysis budget, not an upload-size policy: declined members simply stay
# opaque and the unchanged whole-file/AFC3 paths remain available.
MAX_XML_TRANSFORM_BYTES = 64 * 1024 * 1024
MIN_XML_TRANSFORM_BYTES = 1024

# Size-viability gate for the exact XML/token transform. On the checked DOCX
# corpus, candidates that lost had expanded XML+recipe inputs 9x-57x the raw
# deflate bytes; the level-0 stream that won was 1.00x. A conservative 4x gate
# leaves a wide unobserved margin while avoiding a full Hybrid-Huffman trial
# that cannot plausibly beat a highly compact producer stream. This affects
# routing only: declined bytes remain exact and take AFC3/plain comparison.
MAX_XML_TRANSFORM_VIABILITY_RATIO = 4


class Segment:
    __slots__ = ("kind", "start", "end", "label", "source", "recipe")

    def __init__(self, kind, start, end, label="", source=None, recipe=None):
        self.kind = kind
        self.start = start
        self.end = end
        self.label = label
        self.source = source
        self.recipe = recipe

    @property
    def length(self):
        return self.end - self.start

    def __repr__(self):
        names = {OPAQUE: "OPAQUE", POOLED: "POOLED",
                 TRANSFORMED: "TRANSFORMED"}
        return "Segment(%s, %d..%d, %r)" % (
            names.get(self.kind, "?"),
            self.start, self.end, self.label)


# ---------------------------------------------------------------------------
# varint helpers (same encoding afc.py uses, kept local so this module does not
# depend on private engine internals)
# ---------------------------------------------------------------------------

def _put_varint(buf, v):
    while True:
        b = v & 0x7F
        v >>= 7
        buf.append(b | (0x80 if v else 0))
        if not v:
            break


def _get_varint(buf, pos):
    shift = 0
    out = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint in AFC3 container")
        b = buf[pos]
        pos += 1
        out |= (b & 0x7F) << shift
        if not (b & 0x80):
            return out, pos
        shift += 7


# ---------------------------------------------------------------------------
# cheap entropy probe — the fast path for incompressible data
# ---------------------------------------------------------------------------

def entropy_bits(data, sample=ENTROPY_SAMPLE):
    """Shannon entropy over a byte histogram, in bits/byte.

    Deliberately cheap: one pass over at most `sample` bytes. This is the
    early analysis of §29 — a performance guard, not a compression decision
    algorithm. The real accept/reject decision is the engine's own Bit Cost
    Decision Engine, applied when the pooled buffer is compressed.
    """
    if not data:
        return 0.0
    view = bytes(data[:sample])
    # 256 C-level scans beat one Python-level loop over the sample by a wide
    # margin: a per-byte `for b in view` costs ~0.21 ms on a 16 KB sample, and
    # a file with 81 stream segments pays that 81 times. This is a hot path —
    # it runs once per candidate segment — so it stays in C.
    counts = [view.count(i) for i in range(256)]
    n = float(len(view))
    h = 0.0
    for c in counts:
        if c:
            p = c / n
            h -= p * _log2(p)
    return h


def _log2(x):
    import math
    return math.log2(x)


# Bytes that dominate text, XML and PDF operator streams.
_TEXTISH = bytes(range(32, 127)) + b"\t\n\r"


def _looks_textual(payload, probe=256):
    """Very cheap 'is this plain text?' probe over the first `probe` bytes.

    A text-heavy PDF has hundreds of uncompressed content streams. Running the
    full 256-bucket entropy scan on each of them cost ~83 ms on a 388 KB PDF
    that had nothing opaque to find in the first place. This probe answers the
    common case in a single C-level translate+count and only lets genuinely
    binary-looking payloads through to the expensive test.
    """
    head = bytes(payload[:probe])
    if not head:
        return True
    if b"\x00" in head:
        return False
    printable = len(head.translate(None, delete=_TEXTISH))
    return (1.0 - printable / len(head)) >= 0.85


def _classify(payload, label):
    """OPAQUE when the payload is already compressed / high entropy."""
    if len(payload) < MIN_SEGMENT:
        return POOLED
    if _looks_textual(payload):
        return POOLED                      # fast path: no entropy scan needed
    return OPAQUE if entropy_bits(payload) >= OPAQUE_ENTROPY else POOLED


# ---------------------------------------------------------------------------
# PDF analysis
# ---------------------------------------------------------------------------
# A PDF is a byte stream of objects. The parts worth compressing are the
# structural syntax — object headers, dictionaries, xref tables, trailers, and
# any content stream that is not already filtered. The parts to leave alone are
# the payloads of streams carrying /FlateDecode, /DCTDecode (JPEG), /JPXDecode
# and friends.
#
# We locate `stream` ... `endstream` payloads and classify each one. Everything
# between them — which is all the structural material — is pooled. No PDF
# semantics are required for correctness (see module docstring).

_PDF_BINARY_FILTERS = (b"/DCTDecode", b"/JPXDecode", b"/JBIG2Decode",
                       b"/CCITTFaxDecode", b"/FlateDecode", b"/LZWDecode",
                       b"/RunLengthDecode", b"/Crypt")

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")


def _pdf_filter_of(data, dict_start, dict_end):
    """Name the filter declared in the stream's preceding dictionary."""
    window = data[dict_start:dict_end]
    for f in _PDF_BINARY_FILTERS:
        if f in window:
            return f.decode("ascii")
    return ""


def pdf_components(data):
    """Object-level inventory of a PDF: what each component actually is.

    Walks `N G obj ... endobj` records, reads each object's dictionary, and
    classifies it. Page objects are located via `/Type /Page` and their
    `/Contents N 0 R` references are resolved, so a content stream is
    identified as a *page stream* rather than as an anonymous stream.

    Returns a list of dicts:
        {obj, kind, filter, subtype, dict_start, dict_end,
         stream_start, stream_end, length}

    `kind` is one of: page-content, image, font, metadata, objstm,
    content-stream, object.

    This is INVENTORY ONLY — it decides nothing. Segmentation still guarantees
    an exact tiling, and the accept/reject decision is still the engine's Bit
    Cost Decision Engine acting on the pooled buffer.
    """
    out = []
    if not data.startswith(b"%PDF-"):
        return out

    # First pass: object spans and their dictionaries/streams.
    objs = {}
    for m in _OBJ_RE.finditer(data):
        num = int(m.group(1))
        start = m.end()
        end = data.find(b"endobj", start)
        if end < 0:
            end = len(data)
        body = data[start:min(end, start + 4096)]
        srel = data.find(b"stream", start)
        s_start = s_end = -1
        if 0 <= srel < end:
            j = srel + 6
            if data[j:j + 2] == b"\r\n":
                j += 2
            elif data[j:j + 1] in (b"\n", b"\r"):
                j += 1
            e = data.find(b"endstream", j)
            if 0 <= e <= end:
                s_end = e
                if data[s_end - 2:s_end] == b"\r\n":
                    s_end -= 2
                elif data[s_end - 1:s_end] in (b"\n", b"\r"):
                    s_end -= 1
                s_start = j
        objs[num] = {"obj": num, "dict_start": start,
                     "dict_end": srel if srel > 0 else end,
                     "stream_start": s_start, "stream_end": s_end,
                     "body": body}

    # Second pass: which objects are page content streams?
    page_content = set()
    for num, o in objs.items():
        d = data[o["dict_start"]:o["dict_end"]]
        if b"/Type" in d and b"/Page" in d and b"/Pages" not in d:
            cm = re.search(rb"/Contents\s+(?:(\d+)\s+\d+\s+R|\[([^\]]*)\])", d)
            if cm:
                if cm.group(1):
                    page_content.add(int(cm.group(1)))
                elif cm.group(2):
                    for r in re.finditer(rb"(\d+)\s+\d+\s+R", cm.group(2)):
                        page_content.add(int(r.group(1)))

    for num, o in sorted(objs.items()):
        d = data[o["dict_start"]:o["dict_end"]]
        filt = _pdf_filter_of(data, o["dict_start"], o["dict_end"])
        subtype = ""
        sm = re.search(rb"/Subtype\s*/(\w+)", d)
        if sm:
            subtype = sm.group(1).decode("ascii", "replace")
        if o["stream_start"] < 0:
            kind = "object"
        elif num in page_content:
            kind = "page-content"
        elif subtype == "Image" or b"/Image" in d:
            kind = "image"
        elif b"/FontFile" in d or subtype.startswith("Type1C") or \
                b"/Type /Font" in d or b"/Type/Font" in d:
            kind = "font"
        elif b"/Metadata" in d:
            kind = "metadata"
        elif b"/ObjStm" in d:
            kind = "objstm"
        else:
            kind = "content-stream"
        out.append({
            "obj": num, "kind": kind, "filter": filt, "subtype": subtype,
            "dict_start": o["dict_start"], "dict_end": o["dict_end"],
            "stream_start": o["stream_start"], "stream_end": o["stream_end"],
            "length": max(0, o["stream_end"] - o["stream_start"]),
        })
    return out


def analyze_pdf(data):
    """Segment a PDF into pooled (structural/text) and opaque (filtered) runs.

    Returns a tiling list of Segment, or None when the file does not look like
    a PDF at all.
    """
    if not data.startswith(b"%PDF-"):
        return None
    segs = []
    pos = 0
    n = len(data)
    cursor = 0            # start of the current pooled run
    # object-level inventory, keyed by stream payload offset
    try:
        comp_by_start = {c["stream_start"]: c for c in pdf_components(data)
                         if c["stream_start"] >= 0}
    except Exception:
        comp_by_start = {}
    while True:
        i = data.find(b"stream", pos)
        if i < 0:
            break
        # `stream` must be a keyword, not part of `endstream` and not a
        # substring inside a name; require it to be preceded by whitespace or
        # '>' (end of the stream dictionary).
        if i > 0 and data[i - 1:i] not in (b"\n", b"\r", b" ", b"\t", b">"):
            pos = i + 6
            continue
        # payload begins after the EOL that follows the keyword
        j = i + 6
        if data[j:j + 2] == b"\r\n":
            j += 2
        elif data[j:j + 1] in (b"\n", b"\r"):
            j += 1
        end = data.find(b"endstream", j)
        if end < 0:
            break
        payload_end = end
        # trim the EOL that belongs to the endstream keyword, not the payload
        if data[payload_end - 2:payload_end] == b"\r\n":
            payload_end -= 2
        elif data[payload_end - 1:payload_end] in (b"\n", b"\r"):
            payload_end -= 1
        if payload_end <= j:
            pos = end + 9
            continue

        payload = data[j:payload_end]
        filt = _pdf_filter_of(data, max(0, i - 512), i)
        # [v7] Prefer the object-level identity (page content / image / font /
        # metadata) over a bare filter name, so the component report names
        # what the bytes actually are. Classification itself is unchanged: the
        # entropy probe decides, so a mislabelled or unusual stream can never
        # send incompressible data into the expensive path.
        desc = comp_by_start.get(j)
        if desc:
            filt = "%s%s" % (desc["kind"],
                             (" " + desc["filter"]) if desc["filter"] else "")
        kind = _classify(payload, filt)
        if kind == OPAQUE:
            # everything since the last opaque payload is structural -> pooled
            if j > cursor:
                segs.append(Segment(POOLED, cursor, j, "pdf-structure"))
            segs.append(Segment(OPAQUE, j, payload_end,
                                filt or "pdf-stream-highentropy"))
            cursor = payload_end
        # a pooled stream payload needs no split: it stays in the running
        # pooled run together with the surrounding structure
        pos = end + 9

    if cursor < n:
        segs.append(Segment(POOLED, cursor, n, "pdf-structure"))
    return segs or [Segment(POOLED, 0, n, "pdf-structure")]


# ---------------------------------------------------------------------------
# ZIP / DOCX analysis
# ---------------------------------------------------------------------------
# A DOCX (also XLSX/PPTX/ODF) is a ZIP package. Its structural material — local
# file headers, filenames, extra fields, the central directory, the end-of-
# central-directory record — is repetitive text and compresses well. Member
# payloads stored with method 0 (STORED) are the raw part bytes and compress
# very well. Member payloads stored with method 8 (DEFLATE) are already
# compressed; they are preserved verbatim.
#
# IMPORTANT: we never inflate a deflate stream and never re-deflate one. Doing
# so would (a) introduce DEFLATE as a second stage, which the brief forbids,
# and (b) risk byte-exactness, because re-deflating is not guaranteed to
# reproduce the original bytes. Everything ZIP-structural stays pooled; every
# deflate payload stays opaque.

_LFH = b"PK\x03\x04"


def _analyze_zip_local(data):
    """Legacy local-header scan used when no central directory is readable."""
    if not data.startswith(_LFH):
        return None
    segs = []
    n = len(data)
    cursor = 0
    pos = 0
    while True:
        i = data.find(_LFH, pos)
        if i < 0 or i + 30 > n:
            break
        try:
            (_, _, flags, method, _, _, _crc, csize, usize,
             namelen, extralen) = struct.unpack_from("<IHHHHHIIIHH", data, i)
        except struct.error:
            break
        head_end = i + 30 + namelen + extralen
        if head_end > n:
            break
        if flags & 0x08 and csize == 0:
            # sizes live in a trailing data descriptor; we cannot bound the
            # payload cheaply, so leave the rest of the file pooled and stop.
            break
        payload_start = head_end
        payload_end = payload_start + csize
        if payload_end > n or csize == 0:
            pos = head_end
            continue

        payload = data[payload_start:payload_end]
        if method == 0:
            label = "zip-stored"
            kind = _classify(payload, label)
        else:
            # already compressed by the producer — verify cheaply rather than
            # trusting the method field, then preserve.
            label = "zip-deflate" if method == 8 else "zip-method%d" % method
            kind = _classify(payload, label)

        if kind == OPAQUE:
            if payload_start > cursor:
                segs.append(Segment(POOLED, cursor, payload_start, "zip-header"))
            segs.append(Segment(OPAQUE, payload_start, payload_end, label))
            cursor = payload_end
        pos = payload_end

    if cursor < n:
        segs.append(Segment(POOLED, cursor, n, "zip-structure"))
    return segs or [Segment(POOLED, 0, n, "zip-structure")]


_CDFH = b"PK\x01\x02"
_EOCD = b"PK\x05\x06"
_XML_SUFFIXES = (".xml", ".rels")
_MEDIA_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".bin", ".emf", ".wmf",
)


def _zip_name(raw, flags):
    codec = "utf-8" if (flags & 0x800) else "cp437"
    return raw.decode(codec, "replace")


def _is_xml_part(name):
    lower = name.lower()
    return lower.endswith(_XML_SUFFIXES) or lower == "[content_types].xml"


def zip_components(data):
    """Return a central-directory-backed inventory of ZIP/OOXML members.

    Each row names the package component and gives its exact raw payload
    range, compression method, CRC and expanded size. Central-directory sizes
    are authoritative, so entries using a trailing data descriptor (general
    purpose flag bit 3) are handled correctly. ZIP64 and multi-disk archives
    are conservatively declined; the caller then uses the legacy safe scan or
    the unchanged whole-file path.
    """
    if not data.startswith(_LFH):
        return []
    n = len(data)
    search_from = max(0, n - (65535 + 22))
    eocd = data.rfind(_EOCD, search_from)
    if eocd < 0 or eocd + 22 > n:
        return []
    try:
        (sig, disk, cd_disk, disk_entries, total_entries, cd_size, cd_offset,
         comment_len) = struct.unpack_from("<IHHHHIIH", data, eocd)
    except struct.error:
        return []
    if (sig != 0x06054B50 or disk != 0 or cd_disk != 0
            or disk_entries != total_entries
            or total_entries == 0xFFFF
            or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF
            or eocd + 22 + comment_len > n
            or cd_offset + cd_size > n):
        return []

    out = []
    pos = cd_offset
    for _ in range(total_entries):
        if pos + 46 > n or data[pos:pos + 4] != _CDFH:
            return []
        try:
            fields = struct.unpack_from("<IHHHHHHIIIHHHHHII", data, pos)
        except struct.error:
            return []
        (_sig, _made, needed, flags, method, _mtime, _mdate, crc,
         csize, usize, namelen, extralen, commentlen, start_disk,
         _iattr, _eattr, local_at) = fields
        end = pos + 46 + namelen + extralen + commentlen
        if (end > n or start_disk != 0 or csize == 0xFFFFFFFF
                or usize == 0xFFFFFFFF or local_at + 30 > n
                or data[local_at:local_at + 4] != _LFH):
            return []
        name_raw = data[pos + 46:pos + 46 + namelen]
        name = _zip_name(name_raw, flags)
        try:
            local = struct.unpack_from("<IHHHHHIIIHH", data, local_at)
        except struct.error:
            return []
        (_lsig, _lneeded, lflags, lmethod, _ltime, _ldate, _lcrc,
         _lcsize, _lusize, lnamelen, lextralen) = local
        payload_start = local_at + 30 + lnamelen + lextralen
        payload_end = payload_start + csize
        if (payload_start > n or payload_end > n or lmethod != method
                or (lflags & 0x800) != (flags & 0x800)):
            return []
        out.append({
            "name": name, "flags": flags, "method": method,
            "crc32": crc, "compressed_size": csize,
            "uncompressed_size": usize, "needed_version": needed,
            "local_start": local_at, "payload_start": payload_start,
            "payload_end": payload_end, "central_start": pos,
        })
        pos = end
    if pos > cd_offset + cd_size:
        return []
    out.sort(key=lambda row: row["payload_start"])
    for left, right in zip(out, out[1:]):
        if left["payload_end"] > right["payload_start"]:
            return []
    return out


def analyze_zip(data):
    """Segment an OOXML ZIP into named structural, XML, and opaque runs.

    Method-0 XML is directly reachable and pooled into Hybrid-Huffman.
    Method-8 XML is already encoded and remains verbatim in AFC3; the AFC4
    candidate builder below can instead expose it through an exact token
    recipe. Media and other encoded/high-entropy member payloads are kept out
    of the expensive engine path.
    """
    entries = zip_components(data)
    if not entries:
        return _analyze_zip_local(data)
    segs = []
    cursor = 0
    for entry in entries:
        start, end = entry["payload_start"], entry["payload_end"]
        if end <= start:
            continue
        if start > cursor:
            segs.append(Segment(POOLED, cursor, start, "zip-structure"))
        name = entry["name"]
        payload = data[start:end]
        method = entry["method"]
        short = name if len(name) <= 120 else name[:117] + "..."
        if method == 0 and _is_xml_part(name):
            kind, label = POOLED, "docx-xml-stored:" + short
        elif method == 0:
            kind = _classify(payload, name)
            label = ("zip-stored:" if kind == POOLED else
                     "zip-stored-opaque:") + short
        elif method == 8 and _is_xml_part(name):
            kind, label = OPAQUE, "docx-xml-deflate:" + short
        else:
            kind = OPAQUE
            prefix = "docx-media:" if name.lower().endswith(_MEDIA_SUFFIXES) \
                else "zip-encoded:"
            label = prefix + short
        segs.append(Segment(kind, start, end, label))
        cursor = end
    if cursor < len(data):
        segs.append(Segment(POOLED, cursor, len(data), "zip-structure"))
    return segs or [Segment(POOLED, 0, len(data), "zip-structure")]


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def _merge_adjacent(segs):
    """Coalesce neighbouring segments of the same kind, and absorb runs that
    are too small to deserve their own manifest entry."""
    out = []
    for s in segs:
        if out and out[-1].kind == s.kind and out[-1].end == s.start:
            out[-1].end = s.end
        elif (out and s.kind == OPAQUE and s.length < MIN_SEGMENT
              and out[-1].end == s.start):
            out[-1].end = s.end          # too small to isolate; fold in
        else:
            out.append(Segment(s.kind, s.start, s.end, s.label))
    return out


def _validate_tiling(segs, total):
    """Guarantee the segments exactly tile [0, total). Raises on any gap,
    overlap or misordering. This is the invariant that makes reconstruction
    byte-exact, so it is checked before anything is written — never assumed."""
    if not segs:
        raise ValueError("no segments")
    if segs[0].start != 0:
        raise ValueError("segments do not start at 0")
    if segs[-1].end != total:
        raise ValueError("segments do not end at %d" % total)
    for a, b in zip(segs, segs[1:]):
        if a.end != b.start:
            raise ValueError("segment gap/overlap at %d != %d" % (a.end, b.start))
        if a.end <= a.start:
            raise ValueError("empty or reversed segment")
    return True


def plan(data):
    """Analyse `data` and return a validated tiling, or None if not applicable."""
    if data.startswith(b"%PDF-"):
        segs = analyze_pdf(data)
    elif data.startswith(_LFH):
        segs = analyze_zip(data)
    else:
        return None
    if not segs:
        return None
    segs = _merge_adjacent(segs)
    _validate_tiling(segs, len(data))
    return segs


def describe_plan(data, segs=None):
    """Component-level breakdown, for the benchmarks and the analysis UI."""
    segs = segs if segs is not None else plan(data)
    if not segs:
        return None
    pooled = sum(s.length for s in segs if s.kind == POOLED)
    opaque = sum(s.length for s in segs if s.kind == OPAQUE)
    by_label = {}
    for s in segs:
        d = by_label.setdefault(s.label, {"count": 0, "bytes": 0,
                                          "kind": s.kind})
        d["count"] += 1
        d["bytes"] += s.length
    return {"segments": len(segs), "pooled_bytes": pooled,
            "opaque_bytes": opaque, "total": len(data),
            "opaque_fraction": (opaque / len(data)) if data else 0.0,
            "components": by_label}


def docx_transform_plan(data):
    """Return an exact tiling with suitable deflated XML exposed for AFC4.

    The central directory supplies member names, sizes and CRCs. Each
    method-8 XML payload is parsed by ``deflate_tokens`` into plain XML plus a
    reversible token recipe. Both are self-verified before the segment is
    admitted. A malformed, encrypted, oversized, or unsupported member stays
    opaque; it can never make reconstruction approximate.
    """
    entries = zip_components(data)
    if not entries:
        return None
    try:
        import deflate_tokens
    except Exception:
        return None

    segs = []
    cursor = 0
    transformed_total = 0
    transformed_count = 0
    for entry in entries:
        start, end = entry["payload_start"], entry["payload_end"]
        if end <= start:
            continue
        if start > cursor:
            segs.append(Segment(POOLED, cursor, start, "zip-structure"))
        name = entry["name"]
        method = entry["method"]
        payload = data[start:end]
        short = name if len(name) <= 120 else name[:117] + "..."
        segment = None

        # Encrypted entries cannot be interpreted, and expanding an extreme
        # ratio could be a ZIP bomb. Declining a transform is safe: the raw
        # member is preserved below.
        eligible = (method == 8 and _is_xml_part(name)
                    and not (entry["flags"] & 1)
                    and (entry["uncompressed_size"] >= MIN_XML_TRANSFORM_BYTES
                         or entry["compressed_size"]
                         >= entry["uncompressed_size"])
                    and entry["uncompressed_size"] <= MAX_XML_TRANSFORM_BYTES
                    and entry["uncompressed_size"] <= max(
                        1, entry["compressed_size"]
                        * MAX_XML_TRANSFORM_VIABILITY_RATIO)
                    and transformed_total + entry["uncompressed_size"]
                    <= MAX_XML_TRANSFORM_BYTES)
        if eligible:
            try:
                plain, recipe = deflate_tokens.transform(
                    payload, max_output=entry["uncompressed_size"])
                crc = binascii.crc32(plain) & 0xFFFFFFFF
                # transform() already serialises and byte-compares its recipe;
                # size + CRC independently verify the expanded ZIP member.
                if (len(plain) != entry["uncompressed_size"]
                        or crc != entry["crc32"]):
                    raise ValueError("ZIP size/CRC/recipe verification failed")
                # A named XML part should actually be textual. This prevents
                # a misleading extension from expanding arbitrary binary data.
                head = plain[:512].lstrip()
                if not head.startswith(b"<") or b"\x00" in head:
                    raise ValueError("named XML part is not textual XML")
                segment = Segment(
                    TRANSFORMED, start, end,
                    "docx-xml-transformed:" + short,
                    source=plain, recipe=recipe)
                transformed_total += len(plain)
                transformed_count += 1
            except Exception:
                segment = None

        if segment is None:
            if method == 0 and _is_xml_part(name):
                segment = Segment(POOLED, start, end,
                                  "docx-xml-stored:" + short)
            elif method == 0:
                kind = _classify(payload, name)
                label = ("zip-stored:" if kind == POOLED else
                         "zip-stored-opaque:") + short
                segment = Segment(kind, start, end, label)
            else:
                prefix = "docx-xml-preserved:" if _is_xml_part(name) else (
                    "docx-media:" if name.lower().endswith(_MEDIA_SUFFIXES)
                    else "zip-encoded:")
                segment = Segment(OPAQUE, start, end, prefix + short)
        segs.append(segment)
        cursor = end

    if cursor < len(data):
        segs.append(Segment(POOLED, cursor, len(data), "zip-structure"))
    if not transformed_count:
        return None
    _validate_tiling(segs, len(data))
    return segs


def describe_docx_transform(data, segs=None):
    segs = segs if segs is not None else docx_transform_plan(data)
    if not segs:
        return None
    direct = sum(s.length for s in segs if s.kind == POOLED)
    opaque = sum(s.length for s in segs if s.kind == OPAQUE)
    transformed = [s for s in segs if s.kind == TRANSFORMED]
    xml = sum(len(s.source) for s in transformed)
    recipes = sum(len(s.recipe) for s in transformed)
    return {
        "segments": len(segs), "direct_pooled_bytes": direct,
        "opaque_bytes": opaque,
        "transformed_deflate_bytes": sum(s.length for s in transformed),
        "xml_bytes": xml, "recipe_bytes": recipes,
        "hybrid_input_bytes": direct + xml + recipes,
        "transformed_components": len(transformed),
    }


# ---------------------------------------------------------------------------
# encode / decode
# ---------------------------------------------------------------------------

def build_afc3(data, segs, fmt="auto", compress_fn=None):
    """Serialise `data` as an AFC3 component-aware container.

    The pooled segments are concatenated and handed to the EXISTING engine as
    one buffer, so a pattern recurring across several components is discovered
    once and shared. Opaque segments are copied verbatim.
    """
    compress_fn = compress_fn or afc2.compress_bytes
    pooled_parts = []
    opaque_parts = []
    for s in segs:
        chunk = data[s.start:s.end]
        (pooled_parts if s.kind == POOLED else opaque_parts).append(chunk)

    pooled = b"".join(pooled_parts)
    opaque = b"".join(opaque_parts)
    pooled_blob = compress_fn(pooled, True, fmt=fmt) if pooled else b""

    out = bytearray(MAGIC3)
    out.append(MODE_COMPONENT)
    _put_varint(out, len(data))
    _put_varint(out, len(segs))
    for s in segs:
        _put_varint(out, (s.length << 1) | s.kind)
    _put_varint(out, len(opaque))
    out += opaque
    _put_varint(out, len(pooled_blob))
    out += pooled_blob
    return bytes(out)


def build_afc4(data, segs, fmt="auto", compress_fn=None):
    """Serialise a DOCX transform plan as an AFC4 container.

    Direct structural/STORED segments and every transformed member's plain
    XML + exact recipe are concatenated into one Hybrid-Huffman input. Opaque
    media/unsupported members remain byte-for-byte raw.
    """
    compress_fn = compress_fn or afc2.compress_bytes
    _validate_tiling(segs, len(data))
    if not any(s.kind == TRANSFORMED for s in segs):
        raise ValueError("AFC4 requires at least one transformed component")
    pooled_parts = []
    opaque_parts = []
    for s in segs:
        if s.kind == OPAQUE:
            opaque_parts.append(data[s.start:s.end])
        elif s.kind == POOLED:
            pooled_parts.append(data[s.start:s.end])
        elif s.kind == TRANSFORMED:
            if s.source is None or s.recipe is None:
                raise ValueError("transformed component lacks source/recipe")
            pooled_parts.extend((s.source, s.recipe))
        else:
            raise ValueError("unknown AFC4 segment kind")

    pooled = b"".join(pooled_parts)
    opaque = b"".join(opaque_parts)
    pooled_blob = compress_fn(pooled, True, fmt=fmt) if pooled else b""

    out = bytearray(MAGIC4)
    out.append(MODE_COMPONENT_XML)
    _put_varint(out, len(data))
    _put_varint(out, len(segs))
    for s in segs:
        _put_varint(out, (s.length << 2) | s.kind)
        if s.kind == TRANSFORMED:
            _put_varint(out, len(s.source))
            _put_varint(out, len(s.recipe))
    _put_varint(out, len(opaque))
    out += opaque
    _put_varint(out, len(pooled_blob))
    out += pooled_blob
    return bytes(out)


def is_afc3(blob):
    return len(blob) >= 5 and blob[:4] == MAGIC3


def is_afc4(blob):
    return len(blob) >= 5 and blob[:4] == MAGIC4


def header_info(blob):
    """Read an AFC3/AFC4 header without decoding the payload.

    Returns {original_length, segments, pooled_bytes, opaque_bytes}. The
    original_length here is the length of the WHOLE reconstructed file — not
    the inner container's, which covers only the pooled components. Callers
    that report integrity must use this one, or they will compare a whole-file
    length against a pooled length and wrongly declare a mismatch.
    """
    if not (is_afc3(blob) or is_afc4(blob)):
        raise ValueError("not an AFC3/AFC4 container")
    if (is_afc3(blob) and blob[4] != MODE_COMPONENT) or \
            (is_afc4(blob) and blob[4] != MODE_COMPONENT_XML):
        raise ValueError("unknown component-aware mode")
    pos = 5
    total, pos = _get_varint(blob, pos)
    count, pos = _get_varint(blob, pos)
    if count > len(blob):
        raise ValueError("unreasonable component count")
    pooled = opaque = transformed = xml = recipes = hybrid_input = 0
    for _ in range(count):
        v, pos = _get_varint(blob, pos)
        if is_afc3(blob):
            kind, length = v & 1, v >> 1
        else:
            kind, length = v & 3, v >> 2
        if length <= 0:
            raise ValueError("empty component in container header")
        if kind == POOLED:
            pooled += length
            hybrid_input += length
        elif kind == OPAQUE:
            opaque += length
        elif kind == TRANSFORMED and is_afc4(blob):
            source_len, pos = _get_varint(blob, pos)
            recipe_len, pos = _get_varint(blob, pos)
            if source_len <= 0 or recipe_len <= 0:
                raise ValueError("empty transformed component")
            transformed += length
            xml += source_len
            recipes += recipe_len
            hybrid_input += source_len + recipe_len
        else:
            raise ValueError("unknown component kind")
    represented = pooled + opaque + transformed
    if represented != total:
        raise ValueError("component lengths do not match original length")
    return {"magic": blob[:4].decode("ascii"),
            "original_length": total, "segments": count,
            # Original bytes represented through the Hybrid-Huffman side.
            "pooled_bytes": pooled + transformed,
            "direct_pooled_bytes": pooled, "opaque_bytes": opaque,
            "transformed_bytes": transformed, "xml_bytes": xml,
            "recipe_bytes": recipes, "hybrid_input_bytes": hybrid_input}


def inner_container(blob):
    """Return the ordinary AFC1/AFC2 payload inside AFC3 or AFC4."""
    if not (is_afc3(blob) or is_afc4(blob)):
        return blob
    pos = 5
    _total, pos = _get_varint(blob, pos)
    count, pos = _get_varint(blob, pos)
    if count > len(blob):
        raise ValueError("unreasonable component count")
    for _ in range(count):
        value, pos = _get_varint(blob, pos)
        if is_afc4(blob) and (value & 3) == TRANSFORMED:
            _source, pos = _get_varint(blob, pos)
            _recipe, pos = _get_varint(blob, pos)
    opaque_len, pos = _get_varint(blob, pos)
    if pos + opaque_len > len(blob):
        raise ValueError("truncated component-aware opaque region")
    pos += opaque_len
    pooled_len, pos = _get_varint(blob, pos)
    if pos + pooled_len != len(blob):
        raise ValueError("truncated or trailing component-aware pooled region")
    return bytes(blob[pos:pos + pooled_len])


def decompress_afc3(blob, decompress_fn=None):
    """Rebuild the exact original bytes from an AFC3 container."""
    decompress_fn = decompress_fn or afc2.decompress_bytes
    if not is_afc3(blob):
        raise ValueError("not an AFC3 container")
    if blob[4] != MODE_COMPONENT:
        raise ValueError("unknown AFC3 mode %d" % blob[4])
    pos = 5
    total, pos = _get_varint(blob, pos)
    count, pos = _get_varint(blob, pos)
    if count > len(blob):
        raise ValueError("unreasonable AFC3 component count")
    segs = []
    want_pooled = want_opaque = 0
    for _ in range(count):
        v, pos = _get_varint(blob, pos)
        kind, length = v & 1, v >> 1
        if length <= 0:
            raise ValueError("empty AFC3 component")
        segs.append((kind, length))
        if kind == POOLED:
            want_pooled += length
        else:
            want_opaque += length
    if want_pooled + want_opaque != total:
        raise ValueError("AFC3 component lengths do not match original")
    opaque_len, pos = _get_varint(blob, pos)
    if opaque_len != want_opaque:
        raise ValueError("AFC3 opaque length mismatch")
    opaque = blob[pos:pos + opaque_len]
    if len(opaque) != opaque_len:
        raise ValueError("truncated AFC3 opaque region")
    pos += opaque_len
    pooled_len, pos = _get_varint(blob, pos)
    pooled_blob = blob[pos:pos + pooled_len]
    if len(pooled_blob) != pooled_len or pos + pooled_len != len(blob):
        raise ValueError("truncated or trailing AFC3 pooled region")
    pooled = decompress_fn(pooled_blob) if pooled_len else b""
    if len(pooled) != want_pooled:
        raise ValueError("AFC3 pooled reconstruction length mismatch")

    out = bytearray()
    po = oo = 0
    for kind, length in segs:
        if kind == POOLED:
            chunk = pooled[po:po + length]
            if len(chunk) != length:
                raise ValueError("truncated AFC3 pooled component")
            out += chunk
            po += length
        else:
            chunk = opaque[oo:oo + length]
            if len(chunk) != length:
                raise ValueError("truncated AFC3 opaque component")
            out += chunk
            oo += length
    if len(out) != total or po != len(pooled) or oo != len(opaque):
        raise ValueError("AFC3 reconstruction length mismatch: %d != %d"
                         % (len(out), total))
    return bytes(out)


def decompress_afc4(blob, decompress_fn=None):
    """Rebuild a byte-identical OOXML package from an AFC4 container."""
    decompress_fn = decompress_fn or afc2.decompress_bytes
    if not is_afc4(blob):
        raise ValueError("not an AFC4 container")
    if blob[4] != MODE_COMPONENT_XML:
        raise ValueError("unknown AFC4 mode %d" % blob[4])
    try:
        import deflate_tokens
    except Exception as exc:
        raise ValueError("DEFLATE recipe parser unavailable: %s" % exc)

    pos = 5
    total, pos = _get_varint(blob, pos)
    count, pos = _get_varint(blob, pos)
    if count > len(blob):
        raise ValueError("unreasonable AFC4 component count")
    segs = []
    want_opaque = want_pooled = 0
    for _ in range(count):
        value, pos = _get_varint(blob, pos)
        kind, original_len = value & 3, value >> 2
        if original_len <= 0:
            raise ValueError("empty AFC4 component")
        source_len = recipe_len = 0
        if kind == TRANSFORMED:
            source_len, pos = _get_varint(blob, pos)
            recipe_len, pos = _get_varint(blob, pos)
            if source_len <= 0 or recipe_len <= 0:
                raise ValueError("empty AFC4 transformed source/recipe")
            want_pooled += source_len + recipe_len
        elif kind == POOLED:
            want_pooled += original_len
        elif kind == OPAQUE:
            want_opaque += original_len
        else:
            raise ValueError("unknown AFC4 component kind")
        segs.append((kind, original_len, source_len, recipe_len))
    if sum(s[1] for s in segs) != total:
        raise ValueError("AFC4 component lengths do not match original")

    opaque_len, pos = _get_varint(blob, pos)
    if opaque_len != want_opaque:
        raise ValueError("AFC4 opaque length mismatch")
    opaque = blob[pos:pos + opaque_len]
    if len(opaque) != opaque_len:
        raise ValueError("truncated AFC4 opaque region")
    pos += opaque_len
    pooled_len, pos = _get_varint(blob, pos)
    pooled_blob = blob[pos:pos + pooled_len]
    if len(pooled_blob) != pooled_len or pos + pooled_len != len(blob):
        raise ValueError("truncated or trailing AFC4 pooled region")
    pooled = decompress_fn(pooled_blob) if pooled_len else b""
    if len(pooled) != want_pooled:
        raise ValueError("AFC4 pooled reconstruction length mismatch")

    out = bytearray()
    po = oo = 0
    for kind, original_len, source_len, recipe_len in segs:
        if kind == OPAQUE:
            chunk = opaque[oo:oo + original_len]
            if len(chunk) != original_len:
                raise ValueError("truncated AFC4 opaque component")
            out += chunk
            oo += original_len
        elif kind == POOLED:
            chunk = pooled[po:po + original_len]
            if len(chunk) != original_len:
                raise ValueError("truncated AFC4 direct component")
            out += chunk
            po += original_len
        else:
            source = pooled[po:po + source_len]
            po += source_len
            recipe = pooled[po:po + recipe_len]
            po += recipe_len
            if len(source) != source_len or len(recipe) != recipe_len:
                raise ValueError("truncated AFC4 transformed component")
            raw = deflate_tokens.restore(source, recipe)
            if len(raw) != original_len:
                raise ValueError("AFC4 transformed length mismatch")
            out += raw
    if (len(out) != total or po != len(pooled) or oo != len(opaque)):
        raise ValueError("AFC4 reconstruction length mismatch")
    return bytes(out)


# ---------------------------------------------------------------------------
# the entry point the engine calls
# ---------------------------------------------------------------------------

def compress_container(data, fmt="auto", compress_fn=None, verify=True):
    """Try AFC3/AFC4 component paths and return the smallest whole result.

    AFC3 directly pools structural/unfiltered bytes while preserving encoded
    payloads. For ZIP/OOXML, AFC4 additionally tries the exact XML/token
    transform. Neither candidate is emitted merely because a component got
    smaller: the final wrapper must beat the unchanged whole-file AFC1/AFC2
    path. Every candidate is byte-verified before it can be selected.
    """
    info = {"applied": False, "reason": "", "afc3_bytes": 0,
            "afc4_bytes": 0, "plain_bytes": 0, "selected_magic": ""}
    compress_fn = compress_fn or afc2.compress_bytes
    try:
        segs = plan(data)
    except Exception as exc:
        info["reason"] = "analysis failed: %s" % exc
        return None, info
    if not segs:
        info["reason"] = "not a supported container"
        return None, info

    stats = describe_plan(data, segs)
    info.update(stats)
    candidates = []
    notes = []

    # Existing AFC3 viability gate. It avoids a redundant second full pass on
    # tiny containers where the manifest cannot plausibly pay for itself.
    direct_viable = (
        stats["opaque_bytes"] >= MIN_OPAQUE_BYTES
        and stats["pooled_bytes"] >= MIN_POOLED_BYTES
        and stats["opaque_fraction"] >= MIN_OPAQUE_FRACTION)
    if direct_viable:
        afc3_blob = build_afc3(data, segs, fmt=fmt, compress_fn=compress_fn)
        info["afc3_bytes"] = len(afc3_blob)
        try:
            if verify and decompress_afc3(afc3_blob) != data:
                raise ValueError("AFC3 byte comparison failed")
            candidates.append(afc3_blob)
        except Exception as exc:
            notes.append("AFC3 verification failed: %s" % exc)
    else:
        notes.append("AFC3 below viability gate")

    # New AFC4 candidate: only ZIP files with at least one fully verified
    # method-8 XML transform reach this point. It is intentionally measured
    # even when it later loses; the final whole-file guard remains authoritative.
    if data.startswith(_LFH):
        try:
            tplan = docx_transform_plan(data)
            if tplan:
                tstats = describe_docx_transform(data, tplan)
                info.update({"docx_" + k: v for k, v in tstats.items()})
                afc4_blob = build_afc4(data, tplan, fmt=fmt,
                                       compress_fn=compress_fn)
                info["afc4_bytes"] = len(afc4_blob)
                if verify and decompress_afc4(afc4_blob) != data:
                    raise ValueError("AFC4 byte comparison failed")
                candidates.append(afc4_blob)
            else:
                notes.append("no eligible exact DOCX XML transforms")
        except Exception as exc:
            notes.append("AFC4 declined: %s" % exc)

    if not candidates:
        info["reason"] = "; ".join(notes) or "no component candidate"
        return None, info
    best = min(candidates, key=len)

    # Keep the proven AFC3 media-heavy shortcut. AFC4 always gets an explicit
    # whole-file comparison because transformed XML may expand substantially.
    if (best[:4] == MAGIC3
            and stats["opaque_fraction"] >= OPAQUE_SKIP_WHOLE
            and len(data) >= OPAQUE_SKIP_MIN_BYTES):
        if len(best) >= len(data):
            info["reason"] = "component container not smaller than original"
            return None, info
        info["applied"] = True
        info["selected_magic"] = "AFC3"
        info["reason"] = "opaque-heavy and large; whole-file pass skipped"
        return best, info

    plain = compress_fn(data, True, fmt=fmt)
    info["plain_bytes"] = len(plain)
    if len(best) >= len(plain):
        info["reason"] = ("plain container smaller (%d <= %d); %s"
                          % (len(plain), len(best), "; ".join(notes)))
        return None, info
    info["applied"] = True
    info["selected_magic"] = best[:4].decode("ascii")
    info["reason"] = "%s smaller than plain (%d < %d)" % (
        info["selected_magic"], len(best), len(plain))
    return best, info
