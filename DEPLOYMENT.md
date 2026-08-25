# Deploying the hosted instance

This describes how to put the application on a public HTTPS address so study
participants can reach it from their own machines.  It is the last piece of the
General Objective's "hosted web application": after the Supabase migration the
*database* and the *object store* are hosted, but the Flask process still ran
only on a developer's laptop.

The target is **Azure Container Apps**, on the Consumption plan under Azure for
Students.  The image is built and verified by GitHub Actions and published to
GitHub Container Registry, so no paid Azure Container Registry is needed.

Everything below was measured against this codebase.  Where a number is quoted,
the conditions that produced it are quoted with it.

---

## 1. What the deployment has to satisfy

### 1.1 One worker, one thread, one replica

`app.py` keeps finished results in a per-process dictionary:

```python
# app.py:112
RESULTS = {}
MAX_KEEP = 60
```

`_stash()` writes to it and `/download/<token>` reads from it.  Decompressed
originals exist *only* there — they are deliberately never written to disk.

* **Multiple workers break downloads.**  A participant who compresses a file on
  worker A and then clicks Download, routed to worker B, gets a 404 for a token
  B has never seen.  With two workers it is a coin flip on every download.
* **On Container Apps this extends to replicas.**  Every replica is a separate
  container with its own empty `RESULTS`.  Deploy with `--max-replicas 1`, or
  the same bug arrives from the platform instead of from gunicorn.
* **One worker with one thread is also the concurrency gate.**  `app.py:617`
  records that the batch queue is driven by the client, one request per file,
  because the DP parse peaks near 19x the input size and running files
  concurrently multiplies peak RSS instead of saving wall-clock time.  Nothing
  server-side enforces that; a single sync worker is the enforcement.

**What a restart actually costs.**  A replica that scales to zero and comes back
has an empty `RESULTS`.  That is survivable because `/files/<id>/download`
(`app.py:1190`) serves compressed artifacts straight from Supabase Storage with a
fresh SHA-256 check, independent of `RESULTS`.  A participant whose download
token has expired re-downloads from the Files page.  Only an un-downloaded
*decompressed* original is unrecoverable, and decompressing again regenerates it.

### 1.2 Two timeouts, and the tighter one belongs to the platform

Compression happens synchronously inside the HTTP request (`_process_one`,
`app.py:429`), and it includes a full decompress-and-compare round trip before
the result is saved.  Gunicorn's default worker timeout is 30 seconds, which
would kill the worker part-way through anything large; the `Dockerfile` sets
`--timeout 600`.

The binding limit is not gunicorn's.  **Container Apps' ingress applies a
default request timeout of 240 seconds**, and the upload travels inside the same
request as the compression.  Section 3.3 works out what that means for the file
cap — the short version is that 100 MB fits, but not on every connection.

### 1.3 The native core must be built into the image and verified there

`afc_native.py` can compile `afc_native.cpp` lazily on first use.  On a hosted
instance that is wrong twice over: it puts a ~10 second compile inside the first
participant's first request, and if the image has no compiler it fails
*silently* and falls back to the pure-Python path, roughly 12x slower with
byte-identical output.  A participant would experience that as "the compressor
is slow", and the PSSUQ scores would be measuring the wrong engine.

The `Dockerfile` compiles it and then runs `tools/verify_native.py` in the same
`RUN` layer, so a failure fails the build rather than shipping a slower engine.
A short ladder is not a slower build — it is a *different experiment*, because
`presets.ladder_for()` decides which profile wins.

**This is now verified in CI, not just claimed.**  `.github/workflows/publish-azure-image.yml`
builds the image on every push to `main` and `Aegyog-edits`.  Because
`verify_native.py` runs inside a `RUN` layer, a green build is proof that the
native core loaded and that all three ladders were reachable inside the image.

### 1.4 The Flask signing key must come from the environment

By default `app.py` persists a generated key to `.afc_secret` beside the source.
A container filesystem is ephemeral, so that file is regenerated on every
restart, which silently invalidates every session — participants would be logged
out mid-task by an ordinary scale event.  Set `AFC_SECRET_KEY` as a Container
Apps secret and the file is never touched (`app.py:215`).

### 1.5 Proxy trust has to be explicit

Container Apps terminates HTTPS and forwards to the container over plain HTTP,
one hop.  Two settings, both defaulting off so local development is unaffected:

* `AFC_TRUST_PROXY=1` installs `ProxyFix(x_for=1, x_proto=1, x_host=1, x_port=1)`
  (`app.py:1301`), so `request.remote_addr` and generated URLs reflect the real
  client rather than the ingress.
* `AFC_SECURE_COOKIES=1` sets `SESSION_COOKIE_SECURE`, so the session cookie is
  only ever sent over HTTPS.

`auth.py:73` keys login rate limiting on `request.remote_addr`, which makes
`x_for=1` load-bearing: if the ingress adds more than one `X-Forwarded-For`
entry, every client resolves to the same address and the rate limiter either
locks everyone out together or stops working.  Verify this once after the first
deploy — see 5.5.

---

## 2. Files involved

| File | Purpose |
| --- | --- |
| `Dockerfile` | The image: g++ at build time, native core compiled in and verified, gunicorn with one worker binding `${PORT:-7860}`. |
| `.dockerignore` | Keeps `benchmarks/` (~15 MB of corpora nothing at runtime reads), `tests/`, and any local `.env` out of the image. |
| `tools/verify_native.py` | Fails a build whose native core did not load or whose ladder is short.  Used by the `Dockerfile` and by `build.sh`. |
| `.github/workflows/publish-azure-image.yml` | Builds and publishes `ghcr.io/jaycenn/huffmanv7:azure` and a `:<sha>` tag on every push to `main` or `Aegyog-edits`. |
| `azure.env.example` | Inventory of the non-secret settings, plus the names of the four that must be Container Apps secrets. |
| `requirements.txt` | Pinned runtime dependencies.  Five packages plus gunicorn; the engine itself imports nothing outside the standard library. |
| `Procfile`, `build.sh`, `.python-version` | A Procfile-host path, kept so the deployment is not locked to one provider. |

No application code was changed for hosting beyond 1.5.  `app = create_app()` is
already a module-level WSGI callable, so `gunicorn app:app` works against the
code as it stands; the `app.run(...)` block at the bottom of `app.py` is the
local development entry point and is not used by the host.

---

## 3. Sizing, cost, and the file cap

Measured through the exact work `/api/compress` performs — the `balanced`
preset ladder, the AFC5 wrap, and the SHA-256 round-trip verification — on
**exactly two cores**, matching the `--cpu 2.0` the deploy asks for:

| Input | Wall time (2 vCPU) | Peak RSS |
| ---: | ---: | ---: |
| 10 MB | 4.3 s | 442 MB |
| 25 MB | 12.9 s | 606 MB |
| 50 MB | 28.4 s | 976 MB |
| 100 MB | 63.5 s | 1876 MB |

The idle application, fully imported, is 34 MB.  Container Apps pairs 2 GiB of
memory with each vCPU, so `--cpu 2.0 --memory 4.0Gi` is the natural size: it
matches the measured hardware and leaves better than 2x headroom over the
1876 MB peak at the documented 100 MB ceiling.

### 3.1 Why these figures are higher than `SIZE_POLICY.md`

`SIZE_POLICY.md` records peak RSS at a steady ~16-20x the input (8 MB ->
162 MB, 100 MB -> 1 610 MB).  The table above is roughly twice that in the
middle of the range, and the difference is not a contradiction — the two
measure different work.  `SIZE_POLICY.md` measures *one* compression;
`/api/compress` runs the preset **ladder**, several profiles whose concurrency
is bounded by `AFC_PROFILE_MEMORY_BUDGET`.  Measured side by side on identical
input:

| Input | One profile | Full `balanced` ladder |
| ---: | ---: | ---: |
| 10 MB | 211 MB (21.1x) | 442 MB |
| 25 MB | 487 MB (19.5x) | 605 MB |

The single-profile column reproduces `SIZE_POLICY.md` closely, which is the
check that both are right.  Size the instance from the ladder column — that is
what a participant's upload actually costs.

`AFC_PROFILE_MEMORY_BUDGET` exists to serialize the ladder on a constrained
host, but it is not a way around the memory cost: measured at 256 MB it helps
in the middle of the range (10 MB: 442 MB -> 237 MB) and does nothing at 50 MB
(976 MB -> 975 MB), because there a single profile's working set already
dominates.

### 3.2 Scale to zero, or the student credit disappears

Container Apps' Consumption plan grants 180,000 vCPU-seconds, 360,000
GiB-seconds and 2 million requests free per subscription per calendar month.
Those grants are generous *only* if the app is not running continuously:

| Setting | vCPU-seconds per month | Against the 180,000 grant |
| --- | ---: | --- |
| `--min-replicas 1` at 2 vCPU, always on | 5,184,000 | ~29x over |
| `--min-replicas 0`, ~300 compressions at 12.9 s | 7,740 | 4.3% of it |

Always-on is billed even while idle, at a reduced idle rate rather than free, and
against a fixed Azure for Students credit that adds up.  **Deploy with
`--min-replicas 0 --max-replicas 1`.**

The cost is a cold start on the first request after a quiet period — an image
pull plus application import.  For a scheduled session, warm it deliberately:

```
az containerapp update --name bytesize --resource-group bytesize-rg --min-replicas 1
```

and set it back to `0` afterwards.  That is the whole mitigation, and it keeps
cold starts away from participants without spending credit between sessions.

### 3.3 The cap stays at 100 MB, and what that risks

`azure.env.example` sets `AFC_MAX_FILE_SIZE=104857600` and
`AFC_MAX_BATCH_SIZE=524288000` — the ceiling Appendix C documents.  Container
Apps publishes no request-body size limit, so unlike some platforms nothing
rejects a large upload outright.  **The manuscript and the deployment agree, and
no delimitation sentence is needed.**

What constrains it instead is the 240-second ingress timeout, shared by the
upload and the compression:

| Participant's upstream | Upload of 100 MB | + 63.5 s compression | vs 240 s |
| --- | ---: | ---: | --- |
| 10 Mbps | ~80 s | ~144 s | fits |
| 5 Mbps | ~160 s | ~224 s | 16 s of margin |
| 3 Mbps | ~267 s | — | **times out during upload** |

A participant on a slow home connection uploading a file near the ceiling would
see what looks like the application crashing.  Three honest responses, in order
of preference:

1. **Test it.**  Upload a real 100 MB file from an off-campus connection before
   any session.  One test settles whether this is theoretical.
2. **Give participants files well under the ceiling.**  A usability session uses
   a document, a PDF, an image and a text file; none of them approach 100 MB, so
   the risk may never be exercised.
3. **Lower `AFC_MAX_FILE_SIZE`** if the test fails.  That reintroduces the
   delimitation sentence, which is why it is the last resort rather than the
   first.

Do not treat the 240 seconds as fixed without checking: if a raised ingress
timeout is available on the Consumption plan, that removes the problem without
touching the documented ceiling.

---

## 4. Environment variables and secrets

The hosted instance points at the same Supabase project the local application
was cut over to, so participant accounts, history and stored artifacts are
already there and nothing needs seeding.  `azure.env.example` is the inventory;
it carries no values and is safe in the repository.

**Four must be Container Apps secrets**, never plain environment variables:

| Secret | Why |
| --- | --- |
| `AFC_SECRET_KEY` | Forges sessions if leaked. |
| `AFC_PG_DSN` | Full database credentials. |
| `SUPABASE_SERVICE_ROLE_KEY` | Bypasses row-level security entirely. |
| `AFC_ADMIN_PASSWORD` | Administrative login. |

The rest are ordinary environment variables: `AFC_DB_BACKEND=supabase`,
`AFC_STORAGE_BACKEND=supabase`, `SUPABASE_URL`, `SUPABASE_BUCKET=artifacts`,
`AFC_TRUST_PROXY=1`, `AFC_SECURE_COOKIES=1`, the size limits, and the admin
identity.  Take the current list from `azure.env.example` rather than from here,
so there is one source of truth.

Do not set `PORT`.  The container binds `${PORT:-7860}`, and the deploy points
ingress at 7860.

Note the DSN must use the **session pooler** host (username
`postgres.<project-ref>`), not the direct database host: the direct host is
IPv6-only and the container is not guaranteed IPv6.  This is the same failure
that produced the DNS error during the local cutover.

---

## 5. Deploying

### 5.1 Confirm the image published

The workflow runs on every push.  Check the Actions tab: a green
**Publish Azure container image** run means the image built *and*
`tools/verify_native.py` passed inside it.  A red run means there is nothing
worth deploying yet.

### 5.2 Make the registry readable

The image lives at `ghcr.io/jaycenn/huffmanv7:azure`.  If that package is
private, Container Apps cannot pull it without credentials — either set the
package to public in its GitHub package settings, or pass
`--registry-server ghcr.io` with a username and a read-scoped token on the
create command below.

### 5.3 Generate the signing key

Run once and keep the output:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5.4 Create the app

```
az login
az group create --name bytesize-rg --location southeastasia
az containerapp env create --name bytesize-env --resource-group bytesize-rg --location southeastasia
```

Then create the app itself, with the secrets first and the environment variables
referring to them:

```
az containerapp create \
  --name bytesize \
  --resource-group bytesize-rg \
  --environment bytesize-env \
  --image ghcr.io/jaycenn/huffmanv7:azure \
  --target-port 7860 \
  --ingress external \
  --cpu 2.0 --memory 4.0Gi \
  --min-replicas 0 --max-replicas 1 \
  --secrets afc-secret-key=<key> pg-dsn=<dsn> supabase-key=<service-role-key> admin-password=<password> \
  --env-vars \
      AFC_SECRET_KEY=secretref:afc-secret-key \
      AFC_PG_DSN=secretref:pg-dsn \
      SUPABASE_SERVICE_ROLE_KEY=secretref:supabase-key \
      AFC_ADMIN_PASSWORD=secretref:admin-password \
      AFC_DB_BACKEND=supabase \
      AFC_STORAGE_BACKEND=supabase \
      SUPABASE_URL=https://<project-ref>.supabase.co \
      SUPABASE_BUCKET=artifacts \
      AFC_TRUST_PROXY=1 \
      AFC_SECURE_COOKIES=1 \
      AFC_MAX_FILE_SIZE=104857600 \
      AFC_MAX_BATCH_SIZE=524288000
```

Each flag that is not obvious earns its place:

| Flag | Why |
| --- | --- |
| `--max-replicas 1` | One `RESULTS` dictionary.  See 1.1. |
| `--min-replicas 0` | Scale to zero, so idle time is not billed.  See 3.2. |
| `--cpu 2.0 --memory 4.0Gi` | Matches the hardware every timing in section 3 was measured on, with headroom over the 1876 MB peak. |
| `--target-port 7860` | What the container binds when no `PORT` is injected. |
| `--ingress external` | Participants reach the app directly; the application's own login is the access control. |

To roll out a new build afterwards:

```
az containerapp update --name bytesize --resource-group bytesize-rg --image ghcr.io/jaycenn/huffmanv7:<sha>
```

Prefer the `:<sha>` tag over `:azure` for anything you want to be able to roll
back to a known image.

### 5.5 Verify

In order, and do not skip the third or the fourth:

1. **The Actions build log** must show the three ladder lines.  If the native
   core did not load the build should have failed rather than published; if it
   published anyway, read the log before deploying.
2. **The Settings page**, once logged in, must report the C++ native engine and
   name `afc_kernels.so` as the loaded library.
3. **A full round trip through the public URL**: log in, compress a file,
   download the `.afc`, upload that `.afc` back to the Decompress page,
   download the restored file, and confirm it is byte-identical.  On Windows:

   ```powershell
   Compare-Object (Get-Content original.txt -Raw) (Get-Content restored.txt -Raw)
   ```

   No output means identical.  (`fc` in PowerShell is `Format-Custom`, not the
   file-compare tool — use `Compare-Object`, or `fc.exe` explicitly.)
4. **Rate limiting sees distinct clients.**  Per 1.5, `x_for=1` is a guess about
   the ingress until tested.  Fail a login several times from one device, then
   try a correct login from a second device on a different network.  If the
   second device is also blocked, every client is resolving to the same address
   and `ProxyFix` needs a different hop count.

The round trip in step 3 was verified against gunicorn on Linux while preparing
these files: a 54,572-byte source file compressed to 21,695 bytes with
`"engine":"C++ native"` and `"lossless":true`, and the restored download
compared byte-identical to the original.

---

## 6. Known limits of the hosted instance

State these in the manuscript rather than discovering them during a session.

* **One participant at a time.**  Section 1.1.  A second participant's request
  queues behind the first rather than failing, but concurrent sessions are not
  supported.
* **Cold starts after idle.**  Scale-to-zero means the first request after a
  quiet period pays an image pull plus application import.  Warm the app before
  a session, per 3.2.
* **Large uploads on slow connections may hit the 240 s ingress timeout.**
  Section 3.3.
* **A restart drops pending download tokens, not data.**  Compressed artifacts
  remain in Supabase Storage and are re-downloadable from the Files page with a
  fresh SHA-256 check; history remains in PostgreSQL.  Only an un-downloaded
  decompressed original is lost, and decompressing again regenerates it.
* **Registration is open to anyone with the URL.**  The platform does not gate
  access; the application's own login does, but anyone can register.  Record
  each participant's username as you go so the analysis can filter cleanly.
* **The per-user storage quota still applies**
  (`AFC_MAX_STORED_BYTES_PER_USER`) and is now counted against Supabase Storage
  rather than a local directory.

---

## 7. Verification status

| Check | Result |
| --- | --- |
| Image builds, `verify_native.py` passes inside it | **Verified in CI** — the workflow's `RUN` layer would fail the build otherwise |
| `bash build.sh` end to end | dependencies resolved, `afc_kernels.so` built, ladder 2 / 6 / 12 |
| `tools/verify_native.py` failure path (no library, no compiler) | exits 1 with the loader's diagnostics |
| App served by gunicorn 26.2.0, single worker | `/` 200, `/login` 200, unknown route 404 |
| `${PORT:-7860}` with `PORT` unset and `PORT=8080` | binds 7860 and 8080 respectively, `/login` 200 on both |
| Register, compress, download, decompress, download over HTTP | restored file byte-identical to the original |
| `python tests/test_app.py` on Linux / Python 3.11 | **541 passed, 0 failed** — the same count as the Windows run |

**Not yet verified, and only a real deploy can settle them:** whether
`ProxyFix(x_for=1)` resolves the true client address behind Container Apps
ingress (5.5 step 4), and whether a 100 MB upload completes inside the 240-second
ingress timeout from a participant's connection (3.3).
