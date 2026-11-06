// corla-observer extension: check every receipt the server returns, in the extension, as entries are sent.
//
// The observation page (observe.js) announces the server's receipt key when it loads the list, and each
// receipt as it arrives. This checks, for each receipt:
//   - the server's Ed25519 signature over its canonical body (docs/RECEIPTS.md), with the receipt key this
//     extension pinned for that server the first time it saw it: a different key later is a warning;
//   - that it is for the payload this device signed (its payload_sha256);
//   - that the log heads it states agree with every other receipt seen from that server: two different
//     hashes for one position in the log mean the server has shown two different logs.
// Heads and the pinned key are kept in the extension's storage, so the checks hold across sessions.
//
// If the observer gave the published commitments' folder (on the links page, from their coordinator, never
// from the server), it also reads every SHA256SUMS*.txt there, from GitHub, and checks that the extract the
// page shows is one the commitments list, and that no anchored log head (each file's event-log-head) differs
// from a head a receipt stated for the same position.
//
// Then it checks that all those points (the heads its receipts stated and the published anchors) lie on
// one log: it fetches the log's lines between them from the server (/observe/TOKEN/log), recomputes each
// line's hash from its own bytes and follows the prev_hash links (docs/EVENT_LOG.md). The server cannot
// fake a chain through points it has already committed to.
//
// And it computes the observation report (report.js, corla_observer.obsreport) from the device's own
// records, the entries the server has acknowledged, and compares it with the report the server signs for
// this device (/observe/TOKEN/report.json): the same ballots, verdicts and contests, or an alert. The result is shown at the top of the page; a
// failure is an alert.
(function () {
  "use strict";
  var api = typeof browser !== "undefined" ? browser : chrome;
  var server = null;          // the server, from the observer link
  var key = null;             // {algorithm, public_key, key_id}, pinned
  var cryptoKey = null;       // the imported Ed25519 key
  var verified = 0;
  var problems = [];
  var unsupported = false;
  var pending = [];
  var extractNote = "";        // what the commitments say of the extract shown
  var anchors = {};            // position -> hash, from the commitments' headers
  var committedKeys = null;    // the receipt keys' hashes the commitments list (derived/receipt-key.json), once read
  var keyNote = "";
  var reportNote = "";
  var who = null;              // {observer, county, board, extract_sha256, records}: for the report
  var chainNote = "";          // what the log's chain check found
  var chainTimer = null;
  var LOG_LINES = 2000;        // the server's most lines at once (web/observe.py, MAX_LOG_LINES)
  var queue = Promise.resolve();   // checks run one at a time, so the stored heads are read and written in turn

  function storageGet(name, fallback) {
    return new Promise(function (resolve) { var q = {}; q[name] = fallback; api.storage.local.get(q, function (got) { resolve(got[name]); }); });
  }
  function storageSet(name, value) { var q = {}; q[name] = value; return new Promise(function (resolve) { api.storage.local.set(q, resolve); }); }
  function bytes(b64) { return Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); }); }

  function box() {
    var node = document.getElementById("ext-verify");
    if (!node) {
      node = document.createElement("p");
      node.id = "ext-verify";
      node.className = "meta";
      var anchor = document.querySelector("main") || document.body;
      anchor.insertBefore(node, anchor.firstChild);
    }
    return node;
  }
  function show() {
    var node = box();
    if (problems.length) {
      node.className = "notice notice-error";
      node.setAttribute("role", "alert");
      node.textContent = "The extension found a problem with the server's receipts: " + problems.join("; ") +
        ". Keep your record (More, Download my record) and tell your coordinator.";
    } else if (unsupported) {
      node.className = "meta";
      node.textContent = "This browser cannot check the server's receipts (no Ed25519 in Web Crypto): use a current Firefox or Chrome.";
    } else {
      node.className = "meta";
      node.removeAttribute("role");
      node.textContent = "Receipts checked by this extension: " + verified + " verified" +
        (key ? " (server receipt key " + key.key_id.slice(0, 16) + "…)" : "") + "." + (reportNote ? " " + reportNote : "") + (keyNote ? " " + keyNote : "") + (extractNote ? " " + extractNote : "") + (chainNote ? " " + chainNote : "");
    }
  }
  function problem(text) { if (problems.indexOf(text) < 0) problems.push(text); show(); }

  // What the checks found, for the observer's downloaded record (observe.js adds it as extension_checks).
  window.CorlaExtensionChecks = function () {
    return {
      extension_version: chrome.runtime.getManifest().version,
      checked_at: new Date().toISOString(),
      receipts_verified: verified,
      receipt_key_id: key ? key.key_id : null,
      problems: problems.slice(),
      report: reportNote, receipt_key: keyNote, extract: extractNote, chain: chainNote,
      unsupported: !!unsupported
    };
  };

  document.addEventListener("corla-observer:meta", function (event) {
    var detail = event.detail || {};
    var offered = detail.receipt_key;
    server = detail.server || "";
    who = { observer: detail.observer, county: detail.county, board: detail.board, extract_sha256: detail.extract_sha256, records: detail.records };
    if (!offered || offered.algorithm !== "Ed25519") { problem("the server gave no Ed25519 receipt key"); return; }
    commitments(detail.extract_sha256);
    storageGet("receipt-key:" + server, null).then(function (pinned) {
      if (pinned && pinned.public_key !== offered.public_key) {
        problem("the server's receipt key changed from " + pinned.key_id.slice(0, 16) + "… (first seen " + pinned.first_seen + ") to " + offered.key_id.slice(0, 16) + "…");
        return null;
      }
      if (!pinned) storageSet("receipt-key:" + server, { algorithm: offered.algorithm, public_key: offered.public_key, key_id: offered.key_id, first_seen: new Date().toISOString() });
      key = offered;
      return crypto.subtle.importKey("raw", bytes(offered.public_key), { name: "Ed25519" }, false, ["verify"]);
    }).then(function (imported) {
      if (!imported) return;
      cryptoKey = imported;
      checkKeyCommitted();
      show();
      var waiting = pending; pending = [];
      waiting.forEach(enqueue);
    }).catch(function () { unsupported = true; show(); });
  });

  document.addEventListener("corla-observer:receipt", function (event) {
    if (!cryptoKey) { pending.push(event.detail); return; }
    enqueue(event.detail);
  });

  // A check that fails unexpectedly is reported, and never stops the checks after it.
  // The published commitments: their SHA256SUMS files' extract hashes and anchored heads.
  function commitments(extractSha) {
    storageGet("commitments:" + server, null).then(function (where) {
      if (!where) { extractNote = "No published commitments given: the extract is not checked against them."; show(); return null; }
      // GitHub's listing of the folder (a test may name another listing in the same format).
      var listing = where.listing || "https://api.github.com/repos/" + where.owner + "/" + where.repo + "/contents/" + where.path + "?ref=" + encodeURIComponent(where.ref);
      return fetch(listing).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }).then(function (items) {
        var sums = items.filter(function (i) { return /^SHA256SUMS(\.v[0-9]+)?\.txt$/.test(i.name); });
        return Promise.all(sums.map(function (i) {
          return fetch(i.download_url).then(function (r) { return r.text(); }).then(function (text) { return { name: i.name, text: text }; });
        }));
      }).then(function (files) {
        var found = null;
        files.forEach(function (file) {
          file.text.split("\n").forEach(function (line) {
            var head = /^# event-log-head: ([0-9]+) ([0-9a-f]{64})$/.exec(line);
            if (head) {
              if (anchors[head[1]] && anchors[head[1]] !== head[2]) problem("the published commitments anchor two different logs at position " + head[1]);
              anchors[head[1]] = head[2];
            }
            var entry = /^([0-9a-f]{64})  (derived\/extract-[^ ]+\.json)$/.exec(line);
            if (entry && entry[1] === extractSha && !found) found = file.name;
            var keyLine = /^([0-9a-f]{64})  derived\/receipt-key\.json$/.exec(line);
            if (keyLine) { committedKeys = committedKeys || []; committedKeys.push(keyLine[1]); }
          });
        });
        committedKeys = committedKeys || [];
        checkKeyCommitted();
        extractNote = found ? "The extract shown is in the published commitments (" + found + ")." :
          "The extract shown is not in the published commitments (" + files.length + " checked): ask your coordinator whether it has been committed yet.";
        show();
        // A head a receipt already stated for an anchored position must agree with it.
        return storageGet("heads:" + server, {}).then(function (heads) {
          Object.keys(anchors).forEach(function (seq) {
            if (heads[seq] && heads[seq] !== anchors[seq]) problem("the server's log differs from the published anchor at position " + seq);
          });
          soonCheckChain();
        });
      });
    }).catch(function (error) { extractNote = "The published commitments could not be read (" + error + ")."; show(); });
  }

  // The receipt key, against the keys the commitments list: the SHA-256 of its canonical JSON (algorithm, public key,
  // key ID), as commit-evidence stores it. Needs both the key and the commitments; runs when the second arrives.
  function checkKeyCommitted() {
    if (!key || committedKeys === null) return;
    if (!committedKeys.length) { keyNote = "The published commitments do not list a receipt key yet."; show(); return; }
    var text = window.CorlaSigning.canonical({ algorithm: key.algorithm, public_key: key.public_key, key_id: key.key_id });
    window.CorlaSigning.sha256Hex(text).then(function (hash) {
      if (committedKeys.indexOf(hash) >= 0) { keyNote = "The receipt key is the one in the published commitments."; show(); }
      else problem("the server's receipt key is not the one in the published commitments");
    });
  }

  // The log's chain through every known point, a few seconds after the last change (receipts come in bursts).
  function soonCheckChain() {
    clearTimeout(chainTimer);
    chainTimer = setTimeout(function () { queue = queue.then(checkReport).then(checkChain).catch(function (error) { chainNote = "The log could not be checked (" + error + ")."; show(); }); }, 3000);
  }

  // The stretch of the log already checked (kept per server: {lo, hi, points: {seq: hash}}), so a page load
  // fetches only the lines around points not yet checked, not the whole log again.
  function checkChain() {
    return Promise.all([storageGet("heads:" + server, {}), storageGet("chain:" + server, null)]).then(function (got) {
      var heads = got[0], done = got[1];
      var points = {};
      Object.keys(heads).forEach(function (seq) { points[seq] = heads[seq]; });
      Object.keys(anchors).forEach(function (seq) { points[seq] = anchors[seq]; });
      var seqs = Object.keys(points).map(Number).sort(function (a, b) { return a - b; });
      if (seqs.length < 2) return null;
      var verified = done && done.points ? done.points : {};
      for (var k in verified) {
        if (points[k] !== undefined && points[k] !== verified[k]) {
          problem("the server's log is not one chain through your receipts and the published anchors: position " + k +
            " is not the one a receipt or a published anchor states");
          return null;
        }
      }
      var known = Object.keys(verified).map(Number).sort(function (a, b) { return a - b; });
      // The stretches to fetch: each new point to its nearest checked neighbours (or, with none checked yet, the whole span).
      var spans = [];
      seqs.forEach(function (seq) {
        if (verified[String(seq)] !== undefined) return;
        if (!known.length) { spans.push([seqs[0], seqs[seqs.length - 1]]); return; }
        var below = known.filter(function (k) { return k < seq; }), above = known.filter(function (k) { return k > seq; });
        spans.push([below.length ? below[below.length - 1] : seq, above.length ? above[0] : seq]);
      });
      spans.sort(function (a, b) { return a[0] - b[0]; });
      var merged = [];
      spans.forEach(function (span) {
        var last = merged[merged.length - 1];
        if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
        else merged.push([span[0], span[1]]);
      });
      var token = document.body.getAttribute("data-token");
      var base = server + "/observe/" + encodeURIComponent(token) + "/log";
      var fetched = 0;
      function segment(lo, hi) {
        var previous = null, at = lo;
        function page() {
          if (at > hi) return Promise.resolve();
          var to = Math.min(hi, at + LOG_LINES - 1);
          return fetch(base + "?from=" + at + "&to=" + to).then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.arrayBuffer();
          }).then(function (buffer) {
            var bytes = new Uint8Array(buffer), lines = [], start = 0;
            for (var i = 0; i < bytes.length; i++) if (bytes[i] === 10) { lines.push(bytes.subarray(start, i)); start = i + 1; }
            return lines.reduce(function (chain, line, index) {
              return chain.then(function () { return lineHash(line); }).then(function (computed) {
                var event = JSON.parse(new TextDecoder().decode(line));
                var seq = at + index;
                if (event.seq !== seq) throw new Error("position " + seq + " is missing");
                if (event.hash !== computed) throw new Error("position " + seq + "'s hash does not match its content");
                if (previous !== null && event.prev_hash !== previous) throw new Error("the chain breaks at position " + seq);
                previous = computed;
                var want = points[String(seq)] !== undefined ? points[String(seq)] : verified[String(seq)];
                if (want !== undefined && want !== computed) throw new Error("position " + seq + " is not the one a receipt or a published anchor states");
              });
            }, Promise.resolve()).then(function () {
              if (lines.length !== to - at + 1) throw new Error("the server gave " + lines.length + " lines for " + (to - at + 1) + " positions");
              fetched += lines.length;
              at = to + 1;
              return page();
            });
          });
        }
        return page();
      }
      return merged.reduce(function (chain, span) { return chain.then(function () { return segment(span[0], span[1]); }); }, Promise.resolve()).then(function () {
        seqs.forEach(function (seq) { verified[String(seq)] = points[String(seq)]; });
        var all = Object.keys(verified).map(Number);
        var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
        chainNote = "The log from position " + lo + " to " + hi + " is one chain, through all " + all.length +
          " points your receipts and the published anchors state" + (merged.length ? "" : " (checked before)") + ".";
        show();
        return storageSet("chain:" + server, { lo: lo, hi: hi, points: verified });
      }, function (error) {
        problem("the server's log is not one chain through your receipts and the published anchors: " + error.message);
      });
    });
  }

  // The device's records (the page's IndexedDB store, in this extension's origin), in the order signed.
  function deviceRecords() {
    return new Promise(function (resolve, reject) {
      var open = indexedDB.open("corla-observer", 1);
      open.onerror = function () { reject(new Error("the device's records could not be read")); };
      open.onsuccess = function () {
        var request = open.result.transaction("records").objectStore("records").index("link").getAll(who.records);
        request.onsuccess = function () { resolve(request.result.sort(function (a, b) { return a.device_seq - b.device_seq; })); };
        request.onerror = function () { reject(new Error("the device's records could not be read")); };
      };
    });
  }

  // The device's report, against the one the server signs for it. Only once every entry is acknowledged:
  // an entry still waiting to be sent would make them differ for no fault of the server's.
  function checkReport() {
    if (!who || !cryptoKey) return null;
    return deviceRecords().then(function (records) {
      var signed = records.filter(function (r) { return r.payload && r.payload.type !== "device_registration"; });
      if (!signed.length) return null;
      if (signed.some(function (r) { return !r.receipt && !r.refused; })) { reportNote = "Your report is compared once every entry is sent."; show(); return null; }
      var device = signed[0].payload.device_id;
      var mine = window.CorlaReport.build(signed.filter(function (r) { return r.receipt; }).map(function (r) { return r.payload; }), who);
      var url = server + "/observe/" + encodeURIComponent(document.body.getAttribute("data-token")) + "/report.json?device=" +
        encodeURIComponent(device) + "&extract=" + encodeURIComponent(who.extract_sha256);
      return fetch(url).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }).then(function (got) {
        var body = got.report;
        return crypto.subtle.verify({ name: "Ed25519" }, cryptoKey, bytes(got.signature), new TextEncoder().encode(window.CorlaSigning.canonical(body))).then(function (good) {
          if (!good || body.key_id !== key.key_id || body.device_id !== device) { problem("the server's report of your entries is not signed by its receipt key"); return; }
          var theirs = {};
          Object.keys(body).forEach(function (k) { if (["device_id", "head_seq", "head_hash", "key_id"].indexOf(k) < 0) theirs[k] = body[k]; });
          if (window.CorlaSigning.canonical(theirs) !== window.CorlaSigning.canonical(mine)) {
            problem("the server's signed report of your entries differs from your device's own (" + mine.summary.ballots + " ballots here, " + theirs.summary.ballots + " there)");
            return;
          }
          reportNote = "The server's signed report of your entries matches your device's: " + mine.summary.ballots + " ballots, " +
            mine.summary.discrepancies + " with discrepancies.";
          show();
          // The head it was computed at is one more point the log must pass through.
          return storageGet("heads:" + server, {}).then(function (heads) {
            var known = heads[String(body.head_seq)];
            if (known && known !== body.head_hash) { problem("the server's report states another log than its receipts at position " + body.head_seq); return; }
            heads[String(body.head_seq)] = body.head_hash;
            return storageSet("heads:" + server, heads);
          });
        });
      });
    });
  }

  // An event's hash from its JSONL line's own bytes: drop the last 75 (,"hash":"…"}), add "}", SHA-256.
  function lineHash(line) {
    var input = new Uint8Array(line.length - 74);
    input.set(line.subarray(0, line.length - 75));
    input[input.length - 1] = 125;
    return crypto.subtle.digest("SHA-256", input).then(function (digest) {
      return Array.from(new Uint8Array(digest)).map(function (b) { return b.toString(16).padStart(2, "0"); }).join("");
    });
  }

  function enqueue(detail) {
    queue = queue.then(function () { return check(detail); }).catch(function (error) { problem("a receipt could not be checked (" + error + ")"); });
  }

  function check(detail) {
    var receipt = detail && detail.receipt, body = receipt && receipt.receipt;
    if (!body || typeof receipt.signature !== "string") { problem("a receipt without a body or signature"); return; }
    if (body.key_id !== key.key_id) { problem("receipt " + body.event_seq + " names another key"); return; }
    if (body.payload_sha256 !== detail.payload_sha256) { problem("receipt " + body.event_seq + " is for another payload than this device signed"); return; }
    var text = new TextEncoder().encode(window.CorlaSigning.canonical(body));
    return crypto.subtle.verify({ name: "Ed25519" }, cryptoKey, bytes(receipt.signature), text).then(function (good) {
      if (!good) { problem("receipt " + body.event_seq + " is not signed by the server's receipt key"); return; }
      return storageGet("heads:" + server, {}).then(function (heads) {
        var clash = [[body.event_seq, body.event_hash], [body.head_seq, body.head_hash]].filter(function (pair) {
          var known = heads[String(pair[0])] || anchors[String(pair[0])];
          if (known && known !== pair[1]) return true;
          heads[String(pair[0])] = pair[1];
          return false;
        });
        if (clash.length) { problem("the server has shown two different logs: position " + clash[0][0] + " has two hashes"); return; }
        verified += 1;
        show();
        soonCheckChain();
        return storageSet("heads:" + server, heads);
      });
    }).catch(function () { problem("receipt " + body.event_seq + " could not be checked"); });
  }
})();
