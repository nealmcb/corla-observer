# The observers' extract

Version 2 (`"format": 2`), with page format 6. (Version 1 had no `certain`, `boundary` or `board_split`, sorted a predicted order by tabulator, batch and position, and split it into runs of equal length.)

The extract is the reference observers follow during a county's audit: the county's selected ballots in pull order, each with the votes its redacted CVR records. The printout, the spreadsheet and the live observation page are all rendered from one structure, which is a deterministic function of committed inputs. This document specifies that structure, its hash, the hash of each printed page and the printout itself, a PDF whose bytes anyone can reproduce, so a photograph of a printed page can later be checked against the committed extract.

The code is `corla_observer.extract` (the structure and its pages), `corla_observer.printlayout` (the sheets' geometry and fonts) and `corla_observer.pdfextract` (the PDF); the commands are `corla-observer extract build|show|print`, and the coordinator pages are under `/coord/extract`.

## Inputs

For one county:

| Input | Where it comes from |
|---|---|
| Pull list | The county's pull list (CORLA's "List of ballots to audit"), uploaded by its coordinator, if there is one. |
| CORLA's posted selections | CORLA's selection list and CVR comparison file, as the audit center posts them, if fetched: the county's selected ballots (by its rows in the comparison file). |
| Redacted CVR | The county's redacted CVR, uploaded by its coordinator, which passed the redaction check. |
| Manifest | The data map's chosen manifest for the county (storage locations). |
| All manifests | Every county's chosen manifest (the statewide sampling domain). |
| Overrides | The data map overrides last applied. |
| Prediction | The latest recorded prediction (`prediction.made`), if it was made from the seed in effect. |
| Seed | The seed in effect: the latest a coordinator entered (`seed.entered`), if any. |

An extract needs the county's redacted CVR, and its ballots from one of: the county's pull list, CORLA's posted selections, or a prediction made from the seed in effect. No prediction and no seed are needed with a pull list or a posting: the extract is built from it and the redacted CVR. A prediction from the seed in effect, if there is one, adds which contests each ballot was drawn for (`picks`) and the checks of the listed ballots against it (`not_predicted`, `predicted_not_listed`); without one, `picks` is empty, `prediction` and (with no seed entered) `seed` are null, and those two checks are absent.

**A test county** (Testland, `corla_observer.testcounty`) is self-contained: its extract uses its own uploads only, never a real election's manifests, overrides, prediction or posting (`manifests`, `overrides`, `selection_list` and `cvr_comparison` are null, and `election` is null). Its `county_manifest` is its own uploaded manifest, which gives the storage locations. Without its pull list, its selections are predicted as for a real county, from the seed in effect (or else the test county's own fictional seed, `corla_observer.testcounty.SEED`), its own manifest as the only sampling domain, its one targeted contest (`TARGETED`) and a fixed sample count (`SAMPLE_COUNT`, 50, or `extract build --sample-count N`), recorded in the prediction as fixed, not estimated. That prediction is stored, and its hash is the extract's `prediction` input, but it is never recorded as a `prediction.made` event, so it never stands in for a real election's. A test county's extract is always a practice extract, and is never part of an evidence commitment.

The `inputs` object of the extract records each input by its SHA-256 (the seed by its value):

```
"inputs": {
  "pull_list": SHA-256 of the pull list file, or null,
  "redacted_cvr": SHA-256 of the redacted CVR file,
  "county_manifest": SHA-256 of the county's chosen manifest file, or null,
  "manifests": SHA-256 of the UTF-8 text with one line "COUNTY<TAB>SHA-256<LF>" per chosen manifest, sorted by county name, or null,
  "overrides": SHA-256 of the overrides file, or null,
  "prediction": SHA-256 of the stored prediction (its canonical JSON), or null,
  "seed": the 20-digit seed, or null,
  "selection_list": SHA-256 of CORLA's selection list, if its posting is used, or null,
  "cvr_comparison": SHA-256 of CORLA's CVR comparison file, if its posting is used, or null
}
```

Every file is kept in the workspace under its SHA-256, and the event log records its arrival.

## Order and boards

The ballots come from the first of these to arrive, and the other is an independent check (`checks.other_source`, below):

- **The county's pull list:** the ballots in the list's own order, each imprinted ID once. A ballot's imprinted ID is the list's imprinted ID column, or its tabulator, batch and record columns joined by `-`. Each audit board is numbered by the last number in its label ("Audit Board 2" is 2), provided every label has one, and they are distinct and 1 to 20; otherwise boards are numbered in the order their labels first appear. `"order": "pull_list"`, `"board_split": "pull_list"`. A list without a board column is split as below.
- **CORLA's posted selections** (the audit center's posting of the selection list and the CVR comparison file): the county's selected ballots, each once, in CORLA's pull order (below). `"order": "audit_center"`. If the posting came first and the pull list arrives listing exactly the same ballots, the pull list's order and boards are used (`"order": "pull_list"`); if it lists others, the posting stays and the disagreement is reported.
- **Neither yet:** every ballot drawn in the county for any contest in the prediction, each once, in CORLA's pull order. `"order": "predicted"`, and every rendering says so.
- **A targeted list** (`corla_observer.targets`, docs/TARGETING.md): ballots the observers chose, which the county pulls at their request and which are not part of the RLA sample, in CORLA's pull order, as a pull list the tool writes when a coordinator sends the working list to the county (each board a contiguous run). It replaces the county's own list and posting for this extract only, and is named in `inputs` as `targeted_list` (an input only targeted extracts have). `"order": "targeted"`, and every rendering says so ("TARGETED (NOT RLA)" on each page). A targeted extract never replaces the county's RLA extract: see "Pull lists" below.

## Pull lists

A county can have, side by side (`corla_observer.pulllists`):

- **the RLA sample:** its latest extract whose order is not `targeted`, built from the pull list, the posting or the prediction as above. This is what the county's observer and reader links show by default (until the county has one, a link opens on its first targeted list and says so), what `corla-observer extract show` prints and what the scheduler rebuilds; building it again leaves the targeted lists as they are;
- **any number of targeted lists,** "Targeted list 1", "Targeted list 2", ..., each the working list of docs/TARGETING.md as a coordinator sent it to the county (on `/coord/targets`, or `corla-observer targets send`): a targeted extract built under the list's name, with a stored list record (its sources and its ballots) named in the event. A list's key is `t-` and the first 12 hex digits of the SHA-256 of its name. A list built by the first design under a name of its own is numbered the same way and labelled "Targeted list N (name)"; its ballots may carry `"source_list"`, the list each came from when several were combined (shown as "FROM: name"). A coordinator can retire a list, which hides it from observers until it is restored.

Observers switch between the lists with the list in their page's header (docs/RECEIPTS.md, `list_switch`); the reader's page has the same choice. Each list keeps its own entries, progress and statistics: an entry belongs to the list whose extract it names (its `extract_sha256`), so entries on a targeted list never count toward the RLA sample's progress, dashboard, statistics or comparisons. An observer's page also offers **Look up a ballot** (the list key `lookup`), for a ballot picked from a storage box: it has no extract, and its entries (`observation.lookup_contest`, `observation.lookup_done`) are counted apart from every list (docs/TARGETING.md, "Two kinds of targeted observation").

**Overlaps** (docs/TARGETING.md, "Duplicates and overlaps") are marked both ways when an extract is built (`corla_observer.extract.overlaps`): a targeted extract's ballot that the county's RLA extract holds has `"in_rla": true` and is printed and shown with "ALSO IN THE RLA SAMPLE: entered there; no need to pull it twice"; an RLA extract's ballot that an earlier sent targeted list holds has `"targeted_in"`, the list's label, and the mark "PREVIOUSLY TARGETED: Targeted list N, may already have been pulled". `checks.in_rla` and `checks.previously_targeted` list them, and the `extract.built` event counts them.

**CORLA's pull order** (`corla_observer.pullorder`, reimplemented from CORLA's `BallotSequencer` and `CVRToAuditResponse.compareTo`): by the storage location of the ballot's batch in the county's chosen manifest, compared by CORLA's `NaturalOrderComparator`; then the scanner (tabulator) as a number; then the batch, compared naturally; then the record (the last part of the imprinted ID) as a number. The natural comparison splits each string into runs of ASCII digits and runs of other characters, compares two digit runs as 64-bit numbers (as text, ignoring case, if one is too large), and any other two runs as text ignoring case, *in reverse* (so `B` comes before `A`, as CORLA's code does); when all runs compared are equal, the string with fewer runs comes first. (The 2017 and 2018 published lists, from older CORLA code, were by tabulator, batch and record, without the location.) In later rounds each round's list holds only that round's ballots, sorted the same way; the extract is round one's.

**Boards without a pull list** (`"board_split": "uncertain"`): CORLA gives each of the county's *b* audit boards `n // b` of the *n* ballots and one more to `n % b` boards, chosen by a random shuffle, and each board takes a contiguous run of the sorted list in board order. So each board's run is known except at its ends: board *i* (from 0) starts after between `i * (n // b) + max(0, i - (b - n % b))` and `i * (n // b) + min(i, n % b)` ballots. Every ballot that can fall to more than one board is listed on **each** of those boards, in its sorted place, with `boundary` naming the other boards, and is marked on paper, on screen and in the spreadsheet: "BOUNDARY: may be Board 2's ballot. If this board doesn't pull it, skip it." Each board's pages and page hashes include its boundary ballots. The board count is the county's recorded audit board count (or the number given when building, default 1); with one board there are no boundary ballots.

`position` is a ballot's place in the whole county's order, from 1 (a boundary ballot has the same `position` on each board it is listed on); `board_position` is its place in its board's list, from 1, counting the boundary ballots listed on that board.

**Certain and possible ballots.** In a predicted order, a ballot is `certain` when one of its draws is within its contest's low count (`low_count` in the prediction: the low estimate of the sample count, `corla_observer.estimate`, or CORLA's count once published), and otherwise only possible: it is drawn only if the count turns out above its low estimate. Since CORLA's draws are a fixed sequence, every certain ballot will be drawn if CORLA's count is at least the low estimate. Possible ballots are marked "POSSIBLE: drawn only if the sample count is above its low estimate". With a pull list or CORLA's posting, every ballot is certain.

## Expected votes

Each ballot is found in the redacted CVR by imprinted ID, or else by tabulator, batch and record. IDs are compared with spaces and leading zeros in each part removed. For each contest on the card (a contest whose cells on that row are not all blank), the expected value is:

- **One choice:** the choice's name. Choice names are the CVR's second header row, without Excel's `="..."` wrapping.
- **Several choices, up to the contest's `Vote For`:** the names joined by ` + `.
- **More choices than `Vote For`:** `overvote: ` followed by the names joined by ` + `.
- **No choice marked:** `no vote`.
- **Every cell blank or a redaction marker** (`*`, `X`, `Redacted` and so on): `redacted`.

Contest names lose their ` (Vote For=N)` suffix.

A ballot's `status` is:

- `votes`: in the CVR, with at least one vote value;
- `redacted`: in the CVR, but with no vote values left (its `votes` is empty);
- `not_in_cvr`: not found in the CVR (its `votes` is empty).

## Structure

```
{
  "format": 2,
  "kind": "observer_extract",
  "purpose": "audit" | "practice",
  "election": "2026/general" or null,
  "county": "La Plata",
  "seed": "49006417086137856424" or null,
  "order": "pull_list" | "audit_center" | "predicted" | "targeted",
  "order_note": text saying which order this is,
  "board_split": "pull_list" | "uncertain",
  "inputs": { ... as above ... },
  "boards": { "1": number of ballots, "2": ... },
  "contests": { contest name: { "scope": "county" | "multi-county" | "statewide", "counties": [names] } },
  "choices": { contest name: [choice names, in CVR column order] },
  "checks": {
    "not_predicted": [imprinted IDs listed that the prediction did not draw],          (only with a prediction)
    "predicted_not_listed": [imprinted IDs the prediction drew that are not listed, by tabulator, batch and record],   (only with a prediction)
    "other_source": {                       (only when both the pull list and CORLA's posting are in)
      "source": "audit_center" | "pull_list" (the one that is the check),
      "not_listed": [its imprinted IDs that the extract does not list],
      "not_in_other": [the extract's imprinted IDs that it lacks]
    },
    "sources_disagree": true | false,       (with other_source)
    "in_rla": [imprinted IDs the RLA sample also holds],                       (only on a targeted extract, when any)
    "previously_targeted": [{"imprinted_id": ..., "list": "Targeted list 1"}]  (only on an RLA extract, when any)
  },
  "ballots": [
    {
      "position": 1,
      "board": 1,
      "board_position": 1,
      "imprinted_id": "102-2-36",
      "location": "Bin # 1" or null,
      "status": "votes" | "redacted" | "not_in_cvr",
      "votes": [[contest name, expected value], ...],
      "picks": [[contest name, scope], ...],
      "predicted": true,
      "certain": true | false,
      "boundary": [the other boards this ballot may belong to, or empty],
      "source_list": name of the targeted list it came from,   (only in a combined targeted list of the first design)
      "in_rla": true,                                          (only on a targeted extract, for a ballot the RLA sample also holds)
      "targeted_in": "Targeted list 1"                         (only on an RLA extract, for a ballot an earlier sent targeted list holds)
    },
    ...
  ]
}
```

- **`contests`:** the contests the prediction drew ballots for in this county (empty without a prediction).
- **`picks`:** the contests a ballot was drawn for. Statewide and multi-county contests appear alongside the county's own contests, in the prediction's contest order.
- **`location`:** the pull list's storage location column, else the location of the ballot's batch in the county's chosen manifest.
- **`choices`:** only the contests that appear in some ballot's `votes`.

## Canonical serialization and the extract's hash

The canonical serialization is that of the event log (docs/EVENT_LOG.md): JSON with object keys sorted, no insignificant whitespace, non-ASCII characters written as themselves, encoded as UTF-8. In Python:

```python
json.dumps(extract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

The extract's hash is the SHA-256 of these bytes. The extract is stored in the workspace under that hash. It contains no time and no software version, so the same inputs always give the same bytes.

Because the extract holds the seed and the `inputs` object, its hash covers every input and the seed: it is the one digest that identifies the whole audit definition for the county (the seed, the redacted CVR, the manifests, the overrides, the prediction and the pull list), and every page's hash covers it in turn. So a printed page needs only short forms of these two hashes, and the provenance page lists the rest in full.

## Pages

Each board's ballots are split into printed pages by a fixed rule for each print layout. A layout is a letter sheet, portrait or landscape, with one, two or three columns of ballots; a physical sheet is always one page, with one identification line and one QR code:

| Layout | Sheet | Columns of ballots per page | Lines per column | Text |
|---|---|---|---|---|
| `portrait-2` (the default) | letter, portrait | 2 (left, then right) | 87 | 6.5 pt on 7.8 pt lines |
| `portrait-1` | letter, portrait | 1 | 76 | 7.5 pt on 9 pt lines |
| `landscape-2` | letter, landscape | 2 (left, then right) | 64 | 6.5 pt on 7.8 pt lines |
| `landscape-3` | letter, landscape | 3 (left to right) | 64 | 6.5 pt on 7.8 pt lines |

The lines per column are what the PDF's geometry holds (`corla_observer.printlayout`, below): the sheet's height, less a margin of 1/4 inch at the top and bottom and the header band (the QR code, 22 mm, and its quiet zone, 2.7 mm, then 2 points of space), divided by the line height, rounded down. `LAYOUTS` in `corla_observer.extract` is computed from it. So page boundaries, page numbers and page hashes depend on the layout, and a page's record names its layout.

- **A ballot's lines:** `1 + (1 if it is possible or a boundary ballot) + max(1, the sum over its votes of the value's lines)`, where a value's lines are its number of choices (a value of several choices, `A + B + C`, or an overvote, `overvote: A + B`, has one line per choice) and 1 for `no vote` or `redacted`. That is the line with its imprinted ID, a line with its marks (POSSIBLE, BOUNDARY) if it has any, and one line per contest, plus a continuation line for each choice after a contest's first. A redacted or missing ballot has one line saying so.
- **Filling pages:** ballots are taken in board order. A ballot goes in the current column if the column's lines plus its own are at most the layout's lines per column; otherwise it starts the page's next column, or, after its last column, a new page. A ballot longer than a column has a column of its own.

A page's hash is the SHA-256 of the canonical serialization of its page record:

```
{
  "format": 6,
  "kind": "observer_extract_page",
  "layout": "portrait-1" | "portrait-2" | "landscape-2" | "landscape-3",
  "extract_sha256": the extract's hash (lower-case hex),
  "county": the extract's county,
  "board": the board number,
  "page": the page number, from 1,
  "pages": the board's number of pages in this layout,
  "ballots": [the page's ballots, exactly as they appear in the extract's "ballots"]
}
```

The ballots hold the contest and choice names in full, as in the extract: the shortened names on paper (below) are presentation only, and no hash covers them.

Page format 6 is the rule above. (Page format 5 counted one line per contest, a contest's several choices sharing its line, which cut their names on a narrow column; its pages are no longer reproduced, so a version 2 extract built before 27 October 2026 is built again to be printed or checked.) A version 1 extract keeps page format 2, so its committed page hashes still check; it has only the portrait layout, which its printout draws as `portrait-1`'s sheet, and its layout is shown as `portrait`. (Page format 4 had two layouts, `portrait` with 72 lines and `two-up`, landscape with two columns of 66 lines, measured by printing an HTML print view to PDF with a browser, and a 10 to 12 mm QR code; its pages are no longer reproduced, so a version 2 extract built before 26 October 2026 is built again to be printed or checked. Page format 3 split every board at 70 lines per page whatever the layout, a two-up sheet holding two pages side by side, and had no `layout`; page format 2 counted `1 + max(1, number of votes)` per ballot, with no marks line; page format 1 split pages at 40 lines, counting `4 + max(1, number of votes)` per ballot, for a layout that printed each ballot's location, picks and match line on lines of their own.)

The page index lists every page of every layout:

```
{
  "format": 6,
  "kind": "observer_extract_pages",
  "extract_sha256": ...,
  "layouts": {
    "portrait-1": {"columns": 1, "column_lines": 76, "boards": {"1": [{"page": 1, "sha256": ..., "positions": [positions of its ballots]}, ...]}},
    "portrait-2": {"columns": 2, "column_lines": 87, "boards": {...}},
    "landscape-2": {"columns": 2, "column_lines": 64, "boards": {...}},
    "landscape-3": {"columns": 3, "column_lines": 64, "boards": {...}}
  }
}
```

It is stored in the workspace under the SHA-256 of its canonical serialization, recorded in the `extract.built` event and published with the evidence (docs/TIMESTAMPING.md). The spreadsheet gives each ballot's page and page hash in each layout.

## The printout

The printout is a PDF, drawn by `corla_observer.pdfextract` in Python with ReportLab, without a browser: the same file prints the same on any printer, opens on any phone, and has the same bytes whenever it is drawn (below). It is compact, in the style of the observers' printouts from Lori Stevens's RLAAuditHelper (https://github.com/loriinboulder/AuditVerificationAssistant).

When an extract is built, each board's printout is drawn in every layout, stored in the workspace under its SHA-256, and listed in the `extract.built` event (`pdfs`), in the spreadsheet and in the evidence commitment (`derived/extract-COUNTY-boardN-LAYOUT.pdf`, docs/TIMESTAMPING.md). It is downloaded with "Download PDF" links on `/coord/extract` and on the observer's and the practice reader's pages (`/coord/extract/pdf?county=C&board=N&layout=L`, `/observe/TOKEN/pdf?layout=L`, `/read/TOKEN/pdf?layout=L`; a file named like `weld-board1-landscape-2-e462f425.pdf`), or with

```
corla-observer extract print --workspace DIR --county C --board N --format pdf [--layout portrait-2|portrait-1|landscape-2|landscape-3] --out FILE
```

(`portrait-2`, portrait with two columns, by default.)

Print it at actual size (100%), not scaled to fit. There is no HTML print view: a browser's printing depends on its fonts, its margins and the phone, which is what the PDF avoids. The provenance page alone is also a web page (below).

### Fonts and sizes

The text is DejaVu Sans Condensed, regular and bold, and the hashes and identification lines DejaVu Sans Mono (the identification line's two short hashes in DejaVu Sans Mono Bold at 10 pt, letter-spaced by 0.8 pt, the rest of it at 9 pt, so that a phone photo's OCR reads them character by character; its zero is dotted), version 2.37 (https://dejavu-fonts.github.io/), bundled unmodified in `src/corla_observer/fonts/` with their licence (`LICENSE-DejaVu`: the Bitstream Vera and Arev font licences, which allow redistribution and embedding). Each file's SHA-256:

```
8550cd5ca1acb65a8fc7877c46939cd0b4d909f8e7bc1e24716873d918c5e549  DejaVuSansCondensed.ttf
38098d0b8edd8430a0f53fb2831b5d36037563a998a620d3c87a4d7591766d20  DejaVuSansCondensed-Bold.ttf
b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582  DejaVuSansMono.ttf
bce60f1b4421acd9ea51ba6623d7024ecbe6817a953e3654df62a5e6bdf8f769  DejaVuSansMono-Bold.ttf
```

A narrow, freely licensed sans serif whose metrics are fixed by the file in the package, so measuring and drawing agree on every machine. The sizes are set against the July 2026 Weld printout, Courier at 7.46 pt in two landscape columns, found readable: what matters for reading is the x-height (the height of the small letters), and Courier's is 0.426 em, 3.18 pt at that size. DejaVu's is 0.547 em, so 5.8 pt would match it; the multi-column layouts use 6.5 pt (x-height 3.55 pt, 12% larger), and `portrait-1`, which has the room, 7.5 pt (4.10 pt). DejaVu Sans Condensed is also narrower than Courier: about 3.3 points per character at 6.5 pt against Courier's 4.5.

### The provenance page

Each board's printout starts with a provenance page, on a sheet of its own (the layout's sheet), in one column across the full width of the sheet. It comes before page 1 and is not counted in "page N of M", so it changes neither the pagination nor any page's hash. At its top, beside its QR code, are the "keep out of sight" notice and, if the order is predicted, CORLA's posting or targeted, a notice saying so (they are on no other page, since the provenance page is first on paper), and its identification (`Weld · Board 1 · PROVENANCE · extract e462f425`). Then it lists, in full:

- the county, the audit board, the layout, the board's number of pages in this layout and in the others, and the order;
- the seed and the extract's hash;
- each input's SHA-256, by name (pull list, redacted CVR, county manifest, all manifests, overrides, prediction; "none" for an input that is absent);
- the software that built the extract, with its git commit (`corla-observer 0.1.0 (f855b1a1c2d3)`, from its `extract.built` event; not part of any hash);
- the page index's hash (above);
- the key: the abbreviations used in this layout's contest names, "…" if any name is cut, and the marks POSSIBLE and BOUNDARY;
- the full name of each contest printed shorter than as always printed (below);
- how to check a printed page (below).

Hashes are in DejaVu Sans Mono, in groups of 8 hex digits. The page hashes themselves are not listed: they are in the page index, published with the evidence, and in the spreadsheet. If the provenance is too long for one sheet (a county with very many shortened contest names), it continues on another, marked "(continued)", also not counted among the pages.

Its QR code holds the extract's hash: `COX2E:<extract SHA-256 in upper-case hex>`.

### Each page

Every sheet has a header band across its top: the page's QR code at the top right, as tall as the band, and the page's identification line beside it:

```
Weld · Board 1 · page 3 of 21 portrait-2 · AUDIT CENTER ORDER · extract e462f425 · page 743b5032
Weld · Board 1 · page 2 of 20 landscape-3 · AUDIT CENTER ORDER · extract e462f425 · page 8d96c2d7
```

that is: the county, the audit board, "page N of M" and the layout; "PREDICTED ORDER", "AUDIT CENTER ORDER" or "TARGETED (NOT RLA)" if the order is not the pull list's; and the first 8 hex digits of the extract's hash and of the page's hash. It is in DejaVu Sans Mono at 9 pt; where it is too long for the room beside the code it is printed on two lines, broken between its parts. The seed and the inputs are not on the page: the extract's hash covers them, and the provenance page lists them.

The QR code (error correction level M, alphanumeric mode, a version 4 symbol of 33 by 33 modules, drawn as vector squares) holds the page's hash alone, which covers the extract's hash:

```
COX2:<page SHA-256 in upper-case hex>
```

It is 22 mm square, so each module is 2/3 mm, with a quiet zone of 4 modules (2.7 mm) below and beside it, and the sheet's margin above and to its right. That size was chosen by measuring how often it decodes from simulated phone photos of the whole sheet ("How big the QR code is", below). (Page format 4's printouts had a 12 mm code, 10 mm on a two-up sheet; earlier ones used `COX1:<page SHA-256>:<extract SHA-256>`, with the seed and every input's short hash on every page.)

The extract has no round number: its inputs do not say which round a pull list is for, so the identification does not show one.

Then the ballots, down each column in turn, left to right. Each ballot is a line with its place on the board ("41/316") at the left, "ImprintedId 101-1-67" in bold centred between rules, and its location at the right ("loc 3"); then, for a possible or boundary ballot, a line with its marks in bold; then one line per contest on its card in three cells: the contest's name right-aligned in the left half of the column, a check box (2 ems wide), and the choice in bold. A contest with several choices (a multi-seat contest, or an overvote) has its first choice there and each further one on a continuation line below, in the choices' cell, with no contest name and no box. Every second contest (with its continuation lines) is lightly shaded, to guide the eye along it.

In the plain text (`corla-observer extract print --format text`, monospaced, pages separated by form feeds, the provenance page first, without the QR codes):

```
----------------------------- ImprintedId 101-1-67 -----------------------------
   United States Senator - DEM __ John Hickenlooper
            US Rep - CD8 - DEM __ Manny Rutinel
                Governor - DEM __ Michael Bennet
 State Rep - District 48 - DEM __ Dez Packard
 School Board Director At Large __ Malik Oyelaran
                                   Constance Whitby
```

The plain text has `portrait-1`'s pages. The contest names are right-aligned to the page's longest; a continuation line has the choice under the choices, without the `__`. The ID's line is 80 characters wide, and so are the others (apart from the identification line).

As printed:

- a contest's name always loses "(Vote For=1)", "(Statutory)" and "(Constitutional)", and "Representative to the NNNth United States Congress - District N", "Regent of the University of Colorado - Congressional District N" and "State Board of Education Member - Congressional District N" are always shortened to "US Rep - CDN", "CU Regent - CDN" and "State BOE - CDN" (anything after them is kept);
- the choices are those of the extract, one per line (so an overvote is `overvote: A` and then `B` on the line below), `NO VOTE` for no vote, and `redacted` as it is;
- a redacted ballot has the line `REDACTED __ redacted in the CVR: nothing to check`, and a ballot missing from the CVR `NOT IN THE CVR __ this ballot is not in the redacted CVR` (in a column too narrow for these, `redacted: nothing to check` or `nothing to check`, and `not in the redacted CVR` or `not in the CVR`);
- a possible or boundary ballot has, below its ID's line, one line with its marks (in the plain text ` * POSSIBLE: ... * BOUNDARY: ...`), in full if they fit on the line, else shorter ("POSSIBLE: only if the count is above its low estimate", "BOUNDARY: may be Board 2's; skip it if not pulled", then "POSSIBLE" and "BOUNDARY: may be Board 2's").

**No name wraps or overflows.** Each line is drawn as one string at a fixed place, so nothing can wrap; and each contest name, choice and marks line is measured, in the font and size it is drawn in, against the width of its cell (`corla_observer.printnames.budget`), less 0.15 em of space at each side:

| Layout | Column | Room for a contest name | Room for the choices | Room for the marks |
|---|---|---|---|---|
| `portrait-1` | 203.2 mm | 100.8 mm | 95.5 mm | 202.4 mm |
| `portrait-2` | 99.1 mm | 48.9 mm | 44.3 mm | 98.4 mm |
| `landscape-2` | 130.3 mm | 64.5 mm | 59.9 mm | 129.7 mm |
| `landscape-3` | 85.6 mm | 42.1 mm | 37.5 mm | 84.9 mm |
| plain text | 80 characters | 36 characters | 40 characters | 77 characters |

A name too wide for its cell is shortened by these rules, one at a time in this order, only until it fits: "Regional Transportation District" is "RTD", "United States" "US", "State Representative" "State Rep", "State Senator" "State Sen", "County Commissioner" "Co Commr", "Court of Appeals" "Ct App", "Supreme Court" "Sup Ct", "School District" "SD", "Judicial District" "JD", "Ballot Issue" "Issue", "Ballot Question" "Q", "Democratic Party" "DEM", "Republican Party" "REP", "District" "Dist", "County" "Co", and a primary's party " - DEM" (and so on) " DEM". A choice too wide has its first names as initials ("Kelley Anne Dennison" is "K. A. Dennison"; quoted nicknames and bracketed words are kept); each of several choices has the whole cell, on its own line. Only if a name is still too wide is it cut, ending in "…". Two different names never print alike within an extract: if two shortened names would be the same, they are cut in the middle instead, keeping as much of the end of the name (a district number, a party) as it takes to tell them apart. The provenance page lists the abbreviations used and each shortened contest's full name. A name is shortened only as far as its own layout needs: the same contest may be shortened in `landscape-3` and printed in full in `portrait-1`.

### The four layouts compared

Each layout prints the same pages' content, with the same header band and QR code; they differ in how much fits on a sheet and how wide a column is, and so in how many names must be shortened. On Weld's audit board 1 in the July 2026 primary rehearsal (`corla-observer rehearse all`, then `extract build`: 316 ballots in CORLA's posted order, 3,111 contest lines, 38 contest names and 54 choices), measured with `scripts/layout_comparison.py`:

| Layout | Text | x-height (against July's Courier 7.46 pt) | Column | Characters per column | Lines per column | Lines per sheet | Sheets for board 1 (with the provenance sheet) | Contest names shortened | Choices shortened | QR code decoded, whole-sheet phone photo 1600 px wide (1200 px) |
|---|---|---|---|---|---|---|---|---|---|---|
| `portrait-1` | 7.5 pt | 4.10 pt (1.29 times) | 203.2 mm | about 166 | 76 | 76 | 50 | 0 of 38 | 0 of 54 | 12 of 12 (12 of 12) |
| `portrait-2` | 6.5 pt | 3.55 pt (1.12 times) | 99.1 mm | about 93 | 87 | 174 | 22 | 0 of 38 | 0 of 54 | 12 of 12 (12 of 12) |
| `landscape-2` | 6.5 pt | 3.55 pt (1.12 times) | 130.3 mm | about 123 | 64 | 128 | 30 | 0 of 38 | 0 of 54 | 12 of 12 (10 of 12) |
| `landscape-3` | 6.5 pt | 3.55 pt (1.12 times) | 85.6 mm | about 80 | 64 | 192 | 21 | 2 of 38 (232 of the 3,111 lines) | 0 of 54 | 12 of 12 (12 of 12) |

"Characters per column" is the column's width over the mean width of the extract's own names in the text font: the July printout's columns were 80 characters of Courier, about 27 sheets for 281 ballots. In `landscape-3` the two names shortened are Weld's county commissioner contests ("Weld Co Commr - At-Large - REP"); every other layout prints every name in full. The QR code's decoding is the photo check's decoder (below). The default layout is `portrait-2` (`DEFAULT_LAYOUT` in `corla_observer.extract`): it is the first "Download PDF" link on every page and the command's default; the other three stay available, and all four are drawn, stored and committed. The plain text, which is one column, has `portrait-1`'s pages.

### How big the QR code is

A printed page is checked by photographing it, and an observer will often photograph the whole sheet, from a distance, with a phone. The code then has few pixels per module: in a photo of a landscape sheet 1,600 pixels wide, a module of the old 10 mm code was under 2 pixels, too few for zbar. `scripts/qr_size_rates.py` draws page 1 of the synthetic extract's board 1 (`tests/photofixtures.py`) in each layout with the code at each size, the rest of the sheet unchanged, rasterizes it at 300 dpi, and simulates hand-held photos of the whole sheet: on a desk, turned up to 4 degrees, in perspective by up to 5 percent, blurred by 0.5 to 1.2 pixels, JPEG quality 50 to 80, and reduced so the whole photo is 1,600, 1,200 or 1,000 pixels wide (12 photos of each, with their own random amounts; a phone's own photos are 3,000 to 4,000 pixels wide). Decoded by the photo check's decoder (`corla_observer.photoimage.decode_qr`, which also tries the corners enlarged), and, in brackets, by `zbarimg` once on the photo as it is, of 12 (measured 26 October 2026):

| Layout | Photo width | 10 mm | 12 mm | 16 mm | 19 mm | 22 mm | 25 mm |
|---|---|---|---|---|---|---|---|
| `portrait-1` | 1600 px | 7 (0) | 11 (5) | 12 (10) | 12 (12) | 12 (12) | 12 (12) |
| `portrait-1` | 1200 px | 0 (0) | 4 (0) | 12 (2) | 12 (6) | 12 (11) | 12 (12) |
| `portrait-1` | 1000 px | 0 (0) | 0 (0) | 7 (0) | 11 (4) | 12 (6) | 12 (11) |
| `portrait-2` | 1600 px | 10 (0) | 11 (1) | 12 (10) | 12 (12) | 12 (12) | 12 (12) |
| `portrait-2` | 1200 px | 0 (0) | 5 (0) | 7 (5) | 12 (8) | 12 (11) | 12 (12) |
| `portrait-2` | 1000 px | 0 (0) | 0 (0) | 7 (0) | 10 (2) | 12 (6) | 12 (10) |
| `landscape-2` | 1600 px | 2 (0) | 4 (0) | 10 (5) | 12 (8) | 12 (11) | 12 (12) |
| `landscape-2` | 1200 px | 0 (0) | 0 (0) | 7 (0) | 9 (1) | 10 (3) | 12 (6) |
| `landscape-2` | 1000 px | 0 (0) | 0 (0) | 2 (0) | 6 (0) | 7 (0) | 12 (1) |
| `landscape-3` | 1600 px | 2 (0) | 3 (0) | 12 (4) | 12 (10) | 12 (12) | 12 (12) |
| `landscape-3` | 1200 px | 0 (0) | 0 (0) | 5 (0) | 9 (0) | 12 (4) | 12 (4) |
| `landscape-3` | 1000 px | 0 (0) | 0 (0) | 1 (0) | 4 (0) | 4 (0) | 9 (3) |

The old sizes (12 mm portrait, 10 mm two-up) decode from close photos and scans, but rarely from a whole-sheet photo, least of all on a landscape sheet. At 22 mm (modules of 2/3 mm) every layout decodes from every 1,600-pixel photo, and nearly every 1,200-pixel one, also with a plain decoder. 25 mm helps only below that, at the cost of about one more line per column, so the code is 22 mm. Under the photo check's own degradations (`tests/photoimages.py`, from a 200 dpi raster), every size decodes from every condition but a 2-pixel blur, which needs 19 mm (22 mm on `landscape-3`).

### Reproducing the PDF's bytes

The same extract, board, layout and software string always give the same PDF, byte for byte, so anyone with this software can check that a printout is the one committed: draw it again (`corla-observer extract print --format pdf`, or `corla_observer.pdfextract.render_pdf(extract, sha256, board, layout, software=...)`, where the software string is the `software` of the extract's `extract.built` event) and compare its SHA-256 with the event's `pdfs`. The software string names the software's version and git commit (`corla-observer 0.1.0 (f855b1a1c2d3)`, with `+dirty` if it ran with changes not in that commit; `corla_observer.version`), and is printed on the provenance sheet, so a build by another commit gives different bytes, as intended: the same extract and the same software commit always give the same bytes, and the printout says which commit drew it. To reproduce a printout byte for byte, check out that commit (`git checkout f855b1a1c2d3`, whose page is on GitHub) and draw it with that software string. What makes it reproducible:

- ReportLab's invariant mode: no timestamps or random names anywhere in the file;
- no creation or modification date in the document's information, which holds only its title (`Extract: Weld, audit board 1, landscape-2`), its subject (the extract's and page index's hashes), and creator and producer `corla-observer`;
- the document ID is the first 16 bytes of the SHA-256 of the UTF-8 text `corla-observer extract printout`, the extract's hash, the page index's hash, the layout, the board number and the software string, one per line (`corla_observer.pdfextract.document_id`);
- the bundled fonts (above), subset to the characters used, in the order they are drawn;
- the QR codes drawn from segno's matrix, as filled squares.

The bytes also depend on the ReportLab version, pinned in `uv.lock` (5.1.0), and the code. `tests/test_pdfextract.py` builds the PDFs twice, and in a fresh interpreter, and compares the bytes, and pins the SHA-256 of a small extract's PDF in each layout, so that any change to the library, the fonts or the drawing is noticed.

### On screen

The live observation page shows each ballot with a header that identifies it, in both entry modes: its imprinted ID (bold, at the left) and "Ballot 41 of 316" (at the right) on one line, then the extract's short identification, `Weld · Board 1 · extract e462f425` (the short hash in bold), and beside them a small QR code for that ballot:

```
COX2S:<the first 32 hex digits of the extract's SHA-256, upper case>:<board>:<the ballot's board_position>
```

for example `COX2S:E462F425D1A2...:1:41` (error correction M, a version 3 symbol, 29 modules). The extract's hash commits to every input and the ballot's place on its board locates it exactly, so a screenshot or photo of the screen can be checked against the committed extract (docs/PHOTO_CHECK.md); 128 bits of the hash are plenty to find the extract among those a workspace built, and keep the code small enough for a corner of a phone screen. The header stays at the top while a long ballot scrolls, so every screenshot of a ballot carries it.

The contests are shown as on paper: the contest's name right-aligned, a box (or, contest by contest, the two readings' outcomes) in the middle, and the choices left-aligned, one per line. The names are those of `corla_observer.printnames` for the layout `screen`: measured for a phone 390 pixels wide, shortened by the same rules as on paper so that most rows stay one line, a choice never cut (with initials if too wide, else wrapping), a contest's name cut only beyond two lines' width. The provenance (the provenance page as a web page, without the notices) and the "Download PDF" links are under the page's **More**. The provenance holds only the seed, which is public once rolled, and hashes; coordinators reach it at `/coord/extract/provenance`, and whoever holds an observer's or a reader's link at `/observe/TOKEN/provenance` or `/read/TOKEN/provenance`.

The spreadsheet (CSV) starts with identification rows (the county, the notice, the order if not the pull list's, the seed, each input's hash in full, the extract's hash, the page index's hash, and each printout's PDF hash, `PDF: LAYOUT, board N`), then a blank row, then one row per ballot (a boundary ballot once per board it is listed on), with columns `certain or possible` and `boundary` (the hint, or empty), and the ballot's page and full page hash in each layout (`page (portrait-1)`, `page hash (portrait-1)`, and so on for `portrait-2`, `landscape-2` and `landscape-3`). It has the names in full. Any cell that starts with `=`, `+`, `-`, `@`, a tab, a carriage return or a line feed is prefixed with `'`, so that no spreadsheet reads it as a formula.

## The event

`extract.built`, with `county` and `file_sha256` set to the county and the extract's hash. Its `data` holds:

- `sha256`, `size`, `format` and `purpose`;
- `order` and `board_split`;
- `inputs`, as in the extract;
- the counts: `ballots` (distinct ballots), `boards` (ballots listed per board, boundary ballots counted on each), `possible`, `boundary`, `redacted` and `not_in_cvr`;
- `sources_disagree` and `other_source`: whether the pull list and CORLA's posting disagree, and which is the check (a disagreement is a high-severity flag, `extract_sources_disagree`);
- `pages`, per layout, the number of pages per board;
- `page_index_sha256`;
- `pdfs`, per layout, each board's printout's SHA-256 (the PDF is stored in the workspace under it);
- `software`, the version and git commit that built it (printed on the provenance page, and so part of the PDF);
- `in_rla` and `previously_targeted`, how many ballots the overlap checks found ("Pull lists" above);
- for a targeted extract only: `list` (the list's key), `list_name`, `combined` (whether the first design combined it from several lists) and `list_record_sha256`, the hash of the list's stored record (docs/TARGETING.md: its sources and its ballots with their sources), kept in the workspace and out of the published log until a coordinator publishes the list's details. The send itself is a `targets.sent` event.

A targeted list is retired and restored with `extract.retired` and `extract.restored`, and its details are put on the public dashboard with `extract.details_published` (which carries them: the name, the draws' patterns in words, and the imprinted IDs) and taken off with `extract.details_withdrawn` (`corla_observer.pulllists`).

## Checking an extract

To recompute an extract:

1. Take the input files by their hashes (on the provenance page, or in the `extract.built` event) from the published evidence.
2. Follow the rules above.
3. Compare the SHA-256 of the canonical serialization with the extract's hash.

To check a printed page with this software, photograph or scan it and use `corla-observer photo check` or the "Check a page" web page: it reads the QR code and the printed text and compares every line with the page as committed (docs/PHOTO_CHECK.md). By hand, or without this software:

1. Read its QR code (`COX2:` and the page's full hash), or its short hashes.
2. Find that hash in the page index (published with the evidence; the provenance page has its hash) or the spreadsheet. To check the index itself, recompute the extract (above) and the board's pages from it in the page's layout, and find the page whose hash matches.
3. Compare the imprinted IDs and choices printed on the photographed page with that page's ballots.

For example, in Python, given the extract's bytes:

```python
import hashlib, json

LAYOUTS = {"portrait-1": (1, 76), "portrait-2": (2, 87), "landscape-2": (2, 64), "landscape-3": (3, 64)}  # columns per page, lines per column

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def split(extract, board, layout="portrait-1"):
    columns, column_lines = LAYOUTS[layout]
    pages, lines = [], 0  # each page a list of columns, each a list of ballots
    for b in (b for b in extract["ballots"] if b["board"] == board):
        marks = (not b["certain"]) or bool(b["boundary"])
        body = sum(max(1, len(v.removeprefix("overvote: ").split(" + "))) if v not in ("no vote", "redacted") else 1 for _, v in b["votes"])
        need = 1 + (1 if marks else 0) + max(1, body)
        if pages and lines + need <= column_lines:
            pages[-1][-1].append(b); lines += need
        elif pages and len(pages[-1]) < columns:
            pages[-1].append([b]); lines = need
        else:
            pages.append([[b]]); lines = need
    return [[b for column in page for b in column] for page in pages]

def pages(extract, sha, board, layout="portrait-1"):
    split_ = split(extract, board, layout)
    return [
        hashlib.sha256(canonical({"format": 6, "kind": "observer_extract_page", "layout": layout, "extract_sha256": sha,
            "county": extract["county"], "board": board, "page": n, "pages": len(split_), "ballots": page})).hexdigest()
        for n, page in enumerate(split_, start=1)
    ]

def page_index_sha256(extract, sha):
    layouts = {}
    for layout, (columns, column_lines) in LAYOUTS.items():
        boards = {}
        for board in sorted(int(b) for b in extract["boards"]):
            boards[str(board)] = [{"page": n, "sha256": h, "positions": [b["position"] for b in page]}
                for n, (page, h) in enumerate(zip(split(extract, board, layout), pages(extract, sha, board, layout)), start=1)]
        layouts[layout] = {"columns": columns, "column_lines": column_lines, "boards": boards}
    index = {"format": 6, "kind": "observer_extract_pages", "extract_sha256": sha, "layouts": layouts}
    return hashlib.sha256(canonical(index)).hexdigest()

data = open("extract.json", "rb").read()
sha = hashlib.sha256(data).hexdigest()
extract = json.loads(data)
assert canonical(extract) == data
print("extract", sha, "seed", extract["seed"], "inputs", extract["inputs"])
print("page index", page_index_sha256(extract, sha))
print(pages(extract, sha, 1), pages(extract, sha, 1, "landscape-3"))
```
