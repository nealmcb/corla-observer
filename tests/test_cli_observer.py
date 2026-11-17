"""The command-line observer client, scripts/corla-observe.py (docs/CLI_OBSERVER.md), against a real server.

A uvicorn server runs in a thread with Testland's extract and an observer
link (tests/testland.py), as in the browser tests. The client is driven with
scripted input: it registers its device, enters matches, a note and an edit,
queues entries while the server can't be reached and sends them later, and
its record passes ``corla-observer check-record``. Logs altered in several
ways, and forged receipts, raise its alarms.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.events import canonical_json
from corla_observer.observer_record import check_record, load_record, render_report
from corla_observer.receipts import ReceiptSigner
from corla_observer.web.app import AppConfig, create_app
from test_browser_per_contest import free_port
from testland import make_testland

SCRIPT = Path(__file__).parent.parent / "scripts" / "corla-observe.py"
DEAD = "http://127.0.0.1:9"  # nothing listens here: the server "can't be reached"


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corla_observe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_client()


class Live:
    """A server with Testland's extract, and one observer link."""

    def __init__(self, url: str, token: str, signer: ReceiptSigner, root: Path, app: Any) -> None:
        self.url, self.token, self.signer, self.root, self.app = url, token, signer, root, app
        self.link = f"{url}/observe/{token}"


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    root = tmp_path / "ws"
    (token,) = make_testland(root)
    signer = ReceiptSigner.generate()
    port = free_port()
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, receipt_key=signer, path_prefix="/p"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield Live(f"http://127.0.0.1:{port}/p", token, signer, root, app)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def scripted(lines: list[str | Callable[[], None]]) -> Callable[[str], str]:
    """An input source: each line in turn; a callable in the list is run (to go offline, say) before the next line."""
    queue = list(lines)

    def read(prompt: str) -> str:
        while queue:
            line = queue.pop(0)
            if callable(line):
                line()
                continue
            return line
        raise EOFError

    return read


def started(live: Live, folder: Path, said: list[str], commitments: Path | None = None) -> Any:
    observer = cli.Observer(live.link, folder, server_key=live.signer.public_b64, commitments=commitments and str(commitments), say=said.append)
    observer.start()
    return observer


def texts(observer: Any) -> list[str]:
    return [a["text"] for a in observer.alarms]


def checks(record_path: Path, events: Path, signer: ReceiptSigner) -> None:
    report = check_record(load_record(record_path), events, server_key=signer.public_raw)
    assert report.ok, render_report(report)


def test_observing_offline_and_on_and_the_record_checks(live: Live, tmp_path: Path) -> None:
    folder = tmp_path / "observer"
    said: list[str] = []
    observer = started(live, folder, said)
    assert said[0] == f"corla-observe.py sha256 {hashlib.sha256(SCRIPT.read_bytes()).hexdigest()}"
    assert observer.extract_at is not None and not observer.alarms, observer.alarms
    assert (folder / "device-key.pem").stat().st_mode & 0o777 == 0o600
    first = observer.ballots[0]

    def offline() -> None:
        observer.base = DEAD

    def online() -> None:
        assert len(observer.pending()) == 4  # ballot 4's two taps, ballot 5's note, and the edit of ballot 1
        observer.base = live.url

    shown: list[str] = []
    read = scripted([
        "",  # I understand, start
        "", "",  # ballot 1: first reading, confirmed match
        "", "",  # ballot 2
        "heard Bob for US Senator",  # ballot 3: a note
        offline,
        "", "",  # ballot 4, queued
        "  heard “No” on 2\x07 ",  # ballot 5, queued; the control character and spaces are removed before signing
        ":edit 1", "heard Yes on the last contest after all",  # an edit, queued
        ":status",
        online,
        "", "",  # ballot 6: sends everything
        ":check",
        ":quit",
    ])  # fmt: skip
    cli.run(observer, read, shown.append, background=False)
    assert observer.flush() and observer.check_log()
    assert not observer.alarms, observer.alarms
    assert first["id"] in shown[1] and "Ballot 1 of" in shown[1] and "██" in shown[1]
    assert any("can't be reached: 4 entries queued" in s for s in shown) and any("Log checked" in s for s in shown)

    path = observer.save()
    record = json.loads(path.read_text())
    payloads = [e["payload"] for e in record["entries"]]
    assert [p["type"] for p in payloads] == [
        "device_registration", "entry_mode", "acknowledged",
        "first_reading", "entry", "first_reading", "entry", "entry",
        "first_reading", "entry", "entry", "entry", "first_reading", "entry",
    ]  # fmt: skip
    assert payloads[1]["content"] == {"mode": "two_tap"} and payloads[2]["content"] == {"notice": "out_of_sight_1"}
    assert [p["device_seq"] for p in payloads] == list(range(len(payloads)))
    assert all(e["receipt"] is not None and e["refused"] is None for e in record["entries"])
    note, edit = payloads[10]["content"], payloads[11]["content"]
    assert note["note"] == "heard “No” on 2" and not note["match"] and not note["edit"]
    assert payloads[11]["position"] == first["position"] and edit["edit"] and edit["note"].startswith("heard Yes")
    assert payloads[3]["content"]["elapsed_ms"] <= payloads[4]["content"]["elapsed_ms"]
    assert record["page"] == {"software": "corla-observe.py", "sha256": observer.sha256, "entry_mode": "two_tap"}
    assert record["client_checks"]["receipts_verified"] == len(payloads) and not record["client_checks"]["problems"]

    # The record checks against the server's log and against the client's own verified copy, which is the same bytes.
    copy = (folder / "events.jsonl").read_bytes()
    assert (live.root / "events.jsonl").read_bytes().startswith(copy)
    checks(path, live.root / "events.jsonl", live.signer)
    checks(path, folder / "events.jsonl", live.signer)
    result = CliRunner().invoke(cli_app, ["check-record", str(path), str(folder / "events.jsonl"), "--server-key", str(folder / "server-key.json")])
    assert result.exit_code == 0, result.output

    # The server shows the entries: ballot 1 changed to a note.
    data = json.loads(observer.http(f"/observe/{live.token}/data.json"))
    assert data["entries"][str(first["position"])]["note"].startswith("heard Yes") and len(data["entries"]) == 6

    # An entry made offline and never sent is kept; the next run of the client, with the same folder, sends it.
    observer.base = DEAD
    observer.enter(observer.next_ballot(), True, "", 900)
    assert not observer.flush() and len(observer.pending()) == 1
    again = started(live, folder, [])
    assert again.device.device_id == observer.device.device_id and len(again.items) == len(payloads) + 1
    assert again.flush() and again.check_log() and not again.alarms, again.alarms
    assert len(again.sessions) == 2 and len(again.done) == 7
    checks(again.save(), folder / "events.jsonl", live.signer)


def session(live: Live, folder: Path) -> Any:
    """A short session: registration, the start, a match and a note, all receipted and checked."""
    observer = started(live, folder, [])
    cli.run(observer, scripted(["", "", "", "heard Bob"]), lambda text: None, background=False)
    assert observer.flush() and observer.check_log() and not observer.alarms, observer.alarms
    return observer


def rehash(events: list[dict[str, Any]], start: int) -> list[bytes]:
    """The log's lines, with every hash from event ``start`` on recomputed: a rewritten chain that verifies."""
    lines, prev = [], "0" * 64
    for event in events:
        event = {k: v for k, v in event.items() if k != "hash"}
        if event["seq"] >= start:
            event["prev_hash"] = prev
        digest = hashlib.sha256(canonical_json(event)).hexdigest()
        lines.append(canonical_json(event)[:-1] + b',"hash":"' + digest.encode() + b'"}\n')
        prev = digest
    return lines


def served_as(observer: Any, live: Live, lines: list[bytes]) -> None:
    """Have the client read ``lines`` as the server's log (and its head), the rest from the real server."""
    real = observer.http

    def http(path: str, body: Any = None) -> bytes:
        if path == "/head":
            last = json.loads(lines[-1])
            return json.dumps({"seq": last["seq"], "hash": last["hash"]}).encode()
        if path.startswith(f"/observe/{live.token}/log?"):
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            return b"".join(lines[int(query["from"]) - 1 : int(query["to"])])
        result: bytes = real(path, body)
        return result

    observer.http = http


def tampered(live: Live, folder: Path, tmp_path: Path, name: str, change: Callable[[list[dict[str, Any]], str], list[bytes]]) -> list[str]:
    """The alarms of a client starting afresh (no copy of the log yet) from ``folder``'s record, given a log changed by ``change``."""
    fresh = tmp_path / name
    shutil.copytree(folder, fresh)
    (fresh / "events.jsonl").unlink()
    events = [json.loads(line) for line in (live.root / "events.jsonl").read_bytes().splitlines()]
    observer = cli.Observer(live.link, fresh, server_key=live.signer.public_b64, say=lambda text: None)
    device = json.loads((fresh / "record.jsonl").read_bytes().splitlines()[0])["item"]["payload"]["device_id"]
    served_as(observer, live, change(events, device))
    observer.start()
    return texts(observer)


def test_a_tampered_log_raises_alarms(live: Live, tmp_path: Path) -> None:
    folder = tmp_path / "observer"
    session(live, folder)

    def note_of(events: list[dict[str, Any]], device: str) -> dict[str, Any]:
        return next(e for e in events if e["device_id"] == device and e["data"].get("note") == "heard Bob")

    def edited_in_place(events: list[dict[str, Any]], device: str) -> list[bytes]:
        lines = rehash(events, len(events) + 1)
        seq = note_of(events, device)["seq"]
        lines[seq - 1] = lines[seq - 1].replace(b"heard Bob", b"heard Ann")
        return lines

    def rewritten(events: list[dict[str, Any]], device: str) -> list[bytes]:
        event = note_of(events, device)
        event["data"]["note"] = event["data"]["payload"]["content"]["note"] = "heard Ann"
        return rehash(events, event["seq"])

    def injected(events: list[dict[str, Any]], device: str) -> list[bytes]:
        forged = json.loads(json.dumps(note_of(events, device)))
        forged["seq"] = len(events) + 1
        forged["data"]["payload"]["device_seq"] = forged["data"]["device_seq"] = 99
        forged["data"]["payload_sha256"] = hashlib.sha256(canonical_json(forged["data"]["payload"])).hexdigest()
        return rehash([*events, forged], forged["seq"])

    def cut_back(events: list[dict[str, Any]], device: str) -> list[bytes]:
        return rehash(events[:-3], len(events))

    def reordered(events: list[dict[str, Any]], device: str) -> list[bytes]:
        lines = rehash(events, len(events) + 1)
        lines[5], lines[6] = lines[6], lines[5]
        return lines

    def no_extract(events: list[dict[str, Any]], device: str) -> list[bytes]:
        for e in events:
            if e["kind"] == "extract.built":
                e["file_sha256"] = "0" * 64
        return rehash(events, 1)

    def untouched(events: list[dict[str, Any]], device: str) -> list[bytes]:
        return rehash(events, len(events) + 1)

    assert tampered(live, folder, tmp_path, "untouched", untouched) == []
    found = {name: " | ".join(tampered(live, folder, tmp_path, name, change)) for name, change in [
        ("edited", edited_in_place), ("rewritten", rewritten), ("injected", injected), ("cut", cut_back),
        ("chain", reordered), ("extract", no_extract),
    ]}  # fmt: skip
    assert "the hash does not match the content" in found["edited"]
    assert "but the log has" in found["rewritten"] and "differs from what the device signed in: payload" in found["rewritten"]
    assert "under this device's ID, but this device did not sign it" in found["injected"]
    assert "the server shows a shorter log" in found["cut"]
    assert "the chain does not continue" in found["chain"]
    assert "is not recorded in the log as built for Testland" in found["extract"]


def test_forged_receipts_raise_alarms(live: Live, tmp_path: Path) -> None:
    said: list[str] = []
    observer = session(live, tmp_path / "observer")
    observer.say = said.append
    item = observer.items[-1]
    receipt = json.loads(json.dumps(item["receipt"]))
    other = ReceiptSigner.generate()

    receipt["receipt"]["payload_sha256"] = "0" * 64
    observer.accept_receipt(item, receipt)
    assert "its signature does not verify with the pinned receipt key" in texts(observer)[-1] and "another payload" in texts(observer)[-1]
    assert said[-1].startswith("\a\n*** ALARM: ")

    # Signed with the real key's ID but another key; a second hash for an event already receipted.
    body = json.loads(json.dumps(receipt["receipt"])) | {"payload_sha256": item["payload_sha256"], "event_hash": "f" * 64}
    forged = {"receipt": body, "signature": cli.b64(other._key.sign(canonical_json(body)))}
    observer.accept_receipt(item, forged)
    latest = " | ".join(texts(observer)[1:])
    assert "does not verify" in latest and "there are two logs" in latest and "but the log has" in latest


def test_the_script_runs_alone(live: Live, tmp_path: Path) -> None:
    """``python3 -I``, standard input and output, and nothing from this repository on its path."""
    folder = tmp_path / "observer"
    key = tmp_path / "receipt-key.json"
    key.write_text(json.dumps(live.signer.public_info()))
    command = [sys.executable, "-I", str(SCRIPT), live.link, "--dir", str(folder), "--server-key", str(key)]
    done = subprocess.run(command, input="\n\n\n\nheard Ann\n:status\n:quit\n", capture_output=True, text=True, timeout=120, check=False, cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ALARM" not in done.stdout and "Record: " in done.stdout and "corla-observer check-record" in done.stdout
    checks(folder / "record.json", folder / "events.jsonl", live.signer)
    assert len(json.loads((folder / "record.json").read_text())["entries"]) == 7

    other = tmp_path / "other-key.json"
    other.write_text(json.dumps(ReceiptSigner.generate().public_info()))
    refused = subprocess.run([*command[:-1], str(other)], input="", capture_output=True, text=True, timeout=60, check=False, cwd=tmp_path)
    assert refused.returncode == 2 and "is not the one given with --server-key" in refused.stderr


def forget(live: Live, device: str) -> None:
    """Make the server forget a device's registration, as a server restored from an older backup would."""
    registry = live.app.state.web.devices
    with registry.lock:
        registry.refresh()
        for key in [k for k in registry.keys if k[1] == device]:
            del registry.keys[key]
        for key in [k for k in registry.payloads if k[1] == device and k[2] == 0]:
            del registry.payloads[key]


def test_a_forgotten_device_registers_again_once(live: Live, tmp_path: Path) -> None:
    folder = tmp_path / "observer"
    said: list[str] = []
    observer = session(live, folder)
    observer.say = said.append
    forget(live, observer.device.device_id)
    observer.enter(observer.next_ballot(), True, "", 500)
    assert observer.flush() and not observer.alarms, texts(observer)
    assert any("sending its registration again" in s for s in said)
    journal = [json.loads(line) for line in (folder / "record.jsonl").read_bytes().splitlines()]
    kinds = [next(k for k in line if k != "device_seq") for line in journal]  # canonical JSON: keys sorted
    at = kinds.index("unknown_device")
    assert kinds[at : at + 4] == ["unknown_device", "registered_again", "receipt", "receipt"]
    assert journal[at + 2]["device_seq"] == 0 and journal[at + 1]["registered_again"]["earlier_receipt"] is not None
    # The log now records the registration twice; the record, the client and check-record all accept that.
    assert observer.check_log() and not observer.alarms, texts(observer)
    checks(observer.save(), live.root / "events.jsonl", live.signer)
    # The same session again: final, with an ALARM.
    forget(live, observer.device.device_id)
    observer.enter(observer.next_ballot(), True, "", 500)
    observer.flush()
    assert "is not registered under this observer" in texts(observer)[-1] and observer.items[-1]["refused"]["code"] == "unknown_device"
    # A later run reads the recovery back from the journal without alarms about it.
    again = started(live, folder, [])
    assert texts(again) == texts(observer) and again.registered_again


def test_alarms_are_repeated_until_acknowledged_and_reported(live: Live, tmp_path: Path) -> None:
    folder = tmp_path / "observer"
    observer = session(live, folder)
    said: list[str] = []
    observer.say = said.append
    item = observer.items[-1]
    observer.accept_receipt(item, json.loads(json.dumps(item["receipt"])) | {"signature": cli.b64(b"x" * 64)})
    statement = said[-1]
    for part in ("ALARM", "Found:  the receipt for entry", f"Server: {live.url}", "Link:   Testland, board 1",
                 f"Device: {observer.device.device_id}", f"SHA-256 {observer.sha256}", "Where:  receipt "):  # fmt: skip
        assert part in statement, part
    assert live.token not in statement
    shown: list[str] = []
    cli.run(observer, scripted(["", ":status", ":report", ":ack", ":status", ":quit"]), shown.append, background=False)
    banners = [i for i, s in enumerate(shown) if s.startswith("*** 1 ALARM(S) not acknowledged")]
    acked = next(i for i, s in enumerate(shown) if "acknowledged. They stay" in s)
    assert banners and max(banners) < acked
    assert any(s.startswith("To my coordinator") and live.token not in s for s in shown)
    journal = [json.loads(line) for line in (folder / "record.jsonl").read_bytes().splitlines()]
    assert [line["ack"]["alarms"] for line in journal if "ack" in line] == [1]
    record = observer.record()["client_checks"]
    assert record["acknowledged"] == 1 and len(record["problems"]) == 1


def commitments(root: Path, name: str, head: tuple[int, str], lines: list[str], key: dict[str, Any] | None = None, base: str | None = "..") -> Path:
    """A folder of commitments as commit-evidence writes them (docs/TIMESTAMPING.md): ROOT/observerfiles, holding a SUMS file
    whose paths are relative to the election's folder (``base``, from the SUMS file's folder), and the key file, if any."""
    folder = root / "observerfiles"
    (folder / "derived").mkdir(parents=True, exist_ok=True)
    header = ["# SHA-256 digests of the observers' evidence for election 2026/general, written by corla-observer.",
              "# version: 1", "# time: 2026-11-16T17:32:43Z", f"# event-log-head: {head[0]} {head[1]}"]  # fmt: skip
    (folder / name).write_text("\n".join(header + ([f"# base: {base}"] if base else []) + lines) + "\n")
    if key is not None:
        (folder / "derived" / "receipt-key.json").write_bytes(canonical_json(key))
    return folder


def test_published_commitments(live: Live, tmp_path: Path) -> None:
    # The honest case: the extract, the receipt key and a head of the log as it is now.
    probe = started(live, tmp_path / "probe", [])
    extract, head = probe.extract, (len(probe.log_hashes), probe.log_hashes[-1])
    key, other = live.signer.public_info(), ReceiptSigner.generate().public_info()
    key_line = f"{hashlib.sha256(canonical_json(key)).hexdigest()}  observerfiles/derived/receipt-key.json"
    other_line = f"{hashlib.sha256(canonical_json(other)).hexdigest()}  observerfiles/derived/receipt-key.json"
    listed = [f"{extract}  observerfiles/derived/extract-Testland.json", "1" * 64 + "  observerfiles/derived/prediction.json"]
    name = "SHA256SUMS-v1-20261116T173243Z.txt"

    said: list[str] = []
    honest = commitments(tmp_path / "honest", name, head, [*listed, key_line], key)
    (honest / f"{name}.ots").write_text("not read")
    observer = started(live, tmp_path / "a", said, honest)
    assert not observer.alarms, texts(observer)
    assert any("extract shown is in the published commitments" in s for s in said) and any("receipt key is the one" in s for s in said)
    summary = observer.record()["client_checks"]["commitments"]
    assert summary["extract_in"] == name and summary["receipt_key"] == "listed"
    assert summary["anchors"] == [{"seq": head[0], "hash": head[1], "file": name, "verified": True}]

    # The earlier layout: no base line, paths relative to the SUMS file's own folder.
    plain = [line.replace("observerfiles/", "") for line in [*listed, key_line]]
    observer = started(live, tmp_path / "b", [], commitments(tmp_path / "plain", "SHA256SUMS.v2.txt", head, plain, key, base=None))
    assert not observer.alarms and observer.committed["receipt_key"] == "listed", texts(observer)

    # Commitments made before the key was committed (as the real ones so far): said plainly, not an alarm.
    said = []
    early = commitments(tmp_path / "early", "SHA256SUMS.txt", head, listed)
    assert not started(live, tmp_path / "c", said, early).alarms and any("list no receipt key yet" in s for s in said)

    # No commitments given: said plainly.
    said = []
    assert not started(live, tmp_path / "d", said).alarms and any("No --commitments given" in s for s in said)

    cases = {
        "no extract": (head, [listed[1], key_line], key, "the extract shown is not in the published commitments"),
        "other key": (head, [*listed, other_line], other, "receipt key is not the one in the published commitments"),
        "key file not as listed": (head, [*listed, key_line], other, "derived/receipt-key.json in the commitments is not the file"),
        "false anchor": ((head[0] - 1, "2" * 64), [*listed, key_line], key, "but the log has"),
        "longer log": ((head[0] + 50, "3" * 64), [*listed, key_line], key, "the server shows a shorter log"),
    }
    for n, (case, (anchor, lines, committed_key, expected)) in enumerate(cases.items()):
        folder = commitments(tmp_path / f"case{n}", "SHA256SUMS-v2-20261116T180000Z.txt", anchor, lines, committed_key)
        found = " | ".join(texts(started(live, tmp_path / f"observer{n}", [], folder)))
        assert expected in found, (case, found)
