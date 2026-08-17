#!/usr/bin/env python3
"""Full verification suite.

1. SHA-256 round trips: every corpus file x {adaptive, baseline} x
   {auto, afc1, afc2}, on the native engine AND the pure-Python engine.
2. Byte-identity: native container == pure-Python container for every combo.
3. Cross-decoding: every container decoded by the Python decoder, the native
   decoder, and (when node is available) the JavaScript engine.
4. Edge cases: empty file, 1 byte, single repeated byte, all 256 byte values,
   input larger than the 1 MB scan window, and compressing an .afc container
   as ordinary data.

Exit code 0 = everything passed.  Run from the project root:
    python tools/run_verification.py
"""
import glob
import hashlib
import os
import random
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import afc                      # noqa: E402
import afc2                     # noqa: E402

FAIL = 0


def check(label, ok):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return ok


def edge_inputs():
    rng = random.Random(2026)
    big = bytes(rng.choice(b"abcdefgh \n") for _ in range(3 * 1024)) * 512
    yield "empty", b""
    yield "one_byte", b"A"
    yield "same_byte_64k", b"z" * 65536
    yield "alphabet_1k", bytes(range(256)) * 4
    yield "bigger_than_window", big          # 1.5 MB > 1 MB scan window
    afcfile = afc2.compress_bytes(b"nested container test " * 50, True)
    yield "afc_as_data", afcfile


def main():
    files = sorted(glob.glob(os.path.join("benchmarks", "corpus", "*")))
    combos = [(a, f) for a in (True, False) for f in ("auto", "afc1", "afc2")]

    # ---- 1+2: round trips + native/python byte identity ----
    print(f"== corpus round trips (engine: "
          f"{'native' if afc2.NATIVE else 'python'}) ==")
    blobs = {}
    ok_all = True
    for path in files:
        data = open(path, "rb").read()
        ref = hashlib.sha256(data).digest()
        for adaptive, fmt in combos:
            blob = afc2.compress_bytes(data, adaptive, fmt=fmt)
            blobs[(path, adaptive, fmt)] = blob
            out = afc2.decompress_bytes(blob)
            ok_all &= hashlib.sha256(out).digest() == ref
    check(f"{len(blobs)} corpus round trips (SHA-256)", ok_all)

    if afc2.NATIVE:
        code = (
            "import glob,hashlib,os,sys;"
            "sys.path.insert(0,'.');import afc2;assert not afc2.NATIVE;\n"
            "for f in sorted(glob.glob('benchmarks/corpus/*')):\n"
            "  d=open(f,'rb').read()\n"
            "  for a in (True,False):\n"
            "    for fmt in ('auto','afc1','afc2'):\n"
            "      b=afc2.compress_bytes(d,a,fmt=fmt)\n"
            "      print(f,a,fmt,len(b),hashlib.sha256(b).hexdigest())\n")
        r = subprocess.run([sys.executable, "-c", code], text=True,
                           capture_output=True,
                           env=dict(os.environ, AFC_NO_NATIVE="1"))
        ident = r.returncode == 0
        if ident:
            for line in r.stdout.strip().splitlines():
                p, a, fmt, ln, h = line.split()
                b = blobs[(p, a == "True", fmt)]
                if (str(len(b)), hashlib.sha256(b).hexdigest()) != (ln, h):
                    ident = False
                    print("   mismatch:", p, a, fmt)
        check("native vs pure-Python containers byte-identical", ident)

    # ---- 3: cross decoding ----
    ok_py = all(afc.decompress_bytes(b) == open(p, "rb").read()
                for (p, _, _), b in blobs.items())
    check("Python decoder decodes every container", ok_py)
    if afc2.NATIVE:
        import afc_native
        ok_nat = all(afc_native.decompress(b) == open(p, "rb").read()
                     for (p, _, _), b in blobs.items())
        check("native decoder decodes every container", ok_nat)
    node = shutil.which("node")
    if node and os.path.exists("afc_engine.js"):
        tmp = os.path.join("benchmarks", "_xdec")
        os.makedirs(tmp, exist_ok=True)
        manifest = []
        for i, ((p, a, fmt), b) in enumerate(sorted(blobs.items())):
            fn = os.path.join(tmp, f"{i}.afc")
            open(fn, "wb").write(b)
            manifest.append((fn, p))
        open(os.path.join(tmp, "list.txt"), "w").write(
            "\n".join(f"{a}\t{b}" for a, b in manifest))
        js = (
            "const fs=require('fs');const AFC=require('./afc_engine.js');"
            "let bad=0;"
            "for(const line of fs.readFileSync('" + tmp +
            "/list.txt','utf8').trim().split('\\n')){"
            "const [fn,src]=line.split('\\t');"
            "const out=AFC.decompressBytes(new Uint8Array(fs.readFileSync(fn)));"
            "const ref=new Uint8Array(fs.readFileSync(src));"
            "if(Buffer.compare(Buffer.from(out),Buffer.from(ref))!==0){bad++;}}"
            "console.log('jsbad='+bad);process.exit(bad?1:0);")
        r = subprocess.run([node, "-e", js], capture_output=True, text=True)
        check("JavaScript engine decodes every container",
              r.returncode == 0)
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("SKIP  JavaScript cross-decode (node not available)")

    # ---- 4: edge cases ----
    print("== edge cases ==")
    for name, data in edge_inputs():
        ok = True
        for adaptive, fmt in combos:
            blob = afc2.compress_bytes(data, adaptive, fmt=fmt)
            ok &= afc2.decompress_bytes(blob) == data
            ok &= afc.decompress_bytes(blob) == data
            ok &= len(blob) <= len(data) + 16   # raw fallback bound
        check(f"edge: {name} ({len(data)} B)", ok)

    print(f"\n{'ALL CHECKS PASSED' if FAIL == 0 else f'{FAIL} CHECKS FAILED'}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
