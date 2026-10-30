"""The append-only, hash-chained event log.

Every action the tool takes or records (an upload, a fetch, a check result, a
seed entry, an observer's tap) is an event, written before it takes effect
and never edited or deleted. Each event carries the SHA-256 of the previous
one, so any later alteration, deletion, insertion or reordering breaks the
chain and is detectable by anyone holding a copy of the log.

The format is specified in docs/EVENT_LOG.md, precisely enough to verify a log
independently in any language. In short: an event's ``hash`` is the SHA-256
of the canonical JSON of all its other fields (sorted keys, no whitespace,
UTF-8, integers only, absent optional fields as ``null``, and the format
version ``v``, currently 1); the first event's
``prev_hash`` is 64 zeros; and each line of the JSONL file is that canonical
JSON with ``,"hash":"<hash>"`` inserted before the final ``}``.

Storage is twofold:

- **JSONL file** (``events.jsonl``): one line per event, appended, flushed
  and fsynced *before* the database transaction commits. It is the
  write-ahead record and is enough on its own to reconstruct everything.
- **SQLite database** (``events.sqlite3``, WAL mode): an ``events`` table
  whose triggers abort any UPDATE or DELETE and any INSERT that does not
  extend the chain. Appends take the write lock with ``BEGIN IMMEDIATE``, so
  concurrent writers in threads or processes are serialized.

A crash between the JSONL write and the database commit leaves exactly one
event in the JSONL that the database lacks. Opening the log (or the next
append) recovers it into the database. Any other disagreement between the
two is an error and needs a person to look at it.

`verify_jsonl` checks a JSONL file alone, with nothing from this package but
the canonical serialization, for third parties.

Standard library only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TypeAlias

JSONValue: TypeAlias = "None | bool | int | str | list[JSONValue] | dict[str, JSONValue]"

#: The ``prev_hash`` of the first event.
GENESIS_PREV_HASH = "0" * 64

#: Every field of an event, in the order used for display. Canonical
#: serialization sorts keys, so this order does not affect hashes.
FIELDS = (
    "seq",
    "server_time",
    "client_time",
    "actor",
    "kind",
    "county",
    "board",
    "session_id",
    "device_id",
    "file_sha256",
    "signature",
    "data",
    "v",
    "prev_hash",
    "hash",
)

#: The event format version this module writes and accepts, carried in every
#: event's ``v`` field.
FORMAT_VERSION = 1

#: Integers anywhere in an event must lie within +-(2**53 - 1), the range
#: every common JSON implementation (including JavaScript's) represents exactly.
MAX_SAFE_INTEGER = 2**53 - 1

#: Upper bound on one event's canonical serialization, in bytes.
MAX_EVENT_BYTES = 64 * 1024

DB_FILENAME = "events.sqlite3"
JSONL_FILENAME = "events.jsonl"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_ACTOR_RE = re.compile(r"^(system|(coordinator|observer):[A-Za-z0-9._-]{1,64})$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BASE64_RE = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
_DATA_KEY_RE = re.compile(r"^[\x20-\x7e]{1,256}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_KIND_LENGTH = 64
_MAX_LABEL_LENGTH = 128
_MAX_SIGNATURE_LENGTH = 4096


class EventLogError(Exception):
    """Base class for event log errors."""


class InvalidEventError(EventLogError, ValueError):
    """An event, or a value offered for one, does not satisfy the format."""


class LogMismatchError(EventLogError):
    """The JSONL file and the database disagree in a way that can't be recovered automatically."""


# ---------------------------------------------------------------------------
# Canonical serialization


def canonical_json(obj: Mapping[str, Any]) -> bytes:
    """The canonical serialization of a JSON object, as specified in docs/EVENT_LOG.md.

    Keys sorted, no insignificant whitespace, non-ASCII characters written
    as themselves (not ``\\u`` escapes), encoded as UTF-8. NaN and infinities
    are refused here; floats are refused by event validation before this is
    called.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _jsonl_line(hash_input: bytes, event_hash: str) -> bytes:
    """The JSONL line (without its newline): the hash input with the hash added last."""
    return hash_input[:-1] + b',"hash":"' + event_hash.encode("ascii") + b'"}'


def format_server_time(moment: datetime) -> str:
    """Format a moment as UTC ISO 8601 with milliseconds, e.g. ``2026-11-16T15:30:00.123Z``.

    Sub-millisecond digits are truncated, not rounded.
    """
    if moment.tzinfo is None:
        raise ValueError("server time must be timezone-aware")
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Validation


def _check_text(name: str, value: object, max_length: int = _MAX_LABEL_LENGTH) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidEventError(f"{name} must be a non-empty string, got {value!r}")
    if len(value) > max_length:
        raise InvalidEventError(f"{name} is longer than {max_length} characters")
    if _CONTROL_RE.search(value):
        raise InvalidEventError(f"{name} contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidEventError(f"{name} is not valid Unicode text") from None
    return value


def _check_pattern(name: str, value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise InvalidEventError(f"{name} must be {what}, got {value!r}")
    return value


def check_actor(actor: object) -> str:
    """Return ``actor`` if it is a valid event actor; raise `InvalidEventError` otherwise.

    For callers that must refuse a bad actor before doing anything else.
    """
    return _check_pattern(
        "actor",
        actor,
        _ACTOR_RE,
        '"system", "coordinator:<id>" or "observer:<id>" (id: letters, digits, ._-)',
    )


def _check_time(name: str, value: object) -> str:
    _check_pattern(name, value, _TIME_RE, "UTC ISO 8601 with milliseconds (YYYY-MM-DDTHH:MM:SS.mmmZ)")
    assert isinstance(value, str)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise InvalidEventError(f"{name} is not a valid date and time: {value!r}") from None
    return value


def _check_int(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidEventError(f"{name} must be an integer, got {value!r}")
    if abs(value) > MAX_SAFE_INTEGER:
        raise InvalidEventError(f"{name} is outside +-(2**53 - 1): {value}")
    return value


def _copy_json(value: object, path: str) -> JSONValue:
    """Validate a JSON value for ``data`` and return a deep copy of it."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _check_int(path, value)
    if isinstance(value, float):
        raise InvalidEventError(f"{path} is a float ({value!r}); floats are not allowed in data")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidEventError(f"{path} is not valid Unicode text") from None
        return value
    if isinstance(value, (list, tuple)):
        return [_copy_json(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        copied: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _DATA_KEY_RE.match(key):
                raise InvalidEventError(
                    f"{path} has key {key!r}; keys must be 1 to 256 printable ASCII characters"
                )
            copied[key] = _copy_json(item, f"{path}.{key}")
        return copied
    raise InvalidEventError(f"{path} has a value of unsupported type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Events


@dataclass(frozen=True, eq=True)
class Event:
    """One event of the log. Construct with `Event.create` or `Event.from_dict`."""

    seq: int
    server_time: str
    actor: str
    kind: str
    prev_hash: str
    hash: str
    data: dict[str, JSONValue] = field(default_factory=dict)
    client_time: str | None = None
    county: str | None = None
    board: str | None = None
    session_id: str | None = None
    device_id: str | None = None
    file_sha256: str | None = None
    signature: str | None = None
    #: The event format version; always `FORMAT_VERSION`.
    v: int = FORMAT_VERSION

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        server_time: str,
        actor: str,
        kind: str,
        prev_hash: str,
        data: Mapping[str, Any] | None = None,
        client_time: str | None = None,
        county: str | None = None,
        board: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        file_sha256: str | None = None,
        signature: str | None = None,
        v: int = FORMAT_VERSION,
    ) -> Event:
        """Validate the fields, compute the hash and return the event."""
        fields: dict[str, Any] = {
            "seq": seq,
            "server_time": server_time,
            "client_time": client_time,
            "actor": actor,
            "kind": kind,
            "county": county,
            "board": board,
            "session_id": session_id,
            "device_id": device_id,
            "file_sha256": file_sha256,
            "signature": signature,
            "data": {} if data is None else data,
            "v": v,
            "prev_hash": prev_hash,
        }
        validated = _validate_fields(fields)
        hash_input = canonical_json(validated)
        if len(hash_input) > MAX_EVENT_BYTES:
            raise InvalidEventError(f"event is larger than {MAX_EVENT_BYTES} bytes")
        return cls(hash=hashlib.sha256(hash_input).hexdigest(), **validated)

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> Event:
        """Rebuild an event from its fields, including ``hash``, and check the hash.

        Raises:
            InvalidEventError: if a field is missing, unexpected or invalid,
                or ``hash`` does not match the other fields.
        """
        keys = set(obj)
        if keys != set(FIELDS):
            missing = sorted(set(FIELDS) - keys)
            extra = sorted(keys - set(FIELDS))
            raise InvalidEventError(f"wrong fields: missing {missing}, unexpected {extra}")
        claimed = _check_pattern("hash", obj["hash"], _HASH_RE, "64 lowercase hex digits")
        event = cls.create(**{k: obj[k] for k in FIELDS if k != "hash"})
        if event.hash != claimed:
            raise InvalidEventError(
                f"event {event.seq}: hash {claimed} does not match its content ({event.hash})"
            )
        return event

    @classmethod
    def from_line(cls, line: bytes) -> Event:
        """Parse one JSONL line (with or without its newline) and check it is canonical."""
        stripped = line[:-1] if line.endswith(b"\n") else line
        obj = parse_json_object(stripped)
        event = cls.from_dict(obj)
        if event.to_line() != stripped:
            raise InvalidEventError(f"event {event.seq}: line is not in canonical form")
        return event

    def fields_without_hash(self) -> dict[str, JSONValue]:
        """Every field but ``hash``: what the hash covers."""
        return {name: getattr(self, name) for name in FIELDS if name != "hash"}

    def hash_input(self) -> bytes:
        """The exact bytes whose SHA-256 is ``hash``."""
        return canonical_json(self.fields_without_hash())

    def to_line(self) -> bytes:
        """The event's JSONL line, without the trailing newline."""
        return _jsonl_line(self.hash_input(), self.hash)

    def to_dict(self) -> dict[str, JSONValue]:
        """Every field, including ``hash``."""
        result = self.fields_without_hash()
        result["hash"] = self.hash
        return result


def _validate_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Check every field except ``hash``; return them with ``data`` deep-copied."""
    seq = _check_int("seq", fields["seq"])
    if seq < 1:
        raise InvalidEventError(f"seq must be at least 1, got {seq}")
    out: dict[str, Any] = {"seq": seq}
    out["server_time"] = _check_time("server_time", fields["server_time"])
    out["client_time"] = (
        None if fields["client_time"] is None else _check_time("client_time", fields["client_time"])
    )
    out["actor"] = check_actor(fields["actor"])
    kind = _check_pattern(
        "kind", fields["kind"], _KIND_RE, 'a dotted lowercase name such as "file.received"'
    )
    if len(kind) > _MAX_KIND_LENGTH:
        raise InvalidEventError(f"kind is longer than {_MAX_KIND_LENGTH} characters")
    out["kind"] = kind
    for name in ("county", "board"):
        out[name] = None if fields[name] is None else _check_text(name, fields[name])
    for name in ("session_id", "device_id"):
        out[name] = (
            None
            if fields[name] is None
            else _check_pattern(name, fields[name], _ID_RE, "1 to 128 of A-Z a-z 0-9 . _ : -")
        )
    out["file_sha256"] = (
        None
        if fields["file_sha256"] is None
        else _check_pattern("file_sha256", fields["file_sha256"], _HASH_RE, "64 lowercase hex digits")
    )
    signature = fields["signature"]
    if signature is not None:
        _check_pattern("signature", signature, _BASE64_RE, "padded standard base64")
        if not signature or len(signature) > _MAX_SIGNATURE_LENGTH:
            raise InvalidEventError(f"signature must be 1 to {_MAX_SIGNATURE_LENGTH} characters")
    out["signature"] = signature
    data = fields["data"]
    if not isinstance(data, Mapping):
        raise InvalidEventError(f"data must be a JSON object, got {type(data).__name__}")
    out["data"] = _copy_json(data, "data")
    version = fields["v"]
    if not isinstance(version, int) or isinstance(version, bool) or version != FORMAT_VERSION:
        raise InvalidEventError(
            f"unsupported event format version v={version!r}; only {FORMAT_VERSION} is supported"
        )
    out["v"] = version
    out["prev_hash"] = _check_pattern("prev_hash", fields["prev_hash"], _HASH_RE, "64 lowercase hex digits")
    return out


def _reject_float(text: str) -> float:
    raise InvalidEventError(f"floats are not allowed: {text}")


def _reject_constant(text: str) -> float:
    raise InvalidEventError(f"{text} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEventError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_json_object(text: bytes) -> dict[str, Any]:
    """Parse strict JSON: UTF-8, an object at the top, no floats, NaN or duplicate keys."""
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidEventError("not valid UTF-8") from None
    try:
        obj = json.loads(
            decoded,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise InvalidEventError(f"not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise InvalidEventError("not a JSON object")
    return obj


# ---------------------------------------------------------------------------
# Verification


@dataclass(frozen=True)
class VerificationReport:
    """The result of checking a log."""

    #: Number of events checked and found good before any problem.
    event_count: int
    #: The last good event's seq and hash (0 and 64 zeros if none).
    head_seq: int
    head_hash: str
    #: Problems found, first problem first; empty if the log is intact.
    errors: tuple[str, ...] = ()
    #: The seq (where known) and 1-based JSONL line number of the first problem.
    first_bad_seq: int | None = None
    first_bad_line: int | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_jsonl(path: Path | str) -> VerificationReport:
    """Check a JSONL event log on its own, stopping at the first broken link.

    Needs nothing but the file: no database and nothing from this package
    beyond `canonical_json`. For each line it checks that the line is a JSON
    object with exactly the event fields and format version ``v`` 1, that
    ``hash`` is the SHA-256 of the
    canonical serialization of the other fields, that the line itself is in
    canonical form, that ``seq`` counts up from 1, and that ``prev_hash`` is
    the previous event's ``hash`` (64 zeros for the first).
    """
    expected_keys = set(FIELDS)
    prev_hash = GENESIS_PREV_HASH
    count = 0
    line_number = 0
    seq: int | None = None

    def fail(message: str) -> VerificationReport:
        return VerificationReport(
            event_count=count,
            head_seq=count,
            head_hash=prev_hash,
            errors=(f"line {line_number}: {message}",),
            first_bad_seq=seq,
            first_bad_line=line_number,
        )

    with open(path, "rb") as f:
        for raw in f:
            line_number += 1
            seq = count + 1
            if not raw.endswith(b"\n"):
                return fail("incomplete last line (no newline)")
            try:
                obj = parse_json_object(raw[:-1])
            except InvalidEventError as exc:
                return fail(str(exc))
            if "v" not in obj:
                return fail("no format version field v")
            if obj["v"] != FORMAT_VERSION or isinstance(obj["v"], bool):
                return fail(
                    f"unsupported format version v={obj['v']!r}; this verifier knows only "
                    f"version {FORMAT_VERSION}"
                )
            if set(obj) != expected_keys:
                return fail(f"fields are {sorted(obj)}, expected {sorted(expected_keys)}")
            if isinstance(obj["seq"], int) and not isinstance(obj["seq"], bool):
                seq = obj["seq"]
            claimed = obj["hash"]
            if not isinstance(claimed, str) or not _HASH_RE.match(claimed):
                return fail("hash is not 64 lowercase hex digits")
            body = {k: v for k, v in obj.items() if k != "hash"}
            hash_input = canonical_json(body)
            if raw[:-1] != _jsonl_line(hash_input, claimed):
                return fail("line is not in canonical form")
            if hashlib.sha256(hash_input).hexdigest() != claimed:
                return fail(f"hash does not match the event's content (event {obj['seq']!r})")
            if obj["seq"] != count + 1:
                return fail(f"seq is {obj['seq']!r}, expected {count + 1}")
            if obj["prev_hash"] != prev_hash:
                return fail(f"prev_hash does not match the hash of event {count}")
            count += 1
            prev_hash = claimed
    return VerificationReport(event_count=count, head_seq=count, head_hash=prev_hash)


# ---------------------------------------------------------------------------
# Storage

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    server_time TEXT NOT NULL,
    client_time TEXT,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    county TEXT,
    board TEXT,
    session_id TEXT,
    device_id TEXT,
    file_sha256 TEXT,
    signature TEXT,
    data TEXT NOT NULL,
    v INTEGER NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    -- Byte offset in the JSONL file just past this event's newline.
    jsonl_end INTEGER NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'the event log is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'the event log is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS events_extend_chain BEFORE INSERT ON events
WHEN NEW.seq IS NOT (SELECT COALESCE(MAX(seq), 0) + 1 FROM events)
  OR NEW.prev_hash IS NOT COALESCE(
        (SELECT hash FROM events ORDER BY seq DESC LIMIT 1), '{GENESIS_PREV_HASH}')
BEGIN
    SELECT RAISE(ABORT, 'an inserted event must extend the chain');
END;
"""

_TRIGGERS = ("events_no_update", "events_no_delete", "events_extend_chain")
_COLUMNS = tuple(FIELDS) + ("jsonl_end",)
_HASH_COLUMN = _COLUMNS.index("hash")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM events"


def _row_to_event(row: tuple[Any, ...]) -> Event:
    """Rebuild an event from a database row exactly as stored, without checking its hash."""
    values = dict(zip(_COLUMNS, row, strict=True))
    data = json.loads(values["data"])
    return Event(
        seq=values["seq"],
        server_time=values["server_time"],
        actor=values["actor"],
        kind=values["kind"],
        prev_hash=values["prev_hash"],
        hash=values["hash"],
        data=data,
        client_time=values["client_time"],
        county=values["county"],
        board=values["board"],
        session_id=values["session_id"],
        device_id=values["device_id"],
        file_sha256=values["file_sha256"],
        signature=values["signature"],
        v=values["v"],
    )


def _event_row(event: Event, jsonl_end: int) -> tuple[Any, ...]:
    values = event.to_dict()
    values["data"] = canonical_json(event.data).decode("utf-8")
    return tuple(values[name] for name in FIELDS) + (jsonl_end,)


_INSERT = f"INSERT INTO events ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    # FULL: every commit is durable, so after a crash at most the one event
    # written to the JSONL but not yet committed can be missing.
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve_paths(path: Path | str, jsonl_path: Path | str | None = None) -> tuple[Path, Path]:
    """Return ``(database, jsonl)`` paths for a log given as a directory or a database file.

    A path ending ``.sqlite3`` is the database file, which pairs with the
    file of the same name ending ``.jsonl`` unless ``jsonl_path`` says
    otherwise. Any other path is a log directory holding ``events.sqlite3``
    and ``events.jsonl``.
    """
    path = Path(path)
    db_path = path if path.suffix == ".sqlite3" else path / DB_FILENAME
    return db_path, Path(jsonl_path) if jsonl_path is not None else db_path.with_suffix(".jsonl")


class EventLog:
    """An open event log. Safe to share between threads; each process opens its own."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        db_path: Path,
        jsonl_path: Path,
        clock: Callable[[], datetime],
        read_only: bool = False,
    ) -> None:
        self._conn = conn
        self.read_only = read_only
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self._clock = clock
        self._lock = threading.RLock()
        #: Events recovered from the JSONL into the database by this instance.
        self.recovered: list[Event] = []

    @classmethod
    def open(
        cls,
        path: Path | str,
        jsonl_path: Path | str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        create: bool = True,
        read_only: bool = False,
    ) -> EventLog:
        """Open (and by default create) the log at ``path``; see `resolve_paths`.

        Checks that the JSONL and the database agree: the JSONL must be
        exactly as long as the database records, and the database's last
        event must be the JSONL's last line. One trailing JSONL event missing
        from the database (a crash between the two writes) is recovered into
        the database and listed in `recovered`. A full check of every event
        is `verify`.

        ``read_only`` opens an existing log for reading, verifying and
        exporting only: nothing is created, recovered or appended, and any
        disagreement is left for `verify` to report. Verifying a copy of
        someone else's log should use it.

        ``clock`` returns the current time for ``server_time`` (UTC now by
        default), for tests.

        Raises:
            LogMismatchError: if the JSONL and database disagree otherwise.
            FileNotFoundError: if ``create`` is false, or ``read_only`` is
                true, and there is no database.
        """
        db_path, jsonl = resolve_paths(path, jsonl_path)
        if (read_only or not create) and not db_path.exists():
            raise FileNotFoundError(f"no event log database at {db_path}")
        clock = clock or _utc_now
        if read_only:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            ro_conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
            return cls(ro_conn, db_path, jsonl, clock, read_only=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db_path)
        log = cls(conn, db_path, jsonl, clock)
        try:
            with log._lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_schema(_SCHEMA):
                        conn.execute(statement)
                    if not jsonl.exists() and log._last_row() is None:
                        jsonl.touch()
                        _fsync_dir(jsonl.parent)
                    log._reconcile(check_last_line=True)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except BaseException:
            conn.close()
            raise
        return log

    # -- context manager -----------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- reconciliation ------------------------------------------------------

    def _last_row(self) -> tuple[Any, ...] | None:
        row: tuple[Any, ...] | None = self._conn.execute(
            f"{_SELECT} ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row

    def _reconcile(self, check_last_line: bool) -> int:
        """Bring the database level with the JSONL; return the JSONL's length.

        Must be called holding the database write lock (inside BEGIN IMMEDIATE).
        """
        last = self._last_row()
        expected_end = 0 if last is None else int(last[-1])
        try:
            size = self.jsonl_path.stat().st_size
        except FileNotFoundError:
            raise LogMismatchError(f"the JSONL file {self.jsonl_path} is missing") from None
        if size < expected_end:
            raise LogMismatchError(
                f"the JSONL file is {size} bytes, shorter than the {expected_end} bytes "
                "the database records"
            )
        if last is not None and check_last_line:
            line = _row_to_event(last).to_line() + b"\n"
            on_disk = b""
            if expected_end >= len(line):
                with open(self.jsonl_path, "rb") as f:
                    f.seek(expected_end - len(line))
                    on_disk = f.read(len(line))
            if on_disk != line:
                raise LogMismatchError(
                    f"the JSONL file's line for event {last[0]} differs from the database"
                )
        if size == expected_end:
            return size
        with open(self.jsonl_path, "rb") as f:
            f.seek(expected_end)
            tail = f.read(size - expected_end)
        if not tail.endswith(b"\n") or tail.count(b"\n") != 1:
            raise LogMismatchError(
                f"the JSONL file has {size - expected_end} bytes after the database's last event "
                "that are not exactly one complete line"
            )
        try:
            event = Event.from_line(tail)
        except InvalidEventError as exc:
            raise LogMismatchError(f"the trailing JSONL line is not a valid event: {exc}") from None
        head_seq, head_hash = (0, GENESIS_PREV_HASH) if last is None else (last[0], last[_HASH_COLUMN])
        if event.seq != head_seq + 1 or event.prev_hash != head_hash:
            raise LogMismatchError(
                f"the trailing JSONL event (seq {event.seq}) does not extend the database's "
                f"chain (head seq {head_seq})"
            )
        self._conn.execute(_INSERT, _event_row(event, size))
        self.recovered.append(event)
        return size

    # -- appending -----------------------------------------------------------

    def append(
        self,
        kind: str,
        actor: str,
        *,
        data: Mapping[str, Any] | None = None,
        client_time: str | None = None,
        county: str | None = None,
        board: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        file_sha256: str | None = None,
        signature: str | None = None,
    ) -> Event:
        """Append an event and return it once it is durable in both the JSONL and the database.

        Raises:
            InvalidEventError: if a field is invalid; nothing is written.
        """
        if self.read_only:
            raise EventLogError("this event log was opened read-only")
        fields: dict[str, Any] = {
            "client_time": client_time,
            "actor": actor,
            "kind": kind,
            "county": county,
            "board": board,
            "session_id": session_id,
            "device_id": device_id,
            "file_sha256": file_sha256,
            "signature": signature,
            "data": {} if data is None else data,
        }
        # Validate before taking the write lock, so a bad request touches nothing.
        _validate_fields(
            fields
            | {
                "seq": 1,
                "server_time": "2000-01-01T00:00:00.000Z",
                "v": FORMAT_VERSION,
                "prev_hash": GENESIS_PREV_HASH,
            }
        )
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                size = self._reconcile(check_last_line=False)
                last = self._last_row()
                head_seq, head_hash = (0, GENESIS_PREV_HASH) if last is None else (last[0], last[_HASH_COLUMN])
                event = Event.create(
                    seq=head_seq + 1,
                    server_time=format_server_time(self._clock()),
                    prev_hash=head_hash,
                    **fields,
                )
                line = event.to_line() + b"\n"
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            try:
                with open(self.jsonl_path, "ab") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                conn.execute(_INSERT, _event_row(event, size + len(line)))
                conn.execute("COMMIT")
            except BaseException:
                # Undo the JSONL write while we still hold the write lock, so
                # no other writer can see (and recover) an event that the
                # caller is told failed. If the lock was lost with the
                # transaction, leave the line: it is then a complete,
                # recoverable event, recovered by the next open or append.
                if conn.in_transaction:
                    os.truncate(self.jsonl_path, size)
                    conn.execute("ROLLBACK")
                raise
            return event

    # -- reading -------------------------------------------------------------

    def head(self) -> tuple[int, str]:
        """The last event's ``(seq, hash)``, or ``(0, 64 zeros)`` for an empty log."""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return (0, GENESIS_PREV_HASH) if row is None else (int(row[0]), str(row[1]))

    def __len__(self) -> int:
        return self.head()[0]

    def iter_events(self, after_seq: int = 0, page_size: int = 1000) -> Iterator[Event]:
        """Yield events with seq greater than ``after_seq`` in order, as stored in the database."""
        cursor_seq = after_seq
        while True:
            with self._lock:
                rows = self._conn.execute(
                    f"{_SELECT} WHERE seq > ? ORDER BY seq LIMIT ?", (cursor_seq, page_size)
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield _row_to_event(row)
            cursor_seq = rows[-1][0]

    # -- verification and export ---------------------------------------------

    def verify(self) -> VerificationReport:
        """Check every event's hash and link, in the database and the JSONL, and that they agree.

        Also reports if the append-only triggers have been removed.
        """
        errors: list[str] = []
        first_bad_seq: int | None = None
        first_bad_line: int | None = None

        def note(message: str, seq: int | None, line: int | None = None) -> None:
            nonlocal first_bad_seq, first_bad_line
            errors.append(message)
            if seq is not None and (first_bad_seq is None or seq < first_bad_seq):
                first_bad_seq = seq
            if line is not None and (first_bad_line is None or line < first_bad_line):
                first_bad_line = line

        with self._lock:
            triggers = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'events'"
                )
            }
        missing = [name for name in _TRIGGERS if name not in triggers]
        if missing:
            note(f"database: append-only triggers missing: {', '.join(missing)}", None)

        jsonl_exists = self.jsonl_path.is_file()
        if jsonl_exists:
            jsonl_report = verify_jsonl(self.jsonl_path)
        else:
            jsonl_report = VerificationReport(0, 0, GENESIS_PREV_HASH)
            note(f"the JSONL file {self.jsonl_path} is missing", None)
        for message in jsonl_report.errors:
            note(f"JSONL {message}", jsonl_report.first_bad_seq, jsonl_report.first_bad_line)

        # The database's own chain, and line-by-line agreement with the JSONL.
        db_count = 0
        db_head = GENESIS_PREV_HASH
        db_ok = True
        previous_end = 0
        with open(self.jsonl_path, "rb") if jsonl_exists else io.BytesIO() as jsonl:
            for line_number, row in enumerate(self._iter_rows(), start=1):
                stored = _row_to_event(row)
                try:
                    event = Event.from_dict(stored.to_dict())
                    if event != stored:
                        raise InvalidEventError(f"event {stored.seq}: stored values are not canonical")
                except InvalidEventError as exc:
                    note(f"database: {exc}", stored.seq)
                    db_ok = False
                    break
                if event.seq != db_count + 1:
                    note(f"database: seq {event.seq} follows {db_count}", db_count + 1)
                    db_ok = False
                    break
                if event.prev_hash != db_head:
                    note(f"database: event {event.seq} prev_hash does not match event {db_count}", event.seq)
                    db_ok = False
                    break
                line = event.to_line() + b"\n"
                jsonl_line = jsonl.readline()
                if jsonl_line != line or row[-1] != previous_end + len(line):
                    note(
                        f"database event {event.seq} differs from JSONL line {line_number}",
                        event.seq,
                        line_number,
                    )
                    db_ok = False
                    break
                previous_end += len(line)
                db_count += 1
                db_head = event.hash
            if db_ok and jsonl.readline():
                note(
                    f"JSONL has events after the database's last event ({db_count})",
                    db_count + 1,
                    db_count + 1,
                )
        good = min(db_count, jsonl_report.event_count)
        head_hash = db_head if good == db_count else jsonl_report.head_hash
        return VerificationReport(
            event_count=good,
            head_seq=good,
            head_hash=head_hash,
            errors=tuple(errors),
            first_bad_seq=first_bad_seq,
            first_bad_line=first_bad_line,
        )

    def _iter_rows(self) -> Iterator[tuple[Any, ...]]:
        after = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    f"{_SELECT} WHERE seq > ? ORDER BY seq LIMIT 1000", (after,)
                ).fetchall()
            if not rows:
                return
            yield from rows
            after = rows[-1][0]

    def export_jsonl(self, dest: Path | str) -> int:
        """Write the database's events to ``dest`` as JSONL; return the number written.

        The file is written beside ``dest``, fsynced and renamed into place.
        For an intact log the result is byte-for-byte the log's own JSONL file.
        """
        dest = Path(dest)
        tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
        count = 0
        try:
            with open(tmp, "wb") as f:
                for event in self.iter_events():
                    f.write(event.to_line() + b"\n")
                    count += 1
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(dest.parent)
        return count


def _split_schema(script: str) -> list[str]:
    """Split the schema into statements (triggers contain semicolons)."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.strip().splitlines():
        if line.lstrip().startswith("--"):
            continue
        current.append(line)
        text = "\n".join(current).strip()
        if text and sqlite3.complete_statement(text):
            statements.append(text)
            current = []
    return statements


def rebuild_database_from_jsonl(jsonl_path: Path | str, db_path: Path | str) -> VerificationReport:
    """Build a new event database from a JSONL file, for recovery.

    The JSONL must verify completely (see `verify_jsonl`); ``db_path`` must
    not exist. Open the result with ``EventLog.open(db_path, jsonl_path)``,
    or copy the JSONL beside it under the paired name.

    Raises:
        LogMismatchError: if the JSONL does not verify.
        FileExistsError: if ``db_path`` exists.
    """
    jsonl_path = Path(jsonl_path)
    db_path = Path(db_path)
    if db_path.exists():
        raise FileExistsError(f"refusing to overwrite {db_path}")
    report = verify_jsonl(jsonl_path)
    if not report.ok:
        raise LogMismatchError(f"the JSONL does not verify: {report.errors[0]}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_schema(_SCHEMA):
                conn.execute(statement)
            end = 0
            with open(jsonl_path, "rb") as f:
                for raw in f:
                    event = Event.from_line(raw)
                    end += len(raw)
                    conn.execute(_INSERT, _event_row(event, end))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return report
