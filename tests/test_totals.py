"""Fast tests for a results file uploaded by hand (corla_observer.enr.receive_results) and the totals check (corla_observer.totals)."""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.datamap import build_map
from corla_observer.election import parse_setup
from corla_observer.enr import (
    ResultsError,
    load_results,
    parse_clarity_time,
    read_results,
    receive_results,
    record_results_file,
    results_file_time,
)
from corla_observer.estimate import estimate_contests
from corla_observer.events import EventLog
from corla_observer.flags import check_flags
from corla_observer.totals import check_totals, last_totals_check, record_totals_check
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import CONTEST_IDS, clarity_detail, clarity_summary, election_files, make_xlsx
from test_datamap import fetch

ACTOR = "coordinator:alice"
STATE_URL = "https://results.enr.clarityelections.com/CO/1/9/reports/detailxml.zip"
SETUP = parse_setup('format = 1\nelection = "2026/general"\n[cards_per_ballot]\nAdams = { value = 1, source = "test" }\n')


def results(adams: tuple[int, int], san_juan: tuple[int, int], cast: dict[str, int], timestamp: str) -> bytes:
    """The Governor's race: Adams as given, Boulder 30-20, Weld 40-20, San Juan as given."""
    return clarity_detail(
        {"Governor - Democratic Party (Vote For 1)": {
            "A": {"Adams": adams[0], "Boulder": 30, "Weld": 40, "San Juan": san_juan[0]},
            "B": {"Adams": adams[1], "Boulder": 20, "Weld": 20, "San Juan": san_juan[1]},
        }},
        cast, timestamp=timestamp,
    )  # fmt: skip


#: The newer file (13 Nov): Adams has 2 more votes for A, and 52 ballots cast to its manifest's 50.
NEWER = results((32, 20), (5, 1), {"Adams": 52, "Boulder": 50, "Weld": 65, "San Juan": 7}, "11/13/2026 3:00:00 PM MST")
#: The older file (10 Nov), which agrees with CORLA's tallies and the manifests.
OLDER = results((30, 20), (4, 1), {"Adams": 50, "Boulder": 50, "Weld": 65, "San Juan": 6}, "11/10/2026 2:30:00 PM MST")
VOTE_TOTALS = (
    b"county_name,contest_name,choice,votes\r\n"
    b"Adams,Governor - DEM,A,30\r\nAdams,Governor - DEM,B,20\r\n"
    b"Boulder,Governor - DEM,A,30\r\nBoulder,Governor - DEM,B,20\r\n"
    b"Weld,Governor - DEM,A,40\r\nWeld,Governor - DEM,B,20\r\n"
)
CONTESTS_LIST = (
    b"contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,"
    b"contest_ballot_card_count,winners,min_margin,risk_limit,audited_sample_count\r\n"
    b'Governor - DEM,state_wide_contest,in_progress,1,165,150,"[""A""]",40,0.03,9\r\n'
)


def weld_cvr() -> bytes:
    """Weld's redacted CVR: a row for each of its 65 ballots, 39 for A, 16 for B, and 10 whose votes are blank."""
    lines = [
        "2026 Synthetic General Election,5.17.17.1,,,,,,,,",
        ",,,,,,,,Governor - Democratic Party (Vote For=1),Governor - Democratic Party (Vote For=1)",
        ",,,,,,,,A,B",
        "CvrNumber,TabulatorNum,BatchId,RecordId,ImprintedId,CountingGroup,PrecinctPortion,BallotType,,",
    ]
    for n in range(1, 66):
        votes = "1,0" if n <= 39 else ("0,1" if n <= 55 else "0,0")
        lines.append(f"{n},7,1,{n},7-1-{n},Mail,101,BT-1,{votes}")
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def zipped(name: str, data: bytes, when: tuple[int, int, int, int, int, int] = (2026, 11, 13, 18, 21, 16)) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(zipfile.ZipInfo(name, when), data)
    return out.getvalue()


SUMMARY_CSV = (
    b'"line number","contest name","choice name","party name","total votes","percent of votes","registered voters","ballots cast"\r\n'
    b'1,"Governor - Democratic Party (Vote For 1)","A","1",107,60.0,1,1\r\n'
    b'2,"Governor - Democratic Party (Vote For 1)","B","2",61,40.0,1,1\r\n'
)


def test_times_and_forms() -> None:
    assert parse_clarity_time("7/23/2026 4:21:16 PM MDT") == "2026-07-23T22:21:16.000Z"
    assert parse_clarity_time("11/4/2026 9:40 PM") == "2026-11-05T04:40:00.000Z"  # Colorado time when no zone is named
    assert parse_clarity_time("not a time") is None
    detail = read_results(NEWER)
    assert detail.form == "detailxml.zip" and detail.generated == "2026-11-13T22:00:00.000Z"
    # summary.zip: totals only, timed by its member's date, Clarity's Eastern time (18:21 EST is 16:21 MST).
    summary = read_results(zipped("summary.csv", SUMMARY_CSV), "state")
    assert summary.form == "summary.zip" and not summary.areas_are_counties and summary.contests[0].totals == {"A": 107, "B": 61}
    assert summary.generated == "2026-11-13T23:21:16.000Z"
    assert read_results(SUMMARY_CSV).form == "summary.csv"
    # detail.xlsx: its creation time is Eastern wall-clock time, though marked Z.
    book = make_xlsx({"Registered Voters": [["County", "Registered Voters", "Ballots Cast"], ["Adams", "9", "5"]],
                      "2": [["G (Vote For 1)"], ["", "", "A", ""], ["County", "Registered Voters", "Total Votes", "Total"], ["Adams", "9", "5", "5"]]})  # fmt: skip
    buffer = io.BytesIO(book)
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("docProps/core.xml", '<cp:coreProperties><dcterms:created xsi:type="dcterms:W3CDTF">2026-07-23T18:21:16Z</dcterms:created></cp:coreProperties>')
    book = buffer.getvalue()
    assert results_file_time(book)[0] == "2026-07-23T22:21:16.000Z"
    assert results_file_time(zipped("detail.xlsx", book))[0] == "2026-07-23T22:21:16.000Z"
    assert read_results(clarity_summary({"C": {"X": 1}}), "Weld").generated is None
    with pytest.raises(ResultsError, match="not a Clarity results file"):
        read_results(b"County,Tabulator,Batch\r\n")
    with pytest.raises(ResultsError, match="not a Clarity results file"):
        read_results(zipped("notes.txt", b"hello"))


def test_receive_results_identifies_and_records(ws: Workspace, log: EventLog) -> None:
    with pytest.raises(ResultsError, match="source URL or a note"):
        receive_results(ws, log, NEWER, "detailxml.zip", ACTOR)
    with pytest.raises(ResultsError, match="scope"):
        receive_results(ws, log, NEWER, "detailxml.zip", ACTOR, scope="Atlantis", note="x")
    bad = receive_results(ws, log, b"County,Tabulator,Batch\r\n", "manifest.csv", ACTOR, note="by mistake")
    assert not bad.accepted and "not a Clarity results file" in str(bad.reason) and bad.sha256 is not None
    unscoped = receive_results(ws, log, clarity_summary({"Weld Clerk": {"P": 9, "Q": 2}}), "summary.json", ACTOR, note="x")
    assert not unscoped.accepted and "does not say whose" in str(unscoped.reason)
    wrong = receive_results(ws, log, NEWER, "detailxml.zip", ACTOR, scope="Weld", note="x")
    assert not wrong.accepted and "it is the state's" in str(wrong.reason)
    assert not any(e.kind == "results.uploaded" for e in log.iter_events())
    assert [e.kind for e in log.iter_events()].count("results.upload_rejected") == 3
    assert not ws.has(bad.sha256)

    good = receive_results(ws, log, OLDER, "detailxml.zip", ACTOR, source_url="https://results.enr.clarityelections.com/CO/1/", note="fetch blocked")
    assert good.accepted and good.scope == "state" and good.form == "detailxml.zip" and good.generated == "2026-11-10T21:30:00.000Z"
    county = receive_results(ws, log, clarity_summary({"Weld Clerk": {"P": 9, "Q": 2}}), "summary.json", ACTOR, scope="weld",
                             note="Weld's page", file_time="2026-11-10 21:40")  # fmt: skip
    assert county.accepted and county.scope == "Weld" and county.generated == "2026-11-11T04:40:00.000Z" and "as given" in county.generated_basis
    event = next(e for e in log.iter_events() if e.kind == "results.uploaded")
    assert event.actor == ACTOR and event.data["uploaded"] is True and event.data["note"] == "fetch blocked" and event.file_sha256 == good.sha256
    loaded = load_results(ws, log)
    assert loaded["state"][1].uploaded and loaded["state"][1].origin == f"uploaded by hand by {ACTOR}"
    assert loaded["Weld"][0].contests[0].name == "Weld Clerk"


def test_newer_file_wins_and_a_tie_goes_to_the_fetched_one(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    record_results_file(ws, log, OLDER, url=STATE_URL, scope="state", what="detail", election_id="1", version="9")
    receive_results(ws, log, NEWER, "detailxml.zip", ACTOR, note="downloaded by hand: the fetch was blocked")
    assert load_results(ws, log)["state"][1].uploaded
    governor = next(e for e in estimate_contests(build_map(log, ws), results=load_results(ws, log), log=log) if e.statewide)
    # Over the counties in CORLA's audit: San Juan's votes are left out, and the note says so.
    assert governor.margin == 102 - 60 and "uploaded by hand by coordinator:alice" in governor.margin_source
    assert any(n.startswith("left out: San Juan's votes (A 5, B 1)") and "would be 46" in n for n in governor.notes)
    # The same file fetched later, with the same generated time: the fetched one is used.
    record_results_file(ws, log, NEWER, url=STATE_URL, scope="state", what="detail", election_id="1", version="10")
    assert not load_results(ws, log)["state"][1].uploaded
    # An upload of a different file generated at the same time as the fetched one: still the fetched one.
    same_time = results((31, 20), (5, 1), {"Adams": 52, "Boulder": 50, "Weld": 65, "San Juan": 7}, "11/13/2026 3:00:00 PM MST")
    receive_results(ws, log, same_time, "detailxml.zip", ACTOR, note="x")
    assert not load_results(ws, log)["state"][1].uploaded
    lines = [d for d in check_totals(ws, log, build_map(log, ws), SETUP).differences if d.check == "results" and d.county == "Adams"]
    assert lines and lines[0].flag and "32–20" in lines[0].line and "31–20" in lines[0].line  # same time, different numbers


def test_totals_check(ws: Workspace, log: EventLog) -> None:
    files = election_files(files_uploadStatus_csv=None) | {
        "finalReports/ContestIDsByCounty.csv": CONTEST_IDS,
        "finalReports/ContestsListRound1.csv": CONTESTS_LIST,
        "finalReports/CandidateVoteTotalsByCounty.csv": VOTE_TOTALS,
    }
    fetch(ws, log, files)
    cvr = receive(ws, log, "Weld", "redacted_cvr", io.BytesIO(weld_cvr()), "weld.csv", ACTOR)
    assert cvr.accepted, cvr.report
    receive_results(ws, log, OLDER, "detailxml.zip", ACTOR, note="downloaded on 10 Nov")
    record_results_file(ws, log, NEWER, url=STATE_URL, scope="state", what="detail", election_id="1", version="9")
    datamap = build_map(log, ws)
    seq = record_totals_check(ws, log, datamap, SETUP)
    assert seq is not None and record_totals_check(ws, log, datamap, SETUP) is None  # unchanged inputs
    found = last_totals_check(log)
    assert found is not None
    diffs = [d for d in found[2]["differences"] if isinstance(d, dict)]  # type: ignore[union-attr]
    by = {(d["check"], d["county"]): d for d in diffs}
    # San Juan: not in CORLA's audit, left out (information).
    excluded = by[("excluded", "San Juan")]
    assert not excluded["flag"] and "5–1 (margin 4)" in str(excluded["line"])
    # Adams: 2 more votes in the newer results than in CORLA's tally (a flag), which the older file matched.
    adams = by[("corla_votes", "Adams")]
    assert adams["flag"] and "32–20 (margin 12)" in str(adams["line"]) and "30–20 (margin 10)" in str(adams["line"])
    assert "2 more votes than CORLA's tally" in str(adams["line"]) and "uploaded by hand by coordinator:alice) agreed" in str(adams["line"])
    # The newer file against the older: information only.
    newer = next(d for d in diffs if d["check"] == "results" and d["county"] == "Adams" and d["contest"])
    assert not newer["flag"] and "in the newer file" in str(newer["line"])
    # CORLA's min_margin 40 against 42 over its counties: accounted for by Adams.
    margin = by[("corla_margin", None)]
    assert not margin["flag"] and "42, vs CORLA's min_margin 40 (+2): the county differences above account for it" in str(margin["line"])
    # Weld's CVR has every ballot, with votes blanked by redaction: explained.
    weld = by[("cvr", "Weld")]
    assert not weld["flag"] and "39–16" in str(weld["line"]) and "blanked by its redaction" in str(weld["line"])
    # Adams's manifest: 50 cards for 52 ballots cast at one card per ballot (a flag).
    manifest = by[("manifest", "Adams")]
    assert manifest["flag"] and "50 ballot cards vs 52 ballots cast" in str(manifest["line"])
    assert ("manifest", "Weld") not in by and ("cvr", "Boulder") not in by
    flags = [f for f in check_flags(log, ws, datamap) if f.reason == "totals_differ"]
    assert {f.counties for f in flags} == {("Adams",)} and all(f.severity == "normal" for f in flags)


def test_results_add_command(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    Workspace.open(root)
    path = tmp_path / "detailxml.zip"
    path.write_bytes(OLDER)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["results", "add", str(path), "--workspace", str(root), "--actor", ACTOR, "--note", "fetch blocked"])
    assert result.exit_code == 0, result.output
    assert "recorded (event" in result.output and "state's detailxml.zip" in result.output
    shown = runner.invoke(cli_app, ["results", "show", "--workspace", str(root)])
    assert "uploaded by hand by coordinator:alice" in shown.output
    (tmp_path / "x.json").write_bytes(gzip.compress(json.dumps([{"C": "Clerk", "CH": ["P"], "V": [3]}]).encode()))
    refused = runner.invoke(cli_app, ["results", "add", str(tmp_path / "x.json"), "--workspace", str(root), "--actor", ACTOR, "--note", "x"])
    assert refused.exit_code == 1 and "does not say whose" in refused.output
    checked = runner.invoke(cli_app, ["results", "check", "--workspace", str(root)])
    assert checked.exit_code == 0, checked.output
