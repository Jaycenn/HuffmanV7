# Deploying the hosted instance

This describes how to put the application on a public HTTPS address so study
participants can reach it from their own machines.  It is the last piece of the
General Objective's "hosted web application": after the Supabase migration the
*database* and the *object store* are hosted, but the Flask process still ran
only on a developer's laptop.

The target is a **Hugging Face Space using the Docker SDK**, on the free
`cpu-basic` hardware (2 vCPU, 16 GB RAM).  Section 3 shows the measurements
that led there.

Everything below was measured against this codebase.  Where a number is quoted,
the conditions that produced it are quoted with it.

---

## 1. What the deployment has to satisfy

Four constraints come out of the code and the host.  None of them is optional.

### 1.1 Exactly one worker, exactly one thread

`app.py` keeps finished results in a per-process dictionary:

```python
# app.py:112
RESULTS = {}
MAX_KEEP = 60
```

`_stash()` writes to it and `/download/<token>` reads from it.  Decompressed
originals exist *only* there — they are deliberately never written to disk.  Two
consequences:

* **Multiple workers break downloads.**  A participant who compresses a file on
  worker A and then clicks Download, load-balanced to worker B, gets a 404 for
  a token that worker B has never seen.  This is not a rare race; with two
  workers it is a coin flip on every download.
* **One worker with one thread is also the concurrency gate.**  `app.py:617`
  records that the batch queue is driven by the client, one request per file,
  because the DP parse peaks near 19x the input size and running files
  concurrently multiplies peak RSS instead of saving wall-clock time.  There is
  no server-side semaphore enforcing that.  A single sync worker *is* the
  enforcement: requests are served strictly one at a time, so two participants
  can never have two compressions in flight in the same process.

This also rules out any host that scales horizontally by default — Cloud Run
and similar would put each new container behind the same URL with its own empty
`RESULTS`, reproducing the download bug at the infrastructure layer.  A Space
is a single container, which satisfies this natively.

### 1.2 A generous worker timeout, and a much tighter gateway one

Compression happens synchronously inside the HTTP request (`_process_one`,
`app.py:429`), and it includes a full decompress-and-compare round trip before
the result is saved.  Gunicorn's default worker timeout is 30 seconds, which
would kill the worker part-way through anything large.  The `Dockerfile` sets
`--timeout 600`.

Gunicorn is not the binding limit, though.  **A Space sits behind a gateway
that returns 504 on requests taking much longer than a minute**, and the upload
itself is inside that same request.  This is what sets the file cap — see 3.3.

### 1.3 The native core must be built into the image

`afc_native.py` can compile `afc_native.cpp` lazily on first use, but on a
hosted instance that is the wrong behaviour twice over: it puts a ~10 second
compile inside the first participant's first request, and if the host image has
no compiler it fails *silently* and falls back to the pure-Python path, roughly
12x slower with byte-identical output.  A participant would experience that as
"the compressor is slow", and the PSSUQ scores would be measuring the wrong
engine.

The `Dockerfile` compiles it and then refuses the image unless
`tools/verify_native.py` confirms the library loads and every rung of the
ladder is reachable — a short ladder is not a slower build, it is a *different
experiment*, because `presets.ladder_for()` decides which profile wins.

Verified both ways.  Working:

```
native core: /home/user/HuffmanV7/afc_kernels.so
  ladder fast       2 profile(s)
  ladder balanced   6 profile(s)
  ladder maximum   12 profile(s)
```

and with the library removed and no compiler on PATH, it exits non-zero with
the loader's own diagnosis rather than continuing:

```
native core did not load: no usable C++ compiler. Tried g++: exited 127; ...
  [--] load afc_kernels.dll: /home/user/HuffmanV7/afc_kernels.dll: invalid ELF header.
  [--] build with g++: exit 127: (no output)
```

### 1.4 The Flask signing key must come from the environment

By default `app.py` persists a generated key to `.afc_secret` beside the source.
A Space's filesystem is not persistent, so that file is recreated on every
rebuild, which silently invalidates every session — participants would be
logged out mid-task by an unrelated redeploy.  Set `AFC_SECRET_KEY` as a Space
secret and the file is never touched (`app.py:215`).

---

## 2. Files added for deployment

| File | Purpose |
| --- | --- |
| `Dockerfile` | The image: g++ at build time, the native core compiled in and verified, gunicorn with one worker. |
| `.dockerignore` | Keeps `benchmarks/` (~15 MB of corpora nothing at runtime reads), `tests/`, and any local `.env` out of the image. |
| `tools/verify_native.py` | Fails a build whose native core did not load or whose ladder is short.  Used by both the `Dockerfile` and `build.sh`. |
| `requirements.txt` | Pinned runtime dependencies.  Five packages plus gunicorn; the engine itself imports nothing outside the standard library. |
| `Procfile`, `build.sh`, `.python-version` | A Procfile-host path (Render, Railway, Heroku-style), kept so the deployment is not locked to one provider. |
| README frontmatter | The YAML block at the top of `README.md` is how a Space is configured — `sdk: docker` and `app_port: 7860`.  GitHub renders it as a small table above the README; that is the cost of keeping one repository as the single source. |

No application code was changed.  `app = create_app()` is already a module-level
WSGI callable, so `gunicorn app:app` works against the code as it stands; the
`app.run(...)` block at the bottom of `app.py` is the local development entry
point and is simply not used by the host.

---

## 3. Sizing, and the file-size cap

Measured through the exact work `/api/compress` performs — the `balanced`
preset ladder, the AFC5 wrap, and the SHA-256 round-trip verification — on
**exactly two cores**, matching the Space's free `cpu-basic` hardware:

| Input | Wall time (2 vCPU) | Peak RSS |
| ---: | ---: | ---: |
| 10 MB | 4.3 s | 442 MB |
| 25 MB | 12.9 s | 606 MB |
| 50 MB | 28.4 s | 976 MB |
| 100 MB | 63.5 s | 1876 MB |

The idle application, fully imported, is 34 MB.

### 3.1 Why not a small Procfile host

Render's free web service is 512 MB RAM and **0.1 CPU** — a tenth of a core.
Reproduced here with a cgroup CPU quota (sanity-checked: 3.0 s of CPU work took
30.0 s wall, exactly 0.1):

| Input | 1 full vCPU | 0.1 CPU |
| ---: | ---: | ---: |
| 1 MB | 1.4 s | **23.5 s** |
| 5 MB | 4.9 s | **85.0 s** |

Memory was never the constraint there — peak RSS stayed at 274 MB for the 5 MB
file.  CPU was.  A participant compressing a 5 MB file would wait a minute and
a half and then rate that on the questionnaire, which measures the host's
billing tier rather than the software.  A Space's 2 vCPU is roughly twenty
times that hardware, for free.

### 3.2 Why these figures are higher than `SIZE_POLICY.md`

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
check that both are right.  Size the hosted instance from the ladder column —
that is what a participant's upload actually costs.

`AFC_PROFILE_MEMORY_BUDGET` exists to serialize the ladder on a constrained
host, but it is not a way around the memory cost: measured at 256 MB it helps
in the middle of the range (10 MB: 442 MB -> 237 MB) and does nothing at 50 MB
(976 MB -> 975 MB), because there the working set of a *single* profile already
dominates.

### 3.3 The cap is set by the gateway, not by memory

16 GB is far more memory than this workload needs — even the full 100 MB
ceiling peaks at 1.9 GB, about 12% of what the Space has.  **Memory is not the
binding constraint on a Space.  Time is.**

At 63.5 seconds, a 100 MB compression is already past the point where the
gateway in front of a Space starts returning 504, and that is compression time
alone.  The upload travels inside the same request, so a participant on a
typical home connection would spend another minute or more pushing the bytes up
before any compression starts.  A 100 MB cap would produce gateway timeouts
that look, from the participant's side, like the application crashing.

**Set `AFC_MAX_FILE_SIZE` to 25 MB (`26214400`).**  That is 12.9 seconds of
compression with room for the upload inside the same minute, and it leaves the
50 MB rung (28.4 s) as headroom rather than as the working limit.

### 3.4 What to disclose in the manuscript

Scope and Delimitations and Appendix C both state a 100 MB per-file ceiling.
The hosted instance will run at 25 MB, and quietly lowering it would make the
manuscript inaccurate about the artefact the participants actually used.

The honest framing — and a defensible one — is that these are two different
things:

* **The 100 MB ceiling is a property of the application**, and the compression
  evidence behind it (Appendix F, the GovDocs1 thread 000 run, the Silesia and
  Canterbury tables) was produced by `benchmarks/external_corpus.py` running
  locally, *not* through the hosted web instance.  Nothing in the compression
  results depends on the deployment.  The engine's behaviour at 100 MB is
  separately evidenced in `SIZE_POLICY.md`, which also records verified 150 MB
  and 250 MB runs.
* **The hosted instance is the vehicle for the usability study**, and it runs a
  25 MB per-file cap so that a request completes inside the host gateway's
  timeout.

For a usability session this costs nothing methodologically: participants
compress a document, a PDF, an image and a text file, all far below 25 MB.

### 3.5 Exact wording to add

Following the convention of `SIZE_POLICY.md` §4, which carries the sentence to
change if the documented ceiling is ever raised.  This is an addition, not a
replacement -- Appendix C's existing sentence ("The engine was tested with
files up to 100 MB in size and batches up to 500 MB.") stays as it is, because
it remains true of the application and of the benchmark harness.

**Add to Scope and Delimitations:**

> The demonstration instance used for the usability evaluation was hosted on
> free-tier infrastructure and configured with a 25 MB per-file limit rather
> than the 100 MB ceiling the application supports. This reduction is a
> property of the hosting environment, not of the compression system: on the
> hosted platform an upload and its compression share a single HTTP request,
> and the platform's gateway terminates requests exceeding approximately one
> minute. On the hosted hardware (2 vCPU) a 100 MB input requires 63.5 seconds
> of compression alone, leaving no margin for the upload. The compression
> results reported in this study were produced by the local benchmark harness
> and are unaffected by this limit.

**Add to Appendix C, after the existing ceiling sentence:**

> The stated ceiling applies to the application as installed and to the
> benchmark harness. The hosted demonstration instance used for the usability
> evaluation was configured to 25 MB (AFC_MAX_FILE_SIZE = 26214400) so that an
> upload and its compression complete within the hosting platform's request
> timeout.

Record the cap the hosted instance actually ran with alongside the PSSUQ
results, so the questionnaire responses are attributable to a known
configuration.

### 3.6 The alternative, and why not to take it

The only way to keep 100 MB on free hosting would be to stop the upload and the
compression sharing one request -- accept the upload, return immediately, run
the compression as a background job, and have the page poll for completion.
That is a real feature, and it works against three things this system is built
on: the single worker that enforces `MAX_CONCURRENT_JOBS == 1`, the per-process
`RESULTS` cache, and the fact that the application is meant to be frozen for
evaluation rather than gaining a job queue weeks before a defence.  A disclosed
delimitation is the cheaper and more honest answer.

---

## 4. Space secrets

The hosted instance points at the same Supabase project the local application
was cut over to, so participant accounts, history and stored artifacts are
already there and nothing needs seeding.

Add these under **Settings → Variables and secrets** on the Space.  Everything
carrying a credential goes in as a **Secret**, not a Variable — Variables are
visible to anyone who can see the Space.

**Required:**

| Name | Value | Kind |
| --- | --- | --- |
| `AFC_SECRET_KEY` | 64 hex characters, generated once — see 5.2.  Never regenerate it mid-study. | Secret |
| `AFC_DB_BACKEND` | `supabase` | Variable |
| `AFC_PG_DSN` | The session-pooler connection string, same as in your local `.env` | Secret |
| `AFC_STORAGE_BACKEND` | `supabase` | Variable |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` | Variable |
| `SUPABASE_SERVICE_ROLE_KEY` | The service role key | Secret |
| `SUPABASE_BUCKET` | `artifacts` | Variable |

**Strongly recommended:**

| Name | Value | Why |
| --- | --- | --- |
| `AFC_MAX_FILE_SIZE` | `26214400` | 25 MB — see 3.3.  Without it the app offers 100 MB and the gateway times those out. |
| `AFC_MAX_BATCH_SIZE` | `104857600` | 100 MB, four files at the cap, keeping batch totals inside the same envelope |
| `AFC_ADMIN_PASSWORD` | a strong password | Only used if the admin row is ever re-seeded; harmless to set |

Do not set `PORT` **on a Space**: the container binds `${PORT:-7860}`, and
7860 is what `app_port` in the README frontmatter tells the Space to route to,
so defining a `PORT` variable would move the listener away from the door the
router knocks at.  On a host that injects `PORT` itself (Railway and most
others), the same image binds whatever it is given — no change needed.

Note the DSN must use the **session pooler** host (username
`postgres.<project-ref>`), not the direct database host: the direct host is
IPv6-only and the container is not guaranteed IPv6.  This is the same failure
that produced the DNS error during the local cutover.

---

## 5. Deploying

### 5.1 Push the deployment files to GitHub

```
git add Dockerfile .dockerignore tools/verify_native.py requirements.txt \
        build.sh Procfile .python-version DEPLOYMENT.md README.md
git commit -m "Add the deployment scaffolding for the hosted instance"
git push
```

### 5.2 Generate the signing key

Run this once and keep the output:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5.3 Create the Space

On huggingface.co: **New → Space**.

* **Space name** — `bytesize` (the URL becomes `<your-username>-bytesize.hf.space`)
* **License** — whatever the thesis uses
* **SDK** — **Docker**, then **Blank** as the template
* **Hardware** — CPU basic, free
* **Visibility** — Public.  A private Space is visible only to you, so
  participants could not reach it.

### 5.4 Push the code to the Space

The Space is its own git repository.  Add it as a second remote and push:

```
git remote add space https://huggingface.co/spaces/<your-username>/bytesize
git push space <your-branch>:main
```

Git will ask for a username and password — the password is a Hugging Face
**access token** with write scope, created under Settings → Access Tokens, not
your account password.

The Space's README is replaced by yours, which is why the frontmatter has to be
in `README.md` on GitHub: it is the same file.

### 5.5 Add the secrets

Add every row from section 4 under **Settings → Variables and secrets**, then
**Factory rebuild** the Space so the container restarts with them.  A Space
that boots without `AFC_PG_DSN` will fail on startup trying to reach a database
it has no address for.

### 5.6 Verify

In order, and do not skip the third:

1. **The build log** (the Space's *Logs → Build* tab) must end with the three
   ladder lines from 1.3.  If it says the native core did not load, stop — the
   instance is running the pure-Python engine and any timing a participant sees
   is meaningless.
2. **The Settings page**, once logged in, must report the C++ native engine and
   name `afc_kernels.so` as the loaded library.
3. **A full round trip through the public URL**: log in, compress a file,
   download the `.afc`, upload that `.afc` back to the Decompress page,
   download the restored file, and confirm it is byte-identical to what you
   started with.  On Windows:

   ```powershell
   Compare-Object (Get-Content original.txt -Raw) (Get-Content restored.txt -Raw)
   ```

   No output means identical.  (`fc` in PowerShell is `Format-Custom`, not the
   file-compare tool — use `Compare-Object`, or `fc.exe` explicitly.)

This exact round trip was verified against gunicorn on Linux while preparing
these files: a 54,572-byte source file compressed to 21,695 bytes with
`"engine":"C++ native"` and `"lossless":true`, and the restored download
compared byte-identical to the original.

---

## 6. Known limits of the hosted instance

State these in the manuscript rather than discovering them during a session.

* **One participant at a time.**  Section 1.1.  With a 25 MB cap a compression
  is around 13 seconds, so a queued second participant sees a delay rather than
  a hang — but concurrent sessions are not supported.
* **A rebuild drops pending downloads.**  `RESULTS` is per-process, so a restart
  invalidates download tokens that have not been clicked yet.  Nothing durable
  is lost: compressed artifacts are in Supabase Storage and history is in
  PostgreSQL.  Do not rebuild during a session.
* **The Space sleeps after 48 hours idle** on free hardware, and the first
  request afterwards pays a cold start.  That is far more forgiving than the
  15-minute spin-down of a free Procfile host, but wake it before a session
  rather than in front of a participant.
* **Registration is open to anyone with the URL.**  A public Space is
  discoverable, so strangers can create accounts that land in the same Supabase
  tables as your participants.  Record each participant's username as you go so
  the analysis can filter cleanly.
* **The per-user storage quota still applies**
  (`AFC_MAX_STORED_BYTES_PER_USER`) and is now counted against Supabase Storage
  rather than a local directory.

---

## 7. Verified while preparing this

| Check | Result |
| --- | --- |
| `bash build.sh` end to end | dependencies resolved, `afc_kernels.so` built, ladder 2 / 6 / 12 |
| `tools/verify_native.py` failure path (no library, no compiler) | exits 1 with the loader's diagnostics |
| App served by gunicorn 26.2.0, single worker, on port 7860 | `/` 200, `/login` 200, unknown route 404 |
| Register, compress, download, decompress, download over HTTP | restored file byte-identical to the original |
| `python tests/test_app.py` on Linux / Python 3.11 | **541 passed, 0 failed** — the same count as the Windows run |

**Not verified:** the `Dockerfile` has not been built.  No Docker daemon was
available in the environment these files were prepared in, so every step inside
it was run natively instead — the g++ command, `tools/verify_native.py`, and
the exact gunicorn command line from `CMD`, all on Python 3.11.  The first real
build of the image will happen on the Space, which is why 5.6 starts with
reading the build log.
