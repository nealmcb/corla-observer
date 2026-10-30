"""Device-signed observer entries and the server's receipts: the definitions and the cryptography.

Specified in docs/RECEIPTS.md. In short:

**Payloads.** Everything an observer's device sends is a *payload*, a JSON
object signed on the device. Its bytes are the canonical serialization of
docs/EVENT_LOG.md (`corla_observer.events.canonical_json`), and its hash is
the SHA-256 of those bytes. A device's payloads form a chain: each has the
next ``device_seq`` (0 for the device's registration, then 1, 2, ...) and
carries the hash of the one before it (``prev_payload_sha256``, 64 zeros for
the registration). `REGISTRATION_FIELDS` and `ENTRY_FIELDS` list their
fields; `check_payload` checks one.

**Device keys.** The browser makes an ECDSA P-256 key pair with Web Crypto,
keeps the private key non-extractable, and sends its public key, as the
65-byte uncompressed point in standard padded base64, in its registration
payload (signed with that key). The device ID is derived from the key
(`device_id_for`), so it can't be claimed by another key. Signatures are
Web Crypto's: ECDSA with SHA-256 over the payload's bytes, as the 64-byte
``r || s``, in standard padded base64 (`verify_device_signature`).

**Receipts.** For every payload it records, the server returns a receipt
(`ReceiptSigner.issue`): the event's sequence number, hash and kind, the
payload's hash, the server's receive time and the chain head after it,
signed with the server's Ed25519 receipt key. The key is kept in a PEM file
outside the repository and the workspace (`load_receipt_key`), and its
public half is published at ``/receipt-key`` and in the event log
(``receipt.key_published``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from corla_observer.events import Event, JSONValue, canonical_json

PAYLOAD_VERSION = 1
RECEIPT_VERSION = 1
DEVICE_ALGORITHM = "ECDSA-P256-SHA256"
RECEIPT_ALGORITHM = "Ed25519"
ZERO_HASH = "0" * 64

#: Fields of a device's registration payload (``type`` ``device_registration``).
REGISTRATION_FIELDS = frozenset(
    {"v", "type", "observer", "device_id", "county", "board", "public_key", "algorithm",
     "client_time", "device_seq", "prev_payload_sha256"}
)
#: Fields of every other payload (``type`` ``entry``, ``first_reading``, ``heard_id``, ``other_board``,
#: ``contest``, ``ballot_done`` or ``entry_mode``).
ENTRY_FIELDS = frozenset(
    {"v", "type", "observer", "device_id", "county", "board", "extract_sha256", "position",
     "imprinted_id", "content", "client_time", "device_seq", "prev_payload_sha256"}
)
#: The ``content`` of each entry type.
CONTENT_FIELDS = {
    "entry": frozenset({"match", "note", "edit", "elapsed_ms"}),
    "first_reading": frozenset({"elapsed_ms"}),
    "heard_id": frozenset({"heard", "elapsed_ms"}),
    "other_board": frozenset({"elapsed_ms"}),
    # Contest-by-contest entry (docs/RECEIPTS.md): one contest's outcome in one reading,
    # the ballot marked done, and the observer's choice of entry mode.
    "contest": frozenset({"contest", "row", "rows", "expected", "reading", "outcome", "heard", "correction", "elapsed_ms"}),
    "ballot_done": frozenset({"reading", "rows", "unresolved", "elapsed_ms"}),
    # A ballot picked from a storage box and looked up by imprinted ID (docs/TARGETING.md): the same fields,
    # recorded apart from every pull list; extract_sha256 is then the redacted CVR's hash.
    "lookup_contest": frozenset({"contest", "row", "rows", "expected", "reading", "outcome", "heard", "correction", "elapsed_ms"}),
    "lookup_done": frozenset({"reading", "rows", "unresolved", "elapsed_ms"}),
    "entry_mode": frozenset({"mode"}),
    # The observer acknowledged a notice shown before their page (the keep-out-of-sight rule).
    "acknowledged": frozenset({"notice"}),
    # The observer switched to another of the county's pull lists: the RLA sample or a targeted list.
    "list_switch": frozenset({"list", "name"}),
}
PAYLOAD_TYPES = (
    "device_registration", "entry", "first_reading", "heard_id", "other_board", "contest", "ballot_done", "entry_mode",
    "acknowledged", "list_switch", "lookup_contest", "lookup_done",
)  # fmt: skip
MAX_NOTE = 500
MAX_HEARD = 64
#: Longest choice heard instead in one contest, and longest contest name or expected choice.
MAX_HEARD_CHOICE = 200
MAX_CONTEST_TEXT = 500
#: Most contests on one ballot.
MAX_ROWS = 500
#: A contest's outcome in one reading, and the entry modes.
OUTCOMES = ("confirmed", "heard_instead", "unclear")
ENTRY_MODES = ("two_tap", "per_contest")
#: The notices an observer acknowledges: the keep-out-of-sight rule and essentials, shown before their page, version 1.
NOTICES = ("out_of_sight_1",)

#: A pull list's key (`corla_observer.pulllists`): the RLA sample's, or a targeted list's.
_LIST_RE = re.compile(r"^(rla|lookup|t-[0-9a-f]{12})$")
#: Longest pull list label in a switch.
MAX_LIST_LABEL = 120
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_DEVICE_RE = re.compile(r"^d-[0-9a-f]{32}$")
_ACTOR_RE = re.compile(r"^observer:[A-Za-z0-9._-]{1,64}$")
_B64_RE = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class PayloadError(ValueError):
    """A payload, signature or receipt is unacceptable; ``code`` says why, briefly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Encoding


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: object, what: str = "value") -> bytes:
    """Decode standard padded base64 strictly."""
    if not isinstance(text, str) or not text or len(text) > 8192 or not _B64_RE.match(text):
        raise PayloadError("invalid", f"{what} is not standard padded base64")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise PayloadError("invalid", f"{what} is not standard padded base64") from None


def payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """The bytes a device signs: the payload's canonical serialization."""
    try:
        return canonical_json(payload)
    except (TypeError, ValueError):
        raise PayloadError("invalid", "the payload is not plain JSON") from None


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(payload_bytes(payload)).hexdigest()


def device_id_for(public_key: bytes) -> str:
    """A device's ID, from its public key: ``d-`` and the first 32 hex digits of the key's SHA-256."""
    return "d-" + hashlib.sha256(public_key).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Payloads


def _is_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def check_payload(payload: object) -> dict[str, Any]:
    """Check a payload's structure (not its signature); return it.

    Raises:
        PayloadError: naming the first problem.
    """

    def need(cond: bool, what: str) -> None:
        if not cond:
            raise PayloadError("invalid", f"invalid payload: {what}")

    need(isinstance(payload, dict), "not an object")
    assert isinstance(payload, dict)
    need(payload.get("v") == PAYLOAD_VERSION and not isinstance(payload.get("v"), bool), f"v must be {PAYLOAD_VERSION}")
    kind = payload.get("type")
    need(isinstance(kind, str) and kind in PAYLOAD_TYPES, "type")
    assert isinstance(kind, str)
    fields = REGISTRATION_FIELDS if kind == "device_registration" else ENTRY_FIELDS
    need(set(payload) == fields, f"fields must be {', '.join(sorted(fields))}")
    need(isinstance(payload["observer"], str) and bool(_ACTOR_RE.match(payload["observer"])), "observer")
    need(isinstance(payload["device_id"], str) and bool(_DEVICE_RE.match(payload["device_id"])), "device_id")
    for name in ("county", "board"):
        value = payload[name]
        need(isinstance(value, str) and 0 < len(value) <= 128 and not _CONTROL_RE.search(value), name)
    need(isinstance(payload["client_time"], str) and bool(_TIME_RE.match(payload["client_time"])), "client_time")
    need(isinstance(payload["prev_payload_sha256"], str) and bool(_HASH_RE.match(payload["prev_payload_sha256"])), "prev_payload_sha256")
    if kind == "device_registration":
        need(payload["device_seq"] == 0 and _is_int(payload["device_seq"], 0, 0), "device_seq of a registration is 0")
        need(payload["prev_payload_sha256"] == ZERO_HASH, "prev_payload_sha256 of a registration is 64 zeros")
        need(payload["algorithm"] == DEVICE_ALGORITHM, f"algorithm must be {DEVICE_ALGORITHM}")
        key = b64decode(payload["public_key"], "public_key")
        public_key(key)
        need(payload["device_id"] == device_id_for(key), "device_id is not the one derived from public_key")
        return payload
    need(_is_int(payload["device_seq"], 1, 2**53 - 1), "device_seq")
    need(isinstance(payload["extract_sha256"], str) and bool(_HASH_RE.match(payload["extract_sha256"])), "extract_sha256")
    need(_is_int(payload["position"], 1, 100_000), "position")
    imprinted = payload["imprinted_id"]
    need(isinstance(imprinted, str) and 0 < len(imprinted) <= 64, "imprinted_id")
    content = payload["content"]
    need(isinstance(content, dict) and set(content) == CONTENT_FIELDS[kind], f"content fields must be {', '.join(sorted(CONTENT_FIELDS[kind]))}")
    assert isinstance(content, dict)
    elapsed = content.get("elapsed_ms")
    need(elapsed is None or _is_int(elapsed, 0, 86_400_000), "elapsed_ms")

    def text(name: str, longest: int, *, empty: bool) -> str:
        value = content[name]
        need(isinstance(value, str) and len(value) <= longest and not _CONTROL_RE.search(value), name)
        need(empty or bool(value.strip()), f"{name} is empty")
        need(value == value.strip(), f"{name} has leading or trailing spaces")
        return str(value)

    if kind == "entry":
        need(isinstance(content["match"], bool), "match")
        need(isinstance(content["edit"], bool), "edit")
        note = content["note"]
        need(isinstance(note, str) and len(note) <= MAX_NOTE and not _CONTROL_RE.search(note), "note")
        need(content["match"] or bool(note.strip()), "an entry is a match or a note")
    elif kind == "heard_id":
        heard = content["heard"]
        need(isinstance(heard, str) and bool(heard.strip()) and len(heard) <= MAX_HEARD and not _CONTROL_RE.search(heard), "heard")
    elif kind in ("contest", "lookup_contest"):
        text("contest", MAX_CONTEST_TEXT, empty=False)
        text("expected", MAX_CONTEST_TEXT, empty=True)
        need(_is_int(content["rows"], 1, MAX_ROWS), "rows")
        need(_is_int(content["row"], 1, content["rows"]), "row is 1 to rows")
        need(content["reading"] in (1, 2) and not isinstance(content["reading"], bool), "reading is 1 or 2")
        need(content["outcome"] in OUTCOMES, f"outcome is one of {', '.join(OUTCOMES)}")
        heard_text = text("heard", MAX_HEARD_CHOICE, empty=content["outcome"] != "heard_instead")
        need(content["outcome"] == "heard_instead" or not heard_text, "heard is empty unless the outcome is heard_instead")
        need(isinstance(content["correction"], bool), "correction")
    elif kind in ("ballot_done", "lookup_done"):
        need(content["reading"] in (1, 2) and not isinstance(content["reading"], bool), "reading is 1 or 2")
        need(_is_int(content["rows"], 0, MAX_ROWS), "rows")
        need(_is_int(content["unresolved"], 0, content["rows"]), "unresolved is 0 to rows")
    elif kind == "entry_mode":
        need(content["mode"] in ENTRY_MODES, f"mode is one of {', '.join(ENTRY_MODES)}")
    elif kind == "acknowledged":
        need(content["notice"] in NOTICES, f"notice is one of {', '.join(NOTICES)}")
    elif kind == "list_switch":
        need(isinstance(content["list"], str) and bool(_LIST_RE.match(content["list"])), "list")
        text("name", MAX_LIST_LABEL, empty=False)
    return payload


# ---------------------------------------------------------------------------
# Device signatures


def public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    """A P-256 public key from its 65-byte uncompressed point."""
    if len(raw) != 65 or raw[0] != 4:
        raise PayloadError("invalid", "public_key must be a 65-byte uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise PayloadError("invalid", "public_key is not a point on P-256") from None


def verify_device_signature(public_key_b64: str, payload: Mapping[str, Any], signature_b64: object) -> bool:
    """Whether ``signature_b64`` is the device's ECDSA P-256 SHA-256 signature (``r || s``) of the payload."""
    try:
        key = public_key(b64decode(public_key_b64, "public_key"))
        signature = b64decode(signature_b64, "signature")
    except PayloadError:
        return False
    if len(signature) != 64:
        return False
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    try:
        key.verify(encode_dss_signature(r, s), payload_bytes(payload), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, PayloadError):
        return False
    return True


# ---------------------------------------------------------------------------
# Receipts

#: Fields of a receipt's signed body.
RECEIPT_FIELDS = frozenset(
    {"v", "type", "key_id", "event_seq", "event_hash", "event_kind", "payload_sha256", "server_time", "head_seq", "head_hash"}
)


def key_id(public_raw: bytes) -> str:
    """A receipt key's ID: the SHA-256 of its 32-byte raw public key."""
    return hashlib.sha256(public_raw).hexdigest()


class ReceiptSigner:
    """The server's Ed25519 receipt key."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        self.public_raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = key_id(self.public_raw)

    @classmethod
    def generate(cls) -> ReceiptSigner:
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_b64(self) -> str:
        return b64encode(self.public_raw)

    @property
    def public_pem(self) -> str:
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    def public_info(self) -> dict[str, JSONValue]:
        """What ``/receipt-key`` serves and ``receipt.key_published`` records."""
        return {"algorithm": RECEIPT_ALGORITHM, "public_key": self.public_b64, "key_id": self.key_id}

    def issue(self, event: Event, head: tuple[int, str], payload_hash: str) -> dict[str, JSONValue]:
        """The receipt for an event that recorded a payload: ``{"receipt": body, "signature": base64}``."""
        body: dict[str, JSONValue] = {
            "v": RECEIPT_VERSION,
            "type": "receipt",
            "key_id": self.key_id,
            "event_seq": event.seq,
            "event_hash": event.hash,
            "event_kind": event.kind,
            "payload_sha256": payload_hash,
            "server_time": event.server_time,
            "head_seq": head[0],
            "head_hash": head[1],
        }
        return {"receipt": body, "signature": b64encode(self._key.sign(canonical_json(body)))}


def receipt_code(receipt: Mapping[str, Any]) -> str:
    """A short code for a receipt, shown on the page: the event's seq and the start of its hash (``412-3f9a1c``)."""
    body = receipt.get("receipt", receipt)
    return f"{body.get('event_seq')}-{str(body.get('event_hash', ''))[:6]}"


def verify_receipt(receipt: Mapping[str, Any], server_public_raw: bytes) -> bool:
    """Whether a receipt (``{"receipt": body, "signature": ...}``) is signed by the server key given."""
    body = receipt.get("receipt")
    if not isinstance(body, dict) or set(body) != RECEIPT_FIELDS or body.get("key_id") != key_id(server_public_raw):
        return False
    try:
        signature = b64decode(receipt.get("signature"), "signature")
        Ed25519PublicKey.from_public_bytes(server_public_raw).verify(signature, canonical_json(body))
    except (PayloadError, InvalidSignature, ValueError, TypeError):
        return False
    return True


def parse_server_public_key(text: str) -> bytes:
    """A receipt key's raw 32 bytes, from its base64, its PEM, or the JSON served at ``/receipt-key``.

    The server's own private key file (``receipt-key.pem``, a PKCS 8 private
    key PEM) is accepted too: its public key is derived from it. (Checking a
    record against the server's own key file is a check of the server
    against itself; the independent check uses the key published at
    ``/receipt-key`` or in the log, from another source.)

    Raises:
        PayloadError: if the text is none of these.
    """
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            raise PayloadError("invalid", "the server key JSON does not parse") from None
        text = str(data.get("public_key", "")) if isinstance(data, dict) else ""
    if text.startswith("-----BEGIN") and "PRIVATE KEY-----" in text:
        try:
            private = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        except (ValueError, TypeError):
            raise PayloadError("invalid", "the server key PEM is a private key that does not load (or is encrypted)") from None
        if not isinstance(private, Ed25519PrivateKey):
            raise PayloadError("invalid", "the server key is not an Ed25519 key")
        return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_public_key(text.encode("ascii"))
        except ValueError:
            raise PayloadError("invalid", "the server key PEM does not load") from None
        if not isinstance(key, Ed25519PublicKey):
            raise PayloadError("invalid", "the server key is not an Ed25519 key")
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    raw = b64decode(text, "server key")
    if len(raw) != 32:
        raise PayloadError("invalid", "an Ed25519 public key is 32 bytes")
    return raw


# ---------------------------------------------------------------------------
# The receipt key file

RECEIPT_KEY_ENV = "CORLA_OBSERVER_RECEIPT_KEY_FILE"
RECEIPT_KEY_NAME = "receipt-key.pem"


class ReceiptKeyError(ValueError):
    """The receipt key file is unusable."""


def default_receipt_key_path(secret_path: Path | str) -> Path:
    """``$CORLA_OBSERVER_RECEIPT_KEY_FILE``, else ``receipt-key.pem`` next to the server secret."""
    env = os.environ.get(RECEIPT_KEY_ENV)
    if env:
        return Path(env)
    return Path(secret_path).with_name(RECEIPT_KEY_NAME)


def load_receipt_key(path: Path | str, *, workspace: Path | str | None = None) -> ReceiptSigner:
    """Read the receipt key (PKCS #8 PEM), creating it with mode 0600 if it doesn't exist.

    Raises:
        ReceiptKeyError: if the file is inside ``workspace``, readable by
            others, or not an Ed25519 private key.
    """
    path = Path(path)
    if workspace is not None:
        resolved, root = path.resolve(), Path(workspace).resolve()
        if resolved == root or root in resolved.parents:
            raise ReceiptKeyError("the receipt key must be kept outside the workspace")
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
            f.flush()
            os.fsync(f.fileno())
    if path.stat().st_mode & 0o077:
        raise ReceiptKeyError(f"the receipt key {path} must not be readable by others (chmod 600)")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError):
        raise ReceiptKeyError(f"the receipt key {path} is not a PEM private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ReceiptKeyError(f"the receipt key {path} is not an Ed25519 key")
    return ReceiptSigner(key)
