// corla-observer: contest-by-contest entry, the logic without the page.
//
// The observer page's contest-by-contest mode (percontest.js) keeps, for
// each ballot, each contest row's latest outcome in each of the two readings:
// {outcome: "confirmed" | "heard_instead" | "unclear", heard: "...", seq}.
// This file holds what can be worked out from that alone, mirroring
// corla_observer.contests on the server: whether two readings differ,
// which rows are unresolved, whether a ballot is done, a one-line note of
// what differs, the next unresolved row, and the choices to suggest while
// typing what was heard. tests/test_contests_js.py runs it in Node. Loads in
// a browser (window.CorlaContests) or in Node.
"use strict";

(function (root) {
  var OUTCOMES = ["confirmed", "heard_instead", "unclear"];

  function norm(text) { return String(text || "").toLowerCase().replace(/\s+/g, " ").trim(); }

  function same(a, b) {
    return a.outcome === b.outcome && (a.outcome !== "heard_instead" || norm(a.heard) === norm(b.heard));
  }

  // state: {position: {rows: {row: {"1": mark, "2": mark}}, done: bool}}
  function ballotOf(state, position) {
    var key = String(position);
    if (!state[key]) state[key] = { rows: {}, done: false };
    return state[key];
  }

  function rowOf(ballot, row) {
    var key = String(row);
    if (!ballot.rows[key]) ballot.rows[key] = {};
    return ballot.rows[key];
  }

  // Apply a signed payload (contest or ballot_done) to the state.
  // A looked-up ballot's payloads ("lookup_contest", "lookup_done": docs/TARGETING.md) carry the same content.
  function apply(state, payload, seq) {
    if (payload.type === "contest" || payload.type === "lookup_contest") {
      var c = payload.content;
      rowOf(ballotOf(state, payload.position), c.row)[String(c.reading)] = { outcome: c.outcome, heard: c.heard, seq: seq || 0 };
    } else if (payload.type === "ballot_done" || payload.type === "lookup_done") {
      ballotOf(state, payload.position).done = true;
    }
  }

  // The server's state (data.json's "contests") as a fresh state.
  function fromServer(contests) {
    var state = {};
    Object.keys(contests || {}).forEach(function (position) {
      var b = contests[position];
      var ballot = ballotOf(state, position);
      ballot.done = Boolean(b.done);
      Object.keys(b.rows || {}).forEach(function (row) {
        var r = rowOf(ballot, row);
        Object.keys(b.rows[row]).forEach(function (reading) { r[reading] = b.rows[row][reading]; });
      });
    });
    return state;
  }

  function marks(state, position, row) {
    var b = state[String(position)];
    return (b && b.rows[String(row)]) || {};
  }

  function finalMark(r) { return r["2"] || r["1"] || null; }

  // Heard differently in the two readings (not heard clearly in one is not a difference).
  function differs(r) {
    var a = r["1"], b = r["2"];
    if (!a || !b || a.outcome === "unclear" || b.outcome === "unclear") return false;
    return !same(a, b);
  }

  // Whether a row still needs attention in a reading: in the first, no
  // outcome yet or not heard clearly; in the second, no second-reading
  // outcome, not heard clearly in the latest reading, or readings that
  // differ. A row confirmed only in the second reading is resolved.
  function unresolved(r, reading) {
    if (reading === 1) return !r["1"] || r["1"].outcome === "unclear";
    var f = finalMark(r);
    return !r["2"] || !f || f.outcome === "unclear" || differs(r);
  }

  function ballotRows(ballot) { return ballot.redacted || ballot.missing ? 0 : ballot.votes.length; }

  function unresolvedRows(state, ballot, reading) {
    var out = [];
    for (var n = 1; n <= ballotRows(ballot); n++) if (unresolved(marks(state, ballot.position, n), reading)) out.push(n);
    return out;
  }

  function complete(state, ballot) {
    var n = ballotRows(ballot);
    if (!n) return false;
    for (var i = 1; i <= n; i++) if (!marks(state, ballot.position, i)["2"]) return false;
    return true;
  }

  function done(state, ballot) {
    var b = state[String(ballot.position)];
    return Boolean(b && b.done) || complete(state, ballot);
  }

  function touched(state, ballot) {
    var b = state[String(ballot.position)];
    return Boolean(b && (b.done || Object.keys(b.rows).length));
  }

  function markText(m) {
    if (!m) return "not yet";
    if (m.outcome === "confirmed") return "confirmed";
    if (m.outcome === "unclear") return "not heard clearly";
    return "heard “" + m.heard + "”";
  }

  // Whether the ballot matched, and a note of what differs, as the server's
  // BallotRows.note says it.
  function summary(state, ballot) {
    var parts = [];
    var missing = 0;
    for (var n = 1; n <= ballotRows(ballot); n++) {
      var r = marks(state, ballot.position, n), f = finalMark(r), contest = ballot.votes[n - 1][2] || ballot.votes[n - 1][0];
      if (!f) { missing += 1; continue; }
      if (differs(r)) parts.push(contest + ": first reading " + markText(r["1"]) + ", second " + markText(r["2"]));
      else if (f.outcome !== "confirmed") parts.push(contest + ": " + markText(f));
    }
    if (missing && done(state, ballot)) parts.push(missing + " contest" + (missing !== 1 ? "s" : "") + " not entered");
    return { match: done(state, ballot) && !parts.length, note: parts.join("; ") };
  }

  // The next unresolved row after `row` of the ballot at `index`: later rows
  // of this ballot, then its earlier rows, then the rows of earlier ballots
  // that have been entered (in the second reading's terms). `readingOf(b)` is
  // the reading a ballot is in. Returns {index, row} or null.
  function nextUnresolved(state, ballots, index, row, readingOf) {
    var here = ballots[index];
    if (here) {
      var rows = unresolvedRows(state, here, readingOf(here));
      var after = rows.filter(function (n) { return n > row; });
      var before = rows.filter(function (n) { return n < row; });
      if (after.length) return { index: index, row: after[0] };
      if (before.length) return { index: index, row: before[0] };
    }
    for (var i = 0; i < index; i++) {
      var b = ballots[i];
      if (!touched(state, b)) continue;
      var left = unresolvedRows(state, b, 2);
      if (left.length) return { index: i, row: left[0] };
    }
    return null;
  }

  // Choices to suggest for what was heard instead in a contest: its legal
  // choices that fit the text typed (every word typed starts a word of the
  // choice, or the text appears in it), then NO VOTE, overvote, and the
  // text itself as "other". Each is {label, value}.
  function suggestions(text, choices, limit) {
    var typed = norm(text);
    var words = typed ? typed.split(" ") : [];
    var scored = [];
    (choices || []).concat(["no vote", "overvote"]).forEach(function (choice, i) {
      var c = norm(choice);
      var cw = c.split(/[\s/+().,-]+/).filter(Boolean);
      var score;
      if (!typed) score = 3;
      else if (c.indexOf(typed) === 0) score = 0;
      else if (words.every(function (w) { return cw.some(function (x) { return x.indexOf(w) === 0; }); })) score = 1;
      else if (c.indexOf(typed) >= 0) score = 2;
      else return;
      scored.push({ score: score, i: i, label: choice === "no vote" ? "NO VOTE" : choice, value: choice });
    });
    scored.sort(function (a, b) { return a.score - b.score || a.i - b.i; });
    var out = scored.slice(0, limit || 8).map(function (s) { return { label: s.label, value: s.value, kind: "choice" }; });
    var exact = out.some(function (s) { return norm(s.value) === typed; });
    if (typed && !exact) out.push({ label: "other: " + String(text).trim(), value: String(text).trim(), kind: "other" });
    return out;
  }

  var api = {
    OUTCOMES: OUTCOMES,
    norm: norm,
    same: same,
    apply: apply,
    fromServer: fromServer,
    marks: marks,
    finalMark: finalMark,
    differs: differs,
    unresolved: unresolved,
    unresolvedRows: unresolvedRows,
    ballotRows: ballotRows,
    complete: complete,
    done: done,
    touched: touched,
    markText: markText,
    summary: summary,
    nextUnresolved: nextUnresolved,
    suggestions: suggestions
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CorlaContests = api;
})(typeof window !== "undefined" ? window : globalThis);
