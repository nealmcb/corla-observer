#!/usr/bin/env python3
"""Show the targeting menu (/coord/targets/menu) in a real browser at desktop and phone widths, checking it and taking screenshots.

Written 2026-11-11 for the targeting menu (src/corla_observer/targetmenu.py,
web/templates/coord_targets_menu.html, docs/TARGETING.md, "The targeting
menu"): does the page open for COUNTY with its totals by property, its
domain lines with their tags in words, a suggestion for BUDGET draws and a
draw form per domain, and does a domain's table of contests open; how does
it look on a desktop and on a phone? It uses Playwright's Chromium (not a
dependency of corla-observer: run it with a Python that has ``playwright``),
logs in as a coordinator and, at each size, writes ``<size>-menu.png`` (the
whole page) and ``<size>-domain-open.png`` (the first domain's table open).

It prints a JSON summary of what it checked and exits 1 if any check failed
(tests/test_browser_targetmenu.py runs it on Testland, with a domain draw).

    python3 scripts/browser_targetmenu.py OUT BASE_URL NAME PASSWORD COUNTY [BUDGET]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

SIZES = {"desktop": (1366, 900), "phone": (390, 844)}


def main(argv: list[str]) -> int:
    out, base, name, password, county = (
        Path(argv[0]),
        argv[1].rstrip("/"),
        argv[2],
        argv[3],
        argv[4],
    )
    budget = argv[5] if len(argv) > 5 else "20"
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # One page, logged in once (the server limits logins), resized for each size.
        page = browser.new_page(
            viewport={"width": SIZES["desktop"][0], "height": SIZES["desktop"][1]}
        )
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/login")
        page.fill("input[name=name]", name)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        for size, (width, height) in SIZES.items():
            page.set_viewport_size({"width": width, "height": height})
            page.goto(
                f"{base}/coord/targets/menu?county={quote(county)}&budget={budget}"
            )
            text = page.inner_text("main")
            check(f"{size}: the page opens", "Targeting menu" in text, text[:200])
            check(
                f"{size}: totals by property",
                page.locator("#totals").count() == 1,
                page.inner_text("#totals") if page.locator("#totals").count() else "",
            )
            lines = page.locator("ol.target-menu > li")
            check(f"{size}: domain lines", lines.count() > 0, lines.count())
            check(
                f"{size}: tags as labelled badges",
                page.locator("ol.target-menu .badge").count() > 0
                and "[unseen]" not in text,
            )
            check(f"{size}: the suggestion", f", {budget} draws" in text, budget)
            check(
                f"{size}: a draw form per domain",
                page.locator("ol.target-menu button[type=submit]").count()
                == lines.count(),
            )
            width_scroll = page.evaluate("document.documentElement.scrollWidth")
            check(
                f"{size}: no sideways scroll of the page",
                width_scroll <= width + 1,
                width_scroll,
            )
            page.screenshot(path=str(out / f"{size}-menu.png"), full_page=True)
            if lines.count():
                lines.first.locator("details summary").first.click()
                check(
                    f"{size}: a domain's table opens",
                    lines.first.locator("table").is_visible(),
                )
                lines.first.scroll_into_view_if_needed()
                page.screenshot(
                    path=str(out / f"{size}-domain-open.png"), full_page=False
                )
        check("no script errors", not errors, errors)
        browser.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
