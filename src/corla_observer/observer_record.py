"""Checking an observer's device-held record against a published event log.

``corla-observer check-record RECORD.json EVENTS.jsonl [--server-key KEY]``
runs `check_record`, specified with the record's format in docs/RECEIPTS.md.
It needs only the record the observer downloaded from their own device, a
copy of the published ``events.jsonl``, and, for an independent check of the
receipts, the server's receipt key obtained from somewhere other than that
log. It reports:

- **the log's chain** (`corla_observer.events.verify_jsonl`), and whether the
  log agrees with every anchor: each receipt's event and chain head, and any
  anchors given (``--anchor SEQ:HASH``, such as a head published with an
  evidence commitment);
- **the record itself:** every payload's hash and device signature, and the
  device's chain (sequence numbers 0, 1, 2, ... each carrying the hash of
  the payload before it);
- **receipts:** whether each is signed by the server key, names this
  payload, and points to an event in the log that holds it;
- **entries missing from the log or differing from it:** a payload of the
  record that no event holds, an event with the same sequence number but a
  different payload, or an event whose plain fields (position, match, note
  and so on, which the dashboard counts) disagree with the signed payload;
- **entries in the log under this observer and device that the device did
  not make:** events without a signature that verifies with the device's
  key. Events the device did sign that the record lacks (a record
  downloaded before them) are noted, not counted as problems.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.contests import PAYLOAD_KINDS, plain_fields
from corla_observer.events import VerificationReport, verify_jsonl
from corla_observer.receipts import (
    ZERO_HASH,
    PayloadError,
    key_id,
    payload_sha256,
    receipt_code,
    verify_device_signature,
    verify_receipt,
)
from corla_observer.signed import KEY_KIND, REGISTERED_KIND, SIGNED_KINDS

RECORD_FORMAT = "corla-observer-record"
RECORD_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024 * 1024


class RecordError(ValueError):
    """The record file can't be read as a device-held record."""


@dataclass(frozen=True)
class Finding:
    """One finding: a ``problem`` or a ``note``, of a ``kind`` such as ``missing`` or ``device_chain``."""

    severity: str
    kind: str
    message: str
    device_seq: int | None = None


@dataclass
class RecordReport:
    observer: str | None
    device_id: str | None
    entries: int
    receipts: int
    log: VerificationReport
    server_key: str
    anchors_checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    #: The record's entries per pull list (`corla_observer.pulllists`), by the extract each refers to: "RLA sample" or
    #: "Targeted: NAME", as the log's ``extract.built`` events name them, each with its extracts' short hashes.
    lists: dict[str, int] = field(default_factory=dict)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "problem"]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "observer": self.observer,
            "device_id": self.device_id,
            "entries": self.entries,
            "receipts": self.receipts,
            "log": {
                "ok": self.log.ok,
                "events": self.log.event_count,
                "head_seq": self.log.head_seq,
                "head_hash": self.log.head_hash,
                "errors": list(self.log.errors),
            },
            "server_key": self.server_key,
            "anchors_checked": self.anchors_checked,
            "lists": dict(self.lists),
            "findings": [f.__dict__ for f in self.findings],
        }


def load_record(path: Path | str) -> dict[str, Any]:
    """Read a downloaded record (JSON).

    Raises:
        RecordError: if it isn't a record of a known format.
    """
    path = Path(path)
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise RecordError(f"{path} is larger than {MAX_RECORD_BYTES} bytes")
    try:
        record = json.loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecordError(f"{path} is not JSON: {exc}") from None
    if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
        raise RecordError(f"{path} is not a {RECORD_FORMAT} file")
    if record.get("version") != RECORD_VERSION:
        raise RecordError(f"{path} is record version {record.get('version')!r}; this checker knows version {RECORD_VERSION}")
    if not isinstance(record.get("entries"), list):
        raise RecordError(f"{path} has no list of entries")
    return record


def _read_events(path: Path | str, count: int) -> list[dict[str, Any]]:
    """The first ``count`` events of a JSONL log (the ones that verified)."""
    events: list[dict[str, Any]] = []
    with open(path, "rb") as f:
        for line in f:
            if len(events) >= count:
                break
            events.append(json.loads(line))
    return events


def _normalized_note(note: str) -> str:
    """The note as the server records it in the event's plain ``note`` field (`corla_observer.practice`)."""
    return "".join(ch for ch in note if ch.isprintable()).strip()[:500]


def _plain_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The event's plain ``data`` fields that a payload determines."""
    content = payload.get("content") or {}
    if payload.get("type") != "device_registration":
        return _entry_fields(payload, content) | {"extract_sha256": payload.get("extract_sha256")}
    return {"public_key": payload.get("public_key"), "device_seq": 0}


def _entry_fields(payload: Mapping[str, Any], content: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("type") == "entry":
        return {
            "position": payload.get("position"),
            "imprinted_id": payload.get("imprinted_id"),
            "match": content.get("match"),
            "note": _normalized_note(str(content.get("note", ""))),
            "edit": content.get("edit"),
            "elapsed_ms": content.get("elapsed_ms"),
            "device_seq": payload.get("device_seq"),
        }
    if payload.get("type") in ("first_reading", "other_board"):
        return {
            "position": payload.get("position"),
            "imprinted_id": payload.get("imprinted_id"),
            "elapsed_ms": content.get("elapsed_ms"),
            "device_seq": payload.get("device_seq"),
        }
    if payload.get("type") in PAYLOAD_KINDS:
        return {
            "position": payload.get("position"),
            "imprinted_id": payload.get("imprinted_id"),
            "device_seq": payload.get("device_seq"),
        } | plain_fields(payload)
    if payload.get("type") == "heard_id":
        return {
            "expected_position": payload.get("position"),
            "expected_imprinted_id": payload.get("imprinted_id"),
            "heard": _normalized_note(str(content.get("heard", ""))),
            "elapsed_ms": content.get("elapsed_ms"),
            "device_seq": payload.get("device_seq"),
        }
    return {}


def _describe(payload: Mapping[str, Any]) -> str:
    kind = payload.get("type")
    seq = payload.get("device_seq")
    if kind == "device_registration":
        return f"entry {seq} (the device's registration)"
    content = payload.get("content") or {}
    if kind == "heard_id":
        what = "heard ID " + repr(content.get("heard"))
    elif kind == "first_reading":
        what = "first reading"
    elif kind == "other_board":
        what = "skipped: other board"
    elif kind in ("contest", "lookup_contest"):
        outcome = content.get("outcome")
        heard = f" {content.get('heard')!r}" if outcome == "heard_instead" else ""
        looked = "looked-up ballot, " if kind == "lookup_contest" else ""
        what = f"{looked}reading {content.get('reading')}, contest {content.get('row')} {content.get('contest')!r}: {outcome}{heard}"
    elif kind in ("ballot_done", "lookup_done"):
        looked = "looked-up " if kind == "lookup_done" else ""
        what = f"{looked}ballot done after reading {content.get('reading')}, {content.get('unresolved')} unresolved"
    elif kind == "entry_mode":
        what = f"entry mode {content.get('mode')}"
    elif kind == "acknowledged":
        what = f"acknowledged the notice {content.get('notice')}"
    elif kind == "list_switch":
        what = f"switched to the pull list {content.get('name')!r}"
    else:
        what = "note " + repr(content.get("note")) if content.get("note") else "match"
    return f"entry {seq} (position {payload.get('position')}, {payload.get('imprinted_id')}: {what})"


def check_record(
    record: Mapping[str, Any],
    events_path: Path | str,
    *,
    server_key: bytes | None = None,
    anchors: Sequence[tuple[int, str]] = (),
) -> RecordReport:
    """Check a device-held record against an event log; see the module documentation."""
    log_report = verify_jsonl(events_path)
    events = _read_events(events_path, log_report.event_count)
    by_seq = {int(e["seq"]): e for e in events}
    entries = [e for e in record.get("entries", []) if isinstance(e, Mapping)]
    observer = record.get("observer") if isinstance(record.get("observer"), str) else None
    device = record.get("device_id") if isinstance(record.get("device_id"), str) else None
    findings: list[Finding] = []

    def problem(kind: str, message: str, seq: int | None = None) -> None:
        findings.append(Finding("problem", kind, message, seq))

    def note(kind: str, message: str, seq: int | None = None) -> None:
        findings.append(Finding("note", kind, message, seq))

    if not log_report.ok:
        problem("log_broken", f"the log's chain is broken: {log_report.errors[0]}; only the {log_report.event_count} events before it are used")

    # -- the server key --------------------------------------------------
    logged_keys: dict[str, bytes] = {}
    for e in events:
        if e["kind"] == KEY_KIND and e["actor"] == "system":
            try:
                logged_keys[str(e["data"].get("key_id"))] = base64.b64decode(str(e["data"].get("public_key")), validate=True)
            except (binascii.Error, ValueError):
                pass
    if server_key is not None:
        key_source = f"{key_id(server_key)} (given)"
        if logged_keys and key_id(server_key) not in logged_keys:
            problem("server_key", "the server key given was never published in the log")
    else:
        key_source = "not given"

    # -- the record itself -------------------------------------------------
    payloads: list[tuple[Mapping[str, Any], str, Mapping[str, Any]]] = []
    for entry in entries:
        payload = entry.get("payload")
        if not isinstance(payload, Mapping):
            problem("record_corrupt", "an entry of the record has no payload")
            continue
        try:
            sha = payload_sha256(payload)
        except PayloadError:
            problem("record_corrupt", "an entry's payload is not plain JSON")
            continue
        if entry.get("payload_sha256") not in (None, sha):
            problem("record_corrupt", f"{_describe(payload)}: its recorded hash is not its payload's hash", payload.get("device_seq"))
        payloads.append((payload, sha, entry))
    payloads.sort(key=lambda p: int(p[0]["device_seq"]) if isinstance(p[0].get("device_seq"), int) else -1)

    registration = next((p for p in payloads if p[0].get("type") == "device_registration"), None)
    public_key: str | None = None
    if registration is None:
        problem("no_registration", "the record has no device registration, so its signatures can't be checked")
    else:
        public_key = str(registration[0].get("public_key"))
        device = device or str(registration[0].get("device_id"))
        observer = observer or str(registration[0].get("observer"))

    prev, expected_seq = ZERO_HASH, 0
    for payload, sha, entry in payloads:
        seq = payload.get("device_seq")
        seq_int = seq if isinstance(seq, int) else None
        if payload.get("device_id") != device or payload.get("observer") != observer:
            problem("record_corrupt", f"{_describe(payload)} names another observer or device", seq_int)
        if public_key is not None and not verify_device_signature(public_key, payload, entry.get("signature")):
            problem("bad_device_signature", f"{_describe(payload)}: its signature in the record does not verify with the device's key", seq_int)
        if seq != expected_seq:
            problem("device_chain", f"the device's chain skips from sequence number {expected_seq - 1} to {seq}", seq_int)
        elif payload.get("prev_payload_sha256") != prev:
            problem("device_chain", f"{_describe(payload)} does not carry the hash of the device's previous entry", seq_int)
        prev, expected_seq = sha, (seq_int if seq_int is not None else expected_seq) + 1

    # -- the log's events under this observer and device -------------------
    logged = [e for e in events if e["kind"] in SIGNED_KINDS and e["actor"] == observer and e["device_id"] == device]
    rejected = [e for e in events if e["kind"] == "observation.rejected" and e["actor"] == observer]
    logged_by_sha: dict[str, dict[str, Any]] = {}
    logged_by_seq: dict[int, list[dict[str, Any]]] = {}
    for e in logged:
        sha_value = e["data"].get("payload_sha256")
        if isinstance(sha_value, str):
            logged_by_sha.setdefault(sha_value, e)
        seq_value = e["data"].get("device_seq")
        if isinstance(seq_value, int):
            logged_by_seq.setdefault(seq_value, []).append(e)

    record_hashes = {sha for _, sha, _ in payloads}
    receipts = 0
    receipt_anchors: list[tuple[int, str, str]] = []
    for payload, sha, entry in payloads:
        seq = payload.get("device_seq")
        seq_int = seq if isinstance(seq, int) else None
        event = logged_by_sha.get(sha)
        if event is None:
            others = logged_by_seq.get(seq_int or -1, [])
            refusal = next((r for r in rejected if r["data"].get("payload_sha256") == sha), None)
            if others:
                problem("differs", f"{_describe(payload)}: the log holds a different entry with this sequence number (event {others[0]['seq']})", seq_int)
            elif refusal is not None:
                problem("refused", f"{_describe(payload)}: the server refused it (event {refusal['seq']}: {refusal['data'].get('reason')})", seq_int)
            elif entry.get("receipt"):
                problem("missing", f"{_describe(payload)}: not in the log, although the device holds a receipt for it", seq_int)
            else:
                problem("missing", f"{_describe(payload)}: not in the log, and the device holds no receipt (perhaps never sent)", seq_int)
        else:
            if event.get("signature") != entry.get("signature") and public_key is not None and not verify_device_signature(
                public_key, event["data"].get("payload", {}), event.get("signature")
            ):
                problem("differs", f"{_describe(payload)}: event {event['seq']} holds it with a signature that does not verify", seq_int)
            plain = _plain_fields(payload)
            # An event recorded before entries carried their extract in plain has only the payload's.
            changed = sorted(k for k, v in plain.items() if event["data"].get(k) != v and (k != "extract_sha256" or k in event["data"]))
            if event["data"].get("payload") != payload:
                changed.insert(0, "payload")
            if (event["county"], event["board"]) != (payload.get("county"), payload.get("board")):
                changed.append("county or board")
            if event["client_time"] != payload.get("client_time"):
                changed.append("client_time")
            if changed:
                problem("differs", f"{_describe(payload)}: event {event['seq']} differs from the signed entry in {', '.join(changed)}", seq_int)

        receipt = entry.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        receipts += 1
        body = receipt.get("receipt")
        if not isinstance(body, Mapping):
            problem("receipt_invalid", f"{_describe(payload)}: its receipt is malformed", seq_int)
            continue
        code = receipt_code(receipt)
        key = server_key if server_key is not None else logged_keys.get(str(body.get("key_id")))
        if key is not None and not verify_receipt(receipt, key):
            problem("receipt_signature", f"{_describe(payload)}: receipt {code} is not signed by the server key", seq_int)
        if body.get("payload_sha256") != sha:
            problem("receipt_mismatch", f"{_describe(payload)}: receipt {code} is for a different payload", seq_int)
        event_seq, event_hash = body.get("event_seq"), body.get("event_hash")
        if isinstance(event_seq, int) and isinstance(event_hash, str):
            receipt_anchors.append((event_seq, event_hash, f"receipt {code}'s event"))
            at = by_seq.get(event_seq)
            if at is not None and at["hash"] == event_hash and at["data"].get("payload_sha256") != sha:
                problem("receipt_not_in_log", f"{_describe(payload)}: receipt {code}'s event {event_seq} does not hold this entry", seq_int)
        head_seq, head_hash = body.get("head_seq"), body.get("head_hash")
        if isinstance(head_seq, int) and isinstance(head_hash, str):
            receipt_anchors.append((head_seq, head_hash, f"receipt {code}'s chain head"))

    if server_key is None:
        if logged_keys:
            key_source = "taken from the log itself (not independent; pass --server-key from another source)"
            note("server_key", "receipts were checked with the server key published in the log, not an independent copy")
        elif receipts:
            note("server_key", "no server key given or published in the log: receipt signatures were not checked")

    for e in logged:
        sha_value = e["data"].get("payload_sha256")
        if isinstance(sha_value, str) and sha_value in record_hashes:
            continue
        payload = e["data"].get("payload")
        made = (
            public_key is not None
            and isinstance(payload, Mapping)
            and payload_sha256(payload) == sha_value
            and verify_device_signature(public_key, payload, e.get("signature"))
        )
        what = f"event {e['seq']} ({e['kind']}, device sequence number {e['data'].get('device_seq')})"
        if made:
            note("not_in_record", f"{what} is signed by this device but not in this record (downloaded before it?)")
        else:
            problem("not_by_device", f"{what} is under this observer and device, but the device did not sign it")
    if registration is not None and public_key is not None:
        registered = [e for e in logged if e["kind"] == REGISTERED_KIND]
        if registered and registered[0]["data"].get("public_key") != public_key:
            problem("differs", "the log registers a different key for this device")

    # -- anchors ----------------------------------------------------------
    checked = 0
    for seq, digest, what in [*receipt_anchors, *((s, h, f"anchor {s}") for s, h in anchors)]:
        checked += 1
        at = by_seq.get(seq)
        if at is None:
            if seq > log_report.event_count and log_report.ok:
                problem("log_truncated", f"{what} is event {seq}, but the log ends at event {log_report.event_count}")
            else:
                problem("anchor_mismatch", f"{what} is event {seq}, which is not in the verified part of the log")
        elif at["hash"] != digest:
            problem("anchor_mismatch", f"{what}: event {seq}'s hash in the log is {at['hash'][:16]}..., not {digest[:16]}...")

    return RecordReport(
        observer=observer,
        device_id=device,
        entries=len(payloads),
        receipts=receipts,
        log=log_report,
        server_key=key_source,
        anchors_checked=checked,
        findings=findings,
        lists=_lists(events, [p for p, _, _ in payloads]),
    )


def _lists(events: Sequence[Mapping[str, Any]], payloads: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """The payloads (registration aside) per pull list of the extract each refers to, as the log names the lists."""
    names: dict[str, str] = {}
    for e in events:
        sha = e.get("file_sha256")
        data = e.get("data") or {}
        if e.get("kind") == "extract.built" and isinstance(sha, str):
            names[sha] = f"Targeted: {data.get('list_name') or 'Targeted list'}" if data.get("order") == "targeted" else "RLA sample"
    counts: dict[str, int] = {}
    for payload in payloads:
        sha = payload.get("extract_sha256")
        if payload.get("type") == "device_registration" or not isinstance(sha, str):
            continue
        if payload.get("type") in ("lookup_contest", "lookup_done"):
            label = f"Looked up from boxes (redacted CVR {sha[:8]})"
        else:
            label = f"{names.get(sha, 'an extract not in the log')} (extract {sha[:8]})"
        counts[label] = counts.get(label, 0) + 1
    return counts


def render_report(report: RecordReport) -> str:
    """The report as text, as ``corla-observer check-record`` prints it."""
    log = report.log
    lines = [
        f"Record: {report.observer}, device {report.device_id}: {report.entries} entries, {report.receipts} with receipts",
        f"Log: {'intact' if log.ok else 'BROKEN'}, {log.event_count} events verified, head {log.head_seq} {log.head_hash}",
        f"Server key: {report.server_key}",
        f"Anchors: {report.anchors_checked} checked (receipts' events and chain heads, and any given)",
    ]
    if report.lists:
        lines.append("Entries by pull list: " + "; ".join(f"{label}: {n}" for label, n in report.lists.items()))
    for f in report.findings:
        lines.append(f"{'PROBLEM' if f.severity == 'problem' else 'note'} [{f.kind}] {f.message}")
    if report.ok:
        lines.append("OK: every entry in the record is in the log as signed, and no entry under this device is unsigned by it.")
    else:
        lines.append(f"{len(report.problems)} problem(s) found.")
    return "\n".join(lines)
