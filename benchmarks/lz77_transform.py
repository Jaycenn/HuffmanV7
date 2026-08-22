#!/usr/bin/env python3
"""LZ77 as a reversible pre-transform, to test LZ77 + Hybrid Huffman.

WHAT THIS IS FOR
----------------
An experiment, not a proposal. It answers one question with measurement:
if LZ77 runs before the Hybrid Huffman engine, does the combination beat the
engine alone, and by how much?

It is deliberately a *transform* — bytes in, bytes out, exactly invertible —
so nothing in afc2.py, containers.py or the container format has to change.
Run it, measure it, then decide.

    plain = lz77_encode(data)          # data -> token representation
    assert lz77_decode(plain) == data  # exactly invertible
    afc2.compress_bytes(plain)         # engine sees the transformed bytes

LAYOUT
------
Literals and match tokens go in SEPARATE regions rather than interleaved.
That matters for a fair test: the engine builds one frequency model over its
input, and interleaving text with binary distance fields would pollute that
model and understate what LZ77 can do. DEFLATE keeps separate alphabets for
literals and distances for the same reason.

    8 bytes   original length
    4 bytes   literal region length
    4 bytes   token count
    N bytes   literals, concatenated
    5*T bytes tokens: run_len(2) match_len(1) distance(2)

A token means: copy `run_len` bytes from the literal region, then copy
`match_len` bytes starting `distance` back in the output already produced.
A final token with match_len 0 flushes any trailing literals.
"""
import struct

WINDOW = 65535          # distance fits in 2 bytes
MIN_MATCH = 4           # below this a token costs more than the literals
MAX_MATCH = 255 + MIN_MATCH
MAX_RUN = 65535
MAX_CHAIN = 32          # bounded search: quality vs. speed
HEADER = struct.Struct("<QII")
TOKEN = struct.Struct("<HBH")


def lz77_encode(data: bytes) -> bytes:
    n = len(data)
    literals = bytearray()
    tokens = bytearray()
    heads = {}              # 4-byte key -> most recent position
    chains = {}             # position -> previous position with same key
    run_start = 0
    i = 0

    while i < n:
        best_len = 0
        best_dist = 0
        if i + MIN_MATCH <= n:
            key = data[i:i + MIN_MATCH]
            cand = heads.get(key)
            steps = 0
            while cand is not None and steps < MAX_CHAIN:
                dist = i - cand
                if dist > WINDOW:
                    break
                # extend the candidate match
                length = 0
                limit = min(MAX_MATCH, n - i)
                while (length < limit
                       and data[cand + length] == data[i + length]):
                    length += 1
                if length > best_len:
                    best_len = length
                    best_dist = dist
                    if length >= limit:
                        break
                cand = chains.get(cand)
                steps += 1

        if best_len >= MIN_MATCH:
            run = i - run_start
            # a run longer than MAX_RUN needs splitting into empty-match tokens
            while run > MAX_RUN:
                literals += data[run_start:run_start + MAX_RUN]
                tokens += TOKEN.pack(MAX_RUN, 0, 0)
                run_start += MAX_RUN
                run -= MAX_RUN
            literals += data[run_start:i]
            tokens += TOKEN.pack(run, best_len - MIN_MATCH, best_dist)
            # index every position the match covers, so later matches find them
            for k in range(i, min(i + best_len, n - MIN_MATCH + 1)):
                key = data[k:k + MIN_MATCH]
                chains[k] = heads.get(key)
                heads[key] = k
            i += best_len
            run_start = i
        else:
            if i + MIN_MATCH <= n:
                key = data[i:i + MIN_MATCH]
                chains[i] = heads.get(key)
                heads[key] = i
            i += 1

    run = n - run_start
    while run > MAX_RUN:
        literals += data[run_start:run_start + MAX_RUN]
        tokens += TOKEN.pack(MAX_RUN, 0, 0)
        run_start += MAX_RUN
        run -= MAX_RUN
    if run:
        literals += data[run_start:n]
        tokens += TOKEN.pack(run, 0, 0)

    count = len(tokens) // TOKEN.size
    return HEADER.pack(n, len(literals), count) + bytes(literals) + bytes(tokens)


def lz77_decode(blob: bytes) -> bytes:
    total, lit_len, count = HEADER.unpack_from(blob, 0)
    lit_at = HEADER.size
    tok_at = lit_at + lit_len
    out = bytearray()
    lp = lit_at
    for t in range(count):
        run, mlen, dist = TOKEN.unpack_from(blob, tok_at + t * TOKEN.size)
        if run:
            out += blob[lp:lp + run]
            lp += run
        if dist:
            length = mlen + MIN_MATCH
            start = len(out) - dist
            # overlapping copies are legal and common; copy byte by byte
            for k in range(length):
                out.append(out[start + k])
    return bytes(out[:total])


if __name__ == "__main__":
    import os
    import random
    cases = [
        b"", b"a", b"ab", b"abc", b"aaaa", b"a" * 100000,
        b"abcabcabcabc" * 500,
        b"the quick brown fox " * 2000,
        os.urandom(50000),
        bytes(range(256)) * 300,
        b"x" * 70000 + b"y" * 70000,                 # long runs
        bytes(random.choice(b"ab") for _ in range(80000)),
    ]
    for path in ("benchmarks/canterbury/alice29.txt",
                 "benchmarks/corpus/data.csv",
                 "benchmarks/corpus/server.log"):
        try:
            cases.append(open(path, "rb").read())
        except OSError:
            pass
    for idx, sample in enumerate(cases):
        enc = lz77_encode(sample)
        dec = lz77_decode(enc)
        assert dec == sample, "ROUND TRIP FAILED on case %d (%d bytes)" % (
            idx, len(sample))
    print("all %d cases round-trip exactly" % len(cases))
