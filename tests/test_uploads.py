"""Fast tests for corla_observer.uploads and its command-line commands, in temporary workspaces."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.classify import classify_bytes
from corla_observer.cli import app
from corla_observer.events import EventLog
from corla_observer.uploads import (
    STAGING_DIRNAME,
    UploadError,
    inspect_pull_list,
    receive,
    record_audit_boards,
    status,
)
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr

ACTOR = "coordinator:c1"

GOOD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"))
BAD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(3, "Mayor", "Fire", precinct="7"))
MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nBoulder,101,1,25,Box 1\r\n,101,2,30,Box 1\r\n"
PULL_LIST = (
    b"storage location,scanner id,batch id,ballot position,imprinted id,ballot type,cvr number,"
    b"audited,audit board\r\n"
    b"Box 1,101,1,7,101-1-7,BT-1,7,No,1\r\nBox 1,101,2,3,101-2-3,BT-1,32,No,2\r\n"
    b"Box 1,101,2,9,101-2-9,BT-1,38,No,1\r\n"
)
BALLOT_LIST = b'"scanner_id","batch_id","record_id","imprinted_id","ballot_type","storage_location","cvr_number","audited"\r\n1,9,253,"1-9-253","28","Blue-5",3399,"Yes"\r\n'


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def source(tmp_path: Path, data: bytes, name: str = "upload.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def kinds(log: EventLog) -> list[str]:
    return [event.kind for event in log.iter_events()]


def all_bytes_under(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def test_redacted_cvr_that_passes_is_stored(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    path = source(tmp_path, GOOD_CVR, "Boulder_Redacted_CVR.csv")
    result = receive(ws, log, "boulder", "redacted_cvr", path, path.name, ACTOR)
    sha256 = hashlib.sha256(GOOD_CVR).hexdigest()
    assert (result.accepted, result.county, result.sha256, result.size) == (True, "Boulder", sha256, len(GOOD_CVR))
    assert result.report is not None and result.report.passed
    assert ws.get_bytes(sha256) == GOOD_CVR
    events = list(log.iter_events())
    assert [e.kind for e in events] == ["file.received", "redaction.checked"]
    received, checked = events
    assert (received.county, received.actor, received.file_sha256) == ("Boulder", ACTOR, sha256)
    assert received.data == {
        "kind": "redacted_cvr",
        "original_name": "Boulder_Redacted_CVR.csv",
        "sha256": sha256,
        "size": len(GOOD_CVR),
    }
    assert checked.data["verdict"] == "PASS"
    report = checked.data["report"]
    assert isinstance(report, dict) and report["ballot_rows"] == 21
    assert list((ws.root / STAGING_DIRNAME).iterdir()) == []
    assert log.verify().ok


def test_rejected_cvr_leaves_no_bytes_in_the_workspace(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    path = source(tmp_path, BAD_CVR)
    result = receive(ws, log, "Boulder", "redacted_cvr", path, "cvr.csv", ACTOR)
    sha256 = hashlib.sha256(BAD_CVR).hexdigest()
    assert (result.accepted, result.reason, result.sha256) == (False, "redaction_check_failed", sha256)
    assert list(ws.iter_objects()) == []
    assert list((ws.root / STAGING_DIRNAME).iterdir()) == []
    assert list((ws.files_dir / ".incoming").iterdir()) == []
    # Nothing anywhere under the workspace, the event log included, holds the file or its rows.
    everything = all_bytes_under(ws.root)
    data_rows = BAD_CVR.split(b"\r\n")[4:-1]
    assert BAD_CVR not in everything
    assert not any(row in everything for row in data_rows)
    checked, rejected = list(log.iter_events())
    assert (checked.kind, checked.file_sha256, checked.data["verdict"]) == ("redaction.checked", sha256, "FAIL")
    report = checked.data["report"]
    assert isinstance(report, dict)
    assert report["rare_groups"] == [
        {"size": 3, "precinct": "7", "ballot_type": "BT-1", "contests": 2, "style_hash": result.report.rare_groups[0].style_hash}  # type: ignore[union-attr]
    ]
    assert rejected.kind == "upload.rejected"
    assert rejected.data == {
        "kind": "redacted_cvr",
        "original_name": "cvr.csv",
        "reason": "redaction_check_failed",
        "sha256": sha256,
        "size": len(BAD_CVR),
    }


def test_too_large_is_rejected_unread(ws: Workspace, log: EventLog) -> None:
    result = receive(ws, log, "Boulder", "manifest", io.BytesIO(MANIFEST), "m.csv", ACTOR, max_bytes=20)
    assert (result.accepted, result.reason, result.sha256) == (False, "too_large", None)
    assert list(ws.iter_objects()) == []
    assert list((ws.root / STAGING_DIRNAME).iterdir()) == []
    (event,) = log.iter_events()
    assert (event.kind, event.data["reason"], event.data["size_limit"]) == ("upload.rejected", "too_large", 20)


def test_bad_requests_record_nothing(ws: Workspace, log: EventLog) -> None:
    for county, kind, actor in [
        ("Boulder", "ballots", ACTOR),
        ("Atlantis", "manifest", ACTOR),
        ("Boulder", "manifest", "someone"),
    ]:
        with pytest.raises(UploadError):
            receive(ws, log, county, kind, io.BytesIO(MANIFEST), "m.csv", actor)
    assert len(log) == 0
    assert not (ws.root / STAGING_DIRNAME).exists()


def test_manifest_for_the_stated_county(ws: Workspace, log: EventLog) -> None:
    result = receive(ws, log, "Boulder", "manifest", io.BytesIO(MANIFEST), "C:\\county\\manifest.csv", ACTOR)
    assert (result.accepted, result.original_name, result.warnings) == (True, "manifest.csv", ())
    assert result.classification is not None and result.classification.kind == "county_manifest"
    assert kinds(log) == ["file.received", "file.classified", "manifest.checked"]
    checked = list(log.iter_events())[-1]
    assert checked.data == {"loads": True, "batches": 2, "ballot_cards": 55}


def test_manifest_problems_are_warnings_and_the_file_is_kept(ws: Workspace, log: EventLog) -> None:
    other = receive(ws, log, "Adams", "manifest", io.BytesIO(MANIFEST), "m.csv", ACTOR)
    assert other.accepted and ws.has(other.sha256 or "")
    assert [w.split(":")[0] for w in other.warnings] == ["county_mismatch"]
    junk = receive(ws, log, "Adams", "manifest", io.BytesIO(b"hello,world\r\n1,2\r\n"), "m2.csv", ACTOR)
    assert junk.accepted and ws.has(junk.sha256 or "")
    assert [w.split(":")[0] for w in junk.warnings] == ["classified_as_other_kind", "manifest_does_not_load"]
    # Server paths never reach the published log.
    assert all("staging" not in str(e.data) for e in log.iter_events())


def test_pull_list_is_recognized_with_its_boards(ws: Workspace, log: EventLog) -> None:
    result = receive(ws, log, "Boulder", "pull_list", io.BytesIO(PULL_LIST), "ballots.csv", ACTOR)
    assert (result.accepted, result.warnings) == (True, ())
    classified = next(e for e in log.iter_events() if e.kind == "file.classified")
    assert classified.data["pull_list"] == {"rows": 3, "audit_board_count": 2, "audit_board_values": ["1", "2"]}
    classification = classified.data["classification"]
    assert isinstance(classification, dict) and classification["kind"] == "pull_list"


def test_pull_list_variants() -> None:
    by_position = (
        b"Tabulator,Batch,Record,Audit Board\r\n101,1,7,Board A\r\n101,2,3,Board B\r\n"
    )
    found = classify_bytes(by_position)
    assert found.kind == "pull_list"
    assert inspect_pull_list(by_position, found)["audit_board_count"] == 2
    ballot_list = classify_bytes(BALLOT_LIST)
    assert ballot_list.kind == "pull_list"
    assert inspect_pull_list(BALLOT_LIST, ballot_list) == {"rows": 1}
    assert inspect_pull_list(MANIFEST, classify_bytes(MANIFEST)) == {}


def test_pull_list_warnings(ws: Workspace, log: EventLog) -> None:
    no_board = receive(ws, log, "Adams", "pull_list", io.BytesIO(BALLOT_LIST), "list.csv", ACTOR)
    assert no_board.accepted
    assert [w.split(":")[0] for w in no_board.warnings] == ["pull_list_without_audit_board"]
    unknown = receive(ws, log, "Adams", "pull_list", io.BytesIO(MANIFEST), "list2.csv", ACTOR)
    assert unknown.accepted
    assert [w.split(":")[0] for w in unknown.warnings] == ["pull_list_unrecognized"]


def test_audit_boards(log: EventLog) -> None:
    seq = record_audit_boards(log, "la plata", 3, ACTOR)
    (event,) = log.iter_events()
    assert (event.seq, event.kind, event.county, event.data) == (seq, "audit_boards.recorded", "La Plata", {"count": 3})
    for count in (0, 101, True):
        with pytest.raises(UploadError):
            record_audit_boards(log, "La Plata", count, ACTOR)
    with pytest.raises(UploadError):
        record_audit_boards(log, "La Plata", 2, "nobody")


def test_status(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    assert status(log) == {}
    receive(ws, log, "Boulder", "redacted_cvr", source(tmp_path, BAD_CVR), "first.csv", ACTOR)
    receive(ws, log, "Boulder", "redacted_cvr", source(tmp_path, GOOD_CVR), "second.csv", ACTOR)
    receive(ws, log, "Boulder", "manifest", io.BytesIO(MANIFEST), "m.csv", ACTOR)
    receive(ws, log, "Adams", "manifest", io.BytesIO(MANIFEST), "m.csv", ACTOR)
    record_audit_boards(log, "Boulder", 2, ACTOR)
    counties = status(log, ["Weld"])
    assert list(counties) == ["Adams", "Boulder", "Weld"]
    boulder = counties["Boulder"]
    cvr = boulder.latest["redacted_cvr"]
    assert (cvr.original_name, cvr.verdict, cvr.sha256) == ("second.csv", "PASS", hashlib.sha256(GOOD_CVR).hexdigest())
    assert (boulder.rejected, boulder.audit_boards, boulder.missing) == (1, 2, ["pull_list"])
    assert boulder.last_rejection is not None and boulder.last_rejection["original_name"] == "first.csv"
    assert boulder.latest["manifest"].warnings == ()
    adams = counties["Adams"]
    assert adams.missing == ["redacted_cvr", "pull_list", "audit_boards"]
    assert [w.split(":")[0] for w in adams.latest["manifest"].warnings] == ["county_mismatch"]
    assert counties["Weld"].latest == {}
    receive(ws, log, "Boulder", "pull_list", io.BytesIO(PULL_LIST), "p.csv", ACTOR)
    assert status(log)["Boulder"].missing == []


def test_cli_upload_audit_boards_and_status(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    good = source(tmp_path, GOOD_CVR, "good.csv")
    bad = source(tmp_path, BAD_CVR, "bad.csv")
    base = ["upload", "--workspace", str(workspace), "--county", "Boulder", "--actor", ACTOR]
    accepted = runner.invoke(app, base + ["--kind", "redacted_cvr", str(good)])
    assert accepted.exit_code == 0, accepted.output
    assert "Boulder redacted_cvr good.csv: accepted" in accepted.output
    rejected = runner.invoke(app, base + ["--kind", "redacted_cvr", str(bad)])
    assert rejected.exit_code == 1
    assert "REJECTED (redaction_check_failed)" in rejected.output
    assert "3 ballot(s): precinct '7'" in rejected.output
    assert runner.invoke(app, base + ["--kind", "ballots", str(good)]).exit_code == 2
    assert runner.invoke(app, base + ["--kind", "manifest", str(tmp_path / "absent.csv")]).exit_code == 2
    boards = runner.invoke(
        app, ["audit-boards", "--workspace", str(workspace), "--county", "boulder", "--count", "2", "--actor", ACTOR]
    )
    assert boards.exit_code == 0, boards.output
    assert "recorded 2 audit board(s) for Boulder" in boards.output
    shown = runner.invoke(app, ["status", "--workspace", str(workspace), "--county", "Weld"])
    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    assert lines[0] == "Boulder"
    assert any(line.startswith("  redacted_cvr: good.csv, sha256 ") and "PASS" in line for line in lines)
    assert "  manifest: not received (optional)" in lines
    assert "  audit boards: 2" in lines
    assert "  rejected uploads: 1 (latest: redacted_cvr bad.csv, redaction_check_failed)" in lines
    assert "  missing: pull_list" in lines
    assert lines[lines.index("Weld") + 1 :][-1] == "  missing: redacted_cvr, pull_list, audit_boards"
    with EventLog.open(workspace, read_only=True) as log:
        assert log.verify().ok
