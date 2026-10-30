#!/usr/bin/env python3
"""Drive an observer link in a real browser, switching between the RLA sample and a targeted list.

Written 2026-10-28 for the pull-list switcher (corla_observer.pulllists, the
observer page's header list): does switching keep each list's entries and
place apart, is the targeted list marked TARGETED (NOT RLA), and what does
it look like on a phone? It uses Playwright's Chromium (not a dependency of
corla-observer: run it with a Python that has ``playwright``) at phone size
and:

1. opens the observer link (the RLA sample), checks that the notice has
   contest by contest chosen (the default on a new device), starts, checks
   the page is in it, switches to two taps in the header, and enters a
   match on its first ballot (``switch-1-rla.png``);
2. switches to the targeted list KEY with the header's list, checks the
   marking and that nothing is entered there yet, and enters a match
   (``switch-2-targeted.png``);
3. switches back, and checks the RLA sample's entry and place are kept
   (``switch-3-back.png``).

It prints a JSON summary of what it checked and exits 1 if any check failed
(tests/test_browser_switch.py runs it on a 2020 demonstration county).

    python3 scripts/browser_switch.py OUT OBSERVER_URL KEY
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright  # type: ignore[import-not-found]


def main(argv: list[str]) -> int:
    out, url, key = Path(argv[0]), argv[1], argv[2]
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    def progress(page: Page) -> str:
        page.wait_for_function("() => /done/.test(document.getElementById('obs-progress').textContent)", timeout=15000)
        return str(page.inner_text("#obs-progress"))

    def sent(page: Page) -> None:
        page.wait_for_function("() => document.getElementById('obs-status').textContent.trim().endsWith('Live')", timeout=20000)

    def match(page: Page) -> None:
        page.click("#obs-match")
        page.click("#obs-match")
        sent(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#obs-intro-start", state="visible", timeout=15000)
        check("the notice has contest by contest chosen", page.get_attribute("#obs-intro-mode-per_contest", "aria-pressed") == "true"
              and page.get_attribute("#obs-intro-mode-two_tap", "aria-pressed") == "false")  # fmt: skip
        page.click("#obs-intro-start")
        page.wait_for_selector("#pc:not([hidden])", timeout=15000)
        check("it starts contest by contest", page.get_attribute("#obs-mode-per_contest", "aria-pressed") == "true")
        page.click("#obs-mode-two_tap")
        page.wait_for_selector("#obs-current:not([hidden])", timeout=15000)
        check("the header has the pull-list switcher", page.locator("#obs-list option").count() >= 2, page.locator("#obs-list option").all_inner_texts())
        check("it opens on the RLA sample", page.input_value("#obs-list") == "rla" and page.locator(".obs-order:has-text('TARGETED')").count() == 0)
        total = re.search(r"of (\d+) done", progress(page))
        n = int(total.group(1)) if total else 0
        match(page)
        check("a match on the RLA sample", progress(page) == f"1 of {n} done", progress(page))
        page.screenshot(path=str(out / "switch-1-rla.png"))

        page.select_option("#obs-list", key)
        page.wait_for_url(re.compile(r"list=" + re.escape(key)), timeout=15000)
        if page.locator("#obs-intro-start").is_visible():
            page.click("#obs-intro-start")
        targeted = progress(page)
        check("the targeted list starts with nothing entered", targeted.startswith("0 of "), targeted)
        check("it is marked TARGETED (NOT RLA)", "TARGETED (NOT RLA)" in page.inner_text(".obs-order"))
        check("its first ballot is shown", page.inner_text("#obs-pos").startswith("Ballot 1 of "), page.inner_text("#obs-pos"))
        match(page)
        check("a match on the targeted list", progress(page).startswith("1 of "), progress(page))
        page.screenshot(path=str(out / "switch-2-targeted.png"))

        page.select_option("#obs-list", "rla")
        page.wait_for_url(lambda u: "list=" not in u, timeout=15000)
        if page.locator("#obs-intro-start").is_visible():
            page.click("#obs-intro-start")
        back = progress(page)
        check("the RLA sample keeps its entry", back == f"1 of {n} done", back)
        check("and its place: the next ballot", page.inner_text("#obs-pos") == f"Ballot 2 of {n}", page.inner_text("#obs-pos"))
        sent(page)
        page.screenshot(path=str(out / "switch-3-back.png"))
        check("no script errors", not errors, errors)
        browser.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
