// corla-observer: the observer entry page.
//
// One ballot at a time: two taps for a match, or a note of what was heard
// instead. The first tap ("First reading", on hearing the first audit board
// member read the ballot) is recorded as a signed first_reading entry and
// turns the button into "Confirmed match"; the second tap, after the second
// reading, records the match and moves on. A note can be typed at any
// point, before or after the first tap, and records a non-match. The
// first time a link is opened on a device, the page makes a signing key for
// it (signing.js: ECDSA P-256 with Web Crypto, the private key
// non-extractable, kept in IndexedDB) and registers its public key with the
// server. Every entry is then signed on the device, with the device's next
// sequence number and the hash of its previous entry, kept in IndexedDB at
// once, and queued and sent; the server answers each with a signed receipt,
// kept with the entry, and the page shows each ballot's short receipt code.
// While the network is down the queue grows and is sent when it comes back.
// "Download my record" saves every entry, signature and receipt as JSON
// (for `corla-observer check-record`) and as CSV. Edits of earlier ballots
// are new entries. An imprinted ID typed in the box (matched by idmatch.js)
// jumps to that ballot when the board reads one out of order, leaving the
// skipped ones marked not read yet. No sound, no vibration.
//
// The observer can choose instead to enter each ballot contest by contest
// (percontest.js, with its logic in contestlogic.js): the choice is kept on
// the device and recorded as a signed entry_mode entry, and can be changed
// at any time. Its many small entries are signed and kept at once but sent
// in batches: every few seconds, every 20 entries, and on leaving a ballot.
//
// Before the first ballot, the first time the link is opened on a device and
// again at the start of each browser session and each Colorado day, a short
// notice states the keep-out-of-sight rule and the essentials; its "I
// understand, start" is recorded as a signed acknowledged entry. Everything
// else (the entry mode, the record's downloads, the printouts, the check
// page, Dim, the long help) is behind "More". Each ballot's header carries
// what a screenshot or photo of the screen needs to be checked: the
// imprinted ID, "Ballot n of N", the county, board and extract's short hash,
// and a QR code locating the ballot (docs/PHOTO_CHECK.md).
//
// A county may have targeted lists besides its RLA sample (ballots observers
// chose; corla_observer.pulllists). The header's list switches between them:
// the page reloads on the list chosen (?list=KEY), and the first time this
// device shows a list other than the one it showed last, it records a signed
// list_switch entry. Each list keeps its own entries, place and ballots on
// the device; the signing key and the record are the link's, for every list.
// Until the board has an RLA sample, the server opens the link on its first
// targeted list and says so (data.json's "notice"); if the page and the data
// disagree about the list (a page kept from before, say), the page reloads on
// the data's list, and an answer without ballots fills the switcher from the
// lists it names and never leaves the status at "Loading".
"use strict";

(function () {
  var body = document.body;
  var prefix = body.getAttribute("data-prefix") || "";
  var token = body.getAttribute("data-token");
  var base = prefix + "/observe/" + encodeURIComponent(token);
  var storeKey = "corla-observer:" + token;
  // The pull list shown: "rla" (the RLA sample) or a targeted list's key. Its entries and place are kept under listStore.
  var listKey = body.getAttribute("data-list") || "rla";
  var listStore = listKey === "rla" ? storeKey : storeKey + ":" + listKey;
  var listQuery = listKey === "rla" ? "" : "?list=" + encodeURIComponent(listKey);
  // "Look up a ballot": ballots picked from storage boxes, looked up by imprinted ID and entered contest by contest, each a
  // "lookup_contest"/"lookup_done" payload, apart from every pull list (docs/TARGETING.md). The list "lookup" has no extract:
  // its extract_sha256 is the redacted CVR's hash, and its ballots are this device's lookups, numbered from 1.
  var lookupMode = listKey === "lookup";
  var Signing = window.CorlaSigning;

  // -- small storage (any failure, as in a private window, falls back to memory) --

  function load(key, fallback) {
    try {
      var text = window.localStorage.getItem(key);
      return text ? JSON.parse(text) : fallback;
    } catch (e) { return fallback; }
  }
  function save(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* memory only */ }
  }

  // -- the device-held record, in IndexedDB --
  //
  // "devices": one per link (keyed by storeKey): the key pair, device ID,
  // sequence number and chain hash. "records": every signed item (keyed by
  // link and device sequence number) with its receipt, or why it was refused.

  var db = (function () {
    var opened = null;
    function open() {
      if (!opened) {
        opened = new Promise(function (resolve, reject) {
          if (!window.indexedDB) { reject(new Error("no IndexedDB")); return; }
          var request = window.indexedDB.open("corla-observer", 1);
          request.onupgradeneeded = function () {
            var d = request.result;
            d.createObjectStore("devices", { keyPath: "id" });
            d.createObjectStore("records", { keyPath: ["link", "device_seq"] }).createIndex("link", "link");
          };
          request.onsuccess = function () { resolve(request.result); };
          request.onerror = function () { reject(request.error); };
        });
      }
      return opened;
    }
    function run(names, mode, work) {
      return open().then(function (d) {
        return new Promise(function (resolve, reject) {
          var tx = d.transaction(names, mode);
          var result = null;
          tx.oncomplete = function () { resolve(result); };
          tx.onerror = tx.onabort = function () { reject(tx.error); };
          work(tx, function (value) { result = value; });
        });
      });
    }
    function get(store, key) {
      return run([store], "readonly", function (tx, done) {
        var request = tx.objectStore(store).get(key);
        request.onsuccess = function () { done(request.result || null); };
      });
    }
    return {
      device: function () { return get("devices", storeKey); },
      records: function () {
        return run(["records"], "readonly", function (tx, done) {
          var request = tx.objectStore("records").index("link").getAll(storeKey);
          request.onsuccess = function () { done(request.result || []); };
        });
      },
      // The device and a record in one transaction, so the chain and the
      // record never disagree.
      put: function (dev, record) {
        return run(["devices", "records"], "readwrite", function (tx) {
          if (dev) tx.objectStore("devices").put(dev);
          if (record) tx.objectStore("records").put(record);
        });
      }
    };
  })();
  var persistent = true;   // false once IndexedDB has failed: memory only

  function persist(dev, record) {
    if (!persistent) return Promise.resolve();
    return db.put(dev, record).catch(function () {
      persistent = false;
      showError("This browser is not keeping your entries on the device. Download your record before closing the page.");
    });
  }

  function now() { return new Date().toISOString(); }

  // Colorado local time with its UTC offset ("2026-10-26 22:21:05 -06:00"), for the downloads; UTC if the browser can't tell.
  function coloradoParts(date) {
    try {
      var format = new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", hourCycle: "h23", year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
      var p = {};
      format.formatToParts(date).forEach(function (part) { p[part.type] = part.value; });
      var asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
      var minutes = Math.round((asUtc - Math.floor(date.getTime() / 1000) * 1000) / 60000);
      var sign = minutes < 0 ? "-" : "+";
      var abs = Math.abs(minutes);
      var offset = sign + String(Math.floor(abs / 60)).padStart(2, "0") + ":" + String(abs % 60).padStart(2, "0");
      return { date: p.year + "-" + p.month + "-" + p.day, time: String(+p.hour % 24).padStart(2, "0") + ":" + p.minute + ":" + p.second, offset: offset };
    } catch (e) {
      var iso = date.toISOString();
      return { date: iso.slice(0, 10), time: iso.slice(11, 19), offset: "+00:00" };
    }
  }
  function coloradoTime(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    var p = coloradoParts(date);
    return p.date + " " + p.time + " " + p.offset;
  }

  // local: { entries: {position: entry} }, what each ballot shows.
  var local = load(listStore, { entries: {} });
  delete local.queue;
  var meta = load(listStore + ":meta", null);   // {observer, county, board, extract_sha256, receipt_key}
  var device = null;       // this link's signing device
  var records = [];        // every signed item, in sequence order
  var signing = Promise.resolve();

  var ballots = [];
  var current = null;      // the ballot shown
  // "next": the first ballot not yet done; "edit": changing an entry;
  // "jump": a ballot the board read out of order; "chosen": a ballot not
  // read yet, tapped to enter it now.
  var mode = "next";
  var jumpFrom = null;     // the ballot expected when the board read another
  var shownAt = 0;
  var shownFor = null;     // the ballot `shownAt` is for
  var armed = null;        // the ballot whose first reading has been tapped
  var sending = false;
  var lastSendOk = true;
  var lastReceipt = "";
  var retryDelay = 2000;
  var retryTimer = null;
  var offeredDownload = false;

  var el = function (id) { return document.getElementById(id); };

  function pending() { return records.filter(function (r) { return !r.receipt && !r.refused; }); }
  // Whether a signed item is for the list shown: for its extract, or, on the RLA sample, for an extract no other list has had.
  function inList(r) {
    var sha = r.payload.extract_sha256;
    if (!meta || !sha) return listKey === "rla";
    if ((meta.list_shas || [meta.extract_sha256]).indexOf(sha) >= 0) return true;
    return listKey === "rla" && (meta.other_shas || []).indexOf(sha) < 0;
  }
  function recordBySeq(seq) {
    for (var i = 0; i < records.length; i++) if (records[i].device_seq === seq) return records[i];
    return null;
  }
  function code(receipt) {
    var r = receipt && receipt.receipt;
    return r ? r.event_seq + "-" + String(r.event_hash).slice(0, 6) : "";
  }

  // -- status --

  // What is waiting to be sent, in ballots and entries: "2 ballots (4 entries)", since a match is two signed taps.
  var BALLOT_TYPES = { entry: 1, first_reading: 1, other_board: 1, heard_id: 1, contest: 1, ballot_done: 1, lookup_contest: 1, lookup_done: 1 };
  var CONTEST_TYPES = { contest: 1, ballot_done: 1, lookup_contest: 1, lookup_done: 1 };
  function waiting() {
    var items = pending().filter(function (r) { return r.payload.type !== "device_registration"; });
    var positions = {};
    items.forEach(function (r) { if (BALLOT_TYPES[r.payload.type]) positions[r.payload.extract_sha256 + ":" + r.payload.position] = true; });
    var n = Object.keys(positions).length;
    var entries = items.length + " entr" + (items.length === 1 ? "y" : "ies");
    var text = n ? n + " ballot" + (n === 1 ? "" : "s") + " (" + entries + ")" : entries;
    return { count: items.length, text: text };
  }

  // Keep `node` at least as wide as the widest of `texts` (each its text, or a function that fills it), so that the header and the bars around it
  // don't change width or wrap as a count or a label changes. Measured once per set of texts, while it is shown.
  function reserveWidth(node, texts) {
    var key = texts.map(String).join("\n");
    if (node.getAttribute("data-reserved") === key) return;
    var saved = Array.prototype.slice.call(node.childNodes);
    node.style.minWidth = "";
    var widest = 0;
    texts.forEach(function (t) {
      node.textContent = "";
      if (typeof t === "function") t(node); else node.textContent = t;
      widest = Math.max(widest, node.getBoundingClientRect().width);
    });
    node.textContent = "";
    saved.forEach(function (c) { node.appendChild(c); });
    if (widest > 0) {
      node.style.minWidth = Math.ceil(widest) + "px";
      node.setAttribute("data-reserved", key);
    }
  }

  // The status in the bar: a short label, the same width in every state (the full description is its title).
  var STATUS_WIDEST = ["● Reconnecting", "● Sending 999", "○ Offline 999", "◐ Queued 999", "○ Not loaded"];
  var loadFailed = false;  // the ballots could not be loaded (and none were kept): the status says so, not "Loading"
  function setStatus() {
    var status = el("obs-status");
    var wait = waiting();
    var queued = wait.count;
    var state, icon, text, title;
    if (loadFailed) { state = "offline"; icon = "○"; text = "Not loaded"; title = "The ballots could not be loaded"; }
    else if (!navigator.onLine) {
      state = "offline"; icon = "○"; text = queued ? "Offline " + queued : "Offline";
      title = queued ? "Offline: " + wait.text + " waiting" : "Offline";
    }
    else if (queued && lastSendOk && pending().every(function (r) { return Date.now() - (r.made || 0) < 15000; })) {
      state = "live"; icon = "●"; text = "Sending " + queued; title = "Live, sending " + wait.text;
    }
    else if (queued) { state = "queued"; icon = "◐"; text = "Queued " + queued; title = "Waiting to send: " + wait.text; }
    else if (!lastSendOk) { state = "queued"; icon = "◐"; text = "Reconnecting"; title = text; }
    else { state = "live"; icon = "●"; text = "Live"; title = "Live: everything is sent"; }
    reserveWidth(status, STATUS_WIDEST);
    status.setAttribute("data-state", state);
    status.setAttribute("title", title);
    status.textContent = "";
    var i = document.createElement("span");
    i.setAttribute("aria-hidden", "true");
    i.textContent = icon + " ";
    status.appendChild(i);
    status.appendChild(document.createTextNode(text));
    if (perContest) perContest.refresh();
  }

  // "n of N done": as wide as "N of N done".
  function setProgress(done) {
    var node = el("obs-progress");
    reserveWidth(node, [ballots.length + " of " + ballots.length + " done"]);
    node.textContent = done + " of " + ballots.length + " done";
  }

  // -- the device --

  function startDevice() {
    return (persistent ? db.device() : Promise.resolve(null)).catch(function () {
      persistent = false;
      return null;
    }).then(function (stored) {
      if (stored && stored.keys) {
        device = stored;
        return (persistent ? db.records() : Promise.resolve([])).catch(function () { return []; }).then(function (found) {
          records = found.sort(function (a, b) { return a.device_seq - b.device_seq; });
        });
      }
      return Signing.newDevice(meta, now()).then(function (made) {
        device = made.device;
        device.id = storeKey;
        var registration = toRecord(made.registration);
        records = [registration];
        return persist(device, registration);
      });
    });
  }

  function toRecord(signed) {
    return {
      link: storeKey,
      device_seq: signed.device_seq,
      payload: signed.payload,
      signature: signed.signature,
      payload_sha256: signed.payload_sha256,
      receipt: null,
      refused: null,
      made: Date.now()
    };
  }

  // Sign `fields` as the device's next item, in order, then keep and send it
  // (with `options.batch`, send it with the next batch).
  function signAndQueue(fields, uiEntry, options) {
    var batch = Boolean(options && options.batch);
    signing = signing.then(function () {
      if (!device) throw new Error("no signing key on this device");
      return Signing.signNext(device, fields);
    }).then(function (signed) {
      var record = toRecord(signed);
      records.push(record);
      if (uiEntry) { uiEntry.device_seq = record.device_seq; save(listStore, local); }
      return persist(device, record);
    }).then(function () { if (batch) scheduleFlush(); else flush(); }, function (error) {
      showError("Could not sign the entry (" + error.message + "). Write it on paper; reload the page to try again.");
    });
  }

  // -- sending --

  var BATCH_MS = 3000;     // a batch waits at most this long
  var BATCH_SIZE = 20;     // or is sent at once when this many are waiting
  var batchTimer = null;

  function scheduleFlush() {
    if (pending().length >= BATCH_SIZE) { flushNow(); return; }
    if (!batchTimer) batchTimer = setTimeout(function () { batchTimer = null; flush(); }, BATCH_MS);
    setStatus();
  }

  function flushNow() {
    clearTimeout(batchTimer);
    batchTimer = null;
    flush();
  }

  function flush() {
    var queue = pending();
    if (sending || !queue.length) { setStatus(); return; }
    sending = true;
    var batch = queue.slice(0, 50);
    var progressed = false;
    fetch(base + "/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: batch.map(function (r) { return { payload: r.payload, signature: r.signature }; }) }),
      credentials: "same-origin",
      cache: "no-store"
    }).then(function (response) {
      if (response.status === 404) {
        showError("This link no longer works: your coordinator revoked or replaced it. Ask them for a new one. " +
          "Your entries are kept on this device: save your record now (More, Download my record).");
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (result) {
      var receipts = result.receipts || {};
      var unknownDevice = false;
      batch.forEach(function (r) {
        var receipt = receipts[String(r.device_seq)];
        if (!receipt) return;
        r.receipt = receipt;
        progressed = true;
        lastReceipt = code(receipt);
        var entry = (r.payload.type === "entry" || r.payload.type === "other_board") && inList(r) ? local.entries[String(r.payload.position)] : null;
        if (entry && entry.device_seq === r.device_seq) entry.receipt = code(receipt);
        (local.offlist || []).forEach(function (o) { if (o.device_seq === r.device_seq) o.receipt = code(receipt); });
        persist(null, r);
      });
      (result.errors || []).forEach(function (e) {
        var r = recordBySeq(e.device_seq);
        if (e.code === "unknown_device" && r && r.device_seq !== 0) { unknownDevice = true; return; }
        if (r) { r.refused = { code: e.code, error: e.error }; persist(null, r); progressed = true; }
        showError("An entry was refused: " + e.error);
      });
      if (unknownDevice) {
        // The server doesn't know this device's key (a new log, say): send
        // the registration again before the entries, unless it was refused.
        var registration = recordBySeq(0);
        if (registration && !registration.refused) { registration.receipt = null; progressed = true; }
        else showError("This device's key was refused, so its entries can't be recorded. Use paper, and tell your coordinator.");
      }
      save(listStore, local);
      lastSendOk = true;
      retryDelay = 2000;
    }).catch(function () {
      lastSendOk = false;
      clearTimeout(retryTimer);
      retryTimer = setTimeout(flush, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    }).then(function () {
      sending = false;
      render();
      if (lastSendOk && progressed && pending().length) flush();
    });
  }

  window.addEventListener("online", function () { retryDelay = 2000; flush(); });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flushNow(); });
  window.addEventListener("offline", setStatus);

  function showError(text) {
    var box = el("obs-error");
    box.textContent = text;
    box.hidden = false;
  }

  // Nothing to show: the error, the status (not "Loading"), and the board's lists in the switcher, if the answer names any.
  function showNothing(text, lists) {
    showError(text);
    loadFailed = true;
    setStatus();
    if (lists && lists.length) fillSwitcher(lists);
  }

  // The header's list switcher, from the lists the server names (made if the page has none).
  function fillSwitcher(lists) {
    var select = el("obs-list");
    if (!select) {
      var label = document.createElement("label");
      label.className = "obs-list";
      var hidden = document.createElement("span");
      hidden.className = "visually-hidden";
      hidden.textContent = "Pull list";
      select = document.createElement("select");
      select.id = "obs-list";
      select.setAttribute("aria-label", "Pull list: the RLA sample or a targeted list");
      label.appendChild(hidden);
      label.appendChild(select);
      var modes = el("obs-modes");
      modes.parentNode.insertBefore(label, modes);
      select.addEventListener("change", switchList);
    }
    select.textContent = "";
    lists.forEach(function (x) {
      var option = document.createElement("option");
      option.value = x.key;
      option.textContent = x.label + " (" + x.ballots + ")";
      option.selected = x.key === listKey;
      select.appendChild(option);
    });
    setBarHeight();
  }

  function switchList() {
    var select = el("obs-list");
    flushNow();
    window.location.href = base + (select.value === "rla" ? "" : "?list=" + encodeURIComponent(select.value));
  }

  function message(text) {
    var box = el("obs-message");
    box.textContent = text || "";
    box.hidden = !text;
  }

  // -- the downloadable record --

  function recordFile() {
    return {
      format: "corla-observer-record",
      version: 1,
      observer: meta && meta.observer,
      county: meta && meta.county,
      board: meta && String(meta.board),
      device_id: device && device.device_id,
      public_key: device && device.public_key,
      algorithm: Signing.ALGORITHM,
      extract_sha256: meta && meta.extract_sha256,
      list: meta && meta.list_label,
      server_receipt_key: meta && meta.receipt_key,
      page: {
        software: body.getAttribute("data-version"),
        observe_js: body.getAttribute("data-observe-integrity"),
        signing_js: body.getAttribute("data-signing-integrity"),
        percontest_js: body.getAttribute("data-percontest-integrity"),
        contestlogic_js: body.getAttribute("data-contestlogic-integrity"),
        entry_mode: entryMode
      },
      exported_at: coloradoTime(now()),
      time_zone: "America/Denver (Colorado local time, with its offset from UTC; the signed payloads' client_time is UTC, as signed)",
      entries: records.map(function (r) {
        return { payload: r.payload, signature: r.signature, payload_sha256: r.payload_sha256, receipt: r.receipt, refused: r.refused };
      })
    };
  }

  function cell(value) {
    var text = value === null || value === undefined ? "" : String(value);
    if (/^[=+\-@\t\r\n]/.test(text)) text = "'" + text;
    return /[",\r\n]/.test(text) ? "\"" + text.replace(/"/g, "\"\"") + "\"" : text;
  }

  function recordCsv() {
    var rows = [["device_seq", "type", "position", "imprinted_id", "match", "note_or_heard", "edit", "client_time (Colorado)",
      "payload_sha256", "receipt", "event_seq", "server_time (Colorado)", "status", "contest", "reading", "outcome"]];
    records.forEach(function (r) {
      var p = r.payload, c = p.content || {}, receipt = r.receipt && r.receipt.receipt;
      rows.push([
        r.device_seq, p.type, p.position, p.imprinted_id,
        p.type === "entry" ? (c.match ? "yes" : "no") : "",
        p.type === "entry" ? c.note : p.type === "first_reading" ? "first reading" : p.type === "other_board" ? "skipped: other board" :
          p.type === "ballot_done" ? "ballot done" : p.type === "entry_mode" ? "entry mode: " + c.mode :
          p.type === "acknowledged" ? "acknowledged: " + c.notice : p.type === "device_registration" ? "device registered" : c.heard,
        p.type === "entry" ? (c.edit ? "yes" : "no") : "",
        coloradoTime(p.client_time), r.payload_sha256, code(r.receipt),
        receipt ? receipt.event_seq : "", receipt ? coloradoTime(receipt.server_time) : "",
        r.refused ? "refused: " + r.refused.error : receipt ? "received" : "queued",
        p.type === "contest" ? c.row + ". " + c.contest : "", p.type === "contest" || p.type === "ballot_done" ? c.reading : "",
        p.type === "contest" ? c.outcome : ""
      ]);
    });
    return rows.map(function (row) { return row.map(cell).join(","); }).join("\r\n") + "\r\n";
  }

  function download(text, type, extension) {
    var t = coloradoParts(new Date());
    var stamp = t.date + "T" + t.time.slice(0, 5).replace(":", "") + t.offset.replace(":", "");
    var name = "corla-observer-record-" + (meta ? (meta.county + "-board" + meta.board + "-" + meta.observer.split(":")[1]) : "observer") +
      "-" + stamp + "." + extension;
    var url = URL.createObjectURL(new Blob([text], { type: type }));
    var a = document.createElement("a");
    a.href = url;
    a.download = name.replace(/[^A-Za-z0-9._+-]+/g, "_");
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }

  function downloadJson() { download(JSON.stringify(recordFile(), null, 1) + "\n", "application/json", "json"); }
  function downloadCsv() { download(recordCsv(), "text/csv", "csv"); }

  // -- entries --

  function entryFor(ballot) { return local.entries[String(ballot.position)]; }

  function stampFields(type, ballot, content) {
    content.elapsed_ms = Math.min(86400000, Math.max(0, Math.round(performance.now() - shownAt)));
    return {
      type: type,
      extract_sha256: meta.extract_sha256,
      position: ballot.position,
      imprinted_id: ballot.id,
      content: content,
      client_time: now()
    };
  }

  // The match button: the first tap marks the first reading, the second
  // (on the same ballot) records the confirmed match.
  function tapMatch() {
    if (!current) return;
    if (armed !== current) {
      signAndQueue(stampFields("first_reading", current, {}), null);
      armed = current;
      local.armed = { position: current.position };
      save(listStore, local);
      showMatchButton();
      message("First reading heard. Tap “Confirmed match” after the second reading, or type what was heard instead.");
      return;
    }
    record(true, "");
  }

  function showMatchButton() {
    var button = el("obs-match");
    var second = Boolean(current) && armed === current;
    button.classList.toggle("is-armed", second);
    button.textContent = "";
    var icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.className = "obs-match-icon";
    icon.textContent = second ? "✓✓" : "1";
    var key = document.createElement("kbd");
    key.textContent = "Enter";
    button.appendChild(icon);
    button.appendChild(document.createTextNode(second ? " Confirmed match " : " First reading "));
    button.appendChild(key);
    button.setAttribute("aria-label", second ? "Confirmed match: record that both readings match" : "First reading heard");
  }

  function record(match, note) {
    if (!current) return;
    armed = null;
    delete local.armed;
    note = Signing.cleanText(note, 500);
    if (!match && !note) return;
    var before = entryFor(current);
    var edit = Boolean(before);
    var fields = stampFields("entry", current, { match: match, note: note, edit: edit });
    var entry = { position: current.position, imprinted_id: current.id, match: match, note: note, edit: edit };
    if (before) {
      entry.corrected = (before.corrected || 0) + 1;
      entry.previous = { match: before.match, note: before.note, other_board: before.other_board, receipt: before.receipt || "" };
    }
    local.entries[String(current.position)] = entry;
    save(listStore, local);
    signAndQueue(fields, entry);
    mode = "next";
    jumpFrom = null;
    el("obs-note").value = "";
    message("");
    render();
  }

  function ballotById(id) {
    for (var i = 0; i < ballots.length; i++) if (ballots[i].id === id) return ballots[i];
    return null;
  }

  // The board read an imprinted ID other than the one shown. Returns true
  // if the text was dealt with (and the box can be cleared).
  function heardId(text, fromOtherForm) {
    if (!current) return false;
    var result = window.CorlaIdMatch.match(text, ballots.map(function (b) { return b.id; }));
    if (result.status === "not_id") {
      if (fromOtherForm) { message("Type the imprinted ID the board read, such as 102-46-5 or its end, 46-5."); return false; }
      record(false, text);
      return true;
    }
    if (result.status === "ambiguous") {
      message("“" + text + "” fits several ballots on this board (" + result.candidates.slice(0, 6).join(", ") +
        (result.candidates.length > 6 ? ", …" : "") + "). Type more of the ID.");
      return false;
    }
    if (result.status === "found" && result.imprinted_id === current.id) {
      message("That is the ballot shown.");
      return true;
    }
    signAndQueue(stampFields("heard_id", current, { heard: Signing.cleanText(text, 64) }), result.status === "found" ? null : noteOffList(text, current));
    if (result.status === "found") {
      jumpFrom = current;
      current = ballotById(result.imprinted_id);
      mode = "jump";
      render();
      message("The board read " + current.id + " out of order. " + jumpFrom.id +
        " and any ballots skipped stay marked not read yet.");
    } else {
      message("“" + text + "” is not on this board's list. Your note is recorded, and listed above; the page stays on " + current.id + ".");
      render();
    }
    return true;
  }

  // An ID heard that is not on this board's list: kept in the observer's own list, after the ballot expected then.
  // Its device_seq is set once it is signed (signAndQueue).
  function noteOffList(text, ballot) {
    local.offlist = local.offlist || [];
    var item = { heard: Signing.cleanText(text, 64), at: ballot.position, device_seq: null, receipt: "" };
    local.offlist.push(item);
    save(listStore, local);
    return item;
  }

  function offListItems(position) {
    return (local.offlist || []).filter(function (o) { return o.at === position; });
  }

  function offListItem(o) {
    var li = document.createElement("li");
    var div = document.createElement("div");
    div.className = "obs-past is-offlist";
    var n = document.createElement("span");
    n.className = "obs-past-n";
    n.textContent = "≠";
    n.setAttribute("aria-hidden", "true");
    var id = document.createElement("span");
    id.className = "obs-past-id";
    id.textContent = o.heard;
    var what = document.createElement("span");
    what.className = "obs-past-what";
    what.textContent = "not on this board's list: noted";
    var receipt = document.createElement("span");
    receipt.className = "obs-past-receipt";
    receipt.textContent = o.receipt || (o.seq ? "" : "queued");
    [n, id, what, receipt].forEach(function (x) { div.appendChild(x); });
    li.appendChild(div);
    return li;
  }

  // -- rendering --

  function voteList(target, ballot) {
    target.textContent = "";
    if (ballot.redacted || ballot.missing) {
      var li = document.createElement("li");
      li.className = "obs-redacted";
      li.textContent = ballot.redacted ? "Redacted: this ballot's votes are not in the redacted CVR" : "Not in the CVR";
      target.appendChild(li);
      return;
    }
    ballot.votes.forEach(function (vote) {
      // As on the printout: the contest's name right-aligned, a box in the middle, the choices left-aligned, one per line.
      var item = document.createElement("li");
      var contest = document.createElement("span");
      contest.className = "obs-contest";
      contest.textContent = vote[0];
      var box = document.createElement("span");
      box.className = "obs-box";
      box.setAttribute("aria-hidden", "true");
      var choice = document.createElement("span");
      choice.className = "obs-choice" + (vote[1] === "no vote" ? " obs-novote" : "");
      var lines = vote[3] && vote[3].length ? vote[3] : [vote[1]];
      lines.forEach(function (text) {
        var line = document.createElement("span");
        line.className = "obs-choice-line";
        line.textContent = text;
        choice.appendChild(line);
      });
      item.appendChild(contest);
      item.appendChild(box);
      item.appendChild(choice);
      item.setAttribute("aria-label", vote[0] + ": " + (vote[1] === "no vote" ? "no vote" : lines.join(", ")));
      target.appendChild(item);
    });
  }

  function listItem(ballot, entry) {
    var li = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    var state = !entry ? " is-pending" : entry.other_board ? " is-other" : entry.match && !entry.note ? " is-match" : " is-note";
    button.className = "obs-past" + state;
    var n = document.createElement("span");
    n.className = "obs-past-n";
    n.textContent = ballot.n;
    var id = document.createElement("span");
    id.className = "obs-past-id";
    id.textContent = ballot.id;
    var what = document.createElement("span");
    what.className = "obs-past-what";
    what.textContent = (!entry ? "○ not read yet" : entry.other_board ? "↷ skipped: other board" : entry.note ? "▲ " + entry.note :
      entry.per_contest ? "✓ match, contest by contest" : "✓ match") + (entry && entry.corrected ? " · corrected (tap to see the earlier entry)" : "");
    var receipt = document.createElement("span");
    receipt.className = "obs-past-receipt";
    receipt.textContent = !entry ? "" : entry.receipt ? entry.receipt : entry.device_seq ? "queued" : "";
    button.appendChild(n);
    button.appendChild(id);
    button.appendChild(what);
    button.appendChild(receipt);
    button.setAttribute("aria-label", "Ballot " + ballot.n + ", " + ballot.id + ": " +
      (!entry ? "not read yet. Tap to enter it now." : (entry.other_board ? "skipped: other board" : entry.note || "match") +
        (entry.receipt ? ", receipt " + entry.receipt : "") + ". Tap to change."));
    button.addEventListener("click", function () {
      current = ballot;
      mode = entry ? "edit" : "chosen";
      jumpFrom = null;
      message("");
      render();
      el("obs-note").value = entry && entry.note ? entry.note : "";
    });
    li.appendChild(button);
    return li;
  }

  function entryText(e) {
    return e.other_board ? "skipped: other board" : e.note ? "“" + e.note + "”" : "match";
  }

  // A QR code from the server's rows of hex digits (extract.qr_rows), as an SVG with a quiet zone.
  var SVG = "http://www.w3.org/2000/svg";
  function qrSvg(rows) {
    var n = rows.length, q = 2, size = n + 2 * q, d = "";
    rows.forEach(function (hex, y) {
      var bits = hex.split("").map(function (h) { return ("000" + parseInt(h, 16).toString(2)).slice(-4); }).join("").slice(0, n);
      var x = 0;
      while (x < n) {
        if (bits[x] === "1") {
          var start = x;
          while (x < n && bits[x] === "1") x++;
          d += "M" + (start + q) + " " + (y + q) + "h" + (x - start) + "v1h-" + (x - start) + "z";
        } else x++;
      }
    });
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 " + size + " " + size);
    svg.setAttribute("shape-rendering", "crispEdges");
    var bg = document.createElementNS(SVG, "rect");
    bg.setAttribute("width", size);
    bg.setAttribute("height", size);
    bg.setAttribute("fill", "#fff");
    var path = document.createElementNS(SVG, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "#000");
    svg.appendChild(bg);
    svg.appendChild(path);
    return svg;
  }

  // A ballot's header, in either mode: its imprinted ID, "Ballot n of N", the extract's identification and its QR code.
  function showBallotHead(prefixId, ballot) {
    el(prefixId + "-id").textContent = ballot.id;
    var pos = el(prefixId + "-pos");
    reserveWidth(pos, ["Ballot " + ballots.length + " of " + ballots.length]);
    pos.textContent = "Ballot " + ballot.n + " of " + ballots.length;
    // The extract's short hash in bold, larger, for a photo's OCR to read character by character.
    var ident = el(prefixId + "-ident");
    var text = (meta && meta.ident) || "";
    var hash = text.match(/^(.* extract )([0-9a-f]{8})$/);
    ident.textContent = hash ? hash[1].replace(/extract $/, "") : text;
    if (hash) {
      var part = document.createElement("span");
      part.className = "ballot-ident-hash";
      part.appendChild(document.createTextNode("extract "));
      var strong = document.createElement("b");
      strong.className = "ballot-hash";
      strong.textContent = hash[2];
      part.appendChild(strong);
      ident.appendChild(part);
    }
    if (listKey !== "rla") {
      var mark = document.createElement("strong");
      mark.className = lookupMode ? "ballot-lookup" : "ballot-targeted";
      mark.textContent = lookupMode ? "LOOKED UP (NOT RLA) " : "TARGETED (NOT RLA) ";
      ident.insertBefore(mark, ident.firstChild);
    }
    var qr = el(prefixId + "-qr");
    qr.textContent = "";
    if (ballot.qr && ballot.qr.length) qr.appendChild(qrSvg(ballot.qr));
  }

  function render() {
    body.classList.toggle("intro-open", introOpen);
    var contestMode = Boolean(perContest && perContest.isActive());
    el("obs-history").hidden = contestMode;
    if (contestMode) {
      el("obs-current").hidden = true;
      el("obs-done").hidden = true;
      setProgress(ballots.filter(function (b) { return entryFor(b) || perContest.doneFor(b); }).length);
      setStatus();
      return;
    }
    var list = el("obs-history");
    list.textContent = "";
    var next = null;
    var done = 0;
    var furthest = 0;
    ballots.forEach(function (ballot) {
      if (entryFor(ballot)) { done += 1; furthest = Math.max(furthest, ballot.position); }
      else if (!next) next = ballot;
    });
    if (mode === "next") current = next;
    // Every ballot up to the furthest one done, except the one shown below:
    // done ones, and ones skipped (not read yet).
    ballots.forEach(function (ballot) {
      if (ballot !== current) {
        var entry = entryFor(ballot);
        if (entry || ballot.position < furthest) list.appendChild(listItem(ballot, entry));
      }
      offListItems(ballot.position).forEach(function (o) { list.appendChild(offListItem(o)); });
    });
    setProgress(done);
    var finished = ballots.length > 0 && !current;
    el("obs-done").hidden = !finished;
    if (finished && !offeredDownload) {
      offeredDownload = true;
      el("obs-download").focus();
    }
    showCurrent(furthest);
    setStatus();
  }

  function showCurrent(furthest) {
    var section = el("obs-current");
    if (!current) { section.hidden = true; return; }
    section.hidden = false;
    showBallotHead("obs", current);
    var marks = [];
    if (current.possible) marks.push("POSSIBLE: drawn only if the sample count is above its low estimate");
    if (current.boundary) marks.push(current.boundary);
    if (current.source) marks.push("From the list " + current.source);
    if (current.in_rla) marks.push(current.in_rla);
    if (current.targeted_in) marks.push(current.targeted_in);
    var marksEl = el("obs-marks");
    marksEl.textContent = marks.join(" · ");
    marksEl.hidden = !marks.length;
    el("obs-other-board").hidden = !current.boundary;
    var label = "";
    if (mode === "edit") label = "changing an earlier entry";
    else if (mode === "jump") label = "read out of order (expected " + (jumpFrom ? jumpFrom.id : "another") + ")";
    else if (mode === "chosen" || (!entryFor(current) && current.position < (furthest || 0))) label = "not read yet: skipped earlier";
    var badge = el("obs-mode");
    badge.textContent = "";
    if (label) {
      var icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "○ ";
      badge.appendChild(icon);
      badge.appendChild(document.createTextNode(label));
    }
    badge.hidden = !label;
    var earlier = el("obs-earlier");
    var had = entryFor(current);
    earlier.textContent = "";
    if (mode === "edit" && had) {
      earlier.textContent = "Entered: " + entryText(had) + (had.receipt ? " (receipt " + had.receipt + ")" : "") +
        (had.previous ? ". Before that: " + entryText(had.previous) + (had.previous.receipt ? " (receipt " + had.previous.receipt + ")" : "") : "") +
        (had.corrected > 1 ? " (changed " + had.corrected + " times)" : "") + ". A new entry is added; nothing is overwritten.";
    }
    earlier.hidden = !earlier.textContent;
    el("obs-cancel").hidden = mode === "next";
    voteList(el("obs-votes"), current);
    var fresh = shownFor !== current;
    if (fresh) {
      // A new ballot: time from now, and start again at its first reading, unless its first reading was
      // tapped before the page was reloaded (kept on the device, so it isn't recorded twice).
      shownFor = current;
      shownAt = performance.now();
      armed = null;
      if (local.armed && local.armed.position === current.position && !entryFor(current)) {
        armed = current;
        message("First reading already heard (before the page was reloaded). Tap “Confirmed match” after the second reading, or type what was heard instead.");
      } else if (local.armed) {
        delete local.armed;
        save(listStore, local);
      }
    }
    showMatchButton();
    // A new ballot is shown from its top (its ID and first contest), however far
    // down the last one was scrolled; a long ballot is taller than the screen.
    section.scrollIntoView({ block: fresh ? "start" : "nearest" });
  }

  function backToNext() {
    mode = "next";
    jumpFrom = null;
    el("obs-note").value = "";
    message("");
    render();
  }

  // -- controls --

  el("obs-match").addEventListener("click", tapMatch);
  // A boundary ballot (listed on two boards until the pull list arrives)
  // that this board did not pull: one tap settles it as another board's.
  function otherBoard(ballot) {
    var entry = { position: ballot.position, imprinted_id: ballot.id, match: false, note: "", other_board: true, edit: Boolean(entryFor(ballot)) };
    local.entries[String(ballot.position)] = entry;
    save(listStore, local);
    signAndQueue(stampFields("other_board", ballot, {}), entry);
  }
  el("obs-other-board").addEventListener("click", function () {
    if (!current || !current.boundary) return;
    armed = null;
    otherBoard(current);
    mode = "next";
    jumpFrom = null;
    message("");
    render();
  });
  el("obs-note-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var box = el("obs-note");
    var note = box.value.trim();
    if (!note) tapMatch();
    else if (window.CorlaIdMatch.looksLikeId(note)) { if (heardId(note, false)) box.value = ""; }
    else record(false, note);
    box.blur();
  });
  el("obs-cancel").addEventListener("click", backToNext);
  el("obs-other").addEventListener("click", function () {
    var form = el("obs-other-form");
    form.hidden = !form.hidden;
    el("obs-other").setAttribute("aria-expanded", form.hidden ? "false" : "true");
    if (!form.hidden) el("obs-other-id").focus();
  });
  el("obs-other-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var box = el("obs-other-id");
    var text = box.value.trim();
    if (text && heardId(text, true)) {
      box.value = "";
      el("obs-other-form").hidden = true;
      el("obs-other").setAttribute("aria-expanded", "false");
    }
  });
  ["obs-download", "obs-download-end"].forEach(function (id) { el(id).addEventListener("click", downloadJson); });
  ["obs-download-csv", "obs-download-csv-end"].forEach(function (id) { el(id).addEventListener("click", downloadCsv); });

  document.addEventListener("keydown", function (event) {
    if (introOpen || el("obs-more").open) return;  // the notice's button, and the More dialog, take their own keys
    if (perContest && perContest.isActive()) {
      perContest.keydown(event);
      return;
    }
    var target = event.target;
    var typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
    if (typing) {
      if (event.key === "Escape") { target.value = ""; target.blur(); }
      return;
    }
    if (target && target.tagName === "BUTTON" && (event.key === "Enter" || event.key === " ")) {
      if (target.id !== "obs-match") return;  // let other buttons work as usual
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      tapMatch();
    } else if (event.key.length === 1) {
      el("obs-note").focus();  // the key's character goes into the box
    } else if (event.key === "Escape" && mode !== "next") {
      backToNext();
    }
  });

  var dim = load("corla-observer:dim", false);
  function applyDim() {
    body.classList.toggle("dim", dim);
    el("obs-dim").setAttribute("aria-pressed", dim ? "true" : "false");
  }
  el("obs-dim").addEventListener("click", function () { dim = !dim; save("corla-observer:dim", dim); applyDim(); });
  applyDim();

  // -- More: everything but the ballot, its place and its buttons --

  function openMore(withHelp) {
    var dialog = el("obs-more");
    if (withHelp) el("obs-help").open = true;
    if (!dialog.open) { if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", ""); }
  }
  function closeMore() {
    var dialog = el("obs-more");
    if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
  }
  el("obs-more-button").addEventListener("click", function () { openMore(false); });
  el("obs-more-close").addEventListener("click", closeMore);
  // Closed (by Close or Esc): the focus goes back to the ballot, not to "More", so that Enter taps or confirms again.
  el("obs-more").addEventListener("close", function () {
    window.setTimeout(function () {
      if (document.activeElement && document.activeElement.id === "obs-more-button") document.activeElement.blur();
      if (perContest && perContest.isActive()) perContest.refocus();
    }, 0);
  });

  // -- the notice before the first ballot: keep the screen out of sight --

  var ACK_NOTICE = "out_of_sight_1";
  var ackKey = storeKey + ":ack";
  var introOpen = false;
  function sessionGet(key) { try { return window.sessionStorage.getItem(key); } catch (e) { return null; } }
  function sessionSet(key, value) { try { window.sessionStorage.setItem(key, value); } catch (e) { /* memory only */ } }
  function today() { return coloradoParts(new Date()).date; }
  // Shown the first time the link is opened on this device, and again in each new browser session and on each new day.
  function needsIntro() { return !(sessionGet(ackKey) === "1" && load(ackKey, null) === today()); }
  // The notice's choice of entry mode: shown pressed, applied (and recorded, if changed) by its start button.
  var introMode = null;
  function showIntroMode() {
    ["two_tap", "per_contest"].forEach(function (m) {
      el("obs-intro-mode-" + m).setAttribute("aria-pressed", (introMode || entryMode || DEFAULT_MODE) === m ? "true" : "false");
    });
  }
  ["two_tap", "per_contest"].forEach(function (m) {
    el("obs-intro-mode-" + m).addEventListener("click", function () { introMode = m; showIntroMode(); el("obs-intro-start").focus({ preventScroll: true }); });
  });
  function showIntro() {
    introMode = null;
    showIntroMode();
    introOpen = true;
    closeMore();
    el("obs-intro").hidden = false;
    render();
    window.scrollTo(0, 0);
    el("obs-intro-start").focus({ preventScroll: true });
  }
  function startFromIntro() {
    introOpen = false;
    el("obs-intro").hidden = true;
    // Always record the mode in effect as the observer starts, chosen or the default, so the record never leaves it to be guessed.
    chooseMode(introMode || entryMode || DEFAULT_MODE, true);
    introMode = null;
    save(ackKey, today());
    sessionSet(ackKey, "1");
    var at = current || ballots[0];
    if (at && meta) {
      signAndQueue({ type: "acknowledged", extract_sha256: meta.extract_sha256, position: at.position, imprinted_id: at.id,
        content: { notice: ACK_NOTICE }, client_time: now() }, null);
    }
    render();
    if (perContest && perContest.isActive()) perContest.show();
  }
  el("obs-intro-start").addEventListener("click", startFromIntro);
  el("obs-intro-more").addEventListener("click", function () { openMore(true); });
  el("obs-intro-again").addEventListener("click", showIntro);

  // -- entry modes: two taps per ballot (above), or contest by contest (percontest.js) --

  var MODES = ["two_tap", "per_contest"];
  // A device that never chose a mode, and whose observer has none recorded for the board, starts contest by contest.
  var DEFAULT_MODE = "per_contest";
  var modeKey = storeKey + ":mode";
  var entryMode = load(modeKey, null);
  var perContest = null;
  var choices = load(listStore + ":choices", {});
  var serverContests = load(listStore + ":contests", {});

  function barHeight() { return el("obs-bar").getBoundingClientRect().height; }
  function setBarHeight() { document.documentElement.style.setProperty("--obs-bar-h", Math.ceil(barHeight()) + "px"); }
  window.addEventListener("resize", setBarHeight);
  // The bar's height changes as its status wraps on a phone; the contest-by-contest header sits just below it.
  if (window.ResizeObserver) new window.ResizeObserver(setBarHeight).observe(el("obs-bar"));

  // The two-tap view of a ballot done contest by contest.
  function setEntry(ballot, entry) {
    local.entries[String(ballot.position)] = entry;
    save(listStore, local);
  }

  // An imprinted ID the board read instead of `ballot`'s, typed in contest-by-contest mode.
  function heardIdFor(ballot, text) {
    var result = window.CorlaIdMatch.match(text, ballots.map(function (b) { return b.id; }));
    if (!text || result.status === "not_id") return { status: "not_id", message: "Type the imprinted ID the board read, such as 102-46-5 or its end, 46-5." };
    if (result.status === "ambiguous") {
      return { status: "ambiguous", message: "“" + text + "” fits several ballots on this board (" + result.candidates.slice(0, 6).join(", ") +
        (result.candidates.length > 6 ? ", …" : "") + "). Type more of the ID." };
    }
    if (result.status === "found" && result.imprinted_id === ballot.id) return { status: "same", message: "That is the ballot shown." };
    shownAt = performance.now();
    signAndQueue(stampFields("heard_id", ballot, { heard: Signing.cleanText(text, 64) }), result.status === "found" ? null : noteOffList(text, ballot));
    if (result.status === "found") return { status: "found", ballot: ballotById(result.imprinted_id), message: "" };
    return { status: "not_found", message: "“" + text + "” is not on this board's list. Your note is recorded; the page stays on " + ballot.id + "." };
  }

  function contestApi() {
    return {
      storeKey: listStore,
      load: load,
      save: save,
      now: now,
      cleanText: Signing.cleanText,
      ballots: function () { return ballots; },
      meta: function () { return meta; },
      choices: function () { return choices; },
      entryFor: entryFor,
      setEntry: setEntry,
      otherBoard: otherBoard,
      heardId: heardIdFor,
      barHeight: barHeight,
      ballotHead: function (ballot) { showBallotHead("pc", ballot); },
      reserveWidth: reserveWidth,
      sign: function (fields, options) { signAndQueue(fields, null, options); },
      flushNow: flushNow,
      pendingCount: function () { return pending().length; },
      pendingRecords: function () {
        return pending().filter(function (r) { return CONTEST_TYPES[r.payload.type] && inList(r); });
      },
      // The payload type for a contest outcome or a ballot done: a looked-up ballot's are recorded apart.
      payloadType: function (type) { return lookupMode ? (type === "ballot_done" ? "lookup_done" : "lookup_contest") : type; }
    };
  }

  function showModeButtons() {
    MODES.forEach(function (m) {
      var button = el("obs-mode-" + m);
      button.setAttribute("aria-pressed", entryMode === m ? "true" : "false");
      button.classList.toggle("is-on", entryMode === m);
    });
  }

  function applyMode() {
    showModeButtons();
    body.classList.toggle("mode-per-contest", entryMode === "per_contest");
    if (!perContest) { render(); return; }
    if (entryMode === "per_contest") { if (!perContest.isActive()) perContest.show(); }
    else perContest.hide();
    render();
  }

  // The observer chose a mode: keep it on the device, record it as a signed entry, and switch.
  // With `always` (starting from the notice), the mode is recorded even if it is the one already in effect.
  function chooseMode(chosen, always) {
    if (lookupMode) { entryMode = "per_contest"; applyMode(); return; }
    if (MODES.indexOf(chosen) < 0 || (chosen === entryMode && !always)) { showModeButtons(); return; }
    var at = (perContest && perContest.isActive() && perContest.current()) || current || ballots[0];
    var changed = chosen !== entryMode;
    entryMode = chosen;
    save(modeKey, chosen);
    if (at && meta) {
      signAndQueue({ type: "entry_mode", extract_sha256: meta.extract_sha256, position: at.position, imprinted_id: at.id,
        content: { mode: chosen }, client_time: now() }, null);
    }
    if (!changed) { applyMode(); return; }
    mode = "next";
    jumpFrom = null;
    message("");
    applyMode();
  }
  // The switch in the bar: after a tap the focus leaves it, so that Enter taps or confirms the ballot again.
  MODES.forEach(function (m) {
    el("obs-mode-" + m).addEventListener("click", function () {
      chooseMode(m);
      el("obs-mode-" + m).blur();
      if (perContest && perContest.isActive()) perContest.refocus();
    });
  });

  function startModes() {
    if (!window.CorlaPerContest || !window.CorlaContests) return;
    if (!perContest) perContest = window.CorlaPerContest(contestApi());
    perContest.rebuild(serverContests);
    if (lookupMode) entryMode = "per_contest";
    el("obs-modes").hidden = lookupMode;
    setBarHeight();
    applyMode();
  }

  // -- "Look up a ballot": a ballot picked from a storage box, by imprinted ID --

  function lookupPosition() {
    var n = 0;
    ballots.forEach(function (b) { if (b.position > n) n = b.position; });
    return n + 1;
  }

  function lookupBallot(text) {
    var status = el("obs-lookup-status");
    var input = el("obs-lookup-id");
    if (!meta) { status.textContent = "Not loaded yet: wait a moment."; return; }
    status.textContent = "Looking up " + text + "…";
    var params = new URLSearchParams();
    params.append("id", text);
    params.append("position", String(lookupPosition()));
    fetch(base + "/lookup.json?" + params.toString(), { credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); })
      .then(function (result) {
        if (!result.ok || result.data.error) { status.textContent = result.data.error || "Could not look the ballot up."; return; }
        var ballot = result.data.ballot;
        ballots.push(ballot);
        save(listStore + ":ballots", ballots);
        Object.keys(result.data.choices || {}).forEach(function (c) { choices[c] = result.data.choices[c]; });
        save(listStore + ":choices", choices);
        input.value = "";
        status.textContent = ballot.id + (result.data.found ? "" : " (not in the redacted CVR)") + (result.data.location ? ", stored at " + result.data.location : "") +
          ": ballot " + ballot.position + " of your lookups. Enter it contest by contest below.";
        if (perContest) { perContest.open(ballot.position); perContest.refocus(); }
        render();
      })
      .catch(function () { status.textContent = "Could not reach the server; check the connection and try again."; });
  }

  var lookupForm = el("obs-lookup-form");
  if (lookupForm) {
    lookupForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = el("obs-lookup-id").value.trim();
      if (text) lookupBallot(text);
    });
  }

  // -- pull lists: the RLA sample and any targeted lists --

  var lastListKey = storeKey + ":list";
  var picker = el("obs-list");
  if (picker) picker.addEventListener("change", switchList);
  // The first time this device shows a list other than the one it showed last: a signed list_switch entry.
  function noteSwitch() {
    var last = load(lastListKey, "rla");
    if (last === listKey || !meta || !device || !ballots.length) return;
    var at = current || ballots[0];
    signAndQueue({ type: "list_switch", extract_sha256: meta.extract_sha256, position: at.position, imprinted_id: at.id,
      content: { list: listKey, name: Signing.cleanText(meta.list_label || listKey, 120) }, client_time: now() }, null);
    save(lastListKey, listKey);
  }

  // -- start --

  function begin() {
    if (!Signing || !Signing.available()) {
      showError("This browser can't sign entries (it needs a secure https page and Web Crypto). Use paper, or another browser.");
      render();
      return;
    }
    signing = startDevice().then(function () { startModes(); render(); noteSwitch(); flush(); }, function (error) {
      showError("Could not make a signing key on this device (" + error.message + "). Use paper, or another browser.");
    });
  }

  fetch(base + "/data.json" + listQuery, { credentials: "same-origin", cache: "no-store" })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (data) {
      // The data is another list's than the page's (the default moved, or a page kept from before): reload on that list.
      var shown = data.error ? data["default"] : data.list;
      if (shown && shown !== listKey) {
        window.location.replace(base + (shown === "rla" ? "" : "?list=" + encodeURIComponent(shown)));
        return;
      }
      if (data.error) { showNothing(data.error, data.lists); return; }
      if (data.notice) { el("obs-rla-missing-text").textContent = data.notice; el("obs-rla-missing").hidden = false; }
      ballots = data.ballots;
      meta = { observer: data.observer, county: data.county, board: String(data.board), extract_sha256: data.extract_sha256, receipt_key: data.receipt_key,
        ident: data.ident, list: data.list, list_label: data.list_label, list_shas: data.list_shas, other_shas: data.other_shas };
      save(listStore + ":ballots", ballots);
      save(listStore + ":meta", meta);
      choices = data.choices || {};
      serverContests = data.contests || {};
      save(listStore + ":choices", choices);
      save(listStore + ":contests", serverContests);
      if (!entryMode) entryMode = MODES.indexOf(data.mode) >= 0 ? data.mode : DEFAULT_MODE;
      if (lookupMode) entryMode = "per_contest";
      // The server's record, then this device's queued entries on top.
      var queued = {};
      pending().forEach(function (r) { if ((r.payload.type === "entry" || r.payload.type === "other_board") && inList(r)) queued[String(r.payload.position)] = true; });
      Object.keys(data.entries).forEach(function (position) {
        if (!local.entries[position] || !queued[position]) {
          var mine = local.entries[position];
          var theirs = data.entries[position];
          local.entries[position] = theirs;
          // Keep this device's receipt code if the server's latest entry is the one it is for.
          if (mine && mine.receipt && mine.receipt.split("-")[0] === String(theirs.seq)) theirs.receipt = mine.receipt;
        }
      });
      // IDs heard that are not on the list: the server's, then this device's not yet recorded.
      var server = data.not_on_list || [];
      var old = local.offlist || [];
      function same(a, b) { return a.device_seq === b.device_seq && a.heard === b.heard; }
      local.offlist = server.map(function (o) {
        var m = old.filter(function (x) { return same(x, o); })[0];
        return { heard: o.heard, at: o.at, device_seq: o.device_seq, seq: o.seq, receipt: m ? m.receipt : "" };
      });
      old.forEach(function (m) { if (!server.some(function (o) { return same(o, m); })) local.offlist.push(m); });
      save(listStore, local);
      if (needsIntro()) introOpen = true;
      if (introOpen) { showIntroMode(); el("obs-intro").hidden = false; }
      render();
      begin();
      if (introOpen) el("obs-intro-start").focus({ preventScroll: true });
    })
    .catch(function () {
      ballots = load(listStore + ":ballots", []);
      if (!entryMode) entryMode = DEFAULT_MODE;
      if (ballots.length && meta) {
        lastSendOk = false;
        if (needsIntro()) { introOpen = true; showIntroMode(); el("obs-intro").hidden = false; }
        render();
        begin();
      }
      else showNothing("Could not load the ballots. Check the connection and reload.");
    });
})();
