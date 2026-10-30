# corla-observer

Support for independent public observation of Colorado's risk-limiting audits (RLAs), which counties run with the state's CORLA software.

Status: early development, for the November 2026 general election pilots. The plan is in [docs/PLAN.md](docs/PLAN.md).

## What's here so far

- `corla_observer.sampling`: a reimplementation of CORLA's ballot selection (its SHA-256 pseudo-random number generator, sampling domains built from county manifests, and sample selection).
- `corla_observer.workspace`: a content-addressed store that keeps every accepted file once, unmodified, under its SHA-256, with atomic writes that never overwrite and a hash check on every read.
- `corla_observer.events`: the append-only, hash-chained event log, kept both in SQLite and in a plain JSONL file written ahead of the database. Its format is specified in [docs/EVENT_LOG.md](docs/EVENT_LOG.md), which includes a short standalone verifier and a worked example, so anyone can check a published log without this software.
- `corla_observer.fetch`: the audit center fetcher. It fetches the Secretary of State's Audit Center page and the files and pages it links to under the RLA path (HTTPS only, from an allow-listed host, with size limits, timeouts and retries), stores every file in the workspace and records each fetch in the event log. It compares each file with its previous fetch (pages after removing Cloudflare's per-request tokens), and records alerts when a file changes or disappears, or when two URLs serve identical content. By default it re-fetches everything, so corrected files are not missed. It makes at most 10 requests a second (`--rate`), so a crawl of an election's hundred or so files takes about ten seconds plus download time, and it halves its rate, recording that in the log, whenever the server answers 429 or 503 or with a Cloudflare challenge page.
- `corla_observer.due`: the data map's problems as of a moment. Each missing input is due at a milestone of the election's calendar (the workspace's `election.toml`, else the built-in calendar of a known election): the manifests, the counties' redacted CVRs and CORLA's upload status by the upload deadline (the 10th day after the election, 2:00 PM), the seed at the roll (the 13th day), the pull lists and CORLA's contest list and selections after the roll; election night results are only status. Before its due time a missing input is not a problem but a line in a short "not yet expected" summary with counts ("Manifests: 0 of 63 posted, due Fri 13 Nov 2:00 PM"); after it, it is a problem. Anything present but wrong is a problem at any time. The clock is the real time, or a walkthrough's simulated time (`--as-of` sets it for `map show` and `map check`). A problem shared by three or more counties is one line with the count and the county names, except for the counties followed (the pilot counties, a walkthrough's county, counties with uploads or observers). The map's pages, the dashboard, the coordinator home and the county cards use the same view.
- `corla_observer.flags`: the change flags. The data map records the election's roll time (the earliest of the seed's entry, a roll time a coordinator records, and a scheduled roll time set as an override). Every manifest, CVR, target contest list, contest list or county-ID file that is new, changed or removed after it, every manifest or CVR upload CORLA's upload status records after it or marks HASH_MISMATCH or FAILED, every manifest that is not byte-identical to the one CORLA records importing, and every manifest under another county's name or identical to another county's file is a high-severity flag; any other file change, and any change after the first round starts, is a normal one. Each comes with what to investigate. One problem is one flag: every row of the upload status about a county, every copy of a county's manifest unlike the import, and every file with the same content are each grouped into one flag that lists them, and the data map's problems leave out what a flag already reports.
- `corla_observer.classify`: recognizes each file by its content rather than its name (county manifest, contest list, target contests, selections, comparison files, upload status, CORLA's file hashes, the canonical contest list, vote totals, State and county audit reports, discrepancy report, a county's CVR or pull list, and others), with the county where there is one and the evidence used. It reads CSV in any of the encodings and header spellings seen so far, CORLA's JSON exports, and .xlsx workbooks with a small standard-library reader, which also extracts the audit reports' random seed. [docs/AUDITCENTER_FORMATS.md](docs/AUDITCENTER_FORMATS.md) surveys what each election's audit center published and how it is classified. Files it doesn't recognize are kept and reported as `unknown`.
- `corla_observer.cvr`: a reader for counties' Dominion cast vote record exports that tolerates their quirks (Excel `="..."` wrapping, quote-all exports, byte order marks, Latin-1, CRLF or LF, removed or blanked precinct columns). It gives each row's identification, precinct, ballot type and the contests on its card, whether it is marked redacted, and whether it has no vote values left or is an aggregate row combining several ballots, but never its votes.
- `corla_observer.redaction`: the redaction check. It judges a redacted CVR as provided against Colorado's rule that no group of fewer than 10 ballots identical in contests and precinct coding be identifiable. Every row that still carries vote values (a vote cell neither blank nor a redaction marker such as `*`, `X` or `Redacted`) is a ballot, including rows marked redacted in their identification or other cells, since their votes and contests are visible; only rows with no vote values left, and aggregate rows that combine several ballots (a vote value above 1, or a first cell such as `AGGREGATED` or `Redacted and Consolidated 18 Ballots`), are excluded. A ballot's style is the set of contests on its card, and ballots are grouped by style, precinct and ballot type, using precinct and ballot type when the file carries them (a `PrecinctPortion` or `BallotType` column, or a similar name, with a value on some ballot row). It passes when every group has at least 10 ballots. The report gives counts and style, precinct and ballot type information only, never votes, and the check runs in a separate process with memory and time limits.
- `corla_observer.datamap`: the election's data map, built from the event log: every fetched and uploaded file with its kind, county and election, the manifest chosen for each county (checked against the file CORLA recorded importing), the seed, the county-ID order, the targeted contests with their counties and sample counts, and every problem found, with a hint. It is adjusted only through a reviewable overrides file, applied deterministically and logged.
- `corla_observer.predict`: predicts each targeted contest's round-one draws from the data map and the seed, with CORLA's sample counts once published and estimates (CORLA's optimistic sample size plus one) before then, and compares them with CORLA's published selections.
- `corla_observer.rehearse`: re-runs the July 2026 primary audit, stage by stage, from the data gathered then: the audit center files as the public mirror held them at chosen commits, the counties' redacted CVRs and manifests through the real upload path, the overrides, the seed, the prediction and the comparison. See [docs/REHEARSAL.md](docs/REHEARSAL.md).
- `corla_observer.localtime`: Colorado time (America/Denver) with the standard library. The event log stores UTC; every time shown on the web pages and on the command line is Colorado time with its zone, `MDT` or `MST`, correct across daylight-saving changes.
- `corla_observer.uploads`: receives the redacted CVR, the county's manifest and its pull list from each county's coordinator, and the number of audit boards. Every upload is staged outside the store, hashed and size-limited; a CVR that fails the redaction check is deleted and never stored, and only its hash and the counts-only report are recorded. It also reports, per county, what has arrived and what is missing.
- `corla_observer.county_status`: each county's status in plain words, with the source of each part: whether CORLA's upload status records its manifest and CVR as imported (in Colorado time), what its coordinator has provided (the redacted CVR and its redaction check, the manifest, the pull list, the audit boards), its round from the CORLA files that name a round or from its pull list (or before the drawing, or the drawing done once the seed is entered), observation progress per board, and whether it takes part in observation. San Juan County, which counts by hand, is noted as not taking part in CORLA's audit.

The command line can check, export and summarize an event log:

```
corla-observer log verify PATH
corla-observer log export PATH DEST
corla-observer log head PATH
```

PATH is a log directory (holding `events.sqlite3` and `events.jsonl`) or an `events.sqlite3` file. `log verify` also accepts a JSONL file on its own.

It can also fetch and classify audit center files:

```
corla-observer fetch [URL] --workspace DIR
corla-observer classify FILE...
corla-observer seed StateAuditReport.xlsx
```

`fetch` starts from the Audit Center page, https://www.coloradosos.gov/pubs/elections/auditCenter.html by default, and follows its links to files and pages under `/pubs/elections/RLA/`. It stores files in the workspace DIR and records events in the log given by `--log` (by default, DIR itself); `--conditional` skips downloading files the server reports unchanged. `classify` prints each file's kind, county and evidence, and `seed` prints the "Audit Random Seed" from a State Audit Report.

Coordinators record what each county provides:

```
corla-observer upload --workspace DIR --actor coordinator:ID FILE... [--county NAME] [--kind KIND]
corla-observer audit-boards --workspace DIR --county NAME --count N --actor coordinator:ID
corla-observer status --workspace DIR
corla-observer check-redaction FILE
```

`upload` takes one or more files and works out each one's kind (`redacted_cvr`, `manifest`, `pull_list`, or `results` for a Clarity results file) and county from its content, printing the evidence (`corla_observer.intake`): a CVR's first row names its election and county, a manifest or pull list has a County column, a results file has its own region or ballots cast by county; the file name is only weak evidence, and the county is checked against the workspace's counties. `--kind` and `--county` override the detection, and are needed only when it cannot tell (it exits 2 and lists the candidates). It exits 1 if a file was rejected. Size limits: a redacted CVR up to 512 MB (CORLA's largest recorded CVR upload, in the audit center's upload status files, is Denver's 2024 general election CVR at 311 MB, and Denver's 2020 general at 306 MB), a manifest or pull list up to 16 MB, a results file up to 64 MB; on the web, up to 20 files and 1 GB in one upload. `check-redaction` checks a CVR without storing anything, and prints only counts, styles, precincts and ballot types (`--names` adds each rare style's contest names); it also lists the contest patterns shared by several ballot types, with the ballot count under each, since the ballot type column keeps them in separate groups. The workspace's `staging/` directory holds uploads only while they are checked, and must never be backed up.

The election's data map says which file is which and lists everything a sampling run still needs, and is adjusted through the workspace's `overrides.toml` (`corla_observer.datamap`):

```
corla-observer map show --workspace DIR [--for-ai] [--json] [--preview] [--as-of TIME]
corla-observer map explain FILE --workspace DIR
corla-observer map check --workspace DIR [--county NAME] [--as-of TIME]
corla-observer flags --workspace DIR [--severity high] [--county NAME] [--json] [--record --actor coordinator:ID]
corla-observer audit-time roll|round_start TIME --workspace DIR --actor coordinator:ID --source TEXT
corla-observer map override SECTION --workspace DIR --set KEY=VALUE ... --reason TEXT
corla-observer map apply --workspace DIR --actor coordinator:ID
```

An override can reassign a file's kind or county, ignore a file, supply a missing file by path or URL with its SHA-256, run a named adapter module from the workspace's `adapters/` directory to convert an unexpected layout, set the seed or the county-ID order, add manifest adjustments with their evidence, add county-name aliases, and add, change or exclude contests. Every item needs a reason. `map apply` checks the whole file and records it, with its hash and every reason, in the event log; the map is always built from the overrides last applied. `--for-ai` prints a compact, self-contained description of the map, its problems, its flags and the overrides format, to paste into an AI assistant. `flags` lists every change flag with what to investigate, and exits 1 if any is high severity; `--record` records the ones not yet recorded as events. `audit-time` records when the roll took place or the first round started (TIME is Colorado time, such as `2026-11-16 10:10`, unless it has a UTC offset).

The July 2026 primary can be rehearsed from the public mirror, all at once or stage by stage ([docs/REHEARSAL.md](docs/REHEARSAL.md)):

```
corla-observer rehearse all --workspace DIR --mirror MIRROR
corla-observer rehearse fetch|uploads|overrides|seed|predict|compare --workspace DIR ...
```

## Running an election live

The general election's workspace has an election setup, a crawl schedule and a scheduler ([docs/RUNBOOK-2026-GENERAL.md](docs/RUNBOOK-2026-GENERAL.md)):

```
corla-observer election init|show --workspace DIR [--election 2026/general|2020/general] [--mirror MIRROR]
corla-observer schedule init|show|cycle --workspace DIR
corla-observer schedule run --workspace DIR
corla-observer schedule sprint --workspace DIR --actor coordinator:ID [--minutes 180] [--every 60] [--here]
corla-observer results gather|show|check --workspace DIR
corla-observer results add FILE --workspace DIR --actor coordinator:ID [--source-url URL] [--note TEXT] [--scope state|COUNTY] [--file-time TIME]
corla-observer estimate show|record --workspace DIR
corla-observer user copy --workspace DIR --from OTHER_WORKSPACE
corla-observer walkthrough init|next|status --workspace DIR [--county Weld]
```

- `corla_observer.election`: the workspace's `election.toml`: the Audit Center page, probe URLs for files not yet linked, pilot counties, the calendar, the results source and each county's cards per ballot. `2020/general` is a stand-in for the 2026 general in demonstrations: its audit center is replayed from a clone of the public mirror (`--mirror`), one state per **Fetch now**, and the live site is never crawled ([docs/walkthroughs/demo-delta-2020-general.md](docs/walkthroughs/demo-delta-2020-general.md), set up or reset by `scripts/demo_delta_2020.sh`).
- `corla_observer.schedule`: one cycle (crawl, results, flags, estimates, and after the roll the prediction and interim extracts) under a lock, the coordinator-editable `schedule.toml`, the long-running scheduler, and sprints.
- `corla_observer.enr`: election night results from Clarity's ENR, fetched only when their version changes, or downloaded by hand and uploaded (`results add`, or `/coord/upload`) when the fetch is blocked or behind; stored by hash, the newest by the file's own generated time used.
- `corla_observer.totals`: the totals check, run whenever results arrive. It compares each targeted contest's winner and loser votes and each county's ballots cast with the other results files, the redacted CVR tallies, CORLA's vote totals and contest list, and the manifests, with one line per difference on `/coord/estimates` and a flag for each one that matters.
- `corla_observer.estimate`: each targeted contest's central and low round-one count by CORLA's formula, from the best margin and denominator at hand, each with its source: CORLA's contest list, then the manifests and the target list (its margins, denominators and per-county cards per ballot), then the election night results, then inputs supplied by hand in the overrides file (`[[estimate]]`, `[[county_estimate]]`), which a later posting replaces by itself; `scripts/validate_estimates.py` checks them against CORLA's counts.
- `corla_observer.tally`: a redacted CVR's contest totals and margins, counting aggregate rows.
- `corla_observer.pullorder`: CORLA's pull-list order and board split, reimplemented from its source.
- `corla_observer.walkthrough`: a past audit replayed step by step on the web pages (docs/REHEARSAL.md).

On the web, `/coord/estimates`, `/coord/schedule` and `/coord/walkthrough`.

## Development

```
uv sync
uv run pytest
uv run mypy
```

`uv run pytest` runs the fast tests: small synthetic fixtures, no real election data, and no network beyond a test server on the loopback interface, in a few seconds.

```
uv run pytest -m slow
```

runs the slow tests: the photo check end to end on printed pages (the printout's PDF rasterized; needs `pdftoppm`, `tesseract` and `zbarimg`), and the acceptance tests, which reproduce CORLA's ballot selections for a real election from its public files, classify real files from several elections, and run the redaction check on counties' published CVRs. The files are not in this repository: [tests/acceptance/mirror_files.json](tests/acceptance/mirror_files.json) lists each one by its path in the public audit center mirror, https://github.com/nealmcb/auditcenter, a pinned mirror commit and its SHA-256. The tests read them from a local clone of the mirror if the environment variable `CORLA_OBSERVER_MIRROR` names one, and otherwise download them from GitHub into a cache (`~/.cache/corla-observer/mirror`, or `$CORLA_OBSERVER_CACHE`). Every file is checked against its SHA-256 before use. If the files can't be obtained, the tests are skipped.

```
uv run pytest -m browser
```

drives the observer page's contest-by-contest entry in a real browser on a Testland 60-contest ballot, at phone and desktop sizes (`scripts/browser_per_contest.py`; needs a `python3` with Playwright and its Chromium, or `$CORLA_OBSERVER_PLAYWRIGHT_PYTHON` naming one, and is skipped otherwise). `$CORLA_OBSERVER_SCREENSHOTS` names a directory to keep its screenshots in.

If the environment variable `CORLA_OBSERVER_ANONYMIZER` names a local checkout of the public CVR anonymizer, https://github.com/loriinboulder/anonymize_cvr, the slow tests also compare the redaction check with that anonymizer's own check, on synthetic files and on the real CVRs. No code of the anonymizer is copied into this repository. The definitional differences between the two are listed in [tests/acceptance/test_redaction_crosscheck.py](tests/acceptance/test_redaction_crosscheck.py).

## Security

Report vulnerabilities, and privacy problems in data the tool handles or publishes, privately: see [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Web interface

`corla-observer serve` runs the web interface (`corla_observer.web`), a FastAPI app with server-rendered pages:

```
corla-observer user add NAME --workspace DIR
corla-observer serve --workspace DIR --host 127.0.0.1 --port 8080 [--path-prefix /x] [--county NAME ...]
```

- The public dashboard (`/`, no login) shows the counties taking part in observation (those that provided a redacted CVR, or have observer links or entries), each with its status in one line, such as "Round 1 · manifest and CVR uploaded to CORLA · redacted CVR provided" (`corla_observer.county_status`), and in detail: its stage (its round from CORLA's round files, or before the drawing, or the drawing done once the seed is entered), whether CORLA's upload status records its manifest and CVR as imported and when, in Colorado time, what has arrived here (a redacted CVR with its redaction verdict and counts only, the manifest, the pull list, the audit boards, a placeholder count shown as not yet known) and what has not, and its boards' observation progress; the data map's problems, recent fetch activity, the event log's head hash and count, and the seed once entered. It updates live by server-sent events, and is also available as `/dashboard.json`. `/counties` (and `/counties.json`) lists every Colorado county with its CORLA status where known (manifest and CVR imported, its round or stage) and whether it takes part in observation, with a note that San Juan County counts by hand and does not take part in CORLA's audit. Neither page shows the change flags, nor the data map problems and fetch alerts that report the same findings: many are addressed before the public would see them. Neither shows any vote.
- Coordinator pages (`/coord`, after login) upload files through the same path as `corla-observer upload` (several at once, with nothing to choose unless a file's kind or county cannot be told from its content), record audit board counts, show the data map and the `map show --for-ai` text, and start a fetch. `/coord/flags` lists the open change flags, most severe first, each with its findings expandable and a form to mark it addressed with a note (a `flag.addressed` event; `flag.reopened` reopens it), and the addressed ones separately; a flag that gains a new finding is open again. `corla-observer flags` says which are addressed, and `--open` lists only the others. Every action is recorded in the event log under the coordinator's actor ID.

`user add` asks for the password twice (or, with `--password-stdin`, reads it from the first line of standard input, for scripts) and stores only a salted scrypt hash, in the workspace's `accounts/` directory, which must not be published. A coordinator stays logged in when the browser is closed, for up to 12 hours from logging in and 2 hours without use; logging out, or restarting the server, ends the session. Session cookies are signed with a server secret kept outside the repository and the workspace (`--secret-file`, or `$CORLA_OBSERVER_SECRET_FILE`, created if missing). Cookies are always `Secure`, so outside `localhost` the site must be served over HTTPS, for example behind Cloudflare. `--path-prefix` serves everything under an unguessable path, with nothing outside it.

To practise live observation on the July 2026 primary (`corla_observer.practice`), from a clone of the public mirror:

```
corla-observer practice build --workspace DIR --county "La Plata" [--boards N] --mirror MIRROR
corla-observer practice link --workspace DIR --county "La Plata" --board 1 --observer NAME --base-url https://HOST/PREFIX
corla-observer practice link --workspace DIR --county "La Plata" --board 1 --reader NAME --base-url https://HOST/PREFIX
```

For a ballot with very many contests, a fictional test county, Testland (`corla_observer.testcounty`), has synthetic files: a manifest, a redacted CVR with random votes (from a seed, so they can be shown anywhere) and four ballot styles of 60, 47, 42 and 27 contests, and a pull list of 21 ballots for one audit board, a third of them on the 60-contest style. The longest style is modeled on the longest 2020 general election ballots (48 contests in Denver, 39 in Adams), with the 2020 statewide contests and fictional local ones, including multi-seat contests, judicial retentions, ballot issues with long titles, overvotes, ballots with no vote in some contests, and redacted ballots and contests. A test county is never a Colorado county: its files are uploaded with the county chosen, and it is kept out of the data map, the prediction, the scheduler's extracts, the evidence commitments and the public dashboard; it is self-contained: a workspace with only Testland in it works end to end, with no real election loaded. Its extract is always a practice extract, built from its own uploads only: from its pull list and redacted CVR, or, without the pull list, from its own predicted selections (the seed entered, or else its own fictional seed; its own manifest; its one targeted contest; a fixed sample count of 50, `extract build --sample-count N`), in CORLA's pull order (docs/EXTRACT.md).

```
corla-observer practice test-county --out DIR [--seed N]
corla-observer upload --workspace WS --actor coordinator:ID --county Testland DIR/*.csv   # or leave out the pull list, for a predicted extract
corla-observer extract build --workspace WS --county Testland --actor coordinator:ID
corla-observer practice link --workspace WS --county Testland --board 1 --observer NAME --base-url https://HOST/PREFIX
```

Coordinators can also make links on the web, at `/coord/links`: choose observer or practice reader, the person's name (which becomes the observer ID, `observer:NAME`), the county and the board, and the page shows the link's full URL to copy. It is shown only then, since only a hash of the token is kept. The page lists every link made, with its observer ID, county, board and time, and can revoke one.

`practice build` rehearses the primary into a new workspace, then lists each county's predicted ballots in pull order with their expected votes from the county's redacted CVR. An observer link opens a page showing one ballot at a time (its imprinted ID large, then the expected votes), with a box for what was heard instead and a button that takes two taps for a match: "First reading" on hearing the first board member read the ballot, which turns it into "Confirmed match", and that, after the second reading, records the match and moves on (Enter or space twice on a keyboard); both taps are signed and recorded with their times; entries are signed on the device with a key that never leaves it, kept on the device, queued while offline, and recorded with their timing, and each gets a receipt signed by the server, whose code the page shows by the ballot. "Download my record" saves every entry with its signature and receipt. By default an observer enters each ballot contest by contest instead (and can choose two taps on the notice before the first ballot or at the top of their page): the contests as rows, each confirmed (Enter, or a tap), noted as heard instead (type, with the contest's choices suggested) or marked not heard clearly ("?"), in the first reading and again in the second, with a prompt before each new reading or ballot, a jump to the next unresolved contest, and a "Keys" help; each outcome is a signed entry with its own receipt, sent in small batches, and the mode in effect at each start, and each change of it, is recorded (`corla_observer.contests`, [docs/RECEIPTS.md](docs/RECEIPTS.md)). A reader link opens a page for someone acting as the audit board, which prompts a first and a second reading of each ballot and can inject deliberate differences for scoring. The dashboard shows each board's progress, and `/coord/stats` shows each observer's entry mode, entries, notes, entry times, times to the first reading and from it to the confirmation, contest-by-contest outcomes and score, and compares the observers of each board ballot by ballot.

An observer's downloaded record can be checked against the published event log by anyone ([docs/RECEIPTS.md](docs/RECEIPTS.md)):

```
corla-observer check-record RECORD.json EVENTS.jsonl --server-key KEY [--anchor SEQ:HASH ...] [--json]
```

It reports entries missing from the log or differing from it, entries in the log under that observer and device that the device did not sign, a broken device chain, receipts not signed by the server key or not matching the log, and whether the log's chain is intact and agrees with every receipt and anchor. The server's receipt key is created on its first run next to the server secret (`--receipt-key-file` names another) and is published at `/receipt-key` and on the dashboard.

## Timestamping the evidence

`corla-observer commit-evidence` (`corla_observer.timestamp`) commits to the evidence publicly, as in July 2026: it writes a new, never-overwritten, versioned `SHA256SUMS` file listing every file the data map uses, the overrides, the adapters, the prediction and the extracts with their page indexes, with the event log's head and the software version in its header; stamps it with OpenTimestamps and signs it with Sigstore where `ots` and `cosign` are installed (reporting each step as done, pending, skipped or failed); commits and pushes it to a clone of the mirror; and records it in the event log. See [docs/TIMESTAMPING.md](docs/TIMESTAMPING.md) for when to run it and how anyone can check it.

```
corla-observer commit-evidence --workspace DIR --election 2026/general --out-repo MIRROR [--subdir PATH] [--actor coordinator:ID] [--no-push] [--include-files] [--cosign-key FILE]
corla-observer upgrade-proofs --out-repo MIRROR --workspace DIR [--no-push]
corla-observer verify-evidence SHA256SUMS.v2.txt [--workspace DIR] [--certificate-identity EMAIL --certificate-oidc-issuer URL]
```

## Seed entry and the extract

A coordinator's entry of the seed takes effect at once (`seed set`, `corla_observer.seeds`). A second entry by another coordinator is optional (`seed confirm`): it is compared with the seed in effect, and a mismatch is a high-severity flag, and a data map problem until the seed is entered again. The same coordinator entering it again is recorded as a re-entry, and any entry that changes the value is recorded and flagged as high severity. So is a seed on the audit center (a seed file or the State Audit Report's "Audit Random Seed") that differs from the one in effect. The roll time is taken from the first entry. A rehearsal enters its seed once, marked as rehearsal. `seed FILE` still prints a State Audit Report's seed, as `seed read FILE`.

```
corla-observer seed set --workspace DIR --actor coordinator:ID --source TEXT [SEED]
corla-observer seed confirm --workspace DIR --actor coordinator:OTHER [SEED]
corla-observer seed status --workspace DIR
```

The extract (`corla_observer.extract`, specified in [docs/EXTRACT.md](docs/EXTRACT.md)) lists each county's ballots per audit board, in pull order. Each ballot has its position, its imprinted ID, its location where known, its expected votes from the redacted CVR, and the contests it was drawn for, statewide alongside county. It is built from whichever of the county's pull list and CORLA's posted selections arrives first (the other becomes an independent check, and a disagreement is a high-severity flag) or, until then, from the predicted selections, flagged as predicted. Without a pull list, ballots are in CORLA's pull order (storage location, scanner, batch, record, `corla_observer.pullorder`), and a ballot that CORLA's random split among the boards may put on either of two boards is listed on both, marked BOUNDARY; the observer page has a one-tap "Skipped: other board" for it. In a predicted extract, ballots beyond the low estimate of the sample count are marked POSSIBLE. Its canonical serialization is stored under its SHA-256 and recorded in an `extract.built` event with the hash of every input. The practice extract and the observer page use the same structure.

```
corla-observer extract build --workspace DIR --county NAME --actor coordinator:ID [--boards N]
corla-observer extract show --workspace DIR --county NAME [--pages]
corla-observer extract print --workspace DIR --county NAME --board N [--format pdf|text|csv] [--layout portrait-2|portrait-1|landscape-2|landscape-3] [--out FILE]
```

The printout is a PDF (`corla_observer.pdfextract`), drawn in Python with ReportLab and the bundled DejaVu fonts, without a browser, so it prints the same everywhere and downloads from a phone; the same extract always gives the same bytes, and each board's PDF in each layout is stored and its SHA-256 recorded when the extract is built (docs/EXTRACT.md, "The printout"). There are four layouts, a letter sheet portrait or landscape with one, two or three columns of ballots, each sheet one page; the default is `portrait-2`, portrait with two columns. Each board's printout starts with a provenance page, not counted among its pages, which lists the seed, the extract's hash, every input's hash and the page index's hash in full. The extract's hash covers the seed and every input, so every printed page carries only, in a header band:

- one identification line: county, board, "page N of M" and the layout, "PREDICTED ORDER" when predicted, and the first 8 hex digits of the extract's hash and of the page's hash;
- a 22 mm QR code of the page's own hash, big enough to decode from a phone photo of the whole sheet.

Names are shortened only as far as needed to fit their column, measured with the fonts' real metrics, so nothing wraps. The CSV is escaped against formula injection. On the web, `/coord/seed` takes the seed and an optional second entry, and `/coord/extract` builds the extract and offers "Download PDF" for each board in each layout, its provenance page, plain text and CSV. The observer page and the practice reader show the short identification (county, board, the extract's hash, short), link to the provenance and offer the board's PDFs.

Targeted ballots, which observers choose outside the RLA's random sample and ask the county to pull (docs/TARGETING.md): on `/coord/targets`, a county's statewide and US House contests (and any other contest added by name) ordered by the results' margins, with live counts of the ballots matching the candidates checked, split-ticket presets, seeded random draws (any text as the seed) that skip ballots already listed or sampled, imprinted IDs entered by hand, the working list of included sources in CORLA's pull order, and "Send to county", which freezes it as Targeted list N (its extract, PDFs, CSV and email text) beside the county's RLA sample, which observers switch to on their page (entries kept apart, shown on the dashboards as "targeted (not RLA)"); overlaps with the RLA sample are marked both ways; a ballot picked from a storage box is looked up on the observer's screen and entered as a box-targeted observation, counted apart (`corla_observer.targets`, `corla-observer targets`). The demonstration counties "Adams (2020 demo)", "Broomfield (2020 demo)" and "Delta (2020 demo)" take those counties' published 2020 general election CVRs and manifests, kept apart from current data.

## Checking a printed page from a photo

The photo check (`corla_observer.photocheck`, [docs/PHOTO_CHECK.md](docs/PHOTO_CHECK.md)) confirms that a printed extract page, photographed or scanned, matches the committed extract: it reads the page's QR code (or its short hashes) and finds the page in any extract the workspace built, lists the evidence commitments covering it with their proofs (the Bitcoin block, the Sigstore entry, the commit), reads the printed text with tesseract, and compares every imprinted ID and choice with the page as committed. The content verdict is MATCH, MATCH WITH UNREAD LINES, MISMATCH (listing what differs) or UNKNOWN PAGE (the page could not be found at all); an unreadable line is reported as unread, never as matched. Whether the page is covered by a published, timestamped commitment yet is reported separately (committed, with its proofs, or NOT YET COMMITTED).

```
corla-observer photo check IMAGE... --workspace DIR [--actor coordinator:ID] [--json] [--no-record] [--require-committed]
```

exits 0 for MATCH, 1 for MISMATCH or UNKNOWN PAGE, 2 for MATCH WITH UNREAD LINES (`--require-committed`: 1 also for a page not yet committed). On the web, "Check a page" (`/coord/check`) takes a photo, taken with the phone's camera or chosen from those already taken, or the typed short hashes; observers and readers have the same page through their links for their own county's extracts, and `serve --public-check` opens `/check` to anyone (off by default, for after the audit). Each check is recorded as `photo.checked`, with the photo stored in the workspace without its metadata and never served publicly. It needs the system tools `tesseract` (with English data) and `zbarimg`: `apt-get install tesseract-ocr tesseract-ocr-eng zbar-tools`.
