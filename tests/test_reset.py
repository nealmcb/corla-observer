"""Resetting a practice workspace (corla_observer.reset, ``corla-observer workspace reset``): the new log is linked to the old one."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog
from corla_observer.reset import RESET_KIND, reset_workspace
from corla_observer.web.accounts import AccountStore


def _used_workspace(root: Path) -> tuple[int, str]:
    with EventLog.open(root) as log:
        AccountStore(root).add("ann", "correct horse battery", log)
        log.append("coordinator.login", "coordinator:ann", data={"name": "ann"})
        log.append("coordinator.reset_requested", "coordinator:ann", data={"command": ["reset"]})
        head = log.head()
    (root / "files").mkdir()
    (root / "files" / "something").write_bytes(b"a stored file")
    (root / "accounts" / "links.json").write_text("{}", encoding="utf-8")
    return head


def test_a_reset_archives_empties_and_links_the_new_log_to_the_old(tmp_path: Path) -> None:
    root = tmp_path / "practice-ws"
    head_seq, head_hash = _used_workspace(root)
    accounts = (root / "accounts" / "coordinators.json").read_bytes()
    report = reset_workspace(root, tmp_path / "backups")

    # The archive holds the old workspace, its old log included; its hash is the one recorded.
    assert report.archive.parent == tmp_path / "backups" and report.archive.name.startswith("practice-ws-")
    assert hashlib.sha256(report.archive.read_bytes()).hexdigest() == report.archive_sha256
    with tarfile.open(report.archive) as tar:
        assert {"practice-ws/events.jsonl", "practice-ws/files/something", "practice-ws/accounts/links.json"} <= set(tar.getnames())
    # The workspace: only the coordinator accounts, and a new log whose first event names the old log's head and the archive.
    assert (root / "accounts" / "coordinators.json").read_bytes() == accounts and not (root / "files").exists()
    assert not (root / "accounts" / "links.json").exists()
    assert (root / "accounts" / "coordinators.json").stat().st_mode & 0o777 == 0o600
    with EventLog.open(root, read_only=True) as log:
        (event,) = list(log.iter_events())
        assert log.verify().ok
    assert event.kind == RESET_KIND and event.seq == 1 == report.seq
    assert event.data["previous_head_seq"] == head_seq and event.data["previous_head_hash"] == head_hash and event.data["previous_events"] == head_seq
    assert event.data["archive"] == report.archive.name and event.data["archive_sha256"] == report.archive_sha256
    assert event.data["requested_by"] == "coordinator:ann" and event.data["requested_seq"] == head_seq
    assert event.data["kept"] == ["accounts/coordinators.json"]

    # A second reset links to the first's log, of one event, with no request recorded.
    again = reset_workspace(root, tmp_path / "backups")
    with EventLog.open(root, read_only=True) as log:
        (second,) = list(log.iter_events())
    assert second.data["previous_head_seq"] == 1 and second.data["previous_events"] == 1 and second.data["requested_by"] is None
    assert again.archive != report.archive or again.archive_sha256 != report.archive_sha256


def test_the_command_needs_confirm(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _used_workspace(root)
    runner = CliRunner()
    refused = runner.invoke(app, ["workspace", "reset", "--workspace", str(root), "--backups", str(tmp_path / "b")])
    assert refused.exit_code == 2 and (root / "files" / "something").exists()
    done = runner.invoke(app, ["workspace", "reset", "--workspace", str(root), "--backups", str(tmp_path / "b"), "--confirm"])
    assert done.exit_code == 0, done.output
    assert "the new log starts with workspace.reset, after the old log's 3 events" in done.output and not (root / "files").exists()
