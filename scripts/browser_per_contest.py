#!/usr/bin/env python3
"""Drive contest-by-contest entry in a real browser on a long ballot, checking it and taking screenshots.

Written 2026-10-27 for the observer page's contest-by-contest mode
(src/corla_observer/web/static/percontest.js): does the keyboard flow work
on one of Testland's 60-contest ballots (corla_observer.testcounty), and
what do the first and second readings look like on a phone and a desktop?
It uses Playwright's Chromium (not a dependency of corla-observer: run it
with a Python that has ``playwright`` installed) and, at each size, with a
fresh browser profile:

1. opens the observer link, checks the notice before the first ballot
   (contest by contest chosen, the default), chooses two taps on it and
   starts with "I understand, start", takes a screenshot of the first
   ballot in two taps (``<size>-screen-two_tap.png``, for the photo check of
   the observer screen, and ``<size>-screen-tampered.png`` with one choice
   changed in the page), switches to "Contest by contest" (in the header),
   takes the same of the first ballot in it (``<size>-screen-per_contest.png``),
   and pages to the first 60-contest ballot;
2. presses Enter through several contests, types the start of another
   choice in one contest and takes the suggestion, marks one "/" (not
   heard clearly), and confirms a few more;
3. goes to the last contest, and at the end-of-reading prompt starts the
   second reading with a second Enter;
4. confirms the contests again, which makes the one heard differently a
   disagreement, and jumps to the next unresolved contest (Alt+N);
5. opens the key help with "??", confirms a contest with a tap, and at the
   last contest marks the ballot done with a second Enter (a held Enter
   must not do it);
6. reloads the page (the mode, ballot and outcomes stay), switches to two
   taps and back, shows the Dim mode, and downloads the device's record.

Each step's screenshot is ``<size>-<n>-<step>.png`` in OUT, the records
``<size>-record.json``; it prints a JSON summary of what it checked and
exits 1 if any check failed. The entries are recorded on the server under
the link's observer, so use links made for this, one per size, since each
size starts from a ballot nobody has entered (tests/testland.py makes a
workspace with links; tests/test_browser_per_contest.py runs all of it).

    python3 scripts/browser_per_contest.py OUT PHONE_URL [DESKTOP_URL] [--sizes phone,desktop]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright  # type: ignore[import-not-found]

SIZES = {"phone": (390, 844), "desktop": (1366, 900)}


class Run:
    def __init__(self, page: Page, out: Path, size: str) -> None:
        self.page, self.out, self.size = page, out, size
        self.step = 0
        self.checks: list[dict[str, Any]] = []
        self.shots: list[str] = []

    def shot(self, name: str, full: bool = False) -> None:
        self.step += 1
        self.page.wait_for_timeout(250)
        path = self.out / f"{self.size}-{self.step}-{name}.png"
        self.page.screenshot(path=str(path), full_page=full)
        self.shots.append(str(path))

    def check(self, what: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"size": self.size, "check": what, "ok": bool(ok), "detail": detail})

    def key(self, key: str, times: int = 1) -> None:
        for _ in range(times):
            self.page.keyboard.press(key)
            self.page.wait_for_timeout(60)

    def focused(self) -> dict[str, Any]:
        result: dict[str, Any] = self.page.evaluate(
            """() => {
              const li = document.querySelector('.pc-row.is-focus');
              return {
                row: li ? Number(li.dataset.row) : null,
                contest: li ? li.querySelector('.pc-contest').textContent : null,
                expected: li ? li.querySelector('.pc-expected').textContent : null,
                active: document.activeElement ? document.activeElement.className : null,
                progress: document.getElementById('pc-progress').textContent,
                reading: document.getElementById('pc-reading').textContent.trim(),
              };
            }"""
        )
        return result

    def layout(self) -> dict[str, Any]:
        """The header's and the contest bar's heights, the first contest row's place in the page, and the status shown."""
        result: dict[str, Any] = self.page.evaluate(
            """() => {
              const box = (id) => document.getElementById(id).getBoundingClientRect();
              const row = document.querySelector('.pc-row[data-row="1"]').getBoundingClientRect();
              return {
                bar: Math.round(box('obs-bar').height), head: Math.round(box('pc-head').height),
                line: Math.round(document.querySelector('.pc-line').getBoundingClientRect().height),
                row1: Math.round(row.top + window.scrollY),
                status_w: Math.round(box('obs-status').width), status: document.getElementById('obs-status').textContent,
                send: document.getElementById('pc-send').textContent,
              };
            }"""
        )
        return result

    def wait_sent(self, seconds: float = 20) -> None:
        """Wait until every entry has been sent (polled from here: the page's CSP forbids Playwright's eval-based waits)."""
        for _ in range(int(seconds * 10)):
            if self.page.text_content("#pc-send") == "all sent":
                return
            self.page.wait_for_timeout(100)
        raise TimeoutError("entries still waiting to be sent")

    def row_classes(self, row: int) -> str:
        return str(self.page.evaluate(f"document.querySelector('.pc-row[data-row=\"{row}\"]').className"))


def session(page: Page, url: str, out: Path, size: str) -> Run:
    run = Run(page, out, size)
    page.goto(url)
    page.wait_for_selector("#obs-intro:not([hidden])", timeout=20_000)
    run.check("the notice comes first", page.is_hidden("#obs-current") and "out of sight" in (page.text_content("#obs-intro") or ""))
    run.check("the notice offers both ways of entering, contest by contest chosen", page.is_visible("#obs-intro-mode-two_tap")
              and page.get_attribute("#obs-intro-mode-per_contest", "aria-pressed") == "true")  # fmt: skip
    run.shot("notice")
    page.click("#obs-intro-mode-two_tap")
    run.check("choosing two taps on the notice", page.get_attribute("#obs-intro-mode-two_tap", "aria-pressed") == "true"
              and page.get_attribute("#obs-intro-mode-per_contest", "aria-pressed") == "false")  # fmt: skip
    page.click("#obs-intro-start")
    run.check("the switch in the bar shows the mode", page.is_visible("#obs-modes") and page.get_attribute("#obs-mode-two_tap", "aria-pressed") == "true")
    page.wait_for_selector("#obs-current:not([hidden])", timeout=20_000)
    data = page.evaluate("fetch(location.pathname + '/data.json').then(r => r.json())")
    first = data["ballots"][0]
    head = page.text_content(".obs-head") or ""
    run.check("the ballot's header identifies it", first["id"] in head and f"Ballot 1 of {len(data['ballots'])}" in head and data["ident"] in head, head)
    page.wait_for_timeout(300)
    page.screenshot(path=str(out / f"{size}-screen-two_tap.png"))
    run.shots.append(str(out / f"{size}-screen-two_tap.png"))
    # A forger's screen: the second contest's choice changed to another of its choices (in this browser only), then reloaded.
    other = next(c for c in data["choices"][first["votes"][1][2]] if c != first["votes"][1][1])
    page.evaluate(f"document.querySelectorAll('#obs-votes .obs-choice-line')[1].textContent = {json.dumps(other)}")
    page.screenshot(path=str(out / f"{size}-screen-tampered.png"))
    run.shots.append(str(out / f"{size}-screen-tampered.png"))
    page.reload()
    page.wait_for_selector("#obs-current:not([hidden])", timeout=20_000)
    run.check("the notice is not shown again on a reload", page.is_hidden("#obs-intro"))
    page.click("#obs-mode-per_contest")
    page.wait_for_selector("#pc:not([hidden])")
    page.wait_for_timeout(300)
    page.screenshot(path=str(out / f"{size}-screen-per_contest.png"))
    run.shots.append(str(out / f"{size}-screen-per_contest.png"))
    run.check("mode button pressed", page.get_attribute("#obs-mode-per_contest", "aria-pressed") == "true")
    # Page to the first 60-contest ballot.
    for _ in range(len(data["ballots"])):
        if run.focused()["progress"].endswith("of 60 · 0 entered") or " of 60 " in run.focused()["progress"]:
            break
        run.key("PageDown")
    start = run.focused()
    run.check("a 60-contest ballot", " of 60 " in start["progress"], start)
    run.check("the first contest has the focus", start["row"] == 1 and "pc-main" in str(start["active"]), start)
    run.shot("first-reading-start")

    # Nothing in the header moves as Enter is pressed: sending, sent, and queued while offline.
    seen = [run.layout()]
    for n in range(4):
        if n == 3:
            page.context.set_offline(True)
        page.keyboard.press("Enter")
        seen.append(run.layout())  # just after: sending (or offline)
        page.wait_for_timeout(700)
        seen.append(run.layout())
    page.context.set_offline(False)
    run.wait_sent()
    seen.append(run.layout())
    states = {x["status"] for x in seen}
    geometry = {(x["bar"], x["head"], x["line"], x["row1"], x["status_w"]) for x in seen}
    run.check("the header keeps its size and the first row its place while sending, sent and offline",
              len(geometry) == 1 and any("Sending" in t or "Offline" in t for t in states) and any(t.endswith("Live") for t in states),
              seen)  # fmt: skip
    at = run.focused()
    run.check("Enter confirms and moves on", at["row"] == 5 and "is-confirmed" in run.row_classes(4), at)
    # Type the start of a choice other than the one expected, and take the suggestion.
    position_choices = data["choices"]
    ballot = next(b for b in data["ballots"] if b["id"] == page.text_content("#pc-id"))
    row, alternative = None, None
    for n in range(5, 30):
        full = ballot["votes"][n - 1][2]
        expected = ballot["votes"][n - 1][1]
        others = [c for c in position_choices.get(full, []) if c != expected and len(c) >= 4 and c[0].isalpha()]
        if others:
            row, alternative = n, others[-1]
            break
    assert row is not None and alternative is not None
    run.key("Enter", row - 5)
    prefix = alternative.split()[0][:3]
    page.keyboard.type(prefix, delay=40)
    page.wait_for_selector(".pc-editor")
    active = page.text_content(".pc-option.is-active")
    run.check("typing opens the box with suggestions", page.input_value(".pc-editor input") == prefix, prefix)
    run.shot("heard-instead-typing")
    target_index = page.evaluate(f"[...document.querySelectorAll('.pc-option')].findIndex(o => o.textContent === {json.dumps(alternative)})")
    run.check("the alternative is suggested", target_index >= 0, {"prefix": prefix, "alternative": alternative, "active": active})
    current = page.evaluate("[...document.querySelectorAll('.pc-option')].findIndex(o => o.classList.contains('is-active'))")
    for _ in range(max(0, target_index - current)):
        run.key("ArrowDown")
    run.key("Enter")
    run.check("heard instead saved", "is-heard" in run.row_classes(row) and alternative in (page.text_content(f".pc-row[data-row=\"{row}\"] .pc-heard") or ""),
              run.row_classes(row))  # fmt: skip
    unclear_row = row + 1
    run.key("/")
    run.check("/ marks not heard clearly", "is-unclear" in run.row_classes(unclear_row), run.row_classes(unclear_row))
    run.key("Enter", 3)
    run.shot("first-reading-marked")
    run.shot("first-reading-marked-full", full=True)

    run.key("End")
    run.key("Enter")
    page.wait_for_selector("#pc-prompt:not([hidden])")
    run.check("the last contest asks before the second reading", "End of the first reading" in (page.text_content("#pc-prompt") or ""))
    run.shot("end-of-first-reading-prompt")
    page.wait_for_timeout(400)
    run.key("Enter")
    second = run.focused()
    run.check("a second Enter starts the second reading at the first contest", second["row"] == 1 and "Second" in second["reading"], second)
    now = run.layout()
    run.check("the second reading keeps the header's size", [now[k] for k in ("bar", "head", "line", "row1")] == [seen[0][k] for k in ("bar", "head", "line", "row1")],
              (seen[0], now))  # fmt: skip
    run.shot("second-reading-start")

    run.key("Enter", row + 2)
    differs = run.row_classes(row)
    run.check("confirming in the second what was heard instead in the first shows they differ", "is-differs" in differs, differs)
    run.shot("second-reading-differs")
    page.keyboard.press("Home")
    page.keyboard.press("Alt+N")
    page.wait_for_timeout(100)
    jumped = run.focused()
    run.check("Alt+N goes to the first unresolved contest", jumped["row"] == row, jumped)
    run.key("Alt+N")
    jumped2 = run.focused()
    run.check("and again to the next", jumped2["row"] is not None and jumped2["row"] > row, jumped2)
    run.shot("second-reading-next-unresolved")
    page.keyboard.press("?")
    page.keyboard.press("?")
    page.wait_for_timeout(200)
    run.check("?? opens the key help", page.evaluate("document.getElementById('pc-keys').open"))
    run.shot("keys-help")
    run.key("Escape")
    run.check("Esc closes it", not page.evaluate("document.getElementById('pc-keys').open"))
    # A tap on a contest confirms it and moves on.
    tapped = run.focused()["row"]
    page.click(f".pc-row[data-row=\"{tapped}\"] .pc-main")
    run.check("a tap confirms the contest", "is-confirmed" in run.row_classes(tapped) and run.focused()["row"] == tapped + 1, run.focused())
    run.wait_sent()
    run.check("every entry sent", True)
    run.shot("second-reading-all-sent")
    # The last contest, then "Ballot done" with a second Enter, goes to the next ballot.
    ballot_id = page.text_content("#pc-id")
    run.key("End")
    run.key("ArrowUp")
    for _ in range(4):  # Enter held down from the next-to-last contest: the key repeats
        page.keyboard.down("Enter")
        page.wait_for_timeout(40)
    page.keyboard.up("Enter")
    run.check("a held Enter stops at the last contest", page.text_content("#pc-id") == ballot_id and page.is_hidden("#pc-prompt")
              and run.focused()["row"] == 60, run.focused())  # fmt: skip
    run.key("Enter")
    page.wait_for_selector("#pc-prompt:not([hidden])")
    run.check("the last contest of the second reading asks before the next ballot", "done?" in (page.text_content("#pc-prompt") or ""))
    run.shot("ballot-done-prompt")
    page.wait_for_timeout(400)
    run.key("Enter")
    run.check("a second Enter goes to the next ballot, at its first reading",
              page.text_content("#pc-id") != ballot_id and "First" in run.focused()["reading"], run.focused())  # fmt: skip
    run.wait_sent()
    # Reloading keeps the mode, the ballot shown and the earlier ballot's outcomes.
    shown = page.text_content("#pc-id")
    page.reload()
    page.wait_for_selector("#pc:not([hidden])", timeout=20_000)
    run.check("the mode and ballot are kept on reload", page.text_content("#pc-id") == shown)
    while page.text_content("#pc-id") != ballot_id:
        run.key("PageUp")
    run.check("the earlier ballot's outcomes are restored", "is-differs" in run.row_classes(row) and "is-confirmed" in run.row_classes(1),
              run.row_classes(row))  # fmt: skip
    run.check("and it shows as done", page.is_visible("#pc-done"))
    # Back to two taps: the ballot done contest by contest is done there too.
    page.click("#obs-mode-two_tap")
    page.wait_for_selector("#obs-current:not([hidden])")
    history = page.text_content("#obs-history") or ""
    run.check("two taps again: the ballot done contest by contest is listed", ballot_id in history and "first reading heard" in history, history[:300])
    run.shot("two-taps-again")
    page.click("#obs-mode-per_contest")
    page.wait_for_selector("#pc:not([hidden])")
    page.click("#obs-more-button")
    page.click("#obs-dim")
    page.click("#obs-more-close")
    run.shot("dim")
    page.click("#obs-more-button")
    page.click("#obs-dim")
    with page.expect_download() as info:
        page.click("#obs-download")
    page.click("#obs-more-close")
    record = out / f"{size}-record.json"
    info.value.save_as(str(record))
    run.shots.append(str(record))
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("urls", nargs="+", help="An observer link for each size, in order.")
    parser.add_argument("--sizes", default="phone,desktop")
    args = parser.parse_args()
    sizes = args.sizes.split(",")
    if len(args.urls) != len(sizes):
        parser.error("give one observer link for each size")
    args.out.mkdir(parents=True, exist_ok=True)
    runs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size, url in zip(sizes, args.urls, strict=True):
            width, height = SIZES[size]
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, accept_downloads=True)
            runs.append(session(context.new_page(), url, args.out, size))
            context.close()
        browser.close()
    checks = [c for r in runs for c in r.checks]
    print(json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks, "files": [s for r in runs for s in r.shots]}, indent=1))
    sys.exit(0 if all(c["ok"] for c in checks) else 1)


if __name__ == "__main__":
    main()
