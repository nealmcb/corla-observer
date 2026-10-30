// corla-observer: which ballot an observer meant by an imprinted ID they typed.
//
// An exact mirror of corla_observer.practice.match_imprinted_id (the server
// matches again with that for the record); tests/test_practice.py runs both
// on the same cases. Loads in a browser (window.CorlaIdMatch) or in Node.
"use strict";

(function (root) {
  var MIN_ID_DIGITS = 3;
  var ID_TEXT = /^[0-9]+(?:[\s-]+[0-9]+)*$/;

  function idGroups(text) {
    return (text.match(/[0-9]+/g) || []).map(function (g) { return g.replace(/^0+/, "") || "0"; });
  }

  function looksLikeId(text) {
    text = text.trim();
    if (!ID_TEXT.test(text)) return false;
    return idGroups(text).length > 1 || text.length >= MIN_ID_DIGITS;
  }

  function sameList(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  function pullOrderKey(a, b) {
    var pa = a.split("-"), pb = b.split("-");
    for (var i = 0; i < Math.min(pa.length, pb.length); i++) {
      var na = /^[0-9]+$/.test(pa[i]), nb = /^[0-9]+$/.test(pb[i]);
      if (na && nb && Number(pa[i]) !== Number(pb[i])) return Number(pa[i]) - Number(pb[i]);
      if (na !== nb) return na ? -1 : 1;
      if (!na && pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
    }
    return pa.length - pb.length;
  }

  function match(text, ids) {
    text = text.trim();
    if (!looksLikeId(text)) return { status: "not_id", imprinted_id: null, candidates: [] };
    if (ids.indexOf(text) >= 0) return { status: "found", imprinted_id: text, candidates: [] };
    var groups = idGroups(text);
    var digits = text.replace(/[^0-9]/g, "");
    var rules = [
      function (id) { return sameList(idGroups(id), groups); },
      function (id) { return id.replace(/[^0-9]/g, "") === digits; }
    ];
    if (groups.length > 1) {
      rules.push(function (id) {
        var g = idGroups(id);
        return g.length > groups.length && sameList(g.slice(g.length - groups.length), groups);
      });
    }
    for (var r = 0; r < rules.length; r++) {
      var found = [];
      ids.forEach(function (id) { if (rules[r](id) && found.indexOf(id) < 0) found.push(id); });
      found.sort(pullOrderKey);
      if (found.length === 1) return { status: "found", imprinted_id: found[0], candidates: [] };
      if (found.length) return { status: "ambiguous", imprinted_id: null, candidates: found };
    }
    return { status: "not_found", imprinted_id: null, candidates: [] };
  }

  var api = { match: match, looksLikeId: looksLikeId, idGroups: idGroups };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CorlaIdMatch = api;
})(typeof window !== "undefined" ? window : this);
