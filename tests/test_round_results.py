"""Fast tests for corla_observer.round_results and the stage it gives each county: CORLA's results, round by round.

Small synthetic contest lists, a contest-to-county file and a comparison file,
shaped as the 2026 general's (``round1/contest.csv``, ``round2/contest.csv``,
``round1/contestComparison.csv``, ``contestsByCounty.csv``), are fetched into
a temporary workspace as in tests/test_county_status.py.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from synthetic import Ballot, ballots, election_files, make_cvr, make_xlsx

from corla_observer import round_results as rr
from corla_observer.classify import classify_bytes
from corla_observer.county_status import audit_rounds, corla_uploads, county_statuses, stage
from corla_observer.datamap import build_map
from corla_observer.events import EventLog
from corla_observer.progress import audit_progress
from corla_observer.round_results import (
    choices_differ,
    choices_text,
    contest_counties,
    parse_choices,
    redacted_card_ids,
    round_results,
)
from corla_observer.uploads import receive, status
from corla_observer.web.views import build_snapshot
from corla_observer.workspace import Workspace
from test_county_status import UPLOAD_STATUS, fetch
from test_web import Factory, make_client, workspace  # noqa: F401 (fixtures)

ACTOR = "coordinator:c1"
HEADER = (
    "contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,contest_ballot_card_count,winners,"
    "min_margin,risk_limit,audited_sample_count,two_vote_over_count,one_vote_over_count,zero_discrepancy_count,"
    "one_vote_under_count,two_vote_under_count,disagreement_count,gamma,overstatements,optimistic_samples_to_audit,"
    "estimated_samples_to_audit"
)


def contest_row(
    name: str, reason: str, status: str, audited: int, one_over: int = 0, estimated: int = 10, one_under: int = 0, vote_for: int = 1
) -> str:
    return f'{name},{reason},{status},{vote_for},100,90,"""A""",20,0.03,{audited},0,{one_over},{audited - one_over},{one_under},0,0,1.03905,{one_over},{estimated},{estimated}'


ROUND1 = "\r\n".join([
    HEADER,
    contest_row("United States Senator", "state_wide_contest", "in_progress", 0, estimated=20),
    contest_row("Weld County Council - At-Large", "county_wide_contest", "in_progress", 0, estimated=12),
    contest_row("Morgan - Proposition 137 (STATUTORY)", "county_wide_contest", "in_progress", 0, estimated=15),
    contest_row("Jackson County Assessor", "county_wide_contest", "in_progress", 0, estimated=32),
    contest_row("Town of Nowhere Question 1", "opportunistic_benefits", "in_progress", 0),
]).encode() + b"\r\n"  # fmt: skip
ROUND2 = "\r\n".join([
    HEADER,
    contest_row("United States Senator", "state_wide_contest", "risk_limit_achieved", 25, estimated=20),
    contest_row("Weld County Council - At-Large", "county_wide_contest", "risk_limit_achieved", 14, estimated=12),
    contest_row("Morgan - Proposition 137 (STATUTORY)", "county_wide_contest", "risk_limit_achieved", 16, estimated=15),
    contest_row("Jackson County Assessor", "county_wide_contest", "in_progress", 33, one_over=1, estimated=40),
    contest_row("Town of Nowhere Question 1", "opportunistic_benefits", "ended", 3),
    contest_row("Weld School Board Directors", "opportunistic_benefits", "ended", 3, vote_for=2),
]).encode() + b"\r\n"  # fmt: skip
BY_COUNTY = (
    b"county_id,county_name,contest_name,contest_id\r\n"
    + b"".join(f"{i},{c},United States Senator,1\r\n".encode() for i, c in ((1, "Adams"), (30, "Jackson"), (45, "Morgan"), (62, "Weld")))
    + b"62,Weld,Weld County Council - At-Large,2\r\n45,Morgan,Morgan - Proposition 137 (STATUTORY),3\r\n30,Jackson,Jackson County Assessor,4\r\n"
)


def comparison(rows: Sequence[tuple[str, str, str, str, str]]) -> bytes:
    """A contestComparison.csv of (county, contest, imprinted ID, CVR choice, board choice) rows."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["county_name", "contest_name", "imprinted_id", "ballot_type", "choice_per_voting_computer", "audit_board_selection",
                     "consensus", "record_type", "audit_board_comment", "timestamp", "cvr_id", "audit_reason"])  # fmt: skip
    for n, (county, contest, card, cvr, board) in enumerate(rows, start=1):
        writer.writerow([county, contest, card, "1", cvr, board, "YES", "uploaded", "", "2026-11-17 10:14:45.755636", n, ""])
    return out.getvalue().encode()


COMPARISON = comparison([
    ("Weld", "United States Senator", "101-1-3", '"John Hickenlooper"', '"John Hickenlooper","Ryan Apelbaum"'),
    ("Weld", "Weld County Council - At-Large", "101-1-3", '"Ann"', '"Ann"'),
    ("Weld", "Weld School Board Directors", "101-1-4", '"B","A"', '"A","B"'),  # the same choices in another order: no difference
    ("Weld", "United States Senator", "101-1-99", '"Yes"', '"No"'),  # not in Weld's redacted CVR: never listed
    ("Weld", "United States Senator", "101-1-5", "", '"A","B"'),  # a voter's overvote, entered as marked: no discrepancy
    ("Weld", "Weld School Board Directors", "101-1-6", "", '"A","B"'),  # two choices in a Vote For 2 contest: a difference
    ("Weld", "Town of Weld Mayor", "101-1-7", '"Ann(1)","Bob(2)"', '"Bob(1)","Ann(2)"'),  # ranked-choice: listed apart
    ("Morgan", "Colorado Court of Appeals Judge - Harris", "103-78-8", "", '"Yes"'),
    ("Morgan", "Morgan - Proposition 137 (STATUTORY)", "103-78-8", '"Yes/For"', '"Yes/For"'),
    ("Jackson", "Jackson County Assessor", "102-8-9", '"Cheryl Corkle"', ""),
])  # fmt: skip

FILES = {
    "files/round1/contest.csv": ROUND1,
    "files/round2/contest.csv": ROUND2,
    "files/round2/contestSelection.csv": b"min_margin,contest_name,contest_cvr_ids\r\n20,United States Senator,[1]\r\n",
    "files/round1/contestComparison.csv": COMPARISON,
    "files/contestsByCounty.csv": BY_COUNTY,
}
#: Weld's redacted CVR: cards 101-1-1 to 101-1-21 (tabulator 101, batch 1), as tests/synthetic.make_cvr numbers them.
WELD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"))


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    rr.clear_cache()
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def receive_cvr(ws: Workspace, log: EventLog, county: str, tmp_path: Path, data: bytes = WELD_CVR) -> str:
    source = tmp_path / f"{county}.csv"
    source.write_bytes(data)
    result = receive(ws, log, county, "redacted_cvr", source, "cvr.csv", ACTOR)
    assert result.accepted
    return str(result.sha256)


# ---------------------------------------------------------------------------
# Choices and contests' counties


def test_choices_are_parsed_rendered_and_compared_as_sets() -> None:
    assert parse_choices('"Yes/For"') == ("Yes/For",)
    assert parse_choices('"John Hickenlooper","Ryan Apelbaum"') == ("John Hickenlooper", "Ryan Apelbaum")
    assert parse_choices("") == () and parse_choices('["A", "B"]') == ("A", "B")
    assert parse_choices('"Smith, Jr.","Doe"') == ("Smith, Jr.", "Doe")
    assert choices_text(()) == "no vote" and choices_text(("Yes/For",)) == "Yes/For"
    assert choices_text(("John Hickenlooper", "Ryan Apelbaum")) == "John Hickenlooper and Ryan Apelbaum (two choices)"
    assert choices_text(("A", "B", "C")) == "A, B and C (three choices)"
    assert not choices_differ(("A", "B"), ("B", "A")) and choices_differ((), ("Yes",)) and choices_differ(("A",), ("A", "B"))


def test_contest_counties_from_prefix_file_and_name() -> None:
    listed = {"Weld County Council - At-Large": ["Weld"], "City of Holyoke Ballot Measure 2B": ["Phillips"],
              "State Senator - District 9": ["El Paso", "Teller"], "Cheyenne - Colorado Court of Appeals Judge - Harris": ["Kit Carson"],
              "United States Senator": ["Adams", "Weld"]}  # fmt: skip
    assert contest_counties("Morgan - Proposition 137 (STATUTORY)", "county_wide_contest", {})[:2] == (("Morgan",), False)
    # A prefix the contest-to-county file contradicts keeps the prefix's county, and says so.
    counties, _, note = contest_counties("Cheyenne - Colorado Court of Appeals Judge - Harris", "county_wide_contest", listed)
    assert counties == ("Cheyenne",) and "lists Kit Carson" in note
    assert contest_counties("Attorney General - REP (Weld)", "county_wide_contest", {})[0] == ("Weld",)
    assert contest_counties("City of Holyoke Ballot Measure 2B", "county_wide_contest", listed)[0] == ("Phillips",)
    # "Weld County Council" is not a county before " - ": the file, else the name's "<County> County", decides.
    assert contest_counties("Weld County Council - At-Large", "county_wide_contest", listed)[0] == ("Weld",)
    assert contest_counties("Weld County Council - At-Large", "county_wide_contest", {})[0] == ("Weld",)
    assert contest_counties("CLEAR CREEK COUNTY BALLOT ISSUE 1A", "county_wide_contest", {})[0] == ("Clear Creek",)
    # Ambiguous: the file lists several counties; all of them, and said so.
    counties, statewide, note = contest_counties("State Senator - District 9", "county_wide_contest", listed)
    assert counties == ("El Paso", "Teller") and not statewide and "2 counties" in note
    assert contest_counties("Town Question 3", "county_wide_contest", {})[0] == ()
    # Statewide: the file's counties, else every county in CORLA's audit (not San Juan).
    assert contest_counties("United States Senator", "state_wide_contest", listed)[:2] == (("Adams", "Weld"), True)
    everywhere = contest_counties("Secretary of State", "state_wide_contest", {})[0]
    assert len(everywhere) == 63 and "San Juan" not in everywhere


# ---------------------------------------------------------------------------
# The stage


def test_only_counties_with_a_contest_in_progress_are_in_round_two(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS) | FILES)
    datamap = build_map(log, ws)
    rounds = audit_rounds(datamap, ws)
    # The statewide round-2 files would have put every county in round 2.
    assert rounds.election_round == 2 and [x.round for x in rounds.contest_lists] == [1, 2]
    jackson = stage("Jackson", rounds, seed_source="s", round_start=None)
    assert jackson.label == "Round 2" and jackson.round == 2 and not jackson.finished
    assert "Jackson County Assessor" in jackson.source and "round2/contest.csv" in jackson.source
    weld = stage("Weld", rounds, seed_source="s", round_start=None)
    assert weld.label == "Round 1 finished: its targeted contests met the risk limit" and weld.round == 1 and weld.finished
    # Covered only by the statewide contest, which met the risk limit.
    assert stage("Adams", rounds, seed_source="s", round_start=None).label == "Round 1 finished: its targeted contest met the risk limit"

    statuses = county_statuses(status(log), corla_uploads(datamap, ws), rounds, seed_source="s")
    assert statuses["Jackson"].round == 2 and statuses["Weld"].finished and statuses["Weld"].summary.startswith("Round 1 finished")
    steps = audit_progress(contests_posted=True, corla=corla_uploads(datamap, ws), seed=True, selections=True, statuses=statuses)
    assert steps[-1].detail == "Round 2: 1 county (Jackson); 3 counties finished in round 1; 59 not yet in a round" and not steps[-1].done
    # When every county in CORLA's audit is covered by a contest list (as in the 2026 general):
    covered = {c: s for c, s in statuses.items() if c in ("Adams", "Jackson", "Morgan", "Weld")}
    steps = audit_progress(contests_posted=True, corla=corla_uploads(datamap, ws), seed=True, selections=True, statuses=covered)
    assert steps[-1].detail == "Round 2: 1 county (Jackson); the other counties finished in round 1"
    finished = {c: s for c, s in covered.items() if c != "Jackson"}
    steps = audit_progress(contests_posted=True, corla=corla_uploads(datamap, ws), seed=True, selections=True, statuses=finished)
    assert steps[-1].detail == "every county finished in round 1" and steps[-1].state == "done"


def test_without_contest_lists_the_round_files_decide_as_before(ws: Workspace, log: EventLog) -> None:
    files = {k: v for k, v in FILES.items() if "contest.csv" not in k}
    fetch(ws, log, election_files() | files)
    rounds = audit_rounds(build_map(log, ws), ws)
    assert rounds.contest_lists == () and rounds.election_round == 2
    assert stage("Weld", rounds, seed_source="s", round_start=None).label == "Round 2"


def test_round_one_list_alone_puts_every_covered_county_in_round_one(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files() | {"files/round1/contest.csv": ROUND1, "files/contestsByCounty.csv": BY_COUNTY})
    rounds = audit_rounds(build_map(log, ws), ws)
    assert stage("Weld", rounds, seed_source="s", round_start=None).label == "Round 1"
    # The statewide contest covers the counties the file lists for it; Denver is not among them, so its stage comes from the seed.
    assert stage("Denver", rounds, seed_source="s", round_start=None).label == "drawing done (seed entered)"


# ---------------------------------------------------------------------------
# The explanations and CORLA's comparison


def test_standings_continuing_contests_and_estimates(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files() | FILES)
    results = round_results(build_map(log, ws), ws, {})
    first, second = (s.text for s in results.standings)
    assert first == "Round 1 began (RLA/2026/general/files/round1/contest.csv) with 4 targeted contests to audit (1 statewide, 3 county-wide)."
    assert second == (
        "After round 1 (RLA/2026/general/files/round2/contest.csv): 3 of 4 targeted contests met the risk limit; "
        "1 continues in round 2: Jackson County Assessor (Jackson)."
    )
    (jackson,) = results.continuing
    assert jackson.counties == "Jackson" and jackson.audited_text.startswith("33 cards audited so far (audited_sample_count in RLA/")
    assert jackson.discrepancies == [
        "one card where the audit board's reading took one vote from the reported winner's margin (a one-vote overstatement)"
    ]
    assert jackson.estimate_text == (
        "CORLA's estimate of the cards it needs (estimated_samples_to_audit) rose from 32, in round 1's contest list "
        "(RLA/2026/general/files/round1/contest.csv), to 40, in round 2's (RLA/2026/general/files/round2/contest.csv)."
    )
    assert "does not increase the cards needed" in results.overstatement_note
    understated = rr.ContestStatus("X", "county_wide_contest", "in_progress", ("Weld",), False, "", audited=9, one_under=2)
    assert rr.discrepancy_lines(understated) == [
        "two cards where the audit board's reading added one vote to the reported winner's margin (one-vote understatements)"
    ]


def test_comparison_counts_and_the_privacy_rule(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files() | FILES)
    weld_cvr = receive_cvr(ws, log, "Weld", tmp_path)
    results = round_results(build_map(log, ws), ws, {"Weld": weld_cvr, "Morgan": None})
    (round1,) = results.comparisons
    assert round1.label == "Round 1" and round1.ref == "RLA/2026/general/files/round1/contestComparison.csv"
    counties = {c.county: c for c in round1.counties}
    weld, morgan, jackson = counties["Weld"], counties["Morgan"], counties["Jackson"]
    # Six cards; 101-1-4's choices differ only in order; 101-1-5's overvote is no discrepancy; 101-1-7 is ranked-choice.
    assert (weld.cards, weld.differing_cards, weld.voter_overvote_cards, weld.ranked_cards) == (6, 3, 1, 1)
    assert (round1.differing_cards, round1.voter_overvote_cards, round1.ranked_cards) == (5, 1, 1)
    row, two = weld.rows
    # No results report with the contest's draws: "targeted contest", nothing said about its sample.
    assert (row.imprinted_id, row.contest, row.targeted) == ("101-1-3", "United States Senator", "targeted contest (statewide)")
    assert (row.cvr_choice, row.board_choice) == ("John Hickenlooper", "John Hickenlooper and Ryan Apelbaum (two choices)")
    assert row.note == rr.BOARD_OVERVOTE_NOTE
    assert (two.imprinted_id, two.cvr_choice, two.board_choice, two.note, two.targeted) == ("101-1-6", "no vote", "A and B (two choices)", "", "not targeted")
    (overvote,) = weld.overvote_rows
    assert (overvote.imprinted_id, overvote.cvr_choice, overvote.board_choice) == ("101-1-5", "no vote", "A and B (two choices)")
    (ranked,) = weld.ranked_rows
    assert (ranked.imprinted_id, ranked.contest) == ("101-1-7", "Town of Weld Mayor")
    # 101-1-99 is not in Weld's redacted CVR: counted, never listed.
    assert weld.withheld == 1 and "protect voters' privacy" in weld.note
    # It is named, by ID only, as audited but not in the redacted CVR.
    assert weld.redacted == ["101-1-99"] and morgan.redacted == []
    # Morgan takes part but has no redacted CVR that passed: nothing listed.
    assert morgan.listed and morgan.rows == [] and morgan.withheld == 1 and "no redacted CVR" in morgan.note
    # Jackson does not take part in observation: counts only.
    assert (jackson.cards, jackson.differing_cards, jackson.listed, jackson.rows) == (1, 1, False, [])


def test_corla_counts_an_overvote_the_board_reads_as_no_vote() -> None:
    def kind(cvr: tuple[str, ...], board: tuple[str, ...], vote_for: int = 1, contest: str = "Mayor") -> str:
        ranked = rr.is_ranked(contest, cvr, board)
        return rr.CardRow(contest, cvr, board, vote_for=vote_for, ranked=ranked).kind

    assert rr.corla_board_reading(("A", "B"), 1) == () and rr.corla_board_reading(("A", "B"), 2) == ("A", "B")
    # CVR no vote, board overvote: no discrepancy. CVR vote, board overvote including it: a difference.
    assert kind((), ("A", "B")) == rr.VOTER_OVERVOTE and not rr.CardRow("Mayor", (), ("A", "B")).differs
    assert kind(("A",), ("A", "B")) == rr.BOARD_OVERVOTE and rr.CardRow("Mayor", ("A",), ("A", "B")).differs
    # Vote For 2: two choices are a vote, three an overvote.
    assert kind((), ("A", "B"), 2) == rr.DIFFERS and kind(("A", "B"), ("B", "A"), 2) == rr.SAME
    assert kind((), ("A", "B", "C"), 2) == rr.VOTER_OVERVOTE and kind(("A", "B"), ("A", "B", "C"), 2) == rr.BOARD_OVERVOTE
    assert kind(("A",), ("B",)) == rr.DIFFERS and kind((), ()) == rr.SAME
    # Ranked-choice: never an overvote; compared in order; listed apart, not as a difference.
    assert rr.is_ranked("Mayor (Number of positions=1, Number of ranks=5)") and rr.is_ranked("Mayor", ("Ann(1)",))
    assert not rr.is_ranked("Proposition 137 (STATUTORY)", ("Yes/For",))
    assert kind(("Ann(1)", "Bob(2)"), ("Ann(1)", "Bob(2)")) == rr.SAME
    assert kind(("Ann(1)", "Bob(2)"), ("Bob(1)", "Ann(2)")) == rr.RANKED
    assert not rr.CardRow("Mayor", ("Ann(1)",), ("Ann(1)", "Bob(2)"), ranked=True).differs


def test_a_contest_with_several_choices_in_the_cvr_is_taken_as_ranked() -> None:
    """A Dominion CVR records a plurality overvote as no vote: a CVR side with more choices than Vote For is a ranking."""
    data = comparison([
        ("Boulder", "City of Boulder Mayoral Candidates", "101-1-1", '"Ann","Bob"', '"Ann","Bob"'),
        ("Boulder", "City of Boulder Mayoral Candidates", "101-1-2", '"Ann"', '"Ann","Bob"'),
        ("Boulder", "Boulder County Treasurer", "101-1-2", '"Cy"', '"Cy","Di"'),
    ])  # fmt: skip
    found = rr.read_comparison_file(data, classify_bytes(data), ref="r", round_number=1, vote_for={})
    rows = {(card, r.contest): r for card, rs in found.counties["Boulder"].items() for r in rs}
    assert rows[("101-1-1", "City of Boulder Mayoral Candidates")].kind == rr.SAME
    assert rows[("101-1-2", "City of Boulder Mayoral Candidates")].kind == rr.RANKED  # not an overvote
    assert rows[("101-1-2", "Boulder County Treasurer")].kind == rr.BOARD_OVERVOTE  # Vote For 1 when not known


def report_with_draws(senate_sheet: str, cards: Sequence[tuple[str, str]]) -> bytes:
    """A results report whose "United States Senator" sheet lists ``cards`` (county, imprinted ID) as its draws."""
    return make_xlsx({
        "Contests to Sheets": [["Contests to Sheets", "No Data"], ["Contest Name", "Sheet Name"], ["United States Senator", senate_sheet]],
        "Summary": [["Summary", "No Data"], SUMMARY_HEADER, summary_row("United States Senator", "Yes", "1.0", 25)],
        senate_sheet.replace("/", " "): [["title", "No Data"], ["county", "imprinted id", "round"], *[[c, i, "1"] for c, i in cards], ["End of worksheet"]],
    })  # fmt: skip


def test_targeted_only_on_a_card_of_the_contests_own_sample(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    def senate_row(report: bytes) -> str:
        rr.clear_cache()
        fetch(ws, log, election_files() | FILES | {"files/round1/resultsReport.xlsx": report})
        weld_cvr = receive_cvr(ws, log, "Weld", tmp_path)
        (round1,) = round_results(build_map(log, ws), ws, {"Weld": weld_cvr}).comparisons
        weld = next(c for c in round1.counties if c.county == "Weld")
        return next(r.targeted for r in weld.rows if r.imprinted_id == "101-1-3")

    assert senate_row(report_with_draws("United States Senar", [("Weld", "101-1-3")])) == "targeted (statewide)"
    # Drawn only for another contest: the same card ID in another county is another card.
    other = [("Weld", "101-1-4"), ("Adams", "101-1-3")]
    assert senate_row(report_with_draws("United States Senar", other)) == "targeted contest (statewide), card drawn for another contest"
    # The map may name a sheet with a character Excel forbids; the sheet has a space for it.
    assert senate_row(report_with_draws("United/States Senator", [("Weld", "101-1-3")])) == "targeted (statewide)"
    report = rr.read_results_report(report_with_draws("S", [("Weld", "01-1-3")]), ref="r", round_number=1)
    assert report is not None and report.draws == {"United States Senator": frozenset({("Weld", "1-1-3")})}
    # A report of an earlier round than the comparison file says nothing of a later round's draws.
    assert rr.own_samples([report], 2) is None and rr.own_samples([report], 1) == report.draws


def test_redacted_rows_are_not_cards_the_cvr_shows(ws: Workspace) -> None:
    rows = [*ballots(3, "Mayor"), Ballot(("Mayor",), marker="REDACTED"), Ballot(("Mayor",), blank=True)]
    stored = ws.put_bytes(make_cvr(rows))
    assert redacted_card_ids(ws, stored.sha256) == frozenset({"101-1-1", "101-1-2", "101-1-3"})
    junk = ws.put_bytes(b"not a cvr\r\n")
    assert redacted_card_ids(ws, junk.sha256) is None


def test_the_public_dashboard_shows_the_results(workspace: Path, make_client: Factory, tmp_path: Path) -> None:  # noqa: F811
    rr.clear_cache()
    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        fetch(store, log, election_files() | FILES)
        receive_cvr(store, log, "Weld", tmp_path)
        snap = build_snapshot(log, store, ["Weld"])
    assert [s.round for s in snap.round_results.standings] == [1, 2]
    client: TestClient = make_client(counties=("Weld",))
    page = client.get("/").text
    assert "CORLA's results, round by round" in page and "What continues, and why" in page
    assert "Jackson County Assessor" in page and "John Hickenlooper and Ryan Apelbaum (two choices)" in page
    # A card not in the redacted CVR is named by ID in the note on such cards, and nowhere else.
    assert "101-1-4" not in page and page.count("101-1-99") == 1
    assert "Overvotes read by the board where the CVR has no vote (a voter&#39;s overvote, entered as marked; CORLA counts no discrepancy):" in page
    assert "<code>101-1-5</code>, United States Senator (targeted contest (statewide)): CVR no vote; audit board A and B (two choices)" in page
    assert f"audit board John Hickenlooper and Ryan Apelbaum (two choices): {rr.BOARD_OVERVOTE_NOTE}" in page
    assert "Ranked-choice contest: compared as CORLA reports it, ranks not interpreted here:" in page
    assert "not in the county's redacted CVR, which leaves out ballots to protect voters' privacy, so not checked here and not shown: <code>101-1-99</code>" in page
    data = client.get("/dashboard.json").json()["round_results"]
    assert data["continuing"][0]["name"] == "Jackson County Assessor"
    assert client.get("/dashboard.json").json()["progress"][-1]["detail"].startswith("Round 2: 1 county (Jackson)")


# ---------------------------------------------------------------------------
# Results reports: the status at the end of a round

SUMMARY_HEADER = [
    "Contest", "targeted", "Winner", "Risk Limit met?", "Risk measurement %", "Audit Risk Limit %", "diluted margin %",
    "disc +2", "disc +1", "disc 0", "disc -1", "disc -2", "gamma", "audited sample count", "ballot count", "min margin",
]  # fmt: skip


def summary_row(name: str, met: str, risk: str, audited: int, one_over: int = 0, targeted: str = "Yes", one_under: int = 0) -> list[str]:
    return [name, targeted, "A", met, risk, "3.0", "20.0", "0", str(one_over), str(audited - one_over - one_under), str(one_under), "0",
            "1.03905000", str(audited), "100", "20"]  # fmt: skip


def results_report(rows: Sequence[Sequence[str]]) -> bytes:
    """A resultsReport.xlsx shaped as CORLA's: "Contests to Sheets", "Summary" (a title row, then the header), a per-contest sheet."""
    return make_xlsx({
        "Contests to Sheets": [["Contests to Sheets", "No Data"], ["Contest Name", "Sheet Name"], ["Jackson County Assessor", "Jackson County Assessor"]],
        "Summary": [["Summary", "No Data"], SUMMARY_HEADER, *rows],
        "Jackson County Assessor": [["imprinted id", "record type", "audit board", "discrepancy", "county"], ["102-8-9", "uploaded", "1", "1", "Jackson"]],
    })  # fmt: skip


ALL_MET_ROUND2 = results_report([
    summary_row("United States Senator", "Yes", "1.0", 25),
    summary_row("Weld County Council - At-Large", "Yes", "2.1", 14),
    summary_row("Morgan - Proposition 137 (STATUTORY)", "Yes", "2.5", 16),
    summary_row("Jackson County Assessor", "Yes", "2.0", 39, one_over=1),
    summary_row("Town of Nowhere Question 1", "No", "40.0", 3, targeted="No"),
])  # fmt: skip


def test_a_results_report_is_read_with_its_discrepancies_apart() -> None:
    report = rr.read_results_report(
        results_report([summary_row("Jackson County Assessor", "Yes", "2.0", 39, one_over=1, one_under=2)]), ref="r", round_number=2
    )
    assert report is not None
    (jackson,) = report.contests
    assert (jackson.targeted, jackson.met, jackson.risk, jackson.limit, jackson.audited) == (True, True, "2.0", "3.0", 39)
    # "disc +1" and "disc -1" are told apart.
    assert (jackson.one_over, jackson.one_under, jackson.two_over, jackson.zero) == (1, 2, 0, 36)
    # A state audit report's Summary, or no workbook at all, is not a results report.
    assert rr.read_results_report(make_xlsx({"Summary": [["Audit Random Seed", "123"]]}), ref="r", round_number=2) is None
    assert rr.read_results_report(b"not a workbook", ref="r", round_number=2) is None


def test_a_contest_finishing_in_round_two_and_the_final_standing(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files() | FILES | {"files/round2/resultsReport.xlsx": ALL_MET_ROUND2})
    results = round_results(build_map(log, ws), ws, {})
    texts = [s.text for s in results.standings]
    assert len(texts) == 3 and texts[1].startswith("After round 1 (RLA/2026/general/files/round2/contest.csv): 3 of 4")
    assert texts[2] == "After round 2 (RLA/2026/general/files/round2/resultsReport.xlsx): all 4 targeted contests met the risk limit; the audit is complete."
    assert results.standings[2].complete
    assert results.continuing == []
    (jackson,) = results.finished_later
    assert f"{jackson.name} ({jackson.counties}): {jackson.finished_text}" == (
        "Jackson County Assessor (Jackson): met the risk limit in round 2 (RLA/2026/general/files/round2/resultsReport.xlsx): "
        "measured risk 2.0% against the 3.0% limit, 39 cards audited, one one-vote overstatement."
    )
    # Why it went to round 2 is kept.
    assert jackson.round == 2 and jackson.estimate_text.startswith("CORLA's estimate of the cards it needs (estimated_samples_to_audit) rose from 32")


def test_a_contest_not_yet_met_and_one_a_later_report_leaves_out(ws: Workspace, log: EventLog) -> None:
    # Round 1's report shows every targeted contest; round 2's only the one audited in round 2, not yet met.
    round1 = results_report([
        summary_row("United States Senator", "Yes", "1.0", 25), summary_row("Weld County Council - At-Large", "Yes", "2.1", 14),
        summary_row("Morgan - Proposition 137 (STATUTORY)", "Yes", "2.5", 16), summary_row("Jackson County Assessor", "No", "4.0", 33, one_over=1),
    ])  # fmt: skip
    round2 = results_report([summary_row("Jackson County Assessor", "No", "3.5", 39, one_over=2)])
    fetch(ws, log, election_files() | FILES | {"files/round1/resultsReport.xlsx": round1, "files/round2/resultsReport.xlsx": round2})
    results = round_results(build_map(log, ws), ws, {})
    # Round 1's report says no more than round 2's contest list: no line of its own.
    assert [s.ref for s in results.standings][-1] == "RLA/2026/general/files/round2/resultsReport.xlsx" and len(results.standings) == 3
    assert results.standings[-1].text == (
        "After round 2 (RLA/2026/general/files/round2/resultsReport.xlsx): 3 of 4 targeted contests met the risk limit; "
        "1 has not: Jackson County Assessor (Jackson)."
    )
    assert [c.name for c in results.continuing] == ["Jackson County Assessor"] and results.finished_later == []
    rounds = audit_rounds(build_map(log, ws), ws)
    assert stage("Jackson", rounds, seed_source="s", round_start=None).label == "Round 2"
    # The other counties' contests met the limit in round 1's report and stay met, though round 2's leaves them out.
    weld = stage("Weld", rounds, seed_source="s", round_start=None)
    assert weld.finished and weld.complete


def test_the_stage_and_the_summary_once_every_contest_met_the_limit(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS) | FILES | {"files/round2/resultsReport.xlsx": ALL_MET_ROUND2})
    datamap = build_map(log, ws)
    rounds = audit_rounds(datamap, ws)
    assert [r.round for r in rounds.results_reports] == [2]
    jackson = stage("Jackson", rounds, seed_source="s", round_start=None)
    assert jackson.label == "Round 2 finished: its targeted contests met the risk limit" and jackson.round == 2
    assert jackson.finished and jackson.complete and "round2/resultsReport.xlsx" in jackson.source
    assert stage("Weld", rounds, seed_source="s", round_start=None).label == "Round 1 finished: its targeted contests met the risk limit"
    statuses = county_statuses(status(log), corla_uploads(datamap, ws), rounds, seed_source="s")
    covered = {c: s for c, s in statuses.items() if c in ("Adams", "Jackson", "Morgan", "Weld")}
    steps = audit_progress(contests_posted=True, corla=corla_uploads(datamap, ws), seed=True, selections=True, statuses=covered)
    assert steps[-1].detail == "All counties finished: every targeted contest met the risk limit" and steps[-1].state == "done"


def test_the_public_dashboard_shows_a_contest_finished_in_a_later_round(workspace: Path, make_client: Factory) -> None:  # noqa: F811
    rr.clear_cache()
    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        fetch(store, log, election_files() | FILES | {"files/round2/resultsReport.xlsx": ALL_MET_ROUND2})
    client: TestClient = make_client(counties=("Weld",))
    page = client.get("/").text
    assert "the audit is complete." in page and "Finished in a later round" in page and "No targeted contest continues." in page
    assert "Jackson County Assessor (Jackson): met the risk limit in round 2" in page and "Why it went to round 2" in page
    data = client.get("/dashboard.json").json()
    assert data["round_results"]["continuing"] == [] and data["round_results"]["finished_later"][0]["name"] == "Jackson County Assessor"
