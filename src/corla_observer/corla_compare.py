"""Observers' findings against CORLA's results, card by card: for the coordinators only, never the public dashboard.

For each audit board of the RLA sample and each observer on it, every card is
put in one category (`CATEGORIES`), comparing what the observer recorded with
CORLA's CVR-to-audit-board comparison for that card (``contestComparison.csv``,
read by `corla_observer.round_results.comparison_files`; the latest round's
file, which includes the earlier rounds' cards):

- **agree**: the observer recorded the card as matching the CVR, and CORLA's
  audit board reading matches the CVR in every contest;
- **agree: both read an overvote where the CVR has no vote**: the observer's
  only entries say overvote (a contest-by-contest entry heard with "overvote"
  or more choices than the contest allows, on a contest where the audit board
  read an overvote and the CVR has no vote; or a two-tap note containing
  "overvote", on a card with such a contest), and CORLA's reading matches the
  CVR in every contest;
- **CORLA only**: CORLA's reading differs from the CVR in some contest on a
  card the observer recorded as matching;
- **possible difference noted by the observer**: the observer entered
  something other than a match (a note or a different reading) on a card
  where CORLA's reading matches the CVR; notes are not interpreted, so a
  note may in fact agree with the CVR;
- **CORLA differs; the observer noted a possible difference**: both have
  something other than a match (the contests and both
  readings are shown; they may or may not be the same contest);
- **not observed**: a card of the board in CORLA's comparison that the
  observer has no entry for;
- **not in CORLA's comparison**: a card the observer entered that the
  comparison does not have (not yet audited in a posted file);
- **entry unfinished**: a card the observer began contest by contest and
  neither finished nor recorded a difference on;
- **not in the redacted CVR**: a card the county's redacted CVR (the one we
  hold that passed the redaction check, `corla_observer.round_results.redacted_card_ids`)
  does not show as a single ballot, so there is nothing to compare against.
  Neither side's votes, nor the observer's note, are ever shown for it.

The observer's side is `corla_observer.contests.compare_board`, which reads
both entry modes (two taps, contest by contest) from the event log, on the
RLA sample's entries only (`corla_observer.pulllists.events_for`). A board's
cards are those its latest RLA extract assigns to it; without an extract,
every card of the county in CORLA's comparison counts as the board's, which
the report says. CORLA's side shows, for each card that is not an agreement,
the contests where the board's reading differs from the CVR, and for a
difference only the observer recorded, CORLA's rows for the contests the
observer named (contest by contest) or, for a two-tap note, the contests
whose distinctive words the note uses (said as such: the note may refer to
them). Choices are compared and rendered as in `corla_observer.round_results`,
with CORLA's rule for overvotes (`corla_observer.round_results.CardRow.kind`):
an overvote the audit board read counts as no vote, so where the CVR has no
vote it is no difference, and where the CVR has a choice it is one. An
observer's overvote entry that agrees with such a reading is not a possible
difference: it agrees (the CVR shows a voter's overvote as no vote).

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from corla_observer.contests import BallotComparison, compare_board
from corla_observer.datamap import DataMap, contest_key
from corla_observer.events import Event
from corla_observer.pulllists import RLA, events_for, extract_lists, find_list, load
from corla_observer.round_results import (
    BOARD_OVERVOTE,
    BOARD_OVERVOTE_NOTE,
    RANKED,
    RANKED_NOTE,
    VOTER_OVERVOTE,
    CardRow,
    ComparisonFile,
    card_key,
    choices_text,
    comparison_files,
    redacted_card_ids,
)
from corla_observer.sampling import county_key
from corla_observer.workspace import ObjectNotFoundError, Workspace

AGREE = "agree"
AGREE_OVERVOTE = "agree_overvote"
CORLA_ONLY = "corla_only"
OBSERVER_ONLY = "observer_only"
BOTH_DIFFER = "both_differ"
NOT_OBSERVED = "not_observed"
NOT_IN_CORLA = "not_in_corla"
UNFINISHED = "unfinished"
NOT_IN_CVR = "not_in_cvr"

#: Each category and how it is named, in the order shown.
CATEGORIES = {
    AGREE: "agree",
    AGREE_OVERVOTE: "agree: both read an overvote where the CVR has no vote",
    CORLA_ONLY: "CORLA only",
    OBSERVER_ONLY: "possible difference noted by the observer",
    BOTH_DIFFER: "CORLA differs; the observer noted a possible difference",
    NOT_OBSERVED: "not observed",
    NOT_IN_CORLA: "not in CORLA's comparison",
    UNFINISHED: "entry unfinished",
    NOT_IN_CVR: "not in the redacted CVR",
}

#: Words too common in contest names to tie a two-tap note to a contest.
_COMMON_WORDS = frozenset(
    "county court judge district ballot issue question measure proposition amendment state colorado united states city town "
    "school board commissioner council member seat at-large with blank vote votes none over under yes no for against".split()
)


@dataclass(frozen=True)
class CorlaRow:
    """One contest of a card in CORLA's comparison, rendered."""

    contest: str
    cvr_choice: str
    board_choice: str
    differs: bool
    #: How CORLA's rule reads the row when its choices alone don't say: an overvote, a ranked-choice contest; else "".
    note: str = ""


@dataclass
class CardFinding:
    """One card as one observer and CORLA recorded it. ``corla`` and ``observer`` are empty for a card not in the redacted CVR."""

    imprinted_id: str
    category: str
    position: int | None = None
    corla: list[CorlaRow] = field(default_factory=list)
    #: Whether ``corla`` holds rows matched by a two-tap note's words, not rows that differ.
    corla_guessed: bool = False
    observer: list[str] = field(default_factory=list)

    @property
    def category_text(self) -> str:
        return CATEGORIES[self.category]


@dataclass
class ObserverComparison:
    """One observer on one board: counts per category, and each card that is not an agreement."""

    actor: str
    county: str
    board: str
    counts: dict[str, int] = field(default_factory=dict)
    cards: list[CardFinding] = field(default_factory=list)

    @property
    def counts_text(self) -> str:
        return ", ".join(f"{n} {CATEGORIES[c]}" for c, n in self.counts.items() if n) or "no cards"


@dataclass
class BoardComparison:
    county: str
    board: str
    observers: list[ObserverComparison] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class CorlaComparisonReport:
    """Every RLA board's observers against CORLA's comparison file ``ref`` (None if none is fetched)."""

    ref: str | None = None
    boards: list[BoardComparison] = field(default_factory=list)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", text.lower()) if len(w) >= 4 and w not in _COMMON_WORDS}


#: What `CorlaRow.note` says, by `CardRow.kind`.
_ROW_NOTES = {
    BOARD_OVERVOTE: BOARD_OVERVOTE_NOTE,
    VOTER_OVERVOTE: "an overvote where the CVR has no vote: CORLA counts no discrepancy",
    RANKED: RANKED_NOTE,
}


def _render(rows: Iterable[CardRow]) -> list[CorlaRow]:
    return [CorlaRow(r.contest, choices_text(r.cvr), choices_text(r.board), r.differs, _ROW_NOTES.get(r.kind, "")) for r in rows]


def says_overvote(heard: str, vote_for: int) -> bool:
    """Whether an observer's entry says overvote: it contains "overvote", or names more choices than ``vote_for`` (joined by "+", "," or ";")."""
    text = " ".join(heard.split()).casefold()
    if "overvote" in text:
        return True
    parts = [p for p in re.split(r"\s*[+,;]\s*", text) if p]
    return len(parts) > max(vote_for, 1)


def _overvotes_agree(comparison: BallotComparison, actor: str, rows: Sequence[CardRow]) -> bool:
    """Whether every entry the observer made on the card says overvote where the audit board read one and the CVR has no vote."""
    voter = {contest_key(r.contest): r for r in rows if r.voter_overvote}
    if not voter:
        return False
    findings = [f for f in comparison.contests if f.observer == actor]
    notes = [note for who, note in comparison.ballot_notes if who == actor]
    if not findings and not notes:
        return False
    for f in findings:
        row = voter.get(contest_key(f.contest))
        if row is None or not says_overvote(f.heard, row.vote_for):
            return False
    return all("overvote" in note.casefold() for note in notes)


def _observer_side(comparison: BallotComparison, actor: str) -> tuple[str, list[str], list[str]]:
    """The observer's state on a card (``match``, ``differs``, ``unfinished`` or ``none``), the contests they named, and their entries in words."""
    findings = [f for f in comparison.contests if f.observer == actor]
    notes = [note for who, note in comparison.ballot_notes if who == actor]
    lines = [f"{f.contest}: {f.text} (CVR {f.expected or 'no vote'})" for f in findings] + [f"note: {n}" for n in notes]
    if findings or notes:
        return "differs", [f.contest for f in findings], lines
    if actor in comparison.matched_by:
        return "match", [], []
    if any(who == actor for who, _ in comparison.unresolved_by):
        return "unfinished", [], []
    return "none", [], []


def _named_rows(rows: Sequence[CardRow], contests: Sequence[str], lines: Sequence[str]) -> tuple[list[CorlaRow], bool]:
    """CORLA's rows for the contests the observer named, else for the contests a note's distinctive words point to."""
    if contests:
        wanted = {contest_key(c) for c in contests}
        return _render(r for r in rows if contest_key(r.contest) in wanted), False
    words = _words(" ".join(lines))
    return _render(r for r in rows if words & _words(r.contest)), True


def _board_cards(workspace: Workspace, events: Sequence[Event], county: str, board: str) -> list[str] | None:
    """The imprinted IDs the county's latest RLA extract assigns to the board, or None without one."""
    item = find_list(events, county, RLA)
    if item is None:
        return None
    try:
        extract = load(workspace, item)
    except (ObjectNotFoundError, ValueError):
        return None
    return [str(b["imprinted_id"]) for b in extract.get("ballots", []) if str(b.get("board")) == str(board) and b.get("imprinted_id")]


def compare_observer(
    actor: str,
    county: str,
    board: str,
    compared: Sequence[BallotComparison],
    corla_cards: Mapping[str, Sequence[CardRow]],
    board_cards: Sequence[str],
    visible: frozenset[str] | None,
) -> ObserverComparison:
    """One observer's cards against CORLA's (see the module documentation). ``visible``: the cards the redacted CVR shows, or None."""
    result = ObserverComparison(actor, county, board, {c: 0 for c in CATEGORIES})
    corla = {card_key(card): (card, rows) for card, rows in corla_cards.items()}
    entered = {card_key(c.imprinted_id): c for c in compared if c.imprinted_id}
    mine = {k: c for k, c in entered.items() if _observer_side(c, actor)[0] != "none"}
    keys = list(dict.fromkeys([*(card_key(c) for c in board_cards if card_key(c) in corla), *mine]))
    for key in keys:
        comparison = mine.get(key)
        card = comparison.imprinted_id if comparison else corla[key][0]
        finding = CardFinding(card, AGREE, comparison.position if comparison else None)
        if visible is None or key not in visible:
            finding.category = NOT_IN_CVR
        elif comparison is None:
            finding.category = NOT_OBSERVED
        elif key not in corla:
            finding.category = NOT_IN_CORLA
        else:
            state, contests, lines = _observer_side(comparison, actor)
            rows = corla[key][1]
            corla_differs = [r for r in rows if r.differs]
            finding.observer = lines
            if state == "unfinished":
                finding.category = UNFINISHED
            elif state == "match":
                finding.category = CORLA_ONLY if corla_differs else AGREE
                finding.corla = _render(corla_differs)
            elif _overvotes_agree(comparison, actor, rows):
                # The observer's only entries are the overvote the audit board read where the CVR has no vote.
                finding.category = CORLA_ONLY if corla_differs else AGREE_OVERVOTE
                finding.corla = _render(corla_differs or [r for r in rows if r.voter_overvote])
            elif corla_differs:
                finding.category = BOTH_DIFFER
                finding.corla = _render(corla_differs)
            else:
                finding.category = OBSERVER_ONLY
                finding.corla, finding.corla_guessed = _named_rows(rows, contests, lines)
        result.counts[finding.category] += 1
        if finding.category != AGREE:
            result.cards.append(finding)
    order = list(CATEGORIES)
    result.cards.sort(key=lambda f: (order.index(f.category), f.position if f.position is not None else 1 << 30, f.imprinted_id))
    return result


def _latest(files: Sequence[ComparisonFile]) -> ComparisonFile | None:
    named = [f for f in files if f.round is not None]
    return max(named, key=lambda f: f.round or 0) if named else (files[-1] if files else None)


def corla_comparison(
    datamap: DataMap,
    workspace: Workspace,
    events: Sequence[Event],
    boards: Iterable[tuple[str, str, Sequence[str]]],
    redacted_cvrs: Mapping[str, str | None],
) -> CorlaComparisonReport:
    """Each RLA board's observers (``boards``: county, board, observers) against CORLA's latest comparison file.

    ``redacted_cvrs`` maps a county to the SHA-256 of its redacted CVR that passed the redaction check, or None.
    """
    latest = _latest(comparison_files(datamap, workspace))
    report = CorlaComparisonReport(latest.ref if latest else None)
    rla_events = events_for(events, RLA, extract_lists(events))
    visible_by_county: dict[str, frozenset[str] | None] = {}
    for county, board, observers in sorted(boards, key=lambda b: (county_key(b[0]), b[1])):
        result = BoardComparison(county, board)
        report.boards.append(result)
        if latest is None:
            result.notes.append("CORLA's comparison has not been fetched")
            continue
        if county not in visible_by_county:
            sha = redacted_cvrs.get(county)
            visible_by_county[county] = redacted_card_ids(workspace, sha) if sha else None
        visible = visible_by_county[county]
        if visible is None:
            result.notes.append("no redacted CVR of the county that passed the redaction check could be read: every card counts as not in the redacted CVR")
        corla_cards = latest.counties.get(county, {})
        if not corla_cards:
            result.notes.append(f"CORLA's comparison ({latest.ref}) has no card of the county")
        cards = _board_cards(workspace, events, county, board)
        if cards is None:
            cards = list(corla_cards)
            result.notes.append("no RLA extract for the county: every card of the county in CORLA's comparison counts as this board's")
        compared = compare_board(rla_events, county, board)
        for actor in sorted(observers):
            result.observers.append(compare_observer(actor, county, board, compared, corla_cards, cards, visible))
    return report
