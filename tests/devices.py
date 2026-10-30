"""A stand-in for an observer's browser in the tests: an ECDSA P-256 device key and signed payloads.

`Device` signs exactly what ``static/signing.js`` signs (docs/RECEIPTS.md),
with a key made by the ``cryptography`` package instead of Web Crypto, and
the signature in Web Crypto's ``r || s`` form.
"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from corla_observer.receipts import DEVICE_ALGORITHM, ZERO_HASH, device_id_for, payload_bytes, payload_sha256

CLIENT_TIME = "2026-10-24T12:00:00.000Z"
EXTRACT = "e" * 64


class Device:
    """One observer's device for one link: its key, sequence number and chain."""

    def __init__(self, observer: str = "observer:ann", county: str = "La Plata", board: str = "1", extract: str = EXTRACT) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.b64encode(raw).decode()
        self.device_id = device_id_for(raw)
        self.observer, self.county, self.board, self.extract = observer, county, board, extract
        self.seq = 0
        self.prev = ZERO_HASH
        #: Every item made, in order, as the page keeps them.
        self.items: list[dict[str, Any]] = []

    def sign(self, payload: dict[str, Any]) -> dict[str, Any]:
        r, s = decode_dss_signature(self.key.sign(payload_bytes(payload), ec.ECDSA(hashes.SHA256())))
        signature = base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()
        return {"payload": payload, "signature": signature}

    def _chain(self, item: dict[str, Any]) -> dict[str, Any]:
        self.prev = payload_sha256(item["payload"])
        self.items.append(item)
        return item

    def registration(self, client_time: str = CLIENT_TIME) -> dict[str, Any]:
        return self._chain(self.sign({
            "v": 1, "type": "device_registration", "observer": self.observer, "device_id": self.device_id,
            "county": self.county, "board": self.board, "public_key": self.public_key, "algorithm": DEVICE_ALGORITHM,
            "client_time": client_time, "device_seq": 0, "prev_payload_sha256": ZERO_HASH,
        }))

    def payload(self, kind: str, position: int, imprinted_id: str, content: dict[str, Any], client_time: str) -> dict[str, Any]:
        self.seq += 1
        return {
            "v": 1, "type": kind, "observer": self.observer, "device_id": self.device_id, "county": self.county,
            "board": self.board, "extract_sha256": self.extract, "position": position, "imprinted_id": imprinted_id,
            "content": content, "client_time": client_time, "device_seq": self.seq, "prev_payload_sha256": self.prev,
        }

    def entry(
        self, position: int, imprinted_id: str, match: bool = True, note: str = "", *, edit: bool = False,
        elapsed_ms: int | None = 1500, client_time: str = CLIENT_TIME,
    ) -> dict[str, Any]:
        content = {"match": match, "note": note, "edit": edit, "elapsed_ms": elapsed_ms}
        return self._chain(self.sign(self.payload("entry", position, imprinted_id, content, client_time)))

    def first_reading(
        self, position: int, imprinted_id: str, *, elapsed_ms: int | None = 800, client_time: str = CLIENT_TIME
    ) -> dict[str, Any]:
        """The first of a match's two taps, on hearing the ballot's first reading."""
        content = {"elapsed_ms": elapsed_ms}
        return self._chain(self.sign(self.payload("first_reading", position, imprinted_id, content, client_time)))

    def other_board(self, position: int, imprinted_id: str, *, client_time: str = CLIENT_TIME) -> dict[str, Any]:
        """One tap, "Skipped: other board", on a boundary ballot this board did not pull."""
        return self._chain(self.sign(self.payload("other_board", position, imprinted_id, {"elapsed_ms": 700}, client_time)))

    def heard(self, position: int, imprinted_id: str, heard: str, *, client_time: str = CLIENT_TIME) -> dict[str, Any]:
        content = {"heard": heard, "elapsed_ms": 2500}
        return self._chain(self.sign(self.payload("heard_id", position, imprinted_id, content, client_time)))

    def contest(
        self, position: int, imprinted_id: str, row: int, rows: int, contest: str, expected: str, *, reading: int = 1,
        outcome: str = "confirmed", heard: str = "", correction: bool = False, client_time: str = CLIENT_TIME,
    ) -> dict[str, Any]:
        """One contest's outcome in one reading, entered contest by contest."""
        content = {
            "contest": contest, "row": row, "rows": rows, "expected": expected, "reading": reading, "outcome": outcome,
            "heard": heard, "correction": correction, "elapsed_ms": 300 * row,
        }
        return self._chain(self.sign(self.payload("contest", position, imprinted_id, content, client_time)))

    def ballot_done(
        self, position: int, imprinted_id: str, rows: int, *, reading: int = 2, unresolved: int = 0, client_time: str = CLIENT_TIME
    ) -> dict[str, Any]:
        content = {"reading": reading, "rows": rows, "unresolved": unresolved, "elapsed_ms": 9000}
        return self._chain(self.sign(self.payload("ballot_done", position, imprinted_id, content, client_time)))

    def entry_mode(self, mode: str, position: int = 1, imprinted_id: str = "101-1-1", *, client_time: str = CLIENT_TIME) -> dict[str, Any]:
        return self._chain(self.sign(self.payload("entry_mode", position, imprinted_id, {"mode": mode}, client_time)))

    def list_switch(self, key: str, name: str, position: int = 1, imprinted_id: str = "101-1-1", *, client_time: str = CLIENT_TIME) -> dict[str, Any]:
        """A switch to another of the county's pull lists, made on the list's extract (``self.extract``)."""
        return self._chain(self.sign(self.payload("list_switch", position, imprinted_id, {"list": key, "name": name}, client_time)))
