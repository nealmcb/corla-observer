// corla-observer extension: the observer's links, kept in the extension's storage, and the extension's own identity.
(function () {
  "use strict";
  var api = typeof browser !== "undefined" ? browser : chrome;
  var PATTERN = /^(https?:\/\/[^\/?#]+)(\/[^?#]*?)?\/observe\/([A-Za-z0-9_\-]+)$/;

  function stored() {
    return new Promise(function (resolve) { api.storage.local.get({ links: [] }, function (got) { resolve(got.links || []); }); });
  }
  function save(links) { return new Promise(function (resolve) { api.storage.local.set({ links: links }, resolve); }); }
  function open(link) { api.tabs.create({ url: api.runtime.getURL("client.html") + "#" + encodeURIComponent(link) }); }

  function render(links) {
    var list = document.getElementById("links");
    list.textContent = "";
    document.getElementById("none").hidden = links.length > 0;
    links.forEach(function (link) {
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "Open";
      button.addEventListener("click", function () { open(link); });
      var text = document.createElement("code");
      text.textContent = link.replace(/\/observe\/.*/, "/observe/…");
      item.appendChild(button);
      item.appendChild(document.createTextNode(" "));
      item.appendChild(text);
      list.appendChild(item);
    });
  }

  document.getElementById("add").addEventListener("submit", function (event) {
    event.preventDefault();
    var status = document.getElementById("add-status");
    var link = document.getElementById("link").value.trim().replace(/[?#].*$/, "");
    var match = PATTERN.exec(link);
    if (!match) { status.textContent = "That is not an observer link (…/observe/…)."; return; }
    // Access to this one server only, asked for now, while the person is pressing the button.
    api.permissions.request({ origins: [match[1] + "/*"] }, function (granted) {
      if (!granted) { status.textContent = "Without access to " + match[1] + " the page cannot reach the server."; return; }
      stored().then(function (links) {
        if (links.indexOf(link) < 0) links.push(link);
        return save(links).then(function () { render(links); open(link); });
      });
    });
  });

  // The extension's identity: its version, and the SHA-256 of its SHA256SUMS (each file's SHA-256 and name, in
  // the order of files.json), to compare with the published release's.
  document.getElementById("version").textContent = api.runtime.getManifest().version;
  fetch(api.runtime.getURL("files.json")).then(function (r) { return r.json(); }).then(function (names) {
    return Promise.all(names.map(function (name) {
      return fetch(api.runtime.getURL(name)).then(function (r) { return r.arrayBuffer(); }).then(function (buffer) {
        // As sha256sum prints it, so the release's SHA256SUMS (and sha256sum on the unpacked files) can be compared.
        return crypto.subtle.digest("SHA-256", buffer).then(function (digest) { return hex(digest) + "  " + name + "\n"; });
      });
    }));
  }).then(function (lines) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(lines.join("")));
  }).then(function (digest) {
    document.getElementById("code-hash").textContent = hex(digest);
  }).catch(function () { document.getElementById("code-hash").textContent = "(could not be computed)"; });

  function hex(buffer) { return Array.from(new Uint8Array(buffer)).map(function (b) { return b.toString(16).padStart(2, "0"); }).join(""); }

  stored().then(render);
})();
