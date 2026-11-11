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
  drew attention, or a ballot found in a box. The tool supports any of these. Only the targeting
  menu (below) suggests anything: random draws that lower the risk of the county's own contests,
  which coordinators are free to take, change or ignore.
- **From the redacted CVR.** Every pattern is counted in, and every draw is made from, the county's
  published redacted CVR as uploaded to the project, so anyone with the same file can redo the
  count and the draw. Nothing is read from CORLA, except by the targeting menu, which reads CORLA's
  posted contest lists and comparison to compute the risks it shows.
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
  were chosen), each looked up in the CVR and the manifest;
- a **domain draw**: N cards drawn uniformly with replacement from the cards carrying a set of
  local contests, from the targeting menu (below), with the seed text and the draw's number in the
  county.

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

### Drafts

Press **Send to county** when the list is sent to the county. To have a list's extract, printouts
and spreadsheet before deciding, for instance to explore patterns before the seed is rolled, **Make
draft** freezes the working list as **Draft N** instead (N counts the county's drafts from 1):
the same list record and extract as a sent list, marked as a draft, and a `targets.drafted` event
naming its sources, which leave the working list. Several drafts may be open at once, so more than
one line of inquiry can be kept.

A draft is not a targeted list. Observers, readers and the public dashboard do not see it, since
offering a list the county never heard of would only confuse; in testing mode (a deployment with a
Reset button) or for a test county they do, so the whole flow can be tried. A draft counts toward
no total and marks no RLA ballot as previously targeted. Its ballots are held, though: later draws
skip them, so two drafts never share a ballot by accident.

Each draft is then **sent** or **discarded**. Sending it makes it the next Targeted list N, its
extract built again under that name, and the `targets.sent` event names the draft. Discarding it
(`targets.draft_discarded`) returns its sources to the working list, excluded and on record, and
frees its ballots for later draws.

## Duplicates and overlaps

Pattern draws and manual entries never sample with replacement and never count multiplicity; a
domain draw samples with replacement and counts every draw in the pooled risk (below). Either way
each ballot appears on a list once, however many sources drew or named it.

- **A domain draw never skips.** To stay uniform over its domain it takes the cards its draws land
  on, including one already in a list, a draft, the working list or the RLA sample (noted in its
  record as `held`); the working list lists that card once, and the county pulls it once.
- **A pattern draw picks more until it has N new ballots.** When a ballot a draw would pick is
  already in the working list (an included source has it), in an earlier sent list of the county, or
  in the county's RLA sample (any round, as the RLA extract lists it), the draw skips it and takes
  the next ballot in its order, until it has N distinct ballots none of those hold, or the pattern's
  ballots run out. Each skipped ballot is noted in the draw's record (`collisions`: the ballot and
  what already held it), so the draw remains reproducible: the same CVR, pattern, seed, draw number
  and exclusions give the same ballots.
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
- **Manual ID entry** stays in place beside the working list: IDs typed or pasted in (several at
  once, one per line as from a spreadsheet column, or separated by commas, semicolons or spaces;
  a repeated ID is entered once) with an optional note; the page says inline where each was found
  (in the CVR, in the manifest, its storage location) or that it was not found and not added.

## The targeting menu

The pattern grid leaves the choosing to the coordinator. The **targeting menu**
(`/coord/targets/menu`, `corla-observer targets menu`, the library `corla_observer.targetmenu`)
offers one particular, principled choice: random draws that lower the risk of the county's own
contests that CORLA's sample left unseen or thin. It can be shown after the official rounds, or at
any time, and is recomputed on every view.

**Local and regional.** A contest held only in this county is **local**; one spanning several
counties is **regional**. CORLA's contest list by county says which; without it a contest is
regional when its name is a statewide office's or a US House seat's, else local, and the page says
so.

**Pooled risk.** CORLA draws cards uniformly over the county (or the state). Its draws that landed
on cards carrying a local contest are, given where they landed, uniform over that contest's own
cards. Extra cards drawn uniformly with replacement from the same cards join them as one uniform
sample of the contest's domain, and the sequential Kaplan-Markov test may go on after seeing
results. So a local contest's risk, fixed in advance and never the lower of two, is Kaplan-Markov
(gamma 1.03905) with:

- diluted margin = the contest's margin / its cards in the county, the larger of the CVR's count
  and CORLA's `contest_ballot_card_count`;
- n = CORLA's draws on cards carrying the contest, counted with multiplicity (a card drawn twice
  counts twice), plus the domain draws made from its domain (not excluded or discarded);
- the discrepancies (one- and two-vote over- and understatements) seen on those draws.

The page says: the pooled risks assume the CVR's card-style information (which cards carry which
contest) is accurate, although the CVR is what is being audited; formal checks of that style
information are not made; only the CVR's contest card counts are compared with CORLA's. No combined
number other than this pooled risk is shown.

**Where the numbers come from.** Each is named on the page and in the command's output.

- CORLA's draws are its own when it has posted them: its selection lists (each targeted contest's
  draws as CVR IDs in draw order, a card drawn twice listed twice; of every round's lists, each
  contest's longest), placed in the county by the CVR-to-audit-board comparison, which gives each
  CVR ID's county and imprinted ID. Targeted contests with the same domain (the same counties, by
  CORLA's contest list by county) draw one sequence from the seed, so each domain's longest list is
  counted once.
- Until then they are reproduced from the seed and the manifests (`corla_observer.sampling`), one
  sequence per domain of targeted contests including the county, as far as its contests' latest
  cumulative draw count from the contest lists of the rounds fetched
  (`corla_observer.datamap.round_target`); from the latest prediction when it has enough draws,
  otherwise predicted again. Before the round-one contest list is posted the counts are the
  prediction's estimates, and the page says so. A reproduction is only as good as the manifests:
  where they differ from those CORLA imported, its draws land on other cards, which the posted lists
  then correct. Without a seed or a contest list, the RLA extract's distinct cards stand in, once
  each, which can only understate n; before the seed there are no draws, and every contest is
  unseen.
- Discrepancies come from CORLA's posted CVR-to-audit-board comparison when the workspace has
  it: each card's choices per the CVR and per the audit board, typed as CORLA types them (the
  largest overstatement over every winner and loser; an overvote is no vote). The file has one
  row per card and contest (the last is used); a card's discrepancy counts once for each time the
  reproduced draws drew it. A drawn card with no row yet is counted as drawn and assumed to show
  none, and the page counts such draws. Until
  the comparison is posted, the observers' own entries are used instead: the expected choices and
  what was heard instead, in each observer's final reading; a reading that matches no choice of
  the contest counts as a two-vote overstatement, the conservative reading.
- A domain draw's cards are never in CORLA's comparison, so their discrepancies always come from
  the observers' entries on them; a card not yet examined is assumed to show none.
- The margin is CORLA's `min_margin` when its contest list has the contest, else the state's
  election night results, else the county's CVR.
- A CVR contest is CORLA's contest of the same name, compared without punctuation, case or the word
  "ballot" (counties often leave it out). A county's CVR sometimes spells a contest otherwise ("Ciyt
  of Craig", "BALLOT ISSUE 6AB" for "Redlands 360 Metropolitan District No. 1 Ballot Issue 6AB",
  "Colorado Supreme Court Justice - Hart"); such a contest is matched to one of the county's CORLA
  contests the CVR does not otherwise name, only when exactly one fits: the same without spaces; the
  one closest spelling, at least 90% alike; the same words in any order; or one's words all among
  the other's, ending in the same word. Each such match is listed with the sources.
- The risk limit is the contest list's, else the target contest list's, else 3%, said so.

**Properties.** Each contest has these properties, independent of one another, shown as words (thin,
settled) and on the page as small labelled badges (the others; plain words in the command's output),
never by colour alone:

- **unseen**: no card of the official sample carried it. Domain draws do not change this; when
  they have landed on its cards the page says so apart ("seen by N domain draws"). Unseen says
  nothing about the risk by itself.
- **flagged**: a discrepancy was found on its cards, by CORLA's draws or the domain draws, shown
  with its types ("flagged: o1 x1").
- **thin** or **settled**: its pooled risk is above the risk limit, or at or under it. An
  uncontested contest has no risk and is neither; the page shows whether a card of it has been
  seen.
- **uncontested**: no more candidates than seats, write-in placeholders aside.
- **recount**: a margin of at most 0.5% of the highest vote, or for a multi-winner contest of the
  lowest winner's votes (C.R.S. 1-10.5-101), from the results, else the CVR's votes. It is
  information only: Colorado's recounts usually re-tabulate the ballots by machine rather than
  interpret the paper by hand, so the outcome still needs checking, and the contest is treated
  like any other.
- **tie**: a margin of zero. Each card checks the CVR's count, which for a tie is the whole
  question, but the risk can't fall.

The page and the command count the county's local contests by each property above the menu. A
regional contest's risk is CORLA's own, computed from its contest list (Kaplan-Markov over its
domain, its draws and its discrepancies), and regional contests get coverage only: in-county draws
are not uniform over a multi-county domain. A regional contest that is unseen or thin, of which
the county holds under 5% of the cards (its CVR count over CORLA's), is **left off** and listed as
such.

**Sampling domains.** Local contests carried by exactly the same cards of the CVR form one domain:
one random draw from it is a valid draw for every contest in it. A contest whose cards merely
overlap the domain gets coverage only, never a pooled update.

**The menu.** A domain is on the menu when any of its local contests is unseen, thin or flagged (an
unseen uncontested contest included: one card checks that the CVR records it). Its line, riskiest
domain first:

```
Redlands 360 Metro No. 1 Ballot Issue 6AB and 9 others  ·  5 cards, 10 contests (10 on the menu)
  Riskiest: Redlands 360 Metro No. 1 Ballot Issue 6AB   100.0%  thin unseen; margin 3 votes, 60.0% of its 5 cards
  after 5 / 10 / 20 draws: 18.2% / 3.3% / 0.1%; under 4% after 10 draws
  Others (7): 7 also unseen, all thin; all under 4% after 7 draws.
  Also: 2 uncontested unseen (one card checks that the CVR records it)
```

(A domain of 5 cards, risk limit 4%. Its pooled risk is 100% because none of its cards has been
drawn at all, by the official sample or a domain draw: n is 0. An unseen contest that domain draws
have reached has the risk those draws give it.)

The first row names the domain after its riskiest contest ("Ballot Issue 6AB and 9 others"), its
cards, its local contests and how many of them are on the menu. Then the riskiest contest that draws
can help, with its pooled risk, thin or settled, its tags, and its margin in votes and as a share of
its own cards ("margin 34 votes, 7.9% of its 428 cards"); its risk after 5, 10 and 20 draws from the
domain, and the draws that bring it under the limit; the other contests, counted by tag, and the
draws that bring them all under the limit; the uncontested contests, unseen or seen; each tie ("tie:
each card checks the CVR's count; the risk can't fall"); the contests on the menu that the domain's
cards also land on, named, coverage only ("also lands on 2 menu contests: A, B", the first five by
name and the rest counted), left out when there are none; and the domain draws already made from it.
Each line opens into a table of the domain's contests: name, risk, thin or settled, tags, margin
(votes and percent of its cards), official draws on its cards, domain draws and discrepancies. Local
contests in domains not on the menu, and the regional ones, are collapsed below.

**The suggestion.** A budget, by default the county's allowed total less the ballots already sent,
or a number the coordinator types, is spread over the domains. First each domain holding an unseen
local contest (uncontested ones included) or a tie, that no domain draw has reached yet, gets one
draw, the largest domains first if the budget is short: for an unseen contest one card checks that
the CVR records it, and for a tie each card checks the CVR's count, which is the whole question. The
rest is spread over the domains, each counted by its riskiest contest, in one of two ways:
**minimax** gives draws to the domain with the highest pooled risk until it falls to the next
highest, and so on; **ladder** brings the cheapest domains under 90%, then 50%, 20%, 10% and the
limit, leaving one it cannot afford as it is. Ties, whose risk can't fall, are left out of the
highest risk that minimax and ladder lower, and that the suggestion reports. The coordinator picks
one way, and the draw sizes on the menu are filled in from it, to be changed at will.

**Domain draws.** "Draw from this domain" (or `corla-observer targets domain-draw`) records a domain
draw. Its inputs are the county, the CVR's SHA-256, the domain's contests (their names in the CVR,
sorted), the seed text (the RLA seed in effect by default, or any other text entered on the menu;
shown once, above the domains, and used by every draw from the page), the draw's number (the
county's domain draws so far, plus one) and the method's name. The derived seed is the SHA-256, in
hex, of their canonical serialization as an object with the keys `county`, `cvr_sha256`, `domain`,
`draw`, `method` and `seed`. The domain's cards are the CVR's rows carrying the contests, in row
order; draw i, for i from 1 to N, takes card 1 + (the SHA-256 of the derived seed, a comma and i,
read as a number) mod the domain's size. The method's name is `corla-observer domain draw 1: card =
1 + int(sha256(derived seed + ',' + i), 16) mod domain size, i = 1..N, cards in CVR row order, with
replacement`. The draw is a source of the working list, included, and goes to the county through the
same working list, drafts and Send to county as any other; a card drawn twice is pulled once and
counted twice.

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
| `targets.domain_drawn` | A domain draw: `file_sha256`: the draw's record (the domain's contests, its cards, the seed, the derived seed, every draw in order with its card, each card once in pull order with how many times it was drawn, the cards already held, the actor); `data`: `draw` (its number among the county's domain draws), `size` (N), `ballots` (distinct cards), `domain_cards`, `held`, `sha256` (the record's), `domain_sha256` (the sorted contest names, hashed). |
| `targets.listed` | A manual entry: `data`: `ids` (the imprinted IDs added, as looked up), `source` `entered`, `note` (the coordinator's note, may be empty). |
| `targets.included` | A source included or excluded: `data`: `source` (`draw N`, `domain N` or `entry SEQ`, the `targets.listed` event's seq), `included` (true or false). |
| `targets.sent` | A working list sent as Targeted list N: `data`: `list` (the list's key), `list_name`, `number` (N), `sources` (the source IDs frozen), `ballots` (how many), `in_rla` (how many are also in the RLA sample), `extract_sha256`, `list_record_sha256`; `draft`, the draft's key, when a draft was sent. |
| `targets.drafted` | A working list made Draft N: `data` as for `targets.sent`, with the draft's `list`, `list_name` and `number`. |
| `targets.draft_discarded` | A draft discarded: `data`: `list`, `list_name`, `sources` (returned to the working list, excluded). |
| `extract.built` (order `targeted`) | The sent list's or draft's extract (docs/EXTRACT.md): `list`, `list_name`, `list_record_sha256`; for a draft, `draft` (true) and `shown` (whether observers see it). |
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

**The list record** (`corla_observer.targets.list_record`, format 2) is stored when a list is sent
or drafted (a draft's has `draft: true`):
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
