#!/usr/bin/env python3
"""Deterministic benchmark corpus generator.  Every file is produced from a
fixed seed (or copied from a stable source), so results are reproducible.
Run from the project root:  python tools/make_corpus.py
"""
import os
import random
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "benchmarks", "corpus")
os.makedirs(OUT, exist_ok=True)


def w(name, data):
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(data)
    print(f"{name:22s} {len(data):>8} B")


# 1) English prose — Project Gutenberg-style paragraphs assembled
#    deterministically from a fixed vocabulary (seeded).
rng = random.Random(20260727)
VOCAB = ("the of and a to in is was he that it his her she had with for as "
         "on at by which but from this all not are were when we there been "
         "their one have or an they you what if would who has will more no "
         "out do so said up into than them can only other time new some "
         "could these two may then first any my now such like our over man "
         "me even most made after also did many before must through years "
         "where much your way well down should because each just those "
         "people how too little state good very make world still own see "
         "men work long get here between both life being under never day "
         "same another know while last might us great old year off come "
         "since against go came right used take three").split()
paras = []
for _ in range(220):
    sent = []
    for _s in range(rng.randint(3, 7)):
        k = rng.randint(8, 18)
        words = [rng.choice(VOCAB) for _ in range(k)]
        words[0] = words[0].capitalize()
        sent.append(" ".join(words) + ".")
    paras.append(" ".join(sent))
w("prose_en.txt", ("\n\n".join(paras) + "\n").encode())

# 2) Source code — stitched from this project's own Python sources.
parts = []
for fn in ("afc.py", "afc2.py", "afc_native.py", "app.py"):
    p = os.path.join(ROOT, fn)
    if os.path.exists(p):
        parts.append(open(p, "rb").read())
w("code_python.py.txt", b"\n\n".join(parts))

# 3) JSON API dump (seeded).
rng = random.Random(1234)
rows = []
CITIES = ["Angeles", "Manila", "Cebu", "Davao", "Baguio", "Iloilo"]
for i in range(1400):
    rows.append('{"id":%d,"user":"user_%04d","city":"%s","score":%d,'
                '"active":%s,"tags":["alpha","beta","%s"]}'
                % (i, rng.randint(0, 9999), rng.choice(CITIES),
                   rng.randint(0, 100000),
                   "true" if rng.random() < 0.5 else "false",
                   rng.choice(CITIES).lower()))
w("data.json", ('[\n' + ',\n'.join(rows) + '\n]\n').encode())

# 4) CSV telemetry (seeded).
rng = random.Random(99)
lines = ["timestamp,sensor_id,temperature_c,humidity_pct,status"]
for i in range(4200):
    lines.append("2026-07-%02d %02d:%02d:%02d,SEN-%03d,%.2f,%.1f,%s"
                 % (rng.randint(1, 27), rng.randint(0, 23),
                    rng.randint(0, 59), rng.randint(0, 59),
                    rng.randint(1, 40), 15 + rng.random() * 20,
                    30 + rng.random() * 60,
                    rng.choice(["OK", "OK", "OK", "WARN", "FAIL"])))
w("data.csv", ("\n".join(lines) + "\n").encode())

# 5) Server log — highly repetitive lines (seeded).
rng = random.Random(7)
lines = []
PATHS = ["/api/v1/users", "/api/v1/orders", "/static/app.js",
         "/static/style.css", "/health", "/api/v1/login"]
for i in range(3000):
    lines.append('192.168.%d.%d - - [27/Jul/2026:%02d:%02d:%02d +0800] '
                 '"GET %s HTTP/1.1" %d %d "-" "Mozilla/5.0 (X11; Linux '
                 'x86_64) AppleWebKit/537.36"'
                 % (rng.randint(0, 4), rng.randint(1, 254),
                    rng.randint(0, 23), rng.randint(0, 59),
                    rng.randint(0, 59), rng.choice(PATHS),
                    rng.choice([200, 200, 200, 304, 404]),
                    rng.randint(180, 9500)))
w("server.log", ("\n".join(lines) + "\n").encode())

# 6) Structured binary — repeated 32-byte records with seeded noise fields.
rng = random.Random(41)
buf = bytearray()
for i in range(4096):
    buf += b"RECD"
    buf += i.to_bytes(4, "little")
    buf += (i * 37 % 65536).to_bytes(2, "little")
    buf += bytes([rng.randint(0, 255) for _ in range(6)])
    buf += b"\x00\x00\xff\xff" + b"PADPADPADPAD"
w("records.bin", bytes(buf))

# 7) Incompressible random data (raw-fallback exercise).
rng = random.Random(0xC0FFEE)
w("random.bin", bytes(rng.randint(0, 255) for _ in range(65536)))

# 8) Tiny file and full byte alphabet.
w("tiny.txt", b"hello adaptive file compression\n")
w("bytes256.bin", bytes(range(256)) * 4)

# 9) universe.sql is copied by the packager if available.
src = os.path.join(ROOT, "benchmarks", "corpus", "universe.sql")
if os.path.exists(src):
    print(f"{'universe.sql':22s} {os.path.getsize(src):>8} B (kept)")

# The synthetic files above are what this generator PRODUCES.  Two real,
# standard corpora are also shipped in the archive and used for the headline
# results; they are NOT generated here (they are third-party benchmark data):
#   benchmarks/canterbury/  — the 5 Canterbury Corpus files supplied for this
#                             study (alice29.txt, asyoulik.txt, cp.html,
#                             fields.c, grammar.lsp), verified against the
#                             canonical sizes.  Drop additional Canterbury or
#                             Silesia files into that folder and re-run
#                             tools/make_report.py to extend the tables.
ct = os.path.join(ROOT, "benchmarks", "canterbury")
if os.path.isdir(ct):
    for name in sorted(os.listdir(ct)):
        fp = os.path.join(ct, name)
        if os.path.isfile(fp):
            print(f"{'canterbury/' + name:22s} {os.path.getsize(fp):>8} B (real)")
