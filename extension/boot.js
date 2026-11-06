// corla-observer extension: start the observation page (client.html) for the observer link in its address.
//
// client.html is the server's observation page, rendered once at build time with placeholders
// (scripts/build_extension.py), and its scripts are this extension's own copies. This script runs
// first, before the page's scripts: it reads the observer link from the address
// (client.html#https://HOST/PREFIX/observe/TOKEN[?list=KEY]), puts the server's address and the token
// where the page's scripts read them, and makes the page's links point at the server. The page's
// scripts then fetch only data (data.json) from the server and send it signed entries; the signing key
// is made and kept in this extension's own storage, which no web page can reach.
(function () {
  "use strict";
  // Another list (the page's switcher, or its move to the list it should open on) changes only the address's
  // fragment, which does not reload the page: reload it, so this runs again for the new link.
  window.addEventListener("hashchange", function () { window.location.reload(); });
  var link = decodeURIComponent(window.location.hash.replace(/^#/, ""));
  var match = /^(https?:\/\/[^\/?#]+(?:\/[^?#]*?)?)\/observe\/([A-Za-z0-9_\-]+)(?:\?list=([A-Za-z0-9_\-]+))?$/.exec(link);
  var body = document.body;
  if (!match) {
    body.setAttribute("data-token", "");
    document.title = "corla-observer: no observer link";
    var notice = document.createElement("p");
    notice.className = "notice notice-error";
    notice.textContent = "This page needs an observer link: open it from the extension's list of links.";
    body.insertBefore(notice, body.firstChild);
    return;
  }
  var server = match[1], token = match[2], list = match[3] || "rla";
  body.setAttribute("data-prefix", server);
  body.setAttribute("data-token", token);
  body.setAttribute("data-list", list);
  body.setAttribute("data-client", "extension");
  var placeholderBase = body.getAttribute("data-placeholder-base");
  var placeholderToken = body.getAttribute("data-placeholder-token");
  // Links and forms that lead to the server's own pages (the provenance, a PDF, the photo check).
  document.querySelectorAll("[href], [action]").forEach(function (node) {
    ["href", "action"].forEach(function (name) {
      var value = node.getAttribute(name);
      if (value && placeholderBase && value.indexOf(placeholderBase) === 0) {
        node.setAttribute(name, server + value.slice(placeholderBase.length).split(placeholderToken).join(token));
      }
    });
  });
})();
