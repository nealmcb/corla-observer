// corla-observer: the only script. It follows the event log's head by
// server-sent events and refreshes the dashboard when it moves, copies
// text to the clipboard, and lets a POST form submit only once (a second
// click on "Build" while the first is on its way would build twice). Pages
// work without it.
"use strict";

(function () {
  // Controls marked js-hidden (a "Use this setting" button the script makes unnecessary) hide when scripts run.
  document.documentElement.classList.add("has-js");
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
      // A page may name where its fresh copy comes from (the Activity page: itself); else the dashboard's fragment.
      var base = live.getAttribute("data-live-url") || prefix + "/fragment/dashboard";
      fetch(base + (base.indexOf("?") < 0 ? "?" : "&") + "v=" + encodeURIComponent(seq), { credentials: "same-origin" })
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
          showContact();
        })
        .catch(function () {
          setStatus("stale", "Could not refresh; reload the page");
        })
        .then(function () {
          loading = false;
          if (wanted !== null && String(wanted) !== String(shown)) refresh(wanted);
        });
    }

    // The last word from the server (a head or a ping) and when the next is due at the latest: shown beside
    // "Live", and checked every few seconds, so a silent connection reads as such rather than as a quiet log.
    var lastContact = null;
    var nextBy = null;
    var auditCenter = "";
    var connected = false;
    var LATE_MS = 10000;
    function clock(t) { return t.toLocaleTimeString(); }
    function showContact() {
      if (!connected) return;
      if (lastContact === null) { setStatus("live", "Live"); return; }
      var now = Date.now();
      if (nextBy !== null && now > nextBy.getTime() + LATE_MS) {
        setStatus("stale", "No word from the server since " + clock(lastContact) + "; reconnecting…");
      } else {
        setStatus("live", "Live · last contact " + clock(lastContact) + (nextBy ? " · next by " + clock(nextBy) : "") + (auditCenter ? " · " + auditCenter : ""));
      }
    }
    function contact(data) {
      lastContact = new Date();
      var within = data && typeof data.next_within === "number" ? data.next_within : null;
      nextBy = within === null ? null : new Date(lastContact.getTime() + within * 1000);
      if (data && typeof data.audit_center === "string") auditCenter = data.audit_center;
      showContact();
    }
    setInterval(showContact, 5000);

    var source = new EventSource(prefix + "/events/stream");
    source.addEventListener("open", function () { connected = true; showContact(); });
    source.addEventListener("error", function () { connected = false; setStatus("stale", "Reconnecting…"); });
    source.addEventListener("ping", function (event) {
      var data = null;
      try { data = JSON.parse(event.data); } catch (e) { /* still a contact */ }
      contact(data);
    });
    source.addEventListener("head", function (event) {
      var head;
      try { head = JSON.parse(event.data); } catch (e) { return; }
      contact(head);
      if (String(head.seq) !== String(shown)) refresh(head.seq);
    });
  }

  // The practice reader's rate of deliberate differences: choosing one sets every form's hidden rate at once,
  // so the next button pressed uses it (without JavaScript, "Use this setting" reloads the page with it).
  function readerRate() {
    var select = document.querySelector("select[data-sets-rate]");
    if (!select) return;
    select.addEventListener("change", function () {
      document.querySelectorAll('input[type="hidden"][name="rate"]').forEach(function (input) { input.value = select.value; });
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
  readerRate();
  submitOnce();
})();
