"""Fast tests for the fictional test county (corla_observer.testcounty) and its separation from the real counties."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from corla_observer.classify import TEST_COUNTIES, canonical_county, is_test_county, known_county
from corla_observer.cli import app
from corla_observer.datamap import build_map
from corla_observer.events import EventLog
from corla_observer.extract import build_extract, latest_extract_events
from corla_observer.intake import detect, take
from corla_observer.practice import LinkStore
from corla_observer.redaction import check_redaction
from corla_observer.testcounty import COUNTY, CVR_NAME, LONGEST, MANIFEST_NAME, PULL_LIST_NAME, REDACTED_CONTEST, generate, make
from corla_observer.timestamp import collect_evidence
from corla_observer.workspace import Workspace
from test_extract import ACTOR, ws  # noqa: F401 - the fixture

KINDS = {CVR_NAME: "redacted_cvr", MANIFEST_NAME: "manifest", PULL_LIST_NAME: "pull_list"}


def test_test_county_names() -> None:
    assert TEST_COUNTIES[0] == COUNTY == "Testland"
    assert all(c.endswith("(2020 demo)") and canonical_county(c) is None for c in TEST_COUNTIES[1:])
    assert known_county("delta (2020 demo)") == "Delta (2020 demo)" and known_county("Delta") == "Delta"
    assert canonical_county("Testland") is None  # never a Colorado county
    assert known_county("testland county") == known_county("TESTLAND") == "Testland"
    assert known_county("Weld") == "Weld" and known_county("Atlantis") is None
    assert is_test_county("Testland") and not is_test_county("Weld") and not is_test_county(None)


def test_files_are_reproducible_and_pass_the_redaction_check(tmp_path: Path) -> None:
    first, again = make(), make()
    assert first.files() == again.files() and make(7).files() != first.files()
    county = make()
    assert len(county.styles[LONGEST]) == 60
    assert len(county.pulled) == 21 and sum(1 for c in county.pulled if c.style == LONGEST) == 7
    paths = generate(tmp_path)
    report = check_redaction(paths[CVR_NAME])
    assert report.passed, report.to_dict()


def test_uploaded_with_the_county_chosen_and_kept_out_of_the_map(ws: Workspace, tmp_path: Path) -> None:  # noqa: F811
    paths = generate(tmp_path / "testland")
    with EventLog.open(ws.root) as log:
        before = build_map(log, ws)
        for name, path in paths.items():
            detection = detect(path, name)
            assert detection.kind == KINDS[name] and detection.county is None  # names no Colorado county
            intake = take(ws, log, path, name, ACTOR, county="testland")
            assert intake.accepted and intake.upload is not None and intake.upload.county == COUNTY, intake.error
        after = build_map(log, ws)
        assert COUNTY not in after.manifests and set(after.manifests) == set(before.manifests)
        assert not [e for e in after.entries if e.county == COUNTY]
        assert [p.code for p in after.check()] == [p.code for p in before.check()]
        assert not any(COUNTY in p.message for p in after.check())

        built = build_extract(ws, log, COUNTY, actor=ACTOR)
        extract: Any = built.extract
        assert extract["purpose"] == "practice" and extract["order"] == "pull_list" and extract["county"] == COUNTY
        assert COUNTY not in latest_extract_events(log)  # not an audit's extract: the scheduler leaves it alone
        ballots = extract["ballots"]
        assert len(ballots) == 21 and {b["board"] for b in ballots} == {1}
        assert max(len(b["votes"]) for b in ballots) == 60
        assert sum(1 for b in ballots if b["status"] == "redacted") == 1
        values = [v for b in ballots for _, v in b["votes"]]
        assert any(v.startswith("overvote: ") for v in values) and "no vote" in values
        assert [REDACTED_CONTEST, "redacted"] in [v for b in ballots for v in b["votes"]]
        assert all(b["location"] for b in ballots)

        _, link = LinkStore(ws.root).create("observer", "lori", "Testland", 1, log, actor=ACTOR)
        assert link.county == COUNTY
        evidence = collect_evidence(ws, log)
    assert not any(f.county == COUNTY for f in evidence.files)


def test_dashboard_leaves_the_test_county_out(ws: Workspace, tmp_path: Path) -> None:  # noqa: F811
    from corla_observer.web.views import build_snapshot

    paths = generate(tmp_path / "testland")
    with EventLog.open(ws.root) as log:
        for name, path in paths.items():
            assert take(ws, log, path, name, ACTOR, county=COUNTY).accepted
        build_extract(ws, log, COUNTY, actor=ACTOR)
        LinkStore(ws.root).create("observer", "lori", COUNTY, 1, log, actor=ACTOR)
        snap = build_snapshot(log, ws, ("Adams",))
    assert COUNTY not in {row.county for row in snap.counties}
    assert COUNTY not in snap.statuses and COUNTY not in {b.county for b in snap.boards}
    assert [b.county for b in snap.test_boards] == [COUNTY]


def test_cli_writes_the_files(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["practice", "test-county", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Style A (60 contests)" in result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(KINDS)


def test_observer_page_shortens_any_congress() -> None:
    from corla_observer.web.observe import short_contest

    assert short_contest("Representative to the 117th United States Congress - District 9") == "US Rep D9"
    assert short_contest("Representative to the 120th United States Congress - District 8 - DEM") == "US Rep D8 - DEM"
