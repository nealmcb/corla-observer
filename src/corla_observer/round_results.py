"""What CORLA's posted results say, round by round, in plain words, with the file each number comes from.

Built only from the Audit Center files the crawler fetched, as the data map
(`corla_observer.datamap`) holds them for the election: CORLA's contest lists
(``contests_list``: ``round1/contest.csv``, ``round2/contest.csv``, ...), its
CVR-to-audit-board comparisons (``cvr_comparison``:
``round1/contestComparison.csv``, ...) and its contest-to-county file
(``contest_ids_by_county``: ``contestsByCounty.csv``). A file a county
uploaded is never used here.

**A round's contest list** describes the audit when that round begins: the
2026 general's ``round1/contest.csv`` has no card audited for any contest and
every contest ``in_progress``; its ``round2/contest.csv`` has the cards
audited in round one (``audited_sample_count``: Jackson County Assessor's
33 are the 33 cards of Jackson in ``round1/contestComparison.csv``), the
discrepancies found, each contest's ``random_audit_status`` after round one
(``risk_limit_achieved``, ``in_progress``, ``ended``, ``not_auditable``) and
CORLA's estimate of the cards it needs (``estimated_samples_to_audit``). A
list's round is the one its URL names (`corla_observer.datamap.round_of`),
or, for a list that names none, round one if no targeted contest has a card
audited (as `corla_observer.datamap.contest_list_round` decides); a list of
unknown round is not used. Of several lists for one round, the one fetched
last is used. Only targeted contests are kept (``audit_reason``
``state_wide_contest`` or ``county_wide_contest``); opportunistic ones
(``opportunistic_benefits``) are not.

**Each targeted contest's counties** (`contest_counties`): a statewide
contest covers every county (those the contest-to-county file lists for it,
else every county in CORLA's audit). A county-wide contest belongs to the
county its name begins with before " - " ("Morgan - Proposition 137
(STATUTORY)", CORLA's name for a statewide contest it targets in one county),
else the county in a "(County)" suffix, else the counties the contest-to-county
file lists for its exact name, else the county its name begins with followed
by "County" ("Weld County Council - At-Large", "CLEAR CREEK COUNTY BALLOT
ISSUE 1A"). Ambiguity is shown, never resolved by guessing: a prefix that
the contest-to-county file contradicts keeps the prefix's county and says
so; a name the file lists for several counties covers all of them, and says
so; a contest with none of these has no county ("county not known"), and no
county is placed in a round by it. In the 2026 general every targeted
contest is listed for exactly one county (or for every county, statewide)
and agrees with its prefix.

**Where the audit stands** (`RoundResults.standings`): for each round's
list, how many targeted contests met the risk limit and how many continue.
**What continues, and why** (`RoundResults.continuing`): each targeted
contest still ``in_progress`` in the latest list, with its counties, the
cards audited, the discrepancies counted (overstatements and understatements
of one or two votes, in words), and how ``estimated_samples_to_audit``
changed from the previous round's list. Nothing about CORLA's internals is
inferred: only what the files hold is shown, with their names.

**What CORLA's comparison shows** (`RoundResults.comparisons`): for each
round's comparison file (each later file includes the earlier rounds'
cards), per county, the cards audited (distinct imprinted IDs with an audit
board entry) and how many have a contest where the audit board's reading
differs from the CVR's. Choices are compared as sets: the same choices in
another order are no difference. For a county taking part in observation,
the differing rows themselves are listed: card, contest, CVR choice, audit
board's choice and whether the contest is targeted, a row already listed for
an earlier round's file not repeated. **Privacy:** a row is listed only for a
card that appears, with its votes, as a single ballot in the county's
redacted CVR that we hold and that passed the redaction check
(`redacted_card_ids`: a row the county redacted, aggregated or marked is
not such a card). A card that cannot be found there is not listed, since its
ballot was left out to protect voters' privacy; nor is any card when the
redacted CVR cannot be read. Only differing rows are listed, never a whole
ballot. CORLA writes choices as CSV-quoted lists (``"Yes/For"``,
``"John Hickenlooper","Ryan Apelbaum"``); `choices_text` renders them
("Yes/For"; "John Hickenlooper and Ryan Apelbaum (two choices)"; none is
"no vote").

Observers' entries are not compared here (see
`corla_observer.corla_compare`, for the coordinators). Standard library only.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

from corla_observer.classify import COLORADO_COUNTIES, Classification, canonical_county
from corla_observer.cvr import CvrFormatError, CvrReader
from corla_observer.datamap import TARGETED_REASONS, DataMap, MapEntry, column, role_column, round_of, table_rows
from corla_observer.drawn import ComparisonRow, read_comparison
from corla_observer.sampling import county_key
from corla_observer.workspace import ObjectNotFoundError, Workspace

STATEWIDE = "state_wide_contest"
IN_PROGRESS = "in_progress"
RISK_LIMIT_ACHIEVED = "risk_limit_achieved"

#: ``random_audit_status`` in words.
STATUS_WORDS = {
    RISK_LIMIT_ACHIEVED: "met the risk limit",
    IN_PROGRESS: "in progress",
    "ended": "ended",
    "not_auditable": "not auditable",
}

#: Counties in CORLA's audit, for a statewide contest the contest-to-county file does not list (San Juan counts by hand).
CORLA_COUNTIES = tuple(c for c in COLORADO_COUNTIES if c != "San Juan")

#: Shown with the contests that continue.
OVERSTATEMENT_NOTE = (
    "An overstatement is a card where the audit board's reading narrows the reported winner's margin compared with the CVR: "
    "each one means the audit needs more cards to reach the risk limit. An understatement widens the margin and does not "
    "increase the cards needed."
)

_RLA = "https://www.coloradosos.gov/pubs/elections/RLA/"
_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _short(ref: str) -> str:
    return "RLA/" + ref[len(_RLA) :] if ref.startswith(_RLA) else ref


def number_words(n: int) -> str:
    """A count in words up to ten ("one", "two"), else in figures."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{number_words(n)} {word if n == 1 else plural or word + 's'}"


# ---------------------------------------------------------------------------
# Choices


def parse_choices(cell: str) -> tuple[str, ...]:
    """The choices in one of CORLA's choice cells: ``"A","B"`` (or a JSON list) as ``("A", "B")``; empty as ``()``."""
    text = cell.strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, list):
            return tuple(str(v).strip() for v in value if str(v).strip())
    try:
        row = next(csv.reader(io.StringIO(text), skipinitialspace=True), [])
    except csv.Error:
        row = [text]
    return tuple(c.strip() for c in row if c.strip())


def choices_text(choices: Sequence[str]) -> str:
    """Choices in words: "no vote", "Yes/For", "A and B (two choices)", "A, B and C (three choices)"."""
    if not choices:
        return "no vote"
    if len(choices) == 1:
        return choices[0]
    return f"{', '.join(choices[:-1])} and {choices[-1]} ({number_words(len(choices))} choices)"


def choices_differ(cvr: Sequence[str], board: Sequence[str]) -> bool:
    """Whether the audit board's reading differs from the CVR's: compared as sets, so order does not count."""
    return set(cvr) != set(board)


# ---------------------------------------------------------------------------
# Contests and their counties

_PREFIX_RE = re.compile(r"^(.+?)\s+-\s+")
_SUFFIX_RE = re.compile(r"\(([^()]+)\)\s*$")
_COUNTIES_LONGEST_FIRST = sorted(COLORADO_COUNTIES, key=len, reverse=True)


def contest_counties(
    name: str, reason: str, listed: Mapping[str, Sequence[str]]
) -> tuple[tuple[str, ...], bool, str]:
    """A targeted contest's counties, whether it is statewide, and how they were found (see the module documentation)."""
    in_file = tuple(sorted({c for c in listed.get(name, ())}, key=county_key))
    if reason == STATEWIDE:
        if in_file:
            return in_file, True, f"statewide: the {len(in_file)} counties the contest-to-county file lists"
        return CORLA_COUNTIES, True, "statewide: every county in CORLA's audit (not in the contest-to-county file)"
    prefix = _PREFIX_RE.match(name)
    county = canonical_county(prefix.group(1)) if prefix else None
    if county:
        note = f"{county}, from the contest's name"
        if in_file and in_file != (county,):
            note += f" (the contest-to-county file lists {', '.join(in_file)})"
        return (county,), False, note
    suffix = _SUFFIX_RE.search(name)
    county = canonical_county(suffix.group(1)) if suffix else None
    if county:
        return (county,), False, f"{county}, from the contest's name"
    if in_file:
        if len(in_file) == 1:
            return in_file, False, f"{in_file[0]}, from the contest-to-county file"
        return in_file, False, f"the contest-to-county file lists {len(in_file)} counties for it: {', '.join(in_file)}"
    lowered = name.lower()
    for candidate in _COUNTIES_LONGEST_FIRST:
        if lowered.startswith(candidate.lower() + " county"):
            return (candidate,), False, f"{candidate}, from the contest's name"
    return (), False, "county not known: not in the contest-to-county file, and its name names none"


def contests_by_county_file(datamap: DataMap, workspace: Workspace) -> dict[str, list[str]]:
    """Each contest's counties from the fetched contest-to-county file (the latest), by CORLA's exact contest name."""
    files = [e for e in datamap.used("contest_ids_by_county") if _fetched(e, datamap)]
    if not files:
        return {}
    entry = max(files, key=lambda e: (e.seq or 0, e.ref))
    data = _bytes(workspace, entry.sha256)
    if data is None:
        return {}
    classification = _classification(entry)
    _, rows = table_rows(data, classification)
    contest_col = role_column(classification, "contest") or "contest_name"
    county_col = role_column(classification, "county") or "county_name"
    found: dict[str, set[str]] = {}
    for row in rows:
        county = canonical_county(row.get(county_col, ""))
        if county:
            found.setdefault(row.get(contest_col, "").strip(), set()).add(county)
    return {name: sorted(counties, key=county_key) for name, counties in found.items()}


# ---------------------------------------------------------------------------
# Contest lists


@dataclass(frozen=True)
class ContestStatus:
    """One targeted contest as one round's contest list shows it."""

    name: str
    reason: str
    status: str
    counties: tuple[str, ...]
    statewide: bool
    county_source: str
    audited: int | None = None
    two_over: int = 0
    one_over: int = 0
    zero: int = 0
    one_under: int = 0
    two_under: int = 0
    disagreement: int = 0
    optimistic: int | None = None
    estimated: int | None = None

    def covers(self, county: str) -> bool:
        return county in self.counties

    @property
    def in_progress(self) -> bool:
        return self.status == IN_PROGRESS

    @property
    def county_text(self) -> str:
        if self.statewide:
            return "statewide"
        return ", ".join(self.counties) or "county not known"

    @property
    def status_text(self) -> str:
        return STATUS_WORDS.get(self.status, self.status or "no status")


@dataclass(frozen=True)
class ContestList:
    """One round's contest list: its round, where it is, and its targeted contests."""

    round: int
    ref: str
    round_source: str
    contests: tuple[ContestStatus, ...]

    @property
    def unaudited(self) -> bool:
        """No targeted contest has a card audited yet: the list from before the round's audit."""
        return all(not c.audited for c in self.contests)

    def contest(self, name: str) -> ContestStatus | None:
        return next((c for c in self.contests if c.name == name), None)


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def read_contest_list(
    data: bytes, classification: Classification, *, ref: str, round_number: int | None, round_source: str,
    listed: Mapping[str, Sequence[str]],
) -> ContestList | None:
    """A contest list's targeted contests; None if its round is not known (see the module documentation)."""
    header, rows = table_rows(data, classification)
    name_col = role_column(classification, "contest") or "contest_name"
    reason_col = role_column(classification, "audit_reason") or "audit_reason"

    def col(name: str) -> str:
        return column(header, name) or name

    contests = []
    for row in rows:
        reason = row.get(reason_col, "").strip().lower()
        if reason not in TARGETED_REASONS:
            continue
        name = row.get(name_col, "").strip()
        counties, statewide, source = contest_counties(name, reason, listed)

        def n(key: str) -> int:
            return _int(row.get(col(key))) or 0

        contests.append(
            ContestStatus(
                name=name, reason=reason, status=row.get(col("random_audit_status"), "").strip().lower(), counties=counties,
                statewide=statewide, county_source=source, audited=_int(row.get(col("audited_sample_count"))),
                two_over=n("two_vote_over_count"), one_over=n("one_vote_over_count"), zero=n("zero_discrepancy_count"),
                one_under=n("one_vote_under_count"), two_under=n("two_vote_under_count"), disagreement=n("disagreement_count"),
                optimistic=_int(row.get(col("optimistic_samples_to_audit"))), estimated=_int(row.get(col("estimated_samples_to_audit"))),
            )
        )  # fmt: skip
    if round_number is None:
        if not contests or any(c.audited for c in contests):
            return None
        round_number, round_source = 1, "no card audited yet in it (audited_sample_count all zero)"
    return ContestList(round_number, ref, round_source, tuple(contests))


def contest_lists(datamap: DataMap, workspace: Workspace) -> tuple[ContestList, ...]:
    """The fetched contest lists of the data map's election, one per round (the latest fetched), in round order."""
    entries = sorted((e for e in datamap.used("contests_list") if _fetched(e, datamap)), key=lambda e: (e.seq or 0, e.ref))
    if not entries:
        return ()
    key = ("lists", tuple((e.ref, e.sha256) for e in entries), _listed_key(datamap))

    def build() -> tuple[ContestList, ...]:
        listed = contests_by_county_file(datamap, workspace)
        by_round: dict[int, ContestList] = {}
        for entry in entries:
            data = _bytes(workspace, entry.sha256)
            if data is None:
                continue
            number = round_of(entry.ref)
            found = read_contest_list(
                data, _classification(entry), ref=_short(entry.ref), round_number=number,
                round_source=f"its URL names round {number}" if number is not None else "", listed=listed,
            )  # fmt: skip
            if found is not None:
                by_round[found.round] = found  # entries in fetch order: the latest wins
        return tuple(by_round[r] for r in sorted(by_round))

    return _cached(key, build)


def _listed_key(datamap: DataMap) -> tuple[tuple[str, str], ...]:
    return tuple((e.ref, e.sha256) for e in datamap.used("contest_ids_by_county") if _fetched(e, datamap))


# ---------------------------------------------------------------------------
# CORLA's comparison


@dataclass(frozen=True)
class CardRow:
    """One contest on one audited card, from CORLA's comparison, with the choices parsed."""

    contest: str
    cvr: tuple[str, ...]
    board: tuple[str, ...]
    comment: str = ""

    @property
    def differs(self) -> bool:
        return choices_differ(self.cvr, self.board)


@dataclass
class ComparisonFile:
    """One comparison file read: per county, each audited card's rows (by imprinted ID)."""

    round: int | None
    ref: str
    counties: dict[str, dict[str, list[CardRow]]] = field(default_factory=dict)


def read_comparison_file(data: bytes, classification: Classification, *, ref: str, round_number: int | None) -> ComparisonFile:
    """A comparison file's audited rows by county and card; rows without an audit board entry left out."""
    found = ComparisonFile(round_number, ref)
    shared: dict[str, str] = {}

    def keep(text: str) -> str:
        return shared.setdefault(text, text)

    choices: dict[str, tuple[str, ...]] = {}

    def parsed(cell: str) -> tuple[str, ...]:
        if cell not in choices:
            choices[cell] = tuple(keep(c) for c in parse_choices(cell))
        return choices[cell]

    rows: list[ComparisonRow] = read_comparison(data, classification)
    for row in rows:
        if not row.audited or not row.imprinted_id:
            continue
        county = canonical_county(row.county) or row.county.strip()
        cards = found.counties.setdefault(keep(county), {})
        cards.setdefault(row.imprinted_id.strip(), []).append(
            CardRow(keep(row.contest.strip()), parsed(row.cvr_choice), parsed(row.audit_board), row.comment.strip())
        )
    return found


def comparison_files(datamap: DataMap, workspace: Workspace) -> tuple[ComparisonFile, ...]:
    """The fetched comparison files of the data map's election, one per round (the latest fetched), rounds in order, unnamed last."""
    entries = sorted((e for e in datamap.used("cvr_comparison") if _fetched(e, datamap)), key=lambda e: (e.seq or 0, e.ref))
    by_round: dict[int | None, MapEntry] = {}
    for entry in entries:
        by_round[round_of(entry.ref)] = entry
    found = []
    for number in sorted(by_round, key=lambda n: (n is None, n or 0)):
        entry = by_round[number]

        def build(entry: MapEntry = entry, number: int | None = number) -> ComparisonFile | None:
            data = _bytes(workspace, entry.sha256)
            if data is None:
                return None
            return read_comparison_file(data, _classification(entry), ref=_short(entry.ref), round_number=number)

        read = _cached(("comparison", entry.ref, entry.sha256), build)
        if read is not None:
            found.append(read)
    return tuple(found)


# ---------------------------------------------------------------------------
# Privacy: the cards a redacted CVR shows


def card_key(imprinted_id: str) -> str:
    """An imprinted ID compared without spaces or leading zeros in its parts (as `corla_observer.extract.id_key`)."""
    return "-".join(part.strip().lstrip("0") or "0" for part in imprinted_id.split("-"))


def redacted_card_ids(workspace: Workspace, sha256: str) -> frozenset[str] | None:
    """The cards a redacted CVR shows as single ballots with their votes (`card_key`), or None if it cannot be read.

    A row the county redacted or aggregated, or marked with a redaction marker anywhere, does not count.
    """

    def build() -> frozenset[str] | None:
        try:
            reader = CvrReader(workspace.path_for(sha256))
            return frozenset(
                card_key(row.imprinted_id)
                for row in reader.rows()
                if row.status == "ballot" and not row.marked and row.imprinted_id
            )
        except (CvrFormatError, csv.Error, OSError, UnicodeError, ValueError):
            return None

    return _cached(("cvr", sha256), build)


# ---------------------------------------------------------------------------
# The results


@dataclass(frozen=True)
class Standing:
    """Where the audit stood, by one round's contest list."""

    round: int
    ref: str
    text: str
    targeted: int
    met: int
    continuing: int
    other: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Continuing:
    """A targeted contest still in progress, and why, from the latest contest list (and the one before)."""

    name: str
    counties: str
    round: int
    ref: str
    audited_text: str
    discrepancies: list[str]
    estimate_text: str


@dataclass(frozen=True)
class DifferingRow:
    """A contest on an audited card where the audit board's reading differs from the CVR."""

    imprinted_id: str
    contest: str
    cvr_choice: str
    board_choice: str
    targeted: str
    comment: str = ""


@dataclass
class CountyCards:
    """One county's cards in one comparison file, and (for a county taking part in observation) its differing rows."""

    county: str
    cards: int
    differing_cards: int
    listed: bool = False
    rows: list[DifferingRow] = field(default_factory=list)
    #: Cards with a difference not listed: not shown as a ballot in the county's redacted CVR.
    withheld: int = 0
    note: str = ""


@dataclass
class RoundComparison:
    round: int | None
    ref: str
    cards: int
    differing_cards: int
    counties: list[CountyCards] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Round {self.round}" if self.round is not None else "Round not named"


@dataclass
class RoundResults:
    """Everything the dashboard's section on CORLA's results shows; see the module documentation."""

    standings: list[Standing] = field(default_factory=list)
    continuing: list[Continuing] = field(default_factory=list)
    comparisons: list[RoundComparison] = field(default_factory=list)
    overstatement_note: str = OVERSTATEMENT_NOTE

    @property
    def empty(self) -> bool:
        return not (self.standings or self.comparisons)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _names(contests: Sequence[ContestStatus]) -> str:
    return "; ".join(f"{c.name} ({c.county_text})" for c in contests)


def standing(listing: ContestList) -> Standing:
    """Where the audit stood by one round's contest list, in a sentence."""
    contests = listing.contests
    total = len(contests)
    counts = Counter(c.status for c in contests)
    met, going = counts.get(RISK_LIMIT_ACHIEVED, 0), counts.get(IN_PROGRESS, 0)
    other = {STATUS_WORDS.get(s, s or "no status"): n for s, n in sorted(counts.items()) if s not in (RISK_LIMIT_ACHIEVED, IN_PROGRESS)}
    r = listing.round
    if listing.unaudited:
        statewide = sum(1 for c in contests if c.statewide)
        text = (
            f"Round {r} ({listing.ref}): {total} targeted contests to audit ({statewide} statewide, {total - statewide} county-wide), "
            "no card audited yet."
        )
    else:
        when = f"After round {r - 1}" if r > 1 else f"In round {r}"
        parts = [f"{met} of {total} targeted contests met the risk limit"]
        if going:
            verb = "continues" if going == 1 else "continue"
            parts.append(f"{going} {verb} in round {r}: {_names([c for c in contests if c.in_progress])}" if r > 1 else f"{going} still in progress")
        parts += [f"{n} {words}" for words, n in other.items()]
        text = f"{when} ({listing.ref}): " + "; ".join(parts) + "."
    return Standing(r, listing.ref, text, total, met, going, other)


def discrepancy_lines(contest: ContestStatus) -> list[str]:
    """The discrepancies a contest list counts for a contest, in words."""
    lines = []
    for n, change, kind in (
        (contest.two_over, "took two votes from", "two-vote overstatement"),
        (contest.one_over, "took one vote from", "one-vote overstatement"),
        (contest.one_under, "added one vote to", "one-vote understatement"),
        (contest.two_under, "added two votes to", "two-vote understatement"),
    ):
        if n:
            where = "card where" if n == 1 else "cards where"
            named = f"a {kind}" if n == 1 else f"{kind}s"
            lines.append(f"{number_words(n)} {where} the audit board's reading {change} the reported winner's margin ({named})")
    if contest.disagreement:
        lines.append(f"disagreement_count {contest.disagreement}, as the contest list gives it")
    return lines or ["no overstatement or understatement counted"]


def continuing(listing: ContestList, previous: ContestList | None) -> list[Continuing]:
    """Each targeted contest in progress in ``listing``, with its discrepancies and the change in CORLA's estimate."""
    if listing.unaudited:
        return []
    found = []
    for contest in listing.contests:
        if not contest.in_progress:
            continue
        audited = contest.audited or 0
        audited_text = f"{_plural(audited, 'card')} audited so far (audited_sample_count in {listing.ref})"
        before = previous.contest(contest.name) if previous is not None else None
        now = contest.estimated
        if before is not None and before.estimated is not None and now is not None:
            assert previous is not None
            was = f"in round {previous.round}'s contest list ({previous.ref})"
            is_ = f"in round {listing.round}'s ({listing.ref})"
            if now == before.estimated:
                how = f"stayed at {now}, {was} and {is_}"
            else:
                how = f"{'rose' if now > before.estimated else 'fell'} from {before.estimated}, {was}, to {now}, {is_}"
            estimate = f"CORLA's estimate of the cards it needs (estimated_samples_to_audit) {how}."
        elif now is not None:
            estimate = (
                f"CORLA's estimate of the cards it needs (estimated_samples_to_audit) is {now} in round {listing.round}'s contest list "
                f"({listing.ref}); the previous round's list has not been fetched."
            )
        else:
            estimate = f"Round {listing.round}'s contest list ({listing.ref}) gives no estimated_samples_to_audit."
        found.append(
            Continuing(contest.name, contest.county_text, listing.round, listing.ref, audited_text, discrepancy_lines(contest), estimate)
        )
    return found


def _targeted_text(contest: str, county: str, lists: Sequence[ContestList]) -> str:
    if not lists:
        return ""
    for listing in reversed(lists):
        found = listing.contest(contest)
        if found is not None and found.covers(county):
            return "targeted (statewide)" if found.statewide else "targeted"
    return "not targeted"


def compare_counties(
    files: Sequence[ComparisonFile],
    lists: Sequence[ContestList],
    shown_cards: Mapping[str, frozenset[str] | None],
) -> list[RoundComparison]:
    """Each comparison file's counts per county, with the differing rows of the counties in ``shown_cards``.

    ``shown_cards`` maps each county taking part in observation to the cards its redacted CVR shows
    (`redacted_card_ids`), or None if no redacted CVR that passed the check can be read.
    """
    out = []
    listed_before: set[tuple[str, str, str]] = set()
    for f in files:
        result = RoundComparison(f.round, f.ref, 0, 0)
        for county in sorted(f.counties, key=county_key):
            cards = f.counties[county]
            differing = {card: [r for r in rows if r.differs] for card, rows in cards.items()}
            differing = {card: rows for card, rows in differing.items() if rows}
            entry = CountyCards(county, len(cards), len(differing))
            result.cards += entry.cards
            result.differing_cards += entry.differing_cards
            if county in shown_cards:
                entry.listed = True
                visible = shown_cards[county]
                if visible is None and differing:
                    entry.withheld = len(differing)
                    entry.note = "no card is listed: no redacted CVR of the county that passed the redaction check is held here, or it could not be read"
                for card in sorted(differing, key=_card_order):
                    if visible is None:
                        break
                    if card_key(card) not in visible:
                        entry.withheld += 1
                        continue
                    for row in differing[card]:
                        key = (county, card, row.contest)
                        if key in listed_before:
                            continue
                        listed_before.add(key)
                        entry.rows.append(
                            DifferingRow(card, row.contest, choices_text(row.cvr), choices_text(row.board), _targeted_text(row.contest, county, lists), row.comment)
                        )
                if differing and not entry.rows and not entry.withheld:
                    entry.note = "listed for an earlier round's file"
                if entry.withheld and not entry.note:
                    entry.note = (
                        f"{_plural(entry.withheld, 'card')} with a difference {'is' if entry.withheld == 1 else 'are'} not listed: "
                        "not in the county's redacted CVR, which leaves out ballots to protect voters' privacy"
                    )
            result.counties.append(entry)
        out.append(result)
    return out


def _card_order(card: str) -> tuple[Any, ...]:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in card.split("-"))


def round_results(datamap: DataMap, workspace: Workspace, redacted_cvrs: Mapping[str, str | None]) -> RoundResults:
    """The dashboard's section on CORLA's results (see the module documentation).

    ``redacted_cvrs`` maps each county taking part in observation to the SHA-256 of its redacted CVR
    that passed the redaction check, or None if it has none.
    """
    used = tuple((e.kind, e.ref, e.sha256) for e in datamap.entries if e.kind in _RESULT_KINDS and not e.ignored and _fetched(e, datamap))
    key = ("results", used, tuple(sorted(redacted_cvrs.items())))
    return _cached(key, lambda: _round_results(datamap, workspace, redacted_cvrs))


#: The kinds of file the results are read from: the same files give the same results, so they are kept by these files' hashes.
_RESULT_KINDS = frozenset({"contests_list", "cvr_comparison", "contest_ids_by_county"})


def _round_results(datamap: DataMap, workspace: Workspace, redacted_cvrs: Mapping[str, str | None]) -> RoundResults:
    lists = contest_lists(datamap, workspace)
    results = RoundResults()
    results.standings = [standing(listing) for listing in lists]
    if lists:
        latest = lists[-1]
        previous = next((x for x in reversed(lists[:-1]) if x.round == latest.round - 1), None)
        results.continuing = continuing(latest, previous)
    files = comparison_files(datamap, workspace)
    shown: dict[str, frozenset[str] | None] = {}
    for county, sha in redacted_cvrs.items():
        shown[county] = redacted_card_ids(workspace, sha) if sha else None
    results.comparisons = compare_counties(files, lists, shown)
    return results


# ---------------------------------------------------------------------------
# Reading and caching


def _fetched(entry: MapEntry, datamap: DataMap) -> bool:
    same = datamap.election is None or entry.election is None or entry.election == datamap.election
    return entry.source == "fetch" and same and bool(entry.sha256)


def _classification(entry: MapEntry) -> Classification:
    return Classification(entry.classified_kind, entry.classified_county, entry.evidence)


def _bytes(workspace: Workspace, sha256: str) -> bytes | None:
    try:
        return workspace.get_bytes(sha256)
    except ObjectNotFoundError:
        return None


T = TypeVar("T")
_CACHE: dict[Any, Any] = {}
_LOCK = threading.Lock()
#: Kept per kind of thing read: a comparison file can be tens of megabytes, a redacted CVR hundreds.
_CACHE_SIZE = 16


def _cached(key: Any, build: Callable[[], T]) -> T:
    """``build()``, kept for the last few keys asked about (each key names the content it reads by SHA-256)."""
    with _LOCK:
        if key in _CACHE:
            kept: T = _CACHE[key]
            return kept
    value = build()
    with _LOCK:
        _CACHE[key] = value
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
    return value


def clear_cache() -> None:
    """Forget everything read (for tests)."""
    with _LOCK:
        _CACHE.clear()

