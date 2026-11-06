// corla-observer extension: the observer's links, kept in the extension's storage, and the extension's own identity.
(function () {
  "use strict";
  var api = typeof browser !== "undefined" ? browser : chrome;
  var PATTERN = /^(https?:\/\/[^\/?#]+)(\/[^?#]*?)?\/observe\/([A-Za-z0-9_\-]+)$/;
  // A public GitHub folder of commitments: https://github.com/OWNER/REPO/tree/REF/PATH
  var FOLDER = /^https:\/\/github\.com\/([^\/]+)\/([^\/]+)\/tree\/([^\/]+)\/(.+?)\/?$/;

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
    var folderText = document.getElementById("commitments").value.trim();
    var folder = folderText ? FOLDER.exec(folderText) : null;
    if (folderText && !folder) { status.textContent = "The commitments folder must be a GitHub folder (https://github.com/OWNER/REPO/tree/BRANCH/PATH)."; return; }
    // Access to this one server only (and GitHub's, for the commitments), asked for now, while the person is pressing the button.
    var origins = [match[1] + "/*"].concat(folder ? ["https://api.github.com/*", "https://raw.githubusercontent.com/*"] : []);
    api.permissions.request({ origins: origins }, function (granted) {
      if (!granted) { status.textContent = "Without access to " + match[1] + " the page cannot reach the server."; return; }
      var server = link.replace(/\/observe\/.*$/, "");
      var commitments = folder ? { owner: folder[1], repo: folder[2], ref: folder[3], path: folder[4], url: folderText } : null;
      stored().then(function (links) {
        if (links.indexOf(link) < 0) links.push(link);
        var writes = [save(links)];
        if (commitments) {
          var q = {}; q["commitments:" + server] = commitments;
          writes.push(new Promise(function (resolve) { api.storage.local.set(q, resolve); }));
        }
        return Promise.all(writes).then(function () { render(links); open(link); });
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
