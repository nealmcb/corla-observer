"""Fast tests for corla_observer.events, in temporary directories."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import (
    FIELDS,
    FORMAT_VERSION,
    GENESIS_PREV_HASH,
    Event,
    EventLog,
    EventLogError,
    InvalidEventError,
    LogMismatchError,
    canonical_json,
    format_server_time,
    rebuild_database_from_jsonl,
    verify_jsonl,
)

EVENT_LOG_DOC = Path(__file__).resolve().parents[1] / "docs" / "EVENT_LOG.md"
START = datetime(2026, 11, 16, 15, 30, 0, 123000, tzinfo=UTC)


def ticking_clock(start: datetime = START) -> Callable[[], datetime]:
    """A clock that advances one millisecond per call, from ``start``."""
    moments: Iterator[datetime] = (start + timedelta(milliseconds=i) for i in range(10**9))
    lock = threading.Lock()

    def clock() -> datetime:
        with lock:
            return next(moments)

    return clock


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "log"


@pytest.fixture(scope="module")
def shared_log(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EventLog]:
    """One log for the many small validation cases, to keep them fast."""
    log = EventLog.open(tmp_path_factory.mktemp("shared"))
    yield log
    assert log.verify().ok
    log.close()


def _populate(log_dir: Path, count: int = 5) -> list[Event]:
    with EventLog.open(log_dir, clock=ticking_clock()) as log:
        return [
            log.append(
                "observation.entered",
                f"observer:o{i % 2}",
                county="Boulder",
                board="1",
                data={"position": i, "match": i % 3 != 0},
            )
            for i in range(1, count + 1)
        ]


def _lines(log_dir: Path) -> list[bytes]:
    return (log_dir / "events.jsonl").read_bytes().splitlines(keepends=True)


def _write_lines(log_dir: Path, lines: list[bytes]) -> None:
    (log_dir / "events.jsonl").write_bytes(b"".join(lines))


def _rehash_line(obj: dict[str, Any]) -> bytes:
    """Recompute an edited event's hash, as a careful forger would."""
    body = {k: v for k, v in obj.items() if k != "hash"}
    obj["hash"] = hashlib.sha256(canonical_json(body)).hexdigest()
    return Event.from_dict(obj).to_line() + b"\n"


# ---------------------------------------------------------------------------
# Appending and verifying


def test_append_and_verify(log_dir: Path) -> None:
    with EventLog.open(log_dir, clock=ticking_clock()) as log:
        assert log.head() == (0, GENESIS_PREV_HASH)
        assert log.verify().ok
        first = log.append(
            "file.received",
            "coordinator:c1",
            county="Boulder",
            file_sha256="a" * 64,
            data={"filename": "cvr.csv", "size": 12},
        )
        second = log.append(
            "observation.entered",
            "observer:o1",
            client_time="2026-11-16T15:29:59.900Z",
            county="Boulder",
            board="2",
            session_id="s-1",
            device_id="d-1",
            signature="c2lnbmF0dXJl",
            data={"imprinted_id": "101-23-45", "match": True, "note": None, "votes": ["Yes"]},
        )
        third = log.append("system.started", "system")
        assert (first.seq, second.seq, third.seq) == (1, 2, 3)
        assert first.prev_hash == GENESIS_PREV_HASH
        assert second.prev_hash == first.hash and third.prev_hash == second.hash
        assert first.server_time == "2026-11-16T15:30:00.123Z"
        assert third.data == {}
        assert log.head() == (3, third.hash)
        assert len(log) == 3
        assert list(log.iter_events()) == [first, second, third]
        assert list(log.iter_events(after_seq=2)) == [third]
        assert list(log.iter_events(after_seq=0, page_size=1)) == [first, second, third]
        report = log.verify()
        assert report.ok, report.errors
        assert (report.event_count, report.head_seq, report.head_hash) == (3, 3, third.hash)
    jsonl_report = verify_jsonl(log_dir / "events.jsonl")
    assert jsonl_report.ok and jsonl_report.head_hash == third.hash
    with EventLog.open(log_dir) as reopened:
        assert reopened.head() == (3, third.hash)
        assert reopened.recovered == []
        assert reopened.append("system.stopped", "system").prev_hash == third.hash


def test_open_paths(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    with EventLog.open(db) as log:
        log.append("system.started", "system")
    assert (tmp_path / "audit.jsonl").stat().st_size > 0
    other = tmp_path / "elsewhere.jsonl"
    with EventLog.open(tmp_path / "b.sqlite3", jsonl_path=other) as log:
        log.append("system.started", "system")
    assert other.stat().st_size > 0
    with pytest.raises(FileNotFoundError):
        EventLog.open(tmp_path / "absent.sqlite3", create=False)
    with pytest.raises(FileNotFoundError):
        EventLog.open(tmp_path / "absent.sqlite3", read_only=True)


def test_data_is_copied_at_append(log_dir: Path) -> None:
    data: dict[str, Any] = {"votes": ["Yes"]}
    with EventLog.open(log_dir) as log:
        event = log.append("observation.entered", "observer:o1", data=data)
        data["votes"].append("No")
        assert event.data == {"votes": ["Yes"]}
        assert log.verify().ok


def test_server_time_format() -> None:
    moment = datetime(2026, 11, 16, 8, 30, 0, 123999, tzinfo=UTC)
    assert format_server_time(moment) == "2026-11-16T08:30:00.123Z"
    mountain = moment.astimezone(timezone(timedelta(hours=-7)))
    assert format_server_time(mountain) == "2026-11-16T08:30:00.123Z"
    with pytest.raises(ValueError):
        format_server_time(datetime(2026, 11, 16))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": "admin"},
        {"actor": "observer:"},
        {"actor": "observer:has space"},
        {"actor": "coordinator:x@example"},
        {"kind": "upload"},
        {"kind": "File.received"},
        {"kind": "file..received"},
        {"kind": "file.received."},
        {"file_sha256": "A" * 64},
        {"file_sha256": "a" * 63},
        {"signature": "not base64!"},
        {"signature": "abc"},
        {"signature": ""},
        {"client_time": "2026-11-16T15:30:00Z"},
        {"client_time": "2026-11-16 15:30:00.000Z"},
        {"client_time": "2026-13-16T15:30:00.000Z"},
        {"county": ""},
        {"county": "Boulder\n"},
        {"board": 1},
        {"session_id": "has space"},
        {"device_id": "x" * 129},
        {"data": []},
        {"data": {"x": 1.5}},
        {"data": {"x": [1, {"y": 2.0}]}},
        {"data": {"x": float("nan")}},
        {"data": {"x": 2**53}},
        {"data": {"x": -(2**53)}},
        {"data": {1: "integer key"}},
        {"data": {"": "empty key"}},
        {"data": {"clé": "non-ASCII key"}},
        {"data": {"x": b"bytes"}},
        {"data": {"x": "\ud800"}},
        {"data": {"x": "y" * 70000}},
    ],
)
def test_invalid_fields_rejected(shared_log: EventLog, kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {"kind": "observation.entered", "actor": "observer:o1"}
    arguments.update(kwargs)
    head = shared_log.head()
    size = shared_log.jsonl_path.stat().st_size
    with pytest.raises(InvalidEventError):
        shared_log.append(**arguments)
    # Nothing was written, and the log still works.
    assert shared_log.head() == head
    assert shared_log.jsonl_path.stat().st_size == size
    assert shared_log.append("system.started", "system").seq == head[0] + 1


def test_accepted_edge_values(log_dir: Path) -> None:
    with EventLog.open(log_dir) as log:
        event = log.append(
            "a.b",
            "coordinator:C.1_x-y",
            county="Río Blanco",
            data={
                "big": 2**53 - 1,
                "small": -(2**53 - 1),
                "text": 'quote " backslash \\ newline \n tab \t nul \x00 emoji \U0001f5f3',
                "nested": {"list": [True, False, None, 0, ""]},
            },
        )
        assert Event.from_line(event.to_line()) == event
        assert log.verify().ok


@pytest.mark.parametrize("version", [0, 2, "1", True, None])
def test_other_format_versions_rejected(version: object) -> None:
    with pytest.raises(InvalidEventError, match="format version"):
        Event.create(
            seq=1,
            server_time="2026-11-16T15:30:00.000Z",
            actor="system",
            kind="system.started",
            prev_hash=GENESIS_PREV_HASH,
            v=version,  # type: ignore[arg-type]
        )


def test_unknown_format_version_reported(log_dir: Path, tmp_path: Path) -> None:
    _populate(log_dir, 3)
    lines = _lines(log_dir)
    obj = json.loads(lines[1])
    obj["v"] = 2
    body = {k: v for k, v in obj.items() if k != "hash"}
    hash_input = canonical_json(body)
    obj_hash = hashlib.sha256(hash_input).hexdigest()
    lines[1] = hash_input[:-1] + b',"hash":"' + obj_hash.encode() + b'"}\n'
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok and report.first_bad_line == 2
    assert "unsupported format version v=2" in report.errors[0]
    assert "format version" in _run_documented_verifier(log_dir / "events.jsonl")
    missing = tmp_path / "missing.jsonl"
    del body["v"]
    hash_input = canonical_json(body)
    obj_hash = hashlib.sha256(hash_input).hexdigest()
    missing.write_bytes(lines[0] + hash_input[:-1] + b',"hash":"' + obj_hash.encode() + b'"}\n')
    report = verify_jsonl(missing)
    assert not report.ok and "no format version" in report.errors[0]


def test_floats_rejected_in_jsonl(log_dir: Path) -> None:
    _populate(log_dir, 2)
    lines = _lines(log_dir)
    lines[1] = lines[1].replace(b'"position":2', b'"position":2.0')
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok and report.first_bad_line == 2
    assert "float" in report.errors[0]


# ---------------------------------------------------------------------------
# The documented format


def worked_example_events() -> list[Event]:
    """The two events of the worked example in docs/EVENT_LOG.md."""
    first = Event.create(
        seq=1,
        server_time="2026-11-16T15:30:00.123Z",
        actor="coordinator:c1",
        kind="file.received",
        county="Boulder",
        file_sha256=hashlib.sha256(b"example\n").hexdigest(),
        data={"filename": "Boulder_redacted_CVR.csv", "role": "redacted_cvr", "size": 8},
        prev_hash=GENESIS_PREV_HASH,
    )
    second = Event.create(
        seq=2,
        server_time="2026-11-17T16:05:42.007Z",
        client_time="2026-11-17T16:05:41.950Z",
        actor="observer:o7",
        kind="observation.entered",
        county="Boulder",
        board="2",
        session_id="s-9f2c",
        device_id="d-41aa",
        signature="c2lnbmF0dXJl",
        data={"imprinted_id": "101-23-45", "match": False, "note": "heard “No” on 2"},
        prev_hash=first.hash,
    )
    return [first, second]


def _hexdump(data: bytes) -> str:
    rows = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        rows.append(f"{offset:04x}  {' '.join(f'{b:02x}' for b in chunk)}")
    return "\n".join(rows)


def worked_example_blocks() -> dict[str, str]:
    """The exact text of each worked-example block in docs/EVENT_LOG.md, from the code."""
    first, second = worked_example_events()
    blocks: dict[str, str] = {}
    for name, event in (("event1", first), ("event2", second)):
        hash_input = event.hash_input()
        blocks[f"{name}-hash-input"] = hash_input.decode("utf-8")
        blocks[f"{name}-hash"] = f"{len(hash_input)} bytes\nSHA-256 {event.hash}"
        blocks[f"{name}-line"] = event.to_line().decode("utf-8")
    note = second.data["note"]
    assert isinstance(note, str)
    blocks["event2-note-bytes"] = _hexdump(json.dumps(note, ensure_ascii=False).encode("utf-8"))
    return blocks


def _doc_blocks(text: str) -> dict[str, str]:
    """Fenced blocks that follow a ``<!-- worked-example:NAME -->`` marker."""
    pattern = re.compile(r"<!-- (worked-example|verifier):([a-z0-9-]+) -->\n```[a-z]*\n(.*?)\n```", re.S)
    return {f"{kind}:{name}": body for kind, name, body in pattern.findall(text)}


def test_worked_example_in_documentation() -> None:
    blocks = _doc_blocks(EVENT_LOG_DOC.read_text(encoding="utf-8"))
    expected = worked_example_blocks()
    for name, body in expected.items():
        assert blocks.get(f"worked-example:{name}") == body, (
            f"docs/EVENT_LOG.md block {name} is out of date; it should be:\n{body}"
        )
    # The example's JSONL lines verify as a log.
    first, second = worked_example_events()
    assert Event.from_line(first.to_line()) == first
    assert hashlib.sha256(expected["event1-hash-input"].encode("utf-8")).hexdigest() == first.hash


def _run_documented_verifier(path: Path) -> str:
    """Run the minimal verifier printed in docs/EVENT_LOG.md on ``path``."""
    blocks = _doc_blocks(EVENT_LOG_DOC.read_text(encoding="utf-8"))
    source = blocks["verifier:python"]
    namespace: dict[str, Any] = {"__name__": "documented_verifier"}
    exec(compile(source, str(EVENT_LOG_DOC), "exec"), namespace)
    result = namespace["verify"](str(path))
    assert isinstance(result, str)
    return result


def test_documented_verifier(tmp_path: Path) -> None:
    jsonl = tmp_path / "example.jsonl"
    jsonl.write_bytes(b"".join(e.to_line() + b"\n" for e in worked_example_events()))
    first, second = worked_example_events()
    assert _run_documented_verifier(jsonl) == f"OK: 2 events, head {second.hash}"
    _populate(tmp_path / "log", 4)
    log_jsonl = tmp_path / "log" / "events.jsonl"
    assert _run_documented_verifier(log_jsonl).startswith("OK: 4 events")
    lines = log_jsonl.read_bytes().splitlines(keepends=True)
    log_jsonl.write_bytes(lines[0] + lines[2] + lines[1] + lines[3])
    assert _run_documented_verifier(log_jsonl).startswith("BROKEN at line 2")
    lines[2] = lines[2].replace(b'"position":3', b'"position":33')
    log_jsonl.write_bytes(b"".join(lines))
    assert _run_documented_verifier(log_jsonl).startswith("BROKEN at line 3")


def test_canonical_serialization_rules() -> None:
    assert canonical_json({"b": 1, "a": [True, None, "é"], "c": {"z": 0, "y": -1}}) == (
        '{"a":[true,null,"é"],"b":1,"c":{"y":-1,"z":0}}'.encode("utf-8")
    )
    assert canonical_json({"s": "\x01\n\"\\/ "}) == '{"s":"\\u0001\\n\\"\\\\/ "}'.encode(
        "utf-8"
    )
    first = worked_example_events()[0]
    line = first.to_line()
    # Every field is present, absent optional ones as null; hash comes last.
    assert set(json.loads(line)) == set(FIELDS)
    assert b'"board":null' in line
    assert line.endswith(b',"v":1,"hash":"' + first.hash.encode() + b'"}')
    assert first.v == FORMAT_VERSION == 1


# ---------------------------------------------------------------------------
# Tampering


def test_edited_jsonl_line_detected(log_dir: Path) -> None:
    _populate(log_dir, 5)
    lines = _lines(log_dir)
    lines[2] = lines[2].replace(b'"position":3', b'"position":33')
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok
    assert (report.first_bad_line, report.first_bad_seq, report.event_count) == (3, 3, 2)
    with EventLog.open(log_dir, read_only=True) as log:
        full = log.verify()
    assert not full.ok and full.first_bad_seq == 3


def test_edited_last_line_detected_on_open(log_dir: Path) -> None:
    _populate(log_dir, 3)
    lines = _lines(log_dir)
    lines[-1] = lines[-1].replace(b'"observer:o1"', b'"observer:o2"')
    _write_lines(log_dir, lines)
    with pytest.raises(LogMismatchError):
        EventLog.open(log_dir)


def test_rehashed_jsonl_edit_breaks_the_next_link(log_dir: Path) -> None:
    _populate(log_dir, 4)
    lines = _lines(log_dir)
    obj = json.loads(lines[1])
    obj["data"]["match"] = not obj["data"]["match"]
    lines[1] = _rehash_line(obj)
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok and report.first_bad_line == 3
    assert "prev_hash" in report.errors[0]


def test_rewritten_jsonl_chain_caught_by_database(log_dir: Path) -> None:
    """A forger who rewrites the whole JSONL chain consistently still disagrees with the database."""
    _populate(log_dir, 3)
    lines = _lines(log_dir)
    prev = GENESIS_PREV_HASH
    rewritten = []
    for i, raw in enumerate(lines):
        obj = json.loads(raw)
        if i == 0:
            obj["data"]["match"] = not obj["data"]["match"]
        obj["prev_hash"] = prev
        line = _rehash_line(obj)
        prev = json.loads(line)["hash"]
        rewritten.append(line)
    _write_lines(log_dir, rewritten)
    assert verify_jsonl(log_dir / "events.jsonl").ok
    with EventLog.open(log_dir, read_only=True) as log:
        report = log.verify()
    assert not report.ok and report.first_bad_seq == 1
    assert any("differs from JSONL" in e for e in report.errors)


def _tamper_database(log_dir: Path, *statements: str) -> None:
    conn = sqlite3.connect(log_dir / "events.sqlite3", isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("DROP TRIGGER events_no_delete")
    conn.execute("DROP TRIGGER events_extend_chain")
    for statement in statements:
        conn.execute(statement)
    conn.close()


def test_changed_database_row_detected(log_dir: Path) -> None:
    _populate(log_dir, 4)
    _tamper_database(log_dir, "UPDATE events SET data = '{\"match\":false,\"position\":2}' WHERE seq = 2")
    with EventLog.open(log_dir, read_only=True) as log:
        report = log.verify()
    assert not report.ok and report.first_bad_seq == 2
    assert any("triggers missing" in e for e in report.errors)
    assert any(e.startswith("database: event 2: hash") for e in report.errors)
    # The JSONL is untouched and still verifies on its own.
    assert verify_jsonl(log_dir / "events.jsonl").ok


def test_database_row_rehashed_still_disagrees(log_dir: Path) -> None:
    _populate(log_dir, 2)
    with EventLog.open(log_dir, read_only=True) as log:
        events = list(log.iter_events())
    obj = events[1].to_dict()
    obj["actor"] = "observer:o9"
    forged = Event.from_line(_rehash_line(obj))
    _tamper_database(
        log_dir, f"UPDATE events SET actor = 'observer:o9', hash = '{forged.hash}' WHERE seq = 2"
    )
    with EventLog.open(log_dir, read_only=True) as log:
        report = log.verify()
    assert not report.ok and report.first_bad_seq == 2


def test_deleted_database_row_detected(log_dir: Path) -> None:
    _populate(log_dir, 4)
    _tamper_database(log_dir, "DELETE FROM events WHERE seq = 2")
    with EventLog.open(log_dir, read_only=True) as log:
        report = log.verify()
    assert not report.ok and report.first_bad_seq == 2


def test_deleted_and_reordered_jsonl_events_detected(log_dir: Path) -> None:
    _populate(log_dir, 4)
    lines = _lines(log_dir)
    jsonl = log_dir / "events.jsonl"
    _write_lines(log_dir, [lines[0], lines[2], lines[3]])
    report = verify_jsonl(jsonl)
    assert not report.ok and report.first_bad_line == 2 and "seq" in report.errors[0]
    _write_lines(log_dir, [lines[0], lines[2], lines[1], lines[3]])
    report = verify_jsonl(jsonl)
    assert not report.ok and report.first_bad_line == 2
    _write_lines(log_dir, lines[:3])
    with pytest.raises(LogMismatchError, match="shorter"):
        EventLog.open(log_dir)


def test_changed_prev_hash_detected(log_dir: Path) -> None:
    _populate(log_dir, 3)
    lines = _lines(log_dir)
    obj = json.loads(lines[1])
    obj["prev_hash"] = "1" * 64
    lines[1] = _rehash_line(obj)
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok and report.first_bad_line == 2
    assert "prev_hash" in report.errors[0]


@pytest.mark.parametrize(
    "mangle",
    [
        lambda line: line.replace(b'{"actor"', b'{ "actor"'),
        lambda line: line.replace(b'"seq":2', b'"seq":2,"seq":2'),
        lambda line: line.replace(b',"hash"', b',"extra":1,"hash"'),
        lambda line: line.rstrip(b"\n"),
        lambda line: b"[1]\n",
        lambda line: b"\xff\n",
    ],
)
def test_malformed_lines_detected(log_dir: Path, mangle: Callable[[bytes], bytes]) -> None:
    _populate(log_dir, 2)
    lines = _lines(log_dir)
    lines[1] = mangle(lines[1])
    _write_lines(log_dir, lines)
    report = verify_jsonl(log_dir / "events.jsonl")
    assert not report.ok and report.first_bad_line == 2


# ---------------------------------------------------------------------------
# Triggers


def test_triggers_block_update_delete_and_out_of_chain_inserts(log_dir: Path) -> None:
    events = _populate(log_dir, 3)
    conn = sqlite3.connect(log_dir / "events.sqlite3", isolation_level=None)
    try:
        for statement in [
            "UPDATE events SET actor = 'observer:x' WHERE seq = 2",
            "UPDATE events SET jsonl_end = 0",
            "DELETE FROM events WHERE seq = 3",
            "DELETE FROM events",
        ]:
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
        row = conn.execute("SELECT * FROM events WHERE seq = 3").fetchone()
        for verb, seq, prev in [
            ("INSERT", 5, events[2].hash),
            ("INSERT", 4, events[1].hash),
            ("INSERT OR REPLACE", 2, events[0].hash),
        ]:
            values = (seq,) + row[1:13] + (prev, "f" * 64, 0)
            with pytest.raises(sqlite3.DatabaseError, match="extend the chain"):
                conn.execute(f"{verb} INTO events VALUES ({','.join('?' * len(values))})", values)
    finally:
        conn.close()
    with EventLog.open(log_dir) as log:
        assert log.verify().ok and log.head() == (3, events[2].hash)


# ---------------------------------------------------------------------------
# Concurrency


def _assert_one_chain(log_dir: Path, expected: int) -> list[Event]:
    with EventLog.open(log_dir) as log:
        report = log.verify()
        assert report.ok, report.errors
        events = list(log.iter_events())
    assert [e.seq for e in events] == list(range(1, expected + 1))
    assert verify_jsonl(log_dir / "events.jsonl").ok
    return events


def test_concurrent_threads_sharing_one_log(log_dir: Path) -> None:
    log = EventLog.open(log_dir)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(8):
                log.append("observation.entered", f"observer:t{n}", data={"i": i})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    assert errors == []
    events = _assert_one_chain(log_dir, 32)
    assert {e.actor for e in events} == {f"observer:t{n}" for n in range(4)}


def test_concurrent_threads_with_their_own_connections(log_dir: Path) -> None:
    EventLog.open(log_dir).close()
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            with EventLog.open(log_dir) as log:
                for i in range(6):
                    log.append("observation.entered", f"observer:c{n}", data={"i": i})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _assert_one_chain(log_dir, 18)


def _append_from_process(log_dir: str, name: str, count: int) -> None:
    with EventLog.open(log_dir) as log:
        for i in range(count):
            log.append("observation.entered", f"observer:{name}", data={"i": i})


def test_concurrent_processes(log_dir: Path) -> None:
    EventLog.open(log_dir).close()
    methods = multiprocessing.get_all_start_methods()
    context: Any = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
    processes = [
        context.Process(target=_append_from_process, args=(str(log_dir), name, 10))
        for name in ("p1", "p2")
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
    assert [p.exitcode for p in processes] == [0, 0]
    events = _assert_one_chain(log_dir, 20)
    by_actor = {a: [e.data["i"] for e in events if e.actor == a] for a in ("observer:p1", "observer:p2")}
    assert by_actor == {"observer:p1": list(range(10)), "observer:p2": list(range(10))}


# ---------------------------------------------------------------------------
# Crash recovery


def _next_event(log_dir: Path, seq_offset: int = 0) -> Event:
    with EventLog.open(log_dir, read_only=True) as log:
        seq, head_hash = log.head()
    return Event.create(
        seq=seq + 1 + seq_offset,
        server_time="2026-11-16T16:00:00.000Z",
        actor="system",
        kind="system.crashed",
        prev_hash=head_hash,
    )


def test_trailing_jsonl_event_recovered(log_dir: Path) -> None:
    _populate(log_dir, 3)
    pending = _next_event(log_dir)
    # A crash after the JSONL write and before the database commit.
    with open(log_dir / "events.jsonl", "ab") as f:
        f.write(pending.to_line() + b"\n")
    with EventLog.open(log_dir, read_only=True) as log:
        report = log.verify()
        assert not report.ok and "after the database's last event" in report.errors[0]
        assert log.head()[0] == 3  # read-only open leaves it alone
    with EventLog.open(log_dir) as log:
        assert log.recovered == [pending]
        assert log.head() == (4, pending.hash)
        assert log.verify().ok
        assert log.append("system.started", "system").seq == 5
    _assert_one_chain(log_dir, 5)


def test_trailing_event_recovered_by_next_append(log_dir: Path) -> None:
    _populate(log_dir, 2)
    with EventLog.open(log_dir) as log:
        pending = _next_event(log_dir)
        with open(log_dir / "events.jsonl", "ab") as f:
            f.write(pending.to_line() + b"\n")
        event = log.append("system.started", "system")
        assert log.recovered == [pending]
        assert event.seq == 4 and event.prev_hash == pending.hash
    _assert_one_chain(log_dir, 4)


@pytest.mark.parametrize("problem", ["two events", "torn line", "wrong seq", "wrong prev_hash", "garbage"])
def test_unrecoverable_jsonl_disagreements(log_dir: Path, problem: str) -> None:
    _populate(log_dir, 2)
    pending = _next_event(log_dir)
    jsonl = log_dir / "events.jsonl"
    with open(jsonl, "ab") as f:
        if problem == "two events":
            follow = Event.create(
                seq=4,
                server_time="2026-11-16T16:00:00.001Z",
                actor="system",
                kind="system.crashed",
                prev_hash=pending.hash,
            )
            f.write(pending.to_line() + b"\n" + follow.to_line() + b"\n")
        elif problem == "torn line":
            f.write(pending.to_line()[:40])
        elif problem == "wrong seq":
            f.write(_next_event(log_dir, seq_offset=1).to_line() + b"\n")
        elif problem == "wrong prev_hash":
            obj = pending.to_dict()
            obj["prev_hash"] = "2" * 64
            f.write(_rehash_line(obj))
        else:
            f.write(b"not json\n")
    size = jsonl.stat().st_size
    with pytest.raises(LogMismatchError):
        EventLog.open(log_dir)
    assert jsonl.stat().st_size == size


def test_missing_jsonl_is_an_error(log_dir: Path) -> None:
    _populate(log_dir, 1)
    (log_dir / "events.jsonl").unlink()
    with pytest.raises(LogMismatchError, match="missing"):
        EventLog.open(log_dir)
    assert not (log_dir / "events.jsonl").exists()
    with EventLog.open(log_dir, read_only=True) as log:
        assert not log.verify().ok


def test_failed_commit_leaves_no_jsonl_line(log_dir: Path) -> None:
    _populate(log_dir, 2)
    jsonl = log_dir / "events.jsonl"
    size = jsonl.stat().st_size
    other = sqlite3.connect(log_dir / "events.sqlite3", isolation_level=None)
    other.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON events "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    with EventLog.open(log_dir) as log:
        with pytest.raises(sqlite3.DatabaseError, match="simulated failure"):
            log.append("system.started", "system")
        assert jsonl.stat().st_size == size
        other.execute("DROP TRIGGER fail_insert")
        other.close()
        assert log.append("system.started", "system").seq == 3
    _assert_one_chain(log_dir, 3)


def test_read_only_log_refuses_appends(log_dir: Path) -> None:
    _populate(log_dir, 1)
    with EventLog.open(log_dir, read_only=True) as log:
        with pytest.raises(EventLogError):
            log.append("system.started", "system")


# ---------------------------------------------------------------------------
# Export and rebuild


def test_export_matches_jsonl(log_dir: Path, tmp_path: Path) -> None:
    _populate(log_dir, 4)
    dest = tmp_path / "export.jsonl"
    with EventLog.open(log_dir, read_only=True) as log:
        assert log.export_jsonl(dest) == 4
    assert dest.read_bytes() == (log_dir / "events.jsonl").read_bytes()


def test_rebuild_database_from_jsonl(log_dir: Path, tmp_path: Path) -> None:
    events = _populate(log_dir, 5)
    jsonl = log_dir / "events.jsonl"
    rebuilt = tmp_path / "restored" / "events.sqlite3"
    report = rebuild_database_from_jsonl(jsonl, rebuilt)
    assert report.ok and report.head_hash == events[-1].hash
    with EventLog.open(rebuilt, jsonl_path=jsonl) as log:
        assert log.verify().ok
        assert list(log.iter_events()) == events
        assert log.append("system.restored", "system").prev_hash == events[-1].hash
    with pytest.raises(FileExistsError):
        rebuild_database_from_jsonl(jsonl, rebuilt)
    broken = tmp_path / "broken.jsonl"
    lines = _lines(log_dir)
    broken.write_bytes(lines[0] + lines[2])
    with pytest.raises(LogMismatchError):
        rebuild_database_from_jsonl(broken, tmp_path / "never.sqlite3")
    assert not (tmp_path / "never.sqlite3").exists()


# ---------------------------------------------------------------------------
# Command line


def test_cli_log_commands(log_dir: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    events = _populate(log_dir, 3)
    head = f"3 {events[-1].hash}"

    result = runner.invoke(app, ["log", "head", str(log_dir)])
    assert result.exit_code == 0 and result.stdout.strip() == head
    result = runner.invoke(app, ["log", "head", str(log_dir / "events.sqlite3")])
    assert result.exit_code == 0 and result.stdout.strip() == head

    result = runner.invoke(app, ["log", "verify", str(log_dir)])
    assert result.exit_code == 0 and result.stdout.startswith(f"OK: 3 events, head {head}")
    result = runner.invoke(app, ["log", "verify", str(log_dir / "events.jsonl")])
    assert result.exit_code == 0 and "OK: 3 events" in result.stdout

    dest = tmp_path / "out.jsonl"
    result = runner.invoke(app, ["log", "export", str(log_dir), str(dest)])
    assert result.exit_code == 0 and dest.read_bytes() == (log_dir / "events.jsonl").read_bytes()
    result = runner.invoke(app, ["log", "export", str(log_dir), str(dest)])
    assert result.exit_code == 2
    result = runner.invoke(app, ["log", "export", str(log_dir), str(dest), "--force"])
    assert result.exit_code == 0

    lines = _lines(log_dir)
    lines[1] = lines[1].replace(b'"observer:o0"', b'"observer:o5"')
    _write_lines(log_dir, lines)
    result = runner.invoke(app, ["log", "verify", str(log_dir)])
    assert result.exit_code == 1 and "FAILED" in result.stderr and "line 2" in result.stderr
    result = runner.invoke(app, ["log", "verify", str(log_dir / "events.jsonl")])
    assert result.exit_code == 1

    result = runner.invoke(app, ["log", "head", str(tmp_path / "absent")])
    assert result.exit_code == 2
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.stdout.startswith("corla-observer ")
