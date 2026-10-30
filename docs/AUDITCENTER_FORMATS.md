# Audit center formats, 2017 to 2026

What the Secretary of State's Audit Center published for each statewide risk-limiting audit, as kept in the public mirror, https://github.com/nealmcb/auditcenter, and what `corla_observer.classify` makes of it. It is the basis for the data map (`corla_observer.datamap`), which records what each file is for an election and lets a coordinator correct it.

The survey covers every file git tracks in the mirror's election directories (`2017`, `2018`, `2018G`, `2019`, `2020/*`, `2021/*`, `2022/*`, `2023/*`, `2024/*`, `2025`, `2025-irv`, `2026/primary`) and the archived Audit Center pages in `auditCenters/`, at mirror commit 39f0ef47b08e3e5a465bae54b028fea76541d63f: 2,352 files, all classified by content (audio and video excepted). To repeat it, for example after a classifier change:

```
CORLA_OBSERVER_MIRROR=/path/to/auditcenter uv run python scripts/survey_mirror.py --out survey.jsonl
```

It writes one JSON line per file (kind, county, evidence, and the county the file's name suggests) and prints a per-election summary.

## What a sampling run needs, and where it comes from

| Input | Where it is published | Before the audit? |
|---|---|---|
| Seed | Announced at the public dice roll. Machine-readable only afterwards: `seed.csv` (2024 primary and general, 2025 instant-runoff, which also has `seed.json`), and the "Audit Random Seed" row of the State Audit Report's Summary sheet and of each county's audit report (every election with a report workbook). Earlier elections publish it in PDF notices (`NoticeOfRandomSeed.pdf`). | No: entered by hand (`seed.entered`). |
| County manifests | One CSV per county; the name and directory change almost every election (below). | Yes. |
| County-ID order (combining counties in a multi-county or statewide domain) | `contests_by_county.csv` (2019 to 2022 primary), `contestsByCounty.csv` (2022 general to 2025), `ContestIDsByCounty.csv` (2026 primary, in `finalReports/`). CORLA's IDs have always been Colorado's standard county numbering: alphabetical ignoring spaces (Elbert 20 before El Paso 21, Lake 33 before La Plata 34), Broomfield 64. Checked in the 2019, 2022 general and 2026 primary files. | Not reliably, so the map uses the standard numbering until the file appears. |
| Targeted contests | `targetContests.xlsx` (2020 and 2021), `targetedContests.xlsx` (2024 primary and general), `2025CoordinatedRLASelectedContests.xlsx`, `targetContestSelections.xlsx` (2026 primary). Only as PDF in 2018, 2019, 2022 general, 2023 and the 2024 presidential primary; the 2017 workbook lists county and contest only, with no margins. | Yes, in the target list. |
| Sample counts (round-one draws per contest) | CORLA's contest list, `audited_sample_count`: `contest.csv` (2018 general to 2025), `ContestsList.csv` (2022 general), `ContestsListRound1.csv` (2026 primary). | After the roll, with the selections. A round-one list posted before any ballot was audited (2022 primary and general, 2023, 2024 primary and general, 2025) holds zeros in `audited_sample_count` and in the discrepancy columns, but fills in `optimistic_samples_to_audit`, from which the draws follow (below). Before the roll, the prediction estimates the count; see below. |
| Contest-to-county mapping | The contest-ID file above; the canonical contest list (`canonicalListFinal.csv`, `CanonicalListOfContestsAndChoices.csv` and similar, 2020 general onwards); the target list's County column, where "Colorado" means statewide. | Yes, from the target list. Before the contest-ID file, the statewide contest covers every county whose manifest CORLA's upload status shows imported. |

Sample counts. In every round-one contest list surveyed that has counts (2019 to 2026), each targeted contest's `audited_sample_count` is CORLA's optimistic sample size plus one: `ceil(2 * 1.03905 * ln(1 / risk limit) / diluted margin) + 1`, the diluted margin being the contest's minimum margin over its domain's ballot cards (`ballot_card_count`: for a county contest, the county's total ballot cards, its manifest total, not only the cards that contain the contest; Adams's 2022 general contests have `ballot_card_count` 333,606 against `contest_ballot_card_count` 169,626). That holds for 64 of 64 contests in 2019, 89 of 89 in the 2020 primary, and 113 of 113 in the 2026 primary. In later rounds, a contest's cumulative draws after the round are `max(optimistic_samples_to_audit + 1, draws already made)`, using the optimistic size in the contest list posted for that round; that holds for 209 of 209 contest transitions (2022 general round 1 to 2, 2024 general rounds 1 to 2 and 2 to 3, 2025 round 1 to 2). So a list posted before its round's audit, with zeros in `audited_sample_count`, gives the round's draws as surely as one published afterwards (`corla_observer.datamap.round_target`), and each round's list gives its own round's target, whatever order the lists arrive in. The target lists' margins come from unofficial results, so an estimate made from them can be well off: 139 draws against CORLA's 210 for the 2026 primary's statewide contest. CORLA's draws for a contest are a fixed sequence, though, so the predicted draws are right as far as they go.

Pull-list order and boards. CORLA (its server's `BallotSequencer` and `CVRToAuditResponse.compareTo`) sorts each round's ballots, each once, by the storage location of the ballot's batch in the manifest (compared by its `NaturalOrderComparator`, which compares non-digit runs ignoring case and in reverse), then scanner as a number, batch naturally, and record as a number; each later round's list holds only that round's ballots, sorted the same way. It splits them among the audit boards in contiguous runs of `n // boards` ballots, one more for `n % boards` boards chosen by a random shuffle (`BallotAssignment.assignToBoards`). The published ballot lists of 2017 and 2018, from older code, were in tabulator, batch and record order in all 117 lists; in 48 of them, sorting by location first would give another order. `corla_observer.pullorder` reimplements the current rule (docs/EXTRACT.md).

Upload status. From 2019 on, CORLA publishes the hash of every file a county uploaded (`upload_status.csv` and its later names). In the 2026 primary, all but one of the 63 published manifests are byte-identical to the last manifest CORLA records importing for that county. The data map checks every manifest against this record and reports any that differs.

## The kinds the classifier knows

`county_manifest`, `contest_ids_by_county`, `contests_list`, `target_contests`, `selection_list` (each contest's drawn CVR IDs), `cvr_comparison` (audit board interpretations against the CVR), `manifest_cvr_comparison` (CORLA's batch-by-batch manifest against CVR counts), `cvr`, `pull_list` (CORLA's ballot lists), `upload_status`, `file_hashes` (2017 and 2018 hash lists), `canonical_contest_list`, `vote_totals`, `state_audit_report`, `county_audit_report`, `discrepancy_report`, `audit_activity_report`, `seed`, `html_page` and `unknown`.

Manifest headers vary more than anything else. The classifier and the loader accept every spelling seen:

- **Count column:** 28 spellings, including `# of Ballot Cards`, `# of Ballots`, `#of Ballots`, `. of Ballots`, `# in Batch`, `# Cards`, `Cards`, `Ballots`, `BatchCount`, `Number of Ballots Batched` and `# Number of Ballot Cards`.
- **Tabulator column:** 10 spellings, from `Tabulator ID` and `Device ID` to `Scanner ID`, `Device` and `tablulator id`.
- **Batch column:** 7 spellings: `Batch`, `Batch #`, `Batch ID`, `ICC Batch #`, `TrackingId` and the upper-case forms.
- **Location column:** 6 spellings, including `Storage Location`, `Locations` and `BallotStorageBox`.
- **County column:** usually `County`. Eleven manifests have none: the header cell is blank, says `County name`, `2` or the county's own name, and the first column is then the county.
- **Layout:** each election has between 7 and 21 distinct header layouts across its counties.
- **County cells:** only the first row of a block may name the county. The loader forward-fills the blank rows.

Not recognized, and kept as `unknown` with their header or format in the evidence (each can be given a kind by an override, or converted by an adapter):

- PDFs (775 files, from public notices to per-county final reports), Word and OpenDocument files, images and binary proof files.
- Ballot-polling tally sheets (`ballotPollingAudit/*TallySheet.xlsx`, 2017 to 2024).
- Audit board sign-in lists (`auditboards.csv`, 2017 and 2018).
- The 2017 selected-contests workbook (county and contest only).
- `2018G/round_4/contest_detail.csv`: audit board interpretations without CORLA's CVR choice.
- `rla_report_state.csv` (2021).
- The 2025 instant-runoff assertion files, ranked-ballot interpretations and `summarize_IRV.csv`.
- Plain hash lists (`sha256sums.txt`, `SHA256SUMS*.txt`) and Sigstore bundles.
- Larimer's 2025 redacted CVR as `.xlsx`: its sheet is larger than the reader's 128 MB limit; the same CVR is also published as CSV.

## Election by election

Paths are the mirror's, under the election directory. "Round" directories hold each audit round's files; the data map reads round one.

### 2017 coordinated

- **Manifests:** `round_1/county_manifest_<County>.csv`, for 50 counties. Of the 14 without one, six were ballot-polling counties, with tally sheets in `ballotPollingAudit/` instead.
- **Pull lists:** `round_1/` and `round_2/ballot_list_<County>.csv`, with an `audited` column and no audit board. Lake's and Teller's lists are identical in both rounds.
- **Reports:** `state_report.xlsx`, with the seed.
- **Hashes:** `m_cvr_hash.csv` and `m_manifest_hash.csv`, classified as `file_hashes`.
- **Not machine-readable:** the targeted contests, in an XLSX with no margins; sample counts and county IDs are not published. Manifest headers have no county column in four files, and a Huerfano header cell says `HUERFANO`.

### 2018 primary (`2018`)

- **Manifests:** `round_1/county_manifest_<County>.csv`, for 61 counties. Separate Democratic and Republican manifests and tally sheets for the ballot-polling counties are in `ballotPollingAudit/`.
- **Pull lists:** `ballot_list_<County>.csv`, as in 2017.
- **Reports:** `state_report.xlsx`.
- **Hashes:** `m_cvr_hash.csv` and `m_manifest_hash.csv`.
- **PDF only:** targeted contests (`RLATargetContests2018Primary.pdf`, and a corrected PDF).
- **Oddities:** `MineralGeneralResults.xlsx` has a single sheet named "Jackson Summary Report".

### 2018 general (`2018G`)

- **Manifests:** `round_1/county_manifest_<County>.csv`, for 60 counties (none for Jackson, Mineral, Montrose or San Juan).
- **Reports:** a county audit report per county and round (`county_report_<County>.xlsx`, now `county_audit_report`) and `state_report.xlsx` per round, all with the seed.
- **Other files:** `cvr_hash.csv`, `manifest_hash.csv`, `batch_count_comparison.csv`, and an `upload_status.csv` in an older layout with `hash_status` and no hashes.
- **Contest list:** only in round 4 (`round_4/contest.csv`).
- **PDF only:** targeted contests (`RLATargetContests2018General.pdf`).

### 2019 coordinated

- **Manifests:** `round_1/county_manifest_<County>.csv`, for 62 counties. Saguache's names the county in the header cell; one county uses the `TrackingId`, `BatchCount` and `BallotStorageBox` vocabulary.
- **Per round:** `contest.csv`, `contestSelection.csv`, `contestComparison.csv`, `stateReport.xlsx`, and the activity and results reports.
- **Election-wide:** `contests_by_county.csv`, `batch_count_comparison.csv`, `upload_status.csv` (with hashes from now on), and `tabulate*.csv`.
- **PDF only:** targeted contests and the seed notice.

### 2020 presidential primary

Fourteen files, and no manifests or audit report in the mirror: `targetContests.xlsx`, `contests_by_county.csv`, round-one `contest.csv`, `contest_selection.csv` and `contest_comparison.csv`, `batch_count_comparison.csv`, `upload_status.csv` and the tabulations. The seed is only in the PDF notice.

### 2020 state primary

- **Manifests:** `round_1/manifest-<County>.csv`, for 63 counties.
- **Other files:** `targetContests.xlsx`, with a Party column, and the CORLA exports as in 2019.
- **Oddities:** a second, separate audit for Boulder in `boulder/`, with its own manifest (`manifest_boulder.csv`), contest lists, selections and reports. The data map treats `boulder` as a subdirectory of the election, so its files compete with the statewide ones and need an override saying which to use.

### 2020 general

- **Manifests:** `round_1/manifest-<County>.csv`, plus one named `Manifest-Gunnison.csv`. Rio Blanco's header cell says `2`, and Yuma's is blank.
- **Other files:** `targetContests.xlsx`, `canonicalTitleCase.csv`, and the CORLA exports.
- **Footnote:** the target list ends with a footnote about the "# of CVRs" column in its County column. The classifier counts it as a footer.

### 2021 coordinated

- **Manifests:** `round_1/<County>.csv`, for 62 counties. Mineral and San Juan have none.
- **Other files:** `targetContests.xlsx`, `canonical_list.csv`, and the CORLA exports.
- **Unknown:** `rla_report_state.csv`, and a `sha256sums.txt` beside the manifests.
- **Oddities:** `Washington.csv` spells its county "Washinton". It needs a `[[county_alias]]`.

### 2022 primary

- **Manifests:** `round_1/<County>.csv`, for 63 counties.
- **Contest lists:** `contest.csv` and `contest_selection.csv` are identical at the top level and in `round_1/`. The round-one contest list holds zero audited counts (its optimistic sample sizes are filled in); round 2's list has them.
- **Other files:** `2022PrimaryRLACounty-CandidateList.csv` (the canonical list), five OpenDocument files, and a `sha256sums.txt`.
- **Not published:** a target list. The seed is in a PDF notice.

### 2022 general

- **Manifests:** `round_1/<County>.csv`, with upper-case and space-padded county names. `canonical_county` reads them.
- **Contest lists:** `ContestsList.csv` (identical to `round_2/ContestsList.csv`), with round-one `contest.csv` holding zero audited counts.
- **Canonical list:** Latin-1 (`canonicalListFinal.csv`).
- **PDF only:** targeted contests (`targetContests.pdf`).
- **Misnamed file:** `round_1/CVRtoAuditBoardInterpretativeComparison.csv` holds CORLA's manifest-to-CVR batch comparison and is byte-identical to `round_1/batchCountComparison.csv`. The classifier calls both `manifest_cvr_comparison`.

### 2023 coordinated

- **Manifests:** `round_1/<County>.csv`, for 57 counties. Delta, Douglas, Moffat, Phillips, Sedgwick, Summit and San Juan have none.
- **PDF only:** targeted contests (`targetedContests.pdf`). The round-one contest list holds zero audited counts.
- **Oddities:** Montezuma's manifest spells its county "Monteuzuma" on its rows, so it needs an alias. Round one also has `-augmented` copies of the comparison file with extra columns; one of them is unknown.

### 2024 presidential primary

- **Manifests:** `<County>Manifest.csv`, for only 27 counties, recovered from Internet Archive captures. The other counties' manifests are not in the mirror.
- **Other files:** CORLA's exports for rounds 1 and 2, and per-county final reports as PDF (`finalReport/<County>.pdf`).
- **PDF only:** targeted contests.

### 2024 primary

- **Manifests:** `<County>Manifest.csv`, for 63 counties. Two header typos were unknown until this survey: Huerfano's `# Number of Ballot Cards` and Phillips's `tablulator id`.
- **Other files:** `seed.csv`, `targetedContests.xlsx`, and `countyManifest.html`. The round-one contest list holds zero audited counts.

### 2024 general

- **Manifests:** `<County>BallotManifest.csv`, twice: at the top level and in `ballotManifests/`, byte for byte. The data map uses identical content once.
- **Other files:** `seed.csv`, `targetedContests.xlsx` (with a footnote row), `2024GEDiscrepancyReport.xlsx`, and a Garfield ballot-polling audit in `ballotPollingAudit/`.
- **Contest lists:** round one holds zero audited counts; rounds 2 and 3 have them.
- **Target list:** its Remarks column says "2 ballot cards per ballot*" (Adams, Alamosa, Boulder, Larimer, Pueblo, Routt) or "3 ballot cards per ballot*" (Denver) on each multi-card county's rows, and its denominator (contest margin over diluted margin) is its "# of CVRs" times the cards per ballot: Denver's 38268 / 0.0346408 is about 1,104,700, three times its 368,220 ballots, against CORLA's `ballot_card_count` of 1,104,271. The data map reads these remarks per county (`target_cards_per_ballot`), and the estimates use them before the manifests, above `election.toml`'s figures. Such remarks are in the target lists of the 2020 general (Denver 3; Eagle, Routt and Saguache 2), the 2021 coordinated (Denver 2) and the 2024 general; not in the 2020 presidential and state primaries, the 2024 primary, the 2025 coordinated or the 2026 primary (no county had more than one card), nor in the PDF lists of 2018, 2019, 2022, 2023 and the 2024 presidential primary (`scripts/survey_target_remarks.py`).

### 2025 coordinated

- **Manifests:** `files/<County>Manifest.csv`.
- **Other files:** `2025CoordinatedRLASelectedContests.xlsx` (with "Multi-county" rows and an "End of Worksheet" footer), `finalReports/`, and counties' own redacted CVRs in `cvrs/` (Boulder as CSV and XLSX, Broomfield, and Larimer as CSV and XLSX).
- **HTML only:** `DoloresCountyNote.html`, a note the Secretary of State published about one county's audit as a page rather than as a data file.
- **Oddities:** two counties' manifest files are byte-identical, so one county's manifest is in effect absent under its own name; the fetcher raises a duplicate-content alert and the data map says which file holds which county's rows. Prowers's manifest has the same "Prowes" spelling as in 2026.

### 2025 instant-runoff audit (`2025-irv`)

- **Scope:** Larimer only, every CORLA export as both CSV and JSON.
- **JSON exports:** a list of flat objects. They are now read as tables, so `seed.json`, `contest.json`, `upload_status.json` and the others are classified like their CSV twins.
- **Unknown:** the assertion files (per-contest JSON documents and CSVs), the ranked-ballot interpretations and `summarize_IRV.csv`.

### 2026 primary

- **Manifests:** `files/<County>.csv`, for 63 counties, plus `files/targetContestSelections.xlsx`, `files/uploadStatus.csv` and `files/2026DiscrepancyReportPE.xlsx` (with a Word copy). The county files that observers received are in `observerfiles/`.
- **Final reports (`finalReports/`):** the contest list, contest IDs, selections, CVR comparison, manifest-to-CVR comparison, canonical list, vote totals, and the state audit report, activity report and results report.
- **Prowers:** `files/Prowers.csv` spells its county "Prowes" on one row, so it needs a `[[county_alias]]`.
- **Manifests posted, then replaced:** manifest files in this election changed over the course of roll day, and some counties' files were posted more than once (the mirror's commits of 13 July show the sequence). Two mechanisms matter for reading such an election, and both are exercised here:
  - *Identical content under two counties' names.* The fetcher raises a duplicate-content alert, and the data map says which file holds which county's rows rather than treating either as that county's manifest.
  - *A posted copy that is not the file CORLA imported.* CORLA's upload status names the file it imported, with a time and a hash, and marks uploads `HASH_MISMATCH` or `FAILED`. Where a posted or county-supplied copy differs, the data map reports the copies as conflicting, uses the most recently obtained one, and raises a change flag if the difference arrived after the roll. Which county, and whether the difference is real, is for the team to establish with the county.
- **A manifest short of CORLA's card count:** a published manifest can sum to fewer cards than CORLA's contest list gives, with the missing batch visible in CORLA's manifest-to-CVR comparison. The data map's domain-size check reports the shortfall as soon as the contest list is fetched, and the batch can be supplied by a `[[manifest_adjustment]]` carrying that evidence (`corla_observer.rehearse.PRIMARY_2026_OVERRIDES`).
- **Counties without a manifest:** San Juan, which counts by hand, is in no 2026 file.

### Archived Audit Center pages (`auditCenters/`)

Twenty copies of `auditCenter.html` from July 2022 to July 2026, all `html_page`. Copies a few hours apart differ only in Cloudflare's per-request tokens, which `corla_observer.fetch.normalize_html` removes. The pages link to files under two URL layouts: `RLA/<year>/<election>/...` and `RLA/files/<year>/<election>/...`. `corla_observer.datamap.election_from_url` reads both.

### Data only in HTML

Some facts are published only in pages, which the classifier keeps as `html_page`:

- the risk limit (`riskLimit.html`);
- the list of manifests (`countyManifest.html`);
- affirmations;
- notices of public meetings;
- `finalReports.html` index pages;
- the 2025 Dolores County note.

None of these are needed for a sampling run.
