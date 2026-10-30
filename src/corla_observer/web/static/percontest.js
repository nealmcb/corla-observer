// corla-observer: contest-by-contest entry on the observer page.
//
// The optional alternative to two taps per ballot (observe.js): the ballot's
// contests as rows, each with its expected choice and its outcome in the
// first and second readings. One row has the focus. Enter (or a tap on the
// row) confirms it as expected and moves to the next; typing a letter or
// digit opens a "heard instead" box on the row, prefilled with it, with the
// contest's choices suggested; "?" or "/" marks the row not heard clearly.
// At the last row, a prompt asks before the second reading starts or the
// ballot is done, so a held Enter can't run on to the next ballot. Every
// outcome is a signed entry ("contest" payload, docs/RECEIPTS.md) in the
// device's chain, kept on the device at once and sent in small batches
// (observe.js); a change is a new entry, marked as a correction. The
// logic without the page is in contestlogic.js. No sound, no vibration.
"use strict";

(function () {
  var C = window.CorlaContests;
  var el = function (id) { return document.getElementById(id); };

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function icon(text) {
    var span = make("span", "pc-icon", text);
    span.setAttribute("aria-hidden", "true");
    return span;
  }

  // Fills the reading button for `reading` (also used to reserve its width for the longer one).
  function readingLabel(reading) {
    return function (button) {
      button.appendChild(icon(reading === 2 ? "2" : "1"));
      button.appendChild(document.createTextNode(reading === 2 ? " Second reading" : " First reading"));
    };
  }

  function isPrintable(key) {
    return key.length === 1 && /[\p{L}\p{N}]/u.test(key);
  }

  // `api` is observe.js's: ballots, meta, choices, signing and sending, and the two-tap entries.
  window.CorlaPerContest = function (api) {
    var viewKey = api.storeKey + ":pc";
    var view = api.load(viewKey, null) || {};
    view.reading = view.reading || {};
    view.focus = view.focus || {};
    var state = {};          // CorlaContests state: every ballot's rows' outcomes
    var active = false;
    var index = -1;          // the ballot shown, as an index into api.ballots()
    var focus = 1;           // the focused row, 1-based
    var rowEls = [];
    var shownAt = 0;
    var prompt = null;       // "second" or "done" while the end-of-reading prompt is shown
    var promptAt = 0;
    var editor = null;       // the open "heard instead" box: {row, wrap, input, list, items, active}
    var pendingUnclear = null;
    var lastQuestion = 0;
    var suppressClick = 0;

    // Milliseconds since the ballot was shown, within what a payload may carry (a day).
    function elapsed() { return Math.min(86400000, Math.max(0, Math.round(performance.now() - shownAt))); }
    function ballots() { return api.ballots(); }
    function ballot() { return ballots()[index] || null; }
    function rows() { var b = ballot(); return b ? C.ballotRows(b) : 0; }
    function saveView() { api.save(viewKey, view); }

    function readingOf(b) {
      var r = view.reading[String(b.position)];
      if (r === 1 || r === 2) return r;
      for (var n = 1; n <= C.ballotRows(b); n++) if (C.marks(state, b.position, n)["2"]) return 2;
      return 1;
    }

    function isDone(b) {
      var e = api.entryFor(b);
      return C.done(state, b) || Boolean(e && (e.match !== undefined || e.other_board));
    }

    // -- the state: the server's record, then this device's unsent entries --

    function rebuild(serverContests) {
      state = C.fromServer(serverContests);
      api.pendingRecords().forEach(function (r) { C.apply(state, r.payload, 0); });
    }

    // -- entries --

    function vote(n) { return ballot().votes[n - 1]; }

    function setMark(n, outcome, heard) {
      var b = ballot();
      if (!b || n < 1 || n > rows()) return;
      var reading = readingOf(b);
      var before = C.marks(state, b.position, n)[String(reading)];
      heard = outcome === "heard_instead" ? api.cleanText(heard, 200) : "";
      if (outcome === "heard_instead" && !heard) return;
      var mark = { outcome: outcome, heard: heard, seq: 0 };
      if (before && C.same(before, mark) && before.heard === mark.heard) return;  // nothing new to record
      var v = vote(n);
      var fields = {
        type: api.payloadType ? api.payloadType("contest") : "contest",
        extract_sha256: api.meta().extract_sha256,
        position: b.position,
        imprinted_id: b.id,
        content: {
          contest: api.cleanText(v[2] || v[0], 500), row: n, rows: rows(), expected: api.cleanText(v[1], 500),
          reading: reading, outcome: outcome, heard: heard, correction: Boolean(before),
          elapsed_ms: elapsed()
        },
        client_time: api.now()
      };
      C.apply(state, fields, 0);
      api.sign(fields, { batch: true });
      updateRow(n);
      if (C.complete(state, b)) settle(b);
      updateHead();
    }

    // Record the two-tap view of a ballot done contest by contest.
    function settle(b) {
      var s = C.summary(state, b);
      api.setEntry(b, { position: b.position, imprinted_id: b.id, match: s.match, note: s.note, per_contest: true, edit: false });
    }

    function markDone() {
      var b = ballot();
      if (!b) return;
      var reading = readingOf(b);
      var left = C.ballotRows(b) ? C.unresolvedRows(state, b, 2).length : 0;
      var fields = {
        type: api.payloadType ? api.payloadType("ballot_done") : "ballot_done",
        extract_sha256: api.meta().extract_sha256,
        position: b.position,
        imprinted_id: b.id,
        content: { reading: reading, rows: C.ballotRows(b), unresolved: left, elapsed_ms: elapsed() },
        client_time: api.now()
      };
      C.apply(state, fields, 0);
      api.sign(fields, { batch: true });
      settle(b);
      closePrompt();
      goTo(nextBallotIndex(index), { announce: "Ballot " + b.n + " done." });
    }

    function nextBallotIndex(from) {
      var list = ballots();
      for (var i = from + 1; i < list.length; i++) if (!isDone(list[i])) return i;
      for (var j = 0; j < list.length; j++) if (!isDone(list[j])) return j;
      return Math.min(from + 1, list.length - 1);
    }

    // -- navigation --

    function goTo(i, options) {
      options = options || {};
      var list = ballots();
      if (!list.length) return;
      i = Math.max(0, Math.min(list.length - 1, i));
      flushQuestion();
      closeEditor();
      closePrompt();
      if (i !== index) api.flushNow();
      index = i;
      var b = ballot();
      view.current = b.position;
      saveView();
      shownAt = performance.now();
      buildRows();
      var start = options.row;
      if (!start) {
        var open = C.unresolvedRows(state, b, readingOf(b));
        start = view.focus[String(b.position)] || open[0] || 1;
      }
      updateHead();
      renderSummary();
      setFocus(Math.max(1, Math.min(rows() || 1, start)), { top: !options.row });
      if (options.announce) announce(options.announce + " Ballot " + b.n + " of " + list.length + ", " + b.id + ", " + readingName(readingOf(b)) + ".");
    }

    function readingName(r) { return r === 2 ? "second reading" : "first reading"; }

    function move(delta) {
      closePrompt();
      var n = focus + delta;
      if (n < 1 || n > rows()) return false;
      setFocus(n);
      return true;
    }

    function setFocus(n, options) {
      options = options || {};
      focus = n;
      var b = ballot();
      if (b) { view.focus[String(b.position)] = n; saveView(); }
      rowEls.forEach(function (li, i) {
        var on = i + 1 === n;
        li.classList.toggle("is-focus", on);
        li.querySelector(".pc-main").tabIndex = on ? 0 : -1;
        li.querySelector(".pc-marker").textContent = on ? "▶" : "";
      });
      var li = rowEls[n - 1] || el("pc-rows").firstChild;
      if (li) {
        var main = li.querySelector(".pc-main");
        if (main && !editor) main.focus({ preventScroll: true });
        keepInView(li, options.top);
      }
      updateHead();
    }

    // Keep the focused row clear of the sticky header and footer, with a row of context.
    function keepInView(li, top) {
      var head = el("pc-head").getBoundingClientRect();
      var foot = el("pc-foot").getBoundingClientRect();
      if (top && focus === 1) {
        var section = el("pc").getBoundingClientRect();
        window.scrollBy(0, section.top - api.barHeight() - 4);
        return;
      }
      var rect = li.getBoundingClientRect();
      var above = head.bottom + 6, below = Math.min(window.innerHeight, foot.top) - 6;
      var context = rect.height;
      if (rect.top - context < above) window.scrollBy(0, rect.top - context - above);
      else if (rect.bottom + context > below) window.scrollBy(0, rect.bottom + context - below);
    }

    function startSecond() {
      var b = ballot();
      if (!b) return;
      closeEditor();
      closePrompt();
      api.flushNow();
      view.reading[String(b.position)] = 2;
      saveView();
      refreshRows();
      renderSummary();
      setFocus(1, { top: true });
      announce("Second reading of " + b.id + ". Contest 1 of " + rows() + ".");
    }

    function toggleReading() {
      var b = ballot();
      if (!b) return;
      view.reading[String(b.position)] = readingOf(b) === 2 ? 1 : 2;
      saveView();
      refreshRows();
      updateHead();
      announce("Now entering the " + readingName(readingOf(b)) + ".");
    }

    function nextUnresolved() {
      closeEditor();
      closePrompt();
      var found = C.nextUnresolved(state, ballots(), index, focus, readingOf);
      if (!found) { announce("Nothing unresolved on this ballot or the ones before it."); flash("Nothing unresolved here or on earlier ballots."); return; }
      if (found.index !== index) goTo(found.index, { row: found.row, announce: "Unresolved contest " + found.row + "." });
      else setFocus(found.row);
    }

    // -- the end-of-reading prompt --

    // At the end of a reading: start the second reading ("second"), or the ballot is done ("done").
    function showPrompt(kind) {
      var b = ballot();
      if (!b) return;
      prompt = kind || (readingOf(b) === 1 && C.ballotRows(b) ? "second" : "done");
      promptAt = performance.now();
      var box = el("pc-prompt");
      box.textContent = "";
      var text;
      if (prompt === "second") {
        var missed = C.unresolvedRows(state, b, 1).length;
        text = "End of the first reading" + (missed ? " (" + missed + " not entered or not clear)" : "") + ". Enter again, or tap below, to start the second reading.";
      } else {
        var left = C.ballotRows(b) ? C.unresolvedRows(state, b, 2).length : 0;
        text = (C.ballotRows(b) ? "Last contest. " : "") + "Ballot " + b.n + " done?" + (left ? " " + left + " unresolved (Alt+N or Next unresolved goes to them)." : "") + " Enter again, or tap below, for the next ballot.";
      }
      box.appendChild(make("span", "pc-prompt-text", text));
      var go = make("button", "pc-prompt-go", prompt === "second" ? "Start second reading" : "Ballot done: next ballot");
      go.type = "button";
      go.addEventListener("click", doPrompt);
      var stay = make("button", "secondary", "Not yet");
      stay.type = "button";
      stay.addEventListener("click", function () { closePrompt(); setFocus(focus); });
      box.appendChild(go);
      box.appendChild(stay);
      box.hidden = false;
      if (rowEls[focus - 1]) keepInView(rowEls[focus - 1]);
      renderSummary();
    }

    function closePrompt() {
      prompt = null;
      el("pc-prompt").hidden = true;
    }

    function doPrompt() {
      if (prompt === "second") startSecond();
      else if (prompt === "done") markDone();
    }

    // -- acting on the focused row --

    function confirmRow(n) {
      closePrompt();
      if (!rows()) { showPrompt(); return; }
      setMark(n, "confirmed", "");
      advance(n);
    }

    function unclearRow(n) {
      closePrompt();
      if (!rows()) return;
      setMark(n, "unclear", "");
      advance(n);
    }

    function advance(n) {
      if (n >= rows()) { setFocus(n); showPrompt(); }
      else setFocus(n + 1);
    }

    // "?" marks the row not heard clearly, a moment later, so that "??" can
    // open the key help instead without recording anything.
    function question() {
      var now = performance.now();
      if (pendingUnclear && now - lastQuestion < 700) {
        clearTimeout(pendingUnclear.timer);
        var row = pendingUnclear.row;
        pendingUnclear = null;
        updateRow(row);
        setFocus(row);
        openHelp();
        return;
      }
      lastQuestion = now;
      if (!rows()) return;
      var n = focus;
      var b = ballot();
      flushQuestion();
      rowEls[n - 1].classList.add("is-pending-unclear");
      pendingUnclear = { row: n, ballot: b, timer: setTimeout(flushQuestion, 700) };
      advanceVisual(n);
    }

    function advanceVisual(n) {
      if (n < rows()) setFocus(n + 1);
    }

    function flushQuestion() {
      if (!pendingUnclear) return;
      var p = pendingUnclear;
      pendingUnclear = null;
      clearTimeout(p.timer);
      if (ballot() !== p.ballot) return;
      if (rowEls[p.row - 1]) rowEls[p.row - 1].classList.remove("is-pending-unclear");
      setMark(p.row, "unclear", "");
      if (p.row >= rows()) showPrompt();
    }

    // -- the "heard instead" box --

    function openEditor(n, initial) {
      closeEditor();
      closePrompt();
      flushQuestion();
      if (!rows()) return;
      setFocus(n);
      var li = rowEls[n - 1];
      var v = vote(n);
      var wrap = make("div", "pc-editor");
      var id = "pc-edit-" + n;
      var label = make("label", "visually-hidden", "What you heard instead in " + v[0]);
      label.htmlFor = id;
      var input = make("input");
      input.id = id;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.maxLength = 200;
      input.placeholder = "Heard instead of “" + v[1] + "”";
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-controls", id + "-list");
      var list = make("ul", "pc-suggest");
      list.id = id + "-list";
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", "Choices in " + v[0]);
      var buttons = make("div", "pc-editor-buttons");
      var save = make("button", "", "Save");
      save.type = "button";
      var cancel = make("button", "secondary", "Cancel");
      cancel.type = "button";
      buttons.appendChild(save);
      buttons.appendChild(cancel);
      wrap.appendChild(label);
      wrap.appendChild(input);
      wrap.appendChild(list);
      wrap.appendChild(buttons);
      li.appendChild(wrap);
      editor = { row: n, wrap: wrap, input: input, list: list, items: [], active: -1 };
      input.value = initial || "";
      input.addEventListener("input", suggest);
      save.addEventListener("click", function () { saveEditor(); });
      cancel.addEventListener("click", function () { cancelEditor(); });
      suggest();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      keepInView(li);
    }

    function suggest() {
      if (!editor) return;
      var v = vote(editor.row);
      var choices = api.choices()[v[2] || v[0]] || [];
      editor.items = C.suggestions(editor.input.value, choices, 8);
      editor.active = editor.input.value.trim() && editor.items.length ? 0 : -1;
      drawSuggestions();
    }

    function drawSuggestions() {
      var list = editor.list;
      list.textContent = "";
      editor.items.forEach(function (item, i) {
        var li = make("li", "pc-option" + (item.kind === "other" ? " pc-option-other" : ""), item.label);
        li.id = editor.input.id + "-opt-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", i === editor.active ? "true" : "false");
        if (i === editor.active) li.classList.add("is-active");
        li.addEventListener("mousedown", function (e) { e.preventDefault(); });
        li.addEventListener("click", function () { saveEditor(item.value); });
        list.appendChild(li);
      });
      if (editor.active >= 0) editor.input.setAttribute("aria-activedescendant", editor.input.id + "-opt-" + editor.active);
      else editor.input.removeAttribute("aria-activedescendant");
    }

    function saveEditor(value) {
      if (!editor) return;
      if (value === undefined) value = editor.active >= 0 ? editor.items[editor.active].value : editor.input.value;
      var n = editor.row;
      value = api.cleanText(value, 200);
      closeEditor();
      if (!value) { setFocus(n); return; }
      var expected = vote(n)[1];
      if (C.norm(value) === C.norm(expected)) setMark(n, "confirmed", "");
      else setMark(n, "heard_instead", value);
      advance(n);
    }

    function cancelEditor() {
      if (!editor) return;
      var n = editor.row;
      closeEditor();
      setFocus(n);
    }

    function closeEditor() {
      if (!editor) return;
      editor.wrap.remove();
      editor = null;
    }

    function editorKey(e) {
      var key = e.key;
      if (key === "ArrowDown" || key === "ArrowUp") {
        if (editor.items.length) {
          var n = editor.items.length;
          editor.active = ((editor.active < 0 ? (key === "ArrowDown" ? -1 : 0) : editor.active) + (key === "ArrowDown" ? 1 : -1) + n) % n;
          drawSuggestions();
        }
      } else if (key === "Enter") {
        if (e.repeat) { e.preventDefault(); return; }
        saveEditor();
      } else if (key === "Escape") {
        cancelEditor();
      } else if (key === "Tab" && editor.active >= 0 && editor.items[editor.active].kind === "choice") {
        editor.input.value = editor.items[editor.active].value;
        suggest();
      } else {
        return;
      }
      e.preventDefault();
    }

    // -- rendering --

    function statusCell(reading, m, current) {
      var span = make("span", "pc-mark pc-mark-" + (m ? m.outcome : "none") + (current ? " is-current" : ""));
      span.appendChild(make("span", "pc-mark-n", reading === 1 ? "1" : "2"));
      var sym = !m ? "○" : m.outcome === "confirmed" ? "✓" : m.outcome === "unclear" ? "?" : "▲";
      span.appendChild(icon(sym));
      return span;
    }

    function buildRows() {
      var list = el("pc-rows");
      list.textContent = "";
      rowEls = [];
      var b = ballot();
      if (!b) return;
      if (!C.ballotRows(b)) {
        var li = make("li", "pc-row pc-row-empty is-focus");
        var main = make("button", "pc-main");
        main.type = "button";
        main.appendChild(make("span", "pc-marker", "▶"));
        main.appendChild(make("span", "pc-text", b.redacted ? "Redacted: this ballot's votes are not in the redacted CVR. Enter, or tap here, when the board has read it." :
          "Not in the CVR. Enter, or tap here, when the board has read it."));
        main.addEventListener("click", function () { showPrompt(); });
        li.appendChild(main);
        list.appendChild(li);
        rowEls.push(li);
        return;
      }
      b.votes.forEach(function (v, i) {
        var n = i + 1;
        var li = make("li", "pc-row");
        li.setAttribute("data-row", String(n));
        // As on the printout: the contest's name right-aligned, the two readings' outcomes in the middle, the choices left-aligned.
        var main = make("button", "pc-main");
        main.type = "button";
        main.tabIndex = -1;
        main.appendChild(make("span", "pc-marker"));
        main.appendChild(make("span", "pc-contest", v[0]));
        main.appendChild(make("span", "pc-status"));
        var expected = make("span", "pc-expected" + (v[1] === "no vote" ? " obs-novote" : ""));
        (v[3] && v[3].length ? v[3] : [v[1]]).forEach(function (line) { expected.appendChild(make("span", "obs-choice-line", line)); });
        main.appendChild(expected);
        main.appendChild(make("span", "pc-heard"));
        main.addEventListener("click", function () {
          if (performance.now() < suppressClick) return;  // the click a keyboard Space makes, already handled
          closeEditor();
          confirmRow(n);
        });
        var edit = make("button", "pc-edit secondary");
        edit.type = "button";
        edit.tabIndex = -1;
        edit.appendChild(icon("✎"));
        edit.setAttribute("aria-label", "Heard something else in " + v[0]);
        edit.addEventListener("click", function () { openEditor(n, ""); });
        var unclear = make("button", "pc-unclear secondary");
        unclear.type = "button";
        unclear.tabIndex = -1;
        unclear.appendChild(icon("?"));
        unclear.setAttribute("aria-label", "Didn't hear " + v[0] + " clearly");
        unclear.addEventListener("click", function () { unclearRow(n); });
        li.appendChild(main);
        li.appendChild(edit);
        li.appendChild(unclear);
        list.appendChild(li);
        rowEls.push(li);
      });
      refreshRows();
    }

    function refreshRows() { for (var n = 1; n <= rowEls.length; n++) updateRow(n); }

    function updateRow(n) {
      var li = rowEls[n - 1];
      var b = ballot();
      if (!li || !b || !C.ballotRows(b)) return;
      var r = C.marks(state, b.position, n);
      var reading = readingOf(b);
      var cell = li.querySelector(".pc-status");
      cell.textContent = "";
      cell.appendChild(statusCell(1, r["1"], reading === 1));
      cell.appendChild(statusCell(2, r["2"], reading === 2));
      var differs = C.differs(r);
      // What was heard instead, and whether the readings differ, on a line of their own.
      var heard = li.querySelector(".pc-heard");
      heard.textContent = "";
      [1, 2].forEach(function (k) {
        var m = r[String(k)];
        if (m && m.outcome === "heard_instead") {
          var line = make("span", "pc-heard-line");
          line.appendChild(icon("▲"));
          line.appendChild(document.createTextNode(" " + (k === 1 ? "1st" : "2nd") + " heard: " + m.heard));
          heard.appendChild(line);
        }
      });
      if (differs) heard.appendChild(make("span", "pc-differs", "≠ readings differ"));
      heard.hidden = !heard.firstChild;
      var mine = r[String(reading)];
      li.classList.toggle("is-differs", differs);
      li.classList.toggle("is-confirmed", Boolean(mine && mine.outcome === "confirmed"));
      li.classList.toggle("is-heard", Boolean(mine && mine.outcome === "heard_instead"));
      li.classList.toggle("is-unclear", Boolean(mine && mine.outcome === "unclear"));
      li.classList.toggle("is-open", !mine);
      var v = vote(n);
      li.querySelector(".pc-main").setAttribute("aria-label",
        "Contest " + n + " of " + rows() + ", " + v[0] + ": expected " + (v[1] === "no vote" ? "no vote" : v[1]) + ". " +
        "First reading " + C.markText(r["1"]) + "; second reading " + C.markText(r["2"]) + (differs ? "; the readings differ" : "") + ".");
    }

    function updateHead() {
      var b = ballot();
      if (!b) return;
      var list = ballots();
      var reading = readingOf(b);
      api.ballotHead(b);
      var readingButton = el("pc-reading");
      api.reserveWidth(readingButton, [readingLabel(2), readingLabel(1)]);
      readingButton.textContent = "";
      readingLabel(reading)(readingButton);
      readingButton.classList.toggle("is-second", reading === 2);
      readingButton.setAttribute("aria-label", (reading === 2 ? "Second reading" : "First reading") + ": tap to switch to the " + readingName(reading === 2 ? 1 : 2));
      var n = rows();
      var entered = 0;
      for (var i = 1; i <= n; i++) if (C.marks(state, b.position, i)[String(reading)]) entered += 1;
      // As wide as its widest value on this ballot, so that the line doesn't change width or wrap as you go.
      api.reserveWidth(el("pc-progress"), [n ? "contest " + n + " of " + n + " · " + n + " entered" : "no contests"]);
      el("pc-progress").textContent = n ? "contest " + focus + " of " + n + " · " + entered + " entered" : "no contests";
      var doneBadge = el("pc-done");
      var done = isDone(b);
      doneBadge.hidden = !done;
      var marks = [];
      if (b.possible) marks.push("POSSIBLE: drawn only if the sample count is above its low estimate");
      if (b.boundary) marks.push(b.boundary);
      if (b.in_rla) marks.push(b.in_rla);
      if (b.targeted_in) marks.push(b.targeted_in);
      if (b.lookup) marks.push(b.location ? "Stored at " + b.location : "Not in the manifest");
      if (b.lookup && b.missing) marks.push("Not in the redacted CVR: no expected votes; enter what the board reads under Done");
      var marksEl = el("pc-marks");
      marksEl.textContent = marks.join(" · ");
      marksEl.hidden = !marks.length;
      el("pc-other-board").hidden = !b.boundary;
      el("pc-prev").disabled = index <= 0;
      el("pc-next").disabled = index >= list.length - 1;
      el("pc-second").disabled = !n;
      var waiting = api.pendingCount();
      api.reserveWidth(el("pc-send"), ["999 to send", "all sent"]);
      el("pc-send").textContent = waiting ? waiting + " to send" : "all sent";
    }

    function renderSummary() {
      var box = el("pc-summary");
      box.textContent = "";
      var list = ballots();
      var items = [];
      var notDone = [];
      list.forEach(function (b, i) {
        if (!C.touched(state, b)) { if (i < index && !isDone(b)) notDone.push(i); return; }
        C.unresolvedRows(state, b, 2).forEach(function (n) { items.push({ i: i, n: n }); });
        if (!isDone(b) && i !== index) notDone.push(i);
      });
      var head = make("h2", "", "Board summary");
      box.appendChild(head);
      box.appendChild(make("p", "meta", items.length ? items.length + " unresolved contest" + (items.length !== 1 ? "s" : "") +
        " (not entered in the second reading, not heard clearly, or heard differently in the two readings)" : "No unresolved contests on the ballots entered so far."));
      if (items.length) {
        var ul = make("ul", "pc-summary-list");
        items.slice(0, 200).forEach(function (item) {
          var b = list[item.i], v = b.votes[item.n - 1], r = C.marks(state, b.position, item.n);
          var li = make("li");
          var go = make("button", "link", "Ballot " + b.n + " (" + b.id + "), contest " + item.n + ": " + v[0]);
          go.type = "button";
          go.addEventListener("click", function () { goTo(item.i, { row: item.n }); });
          li.appendChild(go);
          li.appendChild(document.createTextNode(" · " + (C.differs(r) ? "readings differ" : C.markText(C.finalMark(r)))));
          ul.appendChild(li);
        });
        box.appendChild(ul);
      }
      if (notDone.length) {
        var p = make("p", "", "Not done: ");
        notDone.forEach(function (i, k) {
          var b = list[i];
          var go = make("button", "link", "ballot " + b.n);
          go.type = "button";
          go.addEventListener("click", function () { goTo(i); });
          if (k) p.appendChild(document.createTextNode(", "));
          p.appendChild(go);
        });
        box.appendChild(p);
      }
    }

    function announce(text) { el("pc-live").textContent = text; }

    function flash(text) {
      var box = el("pc-message");
      box.textContent = text || "";
      box.hidden = !text;
    }

    // -- help --

    function openHelp() {
      var dialog = el("pc-keys");
      if (dialog.open) return;
      if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    }

    function closeHelp() {
      var dialog = el("pc-keys");
      if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
      setFocus(focus);
    }

    // -- keys --

    function keydown(e) {
      if (!active) return false;
      var dialog = el("pc-keys");
      if (dialog.open) {
        if (e.key === "?" || e.key === "Escape") { e.preventDefault(); closeHelp(); }
        return true;
      }
      var target = e.target;
      if (editor && target === editor.input) { editorKey(e); return true; }
      var tag = target && target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        if (e.key === "Escape") target.blur();
        return true;
      }
      if (e.ctrlKey || e.metaKey) return false;
      var code = e.code;
      if (e.altKey) {
        var alt = { KeyJ: function () { move(1); }, KeyK: function () { move(-1); }, KeyH: function () { goTo(index - 1); },
          KeyL: function () { goTo(index + 1); }, KeyR: startSecond, KeyN: nextUnresolved }[code];
        if (!alt) return false;
        e.preventDefault();
        flushQuestion();
        alt();
        return true;
      }
      var key = e.key;
      var onRow = target && target.classList && target.classList.contains("pc-main");
      var onControl = !onRow && (tag === "BUTTON" || tag === "A" || tag === "SUMMARY");
      if (prompt && key === "Enter" && !onControl) {
        e.preventDefault();
        if (e.repeat || performance.now() - promptAt < 350) return true;
        doPrompt();
        return true;
      }
      if (onControl && (key === "Enter" || key === " ")) return false;
      if (key !== "?") flushQuestion();
      if (key === "Enter" || key === " ") {
        if (key === " ") suppressClick = performance.now() + 600;
        if (e.repeat && focus >= rows()) { e.preventDefault(); return true; }
        confirmRow(focus);
      } else if (key === "ArrowDown") move(1);
      else if (key === "ArrowUp") move(-1);
      else if (key === "Tab" && (onRow || !onControl)) {
        if (!move(e.shiftKey ? -1 : 1)) return false;
      } else if (key === "PageDown") goTo(index + 1);
      else if (key === "PageUp") goTo(index - 1);
      else if (key === "Home") startSecond();
      else if (key === "End") setFocus(rows() || 1);
      else if (key === "F8") nextUnresolved();
      else if (key === "?") question();
      else if (key === "/") unclearRow(focus);
      else if (key === "Escape") { closePrompt(); flash(""); }
      else if (isPrintable(key) && !e.altKey) openEditor(focus, key);
      else return false;
      e.preventDefault();
      return true;
    }

    // -- controls --

    el("pc-prev").addEventListener("click", function () { goTo(index - 1); });
    el("pc-next").addEventListener("click", function () { goTo(index + 1); });
    el("pc-second").addEventListener("click", startSecond);
    el("pc-unresolved").addEventListener("click", nextUnresolved);
    el("pc-done-button").addEventListener("click", function () {
      if (prompt === "done") markDone(); else showPrompt("done");
    });
    el("pc-reading").addEventListener("click", toggleReading);
    el("pc-keys-button").addEventListener("click", openHelp);
    el("pc-keys-close").addEventListener("click", closeHelp);
    el("pc-other-board").addEventListener("click", function () {
      var b = ballot();
      if (!b || !b.boundary) return;
      api.otherBoard(b);
      goTo(nextBallotIndex(index), { announce: "Ballot " + b.n + " skipped: other board." });
    });
    el("pc-other").addEventListener("click", function () {
      var form = el("pc-other-form");
      form.hidden = !form.hidden;
      el("pc-other").setAttribute("aria-expanded", form.hidden ? "false" : "true");
      if (!form.hidden) el("pc-other-id").focus();
    });
    el("pc-other-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var box = el("pc-other-id");
      var b = ballot();
      if (!b) return;
      var result = api.heardId(b, box.value.trim());
      if (result.status === "found" && result.ballot) {
        box.value = "";
        el("pc-other-form").hidden = true;
        var to = ballots().indexOf(result.ballot);
        goTo(to, { announce: "The board read " + result.ballot.id + " out of order." });
        flash("The board read " + result.ballot.id + " out of order; " + b.id + " stays not done.");
      } else {
        flash(result.message);
      }
    });

    window.addEventListener("resize", function () { if (active && rowEls[focus - 1]) keepInView(rowEls[focus - 1]); });

    return {
      rebuild: rebuild,
      keydown: keydown,
      isActive: function () { return active; },
      show: function () {
        active = true;
        el("pc").hidden = false;
        var list = ballots();
        var start = -1;
        if (view.current !== undefined) list.forEach(function (b, i) { if (b.position === view.current) start = i; });
        if (start < 0) start = nextBallotIndex(-1);
        index = -1;
        goTo(start);
      },
      hide: function () {
        flushQuestion();
        closeEditor();
        closePrompt();
        active = false;
        el("pc").hidden = true;
      },
      refresh: function () { if (active) updateHead(); },
      refocus: function () { if (active) setFocus(focus); },
      current: function () { return ballot(); },
      // Show the ballot at `position` (a ballot just looked up, say).
      open: function (position) {
        var list = ballots();
        var at = -1;
        list.forEach(function (b, i) { if (b.position === position) at = i; });
        if (at < 0) return;
        if (!active) { active = true; el("pc").hidden = false; }
        index = -1;
        goTo(at, { row: 1 });
      },
      doneFor: function (b) { return C.done(state, b); },
      summaryFor: function (b) { return C.touched(state, b) ? C.summary(state, b) : null; }
    };
  };
})();
