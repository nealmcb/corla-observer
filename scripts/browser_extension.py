"""The observer page run from the browser extension, in a real Chromium (tests/test_browser_extension.py).

Written 2026-11-05 with the extension (docs/EXTENSION.md). Run with a Python that has Playwright,
given a built extension (scripts/build_extension.py, allowing the server's origin) and an observer
link: loads the extension, opens the link through it (client.html#LINK), chooses two taps, enters
the first ballot as a match, and checks the page ran from the extension and the entry was sent and
answered with a receipt. Prints a JSON summary of the checks.

Usage: python3 scripts/browser_extension.py EXTENSION_DIR OBSERVER_LINK
"""

from __future__ import annotations

import json
import sys
import tempfile
from typing import Any
from urllib.parse import quote

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]


def main(argv: list[str]) -> int:
    extension, link = argv[0], argv[1]
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        # Chromium's own new headless mode, which loads extensions (Playwright's headless option uses the old one,
        # which does not); asked for directly, so no window opens.
        context = p.chromium.launch_persistent_context(
            profile, headless=False,
            args=["--headless=new", f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )  # fmt: skip
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
        extension_id = worker.url.split("/")[2]
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
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
        page.wait_for_function("document.querySelector('#obs-status') && /Live/.test(document.querySelector('#obs-status').textContent)", timeout=15000)
        status = page.inner_text("#obs-status")
        check("the entry is sent and answered (Live, nothing queued)", "Live" in status, status)
        keys = page.evaluate(
            "new Promise(r => { const q = indexedDB.open('corla-observer', 1); q.onsuccess = () => { const db = q.result;"
            " const names = Array.from(db.objectStoreNames); r({origin: location.origin, stores: names}); }; q.onerror = () => r(null); })"
        )
        check("the signing key's store is in the extension's own origin", keys is not None and keys["origin"] == f"chrome-extension://{extension_id}", keys)
        check("no script errors", not errors, errors)
        context.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
