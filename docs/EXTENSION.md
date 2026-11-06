# The observer client as a browser extension

In development, on the `observer-extension` branch.

## Why

On the served observation page, the code that signs an observer's entries comes from the server
those signatures are meant to hold to account. A compromised server could serve code that signs
something other than what the observer entered, or shows a wrong reference. Subresource integrity
doesn't help, since the server also serves the hashes. The extension carries the observer client's
code itself:

- the server only supplies data (`data.json`: the ballots and the extract) and accepts signed
  entries (`/entries`);
- the device's signing key is made and kept in the extension's own storage, which no web page can
  reach.

See the software-independence argument in docs/PLAN.md, "Software independence and
verifiability".

## How it is built

`scripts/build_extension.py` builds it into `build/extension` (git-ignored):

- **`client.html`.** The server's own observation page (`web/templates/observe.html`), rendered once
  by the real app, under a placeholder path prefix and token, so it can never drift from the served
  page. Its scripts and stylesheet point at the extension's own copies.
- **`boot.js`.** Runs first. It reads the observer link from the page's address
  (`client.html#https://HOST/PREFIX/observe/TOKEN`), puts the server's address and the token where
  the page's scripts read them, and points the page's links (provenance, PDFs, photo check) at the
  server. A change of list reloads the page for the new link.
- **The static files,** copied unchanged from `web/static`: the same code as the served page.
  `observe.js` knows when it runs in the extension only so that a change of list opens the
  extension's page rather than the server's.
- **`verify.js`.** Runs after the page's scripts and checks every receipt the server returns, as
  entries are sent:
  - the server's Ed25519 signature over the receipt's canonical body, with the receipt key the
    extension pinned for that server when it first saw it; a different key later is an alert;
  - that the receipt is for the payload this device signed;
  - that the log positions and heads it states agree with every other receipt from that server,
    kept across sessions; two hashes for one position mean the server has shown two logs.

  If the observer gave the published commitments' folder on the links page (from their
  coordinator, on paper or by email, never from the server), it also reads every
  `SHA256SUMS*.txt` there from GitHub, a source apart from the server. It says whether the extract
  shown is one they list, and raises an alert if an anchored log head (each file's
  `event-log-head`) differs from a head a receipt stated for the same position.

  The result is shown at the top of the page ("Receipts checked by this extension: 12 verified"),
  and any failure is an alert asking the observer to keep their record and tell their coordinator.
  The page announces the receipt key and each receipt as events (`corla-observer:meta`,
  `corla-observer:receipt`), which nothing on the served page listens to.
- **`setup.html` and `setup.js`.** The observer's links: paste one, and the extension asks for
  access to that server only, then opens it. The page also shows the extension's version and its
  identity.
- **`background.js`.** Opens the links page from the toolbar button, and nothing else.
- **`files.json`, `SHA256SUMS`, and the identity.** `SHA256SUMS` lists each file's SHA-256 as
  `sha256sum` prints it. The extension's identity is the SHA-256 of that text, computed by the links
  page from the installed files, to compare with a published release.

Two builds of the same commit are byte-identical. `--browser firefox` writes Firefox's form of the
manifest (a background script, and the add-on's ID). `--allow-host ORIGIN` grants access to a
server at install, for tests.

## Installing and checking it

- **Install it unpacked.** In Chromium-based browsers, use "Load unpacked" on the built directory.
  In Firefox, load it as a temporary add-on, or install a signed release. The files are readable as
  installed, and an unpacked extension never updates itself.
- **Check the identity.** Compare the identity on the links page with the published release's, or
  run `sha256sum` on the files and compare with `SHA256SUMS`.
- **One server only.** Its only permission is storage. It has access to no server until an observer
  adds a link, and then only to that link's server.

## Status

**Done:**
- the build;
- the links page;
- the observation page running from the extension's code, with its key in the extension's storage;
- the receipt checks;
- the check of the extract shown against the published commitments.

`tests/test_browser_extension.py` (marked `browser`) loads the built extension in Chromium against
a local server. It opens an observer link through it and enters a ballot. It checks:
- that the page runs from the extension;
- that the entry is signed and answered with a receipt;
- that the key's store is in the extension's origin;
- that the server was never asked for the page or its scripts;
- that the extension verified the receipts, and raises an alert for a forged one;
- that the extract shown is found in published commitments, served in GitHub's listing format.

The receipt checks need Ed25519 in Web Crypto, in Chrome 137 and later and Firefox 129 and later.
An older browser says it cannot check them. The test runs the newest Chromium in Playwright's
cache, or `$CORLA_OBSERVER_CHROMIUM`. The extension page's policy forbids evaluating strings as
code, so the test waits on selectors rather than on page functions.

**To do:**
- **Check that the receipts and the anchors lie on one log.** A receipt's head and an anchor rarely
  fall at the same position, so the extension needs the events between them, from a public endpoint
  of the log. It would recompute their hash chain, which the server cannot fake. This needs that
  endpoint on the server.
- **Commit the receipt key and the targeted lists' extracts in the evidence.** Today the
  commitments list each county's RLA extract, but neither the receipt key nor a targeted list's
  extract. With both listed, the pinned key and every extract could be checked against them.
- **Put the checks in the downloaded record.**
- **Firefox:** test there, including on Android.
- **Releases:** reproducible zips from tagged commits, their identity published and signed like the
  evidence commitments, and a signed Firefox listing.
- **Test the links page,** and the permission request it makes.
- **The observer's guide:** when to use the extension, and how to check it.
