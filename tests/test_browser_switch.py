"""Switching between the RLA sample and a targeted list on the observer page, in a real browser (scripts/browser_switch.py).

Marked ``browser`` and left out of the default run, like
tests/test_browser_targets.py. It serves a 2020 demonstration county (the
small party-labelled CVR of tests/test_targets.py) with an RLA extract and
two targeted lists, runs the script on an observer link, and checks the log:
two signed switches, and each list's entries counted apart. Set
``$CORLA_OBSERVER_SCREENSHOTS`` to keep the screenshots.
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

from corla_observer.events import EventLog
from corla_observer.extract import targeted_key
from corla_observer.practice import LinkStore, practice_stats
from corla_observer.web.app import AppConfig, create_app
from test_browser_per_contest import free_port, playwright_python
from test_pulllists import _setup
from test_targets import COUNTY

pytestmark = pytest.mark.browser
SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_switch.py"


def test_switching_lists(tmp_path: Path) -> None:
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    _, root, _, _, _ = _setup(tmp_path)
    key = targeted_key("Targeted list 1")
    with EventLog.open(root) as log:
        token, _ = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
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
            [python, str(SCRIPT), str(out), f"http://127.0.0.1:{port}/observe/{token}", key],
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
        failed = [c for c in summary.get("checks", []) if not c["ok"]]
        assert result.returncode == 0 and summary["ok"], json.dumps(failed or summary, indent=1)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    with EventLog.open(root) as log:
        events = list(log.iter_events())
    switches = [e.data["list"] for e in events if e.kind == "observation.list_switched"]
    assert switches == [key, "rla"]
    # The default, recorded as the observer starts, then the switch to two taps (and two taps again at any later start).
    modes = [e.data["mode"] for e in events if e.kind == "observation.entry_mode"]
    assert modes[:2] == ["per_contest", "two_tap"] and set(modes[2:]) <= {"two_tap"}, modes
    _, boards = practice_stats(events)
    done = {b.list_key: b.done for b in boards if b.observers}
    assert done == {"rla": 1, key: 1}
