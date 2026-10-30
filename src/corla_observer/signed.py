"""Recording device-signed observer payloads, and answering each with a receipt.

The definitions are in `corla_observer.receipts` and docs/RECEIPTS.md. This
module takes one item a device sent (``{"payload": ..., "signature": ...}``)
for one observer link (`process_item`) and:

- **checks it:** the payload's structure, that it names the link's observer,
  county and board, that its device is registered under that observer (or,
  for a registration, that it is signed by the key it registers), and that
  the signature verifies with the registered key;
- **records it:** a registration as ``observer.device_registered`` (with the
  public key), an entry as ``observation.entry``, the first tap of a match
  (on the ballot's first reading) as ``observation.first_reading``, a
  boundary ballot the board did not pull (another board's) as
  ``observation.other_board``, a heard imprinted ID as
  ``observation.out_of_order`` or ``observation.id_not_on_list``
  (`corla_observer.practice`), and, from contest-by-contest entry
  (`corla_observer.contests`), one contest's outcome in one reading as
  ``observation.contest``, a ballot marked done as
  ``observation.ballot_done`` and a change of entry mode as
  ``observation.entry_mode``, and a switch to another of the county's pull
  lists as ``observation.list_switched``, each event carrying the device's
  signature in its ``signature`` field and the payload, its hash and the
  extract it refers to (``extract_sha256``) in ``data``;
- **answers it** with a receipt signed by the server's receipt key, whose
  public key is recorded once as ``receipt.key_published`` before its first
  receipt. A payload sent again (after a lost reply) gets a receipt for the
  event that recorded it the first time;
- **refuses** anything unsigned, unverifiable or inconsistent with what the
  same device already sent, with a clear error, and records each refusal as
  ``observation.rejected`` (the reason, the device and sequence number where
  known, and the payload's hash, never the payload itself).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from corla_observer.contests import PAYLOAD_KINDS, plain_fields
from corla_observer.events import Event, EventLog, InvalidEventError, JSONValue
from corla_observer.practice import (
    OBSERVER_KINDS,
    Link,
    PracticeError,
    record_entry,
    record_first_reading,
    record_heard_id,
    record_observation,
    record_other_board,
)
from corla_observer.receipts import (
    DEVICE_ALGORITHM,
    PayloadError,
    ReceiptSigner,
    check_payload,
    payload_sha256,
    verify_device_signature,
)

logger = logging.getLogger(__name__)

REGISTERED_KIND = "observer.device_registered"
REJECTED_KIND = "observation.rejected"
KEY_KIND = "receipt.key_published"
#: Event kinds that record a device-signed payload.
SIGNED_KINDS = OBSERVER_KINDS | {REGISTERED_KIND}


@dataclass
class Outcome:
    """What became of one item: ``recorded``, ``duplicate`` (recorded before) or ``rejected``."""

    status: str
    device_seq: int | None
    receipt: dict[str, JSONValue] | None = None
    error: str | None = None
    code: str | None = None
    #: For a heard imprinted ID: the event kind and the position found, if any.
    heard: dict[str, JSONValue] | None = None


class DeviceRegistry:
    """Registered device keys and recorded payloads, read from the event log and kept up to date.

    `refresh` reads only the events appended since it last ran, so it is
    cheap to call before every batch, and sees events other processes
    appended too.
    """

    def __init__(self, log: EventLog) -> None:
        self.log = log
        #: (observer actor, device ID) -> the registered public key (base64).
        self.keys: dict[tuple[str, str], str] = {}
        #: (observer actor, device ID, device_seq) -> (event seq, payload hash).
        self.payloads: dict[tuple[str, str, int], tuple[int, str]] = {}
        self.published_keys: set[str] = set()
        self.lock = threading.Lock()
        self._seen_to = 0

    def refresh(self) -> None:
        for event in self.log.iter_events(after_seq=self._seen_to):
            self._note(event)
            self._seen_to = event.seq

    def _note(self, event: Event) -> None:
        data = event.data
        if event.kind == KEY_KIND and isinstance(data.get("key_id"), str):
            self.published_keys.add(str(data["key_id"]))
            return
        if event.kind not in SIGNED_KINDS or not event.device_id:
            return
        sha, seq = data.get("payload_sha256"), data.get("device_seq")
        if not isinstance(sha, str) or not isinstance(seq, int):
            return
        if event.kind == REGISTERED_KIND and isinstance(data.get("public_key"), str):
            self.keys.setdefault((event.actor, event.device_id), str(data["public_key"]))
        self.payloads.setdefault((event.actor, event.device_id, seq), (event.seq, sha))


def event_at(log: EventLog, seq: int) -> Event:
    """The event with this seq."""
    for event in log.iter_events(after_seq=seq - 1, page_size=1):
        return event
    raise KeyError(seq)


def publish_key(log: EventLog, registry: DeviceRegistry, signer: ReceiptSigner) -> None:
    """Record the receipt key's public half in the log, once, before its first receipt."""
    if signer.key_id not in registry.published_keys:
        log.append(KEY_KIND, "system", data=signer.public_info())
        registry.published_keys.add(signer.key_id)


def _heard_info(event: Event) -> dict[str, JSONValue] | None:
    if event.kind == "observation.out_of_order":
        return {"kind": event.kind, "found_position": event.data.get("found_position")}
    if event.kind == "observation.id_not_on_list":
        return {"kind": event.kind, "found_position": None}
    return None


def _reject(log: EventLog, link: Link, item: object, exc: PayloadError) -> Outcome:
    payload = item.get("payload") if isinstance(item, Mapping) else None
    # An unsigned item from an older page carries its device and sequence number at the top.
    source = payload if isinstance(payload, Mapping) else item if isinstance(item, Mapping) else {}
    device_id = source.get("device_id")
    seq = source.get("device_seq")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= 2**53 - 1):
        seq = None
    try:
        sha: str | None = payload_sha256(payload) if isinstance(payload, Mapping) else None
    except PayloadError:
        sha = None
    logger.warning("refused an entry from %s (device %r, seq %r): %s", link.actor, device_id, seq, exc)
    data: dict[str, JSONValue] = {"code": exc.code, "reason": str(exc), "device_seq": seq, "payload_sha256": sha}
    try:
        log.append(
            REJECTED_KIND, link.actor, county=link.county, board=str(link.board),
            device_id=device_id if isinstance(device_id, str) else None, data=data,
        )
    except InvalidEventError:  # an unusable device ID: record the refusal without it
        log.append(REJECTED_KIND, link.actor, county=link.county, board=str(link.board), data=data)
    return Outcome("rejected", seq, error=str(exc), code=exc.code)


def process_item(
    log: EventLog,
    link: Link,
    item: object,
    *,
    registry: DeviceRegistry,
    signer: ReceiptSigner,
    seen: set[tuple[str, int]],
    ballots: Callable[[str], Sequence[Mapping[str, Any]]],
) -> Outcome:
    """Check, record and answer one signed item from an observer link's device; see the module documentation.

    The caller holds ``registry.lock`` and has called ``registry.refresh()``.
    ``ballots`` returns the board's ballots in the extract with the hash
    given (the payload's ``extract_sha256``), for matching a heard imprinted ID.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    try:
        if not isinstance(item, Mapping) or set(item) != {"payload", "signature"}:
            raise PayloadError(
                "unsigned", "unsigned entry: every entry must be sent as {payload, signature}, signed on the device"
            )
        payload = check_payload(item["payload"])
        signature = item["signature"]
        for name, expected in (("observer", link.actor), ("county", link.county), ("board", str(link.board))):
            if payload[name] != expected:
                raise PayloadError("wrong_link", f"the entry's {name} is {payload[name]!r}, but this link is for {expected!r}")
        sha = payload_sha256(payload)
        device, seq = str(payload["device_id"]), int(payload["device_seq"])
        registered = registry.keys.get((link.actor, device))
        if payload["type"] == "device_registration":
            key = str(payload["public_key"])
            if registered is not None and registered != key:
                raise PayloadError("conflict", "a different key is already registered under this device ID")
        else:
            if registered is None:
                raise PayloadError(
                    "unknown_device", "this device's key is not registered under this observer: send its registration first"
                )
            key = registered
        if not verify_device_signature(key, payload, signature):
            raise PayloadError("bad_signature", "the signature does not verify with this device's key")
        previous = registry.payloads.get((link.actor, device, seq))
        if previous is not None:
            if previous[1] != sha:
                raise PayloadError(
                    "conflict", f"this device already sent a different entry with sequence number {seq}"
                )
            event = event_at(log, previous[0])
            return Outcome("duplicate", seq, receipt=signer.issue(event, log.head(), sha), heard=_heard_info(event))

        publish_key(log, registry, signer)
        extra: dict[str, JSONValue] = {"payload": payload, "payload_sha256": sha}
        if payload["type"] != "device_registration":
            # Which extract the entry refers to, in plain: entries on different pull lists never mix (corla_observer.pulllists).
            extra["extract_sha256"] = payload["extract_sha256"]
        heard: dict[str, JSONValue] | None = None
        if payload["type"] == "device_registration":
            event = log.append(
                REGISTERED_KIND, link.actor, county=link.county, board=str(link.board), device_id=device,
                client_time=payload["client_time"], signature=str(signature),
                data={"public_key": key, "algorithm": DEVICE_ALGORITHM, "device_seq": 0} | extra,
            )
            registry.keys[(link.actor, device)] = key
        else:
            content = payload["content"]
            common = {
                "position": payload["position"],
                "imprinted_id": payload["imprinted_id"],
                "elapsed_ms": content.get("elapsed_ms"),
                "device_id": device,
                "device_seq": seq,
                "client_time": payload["client_time"],
            }
            try:
                if payload["type"] == "entry":
                    entry = common | {"match": content["match"], "note": content["note"], "edit": content["edit"]}
                    recorded = record_entry(log, link, entry, seen, signature=str(signature), extra=extra)
                    event_seq = recorded
                elif payload["type"] == "first_reading":
                    event_seq = record_first_reading(log, link, common, seen, signature=str(signature), extra=extra)
                elif payload["type"] == "other_board":
                    event_seq = record_other_board(log, link, common, seen, signature=str(signature), extra=extra)
                elif payload["type"] in PAYLOAD_KINDS:
                    event_seq = record_observation(
                        log, link, PAYLOAD_KINDS[payload["type"]], common | {"elapsed_ms": None}, plain_fields(payload), seen,
                        signature=str(signature), extra=extra,
                    )
                else:
                    heard_item = common | {"heard": content["heard"]}
                    outcome = record_heard_id(log, link, heard_item, seen, ballots(str(payload["extract_sha256"])), signature=str(signature), extra=extra)
                    event_seq = None if outcome is None else outcome[0]
                    if outcome is not None:
                        heard = {"kind": outcome[1], "found_position": outcome[2]}
            except PracticeError as exc:
                raise PayloadError("invalid", str(exc)) from None
            if event_seq is None:
                raise PayloadError("conflict", f"an unsigned entry with sequence number {seq} is already recorded for this device")
            event = event_at(log, event_seq)
        registry.payloads[(link.actor, device, seq)] = (event.seq, sha)
        return Outcome("recorded", seq, receipt=signer.issue(event, log.head(), sha), heard=heard)
    except PayloadError as exc:
        return _reject(log, link, item, exc)
