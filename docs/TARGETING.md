# Targeted ballots: requirements and design

Targeted ballots are ballots the observers choose, outside the risk-limiting audit's random sample.
The county pulls and examines them after the state's sample, at the observers' request; they are not
entered into CORLA and have no bearing on the audit's risk. Anyone may define and prioritise them
however they like. The tool only makes the choosing and the asking easier: it finds ballots by their
votes in the redacted CVR, draws some of them at random, takes specific imprinted IDs, and gives the
county one tidy pull list in its own pull order. This document is the requirements and the design;
the coordinator page is `/coord/targets` (`corla_observer.web.targets`), the library is
`corla_observer.targets`, and the lists observers follow are in `corla_observer.pulllists`.

## Purpose

- **Observers' own choice.** The RLA samples at random; observers may want to look at ballots the
  sample rarely reaches, such as split tickets in the tightest statewide contests, a batch that
  drew attention, or a ballot found in a box. The tool supports any of these and recommends none.
- **From the redacted CVR.** Every pattern is counted in, and every draw is made from, the county's
  published redacted CVR as uploaded to the project, so anyone with the same file can redo the
  count and the draw. Nothing is read from CORLA.
- **One pull list for the county.** Whatever the sources, the county receives one list at a time,
  in CORLA's pull order (storage location, tabulator, batch, record), so it pulls box by box.
- **Observers follow it like the sample.** A sent list is a pull list of the county's beside its
  RLA sample, marked "TARGETED (NOT RLA)" on every page and screen, with its own entries, progress
  and printouts, never mixed with the sample's.

## The working list and sent lists

Each county has **one working list**: the union of the ballots of its included **sources**. A
source is one of:

- a **pattern draw**: a pattern of votes, the seed text, the number of ballots asked for (N) and
  the draw's number in the county, with the ballots it drew;
- a **manual entry**: one or more imprinted IDs entered by hand, with an optional note (why they
  were chosen), each looked up in the CVR and the manifest.

On the coordinator page each source is a row with an **include** check box, its description and its
ballot count; the working list below it lists each included ballot once, with its storage location,
ballot type, whether it was found in the CVR and the manifest, and the source or sources it came
from. The list is in CORLA's pull order. Excluding a source takes its ballots out of the working
list (unless another included source has them); the source stays on record and can be included
again. A manual entry whose IDs were found nowhere is not added.

**Send to county** freezes the working list's current contents as **Targeted list N** (N counts the
county's targeted lists from 1, in the order sent), dated, with the extract observers follow, its
SHA-256, its printouts (PDF per board and layout), its spreadsheet and its plain text to copy into
an email. The send is recorded (`targets.sent`, below), naming the sources frozen. The sources of a
sent list leave the working list; sources added later go into the next working list, which becomes
Targeted list N+1 when it is sent. Lists may be sent at any time: before the seed is rolled,
between rounds, after the boards finish.

Observers' and readers' pages offer "RLA sample" and each sent list in the list switcher in the
page's header; the working list is for coordinators only until it is sent. Until the county has an
RLA sample (no seed, or no extract yet), a link opens on the first targeted list and says so.
**Retiring** a sent list hides it from observers' switchers and from the public dashboard but keeps
it on record; it can be restored. There is no combining of lists: the working list is already the
union of its sources.

The county's **allowed total** (the most targeted ballots it agreed to pull, a coordinator's note)
is shown beside the running total of the sent lists' distinct ballots, with a warning above it.

## Duplicates and overlaps

Targeting never samples with replacement and never counts multiplicity. Each ballot appears on a
list once, however many sources drew or named it.

- **A draw picks more until it has N new ballots.** When a ballot a draw would pick is already in
  the working list (an included source has it), in an earlier sent list of the county, or in the
  county's RLA sample (any round, as the RLA extract lists it), the draw skips it and takes the next
  ballot in its order, until it has N distinct ballots none of those hold, or the pattern's ballots
  run out. Each skipped ballot is noted in the draw's record (`collisions`: the ballot and what
  already held it), so the draw remains reproducible: the same CVR, pattern, seed, draw number and
  exclusions give the same ballots.
- **A targeted ballot that is also in the RLA sample.** A sent list's ballot that the RLA sample
  turns out to hold (the sample arriving after the list was sent, or a manual entry of a sampled
  ballot) is shown on the targeted list as "ALSO IN THE RLA SAMPLE", on paper and on screen, and the
  list's progress counts the observers' RLA entry for it: the county is not asked to pull it twice,
  and observers need not enter it twice.
- **An RLA selection that was targeted earlier.** A ballot of the RLA sample (any round) that
  appeared on an earlier sent targeted list is marked on the RLA extract, on paper and on screen
  ("PREVIOUSLY TARGETED: Targeted list N, may already have been pulled"), counted on the
  coordinator's extract page and on the dashboard, so the county and the boards know the ballot may
  be out of its box already.
- **Lists made by other samplers.** Anyone who imports a list produced by another sampler (Arlo,
  a consistent sampler, CORLA's own pull list) that counts repeated draws must handle the
  multiplicity themselves: this tool lists each ballot once and never says how many times a ballot
  was drawn.

## Two kinds of targeted observation, kept separate

- **By CVR (pull lists).** Ballots chosen from the CVR by pattern or by imprinted ID and sent to the
  county as Targeted list N, above. Observers follow each list on their page like the RLA sample,
  switching lists with the list in the header. Each entry names the list's extract, so a targeted
  list's entries never count toward the RLA sample, nor another list.
- **By manifest (boxes).** Ballots picked on the spot from the storage boxes, with the county's
  agreement, and looked up by imprinted ID on the observer's screen ("Look up a ballot", in the
  list switcher when the page shows one and under More otherwise): the page fetches that one
  ballot's expected votes from the county's redacted CVR and its
  storage location from the manifest, and the observer enters it contest by contest as any other.
  Each is an ad-hoc observation of its own kind (`observation.lookup_contest` and
  `observation.lookup_done`, on the list key `lookup`), signed and receipted like every entry,
  and never merged into a pull list, an extract or the RLA sample's counts. A ballot not in the CVR
  can still be looked up: its contests are then unknown, and the observer enters what was read.

Coordinator pages, the statistics and the dashboard report the two separately: "Targeted by CVR:
lists 1–2, 15 ballots, 12 observed" and "Targeted from boxes: 7 observed". Record downloads and
`corla-observer check-record` list a device's lookups under "Looked up from boxes", apart from
every pull list.

## The pattern grid

- **Rows** are contests of the county's CVR. By default the statewide candidate contests and the US
  House contest(s) are shown; a **name search** adds any other contest of the CVR to the grid (a
  local contest, a ballot measure, a state legislative seat), so a pattern may name any of them.
- **Order** is by the contest's actual margin as a share of its votes, tightest first, from the
  results in hand: the state's election night results, else CORLA's posted totals from the audit
  center's contest list (its margin over its ballot cards); the county's own tally is shown beside
  it for information, never used alone for the order while results are in hand. Contests the results
  do not cover come after, in the county tally's order, each marked so. When no results are loaded
  the page says so, links to the results upload and the fetch, and falls back to the county tally's
  order with a clear note.
- **Candidate columns** are each contest's choices, most votes first (the first few open, the rest
  behind "more"). Checking a candidate requires a vote for them; "any" means no requirement in that
  contest. A ballot matches when, in every contest with a requirement, it shows a valid vote (no
  overvote) for each candidate required. Redacted rows and contests, and aggregate rows, never match.
- **Live counts.** The ballots matching the pattern, and beside each box how many would match with
  it checked too, update as boxes change, without a reload (`static/targets.js`; the page's "Count"
  button does the same without it).
- **Split-ticket presets** are offered only when the parties are known: from the CVR's party
  labels, from the state results' candidate names ("Jane Doe (DEM)") or from the contest's own name
  ("US Rep - CD8 - REP"). There is no form for entering party labels.
- **The pattern row**, under the grid, reads left to right: the pattern in words · the matching
  count · **Seed** · **Ballots** · **Draw**. The seed is any text (it is hashed), prefilled with the
  RLA seed in effect when there is one, never required to be digits; Ballots is N (20 unless
  changed). Draw records the draw and adds it to the working list, included. A source row's **Draw
  again** makes another draw of the same pattern, seed and N under the next draw number.
- **Manual ID entry** stays in place beside the working list: IDs typed in (several at once,
  separated by spaces or commas) with an optional note; the page says inline where each was found
  (in the CVR, in the manifest, its storage location) or that it was not found and not added.

## Privacy and the public view

- The counts, the patterns and the imprinted IDs are for coordinators only during the audit: the
  pattern says how its ballots are marked, which the audit board should not learn before it
  examines them. The public dashboard and `dashboard.json` show each sent list as "Targeted list N"
  with its counts (ballots, done, matches, notes, observers), and "Targeted from boxes" with its
  count of ballots observed, and nothing else, until a coordinator **publishes details** for a list
  (for after its ballots are examined): its sources in words and its imprinted IDs.
- A rare pattern, matched by a few ballots, is allowed: the file is the published, redacted CVR, and
  the count reveals nothing the file does not.
- No expected votes and no imprinted IDs go into the public log through targeting: the draw's and
  the list's full records are stored in the workspace and committed to by hash in the events.

## Drawing

A draw's inputs are the county, the CVR's SHA-256, the pattern's definition (each contest and its
required choices, by name, sorted), the seed text, the draw's number (the county's draws so far,
plus one) and the method's name. The derived seed is the SHA-256, in hex, of their canonical
serialization (`corla_observer.events.canonical_json`) as an object with the keys `county`,
`cvr_sha256`, `draw`, `method`, `pattern` and `seed`. Each matching ballot's key is the SHA-256 of
the derived seed followed by its imprinted ID (UTF-8, no separator); the ballots are taken in order
of their keys, smallest first, skipping those already held (above), until N are taken. The method's
name is `corla-observer targets 2: sha256(derived seed + imprinted ID), smallest first, skipping
ballots already listed`; records of the first method (which did not skip) keep its name, and are
redone by it.

## Events and records

Every event has the county set. The full records (patterns, ballots, expected votes) are files in
the workspace, named by hash from the events and kept out of the published log until a coordinator
publishes a list's details.

| Event | Holds |
|---|---|
| `targets.drawn` | `file_sha256`: the draw's record (the pattern's definition, the seed, the derived seed, N, the counts matching, the ballots in pull order with locations, the collisions skipped, the actor); `data`: `draw` (its number), `ballots` (how many), `size` (N), `collisions` (how many were skipped), `sha256` (the record's), `pattern_sha256` (the pattern's definition, hashed). |
| `targets.listed` | A manual entry: `data`: `ids` (the imprinted IDs added, as looked up), `source` `entered`, `note` (the coordinator's note, may be empty). |
| `targets.included` | A source included or excluded: `data`: `source` (`draw N` or `entry SEQ`, the `targets.listed` event's seq), `included` (true or false). |
| `targets.sent` | A working list sent as Targeted list N: `data`: `list` (the list's key), `list_name`, `number` (N), `sources` (the source IDs frozen), `ballots` (how many), `in_rla` (how many are also in the RLA sample), `extract_sha256`, `list_record_sha256`. |
| `extract.built` (order `targeted`) | The sent list's extract (docs/EXTRACT.md): `list`, `list_name`, `list_record_sha256`. |
| `extract.retired`, `extract.restored` | A sent list hidden from or shown to observers again: `list`, `list_name`. |
| `extract.details_published`, `extract.details_withdrawn` | A list's details put on or taken off the public dashboard; the former carries them (`details`: `name`, `patterns`: each draw's `draw`, `name` and `description`, and `ids`). |
| `targets.allowed_set` | `data`: `allowed`, the most targeted ballots the county allows in all, or null. |
| `observation.lookup_contest`, `observation.lookup_done` | A box-targeted observation: a looked-up ballot's contest outcome, or the ballot marked done, with the same fields as `observation.contest` and `observation.ballot_done` (docs/RECEIPTS.md); `extract_sha256` is the redacted CVR's hash, `position` the lookup's number on the device. |

Events the first design recorded and this one no longer does, which the tool still reads:
`targets.pattern_saved` and `targets.pattern_removed` (named patterns, now part of each draw's
record), `targets.unlisted` (removal of imprinted IDs from the pull list, now exclusion of a
source), `targets.parties_set` (party labels entered by hand). A `targets.listed` event with
source `draw N` includes that draw in the working list, as the first design's "add the draw to
the pull list" did.

**The list record** (`corla_observer.targets.list_record`, format 2) is stored when a list is sent:
the county, the name, the number, each source (its ID, kind, description, seed, N, draw number,
note and ballot count) and each ballot (imprinted ID, location, its sources, whether it is also in
the RLA sample). Its hash is in the `targets.sent` and `extract.built` events; publishing a list's
details writes its sources' descriptions and its IDs into the log.

**Compatibility.** Lists built by the first design (a targeted extract built from the pull list
under a name) are sent lists: Targeted list N by the order built, shown with their given name
("Targeted list 1 (p1)"), their sources read from their list records. Draws never added to the
pull list are excluded sources in the working list; imprinted IDs entered and not built are an
included manual source. Nothing is rewritten: the reading of the old events gives the new model.

## Provenance

A sent list's extract hash, page-index hash and PDF hashes are recorded in its `extract.built`
event and shown on the coordinator page, in the email text ("Record ..."), on every printed page
and on the observer's screen (short), exactly as the RLA sample's are. Sent lists of real counties
are included in the evidence commitment (`corla-observer commit-evidence`,
`corla_observer.timestamp`): the county acts on them, so they are part of the record the project
commits to and stamps. The working list is not committed until it is sent. The draw records and the
list records are committed to by hash in the log and published with the list's details after the
audit, when anyone can redo each draw from the CVR and compare.
