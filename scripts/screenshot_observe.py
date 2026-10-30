#!/usr/bin/env python3
"""Screenshot an observer link's page through a short scripted session, at phone and desktop sizes.

Written 2026-10-26 to look at the observer page for Testland's 60-contest
ballots (corla_observer.testcounty): can an observer follow a long ballot
being read aloud, see where they are, tap "First reading" and "Confirmed
match", and note a different vote in one contest? It drives a real browser
(Playwright's Chromium; not a dependency of corla-observer: run it with a
Python that has ``playwright`` installed) through:

1. the page as first shown (viewport and full page);
2. a match on the first ballot (two taps), leaving the second ballot shown;
3. the second ballot scrolled down to its middle, then to its end;
4. "First reading" tapped, then a note typed for one contest and sent.

Each step is saved as ``<size>-<step>.png`` in OUT. The taps and the note are
recorded on the server as that link's entries, so use a link made for this
(``practice link --observer uicheck``) and revoke it afterwards.

    python3 scripts/screenshot_observe.py OBSERVE_URL OUT [--note TEXT]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright  # type: ignore[import-not-found]

SIZES = {"phone": (390, 844), "desktop": (1366, 900)}


def shoot(page: Page, out: Path, name: str, full: bool = False) -> None:
    page.wait_for_timeout(400)
    page.screenshot(path=str(out / f"{name}.png"), full_page=full)


def session(page: Page, url: str, out: Path, size: str, note: str) -> None:
    page.goto(url)
    page.wait_for_selector("#obs-current:not([hidden])", timeout=20_000)
    shoot(page, out, f"{size}-1-first")
    shoot(page, out, f"{size}-1-first-full", full=True)
    page.click("#obs-match")
    shoot(page, out, f"{size}-2-armed")
    page.click("#obs-match")
    page.wait_for_timeout(800)
    shoot(page, out, f"{size}-3-second-ballot")
    shoot(page, out, f"{size}-3-second-ballot-full", full=True)
    height = page.evaluate("document.getElementById('obs-votes').scrollHeight")
    top = page.evaluate("document.getElementById('obs-votes').getBoundingClientRect().top + window.scrollY")
    page.evaluate(f"window.scrollTo(0, {top + height / 2})")
    shoot(page, out, f"{size}-4-middle")
    page.evaluate(f"window.scrollTo(0, {top + height})")
    shoot(page, out, f"{size}-5-end")
    page.click("#obs-match")
    page.fill("#obs-note", note)
    shoot(page, out, f"{size}-6-note-typed")
    page.press("#obs-note", "Enter")
    page.wait_for_timeout(1000)
    shoot(page, out, f"{size}-7-after-note")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url")
    parser.add_argument("out", type=Path)
    parser.add_argument("--note", default="Prop 115: heard Yes/For")
    parser.add_argument("--sizes", default="phone,desktop")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size in args.sizes.split(","):
            width, height = SIZES[size]
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                                          is_mobile=size == "phone", has_touch=size == "phone")  # fmt: skip
            session(context.new_page(), args.url, args.out, size, args.note)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
