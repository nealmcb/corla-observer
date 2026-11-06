// corla-observer: an observation report from an observer's signed entries, mirroring corla_observer.obsreport.
//
// The same rules as the server's (see corla_observer/obsreport.py for the report's definition): from the
// payloads in the order signed, each ballot's latest entry counts, two taps or contest by contest; a contest
// row's final outcome is its second reading's, else its first's; a ballot is done when marked done or every
// row has a second reading. The browser extension (extension/verify.js) computes it from the device's own
// records and compares it with the report the server signs. tests/test_obsreport.py runs both on the same
// payloads in Node and checks they are identical. Loads in a browser (window.CorlaReport) or in Node.
"use strict";

(function (root) {
  var ENTRY_TYPES = { entry: true, other_board: true, contest: true, ballot_done: true };

  function norm(text) { return String(text || "").toLowerCase().split(/\s+/).filter(Boolean).join(" "); }
  function isInt(x) { return typeof x === "number" && Number.isInteger(x); }

  function markText(mark) {
    if (mark.outcome === "confirmed") return "confirmed";
    if (mark.outcome === "unclear") return "not heard clearly";
    return "heard “" + mark.heard + "”";
  }
  function same(a, b) { return a.outcome === b.outcome && (a.outcome !== "heard_instead" || norm(a.heard) === norm(b.heard)); }

  function contestBallot(rows, total, markedDone) {
    var complete = total > 0;
    for (var n = 1; n <= total; n++) if (!rows[n] || !rows[n].readings[2]) { complete = false; break; }
    if (!(markedDone || complete)) return { verdict: "incomplete", contests: [], note: "" };
    var differing = [];
    Object.keys(rows).map(Number).sort(function (a, b) { return a - b; }).forEach(function (n) {
      var row = rows[n], first = row.readings[1] || null, second = row.readings[2] || null, fin = second || first, observed;
      var differs = first !== null && second !== null && first.outcome !== "unclear" && second.outcome !== "unclear" && !same(first, second);
      if (differs) observed = "first reading " + markText(first) + ", second " + markText(second);
      else if (fin !== null && fin.outcome !== "confirmed") observed = markText(fin);
      else return;
      differing.push({ row: n, contest: row.contest, expected: row.expected, observed: observed });
    });
    var missing = 0;
    for (var m = 1; m <= total; m++) if (!rows[m]) missing += 1;
    var note = missing ? missing + " contest" + (missing !== 1 ? "s" : "") + " not entered" : "";
    if (differing.length || missing) return { verdict: "discrepancy", contests: differing, note: note };
    return { verdict: "match", contests: [], note: "" };
  }

  function build(payloads, who) {
    var twoTap = {}, contestRows = {}, totals = {}, done = {}, lastContest = {}, imprinted = {};
    payloads.forEach(function (payload, order) {
      var kind = payload.type, position = payload.position;
      if (!ENTRY_TYPES[kind] || payload.extract_sha256 !== who.extract_sha256 || !isInt(position)) return;
      imprinted[position] = String(payload.imprinted_id || "");
      var content = payload.content || {};
      if (kind === "entry" || kind === "other_board") { twoTap[position] = { order: order, payload: payload }; return; }
      lastContest[position] = order;
      if (isInt(content.rows)) totals[position] = content.rows;
      if (kind === "ballot_done") { done[position] = true; return; }
      var row = content.row, reading = content.reading;
      if (!isInt(row) || !isInt(reading) || (reading !== 1 && reading !== 2)) return;
      contestRows[position] = contestRows[position] || {};
      var entry = contestRows[position][row] || (contestRows[position][row] = { contest: String(content.contest || ""), expected: String(content.expected || ""), readings: {} });
      entry.readings[reading] = { outcome: content.outcome, heard: String(content.heard || "") };
    });
    var positions = {};
    Object.keys(twoTap).concat(Object.keys(lastContest)).forEach(function (p) { positions[p] = true; });
    var ballots = Object.keys(positions).map(Number).sort(function (a, b) { return a - b; }).map(function (position) {
      var tap = twoTap[position], result;
      var last = lastContest[position] === undefined ? -1 : lastContest[position];
      if (tap && tap.order > last) {
        var content = tap.payload.content || {};
        if (tap.payload.type === "other_board") result = { verdict: "other_board", note: "" };
        else if (content.match) result = { verdict: "match", note: "" };
        else result = { verdict: "discrepancy", note: String(content.note || "") };
        result.contests = [];
      } else {
        result = contestBallot(contestRows[position] || {}, totals[position] || 0, Boolean(done[position]));
      }
      return { position: position, imprinted_id: imprinted[position], verdict: result.verdict, note: result.note, contests: result.contests };
    });
    var byContest = {};
    ballots.forEach(function (b) { b.contests.forEach(function (f) { byContest[f.contest] = (byContest[f.contest] || 0) + 1; }); });
    function count(v) { return ballots.filter(function (b) { return b.verdict === v; }).length; }
    return {
      v: 1, type: "observation_report", observer: who.observer, county: who.county, board: who.board,
      extract_sha256: who.extract_sha256, ballots: ballots,
      summary: {
        ballots: ballots.length, matches: count("match"), discrepancies: count("discrepancy"),
        other_board: count("other_board"), incomplete: count("incomplete"),
        ballot_level: ballots.filter(function (b) { return b.verdict === "discrepancy" && !b.contests.length; }).length,
        by_contest: byContest
      }
    };
  }

  var api = { build: build };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CorlaReport = api;
})(typeof window !== "undefined" ? window : globalThis);
