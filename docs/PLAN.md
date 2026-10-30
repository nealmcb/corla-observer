# Independent CORLA observer support: implementation plan

Version 0.4 (draft), 2026-10-25

This plan is written to become the first document of a new, public repository. It refers only to public sources.

## Goal

A hosted tool that supports independent public observation of Colorado's risk-limiting audits (RLAs), which counties run with the state's CORLA software. It is to be running for the November 2026 general election pilots (state dice roll on 16 November, county audits from 16 to 18 November).

A major aim is to make observation easy to start. Printing the extract (one block of expected votes per selected ballot) can run to dozens or hundreds of pages when there are many selections and many contests. So observers should be able to follow on a phone or laptop instead, while paper stays fully supported for coordinators or observers who prefer it.

The tool will:

1. **Audit center data:** pull the latest data from the Secretary of State's audit center.
2. **County files:** accept the files the project team gets from each county. The redacted cast vote record (CVR) arrives before the drawing, and the county's pull list (CORLA's "List of ballots to audit") arrives afterwards.
3. **Redaction check:** confirm each redacted CVR is adequately redacted as provided, or report rare-style names and counts only if there are issues.
4. **Consistency checks:** check ballot manifests, CVRs and counts against each other.
5. **Timestamping:** commit to all of this evidence before the random seed is rolled, and commit to the derived extract before the audit starts.
6. **Seed:** accept the seed by hand once it is announced.
7. **Selections:** predict each county's selected ballots, and check them against an independent implementation and the county's pull list.
8. **Extract:** produce the observers' extract for printouts, spreadsheets and the online entry site.
9. **Live observation:** support up to several observers per audit board as they follow the read-out ballot by ballot, each on the medium they choose (paper, spreadsheet or online), with online progress pushed live to a public status dashboard that needs no login. The dashboard is shared at first only within a small circle.

## Principles

- **Observers verify; they do not relay or overrule.** The tooling just records what observers heard against what the CVR says.
- **An observer attests only to what they looked at.** Each observer follows the read-out on one medium, because it's hard to watch both while listening. Each record indicates which medium was used.
- **Several observers per board, each in their own way.** One may mark paper while another enters on a phone for live coverage and a third uses a spreadsheet. Their records are independent and are compared afterwards.
- **Software independence.** The design is intended to be able to guarantee that error in this software, and no compromise of it, may change what observers attest to without being detectable (see the section below and help review this).
- **Anyone can check the work.** Every derived result can be recomputed by others from committed public inputs, with published code and instructions.
- **The project team handles the county data.** A coordinator for each county uploads what the county provides. Counties are not responsible for using or needing to understand any of this tooling.
- **No improperly redacted CVR is ever stored.** A file that fails the redaction check is rejected and not kept anywhere, including backups.
- **Two taps per match.** Each ballot gets a match button and a free-text box for what was heard instead. A match takes two taps: "First reading" after the first audit board member reads the ballot, then "Confirmed match" after the second reading. This guards against mis-taps and keeps observers following both readings.
- **Everything is recorded and recoverable.** Every event and every data entry carries a timestamp and the id of whoever is responsible for it. Everything accepted is backed up off the server as it arrives.
- **Built to be attacked.** The site is public and online, so it is designed for hostile traffic, and every design choice favours integrity and recovery over convenience.

## Software independence and verifiability

The tool could fail observers in three places:

1. **The reference extract:** the predicted selections and the expected votes in the extract. A wrong reference would mislead observers toward false matches or false discrepancies.
2. **The record:** what observers entered. Entries could be lost, altered or invented.
3. **The summary:** the dashboard, which could show anything.

Each failure is made detectable by something outside the software.

### The reference

- **Committed inputs:** the redacted CVRs, manifests and audit center files are hashed and timestamped before the seed is rolled.
- **Deterministic derivation:** the predicted selections and the extract are a deterministic function of the committed inputs plus the public seed and the county's pull list, computed by published, versioned code.
- **Committed before the audit:** the printed extract and its per-page hashes are committed and timestamped before the audit starts, so they can't be changed afterwards to fit.
- **Three-way agreement:** before the audit, the tool compares three independent results:
  - its own prediction
  - an independent implementation, the RLAAuditHelper at https://github.com/loriinboulder/AuditVerificationAssistant
  - the county's CORLA pull list

  Any disagreement is reported before the first ballot is read.
- **Seed:** observers note the seed announced at the public ceremony and compare it with the seed shown on their screen or printout.
- **Printed and on-screen identification:** every page or screen of the extract shows:
  - the seed
  - the hashes of the inputs
  - the extract's own hash
  - its position ("page 3 of 12", or "ballot 41 of 316")
  - a QR code carrying the page's own hash
- **Photo check:** tooling is available to take a photograph of a printed extract page, read its QR code and printed text, and confirm that every imprinted ID and expected vote on it matches the committed extract. A coordinator can then confirm a printout is genuine before observers rely on it.

### The record

- **Paper:** the observer's marked printout, initialed and dated, is a record outside the software. It can be photographed at the end of the day, and the photographs hashed and committed.
- **Screen:** each entry made on a phone or laptop produces a receipt (next subsection). The observer's own device keeps every entry and every receipt, independently of the server.

### The summary

The hash-chained event log is published, so anyone can recompute the dashboard's figures from it. An observer can confirm that their own entries are in it, unchanged.

### Receipts and observer-held records

Each observer's device holds a full record of what that observer entered. It can be checked against the published log later.

- **Device key:** when an observer link is first opened, the browser generates a signing key pair with the Web Crypto API. The private key is marked non-extractable and stays on the device. The public key is registered with the server under the observer ID.
- **Signed entries:** every entry (match tap, note, edit, undo) is signed on the device before it is sent. The signature covers:
  - the observer ID
  - the county, board and ballot (imprinted ID and position)
  - the entry content
  - the device's clock time
  - a per-device sequence number
  - the hash of the device's previous entry

  The server cannot forge or alter an entry undetectably, because it doesn't hold the key.
- **Receipt:** the server answers each entry with a receipt containing:
  - the entry's hash
  - its position in the server's hash chain
  - the server's receive time
  - the current chain head
  - the server's own signature over these
- **Device-held record:** the browser stores each entry and its receipt locally in IndexedDB as they happen, whether or not the network is up. The observer can download the record at any time as one small file (JSON plus a readable CSV), and the page offers this automatically at the end of the session.
- **Checking later:** a published checker script takes the observer's downloaded record and the published event log, and reports:
  - every entry of the observer's that is missing from the log or differs from it
  - every entry in the log under the observer's ID that the device did not make
  - whether the chain is intact between published anchors
- **Limits:**
  - The page's JavaScript comes from the server, so it is published in the repository with a version and hash. It is served with subresource integrity, so others can check what code observers ran.
  - An observer who wants more independence can use paper, or both: paper on their own, and a screen held by another observer.

## Architecture

- **A Python package, `corla_observer`,** with a library layer, a Typer command line and a FastAPI web app. It brings in:
  - a reimplementation of CORLA's sampling: its SHA-256 pseudo-random number generator, the combining of county manifests into sampling domains, and sample selection. The project's existing code for this has been checked against CORLA's historical selections and is to be copied into this repository with its tests.
  - CVR reading tolerant of county export quirks: Excel `="..."` wrapping, Latin-1 files, redaction markers, aggregate rows.
  - the redaction analysis from the public anonymizer, https://github.com/loriinboulder/anonymize_cvr
  - the extract and spreadsheet generation of the RLAAuditHelper, also used as the independent check of the selections
  - verification of OpenTimestamps and Sigstore proofs
- **One workspace per election** stores every accepted file unmodified, named by its SHA-256. A SQLite database holds an append-only event table and derived tables (checks, predictions, ballot observations).
- **Event log:** every action is an event, written before it takes effect and never edited or deleted. Each event records:
  - **When:** the server's receive time (UTC, to the millisecond), the client's time when the entry was made, and, for queued entries, both.
  - **Who:** the coordinator login, the observer ID, or "system".
  - **What:** the kind of event, the county and audit board, and the data entered (match, note text, edits), with the device signature for observer entries.
  - **Context:** the file hash or ballot it concerns, and the client's session and device ID.

  Covered actions include uploads, check results, fetches, seed entries and second entries, timestamp commitments, logins and link use, page views of each ballot, every match tap and note, and every edit or undo. Derived tables are rebuilt from the event log, and a full export of the log is available for analysis after each audit.
- **Live updates** are pushed to browsers with server-sent events, so the dashboard updates within a second or two of each tap.
- **Accounts:**
  - coordinator logins, which can upload and enter the seed
  - per-observer links scoped to one county and one audit day, each carrying a stable observer ID and a device key, which can record observations
  - a public dashboard that needs no login, shared during the pilot only within a small circle
- **Evidence:** hash commitments are published in the public audit center mirror, https://github.com/nealmcb/auditcenter, as in July 2026.
- **Verification kit:** a "how to check us" guide and scripts in the repository, covering:
  - recomputing the selections and the extract from the committed inputs
  - verifying the timestamps
  - checking the event log chain and its anchors
  - checking an observer's device-held record against the log

## Security and robustness

### Threats

The site is a plausible target around an election:
- denial of service on audit day
- forged or altered observer entries
- tampering with stored evidence
- account takeover
- malicious uploads, such as oversized or malformed files
- accidental exposure of private data
- ordinary failure: a crashed server, a corrupted disk, an operator mistake

### Separation

- **The public dashboard is read-only,** served as cached snapshots behind Cloudflare, so traffic to it never reaches the write path. If it is overwhelmed, observers' entry keeps working.
- **Write endpoints** (uploads, seed entry, observations) require a coordinator login or an observer link. They are rate-limited per account and per address, and protected against cross-site request forgery.
- **Admin access** to the server is by SSH key only, from named people, with a firewall allowing only Cloudflare's addresses to reach the web service.

### Integrity of the record

- **Hash chaining:** each event carries the SHA-256 of the previous event, so any later alteration or deletion breaks the chain and is detectable.
- **Anchoring:** the head of the chain is signed and timestamped (OpenTimestamps and Sigstore) and published at fixed points: before the seed, when the extract is committed, at the end of each audit day, and when an audit closes.
- **Device signatures:** observer entries carry signatures from keys held only on the observers' devices.
- **Observer links** are random, revocable and limited to one county and one day.

### Capture and backup as data arrives

- **Files:** every accepted upload, fetched audit center file and evidence file is written to the workspace and copied immediately to off-site object storage with write-once retention (DigitalOcean Spaces or Cloudflare R2 with object lock), so a compromised server can't delete or overwrite the backup.
- **Database:** continuous replication of the SQLite database and its write-ahead log to the same storage (for example with Litestream), so the state can be restored to within seconds of a failure.
- **Event log:** each event is also appended to a plain JSONL file that is replicated to the same storage. This lets the record be reconstructed even without the database or in case of an incident.
- **Second copy:** a scheduled pull of the backups to a machine outside the hosting provider.
- **Restore:** a documented restore procedure, tested during the rehearsal before the November pilots.
- **Observer devices:** the observers' devices hold a further copy of every entry they made.

### Input handling

- **Upload limits:** uploads have size limits per file type, are parsed in a separate worker process with time and memory limits, and are never executed or rendered as HTML.
- **CSV handling:** CSV content is treated as data only. Spreadsheets produced for observers are escaped against formula injection.
- **Rejected files:** a CVR failing the redaction check is discarded after its hash and the counts-only result are logged.

### Operations

- **Dependencies:** pinned with hashes, and updated deliberately.
- **Server:** automatic security updates, a configuration kept in the repository, and no secrets in the repository.
- **Monitoring:** uptime and error monitoring with alerts to the coordinators, and a status note on the dashboard if entry is degraded.

## Hosting

**Recommendation:** a small DigitalOcean droplet (Ubuntu, 2 GB) running the FastAPI app under systemd with SQLite on its disk, with Cloudflare in front for DNS, TLS, caching of the public dashboard and protection against denial of service. Backups go to object storage with object lock, as above.
- **Why not Cloudflare Workers:** they would mean rewriting the Python in JavaScript or running it in the Workers Python runtime, and the large CVR uploads (100 MB to 300 MB) would have to go through R2 storage.
- **Staging:** a staging copy on a developer machine is used for rehearsals.

## Components, in pipeline order

### 1. Audit center fetcher

- **Fetching:** fetch the audit center pages and the files they link to, recording each fetch in the event log with URL, time, SHA-256 and headers.
- **Stay current:** try to re-fetch manifests, target-contest files and the seed. A fetcher that skips files it already has will miss corrected files.
- **Change detection:** strip Cloudflare's per-request tokens before comparing pages.
- **Alerts:** flag data that changes or disappears, or two counties serving identical files. In July 2026 one county's manifest was briefly served under other counties' names.
- **Change flags:** each election has a roll time: the earliest of the seed's entry, a roll time a coordinator records, and a scheduled roll time set as an override, kept in the data map with its source. Every manifest, CVR, target-contest list, contest list or county-ID file that is new, changed or removed after it, fetched or uploaded, is a high-severity flag, with its time and both hashes. So is every manifest or CVR upload that CORLA's upload status records after the roll or marks HASH_MISMATCH or FAILED, a posted or provided manifest that is not byte-identical to the one CORLA records importing, and a manifest posted under another county's name or identical to another county's file. Any other file changing, and any change after the first round starts, is a normal flag. The fetcher raises flags as it fetches; the data-map check derives them all again from the log, so a roll time learned late still counts, and `corla-observer flags` lists each one with what to investigate. On the July 2026 data these flags fire: a manifest imported after the roll, whose posted copy listed one batch twice where the imported file had two, changing one selected ballot. Which county, and whether the difference is real, is for the coordinator to take up with the county.
- **Find data where it appears.** The audit center's layout, file names and column headers change from election to election. The fetcher and loaders locate each kind of file (manifest, target contests, contest list, seed, selections) by its content, such as header signatures and expected columns, not by fixed names or paths. Each election gets a small mapping record of what was found where, kept in the event log. Unrecognized files are kept and reported, never silently dropped.

### 2. Uploads

An upload page (and command-line equivalent) for coordinators takes:

- **Before the drawing:** the redacted CVR, and the county's own copy of the manifest if available.
- **After the drawing:** the county's pull list, which assigns ballots to audit boards in pull order.
- **The number of audit boards** the county will use, which determines how the pull list is split.

Each file is hashed on arrival, recorded with uploader and time, backed up, and then checked (3 and 4). The page shows, per county, what has arrived and what is missing.

**File identity.** We publish our own SHA-256 of each file we receive, as part of the commitment before the seed.
- **County-published hash:** a county may also publish a hash of the file it provides. If it does, we record that hash and compare it with ours.
- **CORLA's recorded hash:** CORLA records the hash of each county's uploaded, unredacted CVR. When a county's CVR needed no redaction, the file we receive may be identical to that upload, so we compare it with CORLA's recorded hash when that is published.
- **How to present the hashes:** the comparisons are open question 9.

**Later option: the county data bundle.** As the project's public site describes, a county may post a bundle (redacted CVR, manifest, contest results, the list of audited ballots with board assignments) on its own website. The tool would then fetch the bundle from the county's address, recording the address and hash. It would also check that the CVR's vote totals add up to the county's published results.

### 3. Redaction check

The check judges what the county actually provides. It runs the anonymizer's redaction analysis on the file as uploaded:

- **Grouping:** ballots are grouped by style (the set of contests with visible votes), plus precinct and ballot type whenever the file carries those columns with values. Rare information leaking through ballot type fails a file just as precinct does.
- **Marked rows still count:** a row marked redacted that still shows its votes is grouped like any other ballot, since its votes and contests are visible. Only rows with no votes left, and rows that combine several ballots, are excluded.
- **Pass:** no group of fewer than 10 ballots remains.
- **Fail:** for example, a redacted CVR that keeps precinct information and so leaves rare style-plus-precinct rows.
- **Report:** counts and style/precinct info only, never votes. That is, the number of styles and groups, the sizes of any rare groups, and the number of rows already redacted.

Work: with the anonymizer's maintainer, make its analysis importable without the GUI, with a function that returns a structured result instead of printing.

### 4. Consistency checks before the seed

A per-county report, run on upload and on each manifest fetch:
- per-batch CVR row counts against the manifest
- the manifest total against CORLA's ballot-card count (a shortfall is the signature of a batch missing from the published manifest)
- imprinted-ID coverage
- duplicate files across counties

These are warnings for the coordinator, never silent corrections.

### 5. Timestamping

Turn the [July 2026 procedure](https://github.com/nealmcb/auditcenter/blob/main/observerfiles/evidence-timestamping.md) into one command:

1. Write a new, versioned `SHA256SUMS` file. Earlier versions are never overwritten.
2. Stamp it with OpenTimestamps.
3. Sign it with Sigstore, keyless.
4. Commit and push it to the mirror.
5. Later, upgrade the OpenTimestamps proofs.

It runs:

- **before the seed,** for the inputs
- **when the extract is ready,** for the extract and its page hashes
- **at the anchoring points** of the event log

The web app shows a countdown to the scheduled roll and which counties are still missing files, so the first commitment is made well before the roll. Late files get a new version.

### 6. Seed entry

A coordinator enters the 20-digit seed from the public ceremony, and it takes effect at once. A second entry by another coordinator is optional, since observers compare the seed with the ceremony and the audit center anyway: if one is made, it is compared with the seed in effect, and a mismatch is a high-severity flag and a data map problem until the seed is entered again. Any later entry that changes the value is recorded and flagged as high severity. When the seed later appears on the audit center, the data map compares the two and raises a high-severity flag on any mismatch.

### 7. Selections

- **Prediction:** compute each county's selected ballots from the seed and the manifests as soon as the seed is entered.
- **Independent check:** run the RLAAuditHelper on the same inputs and compare.
- **Pull-list check:** when the county's pull list is uploaded, check every ballot on it against both, and report any ballot missing or extra.
- **Order:** the extract and the live page follow the county's list order and board assignment, since that is what the audit boards follow.
- **A downloaded ENR file.** A coordinator can upload a Clarity ENR file downloaded by hand (for example `detailxml.zip` or a county's summary) when the automatic fetch is blocked or behind. It updates the margins and estimates like a fetched one, and its totals are checked against the other results, the CVR tallies and CORLA's tallies, with any difference flagged.

### 8. The extract

One extract per county, computed from the pull list and the redacted CVR, committed before the audit, and shown in several forms with the same content:

- **Screen:** the live observation page (9). This is the default, with no printing needed.
- **Printout, optional:** for coordinators or observers who want it:
  - one block per ballot in pull order, with the imprinted ID large and bold on the same page as its ballot
  - a mark column and a "keep out of sight" notice
  - a PDF, reproducible byte for byte, in four layouts (portrait or landscape, with one, two or three columns of ballots)
  - the identification on every page described under software independence
- **Spreadsheet, optional:** for observers who prefer to follow on a laptop or tablet spreadsheet.
- **Content:** a ballot whose CVR row was redacted is shown as redacted, and statewide picks appear alongside the county's own picks.
- **Photo check:** the tool that confirms a photographed printout page matches the committed extract, from its QR code and printed text (`docs/PHOTO_CHECK.md`).
- **Check-a-page web page:** for anyone holding a printed page or a photo of one, without running code. It reads the page's QR code from an uploaded photo or the phone's camera, or takes the short hashes typed in when the code won't read; finds the page in the page index; reads the printed text and compares every imprinted ID and choice with the page as committed, giving a verdict (MATCH, MATCH WITH UNREAD LINES, MISMATCH or UNKNOWN PAGE); shows that page as committed beside the photo, line by line; and shows the commitment's timestamp proofs (the Bitcoin block, the Sigstore entry, the commit). Coordinators can check any page; observers and readers, through their links, their own county's; the public only after the audit, if the server is configured for it. The short hashes only locate a page; the QR code's full hash is the check. The recipe in `docs/EXTRACT.md` remains the independent route.
- **TODO: judge what matters for verification.** Instead of requiring every printed line to be read exactly, judge each ballot by what a wrong printout could mislead an observer about: every contest on the ballot appears, with no extra or missing contests; and for each contest, the printed choice is more likely the expected choice than any other legal choice in that contest (comparing the OCR reading against the contest's candidate list, not just against the expected text), with "NO VOTE", overvotes and redacted handled the same way. A line whose reading is closer to a different legal choice is a MISMATCH; a reading that is garbled but closest to the expected choice by a clear margin counts as confirmed. This is likely faster (fewer re-reads) and more relevant than exact-text matching. The QR code and identification line still establish which page it is.
- **TODO, later: video of an observer at work.** Check a video of an observer working: every screen they see on the observation page, and every printed page they fill in, is the proper committed one for that board and position, in order, with nothing substituted or skipped.

### 9. Live observation page

A page for one observer at one audit board, laid out for phones and laptops, built from the committed extract:

- **Before the first ballot:** a short notice, the first time the link is opened on a device and again in each new browser session and on each new day: keep the screen out of sight of the audit board and the public, in a sentence, and the essentials (the choice of entry mode, contest by contest chosen unless the observer chose two taps before, how to record something different, a ballot read out of order), with one "I understand, start" button, whose tap is recorded as a signed entry, with the entry mode in effect as another. A one-line reminder stays in the page's header.
- **Only what is needed:** the ballot, the observer's place and the buttons. Everything else (the entry mode, downloading the record, checking a printed page, the provenance, the PDFs, Dim, the full help) is under one "More" button.
- **Next ballot:** it shows the next expected ballot. Its header, on one line, is its imprinted ID (bold, at the left) and "Ballot 41 of 316" (at the right), then the extract's short identification and a small QR code for the ballot; it stays in view as a long ballot scrolls. Its expected votes are laid out as on paper: the contest's name right-aligned, a box in the middle, the choices left-aligned, a contest's several choices one per line, with the printout's shortened names measured for a phone.
- **Checkable from a screenshot:** every screen showing a ballot carries the county, board, ballot position, imprinted ID, the extract's short hash and the QR code, so a screenshot or photo of it is checked against the committed extract like a printed page (docs/PHOTO_CHECK.md, "The observer screen").
- **Two taps (the other entry mode), two controls per ballot:**
  - a match button, which is what nearly every ballot gets: tapped once after the first reading ("First reading"), and again after the second ("Confirmed match"), both taps signed and timed
  - a text box for what the observer heard instead, which covers a different ballot ID, a different vote, a skipped ballot or anything else, in the observer's own words
- **Advancing:** the page then moves to the next ballot. Observers can scroll back and edit, and edits are recorded as new events, not overwrites: a changed ballot shows as corrected, with the earlier entry shown when it is opened. A first reading tapped survives a reload. An ID heard that is not on the board's list stays in the observer's own list.
- **Contest by contest (the default):** an observer enters each ballot contest by contest unless they choose two taps, on the notice or with the switch at the top of the page; the choice is kept on the device, recorded as a signed event (as is the mode in effect at each start from the notice), and can be changed at any time. Older records, from when two taps was the default and only a change was recorded, are read as two taps where nothing else shows the mode. The ballot's contests are rows (the contest, the expected choice, and the outcome in each of the two readings), with one row in focus under a sticky header (the imprinted ID, "Ballot 7 of 20", the reading, "contest 23 of 61"):
  - **Keys:** Enter confirms the focused contest and moves on; typing a letter or digit opens a box for what was heard instead, with the contest's choices, NO VOTE, overvote and the observer's own words suggested; "?" or "/" marks it not heard clearly. Arrows, Tab, Alt+J and Alt+K move between contests, PageUp, PageDown, Alt+H and Alt+L between ballots; Home or Alt+R starts the second reading; Alt+N or F8 goes to the next unresolved contest (not entered in the second reading, not heard clearly, or heard differently in the two readings) on this ballot and then earlier ones. At the last contest a prompt asks before the second reading starts or the ballot is done, and a held Enter can't pass it. Ctrl+J, K, H and L are left to the browser, which keeps them; the "Keys" help says why.
  - **Touch:** a tap on a contest confirms it; a pencil and a "?" beside each are the other two outcomes; the tools are along the bottom, within a thumb's reach.
  - **Recording:** each outcome is a signed entry in the device's chain with the contest, the reading and the outcome, a change is a new entry marked as a correction, and a ballot can be marked done. The entries are kept on the device at once and sent in small batches, each with its own receipt. A ballot counts as done when it is marked done or every contest has an outcome in the second reading, and a board summary lists the unresolved contests with links to them.
- **Saving:** each entry is signed on the device, stored locally and sent at once. The receipt comes back within a second or two, and the page shows clearly whether it is live or queued. The status in the header is a short label ("Live", "Sending 3", "Queued 3", "Offline 3", the full description as its tooltip), and the header's changing parts (the status, "n of N done", "Ballot n of N", the reading, the contest count, what is left to send, the mode switch) keep the width of their widest value, so the page does not shift or rewrap as the observer works. If the connection drops, entries queue on the device and are sent when it returns.
- **Several observers per board:** each has their own link and record, and the dashboard shows each board's coverage by observer and by medium.
- **Paper observers:** an observer working on paper can register at the start (name, board, "paper"), so the dashboard shows that the board is covered. They can upload photographs of their marked pages at the end of the day.
  - **Breaks:** observers should not be expected to catch up or upload during the audit. In July 2026 there was only one break, after one to two hours, with barely time to catch up.
- **Silence:** no sound or vibration, and a dark, low-brightness option.
- **Public dashboard:** no login is needed. Per county and board it shows ballots done, and (perhaps) matches and notes, and which observers and media are covering it, updated live. Its address is shared at first only within a small circle. What it shows during the audit can be configured (open question 3). It does not show the change flags, many of which are addressed before the public would see them: they are for coordinators, who mark each one addressed with a note, and see the addressed ones separately.
- **Export:** each observer can download their device-held record at any time.
- **Discrepancy guidance:** the page, the dashboard and the project website show the agreed procedure for a discrepancy:
  - note what was heard, without interrupting the audit board
  - recheck the ballot with county staff after the board's submission, where the county allows it
  - compare with the state's discrepancy count when it is published
  - report to the coordinator

  It also explains that observers do not overrule the audit, and that one missed or mistaken observation has only a small effect on the audit's risk.

### 9a. Targeted ballots

Ballots the observers choose themselves, outside the RLA's random sample, which the county pulls and examines at the observers' request and which are not entered into CORLA. Anyone may define and prioritise them however they like; the tool only makes it easier. The requirements and design are in `docs/TARGETING.md`; in short, on the coordinator page `/coord/targets` (`corla_observer.targets`, `corla_observer.web.targets`):

- **Pattern grid:** the county CVR's statewide and US House contests, with a name search to add any other contest, ordered by the actual margin from the results in hand (the state's election night results, else CORLA's posted totals; the county's tally shown beside, and used for the order only when no results are loaded, with a note); candidates by votes; live match counts; split-ticket presets when the parties can be inferred from the CVR, the results or the contest names. The pattern row: the pattern · the matching count · Seed (any text, hashed; the RLA seed in effect by default) · Ballots · Draw.
- **Sources and the working list:** one working list per county, the union of its included sources (pattern draws, and manual imprinted-ID entries with a note), each a row with an include check box and its count; each ballot once, in CORLA's pull order, with its source(s). A draw skips ballots already in the working list, an earlier sent list or the RLA sample and picks more until it has N new ones, noting the collisions in its record; no sampling-with-replacement accounting anywhere.
- **Send to county:** freezes the working list as Targeted list N (dated, extract hash, PDFs, CSV, copy-for-email text; `targets.sent`), which observers then follow beside the RLA sample, marked TARGETED (NOT RLA); later additions go into the next working list. Lists may be sent at any time. A sent list can be retired from observers' switchers (kept on record) and its details published after its ballots are examined.
- **Overlaps both ways:** a targeted ballot also in the RLA sample is marked "ALSO IN THE RLA SAMPLE" on the targeted list and its RLA entry counts; an RLA selection on an earlier sent list is marked "PREVIOUSLY TARGETED" on the RLA extract, the coordinator's extract page and the dashboard.
- **Two kinds, kept apart:** by CVR (the pull lists above) and by manifest (boxes): a ballot picked from a box is looked up by imprinted ID on the observer's screen ("Look up a ballot"), its expected votes shown from the redacted CVR and entered contest by contest as a box-targeted observation (`observation.lookup_contest`, `observation.lookup_done`), never merged into a pull list. Statistics and dashboards report "Targeted by CVR" and "Targeted from boxes" separately.
- **Privacy:** counts, patterns and IDs are for coordinators during the audit; the public dashboard shows "Targeted list N" and "Targeted from boxes" with counts only until details are published. Sent lists are part of the evidence commitment.
- **Demonstration counties:** Adams, Broomfield and Delta's published 2020 general election CVRs (each passed the redaction check with nothing to redact), loaded as "Adams (2020 demo)" and so on, test counties kept apart from the 2026 data (`corla_observer.classify.DEMO_COUNTIES`). Each is loaded with a note saying whether its file reproduces the hash CORLA recorded for that election's upload.

### 10. After the audit

- **Reconcile observers' records:** compare each board's records with each other (screen against screen, screen against photographed paper). Screen against screen is on `/coord/stats` (`corla_observer.contests.compare_board`): each ballot where an observer recorded a difference, contest-by-contest findings (which name the contest) before two-tap notes, and a conflict where a two-tap note meets contests all confirmed, or a two-tap match meets a contest heard differently.
- **Check device records:** check device-held records against the published log, using the checker script.
- **Compare with the state:** import the state's published comparison of audit board interpretations with the CVRs, and its discrepancy report. List every observer note the state's records don't explain, for the coordinator to follow up with the county.
- **TODO: the audit report.** Help prepare a report at the end of each audit (approach to be determined).

## Testing

- **Fast by default:** the default test run takes seconds. It uses small synthetic fixtures built in the tests themselves (a few counties, a few batches, a few contests), covering every branch of the parsing, sampling, redaction-check and consistency code.
- **Acceptance tests on real public data:** these are marked `slow`, are skipped by default and run on demand and before each release. For example, reproducing every targeted contest of an election.
- **Little committed data:** real data stays out of the repository where possible. Acceptance tests fetch the public files they need from the audit center mirror at a pinned commit, and verify their hashes. Only small, essential files are committed.
- **Format variety:** a small fixture of each audit center and county file format seen so far (column-name variants, Excel quoting, Latin-1, redaction markers), so the content-based loaders stay correct as formats change.
- **Rehearsal data:** the full-scale rehearsal before each pilot uses the previous election's public data end to end.

## Schedule for the November pilots

- **By 30 Oct:**
  - repository and package skeleton
  - fetcher and fetch log
  - uploads and hashing
  - event log with hash chaining and off-site backup
  - redaction check
  - consistency report
  - timestamping script
- **By 6 Nov:**
  - seed entry
  - prediction, the independent RLAAuditHelper check and the pull-list check
  - the extract, with page identification and QR codes, committed before the audit (done; the printout a reproducible PDF in four layouts, by default portrait with two columns, its hashes committed with the extract)
  - the droplet deployed behind Cloudflare with coordinator accounts
- **By 10 Nov:**
  - the live observation page with device keys, signed entries, receipts and device-held records
  - the public dashboard
  - the checker script and the "how to check us" guide
  - the check-a-page web page and the photo check (done: finding a page by its QR code or short hashes, reading its printed text and comparing it with the page as committed)
  - a full rehearsal on the July 2026 primary data with several team members per board, one on paper and others on phones, including a restore from backup and a check of the device records against the log
- **13 Nov:** freeze. Print paper extracts for anyone who wants them.
- **16 to 18 Nov:** the pilots.
- **After November:**
  - library refactors agreed with the upstream maintainers
  - wider sharing of the dashboard
  - importing the state's comparison file
  - analysis of the event logs
  - handing the tool to the organizations that will coordinate observers

## Implementation status

Built so far, as library code with a command line and tests (fast synthetic tests by default; slow tests on public data from the audit center mirror at pinned commits):

- **Sampling:** CORLA's SHA-256 generator, manifest loading, single-county, multi-county and statewide domains, and draws with repeats. It reproduces CORLA's published selections for all 113 targeted contests of the July 2026 primary from public data.
- **Event log and file store:** a hash-chained, append-only log (specified in `docs/EVENT_LOG.md`) and content-addressed storage.
- **Audit center fetcher and classifier:** crawls the Audit Center at up to 10 requests a second, slowing down when the server pushes back, logs every fetch, raises alerts on changes, duplicates and missing files, and identifies files by content.
- **Change flags:** the roll time (from the seed's entry, a recorded roll time or a scheduled one), and high-severity flags for every selection input that changes after it, every late, mismatched or failed upload in CORLA's upload status, and every manifest that differs from the one CORLA imported or appears under another county's name; other changes are normal flags. One problem is one flag, listing its findings (every upload row about a county, every copy of its manifest, every file with the same content). `corla-observer flags`, `map check` and `map show --for-ai` list them with what to investigate. They are for coordinators only, on `/coord/flags`, where each can be marked addressed with a note. The July 2026 rehearsal raises a late import, hash mismatches recorded in the upload status, and copies that differ from the imported file.
- **Uploads and redaction check:** staged, hashed uploads; a redacted CVR is stored only if no rare group remains, grouping by style plus precinct and ballot type when present.
- **Seed entry:** one coordinator's entry takes effect at once; an optional second entry by another coordinator is compared with it. A mismatched second entry, a re-entry that changes the value, and an audit center seed unlike the one in effect are high-severity flags.
- **Election setup and scheduled crawls:** each election's `election.toml` (the Audit Center page, probe URLs for files not yet linked, the calendar, the results source, cards per ballot per county) and `schedule.toml` (crawl windows: one crawl on 6 Nov at 7 PM, daily to 12 Nov, hourly from 12 Nov 6 PM through 13 Nov, every 3 hours over the weekend, every 3 minutes on roll day, hourly to 25 Nov), run by a long-running scheduler with coordinator-triggered sprints; each cycle crawls, gathers results, records flags and estimates and, after the roll, predicts and rebuilds interim extracts (`docs/RUNBOOK-2026-GENERAL.md`). A firewall's refusal page served with status 200 is never taken for a file. Contest lists of any round, in any order, each give their round's target.
- **A downloaded results file, and the totals check:** a Clarity results file downloaded by hand (detail.xml or detail.xlsx, either zipped, summary.zip or summary.csv, a county's summary.json; identified by content, anything else refused) is uploaded on `/coord/upload` or with `corla-observer results add`. It is stored by hash and recorded as uploaded by hand, with the coordinator, its source and a note, and then used like a fetched file: the newest by the time the file itself gives wins, and a fetched file wins a tie. Whenever results arrive, the totals check (`corla_observer.totals`) compares each targeted contest's winner and loser votes, and each county's ballots cast, with the other results files, the counties' redacted CVR tallies, CORLA's vote totals and contest list, and the manifests. It writes one line per difference on `/coord/estimates` and raises a normal flag for each that matters. A county not in CORLA's audit (San Juan) is left out of multi-county and statewide margins, with a note. A county whose results have more votes than its CVR or CORLA's tally is flagged, since those ballots may not be in the audit's population. On the 2026 primary it reproduces Weld's Attorney General (Republican) margin of 5,213 and its 69,640 ballots cast against its manifest. It also reports the small gaps between a late results file's statewide margin and CORLA's tally, with the county and the vote counts on each side, as differences for a coordinator to take up rather than as conclusions.
- **Results and estimated counts:** election night results from Clarity's ENR (the state's detail by county, the pilot counties' own summaries), inputs supplied by hand only through the overrides file (a margin, ballots cast, cards per ballot or a ballot card total, each with its reason, ranked below every audit center source and the results, so a later posting replaces it), and per-contest central and low estimates of the round-one count by CORLA's formula, with every source recorded (`corla_observer.enr`, `corla_observer.estimate`). On the 2026 primary, with final results and before the manifests, 112 of 113 estimates equal CORLA's counts.
- **The extract:** built from whichever of the county's pull list and CORLA's posted selections arrives first (the other checked, a disagreement flagged), or, until then, the predicted selections in CORLA's pull order (storage location, scanner, batch, record), flagged as such, with ballots beyond the low estimate marked POSSIBLE and ballots CORLA's random board split may put on either of two boards listed on both, marked BOUNDARY (with a one-tap "Skipped: other board" on the observer's screen); and the redacted CVR, with statewide picks alongside the county's; committed by its SHA-256 and every input's hash (`docs/EXTRACT.md`); printed as a PDF drawn without a browser, byte for byte reproducible, its SHA-256 per board and layout recorded and committed with the extract, with a provenance page first (the seed, the extract's hash, every input's hash, the page index's hash and the key to the shortened names) and, on every physical sheet (four layouts: portrait or landscape, with one, two or three columns of ballots), a header band with one short identification line (county, board, page N of M and the layout, and the extract's and page's hashes, short) and a 22 mm QR code of the page's hash; names shortened only as far as the fonts' measured widths need; and as plain text and as CSV; and loaded by the observer page, which shows its short hash and links to the provenance.
- **Photo check and "Check a page":** a photo or scan of a printed extract page (any layout, or the provenance sheet) is checked against the committed extract (`docs/PHOTO_CHECK.md`, `corla-observer photo check`, `/coord/check` and the observer and reader links' check pages, and `/check` for the public only if configured, off by default). The QR code is decoded with zbar (several preparations, corners enlarged), or the identification line's short hashes looked up; the page is found in the stored page index of any extract built, and the evidence commitments covering it are listed with their proofs. The printed text is read by tesseract twice, aligned with the page's expected lines (the printed, shortened names), and each line judged matched, differing or unread, so that OCR uncertainty never makes a match. Hand-made check marks are tolerated and roughly detected. Each check is a `photo.checked` event, with the photo stored without its metadata and never served publicly. Measured on pages printed by the real print pipeline, degraded like photos: genuine pages are never MISMATCH, and every tampered page that could be located is MISMATCH or at worst MATCH WITH UNREAD LINES, never MATCH.
- **Contest-by-contest entry:** the default entry mode on the observer page (`static/percontest.js`, its logic in `static/contestlogic.js`, `corla_observer.contests`): each contest of each ballot confirmed, heard instead (with the contest's choices suggested) or not heard clearly, in each reading, by keyboard or touch; signed `contest`, `ballot_done` and `entry_mode` payloads recorded as `observation.contest`, `observation.ballot_done` and `observation.entry_mode`, sent in batches, checked by `check-record`; counted on the dashboard and `/coord/stats` with each observer's mode, and compared between observers there. A browser test (`pytest -m browser`, `scripts/browser_per_contest.py`) drives it on a Testland 60-contest ballot at phone and desktop sizes.
- **Targeted ballots:** `/coord/targets` and `corla-observer targets` (9a, `docs/TARGETING.md`): the pattern grid (statewide and US House contests by the results' margins, any other contest added by name, live counts from bit sets per candidate, read once per CVR), seeded draws that skip ballots already listed or sampled and note the collisions, the working list of included sources (draws and manual entries) in pull order, "Send to county" freezing it as Targeted list N (extract, PDFs, CSV, email text, `targets.sent`), overlap marks on both the targeted and the RLA extracts, and box-targeted lookups on the observer page recorded and counted apart. Lists built by the first design read as sent lists. Tested on a small party-labelled CVR, in a browser (`scripts/browser_targets.py`), and on the 2020 demonstration counties.
- **Signed entries and receipts:** each observer link's browser makes an ECDSA P-256 key with Web Crypto (non-extractable, kept in IndexedDB) and registers it; every entry is signed on the device in a per-device chain, verified by the server and answered with a receipt signed by the server's Ed25519 receipt key; the page keeps every entry and receipt, queues them offline, and downloads them as JSON and CSV; and `corla-observer check-record` checks a downloaded record against a published log (`docs/RECEIPTS.md`). The evidence commitment includes each extract's page index.

### Rehearsal

Each part of an audit can be re-run from the data gathered so far, so the whole pipeline is rehearsed before each pilot and any stage can be repeated after a fix. The `rehearse` workflow (`docs/REHEARSAL.md`) builds a fresh workspace and runs these stages, each runnable on its own:

1. **Fetch:** load the audit center files as the public mirror held them at a chosen commit, logged with their original URLs and marked as coming from the mirror.
2. **Uploads:** submit the counties' redacted CVRs and manifests through the real upload path, including the redaction check, and record audit board counts (flagged as placeholders when not known).
3. **Seed:** enter the seed.
4. **Predict:** compute each targeted contest's selections from the data map, with any adjustments (such as a missing manifest batch) supplied as logged overrides with their evidence.
5. **Compare:** check the predictions against CORLA's published selections.

The first rehearsal uses the July 2026 primary (La Plata, Morgan and Weld, and the statewide contest). The walkthrough (`corla-observer walkthrough`) replays it for one county in the general election's order, one arrival at a time, on the web pages, with every page marked as a walkthrough and its simulated time. The full rehearsal before the November pilots adds the live page with several observers per board, and a restore from backup.

### Adjusting at the last minute

Because the audit center's files and formats change from election to election, every election has a data map built by the classifier, plus an overrides file that a coordinator, or an AI assistant working from the command line, can edit. The overrides file can:

- reassign or ignore a file
- supply a missing file from another source, with its hash
- set the seed or the county order
- add manifest adjustments
- add county-name aliases

Each override needs a reason, is applied deterministically and is logged. For a layout nobody anticipated, small adapter modules named in the overrides file can convert it; the hash of each adapter's code is logged when it runs, and nothing not named in the overrides is ever run. Commands show the map, explain any classification, list what is still missing for a sampling run, and print a compact description of the map and its problems for pasting into an AI assistant. The formats found in past elections are surveyed in `docs/AUDITCENTER_FORMATS.md`.

## Open questions

1. When will each county's pull list reach the coordinator: after the drawing on 16 Nov, or only on audit day?
2. Will the spreadsheet output of the RLAAuditHelper be published in its repository, and may its analysis and the anonymizer's be imported as libraries?
3. What may the dashboard show during the audit: counts only, or also observers' notes? When does it move beyond the small circle?
4. Which domain name, hosting account and backup storage, and who administers them?
5. How many observers can each board have in November, and will any of them work on paper?
6. What do observers do with their records afterwards?
7. ~~We need a plan for when the auditcenter is checked for new data.~~ Decided: the schedule above (`docs/RUNBOOK-2026-GENERAL.md`), with sprints on roll morning. Still open: whether to keep the sprite awake from now (the default) or only near the windows.
8. We may need some contingency options for restarting after code is revised etc. The git hash of the deployed software should be part of the appropriate evidence or other hashes or whatever as appropriate. *Partly addressed:* the running software names itself with its git commit (`corla-observer 0.1.0 (f855b1a1c2d3)`, `+dirty` for uncommitted changes; `corla_observer.version`) in every event's `software` field (`extract.built`, `photo.checked`), each evidence SUMS file's header, the printout's provenance sheet (so a PDF's bytes identify the commit that drew it) and every web page's footer, which links to a `/version` page with the full hash, the commit's log entry and its page on GitHub. Restarting after a revision remains open.
9. How should we publish and compare our hash of each received file with any hash the county publishes, and with CORLA's recorded hash when the file needed no redaction?
10. Contest-by-contest entry assumes each reading goes through the whole ballot before the next starts. If a board reads contest by contest (one member reads a contest, the other confirms it, then the next contest), the page would need a "both readings at once" setting. Which do the November boards do?

## Main risks

- **The county's pull list arrives late or differs from the prediction.** Mitigation: predict from the seed first, check the list item by item when it arrives, and keep the extract committed from the county's list.
- **Late or wrong manifests or redaction problems on roll day.** Mitigation: earlier testing with the counties, the countdown, versioned commitments.
- **Attack or failure on audit day.** Mitigation: Cloudflare in front, a read-only cached dashboard, write-once backups, a tested restore, device-held records, and paper for those who want it.
- **Connection problems in the room.** Mitigation: entries are stored on the device and queued until the connection returns.
- **Observers miss ballots while catching up.** Mitigation: a match is two quick taps, one per reading, scrolling back to edit, and more than one observer per board where possible.
- **Schedule.** Three weeks is tight. Mitigation: the rehearsal on 10 Nov decides what is used on the day, and anything not ready falls back to the July method.
