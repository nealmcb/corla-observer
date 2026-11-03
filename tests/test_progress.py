# ruff: noqa: F811 (the fixtures from test_web are parameters here)
"""Where the audit is, statewide (corla_observer.progress), and the dashboard's line of stages."""

from __future__ import annotations

from fastapi.testclient import TestClient

from corla_observer.county_status import CorlaUpload, CorlaUploads, CountyAuditStatus
from corla_observer.progress import audit_progress
from test_web import client, make_client, workspace  # noqa: F401 (fixtures)

IMPORTED = CorlaUpload("manifest", "IMPORTED", "2026-11-12 10:00:00", "2026-11-12 10:00:00 MST")

def _status(county: str, *, files: bool = True, round: int | None = None) -> CountyAuditStatus:
    return CountyAuditStatus(
        county=county, summary="", stage=None, stage_source=None, round=round, corla_source="uploadStatus.csv",
        corla_manifest=IMPORTED if files else None, corla_cvr=IMPORTED if files else None, cvr_provided=False, cvr_verdict=None,
        manifest_copy=False, pull_list=False, audit_boards=None, audit_boards_placeholder=False, audit_boards_text="",
        boards=[], links=0, observers=0, participating=False, in_corla=county != "San Juan",
    )  # fmt: skip

def test_before_anything_every_stage_waits_and_says_when_due() -> None:
    steps = audit_progress(contests_posted=False, corla=CorlaUploads(), seed=False, selections=False, statuses={},
                           upcoming={"targets_deadline": "Fri 6 Nov 5:00 PM", "roll": "Mon 16 Nov 10:00 AM"})  # fmt: skip
    assert [s.state for s in steps] == ["current", "later", "later", "later", "later"]
    assert steps[0].detail == "not yet posted; due Fri 6 Nov 5:00 PM" and steps[2].detail == "not yet; at the roll, Mon 16 Nov 10:00 AM"

def test_counties_files_then_seed_then_rounds_with_floor_and_high_marker() -> None:
    corla = CorlaUploads("uploadStatus.csv")
    partly = {"Adams": _status("Adams"), "Delta": _status("Delta", files=False), "San Juan": _status("San Juan", files=False)}
    steps = audit_progress(contests_posted=True, corla=corla, seed=False, selections=False, statuses=partly)
    assert [s.state for s in steps][:3] == ["done", "current", "later"] and steps[1].detail == "1 of 2 counties have their manifest and CVR imported"
    rounds = {"Adams": _status("Adams", round=3), "Delta": _status("Delta", round=1)}
    steps = audit_progress(contests_posted=True, corla=corla, seed=True, selections=True, statuses=rounds)
    assert [s.state for s in steps] == ["done", "done", "done", "done", "current"]
    assert steps[-1].detail == "every county in round 1 or later; the furthest in round 3"
    even = {"Adams": _status("Adams", round=2), "Delta": _status("Delta", round=2)}
    assert audit_progress(contests_posted=True, corla=corla, seed=True, selections=True, statuses=even)[-1].detail == "every county in round 2"

def test_the_dashboard_shows_the_stages(client: TestClient) -> None:  # noqa: F811
    page = client.get("/")
    assert '<section id="progress"' in page.text and "Where the audit is" in page.text and 'aria-current="step"' in page.text
    assert "Nothing is still to come" not in page.text
    progress = client.get("/dashboard.json").json()["progress"]
    assert [p["label"] for p in progress] == ["Contests chosen", "Counties' files in CORLA", "Seed entered", "Selections posted", "Audit rounds"]
