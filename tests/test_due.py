"""Fast tests for corla_observer.due: problems judged as of a moment, and problems many counties share grouped.

The synthetic 2026 general (tests/synthetic.py) is fetched with a fake
fetcher, as in tests/test_datamap.py, and its problems are judged before and
after the calendar's due times.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from corla_observer.datamap import DataMap, Problem, Sourced, build_map, describe, parse_time
from corla_observer.due import (
    KNOWN_CALENDARS,
    Calendar,
    calendar_for,
    due_text,
    group_problems,
    stage,
    view_for,
)
from corla_observer.election import GENERAL_2026_SETUP, parse_setup
from corla_observer.events import EventLog
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import ADAMS_MANIFEST, election_files
from test_datamap import fetch

GENERAL = calendar_for("2026/general")
BEFORE_UPLOADS = parse_time("2026-11-07 09:00")
AFTER_UPLOADS = parse_time("2026-11-14 09:00")
AFTER_ROLL = parse_time("2026-11-16 12:00")


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def test_calendars_and_due_texts() -> None:
    assert due_text(parse_time("2026-07-10 14:00")) == "Fri 10 Jul 2:00 PM"
    assert due_text(parse_time("2026-11-13 14:00")) == "Fri 13 Nov 2:00 PM"
    assert due_text(parse_time("2026-11-16 10:00")) == "Mon 16 Nov 10:00 AM"
    primary = calendar_for("2026/primary")
    assert primary.at("tabulation_deadline") == parse_time("2026-07-10 14:00") and primary.at("roll") == parse_time("2026-07-13 10:00")
    # The built-in general calendar is the default election.toml's.
    setup = parse_setup(GENERAL_2026_SETUP)
    from_setup = calendar_for("2026/general", setup)
    assert from_setup.source == "the workspace's election.toml"
    for key in KNOWN_CALENDARS["2026/general"]:
        assert from_setup.at(key) == GENERAL.at(key)
    # A setup for another election is not used.
    assert calendar_for("2026/primary", setup).at("roll") == primary.at("roll")
    assert calendar_for("2030/odd").times == {}


def test_missing_inputs_are_not_problems_before_they_are_due(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None))
    datamap = build_map(log, ws)
    raw = {p.code for p in datamap.check()}
    assert {"no_seed", "no_manifest", "statewide_counties_assumed", "estimated_sample_count"} <= raw

    early = stage(datamap, datamap.check(), now=BEFORE_UPLOADS, calendar=GENERAL, followed=["Weld"])
    assert {p.code for p in early.current} == set()
    assert early.errors == 0 and early.lines == []
    texts = [w.text for w in early.waiting]
    assert "Manifests: 2 of 63 posted, due Fri 13 Nov 2:00 PM" in texts
    assert "CORLA's upload status: not yet posted, due Fri 13 Nov 2:00 PM" in texts
    assert "Seed: not yet entered, due at the roll, Mon 16 Nov 10:00 AM" in texts
    assert any(t.startswith("CORLA's contest list: not yet posted (") and "expected after the roll" in t for t in texts)
    assert any(t.startswith("Redacted CVRs of the counties followed: 0 of 1 received (none yet from Weld)") for t in texts)
    assert early.valid_until == GENERAL.at("tabulation_deadline")

    # After the upload deadline: the manifests and the counties' files are problems; the seed is not yet due.
    later = stage(datamap, datamap.check(), now=AFTER_UPLOADS, calendar=GENERAL, followed=["Weld"])
    codes = {p.code for p in later.current}
    assert {"no_manifest", "statewide_counties_assumed", "no_upload_status", "no_redacted_cvr"} <= codes
    assert "no_seed" not in codes and "estimated_sample_count" not in codes
    assert [w.key for w in later.waiting] == ["seed", "contest_list", "selections", "pull_lists"]
    assert later.valid_until == GENERAL.at("roll")

    # After the roll, everything missing is a problem again.
    after = stage(datamap, datamap.check(), now=AFTER_ROLL, calendar=GENERAL, followed=["Weld"])
    assert {"no_seed", "estimated_sample_count", "no_selections_yet", "no_pull_list"} <= {p.code for p in after.current}
    assert after.waiting == [] and after.valid_until is None and after.errors >= 2


def test_something_present_but_wrong_is_always_a_problem(ws: Workspace, log: EventLog) -> None:
    # Boulder's file holds Adams's manifest: a manifest under another county's name.
    fetch(ws, log, election_files(files_Boulder_csv=ADAMS_MANIFEST))
    datamap = build_map(log, ws)
    early = stage(datamap, datamap.check(), now=BEFORE_UPLOADS, calendar=GENERAL)
    assert "manifest_under_other_name" in {p.code for p in early.current}
    assert "no_manifest" not in {p.code for p in early.current}


def test_without_a_clock_every_problem_is_current(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    datamap = build_map(log, ws)
    view = stage(datamap, datamap.check(), now=None, calendar=GENERAL)
    assert {p.code for p in view.current} == {p.code for p in datamap.check()} and view.waiting == []
    assert "ERROR no_seed" in describe(datamap)


def test_view_for_uses_the_setup_and_the_counties_with_uploads(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files())
    (ws.root / "election.toml").write_text(GENERAL_2026_SETUP, encoding="utf-8")
    manifest = tmp_path / "m.csv"
    manifest.write_bytes(ADAMS_MANIFEST)
    receive(ws, log, "Adams", "manifest", manifest, "m.csv", "coordinator:c1")
    datamap = build_map(log, ws)
    view = view_for(datamap, ws, log, now=BEFORE_UPLOADS)
    assert view.calendar.source == "the workspace's election.toml"
    assert view.followed == ["Adams", "La Plata", "Morgan", "Weld"]
    text = describe(datamap, view=view)
    assert "not yet expected:" in text and "Manifests: 3 of 63 posted" in text and "problems: none" in text


def problem(code: str, county: str | None, severity: str = "error") -> Problem:
    return Problem(severity, code, f"{code} for {county}", hint="do this", county=county)


def test_problems_many_counties_share_are_one_line() -> None:
    counties = ["Adams", "Alamosa", "Baca", "Weld", "Yuma"]
    problems = [problem("no_manifest", c) for c in counties] + [problem("no_seed", None), problem("odd", "Bent", "warning")]
    lines = group_problems(problems, keep=["Weld"])
    assert [(g.code, g.county) for g in lines] == [("no_manifest", None), ("no_manifest", "Weld"), ("no_seed", None), ("odd", "Bent")]
    group = lines[0]
    assert group.grouped and group.counties == ["Adams", "Alamosa", "Baca", "Yuma"]
    assert group.message == "no usable manifest (4 counties)" and group.hint == "do this"
    assert group.to_dict()["members"] == [f"no_manifest for {c}" for c in ["Adams", "Alamosa", "Baca", "Yuma"]]
    # Two counties are not many: they keep their own lines.
    assert [g.county for g in group_problems(problems[:2])] == ["Adams", "Alamosa"]
    # Several problems in fewer counties say both counts.
    many = [problem("estimated_sample_count", c, "info") for c in ["Adams", "Adams", "Baca", "Bent"]]
    assert group_problems(many)[0].message.endswith("(4, in 3 counties)")


def test_grouped_lines_in_the_text() -> None:
    from corla_observer.due import view_lines

    problems = [problem("no_manifest", c) for c in ["Adams", "Alamosa", "Baca"]]
    lines = view_lines(stage(_empty_map(), problems, now=None, calendar=Calendar({}, "none")))
    assert lines == [
        "problems (1):",
        "  ERROR no_manifest: no usable manifest (3 counties): Adams, Alamosa, Baca",
        "    hint: do this",
    ]


def _empty_map() -> DataMap:
    return DataMap(None, "", [], {}, None, Sourced({}, ""), [], [], {}, {}, [])


def test_the_dashboard_snapshot_is_staged_and_rebuilt_at_the_due_time(ws: Workspace, log: EventLog, monkeypatch: pytest.MonkeyPatch) -> None:
    from corla_observer.web import views

    fetch(ws, log, election_files(files_Adams_csv=None))
    early = views.build_snapshot(log, ws, ["Weld"], now=BEFORE_UPLOADS)
    assert early.problems == [] and early.error_count == 0 and early.public_error_count == 0
    assert early.valid_until == GENERAL.at("tabulation_deadline")
    assert early.not_yet["Weld"] == {"cvr": "Fri 13 Nov 2:00 PM", "corla": "Fri 13 Nov 2:00 PM"}
    assert any(w["key"] == "manifests" for w in early.waiting)
    late = views.build_snapshot(log, ws, ["Weld"], now=AFTER_ROLL)
    assert late.error_count >= 2 and late.not_yet == {} and late.waiting == []
    assert "no_seed" in {p["code"] for p in late.public_problems}
    # The cache rebuilds by itself once a due time it was judged before has passed.
    built: list[str | None] = []

    def build(previous: views.Snapshot | None) -> views.Snapshot:
        snap = views.build_snapshot(log, ws, ["Weld"], previous=previous)
        built.append(snap.valid_until)
        return snap

    cache = views.SnapshotCache(build, log.head)
    first = cache.get()
    assert cache.get() is first and len(built) == 1
    first.valid_until = "2000-01-01T00:00:00.000Z"
    assert cache.get() is not first and len(built) == 2
