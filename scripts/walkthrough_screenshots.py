#!/usr/bin/env python3
"""Walk through the coordinator and observer walkthroughs in a real browser, saving each step's screenshot.

Written 2026-10-26 to verify, step by step, the two walkthrough scripts in
docs/walkthroughs/ (coordinator-single-county.md and observer.md), meant to
become training videos, and to make their screenshots again whenever the
pages change. It drives Chrome (Playwright, with ``channel="chrome"``; not
a dependency of corla-observer: run it with a Python that has ``playwright``
installed) against a local server for a workspace made by
``scripts/demo_setup.sh DIR`` (its default stage, before any upload):

    scripts/demo_setup.sh DIR
    DIR/serve.sh &
    python3 scripts/walkthrough_screenshots.py DIR OUT

Coordinator steps, at desktop size, into OUT/coordinator/NN-name.png: log in
as alice, upload Testland's manifest and redacted CVR together (choosing the
county for each, since a test county is used only when chosen), record one
audit board, enter the seed, confirm it as bob, build the predicted extract
(Testland's own prediction: no pull list yet), upload the pull list, build
the extract again from it, download the PDF, show the provenance, commit the
evidence locally (no OpenTimestamps, Sigstore or push), make two observer
links and a reader link. Observer steps, into OUT/observer/: ann on a
phone-size browser and ben on a laptop-size one, each a separate browser
profile (so the device key and the queued entries survive reloads and
restarts, as on a real device), with the reader rita: the notice before the
first ballot, the first view, More, two-tap matches, a note, a correction of
an earlier ballot, entries queued offline (Chrome's network emulation
through the DevTools protocol) and sent on reconnecting, a reload and a
browser restart mid-ballot, a second device for ben, an out-of-order ballot
and an ID not on the list, downloading the record, the check page with a
"photo" (the PDF's first ballot page rasterized), contest by contest on ben's
laptop, and the end of the session. Then the coordinator's statistics, the
page check (of the printed page, and of a screenshot of ann's screen) and
flags.

Every step makes real entries in the workspace's event log: use a demo
workspace only. Text the pages show at a few steps is printed, to check the
walkthroughs' "What you see" lines against.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright  # type: ignore[import-not-found]

SIZES = {"phone": (390, 844), "desktop": (1366, 900)}
PASSWORD_DEFAULT = "testland-demo-only"
SEED = "41368579216342078921"
REPO = Path(__file__).resolve().parent.parent
Shot = Callable[[str, bool], None]


def cli(*args: str) -> str:
    """Run corla-observer from this checkout and return its standard output (and error)."""
    done = subprocess.run(["uv", "run", "--quiet", "corla-observer", *args], cwd=REPO, capture_output=True, text=True)
    print(f"$ corla-observer {' '.join(args)}\n{done.stdout}{done.stderr}")
    return done.stdout + done.stderr


class Run:
    def __init__(self, setup: Path, out: Path, base: str, password: str) -> None:
        self.setup, self.out, self.base, self.password = setup, out, base.rstrip("/"), password
        self.ws = setup / "ws"
        self.files = setup / "testland"

    def context(self, play: Any, profile: str, size: str) -> BrowserContext:
        width, height = SIZES[size]
        context: BrowserContext = play.chromium.launch_persistent_context(
            str(self.setup / "profiles" / profile), channel="chrome", headless=True, accept_downloads=True,
            viewport={"width": width, "height": height}, device_scale_factor=1,
            is_mobile=size == "phone", has_touch=size == "phone",
        )  # fmt: skip
        return context

    def shooter(self, page: Page, folder: str, prefix: str = "") -> Shot:
        out = self.out / folder
        out.mkdir(parents=True, exist_ok=True)

        def shot(name: str, full: bool = False) -> None:
            page.wait_for_timeout(500)
            page.screenshot(path=str(out / f"{prefix}{name}.png"), full_page=full)
            print(f"[{folder}/{prefix}{name}.png]")

        return shot

    # -- coordinator --

    def login(self, page: Page, name: str) -> None:
        page.goto(self.base + "/login")
        page.fill("input[name=name]", name)
        page.fill("input[name=password]", self.password)
        page.click("button:has-text('Log in')")
        page.wait_for_load_state()

    def upload(self, page: Page, shot: Shot | None, names: list[str], prefix: str) -> None:
        """Upload files together, then choose Testland for each in turn (the others wait on the next page)."""
        page.goto(self.base + "/coord/upload")
        page.set_input_files("input[type=file]", [str(self.files / n) for n in names])
        if shot:
            shot(f"{prefix}-chosen", False)
        page.click("button:has-text('Upload and check')")
        page.wait_for_load_state()
        first = True
        while page.locator("select[name=county]").count():
            page.locator("select[name=county]").first.select_option("Testland")
            if shot and first:
                shot(f"{prefix}-choose-county", True)
            first = False
            page.locator("button:has-text('Upload as chosen')").first.click()
            page.wait_for_load_state()

    def coordinator(self, play: Any) -> dict[str, str]:
        context = self.context(play, "coordinator-alice", "desktop")
        page = context.pages[0]
        shot = self.shooter(page, "coordinator")
        page.goto(self.base + "/")
        shot("01-dashboard-before", True)
        page.goto(self.base + "/coord")
        shot("02-login", False)
        page.fill("input[name=name]", "alice")
        page.fill("input[name=password]", self.password)
        page.click("button:has-text('Log in')")
        page.wait_for_load_state()
        shot("03-coordinator-home", True)

        self.upload(page, shot, ["Testland_BallotManifest_TEST.csv", "Testland_CVR_Export_TEST.csv"], "04-files")
        shot("05-cvr-accepted-redaction-pass", True)
        page.goto(self.base + "/coord")
        shot("06-home-testland", True)

        page.goto(self.base + "/coord")
        form = page.locator("form:has(h2:has-text('Audit boards'))")
        form.locator("select[name=county]").select_option("Testland")
        form.locator("input[name=count]").fill("1")
        form.locator("input[name=note]").fill("Testland's clerk, by phone")
        shot("07-audit-boards-filled", False)
        form.locator("button:has-text('Record')").click()
        page.wait_for_load_state()
        shot("08-audit-boards-recorded", False)

        page.goto(self.base + "/coord/seed")
        shot("09-seed-none", False)
        form = page.locator("form:has(h2:has-text('Enter the seed'))")
        form.locator("input[name=seed]").fill(SEED)
        form.locator("input[name=source]").fill("Testland's practice roll, read aloud")
        form.locator("button:has-text('Enter')").click()
        page.wait_for_load_state()
        shot("10-seed-entered", True)

        bob = play.chromium.launch(channel="chrome").new_context(viewport={"width": 1366, "height": 900})
        bob_page = bob.new_page()
        self.login(bob_page, "bob")
        bob_page.goto(self.base + "/coord/seed")
        form = bob_page.locator("form:has(h2:has-text('Check the seed'))")
        form.locator("input[name=seed]").fill(SEED)
        form.locator("input[name=source]").fill("my own notes at the roll")
        form.locator("button:has-text('Check')").click()
        bob_page.wait_for_load_state()
        self.shooter(bob_page, "coordinator")("11-seed-confirmed-by-bob", False)
        print(bob_page.inner_text("main")[:400])
        bob.close()

        page.goto(self.base + "/coord/extract")
        form = page.locator("form:has(h2:has-text('Build an extract'))")
        form.locator("select[name=county]").select_option("Testland")
        shot("12-extract-build", False)
        form.locator("button:has-text('Build and record')").click()
        page.wait_for_load_state()
        shot("12-extract-predicted", True)
        print(page.inner_text("main")[:600])

        self.upload(page, None, ["Testland_ballots_to_audit_TEST.csv"], "13-pull-list")
        shot("13-pull-list-accepted", True)

        page.goto(self.base + "/coord/extract")
        form = page.locator("form:has(h2:has-text('Build an extract'))")
        form.locator("select[name=county]").select_option("Testland")
        form.locator("button:has-text('Build and record')").click()
        page.wait_for_load_state()
        shot("14-extract-built", True)
        with page.expect_download() as download:
            page.click("text=Download PDF (portrait, two columns, the default)")
        pdf = self.setup / download.value.suggested_filename
        download.value.save_as(str(pdf))
        print(f"downloaded {pdf}")
        subprocess.run(["pdftoppm", "-r", "80", "-png", "-f", "1", "-l", "2", str(pdf), str(self.out / "coordinator" / "15-printout")], check=True)
        subprocess.run(["pdftoppm", "-r", "200", "-jpeg", "-f", "2", "-l", "2", str(pdf), str(self.setup / "photo-page1")], check=True)
        page.click("a:text-is('provenance')")
        page.wait_for_load_state()
        shot("16-provenance-page", True)

        evidence = self.setup / "evidence-repo"
        if not evidence.exists():
            subprocess.run(["git", "init", "-q", str(evidence)], check=True)
        cli("commit-evidence", "--workspace", str(self.ws), "--election", "2026/general", "--out-repo", str(evidence),
            "--actor", "coordinator:alice", "--no-ots", "--no-cosign", "--no-push")  # fmt: skip

        links: dict[str, str] = {}
        for role, name, label in [("observer", "ann", "ann"), ("observer", "ben", "ben"), ("reader", "rita", "rita")]:
            page.goto(self.base + "/coord/links")
            form = page.locator("form:has(h2:has-text('Make a link'))")
            form.locator("select[name=role]").select_option(role)
            form.locator("input[name=name]").fill(name)
            form.locator("select[name=county]").select_option("Testland")
            form.locator("input[name=board]").fill("1")
            if name == "ann":
                shot("17-link-form", False)
            form.locator("button:has-text('Make the link')").click()
            page.wait_for_load_state()
            links[label] = page.inner_text("#new-link").strip()
            if name == "ann":
                shot("18-link-made", False)
        shot("19-links-list", True)
        (self.setup / "links.json").write_text(json.dumps(links, indent=1))
        context.close()
        return links

    def coordinator_during(self, play: Any) -> None:
        context = self.context(play, "coordinator-alice", "desktop")
        page = context.pages[0]
        shot = self.shooter(page, "coordinator")
        page.goto(self.base + "/coord")
        if page.locator("input[name=password]").count():  # the session outlives the browser, but not a server restart
            self.login(page, "alice")
        page.goto(self.base + "/")
        shot("20-dashboard-during-audit", True)
        page.goto(self.base + "/coord/stats")
        shot("21-observation-stats", True)
        print(page.inner_text("main")[:1200])
        page.goto(self.base + "/coord/check")
        shot("22-check-a-page", False)
        page.set_input_files("input[type=file] >> nth=1", str(next(self.setup.glob("photo-page1*.jpg"))))
        page.locator("main button[type=submit]").first.click()
        page.wait_for_load_state()
        shot("23-check-result", True)
        print(page.inner_text("main")[:500])
        page.goto(self.base + "/coord/check")
        page.set_input_files("input[type=file] >> nth=1", str(self.setup / "ann-screen.png"))
        page.locator("main button[type=submit]").first.click()
        page.wait_for_load_state()
        shot("23-check-screen-result", True)
        print(page.inner_text("main")[:500])
        page.goto(self.base + "/coord/flags")
        shot("24-flags", False)
        context.close()

    # -- observers --

    def observer(self, play: Any, links: dict[str, str]) -> None:
        def ready(page: Page) -> None:
            page.wait_for_selector("#obs-current:not([hidden]), #obs-intro:not([hidden])", timeout=20_000)
            if page.is_visible("#obs-intro"):
                # The walkthrough follows two taps first (steps 4 to 14); contest by contest, the default, comes in step 17.
                page.click("#obs-intro-mode-two_tap")
                page.click("#obs-intro-start")
            page.wait_for_selector("#obs-current:not([hidden])", timeout=20_000)

        def more(page: Page, button: str) -> None:
            page.click("#obs-more-button")
            page.click(button)
            if page.is_visible("#obs-more"):
                page.click("#obs-more-close")

        def top(page: Page) -> None:
            page.evaluate("window.scrollTo(0, 0)")

        def match(page: Page) -> None:
            page.click("#obs-match")
            page.wait_for_timeout(200)
            page.click("#obs-match")
            page.wait_for_timeout(600)

        ann_context = self.context(play, "ann-phone", "phone")
        ann = ann_context.pages[0]
        a = self.shooter(ann, "observer")
        ann.goto(links["ann"])
        ann.wait_for_selector("#obs-intro:not([hidden])", timeout=20_000)
        a("00-ann-notice", False)
        ready(ann)
        a("01-ann-phone-first-view", False)
        ann.screenshot(path=str(self.setup / "ann-screen.png"))
        ann.click("#obs-more-button")
        a("02-ann-more", False)
        ann.click("summary:has-text('How this works')")
        a("02-ann-how-this-works", True)
        ann.click("#obs-more-close")

        rita_context = self.context(play, "rita", "desktop")
        rita = rita_context.pages[0]
        r = self.shooter(rita, "observer")
        rita.goto(links["rita"])
        r("03-reader-start", False)
        rita.click("button:has-text('Next ballot')")
        rita.wait_for_load_state()
        r("04-reader-first-reading", False)

        top(ann)
        ann.click("#obs-match")
        a("05-ann-first-reading-tapped", False)
        rita.click("button:has-text('Second reading')")
        rita.wait_for_load_state()
        r("06-reader-second-reading", False)
        ann.click("#obs-match")
        ann.wait_for_timeout(800)
        a("07-ann-confirmed-next-ballot", False)

        ben_context = self.context(play, "ben-laptop", "desktop")
        ben = ben_context.pages[0]
        b = self.shooter(ben, "observer")
        ben.goto(links["ben"])
        ready(ben)
        b("08-ben-laptop-first-view", False)
        for _ in range(2):  # Enter twice per ballot on a keyboard
            ben.keyboard.press("Enter")
            ben.wait_for_timeout(200)
            ben.keyboard.press("Enter")
            ben.wait_for_timeout(600)
        top(ben)
        b("09-ben-two-matches", False)

        ann.click("#obs-match")
        ann.fill("#obs-note", "Prop 115: heard Yes/For")
        a("10-ann-note-typed", False)
        ann.press("#obs-note", "Enter")
        ann.wait_for_timeout(1000)
        match(ann)
        top(ann)
        a("11-ann-history-after-note", False)

        ann.locator("#obs-history li").nth(0).click()
        ann.wait_for_timeout(600)
        a("12-ann-changing-earlier-entry", False)
        ann.fill("#obs-note", "US Senator: heard Cory Gardner")
        ann.press("#obs-note", "Enter")
        ann.wait_for_timeout(1000)
        top(ann)
        a("13-ann-after-correction", False)
        print(ann.inner_text("#obs-history"))

        cdp = ann_context.new_cdp_session(ann)
        cdp.send("Network.enable")
        off = {"latency": 0, "downloadThroughput": -1, "uploadThroughput": -1}
        cdp.send("Network.emulateNetworkConditions", {"offline": True, **off})
        ann.wait_for_timeout(500)
        match(ann)
        match(ann)
        top(ann)
        a("14-ann-offline-queued", False)
        print(ann.inner_text("header"))
        cdp.send("Network.emulateNetworkConditions", {"offline": False, **off})
        ann.wait_for_timeout(3000)
        top(ann)
        a("15-ann-back-online-receipts", False)
        print(ann.inner_text("header"))

        ann.click("#obs-match")
        ann.wait_for_timeout(400)
        ann.reload()
        ready(ann)
        a("16-ann-after-reload", False)
        print("after reload:", ann.inner_text("#obs-pos"), "/", ann.inner_text("#obs-match"))
        ann_context.close()
        ann_context = self.context(play, "ann-phone", "phone")
        ann = ann_context.pages[0]
        a = self.shooter(ann, "observer")
        ann.goto(links["ann"])
        ready(ann)
        a("17-ann-browser-reopened", False)

        ben2_context = self.context(play, "ben-second-phone", "phone")
        ben2 = ben2_context.pages[0]
        ben2.goto(links["ben"])
        ready(ben2)
        top(ben2)
        self.shooter(ben2, "observer")("18-ben-second-device", False)
        print(ben2.inner_text("header"))
        ben2_context.close()

        ann.click("#obs-other")
        ann.fill("#obs-other-id", "1-22")
        a("19-ann-different-ballot-typed", False)
        ann.press("#obs-other-id", "Enter")
        ann.wait_for_timeout(800)
        a("20-ann-jumped-out-of-order", False)
        print(ann.inner_text("#obs-message"))
        match(ann)
        ann.click("#obs-other")
        ann.fill("#obs-other-id", "103-9-99")
        ann.press("#obs-other-id", "Enter")
        ann.wait_for_timeout(1000)
        a("21-ann-id-not-on-list", False)
        print(ann.inner_text("#obs-message"))

        ann.click("#obs-more-button")
        a("22-ann-download-record", False)
        with ann.expect_download() as download:
            ann.click("#obs-download")
        download.value.save_as(str(self.setup / download.value.suggested_filename))
        print("downloaded", download.value.suggested_filename)
        with ann.expect_download() as download:
            ann.click("#obs-download-csv")
        download.value.save_as(str(self.setup / download.value.suggested_filename))
        ann.click("#obs-more-close")

        ann.goto(links["ann"] + "/check")
        a("23-ann-check-page", False)
        ann.set_input_files("input[type=file] >> nth=1", str(next(self.setup.glob("photo-page1*.jpg"))))
        ann.locator("main button[type=submit]").first.click()
        ann.wait_for_load_state()
        a("24-ann-check-result", False)

        ann.goto(links["ann"])
        ready(ann)
        while ann.is_visible("#obs-current"):
            match(ann)
        top(ann)
        a("25-ann-all-done", True)
        rita.goto(links["rita"])
        r("26-reader-later", False)
        ben.click("#obs-mode-per_contest")
        ben.wait_for_selector("#pc:not([hidden])")
        b("27-ben-contest-by-contest", False)
        ben.keyboard.press("Enter")
        ben.wait_for_timeout(200)
        ben.keyboard.press("Enter")
        ben.wait_for_timeout(300)
        b("28-ben-two-contests-confirmed", False)
        for context in (ann_context, ben_context, rita_context):
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("setup", type=Path, help="The directory scripts/demo_setup.sh made.")
    parser.add_argument("out", type=Path, help="Where to save the screenshots.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--password", default=PASSWORD_DEFAULT)
    parser.add_argument("--only-during", action="store_true", help="Only the coordinator's pages during the audit (after a full run).")
    args = parser.parse_args()
    run = Run(args.setup.resolve(), args.out.resolve(), args.base_url, args.password)
    with sync_playwright() as play:
        if not args.only_during:
            links = run.coordinator(play)
            run.observer(play, links)
        run.coordinator_during(play)


if __name__ == "__main__":
    main()
