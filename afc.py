#!/usr/bin/env python3
"""
afc.py — AFC v1 core: container I/O, canonical hybrid Huffman, baseline
single-tier engine, and the universal decoder (AFC1 legacy + AFC2 compact).

This file is the pure-Python reference implementation.  Everything here is
integer-only and fully deterministic (same input -> same bytes on every
platform / implementation: Python, C++, JavaScript).

Container formats
-----------------
AFC1 (legacy, byte-compatible with the original v3 tool chain):
    "AFC1" | mode u8 | varint orig_len
    mode 0 : raw stored bytes follow (fallback, always lossless)
    mode>0 : varint dict_count
             per entry: varint len | len raw bytes
             varint used_symbol_count
             per symbol: varint id | u8 code_length
             canonical-Huffman bitstream (MSB-first)

AFC2 (compact header, v4):
    "AFC2" | mode u8 | varint orig_len
    mode 0 : raw stored bytes follow
    mode>0 : varint used_symbol_count U
             varint id[0], then U-1 varints of (id[k]-id[k-1]-1)   (ids ascending)
             ceil(U/2) bytes of 4-bit packed (code_length-1), first symbol in
             the high nibble (lengths are limited to 16 by package-merge)
             varint dict_count D
             if D>0: u8 flag (0 = raw entries, 1 = entries coded with the
                     literal Huffman codes)
                     D varints: pattern lengths
                     flag 0: concatenated raw pattern bytes
                     flag 1: one MSB-first bitstream of all pattern bytes,
                             each byte written with its literal Huffman code,
                             zero-padded to a byte boundary
             canonical-Huffman bitstream (MSB-first, same packing as AFC1)

Canonical code assignment (both formats): symbols sorted by
(code_length asc, symbol_id asc); codes assigned sequentially, shifting left
by the length difference when the length increases.

Symbol space: 0..255 literal bytes, 256+i dictionary pattern i.
"""

import hashlib
import sys
from array import array

MAGIC1 = b"AFC1"
MAGIC2 = b"AFC2"
MODE_RAW = 0
MODE_BASELINE = 1
MODE_ADAPTIVE = 2

MAX_CODE_LEN = 16          # package-merge limit (fits the AFC2 4-bit nibble)
NOCODE_COST = 18           # DP cost for a symbol that has no code yet


# --------------------------------------------------------------------------
# varints (LSB base-128, continuation bit 0x80 — identical to the native rv())
# --------------------------------------------------------------------------

def write_varint(out: bytearray, v: int) -> None:
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def read_varint(buf, pos: int):
    v = 0
    sh = 0
    while True:
        x = buf[pos]
        pos += 1
        v |= (x & 0x7F) << sh
        if not (x & 0x80):
            return v, pos
        sh += 7


# --------------------------------------------------------------------------
# integer bit-cost estimate used by the Bit Cost Decision Engine.
# est_code_len(freq,total) ~= ceil(-log2(freq/total)) computed with integers
# only, so Python / C++ / JS agree bit-for-bit.
# --------------------------------------------------------------------------

def est_code_len(freq: int, total: int) -> int:
    if freq <= 0:
        return NOCODE_COST
    q = total // freq
    if q < 1:
        q = 1
    L = q.bit_length()
    return 1 if L < 1 else (24 if L > 24 else L)


# --------------------------------------------------------------------------
# Huffman code lengths
# --------------------------------------------------------------------------

def huffman_lengths(freqs: dict) -> dict:
    """Unlimited-depth Huffman lengths via the deterministic two-queue method.
    freqs: {symbol_id: count>0}.  Ties: leaves before internal nodes, lower
    symbol id first.  Returns {symbol_id: length}."""
    items = sorted(freqs.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 1}
    # nodes: (freq, node_index); leaves 0..n-1, merges n..2n-2
    parent = [0] * (2 * n - 1)
    leafq = list(range(n))          # ascending freq order
    mergq = []
    li = mi = 0
    next_node = n
    freq_of = [kv[1] for kv in items] + [0] * (n - 1)

    def take():
        nonlocal li, mi
        lf = freq_of[leafq[li]] if li < len(leafq) else None
        mf = freq_of[mergq[mi]] if mi < len(mergq) else None
        if mf is None or (lf is not None and lf <= mf):
            li += 1
            return leafq[li - 1]
        mi += 1
        return mergq[mi - 1]

    for _ in range(n - 1):
        a = take()
        b = take()
        freq_of[next_node] = freq_of[a] + freq_of[b]
        parent[a] = next_node
        parent[b] = next_node
        mergq.append(next_node)
        next_node += 1

    root = next_node - 1
    depth = [0] * (2 * n - 1)
    for node in range(root - 1, -1, -1):
        depth[node] = depth[parent[node]] + 1
    return {items[k][0]: max(1, depth[k]) for k in range(n)}


def package_merge_lengths(freqs: dict, limit: int = MAX_CODE_LEN) -> dict:
    """Optimal length-limited Huffman lengths (package-merge), deterministic.
    freqs: {symbol_id: count>0}.

    Portability contract (mirrored bit-for-bit in afc_native.cpp and
    afc_engine.js): leaves are sorted by (freq asc, id asc); at every level
    the leaf list is two-pointer merged with the pairwise packages of the
    previous level, and on equal weight the LEAF comes first."""
    items = sorted(freqs.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 1}
    if n > (1 << limit):
        raise ValueError("alphabet larger than 2^limit")
    leaf_w = [kv[1] for kv in items]
    # each element: (weight, leaves) where leaves is a list of leaf indices
    prev_w = []
    prev_l = []
    for _ in range(limit):
        pack_w = []
        pack_l = []
        for i in range(0, len(prev_w) - 1, 2):
            pack_w.append(prev_w[i] + prev_w[i + 1])
            pack_l.append(prev_l[i] + prev_l[i + 1])
        cur_w = []
        cur_l = []
        li = pi = 0
        while li < n or pi < len(pack_w):
            if pi >= len(pack_w) or (li < n and leaf_w[li] <= pack_w[pi]):
                cur_w.append(leaf_w[li])
                cur_l.append([li])
                li += 1
            else:
                cur_w.append(pack_w[pi])
                cur_l.append(pack_l[pi])
                pi += 1
        prev_w, prev_l = cur_w, cur_l
    lengths = [0] * n
    for leaves in prev_l[:2 * n - 2]:
        for k in leaves:
            lengths[k] += 1
    return {items[k][0]: max(1, lengths[k]) for k in range(n)}


def canonical_codes(lengths: dict) -> dict:
    """{id: length} -> {id: (code,length)} with (length asc, id asc) order."""
    syms = sorted(lengths.items(), key=lambda kv: (kv[1], kv[0]))
    out = {}
    code = 0
    prev = syms[0][1] if syms else 1
    for sid, L in syms:
        code <<= (L - prev)
        prev = L
        out[sid] = (code, L)
        code += 1
    return out


# --------------------------------------------------------------------------
# bitstream writer (MSB-first, identical packing to the native pack_bits)
# --------------------------------------------------------------------------

class BitWriter:
    __slots__ = ("buf", "acc", "nb")

    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.nb = 0

    def put(self, code: int, length: int) -> None:
        self.acc = (self.acc << length) | code
        self.nb += length
        while self.nb >= 8:
            self.nb -= 8
            self.buf.append((self.acc >> self.nb) & 0xFF)
            self.acc &= (1 << self.nb) - 1

    def finish(self) -> bytearray:
        if self.nb:
            self.buf.append((self.acc << (8 - self.nb)) & 0xFF)
            self.acc = 0
            self.nb = 0
        return self.buf


def pack_ids(ids, codes: dict) -> bytes:
    bw = BitWriter()
    put = bw.put
    for sid in ids:
        c, L = codes[sid]
        put(c, L)
    return bytes(bw.finish())


# --------------------------------------------------------------------------
# container emit
# --------------------------------------------------------------------------

def emit_afc1(mode: int, orig_len: int, patterns, lengths: dict,
              bitstream: bytes) -> bytes:
    out = bytearray(MAGIC1)
    out.append(mode)
    write_varint(out, orig_len)
    write_varint(out, len(patterns))
    for p in patterns:
        write_varint(out, len(p))
        out += p
    write_varint(out, len(lengths))
    for sid in sorted(lengths):
        write_varint(out, sid)
        out.append(lengths[sid])
    out += bitstream
    return bytes(out)


def emit_afc2(mode: int, orig_len: int, patterns, lengths: dict,
              bitstream: bytes) -> bytes:
    out = bytearray(MAGIC2)
    out.append(mode)
    write_varint(out, orig_len)
    ids = sorted(lengths)
    write_varint(out, len(ids))
    prev = -1
    for sid in ids:
        write_varint(out, sid if prev < 0 else sid - prev - 1)
        prev = sid
    # 4-bit packed lengths (L-1), first symbol in the high nibble
    nib = bytearray((len(ids) + 1) // 2)
    for k, sid in enumerate(ids):
        L = lengths[sid]
        if not (1 <= L <= 16):
            raise ValueError("AFC2 requires code lengths 1..16")
        v = L - 1
        if k & 1:
            nib[k >> 1] |= v
        else:
            nib[k >> 1] = v << 4
    out += nib
    write_varint(out, len(patterns))
    if patterns:
        codes = canonical_codes(lengths)
        can = all((b in codes) for p in patterns for b in p)
        blob = None
        if can:
            bw = BitWriter()
            for p in patterns:
                for b in p:
                    c, L = codes[b]
                    bw.put(c, L)
            blob = bytes(bw.finish())
        rawlen = sum(len(p) for p in patterns)
        if blob is not None and len(blob) < rawlen:
            out.append(1)
            for p in patterns:
                write_varint(out, len(p))
            out += blob
        else:
            out.append(0)
            for p in patterns:
                write_varint(out, len(p))
            for p in patterns:
                out += p
    out += bitstream
    return bytes(out)


def emit_raw(orig: bytes, magic: bytes = MAGIC1) -> bytes:
    out = bytearray(magic)
    out.append(MODE_RAW)
    write_varint(out, len(orig))
    out += orig
    return bytes(out)


def finish_container(mode: int, data: bytes, patterns, lengths, bitstream,
                     fmt: str = "auto") -> bytes:
    """Build the requested container; 'auto' keeps AFC1 unless AFC2 is
    strictly smaller.  Falls back to raw storage when coding does not pay."""
    if fmt == "afc1":
        blob = emit_afc1(mode, len(data), patterns, lengths, bitstream)
    elif fmt == "afc2":
        blob = emit_afc2(mode, len(data), patterns, lengths, bitstream)
    else:
        blob = emit_afc1(mode, len(data), patterns, lengths, bitstream)
        if all(1 <= L <= 16 for L in lengths.values()):
            b2 = emit_afc2(mode, len(data), patterns, lengths, bitstream)
            if len(b2) < len(blob):
                blob = b2
    raw = emit_raw(data, MAGIC2 if fmt == "afc2" else MAGIC1)
    return raw if len(raw) <= len(blob) else blob


# --------------------------------------------------------------------------
# decoder (both containers, LUT-accelerated)
# --------------------------------------------------------------------------

def _build_decode_lut(lengths: dict, patterns):
    """Single-level LUT decode table for maxlen<=16: index = next maxlen bits,
    value = (advance_bits, expansion_bytes or literal int)."""
    maxlen = max(lengths.values())
    codes = canonical_codes(lengths)
    size = 1 << maxlen
    sym_of = array("i", [-1]) * 1
    sym_of = array("i", bytes(4 * size))
    adv_of = bytearray(size)
    for sid, (c, L) in codes.items():
        base = c << (maxlen - L)
        for k in range(1 << (maxlen - L)):
            sym_of[base + k] = sid
            adv_of[base + k] = L
    return maxlen, sym_of, adv_of


def _decode_bitstream(blob, pos, orig, patterns, lengths):
    out = bytearray()
    maxlen = max(lengths.values()) if lengths else 0
    if 0 < maxlen <= 16:
        ml, sym_of, adv_of = _build_decode_lut(lengths, patterns)
        acc = 0
        nb = 0
        n = len(blob)
        mask = (1 << ml) - 1
        while len(out) < orig:
            while nb < ml and pos < n:
                acc = (acc << 8) | blob[pos]
                pos += 1
                nb += 8
            if nb >= ml:
                idx = (acc >> (nb - ml)) & mask
            else:
                idx = (acc << (ml - nb)) & mask
            sid = sym_of[idx]
            L = adv_of[idx]
            if L == 0 or sid < 0 or L > nb + (8 * (n - pos)):
                raise ValueError("corrupt bitstream")
            if nb >= L:
                nb -= L
                acc &= (1 << nb) - 1
            else:
                raise ValueError("corrupt bitstream")
            if sid < 256:
                out.append(sid)
            else:
                p = patterns[sid - 256]
                room = orig - len(out)
                out += p if len(p) <= room else p[:room]
        return bytes(out)
    # legacy long-code path: firstcode/firstidx walk (like the native decoder)
    syms = sorted(lengths.items(), key=lambda kv: (kv[1], kv[0]))
    firstcode = {}
    firstidx = {}
    cnt = {}
    order = []
    code = 0
    prev = syms[0][1]
    for k, (sid, L) in enumerate(syms):
        code <<= (L - prev)
        prev = L
        if L not in cnt:
            firstcode[L] = code
            firstidx[L] = k
            cnt[L] = 0
        cnt[L] += 1
        order.append(sid)
        code += 1
    code = 0
    L = 0
    for i in range(pos, len(blob)):
        byte = blob[i]
        for sh in range(7, -1, -1):
            code = (code << 1) | ((byte >> sh) & 1)
            L += 1
            if L in cnt and firstcode[L] <= code < firstcode[L] + cnt[L]:
                sid = order[firstidx[L] + code - firstcode[L]]
                if sid < 256:
                    out.append(sid)
                else:
                    p = patterns[sid - 256]
                    room = orig - len(out)
                    out += p if len(p) <= room else p[:room]
                code = 0
                L = 0
                if len(out) >= orig:
                    return bytes(out)
    if len(out) != orig:
        raise ValueError("truncated bitstream")
    return bytes(out)


def decompress_bytes(blob: bytes) -> bytes:
    if len(blob) < 6 or blob[:4] not in (MAGIC1, MAGIC2):
        raise ValueError("not a valid AFC container")
    magic = blob[:4]
    mode = blob[4]
    pos = 5
    orig, pos = read_varint(blob, pos)
    if mode == MODE_RAW:
        if pos + orig > len(blob):
            raise ValueError("truncated raw container")
        return bytes(blob[pos:pos + orig])
    if orig == 0:
        return b""
    if magic == MAGIC1:
        dc, pos = read_varint(blob, pos)
        patterns = []
        for _ in range(dc):
            L, pos = read_varint(blob, pos)
            patterns.append(bytes(blob[pos:pos + L]))
            pos += L
        used, pos = read_varint(blob, pos)
        lengths = {}
        for _ in range(used):
            sid, pos = read_varint(blob, pos)
            lengths[sid] = blob[pos]
            pos += 1
        return _decode_bitstream(blob, pos, orig, patterns, lengths)
    # AFC2
    used, pos = read_varint(blob, pos)
    ids = []
    prev = -1
    for _ in range(used):
        d, pos = read_varint(blob, pos)
        sid = d if prev < 0 else prev + 1 + d
        ids.append(sid)
        prev = sid
    lengths = {}
    nbytes = (used + 1) // 2
    for k, sid in enumerate(ids):
        b = blob[pos + (k >> 1)]
        v = (b >> 4) if not (k & 1) else (b & 0x0F)
        lengths[sid] = v + 1
    pos += nbytes
    dc, pos = read_varint(blob, pos)
    patterns = []
    if dc:
        flag = blob[pos]
        pos += 1
        plens = []
        for _ in range(dc):
            L, pos = read_varint(blob, pos)
            plens.append(L)
        if flag == 0:
            for L in plens:
                patterns.append(bytes(blob[pos:pos + L]))
                pos += L
        else:
            # dictionary bytes are coded with the literal codes of the FULL
            # canonical table (same code space as the main bitstream)
            total = sum(plens)
            raw = _decode_bitstream(blob, pos, total, [], lengths)
            codes = canonical_codes(lengths)
            bits = sum(codes[b][1] for b in raw)
            pos += (bits + 7) // 8
            off = 0
            for L in plens:
                patterns.append(raw[off:off + L])
                off += L
    return _decode_bitstream(blob, pos, orig, patterns, lengths)


# --------------------------------------------------------------------------
# v1 baseline engine: single-tier byte-frequency canonical Huffman (the
# dashboard control row) — Tier-1 only, no dictionary.
# --------------------------------------------------------------------------

def _baseline_lengths(data: bytes) -> dict:
    freqs = {}
    for b in data:
        freqs[b] = freqs.get(b, 0) + 1
    return package_merge_lengths(freqs, MAX_CODE_LEN)


def compress_bytes(data: bytes, adaptive: bool = False,
                   fmt: str = "afc1") -> bytes:
    """v1 API.  adaptive=True delegates to the v3/v4 engine in afc2.py."""
    if adaptive:
        import afc2
        return afc2.compress_bytes(data, True, fmt=fmt)
    if len(data) == 0:
        return emit_raw(data, MAGIC2 if fmt == "afc2" else MAGIC1)
    lengths = _baseline_lengths(data)
    codes = canonical_codes(lengths)
    bitstream = pack_ids(data, codes)
    return finish_container(MODE_BASELINE, data, [], lengths, bitstream, fmt)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="AFC v1 baseline engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("compress", "decompress", "verify"):
        s = sub.add_parser(c)
        s.add_argument("infile")
        if c != "verify":
            s.add_argument("outfile")
        if c == "compress":
            s.add_argument("--format", choices=("afc1", "afc2", "auto"),
                           default="afc1")
    a = ap.parse_args()
    data = open(a.infile, "rb").read()
    if a.cmd == "compress":
        blob = compress_bytes(data, False, fmt=a.format)
        open(a.outfile, "wb").write(blob)
        print(f"{len(data)} -> {len(blob)} bytes")
    elif a.cmd == "decompress":
        out = decompress_bytes(data)
        open(a.outfile, "wb").write(out)
        print(f"{len(data)} -> {len(out)} bytes")
    else:
        blob = compress_bytes(data, False)
        ok = hashlib.sha256(decompress_bytes(blob)).hexdigest() == \
            hashlib.sha256(data).hexdigest()
        print("LOSSLESS OK" if ok else "ROUND-TRIP FAILED")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _cli()
