#!/usr/bin/env python3
"""Reversible parser for raw DEFLATE member streams used by DOCX/OOXML.

This module is deliberately *not* a compressor.  It never searches for a
match, chooses a block type, builds a Huffman tree, or emits a newly optimised
DEFLATE stream.  It only:

1. reads the block headers and tokens that are already present in a ZIP
   member;
2. expands those tokens to the original member bytes (normally XML); and
3. records a compact recipe containing the producer's exact token choices.

``restore`` combines those member bytes with the recipe and serialises the
same headers/tokens, reproducing the original raw DEFLATE bytes bit-for-bit.
The caller can therefore feed the repetitive XML + recipe to the existing
Hybrid-Huffman engine while retaining byte-exact reconstruction of the
original DOCX package.

No zlib/zipfile/gzip/bz2/lzma implementation is imported.  DEFLATE is parsed
as an input container syntax; Hybrid-Huffman remains the only compressor that
stores an AFC payload.
"""

MAGIC = b"DFR1"

_LENGTH_BASE = (
    3, 4, 5, 6, 7, 8, 9, 10,
    11, 13, 15, 17, 19, 23, 27, 31,
    35, 43, 51, 59, 67, 83, 99, 115,
    131, 163, 195, 227, 258,
)
_LENGTH_EXTRA = (
    0, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 1, 2, 2, 2, 2,
    3, 3, 3, 3, 4, 4, 4, 4,
    5, 5, 5, 5, 0,
)
_DIST_BASE = (
    1, 2, 3, 4, 5, 7, 9, 13,
    17, 25, 33, 49, 65, 97, 129, 193,
    257, 385, 513, 769, 1025, 1537, 2049, 3073,
    4097, 6145, 8193, 12289, 16385, 24577,
)
_DIST_EXTRA = (
    0, 0, 0, 0, 1, 1, 2, 2,
    3, 3, 4, 4, 5, 5, 6, 6,
    7, 7, 8, 8, 9, 9, 10, 10,
    11, 11, 12, 12, 13, 13,
)
_CL_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)

_REC_LITERALS = 0
_REC_MATCH = 1
_REC_END = 2
_REC_STORED = 3


class _BitReader:
    __slots__ = ("data", "pos", "limit")

    def __init__(self, data, pos=0, limit=None):
        self.data = data
        self.pos = pos
        self.limit = len(data) * 8 if limit is None else limit

    def read(self, count):
        """Read a DEFLATE integer field (least-significant bit first)."""
        if count < 0 or self.pos + count > self.limit:
            raise ValueError("truncated DEFLATE bitstream")
        value = 0
        for shift in range(count):
            value |= ((self.data[self.pos >> 3] >> (self.pos & 7)) & 1) << shift
            self.pos += 1
        return value

    def read_code(self, table, max_bits):
        """Read a Huffman code (code bits are transmitted MSB first)."""
        code = 0
        for length in range(1, max_bits + 1):
            code = (code << 1) | self.read(1)
            symbol = table.get((length, code))
            if symbol is not None:
                return symbol
        raise ValueError("invalid DEFLATE Huffman code")

    def align(self):
        skip = (-self.pos) & 7
        if skip:
            self.read(skip)


class _BitWriter:
    __slots__ = ("buf", "pos")

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def put_bit(self, bit):
        if (self.pos & 7) == 0:
            self.buf.append(0)
        if bit:
            self.buf[-1] |= 1 << (self.pos & 7)
        self.pos += 1

    def put(self, value, count):
        """Write a DEFLATE integer field (least-significant bit first)."""
        for shift in range(count):
            self.put_bit((value >> shift) & 1)

    def put_code(self, code, length):
        for shift in range(length - 1, -1, -1):
            self.put_bit((code >> shift) & 1)

    def put_blob(self, blob, bit_count):
        if bit_count > len(blob) * 8:
            raise ValueError("truncated recipe bit blob")
        for bitpos in range(bit_count):
            self.put_bit((blob[bitpos >> 3] >> (bitpos & 7)) & 1)

    def put_bytes(self, data):
        """Fast path for the byte-aligned payload of a stored block."""
        if self.pos & 7:
            for byte in data:
                self.put(byte, 8)
            return
        self.buf += data
        self.pos += len(data) * 8

    def finish(self):
        return bytes(self.buf)


def _put_varint(out, value):
    if value < 0:
        raise ValueError("negative recipe integer")
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return


def _get_varint(data, pos):
    value = 0
    shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("truncated or oversized recipe varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7


def _canonical(lengths):
    """Return decode table, encode table and maximum code length."""
    max_bits = max(lengths, default=0)
    counts = [0] * (max_bits + 1)
    for length in lengths:
        if length < 0 or length > 15:
            raise ValueError("invalid DEFLATE code length")
        if length:
            counts[length] += 1
    next_code = [0] * (max_bits + 1)
    code = 0
    for bits in range(1, max_bits + 1):
        code = (code + counts[bits - 1]) << 1
        next_code[bits] = code
    decode = {}
    encode = {}
    for symbol, length in enumerate(lengths):
        if not length:
            continue
        code = next_code[length]
        next_code[length] += 1
        decode[(length, code)] = symbol
        encode[symbol] = (code, length)
    return decode, encode, max_bits


def _fixed_trees():
    lit = [0] * 288
    for symbol in range(0, 144):
        lit[symbol] = 8
    for symbol in range(144, 256):
        lit[symbol] = 9
    for symbol in range(256, 280):
        lit[symbol] = 7
    for symbol in range(280, 288):
        lit[symbol] = 8
    return _canonical(lit), _canonical([5] * 32)


_FIXED_LIT, _FIXED_DIST = _fixed_trees()


def _dynamic_trees(reader):
    hlit = reader.read(5) + 257
    hdist = reader.read(5) + 1
    hclen = reader.read(4) + 4
    if hlit > 286 or hdist > 32:
        raise ValueError("invalid dynamic DEFLATE alphabet size")

    cl_lengths = [0] * 19
    for i in range(hclen):
        cl_lengths[_CL_ORDER[i]] = reader.read(3)
    cl_decode, _cl_encode, cl_max = _canonical(cl_lengths)
    if not cl_decode:
        raise ValueError("empty DEFLATE code-length tree")

    lengths = []
    want = hlit + hdist
    while len(lengths) < want:
        symbol = reader.read_code(cl_decode, cl_max)
        if symbol <= 15:
            lengths.append(symbol)
        elif symbol == 16:
            if not lengths:
                raise ValueError("DEFLATE repeat with no preceding length")
            count = reader.read(2) + 3
            lengths.extend([lengths[-1]] * count)
        elif symbol == 17:
            lengths.extend([0] * (reader.read(3) + 3))
        elif symbol == 18:
            lengths.extend([0] * (reader.read(7) + 11))
        else:  # pragma: no cover - the code-length alphabet is 0..18
            raise ValueError("invalid DEFLATE code-length symbol")
        if len(lengths) > want:
            raise ValueError("DEFLATE code lengths overrun their alphabet")

    lit = _canonical(lengths[:hlit])
    dist = _canonical(lengths[hlit:])
    if 256 not in lit[1]:
        raise ValueError("DEFLATE literal tree has no end-of-block code")
    return lit, dist


def _read_block_header(reader):
    """Read one header and return (final, type, lit_tree, dist_tree)."""
    final = reader.read(1)
    kind = reader.read(2)
    if kind == 0:
        reader.align()
        length = reader.read(16)
        inverse = reader.read(16)
        if (length ^ 0xFFFF) != inverse:
            raise ValueError("invalid DEFLATE stored-block length")
        return final, kind, length, None
    if kind == 1:
        return final, kind, _FIXED_LIT, _FIXED_DIST
    if kind == 2:
        lit, dist = _dynamic_trees(reader)
        return final, kind, lit, dist
    raise ValueError("reserved DEFLATE block type")


def _bit_slice(data, start, end):
    writer = _BitWriter()
    for bitpos in range(start, end):
        writer.put_bit((data[bitpos >> 3] >> (bitpos & 7)) & 1)
    return writer.finish()


def _append_match(output, length, distance, max_output=None):
    if distance <= 0 or distance > len(output) or distance > 32768:
        raise ValueError("invalid DEFLATE match distance")
    if max_output is not None and len(output) + length > max_output:
        raise ValueError("DEFLATE output exceeds its declared limit")
    for _ in range(length):
        output.append(output[-distance])


def transform(raw, max_output=None):
    """Return ``(plain_member_bytes, exact_recipe)`` for a raw DEFLATE stream.

    Raises ``ValueError`` for malformed or unsupported input.  Successful
    output is always self-verified by serialising the recipe and comparing it
    against ``raw`` before it is returned.
    """
    raw = bytes(raw)
    if not raw:
        raise ValueError("empty raw DEFLATE stream")
    reader = _BitReader(raw)
    plain = bytearray()
    blocks = []
    final = 0

    while not final:
        if len(blocks) > len(raw) * 8:
            raise ValueError("unreasonable DEFLATE block count")
        start = reader.pos
        final, kind, a, b = _read_block_header(reader)
        header_end = reader.pos
        header = _bit_slice(raw, start, header_end)
        records = bytearray()

        if kind == 0:
            length = a
            if reader.pos + length * 8 > reader.limit:
                raise ValueError("truncated DEFLATE stored block")
            if max_output is not None and len(plain) + length > max_output:
                raise ValueError("DEFLATE output exceeds its declared limit")
            records.append(_REC_STORED)
            _put_varint(records, length)
            # Stored-block data begins on a byte boundary by definition.
            at = reader.pos >> 3
            plain += raw[at:at + length]
            reader.pos += length * 8
        else:
            lit_decode, _lit_encode, lit_max = a
            dist_decode, _dist_encode, dist_max = b
            literal_run = 0

            def flush_literals():
                nonlocal literal_run
                if literal_run:
                    records.append(_REC_LITERALS)
                    _put_varint(records, literal_run)
                    literal_run = 0

            while True:
                symbol = reader.read_code(lit_decode, lit_max)
                if symbol < 256:
                    if max_output is not None and len(plain) >= max_output:
                        raise ValueError("DEFLATE output exceeds its declared limit")
                    plain.append(symbol)
                    literal_run += 1
                    continue
                flush_literals()
                if symbol == 256:
                    records.append(_REC_END)
                    break
                if symbol < 257 or symbol > 285:
                    raise ValueError("reserved DEFLATE length symbol")
                li = symbol - 257
                lextra = reader.read(_LENGTH_EXTRA[li])
                length = _LENGTH_BASE[li] + lextra
                if not dist_decode:
                    raise ValueError("DEFLATE match without a distance tree")
                dsym = reader.read_code(dist_decode, dist_max)
                if dsym >= 30:
                    raise ValueError("reserved DEFLATE distance symbol")
                dextra = reader.read(_DIST_EXTRA[dsym])
                distance = _DIST_BASE[dsym] + dextra
                _append_match(plain, length, distance, max_output=max_output)
                records.append(_REC_MATCH)
                records.append(li)
                _put_varint(records, lextra)
                records.append(dsym)
                _put_varint(records, dextra)

        blocks.append((header_end - start, header, bytes(records)))

    trailing_bits = reader.limit - reader.pos
    trailing = _bit_slice(raw, reader.pos, reader.limit)

    recipe = bytearray(MAGIC)
    _put_varint(recipe, len(blocks))
    for header_bits, header, records in blocks:
        _put_varint(recipe, header_bits)
        recipe += header
        recipe += records
    _put_varint(recipe, trailing_bits)
    recipe += trailing

    rebuilt = restore(bytes(plain), bytes(recipe))
    if rebuilt != raw:
        raise ValueError("DEFLATE recipe did not reproduce its source")
    return bytes(plain), bytes(recipe)


def _header_trees(header, header_bits):
    reader = _BitReader(header, limit=header_bits)
    final, kind, a, b = _read_block_header(reader)
    if reader.pos != header_bits:
        raise ValueError("DEFLATE recipe header has trailing fields")
    return final, kind, a, b


def restore(plain, recipe):
    """Reproduce the exact raw DEFLATE stream represented by ``recipe``."""
    plain = bytes(plain)
    recipe = bytes(recipe)
    if recipe[:4] != MAGIC:
        raise ValueError("not a DEFLATE token recipe")
    pos = 4
    block_count, pos = _get_varint(recipe, pos)
    if block_count <= 0 or block_count > len(recipe):
        raise ValueError("invalid DEFLATE recipe block count")

    writer = _BitWriter()
    produced = bytearray()
    plain_pos = 0
    saw_final = False

    for block_index in range(block_count):
        header_bits, pos = _get_varint(recipe, pos)
        header_bytes = (header_bits + 7) // 8
        if header_bits < 3 or pos + header_bytes > len(recipe):
            raise ValueError("truncated DEFLATE recipe header")
        header = recipe[pos:pos + header_bytes]
        pos += header_bytes
        final, kind, a, b = _header_trees(header, header_bits)
        if saw_final or (final and block_index != block_count - 1):
            raise ValueError("invalid DEFLATE final-block placement")
        saw_final = bool(final)
        writer.put_blob(header, header_bits)

        if pos >= len(recipe):
            raise ValueError("truncated DEFLATE recipe records")
        if kind == 0:
            tag = recipe[pos]
            pos += 1
            if tag != _REC_STORED:
                raise ValueError("stored DEFLATE block has token records")
            length, pos = _get_varint(recipe, pos)
            if length != a or plain_pos + length > len(plain):
                raise ValueError("stored DEFLATE recipe length mismatch")
            chunk = plain[plain_pos:plain_pos + length]
            writer.put_bytes(chunk)
            produced += chunk
            plain_pos += length
            continue

        _lit_decode, lit_encode, _lit_max = a
        _dist_decode, dist_encode, _dist_max = b
        while True:
            if pos >= len(recipe):
                raise ValueError("truncated DEFLATE token recipe")
            tag = recipe[pos]
            pos += 1
            if tag == _REC_LITERALS:
                count, pos = _get_varint(recipe, pos)
                if count <= 0 or plain_pos + count > len(plain):
                    raise ValueError("invalid DEFLATE literal run")
                for byte in plain[plain_pos:plain_pos + count]:
                    try:
                        code, length = lit_encode[byte]
                    except KeyError:
                        raise ValueError("literal absent from DEFLATE tree")
                    writer.put_code(code, length)
                    produced.append(byte)
                plain_pos += count
            elif tag == _REC_MATCH:
                if pos >= len(recipe):
                    raise ValueError("truncated DEFLATE match recipe")
                li = recipe[pos]
                pos += 1
                lextra, pos = _get_varint(recipe, pos)
                if li >= len(_LENGTH_BASE) or lextra >= (1 << _LENGTH_EXTRA[li]):
                    raise ValueError("invalid DEFLATE length recipe")
                if pos >= len(recipe):
                    raise ValueError("truncated DEFLATE distance recipe")
                dsym = recipe[pos]
                pos += 1
                dextra, pos = _get_varint(recipe, pos)
                if dsym >= len(_DIST_BASE) or dextra >= (1 << _DIST_EXTRA[dsym]):
                    raise ValueError("invalid DEFLATE distance recipe")
                symbol = 257 + li
                try:
                    lcode, llen = lit_encode[symbol]
                    dcode, dlen = dist_encode[dsym]
                except KeyError:
                    raise ValueError("match symbol absent from DEFLATE tree")
                writer.put_code(lcode, llen)
                writer.put(lextra, _LENGTH_EXTRA[li])
                writer.put_code(dcode, dlen)
                writer.put(dextra, _DIST_EXTRA[dsym])
                length = _LENGTH_BASE[li] + lextra
                distance = _DIST_BASE[dsym] + dextra
                before = len(produced)
                _append_match(produced, length, distance,
                              max_output=len(plain))
                if plain_pos + length > len(plain) or \
                        produced[before:] != plain[plain_pos:plain_pos + length]:
                    raise ValueError("DEFLATE recipe does not match member bytes")
                plain_pos += length
            elif tag == _REC_END:
                try:
                    code, length = lit_encode[256]
                except KeyError:
                    raise ValueError("DEFLATE tree has no end-of-block code")
                writer.put_code(code, length)
                break
            else:
                raise ValueError("unknown DEFLATE recipe record %d" % tag)

    if not saw_final:
        raise ValueError("DEFLATE recipe has no final block")
    trailing_bits, pos = _get_varint(recipe, pos)
    trailing_bytes = (trailing_bits + 7) // 8
    if trailing_bits > 7 or pos + trailing_bytes != len(recipe):
        raise ValueError("invalid DEFLATE recipe trailing bits")
    writer.put_blob(recipe[pos:pos + trailing_bytes], trailing_bits)
    if plain_pos != len(plain) or produced != plain:
        raise ValueError("unused bytes in DEFLATE member reconstruction")
    return writer.finish()
