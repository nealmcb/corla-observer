"""Build the browser extension that runs the observer page from its own code (docs/EXTENSION.md).

Written 2026-11-05 for the software-independence review: on the served page, the code that signs an
observer's entries comes from the server those signatures are meant to check. The extension carries
that code itself, so the server only supplies data and receives signed entries.

What it builds, in OUT (default ``build/extension``):

- ``client.html``: the server's own observation page (``web/templates/observe.html``), rendered once
  here by the real app, against a throwaway workspace, under a placeholder path prefix and token,
  so the extension's page can never drift from the served one. Its scripts and stylesheet are
  pointed at the extension's own copies, and ``boot.js`` is put before them: it fills in the real
  server and token from the observer link at run time (``extension/boot.js``); ``verify.js``, after
  them, checks every receipt the server returns (``extension/verify.js``).
- the static files that page uses, copied unchanged from ``web/static``, and the extension's own
  files from ``extension/`` (manifest, ``boot.js``, the links page ``setup.html`` and
  ``setup.js``, ``background.js``), with the manifest's version set;
- ``files.json`` (the files, in order) and ``SHA256SUMS`` (each file's SHA-256 and name, as
  ``sha256sum`` prints them); the extension's links page shows the SHA-256 of that SHA256SUMS
  text, its identity, to compare with a published release.

``--browser firefox`` writes Firefox's form of the manifest (a background script rather than a
service worker, and the add-on's ID); the default is Chrome's (Chromium, Edge).

``--allow-host ORIGIN`` grants the extension access to ORIGIN when installed (for tests, such as
``http://127.0.0.1/*``); a real install asks for the one server an observer link names instead.

Usage: uv run python scripts/build_extension.py [--out DIR] [--version X.Y.Z] [--allow-host ORIGIN]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "extension"
STATIC = ROOT / "src" / "corla_observer" / "web" / "static"
#: The placeholder path prefix the page is rendered under; boot.js replaces it with the observer link's server.
PLACEHOLDER_PREFIX = "/corla-observer-server"
#: The placeholder token, in place of the throwaway link's random one, so that two builds of a commit are identical.
PLACEHOLDER_TOKEN = "corla-observer-token"
EXTENSION_FILES = ("manifest.json", "background.js", "boot.js", "verify.js", "setup.html", "setup.js")


def render_page(workdir: Path) -> tuple[str, str]:
    """The observation page as the server renders it, for a throwaway Testland link; and that link's token."""
    from fastapi.testclient import TestClient

    from corla_observer.practice import LinkStore
    from corla_observer.web.app import AppConfig, create_app
    from corla_observer.workspace import Workspace

    store = Workspace.open(workdir / "ws")
    token, _ = LinkStore(store.root).create("observer", "client", "Testland", 1)
    app = create_app(AppConfig(workspace=store.root, secret=b"x" * 32, path_prefix=PLACEHOLDER_PREFIX, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://placeholder") as client:
        response = client.get(f"{PLACEHOLDER_PREFIX}/observe/{token}")
    response.raise_for_status()
    return response.text, token


def client_html(page: str, token: str) -> tuple[str, list[str]]:
    """The extension's client.html from the rendered page, and the static files it uses."""
    used: list[str] = []

    def local(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in used:
            used.append(name)
        return name

    page = re.sub(re.escape(PLACEHOLDER_PREFIX) + r"/static/([A-Za-z0-9_.\-]+)\?v=[0-9a-f]+", local, page)
    page = re.sub(r' integrity="[^"]*"', "", page)  # the extension's own files: nothing to check against
    page = page.replace(token, PLACEHOLDER_TOKEN)  # the throwaway link's random token: the same in every build
    first_script = page.index("<script ")
    page = page[:first_script] + '<script src="boot.js" defer></script>\n' + page[first_script:]
    # The checks (extension/verify.js), after the page's own scripts, with the observation report it computes (static/report.js).
    head_end = page.index("</head>")
    page = page[:head_end] + '<script src="report.js" defer></script>\n<script src="verify.js" defer></script>\n' + page[head_end:]
    used.append("report.js")
    page = page.replace("<body ", f'<body data-placeholder-base="{PLACEHOLDER_PREFIX}" data-placeholder-token="{PLACEHOLDER_TOKEN}" ', 1)
    return page, used


#: Firefox's add-on ID, and the first version with the Manifest V3 features used.
GECKO = {"id": "corla-observer-client@electionaudits.org", "strict_min_version": "128.0"}


def build(out: Path, version: str, allow_hosts: list[str], browser: str = "chrome") -> str:
    """Build the extension into ``out`` (replaced); return its identity, the SHA-256 of its SHA256SUMS."""
    with tempfile.TemporaryDirectory() as tmp:
        page, token = render_page(Path(tmp))
    html, used = client_html(page, token)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "client.html").write_text(html, encoding="utf-8")
    for name in used:
        shutil.copyfile(STATIC / name, out / name)
    if "app.css" not in used:  # the links page uses it too
        shutil.copyfile(STATIC / "app.css", out / "app.css")
        used.append("app.css")
    for name in EXTENSION_FILES:
        shutil.copyfile(SOURCE / name, out / name)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    if browser == "firefox":
        manifest["background"] = {"scripts": ["background.js"]}
        manifest["browser_specific_settings"] = {"gecko": GECKO}
    if allow_hosts:
        manifest["host_permissions"] = allow_hosts
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files = sorted(["client.html", *used, *EXTENSION_FILES])
    (out / "files.json").write_text(json.dumps(files) + "\n", encoding="utf-8")
    sums = "".join(f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in files)
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return hashlib.sha256(sums.encode("utf-8")).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "extension")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("--browser", choices=("chrome", "firefox"), default="chrome")
    args = parser.parse_args(argv)
    identity = build(args.out, args.version, args.allow_host, args.browser)
    print(f"built {args.out}: identity (SHA-256 of its SHA256SUMS) {identity}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
