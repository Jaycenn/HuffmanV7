/* decompress.js — the single-file DECOMPRESS workflow.
 *
 * SCOPE
 * -----
 * Decompression only. Posts to /api/decompress, which refuses anything that
 * is not an AFC container. A normal file dropped here is caught client-side
 * first and answered with a link to /compress, so the two workflows can never
 * be confused for one another.
 *
 * The archive-extract pane on this page is handled by queue.js (pakInput /
 * extractBtn), which is loaded alongside this file.
 */
"use strict";

(function () {
  var $ = function (id) { return document.getElementById(id); };
  if (!$("dDrop")) return;                    // not on the decompress page

  var picked = null, busy = false;

  function human(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(2) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function show(el, on) { el.classList.toggle("hidden", !on); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function setErr(msg) {
    var el = $("dErr");
    el.innerHTML = msg || "";
    show(el, !!msg);
  }

  // ---- tabs ---------------------------------------------------------------
  document.querySelectorAll(".dtab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".dtab").forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("border-maroon", on);
        b.classList.toggle("text-maroon", on);
        b.classList.toggle("font-semibold", on);
        b.classList.toggle("border-transparent", !on);
        b.classList.toggle("text-dim", !on);
      });
      document.querySelectorAll(".dpane").forEach(function (p) {
        p.classList.add("hidden");
      });
      var pane = $("dpane-" + btn.dataset.dtab);
      if (pane) pane.classList.remove("hidden");
    });
  });

  // ---- AFC guard ----------------------------------------------------------
  function readMagic(file) {
    return new Promise(function (resolve) {
      var fr = new FileReader();
      fr.onload = function () {
        var b = new Uint8Array(fr.result), s = "";
        for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
        resolve(s);
      };
      fr.onerror = function () { resolve(""); };
      fr.readAsArrayBuffer(file.slice(0, 8));
    });
  }

  function pick(file) {
    if (!file) return;
    setErr("");
    show($("dResult"), false);
    readMagic(file).then(function (magic) {
      var four = magic.slice(0, 4);
      if (magic.slice(0, 8) === "AFCPAK01") {
        setErr('This is an archive, not a single AFC container. Use the '
             + '<strong>Extract archive</strong> tab above.');
        return;
      }
      if (four !== "AFC1" && four !== "AFC2" && four !== "AFC3" && four !== "AFC4" && four !== "AFC5" && four !== "AFC6") {
        setErr('This is not an AFC file. The Decompress page only restores '
             + '<code>.afc</code> containers — open the '
             + '<a class="underline font-semibold" href="/compress">Compress '
             + 'page</a> to compress this file instead.');
        return;
      }
      picked = file;
      $("dPickName").textContent = file.name;
      $("dPickSize").textContent = human(file.size);
      $("dPickType").textContent = four + " container";
      show($("dPicked"), true);
      $("dRun").disabled = false;
    });
  }

  // ---- drop zone ----------------------------------------------------------
  var drop = $("dDrop"), input = $("dInput");
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

  $("dClear").addEventListener("click", function () {
    picked = null;
    show($("dPicked"), false);
    show($("dResult"), false);
    setErr("");
    $("dRun").disabled = true;
  });

  // ---- run ----------------------------------------------------------------
  $("dRun").addEventListener("click", function () {
    if (!picked || busy) return;
    busy = true;
    $("dRun").disabled = true;
    setErr("");
    show($("dResult"), false);
    show($("dProgWrap"), true);
    $("dProg").style.width = "0%";
    $("dStatus").textContent = "Uploading…";

    var fd = new FormData();
    fd.append("file", picked);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/decompress");
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      $("dProg").style.width = Math.round(70 * e.loaded / e.total) + "%";
      if (e.loaded >= e.total) $("dStatus").textContent = "Decompressing…";
    };
    xhr.onload = function () {
      busy = false;
      $("dRun").disabled = false;
      $("dProg").style.width = "100%";
      $("dStatus").textContent = "";
      var j;
      try { j = JSON.parse(xhr.responseText); }
      catch (e) { setErr("Unexpected server response."); return; }
      if (xhr.status !== 200) {
        show($("dProgWrap"), false);
        setErr(esc(j.error || "Decompression failed."));
        return;
      }
      render(j);
    };
    xhr.onerror = function () {
      busy = false;
      $("dRun").disabled = false;
      show($("dProgWrap"), false);
      $("dStatus").textContent = "";
      setErr("Network error while uploading.");
    };
    xhr.send(fd);
  });

  function render(j) {
    $("dAfcName").textContent = j.afc_name;
    $("dAfcSize").textContent = human(j.afc_bytes);
    $("dResName").textContent = j.restored_name;
    $("dResSize").textContent = human(j.restored_bytes);
    $("dTime").textContent = j.ms < 1000 ? j.ms.toFixed(2) + " ms"
                                         : (j.ms / 1000).toFixed(2) + " s";
    $("dContainer").textContent = j.container;
    $("dMode").textContent = j.container_mode || "";
    $("dType").textContent = j.detected || "";
    $("dEngine").textContent = j.engine || "";

    $("dIntegrity").innerHTML = j.integrity_ok
      ? '<span class="text-green-700">✓ VERIFIED</span>'
      : '<span class="text-maroon">✗ FAILED</span>';
    $("dIntegrityNote").textContent =
      (j.integrity_note || "") + (j.component_note ? " " + j.component_note : "");

    // Three distinct verdicts. "No reference" is NOT dressed up as a match:
    // without a recorded digest there is nothing to compare against.
    var v = $("dShaVerdict");
    if (j.sha256_status === "match") {
      v.innerHTML = '<span class="text-green-700">✓ MATCH</span>';
    } else if (j.sha256_status === "mismatch") {
      v.innerHTML = '<span class="text-maroon">✗ MISMATCH</span>';
    } else {
      v.innerHTML = '<span class="text-dim dark:text-[#9c968a]">— no reference '
                  + 'on file</span>';
    }
    $("dShaNote").textContent = j.sha256_note || "";
    $("dShaValue").textContent = "SHA-256 of restored file: " + j.sha256_restored;

    $("dDownload").href = "/download/" + j.token;
    $("dDownload").setAttribute("download", j.restored_name);
    $("dDownload").textContent = "Download " + j.restored_name;

    show($("dProgWrap"), false);
    show($("dResult"), true);
    $("dResult").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
})();
