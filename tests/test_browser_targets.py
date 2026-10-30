"""The targeted ballots page in a real browser (scripts/browser_targets.py).

Marked ``browser`` and left out of the default run, like
tests/test_browser_per_contest.py, whose Playwright lookup it uses. It
serves a workspace with the small party-labelled CVR of tests/test_targets.py
for a demonstration county, logs in, checks that the counts update without
a reload, draws twice, enters an imprinted ID, excludes and includes a
source, sends the working list, and looks a ballot up on an observer's phone. Set ``$CORLA_OBSERVER_SCREENSHOTS`` to keep
the screenshots.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from corla_observer.web.app import AppConfig, create_app
from test_browser_per_contest import free_port, playwright_python
from test_targets import COUNTY, PASSWORD, _workspace

pytestmark = pytest.mark.browser
SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_targets.py"


def test_targets_page(tmp_path: Path) -> None:
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    _, root = _workspace(tmp_path)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(AppConfig(workspace=root, secret=b"s" * 32, secure_cookies=False)), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        out = Path(os.environ.get("CORLA_OBSERVER_SCREENSHOTS") or tmp_path / "shots")
        result = subprocess.run(
            [python, str(SCRIPT), str(out), f"http://127.0.0.1:{port}", "alice", PASSWORD, COUNTY, "1-4-1"],
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
        failed = [c for c in summary.get("checks", []) if not c["ok"]]
        assert result.returncode == 0 and summary["ok"], json.dumps(failed or summary, indent=1)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
