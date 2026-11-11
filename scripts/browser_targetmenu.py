#!/usr/bin/env python3
"""Show the suggested draws at the top of /coord/targets in a real browser, at phone and desktop widths, checking them and taking screenshots.

Written 2026-11-11 for the targeting menu (src/corla_observer/targetmenu.py,
web/templates/_targets_menu.html, docs/TARGETING.md, "The coordinator page"),
and revised the same day for its phone-first layout. For COUNTY, at each size
(phone 390 x 844 first, then desktop 1366 x 900), it checks that:

1. the page opens with the suggestion result and its one "Draw these N cards"
   button inside the first screen, without scrolling (``<size>-top.png``, the
   first screen; ``<size>-page.png``, the whole page);
2. pressing Update with BUDGET takes the page to the result, marked "Updated:"
   and highlighted, in view (``<size>-updated.png``);
3. a domain's card opens under "More" (``<size>-more.png``);

and, with ``--draw``, that the one button draws the whole suggestion and lands
on the confirmation in the working list (``drawn.png``). Drawing changes the
workspace: use it on a copy, or a test workspace. It uses Playwright's
Chromium (not a dependency of corla-observer: run it with a Python that has
``playwright``) and logs in once (the server limits logins). PASSWORD may be
``@FILE``, read from FILE, to keep it off the command line. It prints a JSON
summary of what it checked and exits 1 if any check failed
(tests/test_browser_targetmenu.py runs it on Testland).

    python3 scripts/browser_targetmenu.py OUT BASE_URL NAME PASSWORD COUNTY [BUDGET] [--draw]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

SIZES = {"phone": (390, 844), "desktop": (1366, 900)}


def main(argv: list[str]) -> int:
    draw = "--draw" in argv
    args = [a for a in argv if a != "--draw"]
    out, base, name, password, county = (
        Path(args[0]),
        args[1].rstrip("/"),
        args[2],
        args[3],
        args[4],
    )
    if password.startswith("@"):
        password = Path(password[1:]).read_text(encoding="utf-8").strip()
    budget = args[5] if len(args) > 5 else "12"
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    def in_view(page: Any, selector: str) -> tuple[bool, Any]:
        box = page.locator(selector).first.bounding_box()
        height = page.viewport_size["height"]
        return box is not None and box["y"] >= 0 and box["y"] + box[
            "height"
        ] <= height, box

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": SIZES["phone"][0], "height": SIZES["phone"][1]}
        )
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/login")
        page.fill("input[name=name]", name)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        for size, (width, height) in SIZES.items():
            page.set_viewport_size({"width": width, "height": height})
            page.goto(f"{base}/coord/targets?county={quote(county)}")
            result = (
                page.inner_text("#suggestion-result")
                if page.locator("#suggestion-result").count()
                else ""
            )
            check(
                f"{size}: the page opens on the suggestion",
                "Suggested:" in result or "Nothing to draw" in result,
                result[:300],
            )
            check(
                f"{size}: the page is at its top", page.evaluate("window.scrollY") == 0
            )
            ok, box = in_view(page, "#suggestion-result .target-result-text")
            check(f"{size}: the result is on the first screen", ok, box)
            if page.locator(".target-draw-all-button").count():
                ok, box = in_view(page, ".target-draw-all-button")
                check(
                    f"{size}: the Draw button is on the first screen, without scrolling",
                    ok,
                    box,
                )
            else:
                check(
                    f"{size}: a Draw button (a seed is in effect)", False, result[:300]
                )
            width_scroll = page.evaluate("document.documentElement.scrollWidth")
            check(
                f"{size}: no sideways scroll of the page",
                width_scroll <= width + 1,
                width_scroll,
            )
            order = [
                page.locator(f"#{x}").bounding_box()["y"]
                for x in ("suggestion-result", "domains", "list", "other")
            ]
            check(
                f"{size}: the suggestion, the domains, the working list, then the other ways",
                order == sorted(order),
                order,
            )
            check(
                f"{size}: How this works and the other ways start collapsed",
                page.locator("#how-this-works[open], #other-ways[open]").count() == 0,
            )
            badges = [
                c.locator(".badge").count()
                for c in page.locator("li.target-card > p.target-card-name").all()
            ]
            check(
                f"{size}: at most two badges on a card",
                all(n <= 2 for n in badges),
                badges,
            )
            page.screenshot(path=str(out / f"{size}-top.png"), full_page=False)
            page.screenshot(path=str(out / f"{size}-page.png"), full_page=True)
            # Update: the page goes to the result, marked and in view.
            page.fill("form.target-budget input[name=budget]", budget)
            page.locator("form.target-budget button[type=submit]").click()
            page.wait_for_load_state()
            page.wait_for_timeout(200)
            check(
                f"{size}: Update goes to the result",
                page.evaluate("location.hash") == "#suggestion-result",
                page.url,
            )
            updated = page.inner_text("#suggestion-result")
            check(
                f"{size}: the result says it was updated",
                "Updated:" in updated
                and "is-updated"
                in (page.get_attribute("#suggestion-result", "class") or ""),
                updated[:300],
            )
            ok, box = in_view(page, "#suggestion-result .target-result-text")
            check(f"{size}: the updated result is in view", ok, box)
            page.screenshot(path=str(out / f"{size}-updated.png"), full_page=False)
            cards = page.locator("li.target-card")
            if cards.count():
                cards.first.locator("details.target-card-more > summary").click()
                check(
                    f"{size}: a card opens under More",
                    cards.first.locator("table").is_visible(),
                )
                cards.first.scroll_into_view_if_needed()
                page.screenshot(path=str(out / f"{size}-more.png"), full_page=False)
        if draw and page.locator(".target-draw-all-button").count():
            page.goto(f"{base}/coord/targets?county={quote(county)}")
            label = page.inner_text(".target-draw-all-button")
            page.locator(".target-draw-all-button").click()
            page.wait_for_load_state()
            page.wait_for_timeout(200)
            drew = (
                page.inner_text(".target-drew")
                if page.locator(".target-drew").count()
                else ""
            )
            check(
                "the one button draws the whole suggestion",
                f"Drew {label.split()[2]} card" in drew and label.split()[2] in drew,
                {"button": label, "said": drew},
            )
            ok, box = in_view(page, ".target-drew")
            check(
                "the confirmation is in view, in the working list",
                ok and page.evaluate("location.hash") == "#list",
                box,
            )
            page.screenshot(path=str(out / "drawn.png"), full_page=False)
        check("no script errors", not errors, errors)
        browser.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
