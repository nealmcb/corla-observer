"""Coordinator sessions: 30 days, no limit on time without use, kept across restarts, never storing a usable token."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from corla_observer.web import sessions
from corla_observer.web.sessions import SESSION_LIFETIME, SessionStore

SECRET = b"s" * 32


def test_a_session_survives_a_restart_and_logging_out_ends_it(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "sessions.json"
    cookie, session = SessionStore(SECRET, path).create("alice", "coordinator:alice")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = cookie.rsplit(".", 1)[0]
    assert token not in path.read_text()  # only its hash is kept
    restarted = SessionStore(SECRET, path)
    again = restarted.get(cookie)
    assert (
        again is not None
        and again.label == session.label
        and again.csrf == session.csrf
    )
    assert restarted.end(cookie) is not None
    assert SessionStore(SECRET, path).get(cookie) is None


def test_no_idle_limit_but_thirty_days_from_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SESSION_LIFETIME == 30 * 24 * 3600
    now = [1_000_000.0]
    monkeypatch.setattr(sessions.time, "time", lambda: now[0])
    store = SessionStore(SECRET, tmp_path / "sessions.json")
    cookie, _ = store.create("alice", "coordinator:alice")
    now[0] += 10 * 24 * 3600  # ten days untouched
    assert store.get(cookie) is not None
    now[0] += 21 * 24 * 3600
    assert store.get(cookie) is None
    assert SessionStore(SECRET, tmp_path / "sessions.json").get(cookie) is None


def test_a_forged_cookie_or_a_damaged_file_only_means_logging_in_again(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.json"
    cookie, _ = SessionStore(SECRET, path).create("alice", "coordinator:alice")
    assert SessionStore(b"t" * 32, path).get(cookie) is None  # another server secret
    path.write_text("not json")
    assert SessionStore(SECRET, path).get(cookie) is None
    path.write_text(json.dumps({"format": 1, "sessions": {"x": {"account": "a"}}}))
    assert len(SessionStore(SECRET, path)) == 0


def test_without_a_path_sessions_stay_in_memory() -> None:
    store = SessionStore(SECRET)
    cookie, _ = store.create("alice", "coordinator:alice")
    assert store.get(cookie) is not None
