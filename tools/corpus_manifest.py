#!/usr/bin/env python3
"""Download and verify the official Canterbury and Silesia corpora.

ZIP/BZip2 are used only to transport the public benchmark datasets. They are
not used by AFC compression and never appear in an AFC output path.
"""

import argparse
import bz2
import hashlib
import json
import os
import ssl
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST_PATH = os.path.join(ROOT, "benchmarks", "corpus_manifest.json")


def load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as stream:
        return json.load(stream)


def hashes(path):
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1 << 20)
            if not block:
                break
            md5.update(block)
            sha256.update(block)
    return md5.hexdigest(), sha256.hexdigest()


def verify_file(path, record):
    if not os.path.isfile(path):
        return False, "missing"
    if os.path.getsize(path) != record["bytes"]:
        return False, "size %d != %d" % (os.path.getsize(path), record["bytes"])
    md5, sha256 = hashes(path)
    if record.get("md5") and md5 != record["md5"]:
        return False, "MD5 mismatch"
    if record.get("sha256") and sha256 != record["sha256"]:
        return False, "SHA-256 mismatch"
    return True, sha256


def verify(names):
    manifest = load_manifest()
    ok = True
    for corpus in names:
        spec = manifest[corpus]
        directory = os.path.join(ROOT, spec["directory"])
        for name, record in spec["files"].items():
            valid, detail = verify_file(os.path.join(directory, name), record)
            print("%-10s %-18s %-4s %s" %
                  (corpus, name, "OK" if valid else "FAIL", detail))
            ok = ok and valid
    return ok


def _urlopen(url):
    # The managed desktop proxy presents a local certificate. The downloaded
    # bytes are authenticated against the committed corpus hashes/sizes.
    context = ssl._create_unverified_context()
    request = urllib.request.Request(url, headers={"User-Agent": "AFC-corpus/1.0"})
    return urllib.request.urlopen(request, context=context, timeout=120)


def download_canterbury(spec):
    target = os.path.join(ROOT, spec["directory"])
    os.makedirs(target, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp:
        archive = temp.name
        with _urlopen(spec["source"]) as response:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                temp.write(block)
    try:
        with zipfile.ZipFile(archive) as package:
            members = {os.path.basename(info.filename): info
                       for info in package.infolist() if not info.is_dir()}
            for name, record in spec["files"].items():
                info = members.get(name)
                if info is None:
                    raise ValueError("archive is missing %s" % name)
                data = package.read(info)
                if len(data) != record["bytes"]:
                    raise ValueError("unexpected size for %s" % name)
                path = os.path.join(target, name)
                with open(path + ".part", "wb") as stream:
                    stream.write(data)
                os.replace(path + ".part", path)
                print("downloaded canterbury/%s" % name)
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass


def download_silesia(spec):
    target = os.path.join(ROOT, spec["directory"])
    os.makedirs(target, exist_ok=True)
    for name, record in spec["files"].items():
        path = os.path.join(target, name)
        valid, _ = verify_file(path, record)
        if valid:
            print("verified existing silesia/%s" % name)
            continue
        url = spec["source_template"].format(name=name)
        md5 = hashlib.md5(usedforsecurity=False)
        size = 0
        decompressor = bz2.BZ2Decompressor()
        with _urlopen(url) as response, open(path + ".part", "wb") as output:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                plain = decompressor.decompress(block)
                output.write(plain)
                md5.update(plain)
                size += len(plain)
        if size != record["bytes"] or md5.hexdigest() != record["md5"]:
            try:
                os.remove(path + ".part")
            except OSError:
                pass
            raise ValueError("download verification failed for %s" % name)
        os.replace(path + ".part", path)
        print("downloaded silesia/%s" % name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "download"))
    parser.add_argument("corpora", nargs="*", choices=("canterbury", "silesia"))
    args = parser.parse_args()
    names = args.corpora or ["canterbury", "silesia"]
    manifest = load_manifest()
    if args.action == "download":
        for name in names:
            if name == "canterbury":
                download_canterbury(manifest[name])
            else:
                download_silesia(manifest[name])
    return 0 if verify(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
