"""The observation report (corla_observer.obsreport), and its JavaScript twin (static/report.js) giving the same report, in Node."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from corla_observer.events import canonical_json
from corla_observer.obsreport import build_report, sign_report
from corla_observer.receipts import ReceiptSigner

SCRIPT = Path(__file__).parent.parent / "src" / "corla_observer" / "web" / "static" / "report.js"
WHO = {"observer": "observer:ann", "county": "Delta", "board": "1", "extract_sha256": "e" * 64}


def _p(kind: str, position: int, content: dict[str, Any], extract: str = WHO["extract_sha256"]) -> dict[str, Any]:
    return {"type": kind, "position": position, "imprinted_id": f"1-1-{position}", "extract_sha256": extract, "content": content}


def _c(position: int, row: int, reading: int, outcome: str, heard: str = "", rows: int = 2, contest: str = "") -> dict[str, Any]:
    return _p("contest", position, {"row": row, "rows": rows, "reading": reading, "outcome": outcome, "heard": heard,
                                    "contest": contest or f"Contest {row}", "expected": "Ann", "correction": False})  # fmt: skip


def test_two_taps_contest_by_contest_and_the_latest_entry_counts() -> None:
    payloads = [
        _p("entry", 1, {"match": True, "note": ""}),
        _p("entry", 2, {"match": False, "note": "heard Bob for Governor"}),
        _p("other_board", 3, {}),
        # Ballot 4, contest by contest: one contest heard instead, both readings.
        _c(4, 1, 1, "confirmed"), _c(4, 1, 2, "confirmed"),
        _c(4, 2, 1, "heard_instead", "Bob"), _c(4, 2, 2, "heard_instead", "bob "),
        # Ballot 5: readings that differ; ballot 6: not done; ballot 7: marked done with a contest not entered.
        _c(5, 1, 1, "heard_instead", "Bob"), _c(5, 1, 2, "heard_instead", "Cy"), _c(5, 2, 1, "confirmed"), _c(5, 2, 2, "confirmed"),
        _c(6, 1, 1, "confirmed"),
        _c(7, 1, 1, "confirmed"), _c(7, 1, 2, "confirmed"), _p("ballot_done", 7, {"rows": 2, "reading": 2, "unresolved": 1}),
        # Ballot 1 entered again contest by contest, later: that counts. Another extract's entry never does.
        _c(1, 1, 1, "unclear"), _c(1, 1, 2, "unclear"), _c(1, 2, 1, "confirmed"), _c(1, 2, 2, "confirmed"),
        _p("entry", 8, {"match": False, "note": "x"}, extract="f" * 64),
        _p("first_reading", 9, {"elapsed_ms": 5}),
    ]  # fmt: skip
    report = build_report(payloads, **WHO)
    by = {b["position"]: b for b in report["ballots"]}
    assert sorted(by) == [1, 2, 3, 4, 5, 6, 7]
    assert by[1]["verdict"] == "discrepancy" and by[1]["contests"][0]["observed"] == "not heard clearly"
    assert by[2] == {"position": 2, "imprinted_id": "1-1-2", "verdict": "discrepancy", "note": "heard Bob for Governor", "contests": []}
    assert by[3]["verdict"] == "other_board"
    assert by[4]["contests"] == [{"row": 2, "contest": "Contest 2", "expected": "Ann", "observed": "heard “bob ”"}]
    assert by[5]["contests"][0]["observed"] == "first reading heard “Bob”, second heard “Cy”"
    assert by[6]["verdict"] == "incomplete" and by[7]["note"] == "1 contest not entered"
    assert report["summary"] == {"ballots": 7, "matches": 0, "discrepancies": 5, "other_board": 1, "incomplete": 1,
                                 "ballot_level": 2, "by_contest": {"Contest 1": 2, "Contest 2": 1}}  # fmt: skip


def test_a_signed_report_checks_with_the_receipt_key() -> None:
    signer = ReceiptSigner.generate()
    signed = sign_report(signer, build_report([_p("entry", 1, {"match": True, "note": ""})], **WHO), device_id="d-" + "a" * 32, head=(5, "0" * 64))
    body = signed["report"]
    assert body["device_id"] == "d-" + "a" * 32 and body["head_seq"] == 5 and body["key_id"] == signer.key_id
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from corla_observer.receipts import b64decode

    # The receipt key's public half verifies it, over the canonical body, as for a receipt (raises if not).
    Ed25519PublicKey.from_public_bytes(b64decode(signer.public_b64, "key")).verify(b64decode(signed["signature"], "signature"), canonical_json(body))


def _random_payloads(rng: random.Random) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _ in range(rng.randint(0, 40)):
        position = rng.randint(1, 6)
        extract = WHO["extract_sha256"] if rng.random() < 0.9 else "f" * 64
        roll = rng.random()
        if roll < 0.2:
            out.append(_p("entry", position, {"match": rng.random() < 0.5, "note": rng.choice(["", "heard Bob", "Bob  for X"])}, extract))
        elif roll < 0.25:
            out.append(_p("other_board", position, {}, extract))
        elif roll < 0.33:
            out.append(_p("ballot_done", position, {"rows": rng.randint(1, 3), "reading": 2, "unresolved": 0}, extract))
        elif roll < 0.36:
            out.append(_p(rng.choice(["first_reading", "entry_mode", "acknowledged"]), position, {}, extract))
        else:
            payload = _c(position, rng.randint(1, 3), rng.choice([1, 2]), rng.choice(["confirmed", "heard_instead", "unclear"]),
                         rng.choice(["Bob", "bob", " Bob  ", "Cy", ""]), rows=rng.randint(1, 3), contest=rng.choice(["", "Governor", "Ä Mayor"]))  # fmt: skip
            payload["extract_sha256"] = extract
            out.append(payload)
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="no Node")
def test_the_javascript_twin_gives_the_same_report() -> None:
    rng = random.Random(20261105)
    cases = [{"payloads": _random_payloads(rng), "who": WHO} for _ in range(300)]
    runner = f"""
const report = require({json.dumps(str(SCRIPT))});
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(c => report.build(c.payloads, c.who))));
"""
    result = subprocess.run(["node", "-e", runner], input=json.dumps(cases), capture_output=True, text=True, check=True)
    from_js = json.loads(result.stdout)
    for case, js in zip(cases, from_js, strict=True):
        py = build_report(case["payloads"], **WHO)
        assert canonical_json(js) == canonical_json(py), json.dumps({"payloads": case["payloads"], "py": py, "js": js}, indent=1)[:3000]


def test_the_server_signs_each_devices_report(tmp_path: Path) -> None:
    """``/observe/TOKEN/report.json``: one device's report, from the payloads the log holds for it, signed with the receipt key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from fastapi.testclient import TestClient

    from corla_observer.events import EventLog
    from corla_observer.quicksetup import populate
    from corla_observer.receipts import b64decode
    from corla_observer.web.app import AppConfig, create_app
    from corla_observer.workspace import Workspace
    from test_web import SECRET

    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
    signer = ReceiptSigner.generate()
    config = AppConfig(workspace=store.root, secret=SECRET, crawl=lambda ws, log: None, receipt_key=signer)
    with TestClient(create_app(config), base_url="https://t") as client:
        data = client.get(f"/observe/{made.observer_token}/data.json").json()
    first, sha = data["ballots"][0], data["extract_sha256"]
    phone, laptop = "d-" + "a" * 32, "d-" + "b" * 32
    with EventLog.open(store.root) as log:
        for seq, (device, match) in enumerate(((phone, True), (laptop, False)), start=1):
            payload = {"type": "entry", "position": first["position"], "imprinted_id": first["id"], "extract_sha256": sha,
                       "device_id": device, "device_seq": seq, "content": {"match": match, "note": "" if match else "heard Bob"}}  # fmt: skip
            log.append("observation.entry", "observer:ann", county="Testland", board="1", device_id=device, data={"payload": payload})
    with TestClient(create_app(config), base_url="https://t") as client:
        got = client.get(f"/observe/{made.observer_token}/report.json?device={phone}&extract={sha}").json()
        other = client.get(f"/observe/{made.observer_token}/report.json?device={laptop}&extract={sha}").json()
        assert client.get(f"/observe/{made.observer_token}/report.json?device=x&extract={sha}").status_code == 400
    body = got["report"]
    assert body["summary"]["matches"] == 1 and body["summary"]["discrepancies"] == 0 and body["device_id"] == phone
    assert other["report"]["summary"]["discrepancies"] == 1 and other["report"]["ballots"][0]["note"] == "heard Bob"
    Ed25519PublicKey.from_public_bytes(b64decode(signer.public_b64, "key")).verify(b64decode(got["signature"], "signature"), canonical_json(body))
