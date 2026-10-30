"""Fast tests for corla_observer.estimate: the sample-count estimates and their inputs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from corla_observer.datamap import build_map
from corla_observer.election import parse_setup
from corla_observer.enr import load_results, record_results_file
from corla_observer.datamap import OVERRIDES_FILENAME, OverridesError, apply_overrides, parse_overrides
from corla_observer.estimate import (
    check_counts,
    corla_contest_counts,
    estimate_contests,
    estimate_history,
    record_estimates,
)
from corla_observer.events import EventLog
from corla_observer.predict import predict, round_one_sample_count
from corla_observer.seeds import enter_and_confirm
from corla_observer.workspace import Workspace
from synthetic import CONTEST_IDS, clarity_detail, contests_list, election_files
from test_datamap import ACTOR, fetch

SEED = "12345678901234567890"
SETUP = parse_setup(
    'format = 1\nelection = "2026/general"\n[results]\nfinal_after = "2026-11-13 14:00"\n'
    '[estimates]\nlow_margin_factor_preliminary = 1.5\nlow_margin_factor_final = 1.1\n'
    '[cards_per_ballot]\nAdams = { value = 2, source = "test" }\n'
)
#: Results with the target list's contests; Adams's clerk race is not in the state's results.
RESULTS = clarity_detail(
    {"Governor - Democratic Party (Vote For 1)": {"A": {"Adams": 30, "Boulder": 30, "Weld": 30}, "B": {"Adams": 20, "Boulder": 20, "Weld": 30}}},
    {"Adams": 25, "Boulder": 50, "Weld": 65},
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def add_results(ws: Workspace, log: EventLog, data: bytes = RESULTS, time: str = "2026-11-04T05:00:00.000Z") -> None:
    record_results_file(ws, log, data, url="https://results.enr.clarityelections.com/CO/1/2/reports/detailxml.zip",
                        scope="state", what="detail", election_id="1", version="2", simulated={"source": "test", "time": time})  # fmt: skip


def estimates(ws: Workspace, log: EventLog) -> dict[str, object]:
    return {e.name: e for e in estimate_contests(build_map(log, ws), setup=SETUP, results=load_results(ws, log), log=log)}


def test_target_list_only(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None, files_Boulder_csv=None, files_Weld_csv=None))
    found = estimates(ws, log)
    governor = found["Governor - Democratic Party"]
    # Target list: margin 30, denominator 30 / 0.2 = 150.
    assert governor.margin == 30 and governor.denominator == 150  # type: ignore[attr-defined]
    assert "target contest list" in governor.margin_source and "target list's denominator" in governor.denominator_source  # type: ignore[attr-defined]
    assert governor.central == round_one_sample_count("0.03", 30, 150)  # type: ignore[attr-defined]
    assert governor.low == round_one_sample_count("0.03", 45, 150) < governor.central  # type: ignore[attr-defined]


def test_results_before_manifests_then_manifests(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None, files_Boulder_csv=None, files_Weld_csv=None))
    add_results(ws, log)
    governor = estimates(ws, log)["Governor - Democratic Party"]
    # Statewide over every county but San Juan; margin 90 - 70; cards: Adams 25 x 2, the others x 1.
    assert governor.margin == 20 and governor.denominator == 50 + 50 + 65 and governor.low_denominator == 140  # type: ignore[attr-defined]
    assert "Adams 2 cards per ballot (test)" in governor.cards_note and "ESTIMATED" in governor.denominator_source  # type: ignore[attr-defined]
    assert governor.low == round_one_sample_count("0.03", 30, 140)  # type: ignore[attr-defined]
    clerk = estimates(ws, log)["Adams County Clerk - Republican Party (Adams)"]
    assert "target contest list" in clerk.margin_source and clerk.denominator == 50  # type: ignore[attr-defined]
    # Final results (generated after final_after): the narrower low factor.
    final = clarity_detail(
        {"Governor - Democratic Party (Vote For 1)": {"A": {"Adams": 30, "Boulder": 30, "Weld": 30}, "B": {"Adams": 20, "Boulder": 20, "Weld": 30}}},
        {"Adams": 25, "Boulder": 50, "Weld": 65}, timestamp="11/13/2026 3:00:00 PM MST",
    )  # fmt: skip
    add_results(ws, log, final, time="2026-11-13T22:00:00.000Z")
    assert estimates(ws, log)["Governor - Democratic Party"].low_margin_factor == 1.1  # type: ignore[attr-defined]


def test_manifests_give_the_exact_denominator(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    governor = estimates(ws, log)["Governor - Democratic Party"]
    assert governor.denominator == 165 and governor.low_denominator == 165  # type: ignore[attr-defined]
    clerk = estimates(ws, log)["Adams County Clerk - Republican Party (Adams)"]
    assert clerk.denominator == 50 and "exact" in clerk.denominator_source  # type: ignore[attr-defined]


def apply(ws: Workspace, log: EventLog, text: str) -> None:
    (ws.root / OVERRIDES_FILENAME).write_text("format = 1\n" + text, encoding="utf-8")
    result = apply_overrides(ws, log, ACTOR)
    assert result.applied, result.errors


#: The target list with Adams noted as two cards per ballot, as the Secretary of State writes it.
TARGETS_WITH_REMARKS = (
    b"County,Contest,Vote For,Winner,Loser,Contest Margin,Diluted Margin,Risk Limit,"
    b"Estimated # of CVRs to audit,# of CVRs,Remarks\r\n"
    b"Colorado,Governor - Democratic Party,1,90,60,30,0.2,0.03,40,165,All counties\r\n"
    b"Adams,Adams County Clerk - Republican Party,1,30,10,20,0.4,0.03,20,50,3 ballot cards per ballot*\r\n"
)


def test_a_hand_margin_ranks_below_every_published_one(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    add_results(ws, log)
    clerk = "Adams County Clerk - Republican Party (Adams)"
    apply(
        ws, log,
        f'[[estimate]]\ncontest = "{clerk}"\nmargin = 12\nreason = "Adams County\'s results page, 4 Nov 9 PM"\n\n'
        '[[contest]]\nname = "Weld Measure 1A"\ncounties = ["Weld"]\nreason = "targeted by the county"\n\n'
        '[[estimate]]\ncontest = "Weld Measure 1A"\nwinner_votes = 40\nloser_votes = 25\nreason = "the county\'s page"\n',
    )  # fmt: skip
    found = estimates(ws, log)
    # The target list's margin (the clerk's race is not in the state's results) ranks above the hand entry.
    assert found[clerk].margin == 20 and "target contest list" in found[clerk].margin_source  # type: ignore[attr-defined]
    assert any("the margin supplied by hand (12" in n and "is not used" in n for n in found[clerk].notes)  # type: ignore[attr-defined]
    # A contest no published source has a margin for takes the hand entry, as an estimate.
    measure = found["Weld Measure 1A"]
    assert measure.margin == 15 and "supplied by hand in [[estimate]] #2" in measure.margin_source  # type: ignore[attr-defined]
    assert "ESTIMATED" in measure.margin_source and measure.denominator == 65  # type: ignore[attr-defined]
    # CORLA's contest list replaces both the target list and the hand entry.
    fetch(ws, log, election_files() | {"finalReports/ContestsListRound1.csv": contests_list(), "finalReports/ContestIDsByCounty.csv": CONTEST_IDS})
    later = estimates(ws, log)
    county = next(e for e in later.values() if "Clerk" in e.name)  # type: ignore[attr-defined]
    assert county.margin_source == "CORLA's contest list (min_margin)"  # type: ignore[attr-defined]
    assert "CORLA's contest list (ballot_card_count)" in county.denominator_source  # type: ignore[attr-defined]
    assert any("is not used: CORLA's contest list (min_margin) ranks above it" in n for n in county.notes)  # type: ignore[attr-defined]


def test_cards_per_ballot_and_county_inputs_by_rank(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None, files_Boulder_csv=None, files_Weld_csv=None, files_targetContests_csv=TARGETS_WITH_REMARKS))
    datamap = build_map(log, ws)
    assert datamap.target_cards == {"Adams": (3, "3 ballot cards per ballot*")}
    add_results(ws, log)
    # The target list's remark (3) ranks above a hand-supplied number and the setup's (2).
    apply(ws, log, '[[county_estimate]]\ncounty = "Adams"\ncards_per_ballot = 4\nreason = "the county"\n\n'
                   '[[county_estimate]]\ncounty = "Boulder"\ncards_per_ballot = 2\nreason = "the county says 2"\n')  # fmt: skip
    governor = estimates(ws, log)["Governor - Democratic Party"]
    assert governor.denominator == 25 * 3 + 50 * 2 + 65  # type: ignore[attr-defined]
    assert "Adams 3 cards per ballot (the target list's remarks)" in governor.cards_note  # type: ignore[attr-defined]
    assert "Boulder 2 cards per ballot (supplied by hand in [[county_estimate]] #2: the county says 2)" in governor.cards_note  # type: ignore[attr-defined]
    # Once a county's manifest is in, its manifest total is used for it.
    fetch(ws, log, election_files(files_Boulder_csv=None, files_Weld_csv=None, files_targetContests_csv=TARGETS_WITH_REMARKS))
    governor = estimates(ws, log)["Governor - Democratic Party"]
    assert governor.denominator == 50 + 50 * 2 + 65 and "1 county from its manifests" in governor.cards_note  # type: ignore[attr-defined]


def test_hand_ballots_cast_without_results(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None, files_Boulder_csv=None, files_Weld_csv=None))
    apply(ws, log, '[[county_estimate]]\ncounty = "Adams"\nballots_cast = 40\nreason = "the county\'s turnout page"\n')
    clerk = estimates(ws, log)["Adams County Clerk - Republican Party (Adams)"]
    assert clerk.denominator == 80 and clerk.denominator_source.startswith("ballots cast or ballot cards supplied by hand")  # type: ignore[attr-defined]
    assert "Adams 40 ballots cast (supplied by hand" in clerk.cards_note  # type: ignore[attr-defined]
    # The state's results rank above it.
    add_results(ws, log)
    clerk = estimates(ws, log)["Adams County Clerk - Republican Party (Adams)"]
    assert clerk.denominator == 50 and "the state's results" in clerk.denominator_source  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('[[estimate]]\ncontest = "X"\nreason = "r"\n', "give at least one of margin"),
        ('[[estimate]]\ncontest = "X"\nmargin = 5\nwinner_votes = 9\nloser_votes = 4\nreason = "r"\n', "not both"),
        ('[[estimate]]\ncontest = "X"\nwinner_votes = 4\nloser_votes = 9\nreason = "r"\n', "more than"),
        ('[[estimate]]\ncontest = "X"\nmargin = 0\nreason = "r"\n', "positive"),
        ('[[county_estimate]]\ncounty = "Weld"\ncards_per_ballot = 0.5\nreason = "r"\n', "at least 1"),
        ('[[county_estimate]]\ncounty = "Weld"\nreason = "r"\n', "give at least one of ballots_cast"),
        ('[[county_estimate]]\ncounty = "Weld"\nballots_cast = 9\n', "reason"),
    ],
)
def test_hand_inputs_are_checked(text: str, error: str) -> None:
    with pytest.raises(OverridesError, match=error):
        parse_overrides("format = 1\n" + text)


def test_corla_count_is_exact_and_estimates_are_recorded_on_change(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    add_results(ws, log)
    first = estimate_contests(build_map(log, ws), setup=SETUP, results=load_results(ws, log), log=log)
    assert record_estimates(log, first, setup=SETUP, results=load_results(ws, log)) is not None
    assert record_estimates(log, first, setup=SETUP, results=load_results(ws, log)) is None
    fetch(ws, log, election_files() | {"finalReports/ContestsListRound1.csv": contests_list(), "finalReports/ContestIDsByCounty.csv": CONTEST_IDS})
    later = estimate_contests(build_map(log, ws), setup=SETUP, results=load_results(ws, log), log=log)
    governor = next(e for e in later if e.statewide)
    assert governor.exact and governor.central == governor.low == 9 and governor.from_results is not None
    assert record_estimates(log, later, setup=SETUP, results=load_results(ws, log)) is not None
    history = estimate_history(log)
    assert [p.corla_count for p in history["Governor - DEM"]] == [9]
    checks = check_counts(first, corla_contest_counts(contests_list()))
    assert {c.corla_name for c in checks} == {"Governor - DEM", "Adams County Clerk - REP"}


def test_prediction_carries_the_low_count(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    enter_and_confirm(log, SEED, ACTOR, "test")
    datamap = build_map(log, ws)
    found = {e.name: e for e in estimate_contests(datamap, setup=SETUP, results=load_results(ws, log), log=log)}
    prediction = predict(datamap, estimates=found)
    governor = prediction.contests[0]
    low = found[governor.name].low
    assert governor.estimated and low is not None and governor.low_count == low < governor.sample_count
    contests = prediction.to_dict()["contests"]
    assert isinstance(contests, list) and isinstance(contests[0], dict) and contests[0]["low_count"] == low


def test_cli(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from corla_observer.cli import app

    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        fetch(ws, log, election_files())
        add_results(ws, log)
    root = str(ws.root)
    runner = CliRunner()
    shown = runner.invoke(app, ["estimate", "show", "--workspace", root])
    assert shown.exit_code == 0, shown.output
    assert "Governor - Democratic Party: central" in shown.output and "state's results" in shown.output
    # There is no command to enter a margin: a hand input goes in the overrides file.
    assert runner.invoke(app, ["estimate", "margin", "--workspace", root]).exit_code == 2
    assert "recorded" in runner.invoke(app, ["estimate", "record", "--workspace", root]).output
    assert "unchanged" in runner.invoke(app, ["estimate", "record", "--workspace", root]).output
    listed = runner.invoke(app, ["results", "show", "--workspace", root])
    assert "state" in listed.output and "version 2" in listed.output
