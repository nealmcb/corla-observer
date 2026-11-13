"""Tests for corla_observer.drawn: drawn ballots with no audit record, and phantom records."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.datamap import build_map
from corla_observer.drawn import (
    BALLOT_NOT_FOUND,
    ID_REPAIRED,
    NO_CONTESTS,
    NO_RECORD,
    NOT_AUDITED,
    PHANTOM_RECORD,
    ComparisonRow,
    check_round,
    drawn_problems,
    drawn_report,
)
from corla_observer.events import EventLog
from corla_observer.workspace import Workspace
from synthetic import election_files
from test_datamap import fetch

HEADER = [
    "county_name", "contest_name", "imprinted_id", "ballot_type", "choice_per_voting_computer",
    "audit_board_selection", "consensus", "record_type", "audit_board_comment", "timestamp", "cvr_id",
]  # fmt: skip


def row(cvr_id: str, imprinted: str, contest: str = "Governor - DEM", county: str = "Adams", **values: str) -> ComparisonRow:
    base = {"cvr_choice": '"A"', "audit_board": '"A"', "consensus": "YES", "record_type": "uploaded", "timestamp": "2026-11-17"}
    return ComparisonRow(cvr_id, county, contest, imprinted, **(base | values))


def phantom(cvr_id: str, imprinted: str, contest: str = "Governor - DEM") -> ComparisonRow:
    """A phantom record's row, as CORLA's export writes it after auditing it (`PhantomBallots.auditPhantomRecord`)."""
    return ComparisonRow(
        cvr_id, "Adams", contest, imprinted, ballot_type="PHANTOM RECORD", record_type="phantom_record",
        comment="PHANTOM_RECORD - CVR not found", timestamp="2026-11-17",
    )  # fmt: skip


def contest_list(o1: int, o2: int, audited: int = 5) -> list[dict[str, str]]:
    return [
        {"contest_name": "Governor - DEM", "audited_sample_count": str(audited), "one_vote_over_count": str(o1),
         "two_vote_over_count": str(o2)},
    ]  # fmt: skip


def test_each_kind_of_drawn_ballot_is_found() -> None:
    selections = {
        "Governor - DEM": ["1", "2", "2", "3", "4", "5", "6", "7", "8"],
        "Adams County Clerk - REP": ["2", "9"],
    }
    rows = [
        row("1", "1-1-1"),
        phantom("2", "3-7-12"),
        phantom("2", "3-7-12", "Adams County Clerk - REP"),
        row("3", "1-1-3", comment="ballot not found", audit_board="", consensus=""),
        row("4", "1-1-4", audit_board="", consensus="", timestamp=""),
        row("5", "1-1-5", contest="", county=""),
        ComparisonRow("6", "Adams", "Governor - DEM", "3-19-7", stored_id="3/19/2007", audit_board="x", timestamp="t"),
        row("8", "1-1-8"),
        row("10", "1-1-10"),
    ]
    found = check_round(selections, rows, elsewhere={"7": ("Adams", "1-1-7", "round_2/contestComparison.csv")})
    kinds = {f.cvr_id: f.kind for f in found.findings}
    assert kinds == {
        "2": PHANTOM_RECORD, "3": BALLOT_NOT_FOUND, "4": NOT_AUDITED, "5": NO_CONTESTS, "6": ID_REPAIRED, "7": NO_RECORD,
        "9": NO_RECORD,
    }  # fmt: skip
    by_id = {f.cvr_id: f for f in found.findings}
    assert by_id["2"].contests == (("Adams County Clerk - REP", 1), ("Governor - DEM", 2))
    assert by_id["2"].imprinted_id == "3-7-12" and by_id["2"].county == "Adams"
    assert by_id["6"].stored_id == "3/19/2007" and "stored as 3/19/2007" in by_id["6"].text()
    assert (by_id["7"].county, by_id["7"].imprinted_id) == ("Adams", "1-1-7")
    assert "round_2/contestComparison.csv" in by_id["7"].note
    assert by_id["9"].county is None and "in no comparison file" in by_id["9"].note
    assert (found.ballots, found.draws) == (9, 11)
    assert {f.cvr_id for f in found.warnings()} == {"2", "3", "4", "7", "9"}
    assert found.undrawn == {"Adams": 1}  # CVR 10: a row no contest drew
    assert check_round(selections, rows, same_round=False).undrawn == {}


def test_ordinary_draws_are_not_reported() -> None:
    found = check_round({"Governor - DEM": ["1", "1"]}, [row("1", "1-1-1"), row("1", "1-1-1", contest="Other")])
    assert found.findings == [] and found.contests == []


@pytest.mark.parametrize(
    ("o1", "o2", "status"),
    [(0, 2, "agree"), (0, 3, "agree"), (1, 1, "disagree"), (5, 0, "disagree")],
)
def test_contest_counts_against_the_worst_case(o1: int, o2: int, status: str) -> None:
    """A phantom record drawn twice needs two two-vote overstatements (CORLA's `ComparisonAudit.computeDiscrepancy`)."""
    found = check_round({"Governor - DEM": ["1", "2", "2"]}, [row("1", "1-1-1"), phantom("2", "3-7-12")], contest_list(o1, o2))
    (check,) = found.contests
    assert (check.contest, check.phantom_draws, check.two_vote_over, check.status) == ("Governor - DEM", 2, o2, status)


def test_a_ballot_not_found_needs_a_one_vote_overstatement_at_least() -> None:
    rows = [row("1", "1-1-1", comment="ballot not found", audit_board="", consensus="")]
    assert check_round({"Governor - DEM": ["1"]}, rows, contest_list(1, 0)).contests[0].status == "agree"
    assert check_round({"Governor - DEM": ["1"]}, rows, contest_list(0, 0)).contests[0].status == "disagree"


def test_draws_with_no_record_are_undetermined_and_unlisted_contests_said() -> None:
    found = check_round({"Governor - DEM": ["1"], "Other": ["2"]}, [phantom("2", "3-7-12", "Other")], contest_list(0, 0))
    assert [(c.contest, c.status) for c in found.contests] == [("Governor - DEM", "undetermined"), ("Other", "no contest list")]


def test_problems_per_county_and_per_contest() -> None:
    found = check_round(
        {"Governor - DEM": ["1", "2"]}, [phantom("1", "3-7-12"), row("2", "1-1-2", county="Weld")], contest_list(0, 0)
    )
    found.round, found.selection_ref, found.comparison_ref = 1, "sel", "cmp"
    from corla_observer.drawn import DrawnReport

    problems = drawn_problems(DrawnReport([found]))
    assert [(p.code, p.county) for p in problems] == [("drawn_without_record", "Adams"), ("worst_case_not_counted", None)]
    assert "round 1: 1 phantom record(s) (3-7-12 (CVR ID 1): a phantom record" in problems[0].message and "3-7-12 (CVR ID 1)" in problems[0].message
    assert problems[0].refs == ("sel", "cmp")
    assert "DISAGREE Governor - DEM" in problems[1].message


# ---------------------------------------------------------------------------
# From the data map


def comparison_csv(rows: Sequence[Sequence[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return out.getvalue().encode()


SELECTION_KEY = "finalReports_ContestMarginAndContestCVR-IDs-Round1_csv"
SELECTION = b'min_margin,contest_name,contest_cvr_ids\r\n30,Governor - DEM,"[11,12,13,12]"\r\n'
COMPARISON = comparison_csv(
    [
        ["Adams", "Governor - DEM", "1-1-1", "BT-1", '"A"', '"A"', "YES", "uploaded", "", "2026-07-01 10:00", "11"],
        ["Adams", "Governor - DEM", "3-7-12", "PHANTOM RECORD", "", "", "", "phantom_record",
         "PHANTOM_RECORD - CVR not found", "2026-07-01 10:05", "12"],
        ["Adams", "Adams County Clerk - REP", "3-7-12", "PHANTOM RECORD", "", "", "", "phantom_record",
         "PHANTOM_RECORD - CVR not found", "2026-07-01 10:05", "12"],
    ]
)  # fmt: skip
CONTESTS = (
    b"contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,contest_ballot_card_count,"
    b"winners,min_margin,risk_limit,audited_sample_count,two_vote_over_count,one_vote_over_count\r\n"
    b'Governor - DEM,state_wide_contest,in_progress,1,165,150,"[""A""]",30,0.03,4,1,0\r\n'
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def test_the_data_map_warns_once_the_comparison_is_posted(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(**{SELECTION_KEY: SELECTION}))
    assert not [p for p in build_map(log, ws).problems if p.code == "drawn_without_record"]
    fetch(ws, log, election_files(
        **{SELECTION_KEY: SELECTION},
        finalReports_CVRtoAuditBoardInterpretationComparison_csv=COMPARISON,
        finalReports_ContestsListRound1_csv=CONTESTS,
    ))  # fmt: skip
    datamap = build_map(log, ws)
    warned = [p for p in datamap.problems if p.code in ("drawn_without_record", "worst_case_not_counted")]
    assert [(p.code, p.county) for p in warned] == [
        ("drawn_without_record", "Adams"),
        ("drawn_without_record", None),
        ("worst_case_not_counted", None),
    ]
    assert "3-7-12 (CVR ID 12): a phantom record" in warned[0].message and "Governor - DEM (2 times)" in warned[0].message
    assert "CVR ID 13" in warned[1].message
    (check,) = drawn_report(datamap, ws).rounds
    assert check.round == 1 and check.comparison_ref.endswith("CVRtoAuditBoardInterpretationComparison.csv")
    assert [(c.contest, c.phantom_draws, c.two_vote_over, c.status) for c in check.contests] == [
        ("Governor - DEM", 2, 1, "disagree")
    ]


def test_the_command_lists_them_and_exits_1(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(
        **{SELECTION_KEY: SELECTION},
        finalReports_CVRtoAuditBoardInterpretationComparison_csv=COMPARISON,
    ))  # fmt: skip
    log.close()
    result = CliRunner().invoke(app, ["map", "drawn", "--workspace", str(ws.root)])
    assert result.exit_code == 1, result.output
    assert "round 1: 3 ballots drawn (4 draws), 2 with no audit board record" in result.output
    assert "3-7-12 (CVR ID 12): a phantom record" in result.output
    assert "no contest list for this round" in result.output
    result = CliRunner().invoke(app, ["map", "drawn", "--workspace", str(ws.root), "--json"])
    assert '"kind": "phantom_record"' in result.output
