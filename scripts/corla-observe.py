#!/usr/bin/env python3
"""corla-observe.py: a command-line observer client for corla-observer, short enough to read whole.

It plays the observer's part only, as the observer page does in a browser
(docs/RECEIPTS.md), so that anyone can see exactly what code an observer ran:
this file, whose SHA-256 it prints at the start and puts in the record. It
speaks the server's observer protocol unchanged:

- **Start.** From an observer link (``https://HOST/PREFIX/observe/TOKEN``) it
  pins the server's receipt key (``/receipt-key``; with ``--server-key``, from
  another source, only if it is that key), loads the board's ballots and the
  extract's hash (``data.json``), and checks that the log records that
  extract (``extract.built``).
- **The device.** An ECDSA P-256 key in ``device-key.pem`` (mode 0600) beside
  the record. The device ID is ``d-`` and the first 32 hex digits of the
  SHA-256 of the 65-byte uncompressed public key. Its signed registration is
  its entry 0.
- **Two taps.** Once the notice is acknowledged it signs ``entry_mode``
  (``two_tap``) and ``acknowledged``, as the page does. Then, ballot by
  ballot in pull order, Enter at the first reading signs a ``first_reading``,
  Enter again an ``entry`` with ``match`` true, and text typed instead,
  once confirmed, an ``entry`` with ``match`` false and that note.
  ``/e N`` goes back to ballot N; a new entry for a ballot already entered
  has ``edit`` true. ``/c`` copies the ballot, with ``__`` to mark, to the
  clipboard (OSC 52) and to ``copy.txt``, for marking in an editor.
- **Signing and keeping.** Each payload is canonical JSON (docs/EVENT_LOG.md),
  chained by ``device_seq`` and ``prev_payload_sha256``, signed as Web Crypto
  signs (``r || s``, 64 bytes, base64), and appended to ``record.jsonl``,
  synced to disk, before it is sent. What can't be sent stays queued and is
  sent again, oldest first, until it is answered (an answer of 500 or more,
  often from a proxy, counts as not sent). If the server answers
  ``unknown_device`` (it has no registration for the device), it sends the
  same registration again and then those entries, once in a session, as the
  page does; any other refusal is final, with an ALARM.
- **Receipts.** Each is checked as it arrives: the Ed25519 signature under the
  pinned key, the payload's hash and event kind, and that its log positions
  agree with every earlier receipt. Any failure is an ALARM.
- **The log.** At the start, every few minutes, at ``/check`` and at the end,
  it fetches the log's new lines (``/head``, then ``/observe/TOKEN/log``),
  verifies their hash chain as the verifier in docs/EVENT_LOG.md does, keeps
  them in ``events.jsonl``, and checks that every receipted entry is in the
  log where its receipt says, as the device signed it, and that no event
  under this device's ID is one it did not sign. Any failure is an ALARM.
- **The published commitments** (``--commitments``, a GitHub folder or a
  local one, never the server): that every SHA256SUMS file made after the
  extract was built lists it (an extract built after the latest one is noted
  as not committed yet), that they list the receipt key
  (``derived/receipt-key.json``, read and checked against its listed hash),
  and that the log agrees with each one's ``event-log-head``. Any failure is
  an ALARM.
- **Alarms.** Each is printed with a statement to send the coordinator and the
  other observers (``/report`` prints them all), and shown at every prompt
  until ``/ack``. They stay in the record.
- **The record.** After every entry and every answer from the server it writes ``record.json`` in the
  ``corla-observer-record`` format, for ``corla-observer check-record``.

It deliberately leaves out contest-by-contest entry, switching pull lists,
looking up ballots from boxes, heard imprinted IDs (a typed ID is a note),
skipping a boundary ballot as another board's, photo checks and printouts.

Tradeoffs:
- **The key is a file.** Anything that can read ``device-key.pem`` can copy it
  and sign as this device, where the browser's key is non-extractable. Keep
  the folder (made with mode 0700) private, on a computer of your own.
- **It must track docs/RECEIPTS.md.** It makes the payloads itself, so a
  change to the protocol needs a change here.
- **The server must be reachable to start,** to give the ballots. After that,
  entries are kept and queued while it can't be reached.

Requirements: Python 3.12 and the ``cryptography`` package, nothing else;
tested with Python 3.12.11 and cryptography 50.0.2. Installed apart::

    python3 -m venv observe-env && observe-env/bin/pip install cryptography==50.0.2
    observe-env/bin/python -I corla-observe.py 'https://HOST/PREFIX/observe/TOKEN'

Commands start with ``/`` (``:`` also works): ``?`` or ``/h`` (help; never taken as a note),
``/e N``, ``/c`` [all], ``/ref``, ``/check``, ``/report``, ``/ack``, ``/s``, ``/save``, ``/q`` (or end of
input). Exit status 0; 1 if the record holds an ALARM; 2 if it could not
start. See docs/CLI_OBSERVER.md.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import posixpath
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

SOFTWARE = "corla-observe.py"
ZERO_HASH = "0" * 64
ALGORITHM = "ECDSA-P256-SHA256"
NOTICE = "out_of_sight_1"
BATCH = 50  # items sent in one request (the server takes up to 100)
LOG_CHUNK = 2000  # log lines fetched at once (the server's limit)
TIMEOUT = 20  # seconds for one request
#: The event that records each payload type this client sends.
KINDS = {
    "device_registration": "observer.device_registered",
    "entry": "observation.entry",
    "first_reading": "observation.first_reading",
    "entry_mode": "observation.entry_mode",
    "acknowledged": "observation.acknowledged",
}
RECEIPT_FIELDS = {"v", "type", "key_id", "event_seq", "event_hash", "event_kind", "payload_sha256", "server_time", "head_seq", "head_hash"}
LOG_FIELDS = {"seq", "server_time", "client_time", "actor", "kind", "county", "board",
              "session_id", "device_id", "file_sha256", "signature", "data", "v", "prev_hash"}  # fmt: skip
#: A SUMS file's name (docs/TIMESTAMPING.md), and the lines of one that this client reads.
SUMS_NAME = re.compile(r"^SHA256SUMS(?:-v([0-9]+)-[0-9]{8}T[0-9]{6}Z|\.v([0-9]+))?\.txt$")
SUMS_HEAD = re.compile(r"^# event-log-head: ([0-9]+) ([0-9a-f]{64})$")
SUMS_EXTRACT = re.compile(r"^([0-9a-f]{64})  (?:.*/)?derived/extract-[^/]+\.json$")
SUMS_KEY = re.compile(r"^([0-9a-f]{64})  ((?:.*/)?derived/receipt-key\.json)$")
GITHUB_FOLDER = re.compile(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?")
NOTICE_TEXT = """
Before you start
  Keep this screen out of sight of the audit board and the public. The board must
  never see the votes on it: turn the laptop away from the table.
  Two taps per ballot: press Enter after the first board member reads the ballot,
  and Enter again after the second. Heard something different? Type it, then Enter.
  Don't interrupt the board: observers note, they don't overrule.
"""


def canonical(value: Any) -> bytes:
    """The canonical serialization of docs/EVENT_LOG.md: keys sorted, no whitespace, characters as themselves, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def utc_now() -> str:
    """The time now, as payloads carry it: ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean_text(text: str, limit: int) -> str:
    """Text as a payload may carry it: no control characters, trimmed, at most ``limit`` characters (as signing.js)."""
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text).strip()
    return cleaned[:limit].strip()


def describe(payload: dict[str, Any]) -> str:
    if payload["type"] == "device_registration":
        return "entry 0 (the device's registration)"
    return f"entry {payload['device_seq']} ({payload['type']}, ballot {payload['position']} {payload['imprinted_id']})"


# ---------------------------------------------------------------------------
# The event log: one line checked as the verifier in docs/EVENT_LOG.md checks it


class LogBroken(Exception):
    pass


class CannotStart(Exception):
    pass


def _no_floats(text: str) -> Any:
    raise ValueError(f"not an integer: {text}")


def verify_line(line: bytes, seq: int, prev_hash: str) -> tuple[dict[str, Any], str]:
    """Check one line of ``events.jsonl``, which must be event ``seq`` after ``prev_hash``; return the event and its hash."""
    if not line.endswith(b"\n"):
        raise LogBroken("no line feed at the end")
    line = line[:-1]
    tail = line[-75:].decode("ascii", "replace")  # ',"hash":"' + 64 hex digits + '"}'
    claimed = tail[9:73]
    if not (len(line) > 75 and tail.startswith(',"hash":"') and tail.endswith('"}') and re.fullmatch("[0-9a-f]{64}", claimed)):
        raise LogBroken("the line does not end with its hash")
    hash_input = line[:-75] + b"}"
    if sha256_hex(hash_input) != claimed:
        raise LogBroken("the hash does not match the content")
    try:
        event = json.loads(hash_input.decode("utf-8"), parse_float=_no_floats, parse_constant=_no_floats)
        if not isinstance(event, dict) or event.get("v") != 1 or event.get("v") is True or set(event) != LOG_FIELDS:
            raise LogBroken("not a version 1 event with exactly the 14 fields")
        if canonical(event) != hash_input:
            raise LogBroken("not in canonical form")
    except ValueError as exc:
        raise LogBroken(f"not valid JSON: {exc}") from None
    if event["seq"] != seq or event["prev_hash"] != prev_hash:
        raise LogBroken("the chain does not continue from the line before")
    return event, claimed


# ---------------------------------------------------------------------------
# The server's receipt key, given independently


def expected_key_id(text: str) -> str:
    """The key ID (SHA-256 of the 32-byte Ed25519 public key) from ``--server-key``: a file or text holding
    the key ID, the JSON from ``/receipt-key`` or the base64 key."""
    if os.path.isfile(text):
        text = Path(text).read_text(encoding="utf-8")
    text = text.strip()
    if re.fullmatch("[0-9a-f]{64}", text):
        return text
    if text.startswith("{"):
        text = str(json.loads(text).get("public_key", ""))
    raw = base64.b64decode(text, validate=True)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return sha256_hex(raw)


def sums_version(name: str) -> int:
    """A SUMS file's version, from its name: SHA256SUMS.txt is 1."""
    match = SUMS_NAME.match(name)
    return int(match[1] or match[2] or 1) if match else 0


def fetch(url: str) -> bytes:
    """A file from a source apart from the server (the published commitments)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": SOFTWARE}), timeout=TIMEOUT) as response:
        data: bytes = response.read()
        return data


# ---------------------------------------------------------------------------
# The device key


class DeviceKey:
    """The device's ECDSA P-256 key, in a PEM file readable only by its owner, made the first time."""

    def __init__(self, path: Path) -> None:
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise CannotStart(f"{path} must be readable only by you (chmod 600)")
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                raise CannotStart(f"{path} is not a P-256 private key")
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
                f.flush()
                os.fsync(f.fileno())
        self.key = key
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = b64(raw)
        self.device_id = "d-" + sha256_hex(raw)[:32]

    def sign(self, payload: dict[str, Any]) -> str:
        """The signature as Web Crypto makes it: ``r || s``, each 32 bytes big-endian, in base64."""
        r, s = decode_dss_signature(self.key.sign(canonical(payload), ec.ECDSA(hashes.SHA256())))
        return b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


# ---------------------------------------------------------------------------
# The observer


class Observer:
    """One device observing one link: its record, its queue, and its checks of receipts and the log."""

    def __init__(
        self, link: str, folder: Path, *, server_key: str | None = None, commitments: str | None = None, say: Callable[[str], None] = print
    ) -> None:
        match = re.fullmatch(r"(https?://[^?#]+?)/observe/([A-Za-z0-9_-]+)/?", link.strip())
        if not match:
            raise CannotStart("the observer link should look like https://HOST/PREFIX/observe/TOKEN")
        self.base, self.token = match.group(1), match.group(2)
        self.folder, self.server_key, self.commitments, self.say = folder, server_key, commitments, say
        self.sha256 = sha256_hex(Path(__file__).read_bytes())
        self.items: list[dict[str, Any]] = []  # in device_seq order: payload, signature, payload_sha256, receipt, refused
        self.by_sha: dict[str, dict[str, Any]] = {}
        self.alarms: list[dict[str, str]] = []  # each: text, where (the position and hash in question), time
        self.acked = 0  # how many of the alarms the observer has acknowledged (/ack)
        self.committed: dict[str, Any] | None = None  # what the published commitments say, once read
        self.sessions: list[dict[str, Any]] = []
        self.stated: dict[int, tuple[str, str]] = {}  # log position -> (hash, which receipt said so)
        self.log_hashes: list[str] = []  # the verified log's hashes, event 1 first
        self.logged: dict[str, int] = {}  # payload hash -> the verified event holding it
        self.extract_at: int | None = None
        self.key_at: int | None = None
        self.max_head = 0
        self.receipts_ok = 0
        self.retry_delay = 2.0
        self.reachable = True  # whether the last attempt to send reached the server
        self.outage = False  # sending has failed with a 5xx answer since the last success (said once)
        self.registered_again = False  # the registration was sent again after the server forgot the device
        self.retried = False  # ... in this session (once only)
        self.lock = threading.RLock()  # the record and the checks
        self.send_lock = threading.Lock()  # one send at a time
        self.log_lock = threading.Lock()  # one log check at a time

    # -- talking to the server --

    def http(self, path: str, body: Any = None) -> bytes:
        headers = {"User-Agent": SOFTWARE} | ({"Content-Type": "application/json"} if body is not None else {})
        request = urllib.request.Request(self.base + path, canonical(body) if body is not None else None, headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data: bytes = response.read()
            return data

    def get_json(self, path: str) -> Any:
        return json.loads(self.http(path))

    # -- keeping the record --

    def journal(self, line: dict[str, Any]) -> None:
        """Append one line to record.jsonl and sync it to disk."""
        with open(self.folder / "record.jsonl", "ab") as f:
            f.write(canonical(line) + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def alarm(self, text: str, where: str = "") -> None:
        """Shout: something the server said or served disagrees with what the device did, or with what it said before."""
        with self.lock:
            if any(a["text"] == text for a in self.alarms):
                return
            found = {"text": text, "where": where, "time": utc_now()}
            self.alarms.append(found)
            self.journal({"alarm": found})
        self.say(f"\a\n*** ALARM: {text} ***\n\n{self.statement(found)}\n")

    def statement(self, found: dict[str, str]) -> str:
        """An alarm in plain words, for the observer to send to the coordinator and the other observers (not to the server)."""
        device = self.device.device_id if hasattr(self, "device") else "(none yet)"
        return "\n".join([
            "To my coordinator and the other observers: my observer client raised an ALARM.",
            f"  Found:  {found['text']}", f"  Where:  {found['where'] or '(see above)'}", f"  Server: {self.base}",
            f"  Link:   {getattr(self, 'county', '?')}, board {getattr(self, 'board', '?')}", f"  Device: {device}",
            f"  Client: {SOFTWARE}, SHA-256 {self.sha256}", f"  Time:   {found['time']}",
        ])  # fmt: skip

    def acknowledge(self) -> None:
        with self.lock:
            self.acked = len(self.alarms)
            self.journal({"ack": {"alarms": self.acked, "time": utc_now()}})

    # -- starting --

    def start(self) -> None:
        """Pin the server key, load the ballots and the record, register the device if new, and check the log."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.say(f"{SOFTWARE} sha256 {self.sha256}")
        served = self.get_json("/receipt-key")
        raw = base64.b64decode(served["public_key"], validate=True)
        if served.get("algorithm") != "Ed25519" or len(raw) != 32 or sha256_hex(raw) != served.get("key_id"):
            raise CannotStart(f"the server's /receipt-key is not a consistent Ed25519 key: {served}")
        pinned_file = self.folder / "server-key.json"
        if self.server_key is not None and expected_key_id(self.server_key) != served["key_id"]:
            raise CannotStart(f"the server's receipt key {served['key_id']} is not the one given with --server-key: refusing to start")
        if pinned_file.exists() and json.loads(pinned_file.read_text())["key_id"] != served["key_id"]:
            raise CannotStart(f"the server's receipt key is now {served['key_id']}, not the one pinned in {pinned_file}: refusing to start")
        if not pinned_file.exists():
            pinned_file.write_text(json.dumps(served) + "\n")
        self.receipt_key, self.key_id = Ed25519PublicKey.from_public_bytes(raw), str(served["key_id"])
        self.server_info = served
        self.say(f"Receipt key {self.key_id}" + (" (as given)" if self.server_key else " (pinned)"))

        data = self.get_json(f"/observe/{self.token}/data.json")
        if "error" in data or not data.get("ballots"):
            raise CannotStart(f"no ballots for this link yet: {data.get('error', 'the board has none')}")
        self.observer, self.county, self.board = str(data["observer"]), str(data["county"]), str(data["board"])
        if data.get("receipt_key") != served:
            self.alarm("data.json names a different receipt key from /receipt-key", f"receipt key {self.key_id}")
        self.extract, self.ballots, self.list_label = str(data["extract_sha256"]), list(data["ballots"]), str(data.get("list_label"))
        self.done = {str(p) for p in data.get("entries") or {}}
        self.say(f"{data.get('ident')}: {self.list_label}, {len(self.ballots)} ballots, extract {self.extract}")
        if data.get("notice"):
            self.say(str(data["notice"]))

        self.device = DeviceKey(self.folder / "device-key.pem")
        self.load_journal()
        if not self.items:
            self.keep({"v": 1, "type": "device_registration", "observer": self.observer, "device_id": self.device.device_id,
                       "county": self.county, "board": self.board, "public_key": self.device.public_key, "algorithm": ALGORITHM,
                       "client_time": utc_now(), "device_seq": 0, "prev_payload_sha256": ZERO_HASH})  # fmt: skip
        first = self.items[0]["payload"]
        if (first["observer"], first["county"], first["board"], first["device_id"]) != (self.observer, self.county, self.board, self.device.device_id):
            raise CannotStart(f"{self.folder} holds the record of another link or device: use another folder")
        self.say(f"Device {self.device.device_id}, {len(self.items)} entries in {self.folder / 'record.jsonl'}")
        session = {"started": utc_now(), "software": SOFTWARE, "sha256": self.sha256}
        self.sessions.append(session)
        self.journal({"session": session})
        self.load_local_log()
        self.read_commitments()
        self.check_log()
        self.say(f"Log verified to event {len(self.log_hashes)}; the extract was recorded as built in event {self.extract_at}")

    def load_journal(self) -> None:
        """Read record.jsonl back: the items in order, their receipts (checked again) and refusals, earlier sessions."""
        path = self.folder / "record.jsonl"
        if not path.exists():
            return
        for line in path.read_bytes().splitlines():
            entry = json.loads(line)
            if "item" in entry:
                item = entry["item"] | {"receipt": None, "refused": None}
                self.items.append(item)
                self.by_sha[item["payload_sha256"]] = item
                if item["payload"]["type"] == "entry" and item["payload"]["extract_sha256"] == self.extract:
                    self.done.add(str(item["payload"]["position"]))
            elif "receipt" in entry:
                self.accept_receipt(self.items[entry["device_seq"]], entry["receipt"], keep=False)
            elif "refused" in entry:
                self.items[entry["device_seq"]]["refused"] = entry["refused"]
            elif "session" in entry:
                self.sessions.append(entry["session"])
            elif "alarm" in entry:
                self.alarms.append(entry["alarm"])
            elif "ack" in entry:
                self.acked = entry["ack"]["alarms"]
            elif "registered_again" in entry:
                self.items[0]["receipt"], self.registered_again = None, True

    # -- signing --

    def keep(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sign a payload, and append it to the record before anything is sent."""
        item: dict[str, Any] = {"payload": payload, "signature": self.device.sign(payload), "payload_sha256": sha256_hex(canonical(payload))}
        self.journal({"item": item})
        item |= {"receipt": None, "refused": None}
        self.items.append(item)
        self.by_sha[item["payload_sha256"]] = item
        return item

    def sign_next(self, kind: str, ballot: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
        """The device's next payload: the next sequence number, chained to the one before."""
        with self.lock:
            last = self.items[-1]
            return self.keep({
                "v": 1, "type": kind, "observer": self.observer, "device_id": self.device.device_id, "county": self.county,
                "board": self.board, "extract_sha256": self.extract, "position": ballot["position"], "imprinted_id": ballot["id"],
                "content": content, "client_time": utc_now(), "device_seq": last["payload"]["device_seq"] + 1,
                "prev_payload_sha256": last["payload_sha256"],
            })  # fmt: skip

    def next_ballot(self) -> dict[str, Any] | None:
        """The first ballot in pull order without an entry."""
        return next((b for b in self.ballots if str(b["position"]) not in self.done), None)

    def start_two_tap(self) -> None:
        """What the page signs when the observer starts from its notice: the entry mode, then the acknowledgement."""
        at = self.next_ballot() or self.ballots[0]
        self.sign_next("entry_mode", at, {"mode": "two_tap"})
        self.sign_next("acknowledged", at, {"notice": NOTICE})

    def enter(self, ballot: dict[str, Any], match: bool, note: str, elapsed_ms: int) -> dict[str, Any]:
        """A match, or a note of what was heard instead; ``edit`` if the ballot already has an entry."""
        edit = str(ballot["position"]) in self.done
        item = self.sign_next("entry", ballot, {"match": match, "note": "" if match else clean_text(note, 500), "edit": edit, "elapsed_ms": elapsed_ms})
        self.done.add(str(ballot["position"]))
        return item

    # -- sending, and checking receipts --

    def pending(self) -> list[dict[str, Any]]:
        with self.lock:
            return [i for i in self.items if i["receipt"] is None and i["refused"] is None]

    def flush(self) -> bool:
        """Send what is queued, oldest first, in batches. True if nothing is left to send."""
        if not self.send_lock.acquire(blocking=False):
            return False
        try:
            while batch := self.pending()[:BATCH]:
                body = {"entries": [{"payload": i["payload"], "signature": i["signature"]} for i in batch]}
                try:
                    result = json.loads(self.http(f"/observe/{self.token}/entries", body))
                except urllib.error.HTTPError as exc:
                    self.reachable = False
                    if exc.code >= 500:  # often a proxy in front of the server: the request may never have arrived
                        if not self.outage:
                            self.say(f"Not sent yet ({exc.code} from the server or a proxy in front of it): your entries are kept and will be sent again.")
                        self.outage = True
                    else:
                        self.say(f"The server answered {exc.code}" + (": the link was revoked or replaced; ask your coordinator" if exc.code == 404 else ""))
                    self.retry_delay = min(self.retry_delay * 2, 30.0)
                    return False
                except (OSError, http.client.HTTPException, ValueError):
                    self.reachable = False
                    self.retry_delay = min(self.retry_delay * 2, 30.0)
                    return False
                self.reachable, self.retry_delay = True, 2.0
                if self.outage:
                    self.say("Sent: the entries kept while the server answered with errors have reached it.")
                    self.outage = False
                settled = self.answer(batch, result)
                self.save()
                if not settled:
                    self.alarm(f"the server answered {len(batch)} entries with neither a receipt nor a reason")
                    return False
            return True
        finally:
            self.send_lock.release()

    def answer(self, batch: list[dict[str, Any]], result: dict[str, Any]) -> bool:
        """Take the server's answer to a batch: each receipt checked, each refusal kept. True if it settled anything."""
        settled, forgotten = False, []
        with self.lock:
            for item in batch:
                receipt = (result.get("receipts") or {}).get(str(item["payload"]["device_seq"]))
                if receipt is not None:
                    self.accept_receipt(item, receipt)
                    settled = True
            for error in result.get("errors") or []:
                seq = error.get("device_seq")
                if not isinstance(seq, int) or not 0 <= seq < len(self.items):
                    self.alarm(f"the server refused something this device did not send: {error}")
                    continue
                if error.get("code") == "unknown_device" and seq != 0 and not self.retried:
                    # The server has no registration for this device (a server restored from a backup, say). As the page
                    # does, send the same signed registration again, then this entry; but only once in a session.
                    self.journal({"unknown_device": {"code": error.get("code"), "error": error.get("error")}, "device_seq": seq})
                    forgotten.append(seq)
                    continue
                self.items[seq]["refused"] = {"code": error.get("code"), "error": error.get("error")}
                self.journal({"refused": self.items[seq]["refused"], "device_seq": seq})
                self.alarm(f"the server refused {describe(self.items[seq]['payload'])}: {error.get('error')}",
                           f"entry {seq}, payload {self.items[seq]['payload_sha256']}")
                settled = True
            if forgotten:
                self.say(f"The server no longer knows this device's key: sending its registration again, then entries {forgotten}.")
                self.journal({"registered_again": {"time": utc_now(), "earlier_receipt": self.items[0]["receipt"]}})
                self.items[0]["receipt"], self.registered_again, self.retried, settled = None, True, True, True
        return settled

    def accept_receipt(self, item: dict[str, Any], receipt: Any, *, keep: bool = True) -> None:
        """Check a receipt (docs/RECEIPTS.md, "Receipts"), keep it, and alarm on anything wrong with it."""
        with self.lock:
            item["receipt"] = receipt
            if keep:
                self.journal({"receipt": receipt, "device_seq": item["payload"]["device_seq"]})
            what = f"the receipt for {describe(item['payload'])}"
            body = receipt.get("receipt") if isinstance(receipt, dict) else None
            if not isinstance(body, dict) or set(body) != RECEIPT_FIELDS:
                self.alarm(f"{what} is malformed", f"entry {item['payload']['device_seq']}, payload {item['payload_sha256']}")
                return
            problems = []
            try:
                self.receipt_key.verify(base64.b64decode(str(receipt.get("signature")), validate=True), canonical(body))
            except (InvalidSignature, ValueError):
                problems.append("its signature does not verify with the pinned receipt key")
            if body["key_id"] != self.key_id or body["v"] != 1 or body["type"] != "receipt":
                problems.append("it names another key or format")
            if body["payload_sha256"] != item["payload_sha256"]:
                problems.append("it is for another payload")
            if body["event_kind"] != KINDS[item["payload"]["type"]]:
                problems.append(f"it says the payload was recorded as {body['event_kind']}")
            positions = [body[k] for k in ("event_seq", "head_seq")]
            if not all(isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in positions) or body["event_seq"] > body["head_seq"]:
                problems.append("its log positions are not possible")
            else:
                seq = item["payload"]["device_seq"]
                before = [i["receipt"]["receipt"]["event_seq"] for i in self.items[:seq] if self.has_receipt(i)]
                after = [i["receipt"]["receipt"]["event_seq"] for i in self.items[seq + 1 :] if self.has_receipt(i)]
                again = seq == 0 and self.registered_again  # a registration sent again is recorded after the entries
                if not again and (any(e >= body["event_seq"] for e in before) or any(e <= body["event_seq"] for e in after)):
                    problems.append(f"its event {body['event_seq']} is out of order with this device's other entries")
                if body["head_seq"] < self.max_head and keep:
                    problems.append(f"its head {body['head_seq']} is behind an earlier receipt's ({self.max_head})")
                self.max_head = max(self.max_head, body["head_seq"])
                self.anchor(body["event_seq"], str(body["event_hash"]), f"receipt {body['event_seq']}-{str(body['event_hash'])[:6]}")
                self.anchor(body["head_seq"], str(body["head_hash"]), f"receipt {body['event_seq']}-{str(body['event_hash'])[:6]}'s head")
            if problems:
                where = f"receipt {body['event_seq']}-{str(body['event_hash'])[:6]}: event {body['event_seq']} {body['event_hash']}, head {body['head_seq']} {body['head_hash']}"
                self.alarm(f"{what}: " + "; ".join(problems), where)
            else:
                self.receipts_ok += 1

    @staticmethod
    def has_receipt(item: dict[str, Any]) -> bool:
        body = item["receipt"].get("receipt") if isinstance(item["receipt"], dict) else None
        return isinstance(body, dict) and isinstance(body.get("event_seq"), int)

    def anchor(self, seq: int, digest: str, what: str) -> None:
        """A hash something has stated for log position ``seq``: it must agree with every other statement, and with the log."""
        known = self.stated.setdefault(seq, (digest, what))
        if known[0] != digest:
            self.alarm(f"{what} says event {seq} has hash {digest[:16]}..., but {known[1]} said {known[0][:16]}...: there are two logs",
                       f"event {seq}: {digest} ({what}), {known[0]} ({known[1]})")
        if seq <= len(self.log_hashes) and self.log_hashes[seq - 1] != digest:
            self.alarm(f"{what} says event {seq} has hash {digest[:16]}..., but the log has {self.log_hashes[seq - 1][:16]}...",
                       f"event {seq}: {digest} ({what}), {self.log_hashes[seq - 1]} (the log)")

    # -- the published commitments --

    def commitment_file(self, path: str) -> bytes:
        """A file of the commitments, by its path relative to their folder: from GitHub (not its API), or the local folder."""
        assert self.commitments is not None
        if github := GITHUB_FOLDER.fullmatch(self.commitments):
            owner, repo, ref, folder = github.groups()
            full = posixpath.normpath(posixpath.join(folder, path))
            if full == ".." or full.startswith(("../", "/")):
                raise ValueError(f"{path} is outside the repository")
            return fetch(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{full}")
        return (Path(self.commitments) / path).read_bytes()

    def read_commitments(self) -> None:
        """Read the SUMS files of the published commitments (docs/TIMESTAMPING.md) from a source apart from the server: a
        GitHub folder (https://github.com/OWNER/REPO/tree/REF/PATH) or a local folder. Check that they list the extract
        shown and the receipt key, and take each one's event-log-head as an anchor the log must agree with."""
        if self.commitments is None:
            self.say("No --commitments given: the extract, the receipt key and the log are not checked against published commitments.")
            return
        github = GITHUB_FOLDER.fullmatch(self.commitments)
        if not github and not os.path.isdir(self.commitments):
            raise CannotStart("--commitments must be a GitHub folder (https://github.com/OWNER/REPO/tree/REF/PATH) or a local folder")
        try:
            if github:
                owner, repo, ref, folder = github.groups()
                listing = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repo}/contents/{folder}?ref={ref}"))
                names = [str(entry["name"]) for entry in listing if SUMS_NAME.match(str(entry["name"]))]
            else:
                names = [f.name for f in Path(self.commitments).iterdir() if SUMS_NAME.match(f.name)]
            names.sort(key=sums_version)
            files = [(name, self.commitment_file(name).decode("utf-8")) for name in names]
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
            self.say(f"The published commitments could not be read ({exc}): they are not checked this time.")
            return
        listed_in: list[str] = []  # the SUMS files that list the extract shown
        key: tuple[str, str, str] | None = None  # the latest SUMS file listing the key, the hash, the key file's path
        anchors: list[dict[str, Any]] = []
        for name, text in files:
            base = "."  # the election's folder, from the SUMS file's folder; listed paths are relative to it
            for line in text.splitlines():
                if line.startswith("# base: "):
                    base = line.removeprefix("# base: ").strip()
                if head := SUMS_HEAD.match(line):
                    anchors.append({"seq": int(head[1]), "hash": head[2], "file": name})
                if (listed := SUMS_EXTRACT.match(line)) and listed[1] == self.extract and name not in listed_in:
                    listed_in.append(name)
                if listed := SUMS_KEY.match(line):
                    key = (name, listed[1], posixpath.normpath(posixpath.join(base, listed[2])))
        with self.lock:
            self.committed = {"source": self.commitments, "files": names, "extract_in": listed_in, "extract": None, "anchors": anchors}
            for anchor in anchors:
                self.anchor(anchor["seq"], anchor["hash"], f"commitment {anchor['file']}")
            self.committed["receipt_key"] = self.check_committed_key(key)

    def check_committed_extract(self) -> None:
        """Once the log shows when the extract was built: every commitment made at or after that event must list it."""
        if self.committed is None or self.extract_at is None:
            return
        later = [a for a in self.committed["anchors"] if a["seq"] >= self.extract_at]
        missing = [a for a in later if a["file"] not in self.committed["extract_in"]]
        if missing:
            status = f"not listed in {missing[0]['file']}, made at event {missing[0]['seq']}, after it was built"
            self.alarm(f"the extract shown, built in event {self.extract_at}, is not in the commitment {missing[0]['file']}, "
                       f"made later (at event {missing[0]['seq']})", f"extract {self.extract}; {missing[0]['file']}, event-log-head {missing[0]['seq']}")  # fmt: skip
        elif self.committed["extract_in"]:
            status = f"listed in {', '.join(self.committed['extract_in'])}"
        else:
            status = f"built in event {self.extract_at}, after the latest commitment: not committed yet"
        if status != self.committed["extract"]:
            self.committed["extract"] = status
            if not missing:
                self.say(f"The extract shown: {status}.")

    def check_committed_key(self, key: tuple[str, str, str] | None) -> str:
        """The committed receipt key (derived/receipt-key.json: the canonical JSON of what /receipt-key serves), against the server's."""
        if key is None:
            self.say("The published commitments list no receipt key yet (derived/receipt-key.json): the key is not checked against them.")
            return "not listed yet"
        name, listed, path = key
        served = {k: self.server_info[k] for k in ("algorithm", "public_key", "key_id")}
        where = f"receipt key {self.key_id}; {path}, listed in {name} with SHA-256 {listed}"
        try:
            body = self.commitment_file(path)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.say(f"{path} could not be read ({exc}): the receipt key is checked against its listed hash only.")
            body = None
        if body is not None and sha256_hex(body) != listed:
            self.alarm(f"{path} in the commitments is not the file {name} lists", where)
            return "file differs from its listing"
        try:
            same = json.loads(body) == served if body is not None else sha256_hex(canonical(served)) == listed
        except ValueError:
            same = False
        if not same:
            self.alarm("the server's receipt key is not the one in the published commitments", where)
            return "differs"
        self.say(f"The receipt key is the one in the published commitments ({name}).")
        return "listed"

    def commitment_summary(self) -> dict[str, Any] | None:
        """For the record: what the commitments listed, and each anchor, with whether the verified log agrees with it."""
        if self.committed is None:
            return None
        anchors = [a | {"verified": a["seq"] <= len(self.log_hashes) and self.log_hashes[a["seq"] - 1] == a["hash"]} for a in self.committed["anchors"]]
        return self.committed | {"anchors": anchors}

    # -- checking the log --

    def load_local_log(self) -> None:
        """Check events.jsonl, the copy of the log this client verified before; set it aside if it no longer verifies."""
        path = self.folder / "events.jsonl"
        if not path.exists():
            return
        lines = path.read_bytes().split(b"\n")[:-1]
        try:
            with self.lock:
                self.take_lines([line + b"\n" for line in lines])
        except LogBroken as exc:
            aside = path.with_name(f"events.jsonl.unverified-{int(time.time())}")
            os.replace(path, aside)
            self.alarm(f"the local copy of the log no longer verifies ({exc}); set aside as {aside.name}")
            self.log_hashes, self.logged, self.extract_at, self.key_at = [], {}, None, None

    def take_lines(self, lines: list[bytes], copy: Any = None) -> None:
        """Verify lines that continue the verified log, check each event against this device's record, and copy each to ``copy``."""
        for line in lines:
            seq = len(self.log_hashes) + 1
            try:
                event, digest = verify_line(line, seq, self.log_hashes[-1] if self.log_hashes else ZERO_HASH)
            except LogBroken as exc:
                raise LogBroken(f"event {seq}: {exc}") from None
            self.log_hashes.append(digest)
            if copy is not None:
                copy.write(line)
            if seq in self.stated:
                self.anchor(seq, self.stated[seq][0], self.stated[seq][1])
            self.check_event(event)

    def check_event(self, event: dict[str, Any]) -> None:
        seq, kind, data = event["seq"], event["kind"], event["data"]
        if kind == "extract.built" and event["file_sha256"] == self.extract and event["county"] == self.county:
            self.extract_at = seq
        if kind == "receipt.key_published" and event["actor"] == "system" and data.get("key_id") == self.key_id:
            self.key_at = seq
        if event["device_id"] != self.device.device_id:
            return
        item = self.by_sha.get(str(data.get("payload_sha256")))
        where = f"event {seq}, hash {self.log_hashes[seq - 1]}"
        if kind == "observation.rejected":
            if item is None:
                self.alarm(f"event {seq} records a refusal of something sent under this device's ID that this device did not sign", where)
            return
        if item is None:
            self.alarm(f"event {seq} ({kind}) is under this device's ID, but this device did not sign it", where)
            return
        payload = item["payload"]
        expected = {"kind": KINDS[payload["type"]], "actor": self.observer, "county": self.county, "board": self.board,
                    "client_time": payload["client_time"], "signature": item["signature"]}  # fmt: skip
        differ = [k for k, v in expected.items() if event[k] != v]
        if data.get("payload") != payload:
            differ.append("payload")
        if data.get("device_seq") != payload["device_seq"]:
            differ.append("device_seq")
        first = self.logged.setdefault(item["payload_sha256"], seq)
        if seq != first and payload["type"] == "device_registration" and self.registered_again:
            self.logged[item["payload_sha256"]] = seq  # the registration sent again: its receipt names this event
        elif seq != first:
            differ.append(f"it is recorded twice (events {first} and {seq})")
        if differ:
            self.alarm(f"event {seq} holds {describe(payload)}, but differs from what the device signed in: {', '.join(differ)}", where)

    def check_log(self) -> bool:
        """Fetch the log's new lines, verify them, and check them; True if the log is consistent with the record."""
        if not self.log_lock.acquire(blocking=False):
            return False
        try:
            with self.lock:
                seen = max([len(self.log_hashes), *self.stated])
            head = self.get_json("/head")
            if head["seq"] < seen:
                said = self.stated.get(seen) or (self.log_hashes[seen - 1], "this client's verified copy of the log")
                self.alarm(f"the server's log ends at event {head['seq']}, but {said[1]} names event {seen}: the server shows a shorter log",
                           f"head {head['seq']} {head['hash']}; event {seen} {said[0]}")
                return False
            start = len(self.log_hashes) + 1
            while start <= head["seq"]:
                end = min(start + LOG_CHUNK - 1, head["seq"])
                body = self.http(f"/observe/{self.token}/log?from={start}&to={end}")
                lines = [line + b"\n" for line in body.split(b"\n")[:-1]]
                with self.lock, open(self.folder / "events.jsonl", "ab") as copy:
                    try:
                        if len(lines) != end - start + 1 or not body.endswith(b"\n"):
                            raise LogBroken(f"asked for events {start} to {end}, the server sent {len(lines)} lines")
                        self.take_lines(lines, copy)
                    except LogBroken as exc:
                        self.alarm(f"the log does not verify: {exc}", f"events {start} to {end}, after hash {self.log_hashes[-1] if self.log_hashes else ZERO_HASH}")
                        return False
                start = end + 1
            with self.lock:
                if head["seq"] and self.log_hashes[head["seq"] - 1] != head["hash"]:
                    self.alarm(f"/head says event {head['seq']} has hash {head['hash'][:16]}..., but the log has another",
                               f"event {head['seq']}: {head['hash']} (/head), {self.log_hashes[head['seq'] - 1]} (the log)")
                return self.reconcile()
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
            self.say(f"Could not check the log now: {exc}")
            return False
        finally:
            self.log_lock.release()

    def reconcile(self) -> bool:
        """After a check: every receipted entry is in the verified log where its receipt says; the extract and key are logged."""
        before = len(self.alarms)
        receipted = [i for i in self.items if self.has_receipt(i) and i["receipt"]["receipt"]["event_seq"] <= len(self.log_hashes)]
        for item in receipted:
            at = item["receipt"]["receipt"]["event_seq"]
            if self.logged.get(item["payload_sha256"]) != at:
                self.alarm(f"{describe(item['payload'])} has a receipt for event {at}, but the log does not hold it there",
                           f"event {at}, hash {item['receipt']['receipt']['event_hash']}")
        if receipted and self.key_at is None:
            self.alarm(f"the receipt key {self.key_id[:16]}... is not published in the log, though it signed receipts", f"receipt key {self.key_id}")
        if self.extract_at is None:
            self.alarm(f"the extract shown ({self.extract[:16]}...) is not recorded in the log as built for {self.county}", f"extract {self.extract}")
        self.check_committed_extract()
        return len(self.alarms) == before

    # -- the record --

    def record(self) -> dict[str, Any]:
        """The record in the corla-observer-record format (docs/RECEIPTS.md), as the page's "Download my record" saves it."""
        with self.lock:
            return {
                "format": "corla-observer-record", "version": 1, "observer": self.observer, "county": self.county,
                "board": self.board, "device_id": self.device.device_id, "public_key": self.device.public_key,
                "algorithm": ALGORITHM, "extract_sha256": self.extract, "list": self.list_label,
                "server_receipt_key": self.server_info,
                "page": {"software": SOFTWARE, "sha256": self.sha256, "entry_mode": "two_tap"},
                "exported_at": utc_now(),
                "client_checks": {
                    "sessions": self.sessions, "receipts_verified": self.receipts_ok,
                    "log_verified_to": [len(self.log_hashes), self.log_hashes[-1] if self.log_hashes else ZERO_HASH],
                    "extract_built_event": self.extract_at, "commitments": self.commitment_summary(),
                    "problems": [a["text"] for a in self.alarms], "alarms": self.alarms, "acknowledged": self.acked,
                },
                "entries": [{k: i[k] for k in ("payload", "signature", "payload_sha256", "receipt", "refused")} for i in self.items],
            }  # fmt: skip

    def save(self) -> Path:
        """Write record.json from what is held, replacing it whole (a crash leaves the old one, never half a file).
        Done after every entry and every answer from the server, so it is always current."""
        path = self.folder / "record.json"
        with self.lock:
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.record(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        return path

    def status(self) -> str:
        with self.lock:
            received = sum(1 for i in self.items if i["receipt"] is not None)
            refused = sum(1 for i in self.items if i["refused"] is not None)
            return (f"{len(self.done)} of {len(self.ballots)} ballots entered; {len(self.items)} signed entries: "
                    f"{received} with receipts ({self.receipts_ok} checked), {len(self.pending())} queued, {refused} refused; "
                    f"log verified to event {len(self.log_hashes)}; {len(self.alarms)} alarm(s)")  # fmt: skip


# ---------------------------------------------------------------------------
# The terminal

def ballot_lines(observer: Observer, ballot: dict[str, Any], sep: str) -> list[str]:
    """The expected votes as the text printout lays them out (corla_observer.extract.render_text): each contest
    right-aligned to the board's longest, so the separators line up, then the choice, its continuation lines under
    it. The names are the server's, shortened for a screen as on the page."""
    width = max((len(vote[0]) for b in observer.ballots for vote in b["votes"]), default=0)
    lines = []
    if ballot.get("redacted"):
        lines.append("    Redacted in the county's CVR: no expected votes.")
    elif ballot.get("missing"):
        lines.append("    Not in the county's CVR: no expected votes.")
    for vote in ballot["votes"]:
        values = vote[3] if len(vote) > 3 and vote[3] else [vote[1]]
        lines.append(f"{vote[0]:>{width}} {sep} {values[0]}")
        lines += [f"{'':>{width}} {' ' * len(sep)} {value}" for value in values[1:]]
    return lines


def show(observer: Observer, ballot: dict[str, Any], write: Callable[[str], None]) -> None:
    """A ballot: its place, its imprinted ID, and the expected votes, the colons lined up."""
    lines = [f"\n---- Ballot {ballot['n']} of {len(observer.ballots)} ({observer.county}, board {observer.board}, position {ballot['position']}) ----",
             f"Imprinted ID: {ballot['id']}"]  # fmt: skip
    if str(ballot["position"]) in observer.done:
        lines.append("    (already entered: a new entry changes it)")
    write("\n".join(lines + ballot_lines(observer, ballot, ":")))


def for_editor(observer: Observer, ballots: list[dict[str, Any]]) -> str:
    """Ballots as the text printout gives them, with "__" to mark in an editor, as observers did in July 2026."""
    blocks = []
    for ballot in ballots:
        head = f" ImprintedId {ballot['id']} (ballot {ballot['n']} of {len(observer.ballots)}, {observer.county} board {observer.board}) "
        blocks.append("\n".join([head.center(80, "-")] + ballot_lines(observer, ballot, "__")))
    return "\n\n".join(blocks) + "\n"


def to_clipboard(text: str) -> None:
    """Put text on the terminal's clipboard with the OSC 52 escape sequence, which most terminals (and tmux with
    set-clipboard on) accept, also over SSH. Nothing is run and nothing is installed."""
    sys.stdout.write("\x1b]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\a")
    sys.stdout.flush()


def run(observer: Observer, read: Callable[[str], str], write: Callable[[str], None], *, background: bool = True,
        check_every: float = 300, copy: Callable[[str], None] = to_clipboard) -> None:
    """The two-tap loop: Enter, Enter for a match; text, confirmed, for a note; ``/`` commands. Sends in the background,
    or at once."""
    wake, stop = threading.Event(), threading.Event()

    def sender() -> None:
        next_check = time.monotonic() + check_every
        while not stop.is_set():
            wake.wait(observer.retry_delay if observer.pending() else 5.0)
            wake.clear()
            observer.flush()
            if time.monotonic() >= next_check:
                observer.check_log()
                next_check = time.monotonic() + check_every

    def send() -> None:
        if background:
            wake.set()
        else:
            observer.flush()

    write(NOTICE_TEXT)
    try:
        read("Press Enter for “I understand, start” > ")
    except (EOFError, KeyboardInterrupt):
        return
    observer.start_two_tap()
    thread = threading.Thread(target=sender, daemon=True)
    if background:
        thread.start()
    send()
    ballot, armed, shown_at = observer.next_ballot(), False, time.monotonic()
    if ballot:
        show(observer, ballot, write)
    while True:
        if ballot is None:
            write(f"\nAll {len(observer.ballots)} ballots have an entry. /edit N changes one; /quit ends.")
        if unacked := observer.alarms[observer.acked :]:
            write(f"*** {len(unacked)} ALARM(S) not acknowledged. Latest: {unacked[-1]['text']}\n"
                  "*** /report prints a statement to send your coordinator and the other observers; /ack acknowledges.")  # fmt: skip
        prompt = "/command > " if ballot is None else "Enter = confirmed match, or type what you heard > " if armed else "Enter = first reading, or type what you heard > "
        try:
            line = read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            break
        elapsed = min(86_400_000, max(0, round((time.monotonic() - shown_at) * 1000)))
        if line in ("?", "help"):  # never a note: "?" or "help" typed at a ballot would otherwise record a mismatch
            line = "/help"
        if line[:1] in ("/", ":"):  # "/" needs no shift key; ":" is still accepted
            command, _, arg = line[1:].strip().partition(" ")
            if command in ("q", "quit"):
                break
            elif command in ("e", "edit") and arg.strip().isdigit():
                found = next((b for b in observer.ballots if b["n"] == int(arg)), None)
                if found is None:
                    write(f"No ballot {arg} on this board.")
                    continue
                ballot, armed, shown_at = found, False, time.monotonic()
                show(observer, ballot, write)
            elif command in ("c", "copy"):
                chosen = observer.ballots if arg.strip() == "all" or ballot is None else [ballot]
                text = for_editor(observer, chosen)
                path = observer.folder / "copy.txt"
                path.write_text(text, encoding="utf-8")
                copy(text)
                write(f"Copied {len(chosen)} ballot(s) to the clipboard, with __ to mark, and wrote them to {path}")
            elif command in ("ref", "refresh"):
                if ballot is not None:
                    show(observer, ballot, write)
            elif command == "check":
                observer.read_commitments()
                write("Log checked: consistent with this record." if observer.check_log() else "Log check: see above.")
            elif command == "ack":
                observer.acknowledge()
                write(f"{observer.acked} alarm(s) acknowledged. They stay in the record.")
            elif command == "report":
                write("\n\n".join(observer.statement(a) for a in observer.alarms) or "No alarms.")
            elif command in ("s", "status"):
                write(observer.status())
            elif command == "save":
                write(f"Saved {observer.save()}")
            else:
                write("At a ballot: Enter at the first reading, Enter again when it matches, or type what you heard\n"
                      "(a mismatch, with that note, recorded once you confirm it).\n"
                      "Commands: /? or /h (this help, with the status), /e N (go to ballot N), /c (copy this ballot,\n"
                      "with __ to mark, to the clipboard and copy.txt; /c all: every ballot), /check (check the log now),\n"
                      "/ref (show this ballot again), /report, /ack, /s (status), /save (record.json is saved after every entry anyway),\n"
                      "/q (quit)")
                write(observer.status())
                if ballot is not None:
                    show(observer, ballot, write)
            continue
        if ballot is None:
            continue
        if not line and not armed:
            observer.sign_next("first_reading", ballot, {"elapsed_ms": elapsed})
            armed = True
        else:
            if line:
                try:
                    answer = read(f'Record a MISMATCH for ballot {ballot["n"]} ({ballot["id"]}), note "{line}"? Enter = yes, anything else = back > ')
                except (EOFError, KeyboardInterrupt):
                    break
                if answer.strip():
                    write("Not recorded.")
                    show(observer, ballot, write)
                    continue
            observer.enter(ballot, not line, line, elapsed)
            ballot, armed, shown_at = observer.next_ballot(), False, time.monotonic()
            if ballot:
                show(observer, ballot, write)
        observer.save()
        send()
        if not observer.reachable and (queued := len(observer.pending())):
            write(f"    (the server can't be reached: {queued} entries queued, to be sent when it can)")
    stop.set()
    wake.set()
    if background:
        thread.join(TIMEOUT + 5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A command-line observer client for corla-observer (two-tap entry).")
    parser.add_argument("link", help="the observer link, https://HOST/PREFIX/observe/TOKEN")
    parser.add_argument("--dir", type=Path, help="the folder for the key, the record and the log copy (default: one named for the link)")
    parser.add_argument("--server-key", help="the server's receipt key from another source: key ID, base64 key, /receipt-key JSON, or a file")
    parser.add_argument("--commitments", help="the published commitments: a GitHub folder URL, or a local folder of SHA256SUMS files")
    parser.add_argument("--check-every", type=float, default=300, help="seconds between checks of the log (default 300)")
    args = parser.parse_args(argv)
    folder = args.dir or Path(f"corla-observe-{sha256_hex(args.link.strip().encode())[:12]}")
    try:
        observer = Observer(args.link, folder, server_key=args.server_key, commitments=args.commitments)
        observer.start()
    except (CannotStart, OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 2
    observer.flush()
    run(observer, input, print, check_every=args.check_every)
    print("Sending what is queued and checking the log once more...")
    if not observer.flush():
        print(f"{len(observer.pending())} entries are still queued; run this again with the same link and folder to send them.")
    if observer.commitments:
        observer.read_commitments()
    observer.check_log()
    print(observer.status())
    path = observer.save()
    anchors = "".join(f" --anchor {a['seq']}:{a['hash']}" for a in (observer.committed or {}).get("anchors", []))
    print(f"Record: {path}\nCheck it: corla-observer check-record {path} {folder / 'events.jsonl'} --server-key {folder / 'server-key.json'}{anchors}")
    return 1 if observer.alarms else 0


if __name__ == "__main__":
    sys.exit(main())
