"""Publishing an observer's record and the event log with chosen entries withheld.

An observer's device-held record (docs/RECEIPTS.md) and the server's event log
(docs/EVENT_LOG.md) are meant to be published, but an entry may hold text that
must not be, such as the votes read from a ballot whose CVR row was redacted.
The log is hash-chained and anchored, so the entry can't simply be deleted.
Instead, ``corla-observer record withhold`` (`withhold_files`) writes a public
copy of both in which each chosen entry's text is replaced by a marker:

- in the record, the entry's ``payload`` becomes
  ``{"withheld": true, "reason": ..., "device_seq": N}``, keeping its
  ``payload_sha256``, ``signature`` and ``receipt``, so the next entry's
  ``prev_payload_sha256`` still links to the kept hash and the receipt can
  still be checked;
- in the log, each event holding that payload (matched by the payload's hash,
  or by device and device sequence number) becomes a *withheld line*: its
  ``seq``, ``hash``, ``prev_hash``, ``kind``, ``actor``, ``county``,
  ``board``, ``device_id``, ``v`` and times, with ``data`` replaced by the
  marker (with ``payload_sha256`` added) and ``"withheld": true``
  (`corla_observer.events.WITHHELD_FIELDS`).

``corla-observer check-record`` then checks everything else, including the
device's chain and the log's chain across the withheld entries, and counts
the withheld ones separately from problems (`corla_observer.observer_record`).

The hashes kept are of the withheld text. Anyone who could guess that text
exactly (every character of a note, and the times to the millisecond) could
confirm the guess against them; they reveal nothing else about it.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.events import WITHHELD_FIELDS, canonical_json
from corla_observer.observer_record import load_record
from corla_observer.receipts import PayloadError, payload_sha256


class WithholdError(ValueError):
    """The selection can't be withheld as asked."""


def is_withheld(obj: object) -> bool:
    """Whether a record entry's payload, or a log line or its data, is a withheld marker or line."""
    return isinstance(obj, Mapping) and obj.get("withheld") is True


@dataclass(frozen=True)
class Withheld:
    """One withheld entry of a record."""

    device_seq: int
    payload_sha256: str
    imprinted_id: str | None
    kind: str | None


@dataclass
class WithholdResult:
    """What `withhold_files` wrote and withheld."""

    entries: list[Withheld]
    #: The seqs of the log's lines withheld.
    events: list[int]
    #: Log lines not withheld whose text still contains a withheld entry's imprinted ID, for a person to look at.
    mentions: list[int] = field(default_factory=list)
    record_path: Path | None = None
    events_path: Path | None = None


def _record_marker(reason: str, device_seq: int) -> dict[str, Any]:
    return {"withheld": True, "reason": reason, "device_seq": device_seq}


def withhold_record(
    record: Mapping[str, Any],
    *,
    reason: str,
    imprinted_ids: Iterable[str] = (),
    device_seqs: Iterable[int] = (),
) -> tuple[dict[str, Any], list[Withheld]]:
    """A public copy of ``record`` with each selected entry's payload replaced by a withheld marker.

    An entry is selected if its payload's ``imprinted_id`` is one of ``imprinted_ids`` or its
    ``device_seq`` one of ``device_seqs``. Entries already withheld are kept as they are.

    Raises:
        WithholdError: if nothing is selected, the reason is empty, a selected entry is the device's
            registration (its key is needed to check the rest), or a payload's recorded hash is wrong.
    """
    if not reason.strip():
        raise WithholdError("give a reason for withholding")
    ids, seqs = set(imprinted_ids), set(device_seqs)
    public = copy.deepcopy(dict(record))
    withheld: list[Withheld] = []
    for entry in public.get("entries", []):
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, Mapping) or is_withheld(payload):
            continue
        if payload.get("imprinted_id") not in ids and payload.get("device_seq") not in seqs:
            continue
        if payload.get("type") == "device_registration":
            raise WithholdError("the device's registration can't be withheld: its key is needed to check the other entries")
        try:
            sha = payload_sha256(payload)
        except PayloadError:
            raise WithholdError(f"entry {payload.get('device_seq')}'s payload is not plain JSON") from None
        if entry.get("payload_sha256") not in (None, sha):
            raise WithholdError(f"entry {payload.get('device_seq')}'s recorded hash is not its payload's hash")
        seq = payload.get("device_seq")
        if not isinstance(seq, int):
            raise WithholdError("a selected entry has no device sequence number")
        entry["payload"] = _record_marker(reason, seq)
        entry["payload_sha256"] = sha
        withheld.append(Withheld(seq, sha, payload.get("imprinted_id"), payload.get("type")))
    if not withheld:
        raise WithholdError("no entry of the record matches the selection")
    return public, withheld


def withheld_line(event: Mapping[str, Any], reason: str, payload_sha: str | None, device_seq: int | None) -> bytes:
    """An event's withheld line (without its newline): its kept fields, the marker as ``data``, and ``withheld``."""
    data: dict[str, Any] = {"withheld": True, "reason": reason}
    if payload_sha is not None:
        data["payload_sha256"] = payload_sha
    if device_seq is not None:
        data["device_seq"] = device_seq
    kept = {name: event[name] for name in WITHHELD_FIELDS - {"data", "withheld"}}
    return canonical_json(kept | {"data": data, "withheld": True})


def withhold_events(
    lines: Iterable[bytes],
    entries: Iterable[Withheld],
    *,
    device_id: str | None,
    reason: str,
    keep_observer: str | None = None,
    others_reason: str | None = None,
) -> tuple[list[bytes], list[int], list[int]]:
    """The log's lines with each event holding a withheld entry replaced by its withheld line.

    An event holds an entry if its ``data`` carries the entry's ``payload_sha256``, or a ``payload`` with
    that hash, or it is under ``device_id`` with the entry's ``device_seq`` in ``data``. Returns the
    lines (each with its newline), the seqs withheld, and the seqs of other lines whose text still
    contains a withheld entry's imprinted ID.

    With ``others_reason``, every event by an observer other than ``keep_observer`` (an actor
    starting ``observer:``) is withheld too, with that reason: for publishing one observer's
    evidence without the other observers' entries, names apart (a withheld line keeps its actor).
    """
    entries = list(entries)
    by_sha = {w.payload_sha256: w for w in entries}
    by_seq = {w.device_seq: w for w in entries}
    needles = [w.imprinted_id.encode() for w in entries if w.imprinted_id]
    out: list[bytes] = []
    withheld: list[int] = []
    mentions: list[int] = []
    for raw in lines:
        event = json.loads(raw)
        if is_withheld(event):
            out.append(raw)
            continue
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        match = by_sha.get(str(data.get("payload_sha256")))
        payload = data.get("payload")
        if match is None and isinstance(payload, Mapping):
            try:
                match = by_sha.get(payload_sha256(payload))
            except PayloadError:
                pass
        if match is None and device_id is not None and event.get("device_id") == device_id and isinstance(data.get("device_seq"), int):
            match = by_seq.get(data["device_seq"])
        actor = str(event.get("actor", ""))
        if match is None and others_reason is not None and actor.startswith("observer:") and actor != keep_observer:
            out.append(withheld_line(event, others_reason, None, None) + b"\n")
            withheld.append(int(event["seq"]))
            continue
        if match is None:
            out.append(raw)
            if any(needle in raw for needle in needles):
                mentions.append(int(event["seq"]))
            continue
        out.append(withheld_line(event, reason, match.payload_sha256, match.device_seq) + b"\n")
        withheld.append(int(event["seq"]))
    return out, withheld, mentions


def withhold_files(
    record_path: Path | str,
    events_path: Path | str,
    out_dir: Path | str,
    *,
    reason: str,
    imprinted_ids: Iterable[str] = (),
    device_seqs: Iterable[int] = (),
    others_reason: str | None = None,
) -> WithholdResult:
    """Write ``record.json`` and ``events.jsonl`` into ``out_dir`` with the selected entries withheld.

    With ``others_reason``, other observers' events are withheld from the log as well (`withhold_events`).

    Raises:
        WithholdError: as `withhold_record`, or if ``out_dir`` already holds either file.
        corla_observer.observer_record.RecordError: if the record can't be read.
    """
    record = load_record(record_path)
    public, entries = withhold_record(record, reason=reason, imprinted_ids=imprinted_ids, device_seqs=device_seqs)
    device = record.get("device_id") if isinstance(record.get("device_id"), str) else None
    with open(events_path, "rb") as f:
        keep = record.get("observer") if isinstance(record.get("observer"), str) else None
        lines, events, mentions = withhold_events(f, entries, device_id=device, reason=reason, keep_observer=keep, others_reason=others_reason)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    record_out, events_out = out / "record.json", out / "events.jsonl"
    for path in (record_out, events_out):
        if path.exists():
            raise WithholdError(f"{path} already exists")
    record_out.write_text(json.dumps(public, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    events_out.write_bytes(b"".join(lines))
    return WithholdResult(entries, events, mentions, record_out, events_out)
