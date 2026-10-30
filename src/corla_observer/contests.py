"""Contest-by-contest observation: what an observer recorded for each contest of each ballot, and comparing observers.

The observer page has two entry modes (docs/PLAN.md, component 9), which the
observer chooses on their link's page:

- **contest by contest** (``per_contest``, the default on a device that has
  not chosen): the ballot's contests as rows, each confirmed as expected, or
  marked with what was heard instead, or as not heard clearly, in each of the
  two readings;
- **two taps per ballot** (``two_tap``): "First reading", then "Confirmed
  match", or a note of what was heard instead (`corla_observer.practice`).

Each mode change, and the mode in effect each time the observer starts from
the notice before the first ballot, is an ``observation.entry_mode`` event
(records made before the page recorded the mode at the start, when two taps
was the default, may have none: an observer with no mode recorded and no
entries to show it is taken to have used two taps); each contest's
outcome in a reading is an ``observation.contest`` event (``contest``, its
``row`` of the ballot's ``rows``, the ``expected`` choice shown, the
``reading``, 1 or 2, the ``outcome``, ``confirmed``, ``heard_instead`` or
``unclear``, what was ``heard`` and whether it is a ``correction`` of an
earlier outcome for that row and reading); and a ballot the observer marks
done is an ``observation.ballot_done`` event. Every one is signed on the
device (docs/RECEIPTS.md). Nothing is overwritten: a later outcome for the
same row and reading replaces an earlier one only in the summaries.

`plain_fields` gives the plain event fields a signed payload determines, for
recording (`corla_observer.signed`) and checking
(`corla_observer.observer_record`). `fold` reads the log into each observer's
`BallotRows` per ballot, which say whether a ballot is done (marked done, or
every row has an outcome in the second reading), which rows are unresolved
(no outcome in the second reading, not heard clearly, or the two readings
differ) and what differs from the expected choices. `observer_modes` says
which modes each observer used. `compare_board` compares every observer's
record of each ballot of a board, treating contest-by-contest entries as
more precise than a two-tap match or note: they name the contest.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from corla_observer.events import Event, JSONValue

MODE_KIND = "observation.entry_mode"
CONTEST_KIND = "observation.contest"
DONE_KIND = "observation.ballot_done"
#: The same two outcomes for a ballot picked from a storage box and looked up by imprinted ID (docs/TARGETING.md),
#: recorded apart from every pull list (`corla_observer.pulllists.LOOKUP`).
LOOKUP_CONTEST_KIND = "observation.lookup_contest"
LOOKUP_DONE_KIND = "observation.lookup_done"
CONTEST_LIKE = frozenset({CONTEST_KIND, LOOKUP_CONTEST_KIND})
DONE_LIKE = frozenset({DONE_KIND, LOOKUP_DONE_KIND})
#: Event kinds whose payload type is the second part of the kind.
#: An observer's acknowledgement of the notice shown before their page (not contest-by-contest, but recorded the same way).
ACK_KIND = "observation.acknowledged"
#: An observer's switch to another of the county's pull lists (`corla_observer.pulllists`), recorded the same way.
SWITCH_KIND = "observation.list_switched"
PAYLOAD_KINDS = {
    "contest": CONTEST_KIND, "ballot_done": DONE_KIND, "entry_mode": MODE_KIND, "acknowledged": ACK_KIND, "list_switch": SWITCH_KIND,
    "lookup_contest": LOOKUP_CONTEST_KIND, "lookup_done": LOOKUP_DONE_KIND,
}  # fmt: skip
MODE_LABELS = {"two_tap": "two taps", "per_contest": "contest by contest"}

Key = tuple[str, str, str]


def plain_fields(payload: Mapping[str, Any]) -> dict[str, JSONValue]:
    """The plain ``data`` fields of the event recording a ``contest``, ``ballot_done`` or ``entry_mode`` payload.

    Besides these, the event has the payload's ``position``,
    ``imprinted_id`` and ``device_seq``.
    """
    content = payload.get("content") or {}
    kind = payload.get("type")
    names: tuple[str, ...]
    if kind in ("contest", "lookup_contest"):
        names = ("contest", "row", "rows", "expected", "reading", "outcome", "heard", "correction", "elapsed_ms")
    elif kind in ("ballot_done", "lookup_done"):
        names = ("reading", "rows", "unresolved", "elapsed_ms")
    elif kind == "entry_mode":
        names = ("mode",)
    elif kind == "acknowledged":
        names = ("notice",)
    elif kind == "list_switch":
        names = ("list", "name")
    else:
        raise ValueError(f"not a contest-by-contest payload: {kind!r}")
    return {name: content.get(name) for name in names}


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


@dataclass(frozen=True)
class Mark:
    """One contest's latest outcome in one reading."""

    outcome: str
    heard: str
    seq: int

    def same_as(self, other: Mark) -> bool:
        return self.outcome == other.outcome and (self.outcome != "heard_instead" or _same(self.heard, other.heard))

    def text(self) -> str:
        return {"confirmed": "confirmed", "unclear": "not heard clearly"}.get(self.outcome, f"heard “{self.heard}”")


@dataclass
class ContestRow:
    row: int
    contest: str
    expected: str
    readings: dict[int, Mark] = field(default_factory=dict)
    marks: int = 0
    corrections: int = 0

    @property
    def final(self) -> Mark | None:
        """The second reading's outcome, else the first's."""
        return self.readings.get(2) or self.readings.get(1)

    @property
    def differs(self) -> bool:
        """Whether the two readings were heard differently (not heard clearly in one of them is not a difference)."""
        first, second = self.readings.get(1), self.readings.get(2)
        if first is None or second is None or "unclear" in (first.outcome, second.outcome):
            return False
        return not first.same_as(second)

    @property
    def unresolved(self) -> bool:
        """No outcome in the second reading, not heard clearly, or readings that differ.

        A row confirmed in the second reading only (missed in the first) is resolved.
        """
        final = self.final
        return 2 not in self.readings or final is None or final.outcome == "unclear" or self.differs


@dataclass
class BallotRows:
    """One observer's contest-by-contest record of one ballot."""

    position: int
    imprinted_id: str
    rows_total: int = 0
    rows: dict[int, ContestRow] = field(default_factory=dict)
    marked_done: bool = False
    last_seq: int = 0

    @property
    def complete(self) -> bool:
        """Every row has an outcome in the second reading."""
        return self.rows_total > 0 and all(r in self.rows and 2 in self.rows[r].readings for r in range(1, self.rows_total + 1))

    @property
    def done(self) -> bool:
        return self.marked_done or self.complete

    def unresolved(self) -> list[int]:
        """The rows not resolved (`ContestRow.unresolved`), including rows with no outcome at all."""
        return [r for r in range(1, self.rows_total + 1) if r not in self.rows or self.rows[r].unresolved]

    def differences(self) -> list[ContestRow]:
        """Rows whose final outcome is not a confirmation, or whose readings differ."""
        return [
            r for _, r in sorted(self.rows.items())
            if r.differs or (r.final is not None and r.final.outcome != "confirmed")
        ]  # fmt: skip

    @property
    def match(self) -> bool:
        """Done, and every row confirmed in its final reading, with no readings that differ."""
        if not self.done or len(self.rows) < self.rows_total:
            return False
        return not self.differences()

    def note(self) -> str:
        """What differs from the expected choices, in a line, as a two-tap note would say it."""
        parts = []
        for r in self.differences():
            final = r.final
            assert final is not None
            if r.differs:
                first, second = r.readings[1], r.readings[2]
                parts.append(f"{r.contest}: first reading {first.text()}, second {second.text()}")
            else:
                parts.append(f"{r.contest}: {final.text()}")
        missing = sum(1 for n in range(1, self.rows_total + 1) if n not in self.rows)
        if missing and self.done:
            parts.append(f"{missing} contest{'s' if missing != 1 else ''} not entered")
        return "; ".join(parts)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def fold(events: Iterable[Event]) -> dict[Key, dict[int, BallotRows]]:
    """Each observer's (actor, county, board) contest-by-contest record, by ballot position."""
    found: dict[Key, dict[int, BallotRows]] = {}
    for event in events:
        if event.kind not in CONTEST_LIKE and event.kind not in DONE_LIKE or not event.county or not event.board:
            continue
        data = event.data
        position = _int(data.get("position"))
        if position is None:
            continue
        ballots = found.setdefault((event.actor, event.county, event.board), {})
        ballot = ballots.get(position)
        if ballot is None:
            ballot = ballots[position] = BallotRows(position, str(data.get("imprinted_id", "")))
        ballot.last_seq = event.seq
        rows = _int(data.get("rows"))
        if rows is not None:
            ballot.rows_total = max(ballot.rows_total, rows)
        if event.kind in DONE_LIKE:
            ballot.marked_done = True
            continue
        row, reading = _int(data.get("row")), _int(data.get("reading"))
        outcome = data.get("outcome")
        if row is None or reading not in (1, 2) or not isinstance(outcome, str):
            continue
        assert reading is not None
        contest_row = ballot.rows.get(row)
        if contest_row is None:
            contest_row = ballot.rows[row] = ContestRow(row, str(data.get("contest", "")), str(data.get("expected", "")))
        contest_row.readings[reading] = Mark(outcome, str(data.get("heard") or ""), event.seq)
        contest_row.marks += 1
        if data.get("correction"):
            contest_row.corrections += 1
    return found


def observer_modes(events: Iterable[Event]) -> dict[Key, list[str]]:
    """The entry modes each observer (actor, county, board) used, in the order first used.

    From their mode choices (``observation.entry_mode``) and the entries they
    made: a two-tap entry or first reading is ``two_tap``, a contest entry or
    a ballot marked done is ``per_contest``.
    """
    used: dict[Key, list[str]] = {}
    for event in events:
        if not event.county or not event.board:
            continue
        if event.kind == MODE_KIND:
            mode = event.data.get("mode")
        elif event.kind in CONTEST_LIKE or event.kind in DONE_LIKE:
            mode = "per_contest"
        elif event.kind in ("observation.entry", "observation.first_reading"):
            mode = "two_tap"
        else:
            continue
        if isinstance(mode, str):
            modes = used.setdefault((event.actor, event.county, event.board), [])
            if mode not in modes:
                modes.append(mode)
    return used


def latest_mode(events: Iterable[Event], actor: str, county: str, board: str) -> str | None:
    """The observer's latest mode choice for the board, if they made one."""
    mode = None
    for event in events:
        if event.kind == MODE_KIND and (event.actor, event.county, event.board) == (actor, county, board):
            value = event.data.get("mode")
            mode = value if isinstance(value, str) else mode
    return mode


def page_state(ballots: Mapping[int, BallotRows]) -> dict[str, Any]:
    """An observer's contest-by-contest record as the observer page restores it (``data.json``'s ``contests``)."""
    return {
        str(position): {
            "rows": {
                str(n): {str(reading): {"outcome": m.outcome, "heard": m.heard, "seq": m.seq} for reading, m in r.readings.items()}
                for n, r in b.rows.items()
            },
            "done": b.marked_done,
        }
        for position, b in ballots.items()
    }


# ---------------------------------------------------------------------------
# Comparing observers


@dataclass(frozen=True)
class ContestFinding:
    """One observer's outcome for one contest that is not a plain confirmation."""

    observer: str
    contest: str
    expected: str
    text: str


@dataclass
class BallotComparison:
    """Every observer's record of one ballot of a board.

    ``precision`` is ``contest`` when some observer entered the ballot
    contest by contest (their findings name the contests), ``ballot`` when
    only two-tap entries cover it, and ``none`` when nobody entered it.
    ``verdict`` is ``match`` (everyone who entered it found it matching),
    ``difference`` (someone recorded a difference), or ``conflict`` (a
    two-tap note where every contest-by-contest observer confirmed every
    contest, or a two-tap match where one found a difference: worth a
    second look).
    """

    position: int
    imprinted_id: str
    precision: str
    verdict: str
    contests: list[ContestFinding] = field(default_factory=list)
    ballot_notes: list[tuple[str, str]] = field(default_factory=list)
    matched_by: list[str] = field(default_factory=list)
    unresolved_by: list[tuple[str, int]] = field(default_factory=list)


def compare_board(events: Iterable[Event], county: str, board: str) -> list[BallotComparison]:
    """Every entered ballot of a board, with each observer's findings, in position order."""
    events = list(events)
    two_tap: dict[int, dict[str, Mapping[str, JSONValue]]] = {}
    ids: dict[int, str] = {}
    for event in events:
        if event.kind != "observation.entry" or (event.county, event.board) != (county, board):
            continue
        position = _int(event.data.get("position"))
        if position is not None:
            two_tap.setdefault(position, {})[event.actor] = event.data
            ids[position] = str(event.data.get("imprinted_id", ""))
    per_contest: dict[int, dict[str, BallotRows]] = {}
    for (actor, c, b), ballots in fold(events).items():
        if (c, b) != (county, board):
            continue
        for position, rows in ballots.items():
            if rows.rows:
                per_contest.setdefault(position, {})[actor] = rows
                ids[position] = rows.imprinted_id
    out: list[BallotComparison] = []
    for position in sorted(set(two_tap) | set(per_contest)):
        detailed = per_contest.get(position, {})
        comparison = BallotComparison(position, ids.get(position, ""), "contest" if detailed else "ballot", "match")
        for actor, rows in sorted(detailed.items()):
            findings = rows.differences()
            for r in findings:
                final = r.final
                assert final is not None
                text = f"first reading {r.readings[1].text()}, second {r.readings[2].text()}" if r.differs else final.text()
                comparison.contests.append(ContestFinding(actor, r.contest, r.expected, text))
            unresolved = len(rows.unresolved())
            if unresolved and not findings:
                comparison.unresolved_by.append((actor, unresolved))
            if not findings and rows.match:
                comparison.matched_by.append(actor)
        two_tap_matches = 0
        for actor, data in sorted(two_tap.get(position, {}).items()):
            note = str(data.get("note") or "")
            if note:
                comparison.ballot_notes.append((actor, note))
            elif data.get("match"):
                comparison.matched_by.append(actor)
                two_tap_matches += 1
        if comparison.contests or comparison.ballot_notes:
            comparison.verdict = "difference"
        clean = bool(detailed) and not comparison.contests and any(r.match for r in detailed.values())
        if (clean and comparison.ballot_notes) or (comparison.contests and two_tap_matches):
            comparison.verdict = "conflict"
        out.append(comparison)
    return out
