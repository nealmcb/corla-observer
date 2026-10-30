// corla-observer: the only script. It follows the event log's head by
// server-sent events and refreshes the dashboard when it moves, copies
// text to the clipboard, and lets a POST form submit only once (a second
// click on "Build" while the first is on its way would build twice). Pages
// work without it.
"use strict";

(function () {
  var prefix = document.body.getAttribute("data-prefix") || "";

  function setStatus(state, text) {
    var status = document.getElementById("live-status");
    if (!status) return;
    status.setAttribute("data-state", state);
    status.textContent = "";
    var icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = state === "live" ? "● " : "○ ";
    status.appendChild(icon);
    status.appendChild(document.createTextNode(text));
  }

  function followDashboard() {
    var live = document.querySelector("[data-live]");
    if (!live || !window.EventSource) return;
    var shown = live.getAttribute("data-head");
    var loading = false;
    var wanted = null;

    function refresh(seq) {
      wanted = seq;
      if (loading) return;
      loading = true;
      fetch(prefix + "/fragment/dashboard?v=" + encodeURIComponent(seq), { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.text();
        })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var fresh = doc.querySelector("[data-live]");
          var current = document.querySelector("[data-live]");
          if (fresh && current) {
            current.replaceWith(document.importNode(fresh, true));
            shown = fresh.getAttribute("data-head");
          }
          setStatus("live", "Live: updated " + new Date().toLocaleTimeString());
        })
        .catch(function () {
          setStatus("stale", "Could not refresh; reload the page");
        })
        .then(function () {
          loading = false;
          if (wanted !== null && String(wanted) !== String(shown)) refresh(wanted);
        });
    }

    var source = new EventSource(prefix + "/events/stream");
    source.addEventListener("open", function () { setStatus("live", "Live"); });
    source.addEventListener("error", function () { setStatus("stale", "Reconnecting…"); });
    source.addEventListener("head", function (event) {
      var head;
      try { head = JSON.parse(event.data); } catch (e) { return; }
      setStatus("live", "Live");
      if (String(head.seq) !== String(shown)) refresh(head.seq);
    });
  }

  function copyButtons() {
    document.querySelectorAll("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-copy"));
        var status = button.parentNode.querySelector(".copy-status");
        if (!target || !navigator.clipboard) {
          if (status) status.textContent = "Copying is not available here; select the text instead.";
          return;
        }
        navigator.clipboard.writeText(target.textContent).then(
          function () { if (status) status.textContent = "Copied."; },
          function () { if (status) status.textContent = "Could not copy; select the text instead."; }
        );
      });
    });
  }

  // A POST form submits once: a second submit (a double click, Enter and a click) is dropped, and its buttons are
  // disabled once the first is on its way (after this tick, so a button's own name and value still go with the form).
  function submitOnce() {
    var forms = document.querySelectorAll("form[method=post]");
    function buttons(form) { return form.querySelectorAll("button:not([type]), button[type=submit], input[type=submit]"); }
    forms.forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.getAttribute("data-submitted")) { event.preventDefault(); return; }
        form.setAttribute("data-submitted", "1");
        setTimeout(function () {
          buttons(form).forEach(function (b) { if (!b.disabled) { b.disabled = true; b.setAttribute("data-submit-guard", "1"); } });
        }, 0);
      });
    });
    window.addEventListener("pageshow", function () {  // back to the page: its forms may be used again (buttons disabled for other reasons stay so)
      forms.forEach(function (form) {
        form.removeAttribute("data-submitted");
        buttons(form).forEach(function (b) { if (b.getAttribute("data-submit-guard")) { b.disabled = false; b.removeAttribute("data-submit-guard"); } });
      });
    });
  }

  followDashboard();
  copyButtons();
  submitOnce();
})();
