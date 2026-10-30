"""Targeted ballots: ballots observers choose outside the RLA's random sample, found by their votes in a county's redacted CVR, drawn at random, and sent to the county as one pull list.

The requirements and design are docs/TARGETING.md; in short, observers may
ask a county to pull and examine some ballots of their own choosing after
the state's sample. Anyone may define and prioritise them however they
like; this module only makes it easier. It counts the ballots showing a
pattern of votes, draws some of them reproducibly, takes imprinted IDs
entered by hand, keeps the county's **working list** (the union of its
included **sources**) in CORLA's pull order, and freezes it, when a
coordinator sends it, as **Targeted list N**: a targeted extract that
observers follow beside the RLA sample (`corla_observer.pulllists`).

**Contests** (`target_contests`). The grid shows the county CVR's statewide
candidate contests and its US House contest(s), and any other contest of the
CVR a coordinator adds by name. A contest is statewide when the state's
election night results (`corla_observer.enr`) list its votes in at least
`STATEWIDE_MIN_COUNTIES` counties; without the state's results, or for a
contest they do not list, when its name starts as a statewide office's does
(`STATEWIDE_PREFIXES`). A US House contest is one whose name `HOUSE_RE`
matches. The rows are ordered by each contest's margin as a share of its
votes, tightest first, from the results in hand: the state's results, else
CORLA's posted totals (the audit center's contest list: its margin over its
ballot cards); contests no result covers come after, by the county CVR's own
margin, which is shown beside every row for information. Each contest's
choices are ordered by their votes in the CVR, most first. The margin is
CORLA's (`corla_observer.enr.margin_of`): the last winner's votes minus the
first loser's. Each choice's party is the CVR's label for its column, else a
party named in the state results' name for the choice ("Jane Doe (DEM)"),
else one named in the contest's own name ("US Rep - CD8 - REP"), else
unknown; the split-ticket presets (`presets`) need it.

**Matching** (`CvrVotes.count`). A pattern requires, in some contests, a
vote for some choices: a ballot matches when, in every such contest, it
shows a vote for each required choice and no more choices than the
contest's Vote For (an overvote shows no vote). Only single-ballot rows
can match: a row whose votes were all redacted, a contest whose cells were
redacted, and an aggregate row (several ballots combined) never match. The
counts are of ballot cards (rows of the CVR), and the "clean" count is of
those with no redaction marker anywhere in the row.

**Drawing** (`draw`). A draw takes N of a pattern's matching ballots at
random, from a seed, so that anyone with the same CVR can redo it:

1. The draw's inputs are the county, the CVR's SHA-256, the pattern's
   definition (`pattern_definition`: each contest and its required
   choices, sorted), the seed (any text the coordinator enters; the RLA
   seed in effect by default), the draw's number (the county's draws so
   far, plus one) and `METHOD`. The derived seed is the SHA-256, in hex, of
   their canonical serialization (`corla_observer.events.canonical_json`)
   as an object with the keys ``county``, ``cvr_sha256``, ``draw``,
   ``method``, ``pattern`` and ``seed``.
2. Each matching ballot's key is the SHA-256, in hex, of the derived seed
   followed by its imprinted ID (UTF-8, no separator).
3. The ballots are taken in order of their keys, smallest first, skipping
   any already held (in the working list, in an earlier sent list, or in
   the RLA sample: ``exclude``), until N are taken or the pattern's ballots
   run out. Each ballot skipped is noted in the record (``collisions``), so
   the draw is reproducible from the CVR, the inputs and the exclusions.
   Nothing is ever counted twice: the first design's method, `METHOD_1`,
   did not skip, and its records keep its name.

The ballots are listed in CORLA's pull order
(`corla_observer.extract.corla_order`). Every draw is recorded
(`record_draw`) as a ``targets.drawn`` event committing to its full record
(`DrawRecord`) by hash; the record stays out of the published log, since
the pattern says how the ballots are marked, which the audit board should
not learn before it examines them. A draw is a source of the working list
at once, included.

**Sources and the working list** (`Source`, `sources`, `working_list`). A
source is a draw or a manual entry (imprinted IDs entered by hand with an
optional note, `enter_ids`; each is looked up in the CVR and the manifest,
`lookup`). Each source is included or excluded (`set_included`); the working
list is each ballot of every included source not yet sent, once, with the
sources it came from, in CORLA's pull order. `holders` says which ballots a
new draw must skip, and where each is.

**Sending** (`send`). The working list's ballots become Targeted list N (N
counting the county's targeted lists from 1): a list record
(`list_record`, format 2: the sources and the ballots) is stored, a
targeted extract is built (`corla_observer.extract.build_extract` with
``targeted``, order ``targeted``: not part of the RLA sample), and a
``targets.sent`` event names the sources frozen. Those sources leave the
working list. A ballot of the list that the RLA sample also holds is marked
``in_rla``, on the record and on the extract.

**Privacy.** The counts, the patterns and the IDs are shown to coordinators
only, never on a public page, until a coordinator publishes a sent list's
details (`corla_observer.pulllists.publish`). A pattern matched by a few
ballots is fine to draw from when the CVR was properly redacted.

Events (every one with ``county`` set; docs/TARGETING.md lists them with
the events the first design recorded and this module still reads):

``targets.drawn``
    ``file_sha256`` the draw's record (`DrawRecord`); ``data``: ``draw``
    (its number in the county), ``ballots`` (how many were drawn), ``size``
    (N), ``collisions`` (how many were skipped), ``sha256`` (the record's),
    ``pattern_sha256``.
``targets.listed``
    A manual entry: ``data``: ``ids`` (the imprinted IDs added), ``source``
    ``entered``, ``note``.
``targets.included``
    ``data``: ``source`` (``draw N`` or ``entry SEQ``), ``included``.
``targets.sent``
    ``data``: ``list`` (the list's key), ``list_name``, ``number``,
    ``sources``, ``ballots``, ``in_rla``, ``extract_sha256``,
    ``list_record_sha256``.
``targets.allowed_set``
    ``data``: ``allowed``, the most targeted ballots the county allows in
    all (a coordinator's note, or null), shown beside the running total of
    the sent lists (`allowed_total`).

Standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.classify import is_test_county
from corla_observer.cvr import CvrReader, is_redaction_marker, unwrap
from corla_observer.datamap import contest_key
from corla_observer.enr import Results, contest_label, margin_of
from corla_observer.events import EventLog, canonical_json, check_actor
from corla_observer.extract import corla_order, id_key, latest_extract, targeted_key
from corla_observer.workspace import Workspace

METHOD = "corla-observer targets 2: sha256(derived seed + imprinted ID), smallest first, skipping ballots already listed"
#: The first design's method: the same keys, without skipping.
METHOD_1 = "corla-observer targets 1: sha256(derived seed + imprinted ID), smallest first"
#: Ballots in a draw, unless another number is asked for (1 to `MAX_DRAW`).
DRAW_SIZE = 20
MAX_DRAW = 500
#: Most ballots on the working list.
MAX_LIST = 500
#: Longest seed text.
MAX_SEED = 200
#: Counties in the state's results that make a contest statewide.
STATEWIDE_MIN_COUNTIES = 60
#: Name starts of Colorado's statewide candidate contests (compared lowercased), for when the state's results do not say.
STATEWIDE_PREFIXES = (
    "presidential electors",
    "president",
    "united states senator",
    "u.s. senator",
    "us senator",
    "governor",
    "secretary of state",
    "state treasurer",
    "attorney general",
    "regent of the university of colorado - at large",
    "cu regent - at large",
    "state board of education member - at large",
)
HOUSE_RE = re.compile(
    r"^(representative to the \d+\w* united states congress|united states representative|u\.?s\.? rep(resentative)?\b)",
    re.IGNORECASE,
)
#: Party labels, normalized.
PARTY_ALIASES = {
    "dem": "DEM", "democrat": "DEM", "democratic": "DEM", "democraticparty": "DEM",
    "rep": "REP", "republican": "REP", "republicanparty": "REP",
}  # fmt: skip
_PARTY_IN_NAME = re.compile(r"\((DEM|REP|LBR|LIB|GRN|UNI|ACN|APV|IND|UNA|AVP)\)\s*$|\s-\s(DEM|REP)\s*$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The grid's groups, in order: the statewide contests, the US House contests, and contests added by name.
GROUPS = ("statewide", "house", "added")
#: Where a contest's ordering margin comes from.
RESULT_SOURCES = {"state": "the state's results", "corla": "CORLA's posted totals", "county": "the county's CVR only"}


class DrawError(ValueError):
    """A pattern can't be drawn, an entry made or a list sent as asked."""


def party(label: str | None) -> str | None:
    """A party label normalized: ``DEM`` and ``REP`` for the major parties' spellings, other labels upper-cased, None for none."""
    text = unwrap(label or "").strip()
    if not text:
        return None
    key = re.sub(r"[^a-z]", "", text.lower())
    return PARTY_ALIASES.get(key, text.upper()[:12])


def party_in_name(name: str) -> str | None:
    """A party named at the end of a choice's or a contest's name, as "Jane Doe (DEM)", "Jane Doe - REP" or "US Rep - CD8 - REP"."""
    match = _PARTY_IN_NAME.search(contest_label(name)[0])
    return party(match.group(1) or match.group(2)) if match else None


def _strip_party(name: str) -> str:
    return _PARTY_IN_NAME.sub("", name).strip()


# ---------------------------------------------------------------------------
# The CVR's votes, as bit sets


def _mask(indexes: Iterable[int], n: int) -> int:
    """The integer whose bits ``indexes`` are set (bit i is row i)."""
    bits = bytearray((n + 7) // 8)
    for i in indexes:
        bits[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(bits, "little")


def bits_of(mask: int) -> list[int]:
    """The indexes of a mask's set bits, smallest first."""
    text = bin(mask)[:1:-1]
    return [i for i, ch in enumerate(text) if ch == "1"]


@dataclass
class ContestVotes:
    """One contest in the CVR: its choices, their votes and parties, and per choice the rows that show a vote for it."""

    #: Index among the CVR's contests (`corla_observer.cvr.CvrHeader.contests`).
    index: int
    name: str
    vote_for: int
    choices: list[str]
    parties: list[str | None]
    #: Per choice, its votes in the CVR (aggregate rows count in full, as in `corla_observer.tally`).
    votes: list[int]
    #: Per choice, the single-ballot rows showing a valid vote for it (bit i is row i).
    masks: list[int]
    #: Rows whose cells for the contest are all redaction markers.
    redacted: int = 0
    #: Single-ballot rows with the contest on them (not redacted).
    cards: int = 0
    #: Rows with an overvote in it.
    overvotes: int = 0

    @property
    def total(self) -> int:
        return sum(self.votes)

    @property
    def margin(self) -> int:
        return margin_of(dict(zip(self.choices, self.votes, strict=True)), self.vote_for)


@dataclass
class CvrVotes:
    """What a pattern is matched against: a CVR's single-ballot rows, and its contests' votes as bit sets."""

    rows: int
    #: Per row: its imprinted ID (else tabulator-batch-record), and its ballot type.
    ids: list[str]
    ballot_types: list[str]
    #: Per row, `corla_observer.cvr.CvrRow.status`: ``ballot``, ``redacted`` or ``aggregate``.
    statuses: list[str]
    #: Rows that are one ballot card with votes visible (`corla_observer.cvr.CvrRow.status` ``ballot``).
    ballots: int
    #: Ballot rows with no redaction marker anywhere.
    clean: int
    contests: dict[int, ContestVotes]
    #: Rows with every vote cell blank or redacted, and aggregate rows.
    redacted_rows: int = 0
    aggregate_rows: int = 0
    _index: dict[str, int] | None = field(default=None, repr=False)

    def contest_by_name(self, name: str) -> ContestVotes | None:
        key = contest_key(contest_label(name)[0])
        return next((c for c in self.contests.values() if c.name == name or contest_key(c.name) == key), None)

    def match(self, requirement: Mapping[int, Iterable[int]], skip: int | None = None) -> int:
        """The mask of rows meeting ``requirement`` (contest index: choice indexes), leaving out contest ``skip``'s."""
        mask = self.ballots
        for contest, choices in requirement.items():
            if contest == skip or contest not in self.contests:
                continue
            for choice in choices:
                mask &= self.contests[contest].masks[choice]
        return mask

    def count(self, requirement: Mapping[int, Iterable[int]]) -> tuple[int, int]:
        """How many ballots meet ``requirement``, and how many of those have no redaction marker in their row."""
        mask = self.match(requirement)
        return mask.bit_count(), (mask & self.clean).bit_count()

    def grid_counts(self, requirement: Mapping[int, Iterable[int]], shown: Iterable[int] | None = None) -> dict[str, int]:
        """For each choice ``"contest:choice"`` of the contests ``shown`` (all by default), the ballots meeting ``requirement`` with that contest's requirement replaced by a vote for it; ``"contest:any"`` with none."""
        counts: dict[str, int] = {}
        for index in self.contests if shown is None else shown:
            contest = self.contests.get(index)
            if contest is None:
                continue
            base = self.match(requirement, skip=index)
            counts[f"{index}:any"] = base.bit_count()
            for k, choice_mask in enumerate(contest.masks):
                counts[f"{index}:{k}"] = (base & choice_mask).bit_count()
        return counts

    def imprinted(self, row: int) -> str:
        return self.ids[row]

    def row_of(self, imprinted_id: str) -> int | None:
        """The row of a ballot by imprinted ID (compared by `corla_observer.extract.id_key`), or None."""
        if self._index is None:
            self._index = {}
            for i in range(self.rows):
                self._index.setdefault(id_key(self.imprinted(i)), i)
        return self._index.get(id_key(imprinted_id))


def load_votes(path: Path | str, wanted: Iterable[str] | None = None) -> CvrVotes:
    """Read a Dominion CVR export's rows and the votes of the contests named in ``wanted`` (CVR names, with or without "(Vote For=N)"; all by default)."""
    reader = CvrReader(path)
    h = reader.header
    wanted_keys = None if wanted is None else {contest_key(contest_label(w)[0]) for w in wanted}
    with open(reader.path, encoding=h.encoding, newline="") as f:
        rows = csv.reader(f, delimiter=h.delimiter)
        head: list[list[str]] = []
        for number, row in enumerate(rows, start=1):
            head.append(row)
            if number >= h.header_row:
                break
        choice_row = head[h.header_row - 2] if h.header_row >= 2 else []
        party_row = head[h.header_row - 1]
        columns: dict[int, list[int]] = {}
        for offset, contest in enumerate(h.column_contest):
            if wanted_keys is None or contest_key(contest_label(h.contests[contest])[0]) in wanted_keys:
                columns.setdefault(contest, []).append(h.vote_start + offset)
        contests: dict[int, ContestVotes] = {}
        hits: dict[int, list[list[int]]] = {}
        for contest, cols in columns.items():
            name, vote_for = contest_label(h.contests[contest])
            choices = [unwrap(choice_row[i]) if i < len(choice_row) else "" for i in cols]
            choices = [c or f"choice {n + 1}" for n, c in enumerate(choices)]
            parties = [party(party_row[i]) if i < len(party_row) else None for i in cols]
            contests[contest] = ContestVotes(contest, name, vote_for, choices, parties, [0] * len(cols), [])
            hits[contest] = [[] for _ in cols]
        ids: list[str] = []
        types: list[str] = []
        interned: dict[str, str] = {}
        statuses: list[str] = []
        ballot_rows: list[int] = []
        clean_rows: list[int] = []
        redacted_rows = aggregate_rows = 0
        index = 0
        from corla_observer.cvr import _classify_row  # the reader's own row classification

        cache: dict[tuple[int, ...], frozenset[str]] = {}
        for record, row in enumerate(rows, start=h.header_row + 1):
            if not any(cell.strip() for cell in row):
                continue
            meta = _classify_row(row, index + 1, record, h, h.contests, h.column_contest, h.vote_start, h.width, cache)

            def cell(i: int | None) -> str:
                return unwrap(row[i]) if i is not None and i < len(row) else ""

            ids.append(cell(h.imprinted_index) or "-".join((cell(h.tabulator_index), cell(h.batch_index), cell(h.record_index))))
            kind = cell(h.ballot_type_index)
            types.append(interned.setdefault(kind, kind))
            statuses.append(meta.status)
            single = meta.status == "ballot"
            if single:
                ballot_rows.append(index)
                if not meta.marked:
                    clean_rows.append(index)
            elif meta.status == "redacted":
                redacted_rows += 1
            else:
                aggregate_rows += 1
            for contest, cols in columns.items():
                cells = [unwrap(row[i]).strip() if i < len(row) else "" for i in cols]
                if not any(cells):
                    continue
                cv = contests[contest]
                if all(not c or is_redaction_marker(c) for c in cells):
                    cv.redacted += 1
                    continue
                marked = []
                for k, value in enumerate(cells):
                    if value.isdigit() and int(value) > 0:
                        cv.votes[k] += int(value)
                        marked.append(k)
                if not single:
                    continue
                cv.cards += 1
                if len(marked) > cv.vote_for:
                    cv.overvotes += 1
                    continue
                for k in marked:
                    hits[contest][k].append(index)
            index += 1
    for contest, per_choice in hits.items():
        contests[contest].masks = [_mask(rows_, index) for rows_ in per_choice]
    return CvrVotes(
        rows=index,
        ids=ids,
        ballot_types=types,
        statuses=statuses,
        ballots=_mask(ballot_rows, index),
        clean=_mask(clean_rows, index),
        contests=contests,
        redacted_rows=redacted_rows,
        aggregate_rows=aggregate_rows,
    )


# ---------------------------------------------------------------------------
# The contests shown


def is_house(name: str) -> bool:
    """Whether a contest is a US House contest, by its name (`HOUSE_RE`)."""
    return bool(HOUSE_RE.match(contest_label(name)[0]))


def statewide_by_name(name: str) -> bool:
    """Whether a contest's name starts as a statewide office's does (`STATEWIDE_PREFIXES`)."""
    label = contest_label(name)[0].lower()
    return any(label.startswith(p) for p in STATEWIDE_PREFIXES)


def _state_contest(results: Results | None, name: str) -> Any:
    if results is None:
        return None
    key = contest_key(contest_label(name)[0])
    return next((c for c in results.contests if contest_key(c.name) == key), None)


def contest_group(name: str, results: Results | None) -> str | None:
    """``statewide``, ``house`` or None (not shown by default), by the state's results and else by the name; ballot measures are None."""
    if is_house(name):
        return "house"
    found = _state_contest(results, name)
    if found is not None and results is not None and results.areas_are_counties:
        return "statewide" if len(found.by_area) >= STATEWIDE_MIN_COUNTIES else None
    return "statewide" if statewide_by_name(name) else None


@dataclass
class CorlaTotal:
    """A contest's margin and ballot cards as CORLA's posted contest list gives them (`corla_observer.datamap.ContestSpec`)."""

    margin: int
    cards: int | None


@dataclass
class ShownContest:
    """A contest row of the grid: the contest, its group, its margins, and its choices in order of votes."""

    votes: ContestVotes
    group: str
    #: Choice indexes, most votes first.
    order: list[int]
    #: Per choice index, the party: the CVR's label, else the state results' name's, else the contest's name's.
    parties: list[str | None]
    #: The margin that orders the row, from the results in hand: its value, the votes (or ballot cards) it is a share of, and its source (`RESULT_SOURCES`).
    result_margin: int | None = None
    result_total: int | None = None
    result_source: str = "county"

    @property
    def index(self) -> int:
        return self.votes.index

    @property
    def name(self) -> str:
        return self.votes.name

    @property
    def share(self) -> float | None:
        """The county CVR's margin as a share of the votes in the contest."""
        total = self.votes.total
        return self.votes.margin / total if total else None

    @property
    def result_share(self) -> float | None:
        if self.result_margin is None or not self.result_total:
            return None
        return self.result_margin / self.result_total

    @property
    def from_results(self) -> bool:
        return self.result_source != "county"

    def top(self, wanted: str) -> int | None:
        """The choice of party ``wanted`` with the most votes, or None."""
        return next((k for k in self.order if self.parties[k] == wanted), None)


def candidate_contests(contests: Sequence[str], results: Results | None) -> list[tuple[str, str]]:
    """The CVR's contests shown by default, with their groups: ``(name as in the CVR, group)``."""
    found = []
    for name in contests:
        group = contest_group(name, results)
        if group is not None:
            found.append((name, group))
    return found


def search_contests(votes: CvrVotes, query: str, limit: int = 12) -> list[ContestVotes]:
    """The CVR's contests whose names contain every word of ``query`` (case-insensitive), at most ``limit``."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    return [cv for cv in votes.contests.values() if all(w in cv.name.lower() for w in words)][:limit]


def target_contests(
    votes: CvrVotes, results: Results | None, groups: Mapping[str, str], corla: Mapping[str, CorlaTotal] | None = None
) -> list[ShownContest]:
    """The grid's rows (see the module documentation): each group's contests by the results' margin, tightest first.

    ``groups`` is each contest's group by name (`candidate_contests`, plus
    ``added`` for contests added by name); ``corla`` CORLA's posted totals
    by contest name, used when the state's results do not cover a contest.
    Within a group, contests the results cover come first by their share,
    then the rest by the county's.
    """
    shown: list[ShownContest] = []
    by_key = {contest_key(contest_label(n)[0]): g for n, g in groups.items()}
    corla_by_key = {contest_key(contest_label(n)[0]): t for n, t in (corla or {}).items()}
    for cv in votes.contests.values():
        group = by_key.get(contest_key(cv.name))
        if group is None or len(cv.choices) < 2:
            continue
        order = sorted(range(len(cv.choices)), key=lambda k: (-cv.votes[k], k))
        parties = list(cv.parties)
        item = ShownContest(cv, group, order, parties)
        state = _state_contest(results, cv.name)
        if state is not None:
            item.result_margin = margin_of(state.totals, state.vote_for)
            item.result_total = sum(state.totals.values())
            item.result_source = "state"
            named = {contest_key(_strip_party(n)): party_in_name(n) for n in state.totals}
            for k, choice in enumerate(cv.choices):
                if parties[k] is None:
                    parties[k] = named.get(contest_key(choice))
        else:
            total = corla_by_key.get(contest_key(cv.name))
            if total is not None:
                item.result_margin, item.result_total, item.result_source = total.margin, total.cards, "corla"
        in_name = party_in_name(cv.name)
        if in_name is not None:
            item.parties = [p if p is not None else in_name for p in parties]
        shown.append(item)

    def key(item: ShownContest) -> tuple[int, int, float, float, str]:
        county = item.share if item.share is not None else 2.0
        result = item.result_share
        return (GROUPS.index(item.group), 0 if result is not None else 1, result if result is not None else county, county, item.name)

    return sorted(shown, key=key)


# ---------------------------------------------------------------------------
# Patterns


def parse_selection(text: str | Iterable[str], votes: CvrVotes) -> dict[int, list[int]]:
    """A selection (``"3:0,5:1"``, or the parts as a list): contest index to choice indexes, keeping only valid ones, sorted."""
    parts = text.split(",") if isinstance(text, str) else [p for t in text for p in str(t).split(",")]
    found: dict[int, set[int]] = {}
    for part in parts:
        a, _, b = part.strip().partition(":")
        if not (a.isdigit() and b.isdigit()):
            continue
        contest, choice = int(a), int(b)
        cv = votes.contests.get(contest)
        if cv is not None and choice < len(cv.choices):
            found.setdefault(contest, set()).add(choice)
    return {c: sorted(ks) for c, ks in sorted(found.items())}


def selection_text(requirement: Mapping[int, Iterable[int]]) -> str:
    """A selection as text, the inverse of `parse_selection`."""
    return ",".join(f"{c}:{k}" for c, ks in sorted(requirement.items()) for k in sorted(ks))


def pattern_definition(requirement: Mapping[int, Iterable[int]], votes: CvrVotes) -> list[dict[str, Any]]:
    """A selection by name: ``[{"contest": name, "choices": [names, sorted]}]``, sorted by contest name."""
    found = []
    for contest, choices in requirement.items():
        cv = votes.contests[contest]
        names = sorted(cv.choices[k] for k in choices)
        if names:
            found.append({"contest": cv.name, "choices": names})
    return sorted(found, key=lambda d: d["contest"])


def requirement_of(definition: Sequence[Mapping[str, Any]], votes: CvrVotes) -> dict[int, list[int]] | None:
    """A pattern's definition as a selection in this CVR, or None if some contest or choice is not in it."""
    found: dict[int, list[int]] = {}
    for item in definition:
        cv = votes.contest_by_name(str(item.get("contest")))
        if cv is None:
            return None
        ks = []
        for name in item.get("choices") or []:
            if name not in cv.choices:
                return None
            ks.append(cv.choices.index(name))
        found[cv.index] = sorted(ks)
    return found


def describe(definition: Sequence[Mapping[str, Any]], parties: Mapping[tuple[str, str], str | None] | None = None) -> str:
    """A pattern in words: "United States Senator: Jane Doe (DEM); Governor: John Roe (REP)"."""
    parts = []
    for item in definition:
        names = []
        for c in item.get("choices") or []:
            p = (parties or {}).get((str(item["contest"]), str(c)))
            names.append(f"{c} ({p})" if p else str(c))
        parts.append(f"{item['contest']}: {' and '.join(names)}")
    return "; ".join(parts) or "(no requirement)"


@dataclass(frozen=True)
class Preset:
    label: str
    requirement: dict[int, list[int]]


def presets(shown: Sequence[ShownContest]) -> list[Preset]:
    """One-click split-ticket patterns: the top DEM candidate in one contest and the top REP in another, both ways.

    For the two tightest statewide contests, and for the tightest statewide
    contest against each US House contest. Empty when the parties are not known.
    """
    def has_both(c: ShownContest) -> bool:
        return c.top("DEM") is not None and c.top("REP") is not None

    statewide = [c for c in shown if c.group == "statewide" and has_both(c)]
    house = [c for c in shown if c.group == "house" and has_both(c)]
    pairs: list[tuple[ShownContest, ShownContest]] = []
    if len(statewide) >= 2:
        pairs.append((statewide[0], statewide[1]))
    if statewide:
        pairs += [(statewide[0], h) for h in house]
    found = []
    for a, b in pairs:
        for first, second in (("DEM", "REP"), ("REP", "DEM")):
            ka, kb = a.top(first), b.top(second)
            assert ka is not None and kb is not None
            label = f"{a.name}: {a.votes.choices[ka]} ({first}), with {b.name}: {b.votes.choices[kb]} ({second})"
            found.append(Preset(label, {a.index: [ka], b.index: [kb]}))
    return found


@dataclass
class Pattern:
    """A pattern of votes: its definition and the hash that names it (of its canonical record), with a name in words."""

    sha256: str
    county: str
    name: str
    definition: list[dict[str, Any]]


def pattern_of(county: str, definition: Sequence[Mapping[str, Any]]) -> Pattern:
    """The pattern with this definition, named by `describe` and identified by the SHA-256 of its record.

    Raises:
        DrawError: for an empty definition.
    """
    if not definition:
        raise DrawError("choose at least one candidate first")
    record = {"kind": "targeted_pattern", "format": 2, "county": county, "definition": list(definition)}
    return Pattern(hashlib.sha256(canonical_json(record)).hexdigest(), county, describe(definition), [dict(d) for d in definition])


# ---------------------------------------------------------------------------
# Drawing


def derived_seed(*, county: str, cvr_sha256: str, definition: Sequence[Mapping[str, Any]], seed: str, draw: int, method: str = METHOD) -> str:
    """The draw's derived seed: SHA-256 of the canonical serialization of its inputs (see the module documentation)."""
    inputs = {"county": county, "cvr_sha256": cvr_sha256, "draw": draw, "method": method, "pattern": list(definition), "seed": seed}
    return hashlib.sha256(canonical_json(inputs)).hexdigest()


def ballot_key(derived: str, imprinted_id: str) -> str:
    """A matching ballot's key in a draw: SHA-256 of the derived seed followed by its imprinted ID."""
    return hashlib.sha256((derived + imprinted_id).encode("utf-8")).hexdigest()


def choose(ids: Iterable[str], derived: str, size: int = DRAW_SIZE, exclude: Mapping[str, str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """The ``size`` imprinted IDs with the smallest keys (`ballot_key`) not in ``exclude`` (by `id_key`), and the IDs skipped before them with where each is held."""
    chosen: list[str] = []
    skipped: list[tuple[str, str]] = []
    for imprinted in sorted(ids, key=lambda i: (ballot_key(derived, i), i)):
        if len(chosen) >= size:
            break
        where = (exclude or {}).get(id_key(imprinted))
        if where is not None:
            skipped.append((imprinted, where))
        else:
            chosen.append(imprinted)
    return chosen, skipped


def clean_seed(seed: str) -> str:
    """A seed as entered: any text, its spaces collapsed, 1 to `MAX_SEED` characters, no control characters.

    Raises:
        DrawError: for an empty or over-long seed.
    """
    text = " ".join(_CONTROL_RE.sub(" ", seed).split())
    if not text:
        raise DrawError("enter a seed for the draw: any text will do")
    if len(text) > MAX_SEED:
        raise DrawError(f"the seed is at most {MAX_SEED} characters")
    return text


@dataclass
class DrawRecord:
    """A draw's full record, as stored in the workspace (its canonical JSON) and committed to by the ``targets.drawn`` event."""

    county: str
    pattern_name: str
    pattern_sha256: str
    definition: list[dict[str, Any]]
    cvr_sha256: str
    manifest_sha256: str | None
    seed: str
    seed_source: str
    draw: int
    derived_seed: str
    matching: int
    matching_clean: int
    #: In CORLA's pull order: ``imprinted_id``, ``tabulator``, ``batch``, ``record``, ``location``, ``ballot_type``.
    ballots: list[dict[str, Any]]
    actor: str
    time: str = ""
    method: str = METHOD
    #: Ballots asked for (the draw has fewer when fewer match).
    size: int = DRAW_SIZE
    #: Ballots skipped because they were already held: ``imprinted_id``, ``held_by`` ("the working list", "Targeted list 1", "the RLA sample").
    collisions: list[dict[str, str]] = field(default_factory=list)
    format: int = 2

    @property
    def source_id(self) -> str:
        return f"draw {self.draw}"

    @property
    def ids(self) -> list[str]:
        return [str(b["imprinted_id"]) for b in self.ballots]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "targeted_draw",
            "format": self.format,
            "note": "Targeted ballots: not part of the RLA sample.",
            "county": self.county,
            "pattern": {"name": self.pattern_name, "sha256": self.pattern_sha256, "definition": self.definition},
            "cvr_sha256": self.cvr_sha256,
            "manifest_sha256": self.manifest_sha256,
            "seed": self.seed,
            "seed_source": self.seed_source,
            "draw": self.draw,
            "method": self.method,
            "derived_seed": self.derived_seed,
            "size": self.size,
            "matching": self.matching,
            "matching_clean": self.matching_clean,
            "ballots": self.ballots,
            "collisions": self.collisions,
            "actor": self.actor,
            "time": self.time,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DrawRecord:
        pattern = data.get("pattern") or {}
        return cls(
            county=str(data["county"]),
            pattern_name=str(pattern.get("name")),
            pattern_sha256=str(pattern.get("sha256")),
            definition=list(pattern.get("definition") or []),
            cvr_sha256=str(data["cvr_sha256"]),
            manifest_sha256=data.get("manifest_sha256"),
            seed=str(data["seed"]),
            seed_source=str(data.get("seed_source")),
            draw=int(data["draw"]),
            derived_seed=str(data["derived_seed"]),
            matching=int(data["matching"]),
            matching_clean=int(data.get("matching_clean", 0)),
            ballots=list(data.get("ballots") or []),
            actor=str(data.get("actor")),
            time=str(data.get("time") or ""),
            method=str(data.get("method") or METHOD_1),
            size=int(data.get("size") or DRAW_SIZE),
            collisions=[dict(c) for c in data.get("collisions") or []],
            format=int(data.get("format") or 1),
        )


def draw_count(log: EventLog, county: str) -> int:
    """The county's draws recorded so far."""
    return sum(1 for e in log.iter_events() if e.kind == "targets.drawn" and e.county == county)


def draw(
    votes: CvrVotes,
    *,
    county: str,
    cvr_sha256: str,
    pattern: Pattern,
    seed: str,
    seed_source: str,
    number: int,
    locations: Mapping[tuple[str, str], str],
    manifest_sha256: str | None,
    actor: str,
    size: int = DRAW_SIZE,
    exclude: Mapping[str, str] | None = None,
) -> DrawRecord:
    """A pattern's draw (see the module documentation), not yet recorded; ``exclude`` are the ballots already held (`holders`), by `id_key`.

    Raises:
        DrawError: if the pattern's contests or choices are not in the CVR,
            nothing matches, every matching ballot is already held, or the
            seed is empty or over-long.
    """
    seed = clean_seed(seed)
    if not 1 <= size <= MAX_DRAW:
        raise DrawError(f"a draw takes 1 to {MAX_DRAW} ballots")
    requirement = requirement_of(pattern.definition, votes)
    if requirement is None:
        raise DrawError("the pattern names a contest or candidate that is not in the county's current CVR")
    mask = votes.match(requirement)
    matching, clean = mask.bit_count(), (mask & votes.clean).bit_count()
    if matching == 0:
        raise DrawError("no ballot matches this pattern")
    rows = {votes.imprinted(i): i for i in bits_of(mask)}
    derived = derived_seed(county=county, cvr_sha256=cvr_sha256, definition=pattern.definition, seed=seed, draw=number)
    chosen, skipped = choose(rows, derived, size, exclude)
    if not chosen:
        raise DrawError("every ballot matching this pattern is already on a list or in the RLA sample")
    ordered = corla_order(chosen, locations)
    ballots = []
    for imprinted in ordered:
        i = rows[imprinted]
        parts = imprinted.split("-")
        ballots.append(
            {
                "imprinted_id": imprinted,
                "tabulator": parts[0] if len(parts) == 3 else "",
                "batch": parts[1] if len(parts) == 3 else "",
                "record": parts[2] if len(parts) == 3 else "",
                "location": locations.get((id_key(parts[0]), id_key(parts[1]))) if len(parts) == 3 else None,
                "ballot_type": votes.ballot_types[i] or None,
            }
        )
    return DrawRecord(
        county=county,
        pattern_name=pattern.name,
        pattern_sha256=pattern.sha256,
        definition=pattern.definition,
        cvr_sha256=cvr_sha256,
        manifest_sha256=manifest_sha256,
        seed=seed,
        seed_source=seed_source,
        draw=number,
        derived_seed=derived,
        matching=matching,
        matching_clean=clean,
        ballots=ballots,
        actor=actor,
        size=size,
        collisions=[{"imprinted_id": i, "held_by": where} for i, where in skipped],
    )


_record_lock = threading.Lock()


def record_draw(workspace: Workspace, log: EventLog, record: DrawRecord, *, session_id: str | None = None) -> tuple[DrawRecord, str]:
    """Store the draw's record and record ``targets.drawn``; returns the record (with its time) and its SHA-256.

    Raises:
        DrawError: if another draw was recorded for the county since this one was numbered.
    """
    check_actor(record.actor)
    with _record_lock:
        if draw_count(log, record.county) + 1 != record.draw:
            raise DrawError("another draw was recorded meanwhile; draw again")
        from datetime import UTC, datetime

        from corla_observer.events import format_server_time

        record.time = format_server_time(datetime.now(UTC))
        sha = workspace.put_bytes(canonical_json(record.to_json())).sha256
        log.append(
            "targets.drawn",
            record.actor,
            county=record.county,
            file_sha256=sha,
            session_id=session_id,
            data={
                "draw": record.draw, "ballots": len(record.ballots), "size": record.size, "collisions": len(record.collisions),
                "sha256": sha, "pattern_sha256": record.pattern_sha256,
            },  # fmt: skip
        )
    return record, sha


def draws(workspace: Workspace, log: EventLog, county: str) -> list[tuple[DrawRecord, str]]:
    """The county's recorded draws, with their records' SHA-256, in order."""
    found = []
    for event in log.iter_events():
        if event.kind == "targets.drawn" and event.county == county and event.file_sha256:
            try:
                found.append((DrawRecord.from_json(json.loads(workspace.get_bytes(event.file_sha256))), event.file_sha256))
            except Exception:
                continue
    return found


# ---------------------------------------------------------------------------
# Ballots looked up, and manual entries


@dataclass
class Entry:
    """A ballot on the working list or a sent list, or looked up for one."""

    imprinted_id: str
    #: The IDs of the sources it came from (``draw N``, ``entry SEQ``), in order.
    sources: list[str] = field(default_factory=list)
    tabulator: str = ""
    batch: str = ""
    record: str = ""
    location: str | None = None
    ballot_type: str | None = None
    #: ``ballot``, ``redacted`` or ``aggregate`` (its CVR row's status), or None when it is not in the CVR.
    cvr_status: str | None = None
    #: Whether its batch is in the manifest, with at least as many ballots as its position.
    in_manifest: bool = False
    #: Whether the county's RLA sample holds it too.
    in_rla: bool = False

    @property
    def found(self) -> bool:
        return self.cvr_status is not None or self.in_manifest

    def row(self) -> dict[str, Any]:
        return {
            "imprinted_id": self.imprinted_id, "tabulator": self.tabulator, "batch": self.batch, "record": self.record,
            "location": self.location, "ballot_type": self.ballot_type, "in_rla": self.in_rla,
        }  # fmt: skip


_ID_RE = re.compile(r"^\s*([0-9A-Za-z]{1,12})\s*-\s*([0-9A-Za-z]{1,12})\s*-\s*([0-9]{1,6})\s*$")


def normalize_id(text: str) -> str | None:
    """An imprinted ID as typed ("101-12-5", spaces allowed), as tabulator-batch-position, or None if it is not one."""
    match = _ID_RE.match(text)
    return "-".join(match.groups()) if match else None


def lookup(imprinted_id: str, votes: CvrVotes, locations: Mapping[tuple[str, str], str], sizes: Mapping[tuple[str, str], int]) -> Entry:
    """Where a ballot is: its CVR row (matched by `corla_observer.extract.id_key`) and its batch in the manifest."""
    row = votes.row_of(imprinted_id)
    parts = imprinted_id.split("-")
    entry = Entry(imprinted_id if row is None else votes.imprinted(row))
    if len(parts) == 3:
        batch_key = (id_key(parts[0]), id_key(parts[1]))
        entry.tabulator, entry.batch, entry.record = parts
        entry.location = locations.get(batch_key)
        size = sizes.get(batch_key)
        entry.in_manifest = size is not None and parts[2].isdigit() and 1 <= int(parts[2]) <= size
    if row is not None:
        entry.ballot_type = votes.ballot_types[row] or None
        entry.cvr_status = votes.statuses[row]
    return entry


MAX_NOTE = 500


def enter_ids(workspace: Workspace, log: EventLog, county: str, ids: Sequence[str], actor: str, *, note: str = "", session_id: str | None = None) -> int:
    """Record a manual entry (``targets.listed``): imprinted IDs entered by hand, with a note; returns the event's seq (the source is ``entry SEQ``).

    Raises:
        DrawError: for no IDs, or a working list that would be longer than `MAX_LIST`.
    """
    check_actor(actor)
    new = list(dict.fromkeys(ids))
    if not new:
        raise DrawError("enter an imprinted ID, such as 101-12-5")
    held = {id_key(i) for s in sources(workspace, log, county) if s.sent_in is None and s.included for i in s.ids}
    if len(held | {id_key(i) for i in new}) > MAX_LIST:
        raise DrawError(f"a working list holds at most {MAX_LIST} ballots")
    text = " ".join(_CONTROL_RE.sub(" ", note).split())[:MAX_NOTE]
    event = log.append("targets.listed", actor, county=county, session_id=session_id, data={"ids": new, "source": "entered", "note": text})
    return event.seq


# ---------------------------------------------------------------------------
# Sources, the working list and the sent lists


@dataclass
class Source:
    """A source of the working list: a draw, or a manual entry; sent in a list or still in the working list."""

    #: ``draw N`` or ``entry SEQ``.
    id: str
    #: ``draw`` or ``entered``.
    kind: str
    seq: int
    actor: str
    time: str
    #: Its ballots' imprinted IDs.
    ids: list[str]
    included: bool = True
    note: str = ""
    draw: DrawRecord | None = None
    draw_sha256: str | None = None
    #: The number of the targeted list it was sent in, and that list's key; None while in the working list.
    sent_in: int | None = None
    sent_key: str | None = None

    @property
    def description(self) -> str:
        """In words: the pattern, seed and size of a draw; the IDs and note of an entry."""
        if self.draw is not None:
            text = f"{describe(self.draw.definition)} · seed “{self.draw.seed}” · {len(self.ids)} of {self.draw.size} asked"
            if self.draw.collisions:
                text += f" ({len(self.draw.collisions)} already listed, skipped)"
            return text
        ids = ", ".join(self.ids[:8]) + (f", … ({len(self.ids)})" if len(self.ids) > 8 else "")
        return f"{ids}" + (f" — {self.note}" if self.note else "")

    @property
    def title(self) -> str:
        """"Draw 3" or "Entered by ID"."""
        return f"Draw {self.draw.draw}" if self.draw is not None else "Entered by ID"

    def record_item(self) -> dict[str, Any]:
        """The source as a list record (`list_record`) stores it."""
        item: dict[str, Any] = {"id": self.id, "kind": self.kind, "description": self.description, "ballots": len(self.ids), "actor": self.actor, "time": self.time}
        if self.draw is not None:
            item |= {
                "draw": self.draw.draw, "name": self.draw.pattern_name, "pattern": describe(self.draw.definition), "definition": self.draw.definition, "seed": self.draw.seed,
                "size": self.draw.size, "record_sha256": self.draw_sha256, "collisions": len(self.draw.collisions),
            }  # fmt: skip
        else:
            item |= {"note": self.note, "ids": list(self.ids)}
        return item


def _list_numbers(log: EventLog, county: str) -> dict[str, tuple[int, str]]:
    """Each targeted list key of the county, with its number and name (`corla_observer.pulllists`)."""
    from corla_observer.pulllists import county_lists

    return {x.key: (x.number, x.name) for x in county_lists(log.iter_events(), county, retired=True) if x.targeted}


def sources(workspace: Workspace | None, log: EventLog, county: str) -> list[Source]:
    """The county's sources in the order made, each with its inclusion and the list it was sent in (see docs/TARGETING.md, "Compatibility").

    Without ``workspace`` the draws' records are not read: a draw's ballots
    are then unknown (empty), which is enough for counting the manual
    entries' ballots.
    """
    found: dict[str, Source] = {}
    legacy_listed_draws: set[str] = set()
    legacy_present: dict[str, bool] = {}  # the first design's pull list, replayed by id_key
    legacy_formats: dict[str, int] = {}
    legacy_records: dict[str, tuple[str, int]] = {}  # list key -> (record sha, built seq), for lists without targets.sent
    sent_keys: set[str] = set()
    decided: set[str] = set()  # sources with a targets.included event, whose inclusion the first design's events don't decide
    for event in log.iter_events():
        if event.county != county:
            continue
        data = event.data
        if event.kind == "targets.drawn" and event.file_sha256:
            record = None
            if workspace is not None:
                try:
                    record = DrawRecord.from_json(json.loads(workspace.get_bytes(event.file_sha256)))
                except Exception:
                    record = None
            drawn = data.get("draw")
            number = (drawn if isinstance(drawn, int) else 0) if record is None else record.draw
            source = Source(
                f"draw {number}", "draw", event.seq, event.actor, event.server_time, record.ids if record else [],
                included=record is not None and record.format >= 2, draw=record, draw_sha256=event.file_sha256,
            )  # fmt: skip
            legacy_formats[source.id] = record.format if record else 1
            found[source.id] = source
        elif event.kind == "targets.listed":
            listed = data.get("ids")
            ids = [str(i) for i in listed] if isinstance(listed, list) else []
            kind = str(data.get("source") or "entered")
            for i in ids:
                legacy_present[id_key(i)] = True
            if kind.startswith("draw "):
                legacy_listed_draws.add(kind)
                if kind in found:
                    found[kind].included = True
            else:
                found[f"entry {event.seq}"] = Source(f"entry {event.seq}", "entered", event.seq, event.actor, event.server_time, ids, note=str(data.get("note") or ""))
        elif event.kind == "targets.unlisted":
            unlisted = data.get("ids")
            for removed in unlisted if isinstance(unlisted, list) else []:
                legacy_present[id_key(str(removed))] = False
        elif event.kind == "targets.included":
            flagged = found.get(str(data.get("source")))
            if flagged is not None:
                flagged.included = bool(data.get("included"))
                decided.add(flagged.id)
        elif event.kind == "targets.sent":
            key = str(data.get("list") or "")
            sent_keys.add(key)
            number_sent = data.get("number")
            listed_sources = data.get("sources")
            for sid in listed_sources if isinstance(listed_sources, list) else []:
                sent_source = found.get(str(sid))
                if sent_source is not None:
                    sent_source.sent_in, sent_source.sent_key = (number_sent if isinstance(number_sent, int) else 0), key
        elif event.kind == "extract.built" and data.get("order") == "targeted" and isinstance(data.get("list_record_sha256"), str):
            legacy_records[str(data.get("list"))] = (str(data["list_record_sha256"]), event.seq)
    # Lists built by the first design (no targets.sent): their sources from their list records.
    legacy_keys = [k for k in legacy_records if k not in sent_keys]
    if legacy_keys and workspace is not None:
        numbers = _list_numbers(log, county)
        for key in legacy_keys:
            sha, built_seq = legacy_records[key]
            try:
                record = json.loads(workspace.get_bytes(sha))
            except Exception:
                continue
            number = numbers.get(key, (0, ""))[0]
            for ballot in record.get("ballots") or []:
                label, imprinted = str(ballot.get("source") or ""), str(ballot.get("imprinted_id") or "")
                if label.startswith("draw ") and label in found and found[label].sent_in is None:
                    found[label].sent_in, found[label].sent_key = number, key
                elif label == "entered" or label not in found:
                    for source in found.values():
                        if source.kind == "entered" and source.seq < built_seq and source.sent_in is None and id_key(imprinted) in {id_key(i) for i in source.ids}:
                            source.sent_in, source.sent_key = number, key
    # The first design's removals: ballots taken off its pull list by hand, for sources not sent.
    for source in found.values():
        if source.sent_in is not None or source.id in decided:
            continue
        if source.kind == "draw" and legacy_formats.get(source.id, 2) < 2:
            present = [i for i in source.ids if legacy_present.get(id_key(i))]
            if source.id in legacy_listed_draws:
                source.included = bool(present)
                source.ids = present or source.ids
            else:
                source.included = False
        elif source.kind == "entered":
            kept = [i for i in source.ids if legacy_present.get(id_key(i), True)]
            if not kept and source.ids:
                source.included = False
            source.ids = kept or source.ids
    return sorted(found.values(), key=lambda s: s.seq)


def set_included(log: EventLog, county: str, source_id: str, included: bool, actor: str, *, session_id: str | None = None) -> None:
    """Include a source in the working list, or exclude it (``targets.included``).

    Raises:
        DrawError: for a source the county does not have, or one already sent.
    """
    check_actor(actor)
    source = next((s for s in sources(None, log, county) if s.id == source_id), None)
    if source is None:
        raise DrawError("no such source")
    if source.sent_in is not None:
        raise DrawError(f"that source was sent in Targeted list {source.sent_in}")
    log.append("targets.included", actor, county=county, session_id=session_id, data={"source": source_id, "included": included})


def working_sources(found: Sequence[Source]) -> list[Source]:
    """The sources not sent yet, included or not."""
    return [s for s in found if s.sent_in is None]


def working_list(
    found: Sequence[Source], votes: CvrVotes, locations: Mapping[tuple[str, str], str], sizes: Mapping[tuple[str, str], int],
    rla: Collection[str] = (),
) -> list[Entry]:
    """The working list: each ballot of the included, unsent sources once, looked up (`lookup`), in CORLA's pull order; ``rla`` are the RLA sample's IDs by `id_key`."""
    entries: dict[str, Entry] = {}
    for source in found:
        if source.sent_in is not None or not source.included:
            continue
        for imprinted in source.ids:
            key = id_key(imprinted)
            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = lookup(imprinted, votes, locations, sizes)
                entry.imprinted_id = imprinted
                entry.in_rla = key in rla
            if source.id not in entry.sources:
                entry.sources.append(source.id)
    by_id = {e.imprinted_id: e for e in entries.values()}
    return [by_id[i] for i in corla_order(by_id, locations)]


def rla_ids(workspace: Workspace, log: EventLog, county: str) -> set[str]:
    """The imprinted IDs (by `id_key`) of the county's RLA sample, as its RLA extract lists them; empty without one."""
    found = latest_extract(workspace, log, county)
    if found is None:
        return set()
    return {id_key(str(b["imprinted_id"])) for b in found[0]["ballots"]}


def holders(workspace: Workspace, log: EventLog, county: str, found: Sequence[Source]) -> dict[str, str]:
    """Where each ballot a draw must skip is held, by `id_key`: the RLA sample, an earlier sent list ("Targeted list N"), or the working list."""
    from corla_observer.pulllists import county_lists, load

    held: dict[str, str] = {}
    for item in county_lists(log.iter_events(), county, retired=True):
        if not item.targeted:
            continue
        try:
            ballots = load(workspace, item)["ballots"]
        except Exception:
            continue
        for b in ballots:
            held.setdefault(id_key(str(b["imprinted_id"])), f"Targeted list {item.number}")
    for source in found:
        if source.sent_in is None and source.included:
            for i in source.ids:
                held.setdefault(id_key(i), "the working list")
    for key in rla_ids(workspace, log, county):
        held[key] = "the RLA sample"
    return held


def list_name(number: int) -> str:
    return f"Targeted list {number}"


def next_list_number(log: EventLog, county: str) -> tuple[int, str]:
    """The number and name of the county's next targeted list: one more than the last, under a name no list of the county has."""
    taken = _list_numbers(log, county)
    number = 1 + max((n for n, _ in taken.values()), default=0)
    while targeted_key(list_name(number)) in taken:
        number += 1
    return number, list_name(number)


def list_record(county: str, name: str, number: int, entries: Sequence[Entry], found: Sequence[Source]) -> dict[str, Any]:
    """A sent list's record (format 2): its name and number, its sources, and its ballots with their sources and whether each is also in the RLA sample."""
    chosen = {s.id: s for s in found}
    used = sorted({sid for e in entries for sid in e.sources}, key=lambda sid: chosen[sid].seq if sid in chosen else 0)
    return {
        "kind": "targeted_list",
        "format": 2,
        "note": "Targeted ballots: not part of the RLA sample.",
        "county": county,
        "name": name,
        "number": number,
        "sources": [chosen[sid].record_item() for sid in used if sid in chosen],
        "ballots": [
            {"imprinted_id": e.imprinted_id, "location": e.location, "sources": list(e.sources), "in_rla": e.in_rla} for e in entries
        ],
    }


def list_record_sha256(record: Mapping[str, Any]) -> str:
    """The hash a list record has when stored (`store_list_record`)."""
    return hashlib.sha256(canonical_json(dict(record))).hexdigest()


def store_list_record(workspace: Workspace, record: Mapping[str, Any]) -> str:
    return workspace.put_bytes(canonical_json(dict(record))).sha256


def record_patterns(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A list record's draws in words, either format: ``[{"draw": N, "name": ..., "description": ...}]``."""
    if int(record.get("format") or 1) >= 2:
        return [
            {"draw": s.get("draw"), "name": s.get("name"), "description": s.get("pattern") or s.get("description")}
            for s in record.get("sources") or []
            if s.get("kind") == "draw"
        ]
    return [{"draw": p.get("draw"), "name": p.get("name"), "description": p.get("description")} for p in record.get("patterns") or []]


@dataclass
class SentList:
    """What `send` made: the list's number, name and key, its extract and record hashes, and its ballots."""

    number: int
    name: str
    key: str
    extract_sha256: str
    record_sha256: str
    entries: list[Entry]
    sources: list[str]
    seq: int

    @property
    def in_rla(self) -> int:
        return sum(1 for e in self.entries if e.in_rla)


_send_lock = threading.Lock()


def send(
    workspace: Workspace, log: EventLog, county: str, actor: str, *, votes: CvrVotes, locations: Mapping[tuple[str, str], str],
    sizes: Mapping[tuple[str, str], int], boards: int = 1, session_id: str | None = None,
) -> SentList:
    """Send the working list to the county: freeze it as the next Targeted list (its record, its extract and ``targets.sent``).

    Raises:
        DrawError: for an empty working list.
        ExtractError: if the extract can't be built.
    """
    from corla_observer.extract import build_extract

    check_actor(actor)
    with _send_lock:
        found = sources(workspace, log, county)
        rla = rla_ids(workspace, log, county)
        entries = working_list(found, votes, locations, sizes, rla)
        if not entries:
            raise DrawError("the working list is empty: nothing to send")
        number, name = next_list_number(log, county)
        used = sorted({sid for e in entries for sid in e.sources})
        record = list_record(county, name, number, entries, found)
        record_sha = store_list_record(workspace, record)
        body = pull_list_bytes(county, entries, boards)
        built = build_extract(workspace, log, county, boards=boards, actor=actor, targeted=body, list_name=name, list_record=record_sha)
        event = log.append(
            "targets.sent",
            actor,
            county=county,
            session_id=session_id,
            data={
                "list": targeted_key(name), "list_name": name, "number": number, "sources": used, "ballots": len(entries),
                "in_rla": sum(1 for e in entries if e.in_rla), "extract_sha256": built.sha256, "list_record_sha256": record_sha,
            },  # fmt: skip
        )
    return SentList(number, name, targeted_key(name), built.sha256, record_sha, entries, used, event.seq)


def sent_entries(record: Mapping[str, Any]) -> list[Entry]:
    """A sent list's ballots as entries, from its record (either format): the IDs, locations, sources and RLA overlap it stored."""
    entries = []
    for b in record.get("ballots") or []:
        imprinted = str(b.get("imprinted_id") or "")
        parts = imprinted.split("-")
        entry = Entry(imprinted, list(b.get("sources") or ([str(b["source"])] if b.get("source") else [])), location=b.get("location"), in_rla=bool(b.get("in_rla")))
        if len(parts) == 3:
            entry.tabulator, entry.batch, entry.record = parts
        entries.append(entry)
    return entries


def pull_list_bytes(county: str, entries: Sequence[Entry], boards: int = 1) -> bytes:
    """A list as a pull list file (as CORLA's, `corla_observer.extract.read_pull_list` reads it), split into ``boards`` contiguous runs."""
    if not entries:
        raise DrawError("the list is empty")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["County", "Imprinted ID", "Audit Board", "Storage Location", "Ballot Type"])
    per_board = max(1, -(-len(entries) // max(1, boards)))
    for n, e in enumerate(entries):
        writer.writerow([county, e.imprinted_id, f"Audit Board {n // per_board + 1}", e.location or "", e.ballot_type or ""])
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# For email and for a spreadsheet

PULL_COLUMNS = (("imprinted_id", "Imprinted ID"), ("tabulator", "Tabulator"), ("batch", "Batch"), ("record", "Position"),
                ("location", "Storage location"), ("ballot_type", "Ballot type"))  # fmt: skip
ALSO_RLA = "also in the RLA sample"


def expected_for(ids: Iterable[str], definition: Sequence[Mapping[str, Any]], votes: CvrVotes) -> dict[str, str]:
    """Each ballot's votes in the contests of ``definition``, as "Contest: choice; ...", by imprinted ID."""
    found = {}
    for imprinted in ids:
        row = votes.row_of(imprinted)
        parts = []
        for item in definition:
            cv = votes.contest_by_name(str(item["contest"]))
            if cv is None or row is None:
                continue
            marked = [cv.choices[k] for k, m in enumerate(cv.masks) if m >> row & 1]
            parts.append(f"{cv.name}: {' + '.join(marked) or 'no vote'}")
        found[imprinted] = "; ".join(parts) if row is not None else "not in the CVR"
    return found


def table_lines(ballots: Sequence[Mapping[str, Any]], expected: Mapping[str, str] | None = None) -> list[str]:
    """One line per ballot, in aligned columns (the last, the expected votes if given, not padded); a ballot also in the RLA sample says so."""
    header = ["#"] + [label for _, label in PULL_COLUMNS] + (["Expected votes"] if expected is not None else [])
    table = [header]
    for n, b in enumerate(ballots, start=1):
        row = [str(n)] + [str(b.get(key) or "") for key, _ in PULL_COLUMNS]
        if b.get("in_rla"):
            row[-1] = (row[-1] + "  " if row[-1] else "") + f"({ALSO_RLA})"
        if expected is not None:
            row.append(expected.get(str(b["imprinted_id"]), ""))
        table.append(row)
    padded = len(header) - (1 if expected is not None else 0)
    widths = [max(len(r[i]) for r in table) for i in range(padded)]
    return ["  ".join([r[i].ljust(widths[i]) for i in range(padded)] + r[padded:]).rstrip() for r in table]


def email_text(title: str, details: Sequence[str], ballots: Sequence[Mapping[str, Any]], expected: Mapping[str, str] | None = None) -> str:
    """A pull list as plain text for an email: the title and detail lines, a blank line, then one ballot per line.

    The expected votes only when ``expected`` is given: the list goes to
    the people who pull the ballots, who should not learn how they are
    expected to be marked.
    """
    return "\n".join([title, *details, "", *table_lines(ballots, expected)]) + "\n"


def draw_email(record: DrawRecord, sha256: str, expected: Mapping[str, str] | None = None) -> str:
    """A draw's ballots for an email (`email_text`): county, count, draw, seed and date in the header."""
    details = [
        f"{len(record.ballots)} ballots (draw size {record.size}), draw {record.draw}, seed “{record.seed}”, drawn {record.time[:10]} (targeted ballots, not part of the RLA sample)",
    ]
    if record.collisions:
        details.append(f"{len(record.collisions)} ballots already listed or sampled were skipped")
    if expected is not None:
        details.append(f"Pattern: {describe(record.definition)}; {record.matching} ballots match")
    details.append(f"Record {sha256[:16]}")
    return email_text(f"Targeted ballots for {county_title(record.county)}: draw {record.draw}", details, record.ballots, expected)


def list_email(county: str, title: str, entries: Sequence[Entry], date: str, details: Sequence[str] = ()) -> str:
    """A list for an email (`email_text`): its title in the header ("Targeted list 2 for Delta County"), the date, the counts and any detail lines."""
    also = sum(1 for e in entries if e.in_rla)
    lines = [f"{len(entries)} ballots, {date} (targeted ballots, not part of the RLA sample)" + (f"; {also} {ALSO_RLA}: no need to pull them again" if also else "")]
    return email_text(f"{title} for {county_title(county)}", [*lines, *details], [e.row() for e in entries])


def county_title(county: str) -> str:
    """"Weld County"; a test or demonstration county's name as it is ("Delta (2020 demo)")."""
    return county if is_test_county(county) else f"{county} County"


def _safe(text: str) -> str:
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


def to_csv(county: str, label: str, ballots: Sequence[Mapping[str, Any]], expected: Mapping[str, str] | None = None) -> str:
    """A list as CSV, escaped against formula injection; with the expected votes only when given. ``label`` names the draw or list."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["county", "list", "position"] + [key for key, _ in PULL_COLUMNS] + ["also_in_rla_sample"] + (["expected_votes"] if expected is not None else []))
    for n, b in enumerate(ballots, start=1):
        row = [county, label, str(n)] + [str(b.get(key) or "") for key, _ in PULL_COLUMNS] + ["yes" if b.get("in_rla") else ""]
        if expected is not None:
            row.append(expected.get(str(b["imprinted_id"]), ""))
        writer.writerow([_safe(c) for c in row])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Cache


@dataclass
class _Cache:
    lock: threading.Lock = field(default_factory=threading.Lock)
    items: dict[str, CvrVotes] = field(default_factory=dict)


_CACHE = _Cache()
#: CVRs kept read (the three demonstration counties and Testland, or a few real ones).
CACHE_SIZE = 4


def cached_votes(workspace: Workspace, cvr_sha256: str) -> CvrVotes:
    """`load_votes` of a stored CVR, every contest, kept for the last `CACHE_SIZE` CVRs: counting is then bit operations only."""
    with _CACHE.lock:
        found = _CACHE.items.get(cvr_sha256)
    if found is not None:
        return found
    votes = load_votes(workspace.path_for(cvr_sha256))
    with _CACHE.lock:
        _CACHE.items[cvr_sha256] = votes
        while len(_CACHE.items) > CACHE_SIZE:
            _CACHE.items.pop(next(iter(_CACHE.items)))
    return votes


# ---------------------------------------------------------------------------
# The county's allowed total


def set_allowed(log: EventLog, county: str, allowed: int | None, actor: str, *, session_id: str | None = None) -> None:
    """Record the most targeted ballots the county allows in all (``targets.allowed_set``); None clears it."""
    check_actor(actor)
    if allowed is not None and not 1 <= allowed <= 100_000:
        raise DrawError("the allowed total is 1 to 100000 ballots")
    log.append("targets.allowed_set", actor, county=county, session_id=session_id, data={"allowed": allowed})


def allowed_total(log: EventLog, county: str) -> int | None:
    """The county's allowed total of targeted ballots, as last recorded, or None."""
    allowed = None
    for event in log.iter_events():
        if event.kind == "targets.allowed_set" and event.county == county:
            value = event.data.get("allowed")
            allowed = value if isinstance(value, int) else None
    return allowed
