# Demo: one county, start to finish, on the coordinator pages (Delta County, 2020 general)

A hands-on script for a coordinator who does every step on the web pages: Delta County's files,
the targeted ballots, the audit center's files, the seed, the extract and its printout, and an
observer who enters what they hear. It uses Colorado's **2020 general election as a stand-in for the
November 2026 general**: election day was 3 November in both years, and Delta's 2020 files are
public. Everything shown is real public data; nothing is invented.

- Delta's redacted CVR is the county's published 2020 CVR export, in the exact bytes CORLA received:
  its SHA-256 is the one CORLA's upload status records for Delta's CVR. (The copy available
  online had been through a spreadsheet; undoing that transformation gives back CORLA's file.) It
  needs no redaction.
- Delta's manifest stands in for the copy the county sends us: it is byte-identical to the file
  CORLA imported, and to the one the audit center later posted.
- The "audit center" is the public mirror of the Secretary of State's audit center
  (https://github.com/nealmcb/auditcenter), since the live site now holds another election. Each
  **Fetch now** brings in its next state. The first is the files posted before the roll (the target
  contest list, all 63 county manifests and CORLA's upload status of 13 November 2020); the mirror
  keeps the 2020 general in one snapshot taken after the audit, so the files posted at or after the
  roll are left out by name (`GENERAL_2020_AFTER_ROLL` in `corla_observer.election`).
- The seed is the real one rolled on 16 November 2020.
- Election night results are not fetched in this demo.

Times are rough. The whole demo takes about 30 minutes.

## Before you start

- The demo URL (with its path prefix), the coordinator name and the coordinator password, from
  whoever set the demo up (the steps below use the name `neal`). The site starts empty, as below;
  to start it again, see "Starting again" at the end.
- On your laptop, in a folder outside the repository (your download folder will do), the two files
  from Delta County:
  - `Modified CVR_Export_20201113093409.csv` (4.6 MB, the redacted CVR)
  - `Modified 2020 General Ballot Manifest.csv` (7.7 KB, the ballot manifest)

  Their names are the ones CORLA's upload status records for Delta's uploads.
- A phone, or a second browser, for the observer.
- Don't click **Fetch now** before step 5: the demo depends on the audit center arriving after the
  county's files.

## Steps

### 1. Log in, and see the empty start (2 min)

Open the demo URL, then **Coordinators** (top right), name `neal`, the password, **Log in**.

The coordinator home: **Flags: 0 open**, **Data map: 6 problems, 2 blocking**. The **Audit center**
box says that this is a past election replayed for a demonstration, that the live Audit Center is
never fetched here, and "0 of 1 applied. Next: **Before the roll: the target contest list, the 63
county manifests, CORLA's upload status (13 November 2020)**". Under **Counties**, the **Delta**
card: "Before the drawing · redacted CVR not yet provided", **In CORLA**: "CORLA's upload status
not yet fetched", **Audit boards**: not yet known, and "Not yet provided to us: redacted CVR (a
manifest from the county is optional)".

**Data map** lists what is missing: no targeted contests (no target contest list yet), no seed,
Delta's redacted CVR, and CORLA's upload status. The public **Dashboard** shows the same problems,
and "No county is taking part in observation yet."

### 2. Upload Delta's redacted CVR (2 min)

**Upload files**, choose `Modified CVR_Export_20201113093409.csv`, **Upload and check**.

The result page: "Modified CVR_Export_20201113093409.csv: accepted", **Detected: Redacted CVR for
Delta**, with the evidence (its columns, and its first row naming the election "2020 Delta County
General Election"), its size (4.6 MB) and SHA-256 (`70ae835ca04b...`), and **Redaction check: PASS**:
19553 rows read, 19553 ballots grouped, 0 already redacted, 4 contest patterns, 25 groups (contest
pattern, precinct and ballot type), "Rare groups (under 10): 0, holding 0 ballots". Counts only,
never votes.

Nothing had to be chosen: the content said what the file is and whose.

### 3. Upload Delta's manifest, as the county sent it (1 min)

**Upload more**, choose `Modified 2020 General Ballot Manifest.csv`, **Upload and check**:
"accepted", **Ballot manifest for Delta** (its County column says so), SHA-256 `4fdbfda9ba46...`.

**Back to status**: the **Delta** card now says "Before the drawing · redacted CVR provided", with
the redacted CVR's verdict and counts, and **Manifest: county copy received**, used for sampling:
418 batches, 19553 ballot cards.

### 4. Define targeted ballots, before any audit center data and before the seed (5 min)

**Targets**. Delta is chosen (the only county with a redacted CVR); the page reads the CVR and the
county's manifest, nothing else. Targeted ballots are ballots the observers choose outside the RLA's
random sample, which the county pulls at their request (docs/TARGETING.md).

Under **Choose a pattern**, the statewide contests and Delta's US House contest, each with its margin
in Delta and every candidate's votes. No results are loaded in this demo, so a notice says the
contests are in the county tally's order. "19553 ballots match" with nothing checked.

- In **United States Senator**, check **John W. Hickenlooper** (5764 votes).
- In **Representative to the 117th United States Congress-District 3**, check **Lauren Boebert**
  (12937 votes).

The count changes as each box is checked: a split ticket, Hickenlooper and Boebert, "**259**
ballots match". The number beside every other box shows what checking it too would give. (A
search box adds any other contest of the CVR to the grid; the split-ticket presets appear only when
the candidates' parties are known, which this CVR does not say.)

The pattern row under the grid reads: the pattern · **259** matching · **Seed** · **Ballots** ·
**Draw**. In **Seed** type the observers' own words for the draw, `Delta observers 2020` (no RLA
seed exists yet; any text will do, and whoever has it can redo the draw), set **Ballots** to `5`,
and click **Draw**.

"Draw 1: 5 ballots added to the working list." Under **The working list**, **Sources** has one row,
**Draw 1**, checked (included), with the pattern, the seed and "5 of 5 asked"; **Ballots** lists the
five, each with its storage location and ballot type, found in the CVR and in the manifest, in
CORLA's pull order: `1-92-7`, `1-111-17`, `1-128-41`, `1-232-48`, `1-389-16`. Unchecking a source
takes its ballots out of the list; **Draw again** on its row draws five more, skipping these.

Click **Send to county: make Targeted list 1**. The confirmation: "Targeted list 1 sent to the
county" (5 ballots, marked TARGETED (NOT RLA), its extract hash). Under **Sent lists**, Targeted
list 1 has the date, its sources, its PDFs, its CSV and **Text for the county's email** (ballot
locations only, no votes, with "Targeted list 1 for Delta County" as the header), and the working
list is empty again, ready for Targeted list 2.

An observer link made now (step 10) opens on this list, since Delta has no RLA sample yet: the
page says "No RLA sample for Delta board 1 yet (the seed hasn't been rolled). Showing the targeted
list: Targeted list 1." with the list chooser in its header; **Extracts** says the same from the
coordinator's side ("No RLA-sample extract for Delta yet", the seed needed, the targeted list and
its PDFs below). Once the extract is built (step 9) the link opens on the RLA sample.

### 5. CORLA's hashes are not there yet (1 min)

**Data map**. Under "uploads compared with the audit center and CORLA's upload hashes":

```
Delta's manifest as uploaded here (4fdbfda9ba46):
  the audit center has not posted the county's manifest yet
  CORLA's upload hashes: not yet posted (no upload status file from the audit center)
Delta's redacted CVR as uploaded here (70ae835ca04b):
  CORLA's upload hashes: not yet posted (no upload status file from the audit center)
```

We have the county's files, but nothing yet to check them against.

### 6. Update the audit center (2 min)

**Coordinator**, then **Fetch now** in the **Audit center** box. "Fetch started." It takes about
4 minutes.  (We'll add a progress bar!)
**Reload until the box says "1 of 1 applied (last: Before the roll: ...)"** and "Last web fetch
... finished".

This brought in the 2020 general's audit center files as they stood before the roll: the target
contest list, 63 county manifests, the county manifest page, CORLA's upload status and the notice
of the seed's public meeting. Each is recorded in the event log as a fetch of its audit center URL,
marked as coming from the public mirror at its commit, never as a fetch from the Secretary of
State's site.

The **Delta** card now has **In CORLA: Manifest imported** 2020-11-13 10:24:03 MST and **CVR
imported** 2020-11-13 10:22:42 MST, from CORLA's upload status, and its manifest is the audit
center's `RLA/2020/general/round_1/manifest-Delta.csv`, the same 418 batches. **Flags: 15 open, 15
high**: the flags that the `HASH_MISMATCH` and `FAILED` rows of CORLA's own 2020 upload status
raise, for other counties; none for Delta. Each is listed on **Flags** with what to investigate.

### 7. The official manifest and CORLA's hashes check out (3 min)

**Data map** again:

```
manifests (63 counties, ballot cards each)
...
Delta's manifest as uploaded here (4fdbfda9ba46):
  byte-identical to the audit center's posted manifest (RLA/2020/general/round_1/manifest-Delta.csv)
  byte-identical to the file CORLA recorded importing (Modified 2020 General Ballot Manifest.csv,
  2020-11-13 10:24:03 MST, SHA-256 4fdbfda9ba46)
Delta's redacted CVR as uploaded here (70ae835ca04b):
  byte-identical to the file CORLA recorded importing (Modified CVR_Export_20201113093409.csv,
  2020-11-13 10:22:42 MST, SHA-256 70ae835ca04b)
```

So the county's copy of the manifest is the one CORLA used and the audit center posted, and the CVR
we hold is exactly the file CORLA audited. (A county that redacts its CVR sends a different file;
then this line says so, as expected.) The map also has the 64 targeted contests from the target list
(Presidential Electors statewide; Delta's is "Delta County Ballot Issue 1A"), each with a sample
count still to be estimated, and only one blocking problem left: no seed. **Estimates** shows each
contest's central and low estimate from the target list's margin alone.

### 8. The roll: enter the seed (2 min)

**Seed**: "No seed entered yet." Under **Enter the seed**, type

```
92076602804272786701
```

and in **Source**: "Secretary of State's Notice of Random Seed, 16 November 2020". (In the mirror:
`2020/general/20201116NoticeOfRandomSeed.pdf`, and the "Audit Random Seed" on the Summary sheet of
`2020/general/round_1/stateReport.xlsx`.) **Enter**.

"Seed entered and in effect." The selections are predicted from it at once: Delta's targeted
contest and the statewide one, from the seed, the 63 manifests and the estimated sample counts, by
CORLA's method.

### 9. Build the extract, and print it (3 min)

**Extracts**. Under **Build an extract** choose **Delta** (leave the rest empty: one audit board,
since none was recorded) and **Build and record**.

"Extract built and recorded." Delta: "Pull list: not yet", **predicted order**, **38 ballots**, for
board 1 the four PDF layouts (portrait two columns: 9 pages). The predicted order is marked as such
everywhere, until the county's pull list arrives. Below it, Targeted list 1 has its own printouts.
(Had any of the 38 sampled ballots been on the targeted list, the extract would mark it "PREVIOUSLY
TARGETED: Targeted list 1, may already have been pulled".)

Click **Download PDF (portrait, two columns, the default)**: `delta-board1-portrait-2-<hash>.pdf`,
the provenance sheet and then the ballots, each with its imprinted ID, storage location and a box
beside each expected vote. Print at actual size (100%).

### 10. Add an observer (2 min)

**Links**: **For** an observer, **Name** e.g. `neal-phone`, **County** Delta, **Audit board** 1,
**Make the link**. Copy the link (shown only once) and open it on your phone, or in another browser
(a private window will do).

### 11. Enter what you hear, contest by contest (5 min)

On the phone: the notice first ("Keep this screen out of sight of the audit board..."), with
**Contest by contest** chosen. **I understand, start**.

The first ballot: `1-25-2`, "Ballot 1 of 38", `Delta · Board 1 · extract ...` and its QR code;
**1 First reading**; "contest 1 of 30"; the contests as rows, each with its name, the two readings'
marks (○ not yet) and the expected choice.

Read the first few contests aloud yourself, as the board would:

1. **Presidential Electors** (J. R. Biden / K. D. Harris): tap the row. ✓, and the focus moves on.
2. **US Senator** (J. W. Hickenlooper): tap the row.
3. **Representative to the 117th US Congress** (D. E. M. Bush): suppose you heard Boebert. Tap ✎
   (on a laptop, just start typing) and type `Lau`: "Lauren Boebert" is suggested; take it. The row
   shows "▲ 1st heard: Lauren Boebert", with an orange edge.
4. **State BOE - CD3** (Mayling Simpson): suppose you didn't hear it clearly. Tap **?** (on a
   laptop, `/`). The row shows ?.
5. **State Rep - Dist 61** (Julie McCluskie): tap the row.

The header says "5 entered" and "all sent"; each entry is signed on the phone and has the server's
receipt. **Keys** lists the keyboard keys for a laptop.

### 12. Switch to the targeted list, and look up a ballot from a box (2 min)

In the header, the list chooser says "RLA sample (38)". Choose "**Targeted list 1 (5)**": the page
reloads on it, marked **TARGETED (NOT RLA)**, at its first ballot, with its own progress. Your
entries on the RLA sample are kept; choose "RLA sample" to go back.

Choose "**Look up a ballot (from a box)**": this is for a ballot the board pulls from a storage box
at the observers' request. Type `1-7-45` and **Look up**: the ballot's expected votes come from
Delta's redacted CVR, with its storage location, as ballot 1 of your lookups, to enter contest by
contest like any other. These are recorded apart from every list, as box-targeted observations.

### 13. Watch it as the coordinator (2 min)

Back on the laptop: **Observation** (it reloads every 15 seconds). **Boards**: "Delta, 1", 38
ballots, one observer contest by contest; "Delta, 1, targeted (not RLA), Targeted list 1", 5
ballots; and "Delta, 1, targeted (not RLA), Looked up from boxes". **Targeted ballots (not RLA)**
sums the two kinds apart: "Targeted by CVR: 1 list, 5 ballots, 0 observed · Targeted from boxes: 1
observed". **Observers**: `observer:neal-phone`, contest by contest, on each list (no ballot counts
as entered until it is done in both readings). **Comparing observers**, Delta, board 1: ballot 1,
`1-25-2`, "difference": "Representative to the 117th United States Congress-District 3 (expected
Diane E. Mitsch Bush): heard "Lauren Boebert"" and "State Board of Education Member - Congressional
District 3 (expected Mayling Simpson): not heard clearly".

The public **Dashboard** now lists Delta among the counties taking part, "redacted CVR provided",
with its boards' progress, "Targeted list 1" and "Targeted from boxes" with their counts only, and
never a vote.

## Later

Not part of this demo:

- **Election night results.** The setup knows Clarity's ID for the 2020 general (105975, still
  online); with it set as `[results] election_id` in the workspace's `election.toml`, **Fetch now**
  also fetches the results, and **Estimates** then uses the results' margins.
- **The preliminary target list across all counties**, on **Estimates** and the data map, after the
  audit center's update in step 6.
- **More audit center states.** `election.toml` can list further updates from the mirror (such as
  CORLA's round 1 files after the roll), each applied by one **Fetch now**.

## Starting again

On the demo server, as the user that runs the service, the reset script installed there:

```
corla-demo-reset.sh
```

It stops the web service, empties the workspace except the coordinator accounts (observer links go
too), sets it up again empty (`scripts/demo_delta_2020.sh WORKSPACE MIRROR --reset`) and starts the
service. Anyone logged in must log in again.

`scripts/browser_demo_delta_2020.py` does every step above in a headless browser and checks each
one, for verifying the demo before showing it (then start again, as above).
