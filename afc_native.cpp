// afc_native.cpp — C++ performance core for the AFC engine (v4).
//
// Multi-Level Frequency Analysis & Hybrid Huffman only: this core
// accelerates the EXISTING pipeline stages (tier scans, Bit Cost Decision
// Engine, greedy + DP segmentation, structural block growth, strict final
// audit, canonical-Huffman coding, container emit/parse).  No LZ, no
// back-references, no arithmetic/range/ANS coding, no new algorithm classes.
//
// v4 additions:
//   afc_compress()  — the FULL pipeline (both modes, AFC1/AFC2/auto) in one
//                     native call, byte-identical to the pure-Python engine.
//   afc_decompress()— now reads AFC2 as well as legacy AFC1, and uses a
//                     table-driven multi-bit canonical decoder (single-level
//                     LUT) whenever max code length <= 16; legacy AFC1 files
//                     with longer codes fall back to the bit-by-bit walk.
//   Tier-2 scan     — one thread per n-gram length (2..5), deterministic.
//
// The v3 kernels (count_ngrams, segment_ids, pack_bits) remain exported for
// backwards compatibility with older bridges.
//
// Build (Linux/macOS):
//   g++ -O3 -std=c++17 -shared -fPIC -pthread afc_native.cpp -o afc_kernels.so
// Build (Windows MinGW):
//   g++ -O3 -std=c++17 -shared -static -pthread afc_native.cpp -o afc_kernels.dll
// Define AFC_NO_THREADS to build without std::thread (used for the
// single-threaded WebAssembly build; output bytes are identical).
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <numeric>
#include <string>
#include <string_view>
#ifndef AFC_NO_THREADS
#include <thread>
#endif
#include <unordered_map>
#include <vector>
using namespace std;

// ---------------------------------------------------------------------------
// [v7] DLL export visibility
// ---------------------------------------------------------------------------
// MinGW-w64 auto-exports every extern "C" symbol from a shared library, so
// `extern "C"` alone was enough there. MSVC does NOT: a cl.exe build of this
// file produces a DLL that exports NOTHING, the ctypes bridge then fails its
// hasattr(lib, "afc_compress") check, and the engine silently falls back to
// pure Python. Marking the entry points explicitly makes the DLL correct
// under MSVC, MinGW and clang-cl alike, and is harmless elsewhere.
#if defined(_WIN32) || defined(__CYGWIN__)
#  define AFC_API __declspec(dllexport)
#else
#  define AFC_API __attribute__((visibility("default")))
#endif

extern "C" {
AFC_API void afc_free(void* p) { free(p); }
}

// ============================================================================
// shared helpers — every rule here mirrors afc.py / afc2.py exactly
// ============================================================================

static const int NOCODE_COST = 18;
static const int MAX_CODE_LEN = 16;
static const uint32_t SCAN_WINDOW = 1u << 20;
static const int NGRAM_MIN = 2, NGRAM_MAX = 5;
static const int MIN_CANDIDATE_FREQ = 4;
static const int WORD_MIN = 3, WORD_MAX = 24;
static const uint32_t MAX_INITIAL_DICT = 3072;
static const uint32_t MAX_DICT = 4096;
static const uint32_t MAX_BLOCK = 128;
static const int MERGE_ROUNDS_V4 = 6;
static const int MERGES_PER_ROUND = 32;
static const int DP_ROUNDS = 3;
static const uint32_t TUNE_SMALL = 65536;

static inline int bitlen_u32(uint32_t q) {
  int bits = 0;
  while (q) {
    ++bits;
    q >>= 1;
  }
  return bits;
}

static inline int est_code_len(uint32_t f, uint32_t total) {
  if (f == 0) return NOCODE_COST;
  uint32_t q = total / f;
  if (q < 1) q = 1;
  int L = bitlen_u32(q);
  return L < 1 ? 1 : (L > 24 ? 24 : L);
}

static void write_varint(string& out, uint64_t v) {
  for (;;) {
    uint8_t b = v & 0x7F;
    v >>= 7;
    if (v) out.push_back((char)(b | 0x80));
    else { out.push_back((char)b); return; }
  }
}

// bounds-checked varint reader; returns false on overrun
static bool read_varint(const uint8_t* b, uint32_t n, uint32_t& pos,
                        uint64_t& v) {
  v = 0;
  int sh = 0;
  for (;;) {
    if (pos >= n || sh > 63) return false;
    uint8_t x = b[pos++];
    v |= (uint64_t)(x & 0x7F) << sh;
    if (!(x & 0x80)) return true;
    sh += 7;
  }
}

// ---- deterministic package-merge (mirrors afc.package_merge_lengths) ------
static void package_merge(vector<pair<uint32_t, uint32_t>> items,  // (freq,id)
                          int limit, unordered_map<uint32_t, int>& out) {
  sort(items.begin(), items.end());  // (freq asc, id asc)
  size_t n = items.size();
  out.clear();
  if (n == 0) return;
  if (n == 1) { out[items[0].second] = 1; return; }
  vector<uint64_t> prev_w;
  vector<vector<uint32_t>> prev_l;
  for (int lvl = 0; lvl < limit; ++lvl) {
    vector<uint64_t> pack_w;
    vector<vector<uint32_t>> pack_l;
    for (size_t i = 0; i + 1 < prev_w.size(); i += 2) {
      pack_w.push_back(prev_w[i] + prev_w[i + 1]);
      vector<uint32_t> m = prev_l[i];
      m.insert(m.end(), prev_l[i + 1].begin(), prev_l[i + 1].end());
      pack_l.push_back(move(m));
    }
    vector<uint64_t> cur_w;
    vector<vector<uint32_t>> cur_l;
    size_t li = 0, pi = 0;
    while (li < n || pi < pack_w.size()) {
      if (pi >= pack_w.size() ||
          (li < n && (uint64_t)items[li].first <= pack_w[pi])) {
        cur_w.push_back(items[li].first);
        cur_l.push_back({(uint32_t)li});
        ++li;
      } else {
        cur_w.push_back(pack_w[pi]);
        cur_l.push_back(move(pack_l[pi]));
        ++pi;
      }
    }
    prev_w = move(cur_w);
    prev_l = move(cur_l);
  }
  vector<int> lengths(n, 0);
  size_t take = 2 * n - 2;
  for (size_t i = 0; i < take && i < prev_l.size(); ++i)
    for (uint32_t k : prev_l[i]) lengths[k]++;
  for (size_t k = 0; k < n; ++k)
    out[items[k].second] = lengths[k] < 1 ? 1 : lengths[k];
}

// ---- canonical code assignment (length asc, id asc) ------------------------
struct Codes {
  vector<uint32_t> code;
  vector<uint8_t> len;
  void build(const unordered_map<uint32_t, int>& lengths, uint32_t max_id) {
    code.assign(max_id + 1, 0);
    len.assign(max_id + 1, 0);
    vector<pair<int, uint32_t>> syms;  // (len, id)
    syms.resize(lengths.size());
    transform(lengths.begin(), lengths.end(), syms.begin(),
              [](const auto& kv) {
                return make_pair(kv.second, kv.first);
              });
    sort(syms.begin(), syms.end());
    uint32_t c = 0;
    int prev = syms.empty() ? 1 : syms[0].first;
    for (const auto& s : syms) {
      c <<= (s.first - prev);
      prev = s.first;
      code[s.second] = c;
      len[s.second] = (uint8_t)s.first;
      ++c;
    }
  }
};

struct BitWriter {
  string buf;
  uint64_t acc = 0;
  int nb = 0;
  void put(uint32_t c, int L) {
    acc = (acc << L) | c;
    nb += L;
    while (nb >= 8) {
      nb -= 8;
      buf.push_back((char)((acc >> nb) & 0xFF));
      acc &= (1ULL << nb) - 1ULL;
    }
  }
  void finish() {
    if (nb) {
      buf.push_back((char)((acc << (8 - nb)) & 0xFF));
      acc = 0;
      nb = 0;
    }
  }
};

// ============================================================================
// container emit (mirrors afc.emit_afc1 / emit_afc2 / finish_container)
// ============================================================================

static string emit_afc1_c(int mode, uint64_t orig,
                          const vector<string>& patterns,
                          const vector<pair<uint32_t, int>>& lens_sorted,
                          const string& bits) {
  string out("AFC1", 4);
  out.push_back((char)mode);
  write_varint(out, orig);
  write_varint(out, patterns.size());
  for (const auto& p : patterns) {
    write_varint(out, p.size());
    out += p;
  }
  write_varint(out, lens_sorted.size());
  for (const auto& kv : lens_sorted) {
    write_varint(out, kv.first);
    out.push_back((char)kv.second);
  }
  out += bits;
  return out;
}

static string emit_afc2_c(int mode, uint64_t orig,
                          const vector<string>& patterns,
                          const vector<pair<uint32_t, int>>& lens_sorted,
                          const Codes& codes, const string& bits) {
  string out("AFC2", 4);
  out.push_back((char)mode);
  write_varint(out, orig);
  size_t U = lens_sorted.size();
  write_varint(out, U);
  int64_t prev = -1;
  for (const auto& kv : lens_sorted) {
    write_varint(out, prev < 0 ? kv.first : kv.first - prev - 1);
    prev = kv.first;
  }
  string nib((U + 1) / 2, '\0');
  for (size_t k = 0; k < U; ++k) {
    int v = lens_sorted[k].second - 1;  // lengths 1..16 guaranteed
    if (k & 1) nib[k >> 1] |= (char)v;
    else nib[k >> 1] = (char)(v << 4);
  }
  out += nib;
  write_varint(out, patterns.size());
  if (!patterns.empty()) {
    bool can = true;
    size_t rawlen = 0;
    for (const auto& p : patterns) {
      rawlen += p.size();
      if (any_of(p.begin(), p.end(),
                 [&](unsigned char b) { return codes.len[b] == 0; })) {
        can = false;
      }
      if (!can) break;
    }
    string blob;
    if (can) {
      BitWriter bw;
      for (const auto& p : patterns)
        for (unsigned char b : p) bw.put(codes.code[b], codes.len[b]);
      bw.finish();
      blob = move(bw.buf);
    }
    if (can && blob.size() < rawlen) {
      out.push_back((char)1);
      for (const auto& p : patterns) write_varint(out, p.size());
      out += blob;
    } else {
      out.push_back((char)0);
      for (const auto& p : patterns) write_varint(out, p.size());
      for (const auto& p : patterns) out += p;
    }
  }
  out += bits;
  return out;
}

static string emit_raw_c(const uint8_t* data, uint32_t n, bool afc2magic) {
  string out(afc2magic ? "AFC2" : "AFC1", 4);
  out.push_back((char)0);
  write_varint(out, n);
  out.append((const char*)data, n);
  return out;
}

// fmt: 0=auto, 1=afc1, 2=afc2
static string finish_container_c(int mode, const uint8_t* data, uint32_t n,
                                 const vector<string>& patterns,
                                 const unordered_map<uint32_t, int>& lengths,
                                 const Codes& codes, const string& bits,
                                 int fmt) {
  vector<pair<uint32_t, int>> lens_sorted;
  lens_sorted.resize(lengths.size());
  transform(lengths.begin(), lengths.end(), lens_sorted.begin(),
            [](const auto& kv) {
              return make_pair(kv.first, kv.second);
            });
  sort(lens_sorted.begin(), lens_sorted.end());
  string blob;
  if (fmt == 1) {
    blob = emit_afc1_c(mode, n, patterns, lens_sorted, bits);
  } else if (fmt == 2) {
    blob = emit_afc2_c(mode, n, patterns, lens_sorted, codes, bits);
  } else {
    blob = emit_afc1_c(mode, n, patterns, lens_sorted, bits);
    bool le16 = all_of(
        lens_sorted.begin(), lens_sorted.end(),
        [](const auto& kv) { return kv.second >= 1 && kv.second <= 16; });
    if (le16) {
      string b2 = emit_afc2_c(mode, n, patterns, lens_sorted, codes, bits);
      if (b2.size() < blob.size()) blob = move(b2);
    }
  }
  string raw = emit_raw_c(data, n, fmt == 2);
  return raw.size() <= blob.size() ? raw : blob;
}

// ============================================================================
// v4 adaptive pipeline (mirrors afc2._compress_core)
// ============================================================================

static bool word_char(uint8_t c) {
  return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') ||
         (c >= 'a' && c <= 'z') || c == '_';
}

static int64_t bit_cost_gain(const string& pat, uint32_t f,
                             const int* lit_bits, int sym_bits) {
  int64_t spelled = 0;
  spelled = accumulate(
      pat.begin(), pat.end(), int64_t{0},
      [&](int64_t total, unsigned char b) {
        return total + lit_bits[b];
      });
  return (int64_t)f * (spelled - sym_bits) - 8 * ((int64_t)pat.size() + 3);
}

// 64-bit finalizer shared by the open-addressed tables and the dictionary
// automaton below.
static inline uint64_t hash_mix(uint64_t x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
  x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
  x ^= x >> 33; return x;
}

// [v9] Open-addressed 64-bit key counter, used by the two loops that touch
// every byte (or every symbol) of the input: Tier-2 n-gram counting and
// adjacent-pair counting during block growth.  A std::unordered_map spends
// most of its time on node allocation and modulo bucketing here.
//
// It starts small and doubles on a 0.5 load factor rather than reserving for
// the theoretical maximum, because the number of DISTINCT keys is usually far
// below the number of positions — reserving for the worst case meant zeroing
// hundreds of megabytes per round on a large file.
//
// Iteration order over the slots is unspecified, which is safe: every
// consumer sorts its candidates under a total order before using them.
struct U64Counter {
  vector<uint64_t> key;   // 0 = empty; a real key is stored as key+1
  vector<uint32_t> cnt;
  uint64_t mask = 0;
  size_t used = 0, limit = 0;

  void init(size_t hint) {
    // Small inputs must not pay for a large table: a few-hundred-byte file
    // was spending most of its compression time zeroing hash slots.
    size_t cap = 256;
    if (hint > 128) { while (cap < hint && cap < (1u << 22)) cap <<= 1; }
    key.assign(cap, 0);
    cnt.assign(cap, 0);
    mask = cap - 1;
    used = 0;
    limit = cap / 2;
  }
  void grow() {
    vector<uint64_t> ok;
    vector<uint32_t> oc;
    ok.swap(key);
    oc.swap(cnt);
    size_t cap = (ok.size() << 1);
    key.assign(cap, 0);
    cnt.assign(cap, 0);
    mask = cap - 1;
    limit = cap / 2;
    for (size_t i = 0; i < ok.size(); ++i) {
      if (!ok[i]) continue;
      uint64_t j = hash_mix(ok[i]) & mask;
      while (key[j]) j = (j + 1) & mask;
      key[j] = ok[i];
      cnt[j] = oc[i];
    }
  }
  inline void add(uint64_t k) {
    uint64_t kk = k + 1;
    uint64_t i = hash_mix(kk) & mask;
    for (;;) {
      if (key[i] == kk) { ++cnt[i]; return; }
      if (key[i] == 0) {
        key[i] = kk;
        cnt[i] = 1;
        if (++used > limit) grow();
        return;
      }
      i = (i + 1) & mask;
    }
  }
};

// [v9] Every tunable the search profiles vary lives here.  The five original
// fields keep their meaning and their defaults, so afc_compress and
// afc_compress_ex behave exactly as before; the six added below were
// `static const` until now, which is why a preset could only ever deepen the
// search rather than reshape it.
struct Params {
  int dp = 1;
  int dp_rounds = DP_ROUNDS;
  int merge_rounds = MERGE_ROUNDS_V4;
  int min_freq = MIN_CANDIDATE_FREQ;
  int tune = 1;
  uint32_t scan_window = SCAN_WINDOW;
  int ngram_max = NGRAM_MAX;
  uint32_t max_initial_dict = MAX_INITIAL_DICT;
  uint32_t max_dict = MAX_DICT;
  uint32_t max_block = MAX_BLOCK;
  int merges_per_round = MERGES_PER_ROUND;

  void clamp() {
    if (dp_rounds < 0) dp_rounds = 0;
    if (merge_rounds < 0) merge_rounds = 0;
    // min_freq must stay >= 2: the small-file trial also runs min_freq - 1,
    // and a floor of 0 would admit every n-gram and blow up the candidate set.
    if (min_freq < 2) min_freq = 2;
    if (scan_window < 1024u) scan_window = 1024u;
    // n-gram keys are packed into a uint64, so 8 bytes is the hard ceiling.
    if (ngram_max < NGRAM_MIN) ngram_max = NGRAM_MIN;
    if (ngram_max > 8) ngram_max = 8;
    if (max_dict < 256u) max_dict = 256u;
    if (max_initial_dict > max_dict) max_initial_dict = max_dict;
    if (max_block < 2u) max_block = 2u;
    if (max_block > 65535u) max_block = 65535u;
    if (merges_per_round < 1) merges_per_round = 1;
  }
};

// Tier-2 n-gram counting for one length (thread worker).  n-grams of
// length <= 8 pack into a uint64 key (big-endian byte order), which is much
// faster to hash than heap strings; the counts are identical.
static void count_len(const uint8_t* win, uint32_t wn, int L,
                      U64Counter* m) {
  m->init((size_t)wn + 8 < (1u << 16) ? (size_t)wn + 8 : (size_t)(1u << 16));
  if ((uint32_t)L > wn) return;
  uint64_t key = 0;
  const uint64_t mask =
      L >= 8 ? ~0ULL : (((uint64_t)1 << (8 * L)) - 1);
  for (int k = 0; k < L - 1; ++k) key = (key << 8) | win[k];
  for (uint32_t i = 0; i + L <= wn; ++i) {
    key = ((key << 8) | win[i + L - 1]) & mask;
    m->add(key);
  }
}

static void select_candidates(const uint8_t* data, uint32_t n, int min_freq,
                              const int* lit_bits, const Params& P,
                              vector<string>& patterns) {
  uint32_t wn = n < P.scan_window ? n : P.scan_window;
  const int ngram_max = P.ngram_max;
  // Tier-2: one thread per n-gram length (deterministic: separate maps);
  // below ~16 KB thread spawn costs more than the scan itself, so go serial
  U64Counter maps[8 - NGRAM_MIN + 1];
#ifndef AFC_NO_THREADS
  if (wn >= 16384) {
    vector<thread> th;
    for (int L = NGRAM_MIN; L <= ngram_max; ++L)
      th.emplace_back(count_len, data, wn, L, &maps[L - NGRAM_MIN]);
    for (auto& t : th) t.join();
  } else
#endif
  {
    for (int L = NGRAM_MIN; L <= ngram_max; ++L)
      count_len(data, wn, L, &maps[L - NGRAM_MIN]);
  }
  unordered_map<string, uint32_t> cands;
  cands.reserve(maps[0].used + maps[1].used + 64);
  for (int L = NGRAM_MIN; L <= ngram_max; ++L) {
    const U64Counter& m = maps[L - NGRAM_MIN];
    for (size_t slot = 0; slot < m.key.size(); ++slot) {
      if (m.key[slot] == 0 || m.cnt[slot] < (uint32_t)min_freq) continue;
      char buf[8];
      uint64_t k = m.key[slot] - 1;
      for (int j = L - 1; j >= 0; --j) {
        buf[j] = (char)(k & 0xFF);
        k >>= 8;
      }
      cands[string(buf, (size_t)L)] = m.cnt[slot];
    }
  }
  // Tier-3: word tokens (maximal [0-9A-Z_a-z] runs, 3..24 bytes)
  {
    unordered_map<string, uint32_t> words;
    uint32_t i = 0;
    while (i < n) {
      if (word_char(data[i])) {
        uint32_t j = i;
        while (j < n && word_char(data[j])) ++j;
        uint32_t L = j - i;
        if (L >= (uint32_t)WORD_MIN && L <= (uint32_t)WORD_MAX)
          ++words[string((const char*)data + i, L)];
        i = j;
      } else {
        ++i;
      }
    }
    for (const auto& kv : words)
      if (kv.second >= (uint32_t)min_freq &&
          cands.find(kv.first) == cands.end())
        cands[kv.first] = kv.second;
  }
  // Bit Cost Decision Engine gate + deterministic ranking
  vector<pair<int64_t, string>> scored;
  scored.reserve(cands.size());
  for (const auto& kv : cands) {
    int64_t gain = bit_cost_gain(kv.first, kv.second, lit_bits,
                                 est_code_len(kv.second, n));
    if (gain > 0) scored.push_back({gain, kv.first});
  }
  sort(scored.begin(), scored.end(),
       [](const pair<int64_t, string>& a, const pair<int64_t, string>& b) {
         if (a.first != b.first) return a.first > b.first;  // gain desc
         return a.second < b.second;                         // pattern asc
       });
  patterns.clear();
  size_t take = scored.size() < P.max_initial_dict ? scored.size()
                                                   : P.max_initial_dict;
  patterns.reserve(take);
  for (size_t k = 0; k < take; ++k) patterns.push_back(move(scored[k].second));
}

// ---------------------------------------------------------------------------
// [v9] structural-dictionary automaton (Aho-Corasick)
// ---------------------------------------------------------------------------
// Both segmentation passes need to know which dictionary entries occur where.
// The previous index answered that by hashing the input slice once per
// candidate length at each position: ~9 string hashes per byte on English
// text, of which ~9% found anything, which made the DP parse 65-85% of total
// compression time.
//
// A trie answers both questions directly and without hashing input bytes:
//
//   * greedy parse  -- descend from the root at position i and keep the
//     deepest terminal seen; that IS the longest match, so the emitted ids
//     are the same ones the length-ordered probe produced.
//   * DP parse      -- walk the automaton once across the input.  After
//     consuming data[j-1] the terminal chain hanging off the current node is
//     exactly the set of entries ending at j, enumerated LONGEST FIRST.
//
// That order matters and is not incidental.  The old DP relaxed edges out of
// each position i in ascending pattern length; for a fixed target j that is
// i ascending, i.e. length DESCENDING, with the one-byte literal edge (from
// i == j-1) relaxed last.  Reproducing that order under strict `<` updates is
// what keeps the parse — and therefore every emitted container — byte for
// byte identical to the pre-automaton engine.
//
// Nothing about the compression model changes: the dictionary, the Bit Cost
// Decision Engine and the canonical Huffman coder are untouched.  This is a
// search-structure change only.
struct Automaton {
  vector<int32_t> fail;      // failure link
  vector<int32_t> out_link;  // next terminal up the failure chain (0 = none)
  vector<int32_t> term_pat;  // pattern id ending at this node, else -1
  vector<uint16_t> term_len; // its length (<= Params::max_block)
  vector<uint64_t> gk;       // open-addressed goto edges, key (node<<8|byte)+1
  vector<int32_t> gv;
  vector<int32_t> root_next; // depth-1 transitions, -1 when absent
  uint64_t gmask = 0;
  bool has_unit_pattern = false;   // a length-1 entry; see segment_optimal

  inline int32_t go(int32_t node, uint8_t c) const {
    uint64_t key = (((uint64_t)(uint32_t)node << 8) | c) + 1;
    uint64_t i = hash_mix(key) & gmask;
    for (;;) {
      uint64_t k = gk[i];
      if (k == key) return gv[i];
      if (k == 0) return -1;
      i = (i + 1) & gmask;
    }
  }

  // Advance one input byte, following failure links when the edge is absent.
  // Transitions out of the root are by far the most common (every byte that
  // does not extend a live match lands there), and on data with a small or
  // empty dictionary they are ALL of them — so they read a flat array rather
  // than hashing.
  inline int32_t step(int32_t node, uint8_t c) const {
    for (;;) {
      if (node == 0) {
        int32_t t = root_next[c];
        return t < 0 ? 0 : t;
      }
      int32_t t = go(node, c);
      if (t >= 0) return t;
      node = fail[node];
    }
  }

  void put(int32_t node, uint8_t c, int32_t child) {
    uint64_t key = (((uint64_t)(uint32_t)node << 8) | c) + 1;
    uint64_t i = hash_mix(key) & gmask;
    while (gk[i] != 0) {
      if (gk[i] == key) { gv[i] = child; return; }
      i = (i + 1) & gmask;
    }
    gk[i] = key;
    gv[i] = child;
  }

  void build(const vector<string>& patterns) {
    size_t edges = 1;
    edges = accumulate(
        patterns.begin(), patterns.end(), edges,
        [](size_t total, const string& p) {
          return total + p.size();
        });
    size_t cap = 16;
    while (cap < edges * 2) cap <<= 1;      // load factor <= 0.5
    gk.assign(cap, 0);
    gv.assign(cap, -1);
    root_next.assign(256, -1);
    gmask = cap - 1;

    fail.assign(1, 0);
    out_link.assign(1, 0);
    term_pat.assign(1, -1);
    term_len.assign(1, 0);
    has_unit_pattern = false;
    // Child lists are needed for the BFS below and thrown away afterwards;
    // the steady-state lookup path is the flat goto table alone.
    vector<vector<pair<uint8_t, int32_t>>> kids(1);

    for (uint32_t pi = 0; pi < patterns.size(); ++pi) {
      const string& p = patterns[pi];
      if (p.empty()) continue;
      if (p.size() == 1) has_unit_pattern = true;
      int32_t node = 0;
      for (unsigned char ch : p) {
        int32_t nxt = go(node, ch);
        if (nxt < 0) {
          nxt = (int32_t)fail.size();
          fail.push_back(0);
          out_link.push_back(0);
          term_pat.push_back(-1);
          term_len.push_back(0);
          kids.push_back({});
          put(node, ch, nxt);
          if (node == 0) root_next[ch] = nxt;
          kids[node].push_back({ch, nxt});
        }
        node = nxt;
      }
      // Duplicate entries cannot occur (the dictionary is de-duplicated
      // upstream), but keeping the first id is the deterministic choice.
      if (term_pat[node] < 0) {
        term_pat[node] = (int32_t)pi;
        term_len[node] = (uint16_t)p.size();
      }
    }

    vector<int32_t> queue;
    queue.reserve(fail.size());
    for (const auto& kv : kids[0]) {
      fail[kv.second] = 0;
      out_link[kv.second] = 0;
      queue.push_back(kv.second);
    }
    for (size_t qi = 0; qi < queue.size(); ++qi) {
      int32_t u = queue[qi];
      for (const auto& kv : kids[u]) {
        uint8_t c = kv.first;
        int32_t v = kv.second;
        int32_t f = fail[u];
        for (;;) {
          int32_t t = go(f, c);
          if (t >= 0 && t != v) { fail[v] = t; break; }
          if (f == 0) { fail[v] = 0; break; }
          f = fail[f];
        }
        out_link[v] = term_pat[fail[v]] >= 0 ? fail[v] : out_link[fail[v]];
        queue.push_back(v);
      }
    }
  }
};

// Greedy longest-match seed parse.  Descending the trie from the input
// position visits each candidate byte once instead of re-hashing the slice
// for every dictionary length that starts with the same byte; the deepest
// terminal reached is by definition the longest match, so the id stream is
// unchanged.
static void segment_greedy(const uint8_t* data, uint32_t n,
                           const Automaton& ac, vector<uint32_t>& ids) {
  ids.clear();
  ids.reserve(n / 2 + 8);
  uint32_t i = 0;
  while (i < n) {
    int32_t node = ac.root_next[data[i]];
    int32_t best_pat = -1;
    uint32_t best_len = 0;
    if (node >= 0) {
      if (ac.term_pat[node] >= 0) {
        best_pat = ac.term_pat[node];
        best_len = ac.term_len[node];
      }
      for (uint32_t k = i + 1; k < n; ++k) {
        int32_t t = ac.go(node, data[k]);
        if (t < 0) break;
        node = t;
        if (ac.term_pat[node] >= 0) {
          best_pat = ac.term_pat[node];
          best_len = ac.term_len[node];
        }
      }
    }
    if (best_pat >= 0) {
      ids.push_back(256 + (uint32_t)best_pat);
      i += best_len;
    } else {
      ids.push_back(data[i]);
      ++i;
    }
  }
}

// Shortest-path segmentation over the ACTUAL code lengths.
//
// Edges are relaxed by target position rather than by source position.  For a
// fixed target j the old loop produced source i ascending, i.e. pattern
// length descending, with the literal edge (i == j-1, length 1) last; the
// automaton's terminal chain yields exactly that descending-length order, so
// the strict `<` updates resolve every tie the same way and the parse is
// identical.
static void segment_optimal(const uint8_t* data, uint32_t n,
                            const Automaton& ac,
                            const vector<string>& patterns,
                            const unordered_map<uint32_t, int>& lengths,
                            vector<uint32_t>& ids) {
  int litcost[256];
  for (int b = 0; b < 256; ++b) {
    auto it = lengths.find(b);
    litcost[b] = it == lengths.end() ? NOCODE_COST : it->second;
  }
  vector<int> patcost(patterns.size(), NOCODE_COST);
  for (uint32_t i = 0; i < patterns.size(); ++i) {
    auto it = lengths.find(256 + i);
    if (it != lengths.end()) patcost[i] = it->second;
  }
  const int64_t INF = (int64_t)1 << 60;
  vector<int64_t> cost(n + 1, INF);
  vector<uint32_t> back(n + 1, 0);
  vector<uint16_t> blen(n + 1, 0);
  cost[0] = 0;
  int32_t node = 0;
  for (uint32_t j = 1; j <= n; ++j) {
    uint8_t c = data[j - 1];
    node = ac.step(node, c);
    int32_t t = ac.term_pat[node] >= 0 ? node : ac.out_link[node];
    // A length-1 dictionary entry cannot be produced by the tiers (n-grams
    // start at 2, word tokens at 3, merges only grow), but if one ever were,
    // it must be relaxed AFTER the literal to match the old source-ordered
    // loop.  The chain is descending, so such an entry is always last.
    while (t && ac.term_len[t] > 1) {
      uint32_t i = j - ac.term_len[t];
      int64_t nc = cost[i] + patcost[ac.term_pat[t]];
      if (nc < cost[j]) {
        cost[j] = nc;
        back[j] = 256 + (uint32_t)ac.term_pat[t];
        blen[j] = ac.term_len[t];
      }
      t = ac.out_link[t];
    }
    int64_t nc = cost[j - 1] + litcost[c];
    if (nc < cost[j]) {
      cost[j] = nc;
      back[j] = c;
      blen[j] = 1;
    }
    while (t) {
      uint32_t i = j - ac.term_len[t];
      nc = cost[i] + patcost[ac.term_pat[t]];
      if (nc < cost[j]) {
        cost[j] = nc;
        back[j] = 256 + (uint32_t)ac.term_pat[t];
        blen[j] = ac.term_len[t];
      }
      t = ac.out_link[t];
    }
  }
  ids.clear();
  uint32_t pos = n;
  while (pos > 0) {
    ids.push_back(back[pos]);
    pos -= blen[pos];
  }
  reverse(ids.begin(), ids.end());
}

struct GrowCand {
  int64_t gain;
  string merged;
  uint32_t a, b;
};

static void grow_blocks(vector<uint32_t>& ids, vector<string>& patterns,
                        int rounds, int min_freq, const Params& P) {
  for (int r = 0; r < rounds; ++r) {
    if (patterns.size() >= P.max_dict) break;
    uint32_t total = (uint32_t)ids.size();
    if (total < 2) break;
    U64Counter pairs;
    // Distinct adjacent pairs are typically a small fraction of the stream,
    // so start from the smaller of the stream length and a modest cap and let
    // the table double only if the data really is that varied.
    pairs.init((size_t)total + 8 < (1u << 16) ? (size_t)total + 8
                                              : (size_t)(1u << 16));
    for (uint32_t i = 0; i + 1 < total; ++i)
      pairs.add(((uint64_t)ids[i] << 32) | ids[i + 1]);
    vector<uint32_t> sym_counts(256 + patterns.size(), 0);
    for (uint32_t sid : ids) ++sym_counts[sid];
    int lit_bits[256];
    for (int b = 0; b < 256; ++b)
      lit_bits[b] = est_code_len(sym_counts[b], total);
    // Scoring a merge needs only the merged length and the summed literal
    // cost of its bytes, and both are additive over the two children.  The
    // concatenated string is therefore built ONLY for candidates that survive
    // the Bit Cost Decision Engine — previously every distinct pair in the
    // stream allocated one, which on a large file is millions of throwaway
    // heap strings per round.
    size_t nsym = 256 + patterns.size();
    vector<int64_t> spelled(nsym);
    vector<uint32_t> symlen(nsym);
    for (size_t sid = 0; sid < 256; ++sid) {
      spelled[sid] = lit_bits[sid];
      symlen[sid] = 1;
    }
    for (size_t k = 0; k < patterns.size(); ++k) {
      const string& p = patterns[k];
      int64_t sp = accumulate(
          p.begin(), p.end(), int64_t{0},
          [&](int64_t total, unsigned char ch) {
            return total + lit_bits[ch];
          });
      spelled[256 + k] = sp;
      symlen[256 + k] = (uint32_t)p.size();
    }
    auto expand = [&](uint32_t sid) -> string {
      if (sid < 256) return string(1, (char)sid);
      return patterns[sid - 256];
    };
    vector<GrowCand> accepted;
    for (size_t slot = 0; slot < pairs.key.size(); ++slot) {
      if (pairs.key[slot] == 0) continue;
      uint32_t f = pairs.cnt[slot];
      if (f < (uint32_t)min_freq) continue;
      uint64_t pk = pairs.key[slot] - 1;
      uint32_t a = (uint32_t)(pk >> 32), b = (uint32_t)pk;
      uint32_t mlen = symlen[a] + symlen[b];
      if (mlen > P.max_block) continue;
      int64_t gain = (int64_t)f * ((spelled[a] + spelled[b])
                                   - est_code_len(f, total))
                     - 8 * ((int64_t)mlen + 3);
      // [v4] dictionary-refund accounting
      for (uint32_t child : {a, b})
        if (child >= 256 && sym_counts[child] == f && a != b)
          gain += 8 * ((int64_t)patterns[child - 256].size() + 3);
      if (gain > 0)
        accepted.push_back({gain, expand(a) + expand(b), a, b});
    }
    if (accepted.empty()) break;
    sort(accepted.begin(), accepted.end(),
         [](const GrowCand& x, const GrowCand& y) {
           if (x.gain != y.gain) return x.gain > y.gain;
           if (x.merged != y.merged) return x.merged < y.merged;
           if (x.a != y.a) return x.a < y.a;
           return x.b < y.b;
         });
    uint32_t room = P.max_dict - (uint32_t)patterns.size();
    unordered_map<uint64_t, uint32_t> chosen;
    unordered_map<string, uint32_t> pat_index;
    pat_index.reserve(patterns.size() * 2 + 8);
    for (uint32_t i = 0; i < patterns.size(); ++i) pat_index[patterns[i]] = i;
    size_t lim = accepted.size() < (size_t)P.merges_per_round
                     ? accepted.size()
                     : (size_t)P.merges_per_round;
    for (size_t k = 0; k < lim; ++k) {
      GrowCand& gc = accepted[k];
      uint64_t pk = ((uint64_t)gc.a << 32) | gc.b;
      if (chosen.count(pk)) continue;
      auto it = pat_index.find(gc.merged);
      if (it != pat_index.end()) {
        chosen[pk] = 256 + it->second;
      } else if (room > 0) {
        patterns.push_back(gc.merged);
        pat_index[gc.merged] = (uint32_t)patterns.size() - 1;
        chosen[pk] = 256 + (uint32_t)patterns.size() - 1;
        --room;
      }
    }
    if (chosen.empty()) break;
    // At most MERGES_PER_ROUND pairs are ever chosen, so almost every symbol
    // in the stream cannot begin one.  A left-symbol membership bitmap turns
    // the common case into a single array read instead of a hash probe.
    vector<bool> starts_merge(256 + patterns.size(), false);
    for (auto& kv : chosen)
      starts_merge[(uint32_t)(kv.first >> 32)] = true;
    vector<uint32_t> out;
    out.reserve(ids.size());
    uint32_t i = 0, nn = (uint32_t)ids.size();
    while (i < nn) {
      if (i + 1 < nn && starts_merge[ids[i]]) {
        auto it = chosen.find(((uint64_t)ids[i] << 32) | ids[i + 1]);
        if (it != chosen.end()) {
          out.push_back(it->second);
          i += 2;
          continue;
        }
      }
      out.push_back(ids[i]);
      ++i;
    }
    ids = move(out);
  }
}

static void final_audit(vector<uint32_t>& ids, vector<string>& patterns,
                        const int* lit_bits) {
  uint32_t total = (uint32_t)ids.size();
  vector<uint32_t> counts(256 + patterns.size(), 0);
  for (uint32_t sid : ids) ++counts[sid];
  vector<bool> drop(256 + patterns.size(), false);
  bool any_drop = false;
  for (uint32_t idx = 0; idx < patterns.size(); ++idx) {
    uint32_t sid = 256 + idx;
    uint32_t f = counts[sid];
    if (f == 0) { drop[sid] = true; any_drop = true; continue; }
    int64_t gain = bit_cost_gain(patterns[idx], f, lit_bits,
                                 est_code_len(f, total));
    if (gain <= 0) { drop[sid] = true; any_drop = true; }
  }
  if (any_drop) {
    vector<uint32_t> out;
    out.reserve(ids.size());
    for (uint32_t sid : ids) {
      if (sid >= 256 && drop[sid]) {
        if (counts[sid])
          transform(patterns[sid - 256].begin(),
                    patterns[sid - 256].end(), back_inserter(out),
                    [](unsigned char c) { return uint32_t{c}; });
        continue;
      }
      out.push_back(sid);
    }
    ids = move(out);
    counts.assign(256 + patterns.size(), 0);
    for (uint32_t sid : ids) ++counts[sid];
  }
  vector<uint32_t> remap(256 + patterns.size(), 0);
  vector<string> kept;
  for (uint32_t i = 0; i < patterns.size(); ++i) {
    if (counts[256 + i]) {
      remap[256 + i] = 256 + (uint32_t)kept.size();
      kept.push_back(move(patterns[i]));
    }
  }
  patterns = move(kept);
  transform(ids.begin(), ids.end(), ids.begin(),
            [&](uint32_t sid) {
              return sid >= 256 ? remap[sid] : sid;
            });
}

static void build_lengths(const vector<uint32_t>& ids,
                          unordered_map<uint32_t, int>& lengths) {
  unordered_map<uint32_t, uint32_t> counts;
  counts.reserve(1024);
  for (uint32_t sid : ids) ++counts[sid];
  vector<pair<uint32_t, uint32_t>> items;  // (freq, id)
  items.resize(counts.size());
  transform(counts.begin(), counts.end(), items.begin(),
            [](const auto& kv) {
              return make_pair(kv.second, kv.first);
            });
  package_merge(move(items), MAX_CODE_LEN, lengths);
}

// Tunables that the caller may vary per compression. These mirror EXACTLY the
// four afc2.py module globals that presets.py assigns to, so the native core
// can honour a preset instead of ignoring it:
//
//     dp          <- afc2.OPTS["dp"]
//     dp_rounds   <- afc2.DP_ROUNDS
//     merge_rounds<- afc2.MERGE_ROUNDS_V4
//     min_freq    <- afc2.MIN_CANDIDATE_FREQ
//
// Defaults reproduce the historical compiled-in behaviour byte-for-byte, so
// afc_compress() (the original ABI) is unchanged for existing callers.
static string compress_core(const uint8_t* data, uint32_t n, int fmt,
                            int min_freq, int rounds, const int* lit_bits,
                            const Params& P) {
  vector<string> patterns;
  select_candidates(data, n, min_freq, lit_bits, P, patterns);
  Automaton ac;
  ac.build(patterns);
  vector<uint32_t> ids;
  segment_greedy(data, n, ac, ids);
  grow_blocks(ids, patterns, rounds, min_freq, P);
  final_audit(ids, patterns, lit_bits);

  unordered_map<uint32_t, int> lengths;
  // Mirrors afc2._compress_core: when OPTS["dp"] is false the DP loop AND the
  // second final_audit are both skipped. Getting that wrong would change the
  // output for the Fast preset, so the branch covers both statements.
  if (P.dp) {
    build_lengths(ids, lengths);
    ac.build(patterns);  // patterns are stable across the DP iterations
    vector<uint32_t> next_ids;
    for (int r = 0; r < P.dp_rounds; ++r) {
      segment_optimal(data, n, ac, patterns, lengths, next_ids);
      // [v9] Once the parse stops moving the remaining rounds are provably
      // no-ops: identical ids give identical code lengths, which give the
      // same parse again.  Stopping there is a pure work saving.
      bool settled = (next_ids == ids);
      ids.swap(next_ids);
      build_lengths(ids, lengths);
      if (settled) break;
    }
    final_audit(ids, patterns, lit_bits);
  }

  build_lengths(ids, lengths);
  uint32_t max_id = 255 + (uint32_t)patterns.size();
  Codes codes;
  codes.build(lengths, max_id);
  BitWriter bw;
  for (uint32_t sid : ids) bw.put(codes.code[sid], codes.len[sid]);
  bw.finish();
  return finish_container_c(2, data, n, patterns, lengths, codes, bw.buf,
                            fmt);
}

extern "C" {

// fmt: 0=auto, 1=afc1, 2=afc2;  adaptive: 0=baseline, 1=full pipeline
static int afc_compress_impl(const uint8_t* data, uint32_t n, int adaptive,
                             int fmt, const Params& P, void** out,
                             uint32_t* outn) {
  string blob;
  if (n == 0) {
    blob = emit_raw_c(data, 0, fmt == 2);
  } else if (!adaptive) {
    // baseline: single-tier byte-frequency canonical Huffman
    vector<pair<uint32_t, uint32_t>> items;
    {
      uint32_t freqs[256] = {0};
      for (uint32_t i = 0; i < n; ++i) ++freqs[data[i]];
      for (uint32_t b = 0; b < 256; ++b)
        if (freqs[b]) items.push_back({freqs[b], b});
    }
    unordered_map<uint32_t, int> lengths;
    package_merge(move(items), MAX_CODE_LEN, lengths);
    Codes codes;
    codes.build(lengths, 255);
    BitWriter bw;
    for (uint32_t i = 0; i < n; ++i)
      bw.put(codes.code[data[i]], codes.len[data[i]]);
    bw.finish();
    vector<string> nopat;
    blob = finish_container_c(1, data, n, nopat, lengths, codes, bw.buf,
                              fmt);
  } else {
    int lit_bits[256];
    {
      uint32_t freqs[256] = {0};
      for (uint32_t i = 0; i < n; ++i) ++freqs[data[i]];
      for (int b = 0; b < 256; ++b) lit_bits[b] = est_code_len(freqs[b], n);
    }
    if (P.tune && n < TUNE_SMALL) {
      // per-file tuning by trial: both admission floors run (on parallel
      // threads where available); the smaller container wins, ties keep
      // the v3 floor
      string alt;
#ifndef AFC_NO_THREADS
      thread t2([&] {
        alt = compress_core(data, n, fmt, P.min_freq - 1,
                            P.merge_rounds, lit_bits, P);
      });
      blob = compress_core(data, n, fmt, P.min_freq,
                           P.merge_rounds, lit_bits, P);
      t2.join();
#else
      blob = compress_core(data, n, fmt, P.min_freq,
                           P.merge_rounds, lit_bits, P);
      alt = compress_core(data, n, fmt, P.min_freq - 1,
                          P.merge_rounds, lit_bits, P);
#endif
      if (alt.size() < blob.size()) blob = move(alt);
    } else {
      blob = compress_core(data, n, fmt, P.min_freq,
                           P.merge_rounds, lit_bits, P);
    }
  }
  *out = malloc(blob.size() ? blob.size() : 1);
  if (!*out) return -100;
  memcpy(*out, blob.data(), blob.size());
  *outn = (uint32_t)blob.size();
  return 0;
}

// Original ABI, unchanged in signature and behaviour: engine defaults.
// Existing callers and any prebuilt binary check keep working.
AFC_API int afc_compress(const uint8_t* data, uint32_t n, int adaptive, int fmt,
                 void** out, uint32_t* outn) {
  Params P;
  return afc_compress_impl(data, n, adaptive, fmt, P, out, outn);
}

// Extended ABI: the same pipeline with the four preset-controlled tunables
// supplied by the caller. This is what makes Fast and Maximum native-capable.
// Passing the defaults reproduces afc_compress() byte-for-byte.
AFC_API int afc_compress_ex(const uint8_t* data, uint32_t n, int adaptive, int fmt,
                    int dp, int dp_rounds, int merge_rounds, int min_freq,
                    int tune, void** out, uint32_t* outn) {
  Params P;
  P.dp = dp;
  P.dp_rounds = dp_rounds;
  P.merge_rounds = merge_rounds;
  P.min_freq = min_freq;
  P.tune = tune;
  P.clamp();
  return afc_compress_impl(data, n, adaptive, fmt, P, out, outn);
}

// [v9] Profile-aware entry point.
//
// The caller passes a flat int32 array so the ABI can gain fields without
// another export: `nparams` says how many were supplied and anything beyond
// it keeps the engine default. Order:
//
//   0 dp   1 dp_rounds   2 merge_rounds   3 min_freq   4 tune
//   5 scan_window   6 ngram_max   7 max_initial_dict   8 max_dict
//   9 max_block    10 merges_per_round
//
// Fields 5..10 were compile-time constants before v9. Varying them is what
// lets one file be compressed under several search profiles so the smallest
// container can be kept; none of them changes the decoder, because the
// dictionary and the code lengths are written into the container explicitly.
AFC_API int afc_compress_v9(const uint8_t* data, uint32_t n, int adaptive,
                            int fmt, const int32_t* params, int32_t nparams,
                            void** out, uint32_t* outn) {
  Params P;
  if (params == nullptr) nparams = 0;
  if (nparams > 0) P.dp = params[0];
  if (nparams > 1) P.dp_rounds = params[1];
  if (nparams > 2) P.merge_rounds = params[2];
  if (nparams > 3) P.min_freq = params[3];
  if (nparams > 4) P.tune = params[4];
  if (nparams > 5 && params[5] > 0) P.scan_window = (uint32_t)params[5];
  if (nparams > 6) P.ngram_max = params[6];
  if (nparams > 7 && params[7] > 0) P.max_initial_dict = (uint32_t)params[7];
  if (nparams > 8 && params[8] > 0) P.max_dict = (uint32_t)params[8];
  if (nparams > 9 && params[9] > 0) P.max_block = (uint32_t)params[9];
  if (nparams > 10) P.merges_per_round = params[10];
  P.clamp();
  return afc_compress_impl(data, n, adaptive, fmt, P, out, outn);
}

}  // extern "C"

// ============================================================================
// decoder — AFC1 (legacy) + AFC2, table-driven multi-bit canonical decode
// ============================================================================

struct DecTable {
  int maxlen = 0;
  vector<int32_t> sym;   // LUT: next maxlen bits -> symbol id (-1 invalid)
  vector<uint8_t> adv;   // LUT: bits to consume
  vector<uint64_t> firstcode;  // legacy walk (maxlen > 16)
  vector<uint32_t> firstidx, cnt, order;
  bool lut = false;

  bool build(const vector<pair<uint32_t, uint8_t>>& syms_in) {  // (id, len)
    vector<pair<uint32_t, uint8_t>> syms(syms_in);
    sort(syms.begin(), syms.end(),
         [](const pair<uint32_t, uint8_t>& a,
            const pair<uint32_t, uint8_t>& b) {
           return a.second != b.second ? a.second < b.second
                                       : a.first < b.first;
         });
    maxlen = 0;
    for (const auto& s : syms)
      if (s.second > maxlen) maxlen = s.second;
    if (maxlen == 0 || maxlen > 63 || syms.empty()) return false;
    if (maxlen <= 16) {
      lut = true;
      size_t size = (size_t)1 << maxlen;
      sym.assign(size, -1);
      adv.assign(size, 0);
      uint64_t code = 0;
      int prev = syms[0].second;
      for (const auto& s : syms) {
        int L = s.second;
        code <<= (L - prev);
        prev = L;
        uint64_t base = code << (maxlen - L);
        if (base >= size) return false;  // over-subscribed code space
        size_t fill = (size_t)1 << (maxlen - L);
        for (size_t k = 0; k < fill && base + k < size; ++k) {
          sym[base + k] = (int32_t)s.first;
          adv[base + k] = (uint8_t)L;
        }
        ++code;
      }
      return true;
    }
    lut = false;
    firstcode.assign(maxlen + 2, 0);
    firstidx.assign(maxlen + 2, 0);
    cnt.assign(maxlen + 2, 0);
    order.assign(syms.size(), 0);
    uint64_t code = 0;
    int prev = syms[0].second;
    for (size_t k = 0; k < syms.size(); ++k) {
      int L = syms[k].second;
      code <<= (L - prev);
      prev = L;
      if (cnt[L] == 0) {
        firstcode[L] = code;
        firstidx[L] = (uint32_t)k;
      }
      cnt[L]++;
      order[k] = syms[k].first;
      code++;
    }
    return true;
  }
};

struct BitReader {
  const uint8_t* b;
  uint32_t n, pos;
  uint64_t acc = 0;
  int nb = 0;
  uint64_t bits_used = 0;
  BitReader(const uint8_t* b_, uint32_t n_, uint32_t pos_)
      : b(b_), n(n_), pos(pos_) {}
};

// decode until `want` output bytes are produced.  Returns 0 on success.
static int decode_stream(BitReader& br, const DecTable& t,
                         const vector<string>& patterns, uint64_t want,
                         string& out) {
  if (t.lut) {
    const uint64_t mask = ((uint64_t)1 << t.maxlen) - 1;
    while (out.size() < want) {
      while (br.nb < t.maxlen && br.pos < br.n) {
        br.acc = (br.acc << 8) | br.b[br.pos++];
        br.nb += 8;
      }
      uint64_t idx;
      if (br.nb >= t.maxlen)
        idx = (br.acc >> (br.nb - t.maxlen)) & mask;
      else
        idx = (br.acc << (t.maxlen - br.nb)) & mask;
      int32_t sid = t.sym[idx];
      int L = t.adv[idx];
      if (L == 0 || sid < 0 || L > br.nb) return -3;
      br.nb -= L;
      br.acc &= ((uint64_t)1 << br.nb) - 1;
      br.bits_used += L;
      if (sid < 256) {
        out.push_back((char)sid);
      } else {
        if ((size_t)(sid - 256) >= patterns.size()) return -3;
        const string& p = patterns[sid - 256];
        size_t room = (size_t)(want - out.size());
        out.append(p, 0, p.size() <= room ? p.size() : room);
      }
    }
    return 0;
  }
  // legacy bit-by-bit walk (AFC1 files with code lengths > 16)
  uint64_t code = 0;
  int L = 0;
  for (uint32_t i = br.pos; i < br.n && out.size() < want; ++i) {
    uint8_t byte = br.b[i];
    for (int sh = 7; sh >= 0; --sh) {
      code = (code << 1) | ((byte >> sh) & 1);
      ++L;
      if (L <= t.maxlen && t.cnt[L] && code >= t.firstcode[L] &&
          code - t.firstcode[L] < t.cnt[L]) {
        uint32_t sym =
            t.order[t.firstidx[L] + (uint32_t)(code - t.firstcode[L])];
        if (sym < 256) {
          out.push_back((char)sym);
        } else {
          if (sym - 256 >= patterns.size()) return -3;
          const string& p = patterns[sym - 256];
          size_t room = (size_t)(want - out.size());
          out.append(p, 0, p.size() <= room ? p.size() : room);
        }
        code = 0;
        L = 0;
        if (out.size() >= want) break;
      }
    }
  }
  return out.size() == want ? 0 : -4;
}

extern "C" {

AFC_API int afc_decompress(const uint8_t* blob, uint32_t n, void** out,
                   uint32_t* outn) {
  if (n < 6) return -1;
  bool is1 = memcmp(blob, "AFC1", 4) == 0;
  bool is2 = memcmp(blob, "AFC2", 4) == 0;
  if (!is1 && !is2) return -1;
  uint8_t mode = blob[4];
  uint32_t pos = 5;
  uint64_t orig;
  if (!read_varint(blob, n, pos, orig)) return -2;
  if (mode == 0) {
    if (pos + orig > n) return -2;
    uint8_t* o = (uint8_t*)malloc(orig ? (size_t)orig : 1);
    if (!o) return -100;
    memcpy(o, blob + pos, (size_t)orig);
    *out = o;
    *outn = (uint32_t)orig;
    return 0;
  }
  if (orig == 0) {
    *out = malloc(1);
    *outn = 0;
    return 0;
  }
  vector<string> patterns;
  vector<pair<uint32_t, uint8_t>> syms;  // (id, len)
  if (is1) {
    uint64_t dc;
    if (!read_varint(blob, n, pos, dc)) return -2;
    patterns.reserve((size_t)dc);
    for (uint64_t k = 0; k < dc; ++k) {
      uint64_t L;
      if (!read_varint(blob, n, pos, L) || pos + L > n) return -2;
      patterns.emplace_back((const char*)blob + pos, (size_t)L);
      pos += (uint32_t)L;
    }
    uint64_t used;
    if (!read_varint(blob, n, pos, used)) return -2;
    syms.reserve((size_t)used);
    for (uint64_t k = 0; k < used; ++k) {
      uint64_t id;
      if (!read_varint(blob, n, pos, id) || pos >= n) return -2;
      syms.push_back({(uint32_t)id, blob[pos++]});
    }
  } else {
    uint64_t used;
    if (!read_varint(blob, n, pos, used)) return -2;
    vector<uint32_t> ids((size_t)used);
    int64_t prev = -1;
    for (uint64_t k = 0; k < used; ++k) {
      uint64_t d;
      if (!read_varint(blob, n, pos, d)) return -2;
      uint32_t sid = prev < 0 ? (uint32_t)d : (uint32_t)(prev + 1 + d);
      ids[(size_t)k] = sid;
      prev = sid;
    }
    uint32_t nbytes = (uint32_t)((used + 1) / 2);
    if (pos + nbytes > n) return -2;
    syms.reserve((size_t)used);
    for (uint64_t k = 0; k < used; ++k) {
      uint8_t b = blob[pos + (k >> 1)];
      int v = (k & 1) ? (b & 0x0F) : (b >> 4);
      syms.push_back({ids[(size_t)k], (uint8_t)(v + 1)});
    }
    pos += nbytes;
    uint64_t dc;
    if (!read_varint(blob, n, pos, dc)) return -2;
    if (dc) {
      if (pos >= n) return -2;
      uint8_t flag = blob[pos++];
      vector<uint64_t> plens((size_t)dc);
      uint64_t total = 0;
      for (uint64_t k = 0; k < dc; ++k) {
        if (!read_varint(blob, n, pos, plens[(size_t)k])) return -2;
        total += plens[(size_t)k];
      }
      if (flag == 0) {
        for (uint64_t k = 0; k < dc; ++k) {
          uint64_t L = plens[(size_t)k];
          if (pos + L > n) return -2;
          patterns.emplace_back((const char*)blob + pos, (size_t)L);
          pos += (uint32_t)L;
        }
      } else {
        // dictionary bytes coded with the literal codes of the full table
        DecTable t;
        if (!t.build(syms)) return -2;
        uint32_t dict_pos = pos;
        BitReader br(blob, n, dict_pos);
        string raw;
        raw.reserve((size_t)total);
        vector<string> nopat;
        int rc = decode_stream(br, t, nopat, total, raw);
        if (rc) return rc;
        pos = dict_pos + (uint32_t)((br.bits_used + 7) / 8);
        uint64_t off = 0;
        for (uint64_t k = 0; k < dc; ++k) {
          patterns.emplace_back(
              raw.substr((size_t)off, (size_t)plens[(size_t)k]));
          off += plens[(size_t)k];
        }
      }
    }
  }
  DecTable t;
  if (!t.build(syms)) return -2;
  BitReader br(blob, n, pos);
  string outbuf;
  outbuf.reserve((size_t)orig);
  int rc = decode_stream(br, t, patterns, orig, outbuf);
  if (rc) return rc;
  uint8_t* o = (uint8_t*)malloc(outbuf.size() ? outbuf.size() : 1);
  if (!o) return -100;
  memcpy(o, outbuf.data(), outbuf.size());
  *out = o;
  *outn = (uint32_t)outbuf.size();
  return 0;
}

}  // extern "C"

// ============================================================================
// legacy v3 kernels — kept exported, unchanged behavior
// ============================================================================

extern "C" {

// ---- Tier-2 kernel: n-gram frequencies, lengths 2..5, min freq 4 ----
// out: [u32 count] then entries: [u8 len][len bytes][u32 freq]
AFC_API int count_ngrams(const uint8_t* data, uint32_t n, uint32_t window,
                 void** out, uint32_t* outn) {
  if (n > window) n = window;
  string buf(4, '\0');
  uint32_t total = 0;
  for (int L = 2; L <= 5; ++L) {
    if ((uint32_t)L > n) break;
    unordered_map<string, uint32_t> m;
    m.reserve(n / 2 + 8);
    string key(L, '\0');
    for (uint32_t i = 0; i + L <= n; ++i) {
      key.assign((const char*)data + i, L);
      ++m[key];
    }
    for (auto& kv : m) {
      if (kv.second >= 4) {
        buf.push_back((char)L);
        buf.append(kv.first);
        uint32_t f = kv.second;
        buf.append((const char*)&f, 4);
        ++total;
      }
    }
  }
  memcpy(&buf[0], &total, 4);
  *out = malloc(buf.size());
  memcpy(*out, buf.data(), buf.size());
  *outn = (uint32_t)buf.size();
  return 0;
}

// ---- segmentation kernel: greedy longest match over the pattern set ----
// pats blob: [u32 count] then per entry: [u8 len][bytes]
// out: u32 ids (literal byte value, or 256+pattern_index)
AFC_API int segment_ids(const uint8_t* data, uint32_t n, const uint8_t* pats,
                uint32_t patn, void** out, uint32_t* outn) {
  if (!out || !outn) return -1;
  *out = nullptr;
  *outn = 0;
  if ((n && !data) || !pats || patn < 4) return -1;

  uint32_t count = (uint32_t)pats[0]
                 | ((uint32_t)pats[1] << 8)
                 | ((uint32_t)pats[2] << 16)
                 | ((uint32_t)pats[3] << 24);

  // Validate the complete length-prefixed pattern buffer before reading or
  // allocating from values supplied by its header.
  const uint8_t* scan = pats + 4;
  uint32_t remaining = patn - 4;
  for (uint32_t i = 0; i < count; ++i) {
    if (remaining < 1) return -1;
    uint32_t L = *scan++;
    --remaining;
    if (L == 0 || L > remaining) return -1;
    scan += L;
    remaining -= L;
  }
  if (remaining != 0) return -1;

  const uint8_t* p = pats + 4;
  vector<string> plist;
  plist.reserve(count);
  vector<vector<pair<int, uint32_t>>> byfirst(256);
  for (uint32_t i = 0; i < count; ++i) {
    int L = *p++;
    plist.emplace_back((const char*)p, L);
    byfirst[(uint8_t)plist.back()[0]].push_back({L, i});
    p += L;
  }
  for (auto& v : byfirst)
    sort(v.begin(), v.end(),
         [](const pair<int, uint32_t>& a, const pair<int, uint32_t>& b) {
           return a.first > b.first;
         });
  unordered_map<string, uint32_t> pset;
  pset.reserve(count * 2 + 8);
  for (uint32_t i = 0; i < count; ++i) pset[plist[i]] = i;

  vector<uint32_t> ids;
  ids.reserve(n);
  string key;
  uint32_t i = 0;
  while (i < n) {
    uint8_t b = data[i];
    long midx = -1;
    uint32_t mlen = 0;
    for (const auto& pr : byfirst[b]) {
      uint32_t L = (uint32_t)pr.first;
      if (i + L <= n) {
        key.assign((const char*)data + i, L);
        auto it = pset.find(key);
        if (it != pset.end()) {
          midx = (long)it->second;
          mlen = L;
          break;
        }
      }
    }
    if (midx >= 0) {
      ids.push_back(256 + (uint32_t)midx);
      i += mlen;
    } else {
      ids.push_back(b);
      ++i;
    }
  }
  *outn = (uint32_t)ids.size();
  size_t bytes = ids.size() * sizeof(uint32_t);
  *out = malloc(bytes ? bytes : 1);
  if (!*out) {
    *outn = 0;
    return -2;
  }
  if (bytes) {
    copy(ids.begin(), ids.end(), static_cast<uint32_t*>(*out));
  }
  return 0;
}

// ---- bitstream pack kernel ----
// ids u32[n]; codes u32[max_id+1]; lens u8[max_id+1]
AFC_API int pack_bits(const uint32_t* ids, uint32_t n, const uint32_t* codes,
              const uint8_t* lens, void** out, uint32_t* outn) {
  vector<uint8_t> b;
  b.reserve(n);
  uint64_t acc = 0;
  int nb = 0;
  for (uint32_t i = 0; i < n; ++i) {
    uint32_t id = ids[i];
    int L = lens[id];
    acc = (acc << L) | codes[id];
    nb += L;
    while (nb >= 8) {
      nb -= 8;
      b.push_back((uint8_t)((acc >> nb) & 0xFF));
      acc &= ((1ULL << nb) - 1ULL);
    }
  }
  if (nb) b.push_back((uint8_t)((acc << (8 - nb)) & 0xFF));
  *out = malloc(b.size() ? b.size() : 1);
  memcpy(*out, b.data(), b.size());
  *outn = (uint32_t)b.size();
  return 0;
}

}  // extern "C"
