"""CORLA's drawn ballots that have no audit record, and its phantom records, round by round.

When CORLA draws a ballot it looks up the ballot's CVR by its exact county, scanner, batch and
record (``CastVoteRecordQueries.atPosition``). If no uploaded CVR matches, it makes a *phantom
record* (``CastVoteRecordQueries.phantomRecord``: record type ``PHANTOM_RECORD``, ballot type
``PHANTOM RECORD``, imprinted ID ``scanner-batch-record``). When the round's ballots are audited,
``PhantomBallots.auditPhantomRecord`` gives the phantom record every contest of its county, each
with the comment ``PHANTOM_RECORD - CVR not found`` and no choices, and submits a matching audit
record as if from an audit board. ``ComparisonAudit.computeDiscrepancy`` then counts a phantom
record as a two-vote overstatement for every contest, and ``ComparisonAudit.recordDiscrepancy``
adds it to ``two_vote_over_count`` of each contest whose draws include it (``isCovering``), once
per time it was drawn (``ComparisonAuditController.submitAuditCVR``, by ``multiplicity``). A
ballot the audit board reports as not found (``BallotNotFound``: an audit record of type
``PHANTOM_BALLOT``, each contest with the comment ``ballot not found`` and no choices) counts as a
two-vote overstatement if its CVR shows a vote for a winner, else a one-vote overstatement
(``computePhantomBallotDiscrepancy``).

CORLA's CVR-to-audit-board comparison (``contest_comparison.sql``) has one row per contest on each
drawn CVR, with the CVR's record type in lower case (``phantom_record``), its ``cvr_id``, and the
audit board's reading; its selection list (``contest_selection.sql``) gives each contest's drawn
CVR IDs, in draw order, a CVR drawn twice listed twice. Both are cumulative: a later round's files
include the earlier rounds' draws.

`check_round` compares one round's selection list with its comparison file and finds, for each
drawn CVR ID (`DrawnBallot`, by ``kind``):

- ``no_record``: no row in the comparison file at all; its county and imprinted ID are taken
  from another round's comparison file when one has it;
- ``phantom_record``: a phantom record (record type, ballot type, choice or comment says so);
- ``ballot_not_found``: the audit board reported the ballot not found;
- ``not_audited``: rows, but no audit board entry in any (no reading, consensus or time), as in a
  file exported before the ballot was audited;
- ``no_contests``: one row with no county or contest (the CVR lists no contest; CORLA counts no
  discrepancy for a contest a CVR does not list), reported for information;
- ``id_repaired``: an ordinary record whose imprinted ID a spreadsheet had saved as a date and is
  read back (`corla_observer.datamap.repair_excel_date_id`): matching by imprinted ID would miss
  it without the repair.

The first four (`WARN_KINDS`) are drawn ballots with no audit board record of them. For each
contest drawn for such a ballot, `ContestCheck` compares the round's contest list with what CORLA's
source says it counts: a phantom record drawn *m* times must be among the contest's
``two_vote_over_count``, a ballot not found among its one- and two-vote overstatements. The counts
can only be checked as lower bounds (other ballots' discrepancies add to them), so ``agree`` means
CORLA's counts are at least the worst cases these draws require, and ``disagree`` that they are
not. For a ballot with no record, what CORLA counted cannot be read from its files
(``undetermined``).

`drawn_report` reads every round's files from the data map (the selection list, comparison file
and contest list each round's directory or name gives, a file with no round, such as the final
reports', standing in for a round that has none), and `drawn_problems` turns the findings into
data map problems: ``drawn_without_record`` per county and ``worst_case_not_counted`` per contest.

Standard library only.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from corla_observer.classify import Classification, canonical_county
from corla_observer.datamap import (
    DataMap,
    MapEntry,
    Problem,
    column,
    role_column,
    round_of,
    table_rows,
)
from corla_observer.events import JSONValue
from corla_observer.workspace import ObjectNotFoundError, Workspace

NO_RECORD = "no_record"
PHANTOM_RECORD = "phantom_record"
BALLOT_NOT_FOUND = "ballot_not_found"
NOT_AUDITED = "not_audited"
NO_CONTESTS = "no_contests"
ID_REPAIRED = "id_repaired"

#: The kinds that are a drawn ballot with no audit board record of it, which the data map warns about.
WARN_KINDS = (NO_RECORD, PHANTOM_RECORD, BALLOT_NOT_FOUND, NOT_AUDITED)

#: How each kind is described, in the singular.
KIND_TEXT = {
    NO_RECORD: "no row in the comparison file",
    PHANTOM_RECORD: "a phantom record (no CVR matched the ballot)",
    BALLOT_NOT_FOUND: "reported not found by the audit board",
    NOT_AUDITED: "no audit board entry",
    NO_CONTESTS: "a record with no contests",
    ID_REPAIRED: "imprinted ID read back from a spreadsheet date",
}

#: How a count of each kind is described.
KIND_COUNTED = {
    NO_RECORD: "with no row",
    PHANTOM_RECORD: "phantom record(s)",
    BALLOT_NOT_FOUND: "not found",
    NOT_AUDITED: "with no audit board entry",
}


@dataclass(frozen=True)
class ComparisonRow:
    """One row of CORLA's CVR-to-audit-board comparison, with its columns named as here."""

    cvr_id: str
    county: str = ""
    contest: str = ""
    imprinted_id: str = ""
    #: The imprinted ID as the file stored it, when that was a spreadsheet's date (else empty).
    stored_id: str = ""
    record_type: str = ""
    ballot_type: str = ""
    cvr_choice: str = ""
    audit_board: str = ""
    consensus: str = ""
    comment: str = ""
    timestamp: str = ""

    @property
    def phantom(self) -> bool:
        """CORLA marks a phantom record by its record type (``phantom_record``), ballot type and comment."""
        return any("phantom" in text.lower() for text in (self.record_type, self.ballot_type, self.cvr_choice, self.comment))

    @property
    def not_found(self) -> bool:
        return "ballot not found" in self.comment.lower()

    @property
    def audited(self) -> bool:
        return bool(self.audit_board or self.consensus or self.timestamp or self.comment)


@dataclass(frozen=True)
class DrawnBallot:
    """A drawn CVR ID worth reporting, with the contests it was drawn for and how many times."""

    cvr_id: str
    kind: str
    county: str | None
    imprinted_id: str | None
    contests: tuple[tuple[str, int], ...] = ()
    #: The imprinted ID as the comparison file stored it, when it was read back from a date.
    stored_id: str | None = None
    note: str = ""

    @property
    def draws(self) -> int:
        return sum(n for _, n in self.contests)

    def text(self) -> str:
        """One line: the ballot, what is wrong, and the contests it was drawn for."""
        where = self.imprinted_id or "imprinted ID unknown"
        drawn = "; ".join(f"{c}{f' ({n} times)' if n > 1 else ''}" for c, n in self.contests)
        parts = [f"{where} (CVR ID {self.cvr_id}): {KIND_TEXT[self.kind]}"]
        if self.stored_id:
            parts.append(f"stored as {self.stored_id}")
        if self.note:
            parts.append(self.note)
        if drawn:
            parts.append(f"drawn for {drawn}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "cvr_id": self.cvr_id,
            "kind": self.kind,
            "county": self.county,
            "imprinted_id": self.imprinted_id,
            "contests": [{"contest": c, "draws": n} for c, n in self.contests],
            "stored_id": self.stored_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class ContestCheck:
    """Whether a contest's counts in CORLA's contest list include the worst cases its unrecorded draws need.

    ``status``: ``agree`` (``two_vote_over`` at least the phantom records' draws, and the one- and
    two-vote overstatements together at least those and the ballots not found), ``disagree``,
    ``undetermined`` (only draws with no record, whose treatment the files do not show) or
    ``no contest list`` (the round's contest list does not list the contest).
    """

    contest: str
    status: str
    drawn: int
    phantom_draws: int = 0
    not_found_draws: int = 0
    unrecorded_draws: int = 0
    audited: int | None = None
    one_vote_over: int | None = None
    two_vote_over: int | None = None

    def text(self) -> str:
        parts = []
        if self.phantom_draws:
            parts.append(f"{self.phantom_draws} phantom record draw(s)")
        if self.not_found_draws:
            parts.append(f"{self.not_found_draws} not-found draw(s)")
        if self.unrecorded_draws:
            parts.append(f"{self.unrecorded_draws} draw(s) with no audit record")
        counted = (
            f"CORLA counts {self.two_vote_over} two-vote and {self.one_vote_over} one-vote overstatement(s), "
            f"{self.audited} audited of {self.drawn} drawn"
            if self.two_vote_over is not None
            else f"{self.drawn} drawn; the round's contest list does not list it"
        )
        return f"{self.status.upper()} {self.contest}: {', '.join(parts)}; {counted}"

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "contest": self.contest,
            "status": self.status,
            "drawn": self.drawn,
            "phantom_draws": self.phantom_draws,
            "not_found_draws": self.not_found_draws,
            "unrecorded_draws": self.unrecorded_draws,
            "audited": self.audited,
            "one_vote_over": self.one_vote_over,
            "two_vote_over": self.two_vote_over,
        }


@dataclass
class RoundCheck:
    """One round's drawn ballots against its comparison file: the findings, and per contest the counts' check."""

    round: int | None = None
    selection_ref: str = ""
    comparison_ref: str = ""
    contest_list_ref: str | None = None
    #: Distinct CVR IDs drawn, and draws (with multiplicity, summed over contests).
    ballots: int = 0
    draws: int = 0
    findings: list[DrawnBallot] = field(default_factory=list)
    #: Per county, CVR IDs in the comparison file that no contest of the round's selection list drew
    #: (counted only when both files are the same round's).
    undrawn: dict[str, int] = field(default_factory=dict)
    contests: list[ContestCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"round {self.round}" if self.round is not None else "round not named"

    def warnings(self) -> list[DrawnBallot]:
        return [f for f in self.findings if f.kind in WARN_KINDS]

    def by_county(self) -> dict[str | None, list[DrawnBallot]]:
        found: dict[str | None, list[DrawnBallot]] = {}
        for f in self.findings:
            found.setdefault(f.county, []).append(f)
        return found

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "round": self.round,
            "selection_ref": self.selection_ref,
            "comparison_ref": self.comparison_ref,
            "contest_list_ref": self.contest_list_ref,
            "ballots": self.ballots,
            "draws": self.draws,
            "findings": [f.to_dict() for f in self.findings],
            "undrawn": dict(self.undrawn),
            "contests": [c.to_dict() for c in self.contests],
            "notes": list(self.notes),
        }


@dataclass
class DrawnReport:
    """Every round's `RoundCheck`, in round order, and notes about files not paired."""

    rounds: list[RoundCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, JSONValue]:
        return {"rounds": [r.to_dict() for r in self.rounds], "notes": list(self.notes)}


def _int(text: str | None) -> int | None:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None


def check_round(
    selections: Mapping[str, Sequence[str]],
    rows: Sequence[ComparisonRow],
    contest_list: Sequence[Mapping[str, str]] | None = None,
    *,
    elsewhere: Mapping[str, tuple[str, str, str]] | None = None,
    same_round: bool = True,
) -> RoundCheck:
    """One round's findings: ``selections`` maps each contest to its drawn CVR IDs (with repeats),
    ``rows`` are the round's comparison rows, ``contest_list`` the round's contest list rows (CORLA's
    column names), and ``elsewhere`` maps a CVR ID to the county, imprinted ID and file name another
    comparison file gives it. ``same_round`` says the comparison file is the selection list's own
    round's, so that its rows no contest drew can be counted."""
    check = RoundCheck()
    drawn: dict[str, Counter[str]] = {}
    for contest, ids in selections.items():
        for cvr_id in ids:
            drawn.setdefault(str(cvr_id), Counter())[contest] += 1
    check.ballots = len(drawn)
    check.draws = sum(sum(c.values()) for c in drawn.values())
    by_cvr: dict[str, list[ComparisonRow]] = {}
    for row in rows:
        by_cvr.setdefault(row.cvr_id, []).append(row)
    for cvr_id, contests in drawn.items():
        found = _classify(cvr_id, tuple(sorted(contests.items())), by_cvr.get(cvr_id, []), elsewhere or {})
        if found is not None:
            check.findings.append(found)
    check.findings.sort(key=lambda f: (f.county or "~", f.imprinted_id or "~", f.cvr_id))
    if same_round:
        undrawn: Counter[str] = Counter()
        for cvr_id, held in by_cvr.items():
            if cvr_id and cvr_id not in drawn:
                undrawn[next((canonical_county(r.county) or r.county for r in held if r.county), "")] += 1
        check.undrawn = dict(sorted(undrawn.items()))
    check.contests = _contest_checks(selections, check.findings, contest_list)
    return check


def _classify(
    cvr_id: str,
    contests: tuple[tuple[str, int], ...],
    rows: Sequence[ComparisonRow],
    elsewhere: Mapping[str, tuple[str, str, str]],
) -> DrawnBallot | None:
    if not rows:
        other = elsewhere.get(cvr_id)
        if other is None:
            return DrawnBallot(cvr_id, NO_RECORD, None, None, contests, note="in no comparison file fetched")
        return DrawnBallot(cvr_id, NO_RECORD, other[0] or None, other[1] or None, contests, note=f"found in {other[2]}")
    county = next((canonical_county(r.county) or r.county for r in rows if r.county), None)
    imprinted = next((r.imprinted_id for r in rows if r.imprinted_id), None)
    stored = next((r.stored_id for r in rows if r.stored_id), None)
    if any(r.phantom for r in rows):
        kind = PHANTOM_RECORD
    elif any(r.not_found for r in rows):
        kind = BALLOT_NOT_FOUND
    elif not any(r.audited for r in rows):
        kind = NOT_AUDITED
    elif not any(r.contest for r in rows):
        kind = NO_CONTESTS
    elif stored:
        kind = ID_REPAIRED
    else:
        return None
    return DrawnBallot(cvr_id, kind, county, imprinted, contests, stored_id=stored)


def _contest_checks(
    selections: Mapping[str, Sequence[str]],
    findings: Sequence[DrawnBallot],
    contest_list: Sequence[Mapping[str, str]] | None,
) -> list[ContestCheck]:
    listed = {row.get("contest_name", "").strip(): row for row in contest_list or ()}
    tally: dict[str, Counter[str]] = {}
    for f in findings:
        if f.kind not in WARN_KINDS:
            continue
        for contest, n in f.contests:
            tally.setdefault(contest, Counter())[f.kind] += n
    checks = []
    for contest, kinds in sorted(tally.items()):
        phantom, not_found = kinds[PHANTOM_RECORD], kinds[BALLOT_NOT_FOUND]
        unrecorded = kinds[NO_RECORD] + kinds[NOT_AUDITED]
        drawn = len(selections.get(contest, ()))
        row = listed.get(contest.strip())
        if row is None:
            checks.append(ContestCheck(contest, "no contest list", drawn, phantom, not_found, unrecorded))
            continue
        o1, o2 = _int(row.get("one_vote_over_count")) or 0, _int(row.get("two_vote_over_count")) or 0
        if phantom or not_found:
            status = "agree" if o2 >= phantom and o1 + o2 >= phantom + not_found else "disagree"
        else:
            status = "undetermined"
        checks.append(
            ContestCheck(contest, status, drawn, phantom, not_found, unrecorded, _int(row.get("audited_sample_count")), o1, o2)
        )
    return checks


# ---------------------------------------------------------------------------
# Reading the files


def read_selections(data: bytes, classification: Classification) -> dict[str, list[str]]:
    """A selection list's drawn CVR IDs per contest, in its order (contests with none left out)."""
    _, rows = table_rows(data, classification)
    contest_col = role_column(classification, "contest") or "contest_name"
    ids_col = role_column(classification, "cvr_ids") or "contest_cvr_ids"
    found: dict[str, list[str]] = {}
    for row in rows:
        try:
            ids = json.loads(row.get(ids_col) or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(ids, list) and ids:
            found[row.get(contest_col, "").strip()] = [str(i) for i in ids]
    return found


def read_comparison(data: bytes, classification: Classification) -> list[ComparisonRow]:
    """A CVR-to-audit-board comparison's rows, with imprinted IDs a spreadsheet saved as dates read back (and kept as stored)."""
    stored: list[str] = []
    header, rows = table_rows(data, classification, stored_ids=stored)

    def col(role: str | None, *spellings: str) -> str:
        return (role_column(classification, role) if role else None) or column(header, *spellings) or spellings[0]

    names = {
        "cvr_id": col("cvr_id", "cvr_id", "cvr id"),
        "county": col("county", "county_name", "county name"),
        "contest": col("contest", "contest_name", "contest name"),
        "imprinted_id": col("imprinted_id", "imprinted_id"),
        "record_type": col(None, "record_type", "record type"),
        "ballot_type": col(None, "ballot_type", "ballot type"),
        "cvr_choice": col("cvr_choice", "choice_per_voting_computer"),
        "audit_board": col("audit_board", "audit_board_selection"),
        "consensus": col("consensus", "consensus"),
        "comment": col(None, "audit_board_comment", "audit board comment"),
        "timestamp": col(None, "timestamp"),
    }
    found = []
    for row, was in zip(rows, stored, strict=True):
        found.append(ComparisonRow(stored_id=was, **{name: row.get(header_name, "") for name, header_name in names.items()}))
    return found


def _by_round(entries: Iterable[MapEntry], kind: str) -> dict[int | None, MapEntry]:
    """Each round's latest file of ``kind`` (by the round its URL or name gives)."""
    found: dict[int | None, MapEntry] = {}
    for entry in entries:
        if entry.kind != kind or entry.ignored or not entry.sha256:
            continue
        number = round_of(entry.ref)
        held = found.get(number)
        if held is None or (entry.seq or 0) >= (held.seq or 0):
            found[number] = entry
    return found


def _classification(entry: MapEntry) -> Classification:
    return Classification(entry.classified_kind, entry.classified_county, entry.evidence)


_CACHE: dict[tuple[tuple[str, str, str], ...], DrawnReport] = {}
_LOCK = threading.Lock()


def report_from_entries(entries: Sequence[MapEntry], get_bytes: Callable[[str], bytes | None]) -> DrawnReport:
    """Every round's check, from the map's selection lists, comparison files and contest lists.

    Kept for the last few sets of files asked about: the comparison files can be tens of megabytes.
    """
    kinds = ("selection_list", "cvr_comparison", "contests_list")
    used = [e for e in entries if e.kind in kinds and not e.ignored and e.sha256]
    key = tuple(sorted((e.kind, e.ref, e.sha256) for e in used))
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    found = _report(used, get_bytes)
    with _LOCK:
        _CACHE[key] = found
        while len(_CACHE) > 4:
            _CACHE.pop(next(iter(_CACHE)))
    return found


def _report(entries: Sequence[MapEntry], get_bytes: Callable[[str], bytes | None]) -> DrawnReport:
    report = DrawnReport()
    selections, comparisons, lists = (_by_round(entries, k) for k in ("selection_list", "cvr_comparison", "contests_list"))
    if not selections or not comparisons:
        return report
    read: dict[str, list[ComparisonRow]] = {}
    for entry in comparisons.values():
        data = get_bytes(entry.sha256)
        if data is None:
            report.notes.append(f"{entry.name} is not in the workspace")
            continue
        read[entry.ref] = read_comparison(data, _classification(entry))
    elsewhere: dict[str, dict[str, tuple[str, str, str]]] = {}
    for ref, rows in read.items():
        name = ref.rstrip("/").rsplit("/", 1)[-1]
        mine = elsewhere.setdefault(ref, {})
        for row in rows:
            if row.cvr_id and row.cvr_id not in mine:
                mine[row.cvr_id] = (canonical_county(row.county) or row.county, row.imprinted_id, name)
    for number in sorted(selections, key=lambda n: (n is None, n or 0)):
        selection = selections[number]
        comparison = comparisons.get(number) or comparisons.get(None)
        if comparison is None or comparison.ref not in read:
            report.notes.append(f"{selection.name}: no CVR-to-audit-board comparison for its round")
            continue
        data = get_bytes(selection.sha256)
        if data is None:
            report.notes.append(f"{selection.name} is not in the workspace")
            continue
        drawn = read_selections(data, _classification(selection))
        listing = lists.get(number) or lists.get(None)
        list_rows = None
        if listing is not None and (list_data := get_bytes(listing.sha256)) is not None:
            list_rows = table_rows(list_data, _classification(listing))[1]
        others: dict[str, tuple[str, str, str]] = {}
        for ref, mine in elsewhere.items():
            if ref != comparison.ref:
                for cvr_id, where in mine.items():
                    others.setdefault(cvr_id, where)
        check = check_round(
            drawn, read[comparison.ref], list_rows, elsewhere=others, same_round=round_of(comparison.ref) == number
        )
        check.round = number
        check.selection_ref, check.comparison_ref = selection.ref, comparison.ref
        check.contest_list_ref = listing.ref if listing is not None else None
        if listing is None:
            check.notes.append("no contest list for this round: CORLA's counts are not checked")
        report.rounds.append(check)
    return report


def drawn_report(datamap: DataMap, workspace: Workspace) -> DrawnReport:
    """`report_from_entries` for a data map's files."""

    def get(sha256: str) -> bytes | None:
        try:
            return workspace.get_bytes(sha256)
        except ObjectNotFoundError:
            return None

    return report_from_entries(datamap.entries, get)


# ---------------------------------------------------------------------------
# Data map problems and text

#: Ballots listed in a problem's message, per round.
_LISTED = 5


def drawn_problems(report: DrawnReport) -> list[Problem]:
    """A warning per county with drawn ballots that have no audit board record in some round, and one per contest
    whose counts do not include the worst cases its phantom records or ballots not found need."""
    per_county: dict[str | None, list[tuple[RoundCheck, list[DrawnBallot]]]] = {}
    for check in report.rounds:
        for county, found in check.by_county().items():
            warned = [f for f in found if f.kind in WARN_KINDS]
            if warned:
                per_county.setdefault(county, []).append((check, warned))
    problems = []
    for county, items in sorted(per_county.items(), key=lambda kv: kv[0] or "~"):
        parts = []
        for check, warned in items:
            counts = Counter(f.kind for f in warned)
            listed = "; ".join(f.text() for f in warned[:_LISTED]) + (f"; and {len(warned) - _LISTED} more" if len(warned) > _LISTED else "")
            summary = ", ".join(f"{n} {KIND_COUNTED[k]}" for k, n in sorted(counts.items(), key=lambda kv: WARN_KINDS.index(kv[0])))
            parts.append(f"{check.label}: {summary} ({listed})")
        problems.append(
            Problem(
                "warning",
                "drawn_without_record",
                f"{county or 'County unknown'}: ballots CORLA drew with no audit board record in its CVR-to-audit-board "
                f"comparison: {'; '.join(parts)}",
                refs=tuple(dict.fromkeys(r for check, _ in items for r in (check.selection_ref, check.comparison_ref))),
                hint=(
                    "CORLA counts a phantom record (no CVR matched the drawn ballot's tabulator, batch and record) as a "
                    "two-vote overstatement in each contest it was drawn for, and a ballot not found as a one- or "
                    "two-vote overstatement; ask the county and the Secretary of State why the ballot had no CVR or "
                    "record, and check the contest's counts (corla-observer map drawn)"
                ),
                county=county,
            )
        )
    for check in report.rounds:
        for c in check.contests:
            if c.status == "disagree":
                problems.append(
                    Problem(
                        "warning",
                        "worst_case_not_counted",
                        f"{check.label}: {c.text()}",
                        refs=tuple(r for r in (check.contest_list_ref, check.comparison_ref) if r),
                        hint=(
                            "CORLA's source counts each draw of a phantom record as a two-vote overstatement, and of a "
                            "ballot not found as at least a one-vote one; its contest list shows fewer: ask the "
                            "Secretary of State"
                        ),
                    )
                )
    return problems


def report_lines(report: DrawnReport) -> list[str]:
    """The report as text, round by round and county by county."""
    lines = []
    if not report.rounds:
        lines.append("No round has both a selection list and a CVR-to-audit-board comparison in the map.")
    for check in report.rounds:
        warned = check.warnings()
        lines.append(
            f"{check.label}: {check.ballots} ballots drawn ({check.draws} draws), {len(warned)} with no audit board record; "
            f"{check.selection_ref} against {check.comparison_ref}"
        )
        for note in check.notes:
            lines.append(f"  {note}")
        for county, found in check.by_county().items():
            lines.append(f"  {county or 'County unknown'}:")
            lines.extend(f"    {f.text()}" for f in found)
        if check.undrawn:
            listed = ", ".join(f"{county or 'county unknown'} {n}" for county, n in check.undrawn.items())
            lines.append(f"  in the comparison file but drawn for no contest of the selection list: {listed}")
        for c in check.contests:
            lines.append(f"  {c.text()}")
    lines.extend(report.notes)
    return lines
