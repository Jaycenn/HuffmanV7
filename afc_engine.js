/* afc_engine.js — AFC v4 engine for the standalone browser app.
 *
 * Byte-compatible with afc2.py / afc_native.cpp: same multi-tier scan, Bit
 * Cost Decision Engine, structural block growth, strict final audit, DP
 * optimal parsing, package-merge length-limited canonical hybrid Huffman,
 * and the AFC1 (legacy) / AFC2 (compact) containers with raw fallback.
 * Same input -> same output bytes as the Python and C++ engines.
 *
 * All arithmetic is integer-exact in float64 range (encode-side values stay
 * far below 2^53); the only BigInt use is decoding legacy AFC1 files whose
 * code lengths exceed 16 bits.
 *
 * Works in browsers (window.AFC) and Node (module.exports).
 */
"use strict";

(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AFC = factory();
})(typeof self !== "undefined" ? self : this, function () {

  // ---- engine constants (mirror afc2.py) ---------------------------------
  const NOCODE_COST = 18;
  const MAX_CODE_LEN = 16;
  const SCAN_WINDOW = 1 << 20;
  const NGRAM_MIN = 2, NGRAM_MAX = 5;
  const MIN_CANDIDATE_FREQ = 4;
  const WORD_MIN = 3, WORD_MAX = 24;
  const MAX_INITIAL_DICT = 3072;
  const MAX_DICT = 4096;
  const MAX_BLOCK = 128;
  const MERGE_ROUNDS_V4 = 6;
  const MERGES_PER_ROUND = 32;
  const DP_ROUNDS = 3;
  const TUNE_SMALL = 65536;

  const P2 = new Float64Array(54);
  for (let i = 0; i < 54; i++) P2[i] = Math.pow(2, i);

  function bitlen(q) {           // q < 2^32
    let L = 0;
    while (q >= 65536) { q = Math.floor(q / 65536); L += 16; }
    while (q > 0) { q >>= 1; L += 1; }
    return L;
  }

  function estCodeLen(f, total) {
    if (f <= 0) return NOCODE_COST;
    let q = Math.floor(total / f);
    if (q < 1) q = 1;
    const L = bitlen(q);
    return L < 1 ? 1 : (L > 24 ? 24 : L);
  }

  // patterns are kept as latin-1 strings (charCode == byte value), so
  // JS string comparison is exactly bytewise lexicographic order
  function bstr(u8, from, to) {
    let s = "";
    for (let i = from; i < to; i++) s += String.fromCharCode(u8[i]);
    return s;
  }
  function strBytes(s) {
    const a = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i);
    return a;
  }

  // ---- varints ------------------------------------------------------------
  function writeVarint(out, v) {
    for (;;) {
      const b = v % 128;
      v = Math.floor(v / 128);
      if (v) out.push(b | 0x80);
      else { out.push(b); return; }
    }
  }
  function readVarint(u8, pos) {   // -> [value, newPos]; throws on overrun
    let v = 0, mul = 1;
    for (;;) {
      if (pos >= u8.length) throw new Error("truncated varint");
      const x = u8[pos++];
      v += (x & 0x7F) * mul;
      if (!(x & 0x80)) return [v, pos];
      mul *= 128;
    }
  }

  // ---- deterministic package-merge (mirrors afc.package_merge_lengths) ----
  function packageMergeLengths(freqPairs, limit) {
    // freqPairs: array of [freq, id]
    const items = freqPairs.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const n = items.length;
    const out = new Map();
    if (n === 0) return out;
    if (n === 1) { out.set(items[0][1], 1); return out; }
    let prevW = [], prevL = [];
    for (let lvl = 0; lvl < limit; lvl++) {
      const packW = [], packL = [];
      for (let i = 0; i + 1 < prevW.length; i += 2) {
        packW.push(prevW[i] + prevW[i + 1]);
        packL.push(prevL[i].concat(prevL[i + 1]));
      }
      const curW = [], curL = [];
      let li = 0, pi = 0;
      while (li < n || pi < packW.length) {
        if (pi >= packW.length || (li < n && items[li][0] <= packW[pi])) {
          curW.push(items[li][0]);
          curL.push([li]);
          li++;
        } else {
          curW.push(packW[pi]);
          curL.push(packL[pi]);
          pi++;
        }
      }
      prevW = curW; prevL = curL;
    }
    const lengths = new Array(n).fill(0);
    const take = 2 * n - 2;
    for (let i = 0; i < take && i < prevL.length; i++)
      for (const k of prevL[i]) lengths[k]++;
    for (let k = 0; k < n; k++)
      out.set(items[k][1], Math.max(1, lengths[k]));
    return out;
  }

  // ---- canonical code assignment (length asc, id asc) ---------------------
  function canonicalCodes(lengths) {   // Map id->len  ->  Map id->[code,len]
    const syms = [...lengths.entries()].sort(
      (a, b) => a[1] - b[1] || a[0] - b[0]);
    const out = new Map();
    let code = 0;
    let prev = syms.length ? syms[0][1] : 1;
    for (const [sid, L] of syms) {
      code *= P2[L - prev];
      prev = L;
      out.set(sid, [code, L]);
      code++;
    }
    return out;
  }

  // ---- bit writer (MSB-first) ---------------------------------------------
  function BitWriter() {
    this.buf = [];
    this.acc = 0;
    this.nb = 0;
  }
  BitWriter.prototype.put = function (code, L) {
    this.acc = this.acc * P2[L] + code;
    this.nb += L;
    while (this.nb >= 8) {
      this.nb -= 8;
      const div = P2[this.nb];
      this.buf.push(Math.floor(this.acc / div));
      this.acc %= div;
    }
  };
  BitWriter.prototype.finish = function () {
    if (this.nb) {
      this.buf.push((this.acc * P2[8 - this.nb]) % 256);
      this.acc = 0;
      this.nb = 0;
    }
    return this.buf;
  };

  // ---- container emit ------------------------------------------------------
  function emitAfc1(mode, orig, patterns, lengths, bits) {
    const out = [0x41, 0x46, 0x43, 0x31, mode];   // "AFC1"
    writeVarint(out, orig);
    writeVarint(out, patterns.length);
    for (const p of patterns) {
      writeVarint(out, p.length);
      for (let i = 0; i < p.length; i++) out.push(p.charCodeAt(i));
    }
    const ids = [...lengths.keys()].sort((a, b) => a - b);
    writeVarint(out, ids.length);
    for (const sid of ids) {
      writeVarint(out, sid);
      out.push(lengths.get(sid));
    }
    return out.concat(bits);
  }

  function emitAfc2(mode, orig, patterns, lengths, bits) {
    const out = [0x41, 0x46, 0x43, 0x32, mode];   // "AFC2"
    writeVarint(out, orig);
    const ids = [...lengths.keys()].sort((a, b) => a - b);
    writeVarint(out, ids.length);
    let prev = -1;
    for (const sid of ids) {
      writeVarint(out, prev < 0 ? sid : sid - prev - 1);
      prev = sid;
    }
    const nib = new Array(Math.ceil(ids.length / 2)).fill(0);
    for (let k = 0; k < ids.length; k++) {
      const L = lengths.get(ids[k]);
      if (L < 1 || L > 16) throw new Error("AFC2 needs lengths 1..16");
      const v = L - 1;
      if (k & 1) nib[k >> 1] |= v;
      else nib[k >> 1] = v << 4;
    }
    for (const b of nib) out.push(b);
    writeVarint(out, patterns.length);
    if (patterns.length) {
      const codes = canonicalCodes(lengths);
      let can = true, rawlen = 0;
      for (const p of patterns) {
        rawlen += p.length;
        for (let i = 0; i < p.length; i++)
          if (!codes.has(p.charCodeAt(i))) { can = false; break; }
        if (!can) break;
      }
      let blob = null;
      if (can) {
        const bw = new BitWriter();
        for (const p of patterns)
          for (let i = 0; i < p.length; i++) {
            const cl = codes.get(p.charCodeAt(i));
            bw.put(cl[0], cl[1]);
          }
        blob = bw.finish();
      }
      if (blob !== null && blob.length < rawlen) {
        out.push(1);
        for (const p of patterns) writeVarint(out, p.length);
        for (const b of blob) out.push(b);
      } else {
        out.push(0);
        for (const p of patterns) writeVarint(out, p.length);
        for (const p of patterns)
          for (let i = 0; i < p.length; i++) out.push(p.charCodeAt(i));
      }
    }
    return out.concat(bits);
  }

  function emitRaw(data, afc2magic) {
    const out = [0x41, 0x46, 0x43, afc2magic ? 0x32 : 0x31, 0];
    writeVarint(out, data.length);
    const head = out.length;
    const full = new Uint8Array(head + data.length);
    full.set(out, 0);
    full.set(data, head);
    return full;
  }

  function finishContainer(mode, data, patterns, lengths, bits, fmt) {
    let blob;
    if (fmt === "afc1") {
      blob = emitAfc1(mode, data.length, patterns, lengths, bits);
    } else if (fmt === "afc2") {
      blob = emitAfc2(mode, data.length, patterns, lengths, bits);
    } else {
      blob = emitAfc1(mode, data.length, patterns, lengths, bits);
      let le16 = true;
      for (const L of lengths.values())
        if (L < 1 || L > 16) { le16 = false; break; }
      if (le16) {
        const b2 = emitAfc2(mode, data.length, patterns, lengths, bits);
        if (b2.length < blob.length) blob = b2;
      }
    }
    const raw = emitRaw(data, fmt === "afc2");
    return raw.length <= blob.length ? raw : Uint8Array.from(blob);
  }

  // ---- tier scans -----------------------------------------------------------
  const WORD = new Uint8Array(256);
  for (let c = 48; c <= 57; c++) WORD[c] = 1;
  for (let c = 65; c <= 90; c++) WORD[c] = 1;
  for (let c = 97; c <= 122; c++) WORD[c] = 1;
  WORD[95] = 1;

  function tier1LitBits(data) {
    const f = new Uint32Array(256);
    for (let i = 0; i < data.length; i++) f[data[i]]++;
    const out = new Int32Array(256);
    for (let b = 0; b < 256; b++) out[b] = estCodeLen(f[b], data.length);
    return out;
  }

  function bitCostGain(pat, f, litBits, symBits) {
    let spelled = 0;
    for (let i = 0; i < pat.length; i++) spelled += litBits[pat.charCodeAt(i)];
    return f * (spelled - symBits) - 8 * (pat.length + 3);
  }

  function selectCandidates(data, minFreq, litBits) {
    const n = data.length;
    const wn = Math.min(n, SCAN_WINDOW);
    const cands = new Map();   // pattern latin1 -> freq
    for (let L = NGRAM_MIN; L <= NGRAM_MAX; L++) {
      if (L > wn) break;
      const m = new Map();     // numeric rolling key -> count (exact <=2^40)
      const mod = P2[8 * (L - 1)];
      let key = 0;
      for (let k = 0; k < L - 1; k++) key = key * 256 + data[k];
      for (let i = 0; i + L <= wn; i++) {
        key = (key % mod) * 256 + data[i + L - 1];
        m.set(key, (m.get(key) || 0) + 1);
      }
      for (const [k, f] of m) {
        if (f >= minFreq) {
          let v = k, s = "";
          for (let j = 0; j < L; j++) {
            s = String.fromCharCode(v % 256) + s;
            v = Math.floor(v / 256);
          }
          cands.set(s, f);
        }
      }
    }
    // Tier-3 words
    {
      const words = new Map();
      let i = 0;
      while (i < n) {
        if (WORD[data[i]]) {
          let j = i;
          while (j < n && WORD[data[j]]) j++;
          const L = j - i;
          if (L >= WORD_MIN && L <= WORD_MAX) {
            const w = bstr(data, i, j);
            words.set(w, (words.get(w) || 0) + 1);
          }
          i = j;
        } else i++;
      }
      for (const [w, f] of words)
        if (f >= minFreq && !cands.has(w)) cands.set(w, f);
    }
    const scored = [];
    for (const [pat, f] of cands) {
      const gain = bitCostGain(pat, f, litBits, estCodeLen(f, n));
      if (gain > 0) scored.push([gain, pat]);
    }
    scored.sort((a, b) => b[0] - a[0] ||
                          (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
    const take = Math.min(scored.length, MAX_INITIAL_DICT);
    const patterns = new Array(take);
    for (let k = 0; k < take; k++) patterns[k] = scored[k][1];
    return patterns;
  }

  // ---- segmentation ----------------------------------------------------------
  function patIndex(patterns) {
    const pset = new Map();
    for (let i = 0; i < patterns.length; i++) pset.set(patterns[i], i);
    const seen = [];
    for (let b = 0; b < 256; b++) seen.push(null);
    for (const p of patterns) {
      const b = p.charCodeAt(0);
      if (!seen[b]) seen[b] = new Set();
      seen[b].add(p.length);
    }
    const lensAsc = [], lensDesc = [];
    for (let b = 0; b < 256; b++) {
      if (seen[b]) {
        const asc = [...seen[b]].sort((x, y) => x - y);
        lensAsc.push(asc);
        lensDesc.push(asc.slice().reverse());
      } else {
        lensAsc.push(null);
        lensDesc.push(null);
      }
    }
    return { pset, lensAsc, lensDesc };
  }

  function segmentGreedy(data, px) {
    const ids = [];
    const n = data.length;
    let i = 0;
    while (i < n) {
      const b = data[i];
      const ls = px.lensDesc[b];
      let matched = false;
      if (ls) {
        for (const L of ls) {
          if (i + L <= n) {
            const idx = px.pset.get(bstr(data, i, i + L));
            if (idx !== undefined) {
              ids.push(256 + idx);
              i += L;
              matched = true;
              break;
            }
          }
        }
      }
      if (!matched) { ids.push(b); i++; }
    }
    return ids;
  }

  function segmentOptimal(data, patterns, px, lengths) {
    const n = data.length;
    const litcost = new Int32Array(256);
    for (let b = 0; b < 256; b++)
      litcost[b] = lengths.has(b) ? lengths.get(b) : NOCODE_COST;
    const patcost = new Int32Array(patterns.length);
    for (let i = 0; i < patterns.length; i++)
      patcost[i] = lengths.has(256 + i) ? lengths.get(256 + i) : NOCODE_COST;
    const INF = Infinity;
    const cost = new Float64Array(n + 1).fill(INF);
    const back = new Int32Array(n + 1);
    const blen = new Int32Array(n + 1);
    cost[0] = 0;
    for (let i = 0; i < n; i++) {
      const c = cost[i];
      if (c === INF) continue;
      const b = data[i];
      const nc = c + litcost[b];
      if (nc < cost[i + 1]) {
        cost[i + 1] = nc;
        back[i + 1] = b;
        blen[i + 1] = 1;
      }
      const ls = px.lensAsc[b];
      if (ls) {
        for (const L of ls) {
          const j = i + L;
          if (j > n) break;
          const idx = px.pset.get(bstr(data, i, j));
          if (idx === undefined) continue;
          const pc = c + patcost[idx];
          if (pc < cost[j]) {
            cost[j] = pc;
            back[j] = 256 + idx;
            blen[j] = L;
          }
        }
      }
    }
    const ids = [];
    let pos = n;
    while (pos > 0) {
      ids.push(back[pos]);
      pos -= blen[pos];
    }
    ids.reverse();
    return ids;
  }

  // ---- structural block growth ------------------------------------------------
  function growBlocks(ids, patterns, rounds, minFreq) {
    const PKEY = 65536;   // ids < 4608, so a*65536+b is exact
    for (let r = 0; r < rounds; r++) {
      if (patterns.length >= MAX_DICT) break;
      const total = ids.length;
      if (total < 2) break;
      const pairs = new Map();
      for (let i = 0; i + 1 < total; i++) {
        const k = ids[i] * PKEY + ids[i + 1];
        pairs.set(k, (pairs.get(k) || 0) + 1);
      }
      const symCounts = new Uint32Array(256 + patterns.length);
      for (const sid of ids) symCounts[sid]++;
      const litBits = new Int32Array(256);
      for (let b = 0; b < 256; b++)
        litBits[b] = estCodeLen(symCounts[b], total);
      const expand = (sid) =>
        sid < 256 ? String.fromCharCode(sid) : patterns[sid - 256];
      const accepted = [];
      for (const [k, f] of pairs) {
        if (f < minFreq) continue;
        const a = Math.floor(k / PKEY), b = k % PKEY;
        const merged = expand(a) + expand(b);
        if (merged.length > MAX_BLOCK) continue;
        let gain = bitCostGain(merged, f, litBits, estCodeLen(f, total));
        for (const child of [a, b])
          if (child >= 256 && symCounts[child] === f && a !== b)
            gain += 8 * (patterns[child - 256].length + 3);
        if (gain > 0) accepted.push([gain, merged, a, b]);
      }
      if (!accepted.length) break;
      accepted.sort((x, y) => y[0] - x[0] ||
                              (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0) ||
                              x[2] - y[2] || x[3] - y[3]);
      let room = MAX_DICT - patterns.length;
      const chosen = new Map();
      const patIdx = new Map();
      for (let i = 0; i < patterns.length; i++) patIdx.set(patterns[i], i);
      const lim = Math.min(accepted.length, MERGES_PER_ROUND);
      for (let k = 0; k < lim; k++) {
        const [, merged, a, b] = accepted[k];
        const pk = a * PKEY + b;
        if (chosen.has(pk)) continue;
        if (patIdx.has(merged)) {
          chosen.set(pk, 256 + patIdx.get(merged));
        } else if (room > 0) {
          patterns.push(merged);
          patIdx.set(merged, patterns.length - 1);
          chosen.set(pk, 256 + patterns.length - 1);
          room--;
        }
      }
      if (!chosen.size) break;
      const out = [];
      let i = 0;
      const nn = ids.length;
      while (i < nn) {
        if (i + 1 < nn) {
          const nid = chosen.get(ids[i] * PKEY + ids[i + 1]);
          if (nid !== undefined) {
            out.push(nid);
            i += 2;
            continue;
          }
        }
        out.push(ids[i]);
        i++;
      }
      ids = out;
    }
    return [ids, patterns];
  }

  // ---- strict final audit -------------------------------------------------------
  function finalAudit(ids, patterns, litBits) {
    const total = ids.length;
    let counts = new Uint32Array(256 + patterns.length);
    for (const sid of ids) counts[sid]++;
    const drop = new Uint8Array(256 + patterns.length);
    let anyDrop = false;
    for (let idx = 0; idx < patterns.length; idx++) {
      const sid = 256 + idx;
      const f = counts[sid];
      if (f === 0) { drop[sid] = 1; anyDrop = true; continue; }
      const gain = bitCostGain(patterns[idx], f, litBits,
                               estCodeLen(f, total));
      if (gain <= 0) { drop[sid] = 1; anyDrop = true; }
    }
    if (anyDrop) {
      const out = [];
      for (const sid of ids) {
        if (sid >= 256 && drop[sid]) {
          if (counts[sid]) {
            const p = patterns[sid - 256];
            for (let i = 0; i < p.length; i++) out.push(p.charCodeAt(i));
          }
          continue;
        }
        out.push(sid);
      }
      ids = out;
      counts = new Uint32Array(256 + patterns.length);
      for (const sid of ids) counts[sid]++;
    }
    const remap = new Int32Array(256 + patterns.length);
    const kept = [];
    for (let i = 0; i < patterns.length; i++) {
      if (counts[256 + i]) {
        remap[256 + i] = 256 + kept.length;
        kept.push(patterns[i]);
      }
    }
    for (let k = 0; k < ids.length; k++)
      if (ids[k] >= 256) ids[k] = remap[ids[k]];
    return [ids, kept];
  }

  function buildLengths(ids) {
    const counts = new Map();
    for (const sid of ids) counts.set(sid, (counts.get(sid) || 0) + 1);
    const pairs = [];
    for (const [sid, f] of counts) pairs.push([f, sid]);
    return packageMergeLengths(pairs, MAX_CODE_LEN);
  }

  // ---- top level -----------------------------------------------------------------
  function compressCore(data, fmt, minFreq, rounds, litBits) {
    let patterns = selectCandidates(data, minFreq, litBits);
    let px = patIndex(patterns);
    let ids = segmentGreedy(data, px);
    [ids, patterns] = growBlocks(ids, patterns, rounds, minFreq);
    [ids, patterns] = finalAudit(ids, patterns, litBits);

    let lengths = buildLengths(ids);
    px = patIndex(patterns);
    for (let r = 0; r < DP_ROUNDS; r++) {
      ids = segmentOptimal(data, patterns, px, lengths);
      lengths = buildLengths(ids);
    }
    [ids, patterns] = finalAudit(ids, patterns, litBits);

    lengths = buildLengths(ids);
    const codes = canonicalCodes(lengths);
    const bw = new BitWriter();
    for (const sid of ids) {
      const cl = codes.get(sid);
      bw.put(cl[0], cl[1]);
    }
    return finishContainer(2, data, patterns, lengths, bw.finish(), fmt);
  }

  function compressBytes(data, adaptive, fmt) {
    if (adaptive === undefined) adaptive = true;
    if (!fmt) fmt = "auto";
    if (!(data instanceof Uint8Array)) data = new Uint8Array(data);
    if (data.length === 0) return emitRaw(data, fmt === "afc2");
    if (!adaptive) {
      const freqs = new Uint32Array(256);
      for (let i = 0; i < data.length; i++) freqs[data[i]]++;
      const pairs = [];
      for (let b = 0; b < 256; b++) if (freqs[b]) pairs.push([freqs[b], b]);
      const lengths = packageMergeLengths(pairs, MAX_CODE_LEN);
      const codes = canonicalCodes(lengths);
      const bw = new BitWriter();
      for (let i = 0; i < data.length; i++) {
        const cl = codes.get(data[i]);
        bw.put(cl[0], cl[1]);
      }
      return finishContainer(1, data, [], lengths, bw.finish(), fmt);
    }
    const litBits = tier1LitBits(data);
    let blob = compressCore(data, fmt, MIN_CANDIDATE_FREQ, MERGE_ROUNDS_V4,
                            litBits);
    if (data.length < TUNE_SMALL) {
      const alt = compressCore(data, fmt, MIN_CANDIDATE_FREQ - 1,
                               MERGE_ROUNDS_V4, litBits);
      if (alt.length < blob.length) blob = alt;
    }
    return blob;
  }

  // ---- decoder ----------------------------------------------------------------------
  function buildLut(lengths) {
    const syms = [...lengths.entries()].sort(
      (a, b) => a[1] - b[1] || a[0] - b[0]);
    let maxlen = 0;
    for (const [, L] of syms) if (L > maxlen) maxlen = L;
    if (maxlen <= 16) {
      const size = 1 << maxlen;
      const sym = new Int32Array(size).fill(-1);
      const adv = new Uint8Array(size);
      let code = 0;
      let prev = syms[0][1];
      for (const [sid, L] of syms) {
        code = code * P2[L - prev];
        prev = L;
        const base = code * P2[maxlen - L];
        const fill = 1 << (maxlen - L);
        for (let k = 0; k < fill && base + k < size; k++) {
          sym[base + k] = sid;
          adv[base + k] = L;
        }
        code++;
      }
      return { lut: true, maxlen, sym, adv };
    }
    // legacy long codes: BigInt canonical walk
    const firstcode = new Array(maxlen + 2).fill(0n);
    const firstidx = new Array(maxlen + 2).fill(0);
    const cnt = new Array(maxlen + 2).fill(0);
    const order = new Array(syms.length);
    let code = 0n;
    let prev = BigInt(syms[0][1]);
    for (let k = 0; k < syms.length; k++) {
      const L = syms[k][1];
      code <<= (BigInt(L) - prev);
      prev = BigInt(L);
      if (cnt[L] === 0) { firstcode[L] = code; firstidx[L] = k; }
      cnt[L]++;
      order[k] = syms[k][0];
      code++;
    }
    return { lut: false, maxlen, firstcode, firstidx, cnt, order };
  }

  // decodes `want` bytes; returns [outArray, bitsUsed]
  function decodeStream(blob, pos, want, patterns, table) {
    const out = [];
    if (table.lut) {
      const ml = table.maxlen;
      let acc = 0, nb = 0, bitsUsed = 0;
      const n = blob.length;
      while (out.length < want) {
        while (nb < ml && pos < n) {
          acc = acc * 256 + blob[pos++];
          nb += 8;
        }
        let idx;
        if (nb >= ml) idx = Math.floor(acc / P2[nb - ml]) % P2[ml];
        else idx = (acc * P2[ml - nb]) % P2[ml];
        const sid = table.sym[idx];
        const L = table.adv[idx];
        if (L === 0 || sid < 0 || L > nb) throw new Error("corrupt bitstream");
        nb -= L;
        acc %= P2[nb];
        bitsUsed += L;
        if (sid < 256) out.push(sid);
        else {
          const p = patterns[sid - 256];
          if (p === undefined) throw new Error("corrupt bitstream");
          const room = want - out.length;
          const take = Math.min(p.length, room);
          for (let i = 0; i < take; i++) out.push(p.charCodeAt(i));
        }
      }
      return [out, bitsUsed];
    }
    let code = 0n, L = 0, bitsUsed = 0;
    for (let i = pos; i < blob.length && out.length < want; i++) {
      const byte = blob[i];
      for (let sh = 7; sh >= 0; sh--) {
        code = (code << 1n) | BigInt((byte >> sh) & 1);
        L++;
        if (L <= table.maxlen && table.cnt[L] &&
            code >= table.firstcode[L] &&
            code - table.firstcode[L] < BigInt(table.cnt[L])) {
          const sym = table.order[table.firstidx[L] +
                                  Number(code - table.firstcode[L])];
          bitsUsed += L;
          if (sym < 256) out.push(sym);
          else {
            const p = patterns[sym - 256];
            if (p === undefined) throw new Error("corrupt bitstream");
            const room = want - out.length;
            const take = Math.min(p.length, room);
            for (let k = 0; k < take; k++) out.push(p.charCodeAt(k));
          }
          code = 0n;
          L = 0;
          if (out.length >= want) break;
        }
      }
    }
    if (out.length !== want) throw new Error("truncated bitstream");
    return [out, bitsUsed];
  }

  function decompressBytes(blob) {
    if (!(blob instanceof Uint8Array)) blob = new Uint8Array(blob);
    if (blob.length < 6) throw new Error("not a valid AFC container");
    const magic = bstr(blob, 0, 4);
    if (magic !== "AFC1" && magic !== "AFC2")
      throw new Error("not a valid AFC container");
    const mode = blob[4];
    let pos = 5;
    let orig;
    [orig, pos] = readVarint(blob, pos);
    if (mode === 0) {
      if (pos + orig > blob.length) throw new Error("truncated raw container");
      return blob.slice(pos, pos + orig);
    }
    if (orig === 0) return new Uint8Array(0);
    const patterns = [];
    const lengths = new Map();
    if (magic === "AFC1") {
      let dc;
      [dc, pos] = readVarint(blob, pos);
      for (let k = 0; k < dc; k++) {
        let L;
        [L, pos] = readVarint(blob, pos);
        patterns.push(bstr(blob, pos, pos + L));
        pos += L;
      }
      let used;
      [used, pos] = readVarint(blob, pos);
      for (let k = 0; k < used; k++) {
        let sid;
        [sid, pos] = readVarint(blob, pos);
        lengths.set(sid, blob[pos++]);
      }
    } else {
      let used;
      [used, pos] = readVarint(blob, pos);
      const idsList = new Array(used);
      let prev = -1;
      for (let k = 0; k < used; k++) {
        let d;
        [d, pos] = readVarint(blob, pos);
        const sid = prev < 0 ? d : prev + 1 + d;
        idsList[k] = sid;
        prev = sid;
      }
      const nbytes = Math.ceil(used / 2);
      for (let k = 0; k < used; k++) {
        const b = blob[pos + (k >> 1)];
        const v = (k & 1) ? (b & 0x0F) : (b >> 4);
        lengths.set(idsList[k], v + 1);
      }
      pos += nbytes;
      let dc;
      [dc, pos] = readVarint(blob, pos);
      if (dc) {
        const flag = blob[pos++];
        const plens = new Array(dc);
        let total = 0;
        for (let k = 0; k < dc; k++) {
          [plens[k], pos] = readVarint(blob, pos);
          total += plens[k];
        }
        if (flag === 0) {
          for (let k = 0; k < dc; k++) {
            patterns.push(bstr(blob, pos, pos + plens[k]));
            pos += plens[k];
          }
        } else {
          const table = buildLut(lengths);
          const [raw, bitsUsed] = decodeStream(blob, pos, total, [], table);
          pos += Math.ceil(bitsUsed / 8);
          let off = 0;
          for (let k = 0; k < dc; k++) {
            let s = "";
            for (let i = 0; i < plens[k]; i++)
              s += String.fromCharCode(raw[off + i]);
            patterns.push(s);
            off += plens[k];
          }
        }
      }
    }
    const table = buildLut(lengths);
    const [out] = decodeStream(blob, pos, orig, patterns, table);
    return Uint8Array.from(out);
  }

  return {
    compressBytes,
    decompressBytes,
    engineName: "JavaScript",
    MAGIC: ["AFC1", "AFC2"],
  };
});
