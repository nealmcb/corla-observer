"""Fast tests for practice observation: extracts, links, entries, the reader, statistics and their web pages."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.events import EventLog, canonical_json
from corla_observer.practice import (
    DEVIATIONS,
    LinkStore,
    PracticeError,
    build_extract,
    contest_label,
    current_read,
    expected_votes,
    latest_extract,
    match_imprinted_id,
    next_read,
    practice_stats,
    pull_order_key,
    record_entry,
)
from corla_observer.seeds import enter_and_confirm
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from devices import Device

CVR = (
    '"2026 Practice County Primary","5.17.17.1",,,,,,,,,\r\n'
    ',,,,,,,"Governor - DEM (Vote For=1)","Governor - DEM (Vote For=1)","Mayor (Vote For=1)","Mayor (Vote For=1)"\r\n'
    ",,,,,,,Alice,Bob,Carol,Dan\r\n"
    "CvrNumber,TabulatorNum,BatchId,RecordId,ImprintedId,PrecinctPortion,BallotType,,,,\r\n"
    '1,101,1,1,="101-1-1",P1,BT1,1,0,0,1\r\n'  # Alice, Dan
    "2,101,1,2,101-1-2,P1,BT1,0,0,1,1\r\n"  # no vote for governor; overvote for mayor
    "3,101,2,3,101-2-3,P1,BT1,,,0,1\r\n"  # governor not on the card
    "4,101,2,4,101-2-4,P1,BT1,*,*,*,*\r\n"  # redacted
    "5,102,1,9,,P1,BT1,0,1,1,0\r\n"  # no imprinted ID: matched by tabulator, batch and record
)
DRAWN = ["101-2-3", "101-1-1", "102-1-9", "101-2-4", "101-1-2", "103-1-1"]


def make_workspace(root: Path) -> None:
    """A workspace with a county's CVR and a prediction drawing ``DRAWN`` there."""
    ws = Workspace.open(root)
    with EventLog.open(root) as log:
        cvr = ws.put_bytes(CVR.encode())
        log.append(
            "file.received",
            "coordinator:test",
            county="La Plata",
            file_sha256=cvr.sha256,
            data={"kind": "redacted_cvr", "original_name": "cvr.csv", "sha256": cvr.sha256, "size": cvr.size},
        )
        prediction = {
            "election": "2026/practice",
            "seed": "1" * 20,
            "contests": [
                {"name": "Mayor", "draws": [{"index": i + 1, "position": i, "county": "La Plata", "imprinted_id": d} for i, d in enumerate(DRAWN)]},
                {"name": "Governor - DEM", "draws": [{"index": 1, "position": 1, "county": "La Plata", "imprinted_id": "101-1-1"}, {"index": 2, "position": 2, "county": "Weld", "imprinted_id": "9-9-9"}]},
            ],
        }
        stored = ws.put_bytes(canonical_json(prediction))
        enter_and_confirm(log, "1" * 20, "coordinator:test", "test")
        log.append("prediction.made", "coordinator:test", file_sha256=stored.sha256, data={"sha256": stored.sha256})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    make_workspace(path)
    return path


def test_contest_label_and_order() -> None:
    assert contest_label("Governor - DEM (Vote For=1)") == ("Governor - DEM", 1)
    assert contest_label("Council (Vote For=3)") == ("Council", 3)
    assert sorted(["101-10-2", "101-9-30", "100-50-1"], key=pull_order_key) == ["100-50-1", "101-9-30", "101-10-2"]


def test_expected_votes(tmp_path: Path) -> None:
    path = tmp_path / "cvr.csv"
    path.write_text(CVR, newline="")
    found, choices = expected_votes(path, DRAWN)
    assert choices == {"Governor - DEM": ["Alice", "Bob"], "Mayor": ["Carol", "Dan"]}
    assert found["101-1-1"].votes == [("Governor - DEM", "Alice"), ("Mayor", "Dan")]
    assert found["101-1-2"].votes == [("Governor - DEM", "no vote"), ("Mayor", "overvote: Carol + Dan")]
    assert found["101-2-3"].votes == [("Mayor", "Dan")]
    assert found["101-2-4"].redacted and found["101-2-4"].votes == []
    assert found["102-1-9"].votes == [("Governor - DEM", "Bob"), ("Mayor", "Carol")]
    assert "103-1-1" not in found


def test_build_extract(root: Path) -> None:
    ws = Workspace.open(root)
    with EventLog.open(root) as log:
        extract, sha, seq = build_extract(ws, log, "la plata", boards=2)
        assert [b["imprinted_id"] for b in extract["ballots"]] == ["101-1-1", "101-1-2", "101-2-3", "101-2-4", "102-1-9", "103-1-1"]
        assert [b["board"] for b in extract["ballots"]] == [1, 1, 1, 2, 2, 2]
        assert [b["board_position"] for b in extract["ballots"]] == [1, 2, 3, 1, 2, 3]
        first = extract["ballots"][0]
        assert first["picks"] == [["Mayor", "county"], ["Governor - DEM", "county"]]
        assert first["votes"] == [["Governor - DEM", "Alice"], ["Mayor", "Dan"]]
        assert extract["ballots"][3]["status"] == "redacted" and extract["ballots"][5]["status"] == "not_in_cvr"
        assert extract["order"] == "predicted" and extract["purpose"] == "practice"
        assert latest_extract(ws, log, "La Plata") == (extract, sha)
        event = list(log.iter_events())[-1]
        assert event.kind == "extract.built" and event.seq == seq and event.data["boards"] == {"1": 3, "2": 3}
        with pytest.raises(PracticeError):
            build_extract(ws, log, "Weld")  # drawn there, but no CVR
        with pytest.raises(PracticeError):
            build_extract(ws, log, "Atlantis")


def test_links_entries_reader_and_stats(root: Path) -> None:
    ws = Workspace.open(root)
    store = LinkStore(root)
    with EventLog.open(root) as log:
        extract, _, _ = build_extract(ws, log, "La Plata")
        token, link = store.create("observer", "ann", "La Plata", 1, log)
        reader_token, reader = store.create("reader", "bob", "La Plata", 1, log)
        assert store.get(token) == link and store.get("nope") is None
        assert (store.path.stat().st_mode & 0o777) == 0o600
        assert token not in root.joinpath("events.jsonl").read_text()
        with pytest.raises(PracticeError):
            store.create("observer", "bad name", "La Plata", 1)

        # The reader: deterministic deviations with a seeded generator.
        rng = random.Random(3)
        kinds = []
        while True:
            try:
                next_read(log, reader, extract, inject_rate=1.0, rng=rng)
            except PracticeError:
                break
            read = current_read(log, "La Plata", 1)
            assert read is not None
            kinds.append((read["deviation"] or {}).get("type"))
        reads = [e for e in log.iter_events() if e.kind == "practice.read"]
        assert {r.data["position"] for r in reads} == set(range(1, 7))
        deviations = {r.data["position"]: r.data["deviation"] for r in reads}
        # At rate 1 every read is a deliberate difference where one is possible (not the ballot after a skip).
        assert sum(1 for d in deviations.values() if d) >= 3
        assert all(d is None or (isinstance(d, dict) and d.get("type") in DEVIATIONS) for d in deviations.values())
        with pytest.raises(PracticeError):
            next_read(log, link, extract)  # an observer can't read

        # Entries: notes on the first three, matches on the rest, one resent, one edit.
        seen: set[tuple[str, int]] = set()
        for position in range(1, 7):
            entry = {
                "position": position,
                "imprinted_id": extract["ballots"][position - 1]["imprinted_id"],
                "match": position > 3,
                "note": "heard something else" if position <= 3 else "",
                "elapsed_ms": 1000 * position,
                "device_id": "dev-a",
                "device_seq": position,
                "client_time": "2026-10-24T12:00:00.000Z",
            }
            assert record_entry(log, link, entry, seen) is not None
        assert record_entry(log, link, entry, seen) is None  # resent after a lost reply
        edit = entry | {"device_seq": 7, "match": False, "note": "changed my mind", "edit": True, "elapsed_ms": 99_000}
        assert record_entry(log, link, edit, seen) is not None
        with pytest.raises(PracticeError):
            record_entry(log, link, entry | {"device_seq": 8, "match": False, "note": ""}, seen)
        with pytest.raises(PracticeError):
            record_entry(log, reader, entry | {"device_seq": 9}, seen)

        observers, boards = practice_stats(list(log.iter_events()))
    (stats,) = observers
    assert (stats.actor, stats.entries, stats.edits, stats.positions) == ("observer:ann", 6, 1, 6)
    assert (stats.matches, stats.notes) == (2, 4)
    assert stats.median_ms == 3500 and stats.slowest_ms == 6000  # edits are not timed
    assert stats.deviations == sum(1 for d in deviations.values() if d)
    assert stats.caught + stats.missed == stats.deviations
    assert stats.caught == sum(1 for p, d in deviations.items() if d and p in (1, 2, 3, 6))
    (board,) = boards
    assert (board.county, board.board, board.ballots, board.done, board.notes, board.read) == ("La Plata", "1", 6, 6, 4, 6)
    assert kinds  # the reader moved


# ---------------------------------------------------------------------------
# The web pages


@pytest.fixture
def client(root: Path) -> Iterator[TestClient]:
    ws = Workspace.open(root)
    with EventLog.open(root) as log:
        build_extract(ws, log, "La Plata", boards=1)
        AccountStore(root).add("alice", "correct horse battery", log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, path_prefix="/p", sse_max_seconds=0.2, sse_poll_interval=0.05))
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def entry(position: int, seq: int, match: bool = True, note: str = "", **extra: object) -> dict[str, object]:
    return {
        "position": position,
        "imprinted_id": DRAWN[0],
        "match": match,
        "note": note,
        "elapsed_ms": 1500,
        "device_id": "dev-web",
        "device_seq": seq,
        "client_time": "2026-10-24T12:00:00.000Z",
    } | extra


def test_observer_entry_flow(root: Path, client: TestClient) -> None:
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    page = client.get(f"/p/observe/{token}")
    assert page.status_code == 200 and "observe.js" in page.text and "ann" in page.text
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    data = client.get(f"/p/observe/{token}/data.json").json()
    assert [b["id"] for b in data["ballots"]] == ["101-1-1", "101-1-2", "101-2-3", "101-2-4", "102-1-9", "103-1-1"]
    assert data["ballots"][0]["votes"] == [["Governor - DEM", "Alice", "Governor - DEM", ["Alice"]], ["Mayor", "Dan", "Mayor", ["Dan"]]]
    assert data["ballots"][0]["qr"] and data["ident"].startswith("La Plata")
    assert data["entries"] == {}

    url = f"/p/observe/{token}/entries"
    device = Device()
    sent = [device.registration(), device.entry(1, DRAWN[0]), device.entry(2, DRAWN[0], False, "heard Bob")]
    result = client.post(url, json={"entries": sent}).json()
    assert result["recorded"] == [0, 1, 2] and not result["errors"]
    assert sorted(result["receipts"]) == ["0", "1", "2"]
    # Resent after a lost reply (the offline queue): acknowledged, not recorded again.
    again = client.post(url, json={"entries": [sent[2], device.entry(1, DRAWN[0], False, "fix", edit=True)]}).json()
    assert again["duplicates"] == [2] and again["recorded"] == [3]
    assert again["receipts"]["2"]["receipt"]["event_seq"] == result["receipts"]["2"]["receipt"]["event_seq"]
    bad = client.post(url, json={"entries": [device.entry(3, DRAWN[0], False, "")]}).json()
    assert bad["errors"] and bad["errors"][0]["device_seq"] == 4 and bad["errors"][0]["code"] == "invalid"
    unsigned = client.post(url, json={"entries": [entry(3, 5)]}).json()
    assert unsigned["errors"][0]["code"] == "unsigned" and unsigned["errors"][0]["device_seq"] == 5
    assert client.post(url, content=b"entries", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post(url, json={"entries": [entry(1, n) for n in range(200)]}).status_code == 400
    assert client.get("/p/observe/not-a-token").status_code == 404
    assert client.post("/p/observe/not-a-token/entries", json={"entries": []}).status_code == 404

    data = client.get(f"/p/observe/{token}/data.json").json()
    assert data["entries"]["1"]["note"] == "fix" and data["entries"]["2"]["note"] == "heard Bob"
    with EventLog.open(root, read_only=True) as log:
        recorded = [e for e in log.iter_events() if e.kind == "observation.entry"]
    assert [(e.actor, e.county, e.board, e.device_id, e.client_time) for e in recorded][0] == (
        "observer:ann", "La Plata", "1", device.device_id, "2026-10-24T12:00:00.000Z"
    )
    assert len(recorded) == 3
    assert all(e.signature == item["signature"] for e, item in zip(recorded, [sent[1], sent[2]], strict=False))

    # Progress on the public dashboard, and statistics for coordinators.
    dashboard = client.get("/p/").text
    assert "Audit boards, live" in dashboard and "2 of 6" in dashboard and "1 with notes" not in dashboard
    assert "2 with notes" in dashboard
    assert client.get("/p/coord/stats", follow_redirects=False).status_code == 303
    login = client.get("/p/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)
    assert csrf
    client.post("/p/login", data={"csrf": csrf.group(1), "name": "alice", "password": "correct horse battery"})
    stats = client.get("/p/coord/stats")
    assert stats.status_code == 200 and "observer:ann" in stats.text and "1.5 s" in stats.text


def test_two_taps_for_a_match(root: Path, client: TestClient) -> None:
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    page = client.get(f"/p/observe/{token}").text
    assert "First reading" in page and "Confirmed match" in page  # the button, and how it works
    url = f"/p/observe/{token}/entries"
    device = Device()
    sent = [
        device.registration(),
        device.first_reading(1, DRAWN_IN_ORDER[0], elapsed_ms=800),
        device.entry(1, DRAWN_IN_ORDER[0], elapsed_ms=2000),
        device.first_reading(2, DRAWN_IN_ORDER[1], elapsed_ms=1000),
        device.entry(2, DRAWN_IN_ORDER[1], elapsed_ms=3000),
        # A note after the first tap is a difference, not a confirmation.
        device.first_reading(3, DRAWN_IN_ORDER[2], elapsed_ms=900),
        device.entry(3, DRAWN_IN_ORDER[2], False, "heard No on Mayor", elapsed_ms=4000),
    ]
    result = client.post(url, json={"entries": sent}).json()
    assert result["recorded"] == list(range(7)) and not result["errors"]
    # A first reading alone is not an entry: the ballot is not done until it is confirmed.
    data = client.get(f"/p/observe/{token}/data.json").json()
    assert sorted(data["entries"]) == ["1", "2", "3"]
    with EventLog.open(root, read_only=True) as log:
        events = list(log.iter_events())
    assert sum(1 for e in events if e.kind == "observation.first_reading") == 3
    (stats,), (board,) = practice_stats(events)
    assert (stats.first_readings, stats.first_reading_median_ms, stats.confirm_median_ms) == (3, 900, 1600)
    assert (stats.entries, stats.matches, stats.notes, board.done) == (3, 2, 1, 3)
    login = client.get("/p/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)
    assert csrf
    client.post("/p/login", data={"csrf": csrf.group(1), "name": "alice", "password": "correct horse battery"})
    page = client.get("/p/coord/stats").text
    assert "Median time to the first reading" in page and "0.9 s" in page and "1.6 s" in page


def test_reader_pages(root: Path, client: TestClient) -> None:
    token, _ = LinkStore(root).create("reader", "bob", "La Plata", 1)
    page = client.get(f"/p/read/{token}")
    assert page.status_code == 200 and "Press Next" in page.text
    assert client.get(f"/p/observe/{token}", follow_redirects=False).headers["location"] == f"/p/read/{token}"
    response = client.post(f"/p/read/{token}/next", data={"rate": "0"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "101-1-1" in page.text and "Alice" in page.text and "Ballot 1 of 6" in page.text
    # Each ballot is read twice: the first reading, then the second, then Next.
    assert "First reading: read it aloud" in page.text and ">Second reading</button>" in page.text
    assert ">Next ballot</button>" not in page.text
    second = client.get(f"/p/read/{token}?rate=0&step=2").text
    assert "Second reading: read it all aloud again" in second and "101-1-1" in second and ">Next ballot</button>" in second
    for _ in range(10):
        client.post(f"/p/read/{token}/next", data={"rate": "0.33"})
    assert "last ballot" in client.get(f"/p/read/{token}?step=2").text
    client.post(f"/p/read/{token}/restart", data={"rate": "0"})
    assert "Press Next" in client.get(f"/p/read/{token}").text
    observer, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    assert client.get(f"/p/read/{observer}").status_code == 404


def test_cli_practice(root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    built = runner.invoke(cli_app, ["practice", "build", "--workspace", str(root), "--county", "La Plata", "--boards", "2"])
    assert built.exit_code == 0, built.output
    assert "La Plata: 6 ballots (board 1: 3, board 2: 3); 1 redacted, 1 not in the CVR" in built.output
    link = runner.invoke(
        cli_app,
        ["practice", "link", "--workspace", str(root), "--county", "La Plata", "--board", "2", "--observer", "ann", "--base-url", "https://example.org/x1/"],
    )
    assert link.exit_code == 0, link.output
    url = link.stdout.strip().splitlines()[0]
    assert re.fullmatch(r"https://example\.org/x1/observe/[A-Za-z0-9_-]{32}", url)
    stored = LinkStore(root).get(url.rsplit("/", 1)[1])
    assert stored is not None and stored.board == 2
    assert runner.invoke(cli_app, ["practice", "link", "--workspace", str(root), "--county", "La Plata"]).exit_code == 2
    empty = runner.invoke(cli_app, ["practice", "build", "--workspace", str(tmp_path / "new"), "--county", "La Plata"], env={"CORLA_OBSERVER_MIRROR": ""})
    assert empty.exit_code == 2 and "--mirror" in empty.output
    assert json.loads(Workspace.open(root).get_bytes(latest_sha(root)))["boards"] == {"1": 3, "2": 3}


def latest_sha(root: Path) -> str:
    with EventLog.open(root, read_only=True) as log:
        return [e.file_sha256 for e in log.iter_events() if e.kind == "extract.built"][-1] or ""


# ---------------------------------------------------------------------------
# Out-of-order ballots: matching a typed imprinted ID, and what is recorded

#: (typed text, expected status, expected imprinted ID or candidates), against `DRAWN`.
ID_CASES: list[tuple[str, str, object]] = [
    ("101-2-3", "found", "101-2-3"),
    ("  101-1-1 ", "found", "101-1-1"),
    ("101 2 3", "found", "101-2-3"),
    ("101-02-3", "found", "101-2-3"),
    ("10123", "found", "101-2-3"),
    ("2-3", "found", "101-2-3"),
    ("1-2", "found", "101-1-2"),
    ("1-9", "found", "102-1-9"),
    ("1-1", "ambiguous", ["101-1-1", "103-1-1"]),
    ("9-9", "not_found", None),
    ("999", "not_found", None),
    ("12", "not_id", None),
    ("heard Bob for governor", "not_id", None),
    ("101-1-1 Bob", "not_id", None),
]


@pytest.mark.parametrize(("text", "status", "expected"), ID_CASES)
def test_match_imprinted_id(text: str, status: str, expected: object) -> None:
    result = match_imprinted_id(text, DRAWN)
    assert result.status == status
    if status == "found":
        assert result.imprinted_id == expected
    elif status == "ambiguous":
        assert list(result.candidates) == expected


def test_javascript_matcher_agrees() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).parent.parent / "src" / "corla_observer" / "web" / "static" / "idmatch.js"
    program = (
        "const m = require(process.argv[1]); const cases = JSON.parse(process.argv[2]); const ids = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(cases.map(c => m.match(c, ids))));"
    )
    texts = [text for text, _, _ in ID_CASES]
    output = subprocess.run(
        [node, "-e", program, str(script), json.dumps(texts), json.dumps(DRAWN)], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    for (text, _, _), js in zip(ID_CASES, json.loads(output), strict=True):
        py = match_imprinted_id(text, DRAWN)
        assert (js["status"], js["imprinted_id"], js["candidates"]) == (py.status, py.imprinted_id, list(py.candidates)), text


def heard(position: int, seq: int, text: str) -> dict[str, object]:
    return {
        "type": "heard_id",
        "position": position,
        "imprinted_id": DRAWN_IN_ORDER[position - 1],
        "heard": text,
        "match": False,
        "note": text,
        "elapsed_ms": 2500,
        "device_id": "dev-web",
        "device_seq": seq,
        "client_time": "2026-10-24T12:00:00.000Z",
    }


DRAWN_IN_ORDER = ["101-1-1", "101-1-2", "101-2-3", "101-2-4", "102-1-9", "103-1-1"]


def test_out_of_order_entries_are_recorded(root: Path, client: TestClient) -> None:
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    url = f"/p/observe/{token}/entries"
    device = Device()
    client.post(url, json={"entries": [device.registration()]})
    # Expecting ballot 1, the board reads 102-1-9 (typed as its end, "1-9"): out of order.
    result = client.post(url, json={"entries": [device.heard(1, DRAWN_IN_ORDER[0], "1-9")]}).json()
    assert result["recorded"] == [1]
    assert result["heard"] == {"1": {"kind": "observation.out_of_order", "found_position": 5}}
    # The observer matches it, then the board goes back to ballot 1.
    client.post(url, json={"entries": [device.entry(5, "102-1-9"), device.entry(1, DRAWN[0])]})
    # An ID that isn't on this board's list, an ambiguous one, and a resend.
    first = device.heard(2, DRAWN_IN_ORDER[1], "77-7")
    result = client.post(url, json={"entries": [first, device.heard(2, DRAWN_IN_ORDER[1], "1-1"), first]}).json()
    assert result["recorded"] == [4, 5] and result["duplicates"] == [4]
    assert result["heard"]["4"]["kind"] == result["heard"]["5"]["kind"] == "observation.id_not_on_list"
    assert client.post(url, json={"entries": [device.heard(2, DRAWN_IN_ORDER[1], "x" * 65)]}).json()["errors"]

    with EventLog.open(root, read_only=True) as log:
        events = list(log.iter_events())
    out = [e for e in events if e.kind == "observation.out_of_order"]
    assert len(out) == 1
    assert out[0].actor == "observer:ann" and out[0].board == "1" and out[0].device_id == device.device_id
    assert {k: out[0].data[k] for k in ("expected_position", "expected_imprinted_id", "heard", "found_position", "found_imprinted_id")} == {
        "expected_position": 1,
        "expected_imprinted_id": "101-1-1",
        "heard": "1-9",
        "found_position": 5,
        "found_imprinted_id": "102-1-9",
    }
    missing = [e for e in events if e.kind == "observation.id_not_on_list"]
    assert [(e.data["heard"], e.data["match"], e.data["candidates"]) for e in missing] == [
        ("77-7", "not_found", []),
        ("1-1", "ambiguous", ["101-1-1", "103-1-1"]),
    ]
    assert missing[0].data["expected_imprinted_id"] == "101-1-2"
    # The heard IDs are not entries: ballots 2 to 4 are still not done.
    data = client.get(f"/p/observe/{token}/data.json").json()
    assert sorted(data["entries"]) == ["1", "5"]
    observers, _ = practice_stats(events)
    assert (observers[0].out_of_order, observers[0].not_on_list, observers[0].positions) == (1, 2, 2)


def test_pages_revalidate_and_scripts_are_versioned_by_content(root: Path, client: TestClient) -> None:
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    page = client.get(f"/p/observe/{token}")
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/p/").headers["cache-control"] == "public, no-cache"
    tags = re.findall(r'(?:src|href)="(/p/static/[^"]+)" integrity="(sha384-[^"]+)"', page.text)
    assert {t[0].split("?")[0].rsplit("/", 1)[1] for t in tags} == {"app.css", "contestlogic.js", "idmatch.js", "observe.js", "percontest.js", "signing.js"}
    for url, integrity in tags:
        served = client.get(url).content
        assert url.endswith("?v=" + hashlib.sha256(served).hexdigest()[:16])
        assert integrity == "sha384-" + base64.b64encode(hashlib.sha384(served).digest()).decode()
    dashboard = client.get("/p/").text
    assert all("?v=" in tag for tag in re.findall(r'(?:src|href)="/p/static/[^"]+"', dashboard))
