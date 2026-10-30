# Runbook: the November 2026 general election

What runs by itself, and what a coordinator does, from the target list on Friday 6 November to the audit on Monday 16 November and after. Times are Colorado time (MST from 1 November). The Secretary of State's 2026 calendar (updated 31 July 2026) sets the dates:

| When | What |
|---|---|
| Fri 6 Nov, 5 PM | Deadline to select the targeted contests (the target list) |
| Mon 9 Nov | Deadline for the notice of the public meeting to roll the seed |
| Wed 11 Nov, 11:59 PM | Cure deadline |
| Thu 12 Nov | Provisional and military/overseas ballots done |
| Fri 13 Nov, 2 PM | Counties finish tabulating; hashed manifests and CVRs uploaded to CORLA (10th day) |
| Mon 16 Nov | Seed rolled (13th day; past notices said 10 AM); counties notified of their selections by 11:59 PM |
| Tue 24 Nov, 5 PM | Audit board reports due |
| Wed 25 Nov | Canvass deadline |

Pilot counties: La Plata, Weld and Morgan.

## The workspace and its files

The general election has its own workspace (on the server, `~/workspaces/2026-general`), separate from the practice workspace. Its coordinator-editable files:

- `election.toml` (`corla-observer election init`, `election show`): the election (`2026/general`), the Audit Center page, the **probe URLs** (likely addresses of files not yet linked, from the 2026 primary's names under `RLA/2026/general/`), the pilot counties, the calendar, the results source (Clarity's ENR, the state election's ID once known), the low-estimate settings, and each county's **cards per ballot** (defaults from the 2024 general; Denver about 3, Adams, Alamosa, Boulder, Larimer, Pueblo and Routt about 2, the pilots 1). Each version is recorded in the event log on the next cycle.
- `schedule.toml` (`corla-observer schedule init`, `schedule show`): the crawl windows below. Read again before every decision, so edits take effect within 20 seconds.
- `overrides.toml`: the data map overrides, as before (`map override`, `map apply`).

## What runs automatically

The scheduler (`corla-observer schedule run`, the sprite service `corla-observer-schedule`) runs a **cycle** whenever a window or a sprint says so. A cycle crawls the Audit Center (with the probe URLs, at most 10 requests a second), gathers the election night results when the window says so (one small request to Clarity, more only when the results changed, at most one a second), records any new change flags, makes and records the estimates when they change, and once the seed is entered predicts again and rebuilds each county's interim extract when they would change. Each cycle is a `schedule.cycle` event; `/coord/schedule` lists the latest.

| Window | When | Every | Results |
|---|---|---|---|
| target list | Fri 6 Nov 7 PM to midnight | one crawl | yes |
| early postings | Sat 7 to Thu 12 Nov, at noon | daily | yes |
| tabulation deadline | Thu 12 Nov 6 PM to Fri 13 Nov midnight | hourly | yes |
| weekend | Sat 14 Nov to Mon 16 Nov 7 AM | 3 hours | yes |
| roll day | Mon 16 Nov 7 AM to midnight | 3 minutes | no |
| audit | Tue 17 to Wed 25 Nov | hourly | no |

Before 7 PM on 6 November the scheduler only sleeps. On a sprite it holds a Sprites task so the machine is not paused (`keep_awake = "always"` in `schedule.toml`). If the sprite was paused anyway, the first cycle after it wakes catches up. "Fetch now" on `/coord` runs one cycle at any time; so does `corla-observer schedule cycle --workspace WS`.

**When each file first appears** is on `/coord/schedule` ("When each file was first seen"), from the fetch events; the upload status file in particular.

## Before 6 November

1. `corla-observer election show --workspace WS` and `schedule show --workspace WS`: no errors, the calendar and windows as above, `idle (between windows; next starts ...)`.
2. On the sprite: `/.sprite/bin/sprite-env services list` shows `corla-observer-schedule` running.
3. Coordinators can log in to the server pointed at this workspace (the accounts were copied from the practice workspace).
4. The redacted CVRs from the pilot counties can be uploaded as soon as they arrive (`/coord/upload`); each is checked and refused if a rare group remains.

## Friday 6 November: the target list and the first estimates

The scheduler crawls once at 7 PM. Then, or after a "fetch now" once the Secretary of State posts it:

1. **The target list.** `/coord/map` lists it as `target_contests` with the targeted contests. If it was posted at an address the crawl did not reach (the Audit Center page lists its coming files in an HTML comment, without addresses), find its address and add it to `probe_urls` in `election.toml`, then fetch now. If what comes back for the Audit Center page is not that page but a firewall's refusal or challenge page, served with any status (a `fetch.blocked` event), nothing is lost: the fetcher never takes such a page for the file, the files keep their last content, and you try again later.
2. **The results.** The ENR is Clarity's, `https://results.enr.clarityelections.com/CO/<ID>/` (the 2026 primary's ID was 126592). The cycle reads the ID from the first Clarity link on the Secretary of State's results page; if none is found, `/coord/estimates` says "no election night results yet": copy the ID from the "See the results" link on https://www.coloradosos.gov/pubs/elections/resultsData.html into `[results] election_id` and fetch now, or run `corla-observer results gather --workspace WS`.
   **If the fetch is blocked or behind** (Clarity refuses the server's requests, `/coord/schedule` shows `results:` errors or `results.fetch_failed` events, or `/coord/estimates` lists results older than the results site shows), download the file in a browser and upload it:
   - **The state's file:** `reports/detailxml.zip` from the results site's Downloads (or `detailxlsx.zip`, or `summary.zip`, which has totals only). A detail file is recognized as the state's by its ballots cast for every county; for a summary, which does not say, the upload page asks.
   - **A county's own results:** its `json/en/summary.json`, for example `https://results.enr.clarityelections.com/CO/La_Plata/<ID>/<version>/json/en/summary.json` (the county's results page, then Downloads). A summary.json does not say whose it is: the upload page asks you to choose the county.

   Upload it on `/coord/upload`, with where it came from under "For a results file" (the address, and a note on who downloaded it and why), or from the command line:

   ```
   corla-observer results add detailxml.zip --workspace WS --actor coordinator:ID --source-url https://results.enr.clarityelections.com/CO/<ID>/ --note "fetch blocked; downloaded 4 Nov 9:45 PM"
   corla-observer results add summary.json --workspace WS --actor coordinator:ID --scope "La Plata" --note "La Plata's page" --file-time "2026-11-04 21:40"
   ```

   The file is identified by its content (anything else is refused, with the reason), stored by its hash, and recorded as uploaded by hand under your name. It then counts exactly like a fetched one: for each scope the newest file is used, by the time Clarity generated it (a detail file says; a summary.json does not, so give `--file-time` or the page's "When Clarity generated it", as shown on the results site, else the upload time counts), and a fetched file wins a tie. The totals check and the estimates are updated at once. Uploading needs no change to `election.toml`; the fetch carries on and replaces the upload by itself as soon as it gets a newer file.
3. **The estimates** (`/coord/estimates`, or `corla-observer estimate show --workspace WS`). For each targeted contest: the central and low round-one count, the margin and the ballot cards, each with its source. Sources rank: audit center data first (CORLA's contest list, then the manifests, then the target list), then the election night results, and inputs supplied by hand last, so a later posting replaces a hand input by itself (the contest's notes on the page say when one did). Check:
   - every pilot county's contests have a margin from the results; a county contest the state's results lack comes from the county's own results (fetched for the pilot counties);
   - the cards per ballot: each county's from the target list's remarks ("2 ballot cards per ballot*", which its diluted margins use), else from `election.toml`; a county's manifest total replaces both once posted.
   - **Inputs by hand, only when really necessary** (no published source has them yet, or a contest's name in the results does not match): there is no web form. Add them to the workspace's `overrides.toml`, each with its reason naming the evidence, from the command line (by hand or with an AI assistant, `corla-observer map show --for-ai` gives it the format), review with `map show --preview`, and apply with `map apply --actor coordinator:ID`, which records it in the event log. For a contest, as `/coord/estimates` names it: `[[estimate]]` with `contest`, and `margin` (or `winner_votes` and `loser_votes`; `final = true` if from final results), `enr_contest` (its name in the results), or `ballot_cards` (the domain's total). For a county: `[[county_estimate]]` with `county`, and `ballots_cast`, `cards_per_ballot` or `ballot_cards`. For example:

     ```toml
     [[estimate]]
     contest = "Weld County Commissioner - District 1"
     winner_votes = 41250
     loser_votes = 39875
     reason = "Weld County's results page, 4 Nov 9:40 PM"

     [[county_estimate]]
     county = "Morgan"
     ballots_cast = 13250
     cards_per_ballot = 2
     reason = "the county clerk, by phone 5 Nov: two cards per ballot this year"
     ```

     Each ranks below every audit center source and the results: the target list's margin, for instance, is used before a hand margin.
4. **The totals check** (`/coord/estimates`, "Totals check", or `corla-observer results check --workspace WS`). It runs whenever results arrive, fetched or uploaded, and again when a CVR, a manifest or CORLA's files arrive. For each targeted contest and each county with data, it compares the winner's and the loser's votes, and the ballots cast, with the other results files, the county's redacted CVR, CORLA's vote totals and contest list, and the manifests. It lists one line per difference, with the numbers; the flagged ones (normal severity) are also on `/coord/flags`.
   - **Information only:** a newer results file that differs from an older one (results move during the canvass); a CVR with fewer votes than the results when it has a row for every ballot cast (the votes it lacks were blanked by its redaction); a county not in CORLA's audit (San Juan), whose votes are left out of multi-county and statewide margins, as CORLA leaves them out.
   - **Flags, to ask the county about:** a county whose results have more votes than its CVR or CORLA's tally, since those ballots may not be in the audit's population (a late canvass results file can carry a few more votes for a county than CORLA's tally, where an earlier file matched it); CORLA's ballot card count against the manifests; a manifest with fewer cards than the ballots cast, or at one card per ballot any difference; two results files generated at the same time that differ.
5. Record each pilot county's audit board count when known (`/coord`), or a placeholder.

## 7 to 13 November

Daily, then hourly from Thursday 6 PM: watch `/coord/flags` and `/coord/map`. Manifests and the upload status appear in the last day or two before the deadline, with corrections up to roll morning. Once every county's manifest is in, the estimates' denominators become exact (`the manifests of ... (exact)`). After 2 PM Friday the results count as final and the low estimates narrow (`low_margin_factor_final`). Upload each pilot county's manifest as the county provides it.

## Before the roll: commit to the evidence

With the project lead's approval for the public services, once the pilot counties' redacted CVRs and manifests are in, and in any case well before the roll (so the Bitcoin block predates it), commit to the evidence (docs/TIMESTAMPING.md):

```
corla-observer commit-evidence --workspace WS --election 2026/general --out-repo MIRROR --actor coordinator:ID
```

Run it again when a late file arrives; each run is a new version.

## Monday 16 November: roll morning

From 7 AM the scheduler crawls every 3 minutes. To crawl more often (every minute, say, while waiting for a corrected file), start a **sprint** on `/coord/schedule` or:

```
corla-observer schedule sprint --workspace WS --actor coordinator:ID --minutes 180 --every 60
```

(`schedule stop` ends it; `--here` runs it in the terminal if the scheduler is not running.)

1. **The seed.** As soon as it is announced, a coordinator enters it on `/coord/seed` (a second coordinator may confirm it). The next cycle, within 3 minutes (or "fetch now"), predicts the selections with the current estimates.
2. **The extracts.** On `/coord/extract`, build each pilot county's extract (with its audit board count). Until the county's pull list or CORLA's posting arrives it is in **predicted order**: CORLA's pull order (storage location, scanner, batch, record), with
   - ballots beyond the low estimate marked **POSSIBLE** (drawn only if CORLA's count is above the low one);
   - ballots at each board's ends that CORLA's random split may give to the next or previous board listed on both boards, marked **BOUNDARY: may be Board N's ballot. If this board doesn't pull it, skip it.** On the observer's screen, "Skipped: other board" settles one with a tap.

   From then on, each cycle rebuilds an interim extract when the prediction changes (a corrected manifest, CORLA's contest list with the real counts). Print only when the order is final, or print and mark the version.
3. **Commit to the extracts** before the first ballot is read: `commit-evidence` again.
4. **The county's pull list**, when it arrives: upload it (`/coord/upload`; it is recognized as a pull list by its columns) and build the extract again. It replaces the interim extract, with the county's own order and boards, and no BOUNDARY or POSSIBLE marks. If CORLA's posted selections came first, they define the extract and the pull list is checked against them (and gives its order and boards when it lists the same ballots). Any disagreement is the high-severity flag `extract_sources_disagree`: compare the extract's `checks.other_source` with both lists before the audit starts. Commit to the evidence again.
5. **CORLA's contest list** for the round (with `contestSelection.csv`, after the roll) gives CORLA's counts: the estimates page then shows CORLA's count beside the estimate, and the prediction uses it.

What to check on roll morning: `/coord/flags` (every high flag addressed before the first ballot: a manifest changed after the roll, an upload after the roll, a manifest that is not the one CORLA imported, the seed); each extract's order, its counts of POSSIBLE and BOUNDARY ballots (`extract show`), and its checks (`not predicted`, `predicted not listed`).

## After the roll

Hourly crawls until 25 November pick up each round's files (each round's contest list gives its own round's target) and CORLA's reports. Commit to the evidence at the end of each audit day.

## The server

The server (a Fly.io sprite, which has no systemd: long-running programs are its "services") runs two:

- `corla-observer`: the web server (`~/bin/corla-observer-serve.sh`), on port 8080 under its unguessable path prefix;
- `corla-observer-schedule`: the scheduler (`~/bin/corla-observer-schedule.sh`), for `~/workspaces/2026-general`.

```
/.sprite/bin/sprite-env services list
/.sprite/bin/sprite-env services restart corla-observer-schedule
```

To point the web server at the general election's workspace, change `--workspace` in `~/bin/corla-observer-serve.sh` to the general election's workspace (`~/workspaces/2026-general`) (and the `--county` options to the pilot counties), then restart the `corla-observer` service. The practice workspace stays as it is.

## Rehearsing it

`corla-observer walkthrough init --workspace DIR --county Weld` makes a workspace that replays the July 2026 primary in the general election's order, one step at a time from `/coord/walkthrough` (docs/REHEARSAL.md).
