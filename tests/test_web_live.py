# ruff: noqa: F811 (the fixtures from test_web are parameters here)
"""Fast tests for the live coordinator pages (corla_observer.web.live): estimates, schedule and walkthrough."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from corla_observer.election import init_setup
from corla_observer.events import EventLog
from corla_observer.events import format_server_time
from corla_observer.schedule import beat, init_schedule
from corla_observer.workspace import Workspace
from synthetic import election_files
from test_datamap import fetch
from test_web import Factory, client, csrf_of, login, make_client, workspace  # noqa: F401 (fixtures)


def test_estimates_page_shows_hand_inputs_and_has_no_form(workspace: Path, make_client: Factory) -> None:
    from corla_observer.datamap import OVERRIDES_FILENAME, apply_overrides

    ws = Workspace.open(workspace)
    init_setup(ws, "2026/general")
    (workspace / OVERRIDES_FILENAME).write_text(
        'format = 1\n[[estimate]]\ncontest = "Governor - Democratic Party"\nmargin = 25\nreason = "the county\'s page"\n', encoding="utf-8"
    )
    with EventLog.open(workspace) as log:
        fetch(ws, log, election_files())
        assert apply_overrides(ws, log, "coordinator:alice").applied
    client: TestClient = make_client()
    assert client.get("/coord/estimates", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/coord/estimates")
    assert page.status_code == 200 and "Governor - Democratic Party" in page.text and "central" in page.text
    assert "Inputs supplied by hand" in page.text and "margin 25" in page.text
    # The target list's margin ranks above it, and the page says so.
    assert "the margin supplied by hand (25, supplied by hand in [[estimate]] #1: the county&#39;s page) is not used" in page.text
    assert "<form" not in page.text.split("Inputs supplied by hand", 1)[1]
    assert client.post("/coord/estimates/margin", data={"csrf": csrf_of(page)}).status_code in (404, 405)


def test_schedule_page_and_sprint(workspace: Path, make_client: Factory) -> None:
    init_schedule(Workspace.open(workspace), "2026/general")
    client: TestClient = make_client()
    login(client)
    page = client.get("/coord/schedule")
    assert page.status_code == 200 and "roll day" in page.text and "idle" in page.text
    # No scheduler has looked at the workspace: the page says so, and a sprint is refused.
    assert "never seen for this workspace" in page.text and "Start the sprint</button>" in page.text and " disabled>Start the sprint" in page.text
    refused = client.post("/coord/schedule/sprint", data={"csrf": csrf_of(page), "minutes": "30", "every": "60"}, follow_redirects=False)
    assert "no+scheduler+is+running" in refused.headers["location"]
    assert "every 60 s until" not in client.get("/coord/schedule").text
    # A scheduler's heartbeat: running, and the sprint is taken.
    beat(Workspace.open(workspace), format_server_time(datetime.now(UTC)))
    page = client.get("/coord/schedule")
    assert "running" in page.text and " disabled>Start the sprint" not in page.text
    started = client.post("/coord/schedule/sprint", data={"csrf": csrf_of(page), "minutes": "30", "every": "60"}, follow_redirects=False)
    assert started.status_code == 303
    assert "every 60 s until" in client.get("/coord/schedule").text
    client.post("/coord/schedule/stop", data={"csrf": csrf_of(page)})
    assert "every 60 s until" not in client.get("/coord/schedule").text
    with EventLog.open(workspace, read_only=True) as log:
        kinds = [e.kind for e in log.iter_events()]
    assert "schedule.sprint_requested" in kinds and "schedule.sprint_stopped" in kinds


def test_walkthrough_page_outside_a_walkthrough(client: TestClient) -> None:
    login(client)
    page = client.get("/coord/walkthrough")
    assert page.status_code == 200 and "not a walkthrough" in page.text and "WALKTHROUGH" not in page.text


def test_activity_page_lists_the_latest_events(workspace: Path, make_client: Factory) -> None:
    from corla_observer.activity import summary

    init_schedule(Workspace.open(workspace), "2026/general")
    with EventLog.open(workspace) as log:
        sprint = log.append("schedule.sprint_requested", "coordinator:alice", data={"until": "2026-11-16T20:00:00.000Z", "every_seconds": 60})
        assert summary(sprint) == "until 2026-11-16T20:00:00.000Z; every_seconds 60"
    client: TestClient = make_client()
    assert client.get("/coord/activity", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/coord/activity")
    assert page.status_code == 200 and "schedule.sprint_requested" in page.text and "every_seconds 60" in page.text
    assert 'data-live-url="/coord/activity"' in page.text and ">Activity</a>" in page.text
    # Newest first: the login after the sprint request comes before it.
    assert page.text.index("coordinator.login") < page.text.index("schedule.sprint_requested")
    only = client.get("/coord/activity?group=schedule").text
    assert "schedule.sprint_requested" in only and "<code>coordinator.login</code>" not in only and "show everything" in only
