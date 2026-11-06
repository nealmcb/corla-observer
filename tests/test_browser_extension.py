"""The observer page run from the browser extension, in a real Chromium (scripts/browser_extension.py).

Marked ``browser`` and left out of the default run, like the other browser tests. A Testland list is
populated (corla_observer.quicksetup) and served; the extension is built allowing the server's origin
(scripts/build_extension.py), loaded, and the observer link opened through it. Besides the browser's
checks: the entry is in the log with the extension's device, and the server was never asked for the
page or its scripts, only for data and to take entries.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from corla_observer.events import EventLog, canonical_json
from corla_observer.quicksetup import populate
from corla_observer.receipts import ReceiptSigner
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from test_browser_per_contest import free_port, playwright_python
from test_web import SECRET

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "browser_extension.py"


@pytest.mark.parametrize("case", ["honest", "false-anchor", "false-report"])
def test_the_extension_runs_the_observer_page_from_its_own_code(tmp_path: Path, case: str) -> None:
    """Honest, the page passes every check; with a false published anchor, or a server signing a false report of the entries, it raises an alert."""
    forked = case == "false-anchor"
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
        anchor_seq, anchor_hash = log.head()  # the log's head, as a commitment would anchor it
    port = free_port()
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py"), "--out", str(tmp_path / "extension"),
                            "--allow-host", "http://127.0.0.1/*"], capture_output=True, text=True, check=False)  # fmt: skip
    assert built.returncode == 0, built.stderr[-2000:]
    paths: list[str] = []
    # One receipt key for both app instances, as a server keeps one in its key file (without one, each instance makes its own).
    signer = ReceiptSigner.generate()
    config = AppConfig(workspace=store.root, secret=SECRET, secure_cookies=False, crawl=lambda ws, log: None, receipt_key=signer)
    # The extract's hash, from a first app instance: leaving a TestClient closes its app's log, so the served app is another.
    with TestClient(create_app(config), base_url="https://testserver") as client:
        data = client.get(f"/observe/{made.observer_token}/data.json").json()
    extract_sha = data["extract_sha256"]
    # The receipt key as commit-evidence lists it: the SHA-256 of its canonical JSON.
    key_sha = hashlib.sha256(canonical_json(data["receipt_key"])).hexdigest()
    app = create_app(config)
    # Published commitments listing that extract, as a GitHub folder would list them (served here, outside the app).
    if forked:  # a false anchor: the head's position, with another hash
        anchor_hash = "f" * 64
    sums = (f"# event-log-head: {anchor_seq} {anchor_hash}\n# files: 2\n{extract_sha}  derived/extract-Testland.json\n"
            f"{key_sha}  derived/receipt-key.json\n").encode()
    fake = {
        "/fake/listing.json": json.dumps([{"name": "SHA256SUMS.txt", "download_url": f"http://127.0.0.1:{port}/fake/SHA256SUMS.txt"}]).encode(),
        "/fake/SHA256SUMS.txt": sums,
    }

    async def recording(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] in fake:
            body = fake[scope["path"]]
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain"), (b"access-control-allow-origin", b"*")]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] == "http":
            paths.append(f"{scope['method']} {scope['path']}")
        if case == "false-report" and scope["type"] == "http" and scope["path"].endswith("/report.json"):
            # A server signing, with its real key, a report of the entries that says otherwise: every match a discrepancy.
            parts: list[bytes] = []

            async def keep(message: Any) -> None:
                if message["type"] == "http.response.body":
                    parts.append(message.get("body", b""))

            await app(scope, receive, keep)
            got = json.loads(b"".join(parts))
            body = got["report"]
            for ballot in body["ballots"]:
                ballot["verdict"] = "discrepancy" if ballot["verdict"] == "match" else ballot["verdict"]
            body["summary"]["discrepancies"] += body["summary"]["matches"]
            body["summary"]["matches"] = 0
            out = json.dumps({"report": body, "signature": signer.sign(body)}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": out})
            return
        await app(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(recording, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        link = f"http://127.0.0.1:{port}/observe/{made.observer_token}"
        listing = f"http://127.0.0.1:{port}/fake/listing.json"
        command = [python, str(SCRIPT), str(tmp_path / "extension"), link, listing] + ({"false-anchor": ["--expect-forked-log"], "false-report": ["--expect-false-report"]}.get(case, []))
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
        assert result.returncode == 0 and summary["ok"], json.dumps(summary, indent=1)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    # The server served data and took entries; never the page or its code.
    asked = sorted(set(paths))
    assert not [p for p in asked if p == f"GET /observe/{made.observer_token}" or "/static/" in p], asked
    assert any(p.endswith("/data.json") for p in asked) and any(p.endswith("/entries") for p in asked), asked
    with EventLog.open(store.root, read_only=True) as log:
        entries = [e for e in log.iter_events() if e.kind.startswith("observation.") and e.actor == "observer:ann" and e.device_id]
    assert entries, "no entry recorded from the extension"


def test_the_links_page(tmp_path: Path) -> None:
    """The links page shows the identity the build computed, refuses what is not an observer link, and keeps and opens one."""
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py"), "--out", str(tmp_path / "extension"),
                            "--allow-host", "http://127.0.0.1/*"], capture_output=True, text=True, check=False)  # fmt: skip
    assert built.returncode == 0, built.stderr[-2000:]
    identity = built.stdout.strip().rsplit(" ", 1)[-1]
    link = f"http://127.0.0.1:{free_port()}/observe/" + "t" * 32
    result = subprocess.run([python, str(SCRIPT), "--links-page", str(tmp_path / "extension"), link, identity],
                            capture_output=True, text=True, timeout=120, check=False)  # fmt: skip
    summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
    assert result.returncode == 0 and summary["ok"], json.dumps(summary, indent=1)
