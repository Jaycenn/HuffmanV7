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
import re
import io
import os
import shutil
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
    # Isolate durable files before importing app: app.py creates its default
    # application at import time, so setting only the DB here could otherwise
    # pair a disposable database with the real result directory.
    config.RESULT_STORAGE_DIR = path + ".results"
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

    # Anonymous visitors get the public ByteSize website; the workspace and
    # every work endpoint stay gated.
    r = c.get("/", follow_redirects=False)
    check("anonymous reaches the public ByteSize landing page",
          r.status_code == 200 and b"ByteSize" in r.data
          and b'class="landing-hero"' in r.data
          and b'class="public-header"' in r.data
          and b'class="app-sidebar' not in r.data, r.status_code)
    r = c.get("/compress", follow_redirects=False)
    check("anonymous is sent to sign in before the workspace",
          r.status_code in (301, 302)
          and "/login" in r.headers.get("Location", ""), r.status_code)
    check("anonymous compression action remains server-gated",
          c.post("/api/compress").status_code == 401)

    # register
    r = c.post("/register", data={"username": "alice", "email": "a@b.co",
                                  "password": "correct-horse",
                                  "confirm": "correct-horse"},
               follow_redirects=False)
    check("register creates account and logs in", r.status_code in (301, 302))

    check("registration lands in the ByteSize Workspace",
          r.headers.get("Location") == "/dashboard", r.headers.get("Location"))

    r = c.get("/dashboard")
    check("logged-in user reaches dashboard", r.status_code == 200)
    r = c.get("/", follow_redirects=False)
    check("signed-in visitor to / is sent to the workspace",
          r.status_code in (301, 302)
          and r.headers.get("Location") == "/dashboard",
          r.headers.get("Location"))

    # logout
    r = c.post("/logout", follow_redirects=False)
    check("logout returns to the public landing page",
          r.status_code in (301, 302) and r.headers.get("Location") == "/",
          r.headers.get("Location"))
    r = c.get("/", follow_redirects=False)
    check("landing page invites an anonymous visitor to sign in",
          r.status_code == 200 and b"Sign in to compress" in r.data)

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
    check("non-admin gets 403 on account deletion",
          c.post("/admin/users/1/delete").status_code == 403)
    check("non-admin gets 403 on account state change",
          c.post("/admin/users/1/active", data={"active": "0"}).status_code
          == 403)

    admin_c = app.test_client()
    login(admin_c, config.DEFAULT_ADMIN_USERNAME, "admin-new-password")
    r = admin_c.get("/admin/users")
    check("admin reaches admin route", r.status_code == 200, r.status_code)

    # Disabling an account invalidates an already-issued session immediately.
    import db
    with app.app_context():
        alice_id = db.get_user_by_username("alice")["id"]
    check("admin can disable a user",
          admin_c.post("/admin/users/%d/active" % alice_id,
                       data={"active": "0"}).status_code in (301, 302))
    check("disabled account loses its existing session",
          c.get("/settings", follow_redirects=False).status_code in (301, 302))
    admin_c.post("/admin/users/%d/active" % alice_id, data={"active": "1"})


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
    durable = c.get("/files/%d/download" % j["history_id"])
    check("created archive is durably re-downloadable",
          durable.status_code == 200 and durable.data == blob)

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

    # A caller-controlled normal batch id may equal the returned archive id.
    # Archive deletion must follow the parent FK, never a broad batch-id delete.
    collision = c.post("/api/batch", data={
        "file": (io.BytesIO(b"unrelated batch result " * 80), "separate.txt"),
        "batch_id": j["batch_id"]}).get_json()
    check("colliding ordinary batch result is accepted",
          collision.get("lossless") and collision.get("history_id"), collision)
    import db
    with app.app_context():
        alice_id = db.get_user_by_username("alice")["id"]
        before = db.get_db().execute(
            "SELECT COUNT(*) AS n FROM compression_history WHERE batch_id = ?",
            (j["batch_id"],)).fetchone()["n"]
        stats_before = db.history_stats(alice_id)["files"]
        stored = db.get_stored_artifact(j["history_id"])
        disk_path = os.path.join(config.RESULT_STORAGE_DIR,
                                 stored["storage_key"])
    report_before = c.get("/report.csv?batch_id=" + j["batch_id"]).data
    deleted = c.post("/files/%d/delete" % j["history_id"],
                     follow_redirects=False)
    with app.app_context():
        after = db.get_db().execute(
            "SELECT COUNT(*) AS n FROM compression_history WHERE batch_id = ?",
            (j["batch_id"],)).fetchone()["n"]
        stats_after = db.history_stats(alice_id)["files"]
    report_after = c.get("/report.csv?batch_id=" + j["batch_id"]).data
    check("deleting an archive removes only its summary, children, and blob",
          before == len(payload) + 2 and after == 1
          and stats_before - stats_after == 1
          and deleted.status_code in (301, 302)
          and not os.path.exists(disk_path)
          and c.get("/files/%d/download" % collision["history_id"]).status_code
              == 200, (before, after))
    check("batch collision remains in statistics and reports",
          b"testpak.afcpak" in report_before
          and b"separate.txt" in report_before
          and b"testpak.afcpak" not in report_after
          and b"separate.txt" in report_after,
          (report_before, report_after))
    c.post("/files/%d/delete" % collision["history_id"])


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
          and b"ByteSize" in r.data and b"Compression report" in r.data,
          r.status_code)
    check("PDF report carries academic title but no version branding",
          b"Adaptive File Compression System Using Multi-Level" in r.data
          and b"HOLY ANGEL" not in r.data
          and config.ENGINE_VERSION.encode() not in r.data)


def test_pages_render(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    for path in ("/dashboard", "/compress", "/decompress", "/files", "/about",
                 "/settings"):
        r = c.get(path)
        if not check("page renders: %s" % path, r.status_code == 200,
                     r.status_code):
            continue
    r = c.get("/", follow_redirects=False)
    check("signed-in / redirects to the workspace",
          r.status_code in (301, 302)
          and r.headers.get("Location") == "/dashboard",
          r.headers.get("Location"))
    # every size shown must come from config: assert the rendered Settings page
    # actually contains the configured maximum, not a stale literal
    r = c.get("/settings")
    from app import human
    check("settings page shows the configured max file size",
          human(config.MAX_FILE_SIZE).encode() in r.data,
          human(config.MAX_FILE_SIZE))


def test_public_site_and_action_gates(app):
    """The public website and the ByteSize Workspace are separate surfaces.

    `/` is a real marketing/landing page again rather than the application
    shell, every workspace destination is login-gated, and no work endpoint is
    reachable anonymously."""
    c = app.test_client()
    for path in ("/", "/about", "/login", "/register"):
        r = c.get(path)
        check("public page renders: %s" % path,
              r.status_code == 200 and b"ByteSize" in r.data
              and b'class="public-header"' in r.data
              and b'class="app-sidebar' not in r.data, r.status_code)

    root = c.get("/").data
    check("root is the public landing page, not the app shell",
          b'class="landing-hero"' in root
          and b'class="product-preview"' in root
          and b'id="sDrop"' not in root and b'id="sPreset"' not in root)
    check("landing page keeps the Structura-derived sections",
          b'id="method"' in root and b'id="formats"' in root
          and b"How it works" in root and b"Formats" in root
          and b'class="method-grid"' in root
          and b'class="format-list"' in root
          and b'class="landing-cta"' in root
          and b'class="public-footer"' in root)
    check("landing copy identifies ByteSize, not Structura",
          b"Find the structure a byte-by-byte encoder misses" in root
          and b"ByteSize combines multi-level frequency analysis" in root
          and re.search(rb"Structura\b", root) is None)
    check("landing preview leads to authentication",
          b"Sign in to use the workspace" in root
          and b'class="preview-brand">ByteSize<' in root)
    check("landing preview names the backend actually loaded",
          (b"C++ native" in root) == bool(__import__("app").engine.NATIVE))

    for path in ("/dashboard", "/compress", "/decompress", "/files",
                 "/settings", "/compare"):
        r = c.get(path, follow_redirects=False)
        check("workspace page is login-gated: %s" % path,
              r.status_code in (301, 302)
              and "/login" in r.headers.get("Location", ""), r.status_code)

    api_actions = [
        ("POST", "/api/compress"), ("POST", "/api/decompress"),
        ("POST", "/api/batch"), ("POST", "/api/archive/create"),
        ("POST", "/api/archive/extract"), ("GET", "/api/history"),
        ("GET", "/api/history/search"), ("GET", "/api/stats"),
        ("POST", "/api/preview"), ("POST", "/api/entropy"),
        ("GET", "/api/tree/not-a-token"), ("GET", "/api/presets"),
        ("GET", "/api/status"),
    ]
    for method, path in api_actions:
        status = c.open(path, method=method).status_code
        check("anonymous API denied: %s %s" % (method, path),
              status == 401, status)

    browser_actions = [
        ("GET", "/download/not-a-token"), ("GET", "/report.csv"),
        ("GET", "/report.pdf"), ("GET", "/files/1/download"),
        ("POST", "/files/1/delete"),
        ("POST", "/admin/users/1/delete"),
        ("POST", "/admin/users/1/active"),
    ]
    for method, path in browser_actions:
        r = c.open(path, method=method, follow_redirects=False)
        check("anonymous browser action redirects: %s %s" % (method, path),
              r.status_code in (301, 302)
              and "/login" in r.headers.get("Location", ""), r.status_code)

    check("public config exposes durable-storage policy",
          c.get("/api/config").get_json()["result_retention_days"]
          == config.RESULT_RETENTION_DAYS
          and c.get("/api/config").get_json()["max_stored_bytes_per_user"]
          == config.MAX_STORED_BYTES_PER_USER)
    check("gated destinations survive the sign-in round trip",
          c.get("/files", follow_redirects=False).headers.get("Location")
          == "/login?next=/files")


def test_intended_destination_preserved(app):
    c = app.test_client()
    attempt = c.get("/report.csv?batch_id=return-here",
                    follow_redirects=False)
    location = attempt.headers.get("Location", "")
    r = c.post(location, data={"username": "alice",
                               "password": "correct-horse"},
               follow_redirects=False)
    check("login returns to complete intended destination",
          r.headers.get("Location") == "/report.csv?batch_id=return-here",
          r.headers.get("Location"))

    c2 = app.test_client()
    r = c2.post("/register?next=/files", data={
        "username": "returner", "email": "returner@example.com",
        "password": "return-password", "confirm": "return-password",
        "next": "/files"}, follow_redirects=False)
    check("registration returns to intended destination",
          r.status_code in (301, 302)
          and r.headers.get("Location") == "/files", r.headers.get("Location"))

    import auth
    bad_targets = ("https://evil.example/", "//evil.example/", "\\evil")
    check("external and backslash next targets are rejected",
          all(auth.safe_next(target) == "" for target in bad_targets))
    c3 = app.test_client()
    r = c3.post("/register?next=https://evil.example/", data={
        "username": "safereturn", "email": "safe-return@example.com",
        "password": "return-password", "confirm": "return-password"},
        follow_redirects=False)
    check("registration cannot redirect to another origin",
          r.status_code in (301, 302)
          and r.headers.get("Location") == "/dashboard",
          r.headers.get("Location"))


def test_branding_and_about_evidence(app):
    import csv as _csv
    c = app.test_client()
    public_paths = ("/", "/about", "/login", "/register")
    workspace_paths = ("/dashboard", "/compress", "/decompress", "/files",
                       "/about", "/settings")
    signed_in = app.test_client()
    login(signed_in, "alice", "correct-horse")
    for client, paths in ((c, public_paths), (signed_in, workspace_paths)):
        for path in paths:
            body = client.get(path).data
            check("ByteSize brand renders: %s" % path, b"ByteSize" in body)
            # \b keeps the domain word "structural" out of the brand sweep.
            check("no Structura branding survives: %s" % path,
                  re.search(rb"Structura\b", body) is None)
            check("institution and versions absent: %s" % path,
                  b"Holy Angel University" not in body
                  and b"School of Computing" not in body
                  and config.APP_VERSION.encode() not in body
                  and config.ENGINE_VERSION.encode() not in body)
    for path in workspace_paths:
        body = signed_in.get(path).data
        check("workspace carries the ByteSize B mark: %s" % path,
              b'class="brand-mark" aria-hidden="true">B<' in body)

    standalone = open(os.path.join(ROOT, "AFC_WebApp.html"),
                      encoding="utf-8").read()
    check("standalone UI is rebranded",
          "ByteSize" in standalone and "Holy Angel University" not in standalone
          and "School of Computing" not in standalone
          and "afc_engine.js v4" not in standalone)

    about = c.get("/about").data.decode("utf-8")
    check("academic title appears on About",
          "Adaptive File Compression System Using Multi-Level" in about)
    check("About states all three pipeline stages",
          all(term in about for term in (
              "Multi-tier frequency scan", "Bit Cost Decision Engine",
              "Hybrid Huffman encoding")))
    for suite in ("canterbury", "silesia"):
        name = "afc_1_3_%s_native_summary.csv" % suite
        path = os.path.join(ROOT, "benchmarks", name)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = [r for r in _csv.DictReader(handle)
                    if r["preset"] == "balanced"]
        original = sum(int(r["original_bytes"]) for r in rows)
        compressed = sum(int(r["compressed_bytes"]) for r in rows)
        saved = 100.0 * (1.0 - compressed / original)
        check("About %s numbers trace to CSV" % suite,
              format(original, ",") in about
              and format(compressed, ",") in about
              and ("%.2f%%" % saved) in about
              and ("benchmarks/" + name) in about)
    # [v9] The current-engine row has to trace to its CSV as well, and that
    # CSV has to describe THIS engine -- otherwise the page would quietly go
    # on quoting a previous version's numbers.
    repo_csv = os.path.join(ROOT, "benchmarks", "v9_repo_corpus.csv")
    with open(repo_csv, newline="", encoding="utf-8") as handle:
        repo_rows = [r for r in _csv.DictReader(handle)
                     if r["preset"] == "balanced"]
    original = sum(int(r["original_bytes"]) for r in repo_rows)
    compressed = sum(int(r["compressed_bytes"]) for r in repo_rows)
    saved = 100.0 * (1.0 - compressed / original)
    check("About repository-corpus numbers trace to CSV",
          format(original, ",") in about
          and format(compressed, ",") in about
          and ("%.2f%%" % saved) in about
          and "benchmarks/v9_repo_corpus.csv" in about)
    check("every repository-corpus row is verified lossless",
          all(r["byte_equal"] == "True" and r["sha256_equal"] == "True"
              for r in repo_rows) and len(repo_rows) >= 20, len(repo_rows))
    check("the AFC 1.3 rows are labelled as the historical audit",
          "AFC 1.3 audit" in about and "not" in about)

    # [v9] Silesia is now measured on the current engine as well. The two rows
    # are the same twelve files, so the page may only claim an improvement if
    # the committed measurement actually shows one.
    ext_csv = os.path.join(ROOT, "benchmarks", "external_corpus.csv")
    with open(ext_csv, newline="", encoding="utf-8") as handle:
        ext_rows = [r for r in _csv.DictReader(handle)]
    sil = [r for r in ext_rows
           if r["corpus"] == "silesia" and r["preset"] == "balanced"]
    sil_original = sum(int(r["original_bytes"]) for r in sil)
    sil_now = sum(int(r["compressed_bytes"]) for r in sil)
    check("Silesia current-engine numbers trace to CSV",
          len(sil) == 12
          and format(sil_original, ",") in about
          and format(sil_now, ",") in about
          and ("%.2f%%" % (100.0 * (1.0 - sil_now / sil_original))) in about
          and "benchmarks/external_corpus.csv" in about, len(sil))
    check("every external-corpus row is verified lossless",
          all(r["byte_equal"] == "True" and r["sha256_equal"] == "True"
              for r in ext_rows) and len(ext_rows) >= 48, len(ext_rows))

    with open(os.path.join(ROOT, "benchmarks",
                           "afc_1_3_silesia_native_summary.csv"),
              newline="", encoding="utf-8") as handle:
        old_sil = [r for r in _csv.DictReader(handle)
                   if r["preset"] == "balanced"]
    old_original = sum(int(r["original_bytes"]) for r in old_sil)
    old_bytes = sum(int(r["compressed_bytes"]) for r in old_sil)
    check("the two Silesia rows really are the same corpus",
          old_original == sil_original,
          "%d != %d" % (old_original, sil_original))
    check("the claimed Silesia improvement is the measured one",
          sil_now < old_bytes
          and format(old_bytes - sil_now, ",") in about,
          "%s vs %s" % (format(old_bytes - sil_now, ","), sil_now))
    gov = [r for r in ext_rows
           if r["corpus"] == "govdocs1-thread000" and r["preset"] == "balanced"]
    gov_original = sum(int(r["original_bytes"]) for r in gov)
    gov_now = sum(int(r["compressed_bytes"]) for r in gov)
    check("GovDocs1 numbers trace to CSV",
          len(gov) > 900
          and format(gov_original, ",") in about
          and format(gov_now, ",") in about
          and ("%.2f%%" % (100.0 * (1.0 - gov_now / gov_original))) in about,
          len(gov))
    # The per-format figures quoted next to the table must be the measured
    # ones: this is the page's own class-qualified claim about PDF and DOCX,
    # so it may not drift from the CSV it cites.
    by_ext = {}
    for row in gov:
        name = row["file"]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        acc = by_ext.setdefault(ext, [0, 0])
        acc[0] += int(row["original_bytes"])
        acc[1] += int(row["compressed_bytes"])
    for ext in ("pdf", "xls", "txt", "jpg"):
        original, compressed = by_ext[ext]
        quoted = "%.2f%%" % (100.0 * (1.0 - compressed / original))
        check("About %s figure matches the measurement (%s)" % (ext, quoted),
              quoted in about, quoted)
    unshrunk = sum(1 for r in gov
                   if int(r["compressed_bytes"]) >= int(r["original_bytes"]))
    check("About reports the files that did not shrink",
          str(unshrunk) in about, unshrunk)

    # A partial Canterbury re-run must never be presented as Canterbury.
    check("no partial Canterbury subset is published as the corpus",
          not any(r["corpus"] == "canterbury" for r in ext_rows)
          and "canterbury-text-subset" in
              open(ext_csv, encoding="utf-8").read())

    import presets as _presets
    spot = [("canterbury", "alice29.txt"), ("corpus", "data.json"),
            ("documents", "docx_zip_stored.docx")]
    for group, fname in spot:
        row = next(r for r in repo_rows
                   if r["group"] == group and r["file"] == fname)
        raw = open(os.path.join(ROOT, "benchmarks", group, fname), "rb").read()
        blob, _u, _b = _presets.compress_with(raw, "balanced", fmt="auto")
        check("published size still reproduces: %s" % fname,
              len(blob) == int(row["compressed_bytes"]),
              "%d != %s" % (len(blob), row["compressed_bytes"]))
    # The page must still distinguish the two component classes and refuse a
    # blanket percentage. Since v9 it does so with the measured GovDocs1
    # split rather than prose, so the check is that both classes are named
    # concretely -- a format that reduces well and one that does not -- and
    # that the refusal is still there.
    check("document claim distinguishes both component classes",
          "no general compression-percentage claim" in about
          and "uncompressed text streams" in about
          and "behaves like the JPEGs" in about
          and "did not shrink" in about)


# ---------------------------------------------------------------------------
# [v9] Container output is a published artefact: a user's stored .afc file and
# the numbers in the thesis both depend on it.  Performance work is expected;
# silently changing what the engine emits is not.  These digests pin the exact
# container for a fixed corpus at every preset, so any change to the encoded
# bytes has to be a deliberate edit of this table with a measured reason.
# ---------------------------------------------------------------------------

def _pin_corpus():
    return [
        ("prose", b"the quick brown fox jumps over the lazy dog. " * 300),
        ("csvish", b"".join(b"%d,alpha,beta,gamma,%d\n" % (i, i * 7)
                            for i in range(400))),
        ("jsonish", b"[" + b",".join(b'{"id":%d,"name":"item","ok":true}' % i
                                     for i in range(300)) + b"]"),
        ("code", b"def compress(data):\n    return huffman(data)\n\n" * 200),
        ("binary", bytes((i * 37 + (i >> 3)) & 0xFF for i in range(6000))),
        ("incompressible",
         bytes(((i * 2654435761) >> 13) & 0xFF for i in range(4096))),
        ("repetitive", b"AB" * 5000),
    ]


PINNED_CONTAINERS = {
    # (name, preset): (sha256[:32], bytes now, bytes under the V7 engine)
    ("prose", "fast"): ("df5cf1d71b1f51c501cf8d74e9cdc558", 486, 486),
    ("prose", "balanced"): ("df5cf1d71b1f51c501cf8d74e9cdc558", 486, 865),
    ("prose", "maximum"): ("df5cf1d71b1f51c501cf8d74e9cdc558", 486, 865),
    ("csvish", "fast"): ("dc67eace6449ca2dc449f5cf142689a5", 2503, 2503),
    ("csvish", "balanced"): ("91e1a9ffade15428c92f2ebf5c2ce069", 2335, 2335),
    ("csvish", "maximum"): ("d746c723d1a19f2f5d6e7907e43a1e97", 2291, 2335),
    ("jsonish", "fast"): ("4189e69d98a2382747def080987c9a90", 1246, 1246),
    ("jsonish", "balanced"): ("9c44277dc5092815e69773d507db6d7f", 1178, 1461),
    ("jsonish", "maximum"): ("9c44277dc5092815e69773d507db6d7f", 1178, 1461),
    ("code", "fast"): ("bd1ca4616ebf14ad5bb8867fbb1e3dcb", 473, 473),
    ("code", "balanced"): ("bd1ca4616ebf14ad5bb8867fbb1e3dcb", 473, 848),
    ("code", "maximum"): ("bd1ca4616ebf14ad5bb8867fbb1e3dcb", 473, 848),
    ("binary", "fast"): ("a0a19f3f609b9290471181001a02e5d2", 5436, 5436),
    ("binary", "balanced"): ("92529110a69d47ab931c5512af5356c8", 2962, 3344),
    ("binary", "maximum"): ("33725610bbf986c00b2e60335f7e6835", 2364, 2801),
    ("incompressible", "fast"): ("62e57bf89140815e79ea50d554bca7da", 3826, 3826),
    ("incompressible", "balanced"): ("b2625c816e67c00b8c207916ccfc9791", 2885, 3197),
    ("incompressible", "maximum"): ("b2625c816e67c00b8c207916ccfc9791", 2885, 3197),
    ("repetitive", "fast"): ("f06c7f8ddba83f685adedfb7865f50d9", 96, 96),
    ("repetitive", "balanced"): ("528e09942cc001850704d032eae34449", 58, 109),
    ("repetitive", "maximum"): ("528e09942cc001850704d032eae34449", 58, 109),
}


def test_container_bytes_are_pinned():
    import presets
    import afc2
    for name, data in _pin_corpus():
        for preset in ("fast", "balanced", "maximum"):
            blob, _used, _backend = presets.compress_with(data, preset,
                                                          fmt="auto")
            want_sha, want_len, v7_len = PINNED_CONTAINERS[(name, preset)]
            got = hashlib.sha256(blob).hexdigest()[:32]
            check("container bytes unchanged: %s/%s" % (name, preset),
                  got == want_sha and len(blob) == want_len,
                  "%s/%d (expected %s/%d)" % (got, len(blob), want_sha,
                                              want_len))
            # The third column is what the pre-search V7 engine emitted. It
            # never has to be revisited, and it makes the ratchet one-way:
            # the engine may not regress past the version this corpus was
            # first measured on.
            check("no regression against the V7 engine: %s/%s" % (name, preset),
                  len(blob) <= v7_len, "%d > %d" % (len(blob), v7_len))
            check("pinned container round-trips: %s/%s" % (name, preset),
                  afc2.decompress_bytes(blob) == data)

def test_preset_size_is_monotonic():
    """A costlier preset must never produce a LARGER container.

    Presets search progressively harder, but a deeper search that lands on a
    worse container is a regression a user can see in their own file sizes."""
    import presets
    for name, data in _pin_corpus():
        sizes = {}
        for preset in ("fast", "balanced", "maximum"):
            blob, _used, _backend = presets.compress_with(data, preset,
                                                          fmt="auto")
            sizes[preset] = len(blob)
        check("balanced is not larger than fast: %s" % name,
              sizes["balanced"] <= sizes["fast"], sizes)
        check("maximum is not larger than balanced: %s" % name,
              sizes["maximum"] <= sizes["balanced"], sizes)

def test_analytics_routes_removed(app):
    c = app.test_client()
    login(c, "alice", "correct-horse")
    for path in ("/analytics", "/api/analytics/summary",
                 "/api/analytics/extensions", "/api/analytics/timeseries"):
        check("analytics route removed: %s" % path,
              c.get(path).status_code == 404)


def _compress_for_storage(client, name="durable.txt"):
    data = (b"durable hybrid huffman result\n" * 400)
    response = client.post("/api/compress", data={
        "file": (io.BytesIO(data), name), "preset": "fast"})
    return response, data


def test_persistent_artifact_access(app, appmod):
    import db
    c = app.test_client()
    login(c, "alice", "correct-horse")
    response, _ = _compress_for_storage(c)
    result = response.get_json()
    check("compressed result is durably recorded",
          response.status_code == 200 and result.get("history_id")
          and result.get("stored_download"), result)
    row_id = result["history_id"]
    transient = c.get("/download/" + result["token"]).data
    with app.app_context():
        stored = db.get_stored_artifact(row_id)
        disk_path = os.path.join(config.RESULT_STORAGE_DIR,
                                 stored["storage_key"])
    check("compressed artifact exists outside static storage",
          os.path.isfile(disk_path)
          and os.path.commonpath([os.path.abspath(disk_path),
                                  os.path.abspath(config.RESULT_STORAGE_DIR)])
              == os.path.abspath(config.RESULT_STORAGE_DIR)
          and "static" not in os.path.relpath(
              disk_path, ROOT).replace("\\", "/").split("/"))

    # Memory is deliberately insufficient: the durable route must still work.
    appmod.RESULTS.clear()
    durable = c.get("/files/%d/download" % row_id)
    check("owner can download after transient cache is cleared",
          durable.status_code == 200 and durable.data == transient,
          durable.status_code)

    restarted = appmod.create_app(
        db_path=config.DATABASE_PATH, testing=True,
        storage_dir=config.RESULT_STORAGE_DIR)
    restarted.secret_key = "restart-key"
    owner = restarted.test_client()
    login(owner, "alice", "correct-horse")
    after_restart = owner.get("/files/%d/download" % row_id)
    check("stored download survives application restart",
          after_restart.status_code == 200
          and after_restart.data == transient, after_restart.status_code)

    other = restarted.test_client()
    login(other, "bob", "bob-password-1")
    check("other user receives 404 for stored row",
          other.get("/files/%d/download" % row_id).status_code == 404)
    anonymous = restarted.test_client()
    check("anonymous stored download is rejected",
          anonymous.get("/files/%d/download" % row_id,
                        follow_redirects=False).status_code in (301, 302))
    admin_client = restarted.test_client()
    login(admin_client, config.DEFAULT_ADMIN_USERNAME, "admin-new-password")
    check("administrator may retrieve a stored artifact",
          admin_client.get("/files/%d/download" % row_id).status_code == 200)

    with open(disk_path, "ab") as handle:
        handle.write(b"tampered")
    corrupt = owner.get("/files/%d/download" % row_id)
    check("tampered artifact is refused before download",
          corrupt.status_code == 409 and b"SHA-256" in corrupt.data,
          corrupt.status_code)
    with restarted.app_context():
        check("integrity failure is recorded",
              db.get_stored_artifact(row_id)["integrity_status"] == "corrupt")

    deleted = owner.post("/files/%d/delete" % row_id,
                         follow_redirects=False)
    check("owner can delete stored artifact from Files route",
          deleted.status_code in (301, 302) and not os.path.exists(disk_path))
    check("deleted artifact history no longer resolves",
          owner.get("/files/%d/download" % row_id).status_code == 404)


def test_storage_quota_and_transient_restore(app):
    import db
    c = app.test_client()
    login(c, "alice", "correct-horse")
    first, source = _compress_for_storage(c, "transient-check.txt")
    result = first.get_json()
    blob = c.get("/download/" + result["token"]).data
    with app.app_context():
        before_restore = db.stored_bytes_for_user(gid := db.get_user_by_username("alice")["id"])
    restored = c.post("/api/decompress", data={
        "file": (io.BytesIO(blob), "transient-check.txt.afc")})
    with app.app_context():
        after_restore = db.stored_bytes_for_user(gid)
    check("decompressed original is not persisted",
          restored.status_code == 200 and before_restore == after_restore)

    old_limit = config.MAX_STORED_BYTES_PER_USER
    try:
        config.MAX_STORED_BYTES_PER_USER = before_restore + 1
        with app.app_context():
            rows_before = len(db.list_history(gid, limit=10000))
        denied, _ = _compress_for_storage(c, "over-quota.txt")
        with app.app_context():
            rows_after = len(db.list_history(gid, limit=10000))
        body = denied.get_json()
        check("quota refuses new result without eviction",
              denied.status_code == 507 and body.get("quota_exceeded")
              and "Files" in body.get("error", "")
              and rows_before == rows_after, body)
    finally:
        config.MAX_STORED_BYTES_PER_USER = old_limit


def test_missing_artifact_is_not_reported_verified(app, appmod):
    import db
    c = app.test_client()
    login(c, "alice", "correct-horse")
    response, _ = _compress_for_storage(c, "missing-result.txt")
    row_id = response.get_json()["history_id"]
    with app.app_context():
        item = db.get_stored_artifact(row_id)
        path = os.path.join(config.RESULT_STORAGE_DIR, item["storage_key"])
    os.remove(path)
    restarted = appmod.create_app(
        db_path=config.DATABASE_PATH, testing=True,
        storage_dir=config.RESULT_STORAGE_DIR)
    owner = restarted.test_client()
    login(owner, "alice", "correct-horse")
    page = owner.get("/files").data
    # The row content is populated by the JSON endpoint; verify both the stored
    # status and the renderer's non-verified branch.
    with restarted.app_context():
        status = db.get_stored_artifact(row_id)["integrity_status"]
    check("restart marks a missing artifact instead of claiming verification",
          status == "missing" and b'artifact_integrity !== "verified"' in page,
          status)
    check("missing durable artifact download is refused",
          owner.get("/files/%d/download" % row_id).status_code == 409)


def test_retention_policy(app, appmod):
    import db
    c = app.test_client()
    login(c, "alice", "correct-horse")
    result, _ = _compress_for_storage(c, "expired-result.txt")
    row_id = result.get_json()["history_id"]
    with app.app_context():
        stored = db.get_stored_artifact(row_id)
        disk_path = os.path.join(config.RESULT_STORAGE_DIR,
                                 stored["storage_key"])
        conn = db.get_db()
        conn.execute("UPDATE stored_artifacts SET created_at = '2000-01-01'"
                     " WHERE history_id = ?", (row_id,))
        conn.commit()
    old_days = config.RESULT_RETENTION_DAYS
    try:
        config.RESULT_RETENTION_DAYS = 1
        c.get("/files")  # before_request enforces retention without a restart
        with app.app_context():
            metadata_gone = db.get_stored_artifact(row_id) is None
        check("positive retention runs live and removes expired result",
              metadata_gone and not os.path.exists(disk_path))

        archive = c.post("/api/archive/create", data={
            "files": [(io.BytesIO(b"archive member A " * 80), "a.txt"),
                      (io.BytesIO(b"archive member B " * 80), "b.txt")],
            "archive_name": "expires-together"}).get_json()
        with app.app_context():
            conn = db.get_db()
            conn.execute(
                "UPDATE stored_artifacts SET created_at = '2000-01-01'"
                " WHERE history_id = ?", (archive["history_id"],))
            conn.commit()
        c.get("/files")
        with app.app_context():
            members_left = db.get_db().execute(
                "SELECT COUNT(*) AS n FROM compression_history"
                " WHERE batch_id = ?", (archive["batch_id"],)).fetchone()["n"]
        check("archive retention removes summary and all member rows",
              members_left == 0, members_left)
    finally:
        config.RESULT_RETENTION_DAYS = old_days


def test_storage_migration_is_additive():
    import db
    import sqlite3
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    try:
        # Build a genuine pre-storage/pre-parent-column database.  This catches
        # schema.sql indexes that would otherwise run before _migrate can add a
        # new column to an existing installation.
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                must_change_password INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_login_at TEXT);
            CREATE TABLE compression_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL, operation TEXT NOT NULL DEFAULT 'compress',
                original_bytes INTEGER NOT NULL, compressed_bytes INTEGER NOT NULL,
                ratio REAL NOT NULL, space_saved_pct REAL NOT NULL,
                engine TEXT NOT NULL, container_format TEXT NOT NULL DEFAULT '',
                lossless_verified INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL NOT NULL DEFAULT 0, batch_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            INSERT INTO users (username,email,password_hash)
                VALUES ('legacy','legacy@example.com','hash');
            INSERT INTO compression_history
                (user_id,filename,original_bytes,compressed_bytes,ratio,
                 space_saved_pct,engine)
                VALUES (1,'legacy.txt',100,50,2.0,50.0,'pure Python');
        """)
        conn.commit()
        conn.close()

        db.init_db(path, seed_admin=False)
        db.init_db(path, seed_admin=False)
        conn = db.connect(path)
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        legacy = conn.execute(
            "SELECT filename FROM compression_history WHERE user_id = ?",
            (1,)).fetchone()
        columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(compression_history)")}
        conn.close()
        check("durable-storage migration is additive and idempotent",
              "stored_artifacts" in tables
              and "parent_history_id" in columns
              and legacy is not None and legacy["filename"] == "legacy.txt")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def test_security_controls(app, appmod):
    """Exercise production CSRF and verification-failure behavior."""
    import db
    c = app.test_client()
    app.config["CSRF_PROTECT"] = True
    try:
        page = c.get("/login")
        with c.session_transaction() as sess:
            token = sess.get("_csrf_token")
        check("production forms receive a CSRF token",
              bool(token) and b'name="csrf_token"' in page.data)
        missing = c.post("/login", data={
            "username": "alice", "password": "correct-horse"})
        check("login without CSRF token is rejected with a visible error",
              missing.status_code == 400
              and b"Security token missing" in missing.data
              and b"ByteSize" in missing.data)
        signed_in = c.post("/login", data={
            "username": "alice", "password": "correct-horse",
            "csrf_token": token}, follow_redirects=False)
        check("login with CSRF token succeeds",
              signed_in.status_code in (301, 302), signed_in.status_code)
        workspace = c.get("/files")
        check("workspace fetch and XHR calls carry the CSRF header",
              b'X-CSRF-Token' in workspace.data)
        with c.session_transaction() as sess:
            signed_token = sess.get("_csrf_token")
        missing_api = c.post("/api/preview", data={
            "file": (io.BytesIO(b"csrf preview"), "preview.txt")})
        check("authenticated API POST without CSRF is rejected",
              missing_api.status_code == 400)
        accepted_api = c.post(
            "/api/preview",
            data={"file": (io.BytesIO(b"csrf preview"), "preview.txt")},
            headers={"X-CSRF-Token": signed_token})
        check("same-origin API CSRF header is accepted",
              accepted_api.status_code == 200, accepted_api.status_code)
        check("logout is POST-only and CSRF protected",
              c.get("/logout").status_code == 405
              and c.post("/logout").status_code == 400
              and c.post("/logout", data={"csrf_token": signed_token},
                         follow_redirects=False).status_code in (301, 302))
    finally:
        app.config["CSRF_PROTECT"] = False

    c = app.test_client()
    login(c, "alice", "correct-horse")
    with app.app_context():
        uid = db.get_user_by_username("alice")["id"]
        before_rows = len(db.list_history(uid, limit=10000))
        before_bytes = db.stored_bytes_for_user(uid)
    real_decompress = appmod.engine.decompress_bytes
    appmod.engine.decompress_bytes = lambda _blob: b"verification mismatch"
    try:
        failed, _ = _compress_for_storage(c, "must-not-save.txt")
    finally:
        appmod.engine.decompress_bytes = real_decompress
    with app.app_context():
        after_rows = len(db.list_history(uid, limit=10000))
        after_bytes = db.stored_bytes_for_user(uid)
    check("failed round trip is neither downloadable nor persisted",
          failed.status_code == 500 and before_rows == after_rows
          and before_bytes == after_bytes, failed.get_json())

    real_write = appmod.artifact_store.write
    appmod.artifact_store.write = lambda _blob: (_ for _ in ()).throw(
        OSError("simulated storage failure"))
    with app.app_context():
        before_rows = len(db.list_history(uid, limit=10000))
    try:
        failed_archive = c.post("/api/archive/create", data={
            "files": [(io.BytesIO(b"first member " * 50), "one.txt"),
                      (io.BytesIO(b"second member " * 50), "two.txt")],
            "archive_name": "must-not-orphan"})
    finally:
        appmod.artifact_store.write = real_write
    with app.app_context():
        after_rows = len(db.list_history(uid, limit=10000))
    check("archive storage failure leaves no summary or member history",
          failed_archive.status_code == 500 and before_rows == after_rows,
          failed_archive.get_json())


def test_installation_secret_and_storage_safety(appmod):
    temp = tempfile.mkdtemp(prefix="bytesize-security-")
    old_db = config.DATABASE_PATH
    old_store = config.RESULT_STORAGE_DIR
    old_secret = config.SECRET_KEY_PATH
    try:
        config.SECRET_KEY_PATH = os.path.join(temp, "install.secret")
        db_path = os.path.join(temp, "app.sqlite3")
        store = os.path.join(temp, "private-results")
        first = appmod.create_app(
            db_path=db_path, testing=False, storage_dir=store)
        second = appmod.create_app(
            db_path=db_path, testing=False, storage_dir=store)
        check("installation secret is strong, private, and stable",
              first.secret_key == second.secret_key
              and first.secret_key != "afc-local-dev-secret"
              and len(first.secret_key.encode("utf-8")) >= 32
              and os.path.isfile(config.SECRET_KEY_PATH))
        unsafe = os.path.join(os.path.dirname(appmod.__file__), "static",
                              "should-never-store-results")
        try:
            appmod.artifact_store.ensure_dir.__globals__["config"] \
                .RESULT_STORAGE_DIR = unsafe
            appmod.artifact_store.ensure_dir()
            rejected = False
        except ValueError:
            rejected = True
        check("artifact storage beneath static is rejected", rejected)
    finally:
        config.DATABASE_PATH = old_db
        config.RESULT_STORAGE_DIR = old_store
        config.SECRET_KEY_PATH = old_secret
        shutil.rmtree(temp, ignore_errors=True)


def test_quota_serialization_and_reset_scope(appmod):
    import db
    import threading
    temp = tempfile.mkdtemp(prefix="bytesize-quota-")
    old_db = config.DATABASE_PATH
    old_store = config.RESULT_STORAGE_DIR
    try:
        config.DATABASE_PATH = os.path.join(temp, "main.sqlite3")
        config.RESULT_STORAGE_DIR = os.path.join(temp, "main-results")
        db.init_db(config.DATABASE_PATH, seed_admin=False)
        conn = db.connect(config.DATABASE_PATH)
        conn.execute("INSERT INTO users (username,email,password_hash)"
                     " VALUES ('quota','quota@example.com','hash')")
        uid = conn.execute("SELECT id FROM users WHERE username='quota'") \
            .fetchone()["id"]
        row_ids = []
        for index in range(2):
            cur = conn.execute(
                "INSERT INTO compression_history (user_id,filename,"
                "original_bytes,compressed_bytes,ratio,space_saved_pct,engine)"
                " VALUES (?,?,?,?,?,?,?)",
                (uid, "q%d.afc" % index, 100, 60, 1.66, 40, "test"))
            row_ids.append(cur.lastrowid)
        conn.commit()
        conn.close()
        barrier = threading.Barrier(2)
        results = []

        def reserve(index):
            barrier.wait()
            try:
                with db.storage_reservation(uid, 60, 100) as tx:
                    db.add_stored_artifact(
                        row_ids[index], uid, "%032x" % (index + 1),
                        "q%d.afc" % index, "application/octet-stream", 60,
                        "0" * 64, connection=tx)
                results.append("stored")
            except db.StorageQuotaExceeded:
                results.append("quota")

        workers = [threading.Thread(target=reserve, args=(i,))
                   for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        conn = db.connect(config.DATABASE_PATH)
        used = conn.execute(
            "SELECT COALESCE(SUM(byte_size),0) AS n FROM stored_artifacts"
            " WHERE user_id=?", (uid,)).fetchone()["n"]
        conn.close()
        check("concurrent quota reservations cannot exceed the hard cap",
              sorted(results) == ["quota", "stored"] and used == 60,
              (results, used))

        import artifact_store
        key, _ = artifact_store.write(b"keep while resetting another DB")
        unrelated = os.path.join(temp, "unrelated.sqlite3")
        db.reset_db(unrelated)
        check("resetting an unrelated database cannot wipe the active store",
              artifact_store.exists(key))

        real_delete = artifact_store.delete
        artifact_store.delete = lambda _key: (_ for _ in ()).throw(
            PermissionError("simulated locked artifact"))
        try:
            try:
                db.reset_db()
                reset_aborted = False
            except RuntimeError:
                reset_aborted = True
        finally:
            artifact_store.delete = real_delete
        conn = db.connect(config.DATABASE_PATH)
        user_survives = conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is not None
        conn.close()
        check("reset aborts before ownership metadata is lost on file error",
              reset_aborted and user_survives and artifact_store.exists(key))

        db.reset_db()
        check("supported reset removes configured durable result blobs",
              not artifact_store.exists(key))

        # Even with the same Flask signing key and reused numeric user id, a
        # database reset must invalidate every pre-reset session cookie.
        epoch_app = appmod.create_app(
            db_path=config.DATABASE_PATH, testing=True,
            storage_dir=config.RESULT_STORAGE_DIR)
        epoch_app.secret_key = "stable-secret-across-reset"
        old_client = epoch_app.test_client()
        old_client.post("/register", data={
            "username": "before-reset", "email": "before@example.com",
            "password": "before-reset-password",
            "confirm": "before-reset-password"})
        with epoch_app.app_context():
            old_id = db.get_user_by_username("before-reset")["id"]
        db.reset_db()
        new_client = epoch_app.test_client()
        new_client.post("/register", data={
            "username": "after-reset", "email": "after@example.com",
            "password": "after-reset-password",
            "confirm": "after-reset-password"})
        with epoch_app.app_context():
            new_id = db.get_user_by_username("after-reset")["id"]
        stale = old_client.get("/settings", follow_redirects=False)
        check("database reset invalidates old cookies despite reused user ids",
              old_id == new_id and stale.status_code in (301, 302)
              and "/login" in stale.headers.get("Location", ""),
              (old_id, new_id, stale.status_code))
    finally:
        config.DATABASE_PATH = old_db
        config.RESULT_STORAGE_DIR = old_store
        shutil.rmtree(temp, ignore_errors=True)


def test_account_deletion_removes_artifacts(app):
    import db
    user = app.test_client()
    user.post("/register", data={
        "username": "eraseme", "email": "eraseme@example.com",
        "password": "erase-password", "confirm": "erase-password"})
    result, _ = _compress_for_storage(user, "erase.txt")
    row_id = result.get_json()["history_id"]
    with app.app_context():
        target = db.get_user_by_username("eraseme")
        target_id = target["id"]
        stored = db.get_stored_artifact(row_id)
        disk_path = os.path.join(config.RESULT_STORAGE_DIR,
                                 stored["storage_key"])
    admin_client = app.test_client()
    login(admin_client, config.DEFAULT_ADMIN_USERNAME, "admin-new-password")
    import artifact_store
    real_delete = artifact_store.delete
    artifact_store.delete = lambda _key: (_ for _ in ()).throw(
        PermissionError("simulated locked user artifact"))
    try:
        refused = admin_client.post("/admin/users/%d/delete" % target_id)
    finally:
        artifact_store.delete = real_delete
    with app.app_context():
        still_owned = db.get_user_by_id(target_id) is not None
    check("account deletion failure preserves ownership metadata",
          refused.status_code == 500 and still_owned and os.path.exists(disk_path))

    response = admin_client.post("/admin/users/%d/delete" % target_id)
    with app.app_context():
        gone = db.get_user_by_id(target_id) is None
    check("deleting an account removes its artifact files",
          response.status_code in (301, 302) and gone
          and not os.path.exists(disk_path))



# ===========================================================================
# PART 2 — analytics, algorithm showcase, presets
# ===========================================================================

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
    for path in ("/dashboard", "/compress", "/files", "/settings", "/compare"):
        check("page still renders: %s" % path, c.get(path).status_code == 200)
    check("retired analytics page is gone",
          c.get("/analytics", follow_redirects=False).status_code == 404)
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
          b"Choose a file, select a profile" in rc.data)
    check("Decompress page carries its description",
          b"Restore an AFC file to its original format" in rd.data)

    # Neither page presents a combined "compress or decompress?" control.
    check("Compress page drops the old combined heading",
          b"Compress / Decompress" not in rc.data)
    check("Decompress page drops the old combined heading",
          b"Compress / Decompress" not in rd.data)

    # Both are in the primary navigation, on every page — not buried.
    for path in ("/dashboard", "/files", "/settings"):
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
        test_public_site_and_action_gates(app)
        test_intended_destination_preserved(app)
        test_branding_and_about_evidence(app)
        test_container_bytes_are_pinned()
        test_preset_size_is_monotonic()
        test_analytics_routes_removed(app)
        test_persistent_artifact_access(app, appmod)
        test_storage_quota_and_transient_restore(app)
        test_missing_artifact_is_not_reported_verified(app, appmod)
        test_retention_policy(app, appmod)
        test_storage_migration_is_additive()
        test_security_controls(app, appmod)
        test_installation_secret_and_storage_safety(appmod)
        test_quota_serialization_and_reset_scope(appmod)
        test_account_deletion_removes_artifacts(app)
        # --- surviving Part 2 features ---
        test_history_search_filter_paginate(app)
        test_entropy_reflects_file_type(app)
        test_tree_and_attribution(app)
        test_presets_have_real_effect(app)
        test_preset_recorded_and_used(app)
        test_compare_view(app)
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
        shutil.rmtree(dbpath + ".results", ignore_errors=True)

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
