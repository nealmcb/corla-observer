"""The observer page warns, in a real browser, when the observer's devices disagree (scripts/browser_devices.py).

Marked ``browser`` and left out of the default run, like tests/test_browser_per_contest.py, whose
Playwright lookup it uses. A Testland list is populated (corla_observer.quicksetup), two devices'
differing entries for its first ballot are recorded, and the observer link is opened.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from corla_observer.events import EventLog
from corla_observer.quicksetup import populate
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from test_browser_per_contest import free_port, playwright_python
from test_devices import LAPTOP, PHONE
from test_web import SECRET

pytestmark = pytest.mark.browser
SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_devices.py"


def test_the_disagreement_warning(tmp_path: Path) -> None:
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
    app = create_app(AppConfig(workspace=store.root, secret=SECRET, secure_cookies=False, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        first: dict[str, Any] = client.get(f"/observe/{made.observer_token}/data.json").json()
    ballot = first["ballots"][0]
    with EventLog.open(store.root) as log:
        for device, match in ((PHONE, True), (LAPTOP, False)):
            log.append("observation.entry", "observer:ann", county="Testland", board="1", device_id=device,
                       data={"position": ballot["position"], "imprinted_id": ballot["id"], "match": match,
                             "note": "" if match else "heard Bob", "extract_sha256": first["extract_sha256"]})  # fmt: skip
    port = free_port()
    served = create_app(AppConfig(workspace=store.root, secret=SECRET, secure_cookies=False, crawl=lambda ws, log: None))
    server = uvicorn.Server(uvicorn.Config(served, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        result = subprocess.run(
            [python, str(SCRIPT), f"http://127.0.0.1:{port}/observe/{made.observer_token}", ballot["id"]],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
        assert result.returncode == 0 and summary["ok"], json.dumps(summary, indent=1)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
