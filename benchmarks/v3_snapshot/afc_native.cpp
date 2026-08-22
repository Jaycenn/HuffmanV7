// afc_native.cpp — C++ performance kernels for the AFC engine.
// Multi-Level Frequency Analysis & Hybrid Huffman only: these kernels
// accelerate the EXISTING pipeline stages (frequency scan, longest-match
// segmentation, bitstream pack, canonical-Huffman decode). No LZ, no
// back-references, no new algorithm classes.
//
// Build (Linux/macOS):  g++ -O3 -shared -fPIC afc_native.cpp -o afc_native.so
// Build (Windows MinGW): g++ -O3 -shared -static afc_native.cpp -o afc_native.dll
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>
using namespace std;

extern "C" {

void afc_free(void* p) { free(p); }

// ---- Tier-2 kernel: n-gram frequencies, lengths 2..5, min freq 4 ----
// out: [u32 count] then entries: [u8 len][len bytes][u32 freq]
int count_ngrams(const uint8_t* data, uint32_t n, uint32_t window,
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
      memcpy(&key[0], data + i, L);
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
int segment_ids(const uint8_t* data, uint32_t n,
                const uint8_t* pats, uint32_t /*patn*/,
                void** out, uint32_t* outn) {
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
        if (it != pset.end()) { midx = (long)it->second; mlen = L; break; }
      }
    }
    if (midx >= 0) { ids.push_back(256 + (uint32_t)midx); i += mlen; }
    else           { ids.push_back(b); ++i; }
  }
  *outn = (uint32_t)ids.size();
  *out = malloc(ids.size() * 4);
  memcpy(*out, ids.data(), ids.size() * 4);
  return 0;
}

// ---- bitstream pack kernel ----
// ids u32[n]; codes u32[max_id+1]; lens u8[max_id+1]
int pack_bits(const uint32_t* ids, uint32_t n, const uint32_t* codes,
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

// ---- full container decode: canonical-Huffman traversal in C++ ----
static uint64_t rv(const uint8_t* b, uint32_t& pos) {
  uint64_t v = 0;
  int sh = 0;
  for (;;) {
    uint8_t x = b[pos++];
    v |= (uint64_t)(x & 0x7F) << sh;
    if (!(x & 0x80)) return v;
    sh += 7;
  }
}

int afc_decompress(const uint8_t* blob, uint32_t n, void** out,
                   uint32_t* outn) {
  if (n < 6 || memcmp(blob, "AFC1", 4) != 0) return -1;
  uint8_t mode = blob[4];
  uint32_t pos = 5;
  uint64_t orig = rv(blob, pos);
  uint8_t* o = (uint8_t*)malloc(orig ? orig : 1);
  if (mode == 0) {                       // raw fallback container
    if (pos + orig > n) { free(o); return -2; }
    memcpy(o, blob + pos, orig);
    *out = o; *outn = (uint32_t)orig; return 0;
  }
  uint64_t dc = rv(blob, pos);
  vector<pair<const uint8_t*, uint32_t>> pats(dc);
  for (auto& pp : pats) {
    uint32_t L = (uint32_t)rv(blob, pos);
    pp = {blob + pos, L};
    pos += L;
  }
  uint64_t used = rv(blob, pos);
  vector<pair<uint32_t, uint8_t>> syms(used);
  int maxlen = 0;
  for (auto& s : syms) {
    uint32_t id = (uint32_t)rv(blob, pos);
    uint8_t L = blob[pos++];
    s = {id, L};
    if (L > maxlen) maxlen = L;
  }
  sort(syms.begin(), syms.end(),
       [](const pair<uint32_t, uint8_t>& a, const pair<uint32_t, uint8_t>& b) {
         return a.second != b.second ? a.second < b.second : a.first < b.first;
       });
  vector<uint64_t> firstcode(maxlen + 2, 0);
  vector<uint32_t> firstidx(maxlen + 2, 0), cnt(maxlen + 2, 0),
      order(syms.size());
  {
    uint64_t code = 0;
    int prev = syms.empty() ? 1 : syms[0].second;
    for (size_t k = 0; k < syms.size(); ++k) {
      int L = syms[k].second;
      code <<= (L - prev);
      prev = L;
      if (cnt[L] == 0) { firstcode[L] = code; firstidx[L] = (uint32_t)k; }
      cnt[L]++;
      order[k] = syms[k].first;
      code++;
    }
  }
  uint64_t code = 0, w = 0;
  int L = 0;
  for (uint32_t i = pos; i < n && w < orig; ++i) {
    uint8_t byte = blob[i];
    for (int sh = 7; sh >= 0; --sh) {
      code = (code << 1) | ((byte >> sh) & 1);
      ++L;
      if (L <= maxlen && cnt[L] && code >= firstcode[L] &&
          code - firstcode[L] < cnt[L]) {
        uint32_t sym = order[firstidx[L] + (uint32_t)(code - firstcode[L])];
        if (sym < 256) o[w++] = (uint8_t)sym;
        else {
          if (sym - 256 >= pats.size()) { free(o); return -3; }
          auto& pp = pats[sym - 256];
          uint32_t take = pp.second;
          if (w + take > orig) take = (uint32_t)(orig - w);
          memcpy(o + w, pp.first, take);
          w += take;
        }
        code = 0;
        L = 0;
        if (w >= orig) break;
      }
    }
  }
  *out = o;
  *outn = (uint32_t)orig;
  return 0;
}

}  // extern "C"
