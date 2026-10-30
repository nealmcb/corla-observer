"""Fast tests for device-signed entries, receipts and checking observers' records (docs/RECEIPTS.md)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.events import EventLog, canonical_json
from corla_observer.observer_record import Finding, RecordError, check_record, load_record, render_report
from corla_observer.practice import Link, LinkStore, build_extract
from corla_observer.receipts import (
    PayloadError,
    ReceiptKeyError,
    ReceiptSigner,
    check_payload,
    load_receipt_key,
    parse_server_public_key,
    payload_sha256,
    receipt_code,
    verify_device_signature,
    verify_receipt,
)
from corla_observer.signed import KEY_KIND, REGISTERED_KIND, REJECTED_KIND, DeviceRegistry, Outcome, process_item
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from devices import Device
from test_practice import DRAWN_IN_ORDER, make_workspace

LINK = Link("observer", "ann", "La Plata", 1, "2026-10-24T00:00:00Z")
BALLOTS = [{"position": i + 1, "imprinted_id": imprinted} for i, imprinted in enumerate(DRAWN_IN_ORDER)]


class Server:
    """The server side of `corla_observer.signed`, on a plain event log."""

    def __init__(self, root: Path, signer: ReceiptSigner | None = None) -> None:
        self.log = EventLog.open(root)
        self.signer = signer or ReceiptSigner.generate()
        self.registry = DeviceRegistry(self.log)
        self.seen: set[tuple[str, int]] = set()
        self.jsonl = root / "events.jsonl"

    def send(self, *items: dict[str, Any], link: Link = LINK) -> list[Outcome]:
        self.registry.refresh()
        return [
            process_item(self.log, link, item, registry=self.registry, signer=self.signer, seen=self.seen, ballots=lambda sha: BALLOTS)
            for item in items
        ]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    s = Server(tmp_path / "log")
    yield s
    s.log.close()


def record_of(device: Device, outcomes: dict[int, Outcome], items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The record a page would download: every item with its receipt or refusal."""
    entries = []
    for item in items if items is not None else device.items:
        seq = item["payload"]["device_seq"]
        outcome = outcomes.get(seq)
        entries.append({
            "payload": item["payload"],
            "signature": item["signature"],
            "payload_sha256": payload_sha256(item["payload"]),
            "receipt": outcome.receipt if outcome and outcome.receipt else None,
            "refused": {"code": outcome.code, "error": outcome.error} if outcome and outcome.status == "rejected" else None,
        })
    return {
        "format": "corla-observer-record", "version": 1, "observer": device.observer, "county": device.county,
        "board": device.board, "device_id": device.device_id, "public_key": device.public_key, "entries": entries,
    }


def by_seq(outcomes: list[Outcome]) -> dict[int, Outcome]:
    return {o.device_seq: o for o in outcomes if o.device_seq is not None}


def kinds(findings: list[Finding]) -> set[str]:
    return {f.kind for f in findings if f.severity == "problem"}


# ---------------------------------------------------------------------------
# Signatures and payloads


def test_device_signatures_verify_and_tampering_is_caught() -> None:
    device = Device()
    registration = device.registration()
    assert check_payload(registration["payload"])["device_id"] == device.device_id
    assert verify_device_signature(device.public_key, registration["payload"], registration["signature"])
    item = device.entry(1, "101-1-1", False, "heard Bob")
    check_payload(item["payload"])
    assert verify_device_signature(device.public_key, item["payload"], item["signature"])
    tampered = copy.deepcopy(item["payload"])
    tampered["content"]["note"] = "heard Alice"
    assert not verify_device_signature(device.public_key, tampered, item["signature"])
    assert not verify_device_signature(Device().public_key, item["payload"], item["signature"])
    assert not verify_device_signature(device.public_key, item["payload"], "c2lnbmF0dXJl")
    assert not verify_device_signature(device.public_key, item["payload"], None)
    # The DER form is not accepted: Web Crypto signs as r || s.
    assert not verify_device_signature(device.public_key, item["payload"], base64.b64encode(b"0" * 70).decode())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.update(extra=1), "fields"),
        (lambda p: p.update(v=2), "v must be"),
        (lambda p: p.update(device_seq=0), "device_seq"),
        (lambda p: p.update(device_id="d-" + "0" * 32), None),  # still well formed: caught by the signature
        (lambda p: p.update(client_time="yesterday"), "client_time"),
        (lambda p: p["content"].update(note="a\x07b"), "note"),
        (lambda p: p["content"].update(match=False, note="  "), "match or a note"),
        (lambda p: p["content"].update(elapsed_ms=1.5), "elapsed_ms"),
        (lambda p: p.update(type="undo"), "type"),
    ],
)
def test_check_payload_rejects(change: Any, message: str | None) -> None:
    payload = Device().entry(1, "101-1-1")["payload"]
    change(payload)
    if message is None:
        check_payload(payload)
        return
    with pytest.raises(PayloadError, match=message):
        check_payload(payload)


def test_registration_device_id_is_bound_to_the_key() -> None:
    device = Device()
    payload = device.registration()["payload"]
    payload["public_key"] = Device().public_key
    with pytest.raises(PayloadError, match="derived from public_key"):
        check_payload(payload)


# ---------------------------------------------------------------------------
# The server: recording, refusing and receipts


def test_entries_are_recorded_with_signatures_and_receipts(server: Server) -> None:
    device = Device()
    registration = device.registration()
    outcomes = server.send(registration, device.entry(1, "101-1-1"), device.heard(2, "101-1-2", "1-9"), device.heard(2, "101-1-2", "77-7"))
    assert [o.status for o in outcomes] == ["recorded"] * 4
    assert outcomes[2].heard == {"kind": "observation.out_of_order", "found_position": 5}
    events = list(server.log.iter_events())
    assert [e.kind for e in events] == [
        KEY_KIND, REGISTERED_KIND, "observation.entry", "observation.out_of_order", "observation.id_not_on_list"
    ]
    assert events[0].data == server.signer.public_info()
    registered = events[1]
    assert registered.data["public_key"] == device.public_key and registered.signature == registration["signature"]
    for event, item in zip(events[1:], device.items, strict=True):
        assert event.signature == item["signature"]
        assert event.data["payload"] == item["payload"]
        assert event.data["payload_sha256"] == payload_sha256(item["payload"])
        assert event.device_id == device.device_id and event.client_time == item["payload"]["client_time"]
    for outcome, event in zip(outcomes, events[1:], strict=True):
        assert outcome.receipt is not None
        body = outcome.receipt["receipt"]
        assert isinstance(body, dict)
        assert (body["event_seq"], body["event_hash"], body["event_kind"]) == (event.seq, event.hash, event.kind)
        assert body["head_seq"] == event.seq and body["server_time"] == event.server_time
        assert verify_receipt(outcome.receipt, server.signer.public_raw)
        assert not verify_receipt(outcome.receipt, ReceiptSigner.generate().public_raw)
        forged: Any = copy.deepcopy(outcome.receipt)
        forged["receipt"]["head_seq"] = 99
        assert not verify_receipt(forged, server.signer.public_raw)
        assert receipt_code(outcome.receipt) == f"{event.seq}-{event.hash[:6]}"
    # The key is published once, however many receipts follow.
    server.send(device.entry(3, "101-2-3"))
    assert sum(1 for e in server.log.iter_events() if e.kind == KEY_KIND) == 1


def test_first_readings_are_signed_recorded_and_checked(server: Server, tmp_path: Path) -> None:
    device = Device()
    registration = device.registration()
    first = device.first_reading(1, "101-1-1", elapsed_ms=900)
    assert check_payload(first["payload"])["content"] == {"elapsed_ms": 900}
    outcomes = server.send(registration, first, device.entry(1, "101-1-1", elapsed_ms=2100))
    assert [o.status for o in outcomes] == ["recorded"] * 3
    event = next(e for e in server.log.iter_events() if e.kind == "observation.first_reading")
    assert {k: event.data[k] for k in ("position", "imprinted_id", "elapsed_ms", "device_seq")} == {
        "position": 1, "imprinted_id": "101-1-1", "elapsed_ms": 900, "device_seq": 1,
    }  # fmt: skip
    assert event.signature == first["signature"] and event.data["payload"] == first["payload"]
    receipt = outcomes[1].receipt
    assert receipt is not None and verify_receipt(receipt, server.signer.public_raw)
    body = receipt["receipt"]
    assert isinstance(body, dict) and body["event_kind"] == "observation.first_reading"
    # A first reading carries only its time.
    bad = copy.deepcopy(first["payload"])
    bad["content"]["match"] = True
    with pytest.raises(PayloadError, match="content fields"):
        check_payload(bad)
    # The checker checks first readings like any entry, including their plain fields.
    record = record_of(device, by_seq(outcomes))
    report = check_record(record, server.jsonl, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    server.log.close()
    rewritten = tmp_path / "rewritten.jsonl"
    with EventLog.open(tmp_path / "copy") as log:
        for line in server.jsonl.read_bytes().splitlines():
            e = json.loads(line)
            data = e["data"] | ({"elapsed_ms": 1} if e["kind"] == "observation.first_reading" else {})
            log.append(e["kind"], e["actor"], data=data, client_time=e["client_time"], county=e["county"],
                       board=e["board"], device_id=e["device_id"], signature=e["signature"])  # fmt: skip
    shutil.copy(tmp_path / "copy" / "events.jsonl", rewritten)
    report = check_record(record, rewritten, server_key=server.signer.public_raw)
    assert any("(position 1, 101-1-1: first reading)" in f.message and "elapsed_ms" in f.message for f in report.problems)
    server.log = EventLog.open(tmp_path / "log")


def test_unsigned_tampered_and_unknown_entries_are_refused(server: Server) -> None:
    device = Device()
    unregistered = device.entry(1, "101-1-1")
    assert server.send(unregistered)[0].code == "unknown_device"
    device.seq, device.prev, device.items = 0, "0" * 64, []
    server.send(device.registration())
    tampered = device.entry(1, "101-1-1", False, "heard Bob")
    tampered = copy.deepcopy(tampered)
    tampered["payload"]["content"]["note"] = "heard Alice"
    unsigned = {"position": 1, "imprinted_id": "101-1-1", "match": True, "device_id": "dev-x", "device_seq": 9}
    other = Device(county="Weld")
    results = server.send(
        tampered,
        unsigned,
        {"payload": device.entry(2, "101-1-2")["payload"]},
        other.registration(),
        {"payload": device.entry(3, "101-2-3")["payload"], "signature": "not base64!"},
    )
    assert [o.code for o in results] == ["bad_signature", "unsigned", "unsigned", "wrong_link", "bad_signature"]
    assert all(o.status == "rejected" and o.error for o in results)
    assert results[1].device_seq == 9  # an older page's unsigned entry, reported by its sequence number
    assert "signed on the device" in str(results[1].error)
    rejected = [e for e in server.log.iter_events() if e.kind == REJECTED_KIND]
    assert [e.data["code"] for e in rejected] == ["unknown_device", "bad_signature", "unsigned", "unsigned", "wrong_link", "bad_signature"]
    assert rejected[1].data["payload_sha256"] == payload_sha256(tampered["payload"])
    assert "payload" not in rejected[1].data and rejected[1].device_id == device.device_id
    assert not any(e.kind == "observation.entry" for e in server.log.iter_events())


def test_conflicting_resend_and_duplicate_registration(server: Server) -> None:
    device = Device()
    registration = device.registration()
    first = device.entry(1, "101-1-1")
    server.send(registration, first)
    again = server.send(registration, first)
    assert [o.status for o in again] == ["duplicate", "duplicate"]
    # A different payload under a sequence number already used (a second tab, say) is refused.
    device.seq, device.prev = 0, payload_sha256(registration["payload"])
    clash = device.entry(1, "101-1-1", False, "heard Bob")
    assert server.send(clash)[0].code == "conflict"
    # A second device for the same observer gets its own key and device ID.
    second = Device()
    assert second.device_id != device.device_id
    assert [o.status for o in server.send(second.registration(), second.entry(1, "101-1-1"))] == ["recorded", "recorded"]
    assert len({k for k in server.registry.keys if k[0] == LINK.actor}) == 2
    # Another process's registry sees the same keys from the log.
    registry = DeviceRegistry(server.log)
    registry.refresh()
    assert registry.keys == server.registry.keys


def test_offline_queue_resend_keeps_the_chain(server: Server, tmp_path: Path) -> None:
    device = Device()
    queue = [device.registration()] + [device.entry(n, DRAWN_IN_ORDER[n - 1]) for n in range(1, 6)]
    # The first send reaches the server but its reply is lost; the device sends its whole queue again.
    lost = server.send(*queue[:3])
    resent = server.send(*queue)
    assert [o.status for o in resent] == ["duplicate"] * 3 + ["recorded"] * 3
    for a, b in zip(lost, resent, strict=False):
        first: Any = a.receipt
        again: Any = b.receipt
        assert first["receipt"]["event_seq"] == again["receipt"]["event_seq"]
    entries = [e for e in server.log.iter_events() if e.kind == "observation.entry"]
    assert [e.data["device_seq"] for e in entries] == [1, 2, 3, 4, 5]
    payloads: list[Any] = [e.data["payload"] for e in entries]
    chain = [p["prev_payload_sha256"] for p in payloads]
    assert chain == [payload_sha256(item["payload"]) for item in queue[:-1]]
    record = record_of(device, by_seq(resent))
    report = check_record(record, server.jsonl, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    assert report.entries == 6 and report.receipts == 6 and report.anchors_checked == 12


# ---------------------------------------------------------------------------
# The checker


def honest(server: Server) -> tuple[Device, dict[int, Outcome]]:
    device = Device()
    outcomes = server.send(
        device.registration(), device.entry(1, "101-1-1"), device.entry(2, "101-1-2", False, "heard Bob"),
        device.heard(3, "101-2-3", "1-9"), device.entry(3, "101-2-3"),
    )
    return device, by_seq(outcomes)


def test_checker_passes_an_honest_record(server: Server, tmp_path: Path) -> None:
    device, outcomes = honest(server)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record_of(device, outcomes)))
    report = check_record(load_record(path), server.jsonl, server_key=server.signer.public_raw)
    assert report.ok and not report.findings, render_report(report)
    # Without an independent key, the log's own copy is used, and that is noted.
    report = check_record(load_record(path), server.jsonl)
    assert report.ok and {f.kind for f in report.findings} == {"server_key"}
    with pytest.raises(RecordError):
        path.write_text('{"format": "something else"}')
        load_record(path)


def test_checker_finds_entries_missing_differing_and_not_by_the_device(server: Server) -> None:
    device, outcomes = honest(server)
    device.entry(4, "101-2-4")  # never sent
    refused = device.entry(5, "102-1-9", False, "")  # neither a match nor a note
    outcomes |= by_seq(server.send(refused))
    # The server records an entry under this device signed by another key, and an unsigned one.
    forged = Device(observer=device.observer)
    forged.device_id, forged.seq, forged.prev = device.device_id, 6, "0" * 64
    fake = forged.entry(6, "103-1-1")  # device sequence number 7
    server.log.append(
        "observation.entry", LINK.actor, county=LINK.county, board="1", device_id=device.device_id,
        client_time=fake["payload"]["client_time"], signature=fake["signature"],
        data={"position": 6, "imprinted_id": "103-1-1", "match": True, "note": "", "edit": False, "elapsed_ms": 1500,
              "device_seq": 7, "payload": fake["payload"], "payload_sha256": payload_sha256(fake["payload"])},
    )
    server.log.append(
        "observation.entry", LINK.actor, county=LINK.county, board="1", device_id=device.device_id,
        data={"position": 1, "imprinted_id": "101-1-1", "match": True, "note": "", "edit": False, "device_seq": 8},
    )
    record = record_of(device, outcomes)
    report = check_record(record, server.jsonl, server_key=server.signer.public_raw)
    problems = {(f.kind, f.device_seq) for f in report.problems}
    assert problems == {("missing", 5), ("refused", 6), ("not_by_device", None)}
    assert sum(1 for f in report.problems if f.kind == "not_by_device") == 2
    assert "refused it" in next(f.message for f in report.problems if f.kind == "refused")

    # The log holds a different entry under one of the record's sequence numbers.
    altered = copy.deepcopy(record)
    altered["entries"][2]["payload"]["content"]["note"] = "heard Carol"
    report = check_record(altered, server.jsonl, server_key=server.signer.public_raw)
    assert {"bad_device_signature", "differs", "receipt_mismatch"} <= kinds(report.findings)


def test_checker_finds_plain_fields_that_differ_from_the_signed_payload(tmp_path: Path) -> None:
    server = Server(tmp_path / "log")
    device, outcomes = honest(server)
    server.log.close()
    lines = server.jsonl.read_bytes().splitlines(keepends=True)
    # Rewrite the whole chain from the "heard Bob" entry on, as a server with control of the log could.
    rewritten = tmp_path / "rewritten.jsonl"
    with EventLog.open(tmp_path / "copy") as log:
        for line in lines:
            event = json.loads(line)
            data = event["data"]
            if data.get("note") == "heard Bob":
                data = data | {"note": "", "match": True}
            log.append(
                event["kind"], event["actor"], data=data, client_time=event["client_time"], county=event["county"],
                board=event["board"], device_id=event["device_id"], signature=event["signature"],
            )
    shutil.copy(tmp_path / "copy" / "events.jsonl", rewritten)
    report = check_record(record_of(device, outcomes), rewritten, server_key=server.signer.public_raw)
    found = kinds(report.findings)
    assert "differs" in found and "anchor_mismatch" in found
    assert any("match, note" in f.message for f in report.problems if f.kind == "differs")


def test_checker_finds_broken_device_chain_and_bad_receipts(server: Server, tmp_path: Path) -> None:
    device, outcomes = honest(server)
    record = record_of(device, outcomes)
    gap = copy.deepcopy(record)
    del gap["entries"][2]
    report = check_record(gap, server.jsonl, server_key=server.signer.public_raw)
    assert kinds(report.findings) == {"device_chain"}
    assert {f.kind for f in report.findings if f.severity == "note"} == {"not_in_record"}

    # Receipts checked against another key, a receipt for another event, and a receipt whose event isn't in the log.
    report = check_record(record, server.jsonl, server_key=ReceiptSigner.generate().public_raw)
    assert "receipt_signature" in kinds(report.findings) and "server_key" in kinds(report.findings)
    swapped = copy.deepcopy(record)
    first, second = (e["receipt"]["receipt"] for e in swapped["entries"][1:3])
    other = next(e for e in server.log.iter_events() if e.seq == second["event_seq"])
    receipt = server.signer.issue(other, server.log.head(), first["payload_sha256"])
    swapped["entries"][1]["receipt"] = receipt
    report = check_record(swapped, server.jsonl, server_key=server.signer.public_raw)
    assert kinds(report.findings) == {"receipt_not_in_log"}


def test_checker_finds_a_broken_or_truncated_log(server: Server, tmp_path: Path) -> None:
    device, outcomes = honest(server)
    record = record_of(device, outcomes)
    lines = server.jsonl.read_bytes().splitlines(keepends=True)
    truncated = tmp_path / "truncated.jsonl"
    truncated.write_bytes(b"".join(lines[:3]))
    report = check_record(record, truncated, server_key=server.signer.public_raw)
    assert {"log_truncated", "missing"} <= kinds(report.findings)
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(b"".join(lines[:2]) + lines[2].replace(b'"match":true', b'"match":false') + b"".join(lines[3:]))
    report = check_record(record, broken, server_key=server.signer.public_raw)
    assert "log_broken" in kinds(report.findings) and not report.log.ok
    head = server.log.head()
    report = check_record(record, server.jsonl, server_key=server.signer.public_raw, anchors=[(head[0], head[1]), (2, "0" * 64)])
    assert kinds(report.findings) == {"anchor_mismatch"}


def test_cli_check_record(server: Server, tmp_path: Path) -> None:
    device, outcomes = honest(server)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record_of(device, outcomes)))
    key_path = tmp_path / "receipt-key.json"
    key_path.write_text(json.dumps(server.signer.public_info()))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["check-record", str(record_path), str(server.jsonl), "--server-key", str(key_path)])
    assert result.exit_code == 0, result.output
    assert "OK: every entry" in result.output and "(given)" in result.output
    pem = runner.invoke(cli_app, ["check-record", str(record_path), str(server.jsonl), "--server-key", server.signer.public_b64, "--json"])
    assert pem.exit_code == 0 and json.loads(pem.stdout)["ok"] is True
    other = ReceiptSigner.generate().public_b64
    bad = runner.invoke(cli_app, ["check-record", str(record_path), str(server.jsonl), "--server-key", other])
    assert bad.exit_code == 1 and "PROBLEM [receipt_signature]" in bad.output
    assert runner.invoke(cli_app, ["check-record", str(tmp_path / "none.json"), str(server.jsonl)]).exit_code == 2


def test_receipt_key_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "receipt-key.pem"
    signer = load_receipt_key(path, workspace=tmp_path / "ws")
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_receipt_key(path).key_id == signer.key_id
    assert parse_server_public_key(signer.public_pem) == signer.public_raw
    assert parse_server_public_key(json.dumps(signer.public_info())) == signer.public_raw
    with pytest.raises(ReceiptKeyError):
        load_receipt_key(tmp_path / "ws" / "key.pem", workspace=tmp_path / "ws")
    path.chmod(0o644)
    with pytest.raises(ReceiptKeyError):
        load_receipt_key(path)


# ---------------------------------------------------------------------------
# Through the web app, and the page's own signing code


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, str, ReceiptSigner, Path]]:
    root = tmp_path / "ws"
    make_workspace(root)
    with EventLog.open(root) as log:
        build_extract(Workspace.open(root), log, "La Plata", boards=1)
    token, _ = LinkStore(root).create("observer", "ann", "La Plata", 1)
    signer = ReceiptSigner.generate()
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, receipt_key=signer, path_prefix="/p"))
    with TestClient(app, base_url="https://testserver") as client:
        yield client, token, signer, root


def test_web_receipts_and_published_key(web: tuple[TestClient, str, ReceiptSigner, Path]) -> None:
    client, token, signer, root = web
    published = client.get("/p/receipt-key").json()
    assert published == signer.public_info()
    assert parse_server_public_key(client.get("/p/receipt-key.pem").text) == signer.public_raw
    assert signer.key_id in client.get("/p/").text
    data = client.get(f"/p/observe/{token}/data.json").json()
    assert data["receipt_key"] == published
    device = Device(extract=data["extract_sha256"])
    result = client.post(f"/p/observe/{token}/entries", json={"entries": [device.registration(), device.entry(1, "101-1-1")]}).json()
    assert result["recorded"] == [0, 1]
    for receipt in result["receipts"].values():
        assert verify_receipt(receipt, parse_server_public_key(json.dumps(published)))
    page = client.get(f"/p/observe/{token}").text
    assert "Download my record" in page and "signing.js" in page


JS_VALUES: list[Any] = [
    {"b": 1, "a": [True, None, -5, "x"], "é": {"z": "", "y": " /\"\\\n\t\x01\x7f"}},
    "heard “No” on 2",
    [0, 9007199254740991, {"": "empty key"}],
]


def test_javascript_signing_matches_python(web: tuple[TestClient, str, ReceiptSigner, Path]) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    client, token, _, _ = web
    data = client.get(f"/p/observe/{token}/data.json").json()
    script = Path(__file__).parent.parent / "src" / "corla_observer" / "web" / "static" / "signing.js"
    program = """
const S = require(process.argv[1]);
const values = JSON.parse(process.argv[2]);
const link = JSON.parse(process.argv[3]);
(async () => {
  const made = await S.newDevice(link, "2026-10-24T12:00:00.000Z");
  const items = [made.registration];
  const note = S.cleanText("  heard \\u0007Bob, “no” 🗳 \\u0085 ", 500);
  items.push(await S.signNext(made.device, {type: "entry", extract_sha256: link.extract, position: 1, imprinted_id: "101-1-1",
    content: {match: false, note: note, edit: false, elapsed_ms: 1200}, client_time: "2026-10-24T12:00:01.000Z"}));
  items.push(await S.signNext(made.device, {type: "heard_id", extract_sha256: link.extract, position: 2, imprinted_id: "101-1-2",
    content: {heard: S.cleanText("1-9", 64), elapsed_ms: null}, client_time: "2026-10-24T12:00:02.000Z"}));
  items.push(await S.signNext(made.device, {type: "entry", extract_sha256: link.extract, position: 2, imprinted_id: "101-1-2",
    content: {match: true, note: S.cleanText("x".repeat(600), 500), edit: true, elapsed_ms: 0}, client_time: "2026-10-24T12:00:03.000Z"}));
  items.push(await S.signNext(made.device, {type: "first_reading", extract_sha256: link.extract, position: 3, imprinted_id: "101-2-3",
    content: {elapsed_ms: 700}, client_time: "2026-10-24T12:00:04.000Z"}));
  items.push(await S.signNext(made.device, {type: "entry", extract_sha256: link.extract, position: 3, imprinted_id: "101-2-3",
    content: {match: true, note: "", edit: false, elapsed_ms: 1900}, client_time: "2026-10-24T12:00:05.000Z"}));
  process.stdout.write(JSON.stringify({canonical: values.map(S.canonical), items: items, device: made.device.device_id,
    hashes: await Promise.all(values.map(v => S.sha256Hex(S.canonical(v))))}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    link = {"observer": data["observer"], "county": data["county"], "board": data["board"], "extract": data["extract_sha256"]}
    output = subprocess.run(
        [node, "-e", program, str(script), json.dumps(JS_VALUES), json.dumps(link)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    result = json.loads(output)
    for value, js, digest in zip(JS_VALUES, result["canonical"], result["hashes"], strict=True):
        python = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert js == python
        assert digest == hashlib.sha256(python.encode()).hexdigest()
    items = result["items"]
    registration = items[0]["payload"]
    assert check_payload(registration)["device_id"] == result["device"]
    previous = "0" * 64
    for item in items:
        payload = check_payload(item["payload"])
        assert payload["prev_payload_sha256"] == previous
        assert item["payload_sha256"] == payload_sha256(payload) == hashlib.sha256(canonical_json(payload)).hexdigest()
        assert verify_device_signature(registration["public_key"], payload, item["signature"])
        previous = item["payload_sha256"]
    assert items[1]["payload"]["content"]["note"] == "heard Bob, “no” 🗳"
    assert len(items[3]["payload"]["content"]["note"]) == 500
    # The server accepts exactly what the page signed.
    sent = [{"payload": i["payload"], "signature": i["signature"]} for i in items]
    response = client.post(f"/p/observe/{token}/entries", json={"entries": sent}).json()
    assert response["recorded"] == [0, 1, 2, 3, 4, 5] and not response["errors"]
    assert response["heard"]["2"]["kind"] == "observation.out_of_order"
    assert response["receipts"]["4"]["receipt"]["event_kind"] == "observation.first_reading"


def test_documented_verifier_works() -> None:
    text = (Path(__file__).parent.parent / "docs" / "RECEIPTS.md").read_text(encoding="utf-8")
    code = text.split("With Python's `cryptography` package:\n\n```python\n", 1)[1].split("```", 1)[0]
    namespace: dict[str, Any] = {}
    exec(code, namespace)  # noqa: S102 - the documentation's own example
    device = Device()
    item = device.entry(1, "101-1-1", False, "heard “No” on 2")
    namespace["verify"](device.public_key, item["payload"], item["signature"])
    item["payload"]["content"]["note"] = "heard Yes"
    from cryptography.exceptions import InvalidSignature

    with pytest.raises(InvalidSignature):
        namespace["verify"](device.public_key, item["payload"], item["signature"])


def test_other_board_settles_a_boundary_ballot(server: Server) -> None:
    from corla_observer.practice import latest_entries, practice_stats

    device = Device()
    outcomes = server.send(device.registration(), device.entry(1, "101-1-1"), device.other_board(2, "101-1-2"))
    assert [o.status for o in outcomes] == ["recorded"] * 3
    events = list(server.log.iter_events())
    skipped = next(e for e in events if e.kind == "observation.other_board")
    assert skipped.data["other_board"] is True and skipped.data["position"] == 2
    observers, boards = practice_stats(events)
    assert observers[0].other_board == 1 and observers[0].matches == 1 and observers[0].notes == 0
    assert boards[0].done == 2 and boards[0].other_board == 1 and boards[0].matches == 1
    assert latest_entries(events, LINK)[2]["other_board"] is True
    report = check_record(record_of(device, by_seq(outcomes)), server.jsonl, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)


def test_the_servers_own_private_key_file_gives_its_public_key() -> None:
    """check-record --server-key receipt-key.pem: the private key's public half is used."""
    from cryptography.hazmat.primitives import serialization

    from corla_observer.receipts import ReceiptSigner, parse_server_public_key

    signer = ReceiptSigner.generate()
    pem = signer._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    assert parse_server_public_key(pem.decode()) == signer.public_raw
