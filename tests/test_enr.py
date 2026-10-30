"""Fast tests for corla_observer.enr (election night results) and corla_observer.tally (CVR margins)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from corla_observer.election import GENERAL_2026_SETUP, parse_setup
from corla_observer.enr import (
    ResultsError,
    contest_votes,
    discover_election_id,
    find_contest,
    gather,
    load_results,
    margin_of,
    parse_settings,
    read_results,
)
from corla_observer.events import EventLog
from corla_observer.fetch import FetchPolicy, FetchResult
from corla_observer.tally import cvr_margin, tally_cvr
from corla_observer.workspace import Workspace
from synthetic import Ballot, clarity_detail, clarity_summary, make_cvr, make_xlsx

DETAIL = clarity_detail(
    {
        "Governor (Vote For 1)": {"A": {"Adams": 60, "Weld": 30, "San Juan": 5}, "B": {"Adams": 40, "Weld": 35, "San Juan": 1}},
        "Council (Vote For 2)": {"X": {"Adams": 50}, "Y": {"Adams": 40}, "Z": {"Adams": 10}},
    },
    {"Adams": 110, "Weld": 70, "San Juan": 7},
    vote_for={"Council (Vote For 2)": 2},
)


def test_detail_xml() -> None:
    results = read_results(DETAIL)
    assert results.ballots_cast == {"Adams": 110, "Weld": 70, "San Juan": 7}
    governor = find_contest(results, "Governor")
    assert governor is not None and governor.totals == {"A": 95, "B": 76}
    assert margin_of(governor.totals, 1) == 19
    assert margin_of(contest_votes(governor, ["Weld"], True), 1) == 5
    assert margin_of(contest_votes(governor, ["Adams", "Weld"], True), 1) == 15
    council = find_contest(results, "Council (Adams)")
    assert council is not None and council.vote_for == 2 and margin_of(council.totals, 2) == 30
    assert read_results(clarity_detail({"G": {"A": {"Adams": 1}}}, {"Adams": 1}, zipped=False)).contests[0].name == "G"


def test_detail_xml_refuses_a_doctype() -> None:
    with pytest.raises(ResultsError):
        read_results(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><ElectionResult/>')


def test_detail_xlsx() -> None:
    book = make_xlsx(
        {
            "Table of Contents": [["Table of Contents"]],
            "Registered Voters": [["County", "Registered Voters", "Ballots Cast", "Voter Turnout"], ["Adams", "200", "110", "55%"]],
            "2": [
                ["Governor - Democratic Party (Vote For 1)"],
                ["", "", "A", "", "B", ""],
                ["County", "Registered Voters", "Total Votes", "Total Votes", "Total Votes", "Total Votes", "Total"],
                ["Adams", "200", "60", "60", "40", "40", "100"],
                ["Weld", "100", "30", "30", "35", "35", "65"],
            ],
        }
    )
    results = read_results(book)
    assert results.ballots_cast == {"Adams": 110}
    governor = find_contest(results, "Governor - DEM (Weld)")
    assert governor is not None and margin_of(contest_votes(governor, ["Weld"], True), 1) == 5


def test_county_summary() -> None:
    summary = read_results(clarity_summary({"Weld County Clerk - Republican Party (Vote For 1)": {"P": 70, "Q": 20}}), "Weld")
    clerk = find_contest(summary, "Weld County Clerk - REP")
    assert clerk is not None and margin_of(contest_votes(clerk, ["Weld"], summary.areas_are_counties), 1) == 50


def test_discovery_and_settings() -> None:
    page = b'<a href="https://results.enr.clarityelections.com/CO/130001/web.345678/#/summary">results</a>'
    assert discover_election_id(page) == "130001"
    assert discover_election_id(b"<p>nothing yet</p>") is None
    import gzip
    import json

    settings = {"settings": {"electiondetails": {"participatingcounties": ["La_Plata|130010|400001|t|16", "Weld|130020|400002|t|16"]}}}
    assert parse_settings(gzip.compress(json.dumps(settings).encode())) == {"La Plata": ("130010", "400001"), "Weld": ("130020", "400002")}


class FakeClarity:
    """Serves Clarity's files from a dict, and records the URLs requested."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.policy = FetchPolicy(allowed_hosts=frozenset({"results.enr.clarityelections.com"}))
        self.requested: list[str] = []

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        self.requested.append(url)
        body = self.files.get(url)
        return FetchResult(url, url, 200 if body is not None else 404, body or b"", {})


def test_gather_fetches_only_what_changed(tmp_path: Path) -> None:
    import gzip
    import json

    base = "https://results.enr.clarityelections.com/CO/"
    settings = {"settings": {"electiondetails": {"participatingcounties": ["Weld|200|7|t|16"]}}}
    site = FakeClarity(
        {
            base + "130100/current_ver.txt": b"5\n",
            base + "130100/5/reports/detailxml.zip": DETAIL,
            base + "130100/5/json/en/electionsettings.json": gzip.compress(json.dumps(settings).encode()),
            base + "Weld/200/7/json/en/summary.json": clarity_summary({"Weld Clerk": {"P": 9, "Q": 2}}),
        }
    )
    discover = FakeClarity({"https://www.coloradosos.gov/pubs/elections/resultsData.html": b'href="https://results.enr.clarityelections.com/CO/130100/web/"'})
    setup = parse_setup(GENERAL_2026_SETUP)
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        report = gather(ws, log, setup, counties=["Weld"], fetcher=site, discover_fetcher=discover)  # type: ignore[arg-type]
        assert report.election_id == "130100" and report.version == "5" and report.changed
        assert len(site.requested) == 4
        again = gather(ws, log, setup, counties=["Weld"], fetcher=site, discover_fetcher=discover)  # type: ignore[arg-type]
        assert not again.changed and site.requested[4:] == [base + "130100/current_ver.txt"]
        loaded = load_results(ws, log)
        assert set(loaded) == {"state", "Weld"} and loaded["state"][1].version == "5"
        kinds = [e.kind for e in log.iter_events()]
    assert kinds.count("results.fetched") == 5 and kinds.count("results.discovered") == 1
    # No election ID anywhere: nothing is fetched from the results site.
    with EventLog.open(tmp_path / "other") as log2:
        nothing = gather(Workspace.open(tmp_path / "other"), log2, setup, fetcher=site, discover_fetcher=FakeClarity({}))  # type: ignore[arg-type]
    assert nothing.election_id is None and "election_id" in nothing.notes[-1]


def test_cvr_tally_counts_aggregate_rows(tmp_path: Path) -> None:
    rows = [Ballot(("Mayor (Vote For=1)",)) for _ in range(5)] + [Ballot(("Mayor (Vote For=1)",), marker="*")]
    data = make_cvr(rows)
    # An aggregate row, as the anonymizer writes one: the combined counts in the vote cells.
    lines = data.decode().splitlines()
    aggregate = lines[-1].split(",")
    aggregate[0] = "Redacted and Consolidated 3 Ballots"
    head = len(aggregate) - 9
    aggregate[head : head + 2] = ["1", "2"]
    path = tmp_path / "cvr.csv"
    path.write_text("\r\n".join(lines + [",".join(aggregate)]) + "\r\n")
    tally = tally_cvr(path)
    mayor = tally.find("Mayor")
    assert mayor is not None
    assert mayor.votes == {"Ann Able": 6, "Bob Baker": 2}
    assert (mayor.redacted_rows, mayor.aggregate_rows) == (0, 1)
    assert cvr_margin(mayor) == 4
