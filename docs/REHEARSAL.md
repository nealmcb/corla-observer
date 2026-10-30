# Rehearsing the July 2026 primary

The `rehearse` commands re-run parts of the July 2026 primary audit from the data gathered at the time, through the same code a live audit uses. A rehearsal builds a fresh workspace with its own event log. Each stage reads only that workspace (and, for the first two, the mirror), so any stage can be re-run on its own after a fix.

You need a git checkout of the public audit center mirror, https://github.com/nealmcb/auditcenter, named by `--mirror` or by the environment variable `CORLA_OBSERVER_MIRROR`. The mirror's local path is never recorded, only its public URL and the commit.

## All at once

```
export CORLA_OBSERVER_MIRROR=/path/to/auditcenter
corla-observer rehearse all --workspace rehearsal-ws
```

`rehearse all` runs every stage below in order on a new, empty workspace and prints what each found. It fetches at every commit listed by `rehearse commits`, the counties are La Plata, Morgan and Weld, and it compares at the end. It exits 1 if any prediction disagrees with CORLA's.

Outcome: all seven targeted contests of those counties and the statewide contest match CORLA's published selections (compared as multisets, since CORLA's published order is not draw order):

- Attorney General - DEM (Morgan): 86 draws.
- Attorney General - REP (Morgan): 59 draws.
- Attorney General - REP (Weld): 99 draws.
- La Plata's two congressional primaries: 127 and 69 draws.
- State Representative - District 19 - DEM (Weld): 307 draws.
- United States Senator - DEM: 210 draws over 1,444,047 ballot cards.

## Stage by stage

```
corla-observer rehearse commits
corla-observer rehearse fetch --workspace WS --commit COMMIT
corla-observer rehearse uploads --workspace WS [--county NAME] [--audit-boards NAME=N]
corla-observer rehearse overrides --workspace WS
corla-observer rehearse seed --workspace WS [--seed DIGITS --source TEXT]
corla-observer rehearse predict --workspace WS [--county NAME] [--out FILE]
corla-observer rehearse compare --workspace WS
```

1. **fetch:** loads the audit center files as the mirror held them at COMMIT, through the real fetcher's code (`fetch.fetch_urls`). Each event's URL is the file's original audit center URL, its `final_url` is the mirror's raw URL at that commit, and its data carries `"simulated": {"source": "mirror", "repository": ..., "commit": ...}`. Fetching at several commits in turn replays how the files changed, with the fetcher's alerts: the first commit raises a duplicate-content alert where two counties' manifest files hold identical content, and the next ones raise "changed" alerts as files were replaced. Observers' own files and the evidence commitments (`observerfiles/`, `SHA256SUMS*`) are not fetched.
2. **uploads:** receives the counties' redacted CVRs and manifests, from the mirror's `2026/primary/observerfiles/`, through the real upload path, redaction check included (all three pass). It also records each county's audit board count. The counts are not known: each is recorded as a placeholder of 1, flagged as such, unless given with `--audit-boards`. CORLA's final Audit Activity Report shows only audit board 1 for these counties.
3. **overrides:** writes the primary's overrides to the workspace's `overrides.toml`, if it has none, and applies it:
   - a county alias for the "Prowes" row in Prowers's manifest;
   - a manifest adjustment, with its evidence, for a batch that CORLA's own comparison shows but that a published manifest does not.

   Edit the file and run `corla-observer map apply --workspace WS --actor coordinator:ID` to change them.
4. **seed:** enters the seed by hand, as on the day: `49006417086137856424`, with its source. The entry is marked as rehearsal; a single entry is enough, as on the day. The stage also records the time of the roll, about 10:10 MDT on 13 July (`--roll-time`), since the rehearsal's seed entry is not at the time of the roll.
5. **predict:** builds the data map and records the predicted draws, in draw order, for the counties' targeted contests and the statewide contest. The sample counts are CORLA's once its contest list has been fetched, and estimated from the target list before that. The prediction is stored in the workspace and recorded in a `prediction.made` event.
6. **compare:** compares the latest prediction with CORLA's published selections (`ContestMarginAndContestCVR-IDs-Round1.csv` and `CVRtoAuditBoardInterpretationComparison.csv`), once they have been fetched, and records the result in a `prediction.compared` event. Each contest is one of:
   - `match`: the same draws;
   - `consistent`: an estimated count that came out short, whose draws are all among CORLA's;
   - `mismatch`, with the draws on each side.

Every rehearsal event is marked with the time the data was really gathered: a fetch with its mirror commit's time, a county's upload with the time observers received its files. Changes are judged against the roll at those times, and `corla-observer flags --workspace WS` lists the change flags.

Between stages, `corla-observer map show --workspace WS` shows the data map, and `map check --county "La Plata" --county Morgan --county Weld` lists what is still missing or unresolved. `map show --for-ai` prints it all for an AI assistant.

## As of the dice roll

To see what could have been predicted on the day, fetch only the commits from before the roll, predict, and compare after fetching the final reports:

```
corla-observer rehearse fetch --workspace WS --commit a4a2b935445406da32f6ce01ba9bc317990c99fa
corla-observer rehearse fetch --workspace WS --commit 23af1c61f97071337b29386d3ceea675678a44f9
corla-observer rehearse uploads --workspace WS --commit f46c6630655e115bcf959f797aee42c0b35d7c86
corla-observer rehearse overrides --workspace WS
corla-observer rehearse seed --workspace WS
corla-observer rehearse predict --workspace WS
corla-observer rehearse fetch --workspace WS --commit ba18ba6874c5ed4be713b3745a5d32c261cc6083
corla-observer rehearse compare --workspace WS
```

Outcome:

- **County order:** the standard county numbering (CORLA's contest-ID file is published later).
- **Sample counts:** estimated from the target list's unofficial margins: 139 statewide against CORLA's 210, for example.
- **Statewide:** `consistent`. Its 139 predicted draws are all among CORLA's 210.
- **Five of the six county contests:** `match`, on the first CORLA-many predicted draws.
- **One county contest:** a `mismatch` of one draw, because a manifest in the data available at the roll differed from the one CORLA later recorded importing. The data map reports the copies as conflicting once the later file is fetched, and uses the one byte-identical to CORLA's import; the mechanism is in docs/AUDITCENTER_FORMATS.md.

The flags at the end of this run are the ones the mechanisms above raise on real roll-day data: manifest imports that CORLA's upload status records after the roll; uploads it marks `HASH_MISMATCH` or `FAILED`; a provided or posted copy that differs from the imported file; a posted manifest that changed after the roll; identical content under two counties' names; and CORLA's contest list and county-ID file, published after the roll. `corla-observer flags --workspace WS` lists each one, by county, with what to investigate — which is where such a finding belongs: with the coordinator and the county, not in this repository.

Even with the roll-day data, the manifest adjustment is needed for the statewide domain to be the 1,444,047 cards CORLA sampled, and its evidence was published only after the audit, in CORLA's final reports. The data map reports the shortfall before then, as soon as the contest list is fetched.

## The walkthrough: step by step, on the web pages

A walkthrough replays the primary for one county in the order of the general election's calendar, one arrival at a time, so a person can look at every page as it stood after each (`corla_observer.walkthrough`):

```
corla-observer walkthrough init --workspace WALK --county Weld [--mirror MIRROR] [--accounts-from OTHER_WS]
corla-observer user add NAME --workspace WALK          # unless the accounts were copied
corla-observer serve --workspace WALK --port 8080 [--path-prefix /x]
```

Then log in and open `/coord/walkthrough`: each "Next step" applies exactly what arrived then, and runs the normal pipeline (flags, estimates, and after the seed the prediction and the county's interim extract). Every page (the data map, the flags, the estimates, the extract and its print, an observer's screen from a link made at `/coord/links`) carries a banner saying it is a walkthrough, with the simulated time. A walkthrough never fetches from the real sites; "Fetch now" does nothing there. `corla-observer walkthrough next --workspace WALK [--steps N]` and `walkthrough status` do the same from the command line; `walkthrough init --force` starts again (keeping the accounts).

Nothing known only later is applied before its step. A walkthrough starts with no data map overrides: the alias for the "Prowes" spelling in Prowers's manifest is applied with the manifests it is about (09:14), and the manifest adjustment only with CORLA's final reports, the evidence for it; the walkthrough page notes each under its step. A county's audit board count is a placeholder that says only that it is not yet known, the seed's source is the roll's announcement (not the later State Audit Report), and a step whose title would tell what it brings shows a plain title ("Audit center update") until it is applied. The data map judges what is due by the simulated time (`corla_observer.due`): after the first step only the target list has arrived, and the rest is listed as not yet expected, not as problems.

The steps for Weld, from the mirror's history and its `observerfiles/`:

| Simulated time (MDT) | Step |
|---|---|
| Tue 7 Jul 12:33 | The target contest list is posted (commit 5108046) |
| Thu 9 Jul 16:57 | Weld uploads its manifest (16:54, 16:56) and CVR (16:57) to CORLA, as the upload status shows later: nothing arrives |
| Fri 10 Jul 17:00 | Election night results: the state's detail of 10 July (`observerfiles/SOS_ENR_detail_20260710.xlsx`; its time of day is not known) |
| Mon 13 Jul 09:14 | 61 county manifests posted before the roll (commit a4a2b93; two of them hold identical content); the Prowers alias applied |
| Mon 13 Jul 10:03 | Weld's redacted CVR and manifest, by email |
| Mon 13 Jul 10:10 | The roll: the seed entered; Weld's extract built (predicted order) |
| Mon 13 Jul 10:12 | More manifests are posted, after the roll (commit 23af1c6) |
| Mon 13 Jul 10:33 | Two counties' manifests are replaced (commit f46c663) |
| Mon 13 Jul 11:03 | CORLA's upload status (commit c32f441) |
| Mon 13 Jul 16:15 | Audit center update: a county's manifest as CORLA imported it (commit 43b6c78) |
| Thu 16 Jul 16:13 | CORLA's final reports (commit ba18ba6): the extract then follows CORLA's posted selections; the manifest adjustment applied |

Weld's predicted extract has 322 ballots after the roll, 317 after the 10:12 manifests and 316 after the 10:33 update (the statewide domain grew, which moves every statewide draw after it); CORLA's posted selections then match the 316. With the results of 10 July, the estimates for Weld's contests are CORLA's counts (Attorney General - REP 99, State Representative - District 19 - DEM 307).

The fast test tests/test_walkthrough.py replays the same sequence on a small synthetic mirror and checks the estimates, the prediction and the extract's certain and possible ballots at each step; replaying the real primary takes about 15 seconds and is not part of `uv run pytest`.

## Checking the estimates

`scripts/validate_estimates.py` measures the sample-count estimates against CORLA's counts for the primary's 113 targeted contests, from the target list alone, from the results of 10 July, and from the final results with and without the manifests (the final results files come from Clarity; see the script's documentation). On 25 October 2026: with the final results, 112 of 113 exact before the manifests and 111 with them (the exceptions within 12 draws); with the results of 10 July, 93 exact and 105 within 2 draws; from the target list alone, 36 exact and 103 within 10%, the statewide contest 120 against 210. No low estimate was above CORLA's count. The margins in the three counties' redacted CVRs (counting the anonymizer's aggregate rows) equal CORLA's for five of their six contests; the sixth differs by three votes, which the totals check reports as a difference to look into rather than a conclusion.

## Re-running a part

Every stage appends to the same workspace and log, so:

- **A fix to an override:** edit `overrides.toml`, then run `map apply`, `rehearse predict` and `rehearse compare`.
- **Another set of counties:** `rehearse predict --county Weld`.
- **A later or earlier posting:** `rehearse fetch --commit ...`, then predict again.

Nothing is overwritten. The log shows each run, and `corla-observer log verify WS` checks it.

## Tests

`uv run pytest` runs fast tests of every stage on a tiny synthetic mirror built as a git repository in the test (tests/test_rehearse.py). `CORLA_OBSERVER_MIRROR=... uv run pytest -m slow` also runs both rehearsals above on the real mirror (tests/acceptance/test_rehearsal_2026_primary.py), and checks the outcomes described here.

After a rehearsal, `corla-observer extract build --workspace WS --county "La Plata" --actor coordinator:rehearsal` builds La Plata's extract (docs/EXTRACT.md). After `rehearse all`, CORLA's final reports are in the workspace, so it follows CORLA's posted selections (`audit_center` order), which are exactly the predicted draws; before them it is in predicted order. `CORLA_OBSERVER_MIRROR=... uv run pytest -m slow` checks it (tests/acceptance/test_extract_2026_primary.py).

Not covered here: CORLA's own pull-list order and its assignment of ballots to audit boards, which the rehearsal lacks, and the observers' live page.
