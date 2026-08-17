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
  return q ? 32 - __builtin_clz(q) : 0;
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
    syms.reserve(lengths.size());
    for (auto& kv : lengths) syms.push_back({kv.second, kv.first});
    sort(syms.begin(), syms.end());
    uint32_t c = 0;
    int prev = syms.empty() ? 1 : syms[0].first;
    for (auto& s : syms) {
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
  for (auto& p : patterns) {
    write_varint(out, p.size());
    out += p;
  }
  write_varint(out, lens_sorted.size());
  for (auto& kv : lens_sorted) {
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
  for (auto& kv : lens_sorted) {
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
    for (auto& p : patterns) {
      rawlen += p.size();
      for (unsigned char b : p)
        if (codes.len[b] == 0) { can = false; break; }
      if (!can) break;
    }
    string blob;
    if (can) {
      BitWriter bw;
      for (auto& p : patterns)
        for (unsigned char b : p) bw.put(codes.code[b], codes.len[b]);
      bw.finish();
      blob = move(bw.buf);
    }
    if (can && blob.size() < rawlen) {
      out.push_back((char)1);
      for (auto& p : patterns) write_varint(out, p.size());
      out += blob;
    } else {
      out.push_back((char)0);
      for (auto& p : patterns) write_varint(out, p.size());
      for (auto& p : patterns) out += p;
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
  lens_sorted.reserve(lengths.size());
  for (auto& kv : lengths) lens_sorted.push_back({kv.first, kv.second});
  sort(lens_sorted.begin(), lens_sorted.end());
  string blob;
  if (fmt == 1) {
    blob = emit_afc1_c(mode, n, patterns, lens_sorted, bits);
  } else if (fmt == 2) {
    blob = emit_afc2_c(mode, n, patterns, lens_sorted, codes, bits);
  } else {
    blob = emit_afc1_c(mode, n, patterns, lens_sorted, bits);
    bool le16 = true;
    for (auto& kv : lens_sorted)
      if (kv.second < 1 || kv.second > 16) { le16 = false; break; }
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
  for (unsigned char b : pat) spelled += lit_bits[b];
  return (int64_t)f * (spelled - sym_bits) - 8 * ((int64_t)pat.size() + 3);
}

// Tier-2 n-gram counting for one length (thread worker).  n-grams of
// length <= 8 pack into a uint64 key (big-endian byte order), which is much
// faster to hash than heap strings; the counts are identical.
static void count_len(const uint8_t* win, uint32_t wn, int L,
                      unordered_map<uint64_t, uint32_t>* m) {
  if ((uint32_t)L > wn) return;
  m->reserve(wn / 2 + 8);
  uint64_t key = 0;
  const uint64_t mask =
      L >= 8 ? ~0ULL : (((uint64_t)1 << (8 * L)) - 1);
  for (int k = 0; k < L - 1; ++k) key = (key << 8) | win[k];
  for (uint32_t i = 0; i + L <= wn; ++i) {
    key = ((key << 8) | win[i + L - 1]) & mask;
    ++(*m)[key];
  }
}

static void select_candidates(const uint8_t* data, uint32_t n, int min_freq,
                              const int* lit_bits, vector<string>& patterns) {
  uint32_t wn = n < SCAN_WINDOW ? n : SCAN_WINDOW;
  // Tier-2: one thread per n-gram length (deterministic: separate maps);
  // below ~16 KB thread spawn costs more than the scan itself, so go serial
  unordered_map<uint64_t, uint32_t> maps[NGRAM_MAX - NGRAM_MIN + 1];
#ifndef AFC_NO_THREADS
  if (wn >= 16384) {
    vector<thread> th;
    for (int L = NGRAM_MIN; L <= NGRAM_MAX; ++L)
      th.emplace_back(count_len, data, wn, L, &maps[L - NGRAM_MIN]);
    for (auto& t : th) t.join();
  } else
#endif
  {
    for (int L = NGRAM_MIN; L <= NGRAM_MAX; ++L)
      count_len(data, wn, L, &maps[L - NGRAM_MIN]);
  }
  unordered_map<string, uint32_t> cands;
  cands.reserve(maps[0].size() + maps[1].size() + 64);
  for (int L = NGRAM_MIN; L <= NGRAM_MAX; ++L) {
    for (auto& kv : maps[L - NGRAM_MIN]) {
      if (kv.second >= (uint32_t)min_freq) {
        char buf[8];
        uint64_t k = kv.first;
        for (int j = L - 1; j >= 0; --j) {
          buf[j] = (char)(k & 0xFF);
          k >>= 8;
        }
        cands[string(buf, (size_t)L)] = kv.second;
      }
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
    for (auto& kv : words)
      if (kv.second >= (uint32_t)min_freq &&
          cands.find(kv.first) == cands.end())
        cands[kv.first] = kv.second;
  }
  // Bit Cost Decision Engine gate + deterministic ranking
  vector<pair<int64_t, string>> scored;
  scored.reserve(cands.size());
  for (auto& kv : cands) {
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
  size_t take = scored.size() < MAX_INITIAL_DICT ? scored.size()
                                                 : MAX_INITIAL_DICT;
  patterns.reserve(take);
  for (size_t k = 0; k < take; ++k) patterns.push_back(move(scored[k].second));
}

struct PatIndex {
  // string_view keys point into the owning `patterns` vector — zero-copy
  // lookups from the input buffer during greedy and DP segmentation
  unordered_map<string_view, uint32_t> pset;
  vector<vector<int>> lens_desc;  // per first byte, lengths desc
  vector<vector<int>> lens_asc;   // per first byte, lengths asc
  void build(const vector<string>& patterns) {
    pset.clear();
    pset.reserve(patterns.size() * 2 + 8);
    for (uint32_t i = 0; i < patterns.size(); ++i)
      pset[string_view(patterns[i])] = i;
    vector<vector<bool>> seen(256, vector<bool>(MAX_BLOCK + 1, false));
    for (auto& p : patterns) seen[(uint8_t)p[0]][p.size()] = true;
    lens_asc.assign(256, {});
    lens_desc.assign(256, {});
    for (int b = 0; b < 256; ++b) {
      for (int L = 1; L <= (int)MAX_BLOCK; ++L)
        if (seen[b][L]) lens_asc[b].push_back(L);
      lens_desc[b] = lens_asc[b];
      reverse(lens_desc[b].begin(), lens_desc[b].end());
    }
  }
};

static void segment_greedy(const uint8_t* data, uint32_t n,
                           const PatIndex& px, vector<uint32_t>& ids) {
  ids.clear();
  ids.reserve(n / 2 + 8);
  uint32_t i = 0;
  while (i < n) {
    uint8_t b = data[i];
    bool matched = false;
    for (int L : px.lens_desc[b]) {
      if (i + (uint32_t)L <= n) {
        auto it = px.pset.find(
            string_view((const char*)data + i, (size_t)L));
        if (it != px.pset.end()) {
          ids.push_back(256 + it->second);
          i += L;
          matched = true;
          break;
        }
      }
    }
    if (!matched) {
      ids.push_back(b);
      ++i;
    }
  }
}

static void segment_optimal(const uint8_t* data, uint32_t n,
                            const PatIndex& px,
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
  for (uint32_t i = 0; i < n; ++i) {
    int64_t c = cost[i];
    if (c >= INF) continue;
    uint8_t b = data[i];
    int64_t nc = c + litcost[b];
    if (nc < cost[i + 1]) {
      cost[i + 1] = nc;
      back[i + 1] = b;
      blen[i + 1] = 1;
    }
    for (int L : px.lens_asc[b]) {
      uint32_t j = i + (uint32_t)L;
      if (j > n) break;
      auto it = px.pset.find(string_view((const char*)data + i, (size_t)L));
      if (it == px.pset.end()) continue;
      nc = c + patcost[it->second];
      if (nc < cost[j]) {
        cost[j] = nc;
        back[j] = 256 + it->second;
        blen[j] = (uint16_t)L;
      }
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
                        int rounds, int min_freq) {
  for (int r = 0; r < rounds; ++r) {
    if (patterns.size() >= MAX_DICT) break;
    uint32_t total = (uint32_t)ids.size();
    if (total < 2) break;
    unordered_map<uint64_t, uint32_t> pairs;
    pairs.reserve(total + 8);
    for (uint32_t i = 0; i + 1 < total; ++i)
      ++pairs[((uint64_t)ids[i] << 32) | ids[i + 1]];
    vector<uint32_t> sym_counts(256 + patterns.size(), 0);
    for (uint32_t sid : ids) ++sym_counts[sid];
    int lit_bits[256];
    for (int b = 0; b < 256; ++b)
      lit_bits[b] = est_code_len(sym_counts[b], total);
    auto expand = [&](uint32_t sid) -> string {
      if (sid < 256) return string(1, (char)sid);
      return patterns[sid - 256];
    };
    vector<GrowCand> accepted;
    for (auto& kv : pairs) {
      uint32_t f = kv.second;
      if (f < (uint32_t)min_freq) continue;
      uint32_t a = (uint32_t)(kv.first >> 32), b = (uint32_t)kv.first;
      string merged = expand(a) + expand(b);
      if (merged.size() > MAX_BLOCK) continue;
      int64_t gain = bit_cost_gain(merged, f, lit_bits,
                                   est_code_len(f, total));
      // [v4] dictionary-refund accounting
      for (uint32_t child : {a, b})
        if (child >= 256 && sym_counts[child] == f && a != b)
          gain += 8 * ((int64_t)patterns[child - 256].size() + 3);
      if (gain > 0) accepted.push_back({gain, move(merged), a, b});
    }
    if (accepted.empty()) break;
    sort(accepted.begin(), accepted.end(),
         [](const GrowCand& x, const GrowCand& y) {
           if (x.gain != y.gain) return x.gain > y.gain;
           if (x.merged != y.merged) return x.merged < y.merged;
           if (x.a != y.a) return x.a < y.a;
           return x.b < y.b;
         });
    uint32_t room = MAX_DICT - (uint32_t)patterns.size();
    unordered_map<uint64_t, uint32_t> chosen;
    unordered_map<string, uint32_t> pat_index;
    pat_index.reserve(patterns.size() * 2 + 8);
    for (uint32_t i = 0; i < patterns.size(); ++i) pat_index[patterns[i]] = i;
    size_t lim = accepted.size() < (size_t)MERGES_PER_ROUND
                     ? accepted.size()
                     : (size_t)MERGES_PER_ROUND;
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
    vector<uint32_t> out;
    out.reserve(ids.size());
    uint32_t i = 0, nn = (uint32_t)ids.size();
    while (i < nn) {
      if (i + 1 < nn) {
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
          for (unsigned char c : patterns[sid - 256]) out.push_back(c);
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
  for (auto& sid : ids)
    if (sid >= 256) sid = remap[sid];
}

static void build_lengths(const vector<uint32_t>& ids,
                          unordered_map<uint32_t, int>& lengths) {
  unordered_map<uint32_t, uint32_t> counts;
  counts.reserve(1024);
  for (uint32_t sid : ids) ++counts[sid];
  vector<pair<uint32_t, uint32_t>> items;  // (freq, id)
  items.reserve(counts.size());
  for (auto& kv : counts) items.push_back({kv.second, kv.first});
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
struct Params {
  int dp = 1;
  int dp_rounds = DP_ROUNDS;
  int merge_rounds = MERGE_ROUNDS_V4;
  int min_freq = MIN_CANDIDATE_FREQ;
  int tune = 1;
};

static string compress_core(const uint8_t* data, uint32_t n, int fmt,
                            int min_freq, int rounds, const int* lit_bits,
                            const Params& P) {
  vector<string> patterns;
  select_candidates(data, n, min_freq, lit_bits, patterns);
  PatIndex px;
  px.build(patterns);
  vector<uint32_t> ids;
  segment_greedy(data, n, px, ids);
  grow_blocks(ids, patterns, rounds, min_freq);
  final_audit(ids, patterns, lit_bits);

  unordered_map<uint32_t, int> lengths;
  // Mirrors afc2._compress_core: when OPTS["dp"] is false the DP loop AND the
  // second final_audit are both skipped. Getting that wrong would change the
  // output for the Fast preset, so the branch covers both statements.
  if (P.dp) {
    build_lengths(ids, lengths);
    px.build(patterns);  // patterns are stable across the DP iterations
    for (int r = 0; r < P.dp_rounds; ++r) {
      segment_optimal(data, n, px, patterns, lengths, ids);
      build_lengths(ids, lengths);
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
  P.dp_rounds = dp_rounds > 0 ? dp_rounds : 0;
  P.merge_rounds = merge_rounds >= 0 ? merge_rounds : 0;
  // min_freq must stay >= 2: the small-file trial also runs min_freq - 1, and
  // a floor of 0 would admit every n-gram and blow up the candidate set.
  P.min_freq = min_freq >= 2 ? min_freq : 2;
  P.tune = tune;
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
    for (auto& s : syms)
      if (s.second > maxlen) maxlen = s.second;
    if (maxlen == 0 || maxlen > 63 || syms.empty()) return false;
    if (maxlen <= 16) {
      lut = true;
      size_t size = (size_t)1 << maxlen;
      sym.assign(size, -1);
      adv.assign(size, 0);
      uint64_t code = 0;
      int prev = syms[0].second;
      for (auto& s : syms) {
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
                uint32_t /*patn*/, void** out, uint32_t* outn) {
  uint32_t count;
  memcpy(&count, pats, 4);
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
    for (auto& pr : byfirst[b]) {
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
  *out = malloc(ids.size() * 4);
  memcpy(*out, ids.data(), ids.size() * 4);
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
