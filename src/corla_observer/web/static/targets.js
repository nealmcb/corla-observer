// corla-observer: the targeted ballots page (/coord/targets). As boxes are
// checked, it asks the server for the counts and shows them, without a
// reload; "any" clears a contest's boxes, and a candidate's box clears
// "any"; the pattern row's description, count and Draw button follow. A
// source's include box submits its small form at once. The page works
// without it: its "Count" button submits the form, and each source has an
// Include/Exclude button.
"use strict";

(function () {
  document.documentElement.classList.add("has-js");
  var form = document.getElementById("targets-grid");
  if (!form || !window.fetch) return;
  var url = form.getAttribute("data-count-url");
  var county = form.getAttribute("data-county");
  var pending = 0;

  function chosen() {
    return Array.prototype.map.call(form.querySelectorAll("input[name=v]:checked"), function (box) { return box.value; });
  }

  function added() {
    return Array.prototype.map.call(form.querySelectorAll("input[name=add]"), function (box) { return box.value; });
  }

  function show(data) {
    document.getElementById("target-matching").textContent = data.matching;
    document.getElementById("target-clean").textContent = data.clean;
    document.getElementById("target-description").textContent = data.selection ? ": " + data.description : "";
    var hidden = document.getElementById("target-selection");
    if (hidden) hidden.value = data.selection;
    var rowText = document.getElementById("draw-description");
    if (rowText) rowText.textContent = data.selection ? data.description : "No candidate checked yet";
    var rowCount = document.getElementById("draw-matching");
    if (rowCount) rowCount.textContent = data.matching;
    var button = document.getElementById("draw-button");
    if (button) button.disabled = !data.selection || !data.matching;
    Object.keys(data.grid).forEach(function (key) {
      var span = form.querySelector('[data-count="' + key + '"]');
      if (span) span.textContent = (key.slice(-4) === ":any" ? "" : "→ ") + data.grid[key];
    });
  }

  function update() {
    var values = chosen();
    var params = new URLSearchParams();
    params.append("county", county);
    added().forEach(function (a) { params.append("add", a); });
    values.forEach(function (v) { params.append("v", v); });
    var mine = ++pending;
    fetch(url + "?" + params.toString(), { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (mine !== pending) return;
        show(data);
        if (window.history && history.replaceState) history.replaceState(null, "", "?" + params.toString());
      })
      .catch(function () {
        document.getElementById("target-description").textContent = " (could not update: press Count)";
      });
  }

  form.addEventListener("change", function (event) {
    var box = event.target;
    if (box.hasAttribute("data-any")) {
      var contest = box.getAttribute("data-any");
      if (box.checked) {
        form.querySelectorAll('input[data-contest="' + contest + '"]').forEach(function (b) { b.checked = false; });
      } else if (!form.querySelector('input[data-contest="' + contest + '"]:checked')) {
        box.checked = true;
      }
      update();
    } else if (box.hasAttribute("data-contest")) {
      var any = form.querySelector('input[data-any="' + box.getAttribute("data-contest") + '"]');
      if (any) any.checked = !form.querySelector('input[data-contest="' + box.getAttribute("data-contest") + '"]:checked');
      update();
    }
  });

  // A source's include box: the form it is in says the opposite of the box's old state, which is what a change asks for.
  document.querySelectorAll("form.target-include input[data-include]").forEach(function (box) {
    box.addEventListener("change", function () { box.form.submit(); });
  });
})();
