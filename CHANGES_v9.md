# CHANGES v9 — compression speed and size

Two separate pieces of work, in this order and with separate proofs.

## 1. Speed, with byte-identical output

Compression ran at ~1 MB/s on Balanced while decompression ran at ~160 MB/s.
Profiling the native core found the cause in the search structure rather than
the compression model: at every input position the optimal parse hashed the
input slice once per dictionary length starting with that byte — about **9
string hashes per byte on English text, of which ~9% found a pattern**. That
stage was 65–85% of total compression time.

* **Aho-Corasick automaton over the structural dictionary.** The walk reports
  exactly the entries that really end at each position, longest first. That
  order is not incidental: the old loop relaxed edges out of each position in
  ascending pattern length, which for a fixed target is source ascending —
  length *descending* — with the one-byte literal edge last. Reproducing it
  under the same strict `<` updates keeps the parse, and every emitted byte,
  unchanged. The same trie serves the greedy seed parse, where descending from
  the input position finds the longest match without hashing anything.
* **Root fast path.** Transitions out of the trie root read a flat 256-entry
  array, so data with a small or empty dictionary stops paying a hash per byte.
* **DP fixpoint exit.** The round loop stops once the parse stops moving:
  identical ids give identical code lengths, so the remaining rounds are
  provably no-ops.
* **Block growth scores without allocating.** A merge is scored from its two
  children's precomputed spelled cost and length; the concatenated string is
  built only for candidates that survive the Bit Cost Decision Engine, instead
  of once per distinct adjacent pair in the stream.
* **Open-addressed counters** for Tier-2 n-gram counting and adjacent-pair
  counting, sized from the input and doubling on load rather than reserving
  for the worst case.

Nothing about the compression model changed. Verified across 279
configurations (31 corpus files x 3 presets x 3 container formats): every
container byte-identical to the previous build.

| Preset | Before | After | |
|---|---|---|---|
| Fast | 8.14 MB/s | 17.85 MB/s | 2.2x |
| Balanced | 1.04 MB/s | 4.66 MB/s | 4.5x |
| Maximum | 0.41 MB/s | 2.27 MB/s | 5.6x |

## 2. Size, by searching instead of guessing

A preset could only search *deeper* — more DP rounds, more block-growth rounds
— because the rest of the engine's shape was compiled in as constants. Deeper
is not reliably smaller, and Maximum came out **larger** than Balanced on
several files: a deeper block-growth search admits structural blocks that pass
the Bit Cost Decision Engine's estimate but crowd the dictionary and lengthen
the codes of more valuable symbols.

Two structural alternatives were measured first and **rejected on the numbers**:

* *Independent per-block compression* (each block its own dictionary) is worse
  at every block size tried, 32 KB to 2 MB — the repeated dictionary costs more
  than local adaptation saves (+4.9% at best, +120% at worst).
* *One shared dictionary with per-block code tables* is also worse at every
  block count, 2 to 32 — the code-table header exceeds the local-adaptation
  gain (+0.45% at best).

What did pay: making the scan shape per-call. `scan_window`, `ngram_max`,
`max_initial_dict`, `max_dict`, `max_block` and `merges_per_round` moved from
`static const` into `Params` / `afc2.EngineOptions`, reachable through a new
`afc_compress_v9` export (the `afc_compress` and `afc_compress_ex` ABIs are
untouched, and a library without the new export degrades to the single default
profile rather than computing under the wrong parameters).

No single setting wins everywhere:

| Profile | Best | Worst |
|---|---|---|
| 16k dictionary | −8.4% on a 10 MB text | +10.3% on regular delimited CSV |
| 6-byte n-gram scan | −18.3% on a stored-DOCX | +27.6% on the JSON sample |
| 8-byte n-gram scan | −14.8% on multipage PDF text | +45.5% on that same JSON |

So the presets stop guessing. Each compresses the file under a **ladder of
profiles** and keeps the smallest container; ties keep the earliest profile,
so the result is deterministic. The ladders are nested —
`fast ⊂ balanced ⊂ maximum` — which makes **a costlier preset is never larger**
a property of the construction rather than a claim about a corpus.
`test_preset_size_is_monotonic` asserts it. Profiles run concurrently (ctypes
releases the GIL for a native call) with the worker count bounded by input
size, so a large file does not multiply peak memory;
`AFC_PROFILE_MEMORY_BUDGET` overrides the bound.

Above 8 MB the ladder also drops the wider n-gram scans, which is measured
rather than assumed: on multi-megabyte text `ngram6` and `ngram8` both come out
about +6% while the wider dictionary is the entire win, so the trimmed ladder
produces the same bytes for roughly half the work — 10 MB at Balanced goes from
4.55 s to 2.67 s with a byte-identical result. The trim applies to every preset
alike, so the ladders stay nested and the size ordering still holds. A 40 MB
input takes 2.3 s / 20 s / 65 s at Fast / Balanced / Maximum, all still faster
than the pre-v9 engine's single-profile times at the same presets.

Over the repository corpus, against the pre-v9 engine:

| Preset | Size | Saved | Wall clock |
|---|---|---|---|
| Fast | unchanged | 62.81% | 2.0x faster |
| Balanced | −7.71% | 66.60% → 69.17% | 2.0x faster |
| Maximum | −6.90% | 67.09% → 69.36% | 1.9x faster |

Both presets are smaller **and** faster than before.

## What did not change

* The compression model. Tier scans, the Bit Cost Decision Engine, structural
  block growth, length-limited canonical Huffman, and the AFC1/AFC2/AFC3/AFC4/
  AFC5/AFC6 container layouts are as they were. No LZ back-references, no
  arithmetic/range/ANS coding, no BWT/MTF, no second codec.
* The decoder. Every tunable a profile varies affects encoder decisions only;
  the dictionary and the code lengths are written into the container
  explicitly. 270 containers written by the pre-v9 engine were decoded by the
  v9 engine, native and pure-Python paths, byte-exact.
* The pure-Python reference remains authoritative: 210 (file, depth, profile)
  combinations produce byte-identical output on both backends.

## Evidence

* `benchmarks/v9_repo_corpus.py` regenerates `benchmarks/v9_repo_corpus.csv`
  over the corpus this repository actually carries; every row SHA-256-verifies
  its round trip. The About page quotes that file, and the AFC 1.3
  Canterbury/Silesia rows are now labelled as the historical audit rather than
  presented as current numbers.
* `test_container_bytes_are_pinned` fixes the container SHA-256 for a
  seven-file corpus at every preset, and carries the pre-v9 size alongside, so
  the engine can never silently regress past the version the corpus was first
  measured on.
* `benchmarks/govdocs1_tier_ablation.csv` covers 602 files drawn from the
  GovDocs1 thread 000 directory. The `--max-bytes` passed to
  `benchmarks/tier_ablation.py` was not recorded when the run was made and
  cannot be recovered from the repository, but it can be bounded from the
  selection itself. The 602 rows are exactly the thread 000 files satisfying
  `256 <= size <= cap`: the largest included file is 130,944 bytes, the
  smallest excluded one is 132,672 bytes, and a cap of 132,672 would admit
  603 files rather than 602. The cap therefore lies in [130,944, 132,672).
  The only power of two in that interval is 131,072 (128 KiB), which is the
  probable invocation, but that is an inference from the file selection and
  not a recovered record. `--min-bytes` was left at its 256 default: the
  smallest included file is 291 bytes and the largest thread 000 file below
  it is 237. The usage example in the harness docstring,
  `--max-bytes 400000`, is illustrative only and is not a record of either
  ablation's invocation; the committed `benchmarks/tier_ablation.csv`
  contains a 424,437-byte file.
