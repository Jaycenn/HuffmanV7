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
* No DEFLATE / zlib / ZIP / LZ / arithmetic coding anywhere. This module
  imports no compression library at all (asserted by an AST test, exactly as
  for afcpak.py — see SCOPE_NOTES.md §2).
* No inflating and re-deflating of ZIP members. Re-deflating cannot be relied
  on to reproduce the original bytes, and doing it would introduce DEFLATE as
  a second stage. Already-deflated member payloads are therefore treated as
  opaque and preserved verbatim.
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

THE AFC3 CONTAINER
------------------
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
"""
import re
import struct

import afc2
import afc

MAGIC3 = b"AFC3"
MODE_COMPONENT = 3

# Segment kinds
OPAQUE = 0      # stored verbatim: already compressed or high entropy
POOLED = 1      # concatenated into the single Hybrid-Huffman input

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


class Segment:
    __slots__ = ("kind", "start", "end", "label")

    def __init__(self, kind, start, end, label=""):
        self.kind = kind
        self.start = start
        self.end = end
        self.label = label

    @property
    def length(self):
        return self.end - self.start

    def __repr__(self):
        return "Segment(%s, %d..%d, %r)" % (
            "OPAQUE" if self.kind == OPAQUE else "POOLED",
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


def analyze_zip(data):
    """Segment a ZIP package (DOCX/XLSX/PPTX/ODF) into pooled and opaque runs."""
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


def is_afc3(blob):
    return len(blob) >= 5 and blob[:4] == MAGIC3


def header_info(blob):
    """Read an AFC3 header without decoding the payload.

    Returns {original_length, segments, pooled_bytes, opaque_bytes}. The
    original_length here is the length of the WHOLE reconstructed file — not
    the inner container's, which covers only the pooled components. Callers
    that report integrity must use this one, or they will compare a whole-file
    length against a pooled length and wrongly declare a mismatch.
    """
    if not is_afc3(blob):
        raise ValueError("not an AFC3 container")
    pos = 5
    total, pos = _get_varint(blob, pos)
    count, pos = _get_varint(blob, pos)
    pooled = opaque = 0
    for _ in range(count):
        v, pos = _get_varint(blob, pos)
        kind, length = v & 1, v >> 1
        if kind == POOLED:
            pooled += length
        else:
            opaque += length
    return {"original_length": total, "segments": count,
            "pooled_bytes": pooled, "opaque_bytes": opaque}


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
    segs = []
    for _ in range(count):
        v, pos = _get_varint(blob, pos)
        segs.append((v & 1, v >> 1))
    opaque_len, pos = _get_varint(blob, pos)
    opaque = blob[pos:pos + opaque_len]
    if len(opaque) != opaque_len:
        raise ValueError("truncated AFC3 opaque region")
    pos += opaque_len
    pooled_len, pos = _get_varint(blob, pos)
    pooled_blob = blob[pos:pos + pooled_len]
    if len(pooled_blob) != pooled_len:
        raise ValueError("truncated AFC3 pooled region")
    pooled = decompress_fn(pooled_blob) if pooled_len else b""

    out = bytearray()
    po = oo = 0
    for kind, length in segs:
        if kind == POOLED:
            out += pooled[po:po + length]
            po += length
        else:
            out += opaque[oo:oo + length]
            oo += length
    if len(out) != total:
        raise ValueError("AFC3 reconstruction length mismatch: %d != %d"
                         % (len(out), total))
    return bytes(out)


# ---------------------------------------------------------------------------
# the entry point the engine calls
# ---------------------------------------------------------------------------

def compress_container(data, fmt="auto", compress_fn=None, verify=True):
    """Try container-aware compression. Returns (blob, info) or (None, info).

    Returns None when the file is not a supported container, when the analysis
    finds nothing worth separating, or when the resulting AFC3 is not smaller
    than the plain whole-file container (§27: the global size guard — we never
    claim a win because one component shrank).

    `verify` re-expands the produced container and compares it against the
    input before returning it. Losslessness is proven per file, not assumed.
    """
    info = {"applied": False, "reason": "", "afc3_bytes": 0, "plain_bytes": 0}
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
    if stats["opaque_bytes"] == 0:
        # Nothing to exclude: the plain whole-file path does exactly the same
        # work without a manifest, so it wins by definition.
        info["reason"] = "no opaque components; plain path is equivalent"
        return None, info

    # Cheap viability gate, before any compression happens. Attempting the
    # container path costs a second compression pass, so it is only worth
    # starting when there is enough opaque data to exclude and enough pooled
    # data to compress.
    if (stats["opaque_bytes"] < MIN_OPAQUE_BYTES
            or stats["pooled_bytes"] < MIN_POOLED_BYTES
            or stats["opaque_fraction"] < MIN_OPAQUE_FRACTION):
        info["reason"] = ("below the viability gate (opaque %d B, pooled %d B, "
                          "%.0f%% opaque)" % (stats["opaque_bytes"],
                                              stats["pooled_bytes"],
                                              100 * stats["opaque_fraction"]))
        return None, info

    blob = build_afc3(data, segs, fmt=fmt, compress_fn=compress_fn)
    info["afc3_bytes"] = len(blob)

    if verify:
        try:
            if decompress_afc3(blob) != data:
                info["reason"] = "verification failed; falling back"
                return None, info
        except Exception as exc:
            info["reason"] = "verification error: %s" % exc
            return None, info

    # Global size guard (§27). We do NOT claim a win because one component got
    # smaller: the whole produced container, manifest included, must beat the
    # plain whole-file container or we hand back None and the engine takes its
    # normal path. The comparison is only skipped for large, overwhelmingly
    # opaque files, where the plain pass would burn time on data already
    # proven incompressible and AFC3 is not in doubt (see OPAQUE_SKIP_WHOLE).
    if (stats["opaque_fraction"] >= OPAQUE_SKIP_WHOLE
            and len(data) >= OPAQUE_SKIP_MIN_BYTES):
        info["plain_bytes"] = 0
        if len(blob) >= len(data):
            info["reason"] = "AFC3 not smaller than the original"
            return None, info
        info["applied"] = True
        info["reason"] = "opaque-heavy and large; whole-file pass skipped"
        return blob, info

    plain = compress_fn(data, True, fmt=fmt)
    info["plain_bytes"] = len(plain)
    if len(blob) >= len(plain):
        info["reason"] = ("plain container smaller (%d <= %d)"
                          % (len(plain), len(blob)))
        return None, info
    info["applied"] = True
    info["reason"] = "AFC3 smaller than plain (%d < %d)" % (len(blob),
                                                            len(plain))
    return blob, info
