"""Fast tests for corla_observer.county_status: each county's status in the audit and in observation.

A small synthetic election (tests/synthetic.py) is fetched into a temporary
workspace through `corla_observer.fetch.fetch_urls` with a fake fetcher,
with CORLA's upload status, and counties' files are received through
`corla_observer.uploads`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from corla_observer.classify import COLORADO_COUNTIES
from corla_observer.county_status import (
    BEFORE_DRAWING,
    BOARDS_NOT_KNOWN,
    DRAWING_DONE,
    NOT_IN_CORLA,
    Rounds,
    active_links,
    audit_rounds,
    corla_uploads,
    county_statuses,
    stage,
)
from corla_observer.datamap import Sourced, build_map
from corla_observer.events import EventLog
from corla_observer.fetch import FetchPolicy, FetchResult, fetch_urls
from corla_observer.practice import BoardProgress
from corla_observer.seeds import enter_seed
from corla_observer.uploads import receive, record_audit_boards, status
from corla_observer.workspace import Workspace
from synthetic import ELECTION_URL, ballots, election_files, make_cvr

ACTOR = "coordinator:c1"
GOOD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"))
UPLOAD_STATUS = (
    b"name,filename,type,status,timestamp,computed_hash\r\n"
    b"Weld,weld_manifest.csv,manifest,IMPORTED,2026-11-03 09:15:00.5,AA\r\n"
    b"Weld,weld_cvr.csv,CVRs,IMPORTED,2026-11-03 10:20:00,BB\r\n"
    b"Adams,adams_manifest.csv,manifest,IMPORTED,2026-11-03 11:00:00,CC\r\n"
    b"Adams,adams_cvr.csv,CVRs,FAILED,2026-11-03 11:30:00,DD\r\n"
    b"Adams,adams_manifest2.csv,manifest,FAILED,2026-11-03 12:00:00,EE\r\n"
)


class FakeFetcher:
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.policy = FetchPolicy()

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        body = self.files.get(url)
        return FetchResult(url, url, 200 if body is not None else 404, body or b"", {})


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def fetch(ws: Workspace, log: EventLog, files: Mapping[str, bytes]) -> None:
    urls = {ELECTION_URL + path: data for path, data in files.items()}
    fetch_urls(list(urls), workspace=ws, log=log, fetcher=FakeFetcher(urls))


def receive_cvr(ws: Workspace, log: EventLog, county: str, tmp_path: Path) -> None:
    source = tmp_path / f"{county}.csv"
    source.write_bytes(GOOD_CVR)
    assert receive(ws, log, county, "redacted_cvr", source, "cvr.csv", ACTOR).accepted


def test_statuses_combine_corla_our_uploads_and_the_stage(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS))
    receive_cvr(ws, log, "Weld", tmp_path)
    record_audit_boards(log, "Weld", 2, ACTOR)
    record_audit_boards(log, "Boulder", 4, ACTOR, placeholder=True, note="the clerk will say")
    datamap = build_map(log, ws)
    corla = corla_uploads(datamap, ws)
    assert corla.source == "RLA/2026/general/files/uploadStatus.csv"
    statuses = county_statuses(status(log), corla, audit_rounds(datamap))
    assert list(statuses)[:2] == ["Adams", "Alamosa"] and set(COLORADO_COUNTIES) <= set(statuses)

    weld = statuses["Weld"]
    assert weld.summary == f"{BEFORE_DRAWING.capitalize()} · manifest and CVR uploaded to CORLA · redacted CVR provided"
    assert weld.corla_manifest is not None and weld.corla_manifest.time == "2026-11-03 09:15:00 MST"
    assert weld.corla_cvr is not None and weld.corla_cvr.imported and weld.corla_cvr.type == "CVRs"
    assert weld.cvr_provided and weld.cvr_verdict == "PASS" and weld.participating
    assert weld.audit_boards_text == "audit boards: 2"

    # Uploaded to CORLA but no CVR provided to us: not taking part in observation.
    adams = statuses["Adams"]
    assert adams.summary == "Before the drawing · manifest uploaded to CORLA, CVR not yet · redacted CVR not yet provided"
    # The import stands although a later upload failed; the failed CVR shows as such.
    assert adams.corla_manifest is not None and adams.corla_manifest.imported
    assert adams.corla_cvr is not None and adams.corla_cvr.status == "FAILED"
    assert not adams.participating and not adams.cvr_provided

    # A placeholder board count is not yet known.
    boulder = statuses["Boulder"]
    assert boulder.audit_boards == 4 and boulder.audit_boards_placeholder
    assert boulder.audit_boards_text == BOARDS_NOT_KNOWN == "audit boards: not yet known"
    assert boulder.summary.endswith("manifest and CVR not yet uploaded to CORLA · redacted CVR not yet provided")
    assert statuses["Denver"].audit_boards_text == BOARDS_NOT_KNOWN

    # San Juan counts by hand and is not in CORLA's audit.
    san_juan = statuses["San Juan"]
    assert not san_juan.in_corla and san_juan.stage is None and san_juan.note == NOT_IN_CORLA["San Juan"]
    assert "counts its ballots by hand" in (san_juan.note or "")
    assert san_juan.summary == "Counts by hand; not part of CORLA's audit"


def test_without_an_upload_status_corla_is_not_mentioned(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files())
    receive_cvr(ws, log, "Weld", tmp_path)
    datamap = build_map(log, ws)
    corla = corla_uploads(datamap, ws)
    assert corla.source is None
    weld = county_statuses(status(log), corla, audit_rounds(datamap))["Weld"]
    assert weld.summary == "Before the drawing · redacted CVR provided" and weld.corla_source is None


def test_stages_from_the_seed_round_start_and_round_files(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS))
    enter_seed(log, "12345678901234567890", ACTOR, "announced at the dice roll")
    datamap = build_map(log, ws)
    assert datamap.seed is not None
    rounds = audit_rounds(datamap)
    assert rounds.election_round is None
    drawn = stage("Weld", rounds, seed_source=datamap.seed.source, round_start=None)
    assert drawn.label == DRAWING_DONE and drawn.round is None
    started = stage("Weld", rounds, seed_source=datamap.seed.source, round_start=Sourced("2026-11-16T17:00:00Z", "recorded"))
    assert started.label == "Round 1" and started.round == 1

    assert stage("Weld", rounds, seed_source="s", round_start=None, pull_list=True).label == "Round 1"
    # A file named for a round counts, as CORLA's round-one selections do.
    named = {"finalReports/ContestMarginAndContestCVR-IDs-Round1.csv": b"a\r\n1\r\n"}
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS) | named)
    assert audit_rounds(build_map(log, ws)).election_round == 1

    rounds_files = {"round_1/contestSelection.csv": b"x\r\n1\r\n", "round2/contest.csv": b"y\r\n2\r\n"}
    fetch(ws, log, election_files(files_uploadStatus_csv=UPLOAD_STATUS) | rounds_files)
    rounds = audit_rounds(build_map(log, ws))
    assert rounds.election_round == 2 and rounds.election_ref == "RLA/2026/general/round2/contest.csv"
    assert stage("Weld", rounds, seed_source="s", round_start=None).label == "Round 2"
    # A round with a file for the county is that county's round.
    per_county = Rounds(2, "RLA/x/round_2/a.csv", {"Weld": (1, "RLA/x/round_1/ballot_list_Weld.csv")})
    weld = stage("Weld", per_county, seed_source="s", round_start=None)
    assert weld.label == "Round 1" and "ballot_list_Weld" in weld.source
    assert stage("Adams", per_county, seed_source="s", round_start=None).label == "Round 2"

    statuses = county_statuses(status(log), corla_uploads(build_map(log, ws), ws), rounds, seed_source="s")
    assert statuses["Weld"].summary == "Round 2 · manifest and CVR uploaded to CORLA · redacted CVR not yet provided"


def test_observation_progress_and_links_make_a_county_take_part(ws: Workspace, log: EventLog) -> None:
    log.append("practice.link_created", ACTOR, county="Mesa", board="1", data={"role": "observer", "actor": "observer:ann", "board": 1})
    log.append("practice.link_created", ACTOR, county="Pueblo", board="1", data={"role": "observer", "actor": "observer:bo", "board": 1})
    log.append("practice.link_revoked", ACTOR, county="Pueblo", board="1", data={"role": "observer", "actor": "observer:bo", "board": 1})
    links = active_links(log.iter_events())
    assert links == {"Mesa": 1}
    boards = [
        BoardProgress("Larimer", "1", ballots=40, done=12, observers=["observer:cy"]),
        BoardProgress("Larimer", "2", ballots=35, done=3, observers=["observer:cy", "observer:di"]),
    ]
    statuses = county_statuses({}, corla_uploads(build_map(log, ws), ws), Rounds(1, "RLA/r/round_1/a.csv"), boards=boards, links=links)
    larimer = statuses["Larimer"]
    assert larimer.participating and larimer.observers == 2
    assert [b.text for b in larimer.boards] == ["board 1: 12 of 40 ballots entered", "board 2: 3 of 35 ballots entered"]
    assert larimer.summary == "Round 1 · redacted CVR not yet provided · 15 of 75 ballots observed"
    assert statuses["Mesa"].participating and statuses["Mesa"].links == 1
    assert not statuses["Pueblo"].participating
