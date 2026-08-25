/* compress.js — the single-file COMPRESS workflow.
 *
 * SCOPE
 * -----
 * Compression only. This file never calls a decompress endpoint. If the user
 * drops an AFC container here it is refused with a pointer to /decompress —
 * the operation is never silently switched, which is the whole point of
 * splitting the two pages.
 *
 * It posts to the existing /api/compress endpoint and renders what comes back.
 * No compression logic lives in the browser; the engine is unchanged.
 */
"use strict";

(function () {
  var $ = function (id) { return document.getElementById(id); };
  if (!$("sDrop")) return;                    // not on the compress page

  var CFG = null, picked = null, busy = false;

  function human(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(2) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function show(el, on) { el.classList.toggle("hidden", !on); }
  function setErr(msg) {
    var el = $("sErr");
    el.innerHTML = msg || "";
    show(el, !!msg);
  }

  fetch("/api/config").then(function (r) { return r.json(); }).then(function (c) {
    CFG = c;
    $("sLimit").textContent = "Up to " + human(c.max_file_size) + " per file";
  }).catch(function () { $("sLimit").textContent = ""; });

  // ---- tabs (this page's own panes; queue.js owns the queue/archive ones) --
  document.querySelectorAll(".tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("border-maroon", on);
        b.classList.toggle("text-maroon", on);
        b.classList.toggle("font-semibold", on);
        b.classList.toggle("border-transparent", !on);
        b.classList.toggle("text-dim", !on);
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
      });
      document.querySelectorAll(".pane").forEach(function (p) {
        p.classList.add("hidden");
      });
      var pane = $("pane-" + btn.dataset.tab);
      if (pane) pane.classList.remove("hidden");
    });
    btn.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight"
          && event.key !== "Home" && event.key !== "End") return;
      event.preventDefault();
      var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
      var index = tabs.indexOf(btn);
      if (event.key === "Home") index = 0;
      else if (event.key === "End") index = tabs.length - 1;
      else index = (index + (event.key === "ArrowRight" ? 1 : -1)
                    + tabs.length) % tabs.length;
      tabs[index].focus();
      tabs[index].click();
    });
  });

  // ---- AFC guard ----------------------------------------------------------
  // Read the first 4 bytes so an .afc file is caught BEFORE any upload.
  function readMagic(file) {
    return new Promise(function (resolve) {
      var fr = new FileReader();
      fr.onload = function () {
        var b = new Uint8Array(fr.result);
        var s = "";
        for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
        resolve(s);
      };
      fr.onerror = function () { resolve(""); };
      fr.readAsArrayBuffer(file.slice(0, 4));
    });
  }

  function pick(file) {
    if (!file) return;
    setErr("");
    show($("sResult"), false);
    if (CFG) {
      if (file.size < CFG.min_file_size) {
        setErr("That file is " + human(file.size) + ", below the "
               + human(CFG.min_file_size) + " minimum.");
        return;
      }
      if (file.size > CFG.max_file_size) {
        setErr("That file is " + human(file.size) + ", above the "
               + human(CFG.max_file_size) + " maximum for a single file.");
        return;
      }
    }
    readMagic(file).then(function (magic) {
      if (magic === "AFC1" || magic === "AFC2" || magic === "AFC3" || magic === "AFC4" || magic === "AFC5" || magic === "AFC6") {
        setErr('This is already an AFC container. Compressing it again would '
             + 'just nest it. Open the <a class="underline font-semibold" '
             + 'href="/decompress">Decompress page</a> to restore it.');
        return;
      }
      if (magic === "AFCP") {
        setErr('This is an archive. Open the <a class="underline font-semibold" '
             + 'href="/decompress">Decompress page</a> to extract it.');
        return;
      }
      picked = file;
      $("sPickName").textContent = file.name;
      $("sPickSize").textContent = human(file.size);
      $("sPickType").textContent = "detecting…";
      show($("sPicked"), true);
      $("sRun").disabled = false;
      inspect(file);
    });
  }

  // ---- pre-compression estimate, preview and detected type ----------------
  function inspect(file) {
    var fd = new FormData();
    // Detection needs only the file signature/head. Sending the complete
    // upload here made remote deployments transfer a large file once for
    // preview, again for entropy, and then a third time for compression.
    // Preserve the original filename while bounding this advisory request.
    fd.append("file", file.slice(0, 64 * 1024), file.name);
    fetch("/api/preview", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) return;
        if (j.detected) $("sPickType").textContent = j.detected;
        if (j.container_aware) {
          $("sAuto").textContent = j.detected + " detected — its internal "
            + "components are handled automatically. Upload the whole file; "
            + "you never need to extract anything yourself.";
          show($("sAuto"), true);
        } else {
          show($("sAuto"), false);
        }
        $("sPrevKind").textContent = "— " + (j.kind === "hex" ? "binary (hex)" : "text");
        $("sPrevBody").textContent = (j.lines || []).join("\n");
        show($("sPre"), true);
      })
      .catch(function () { $("sPickType").textContent = ""; });

    var fd2 = new FormData();
    // A deterministic prefix sample is sufficient for the advisory entropy
    // estimate. The full file is still uploaded and verified when Compress
    // File is clicked; this changes no compression or losslessness behavior.
    fd2.append("file", file.slice(0, 256 * 1024), file.name);
    fetch("/api/entropy", { method: "POST", body: fd2 })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.error) return;
        $("sEntVal").textContent = j.entropy_bits_per_byte.toFixed(3);
        $("sEntBar").style.width = (100 * j.entropy_bits_per_byte / 8).toFixed(1) + "%";
        // Not a prediction: this is a measured bound for the displayed prefix
        // sample, which keeps remote preflight inspection quick.
        $("sEntBand").innerHTML = "Prefix-sample byte-level limit ≈ <strong>"
          + j.order0_ratio.toFixed(2) + "×</strong> — " + esc(j.band)
          + " redundancy";
        show($("sPre"), true);
      })
      .catch(function () {});
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ---- drop zone ----------------------------------------------------------
  var drop = $("sDrop"), input = $("sInput");
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
    if (e.dataTransfer.files.length) pick(e.dataTransfer.files[0]);
  });
  input.addEventListener("change", function () {
    if (input.files.length) pick(input.files[0]);
    input.value = "";
  });

  $("sClear").addEventListener("click", function () {
    picked = null;
    show($("sPicked"), false);
    show($("sPre"), false);
    show($("sResult"), false);
    setErr("");
    $("sRun").disabled = true;
  });

  // ---- run ----------------------------------------------------------------
  $("sRun").addEventListener("click", function () {
    if (!picked || busy) return;
    busy = true;
    $("sRun").disabled = true;
    setErr("");
    show($("sResult"), false);
    show($("sProgWrap"), true);
    $("sProg").style.width = "0%";
    $("sStatus").textContent = "Uploading…";

    var fd = new FormData();
    fd.append("file", picked);
    // No container field: the server defaults to "auto", which already emits
    // whichever of AFC1/AFC2 is smaller for this file.
    fd.append("mode", $("sMode").value);
    fd.append("preset", $("sPreset").value);

    // XHR (not fetch) so the upload phase can drive a real progress bar.
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/compress");
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round(70 * e.loaded / e.total);
      $("sProg").style.width = pct + "%";
      if (e.loaded >= e.total) $("sStatus").textContent = "Compressing…";
    };
    xhr.onload = function () {
      busy = false;
      $("sRun").disabled = false;
      $("sProg").style.width = "100%";
      $("sStatus").textContent = "";
      var j;
      try { j = JSON.parse(xhr.responseText); }
      catch (e) { setErr("Unexpected server response."); return; }
      if (xhr.status !== 200) {
        show($("sProgWrap"), false);
        setErr(esc(j.error || "Compression failed."));
        return;
      }
      render(j);
    };
    xhr.onerror = function () {
      busy = false;
      $("sRun").disabled = false;
      show($("sProgWrap"), false);
      $("sStatus").textContent = "";
      setErr("Network error while uploading.");
    };
    xhr.send(fd);
  });

  function render(j) {
    $("rOrigName").textContent = j.name;
    $("rOrigSize").textContent = human(j.original)
      + (j.detected ? " · " + j.detected : "");
    $("rCompName").textContent = j.output_name || (j.name + ".afc");
    $("rCompSize").textContent = human(j.compressed);
    $("rSaved").textContent = (j.saved >= 0 ? "" : "") + j.saved.toFixed(2) + "%";
    $("rRatio").textContent = j.ratio.toFixed(2) + "×";
    $("rTime").textContent = j.ms < 1000 ? j.ms.toFixed(1) + " ms"
                                         : (j.ms / 1000).toFixed(2) + " s";
    // Backend, stated plainly. A silent fall back to pure Python is ~12x
    // slower with identical output, so when it happens the reason is shown
    // right here rather than left for the user to discover.
    if (j.native_available) {
      $("rEngine").innerHTML = '<span class="status-ok font-semibold">'
        + 'C++ Native \u2713</span>';
      $("rNative").classList.add("hidden");
    } else {
      $("rEngine").innerHTML = '<span class="status-warning font-semibold">'
        + 'Pure Python \u26a0</span>';
      var n = $("rNative");
      n.innerHTML = '<strong>Native backend unavailable.</strong><br>'
        + '<span class="font-data text-[11px]">Reason: ' + esc(j.native_reason
        || "unknown") + '</span><br><span class="font-data text-[11px]">'
        + 'Run <code>python -m afc_native --diagnose</code> for the full '
        + 'diagnosis. Compression output is identical either way \u2014 only '
        + 'speed differs.</span>';
      n.classList.remove("hidden");
    }
    // AFC5 is the integrity envelope, not the component-routing decision.
    // Show both layers so a PDF/DOCX result makes the winning AFC3/AFC4/AFC6
    // payload visible instead of presenting every new file as merely AFC5.
    var route = j.container || "";
    if (j.payload_container && j.payload_container !== j.container) {
      route += " envelope \u2192 " + j.payload_container + " payload";
    } else {
      route += " container";
    }
    $("rContainer").textContent = route
      + (j.preset ? " \u00b7 " + j.preset + " preset" : "");
    $("rVerify").innerHTML = j.lossless
      ? '<span class="status-ok font-semibold">✓ VERIFIED — lossless '
        + 'SHA-256 round trip</span>'
      : '<span class="status-error font-semibold">✗ VERIFICATION FAILED — '
        + 'do not rely on this file</span>';
    $("rSha").textContent = j.sha256_original
      ? "SHA-256 of original: " + j.sha256_original : "";
    $("rExplain").textContent = j.explain || "";
    $("rDownload").href = "/download/" + j.token;
    $("rDownload").setAttribute("download", j.output_name || (j.name + ".afc"));

    // A negative saving means the container was larger than the input — say so
    // plainly rather than dressing it up.
    $("rSaved").className = "font-data tabular text-2xl mt-1 "
      + (j.saved > 0 ? "text-maroon" : "text-dim");

    show($("sProgWrap"), false);
    show($("sResult"), true);
    $("sResult").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
})();
