# Walkthrough: a coordinator sets up one county from scratch

A script for a training video: one coordinator, one county, two observers, on a laptop. The county is Testland, a fictional county with random votes (`corla_observer.testcounty`), so everything on screen is safe to show. Every step below was done as written, on a local server; the screenshots named in each step were taken then, at desktop size (1366 x 900), by `scripts/walkthrough_screenshots.py`. They are not in the repository: run that script to make them (see "Making it again" at the end).

The observers' side of the same session is in [observer.md](observer.md).

Total: about 17 minutes of video. Times are rough, for narration at a calm pace.

## What a real run differs in

The demo workspace holds Testland alone: no real election's files are loaded, and none are needed, since a test county is self-contained (its own manifest, redacted CVR, pull list, targeted contest, sample count and, if no seed has been entered, seed). For the real election the workspace is the general election's: the county is a pilot county (its files' county is detected, so there is no county to choose), its prediction is made by the scheduler a few minutes after the seed is entered (from the audit center's target list and manifests), and the evidence is stamped, signed and pushed (step 15).

## Steps

### 0. What you need (1 min)

- A Linux or macOS laptop with `git`, [uv](https://docs.astral.sh/uv/), Chrome or Firefox, and for the page check `tesseract` (with English data) and `zbarimg` (`apt-get install tesseract-ocr tesseract-ocr-eng zbar-tools`). `pdftoppm` (poppler) is only for making the screenshots.
- A clone of this repository:

```
git clone https://github.com/nealmcb/corla-observer
cd corla-observer
uv sync
```

Narration: "Everything here runs on one laptop. We use Testland, a made-up county with random votes, so nothing on screen is real."

### 1. Create the workspace (1 min)

One command does steps 1 to 3:

```
scripts/demo_setup.sh ~/demo/testland
```

It prints the coordinators' names and password and how to start the server. What it does, step by step, for the video:

```
uv run corla-observer election init --workspace ~/demo/testland/ws
```

`election init` writes `election.toml`: the 2026 general election's calendar, Audit Center address, pilot counties and results source, as on the real server. You should see `wrote .../election.toml`; `uv run corla-observer election show --workspace ~/demo/testland/ws` prints it. Nothing else is loaded: Testland needs no real election's data.

Narration: "A workspace holds everything for one election: every file, kept by its hash, and a log of every action."

### 2. Add a coordinator account (30 s)

```
uv run corla-observer user add alice --workspace ~/demo/testland/ws
```

It asks for a password (at least 12 characters) twice and prints `added coordinator:alice`. (For a script, `--password-stdin` reads it from standard input instead.) The helper also adds `bob`, a second coordinator, for the optional second seed entry. Accounts live in the workspace's `accounts/` directory, as salted hashes only.

Narration: "Each coordinator has their own login; everything they do is recorded under their name."

### 3. The county's files (20 s)

```
uv run corla-observer practice test-county --out ~/demo/testland/testland
```

It writes Testland's ballot manifest, redacted CVR (940 ballot cards, four ballot styles of 27 to 60 contests) and pull list (21 ballots for audit board 1). In a real audit the county sends these: the manifest and redacted CVR before the roll, the pull list after it.

Narration: "These stand in for the files a county clerk sends us."

### 4. Start the server (20 s)

```
~/demo/testland/serve.sh
```

which runs `uv run corla-observer serve --workspace ~/demo/testland/ws --host 127.0.0.1 --port 8765 --secret-file ~/demo/testland/secrets/server-secret`. You should see `serving on http://127.0.0.1:8765/` and the receipt key's fingerprint. Leave it running.

Screenshot: `01-dashboard-before.png`, the public dashboard at http://127.0.0.1:8765/ before anything arrives: Election "not yet known", Seed "not yet entered", the data map "nothing blocking", "No county is taking part in observation yet.", and under **Not yet expected** the 2026 general election's calendar (the target contest list, the manifests, and the redacted CVRs and pull lists of its pilot counties, La Plata, Morgan and Weld).

Narration: "The server is running. This is the public dashboard; it needs no login. It shows the real election's calendar; our practice county stays off the public pages."

### 5. Log in (20 s)

Click **Coordinators** (top right), type the name `alice` and the password, click **Log in**.

Screenshots: `02-login.png`; `03-coordinator-home.png`: the buttons **Upload files**, **Flags: 0 open**, **Data map: 0 problems**, **Estimates**, **Schedule**, the **Audit boards** form, and the **Audit center** box with **Fetch now**.

Don't click **Fetch now** in the demo: it crawls the real Secretary of State's site.

You stay logged in if you close the browser, for up to 12 hours (2 hours without use); **Log out** ends it, and so does restarting the server.

Narration: "Coordinators log in here. The menu along the top has every coordinator page."

### 6. Upload the manifest and the redacted CVR (1 min 30 s)

Click **Upload files**. Under **Files** choose both `Testland_BallotManifest_TEST.csv` and `Testland_CVR_Export_TEST.csv`, then click **Upload and check**.

The next page (`04-files-choose-county.png`) says "2 files wait for a choice", and for each file what was detected: "Ballot manifest" (or "Redacted CVR") "for county not known", with the evidence: its County column, or the CVR's first row, names "Testland, a fictional test county (practice only)". A test county's files are used only when it is chosen: choose **Testland** in **County** (it is offered first) and click **Upload as chosen**. The next page shows the manifest "accepted" and the CVR still waiting: choose **Testland** for it too, and **Upload as chosen**.

Screenshots: `04-files-chosen.png` (the two files chosen), `04-files-choose-county.png`, `05-cvr-accepted-redaction-pass.png` (below).

Narration: "We upload the county's files. The tool works out what each file is from its contents. Testland is a practice county, so we confirm it by choosing it; a real county's files name their county."

### 7. The redaction check (1 min)

Screenshot: `05-cvr-accepted-redaction-pass.png`: "Redacted CVR for Testland (chosen)", its SHA-256, and **Redaction check: PASS**, with counts only: 940 rows read, 937 ballots grouped, 3 rows already redacted, grouped by contest pattern, precinct and ballot type, 5 contest patterns ("the different sets of contests on a card: a card with a contest blanked out by the county's redaction has a pattern of its own, so this can be more than the ballot styles"; Testland's 12 cards with one contest redacted are the fifth), 8 groups, "Rare groups (under 10): 0, holding 0 ballots".

A CVR that failed would be deleted at once and never stored; only its hash and these counts would be recorded.

Click **Back to status**: the coordinator home now has **Test counties (practice only)**, with Testland's redacted CVR (PASS, with its counts) and manifest received (`06-home-testland.png`). Its **Counties** section says "Nothing has arrived from any real county yet."

Narration: "Before the redacted CVR is kept, it is checked: no group of fewer than ten identical ballots may remain. It passed. A file that fails is thrown away, never stored."

### 8. Record the audit board count (30 s)

In **Audit boards**, choose **Testland**, type `1` in **Number of audit boards**, and in **Note** where the number came from ("Testland's clerk, by phone"). Click **Record**. (Tick **Placeholder: not yet known** if the county hasn't said yet.)

Screenshots: `07-audit-boards-filled.png`, `08-audit-boards-recorded.png` ("Recorded 1 audit board for Testland.").

Narration: "We record how many audit boards the county will use; the extract is split among them."

### 9. Enter the seed, and the optional second entry (1 min 30 s)

Click **Seed**. It says "No seed entered yet." (`09-seed-none.png`). Under **Enter the seed**, type the 20 digits announced at the roll (the demo uses `41368579216342078921`) and in **Source** where it was announced. Click **Enter**.

Screenshot: `10-seed-entered.png`: "Seed entered and in effect.", the seed with the badge "entered", "Entered by coordinator:alice at ..." and the source.

The second entry is optional and must be by another coordinator: log in as `bob` (another browser, or log out first), click **Seed**, and under **Check the seed (optional)** type the seed from the announcement or your own notes (not from the screen), and where you got it. Click **Check**.

Screenshot: `11-seed-confirmed-by-bob.png`: "The second entry matches the seed in effect: the seed is confirmed.", badge "confirmed". A different value would be a high-severity flag.

Narration: "When the seed is announced, one coordinator enters it. A second coordinator can enter it independently; the tool compares the two."

### 10. The predicted extract (45 s)

Before the county's pull list arrives, the extract can already follow the predicted selections. Click **Extracts**. Under **Build an extract** choose **Testland** (leave **Audit boards** and the sample count empty) and click **Build and record** (`12-extract-build.png`).

Screenshot: `12-extract-predicted.png`: "Extract built and recorded.", and under **Testland**: "Pull list: not yet", "audit boards: 1", the short identification `Testland · extract 524b85f9`, badges "predicted order" and "practice", "48 ballots", and the four PDF layouts with their page counts.

For Testland the prediction is its own: its targeted contest (Testland County Commissioner, District 1), a fixed sample count of 50 (**A test county's sample count** changes it), and the seed in effect, drawn from its own manifest by CORLA's method; 50 draws pick 48 different ballots. For a real county the scheduler predicts from the audit center's target list and manifests.

Narration: "Once the seed is in, the tool can predict which ballots will be drawn, and build a first extract from them, marked as predicted."

### 11. Upload the pull list (30 s)

When the county sends its pull list (CORLA's "List of ballots to audit"), upload it as in step 6: **Upload files**, choose `Testland_ballots_to_audit_TEST.csv`, **Upload and check**, choose **Testland**, **Upload as chosen**.

Screenshot: `13-pull-list-accepted.png`: "Pull list for Testland (chosen)", its columns (Imprinted ID, Audit Board, County, Ballot Type, Storage Location).

Narration: "After the roll, the county sends its list of ballots to audit. We upload it the same way."

### 12. Build the extract from the pull list (45 s)

Click **Extracts**, choose **Testland** and click **Build and record** again.

Screenshot: `14-extract-built.png`: "Pull list: received", "audit boards: 1", the short identification `Testland · extract a7898e7d` (it differs if the files do), badges "pull list order" and "practice", "21 ballots". The table has, for board 1, the four PDF layouts with their page counts (8 for portrait two columns), and **text**, **provenance** and **CSV**. No prediction is needed for this: the pull list and the redacted CVR are enough.

Narration: "The extract lists each ballot the board will pull, in pull order, with the votes the CVR says it has. It's built from the pull list and the redacted CVR, and recorded with a hash of everything that went into it."

### 13. Download and print the PDF (1 min)

Click **Download PDF (portrait, two columns, the default)**. You get `testland-board1-portrait-2-a7898e7d.pdf`, 9 pages: the provenance sheet, then pages 1 to 8.

Screenshots: `15-printout-1.png` (the provenance sheet) and `15-printout-2.png` (page 1: the header line `Testland · Board 1 · page 1 of 8 portrait-2 · extract a7898e7d · page 90ec6439`, its QR code, and two columns of ballots, each with its imprinted ID, storage location and a box beside each expected vote; a contest with several choices, such as the school board's three seats, has each on its own line).

Print it at **actual size (100%)**, not "fit to page", one-sided. Give each observer who wants paper a full copy for their board.

Narration: "Observers who prefer paper get a printout. Print it at actual size. Every page carries its own identification and a QR code."

### 14. The provenance sheet (30 s)

The first sheet of each printout is the provenance sheet. Keep it with the printout: it identifies this printout (the seed, the extract's hash and the hash of every input). At its top it says: "KEEP OUT OF SIGHT of the audit board and the public: the audit board must not see the expected votes." The **provenance** link on the Extracts page shows the same on screen (`16-provenance-page.png`).

Narration: "The first sheet says exactly which data this printout came from. Just keep it with the pages. And keep the printout out of the audit board's sight."

### 15. Commit to the evidence (1 min, terminal)

Before the first ballot is read, commit to the evidence. For the demo, into a local git repository, without the public services:

```
git init ~/demo/testland/evidence-repo
uv run corla-observer commit-evidence --workspace ~/demo/testland/ws --election 2026/general --out-repo ~/demo/testland/evidence-repo --actor coordinator:alice --no-ots --no-cosign --no-push
```

You should see `2026/general/observerfiles/evidence/SHA256SUMS.txt: version 1, 0 files`, the event log's head, `git_commit: DONE`, and `opentimestamps`, `sigstore` and `push` each `SKIPPED`. It lists no files because a test county's files and extracts are never part of a commitment; in a real audit it lists every file of the evidence, the counties' extracts among them.

The real run leaves out `--no-ots --no-cosign --no-push`: the SHA256SUMS file is stamped with OpenTimestamps (a Bitcoin timestamp), signed with Sigstore, and pushed to the public mirror, with the project lead's approval for the public services (docs/TIMESTAMPING.md, docs/RUNBOOK-2026-GENERAL.md).

Narration: "Before the audit starts, we publish a fingerprint of all the evidence, so nothing can be changed afterwards without it showing."

### 16. Make the observer links (1 min 30 s)

Click **Links**. For each person: under **For** choose **an observer (enters what they hear)** (or **a practice reader (acts as the audit board)**), type the **Name** (for an observer it becomes their observer ID, such as `observer:ann`), choose **County** Testland, leave **Audit board** 1, click **Make the link**.

Make three: `ann` (she'll use her phone), `ben` (his laptop), and the reader `rita` (for practice: she plays the audit board).

Screenshots: `17-link-form.png`; `18-link-made.png`: "New observer link: observer:ann, Testland board 1", "Copy it now and send it to ann: it is shown only once", the link (starting `http://127.0.0.1:8765/`, as this page was reached; on the real server, `https://`), and **Copy to clipboard**; `19-links-list.png`: the three links, `observer:ann`, `observer:ben` and `rita` as a practice reader, with county and board, time made, and **Revoke**.

`scripts/demo_setup.sh DIR --through links` makes the same three links from the command line.

Narration: "Each observer gets their own link. It's shown only once, so copy it right away."

### 17. Hand the links to the observers (30 s)

Send each link privately to its person (a text message or email to that person only); it works like a password: whoever has it can enter observations under that name. Tell each observer: open it on the device you'll use for the whole session, in one browser; bring it charged. To replace a link, make a new one and **Revoke** the old one: whoever opens a revoked link is told that it no longer works and to ask their coordinator for a new one.

Narration: "Send each link only to its owner. Anyone with the link can enter observations under that name."

### 18. During the audit: the statistics (1 min)

The public dashboard (`20-dashboard-during-audit.png`) shows each real county's boards' progress live; a test county is kept off it. For Testland, watch **Observation** (`/coord/stats`), which reloads itself every 15 seconds.

Screenshot: `21-observation-stats.png`: **Boards**: "Testland, 1 (test county)", 21 ballots, read by the practice reader, entered, matches, with notes, observers (and how many contest by contest); **Observers**: for `observer:ann` and `observer:ben`, the entry mode, ballots entered, changed later, matches, notes and the median time per ballot. Under it, opened when wanted: **Timing** (the two taps, the slowest ballot), **Ballots read out of order, and IDs not on the list**, and, when there are any, the practice scores and the contest-by-contest figures. Then **Comparing observers**: each ballot where an observer recorded a difference.

Narration: "While the audit runs, this page shows each observer's progress: how many ballots, how many matches and notes."

### 19. Check a photographed printout page, or an observer's screen (1 min)

To confirm that a printed page is genuine, click **Check a page**, then under **Choose a photo or file** pick a photo of one printed page (or, on a phone, **Take a photo**), and click **Check this page**.

Screenshots: `22-check-a-page.png`; `23-check-result.png`: "Testland · Board 1 · page 1 of 8 portrait-2 · extract a7898e7d", **MATCH**, "Located by its QR code", "120 lines matched, 0 differ, 0 unread", "this page is from a practice extract", and "not yet committed" (a Testland page is never in a commitment), then each line, printed and as read.

A screenshot or photo of an observer's screen is checked the same way: `23-check-screen-result.png`, a screenshot of ann's phone showing her first ballot: "Testland · Board 1 · ballot 1 of 21 · 101-1-8 · extract a7898e7d · observer screen", **MATCH**, located by the ballot's QR code on the screen, with the contests shown on it checked ("contests 1 to 10 of 47 are on this screenshot and were checked; the others are not shown").

Narration: "Take a photo of any printed page, or a screenshot of an observer's screen, and the tool reads it and confirms every ID and vote on it is what was committed."

### 20. Flags (30 s)

Click **Flags**: the open change flags, most severe first, each with what to investigate and a **Mark addressed** form. In this demo there are none (`24-flags.png`, "No flags."); in a real audit, look at every high one before the first ballot.

Narration: "Flags list anything that changed when it shouldn't have, such as a manifest replaced after the roll. Each one is looked into and marked addressed."

### 21. End of the day (1 min)

- Ask each observer to save their record (**More**, then **Download my record** and **As a spreadsheet (CSV)**; observer.md, steps 15 and 18). Collect paper observers' marked printouts, initialed and dated, and photograph each page (or ask them to, on their check page).
- Commit to the evidence again (step 15).
- Revoke links that won't be used again: **Links**, **Revoke** ("Link revoked.").
- Stop the server with Ctrl-C in its terminal.

Narration: "At the end of the day, observers save their records, we collect the paper, publish the evidence fingerprint again, and revoke the links."

## Later

Not needed for one county and two observers:

- More boards: record the count, build the extract again, one link per observer per board.
- The command-line equivalents of the upload, seed and extract pages: `upload --county`, `audit-boards`, `seed set`/`seed confirm`, `extract build` (with `--sample-count` for a test county), `extract print`, `practice link` (README).
- The other printout layouts (landscape, one column) and the CSV for spreadsheet observers.
- The data map, estimates and schedule pages, and the scheduler, for the real election (docs/RUNBOOK-2026-GENERAL.md).
- Checking an observer's record against the published log: `corla-observer check-record` (observer.md, step 15).
- Targeted ballots, outside the RLA sample (docs/TARGETING.md). On **Targets**, choose the county: its statewide contests and its US House contests, tightest margin first by the results in hand (the county's own margin beside it; with no results loaded the page says so and uses the county's tally), and a search box to add any other contest to the grid. Check a candidate in one contest and another in a second (or click a split-ticket preset): the count of matching ballots changes at once, and each box shows how many would match with it checked. The pattern row below the grid reads: the pattern · the matching count · **Seed** (any text; the RLA seed in effect by default) · **Ballots** (20) · **Draw**. Each draw joins **The working list** as a source, included; so does each set of imprinted IDs typed in under it (with a note; the page says where each ballot is stored, or "not found, not added"). Uncheck a source to leave its ballots out; **Draw again** on a draw's row makes another draw of the same pattern, which skips the ballots already listed or sampled. The working list shows each ballot once, in pull order, with the sources it came from, and its text for an email. **Send to county: make Targeted list 1** freezes it: the list gets its extract hash, PDFs, CSV and the text to copy into the county's email (ballot locations only; a ballot also in the RLA sample is marked), and the working list is empty for the next one. Observers and readers keep opening on the RLA sample and switch to "Targeted list 1" with the list in their page's header, where it is marked "TARGETED (NOT RLA)" (until the county has an RLA sample, their links open on the targeted list, and say so); their "Look up a ballot" takes a ballot pulled from a box. Under **Sent lists** each list has its printouts, **Hide from observers (retire)** and **Publish details** (for after its ballots are examined: the public dashboard otherwise shows only "Targeted list 1" and its counts), and the county's allowed total is noted, with a warning when the sent lists exceed it. With the command line: `corla-observer targets draw`, `targets add`, `targets include`, `targets list` and `targets send`. To try it on real ballots, the demonstration counties ("Delta (2020 demo)", "Adams (2020 demo)", "Broomfield (2020 demo)") take the county's published 2020 CVR and manifest, uploaded with the county chosen.

## Making it again

```
scripts/demo_setup.sh DIR
DIR/serve.sh &
python3 scripts/walkthrough_screenshots.py DIR SHOTS
```

The last needs a Python with Playwright and Chrome. It does every step of this walkthrough and of observer.md and saves the screenshots under `SHOTS/coordinator/` and `SHOTS/observer/`. `scripts/demo_setup.sh DIR --through links` instead does every step through the links from the command line, for recording the observer walkthrough on its own.

## Friction found

What still doesn't work well, or is confusing, in these steps.

1. **The page check can't show a committed page with Testland.** Test counties are never committed, so the check always says "not yet committed", the committed state (with its proofs) can't be demonstrated in this demo, and `commit-evidence` lists no files.
2. **The provenance sheet is dense.** It lists ten full hashes, a key to abbreviations and two columns of shortened contest names, and its instructions for checking a page refer to recomputing hashes. For coordinators and observers the message is just "keep it; it identifies this printout". Review has found the provenance very complicated.
3. **The dashboard and the coordinator home show the real election's calendar** (its pilot counties' files "not yet expected"), unrelated to Testland, since `election.toml` is the general election's. Without it the data map reports "no targeted contests" and "no seed" as blocking problems instead.
