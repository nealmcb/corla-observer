"""Contest-by-contest entry: the page's logic (static/contestlogic.js, run in Node) and the web app on a Testland ballot."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corla_observer.contests import fold
from corla_observer.events import EventLog
from corla_observer.observer_record import check_record, render_report
from corla_observer.receipts import ReceiptSigner, payload_sha256
from corla_observer.web.app import AppConfig, create_app
from devices import Device
from testland import make_testland

SCRIPT = Path(__file__).parent.parent / "src" / "corla_observer" / "web" / "static" / "contestlogic.js"

BALLOTS = [
    {"position": 1, "n": 1, "id": "101-1-1", "votes": [["President", "Ann", "President"], ["Senate", "Bob", "Senate"], ["Prop 1", "no vote", "Prop 1"]]},
    {"position": 2, "n": 2, "id": "101-1-2", "votes": [["President", "Cy", "President"], ["Senate", "Di", "Senate"]]},
    {"position": 3, "n": 3, "id": "101-1-3", "redacted": True, "votes": []},
]

PROGRAM = """
const C = require(process.argv[1]);
const ballots = JSON.parse(process.argv[2]);
const out = {};
const mark = (s, pos, row, reading, outcome, heard) => C.apply(s, {type: "contest", position: pos,
  content: {row: row, reading: reading, outcome: outcome, heard: heard || ""}}, 1);
let s = {};
// Ballot 1, first reading: Ann confirmed, Senate unclear, Prop 1 heard "Yes".
mark(s, 1, 1, 1, "confirmed"); mark(s, 1, 2, 1, "unclear"); mark(s, 1, 3, 1, "heard_instead", "Yes");
out.first = C.unresolvedRows(s, ballots[0], 1);
out.firstAsSecond = C.unresolvedRows(s, ballots[0], 2);
// Second reading: all confirmed but Prop 1 heard " yes " again (the same, ignoring case and spaces).
mark(s, 1, 1, 2, "confirmed"); mark(s, 1, 2, 2, "confirmed"); mark(s, 1, 3, 2, "heard_instead", " YES ");
out.second = C.unresolvedRows(s, ballots[0], 2);
out.complete = C.complete(s, ballots[0]);
out.summary = C.summary(s, ballots[0]);
// Change Prop 1's second reading to confirmed: now the readings differ.
mark(s, 1, 3, 2, "confirmed");
out.differs = C.differs(C.marks(s, 1, 3));
out.afterChange = C.unresolvedRows(s, ballots[0], 2);
out.summary2 = C.summary(s, ballots[0]);
// Ballot 2 started (first reading of one row): next unresolved from ballot 2 row 1, then ballot 1.
mark(s, 2, 1, 1, "confirmed");
const reading = b => (b.position === 2 ? 1 : 2);
out.next1 = C.nextUnresolved(s, ballots, 1, 1, reading);
out.next2 = C.nextUnresolved(s, ballots, 1, 2, reading);
mark(s, 2, 2, 1, "confirmed");
out.next3 = C.nextUnresolved(s, ballots, 1, 2, reading);
// A redacted ballot has no rows: done only when marked done.
out.redacted = [C.ballotRows(ballots[2]), C.done(s, ballots[2])];
C.apply(s, {type: "ballot_done", position: 3, content: {}}, 2);
out.redactedDone = C.done(s, ballots[2]);
out.server = C.fromServer({"1": {rows: {"2": {"1": {outcome: "unclear", heard: "", seq: 5}}}, done: true}});
out.suggest = [
  C.suggestions("gar", ["Cory Gardner", "John Hickenlooper", "Raina Garcia"]).map(x => x.label),
  C.suggestions("john h", ["Cory Gardner", "John Hickenlooper"]).map(x => x.label),
  C.suggestions("no", ["Yes/For", "No/Against"]).map(x => x.label),
  C.suggestions("zzz", ["Yes/For"]).map(x => [x.label, x.value, x.kind]),
  C.suggestions("", ["Yes/For", "No/Against"]).map(x => x.label),
  C.suggestions("yes/for", ["Yes/For", "No/Against"]).map(x => x.kind),
];
process.stdout.write(JSON.stringify(out));
"""


def test_contest_logic_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = subprocess.run([node, "-e", PROGRAM, str(SCRIPT), json.dumps(BALLOTS)], capture_output=True, text=True, check=True, timeout=60).stdout
    out: dict[str, Any] = json.loads(output)
    assert out["first"] == [2]
    assert out["firstAsSecond"] == [1, 2, 3]
    assert out["second"] == [] and out["complete"] is True
    assert out["summary"] == {"match": False, "note": "Prop 1: heard “ YES ”"}
    assert out["differs"] is True and out["afterChange"] == [3]
    assert out["summary2"]["note"] == "Prop 1: first reading heard “Yes”, second confirmed"
    assert out["next1"] == {"index": 1, "row": 2}
    assert out["next2"] == {"index": 0, "row": 3}  # nothing after row 2 here: an earlier ballot's
    assert out["next3"] == {"index": 0, "row": 3}
    assert out["redacted"] == [0, False] and out["redactedDone"] is True
    assert out["server"] == {"1": {"rows": {"2": {"1": {"outcome": "unclear", "heard": "", "seq": 5}}}, "done": True}}
    gar, john, no, other, empty, exact = out["suggest"]
    assert gar == ["Cory Gardner", "Raina Garcia", "other: gar"]
    assert john[0] == "John Hickenlooper"
    assert no == ["No/Against", "NO VOTE", "other: no"]
    assert other == [["other: zzz", "zzz", "other"]]
    assert empty == ["Yes/For", "No/Against", "NO VOTE", "overvote"]
    assert "other" not in exact


# ---------------------------------------------------------------------------
# The web app, on Testland's long ballots


@pytest.fixture
def testland(tmp_path: Path) -> Iterator[tuple[TestClient, str, ReceiptSigner, Path]]:
    root = tmp_path / "ws"
    (token,) = make_testland(root)
    signer = ReceiptSigner.generate()
    with TestClient(create_app(AppConfig(workspace=root, secret=b"s" * 32, receipt_key=signer)), base_url="https://testserver") as client:
        yield client, token, signer, root


def test_testland_ballot_contest_by_contest_through_the_web(testland: tuple[TestClient, str, ReceiptSigner, Path]) -> None:
    client, token, signer, root = testland
    page = client.get(f"/observe/{token}").text
    for needed in ("percontest.js", "contestlogic.js", 'id="obs-mode-per_contest"', 'id="pc-keys"', 'id="obs-intro-mode-per_contest"', "Alt+J"):
        assert needed in page
    data = client.get(f"/observe/{token}/data.json").json()
    assert data["mode"] is None and data["contests"] == {}
    longest = max(data["ballots"], key=lambda b: len(b["votes"]))
    votes = longest["votes"]
    assert len(votes) == 60 and all(len(v) == 4 and v[3] for v in votes)
    assert all(v[2] in data["choices"] for v in votes)
    assert any(len(data["choices"][v[2]]) > 2 for v in votes)

    device = Device(observer=data["observer"], county="Testland", board="1", extract=data["extract_sha256"])
    position, imprinted = longest["position"], longest["id"]
    items = [device.registration(), device.entry_mode("per_contest", position, imprinted)]
    for reading in (1, 2):
        for row, (_, expected, contest, _shown) in enumerate(votes, start=1):
            unclear = reading == 1 and row == 7
            items.append(device.contest(position, imprinted, row, 60, contest, expected, reading=reading, outcome="unclear" if unclear else "confirmed"))
    items.append(device.ballot_done(position, imprinted, 60))
    receipts: dict[str, Any] = {}
    # The page sends at most 50 at a time; the server takes up to 100.
    for start in range(0, len(items), 50):
        result = client.post(f"/observe/{token}/entries", json={"entries": items[start:start + 50]}).json()
        assert not result["errors"], result["errors"]
        receipts |= result["receipts"]
    assert len(receipts) == len(items) == 123
    data = client.get(f"/observe/{token}/data.json").json()
    assert data["mode"] == "per_contest"
    state = data["contests"][str(position)]
    assert state["done"] is True and len(state["rows"]) == 60 and state["rows"]["7"]["1"]["outcome"] == "unclear"
    entry = data["entries"][str(position)]
    assert entry["per_contest"] is True and entry["match"] is True and entry["note"] == ""

    record = {
        "format": "corla-observer-record", "version": 1, "observer": device.observer, "county": "Testland", "board": "1",
        "device_id": device.device_id, "public_key": device.public_key,
        "entries": [{"payload": i["payload"], "signature": i["signature"], "payload_sha256": payload_sha256(i["payload"]),
                     "receipt": receipts[str(i["payload"]["device_seq"])], "refused": None} for i in items],
    }  # fmt: skip
    report = check_record(record, root / "events.jsonl", server_key=signer.public_raw)
    assert report.ok and report.receipts == 123, render_report(report)

    stats = client.get("/dashboard.json")
    assert stats.status_code == 200
    with EventLog.open(root) as log:
        rows = fold(log.iter_events())[(device.observer, "Testland", "1")][position]
    assert rows.match and rows.complete and rows.marked_done


def test_coordinator_statistics_compare_observers(testland: tuple[TestClient, str, ReceiptSigner, Path]) -> None:
    from corla_observer.practice import LinkStore
    from corla_observer.web.accounts import AccountStore
    from test_web import PASSWORD, login

    client, token, _, root = testland
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", PASSWORD, log)
        bob_token, _ = LinkStore(root).create("observer", "bob", "Testland", 1, log)
    data = client.get(f"/observe/{token}/data.json").json()
    first = data["ballots"][0]
    votes = first["votes"]
    lori = Device(observer=data["observer"], county="Testland", board="1", extract=data["extract_sha256"])
    bob = Device(observer="observer:bob", county="Testland", board="1", extract=data["extract_sha256"])
    items = [lori.registration(), lori.entry_mode("per_contest", first["position"], first["id"])]
    for reading in (1, 2):
        for row, (_, expected, contest, _shown) in enumerate(votes, start=1):
            heard = row == 2
            items.append(lori.contest(first["position"], first["id"], row, len(votes), contest, expected, reading=reading,
                                      outcome="heard_instead" if heard else "confirmed", heard="Someone Else" if heard else ""))  # fmt: skip
    assert not client.post(f"/observe/{token}/entries", json={"entries": items[:50]}).json()["errors"]
    assert not client.post(f"/observe/{token}/entries", json={"entries": items[50:]}).json()["errors"]
    assert not client.post(f"/observe/{bob_token}/entries", json={"entries": [bob.registration(), bob.entry(first["position"], first["id"])]}).json()["errors"]
    login(client)
    page = client.get("/coord/stats").text
    assert "Comparing observers" in page and "conflict" in page
    assert f"observer:lori: {votes[1][2]} (expected {votes[1][1]}): heard “Someone Else”" in page
    assert "contest by contest" in page and "two taps" in page


def test_public_dashboard_counts_contest_by_contest_observers(tmp_path: Path) -> None:
    from corla_observer.practice import LinkStore, build_extract
    from corla_observer.workspace import Workspace
    from test_practice import make_workspace

    root = tmp_path / "ws"
    make_workspace(root)
    with EventLog.open(root) as log:
        build_extract(Workspace.open(root), log, "La Plata", boards=1)
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    with TestClient(create_app(AppConfig(workspace=root, secret=b"s" * 32, receipt_key=ReceiptSigner.generate())), base_url="https://testserver") as client:
        data = client.get(f"/observe/{token}/data.json").json()
        first = data["ballots"][0]
        device = Device(extract=data["extract_sha256"])
        items = [device.registration(), device.entry_mode("per_contest", first["position"], first["id"])]
        for reading in (1, 2):
            for row, (_, expected, contest, _shown) in enumerate(first["votes"], start=1):
                items.append(device.contest(first["position"], first["id"], row, len(first["votes"]), contest, expected, reading=reading))
        assert not client.post(f"/observe/{token}/entries", json={"entries": items}).json()["errors"]
        page = client.get("/").text
    assert "1 contest by contest" in page
    assert "Ballots done</dt><dd>1 of" in page
