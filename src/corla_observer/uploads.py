"""Receive the files the project team gets from each county, and record what has arrived.

A county's coordinator uploads (`receive`, usually by way of
`corla_observer.intake.take`, which detects the kind and county from the
file's content):

``redacted_cvr``
    The county's redacted cast vote record, before the drawing. It is
    checked by `corla_observer.redaction.check_redaction` before anything
    is kept. On PASS it is stored. On FAIL its bytes are deleted, never
    stored in the workspace or backed up, and only its SHA-256, size and the
    counts-only report are recorded.
``manifest``
    The county's own copy of its ballot manifest, if available. It is
    classified by content and should be a ``county_manifest`` for the
    stated county that `corla_observer.sampling.load_county_manifest`
    loads; if not, a warning is recorded, and the file is stored anyway.
``pull_list``
    The county's pull list, CORLA's "List of ballots to audit", after the
    drawing. Its columns are not documented, so it is recognized by content
    (`corla_observer.classify` kind ``pull_list``: an imprinted ID column,
    or tabulator, batch and record columns, and an audit board column), and
    the evidence is recorded. An unrecognized file, or one without an audit
    board column, is stored with a warning.

`record_audit_boards` records how many audit boards a county will use, and
`status` summarizes, per county, the latest file of each kind, the audit
boards, rejections and warnings, and what is still missing.

Each upload is streamed into a staging directory, ``<workspace>/staging/``,
outside the content store (``<workspace>/files/``), and hashed and
size-limited on the way (`MAX_UPLOAD_BYTES`: 512 MB for a redacted CVR,
well above CORLA's largest recorded CVR upload, Denver's 2024 general
election CVR at 311 MB, and 306 MB in 2020). Staged bytes are always
deleted when `receive` returns: stored files live on in the content store
only. A file whose kind or county could not be detected waits for the
coordinator's choice in ``staging/pending/`` for at most two hours
(`corla_observer.intake.PendingStore`). Backups must copy ``files/`` and
the event log, never ``staging/``.

Events (``county`` set; ``file_sha256`` set wherever the hash is known;
``simulated`` in the data of a rehearsal's upload, as in `corla_observer.fetch`,
with the time the file was really received):

``file.received``
    A file accepted and stored: ``kind``, ``original_name``, ``sha256``,
    ``size``, and ``already_present`` if the store held it already.
``redaction.checked``
    The `RedactionReport` of a redacted CVR (``report``), with ``sha256``,
    whether it passed or failed.
``upload.rejected``
    An upload refused and not kept: ``kind``, ``original_name``,
    ``reason`` (``redaction_check_failed`` or ``too_large``), and
    ``sha256`` and ``size`` where known.
``file.classified``
    A manifest's or pull list's classification: ``kind`` (as uploaded),
    ``classification`` (``kind``, ``county``, ``evidence``) and, for a pull
    list, the rows and audit board values found.
``manifest.checked``
    Whether the manifest loads (``loads``), with its ``batches`` and
    ``ballot_cards``, or the ``error``.
``upload.warning``
    Something for the coordinator to look at, with a ``reason``
    (``classified_as_other_kind``, ``county_mismatch``,
    ``manifest_does_not_load``, ``pull_list_unrecognized``,
    ``pull_list_without_audit_board``) and ``detail``.
``audit_boards.recorded``
    The number of audit boards (``count``), with ``placeholder`` true for a
    stand-in count not yet known, and an optional ``note``.

Standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO

from corla_observer.classify import (
    Classification,
    classify_bytes,
    decode_text,
    known_county,
)
from corla_observer.events import Event, EventLog, JSONValue, check_actor
from corla_observer.redaction import (
    DEFAULT_THRESHOLD,
    CheckLimits,
    RedactionReport,
    check_redaction,
)
from corla_observer.sampling import load_county_manifest
from corla_observer.workspace import CHUNK_SIZE, Workspace

#: The kinds of file a coordinator uploads.
KINDS = ("redacted_cvr", "manifest", "pull_list")
#: Kinds `status` reports as missing until they arrive (the manifest is optional).
REQUIRED_KINDS = ("redacted_cvr", "pull_list")

#: Largest upload accepted per kind, in bytes.
MAX_UPLOAD_BYTES = {
    "redacted_cvr": 512 * 1024 * 1024,
    "manifest": 16 * 1024 * 1024,
    "pull_list": 16 * 1024 * 1024,
}
#: Most audit boards a county may record.
MAX_AUDIT_BOARDS = 100

STAGING_DIRNAME = "staging"

_MAX_NAME = 255
_MAX_BOARD_VALUES = 40


class UploadError(ValueError):
    """An upload request is invalid (unknown kind, county or actor); nothing was read or recorded."""


@dataclass(frozen=True)
class UploadResult:
    """What happened to one upload."""

    accepted: bool
    kind: str
    county: str
    original_name: str
    #: None if the upload was refused before it was completely read.
    sha256: str | None
    size: int
    #: Why it was refused, if it was.
    reason: str | None = None
    report: RedactionReport | None = None
    classification: Classification | None = None
    warnings: tuple[str, ...] = ()
    #: The seq of every event recorded for this upload.
    event_seqs: tuple[int, ...] = ()


def _county(county: str) -> str:
    canonical = known_county(county)
    if canonical is None:
        raise UploadError(f"not a Colorado county: {county!r}")
    return canonical


def _original_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip() or "(unnamed)"
    return cleaned[:_MAX_NAME]


class _Staged:
    """An upload copied into the staging directory, hashed and size-limited."""

    def __init__(self, workspace: Workspace) -> None:
        directory = workspace.root / STAGING_DIRNAME
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=directory, prefix="upload-")
        os.close(fd)
        self.path = Path(name)
        self.sha256: str | None = None
        self.size = 0

    def fill(self, source: BinaryIO, max_size: int) -> bool:
        """Copy ``source`` in; return False (keeping nothing) if it exceeds ``max_size``."""
        digest = hashlib.sha256()
        with open(self.path, "wb") as out:
            while chunk := source.read(CHUNK_SIZE):
                self.size += len(chunk)
                if self.size > max_size:
                    return False
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        self.sha256 = digest.hexdigest()
        return True

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def receive(
    workspace: Workspace,
    log: EventLog,
    county: str,
    kind: str,
    source_file: Path | str | BinaryIO,
    original_name: str,
    actor: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    limits: CheckLimits | None = None,
    max_bytes: int | None = None,
    simulated: Mapping[str, JSONValue] | None = None,
) -> UploadResult:
    """Receive one county file; see the module documentation.

    ``source_file`` is a path or a binary stream positioned at the start of
    the content. ``max_bytes`` overrides the kind's `MAX_UPLOAD_BYTES`;
    ``threshold`` and ``limits`` are passed to the redaction check.
    ``simulated``, for a rehearsal, says where the file really came from and
    when it was received (``time``); it is added to every event's data.

    Raises:
        UploadError: for an unknown kind, a county that is not a Colorado
            county, or an invalid actor, before anything is read.
    """
    if kind not in KINDS:
        raise UploadError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    county = _county(county)
    try:
        check_actor(actor)
    except ValueError as exc:
        raise UploadError(str(exc)) from None
    name = _original_name(original_name)
    limit = MAX_UPLOAD_BYTES[kind] if max_bytes is None else max_bytes
    staged = _Staged(workspace)
    try:
        if isinstance(source_file, (str, Path)):
            with open(source_file, "rb") as f:
                complete = staged.fill(f, limit)
        else:
            complete = staged.fill(source_file, limit)
        if not complete:
            staged.discard()
            event = _append(
                log,
                simulated,
                "upload.rejected",
                actor,
                county=county,
                data={
                    "kind": kind,
                    "original_name": name,
                    "reason": "too_large",
                    "size_limit": limit,
                    "sha256": None,
                    "size": None,
                },
            )
            return UploadResult(
                accepted=False,
                kind=kind,
                county=county,
                original_name=name,
                sha256=None,
                size=staged.size,
                reason="too_large",
                event_seqs=(event.seq,),
            )
        if kind == "redacted_cvr":
            return _receive_cvr(workspace, log, county, staged, name, actor, threshold, limits, simulated)
        return _receive_other(workspace, log, county, kind, staged, name, actor, simulated)
    finally:
        staged.discard()


def _append(
    log: EventLog, simulated: Mapping[str, JSONValue] | None, kind: str, actor: str, **fields: Any
) -> Event:
    if simulated is not None:
        fields["data"] = dict(fields.get("data") or {}) | {"simulated": dict(simulated)}
    return log.append(kind, actor, **fields)


def _store(
    workspace: Workspace,
    log: EventLog,
    county: str,
    kind: str,
    staged: _Staged,
    name: str,
    actor: str,
    simulated: Mapping[str, JSONValue] | None,
) -> tuple[int, str]:
    stored = workspace.put_file(staged.path)
    if stored.sha256 != staged.sha256:
        raise RuntimeError("the staged upload changed while it was being stored")
    data: dict[str, JSONValue] = {
        "kind": kind,
        "original_name": name,
        "sha256": stored.sha256,
        "size": stored.size,
    }
    if stored.already_present:
        data["already_present"] = True
    event = _append(log, simulated, "file.received", actor, county=county, file_sha256=stored.sha256, data=data)
    return event.seq, stored.sha256


def _receive_cvr(
    workspace: Workspace,
    log: EventLog,
    county: str,
    staged: _Staged,
    name: str,
    actor: str,
    threshold: int,
    limits: CheckLimits | None,
    simulated: Mapping[str, JSONValue] | None,
) -> UploadResult:
    assert staged.sha256 is not None
    sha256 = staged.sha256
    report = check_redaction(staged.path, threshold, limits=limits)
    if not report.passed:
        # Delete the bytes before recording anything: only the hash, the
        # size and the counts-only report remain.
        staged.discard()
        checked = _append(
            log,
            simulated,
            "redaction.checked",
            actor,
            county=county,
            file_sha256=sha256,
            data={"sha256": sha256, "verdict": report.verdict, "report": report.to_dict()},
        )
        rejected = _append(
            log,
            simulated,
            "upload.rejected",
            actor,
            county=county,
            file_sha256=sha256,
            data={
                "kind": "redacted_cvr",
                "original_name": name,
                "reason": "redaction_check_failed",
                "sha256": sha256,
                "size": staged.size,
            },
        )
        return UploadResult(
            accepted=False,
            kind="redacted_cvr",
            county=county,
            original_name=name,
            sha256=sha256,
            size=staged.size,
            reason="redaction_check_failed",
            report=report,
            event_seqs=(checked.seq, rejected.seq),
        )
    received, _ = _store(workspace, log, county, "redacted_cvr", staged, name, actor, simulated)
    checked = _append(
        log,
        simulated,
        "redaction.checked",
        actor,
        county=county,
        file_sha256=sha256,
        data={"sha256": sha256, "verdict": report.verdict, "report": report.to_dict()},
    )
    return UploadResult(
        accepted=True,
        kind="redacted_cvr",
        county=county,
        original_name=name,
        sha256=sha256,
        size=staged.size,
        report=report,
        warnings=report.warnings,
        event_seqs=(received, checked.seq),
    )


def inspect_pull_list(data: bytes, classification: Classification) -> dict[str, JSONValue]:
    """Rows and audit board values of a CSV pull list that `classify_bytes` recognized.

    Returns ``{}`` for anything else (a workbook, say), whose classification
    evidence stands alone.
    """
    evidence = classification.evidence
    columns = evidence.get("matched_columns")
    header_row = evidence.get("header_row")
    delimiter = evidence.get("delimiter")
    if (
        classification.kind != "pull_list"
        or evidence.get("format") != "csv"
        or not isinstance(header_row, int)
        or not isinstance(delimiter, str)
        or not isinstance(columns, dict)
    ):
        return {}
    text, _ = decode_text(data)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header: list[str] = []
    rows = 0
    boards: set[str] = set()
    board_index: int | None = None
    try:
        for number, row in enumerate(reader, start=1):
            if number < header_row:
                continue
            if number == header_row:
                header = [cell.strip() for cell in row]
                name = columns.get("audit_board")
                if isinstance(name, str) and name in header:
                    board_index = header.index(name)
                continue
            if not any(cell.strip() for cell in row):
                continue
            rows += 1
            if board_index is not None and board_index < len(row):
                value = row[board_index].strip()
                if value.startswith('="') and value.endswith('"'):
                    value = value[2:-1]
                if value:
                    boards.add(value)
    except csv.Error:
        pass
    result: dict[str, JSONValue] = {"rows": rows}
    if board_index is not None:
        values = sorted(boards, key=lambda v: (len(v), v))
        result["audit_board_count"] = len(values)
        result["audit_board_values"] = [v[:64] for v in values[:_MAX_BOARD_VALUES]]
    return result


def _receive_other(
    workspace: Workspace,
    log: EventLog,
    county: str,
    kind: str,
    staged: _Staged,
    name: str,
    actor: str,
    simulated: Mapping[str, JSONValue] | None,
) -> UploadResult:
    received, sha256 = _store(workspace, log, county, kind, staged, name, actor, simulated)
    seqs = [received]
    data = staged.path.read_bytes()
    classification = classify_bytes(data)
    warnings: list[str] = []

    def warn(reason: str, detail: str) -> None:
        warnings.append(f"{reason}: {detail}")
        event = _append(
            log,
            simulated,
            "upload.warning",
            actor,
            county=county,
            file_sha256=sha256,
            data={"kind": kind, "reason": reason, "detail": detail},
        )
        seqs.append(event.seq)

    classified: dict[str, JSONValue] = {"kind": kind, "classification": classification.to_dict()}
    pull_list: dict[str, JSONValue] = {}
    if kind == "pull_list":
        pull_list = inspect_pull_list(data, classification)
        classified["pull_list"] = pull_list
    seqs.append(
        _append(log, simulated, "file.classified", actor, county=county, file_sha256=sha256, data=classified).seq
    )

    if kind == "manifest":
        if classification.kind != "county_manifest":
            warn("classified_as_other_kind", f"the file looks like {classification.kind}, not a county manifest")
        elif classification.county is not None and classification.county != county:
            warn("county_mismatch", f"the manifest names {classification.county}, not {county}")
        elif classification.county is None:
            warn("county_mismatch", f"the manifest does not name a single county; expected {county}")
        check: dict[str, JSONValue]
        try:
            batches = load_county_manifest(staged.path)
            check = {
                "loads": True,
                "batches": len(batches),
                "ballot_cards": sum(batch.ballot_count for batch in batches),
            }
        except (ValueError, UnicodeDecodeError, csv.Error) as exc:
            check = {"loads": False, "error": str(exc).replace(str(staged.path), "manifest")[:500]}
            warn("manifest_does_not_load", str(check["error"]))
        seqs.append(
            _append(log, simulated, "manifest.checked", actor, county=county, file_sha256=sha256, data=check).seq
        )
    else:
        if classification.kind != "pull_list":
            warn(
                "pull_list_unrecognized",
                f"no imprinted ID or tabulator/batch/record columns with an audit board column were "
                f"found (classified as {classification.kind})",
            )
        else:
            matched = classification.evidence.get("matched_columns")
            if not (isinstance(matched, dict) and "audit_board" in matched):
                warn("pull_list_without_audit_board", "the list has no audit board column")
            if classification.county is not None and classification.county != county:
                warn("county_mismatch", f"the pull list names {classification.county}, not {county}")
    return UploadResult(
        accepted=True,
        kind=kind,
        county=county,
        original_name=name,
        sha256=sha256,
        size=staged.size,
        classification=classification,
        warnings=tuple(warnings),
        event_seqs=tuple(seqs),
    )


def record_audit_boards(
    log: EventLog, county: str, count: int, actor: str, *, placeholder: bool = False, note: str = ""
) -> int:
    """Record how many audit boards a county will use; return the event's seq.

    ``placeholder`` marks a count that is not known yet (a rehearsal's
    stand-in, say), and ``note`` says why or where a count came from; both
    are kept in the event and shown by `status`.

    Raises:
        UploadError: for a county that is not a Colorado county, an invalid
            actor, or a count outside 1 to `MAX_AUDIT_BOARDS`.
    """
    county = _county(county)
    try:
        check_actor(actor)
    except ValueError as exc:
        raise UploadError(str(exc)) from None
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_AUDIT_BOARDS:
        raise UploadError(f"the number of audit boards must be 1 to {MAX_AUDIT_BOARDS}, got {count!r}")
    data: dict[str, JSONValue] = {"count": count}
    if placeholder:
        data["placeholder"] = True
    if note:
        data["note"] = note.strip()[:500]
    return log.append("audit_boards.recorded", actor, county=county, data=data).seq


# ---------------------------------------------------------------------------
# Status


@dataclass(frozen=True)
class ReceivedFile:
    """The latest accepted file of one kind for a county."""

    kind: str
    sha256: str
    size: int
    original_name: str
    seq: int
    server_time: str
    actor: str
    #: For a redacted CVR, the verdict of its check ("PASS").
    verdict: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass
class CountyStatus:
    """What has arrived from one county."""

    county: str
    latest: dict[str, ReceivedFile] = field(default_factory=dict)
    audit_boards: int | None = None
    #: True if the audit board count is a placeholder, not yet known.
    audit_boards_placeholder: bool = False
    rejected: int = 0
    #: The latest rejection's ``upload.rejected`` data, with its seq.
    last_rejection: dict[str, JSONValue] | None = None

    @property
    def missing(self) -> list[str]:
        """The required kinds not yet received, and ``audit_boards`` if not yet recorded."""
        missing = [kind for kind in REQUIRED_KINDS if kind not in self.latest]
        if self.audit_boards is None:
            missing.append("audit_boards")
        return missing


def status(log: EventLog, counties: Iterable[str] = ()) -> dict[str, CountyStatus]:
    """Per county, sorted by name: the latest file of each kind, audit boards, rejections, what is missing.

    Covers every county with an upload event in the log, plus ``counties``
    (so that a county from which nothing has arrived is listed as missing
    everything).
    """
    result: dict[str, CountyStatus] = {}
    for county in counties:
        name = _county(county)
        result.setdefault(name, CountyStatus(name))
    verdicts: dict[str, str] = {}
    warnings: dict[tuple[str, str, str], list[str]] = {}
    for event in log.iter_events():
        if event.county is None:
            continue
        data = event.data
        if event.kind == "redaction.checked" and event.file_sha256:
            verdict = data.get("verdict")
            if isinstance(verdict, str):
                verdicts[event.file_sha256] = verdict
            continue
        if event.kind == "upload.warning" and event.file_sha256:
            key = (event.county, str(data.get("kind")), event.file_sha256)
            warnings.setdefault(key, []).append(f"{data.get('reason')}: {data.get('detail')}")
            continue
        if event.kind not in ("file.received", "upload.rejected", "audit_boards.recorded"):
            continue
        entry = result.setdefault(event.county, CountyStatus(event.county))
        if event.kind == "audit_boards.recorded":
            count = data.get("count")
            if isinstance(count, int):
                entry.audit_boards = count
                entry.audit_boards_placeholder = bool(data.get("placeholder"))
        elif event.kind == "upload.rejected":
            entry.rejected += 1
            entry.last_rejection = dict(data) | {"seq": event.seq}
        else:
            kind, sha256, size = data.get("kind"), data.get("sha256"), data.get("size")
            if isinstance(kind, str) and isinstance(sha256, str) and isinstance(size, int):
                # Warnings follow the arrival they concern.
                warnings[(event.county, kind, sha256)] = []
                entry.latest[kind] = ReceivedFile(
                    kind=kind,
                    sha256=sha256,
                    size=size,
                    original_name=str(data.get("original_name")),
                    seq=event.seq,
                    server_time=event.server_time,
                    actor=event.actor,
                )
    for entry in result.values():
        for kind, received in list(entry.latest.items()):
            entry.latest[kind] = replace(
                received,
                verdict=verdicts.get(received.sha256) if kind == "redacted_cvr" else None,
                warnings=tuple(warnings.get((entry.county, kind, received.sha256), ())),
            )
    return dict(sorted(result.items()))
