"""Updating the software during an audit (docs/PLAN.md, feedback item 7): the update is recorded, and nothing is lost.

A workspace in use (a populated Testland list, an observer's entry) is served by one version of
the software, then by another, as `scripts/update_sprite.sh` does on a sprite. Every start is
recorded as ``server.started`` (`corla_observer.version.record_start`): the update's names both
versions with ``updated`` true, a restart on the same version has it false; every earlier event
is kept unchanged with the hash chain intact, and the observer's link, list, extract and entry
are as before.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corla_observer import version
from corla_observer.events import EventLog
from corla_observer.practice import LinkStore
from corla_observer.quicksetup import populate
from corla_observer.version import SoftwareIdentity
from corla_observer.web.app import ROUTER_MODULES, AppConfig, create_app
from corla_observer.workspace import Workspace
from test_web import SECRET

# The web modules are imported before `version.current` is patched: a module that imports
# `current` by name when first loaded would otherwise keep the fake one for every later test.
for _module in ROUTER_MODULES:
    importlib.import_module(_module)

OLD = SoftwareIdentity("0.1.0", "a" * 40, False, {"subject": "the version before"}, "git")
NEW = SoftwareIdentity("0.1.0", "b" * 40, False, {"subject": "the version after"}, "git")


def _serve(root: Path, token: str, monkeypatch: pytest.MonkeyPatch, identity: SoftwareIdentity) -> dict[str, Any]:
    """Start the web app on ``root`` as ``identity``, and read the observer's page data."""
    monkeypatch.setattr(version, "current", lambda: identity)
    app = create_app(AppConfig(workspace=root, secret=SECRET, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        data: dict[str, Any] = client.get(f"/observe/{token}/data.json").json()
    return data


def _starts(root: Path) -> list[dict[str, Any]]:
    with EventLog.open(root, read_only=True) as log:
        return [dict(e.data) for e in log.iter_events() if e.kind == version.STARTED_KIND]


def test_every_start_is_recorded_and_an_update_keeps_every_event_link_list_and_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
    before = _serve(store.root, made.observer_token, monkeypatch, OLD)
    assert [(u["software"], u["previous"], u["updated"]) for u in _starts(store.root)] == [(OLD.software, None, False)]
    first = before["ballots"][0]
    with EventLog.open(store.root) as log:
        log.append("observation.entry", "observer:ann", county="Testland", board="1", device_id="d-" + "a" * 32,
                   data={"position": first["position"], "imprinted_id": first["id"], "match": True, "note": "", "extract_sha256": before["extract_sha256"]})  # fmt: skip
        kept = [(e.seq, e.kind, e.hash) for e in log.iter_events()]
    links = LinkStore(store.root).links()

    # The update: the same workspace served by the new version.
    after = _serve(store.root, made.observer_token, monkeypatch, NEW)
    updates = _starts(store.root)
    assert [(u["software"], u["previous"], u["updated"]) for u in updates] == [(OLD.software, None, False), (NEW.software, OLD.software, True)]
    assert updates[-1]["commit"] == NEW.commit and updates[-1]["previous_commit"] == OLD.commit and updates[-1]["subject"] == "the version after"
    with EventLog.open(store.root, read_only=True) as log:
        now = [(e.seq, e.kind, e.hash) for e in log.iter_events()]
        report = log.verify()
    assert now[: len(kept)] == kept and [k for _, k, _ in now[len(kept) :]] == [version.STARTED_KIND, version.STOPPED_KIND]
    assert report.ok, report.errors
    assert LinkStore(store.root).links() == links
    assert after["extract_sha256"] == before["extract_sha256"] and after["ballots"] == before["ballots"] and after["list"] == before["list"]
    assert list(after["entries"]) == [str(first["position"])]  # the entry, by position

    # A restart on the same version is recorded too, as not an update; each graceful stop is recorded before the next start.
    _serve(store.root, made.observer_token, monkeypatch, NEW)
    assert [(u["software"], u["previous"], u["updated"]) for u in _starts(store.root)][-1] == (NEW.software, NEW.software, False)
    with EventLog.open(store.root, read_only=True) as log:
        cycle = [e.kind for e in log.iter_events() if e.kind in (version.STARTED_KIND, version.STOPPED_KIND)]
    assert cycle == [version.STARTED_KIND, version.STOPPED_KIND] * 3


def test_a_sigterm_records_the_stop_even_with_a_live_page_open(tmp_path: Path) -> None:
    """As the sprite's service manager stops it: ``corla-observer serve`` in its own process, a browser's live stream open, SIGTERM, then SIGKILL a second later.

    The stream never ends by itself, and the manager kills the process about a second after its TERM
    (seen on corla-demo): the stop must be recorded as the signal arrives, not after the wait.
    """
    import http.client
    import signal
    import subprocess
    import sys
    import time

    from test_browser_per_contest import free_port

    root = tmp_path / "ws"
    Workspace.open(root)
    port = free_port()
    command = Path(sys.executable).parent / "corla-observer"
    process = subprocess.Popen(
        [str(command), "serve", "--workspace", str(root), "--port", str(port), "--secret-file", str(tmp_path / "secret")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    stream = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                probe.request("GET", "/version.json")
                if probe.getresponse().status == 200:
                    break
            except OSError:
                time.sleep(0.2)
        assert process.poll() is None, process.stderr.read().decode() if process.stderr else ""
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        stream.request("GET", "/events/stream")
        assert stream.getresponse().status == 200  # left open, as a dashboard's
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()  # as the service manager does
            process.wait(timeout=10)
    finally:
        if stream is not None:
            stream.close()
        if process.poll() is None:
            process.kill()
    with EventLog.open(root, read_only=True) as log:
        found = [(e.kind, e.data.get("reason")) for e in log.iter_events() if e.kind in (version.STARTED_KIND, version.STOPPED_KIND)]
    assert found == [(version.STARTED_KIND, None), (version.STOPPED_KIND, "signal SIGTERM")]
