"""Fast tests for corla_observer.intake: a file's kind and county detected from its content, and the upload command without --kind or --county."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog
from corla_observer.intake import PendingStore, counties_in_text, cvr_election_name, detect, known_counties, take, with_choices
from corla_observer.uploads import STAGING_DIRNAME, UploadError
from corla_observer.workspace import Workspace
from synthetic import ballots, clarity_detail, make_cvr

ACTOR = "coordinator:c1"
ROWS = ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102")
WELD_CVR = make_cvr(ROWS, title="2026 Weld County Primary Election")
LA_PLATA_CVR = make_cvr(ROWS, title="2026 La Plata Primary")
NAMELESS_CVR = make_cvr(ROWS, title="2026 Primary Election")
MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nMorgan,101,1,25,Box 1\r\nMorgan,101,2,30,Box 1\r\n"
TWO_COUNTY_MANIFEST = b"County,Tabulator,Batch,# of Ballots\r\nMorgan,101,1,25\r\nWeld,101,2,30\r\n"
PULL_LIST = b"imprinted id,audit board,county\r\n101-1-7,1,Weld\r\n101-2-3,2,Weld\r\n"
PULL_LIST_NO_COUNTY = b"imprinted id,audit board\r\n101-1-7,1\r\n101-2-3,2\r\n"


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_counties_in_text() -> None:
    assert counties_in_text("2026 Weld County Primary Election") == ["Weld"]
    assert counties_in_text("2026 La Plata Primary") == ["La Plata"]
    assert counties_in_text("2026 LaPlata Primary") == ["La Plata"]
    assert counties_in_text("2026 Rio Grande County General") == ["Rio Grande"]  # not also Grand
    assert counties_in_text("2026 Lake County Primary, Park Street precinct") == ["Lake"]  # "County" after a name wins
    assert counties_in_text("2026 Primary Election") == []
    from corla_observer.intake import county_in_name

    assert county_in_name("Weld_ballots_to_audit.csv") == "Weld" and county_in_name("parking-list.csv") is None
    assert cvr_election_name(WELD_CVR) == "2026 Weld County Primary Election"


@pytest.mark.parametrize(
    ("name", "data", "kind", "county"),
    [
        ("export.csv", WELD_CVR, "redacted_cvr", "Weld"),
        ("x.csv", LA_PLATA_CVR, "redacted_cvr", "La Plata"),
        ("m.csv", MANIFEST, "manifest", "Morgan"),
        ("p.csv", PULL_LIST, "pull_list", "Weld"),
        ("Weld_ballots_to_audit.csv", PULL_LIST_NO_COUNTY, "pull_list", "Weld"),  # the file name, as weak evidence
        ("Weld_CVR_Export.csv", NAMELESS_CVR, "redacted_cvr", "Weld"),
    ],
)
def test_detect(tmp_path: Path, name: str, data: bytes, kind: str, county: str) -> None:
    found = detect(write(tmp_path, name, data), name, ["La Plata", "Morgan", "Weld"])
    assert (found.kind, found.county) == (kind, county), found
    assert found.complete and found.evidence and not found.chosen


def test_detect_asks_when_it_cannot_tell(tmp_path: Path) -> None:
    nameless = detect(write(tmp_path, "cvr.csv", NAMELESS_CVR), "cvr.csv", ["Morgan", "Weld"])
    assert nameless.kind == "redacted_cvr" and nameless.county is None and nameless.counties == ("Morgan", "Weld")
    assert "which county" in nameless.problem()
    several = detect(write(tmp_path, "m.csv", TWO_COUNTY_MANIFEST), "m.csv", [])
    assert several.county is None and set(several.counties) == {"Morgan", "Weld"}
    unknown = detect(write(tmp_path, "notes.txt", b"hello\n"), "notes.txt", ["Weld"])
    assert unknown.kind is None and "results" in unknown.kinds
    # A file name naming a county that is not the workspace's: asked, with it first.
    other = detect(write(tmp_path, "Adams_CVR.csv", NAMELESS_CVR), "Adams_CVR.csv", ["Weld"])
    assert other.county is None and other.counties[:2] == ("Adams", "Weld")


def test_content_beats_name_and_known_counties_cross_check(tmp_path: Path) -> None:
    found = detect(write(tmp_path, "Morgan_CVR.csv", WELD_CVR), "Morgan_CVR.csv", ["Morgan"])
    assert found.county == "Weld"
    assert any("suggests Morgan" in w for w in found.warnings) and any("not one of this workspace's counties" in w for w in found.warnings)


def test_detect_results(tmp_path: Path) -> None:
    state = clarity_detail({"Governor (Vote For 1)": {"A": {"Adams": 6, "Weld": 2}}}, {"Adams": 6, "Weld": 2})
    found = detect(write(tmp_path, "detailxml.zip", state), "detailxml.zip", ["Weld"])
    assert (found.kind, found.county, found.results_form) == ("results", "state", "detailxml.zip")
    county = clarity_detail({"Governor (Vote For 1)": {"A": {"Weld": 2}}}, {"Weld": 2}, zipped=False).replace(b"<ElectionName>", b"<Region>Weld</Region><ElectionName>")
    found = detect(write(tmp_path, "detail.xml", county), "detail.xml", ["Weld"])
    assert (found.kind, found.county) == ("results", "Weld") and any("Region" in e for e in found.evidence)


def test_with_choices() -> None:
    from corla_observer.intake import Detection

    base = Detection("redacted_cvr", None, counties=("Weld",))
    chosen = with_choices(base, county="weld")
    assert chosen.county == "Weld" and chosen.chosen == ("county",) and chosen.complete
    with pytest.raises(UploadError):
        with_choices(base, county="Atlantis")
    with pytest.raises(UploadError):
        with_choices(base, kind="ballots")
    assert with_choices(Detection("results", None), county="state").county == "state"
    with pytest.raises(UploadError):
        with_choices(base, county="state")  # "state" is only for results


def test_take_and_pending(tmp_path: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        intake = take(ws, log, write(tmp_path, "a.csv", WELD_CVR), "a.csv", ACTOR, known=["Weld"])
        assert intake.accepted and intake.upload is not None and intake.upload.county == "Weld"
        event = next(e for e in log.iter_events() if e.kind == "upload.detected")
        assert event.county == "Weld" and event.data["kind"] == "redacted_cvr" and event.file_sha256 == intake.upload.sha256
        waiting = take(ws, log, write(tmp_path, "b.csv", NAMELESS_CVR), "b.csv", ACTOR, known=["Weld"])
        assert not waiting.detection.complete and waiting.error and waiting.upload is None
        store = PendingStore(ws)
        staged = ws.root / STAGING_DIRNAME / "x"
        staged.parent.mkdir(exist_ok=True)
        staged.write_bytes(NAMELESS_CVR)
        ident = store.put(staged, "b.csv", ACTOR, waiting.detection)
        assert store.get(ident, "coordinator:someone-else") is None and store.get("../../etc", ACTOR) is None
        pending = store.get(ident, ACTOR)
        assert pending is not None and pending.detection == waiting.detection
        done = take(ws, log, pending.path, pending.original_name, ACTOR, county="Weld", detection=pending.detection)
        assert done.accepted and done.detection.chosen == ("county",)
        store.discard(ident)
        assert not (ws.root / STAGING_DIRNAME / "pending").exists()
        assert known_counties(ws, log) == ["Weld"]


def test_cli_upload_detects(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    base = ["upload", "--workspace", str(workspace), "--actor", ACTOR]
    both = runner.invoke(app, base + [str(write(tmp_path, "export.csv", WELD_CVR)), str(write(tmp_path, "m.csv", MANIFEST))])
    assert both.exit_code == 0, both.output
    assert "export.csv: detected redacted_cvr for Weld" in both.output and "Weld redacted_cvr export.csv: accepted" in both.output
    assert "Morgan manifest m.csv: accepted" in both.output and "evidence: its County column names Morgan" in both.output
    nameless = write(tmp_path, "cvr.csv", NAMELESS_CVR)
    asked = runner.invoke(app, base + [str(nameless)])
    assert asked.exit_code == 2 and "give --county" in asked.output
    given = runner.invoke(app, base + ["--county", "Weld", str(nameless)])
    assert given.exit_code == 0 and "(chosen: county)" in given.output
