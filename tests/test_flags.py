"""Fast tests for corla_observer.flags: the roll time, and every change flag rule.

Each test builds a small synthetic election (tests/synthetic.py) in a
temporary workspace, fetched through `corla_observer.fetch.fetch_urls` with a
fake fetcher, under an event log whose clock the test sets, so that changes
fall before or after the roll as the test needs.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.datamap import (
    OVERRIDES_FILENAME,
    OverridesError,
    apply_overrides,
    audit_times,
    build_map,
    describe,
    parse_overrides,
    parse_time,
    record_audit_time,
    record_seed,
)
from corla_observer.events import EventLog
from corla_observer.localtime import colorado_offset, colorado_time
from corla_observer.fetch import FetchPolicy, FetchResult, fetch_urls
from corla_observer.flags import (
    Flag,
    addressed_flags,
    addressing,
    change_rule,
    check_flags,
    filter_flags,
    record_addressed,
    record_flags,
    record_reopened,
    split_addressed,
    unflagged_problems,
)
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import ADAMS_MANIFEST, BOULDER_MANIFEST, ELECTION_URL, ballots, election_files, make_cvr

ACTOR = "coordinator:c1"
SEED = "12345678901234567890"
#: The synthetic roll: 2026-11-16 10:00 MST.
ROLL = "2026-11-16T17:00:00.000Z"
ADAMS_URL = ELECTION_URL + "files/Adams.csv"
OTHER_ADAMS = b"County,Tabulator,Batch,# of Ballots\r\nAdams,101,1,30\r\nAdams,101,3,20\r\n"


class Clock:
    """The event log's clock, set by the test."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def at(self, text: str) -> None:
        self.now = datetime.fromisoformat(parse_time(text).replace("Z", "+00:00"))


class FakeFetcher:
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.policy = FetchPolicy()

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        body = self.files.get(url)
        return FetchResult(url, url, 200 if body is not None else 404, body or b"", {})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace, clock: Clock) -> Iterator[EventLog]:
    with EventLog.open(ws.root, clock=clock) as log:
        yield log


def fetch(ws: Workspace, log: EventLog, files: Mapping[str, bytes]) -> list[dict[str, object]]:
    urls = {ELECTION_URL + path: data for path, data in files.items()}
    return fetch_urls(list(urls), workspace=ws, log=log, fetcher=FakeFetcher(urls)).alerts  # type: ignore[return-value]


def reasons(flags: list[Flag]) -> list[tuple[str, str]]:
    return [(f.severity, f.reason) for f in flags]


# ---------------------------------------------------------------------------
# Times


def test_parse_time_reads_colorado_time_and_offsets() -> None:
    assert parse_time("2026-07-13 10:10") == "2026-07-13T16:10:00.000Z"  # MDT
    assert colorado_time(parse_time("2026-07-13 10:10")) == "2026-07-13 10:10:00 MDT"
    assert parse_time("2026-11-16 10:00") == "2026-11-16T17:00:00.000Z"  # MST
    assert parse_time("2026-07-13 11:46:37.543509") == "2026-07-13T17:46:37.543Z"
    assert parse_time("2026-07-13T10:10:00-06:00") == "2026-07-13T16:10:00.000Z"
    assert parse_time("2026-07-13T16:10:00Z") == "2026-07-13T16:10:00.000Z"
    assert colorado_offset(datetime(2026, 3, 8, 1, 59)).utcoffset(None) == timedelta(hours=-7)
    assert colorado_offset(datetime(2026, 3, 8, 2, 0)).utcoffset(None) == timedelta(hours=-6)
    assert colorado_offset(datetime(2026, 11, 1, 2, 0)).utcoffset(None) == timedelta(hours=-7)
    with pytest.raises(ValueError):
        parse_time("about ten")


def test_roll_time_is_the_earliest_known(ws: Workspace, log: EventLog, clock: Clock) -> None:
    assert build_map(log, ws).roll_time is None
    clock.at("2026-11-16 10:20")
    record_seed(log, SEED, ACTOR, "the dice roll")
    datamap = build_map(log, ws)
    assert datamap.roll_time is not None and datamap.roll_time.value == "2026-11-16T17:20:00.000Z"
    assert "seed was entered" in datamap.roll_time.source
    record_audit_time(log, "roll", "2026-11-16 10:05", ACTOR, "watched it")
    assert build_map(log, ws).roll_time.value == "2026-11-16T17:05:00.000Z"  # type: ignore[union-attr]
    (ws.root / OVERRIDES_FILENAME).write_text(
        'format = 1\n[roll_time]\nvalue = "2026-11-16 10:00"\nreason = "scheduled"\n'
        '[round_start]\nvalue = "2026-11-16T13:00:00-07:00"\nreason = "first board sat down"\n',
        encoding="utf-8",
    )
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert datamap.roll_time is not None and datamap.roll_time.value == ROLL
    assert "[roll_time] override: scheduled" in datamap.roll_time.source
    assert datamap.round_start is not None and datamap.round_start.value == "2026-11-16T20:00:00.000Z"
    assert datamap.to_dict()["roll_time"] == {"value": ROLL, "source": datamap.roll_time.source}
    times = audit_times(log, ws)
    assert (times.roll, times.round_start) == (datamap.roll_time, datamap.round_start)
    with pytest.raises(ValueError):
        record_audit_time(log, "lunch", "2026-11-16 12:00", ACTOR, "")
    with pytest.raises(OverridesError, match="not a date and time"):
        parse_overrides('[roll_time]\nvalue = "soon"\nreason = "x"\n')


def test_change_rule() -> None:
    assert change_rule("county_manifest", "new", "2026-11-16T18:00:00.000Z", ROLL, None) == ("changed_after_roll", "high")
    assert change_rule("county_manifest", "new", "2026-11-16T16:00:00.000Z", ROLL, None) is None
    assert change_rule("county_manifest", "changed", "2026-11-16T16:00:00.000Z", ROLL, None) == ("changed", "normal")
    assert change_rule("vote_totals", "missing", "2026-11-16T18:00:00.000Z", ROLL, None) == ("missing", "normal")
    assert change_rule("vote_totals", "changed", "2026-11-17T18:00:00.000Z", ROLL, "2026-11-17T00:00:00.000Z") == (
        "changed_after_round_start",
        "normal",
    )
    assert change_rule("html_page", "changed", "2026-11-17T18:00:00.000Z", ROLL, None) is None


# ---------------------------------------------------------------------------
# File changes


def test_manifest_changed_after_the_roll_is_flagged_by_the_fetcher(ws: Workspace, log: EventLog, clock: Clock) -> None:
    fetch(ws, log, election_files())
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    clock.at("2026-11-16 10:30")
    alerts = fetch(ws, log, election_files(files_Adams_csv=OTHER_ADAMS))
    assert len(alerts) == 1
    alert = alerts[0]
    assert (alert["reason"], alert["severity"], alert["change"], alert["kind"]) == (
        "changed_after_roll", "high", "changed", "county_manifest",
    )  # fmt: skip
    assert alert["time"] == "2026-11-16T17:30:00.000Z" and alert["roll_time"] == ROLL
    assert alert["sha256"] == hashlib.sha256(OTHER_ADAMS).hexdigest()
    assert alert["previous_sha256"] == hashlib.sha256(ADAMS_MANIFEST).hexdigest()
    flags = check_flags(log, ws)
    flag = next(f for f in flags if f.reason == "changed_after_roll")
    assert flag.alerts and flag.counties == ("Adams",)
    assert "files/Adams.csv (county_manifest, Adams) changed after the roll" in flag.describe()


def test_changes_are_flagged_once_the_roll_time_is_known(ws: Workspace, log: EventLog, clock: Clock) -> None:
    fetch(ws, log, election_files())
    clock.at("2026-11-16 10:30")
    alerts = fetch(ws, log, election_files(files_Adams_csv=OTHER_ADAMS, files_targetContests_csv=None))
    # No roll time yet: the fetcher saw ordinary changes.
    assert sorted((a["reason"], a["severity"]) for a in alerts) == [("changed", "normal"), ("missing", "normal")]
    clock.at("2026-11-16 11:00")
    record_seed(log, SEED, ACTOR, "late entry")
    assert reasons(check_flags(log, ws)) == [("normal", "changed"), ("normal", "missing")]
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "watched it")
    flags = check_flags(log, ws)
    assert reasons(flags) == [("high", "changed_after_roll"), ("high", "changed_after_roll")]
    removed = next(f for f in flags if f.details["change"] == "missing")
    assert removed.sha256 is None and removed.previous_sha256 and "removed after the roll" in removed.message
    assert all(f.alerts for f in flags), "merged with the fetcher's alerts"


def test_other_files_and_changes_after_the_round_start(ws: Workspace, log: EventLog, clock: Clock) -> None:
    status = b"name,filename,type,status,timestamp,computed_hash\r\n"
    fetch(ws, log, election_files(files_uploadStatus_csv=status))
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    clock.at("2026-11-16 12:00")
    fetch(ws, log, election_files(files_uploadStatus_csv=status + b"Adams,a.csv,manifest,IMPORTED,2026-11-01 09:00,AB\r\n"))
    record_audit_time(log, "round_start", "2026-11-16 13:00", ACTOR, "test")
    clock.at("2026-11-16 14:00")
    page = b"<!DOCTYPE html><html><body>Round 2</body></html>"
    fetch(ws, log, election_files(files_uploadStatus_csv=status, riskLimit_html=page))
    assert reasons(check_flags(log, ws)) == [("normal", "changed"), ("normal", "changed_after_round_start")]


def test_files_of_another_election_are_not_judged_against_the_roll(ws: Workspace, log: EventLog, clock: Clock) -> None:
    fetch(ws, log, election_files())
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    clock.at("2026-11-16 10:30")
    old = "https://www.coloradosos.gov/pubs/elections/RLA/2024/general/files/Adams.csv"
    fetch_urls([old], workspace=ws, log=log, fetcher=FakeFetcher({old: OTHER_ADAMS}), report_missing=False)
    assert not [f for f in check_flags(log, ws) if old in f.refs]


# ---------------------------------------------------------------------------
# Uploads


def test_uploads_after_the_roll(ws: Workspace, log: EventLog, clock: Clock) -> None:
    fetch(ws, log, election_files())
    clock.at("2026-11-15 09:00")
    receive(ws, log, "Adams", "manifest", io.BytesIO(ADAMS_MANIFEST), "Adams_manifest.csv", ACTOR)
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    clock.at("2026-11-16 11:00")
    receive(ws, log, "Adams", "redacted_cvr", io.BytesIO(make_cvr(ballots(12, "Mayor"))), "Adams_CVR.csv", ACTOR)
    pull = b"Imprinted ID,Audit Board\r\n101-1-1,1\r\n"
    receive(ws, log, "Adams", "pull_list", io.BytesIO(pull), "pull.csv", ACTOR)
    flags = check_flags(log, ws)
    assert reasons(flags) == [("high", "changed_after_roll")]
    assert flags[0].refs == ("upload:Adams:redacted_cvr",) and "(cvr, Adams) new after the roll" in flags[0].message
    # A changed manifest from the county after the roll.
    receive(ws, log, "Adams", "manifest", io.BytesIO(OTHER_ADAMS), "Adams_manifest2.csv", ACTOR)
    changed = [f for f in check_flags(log, ws) if f.refs == ("upload:Adams:manifest",)]
    assert len(changed) == 1 and changed[0].previous_sha256 == hashlib.sha256(ADAMS_MANIFEST).hexdigest()


def test_uploads_carry_a_simulated_time(ws: Workspace, log: EventLog, clock: Clock) -> None:
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    clock.at("2026-11-20 10:00")
    marker = {"source": "mirror", "time": "2026-11-16T16:30:00.000Z"}
    receive(ws, log, "Adams", "manifest", io.BytesIO(ADAMS_MANIFEST), "m.csv", ACTOR, simulated=marker)
    events = [e for e in log.iter_events() if e.county == "Adams"]
    assert events and all(e.data["simulated"] == marker for e in events)
    assert check_flags(log, ws) == []


def test_corla_upload_status_flags(ws: Workspace, log: EventLog) -> None:
    imported = hashlib.sha256(BOULDER_MANIFEST).hexdigest().upper()
    status = (
        "name,filename,type,approx_count,size,status,timestamp,computed_hash,submitted_hash\r\n"
        f"Boulder,b.csv,manifest,50,99,IMPORTED,2026-11-16 09:59:00,{imported},{imported}\r\n"
        "Adams,a.csv,manifest,50,99,HASH_MISMATCH,2026-11-14 09:00:00,AAAA,BBBB\r\n"
        "Adams,a.csv,CVRs,50,99,FAILED,2026-11-14 09:05:00,CCCC,CCCC\r\n"
        f"Adams,a2.csv,manifest,50,99,IMPORTED,2026-11-16 11:46:37.5,{hashlib.sha256(ADAMS_MANIFEST).hexdigest()},X\r\n"
        "Weld,w.csv,pull list,5,9,IMPORTED,2026-11-16 12:00:00,DDDD,DDDD\r\n"
    ).encode()
    fetch(ws, log, election_files(files_uploadStatus_csv=status))
    record_audit_time(log, "roll", "2026-11-16 10:00", ACTOR, "test")
    flags = check_flags(log, ws)
    # One flag per county and problem: Adams's late import, and its mismatched and failed uploads together.
    assert sorted(f.reason for f in flags) == ["upload_after_roll", "upload_hash_mismatch"]
    late = next(f for f in flags if f.reason == "upload_after_roll")
    assert late.severity == "high" and late.counties == ("Adams",)
    assert late.time == "2026-11-16T18:46:37.500Z"
    (row,) = late.aspects
    assert (row["filename"], row["type"], row["status"]) == ("a2.csv", "manifest", "IMPORTED")
    assert "at 2026-11-16 11:46:37 MST" in late.message
    errors = next(f for f in flags if f.reason == "upload_hash_mismatch")
    assert errors.reasons == ("upload_hash_mismatch", "upload_failed")
    assert "Adams: 1 hash mismatch and 1 failed upload" in errors.message
    assert "computed hash aaaa, submitted bbbb" in errors.message and "'a.csv', FAILED" in errors.message
    assert [a["status"] for a in errors.aspects] == ["HASH_MISMATCH", "FAILED"]
    assert "CORLA could not import" in errors.investigate and "the hash the county submitted" in errors.investigate
    # A later version of the status file listing the same rows again adds nothing.
    fetch(ws, log, election_files(files_uploadStatus_csv=status + b"Weld,w2.csv,pull list,5,9,IMPORTED,2026-11-17 12:00:00,EE,EE\r\n"))
    assert sorted(f.reason for f in check_flags(log, ws) if f.severity == "high") == ["upload_after_roll", "upload_hash_mismatch"]
    assert filter_flags(flags, counties=["Boulder"]) == []


def test_posted_manifest_unlike_the_import(ws: Workspace, log: EventLog) -> None:
    imported = hashlib.sha256(OTHER_ADAMS).hexdigest()
    status = (
        "name,filename,type,status,timestamp,computed_hash\r\n"
        f"Adams,m.csv,manifest,IMPORTED,2026-11-10 10:00:00,{imported.upper()}\r\n"
    ).encode()
    fetch(ws, log, election_files(files_uploadStatus_csv=status))
    flags = check_flags(log, ws)
    flag = next(f for f in flags if f.reason == "manifest_differs_from_import")
    assert flag.severity == "high" and flag.counties == ("Adams",)
    assert "the imported file is not at hand" in flag.message
    receive(ws, log, "Adams", "manifest", io.BytesIO(OTHER_ADAMS), "Adams_manifest.csv", ACTOR)
    flag = next(f for f in check_flags(log, ws) if f.reason == "manifest_differs_from_import")
    assert "DIFFERENT BATCHES: only in this copy 101-2 (20); only in the imported file 101-3 (20)" in flag.message
    assert (flag.previous_sha256, flag.sha256) == (hashlib.sha256(ADAMS_MANIFEST).hexdigest(), imported)


def test_every_copy_unlike_the_import_is_one_flag_and_not_also_a_problem(ws: Workspace, log: EventLog) -> None:
    imported = hashlib.sha256(BOULDER_MANIFEST).hexdigest()
    status = (
        "name,filename,type,status,timestamp,computed_hash\r\n"
        f"Adams,m.csv,manifest,IMPORTED,2026-11-10 10:00:00,{imported}\r\n"
    ).encode()
    fetch(ws, log, election_files(files_uploadStatus_csv=status))
    receive(ws, log, "Adams", "manifest", io.BytesIO(OTHER_ADAMS), "Adams_manifest.csv", ACTOR)
    flags = check_flags(log, ws)
    (flag,) = [f for f in flags if f.reason == "manifest_differs_from_import"]
    assert "Adams: 2 copies of the manifest are not the manifest CORLA recorded importing" in flag.message
    assert len(flag.aspects) == 2 and flag.previous_sha256 is None
    assert {"upload:Adams:manifest", ELECTION_URL + "files/Adams.csv"} <= set(flag.refs)
    # The data map's problem saying the same is left out wherever both are listed.
    datamap = build_map(log, ws)
    assert "manifest_differs_from_import" in {p.code for p in datamap.check()}
    assert "manifest_differs_from_import" not in {p.code for p in unflagged_problems(datamap.check(), flags)}
    text = describe(datamap, flags=flags)
    assert text.count("manifest_differs_from_import") == 1
    # Recording it, then checking again, merges the recorded alert into the same flag.
    record_flags(log, [flag], ACTOR)
    (again,) = [f for f in check_flags(log, ws) if f.reason == "manifest_differs_from_import"]
    assert again.alerts and again.key == flag.key


def test_manifest_under_another_name_and_identical_across_counties(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Boulder_csv=ADAMS_MANIFEST))
    flags = check_flags(log, ws)
    # One problem, one flag: the copy under Boulder's name is an aspect of the duplicate.
    assert reasons(flags) == [("high", "duplicate_content")]
    duplicate = flags[0]
    assert duplicate.counties == ("Adams", "Boulder") and len(duplicate.alerts) == 2  # both fetch alerts
    assert duplicate.reasons == ("duplicate_content", "manifest_under_other_name")
    assert "RLA/2026/general/files/Boulder.csv holds Adams's manifest, not Boulder's" in [a["message"] for a in duplicate.aspects]
    assert "it is Adams's county_manifest" in duplicate.message
    # Corrected later: the flags stay, since they record what happened.
    fetch(ws, log, election_files())
    assert {"duplicate_content"} == {f.reason for f in check_flags(log, ws) if f.severity == "high"}
    # A county's upload identical to another county's posted file.
    receive(ws, log, "Weld", "manifest", io.BytesIO(BOULDER_MANIFEST), "Weld.csv", ACTOR)
    duplicates = [f for f in check_flags(log, ws) if f.reason == "duplicate_content"]
    assert any(f.counties == ("Boulder", "Weld") for f in duplicates)


# ---------------------------------------------------------------------------
# Recording and the command line


def test_record_flags_once(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_uploadStatus_csv=b"name,filename,type,status,timestamp,computed_hash\r\n"
                                  b"Adams,a.csv,manifest,FAILED,2026-11-14 09:00,AB\r\n"))  # fmt: skip
    flags = check_flags(log, ws)
    assert reasons(flags) == [("high", "upload_failed")]
    seqs = record_flags(log, flags, ACTOR)
    assert len(seqs) == 1
    again = check_flags(log, ws)
    assert again[0].alerts == tuple(seqs)
    assert record_flags(log, again, ACTOR) == []
    event = next(e for e in log.iter_events() if e.kind == "map.alert")
    assert event.data["reason"] == "upload_failed" and event.county == "Adams"


def test_addressed_flags_and_new_findings(ws: Workspace, log: EventLog) -> None:
    header = b"name,filename,type,status,timestamp,computed_hash\r\n"
    first = header + b"Adams,a.csv,manifest,FAILED,2026-11-14 09:00,AB\r\n"
    fetch(ws, log, election_files(files_uploadStatus_csv=first))
    (flag,) = [f for f in check_flags(log, ws) if f.severity == "high"]
    with pytest.raises(ValueError):
        record_addressed(log, flag, ACTOR, "  ")
    record_addressed(log, flag, ACTOR, "a later import succeeded")
    addressed = addressed_flags(log.iter_events())
    still_open, done = split_addressed([flag], addressed)
    assert (still_open, done) == ([], [flag])
    assert addressed[flag.key].note == "a later import succeeded" and addressed[flag.key].actor == ACTOR
    # Another failed row for Adams joins the same flag, which is open again.
    fetch(ws, log, election_files(files_uploadStatus_csv=first + b"Adams,b.csv,CVRs,FAILED,2026-11-14 10:00,CD\r\n"))
    (grown,) = [f for f in check_flags(log, ws) if f.severity == "high"]
    assert grown.key == flag.key and len(grown.aspects) == 2
    marking, holds = addressing(grown, addressed_flags(log.iter_events()))
    assert marking is not None and not holds
    record_addressed(log, grown, ACTOR, "both looked into")
    assert split_addressed([grown], addressed_flags(log.iter_events()))[1] == [grown]
    record_reopened(log, grown.key, ACTOR, "not after all")
    assert addressed_flags(log.iter_events()) == {}
    # The command line says which are addressed, and --open leaves them out.
    record_addressed(log, grown, ACTOR, "really done")
    runner = CliRunner()
    listed = runner.invoke(app, ["flags", "--workspace", str(ws.root)])
    assert listed.exit_code == 0 and "ADDRESSED by coordinator:c1" in listed.output and "really done" in listed.output
    assert "flags: none" in runner.invoke(app, ["flags", "--workspace", str(ws.root), "--open", "--severity", "high"]).output


def test_commands(ws: Workspace, log: EventLog, clock: Clock) -> None:
    runner = CliRunner()
    root = str(ws.root)
    fetch(ws, log, election_files())
    recorded = runner.invoke(
        app, ["audit-time", "roll", "2026-11-16 10:00", "--workspace", root, "--actor", ACTOR, "--source", "watched"]
    )
    assert recorded.exit_code == 0, recorded.output
    bad = runner.invoke(app, ["audit-time", "roll", "soon", "--workspace", root, "--actor", ACTOR, "--source", "x"])
    assert bad.exit_code == 2
    clock.at("2026-11-16 10:30")
    fetch(ws, log, election_files(files_Adams_csv=OTHER_ADAMS))
    listed = runner.invoke(app, ["flags", "--workspace", root, "--severity", "high"])
    assert listed.exit_code == 1, listed.output
    assert "HIGH changed_after_roll: RLA/2026/general/files/Adams.csv" in listed.output
    assert "investigate:" in listed.output
    as_json = runner.invoke(app, ["flags", "--workspace", root, "--json"])
    assert json.loads(as_json.output)[0]["reason"] == "changed_after_roll"
    elsewhere = runner.invoke(app, ["flags", "--workspace", root, "--county", "Weld"])
    assert elsewhere.exit_code == 0 and "flags: none" in elsewhere.output
    check = runner.invoke(app, ["map", "check", "--workspace", root])
    assert "flags (1, 1 high):" in check.output and "changed_after_roll" in check.output
    for_ai = runner.invoke(app, ["map", "show", "--workspace", root, "--for-ai"])
    assert "changed_after_roll" in for_ai.output and "roll time: 2026-11-16 10:00:00 MST" in for_ai.output
    shown = runner.invoke(app, ["map", "show", "--workspace", root, "--json"])
    assert json.loads(shown.output)["flags"][0]["severity"] == "high"
    record = runner.invoke(app, ["flags", "--workspace", root, "--record"])
    assert record.exit_code == 2
