"""Resetting a practice workspace, with the new event log linked to the old one.

A testing-mode deployment (a Reset button on the coordinator home page, or
``scripts/demo_delta_2020.sh --reset``) puts its workspace back to an empty
starting state. Done by hand, the new event log began again at event 1 with
nothing to say a reset happened or which log came before, and the
``coordinator.reset_requested`` event was only in the archived copy.
`reset_workspace` does the whole reset and joins the two records:

1. the old event log, if there is one, is read for its head (last seq and
   hash), its count, and the latest ``coordinator.reset_requested`` (who
   asked, when, its seq);
2. the whole workspace is archived as ``BACKUPS/NAME-TIME.tgz`` and the
   archive hashed (SHA-256);
3. everything in the workspace is deleted except the coordinator accounts
   (``accounts/coordinators.json``; observer and reader links go);
4. the new event log starts with ``workspace.reset``: ``data``
   ``previous_head_seq``, ``previous_head_hash``, ``previous_events``,
   ``archive`` (the archive's file name), ``archive_sha256``,
   ``requested_by``, ``requested_at``, ``requested_seq`` (null if no request
   was recorded), ``kept``.

So the chain of logs can be followed back: the new log's first event names
the old log's head, and the archive holding it by hash. Stop the web server
first (it keeps the event log open) and start it again after.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corla_observer.events import EventLog

#: What a reset keeps, relative to the workspace.
KEPT = ("accounts/coordinators.json",)
RESET_KIND = "workspace.reset"


@dataclass
class ResetReport:
    """What `reset_workspace` did."""

    archive: Path
    archive_sha256: str
    previous_head_seq: int
    previous_head_hash: str | None
    previous_events: int
    seq: int
    data: dict[str, Any] = field(default_factory=dict)


def _previous(root: Path) -> tuple[int, str | None, int, dict[str, Any]]:
    """The old log's head seq and hash, its count, and its latest reset request, if it has a log."""
    if not (root / "events.sqlite3").exists():
        return 0, None, 0, {}
    request: dict[str, Any] = {}
    count = 0
    with EventLog.open(root, read_only=True) as log:
        for event in log.iter_events():
            count += 1
            if event.kind == "coordinator.reset_requested":
                request = {"requested_by": event.actor, "requested_at": event.server_time, "requested_seq": event.seq}
        seq, digest = log.head()
    return seq, digest, count, request


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def reset_workspace(root: Path, backups: Path, *, actor: str = "system", now: datetime | None = None) -> ResetReport:
    """Archive the workspace, empty it but for `KEPT`, and start its new log with ``workspace.reset`` (see the module documentation)."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"no workspace at {root}")
    head_seq, head_hash, count, request = _previous(root)
    backups.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    archive = backups / f"{root.name}-{stamp}.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname=root.name)
    archive_sha = _sha256(archive)
    kept = {name: (root / name).read_bytes() for name in KEPT if (root / name).is_file()}
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name, body in kept.items():
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o600)
    data: dict[str, Any] = {
        "previous_head_seq": head_seq, "previous_head_hash": head_hash, "previous_events": count,
        "archive": archive.name, "archive_sha256": archive_sha,
        "requested_by": request.get("requested_by"), "requested_at": request.get("requested_at"), "requested_seq": request.get("requested_seq"),
        "kept": sorted(kept),
    }  # fmt: skip
    with EventLog.open(root) as log:
        event = log.append(RESET_KIND, actor, data=data)
    return ResetReport(archive, archive_sha, head_seq, head_hash, count, event.seq, data)
