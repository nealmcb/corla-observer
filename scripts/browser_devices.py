"""The observer page's warning when the observer's devices disagree, in a real browser (tests/test_browser_devices.py).

Run with a Python that has Playwright, given an observer link whose observer's devices entered its
first ballot differently: opens the link, checks the warning names the ballot and both devices'
entries, and that no script error occurred. Prints a JSON summary of the checks.

Usage: python3 scripts/browser_devices.py OBSERVER_URL IMPRINTED_ID
"""

from __future__ import annotations

import json
import sys
from typing import Any

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]


def main(argv: list[str]) -> int:
    url, imprinted = argv[0], argv[1]
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 400, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text))
        page.goto(url)
        try:
            page.wait_for_selector("#obs-devices:not([hidden])", timeout=15000)
        except Exception:
            check("the warning appears", False, {"errors": errors, "console": console[-10:], "status": page.inner_text("#obs-status"),
                                                 "devices_box": page.locator("#obs-devices").count(), "list_html": page.inner_html("#obs-devices-list"), "state": page.evaluate("(() => { const b = document.getElementById('obs-devices'); return {hidden: b.hidden, display: getComputedStyle(b).display, parent: getComputedStyle(b.parentElement).display, w: b.offsetWidth, h: b.offsetHeight}; })()"), "url": page.url,
                                                 "data": page.evaluate("fetch(location.pathname + '/data.json').then(r => r.json()).then(d => ({list: d.list, disagreements: d.disagreements, error: d.error}))")})  # fmt: skip
            print(json.dumps({"ok": False, "checks": checks}))
            return 1
        text = page.inner_text("#obs-devices")
        check("the warning says the devices disagree on 1 ballot", "Your devices disagree on 1 ballot" in text, text)
        check("it names the ballot", f"Ballot 1 ({imprinted})" in text, text)
        check("it gives both devices' entries", "matched" in text and "different: heard Bob" in text, text)
        check("no script errors", not errors, errors)
        browser.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
