"""The observer page run from the browser extension, in a real Chromium (tests/test_browser_extension.py).

Written 2026-11-05 with the extension (docs/EXTENSION.md). Run with a Python that has Playwright,
given a built extension (scripts/build_extension.py, allowing the server's origin) and an observer
link: loads the extension, opens the link through it (client.html#LINK), chooses two taps, enters
the first ballot as a match, and checks the page ran from the extension and the entry was sent and
answered with a receipt. Prints a JSON summary of the checks.

With COMMITMENTS_LISTING (a listing of SHA256SUMS files in the format of GitHub's contents API), it is
set as the published commitments for the link's server before the page opens, and the page must say
the extract shown is in them.

Usage: python3 scripts/browser_extension.py EXTENSION_DIR OBSERVER_LINK [COMMITMENTS_LISTING]
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from typing import Any
from urllib.parse import quote

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]


def newest_chromium() -> str | None:
    """``$CORLA_OBSERVER_CHROMIUM``, else the newest Chromium in Playwright's cache: the receipt checks need Ed25519
    in Web Crypto (Chrome 137 and later), newer than an older Playwright's own Chromium. None for Playwright's."""
    import glob
    import os

    if os.environ.get("CORLA_OBSERVER_CHROMIUM"):
        return os.environ["CORLA_OBSERVER_CHROMIUM"]
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")),
                   key=lambda path: int(path.split("chromium-")[1].split("/")[0]))  # fmt: skip
    return found[-1] if found else None


def main(argv: list[str]) -> int:
    extension, link = argv[0], argv[1]
    listing = argv[2] if len(argv) > 2 else None
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        # Chromium's own new headless mode, which loads extensions (Playwright's headless option uses the old one,
        # which does not); asked for directly, so no window opens.
        context = p.chromium.launch_persistent_context(
            profile, headless=False, executable_path=newest_chromium(),
            args=["--headless=new", f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )  # fmt: skip
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
        extension_id = worker.url.split("/")[2]
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text))
        if listing:
            # As the links page stores them, for the link's server.
            setup = context.new_page()
            setup.goto(f"chrome-extension://{extension_id}/setup.html")
            server = link.split("/observe/")[0]
            setup.evaluate("([k, v]) => new Promise(r => chrome.storage.local.set({[k]: v}, r))", [f"commitments:{server}", {"listing": listing}])
            setup.close()
        page.goto(f"chrome-extension://{extension_id}/client.html#{quote(link, safe='')}")
        try:
            page.wait_for_selector("#obs-intro:not([hidden])", timeout=20000)
            page.click("#obs-intro-mode-two_tap")
            page.click("#obs-intro-start")
            page.wait_for_selector("#obs-current:not([hidden])", timeout=20000)
        except Exception as exc:  # noqa: BLE001 - reported as a failed check, with what the page showed
            check("the page loads the ballots", False, {"error": str(exc)[:300], "errors": errors, "status": page.inner_text("#obs-status") if page.query_selector("#obs-status") else None})
            print(json.dumps({"ok": False, "checks": checks}))
            return 1
        check("the page runs from the extension", page.url.startswith(f"chrome-extension://{extension_id}/client.html"), page.url)
        head = page.text_content(".obs-head") or ""
        check("it shows the first ballot", "Ballot 1 of" in head, head[:120])
        page.click("#obs-match")
        page.wait_for_timeout(300)
        page.click("#obs-match")
        # Waits by selector: the extension page's policy forbids evaluating strings as code, which wait_for_function does.
        page.wait_for_selector("#obs-status:has-text('Live')", timeout=15000)
        status = page.inner_text("#obs-status")
        check("the entry is sent and answered (Live, nothing queued)", "Live" in status, status)
        keys = page.evaluate(
            "new Promise(r => { const q = indexedDB.open('corla-observer', 1); q.onsuccess = () => { const db = q.result;"
            " const names = Array.from(db.objectStoreNames); r({origin: location.origin, stores: names}); }; q.onerror = () => r(null); })"
        )
        check("the signing key's store is in the extension's own origin", keys is not None and keys["origin"] == f"chrome-extension://{extension_id}", keys)
        # The extension's receipt checks: every receipt verified, and a forged one is an alert.
        page.wait_for_selector("#ext-verify:text-matches('[1-9][0-9]* verified')", timeout=15000)
        verdict = page.inner_text("#ext-verify")
        check("the extension verified the receipts", re.search(r"[1-9][0-9]* verified", verdict) is not None and "problem" not in verdict, verdict)
        if listing:
            check("the extract shown is in the published commitments", "is in the published commitments (SHA256SUMS.txt)" in verdict, verdict)
        page.evaluate(
            "document.dispatchEvent(new CustomEvent('corla-observer:receipt', {detail: {payload_sha256: 'a'.repeat(64),"
            " receipt: {receipt: {key_id: 'x'}, signature: 'AAAA'}}}))"
        )
        try:
            page.wait_for_selector("#ext-verify[role='alert']", timeout=5000)
        except Exception:  # noqa: BLE001 - reported below, with what the line said
            pass
        check("a forged receipt is an alert", "problem with the server's receipts" in page.inner_text("#ext-verify"), {"line": page.inner_text("#ext-verify"), "console": console[-5:]})
        check("no script errors", not errors, errors)
        context.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
