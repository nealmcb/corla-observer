#!/usr/bin/env python3
"""Do every step of the Delta County demonstration in a real browser, checking each and taking screenshots.

Written 2026-10-28 to verify docs/walkthroughs/demo-delta-2020-general.md end to end, on the demo
server or a local copy, before a coordinator follows it by hand. It starts from the empty state
scripts/demo_delta_2020.sh makes (a 2020 general workspace following Delta, nothing else) and, as a
coordinator, in the script's order:

1. the coordinator home: nothing from Delta yet, and the audit center replayed from the mirror
   (``01-home.png``);
2. uploads Delta's redacted CVR as CORLA received it, detected and checked for redaction
   (``02-cvr.png``), then Delta's manifest as the county sent it (``03-manifest.png``);
3. on the targeted ballots page, with no seed in effect and no audit center files: requires
   Hickenlooper for US Senator and Boebert for the 3rd congressional district, draws 5 ballots
   drawing 5 ballots with the observers' own seed into the targeted pull list, and builds the
   targeted list "Delta targets" (``04-targets.png``, ``05-targets-built.png``);
4. the data map: CORLA's upload hashes not yet posted (``06-map-before.png``);
5. "Fetch now" on the home page: the audit center's first state from the mirror, with the
   manifests, the target list and CORLA's upload status (``07-fetched.png``); the data map then shows
   Delta's manifest identical to the audit center's and to CORLA's import, and its CVR identical to
   CORLA's import (``08-map-after.png``);
6. enters the 2020 general's seed (``09-seed.png``);
7. builds Delta's extract, in the predicted order, and downloads its portrait-2 PDF (``10-extract.png``);
8. makes an observer link (``11-link.png``);
9. as that observer, at phone size in a separate browser: the notice, then contest by contest
   (the default) on the first ballot: confirms two contests, types what was heard instead in the
   third, marks the fourth not heard clearly, confirms the fifth (``12-observer-*.png``); then
   switches to the targeted list (``13-observer-targeted.png``);
10. the observation statistics and the public dashboard (``14-stats.png``, ``15-dashboard.png``).

It changes the workspace (that is the point): reset it afterwards with
``scripts/demo_delta_2020.sh WORKSPACE MIRROR --reset``. It uses Playwright's Chromium (run it with
a Python that has ``playwright``), prints a JSON summary of what it checked, and exits 1 if any
check failed.

    python3 scripts/browser_demo_delta_2020.py OUT BASE_URL NAME PASSWORD CVR_FILE MANIFEST_FILE [OBSERVER]

BASE_URL includes the path prefix. The password is read from the environment variable
DEMO_PASSWORD when PASSWORD is "-".
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright  # type: ignore[import-not-found]

#: The 2020 general's seed, from the Secretary of State's Notice of Random Seed of 16 November 2020.
SEED = "92076602804272786701"
SEED_SOURCE = "the Secretary of State's Notice of Random Seed, 16 November 2020 (the mirror's 2020/general/20201116NoticeOfRandomSeed.pdf)"
#: The observers' own seed for the targeted draw (no RLA seed is in effect yet when it is drawn).
TARGET_SEED = "20201106"
SENATE = "United States Senator"
HOUSE = "Representative to the 117th United States Congress-District 3"


def main(argv: list[str]) -> int:
    out, base, name, password, cvr, manifest = Path(argv[0]), argv[1].rstrip("/"), argv[2], argv[3], Path(argv[4]), Path(argv[5])
    observer = argv[6] if len(argv) > 6 else "demo-observer"
    if password == "-":
        password = os.environ["DEMO_PASSWORD"]
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    def shot(page: Page, name: str, full: bool = True) -> None:
        page.wait_for_timeout(250)
        page.screenshot(path=str(out / f"{name}.png"), full_page=full)

    def main_text(page: Page) -> str:
        return str(page.inner_text("main"))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/coord")
        page.fill("input[name=name]", name)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        home = main_text(page)
        check("logged in", "Logged in." in home)
        check("home: the audit center is replayed from the mirror, nothing applied yet", "0 of 1 applied" in home, home[home.find("Audit center") :][:400])
        check("home: nothing from Delta yet", "418 batches" not in home)
        shot(page, "01-home")

        def upload(path: Path) -> str:
            page.goto(base + "/coord/upload")
            page.set_input_files("input[name=file]", str(path))
            page.click("button[type=submit]:has-text('Upload and check')")
            return main_text(page)

        text = upload(cvr)
        check("upload: Delta's redacted CVR, detected", "Redacted CVR for Delta" in text, text[:400])
        check("upload: the redaction check passes", "PASS" in text and "19553" in text, text[:800])
        shot(page, "02-cvr")
        text = upload(manifest)
        check("upload: Delta's manifest, detected and accepted", "Ballot manifest for Delta" in text and "accepted" in text, text[:400])
        shot(page, "03-manifest")

        page.goto(base + "/coord/targets?county=Delta")
        before = page.inner_text("#target-matching")
        for contest, candidate in ((SENATE, "John W. Hickenlooper"), (HOUSE, "Lauren Boebert")):
            row = page.locator("table.targets-grid tr", has_text=contest)
            box = row.locator("label.target-choice", has_text=candidate).locator("input[name=v]")
            if not box.is_visible():
                row.locator("details.target-more summary").click()
            box.check()
            page.wait_for_timeout(600)
        matching = page.inner_text("#target-matching")
        check("targets: the count follows the boxes", matching != before and int(matching) > 5, {"before": before, "after": matching})
        row = page.locator("#draw-row")
        row.locator("input[name=size]").fill("5")
        row.locator("input[name=seed]").fill(TARGET_SEED)
        row.locator("#draw-button").click()
        page.wait_for_load_state()
        text = main_text(page)
        check("targets: 5 ballots drawn into the working list", "Draw 1: 5 ballots added to the working list" in text and "The working list now: 5 ballots" in text, text[:600])
        shot(page, "04-targets")
        page.locator("form[action$='/coord/targets/send'] button[type=submit]").click()
        page.wait_for_load_state()
        text = main_text(page)
        check("targets: Targeted list 1 sent to the county", "Targeted list 1 sent to the county" in text, text[:600])
        shot(page, "05-targets-sent")

        page.goto(base + "/coord/map")
        text = main_text(page)
        check("map: CORLA's upload hashes not yet posted", "CORLA's upload hashes: not yet posted" in text and "has not posted the county's manifest yet" in text)
        shot(page, "06-map-before")

        page.goto(base + "/coord")
        page.click("form[action$='/coord/fetch'] button[type=submit]")
        for _ in range(120):
            page.goto(base + "/coord")
            home = main_text(page)
            if "1 of 1 applied" in home and "running" not in home[home.find("Audit center") :][:600]:
                break
            page.wait_for_timeout(500)
        check("fetch now: the first state applied, finished", "1 of 1 applied" in home and "finished" in home, home[home.find("Audit center") :][:500])
        check("home: Delta's manifest and CORLA's imports", "418 batches, 19553 ballot cards" in home and "CVR imported" in home)
        shot(page, "07-fetched")
        page.goto(base + "/coord/map")
        text = main_text(page)
        check("map: 63 manifests", "manifests (63 counties" in text)
        check("map: Delta's manifest identical to the audit center's and to CORLA's import",
              "byte-identical to the audit center's posted manifest" in text and "Modified 2020 General Ballot Manifest.csv" in text)  # fmt: skip
        check("map: Delta's CVR identical to CORLA's import", "byte-identical to the file CORLA recorded importing (Modified CVR_Export_20201113093409.csv" in text)
        shot(page, "08-map-after")

        page.goto(base + "/coord/seed")
        check("seed: none yet", "No seed entered yet." in main_text(page))
        form = page.locator("form[action$='/coord/seed/set']")
        form.locator("input[name=seed]").fill(SEED)
        form.locator("input[name=source]").fill(SEED_SOURCE)
        form.locator("button[type=submit]").click()
        text = main_text(page)
        check("seed: entered and in effect", "Seed entered and in effect" in text and SEED in text)
        shot(page, "09-seed")

        page.goto(base + "/coord/extract")
        form = page.locator("form[action$='/coord/extract/build']")
        form.locator("select[name=county]").select_option("Delta")
        form.locator("button[type=submit]").click()
        text = main_text(page)
        ballots = re.search(r"(\d+) ballots", text[text.find("Delta") :])
        check("extract: built, in the predicted order", "Extract built and recorded." in text and "predicted order" in text, ballots.group(0) if ballots else text[:500])
        shot(page, "10-extract")
        with page.expect_download() as info:
            page.locator("a:has-text('Download PDF (portrait, two columns')").first.click()
        pdf = info.value
        target = out / pdf.suggested_filename
        pdf.save_as(str(target))
        check("extract: the portrait-2 PDF downloads", target.read_bytes()[:5] == b"%PDF-", pdf.suggested_filename)

        page.goto(base + "/coord/links")
        form = page.locator("form[action$='/coord/links']")
        form.locator("select[name=role]").select_option("observer")
        form.locator("input[name=name]").fill(observer)
        form.locator("select[name=county]").select_option("Delta")
        form.locator("button[type=submit]").click()
        link = page.inner_text("#new-link").strip()
        check("links: an observer link", "/observe/" in link, link[: link.find("/observe/") + 9])
        shot(page, "11-link", full=False)

        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        obs = phone.new_page()
        obs.on("pageerror", lambda e: errors.append(str(e)))
        obs.goto(link)
        obs.wait_for_selector("#obs-intro:not([hidden])", timeout=20_000)
        check("observer: the notice first, contest by contest chosen", obs.get_attribute("#obs-intro-mode-per_contest", "aria-pressed") == "true")
        shot(obs, "12-observer-1-notice")
        obs.click("#obs-intro-start")
        obs.wait_for_selector("#pc:not([hidden])", timeout=20_000)
        data = obs.evaluate("fetch(location.pathname + '/data.json').then(r => r.json())")
        first = data["ballots"][0]
        check("observer: the first ballot, contest by contest", obs.text_content("#pc-id") == first["id"], first["id"])
        shot(obs, "12-observer-2-first-ballot")

        def classes(row: int) -> str:
            return str(obs.evaluate(f"document.querySelector('.pc-row[data-row=\"{row}\"]').className"))

        obs.click('.pc-row[data-row="1"] .pc-main')
        obs.click('.pc-row[data-row="2"] .pc-main')
        votes = first["votes"]
        expected = votes[2][1]
        other = next((c for c in data["choices"][votes[2][2]] if c != expected and c[:1].isalpha()), None)
        obs.keyboard.type(other.split()[0][:4] if other else "zz", delay=40)
        obs.wait_for_selector(".pc-editor")
        shot(obs, "12-observer-3-heard-instead-typing", full=False)
        index = obs.evaluate(f"[...document.querySelectorAll('.pc-option')].findIndex(o => o.textContent === {json.dumps(other)})")
        active = obs.evaluate("[...document.querySelectorAll('.pc-option')].findIndex(o => o.classList.contains('is-active'))")
        for _ in range(max(0, index - active)):
            obs.keyboard.press("ArrowDown")
        obs.keyboard.press("Enter")
        obs.wait_for_timeout(200)
        check("observer: heard instead, in contest 3", "is-heard" in classes(3), {"contest": votes[2][0], "expected": expected, "heard": other})
        obs.click('.pc-row[data-row="4"] .pc-unclear')
        obs.wait_for_timeout(200)
        check("observer: not heard clearly, in contest 4", "is-unclear" in classes(4), classes(4))
        obs.click('.pc-row[data-row="5"] .pc-main')
        for _ in range(100):
            if obs.text_content("#pc-send") == "all sent":
                break
            obs.wait_for_timeout(100)
        check("observer: every entry sent", obs.text_content("#pc-send") == "all sent")
        check("observer: contests 1, 2 and 5 confirmed", all("is-confirmed" in classes(n) for n in (1, 2, 5)))
        shot(obs, "12-observer-4-entered")
        options = obs.evaluate("[...document.querySelectorAll('#obs-list option')].map(o => [o.value, o.textContent])")
        check("observer: the lists offered", any("Targeted list 1" in t for _, t in options), options)
        target = next(v for v, t in options if "Targeted list 1" in t)
        obs.select_option("#obs-list", target)
        obs.wait_for_selector("#pc:not([hidden]), #obs-current:not([hidden])", timeout=20_000)
        obs.wait_for_timeout(500)
        check("observer: the targeted list, marked as such", "TARGETED (NOT RLA)" in (obs.inner_text("body") or ""))
        shot(obs, "13-observer-targeted")

        page.goto(base + "/coord/stats")
        text = main_text(page)
        check("stats: the observer is listed", f"observer:{observer}" in text, text[:600])
        shot(page, "14-stats")
        page.goto(base + "/")
        text = page.inner_text("main")
        check("dashboard: Delta takes part", "Delta" in text and "redacted CVR provided" in text, text[:600])
        shot(page, "15-dashboard")
        check("no script errors", not errors, errors)
        browser.close()

    print(json.dumps(checks, indent=2))
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
