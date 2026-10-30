"""Contest-by-contest entry in a real browser, on a Testland 60-contest ballot (scripts/browser_per_contest.py).

Marked ``browser`` and left out of the default run: it needs Chromium and a
Python with Playwright (``python3`` on the PATH, or ``$CORLA_OBSERVER_PLAYWRIGHT_PYTHON``),
and takes about half a minute. Run it with ``pytest -m browser``; set
``$CORLA_OBSERVER_SCREENSHOTS`` to a directory to keep the screenshots.
It serves a Testland workspace (tests/testland.py), drives the keyboard
flow at phone and desktop sizes, and then checks the server's log and the
records the browser downloaded.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from corla_observer.contests import fold, observer_modes
from corla_observer.events import EventLog
from corla_observer.observer_record import check_record, load_record, render_report
from corla_observer.practice import practice_stats
from corla_observer.receipts import ReceiptSigner
from corla_observer.web.app import AppConfig, create_app
from testland import make_testland

pytestmark = pytest.mark.browser
SCRIPT = Path(__file__).parent.parent / "scripts" / "browser_per_contest.py"


def playwright_python() -> str | None:
    """``$CORLA_OBSERVER_PLAYWRIGHT_PYTHON``, else the first ``python3`` on the PATH that has Playwright (often not this one)."""
    candidates = [os.environ.get("CORLA_OBSERVER_PLAYWRIGHT_PYTHON", "")]
    candidates += [str(Path(d) / "python3") for d in os.get_exec_path()]
    for python in candidates:
        if python and shutil.which(python):
            found = subprocess.run([python, "-c", "import playwright.sync_api"], capture_output=True, check=False)
            if found.returncode == 0:
                return python
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_keyboard_flow_on_a_long_ballot(tmp_path: Path) -> None:
    python = playwright_python()
    if python is None:
        pytest.skip("no Python with Playwright")
    root = tmp_path / "ws"
    phone, desktop = make_testland(root, ("phone", "desktop"))
    signer = ReceiptSigner.generate()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(AppConfig(workspace=root, secret=b"s" * 32, receipt_key=signer)), host="127.0.0.1", port=port, log_level="warning",
    ))  # fmt: skip
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        out = Path(os.environ.get("CORLA_OBSERVER_SCREENSHOTS") or tmp_path / "shots")
        urls = [f"http://127.0.0.1:{port}/observe/{t}" for t in (phone, desktop)]
        result = subprocess.run([python, str(SCRIPT), str(out), *urls], capture_output=True, text=True, timeout=300, check=False)
        summary: dict[str, Any] = json.loads(result.stdout) if result.stdout.startswith("{") else {"ok": False, "error": result.stderr[-3000:]}
        failed = [c for c in summary.get("checks", []) if not c["ok"]]
        assert result.returncode == 0 and summary["ok"], json.dumps(failed or summary, indent=1)
    finally:
        server.should_exit = True
        thread.join(10)

    with EventLog.open(root) as log:
        events = list(log.iter_events())
    jsonl = root / "events.jsonl"
    for name in ("phone", "desktop"):
        report = check_record(load_record(out / f"{name}-record.json"), jsonl, server_key=signer.public_raw)
        assert report.ok, render_report(report)
        actor = f"observer:{name}"
        assert observer_modes(events)[(actor, "Testland", "1")] == ["two_tap", "per_contest"]  # two taps chosen on the notice, then switched
        modes = [e.data["mode"] for e in events if e.kind == "observation.entry_mode" and e.actor == actor]
        assert modes == ["two_tap", "per_contest", "two_tap", "per_contest"], modes  # recorded at the start, then each switch
        ballots = fold(events)[(actor, "Testland", "1")]
        (rows,) = ballots.values()
        assert rows.rows_total == 60
        heard = [r for r in rows.rows.values() if r.readings.get(1) and r.readings[1].outcome == "heard_instead"]
        unclear = [r for r in rows.rows.values() if r.readings.get(1) and r.readings[1].outcome == "unclear"]
        assert len(heard) == 1 and heard[0].differs and len(unclear) == 1 and not unclear[0].unresolved
    observers, _ = practice_stats(events)
    assert {o.actor: o.contest_differs for o in observers} == {"observer:phone": 1, "observer:desktop": 1}
    assert sum(1 for e in events if e.kind == "observation.acknowledged") == 2  # the notice, once per device
    check_screenshots(root, out)


def check_screenshots(root: Path, out: Path) -> None:
    """The photo check on the browser's own screenshots of the observer screen: both modes, phone and desktop, and a forged one."""
    from corla_observer.photocheck import Verdict, check_photo
    from corla_observer.photoimage import missing_tools
    from corla_observer.workspace import Workspace

    if missing_tools():
        pytest.skip(f"the photo check needs {', '.join(missing_tools())}")
    ws = Workspace.open(root)
    with EventLog.open(root, read_only=True) as log:
        for size in ("phone", "desktop"):
            for mode in ("two_tap", "per_contest"):
                result = check_photo(ws, log, (out / f"{size}-screen-{mode}.png").read_bytes())
                assert result.kind == "screen" and result.located_by == "qr", result.summary
                assert result.screen is not None and result.screen.n == 1 and result.county == "Testland"
                counts = result.counts()
                assert result.verdict == Verdict.MATCH and counts["matched"] >= 8, (size, mode, result.summary, [(x.key, x.read) for x in result.lines if x.status != "matched"])
            forged = check_photo(ws, log, (out / f"{size}-screen-tampered.png").read_bytes())
            assert forged.verdict == Verdict.MISMATCH and [d.key for d in forged.differences()] == ["screen:c1"], (size, forged.summary)
