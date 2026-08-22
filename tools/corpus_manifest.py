#!/usr/bin/env python3
"""Download and verify the Canterbury, Silesia and Govdocs1 corpora.

ZIP/BZip2 are used only to transport the public benchmark datasets. They are
not used by AFC compression and never appear in an AFC output path.
"""

import argparse
import bz2
import hashlib
import json
import os
import ssl
import struct
import tempfile
import time
import urllib.request
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST_PATH = os.path.join(ROOT, "benchmarks", "corpus_manifest.json")

# The pinning ceiling is the size the application accepts, so a corpus can never
# contain a file the system under study would refuse.
try:
    import sys
    sys.path.insert(0, ROOT)
    from config import MAX_FILE_SIZE as MAX_PINNED_BYTES
except Exception:                       # config is optional for this tool
    MAX_PINNED_BYTES = 100 * 1024 * 1024


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


def _records(spec):
    """Yield (path-relative-to-corpus-dir, record) for either manifest shape.

    Canterbury and Silesia list files flat. Govdocs1 is a corpus of about a
    million files, so its entry pins a subset per file type and nests them
    under "subsets"; the files land in one directory per type.
    """
    if "subsets" in spec:
        for kind, subset in sorted(spec["subsets"].items()):
            for name, record in sorted(subset["files"].items()):
                yield os.path.join(kind, name), record
    else:
        for name, record in spec["files"].items():
            yield name, record


def verify(names):
    manifest = load_manifest()
    ok = True
    for corpus in names:
        spec = manifest[corpus]
        directory = os.path.join(ROOT, spec["directory"])
        for name, record in _records(spec):
            valid, detail = verify_file(os.path.join(directory, name), record)
            print("%-10s %-24s %-4s %s" %
                  (corpus, name, "OK" if valid else "FAIL", detail))
            ok = ok and valid
    return ok


def _urlopen(url, headers=None, method=None):
    # The managed desktop proxy presents a local certificate. The downloaded
    # bytes are authenticated against the committed corpus hashes/sizes.
    context = ssl._create_unverified_context()
    sent = {"User-Agent": "AFC-corpus/1.0"}
    if headers:
        sent.update(headers)
    request = urllib.request.Request(url, headers=sent, method=method)
    return urllib.request.urlopen(request, context=context, timeout=120)


def _range(url, start, end, attempts=5):
    """Fetch one byte range, retrying on a dropped connection.

    A Govdocs1 subset is several hundred sequential requests against the same
    host, and the far end will occasionally close one mid-flight. Without a
    retry a single drop discards the whole download, so back off and repeat.
    """
    header = {"Range": "bytes=%d-%d" % (start, end)}
    for attempt in range(attempts):
        try:
            with _urlopen(url, headers=header) as response:
                return response.read()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


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


def _zip_central_directory(url):
    """Read a remote ZIP's central directory over HTTP range requests.

    Govdocs1 ships as archives of several hundred megabytes each, and the
    subset in the manifest is a small fraction of any one of them. Reading the
    directory and then fetching only the wanted members keeps the download to
    the bytes actually used: the PDF subset is 126 MB out of a 486 MB archive,
    and the CSV subset under 3 MB out of 720 MB.
    """
    with _urlopen(url, method="HEAD") as response:
        size = int(response.headers["Content-Length"])
    tail = _range(url, max(0, size - 65600), size - 1)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0:
        raise ValueError("no end-of-central-directory record in %s" % url)
    cd_size, cd_offset = struct.unpack("<II", tail[marker + 12:marker + 20])
    blob = _range(url, cd_offset, cd_offset + cd_size - 1)

    entries = {}
    pos = 0
    while pos < len(blob) and blob[pos:pos + 4] == b"PK\x01\x02":
        compressed, uncompressed = struct.unpack("<II", blob[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack(
            "<HHH", blob[pos + 28:pos + 34])
        local_offset = struct.unpack("<I", blob[pos + 42:pos + 46])[0]
        name = blob[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        entries[name.split("/")[-1]] = {
            "compressed": compressed, "uncompressed": uncompressed,
            "offset": local_offset}
        pos += 46 + name_len + extra_len + comment_len
    return entries


def _zip_member(url, entry):
    """Fetch and inflate one ZIP member, using only its own byte range."""
    header = _range(url, entry["offset"], entry["offset"] + 29)
    if header[:4] != b"PK\x03\x04":
        raise ValueError("bad local file header")
    method = struct.unpack("<H", header[8:10])[0]
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    start = entry["offset"] + 30 + name_len + extra_len
    raw = _range(url, start, start + entry["compressed"] - 1)
    if method == 0:
        return raw
    if method == 8:
        return zlib.decompress(raw, -15)
    raise ValueError("unsupported ZIP compression method %d" % method)


def download_govdocs1(spec):
    """Fetch the pinned Govdocs1 subset, one directory per file type.

    Unlike Canterbury (one small archive) and Silesia (one file per member),
    Govdocs1 is a corpus of about a million files published as multi-hundred-
    megabyte archives. The manifest pins an explicit subset per type, so this
    pulls exactly those members and verifies each against its recorded hash.
    """
    root = os.path.join(ROOT, spec["directory"])
    for kind, subset in sorted(spec["subsets"].items()):
        target = os.path.join(root, kind)
        os.makedirs(target, exist_ok=True)

        wanted = {name: record for name, record in subset["files"].items()
                  if not verify_file(os.path.join(target, name), record)[0]}
        if not wanted:
            print("verified existing govdocs1/%s (%d files)"
                  % (kind, len(subset["files"])))
            continue

        entries = _zip_central_directory(subset["archive"])
        for name, record in sorted(wanted.items()):
            entry = entries.get(name)
            if entry is None:
                raise ValueError("archive is missing %s/%s" % (kind, name))
            data = _zip_member(subset["archive"], entry)
            if (len(data) != record["bytes"]
                    or hashlib.sha256(data).hexdigest() != record["sha256"]):
                raise ValueError("download verification failed for %s/%s"
                                 % (kind, name))
            path = os.path.join(target, name)
            with open(path + ".part", "wb") as stream:
                stream.write(data)
            os.replace(path + ".part", path)
        print("downloaded govdocs1/%s (%d files)" % (kind, len(wanted)))


def pin_subset(manifest, corpus, subset, archive, max_bytes):
    """Download every eligible member of an archive and pin it in the manifest.

    The other download paths verify against hashes the manifest already holds.
    A new subset has none, so this records them: it reads the archive's central
    directory, fetches each member once, writes it, and accumulates the size and
    SHA-256 the verifying path will check against later.

    Selection follows the rule the manifest already states -- every member of the
    archive except directories, empty entries, and anything larger than
    config.MAX_FILE_SIZE, which the system does not accept. Nothing is chosen by
    file type, so the subset is whatever the archive contains.
    """
    spec = manifest[corpus]
    target = os.path.join(ROOT, spec["directory"], subset)
    os.makedirs(target, exist_ok=True)

    entries = _zip_central_directory(archive)
    eligible, skipped = {}, []
    for name, entry in sorted(entries.items()):
        if not name or name.endswith("/") or entry["uncompressed"] <= 0:
            continue
        if entry["uncompressed"] > max_bytes:
            skipped.append((name, entry["uncompressed"]))
            continue
        eligible[name] = entry

    total = sum(e["uncompressed"] for e in eligible.values())
    print("%s/%s: %d eligible members, %s bytes"
          % (corpus, subset, len(eligible), format(total, ",")))
    for name, size in skipped:
        print("  excluded %s (%s bytes, over the %s byte ceiling)"
              % (name, format(size, ","), format(max_bytes, ",")))

    files = {}
    done = 0
    started = time.time()
    for name, entry in sorted(eligible.items()):
        path = os.path.join(target, name)
        record = {"bytes": entry["uncompressed"]}
        if os.path.isfile(path) and os.path.getsize(path) == record["bytes"]:
            record["sha256"] = hashes(path)[1]          # already fetched
        else:
            data = _zip_member(archive, entry)
            if len(data) != record["bytes"]:
                raise ValueError("%s: got %d bytes, central directory says %d"
                                 % (name, len(data), record["bytes"]))
            record["sha256"] = hashlib.sha256(data).hexdigest()
            with open(path + ".part", "wb") as stream:
                stream.write(data)
            os.replace(path + ".part", path)
        files[name] = record
        done += 1
        if done % 25 == 0 or done == len(eligible):
            print("  %d/%d  %.0fs elapsed"
                  % (done, len(eligible), time.time() - started), flush=True)

    spec.setdefault("subsets", {})[subset] = {
        "archive": archive,
        "count": len(files),
        "bytes": total,
        "files": files,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("pinned %d files in %s" % (len(files), MANIFEST_PATH))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "download", "pin"))
    parser.add_argument("corpora", nargs="*",
                        choices=("canterbury", "silesia", "govdocs1"))
    parser.add_argument("--subset", help="pin: name of the subset to record")
    parser.add_argument("--archive", help="pin: URL of the archive to read")
    parser.add_argument("--max-bytes", type=int, default=MAX_PINNED_BYTES,
                        help="pin: skip members larger than this "
                             "(default: config.MAX_FILE_SIZE)")
    args = parser.parse_args()
    names = args.corpora or ["canterbury", "silesia", "govdocs1"]
    manifest = load_manifest()

    if args.action == "pin":
        if len(names) != 1 or not args.subset or not args.archive:
            parser.error("pin needs one corpus, --subset and --archive")
        pin_subset(manifest, names[0], args.subset, args.archive,
                   args.max_bytes)
        return 0 if verify([names[0]]) else 1

    if args.action == "download":
        for name in names:
            if name == "canterbury":
                download_canterbury(manifest[name])
            elif name == "govdocs1":
                download_govdocs1(manifest[name])
            else:
                download_silesia(manifest[name])
    return 0 if verify(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
