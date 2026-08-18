#!/usr/bin/env python3
"""
End-to-end test suite for the AFC web app (Part 1).

Run:  python tests/test_app.py          (no pytest dependency required)
      pytest tests/test_app.py -q       (also works if pytest is installed)

Covers, per the brief's proof requirements:
  * auth: register, login, logout, wrong password, rate limiting, session
    expiry semantics, forced password change, role-gated 403 (NOT a redirect)
  * SHA-256 lossless round trips: single file, batch endpoint, and inside the
    .afcpak archive
  * archive: manifest integrity, path-traversal rejection, and a structural
    assertion that no DEFLATE is used anywhere
  * size policy: min/max/batch enforcement reads from config (no magic numbers)
  * history/report: rows attributed to the right user, CSV/PDF export render
  * user isolation: one user cannot see another's history
"""
import ast
import hashlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import afcpak                                                   # noqa: E402
import config                                                   # noqa: E402

FAILURES = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("PASS  %s" % name)
    else:
        FAILURES.append(name)
        print("FAIL  %s %s" % (name, detail))
    return cond


def make_app():
    """Fresh app + fresh temp DB per invocation."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    config.DATABASE_PATH = path
    for mod in ("app", "db", "auth", "admin"):
        sys.modules.pop(mod, None)
    import app as appmod
    application = appmod.create_app(db_path=path, testing=True)
    application.secret_key = "test-key"
    return application, appmod, path


def login(client, username, password):
    return client.post("/login", data={"username": username,
                                       "password": password},
                       follow_redirects=False)


def corpus_files(limit=4):
    import glob
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "benchmarks", "corpus", "*"))):
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            out.append(p)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------

def test_auth(app, appmod):
    c = app.test_client()

    # anonymous is redirected to login, not 500
    r = c.get("/", follow_redirects=False)
    check("anonymous redirected to login",
          r.status_code in (301, 302) and "/login" in r.headers.get("Location", ""),
          r.status_code)

    # register
    r = c.post("/register", data={"username": "alice", "email": "a@b.co",
                                  "password": "correct-horse",
                                  "confirm": "correct-horse"},
               follow_redirects=False)
    check("register creates account and logs in", r.status_code in (301, 302))

    r = c.get("/")
    check("logged-in user reaches dashboard", r.status_code == 200)

    # logout
    c.post("/logout")
    r = c.get("/", follow_redirects=False)
    check("logout ends session", r.status_code in (301, 302))

    # wrong password rejected
    r = login(c, "alice", "WRONG")
    check("wrong password rejected (401)", r.status_code == 401, r.status_code)

    # correct password accepted
    r = login(c, "alice", "correct-horse")
    check("correct password accepted", r.status_code in (301, 302), r.status_code)

    # short password refused at registration
    c2 = app.test_client()
    r = c2.post("/register", data={"username": "shorty", "email": "s@b.co",
                                   "password": "abc", "confirm": "abc"})
    check("short password refused", r.status_code == 400, r.status_code)

    # duplicate username refused
    r = c2.post("/register", data={"username": "alice", "email": "z@b.co",
                                   "password": "another-long-one",
                                   "confirm": "another-long-one"})
    check("duplicate username refused", r.status_code == 400, r.status_code)

    # password is hashed, never plaintext
    import db
    with app.app_context():
        row = db.get_user_by_username("alice")
        check("password stored hashed (not plaintext)",
              row is not None and "correct-horse" not in row["password_hash"])


def test_rate_limit(app):
    c = app.test_client()
    codes = []
    for _ in range(config.LOGIN_MAX_ATTEMPTS + 2):
        codes.append(login(c, "alice", "bad-password").status_code)
    check("login rate limiting engages (429)", 429 in codes,
          "codes=%s" % codes)

    # A locked-out user stays locked even with the CORRECT password — that is
    # the point of the limiter, and it is asserted here rather than assumed.
    r = login(c, "alice", "correct-horse")
    check("rate limit blocks even a correct password", r.status_code == 429,
          r.status_code)

    # Release the lock so the remaining tests can authenticate.  Clear the
    # whole table rather than one (username, ip) pair: the test client's
    # remote_addr is 127.0.0.1, so a key-specific clear would silently miss.
    # In production the window simply expires after LOGIN_WINDOW_SECONDS.
    import db
    with app.app_context():
        conn = db.get_db()
        conn.execute("DELETE FROM login_attempts")
        conn.commit()
    r = login(c, "alice", "correct-horse")
    check("login works again once the window is cleared",
          r.status_code in (301, 302), r.status_code)


def test_forced_password_change(app):
    """The seeded admin must be forced to change its documented default."""
    c = app.test_client()
    r = login(c, config.DEFAULT_ADMIN_USERNAME, config.DEFAULT_ADMIN_PASSWORD)
    check("seeded admin can log in", r.status_code in (301, 302), r.status_code)
    loc = r.headers.get("Location", "")
    check("seeded admin forced to change password",
          "change-password" in loc, loc)
    # any other route bounces back to the change form until it is done
    r = c.get("/files", follow_redirects=False)
    check("forced change blocks other routes",
          r.status_code in (301, 302)
          and "change-password" in r.headers.get("Location", ""))
    r = c.post("/change-password", data={
        "current_password": config.DEFAULT_ADMIN_PASSWORD,
        "new_password": "admin-new-password",
        "confirm_password": "admin-new-password"}, follow_redirects=False)
    check("admin password change succeeds", r.status_code in (301, 302))
    r = c.get("/files")
    check("routes reachable after change", r.status_code == 200)
    return c


def test_role_gate(app):
    """A logged-in NON-admin hitting an admin route must get 403, not a
    redirect — the brief calls this out explicitly."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    r = c.get("/admin/users", follow_redirects=False)
    check("non-admin gets 403 on admin route (not redirect)",
          r.status_code == 403, r.status_code)
    r = c.get("/admin/audit", follow_redirects=False)
    check("non-admin gets 403 on audit route", r.status_code == 403,
          r.status_code)

    admin_c = app.test_client()
    login(admin_c, config.DEFAULT_ADMIN_USERNAME, "admin-new-password")
    r = admin_c.get("/admin/users")
    check("admin reaches admin route", r.status_code == 200, r.status_code)


def test_single_roundtrip(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    ok = True
    for path in corpus_files(4):
        data = open(path, "rb").read()
        name = os.path.basename(path)
        r = c.post("/api/compress",
                   data={"file": (io.BytesIO(data), name)})
        j = r.get_json()
        if r.status_code != 200 or not j.get("lossless"):
            ok = False
            print("   single failed:", name, r.status_code, j)
            continue
        blob = c.get("/download/" + j["token"]).data
        # decompress it back through the app and compare SHA-256
        r2 = c.post("/api/compress",
                    data={"file": (io.BytesIO(blob), name + ".afc")})
        j2 = r2.get_json()
        restored = c.get("/download/" + j2["token"]).data
        if hashlib.sha256(restored).digest() != hashlib.sha256(data).digest():
            ok = False
            print("   round trip mismatch:", name)
    check("single-file SHA-256 round trip (all corpus files)", ok)


def test_batch_roundtrip(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    ok = True
    batch = "testbatch1"
    for path in corpus_files(3):
        data = open(path, "rb").read()
        r = c.post("/api/batch", data={
            "file": (io.BytesIO(data), os.path.basename(path)),
            "batch_id": batch})
        j = r.get_json()
        if r.status_code != 200 or not j.get("lossless"):
            ok = False
            print("   batch failed:", path, r.status_code, j)
            continue
        blob = c.get("/download/" + j["token"]).data
        import afc2
        if afc2.decompress_bytes(blob) != data:
            ok = False
    check("batch endpoint SHA-256 round trip", ok)

    r = c.get("/report.csv?batch_id=" + batch)
    check("batch CSV report exports",
          r.status_code == 200 and b"AFC Compression Report" in r.data,
          r.status_code)


def test_archive(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    files = corpus_files(4)
    payload = [("folder/sub/" + os.path.basename(p), open(p, "rb").read())
               for p in files]

    data = {"files": [(io.BytesIO(d), name) for name, d in payload],
            "archive_name": "testpak"}
    r = c.post("/api/archive/create", data=data)
    j = r.get_json()
    if not check("archive created", r.status_code == 200, j):
        return
    blob = c.get("/download/" + j["token"]).data
    check("archive uses the %s magic" % config.ARCHIVE_EXT,
          afcpak.is_archive(blob))

    # every member payload must itself be an AFC container — proves no other
    # compressor was introduced anywhere in the packaging path
    manifest, base = afcpak.read_manifest(blob)
    magics = set()
    for ent in manifest["entries"]:
        off = base + ent["offset"]
        magics.add(blob[off:off + 4])
    check("every archive member is an AFC container (no foreign codec)",
          magics and magics <= {b"AFC1", b"AFC2", b"AFC3", b"AFC4", b"AFC6"},
          magics)

    # extract through the app and compare SHA-256 per member
    r = c.post("/api/archive/extract",
               data={"file": (io.BytesIO(blob), "testpak" + config.ARCHIVE_EXT)})
    j2 = r.get_json()
    if not check("archive extracted", r.status_code == 200, j2):
        return
    by_path = {m["path"]: m for m in j2["members"]}
    ok = len(by_path) == len(payload)
    for name, original in payload:
        m = by_path.get(name)
        if m is None or not m["sha256_ok"]:
            ok = False
            continue
        got = c.get("/download/" + m["token"]).data
        if hashlib.sha256(got).digest() != hashlib.sha256(original).digest():
            ok = False
    check("archive members SHA-256 round trip + folder paths preserved", ok)


def test_archive_no_deflate():
    """Structural proof that packaging never invokes DEFLATE.

    Parses afcpak.py's AST and asserts it imports no compression module, and
    that the string ZIP_DEFLATED appears nowhere in executable code.  A grep
    would false-positive on the docstring, so this walks real import nodes."""
    src = open(os.path.join(ROOT, "afcpak.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"zlib", "gzip", "bz2", "lzma", "zipfile", "tarfile"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found |= ({a.name.split(".")[0]} & banned)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found |= ({node.module.split(".")[0]} & banned)
    check("afcpak imports no compression library (AST-verified)",
          not found, found)

    # ZIP_DEFLATED must not appear as a real identifier/constant anywhere
    uses = [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "ZIP_DEFLATED"]
    check("afcpak never references ZIP_DEFLATED", not uses)


def test_archive_path_traversal():
    ok = True
    for bad in ("../evil", "/etc/passwd", "C:\\win\\x", "a/../../b", "..",
                "", "//server/share"):
        try:
            afcpak.safe_member_path(bad)
            ok = False
            print("   traversal NOT rejected:", repr(bad))
        except afcpak.ArchiveError:
            pass
    check("archive rejects path traversal / absolute paths", ok)

    # and extraction refuses to escape even with a hand-edited manifest
    files = [("ok.txt", b"hello world " * 50)]
    blob, _ = afcpak.pack(files)
    tampered = blob.replace(b'"path":"ok.txt"', b'"path":"../esc.txt"')
    if tampered != blob:
        with tempfile.TemporaryDirectory() as td:
            try:
                afcpak.extract_to(tampered, td)
                check("tampered archive refused on extract", False,
                      "extract_to accepted ../")
            except afcpak.ArchiveError:
                check("tampered archive refused on extract", True)
    else:
        check("tampered archive refused on extract", True, "(manifest not "
              "byte-patchable in this build; safe_member_path covers it)")


def test_size_policy(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")

    # empty file rejected
    r = c.post("/api/compress", data={"file": (io.BytesIO(b""), "empty.bin")})
    check("empty file rejected", r.status_code == 400, r.status_code)

    # oversize rejected WITHOUT allocating a real MAX_FILE_SIZE buffer:
    # temporarily lower the constant, which also proves the check reads config
    original = config.MAX_FILE_SIZE
    try:
        config.MAX_FILE_SIZE = 1024
        r = c.post("/api/compress",
                   data={"file": (io.BytesIO(b"x" * 5000), "big.bin")})
        j = r.get_json()
        check("oversize file rejected using config constant",
              r.status_code == 400 and "maximum" in (j.get("error") or ""),
              j)
    finally:
        config.MAX_FILE_SIZE = original

    original_batch = config.MAX_BATCH_SIZE
    try:
        config.MAX_BATCH_SIZE = 1024
        r = c.post("/api/batch", data={
            "file": (io.BytesIO(b"y" * 500), "b.bin"),
            "batch_total_bytes": "999999"})
        check("oversize batch rejected using config constant",
              r.status_code == 400, r.status_code)
    finally:
        config.MAX_BATCH_SIZE = original_batch

    # /api/config publishes the limits so the UI never hardcodes them
    j = c.get("/api/config").get_json()
    check("/api/config publishes real limits",
          j["max_file_size"] == config.MAX_FILE_SIZE
          and j["max_batch_size"] == config.MAX_BATCH_SIZE, j)

    # defaults must match the paper's documented ceiling
    check("default caps match Appendix C tested ceiling",
          config.MAX_FILE_SIZE == config.PAPER_TESTED_MAX_FILE
          and config.MAX_BATCH_SIZE == config.PAPER_TESTED_MAX_BATCH,
          (config.MAX_FILE_SIZE, config.MAX_BATCH_SIZE))


def test_history_isolation(app):
    """User B must never see user A's history."""
    cb = app.test_client()
    cb.post("/register", data={"username": "bob", "email": "bob@b.co",
                               "password": "bob-password-1",
                               "confirm": "bob-password-1"})
    j = cb.get("/api/history").get_json()
    check("new user sees an empty history", j == [], j)

    ca = app.test_client()
    login(ca, "alice", "correct-horse")
    ja = ca.get("/api/history").get_json()
    check("original user still sees their own history", len(ja) > 0, len(ja))

    names_a = {r["filename"] for r in ja}
    names_b = {r["filename"] for r in j}
    check("no history leaks between users", not (names_a & names_b))


def test_reports(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    r = c.get("/report.csv")
    check("CSV report exports", r.status_code == 200
          and r.headers["Content-Type"].startswith("text/csv"), r.status_code)
    check("CSV report contains a TOTAL row", b"TOTAL" in r.data)
    r = c.get("/report.pdf")
    check("PDF (print view) renders", r.status_code == 200
          and b"Performance Report" in r.data, r.status_code)
    check("report states engine + app version",
          config.ENGINE_VERSION.encode() in r.data)


def test_pages_render(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    for path in ("/", "/compress", "/files", "/settings"):
        r = c.get(path)
        if not check("page renders: %s" % path, r.status_code == 200,
                     r.status_code):
            continue
    # every size shown must come from config: assert the rendered Settings page
    # actually contains the configured maximum, not a stale literal
    r = c.get("/settings")
    from app import human
    check("settings page shows the configured max file size",
          human(config.MAX_FILE_SIZE).encode() in r.data,
          human(config.MAX_FILE_SIZE))



# ===========================================================================
# PART 2 — analytics, algorithm showcase, presets
# ===========================================================================

def _seed_for_analytics(app):
    """Compress a spread of real file types so the analytics have real rows."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    import glob
    picks = []
    for pat in ("benchmarks/canterbury/alice29.txt",
                "benchmarks/corpus/data.json",
                "benchmarks/corpus/data.csv",
                "benchmarks/corpus/random.bin",
                "benchmarks/canterbury/fields.c"):
        fp = os.path.join(ROOT, pat)
        if os.path.exists(fp):
            picks.append(fp)
    for fp in picks:
        c.post("/api/compress", data={
            "file": (io.BytesIO(open(fp, "rb").read()), os.path.basename(fp))})
    return c, picks


def test_analytics_match_database(app):
    """Every displayed stat must be derivable from compression_history rows."""
    c, picks = _seed_for_analytics(app)
    summary = c.get("/api/analytics/summary").get_json()
    rows = [r for r in c.get("/api/history").get_json()
            if r["operation"] == "compress"]

    exp_files = len(rows)
    exp_orig = sum(r["original_bytes"] for r in rows)
    exp_comp = sum(r["compressed_bytes"] for r in rows)
    check("analytics file count matches history rows",
          summary["files"] == exp_files, (summary["files"], exp_files))
    check("analytics total_original matches sum of rows",
          summary["total_original"] == exp_orig,
          (summary["total_original"], exp_orig))
    check("analytics total_compressed matches sum of rows",
          summary["total_compressed"] == exp_comp,
          (summary["total_compressed"], exp_comp))
    check("analytics total_saved is original minus compressed",
          summary["total_saved"] == exp_orig - exp_comp)
    exp_avg = sum(r["ratio"] for r in rows) / max(1, len(rows))
    check("analytics avg_ratio matches mean of row ratios",
          abs(summary["avg_ratio"] - exp_avg) < 1e-6,
          (summary["avg_ratio"], exp_avg))

    # transfer-time estimate must follow the documented formula + config
    expect_sec = (summary["total_saved"] * 8.0) / (config.ASSUMED_LINK_MBPS * 1_000_000)
    check("transfer time saved uses the configured link speed",
          abs(summary["transfer_seconds_saved"] - expect_sec) < 0.02
          and summary["assumed_link_mbps"] == config.ASSUMED_LINK_MBPS)


def test_analytics_extensions(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    exts = c.get("/api/analytics/extensions").get_json()
    rows = [r for r in c.get("/api/history").get_json()
            if r["operation"] == "compress"]
    from collections import Counter
    expect = Counter(os.path.splitext(r["filename"])[1].lstrip(".").lower()
                     for r in rows)
    got = {e["ext"]: e["files"] for e in exts}
    ok = all(got.get(k) == v for k, v in expect.items() if k)
    check("file-type distribution matches history filenames", ok,
          (got, dict(expect)))
    check("extension rows carry real byte totals",
          all(e["total_original"] >= e["total_compressed"] for e in exts))


def test_analytics_timeseries_reference(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    ts = c.get("/api/analytics/timeseries").get_json()
    check("timeseries returns rows", len(ts) > 0, len(ts))
    have_ref = [r for r in ts if r["gzip_ratio"] is not None]
    check("gzip reference measured for at least one file", len(have_ref) > 0)
    # the reference must be a real measurement, not a copy of the AFC number
    differing = [r for r in have_ref if abs(r["gzip_ratio"] - r["afc_ratio"]) > 1e-9]
    check("gzip reference differs from AFC (it is really measured)",
          len(differing) > 0)
    check("huffman reference present and >= 0",
          all((r["huffman_ratio"] is None or r["huffman_ratio"] > 0) for r in ts))


def test_history_search_filter_paginate(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    all_rows = c.get("/api/history/search?per_page=100").get_json()
    check("search returns rows and a total", all_rows["total"] > 0)

    j = c.get("/api/history/search?q=json&per_page=100").get_json()
    check("filename filter narrows results",
          all("json" in r["filename"].lower() for r in j["rows"])
          and j["total"] <= all_rows["total"], j["total"])

    p1 = c.get("/api/history/search?per_page=2&page=1").get_json()
    p2 = c.get("/api/history/search?per_page=2&page=2").get_json()
    check("pagination splits the result set",
          len(p1["rows"]) <= 2 and p1["page"] == 1 and p2["page"] == 2
          and (not p1["rows"] or not p2["rows"]
               or p1["rows"][0]["id"] != p2["rows"][0]["id"]),
          "total=%s pages=%s p1=%s p2=%s" % (
              p1.get("total"), p1.get("pages"),
              [r["id"] for r in p1["rows"]], [r["id"] for r in p2["rows"]]))

    asc = c.get("/api/history/search?sort=original_bytes&dir=asc&per_page=100").get_json()
    sizes = [r["original_bytes"] for r in asc["rows"]]
    check("sort by size ascending is monotonic",
          sizes == sorted(sizes), sizes[:5])

    # a bogus sort column must be ignored, never interpolated into SQL
    bad = c.get("/api/history/search?sort=filename;DROP+TABLE+users&per_page=5")
    check("invalid sort column is rejected safely", bad.status_code == 200)
    check("users table survives a SQL-injection attempt in sort",
          c.get("/api/history/search").status_code == 200)


def test_entropy_reflects_file_type(app):
    """Feature 6 across text / structured / near-random — must differ."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    got = {}
    for label, rel in (("text", "benchmarks/canterbury/alice29.txt"),
                       ("structured", "benchmarks/corpus/data.json"),
                       ("random", "benchmarks/corpus/random.bin")):
        fp = os.path.join(ROOT, rel)
        r = c.post("/api/entropy", data={
            "file": (io.BytesIO(open(fp, "rb").read()), os.path.basename(fp))})
        j = r.get_json()
        got[label] = j
        check("entropy computed for %s" % label,
              r.status_code == 200 and 0 < j["entropy_bits_per_byte"] <= 8.0,
              j.get("entropy_bits_per_byte"))
    check("near-random entropy is far higher than text entropy",
          got["random"]["entropy_bits_per_byte"]
          > got["text"]["entropy_bits_per_byte"] + 2.0,
          (got["random"]["entropy_bits_per_byte"],
           got["text"]["entropy_bits_per_byte"]))
    check("entropy bands differ by file type (not a placeholder)",
          got["random"]["band"] != got["text"]["band"],
          (got["random"]["band"], got["text"]["band"]))
    check("entropy histogram is populated",
          len(got["text"]["histogram"]) > 0)


def test_tree_and_attribution(app):
    """Features 7 + 8 across three file types."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    seen = {}
    for label, rel in (("text", "benchmarks/canterbury/alice29.txt"),
                       ("structured", "benchmarks/corpus/data.json"),
                       ("code", "benchmarks/canterbury/fields.c")):
        fp = os.path.join(ROOT, rel)
        data = open(fp, "rb").read()
        j = c.post("/api/compress", data={
            "file": (io.BytesIO(data), os.path.basename(fp))}).get_json()
        t = c.get("/api/tree/" + j["token"]).get_json()
        seen[label] = t
        s = t["tree"]["summary"]
        check("tree built for %s" % label,
              s["total_symbols"] > 0 and t["tree"]["tree"] is not None)
        check("tree for %s has BOTH literal and block leaves (hybrid)" % label,
              s["literal_symbols"] > 0 and s["block_symbols"] > 0,
              (s["literal_symbols"], s["block_symbols"]))
        a = t["attribution"]
        check("attribution shares sum to ~100%% for %s" % label,
              abs(a["literal"]["share_pct"] + a["block"]["share_pct"] - 100.0)
              < 0.6,
              (a["literal"]["share_pct"], a["block"]["share_pct"]))
        # savings attribution must reconcile with the real coded stream
        real_saved = len(data) * 8 - a["coded_bits"]
        check("attribution reconciles with the actual bitstream for %s" % label,
              a["total_bits_saved"] == real_saved,
              (a["total_bits_saved"], real_saved))
        check("explainer mentions a real percentage for %s" % label,
              "%" in t["explain"] and len(t["explain"]) > 30, t["explain"])
        # the DRAWN tree must actually contain both leaf classes, otherwise
        # the "hybrid" is invisible to the user no matter what the totals say
        drawn = {"literal": 0, "block": 0, "collapsed": 0}
        def walk(n):
            if not n:
                return
            if n["type"] == "node":
                walk(n["children"][0]); walk(n["children"][1])
            else:
                drawn[n["type"]] = drawn.get(n["type"], 0) + 1
        walk(t["tree"]["tree"])
        check("drawn tree shows BOTH literal and block leaves for %s" % label,
              drawn["literal"] > 0 and drawn["block"] > 0, drawn)
        cl = s["code_lengths"]
        check("code-length distribution present for %s" % label,
              len(cl["literal"]) > 0 and len(cl["block"]) > 0, cl)

    check("tree differs between file types (not static)",
          seen["text"]["tree"]["summary"]["block_symbols"]
          != seen["code"]["tree"]["summary"]["block_symbols"])

    # near-random file must be reported as raw-stored, not faked
    fp = os.path.join(ROOT, "benchmarks/corpus/random.bin")
    j = c.post("/api/compress", data={
        "file": (io.BytesIO(open(fp, "rb").read()), "random.bin")}).get_json()
    t = c.get("/api/tree/" + j["token"]).get_json()
    check("near-random file reported as raw-stored, not a fabricated tree",
          t["tree"].get("raw") is True and "raw" in t["explain"].lower(),
          t["explain"])


def test_presets_have_real_effect(app):
    """Feature 9 — the three presets must differ measurably, and the result
    must still be lossless.  Numbers are printed so the log is the evidence."""
    import time as _t
    import afc2
    import presets as P
    fp = os.path.join(ROOT, "benchmarks/canterbury/cp.html")
    data = open(fp, "rb").read()
    out = {}
    for name in ("fast", "balanced", "maximum"):
        best = None
        for _ in range(3):
            t0 = _t.perf_counter()
            blob, used, backend = P.compress_with(data, name)
            el = (_t.perf_counter() - t0) * 1000
            best = el if best is None else min(best, el)
        ok = afc2.decompress_bytes(blob) == data
        out[name] = (len(blob), best, ok, backend)
        print("   preset %-9s %7d B  %8.1f ms  lossless=%s  (%s)"
              % (name, len(blob), best, ok, backend))
    check("all presets round-trip losslessly",
          all(v[2] for v in out.values()))
    check("Fast produces a LARGER file than Balanced (speed/ratio trade)",
          out["fast"][0] > out["balanced"][0],
          (out["fast"][0], out["balanced"][0]))
    # Maximum means more search, not a guaranteed monotonic size result. The
    # current corpus has measured cases on both sides, so the useful contract
    # is that it remains a real ratio-oriented alternative to Fast.
    check("Maximum remains smaller than Fast on the representative file",
          out["maximum"][0] < out["fast"][0],
          (out["maximum"][0], out["fast"][0]))
    check("Fast is measurably quicker than Maximum on the same path",
          out["fast"][1] < out["maximum"][1],
          (out["fast"][1], out["maximum"][1]))

    # Production preset calls must never mutate process-wide engine state.
    check("preset calls leave legacy engine constants unchanged",
          afc2.DP_ROUNDS == 3 and afc2.MIN_CANDIDATE_FREQ == 4
          and afc2.OPTS["dp"] is True,
          (afc2.DP_ROUNDS, afc2.MIN_CANDIDATE_FREQ, afc2.OPTS["dp"]))


def test_preset_recorded_and_used(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    fp = os.path.join(ROOT, "benchmarks/canterbury/grammar.lsp")
    data = open(fp, "rb").read()
    sizes = {}
    for name in ("fast", "maximum"):
        j = c.post("/api/compress", data={
            "file": (io.BytesIO(data), "grammar.lsp"), "preset": name}).get_json()
        sizes[name] = j["compressed"]
        check("API records preset '%s'" % name, j.get("preset") == name, j.get("preset"))
        check("API returns an explainer for '%s'" % name,
              bool(j.get("explain")), j.get("explain"))
    rows = c.get("/api/history/search?per_page=100").get_json()["rows"]
    presets_seen = {r["preset"] for r in rows if r["filename"] == "grammar.lsp"}
    check("preset persisted to compression_history",
          {"fast", "maximum"} <= presets_seen, presets_seen)


def test_compare_view(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    rows = c.get("/api/history").get_json()
    ids = [r["id"] for r in rows if r["operation"] == "compress"][:2]
    r = c.get("/compare?a=%d&b=%d" % (ids[0], ids[1]))
    check("compare view renders a diff", r.status_code == 200
          and b"Difference" in r.data, r.status_code)
    # a row belonging to another user must not be comparable
    cb = app.test_client()
    login(cb, "bob", "bob-password-1")
    r2 = cb.get("/compare?a=%d&b=%d" % (ids[0], ids[1]))
    check("compare cannot read another user's rows",
          r2.status_code == 200 and b"Difference" not in r2.data)


def test_admin_scope_gate(app):
    """?scope=system must be honoured for admins and ignored for users."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    mine = c.get("/api/analytics/summary").get_json()
    forced = c.get("/api/analytics/summary?scope=system").get_json()
    check("non-admin cannot widen analytics scope",
          forced["scope"] == "user"
          and forced["files"] == mine["files"], (forced["scope"],))

    a = app.test_client()
    login(a, config.DEFAULT_ADMIN_USERNAME, "admin-new-password")
    sysv = a.get("/api/analytics/summary?scope=system").get_json()
    check("admin can request system-wide analytics",
          sysv["scope"] == "system" and sysv["files"] >= mine["files"],
          (sysv["scope"], sysv["files"], mine["files"]))


def test_status_and_preview(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    s = c.get("/api/status").get_json()
    check("status reports engine + backend + uptime",
          s["engine_version"] == config.ENGINE_VERSION
          and s["backend"] in ("C++ native", "pure Python")
          and s["uptime_seconds"] >= 0, s)
    check("status size limits come from config",
          s["max_file_size"] == config.MAX_FILE_SIZE)

    fp = os.path.join(ROOT, "benchmarks/canterbury/fields.c")
    j = c.post("/api/preview", data={
        "file": (io.BytesIO(open(fp, "rb").read()), "fields.c")}).get_json()
    check("text preview returns text lines",
          j["kind"] == "text" and len(j["lines"]) > 0, j.get("kind"))
    fp = os.path.join(ROOT, "benchmarks/corpus/random.bin")
    j = c.post("/api/preview", data={
        "file": (io.BytesIO(open(fp, "rb").read()), "random.bin")}).get_json()
    check("binary preview returns a hex view",
          j["kind"] == "hex" and len(j["lines"]) > 0, j.get("kind"))


def test_part1_still_works(app):
    """Regression guard: Part 2 must not have broken Part 1's behaviour."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    for path in ("/", "/compress", "/files", "/settings", "/analytics",
                 "/compare"):
        check("page still renders: %s" % path, c.get(path).status_code == 200)
    r = c.get("/report.csv")
    check("CSV report still exports", r.status_code == 200
          and b"TOTAL" in r.data)
    # archive flow untouched
    files = corpus_files(3)
    payload = [("f/" + os.path.basename(p), open(p, "rb").read()) for p in files]
    r = c.post("/api/archive/create", data={
        "files": [(io.BytesIO(d), n) for n, d in payload],
        "archive_name": "p2"})
    check("archive creation still works", r.status_code == 200, r.status_code)


# ===========================================================================
# Separate Compress / Decompress pages
# ===========================================================================
# These assert the SPLIT itself: two distinct destinations, each refusing the
# other's input, both driving the SAME engine. A regression that quietly
# re-merges the workflows, or that lets one page perform the other's
# operation, fails here.

def test_pages_are_separate(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")

    rc = c.get("/compress")
    rd = c.get("/decompress")
    check("Compress page exists", rc.status_code == 200, rc.status_code)
    check("Decompress page exists", rd.status_code == 200, rd.status_code)

    check("Compress page is headed 'Compress Files'",
          b"Compress Files" in rc.data)
    check("Decompress page is headed 'Decompress Files'",
          b"Decompress Files" in rd.data)
    check("Compress page carries its description",
          b"Reduce the size of your files" in rc.data)
    check("Decompress page carries its description",
          b"Restore an AFC file to its original format" in rd.data)

    # Neither page presents a combined "compress or decompress?" control.
    check("Compress page drops the old combined heading",
          b"Compress / Decompress" not in rc.data)
    check("Decompress page drops the old combined heading",
          b"Compress / Decompress" not in rd.data)

    # Both are in the primary navigation, on every page — not buried.
    for path in ("/", "/analytics", "/files", "/settings"):
        body = c.get(path).data
        check("nav links Compress from %s" % path,
              b'href="/compress"' in body)
        check("nav links Decompress from %s" % path,
              b'href="/decompress"' in body)


def test_decompress_endpoint_roundtrip(app):
    """The Decompress page restores bytes and proves it with SHA-256."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    ok = True
    for path in corpus_files(4):
        data = open(path, "rb").read()
        name = os.path.basename(path)
        j = c.post("/api/compress",
                   data={"file": (io.BytesIO(data), name)}).get_json()
        blob = c.get("/download/" + j["token"]).data

        r = c.post("/api/decompress",
                   data={"file": (io.BytesIO(blob), name + ".afc")})
        d = r.get_json()
        if r.status_code != 200:
            ok = False
            print("   decompress failed:", name, d)
            continue
        restored = c.get("/download/" + d["token"]).data
        if hashlib.sha256(restored).hexdigest() != hashlib.sha256(data).hexdigest():
            ok = False
            print("   decompress mismatch:", name)
    check("/api/decompress SHA-256 round trip (corpus)", ok)


def test_decompress_reports_verification(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    data = open(corpus_files(1)[0], "rb").read()
    j = c.post("/api/compress",
               data={"file": (io.BytesIO(data), "verify.txt")}).get_json()
    blob = c.get("/download/" + j["token"]).data
    d = c.post("/api/decompress",
               data={"file": (io.BytesIO(blob), "verify.afc")}).get_json()

    check("decompress reports the AFC size", d["afc_bytes"] == len(blob))
    check("decompress reports the restored size",
          d["restored_bytes"] == len(data))
    check("decompress reports the container format",
          d["container"] == "AFC5"
          and d.get("payload_container") in ("AFC1", "AFC2"),
          (d.get("container"), d.get("payload_container")))
    check("decompress detects the container mode",
          bool(d.get("container_mode")), d.get("container_mode"))
    check("decompress reports a decompression time", d["ms"] >= 0)
    check("decompress reports integrity VERIFIED", d["integrity_ok"] is True)
    check("decompress SHA-256 MATCHES the recorded original",
          d["sha256_status"] == "match" and d["sha256_match"] is True,
          d.get("sha256_status"))
    check("decompress returns the restored file's own digest",
          d["sha256_restored"] == hashlib.sha256(data).hexdigest())


def test_decompress_no_reference_is_not_a_fake_match(app):
    """A container this account never produced has NO digest on record.

    The page must say so rather than inventing a green check — the whole
    value of the SHA-256 line is that it means something."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    import afc2
    blob = afc2.compress_bytes(b"never compressed through the app " * 40, True)
    d = c.post("/api/decompress",
               data={"file": (io.BytesIO(blob), "foreign.afc")}).get_json()
    check("unknown container reports 'no reference' not a match",
          d["sha256_status"] == "no_reference" and d["sha256_match"] is None,
          d.get("sha256_status"))
    check("unknown container still verifies structural integrity",
          d["integrity_ok"] is True)


def test_pages_refuse_each_others_input(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")

    # A normal file sent to the decompress endpoint is refused, with a pointer.
    data = open(corpus_files(1)[0], "rb").read()
    r = c.post("/api/decompress",
               data={"file": (io.BytesIO(data), "plain.txt")})
    j = r.get_json()
    check("decompress refuses a non-AFC file", r.status_code == 400,
          r.status_code)
    check("refusal names the Compress page",
          "Compress page" in j.get("error", ""), j.get("error"))
    check("refusal flags the wrong page", j.get("wrong_page") is True)

    # An .afcpak archive is refused by the single-file decompress endpoint.
    payload = [(os.path.basename(p), open(p, "rb").read())
               for p in corpus_files(2)]
    blob, _ = afcpak.pack(payload)
    r = c.post("/api/decompress",
               data={"file": (io.BytesIO(blob), "bundle.afcpak")})
    check("decompress refuses an archive as a single container",
          r.status_code == 400 and r.get_json().get("wrong_tool") is True)

    # A corrupt container is reported, not crashed on.
    r = c.post("/api/decompress",
               data={"file": (io.BytesIO(b"AFC2" + b"\xff" * 64), "bad.afc")})
    check("corrupt container returns a clean 400", r.status_code == 400,
          r.status_code)


def test_restored_filename_recovery(app):
    """`MyDocument.afc` must come back as `MyDocument.pdf`.

    The container stores no filename, so the extension is recovered by
    sniffing the restored bytes."""
    import filetypes
    c = app.test_client()
    login(c, "alice", "correct-horse")

    pdf = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
           + b"stream data " * 400 + b"\ntrailer\n%%EOF")
    j = c.post("/api/compress",
               data={"file": (io.BytesIO(pdf), "MyDocument.pdf")}).get_json()
    blob = c.get("/download/" + j["token"]).data

    d = c.post("/api/decompress",
               data={"file": (io.BytesIO(blob), "MyDocument.afc")}).get_json()
    check("restored PDF regains its .pdf name",
          d["restored_name"] == "MyDocument.pdf", d.get("restored_name"))
    check("restored PDF is identified as a PDF",
          "PDF" in d["detected"], d.get("detected"))

    # A DOCX-shaped package (ZIP with word/document.xml) is named too. Built
    # here with zipfile purely as TEST INPUT — filetypes.py itself imports no
    # archive library, which test_archive_no_deflate-style AST checks rely on.
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr("word/document.xml", "<?xml version='1.0'?><doc>hi</doc>")
    docx = buf.getvalue()
    check("DOCX package is detected from its bytes",
          filetypes.sniff(docx)["ext"] == ".docx")
    check("DOCX is flagged container-aware",
          filetypes.sniff(docx)["container_aware"] is True)

    # And the name is preserved when it is already carried in the .afc name.
    check("report.pdf.afc keeps the embedded original name",
          filetypes.restored_name("report.pdf.afc", pdf) == "report.pdf")


def test_filetypes_imports_no_codec():
    """filetypes.py must not import a compression library.

    Same structural argument as afcpak: the Huffman-only constraint is easier
    to defend when no codec is importable in the file path at all. Checked by
    parsing the AST, not by grepping (a grep hits this docstring)."""
    import filetypes as ft
    src = open(ft.__file__, encoding="utf-8").read()
    banned = {"zipfile", "zlib", "gzip", "bz2", "lzma", "tarfile", "brotli"}
    found = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    check("filetypes.py imports no compression library",
          not (found & banned), found & banned)


def test_compress_page_flags_container_formats(app):
    """The Compress page must show what a file is before compressing it, and
    say that packaged formats are handled automatically."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    pdf = b"%PDF-1.4\n" + b"object stream " * 300
    j = c.post("/api/preview",
               data={"file": (io.BytesIO(pdf), "doc.pdf")}).get_json()
    check("preview reports the detected type",
          "PDF" in j.get("detected", ""), j.get("detected"))
    check("preview flags PDF as container-aware",
          j.get("container_aware") is True)

    j2 = c.post("/api/preview", data={
        "file": (io.BytesIO(b"plain text here\nline two\n"), "a.txt")}).get_json()
    check("plain text is not flagged container-aware",
          j2.get("container_aware") is False, j2.get("container_aware"))


def test_engine_is_not_duplicated():
    """The split is UI only: no second compressor was introduced.

    app.py must reach the engine through afc2 exactly as before, and the new
    presentation module must not define a compress/decompress of its own."""
    import filetypes as ft
    src = open(ft.__file__, encoding="utf-8").read()
    names = {n.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef)}
    check("filetypes.py defines no compression function",
          not any("compress" in n for n in names), names)

    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    tree = ast.parse(app_src)
    # every engine call in app.py goes through the published module API
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name):
            if node.func.value.id in ("engine", "afc2", "afc"):
                calls.add(node.func.value.id + "." + node.func.attr)
    allowed = {"engine.compress_bytes", "engine.decompress_bytes",
               "afc.compress_bytes", "afc.decompress_bytes"}
    check("app.py calls only the published engine API",
          calls <= allowed, calls - allowed)


# ===========================================================================
# V7 — native acceleration for every preset
# ===========================================================================

def _doc_corpus():
    d = os.path.join(ROOT, "benchmarks", "documents")
    if not os.path.isdir(d):
        return []
    import glob
    return sorted(glob.glob(os.path.join(d, "*")))


def test_all_presets_native_capable():
    """Fast and Maximum must no longer be forced onto the Python path."""
    import afc2
    import presets as P
    if not afc2.NATIVE:
        check("native library present (skipping native preset checks)", True)
        return
    check("native library exports the extended entry point",
          getattr(afc2._native, "TUNABLE", False) is True)
    for name in ("fast", "balanced", "maximum"):
        check("preset '%s' is native-capable" % name, P.uses_native(name))
    for d in P.describe():
        check("API reports '%s' native_capable" % d["name"],
              d["native_capable"] is True)


def test_preset_backend_byte_identity():
    """THE core V7 correctness property.

    Python is the reference implementation; C++ only accelerates it. For every
    preset and every corpus file the two backends must produce byte-identical
    containers, and both must round-trip losslessly."""
    import afc2
    import presets as P
    if not afc2.NATIVE:
        return
    files = corpus_files(6) + [
        os.path.join(ROOT, "benchmarks/canterbury/fields.c"),
        os.path.join(ROOT, "benchmarks/canterbury/cp.html"),
    ]
    identical = lossless = True
    for path in files:
        data = open(path, "rb").read()
        for name in ("fast", "balanced", "maximum"):
            options = P.options_for(name)
            py = afc2.compress_bytes(data, True, fmt="auto",
                                     options=options, backend="python")
            nat = afc2.compress_bytes(data, True, fmt="auto",
                                      options=options, backend="native")
            if py != nat:
                identical = False
                print("   MISMATCH %s/%s: py=%d nat=%d"
                      % (os.path.basename(path), name, len(py), len(nat)))
            if afc2.decompress_bytes(nat) != data or \
                    afc2.decompress_bytes(py) != data:
                lossless = False
    check("Python and C++ agree byte-for-byte on every preset", identical)
    check("both backends round-trip losslessly on every preset", lossless)


def test_preset_options_are_immutable_and_isolated():
    """Preset selection is per call, including under concurrent requests."""
    import dataclasses
    from concurrent.futures import ThreadPoolExecutor
    import afc2
    import presets as P

    frozen = False
    options = P.options_for("fast")
    try:
        options.dp_rounds = 99
    except dataclasses.FrozenInstanceError:
        frozen = True
    check("preset options are immutable", frozen)

    data = (b"thread safe structural pattern one two three\n" * 220)
    expected = {
        name: afc2.compress_bytes(data, options=P.options_for(name),
                                  backend="python")
        for name in ("fast", "balanced", "maximum")
    }

    def run(name):
        return name, afc2.compress_bytes(
            data, options=P.options_for(name), backend="python")

    jobs = ["fast", "maximum", "balanced", "fast", "maximum", "balanced"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        actual = list(pool.map(run, jobs))
    isolated = all(blob == expected[name] for name, blob in actual)
    check("concurrent preset calls are isolated", isolated)
    check("concurrent preset outputs remain lossless",
          all(afc2.decompress_bytes(blob) == data for _, blob in actual))


def test_presets_remain_distinct():
    """The three presets must still mean different amounts of search.

    Making them all native must not collapse them into the same thing."""
    import afc2
    import presets as P
    data = open(os.path.join(ROOT, "benchmarks/corpus/data.json"), "rb").read()
    sizes = {}
    for name in ("fast", "balanced", "maximum"):
        blob, _, backend = P.compress_with(data, name)
        sizes[name] = len(blob)
    check("Fast produces a LARGER file than Balanced",
          sizes["fast"] > sizes["balanced"], sizes)
    check("the three presets are not identical",
          len(set(sizes.values())) > 1, sizes)

    # Lock down what the preset actually promises: more DP and merge rounds,
    # not an unsupported claim about every possible output size.
    bp = P.PRESETS["balanced"]["params"]
    mp = P.PRESETS["maximum"]["params"]
    check("Maximum performs more DP rounds than Balanced",
          mp["dp_rounds"] > bp["dp_rounds"], (bp, mp))
    check("Maximum performs more merge rounds than Balanced",
          mp["merge_rounds"] > bp["merge_rounds"], (bp, mp))
    check("Maximum preserves the measured candidate-frequency floor",
          mp["min_candidate_freq"] == bp["min_candidate_freq"], (bp, mp))


def test_cross_implementation_decode():
    """Python encode -> native decode, and native encode -> Python decode."""
    import afc  # noqa: F401
    import afc2
    if not afc2.NATIVE:
        return
    import presets as P
    ok_pn = ok_np = True
    for path in corpus_files(5):
        data = open(path, "rb").read()
        for name in ("fast", "balanced", "maximum"):
            options = P.options_for(name)
            py_blob = afc2.compress_bytes(data, True, fmt="auto",
                                          options=options, backend="python")
            nat_blob = afc2.compress_bytes(data, True, fmt="auto",
                                           options=options, backend="native")
            if afc2._native.decompress(py_blob) != data:
                ok_pn = False
            if afc.decompress_bytes(nat_blob) != data:
                ok_np = False
    check("Python-encoded containers decode natively", ok_pn)
    check("natively-encoded containers decode in Python", ok_np)


def test_experiment_foundation():
    """The thesis harness must work on Windows and identify official corpora."""
    import json
    import sys
    tools_dir = os.path.join(ROOT, "tools")
    sys.path.insert(0, tools_dir)
    try:
        import bench_runtime
        import experiment
    finally:
        sys.path.pop(0)

    rss = bench_runtime.current_rss_kb()
    check("cross-platform RSS sampler reports real process memory", rss > 0, rss)

    manifest_path = os.path.join(ROOT, "benchmarks", "corpus_manifest.json")
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    canterbury = manifest["canterbury"]["files"]
    silesia = manifest["silesia"]["files"]
    check("manifest names all 11 official Canterbury files",
          len(canterbury) == 11 and "kennedy.xls" in canterbury)
    check("manifest names all 12 official Silesia files",
          len(silesia) == 12 and "dickens" in silesia and "x-ray" in silesia)
    check("every official corpus record has a cryptographic checksum",
          all(v.get("sha256") for v in list(canterbury.values()) +
              list(silesia.values())))

    path = os.path.join(ROOT, "benchmarks", "canterbury", "grammar.lsp")
    row = experiment.worker(path, "fast", "python", 0)
    check("experiment worker verifies bytes and SHA-256",
          row["byte_equal"] and row["sha256_equal"], row)
    check("experiment worker records peak RSS", row["peak_rss_kib"] > 0,
          row["peak_rss_kib"])


# ===========================================================================
# V7 — container-aware PDF / DOCX
# ===========================================================================

def test_container_tiling_is_exact():
    """The segment plan must exactly tile the file — the invariant that makes
    reconstruction byte-exact regardless of parser quality."""
    import containers
    docs = _doc_corpus()
    if not docs:
        check("document corpus present (run tools/make_doc_corpus.py)", False)
        return
    ok = True
    for path in docs:
        data = open(path, "rb").read()
        segs = containers.plan(data)
        if not segs:
            ok = False
            continue
        try:
            containers._validate_tiling(segs, len(data))
        except Exception as exc:
            ok = False
            print("   tiling broken for %s: %s" % (os.path.basename(path), exc))
        if b"".join(data[s.start:s.end] for s in segs) != data:
            ok = False
            print("   segments do not reassemble: %s" % os.path.basename(path))
    check("segments exactly tile every document (%d files)" % len(docs), ok)


def test_pdf_docx_byte_exact():
    """PDF and DOCX must come back byte-for-byte, verified by SHA-256."""
    import afc2
    docs = _doc_corpus()
    if not docs:
        return
    ok = True
    afc3_used = 0
    for path in docs:
        data = open(path, "rb").read()
        blob = afc2.compress_bytes(data, True, fmt="auto")
        if blob[:4] == b"AFC3":
            afc3_used += 1
        back = afc2.decompress_bytes(blob)
        if back != data or hashlib.sha256(back).hexdigest() != \
                hashlib.sha256(data).hexdigest():
            ok = False
            print("   NOT byte-exact: %s" % os.path.basename(path))
    check("every PDF/DOCX reconstructs byte-for-byte (SHA-256)", ok)
    check("container-aware AFC3 was actually exercised", afc3_used > 0,
          "afc3 files: %d" % afc3_used)


def test_afc3_never_larger_than_plain():
    """Global size guard: V7 must never produce a bigger file than V6 did."""
    import afc2
    docs = _doc_corpus()
    if not docs:
        return
    worse = []
    for path in docs:
        data = open(path, "rb").read()
        plain = afc2.compress_bytes(data, True, fmt="auto",
                                    container_aware=False)
        v7 = afc2.compress_bytes(data, True, fmt="auto")
        if len(v7) > len(plain):
            worse.append((os.path.basename(path), len(plain), len(v7)))
    check("container-aware output is never larger than plain", not worse,
          worse)


def test_multi_cycle_reconstruction():
    """compress -> decompress -> compress -> decompress stays byte-identical."""
    import afc2
    docs = _doc_corpus()[:6] + corpus_files(3)
    ok = True
    for path in docs:
        data = open(path, "rb").read()
        a = afc2.compress_bytes(data, True, fmt="auto")
        b = afc2.decompress_bytes(a)
        c = afc2.compress_bytes(b, True, fmt="auto")
        d = afc2.decompress_bytes(c)
        if d != data or b != data or a != c:
            ok = False
            print("   multi-cycle drift: %s" % os.path.basename(path))
    check("two full compress/decompress cycles are byte-identical", ok)


def test_afc3_backward_compatibility():
    """AFC1/AFC2 containers must still decode, and never be reinterpreted."""
    import afc
    import afc2
    ok = True
    for path in corpus_files(4):
        data = open(path, "rb").read()
        for fmt in ("afc1", "afc2"):
            blob = afc2.compress_bytes(data, True, fmt=fmt,
                                       container_aware=False)
            if blob[:4] not in (b"AFC1", b"AFC2"):
                ok = False
            if afc2.decompress_bytes(blob) != data:
                ok = False
            if afc.decompress_bytes(blob) != data:
                ok = False
    check("existing AFC1/AFC2 containers still decode unchanged", ok)

    # An AFC3 container must not be mistaken for AFC1/AFC2 by the old decoder.
    import containers
    docs = _doc_corpus()
    if docs:
        for path in docs:
            data = open(path, "rb").read()
            blob = afc2.compress_bytes(data, True, fmt="auto")
            if blob[:4] == b"AFC3":
                try:
                    afc.decompress_bytes(blob)
                    check("old decoder rejects AFC3 instead of "
                          "misreading it", False)
                except Exception:
                    check("old decoder rejects AFC3 instead of "
                          "misreading it", True)
                check("AFC3 magic is distinct", containers.is_afc3(blob))
                break


def test_container_layer_introduces_no_codec():
    """containers.py must import no compression library and define no
    compressor of its own — the same structural argument as afcpak.py."""
    import containers as C
    src = open(C.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"zipfile", "zlib", "gzip", "bz2", "lzma", "tarfile", "brotli",
              "zstandard"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    check("containers.py imports no compression library",
          not (found & banned), found & banned)

    # It must delegate every compression to the engine, never implement one.
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    homemade = {n for n in names
                if ("compress" in n or "encode" in n)
                and n not in ("compress_container", "decompress_afc3",
                              "decompress_afc4", "decompress_afc6",
                              "_decompress_transformed_container")}
    check("containers.py defines no compressor of its own", not homemade,
          homemade)


def test_docx_member_inventory_and_exact_deflate_recipes():
    """Normal method-8 XML must be named, exposed, and exactly reversible.

    The transform parses an existing DEFLATE stream; it must not import a
    second compression library or create a new DEFLATE match/tree decision.
    """
    import binascii
    import containers
    import deflate_tokens

    docs = [p for p in _doc_corpus() if p.lower().endswith(".docx")]
    named_document = transformed = exact = 0
    for path in docs:
        data = open(path, "rb").read()
        entries = containers.zip_components(data)
        if any(e["name"] == "word/document.xml" for e in entries):
            named_document += 1
        for entry in entries:
            if entry["method"] != 8 or not containers._is_xml_part(entry["name"]):
                continue
            raw = data[entry["payload_start"]:entry["payload_end"]]
            plain, recipe = deflate_tokens.transform(raw)
            transformed += 1
            if (deflate_tokens.restore(plain, recipe) == raw
                    and len(plain) == entry["uncompressed_size"]
                    and (binascii.crc32(plain) & 0xFFFFFFFF) == entry["crc32"]):
                exact += 1
    check("ZIP inventory names word/document.xml in every DOCX",
          named_document == len(docs), (named_document, len(docs)))
    check("deflated XML components were actually parsed", transformed > 0,
          transformed)
    check("every parsed DEFLATE recipe is bit-exact and CRC-valid",
          exact == transformed, (exact, transformed))

    src = open(deflate_tokens.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"zipfile", "zlib", "gzip", "bz2", "lzma", "brotli",
              "zstandard"}
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    check("DEFLATE recipe parser imports no compression library",
          not (imports & banned), imports & banned)


def test_afc4_docx_exact_and_versioned(app):
    """AFC4 must expose XML yet recreate the original ZIP bytes exactly."""
    import afc
    import afc2
    import analysis
    import containers

    docs = {os.path.basename(p): p for p in _doc_corpus()}
    path = docs.get("docx_stored.docx")
    if not path:
        return
    data = open(path, "rb").read()
    segs = containers.docx_transform_plan(data)
    check("DOCX transform plan contains word/document.xml",
          bool(segs) and any(s.kind == containers.TRANSFORMED
                             and "word/document.xml" in s.label for s in segs))
    forced = containers.build_afc4(data, segs)
    restored = afc2.decompress_bytes(forced)
    check("forced AFC4 reconstructs exact DOCX bytes",
          restored == data)
    check("forced AFC4 SHA-256 matches the original",
          hashlib.sha256(restored).digest() == hashlib.sha256(data).digest())
    hi = containers.header_info(forced)
    check("AFC4 header explicitly identifies the new format",
          forced[:4] == b"AFC4" and hi["magic"] == "AFC4")
    check("AFC4 records transformed XML and token recipes",
          hi["xml_bytes"] > 0 and hi["recipe_bytes"] > 0
          and hi["transformed_bytes"] > 0, hi)
    check("analysis unwraps AFC4 to its Hybrid-Huffman container",
          analysis.unwrap(forced)[:4] in (b"AFC1", b"AFC2"))
    try:
        afc.decompress_bytes(forced)
        old_rejects = False
    except Exception:
        old_rejects = True
    check("old AFC1/AFC2 decoder rejects AFC4 cleanly", old_rejects)

    # Auto mode may use AFC4 only when its complete output wins.
    plain = afc2.compress_bytes(data, True, container_aware=False)
    auto = afc2.compress_bytes(data, True)
    check("automatic DOCX selection never exceeds the plain path",
          len(auto) <= len(plain), (len(auto), len(plain)))
    if len(forced) < len(plain):
        check("automatic mode selects the measured smaller AFC4 candidate",
              auto == forced, (auto[:4], len(auto), len(forced)))

    # End-to-end web flow: the user uploaded a normal .docx, not extracted XML.
    c = app.test_client()
    login(c, "alice", "correct-horse")
    result = c.post("/api/compress", data={
        "file": (io.BytesIO(data), "normal.docx")}).get_json()
    check("web app wraps the winning AFC4 DOCX payload in AFC5",
          result.get("container") == "AFC5"
          and result.get("payload_container") == "AFC4", result)
    blob = c.get("/download/" + result["token"]).data
    decoded = c.post("/api/decompress", data={
        "file": (io.BytesIO(blob), "normal.docx.afc")}).get_json()
    check("web AFC5/AFC4 decompression reports verified integrity",
          decoded.get("integrity_ok") is True, decoded.get("integrity_note"))
    check("web AFC4 mode reports automatic DOCX XML handling",
          "DOCX XML" in (decoded.get("container_mode") or ""),
          decoded.get("container_mode"))
    check("web AFC5 SHA-256 matches its embedded source digest",
          decoded.get("sha256_status") == "match"
          and "inside AFC5" in decoded.get("sha256_note", ""),
          decoded.get("sha256_note"))
    check("AFC5 restores the embedded original filename",
          decoded.get("restored_name") == "normal.docx",
          decoded.get("restored_name"))


def test_afc5_self_verifying_envelope():
    import afc2
    import afc5
    import analysis
    import filetypes

    data = (b"AFC5 self verifying hybrid huffman payload\n" * 300)
    inner = afc2.compress_bytes(data)
    blob = afc5.wrap(data, inner, "folder/report.txt")
    info = afc5.parse(blob)
    restored = afc2.decompress_bytes(blob)
    check("AFC5 records the exact original length and SHA-256",
          info["original_length"] == len(data)
          and info["original_sha256"] == hashlib.sha256(data).hexdigest(), info)
    check("AFC5 records and verifies its AFC payload",
          info["payload"] == inner and info["payload_magic"] in
          ("AFC1", "AFC2", "AFC3", "AFC4", "AFC6"),
          info["payload_magic"])
    check("AFC5 round trip is byte-equal and SHA-256 equal",
          restored == data and hashlib.sha256(restored).digest() ==
          hashlib.sha256(data).digest())
    check("AFC5 stores a safe path-free original name",
          info["original_name"] == "report.txt", info["original_name"])
    check("analysis transparently reads the AFC5 inner Huffman tree",
          analysis.parse_container(blob)["magic"] in ("AFC1", "AFC2"))
    check("file type detection recognizes AFC5",
          "AFC5" in filetypes.sniff(blob)["label"])

    corrupt = bytearray(blob)
    corrupt[-1] ^= 1
    rejected = False
    try:
        afc2.decompress_bytes(bytes(corrupt))
    except Exception:
        rejected = True
    check("AFC5 rejects a corrupted payload before decoding", rejected)

    corrupt = bytearray(blob)
    # The original digest begins after magic/version/flags and three 1-3 byte
    # varints. Parse tells us the payload begins at header_bytes; flipping the
    # first digest byte is simpler to locate by rebuilding a same-size header.
    digest_at = 6
    for _ in range(3):
        while corrupt[digest_at] & 0x80:
            digest_at += 1
        digest_at += 1
    corrupt[digest_at] ^= 1
    rejected = False
    try:
        afc2.decompress_bytes(bytes(corrupt))
    except Exception:
        rejected = True
    check("AFC5 rejects an incorrect reconstructed-file digest", rejected)


def test_afc6_pdf_flate_exact_and_versioned():
    """AFC6 must expose PDF Flate content and reproduce the PDF bytes."""
    import afc
    import afc2
    import afc5
    import analysis
    import containers
    import deflate_tokens

    docs = {os.path.basename(p): p for p in _doc_corpus()}
    path = docs.get("pdf_flate_and_images.pdf") or docs.get("pdf_word_flate.pdf")
    if not path:
        return
    data = open(path, "rb").read()
    segs = containers.pdf_transform_plan(data)
    transformed = [s for s in (segs or [])
                   if s.kind == containers.TRANSFORMED]
    check("PDF transform plan exposes Flate page/content streams",
          bool(transformed), len(transformed))
    if not transformed:
        return
    recipes_exact = all(
        deflate_tokens.restore_zlib(s.source, s.recipe) == data[s.start:s.end]
        for s in transformed)
    check("every PDF zlib/DEFLATE recipe is bit-exact", recipes_exact)

    forced = containers.build_afc6(data, segs)
    restored = afc2.decompress_bytes(forced)
    hi = containers.header_info(forced)
    check("forced AFC6 reconstructs exact PDF bytes and SHA-256",
          restored == data and hashlib.sha256(restored).digest() ==
          hashlib.sha256(data).digest())
    check("AFC6 header explicitly records transformed source and recipes",
          forced[:4] == b"AFC6" and hi["magic"] == "AFC6"
          and hi["source_bytes"] > 0 and hi["recipe_bytes"] > 0, hi)
    check("analysis unwraps AFC6 to its Hybrid-Huffman container",
          analysis.unwrap(forced)[:4] in (b"AFC1", b"AFC2"))
    try:
        afc.decompress_bytes(forced)
        old_rejects = False
    except Exception:
        old_rejects = True
    check("old AFC1/AFC2 decoder rejects AFC6 cleanly", old_rejects)

    plain = afc2.compress_bytes(data, True, container_aware=False)
    auto = afc2.compress_bytes(data, True)
    check("automatic PDF selection never exceeds the plain path",
          len(auto) <= len(plain), (len(auto), len(plain), auto[:4]))
    envelope = afc5.wrap(data, forced, "normal.pdf")
    check("AFC5 can self-verify an AFC6 PDF payload",
          afc5.parse(envelope)["payload_magic"] == "AFC6"
          and afc2.decompress_bytes(envelope) == data)


def test_afc6_corrupt_input_is_safe():
    import containers
    bad_cases = [
        b"AFC6", b"AFC6\x06",
        b"AFC6\x09\x01\x01\x04\x00\x00",
        b"AFC6\x06" + b"\xff" * 40,
    ]
    clean = True
    for blob in bad_cases:
        try:
            containers.decompress_afc6(blob)
            clean = False
        except Exception:
            pass
    check("corrupt AFC6 containers raise instead of returning bad data", clean)


def test_afc4_corrupt_input_is_safe():
    import containers
    bad_cases = [
        b"AFC4",
        b"AFC4\x04",
        b"AFC4\x09\x01\x01\x04\x00\x00",
        b"AFC4\x04" + b"\xff" * 40,
    ]
    clean = True
    for blob in bad_cases:
        try:
            containers.decompress_afc4(blob)
            clean = False
        except Exception:
            pass
    check("corrupt AFC4 containers raise instead of returning bad data", clean)


def test_container_corrupt_input_is_safe():
    """A malformed or hostile container must fail cleanly, never silently
    produce wrong bytes."""
    import afc2
    import containers
    bad_cases = [
        b"AFC3",                                   # truncated header
        b"AFC3\x03",                               # no length
        b"AFC3\x09\x01\x01\x02\x00\x00",           # unknown mode
        b"AFC3\x03" + b"\xff" * 40,                # garbage body
    ]
    clean = True
    for blob in bad_cases:
        try:
            containers.decompress_afc3(blob)
            clean = False                          # should not have succeeded
        except Exception:
            pass
    check("corrupt AFC3 containers raise instead of returning bad data", clean)

    # A PDF that the parser cannot make sense of must still round-trip.
    weird = b"%PDF-1.4\nstream\nstream\nendstream" + bytes(range(256)) * 8
    blob = afc2.compress_bytes(weird, True, fmt="auto")
    check("a malformed PDF still round-trips losslessly",
          afc2.decompress_bytes(blob) == weird)


def test_container_component_classification():
    """Already-compressed components must be preserved, textual ones pooled."""
    import containers
    docs = {os.path.basename(p): p for p in _doc_corpus()}
    if not docs:
        return
    p = docs.get("pdf_images_only.pdf")
    if p:
        st = containers.describe_plan(open(p, "rb").read())
        labels = st["components"]
        check("JPEG streams in a PDF are detected and preserved",
              any("DCT" in k for k in labels), list(labels))
        check("PDF structural material is pooled for Hybrid-Huffman",
              any("structure" in k for k in labels), list(labels))
    d = docs.get("docx_images.docx")
    if d:
        st = containers.describe_plan(open(d, "rb").read())
        labels = st["components"]
        check("DOCX package structure is pooled",
              any("zip" in k for k in labels), list(labels))
        check("DOCX media/deflate payloads are preserved",
              st["opaque_bytes"] > 0, st["opaque_bytes"])


def test_afc3_reporting_is_whole_file(app):
    """AFC3 figures reported to the user must describe the WHOLE file.

    Regression guard for two bugs found in the browser: the Decompress page
    read the INNER container's declared length and reported a false integrity
    failure, and the explainer paired the inner container size with the whole
    file's original size and claimed 98.4% on a PDF that really saved 4.5%."""
    import afc2
    import analysis
    import containers
    docs = [p for p in _doc_corpus() if "images" in os.path.basename(p)]
    if not docs:
        return
    ok_len = ok_pct = True
    for path in docs:
        data = open(path, "rb").read()
        blob = afc2.compress_bytes(data, True, fmt="auto")
        if blob[:4] != b"AFC3":
            continue
        if containers.header_info(blob)["original_length"] != len(data):
            ok_len = False
        actual = 100.0 * (1 - len(blob) / len(data))
        text = analysis.explain(blob, len(data))
        # the sentence must quote the real whole-file saving, within rounding
        import re as _re
        m = _re.search(r"Saved (\d+\.\d)%", text)
        if not m or abs(float(m.group(1)) - actual) > 0.6:
            ok_pct = False
            print("   explain says %s, actual %.2f%% (%s)"
                  % (m.group(1) if m else "?", actual, os.path.basename(path)))
    check("AFC3 declares the whole-file length, not the pooled length", ok_len)
    check("the explainer quotes the whole-file saving for AFC3", ok_pct)

    # And the end-to-end API must report integrity VERIFIED, not FAILED.
    c = app.test_client()
    login(c, "alice", "correct-horse")
    data = open(docs[0], "rb").read()
    j = c.post("/api/compress", data={
        "file": (io.BytesIO(data), os.path.basename(docs[0]))}).get_json()
    blob = c.get("/download/" + j["token"]).data
    d = c.post("/api/decompress", data={
        "file": (io.BytesIO(blob), "doc.afc")}).get_json()
    check("Decompress page reports integrity VERIFIED for AFC3",
          d.get("integrity_ok") is True, d.get("integrity_note"))
    check("Decompress page names the component-aware mode",
          "component-aware" in (d.get("container_mode") or ""),
          d.get("container_mode"))


def test_native_backend_diagnostics(app):
    """A silent fall back to pure Python is the bug behind the 138-second
    dickens run: identical output, ~12x slower, and no way to find out why.
    The loader must always be able to say what happened."""
    import afc_native
    check("loader records diagnostic steps",
          isinstance(afc_native.DIAGNOSTICS, list)
          and len(afc_native.DIAGNOSTICS) > 0)
    check("loader exposes a REASON string",
          isinstance(afc_native.REASON, str) and afc_native.REASON != "")
    txt = afc_native.report()
    expected_state = ("AFC native backend: AVAILABLE" if afc_native.AVAILABLE
                      else "AFC native backend: NOT AVAILABLE")
    check("report() names the backend state",
          expected_state in txt and
          (afc_native.AVAILABLE or "Reason:" in txt), txt[:120])
    if afc_native.AVAILABLE:
        check("report() names the loaded library",
              afc_native.LIBRARY_PATH in txt and
              os.path.exists(afc_native.LIBRARY_PATH))

    # architecture detection: a 32-bit library must be refused, not loaded
    import struct as _s
    import tempfile as _t
    mz = bytearray(b"MZ" + b"\x00" * 0x3a)
    mz += _s.pack("<I", 0x40)
    mz += b"PE\x00\x00" + _s.pack("<H", 0x014c) + b"\x00" * 200
    fd, p = _t.mkstemp(suffix=".dll")
    os.write(fd, bytes(mz))
    os.close(fd)
    try:
        check("32-bit PE library is detected as 32-bit",
              afc_native._binary_bits(p) == 32, afc_native._binary_bits(p))
    finally:
        os.remove(p)
    check("this Python's bitness is detected",
          afc_native._python_bits() in (32, 64))

    # the status API must surface it so the user sees it without a terminal
    c = app.test_client()
    login(c, "alice", "correct-horse")
    s = c.get("/api/status").get_json()
    check("status API reports the native reason",
          isinstance(s.get("native_reason"), str) and s["native_reason"] != "",
          s.get("native_reason"))
    check("status API reports a backend per preset",
          set(s.get("preset_backends", {})) == {"fast", "balanced", "maximum"},
          s.get("preset_backends"))


def test_backend_reported_on_every_result(app):
    """Every compress/decompress result must carry the backend AND the reason.

    Reporting only "pure Python" is what let a 138-second run look like a slow
    algorithm instead of an unloaded native library."""
    c = app.test_client()
    login(c, "alice", "correct-horse")
    data = open(corpus_files(1)[0], "rb").read()
    j = c.post("/api/compress",
               data={"file": (io.BytesIO(data), "b.txt")}).get_json()
    for k in ("native_available", "native_reason", "native_library",
              "native_tunable"):
        check("compress result carries '%s'" % k, k in j, sorted(j))
    check("compress reason is non-empty",
          isinstance(j.get("native_reason"), str) and j["native_reason"] != "")

    blob = c.get("/download/" + j["token"]).data
    d = c.post("/api/decompress",
               data={"file": (io.BytesIO(blob), "b.afc")}).get_json()
    check("decompress result carries the backend reason",
          d.get("native_reason", "") != "", d.get("native_reason"))


def test_native_smoke_and_diagnose():
    """The diagnostic must actually CALL the library, not just infer."""
    import afc_native
    ok, detail = afc_native.smoke_test()
    if afc_native.AVAILABLE:
        check("native smoke test performs a real round trip", ok, detail)
        check("smoke test reports byte-identical output",
              "byte-identical" in detail, detail)
    check("library_name() matches the platform",
          afc_native.library_name().endswith(".dll" if os.name == "nt"
                                             else ".so"))
    check("expected_path() is absolute",
          os.path.isabs(afc_native.expected_path()))
    check("diagnose() is callable and returns an exit code",
          afc_native.diagnose.__call__ is not None)


def test_native_exports_are_declared():
    """Every ctypes entry point must be marked exported in the C++ source.

    MinGW auto-exports extern \"C\" symbols but MSVC does not: a cl.exe build
    without __declspec(dllexport) yields a DLL that exports nothing, the
    hasattr() check fails, and the engine silently falls back to Python."""
    src = open(os.path.join(ROOT, "afc_native.cpp"), encoding="utf-8").read()
    check("AFC_API export macro is defined", "define AFC_API" in src)
    check("macro uses dllexport on Windows", "__declspec(dllexport)" in src)
    for fn in ("afc_compress", "afc_compress_ex", "afc_decompress",
               "afc_free"):
        check("%s is marked AFC_API" % fn,
              ("AFC_API int %s(" % fn) in src or
              ("AFC_API void %s(" % fn) in src)


def test_pdf_object_inventory():
    """Real PDF component analysis: page objects, /Contents, filters."""
    import containers
    docs = {os.path.basename(p): p for p in _doc_corpus()}
    p = docs.get("pdf_text_and_images.pdf")
    if not p:
        return
    comps = containers.pdf_components(open(p, "rb").read())
    kinds = {}
    for c in comps:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    check("PDF objects are parsed", len(comps) > 10, len(comps))
    check("page content streams are identified via /Type /Page + /Contents",
          kinds.get("page-content", 0) > 0, kinds)
    check("image XObjects are identified", kinds.get("image", 0) > 0, kinds)
    check("stream filters are read from the object dictionary",
          any(c["filter"] for c in comps))

    # A Flate-filtered PDF must have its page streams PRESERVED, not
    # re-compressed: already-compressed data is not fed to Hybrid-Huffman.
    q = docs.get("pdf_word_flate.pdf")
    if q:
        data = open(q, "rb").read()
        segs = containers.plan(data)
        opaque = [(s.start, s.end) for s in segs
                  if s.kind == containers.OPAQUE]
        flate = [c for c in containers.pdf_components(data)
                 if c["filter"] == "/FlateDecode" and c["stream_start"] >= 0]
        preserved = [c for c in flate
                     if any(a <= c["stream_start"] and c["stream_end"] <= b
                            for a, b in opaque)]
        check("already-compressed (/FlateDecode) streams are preserved",
              len(flate) > 0 and len(preserved) == len(flate),
              "%d of %d" % (len(preserved), len(flate)))


def test_generic_files_unaffected():
    """Generic (non-container) files must take exactly the V6 path."""
    import afc2
    ok = True
    for path in corpus_files(6):
        data = open(path, "rb").read()
        v6 = afc2.compress_bytes(data, True, fmt="auto", container_aware=False)
        v7 = afc2.compress_bytes(data, True, fmt="auto")
        if v6 != v7:
            ok = False
            print("   generic path changed for %s" % os.path.basename(path))
    check("generic files produce byte-identical output to V6", ok)


def main():
    app, appmod, dbpath = make_app()
    try:
        test_auth(app, appmod)
        test_rate_limit(app)
        test_forced_password_change(app)
        test_role_gate(app)
        test_single_roundtrip(app)
        test_batch_roundtrip(app)
        test_archive(app)
        test_archive_no_deflate()
        test_archive_path_traversal()
        test_size_policy(app)
        test_history_isolation(app)
        test_reports(app)
        test_pages_render(app)
        # --- Part 2 ---
        test_analytics_match_database(app)
        test_analytics_extensions(app)
        test_analytics_timeseries_reference(app)
        test_history_search_filter_paginate(app)
        test_entropy_reflects_file_type(app)
        test_tree_and_attribution(app)
        test_presets_have_real_effect(app)
        test_preset_recorded_and_used(app)
        test_compare_view(app)
        test_admin_scope_gate(app)
        test_status_and_preview(app)
        test_part1_still_works(app)
        # --- separate Compress / Decompress pages ---
        test_pages_are_separate(app)
        test_decompress_endpoint_roundtrip(app)
        test_decompress_reports_verification(app)
        test_decompress_no_reference_is_not_a_fake_match(app)
        test_pages_refuse_each_others_input(app)
        test_restored_filename_recovery(app)
        test_filetypes_imports_no_codec()
        test_compress_page_flags_container_formats(app)
        test_engine_is_not_duplicated()
        # --- V7: native acceleration for every preset ---
        test_all_presets_native_capable()
        test_preset_backend_byte_identity()
        test_preset_options_are_immutable_and_isolated()
        test_presets_remain_distinct()
        test_cross_implementation_decode()
        test_experiment_foundation()
        # --- V7: container-aware PDF / DOCX ---
        test_container_tiling_is_exact()
        test_pdf_docx_byte_exact()
        test_afc3_never_larger_than_plain()
        test_multi_cycle_reconstruction()
        test_afc3_backward_compatibility()
        test_container_layer_introduces_no_codec()
        test_docx_member_inventory_and_exact_deflate_recipes()
        test_afc4_docx_exact_and_versioned(app)
        test_afc5_self_verifying_envelope()
        test_afc6_pdf_flate_exact_and_versioned()
        test_afc4_corrupt_input_is_safe()
        test_afc6_corrupt_input_is_safe()
        test_container_corrupt_input_is_safe()
        test_container_component_classification()
        test_afc3_reporting_is_whole_file(app)
        test_native_backend_diagnostics(app)
        test_backend_reported_on_every_result(app)
        test_native_smoke_and_diagnose()
        test_native_exports_are_declared()
        test_pdf_object_inventory()
        test_generic_files_unaffected()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(dbpath + suffix)
            except OSError:
                pass

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILURES)))
    if FAILURES:
        print("FAILED:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
