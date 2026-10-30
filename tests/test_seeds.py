"""Fast tests for seed entry (corla_observer.seeds): the map, the flags, the command line and the web."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.datamap import build_map
from corla_observer.events import EventLog
from corla_observer.flags import check_flags
from corla_observer.seeds import SeedError, confirm_seed, enter_seed, seed_state
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from synthetic import election_files
from test_datamap import codes, fetch

SEED = "12345678901234567890"
OTHER = "12345678901234567899"
ALICE, BOB = "coordinator:alice", "coordinator:bob"


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def seed_flags(log: EventLog, ws: Workspace) -> list[str]:
    return [f.reason for f in check_flags(log, ws) if f.reason.startswith("seed")]


def test_one_entry_takes_effect(ws: Workspace, log: EventLog) -> None:
    first = enter_seed(log, "1234 5678 9012 3456 7890", ALICE, "announced at the dice roll")
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.value == SEED
    assert "entered by coordinator:alice" in datamap.seed.source and "announced at the dice roll" in datamap.seed.source
    assert not [c for c in codes(datamap) if c.startswith("seed")]
    assert datamap.roll_time is not None and f"event {first}" in datamap.roll_time.source  # the first entry's time
    (entered,) = log.iter_events()
    assert entered.kind == "seed.entered" and entered.data["entry"] == "first" and entered.data["effective"] is True
    assert not seed_flags(log, ws)


def test_an_optional_second_entry_confirms(ws: Workspace, log: EventLog) -> None:
    with pytest.raises(SeedError, match="no seed has been entered"):
        confirm_seed(log, SEED, BOB)
    first = enter_seed(log, SEED, ALICE, "announced at the dice roll")
    outcome = confirm_seed(log, SEED, BOB, "my own notes")
    assert outcome.matched and not outcome.reentry and outcome.first.seq == first and outcome.second.actor == BOB
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.value == SEED and "confirmed by coordinator:bob" in datamap.seed.source
    second = list(log.iter_events())[-1]
    assert second.kind == "seed.second_entry" and second.data["match"] is True and second.data["entered_seq"] == first
    assert [e.kind for e in log.iter_events()] == ["seed.entered", "seed.second_entry"]
    assert seed_state(log.iter_events()).confirmation == outcome
    assert not seed_flags(log, ws)
    for bad in ("1234", "1234567890123456789a", SEED + "1"):
        with pytest.raises(SeedError, match="20 digits"):
            enter_seed(log, bad, ALICE, "")


def test_a_mismatch_is_flagged_until_a_new_entry(ws: Workspace, log: EventLog) -> None:
    enter_seed(log, SEED, ALICE, "dice roll")
    outcome = confirm_seed(log, OTHER, BOB)
    assert not outcome.matched
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.value == SEED and "seed_mismatch" in codes(datamap)
    (flag,) = [f for f in check_flags(log, ws) if f.reason == "seed_entry_mismatch"]
    assert flag.severity == "high" and SEED in flag.message and OTHER in flag.message
    assert flag.details["first_seed"] == SEED and flag.details["second_seed"] == OTHER
    second = list(log.iter_events())[-1]
    assert second.kind == "seed.second_entry" and second.data["match"] is False and second.data["entered_seed"] == SEED
    # A new entry resolves it: the same value again, by anyone.
    enter_seed(log, SEED, BOB, "dice roll, checked")
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.value == SEED and "seed_mismatch" not in codes(datamap)
    assert seed_flags(log, ws) == ["seed_entry_mismatch"]  # the flag stays


def test_reentries_and_changes(ws: Workspace, log: EventLog) -> None:
    first = enter_seed(log, SEED, ALICE, "dice roll")
    # The same coordinator entering it again is a re-entry, not refused.
    same = confirm_seed(log, SEED, ALICE)
    assert same.reentry and same.matched
    again = list(log.iter_events())[-1]
    assert again.kind == "seed.entered" and again.data["entry"] == "re-entry" and again.data["previous_seq"] == first
    assert again.data["changed"] is False
    assert not seed_flags(log, ws)
    # A different value, by any coordinator, is in effect and flagged high as a change.
    changed = confirm_seed(log, OTHER, ALICE)
    assert changed.reentry and not changed.matched
    assert build_map(log, ws).seed.value == OTHER  # type: ignore[union-attr]
    enter_seed(log, SEED, BOB, "the announced seed")
    assert build_map(log, ws).seed.value == SEED  # type: ignore[union-attr]
    flags = [f for f in check_flags(log, ws) if f.reason == "seed_changed"]
    assert [(f.severity, f.details["previous_seed"], f.details["seed"]) for f in flags] == [
        ("high", SEED, OTHER),
        ("high", OTHER, SEED),
    ]
    assert len(seed_state(log.iter_events()).changes) == 2


def test_the_audit_center_seed_is_compared(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    enter_seed(log, SEED, ALICE, "dice roll")
    fetch(ws, log, {"seed.csv": f"seed\r\n{SEED}\r\n".encode()}, report_missing=False)
    assert not [f for f in check_flags(log, ws) if f.reason == "seed_differs_from_audit_center"]
    fetch(ws, log, {"seed.csv": f"seed\r\n{OTHER}\r\n".encode()}, report_missing=False)
    (flag,) = [f for f in check_flags(log, ws) if f.reason == "seed_differs_from_audit_center"]
    assert flag.severity == "high" and OTHER in flag.message and SEED in flag.message
    assert "seed_conflict" in codes(build_map(log, ws))


def test_rehearsal_entry_is_marked(ws: Workspace, log: EventLog) -> None:
    from corla_observer.rehearse import ACTOR, stage_seed

    stage_seed(log, seed=SEED, source="test")
    events = [e for e in log.iter_events() if e.kind.startswith("seed.")]
    assert [e.kind for e in events] == ["seed.entered"]
    assert events[0].data.get("rehearsal") is True and events[0].actor == ACTOR
    assert build_map(log, ws).seed is not None


def test_cli_seed_set_confirm_status(tmp_path: Path) -> None:
    runner = CliRunner()
    root = str(tmp_path / "ws")
    Workspace.open(root)
    early = runner.invoke(app, ["seed", "confirm", "--workspace", root, "--actor", BOB, SEED])
    assert early.exit_code == 2 and "no seed has been entered" in early.output
    entered = runner.invoke(app, ["seed", "set", "--workspace", root, "--actor", ALICE, "--source", "dice roll", SEED])
    assert entered.exit_code == 0 and "in effect" in entered.output, entered.output
    status = runner.invoke(app, ["seed", "status", "--workspace", root])
    assert f"seed in effect {SEED}" in status.output and "no matching second entry" in status.output
    same = runner.invoke(app, ["seed", "confirm", "--workspace", root, "--actor", ALICE, SEED])
    assert same.exit_code == 0 and "entered again" in same.output
    typed = runner.invoke(app, ["seed", "confirm", "--workspace", root, "--actor", BOB], input=OTHER + "\n")
    assert typed.exit_code == 1 and "MISMATCH" in typed.output
    confirmed = runner.invoke(app, ["seed", "confirm", "--workspace", root, "--actor", BOB, SEED])
    assert confirmed.exit_code == 0 and f"seed {SEED} confirmed" in confirmed.output
    changed = runner.invoke(app, ["seed", "confirm", "--workspace", root, "--actor", ALICE, OTHER])
    assert changed.exit_code == 1 and "CHANGED" in changed.output
    status = runner.invoke(app, ["seed", "status", "--workspace", root])
    assert f"seed in effect {OTHER}" in status.output and "MISMATCH" in status.output and "CHANGED" in status.output
    assert runner.invoke(app, ["seed", "set", "--workspace", root, "--actor", ALICE, "--source", "x", "123"]).exit_code == 2


# ---------------------------------------------------------------------------
# The web pages


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    root = tmp_path / "web"
    store = Workspace.open(root)
    with EventLog.open(store.root) as log:
        AccountStore(root).add("alice", "correct horse battery", log)
        AccountStore(root).add("bob", "correct horse battery", log)
    application = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(application, base_url="https://testserver") as test_client:
        yield test_client


def login(client: TestClient, name: str) -> str:
    client.cookies.clear()
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert token
    client.post("/login", data={"csrf": token.group(1), "name": name, "password": "correct horse battery"})
    seed_page = client.get("/coord/seed")
    csrf = re.search(r'name="csrf" value="([^"]+)"', seed_page.text)
    assert seed_page.status_code == 200 and csrf
    return csrf.group(1)


def test_web_seed_entry(client: TestClient) -> None:
    assert client.get("/coord/seed", follow_redirects=False).status_code == 303
    csrf = login(client, "alice")
    assert client.post("/coord/seed/set", data={"seed": SEED, "source": "dice roll"}).status_code == 403  # no CSRF token
    response = client.post("/coord/seed/set", data={"csrf": csrf, "seed": "123", "source": "dice roll"})
    assert response.status_code == 400 and "20 digits" in response.text
    page = client.post("/coord/seed/set", data={"csrf": csrf, "seed": SEED, "source": "dice roll"})
    assert "Seed entered and in effect" in page.text and SEED in page.text
    assert client.get("/dashboard.json").json()["seed"] == SEED  # in effect at once
    same = client.post("/coord/seed/confirm", data={"csrf": csrf, "seed": SEED})
    assert same.status_code == 200 and "entered again, unchanged" in same.text

    csrf = login(client, "bob")
    mismatch = client.post("/coord/seed/confirm", data={"csrf": csrf, "seed": OTHER})
    assert "does NOT match" in mismatch.text and SEED in mismatch.text and OTHER in mismatch.text
    confirmed = client.post("/coord/seed/confirm", data={"csrf": csrf, "seed": SEED, "source": "my notes"})
    assert "the seed is confirmed" in confirmed.text and "confirmed by coordinator:bob" in confirmed.text
    changed = client.post("/coord/seed/set", data={"csrf": csrf, "seed": OTHER, "source": "a misreading"})
    assert "DIFFERENT value" in changed.text
    assert client.get("/dashboard.json").json()["seed"] == OTHER
