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
// fake a chain through points it has already committed to. The result is shown at the top of the page; a
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
        (key ? " (server receipt key " + key.key_id.slice(0, 16) + "…)" : "") + "." + (keyNote ? " " + keyNote : "") + (extractNote ? " " + extractNote : "") + (chainNote ? " " + chainNote : "");
    }
  }
  function problem(text) { if (problems.indexOf(text) < 0) problems.push(text); show(); }

  document.addEventListener("corla-observer:meta", function (event) {
    var detail = event.detail || {};
    var offered = detail.receipt_key;
    server = detail.server || "";
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
    chainTimer = setTimeout(function () { queue = queue.then(checkChain).catch(function (error) { chainNote = "The log could not be checked (" + error + ")."; show(); }); }, 3000);
  }

  function checkChain() {
    return storageGet("heads:" + server, {}).then(function (heads) {
      var points = {};
      Object.keys(heads).forEach(function (seq) { points[seq] = heads[seq]; });
      Object.keys(anchors).forEach(function (seq) { points[seq] = anchors[seq]; });
      var seqs = Object.keys(points).map(Number).sort(function (a, b) { return a - b; });
      if (seqs.length < 2) return null;
      var lo = seqs[0], hi = seqs[seqs.length - 1];
      var token = document.body.getAttribute("data-token");
      var base = server + "/observe/" + encodeURIComponent(token) + "/log";
      var previous = null, checkedPoints = 0, at = lo;
      function page() {
        if (at > hi) return Promise.resolve();
        var to = Math.min(hi, at + LOG_LINES - 1);
        return fetch(base + "?from=" + at + "&to=" + to).then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.arrayBuffer();
        }).then(function (buffer) {
          var bytes = new Uint8Array(buffer), lines = [], start = 0;
          for (var i = 0; i < bytes.length; i++) if (bytes[i] === 10) { lines.push(bytes.subarray(start, i)); start = i + 1; }
          return lines.reduce(function (done, line, index) {
            return done.then(function () { return lineHash(line); }).then(function (computed) {
              var event = JSON.parse(new TextDecoder().decode(line));
              var seq = at + index;
              if (event.seq !== seq) throw new Error("position " + seq + " is missing");
              if (event.hash !== computed) throw new Error("position " + seq + "'s hash does not match its content");
              if (previous !== null && event.prev_hash !== previous) throw new Error("the chain breaks at position " + seq);
              previous = computed;
              if (points[String(seq)] !== undefined) {
                if (points[String(seq)] !== computed) throw new Error("position " + seq + " is not the one a receipt or a published anchor states");
                checkedPoints += 1;
              }
            });
          }, Promise.resolve()).then(function () {
            if (lines.length !== to - at + 1) throw new Error("the server gave " + lines.length + " lines for " + (to - at + 1) + " positions");
            at = to + 1;
            return page();
          });
        });
      }
      return page().then(function () {
        chainNote = "The log from position " + lo + " to " + hi + " is one chain, through all " + checkedPoints + " points your receipts and the published anchors state.";
        show();
      }, function (error) {
        problem("the server's log is not one chain through your receipts and the published anchors: " + error.message);
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
