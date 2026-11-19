"""Publishing a record and the log with chosen entries withheld (corla_observer.withhold), and checking the result."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.events import canonical_json, verify_jsonl
from corla_observer.observer_record import check_record, load_record, render_report
from corla_observer.withhold import WithholdError, withhold_files, withhold_record
from test_receipts import Server, honest, kinds, record_of, server  # noqa: F401  (server is a fixture)

REASON = "ballot not in the redacted CVR"
SECRET = "heard Bob"


def publish(server: Server, tmp_path: Path, **selection: Any) -> tuple[Path, Path]:  # noqa: F811
    device, outcomes = honest(server)
    record_path = tmp_path / "in" / "record.json"
    record_path.parent.mkdir()
    record_path.write_text(json.dumps(record_of(device, outcomes)))
    result = withhold_files(record_path, server.jsonl, tmp_path / "out", reason=REASON, **selection)
    assert result.record_path is not None and result.events_path is not None
    return result.record_path, result.events_path


def lines_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def write(path: Path, events: list[dict[str, Any]]) -> None:
    """Write events back as lines: withheld ones as canonical JSON, others with their hash recomputed."""
    out = []
    for e in events:
        if e.get("withheld") is True:
            out.append(canonical_json(e))
        else:
            body = {k: v for k, v in e.items() if k != "hash"}
            digest = hashlib.sha256(canonical_json(body)).hexdigest()
            out.append(canonical_json(body)[:-1] + b',"hash":"' + digest.encode() + b'"}')
    path.write_bytes(b"".join(line + b"\n" for line in out))


def test_withholding_one_entry_keeps_the_check_clean(server: Server, tmp_path: Path) -> None:  # noqa: F811
    record_path, events_path = publish(server, tmp_path, imprinted_ids=["101-1-2"])
    assert SECRET not in record_path.read_text() and SECRET not in events_path.read_text()
    record = load_record(record_path)
    marker = record["entries"][2]["payload"]
    assert marker == {"withheld": True, "reason": REASON, "device_seq": 2}
    assert record["entries"][3]["payload"]["prev_payload_sha256"] == record["entries"][2]["payload_sha256"]
    withheld_lines = [e for e in lines_of(events_path) if e.get("withheld")]
    assert len(withheld_lines) == 1 and withheld_lines[0]["data"]["payload_sha256"] == record["entries"][2]["payload_sha256"]
    assert "signature" not in withheld_lines[0] and withheld_lines[0]["kind"] == "observation.entry"

    report = check_record(record, events_path, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    assert (report.withheld_entries, report.withheld_events) == (1, 1)
    assert {f.kind for f in report.findings} == {"withheld_entry", "withheld_event"}
    text = render_report(report)
    assert "Withheld: 1 entries of the record and 1 lines of the log" in text and "OK: apart from the withheld" in text
    assert report.to_dict()["withheld_entries"] == 1

    # The strict verifier refuses the withheld line; the lenient one accepts it and counts it.
    assert not verify_jsonl(events_path).ok
    lenient = verify_jsonl(events_path, allow_withheld=True)
    assert lenient.ok and lenient.withheld == (withheld_lines[0]["seq"],) and not lenient.withheld_unpinned


def test_withholding_by_device_seq_and_several_entries(server: Server, tmp_path: Path) -> None:  # noqa: F811
    # Ballot 101-2-3 has two entries (heard ID, then the entry); with device_seq 1, three in all.
    record_path, events_path = publish(server, tmp_path, imprinted_ids=["101-2-3"], device_seqs=[1])
    report = check_record(load_record(record_path), events_path, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    assert (report.withheld_entries, report.withheld_events) == (3, 3)
    # The last two log lines are consecutive withheld events: the first is pinned only by its receipt.
    assert any(f.kind == "withheld_unpinned" for f in report.findings)


def test_nothing_selected_or_the_registration_is_refused(server: Server) -> None:  # noqa: F811
    device, outcomes = honest(server)
    record = record_of(device, outcomes)
    with pytest.raises(WithholdError, match="no entry"):
        withhold_record(record, reason=REASON, imprinted_ids=["999-9-9"])
    with pytest.raises(WithholdError, match="registration"):
        withhold_record(record, reason=REASON, device_seqs=[0])
    with pytest.raises(WithholdError, match="reason"):
        withhold_record(record, reason=" ", device_seqs=[1])


@pytest.mark.parametrize("tamper", ["stated_hash", "prev_hash", "next_prev_hash"])
def test_tampering_around_a_withheld_line_is_caught(server: Server, tmp_path: Path, tamper: str) -> None:  # noqa: F811
    record_path, events_path = publish(server, tmp_path, imprinted_ids=["101-1-2"])
    events = lines_of(events_path)
    i = next(n for n, e in enumerate(events) if e.get("withheld"))
    if tamper == "stated_hash":
        events[i]["hash"] = "1" * 64
    elif tamper == "prev_hash":
        events[i]["prev_hash"] = "2" * 64
    else:
        # The next line's hash is recomputed, as a forger would, so only the link is wrong.
        events[i + 1]["prev_hash"] = "3" * 64
    bad = tmp_path / "bad.jsonl"
    write(bad, events)
    log = verify_jsonl(bad, allow_withheld=True)
    # A wrong prev_hash fails at the withheld line; a wrong stated hash, or a next line not carrying it, at the next.
    assert not log.ok and log.event_count == (i if tamper == "prev_hash" else i + 1)
    assert "withheld" in log.errors[0]
    report = check_record(load_record(record_path), bad, server_key=server.signer.public_raw)
    assert "log_broken" in kinds(report.findings)


def test_a_malformed_withheld_line_is_caught(server: Server, tmp_path: Path) -> None:  # noqa: F811
    record_path, events_path = publish(server, tmp_path, imprinted_ids=["101-1-2"])
    events = lines_of(events_path)
    i = next(n for n, e in enumerate(events) if e.get("withheld"))
    events[i]["note"] = "smuggled"
    bad = tmp_path / "bad.jsonl"
    write(bad, events)
    assert "fields" in verify_jsonl(bad, allow_withheld=True).errors[0]
    events[i].pop("note")
    events[i]["data"] = {"reason": REASON}
    write(bad, events)
    assert "marker" in verify_jsonl(bad, allow_withheld=True).errors[0]


def test_a_withheld_entry_with_the_wrong_receipt_or_hash_is_caught(server: Server, tmp_path: Path) -> None:  # noqa: F811
    record_path, events_path = publish(server, tmp_path, imprinted_ids=["101-1-2"])
    record = load_record(record_path)
    swapped = copy.deepcopy(record)
    swapped["entries"][2]["receipt"] = swapped["entries"][1]["receipt"]
    report = check_record(swapped, events_path, server_key=server.signer.public_raw)
    assert "receipt_mismatch" in kinds(report.findings)

    # A changed kept hash breaks the device's chain and no longer matches the receipt or the log.
    rehashed = copy.deepcopy(record)
    rehashed["entries"][2]["payload_sha256"] = "4" * 64
    report = check_record(rehashed, events_path, server_key=server.signer.public_raw)
    assert {"device_chain", "receipt_mismatch"} <= kinds(report.findings)

    # The withheld line says it holds another payload than the receipt's.
    events = lines_of(events_path)
    i = next(n for n, e in enumerate(events) if e.get("withheld"))
    events[i]["data"]["payload_sha256"] = "5" * 64
    other = tmp_path / "other.jsonl"
    write(other, events)
    report = check_record(record, other, server_key=server.signer.public_raw)
    assert report.log.ok and "receipt_not_in_log" in kinds(report.findings)


def test_a_record_with_nothing_withheld_is_checked_as_before(server: Server, tmp_path: Path) -> None:  # noqa: F811
    device, outcomes = honest(server)
    report = check_record(record_of(device, outcomes), server.jsonl, server_key=server.signer.public_raw)
    assert report.ok and not report.findings
    assert (report.withheld_entries, report.withheld_events, report.log.withheld) == (0, 0, ())
    assert "Withheld" not in render_report(report)
    assert render_report(report).endswith("OK: every entry in the record is in the log as signed, and no entry under this device is unsigned by it.")


def test_cli_record_withhold(server: Server, tmp_path: Path) -> None:  # noqa: F811
    device, outcomes = honest(server)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record_of(device, outcomes)))
    out = tmp_path / "out"
    runner = CliRunner()
    args = ["record", "withhold", str(record_path), str(server.jsonl), "--imprinted-id", "101-1-2", "--reason", REASON, "--out", str(out)]
    result = runner.invoke(cli_app, args)
    assert result.exit_code == 0, result.output
    assert "withheld entry 2 (entry, 101-1-2)" in result.output
    assert sorted(p.name for p in out.iterdir()) == ["events.jsonl", "record.json"]
    assert runner.invoke(cli_app, args).exit_code == 2  # won't overwrite
    key = server.signer.public_b64
    check = runner.invoke(cli_app, ["check-record", str(out / "record.json"), str(out / "events.jsonl"), "--server-key", key])
    assert check.exit_code == 0, check.output
    assert "withheld [withheld_entry]" in check.output
    assert runner.invoke(cli_app, ["log", "verify", str(out / "events.jsonl")]).exit_code == 1
    verify = runner.invoke(cli_app, ["log", "verify", str(out / "events.jsonl"), "--allow-withheld"])
    assert verify.exit_code == 0 and "withheld: 1 lines" in verify.output


def test_other_observers_events_can_be_withheld_too(server: Server, tmp_path: Path) -> None:  # noqa: F811
    from devices import Device

    from corla_observer.practice import Link

    device, outcomes = honest(server)
    bob = Device(observer="observer:bob")
    server.send(bob.registration(), bob.entry(1, "101-1-1", False, "bob heard Carol"), link=Link("observer", "bob", "La Plata", 1, "2026-10-24T00:00:00Z"))
    record_path = tmp_path / "in" / "record.json"
    record_path.parent.mkdir()
    record_path.write_text(json.dumps(record_of(device, outcomes)))
    result = withhold_files(
        record_path, server.jsonl, tmp_path / "out", reason=REASON, imprinted_ids=["101-1-2"], others_reason="another observer's entries"
    )
    assert result.events_path is not None
    text = result.events_path.read_text()
    assert "bob heard Carol" not in text and SECRET not in text
    lines = lines_of(result.events_path)
    bobs = [e for e in lines if e["actor"] == "observer:bob"]
    assert bobs and all(e.get("withheld") is True and e["data"] == {"withheld": True, "reason": "another observer's entries"} for e in bobs)
    assert not any(e.get("withheld") for e in lines if e["actor"] == "observer:ann" and e["data"].get("device_seq") != 2)
    report = check_record(load_record(result.record_path), result.events_path, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    assert report.withheld_entries == 1 and report.withheld_events == 1 + len(bobs)
