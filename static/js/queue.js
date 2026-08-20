/* queue.js — drag-and-drop queue, batch results table, and archive flows.
 *
 * DESIGN NOTES FOR PART 2
 * -----------------------
 *  - Every size limit is fetched from /api/config; NOTHING is hardcoded here.
 *    If you need a new limit, add it to config.py + public_dict(), not here.
 *  - The queue is driven client-side, ONE request per file, strictly
 *    sequential (see api_batch docstring for why concurrency is not used).
 *  - Server-side history is authoritative and is written by Flask.  This file
 *    keeps only transient UI state for the current run.
 *  - The results table sorts client-side; rows carry data-v for numeric sorts.
 */
"use strict";

(function () {
  var $ = function (id) { return document.getElementById(id); };
  var CFG = null;                  // filled from /api/config
  var queue = [];                  // {file, status, row, result}
  var results = [];                // completed result objects
  var running = false;
  var batchId = null;

  function human(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(2) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function setErr(el, msg) {
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  // ---- config -------------------------------------------------------------
  fetch("/api/config").then(function (r) { return r.json(); }).then(function (c) {
    CFG = c;
    var hint = $("limitHint");
    if (hint) {
      hint.textContent = "min " + human(c.min_file_size) + " · max "
        + human(c.max_file_size) + " per file · max "
        + human(c.max_batch_size) + " per batch";
    }
  }).catch(function () {
    var hint = $("limitHint");
    if (hint) hint.textContent = "(could not load limits from server)";
  });

  // ---- tabs ---------------------------------------------------------------
  // compress.js owns the .tab/.pane switch for this page (it also maintains
  // aria-selected and the roving tabindex). A second identical listener here
  // only ran the same work twice, so the queue no longer registers one.

  // ---- queue --------------------------------------------------------------
  var drop = $("drop"), input = $("fileInput");

  function batchBytes() {
    return queue.reduce(function (a, q) { return a + q.file.size; }, 0);
  }

  function validate(file) {
    if (!CFG) return null;
    if (file.size < CFG.min_file_size)
      return "below the " + human(CFG.min_file_size) + " minimum";
    if (file.size > CFG.max_file_size)
      return "above the " + human(CFG.max_file_size) + " per-file maximum";
    return null;
  }

  function renderQueue() {
    var body = $("queueBody");
    body.innerHTML = "";
    queue.forEach(function (item, idx) {
      var tr = document.createElement("tr");
      tr.className = "border-b border-line/60";
      var reason = validate(item.file);
      if (reason && item.status === "Queued") {
        item.status = "Error";
        item.error = "Rejected: " + reason;
      }
      tr.innerHTML =
        '<td class="px-3 py-2 break-all">' + escapeHtml(item.file.name) + '</td>' +
        '<td class="px-3 py-2 text-right font-data tabular">' + human(item.file.size) + '</td>' +
        '<td class="px-3 py-2 font-data text-xs ' + statusClass(item.status) + '">' +
          escapeHtml(item.status) + (item.error ? ' — ' + escapeHtml(item.error) : '') + '</td>' +
        '<td class="px-3 py-2"><div class="h-2 bg-line rounded-sm overflow-hidden">' +
          '<div class="h-2 bg-maroon" style="width:' + progressOf(item) + '%"></div></div></td>' +
        '<td class="px-3 py-2 text-right"></td>';
      if (item.status === "Queued") {
        var btn = document.createElement("button");
        btn.className = "text-xs text-maroon underline";
        btn.textContent = "remove";
        btn.addEventListener("click", function () {
          if (running) return;              // cannot remove once started
          queue.splice(idx, 1);
          renderQueue();
        });
        tr.lastElementChild.appendChild(btn);
      }
      body.appendChild(tr);
    });
    $("queueWrap").classList.toggle("hidden", queue.length === 0);
    $("runBtn").disabled = running || queue.filter(function (q) {
      return q.status === "Queued";
    }).length === 0;
    var total = batchBytes();
    var over = CFG && total > CFG.max_batch_size;
    $("queueTotal").textContent = queue.length + " file(s) · " + human(total)
      + (over ? " — OVER the " + human(CFG.max_batch_size) + " batch limit" : "");
    $("queueTotal").className = "font-data text-xs ml-auto "
      + (over ? "status-error font-bold" : "text-dim");
  }

  function statusClass(s) {
    return s === "Done" ? "status-ok"
      : s === "Error" ? "status-error"
      : s === "Compressing" ? "text-gold" : "text-dim";
  }
  function progressOf(item) {
    return item.status === "Done" ? 100
      : item.status === "Compressing" ? 60
      : item.status === "Error" ? 100 : 0;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function addFiles(list) {
    Array.prototype.forEach.call(list, function (f) {
      queue.push({ file: f, status: "Queued", error: null });
    });
    setErr($("err"), "");
    renderQueue();
  }

  if (drop) {
    drop.addEventListener("click", function () { input.click(); });
    drop.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    ["dragover", "dragenter"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.add("border-maroon");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.remove("border-maroon");
      });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
    });
    input.addEventListener("change", function () {
      if (input.files.length) addFiles(input.files);
      input.value = "";
    });
  }

  var clearBtn = $("clearBtn");
  if (clearBtn) clearBtn.addEventListener("click", function () {
    if (running) return;
    queue = []; results = []; batchId = null;
    $("resultsWrap").classList.add("hidden");
    renderQueue();
  });

  var runBtn = $("runBtn");
  if (runBtn) runBtn.addEventListener("click", function () {
    if (running) return;
    var total = batchBytes();
    if (CFG && total > CFG.max_batch_size) {
      setErr($("err"), "This batch is " + human(total) + ", over the "
        + human(CFG.max_batch_size) + " limit. Remove some files.");
      return;
    }
    runQueue();
  });

  function runQueue() {
    running = true;
    batchId = (Date.now().toString(36) + Math.random().toString(36).slice(2));
    setErr($("err"), "");
    var pending = queue.filter(function (q) { return q.status === "Queued"; });
    var i = 0;
    var totalBytes = batchBytes();

    // STRICTLY SEQUENTIAL: next() is only called from the previous response.
    function next() {
      if (i >= pending.length) {
        running = false;
        renderQueue();
        renderResults();
        return;
      }
      var item = pending[i++];
      item.status = "Compressing";
      renderQueue();
      var fd = new FormData();
      fd.append("file", item.file);
      fd.append("format", $("fmt").value);
      fd.append("mode", $("mode").value);
      fd.append("batch_id", batchId);
      fd.append("batch_total_bytes", String(totalBytes));
      fetch("/api/batch", { method: "POST", body: fd })
        .then(function (r) { return r.json().then(function (j) {
          return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (!res.ok) {
            item.status = "Error";
            item.error = res.j.error || "server error";
          } else {
            item.status = "Done";
            item.result = res.j;
            results.push(res.j);
          }
        })
        .catch(function (e) {
          item.status = "Error";
          item.error = e.message || "network error";
        })
        .then(function () { renderQueue(); next(); });
    }
    next();
  }

  // ---- results table ------------------------------------------------------
  function renderResults() {
    if (!results.length) return;
    var body = $("resBody");
    body.innerHTML = "";
    results.forEach(function (r) {
      var isC = r.kind === "compress";
      var out = isC ? r.compressed : r.restored;
      var tr = document.createElement("tr");
      tr.className = "border-b border-line/60";
      tr.innerHTML =
        '<td class="px-3 py-2 break-all">' + escapeHtml(r.name) + '</td>' +
        '<td class="px-3 py-2 text-right font-data tabular" data-v="' + r.original + '">' + human(r.original) + '</td>' +
        '<td class="px-3 py-2 text-right font-data tabular" data-v="' + out + '">' + human(out) + '</td>' +
        '<td class="px-3 py-2 text-right font-data tabular" data-v="' + (r.ratio || 0) + '">' +
          (isC ? (r.ratio || 0).toFixed(3) + "×" : "—") + '</td>' +
        '<td class="px-3 py-2 text-right font-data tabular" data-v="' + (r.saved || 0) + '">' +
          (isC ? (r.saved || 0).toFixed(2) + "%" : "—") + '</td>' +
        '<td class="px-3 py-2 font-data text-xs">' + escapeHtml(r.engine || "") + '</td>' +
        '<td class="px-3 py-2 font-data text-xs">' + escapeHtml(r.container || "") + '</td>' +
        '<td class="px-3 py-2">' + (r.lossless
            ? '<span class="status-ok">✓ verified</span>'
            : '<span class="status-error">✗ FAILED</span>') + '</td>' +
        '<td class="px-3 py-2 text-right"><a class="text-xs text-maroon underline"' +
          ' href="/download/' + r.token + '">download</a></td>';
      body.appendChild(tr);
    });

    var to = 0, tc = 0, fails = 0;
    results.forEach(function (r) {
      to += r.original;
      tc += (r.kind === "compress" ? r.compressed : r.restored);
      if (!r.lossless) fails++;
    });
    var errored = queue.filter(function (q) { return q.status === "Error"; }).length;
    $("resFoot").innerHTML =
      '<tr><td class="px-3 py-2">TOTAL (' + results.length + ' files)</td>' +
      '<td class="px-3 py-2 text-right font-data tabular">' + human(to) + '</td>' +
      '<td class="px-3 py-2 text-right font-data tabular">' + human(tc) + '</td>' +
      '<td class="px-3 py-2 text-right font-data tabular">' +
        (tc ? (to / tc).toFixed(3) : "0") + '×</td>' +
      '<td class="px-3 py-2 text-right font-data tabular">' +
        (to ? (100 * (1 - tc / to)).toFixed(2) : "0") + '%</td>' +
      '<td colspan="2" class="px-3 py-2"></td>' +
      '<td class="px-3 py-2 ' + ((fails + errored) ? "status-error" : "") + '">' +
        (fails + errored) + ' failure(s)</td><td></td></tr>';

    $("csvBtn").href = "/report.csv?batch_id=" + encodeURIComponent(batchId);
    $("pdfBtn").href = "/report.pdf?batch_id=" + encodeURIComponent(batchId);
    $("resultsWrap").classList.remove("hidden");
  }

  // sortable result columns
  var resTable = $("resTable");
  if (resTable) {
    var dir = {};
    resTable.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var i = +th.dataset.sort, tb = $("resBody");
        var rows = [].slice.call(tb.rows);
        dir[i] = !dir[i];
        rows.sort(function (a, b) {
          var x = a.cells[i], y = b.cells[i];
          var xv = x.dataset.v !== undefined ? parseFloat(x.dataset.v)
                                             : x.textContent.trim().toLowerCase();
          var yv = y.dataset.v !== undefined ? parseFloat(y.dataset.v)
                                             : y.textContent.trim().toLowerCase();
          return xv < yv ? (dir[i] ? -1 : 1) : xv > yv ? (dir[i] ? 1 : -1) : 0;
        });
        rows.forEach(function (r) { tb.appendChild(r); });
      });
    });
  }

  // ---- archive create -----------------------------------------------------
  var archFiles = [];
  function pickArchive(list) {
    archFiles = Array.prototype.slice.call(list).filter(function (f) {
      return f.size > 0;
    });
    var total = archFiles.reduce(function (a, f) { return a + f.size; }, 0);
    $("archInfo").textContent = archFiles.length
      ? archFiles.length + " file(s) selected · " + human(total)
      : "No files selected.";
    $("packBtn").disabled = archFiles.length === 0;
    setErr($("archErr"), "");
    if (CFG && total > CFG.max_batch_size) {
      setErr($("archErr"), "Selection is " + human(total) + ", over the "
        + human(CFG.max_batch_size) + " batch limit.");
      $("packBtn").disabled = true;
    }
  }
  ["dirInput", "multiInput"].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("change", function () { pickArchive(el.files); });
  });

  var packBtn = $("packBtn");
  if (packBtn) packBtn.addEventListener("click", function () {
    if (!archFiles.length) return;
    packBtn.disabled = true;
    $("archInfo").textContent = "Compressing " + archFiles.length + " file(s)…";
    var fd = new FormData();
    archFiles.forEach(function (f) {
      // webkitRelativePath preserves the folder structure inside the archive
      fd.append("files", f, f.webkitRelativePath || f.name);
    });
    fd.append("archive_name", $("archName").value || "archive");
    fetch("/api/archive/create", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) {
        return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        packBtn.disabled = false;
        if (!res.ok) { setErr($("archErr"), res.j.error || "failed"); return; }
        var j = res.j;
        $("archInfo").textContent = "";
        $("archSummary").textContent = j.files + " files · " + human(j.original)
          + " → " + human(j.compressed) + " (" + j.ratio + "×, "
          + j.saved + "% saved), every member SHA-256 verified.";
        $("archDl").href = "/download/" + j.token;
        $("archDl").setAttribute("download", j.name);
        $("archResult").classList.remove("hidden");
      })
      .catch(function (e) {
        packBtn.disabled = false;
        setErr($("archErr"), e.message || "network error");
      });
  });

  // ---- archive extract ----------------------------------------------------
  var pakFile = null;
  var pakInput = $("pakInput");
  if (pakInput) pakInput.addEventListener("change", function () {
    pakFile = pakInput.files[0] || null;
    $("pakInfo").textContent = pakFile
      ? pakFile.name + " · " + human(pakFile.size) : "";
    $("extractBtn").disabled = !pakFile;
    setErr($("pakErr"), "");
  });

  var extractBtn = $("extractBtn");
  if (extractBtn) extractBtn.addEventListener("click", function () {
    if (!pakFile) return;
    extractBtn.disabled = true;
    $("pakInfo").textContent = "Extracting…";
    var fd = new FormData();
    fd.append("file", pakFile);
    fetch("/api/archive/extract", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) {
        return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        extractBtn.disabled = false;
        if (!res.ok) {
          $("pakInfo").textContent = "";
          setErr($("pakErr"), res.j.error || "failed");
          return;
        }
        var j = res.j;
        $("pakInfo").textContent = j.files + " member(s) extracted and verified.";
        var body = $("pakBody");
        body.innerHTML = "";
        j.members.forEach(function (m) {
          var tr = document.createElement("tr");
          tr.className = "border-b border-line/60";
          tr.innerHTML =
            '<td class="px-3 py-2 break-all font-data text-xs">' + escapeHtml(m.path) + '</td>' +
            '<td class="px-3 py-2 text-right font-data tabular">' + human(m.bytes) + '</td>' +
            '<td class="px-3 py-2">' + (m.sha256_ok
                ? '<span class="status-ok">✓ match</span>'
                : '<span class="status-error">✗</span>') + '</td>' +
            '<td class="px-3 py-2 text-right"><a class="text-xs text-maroon underline"' +
              ' href="/download/' + m.token + '">download</a></td>';
          body.appendChild(tr);
        });
        $("pakResult").classList.remove("hidden");
      })
      .catch(function (e) {
        extractBtn.disabled = false;
        setErr($("pakErr"), e.message || "network error");
      });
  });
})();

/* ==========================================================================
 * PART 2 additions: pre-compression estimate, preview, presets, hybrid tree,
 * plain-language explainer, and batch-completion notification.
 * All of it reads endpoints that perform READ-ONLY analysis; no engine file
 * is touched.
 * ====================================================================== */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  if (!$("prePanel")) return;                       // not on the compress page

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ---- Feature 6 + 11: entropy estimate + preview on selection ------------
  function inspect(file) {
    var fd = new FormData();
    fd.append("file", file);
    fetch("/api/entropy", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) return;
        $("prePanel").classList.remove("hidden");
        $("preName").textContent = "— " + j.filename;
        $("entVal").textContent = j.entropy_bits_per_byte.toFixed(3);
        $("entBar").style.width =
          (100 * j.entropy_bits_per_byte / 8).toFixed(1) + "%";
        $("entBand").innerHTML = "Predicted compressibility: <strong>" +
          esc(j.band) + "</strong> (order-0 floor ≈ " +
          j.order0_ratio.toFixed(2) + "×, " + j.unique_bytes +
          " distinct byte values)";
        $("entNote").textContent = j.note;
        var hist = $("entHist");
        hist.innerHTML = "";
        var max = j.histogram.reduce(function (a, h) {
          return Math.max(a, h.pct); }, 1);
        j.histogram.forEach(function (h) {
          var d = document.createElement("div");
          d.style.height = Math.max(2, 100 * h.pct / max) + "%";
          d.style.width = "10px";
          d.style.background = "#8E1F1F";
          d.title = h.label + " (0x" + h.byte.toString(16) + ") — " +
                    h.pct.toFixed(2) + "%";
          hist.appendChild(d);
        });
      })
      .catch(function () { /* estimate is advisory; never block the queue */ });

    var fd2 = new FormData();
    fd2.append("file", file);
    fetch("/api/preview", { method: "POST", body: fd2 })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) return;
        $("prevKind").textContent = "— " + (j.kind === "hex"
          ? "binary (hex)" : "text");
        $("prevBody").textContent = (j.lines || []).join("\n");
      })
      .catch(function () {});
  }

  // Hook the existing input WITHOUT changing its behaviour.
  //
  // NOTE: the Part 1 change-handler above clears `input.value` after queueing
  // (so the same file can be re-picked). It was registered first, so a plain
  // listener here would run AFTER the clear and always see zero files — that
  // is exactly the bug this capture-phase listener avoids. Capture on
  // `document` fires before any listener bound to the target element.
  var drop = $("drop");
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "fileInput" && e.target.files
        && e.target.files.length) {
      inspect(e.target.files[0]);
    }
  }, true);
  if (drop) drop.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) inspect(e.dataTransfer.files[0]);
  }, true);

  // ---- Feature 9: send the chosen preset with every request --------------
  // queue.js's own fetch calls build their FormData before this file runs, so
  // we patch FormData.append-time by intercepting fetch for the batch route.
  var origFetch = window.fetch;
  window.fetch = function (url, opts) {
    try {
      if (typeof url === "string" && opts && opts.body instanceof FormData
          && (url === "/api/batch" || url === "/api/compress")) {
        var sel = $("preset");
        if (sel && !opts.body.has("preset")) opts.body.append("preset", sel.value);
      }
    } catch (e) { /* never break the upload because of an optional field */ }
    return origFetch.apply(this, arguments);
  };

  // ---- Feature 7 + 8: tree visualiser ------------------------------------
  var NS = "http://www.w3.org/2000/svg";
  function drawTree(node, summary) {
    var svg = $("treeSvg");
    svg.innerHTML = "";
    if (!node) return;
    var W = 980, H = 330, depth = 0;
    // measure depth for layout
    (function m(n, d) {
      depth = Math.max(depth, d);
      if (n && n.type === "node") {
        m(n.children[0], d + 1); m(n.children[1], d + 1);
      }
    })(node, 0);
    var levelH = Math.max(34, (H - 40) / Math.max(1, depth));
    var leaves = [];
    (function collect(n) {
      if (!n) return;
      if (n.type === "node") { collect(n.children[0]); collect(n.children[1]); }
      else leaves.push(n);
    })(node);
    var slot = 0;
    var total = Math.max(1, leaves.length);

    function add(tag, attrs, text) {
      var e = document.createElementNS(NS, tag);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (text != null) e.textContent = text;
      svg.appendChild(e);
      return e;
    }
    function place(n, d) {
      if (!n) return null;
      if (n.type === "node") {
        var l = place(n.children[0], d + 1);
        var r = place(n.children[1], d + 1);
        var xs = [l, r].filter(Boolean).map(function (p) { return p.x; });
        var x = xs.length ? xs.reduce(function (a, b) { return a + b; }, 0) / xs.length
                          : (slot++ + 0.5) * (W / total);
        var y = 20 + d * levelH;
        [l, r].forEach(function (p) {
          if (p) add("line", {x1: x, y1: y, x2: p.x, y2: p.y,
                              stroke: "#E4E0D6", "stroke-width": 1});
        });
        add("circle", {cx: x, cy: y, r: 3, fill: "#6B675E"});
        return {x: x, y: y};
      }
      var lx = (slot++ + 0.5) * (W / total);
      var ly = 20 + d * levelH;
      var colour = n.type === "block" ? "#8E1F1F"
                 : n.type === "literal" ? "#B57A17" : "#E4E0D6";
      var g = add("circle", {cx: lx, cy: ly, r: n.type === "collapsed" ? 7 : 5,
                             fill: colour});
      var t = document.createElementNS(NS, "title");
      t.textContent = n.type === "collapsed"
        ? (n.leaves + " more symbols (" + n.literals + " literal, "
           + n.blocks + " block)")
        : (n.type === "block"
            ? ('block "' + n.label + '" — ' + n.length + " bytes, "
               + n.bits + "-bit code")
            : ('literal ' + n.label + " — " + n.bits + "-bit code"));
      g.appendChild(t);
      if (n.type !== "collapsed" && total <= 64) {
        add("text", {x: lx, y: ly + 15, fill: "#6B675E", "font-size": 8,
                     "text-anchor": "middle", "font-family": "monospace"},
            n.label.slice(0, 6));
      }
      return {x: lx, y: ly};
    }
    place(node, 0);
    $("treeSummary").textContent = summary
      ? (summary.literal_symbols + " literal + " + summary.block_symbols +
         " block leaves · codes " + summary.min_code_length + "-" +
         summary.max_code_length + " bits · " + summary.container)
      : "";
    drawLengths(summary);
  }

  /* Code-length distribution, literal vs block. The drawn tree is depth-cut
     for legibility, so this chart quantifies the hybrid split across the FULL
     alphabet: literals earn short codes, structural blocks longer ones (but
     each replaces several bytes). */
  function drawLengths(summary) {
    var host = $("lenChart");
    if (!host || !summary || !summary.code_lengths) return;
    var lit = summary.code_lengths.literal || {};
    var blk = summary.code_lengths.block || {};
    var all = {};
    Object.keys(lit).forEach(function (k) { all[k] = true; });
    Object.keys(blk).forEach(function (k) { all[k] = true; });
    var keys = Object.keys(all).map(Number).sort(function (a, b) { return a - b; });
    if (!keys.length) { host.innerHTML = ""; return; }
    var max = 0;
    keys.forEach(function (k) {
      max = Math.max(max, (lit[k] || 0) + (blk[k] || 0)); });
    var html = '<p class="font-data text-[11px] uppercase text-dim ' +
      'dark:text-[#9c968a] mt-4">Code length distribution (whole alphabet)</p>' +
      '<div class="flex items-end gap-1 h-24 mt-1">';
    keys.forEach(function (k) {
      var l = lit[k] || 0, b = blk[k] || 0;
      var hl = 100 * l / max, hb = 100 * b / max;
      html += '<div class="flex-1 flex flex-col justify-end items-center" ' +
        'title="' + k + '-bit codes: ' + b + ' block, ' + l + ' literal">' +
        '<div style="width:100%;height:' + hb + '%;background:#8E1F1F"></div>' +
        '<div style="width:100%;height:' + hl + '%;background:#B57A17"></div>' +
        '<span class="text-[9px] text-dim dark:text-[#9c968a] font-data">' +
        k + '</span></div>';
    });
    html += '</div><p class="text-[10px] text-dim dark:text-[#9c968a] mt-1">' +
      'bits per code →</p>';
    host.innerHTML = html;
  }

  function drawAttribution(a) {
    var wrap = $("attrBars");
    if (!a || a.raw) { wrap.innerHTML = ""; return; }
    var b = a.block.share_pct, l = a.literal.share_pct;
    wrap.innerHTML =
      '<p class="font-data text-[11px] uppercase text-dim mt-4">Where the ' +
      'savings came from</p>' +
      '<div class="flex h-4 mt-1 rounded-sm overflow-hidden border border-line">' +
        '<div style="width:' + Math.max(0, b) + '%;background:#8E1F1F" ' +
          'title="structural blocks ' + b + '%"></div>' +
        '<div style="width:' + Math.max(0, l) + '%;background:#B57A17" ' +
          'title="literal bytes ' + l + '%"></div></div>' +
      '<p class="text-xs text-dim mt-1">structural blocks ' + b.toFixed(1) +
      '% · literal bytes ' + l.toFixed(1) + '% · ' + a.symbols_emitted +
      ' symbols emitted</p>';
  }

  function loadTree(token) {
    fetch("/api/tree/" + token).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) {
          $("explainLine").textContent = j.error;
          return;
        }
        $("explainLine").textContent = j.explain || "";
        drawTree(j.tree && j.tree.tree, j.tree && j.tree.summary);
        drawAttribution(j.attribution);
      });
  }

  // Populate the tree picker whenever a batch finishes. We observe the
  // results table rather than modifying the existing run loop.
  var resBody = $("resBody");
  if (resBody) {
    new MutationObserver(function () {
      var opts = [];
      resBody.querySelectorAll("tr").forEach(function (tr) {
        var a = tr.querySelector('a[href^="/download/"]');
        if (!a) return;
        opts.push({ name: tr.cells[0].textContent.trim(),
                    token: a.getAttribute("href").split("/").pop() });
      });
      if (!opts.length) return;
      var pick = $("treePick");
      pick.innerHTML = "";
      opts.forEach(function (o) {
        var el = document.createElement("option");
        el.value = o.token; el.textContent = o.name;
        pick.appendChild(el);
      });
      $("treeWrap").classList.remove("hidden");
      loadTree(opts[0].token);
      notifyDone(opts.length);
    }).observe(resBody, { childList: true });
  }
  var pick = $("treePick");
  if (pick) pick.addEventListener("change", function () { loadTree(pick.value); });

  // ---- Feature 12: in-app notification on batch completion ---------------
  var notified = 0;
  function notifyDone(count) {
    if (count === notified) return;
    notified = count;
    var n = document.createElement("div");
    n.className = "fixed bottom-20 lg:bottom-6 right-4 z-50 bg-white border " +
      "border-maroon rounded-lg shadow-lg px-4 py-3 text-sm";
    n.setAttribute("role", "status");
    n.innerHTML = '<strong>Batch complete.</strong> ' + count +
      ' file(s) processed. <a class="text-maroon underline" href="#treeWrap">' +
      'Inspect the tree</a>';
    document.body.appendChild(n);
    setTimeout(function () { n.remove(); }, 8000);
  }
})();
