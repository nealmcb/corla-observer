"""The targeting menu in a real browser (scripts/browser_targetmenu.py).

Marked ``browser`` and left out of the default run, like
tests/test_browser_targets.py. It builds Testland (tests/testland.py: four
ballot styles, an RLA extract), makes a domain draw from the first domain on
its menu, serves it, logs in, and shows the suggested draws at phone and desktop widths: on the first screen
with their one Draw button, updated in view, a card opened, and the whole suggestion drawn with the button.
Set ``$CORLA_OBSERVER_SCREENSHOTS`` to keep the screenshots.
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

from corla_observer import targetmenu as tm
from corla_observer import targets as tg
from corla_observer.events import EventLog
from corla_observer.extract import county_manifest
from corla_observer.testcounty import COUNTY
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from test_browser_per_contest import free_port, playwright_python
from testland import make_testland

pytestmark = pytest.mark.browser
SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_targetmenu.py"
PASSWORD = "correct horse battery"


def make_testland_with_a_domain_draw(root: Path) -> None:
    """Testland, a coordinator account ``alice``, and one domain draw of 8 cards from its menu's last domain."""
    make_testland(root)
    store = Workspace.open(root)
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", PASSWORD, log)
        inputs = tm.gather(store, log, COUNTY)
        line = tm.build_menu(inputs).lines[-1]
        manifest = county_manifest(store, log, COUNTY)
        record = tg.domain_draw(
            inputs.votes, county=COUNTY, cvr_sha256=inputs.cvr_sha256, contests=list(line.group.contests), seed="screenshot",
            seed_source="entered", number=1, locations=manifest.locations, manifest_sha256=manifest.sha256,
            actor="coordinator:alice", size=8,
        )  # fmt: skip
        tg.record_domain_draw(store, log, record)


def test_targeting_menu_page(tmp_path: Path) -> None:
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    root = tmp_path / "ws"
    make_testland_with_a_domain_draw(root)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                AppConfig(workspace=root, secret=b"s" * 32, secure_cookies=False)
            ),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        out = Path(os.environ.get("CORLA_OBSERVER_SCREENSHOTS") or tmp_path / "shots")
        result = subprocess.run(
            [python, str(SCRIPT), str(out), f"http://127.0.0.1:{port}", "alice", PASSWORD, COUNTY, "12", "--draw"],
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        summary: dict[str, Any] = (
            json.loads(result.stdout)
            if result.stdout.startswith("{")
            else {"ok": False, "error": result.stderr[-3000:]}
        )
        failed = [c for c in summary.get("checks", []) if not c["ok"]]
        assert result.returncode == 0 and summary["ok"], json.dumps(
            failed or summary, indent=1
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
