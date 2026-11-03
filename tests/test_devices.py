"""One observer on several devices (corla_observer.devices): which ballots their devices entered differently."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corla_observer.devices import disagreements, short_device
from corla_observer.events import EventLog
from corla_observer.practice import Link

LINK = Link(role="observer", name="ann", county="Delta", board=1, created="2026-11-02T00:00:00.000Z")
PHONE, LAPTOP = "d-" + "a" * 32, "d-" + "b" * 32


def _entry(log: EventLog, device: str, position: int, match: bool, note: str = "") -> None:
    log.append("observation.entry", LINK.actor, county="Delta", board="1", device_id=device,
               data={"position": position, "imprinted_id": f"1-1-{position}", "match": match, "note": note})  # fmt: skip


def _contest(log: EventLog, device: str, position: int, row: int, outcome: str, heard: str = "", reading: int = 2) -> None:
    data: dict[str, Any] = {"position": position, "imprinted_id": f"1-1-{position}", "row": row, "rows": 2, "reading": reading,
                            "contest": f"Contest {row}", "expected": "Ann", "outcome": outcome, "heard": heard, "correction": False}  # fmt: skip
    log.append("observation.contest", LINK.actor, county="Delta", board="1", device_id=device, data=data)


def test_two_tap_entries_that_differ_between_devices(tmp_path: Path) -> None:
    with EventLog.open(tmp_path) as log:
        _entry(log, PHONE, 1, True)
        _entry(log, LAPTOP, 1, True)  # agree: not reported
        _entry(log, PHONE, 2, True)
        _entry(log, LAPTOP, 2, False, "heard Bob for Governor")
        _entry(log, PHONE, 3, False, "x")  # one device only: not reported
        found = disagreements(log.iter_events(), LINK)
        assert list(found) == [2]
        assert [(v.device_id, v.verdict) for v in found[2]] == [(PHONE, "match"), (LAPTOP, "different")]
        assert found[2][1].to_dict()["says"] == "different: heard Bob for Governor" and found[2][0].to_dict()["device"] == short_device(PHONE)


def test_a_device_changing_its_own_entry_is_not_a_disagreement(tmp_path: Path) -> None:
    with EventLog.open(tmp_path) as log:
        _entry(log, PHONE, 1, False, "oops")
        _entry(log, PHONE, 1, True)
        _entry(log, LAPTOP, 1, True)
        assert disagreements(log.iter_events(), LINK) == {}


def test_contest_by_contest_outcomes_that_differ(tmp_path: Path) -> None:
    with EventLog.open(tmp_path) as log:
        for device in (PHONE, LAPTOP):
            _contest(log, device, 1, 1, "confirmed")
        _contest(log, PHONE, 1, 2, "confirmed")
        _contest(log, LAPTOP, 1, 2, "heard_instead", "Bob")
        found = disagreements(log.iter_events(), LINK)
        assert list(found) == [1] and {v.device_id for v in found[1]} == {PHONE, LAPTOP}
        # Another observer's or another board's entries never count.
        log.append("observation.entry", "observer:bob", county="Delta", board="1", device_id=PHONE,
                   data={"position": 3, "imprinted_id": "1-1-3", "match": False, "note": "x"})  # fmt: skip
        assert list(disagreements(log.iter_events(), LINK)) == [1]


def test_data_json_reports_the_disagreement(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from corla_observer.quicksetup import populate
    from corla_observer.web.app import AppConfig, create_app
    from corla_observer.workspace import Workspace
    from test_web import SECRET

    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
    app = create_app(AppConfig(workspace=store.root, secret=SECRET, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        before = client.get(f"/observe/{made.observer_token}/data.json").json()
        assert before["disagreements"] == {}
        first = before["ballots"][0]
        # Two devices enter the first ballot differently, against the list's extract, as the page's signed entries do.
        with EventLog.open(store.root) as log:
            for device, match in ((PHONE, True), (LAPTOP, False)):
                log.append("observation.entry", "observer:ann", county="Testland", board="1", device_id=device,
                           data={"position": first["position"], "imprinted_id": first["id"], "match": match,
                                 "note": "" if match else "heard Bob", "extract_sha256": before["extract_sha256"]})  # fmt: skip
        data = client.get(f"/observe/{made.observer_token}/data.json").json()
    assert list(data["disagreements"]) == ["1"]
    assert [v["says"] for v in data["disagreements"]["1"]] == ["matched", "different: heard Bob"]
