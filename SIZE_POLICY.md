# SIZE_POLICY.md — file-size limits, measured rather than assumed

**Question put to us:** raise the UI caps to **1 MB minimum, 200 MB per file,
1 GB per batch**.

**Answer:** *partially — and not as a blanket default.* The 200 MB per-file cap
is safe **only when the C++ native core is available**; it is **not** safe on
the pure-Python fallback. The 1 MB minimum should **not** be adopted at all —
it would reject the entire thesis corpus. Details, with numbers, below.

Shipped defaults (`config.py`): `MIN_FILE_SIZE = 1 byte`,
`MAX_FILE_SIZE = 100 MB`, `MAX_BATCH_SIZE = 500 MB` — the ceiling Appendix C
documents as tested.

---

## 1. Measurements

Command: `python tools/size_policy_bench.py` (raw CSV:
`benchmarks/size_policy_results.csv`). Each size runs in a **fresh subprocess**
so peak RSS is attributable to that file alone. Input is a deterministic mixed
corpus (repetitive log lines + prose) that exercises all three scan tiers, so
these are not best-case numbers.

Machine: 4-core container, 16 GB RAM, Linux 6.18, g++ 13.3 `-O3`,
CPython 3.11.

### Native core (C++)

| Input | Compress | Decompress | Peak RSS | RSS ÷ input | Ratio | Lossless |
|---:|---:|---:|---:|---:|---:|:--|
| 8 MB | 3.65 s | 0.02 s | 162 MB | 20.3× | 5.56× | ✅ |
| 32 MB | 15.36 s | 0.13 s | 607 MB | 19.0× | 5.67× | ✅ |
| **100 MB** | **50.38 s** | 0.41 s | **1 610 MB** | 16.1× | 5.70× | ✅ |
| **150 MB** | **69.61 s** | 0.64 s | **2 394 MB** | 16.0× | 5.63× | ✅ |
| **250 MB** | **127.53 s** | 1.79 s | **3 986 MB** | 15.9× | 5.46× | ✅ |

### Pure-Python fallback

| Input | Compress | Decompress | Peak RSS | RSS ÷ input | Lossless |
|---:|---:|---:|---:|---:|:--|
| 8 MB | 43.59 s | 0.78 s | 690 MB | 86.2× | ✅ |
| 32 MB | 181.97 s | 3.51 s | 2 642 MB | 82.6× | ✅ |
| 150 MB | *not run* | — | — | — | — |
| 250 MB | *not run* | — | — | — | — |

The two large pure-Python rows were **not executed**, and the table says so
rather than inventing numbers. Extrapolating the measured, near-linear trend
(≈5.7 s/MB, ≈83× memory):

* 100 MB ≈ **9.5 minutes**, ≈ **8.3 GB** RSS
* 150 MB ≈ **14 minutes**, ≈ **12.4 GB** RSS
* 250 MB ≈ **24 minutes**, ≈ **20.6 GB** RSS — **exceeds a 16 GB machine**

These are labelled projections, not measurements. Re-run with
`python tools/size_policy_bench.py --python-large` on a machine with the RAM
to spare if the team wants them confirmed.

---

## 2. Findings

**a. The engine completes correctly at 150 MB and 250 MB — natively.**
Both sizes finished, stayed lossless (SHA-256 verified), and memory stayed
bounded and *linear*: peak RSS is a steady ~16× the input at these sizes, with
no thrash or blow-up. Compression time is likewise linear at ~0.51 s/MB.
Nothing degrades non-linearly on the way to 250 MB.

**b. Memory, not time, is the binding constraint.** The DP optimal parse
allocates three arrays across the whole input (`afc_native.cpp`, the
`cost`/`back`/`blen` vectors: 8 + 4 + 2 = 14 bytes per input byte) plus the
symbol stream. That is inherent to the parse the v4 engine uses, and reducing
it would mean changing the compression algorithm — explicitly out of scope.

**c. The pure-Python fallback cannot do 200 MB.** At ~83× memory it needs an
estimated ~16.5 GB for a 200 MB file. Pure Python is not a hypothetical path:
it is what runs on any machine without a C++ toolchain, which the README
presents as supported. A cap the fallback cannot honour would turn "works
everywhere" into "fails on the examiner's laptop".

**d. A 1 GB batch cap is memory-safe but slow.** Batches are processed
**sequentially, one file per request**, so peak memory is set by the *largest
file*, not the batch total. 1 GB of work at ~0.51 s/MB is ≈ **8.5 minutes** of
wall clock with no progress guarantees mid-file. The limit here is user
patience and HTTP timeouts, not RAM.

**e. The requested 1 MB minimum would break the study.** Every file in
`benchmarks/corpus/` and `benchmarks/canterbury/` is under 1 MB — the largest
is `server.log` at ~414 KB; `alice29.txt` is ~149 KB, `grammar.lsp` is 3.7 KB.
A 1 MB floor would make the app refuse the exact files the paper reports
results for. We did not implement it. `MIN_FILE_SIZE` defaults to 1 byte
(rejecting only genuinely empty files) and remains a config constant if the
team wants a different floor for a specific reason.

---

## 3. Recommendation

| Setting | Requested | Shipped default | Verdict |
|---|---|---|---|
| `MIN_FILE_SIZE` | 1 MB | **1 byte** | **Do not adopt.** Rejects the whole thesis corpus. |
| `MAX_FILE_SIZE` | 200 MB | **100 MB** | Adopt-able **only with the native core** and ≥6 GB free RAM. Keep 100 MB until Appendix C is updated. |
| `MAX_BATCH_SIZE` | 1 GB | **500 MB** | Technically safe (sequential), but ~8.5 min per full batch. Raise only if that wait is acceptable. |

**To raise the per-file cap to 200 MB**, in this order:

1. Update Appendix C (sentence in §4 below).
2. Set `AFC_MAX_FILE_SIZE=209715200` (or edit `config.py`).
3. Re-run `python tools/size_policy_bench.py` on the target machine and
   confirm it completes.

Also consider gating it at runtime: allow >100 MB only when `afc2.NATIVE` is
true, so a machine on the pure-Python fallback keeps the safe ceiling. That is
a ~5-line change in `app.size_error()` and is deliberately *not* applied yet —
it is the team's call, and it changes documented behaviour.

---

## 4. Exact sentence to update in the thesis (Appendix C)

If — and only if — the team chooses to raise the documented ceiling, replace
the current Appendix C sentence:

> The engine was tested with files up to 100 MB in size and batches up to
> 500 MB.

with:

> The engine was tested with individual files up to 250 MB and batches up to
> 500 MB. Using the C++ native core on a 4-core machine with 16 GB of RAM, a
> 250 MB input compressed in 127.5 s with a peak resident memory of 3 986 MB
> (approximately 16× the input size) and passed SHA-256 lossless verification;
> a 150 MB input compressed in 69.6 s using 2 394 MB. Memory consumption grows
> linearly with input size because the optimal-parse stage allocates per-byte
> state. The pure-Python fallback implementation was verified only to 32 MB
> (182.0 s, 2 642 MB); it requires approximately 83× the input size in memory
> and is not recommended above 32 MB.

Do not paste the second version until the caps are actually raised — the paper
should not claim a validated range wider than the software enforces.

---

## 5. Reproducing

```bash
python tools/size_policy_bench.py              # native 8→250 MB, python 8/32
python tools/size_policy_bench.py --quick      # 8/32 MB only (fast smoke test)
python tools/size_policy_bench.py --python-large   # adds python 150/250 (slow)
```

Results append to `benchmarks/size_policy_results.csv`. The generator writes
its synthetic input in 1 MB chunks and deletes each file after use, so the
sweep needs ~250 MB of scratch disk, not 500 MB.
