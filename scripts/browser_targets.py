#!/usr/bin/env python3
"""Drive the targeted ballots page (/coord/targets) and an observer's "Look up a ballot" in a real browser, checking them and taking screenshots.

Written 2026-10-28 for the targeted ballots pages (src/corla_observer/web/targets.py,
static/targets.js) and revised 2026-10-29 for the working list and sent lists
of docs/TARGETING.md: do the counts update as boxes are checked, without a
reload; do a preset, a draw with a seed in words, "Draw again", a manual
entry and the include boxes work; does "Send to county" freeze the list; and
does an observer look a ballot up from a box at phone size? It uses
Playwright's Chromium (not a dependency of corla-observer: run it with a
Python that has ``playwright``), logs in as a coordinator and, for COUNTY:

1. opens the page, checks the first candidate box of the first contest and
   waits for the count to change (``targets-1-grid.png``);
2. draws 3 of the first candidate's ballots with a seed in words: the draw is
   a source of the working list (``targets-2-drawn.png``); then draws again
   from its row, which skips any of the first draw's ballots it meets;
3. enters an imprinted ID with a note, unchecks the second draw's include
   box (the working list shrinks) and checks it again (``targets-3-list.png``,
   the whole page);
4. sends the working list to the county: Targeted list 1, with its email
   text and printouts; the working list is empty again (``targets-4-sent.png``);
5. makes an observer link and opens it at phone size: the county has no
   RLA sample yet, so the page opens on Targeted list 1, says so, shows the
   list switcher and the ballots, and never stays at "Loading"
   (``targets-5-observer-phone.png``); then switches to "Look up a ballot",
   looks IMPRINTED_ID up, and confirms its first contest
   (``targets-6-lookup-phone.png``).

It prints a JSON summary of what it checked and exits 1 if any check failed
(tests/test_browser_targets.py runs it on a demonstration-county workspace
without an RLA extract).

    python3 scripts/browser_targets.py OUT BASE_URL NAME PASSWORD COUNTY [IMPRINTED_ID]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]


def main(argv: list[str]) -> int:
    out, base, name, password, county = Path(argv[0]), argv[1].rstrip("/"), argv[2], argv[3], argv[4]
    extra_id = argv[5] if len(argv) > 5 else "1-1-1"
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    def summary(page: Any) -> str:
        return page.inner_text(".target-summary") if page.locator(".target-summary").count() else ""

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/login")
        page.fill("input[name=name]", name)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.goto(f"{base}/coord/targets?county={quote(county)}")
        check("the page opens", page.locator("#targets-grid").count() == 1)
        check("the Draw button waits for a candidate", page.get_attribute("#draw-button", "disabled") is not None, page.inner_text("#draw-row"))
        before = page.inner_text("#target-matching")
        box = page.locator("input[name=v]").first
        box.check()
        page.wait_for_function("b => document.getElementById('target-matching').textContent !== b", arg=before, timeout=15000)
        after = page.inner_text("#target-matching")
        check("the count updates without a reload", after != before, {"before": before, "after": after})
        check("the selection is kept for drawing", page.input_value("#target-selection") != "")
        check("the pattern row follows the boxes", page.inner_text("#draw-matching") == after and page.locator("#draw-button").is_enabled(), page.inner_text("#draw-row"))
        page.screenshot(path=str(out / "targets-1-grid.png"), full_page=False)
        check("split-ticket presets are offered when the parties are known", page.locator("text=Split tickets").count() == 1)
        row = page.locator("#draw-row")
        row.locator("input[name=seed]").fill("the observers' own words")
        row.locator("input[name=size]").fill("3")
        row.locator("#draw-button").click()
        page.wait_for_load_state()
        check("the draw is a source of the working list", page.locator("text=Draw 1: 3 ballots added to the working list").count() == 1, page.inner_text("main")[:600])
        check("the working list says what it holds", "The working list now: 3 ballots" in summary(page), summary(page))
        check("the seed is kept as words", "seed “the observers' own words”" in page.inner_text("table.targets-sources"))
        page.screenshot(path=str(out / "targets-2-drawn.png"), full_page=True)
        page.locator("table.targets-sources tr", has_text="Draw 1").locator("button:has-text('Draw again')").click()
        page.wait_for_load_state()
        check("drawing again makes another draw of 3", page.locator("text=Draw 2: 3 ballots added to the working list").count() == 1, page.inner_text("main")[:600])
        check("the working list has both draws, each ballot once", "The working list now: 6 ballots" in summary(page), summary(page))
        page.locator("a:has-text(\"the draw's ballots and record\")").first.click()
        page.wait_for_load_state()
        email = page.inner_text("#draw-email")
        check("a draw's email names the county and the draw", county in email and " draw " in email, email.splitlines()[:3])
        check("the email keeps the expected votes out", "Expected" not in email)
        page.screenshot(path=str(out / "targets-2-draw.png"), full_page=True)
        page.go_back()
        page.wait_for_load_state()

        page.fill("input[name=ids]", extra_id)
        page.fill("input[name=note]", "seen in a box")
        page.click("text=Look up and add")
        page.wait_for_load_state()
        check("an ID entered by hand is looked up inline", page.locator(f"li:has-text('{extra_id}: ')").count() == 1, page.inner_text("ul.notice") if page.locator("ul.notice").count() else "")
        listed = page.locator("#list table.targets-list tbody tr").count()
        check("the working list has the draws and the entry", listed in (6, 7), listed)
        page.locator("table.targets-sources tr", has_text="Draw 2").locator("input[data-include]").uncheck()
        page.wait_for_load_state()
        page.wait_for_selector("tr.is-excluded", timeout=15000)
        fewer = page.locator("#list table.targets-list tbody tr").count()
        check("unchecking a source takes its ballots out", fewer == listed - 3, {"before": listed, "after": fewer})
        page.locator("table.targets-sources tr", has_text="Draw 2").locator("input[data-include]").check()
        page.wait_for_load_state()
        page.wait_for_function("n => document.querySelectorAll('#list table.targets-list tbody tr').length === n", arg=listed, timeout=15000)
        check("checking it again puts them back", page.locator("#list table.targets-list tbody tr").count() == listed)
        page.screenshot(path=str(out / "targets-3-list.png"), full_page=True)

        send = page.locator("form[action$='/coord/targets/send']")
        check("the send button names the next list", "Targeted list 1" in send.inner_text(), send.inner_text())
        send.locator("button").click()
        page.wait_for_load_state()
        check("the list is sent, and the page says so", page.locator("#sent-notice:has-text('Targeted list 1 sent to the county')").count() == 1, page.inner_text("main")[:800])
        check("the sent list has its email text and printouts", page.locator("#sent-email-1").count() == 1 and page.locator(".target-sent a[download]").count() >= 1)
        check("the email names the list", page.inner_text("#sent-email-1").startswith("Targeted list 1 for"), page.inner_text("#sent-email-1").splitlines()[:2])
        check("the working list is empty again", page.locator("text=Empty: check a source above").count() == 1)
        page.screenshot(path=str(out / "targets-4-sent.png"), full_page=True)

        # An observer's link, opened at phone size: no RLA sample yet, so the page opens on the targeted list.
        page.goto(base + "/coord/links")
        page.select_option("select[name=role]", "observer")
        page.fill("input[name=name]", "ann")
        page.select_option("select[name=county]", county)
        page.fill("input[name=board]", "1")
        page.click("text=Make the link")
        page.wait_for_load_state()
        link = page.inner_text("#new-link").strip()
        check("an observer link is made", link.startswith(base + "/observe/"), link)
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        obs = phone.new_page()
        obs.on("pageerror", lambda e: errors.append(str(e)))
        obs.goto(link)
        for _ in range(150):  # the page's CSP allows no evaluated script, so poll instead of wait_for_function
            if "Loading" not in obs.inner_text("#obs-status"):
                break
            obs.wait_for_timeout(100)
        check("the observer page is not left at Loading", "Loading" not in obs.inner_text("#obs-status"), obs.inner_text("#obs-status"))
        check("no error is shown", obs.locator("#obs-error:not([hidden])").count() == 0, obs.inner_text("#obs-error"))
        note = obs.inner_text("#obs-rla-missing")
        check("it says there is no RLA sample yet and which list is shown", "No RLA sample for" in note and "Showing the targeted list: Targeted list 1" in note, note)
        options = obs.locator("#obs-list option").all_inner_texts()
        check("the switcher shows the targeted list and the lookup", len(options) == 2 and options[0].startswith("Targeted list 1") and options[1].startswith("Look up"), options)
        obs.wait_for_selector("#obs-intro-start", state="visible", timeout=15000)
        obs.screenshot(path=str(out / "targets-5-observer-phone.png"))
        obs.click("#obs-intro-start")
        obs.wait_for_selector("#pc:not([hidden]), #obs-current:not([hidden])", timeout=15000)
        check("its ballots are shown, marked TARGETED (NOT RLA)", "TARGETED (NOT RLA)" in obs.inner_text(".obs-order"))
        check("the ballot header shows Ballot 1 of N", obs.inner_text("#pc-pos, #obs-pos").startswith("Ballot 1 of "), obs.inner_text("#pc-pos, #obs-pos"))

        # Look up a ballot picked from a box.
        obs.select_option("#obs-list", "lookup")
        obs.wait_for_url(lambda u: "list=lookup" in u, timeout=15000)
        obs.wait_for_selector("#obs-lookup-form", state="visible", timeout=15000)
        if obs.locator("#obs-intro-start").is_visible():
            obs.click("#obs-intro-start")
        check("the lookup page is marked LOOKED UP (NOT RLA)", "LOOKED UP (NOT RLA)" in obs.inner_text("#obs-lookup"))
        check("the mode switch is hidden: lookups are contest by contest", obs.locator("#obs-modes:not([hidden])").count() == 0)
        obs.fill("#obs-lookup-id", extra_id)
        obs.click("#obs-lookup-form button[type=submit]")
        obs.wait_for_selector(".pc-row[data-row]", timeout=15000)
        status = obs.inner_text("#obs-lookup-status")
        check("the ballot is looked up and shown", extra_id in status and "ballot 1 of your lookups" in status, status)
        check("its header names it and marks it", obs.inner_text("#pc-id") == extra_id and "LOOKED UP" in obs.inner_text("#pc-ident"), obs.inner_text("#pc-head"))
        obs.click('.pc-row[data-row="1"] .pc-main')
        for _ in range(100):
            if obs.text_content("#pc-send") == "all sent":
                break
            obs.wait_for_timeout(100)
        check("the first contest is confirmed and sent", "is-confirmed" in (obs.get_attribute('.pc-row[data-row="1"]', "class") or "") and obs.text_content("#pc-send") == "all sent")
        obs.screenshot(path=str(out / "targets-6-lookup-phone.png"))
        phone.close()
        check("no script errors", not errors, errors)
        browser.close()
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
