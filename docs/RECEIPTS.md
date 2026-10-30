# Signed entries, receipts and observer-held records

Format version 1, 2026-10-24

Each observer's device signs every entry it makes, and keeps every entry with the server's receipt for it. The observer can download that record at any time, and anyone holding it and a copy of the published event log can check that the log holds exactly what the device entered, and nothing under that device that it didn't enter. This document specifies the signed payloads, the receipts and the downloaded record, and how to run the checker. The event log itself is specified in [EVENT_LOG.md](EVENT_LOG.md); its canonical serialization is used here too.

The code is in `corla_observer.receipts` (definitions and cryptography), `corla_observer.signed` (the server's checks and receipts), `corla_observer.observer_record` (the checker), and the observer page's `static/signing.js` (the device's side).

## Device keys

When an observer link is first opened in a browser, the page:

1. makes an ECDSA key pair on the P-256 curve with the Web Crypto API, with the private key marked non-extractable, so that the page's own code can sign with it but can't read it or send it anywhere;
2. keeps the key pair in the browser's IndexedDB, under that link;
3. derives the device ID from the public key: `d-` followed by the first 32 hex digits of the SHA-256 of the public key's 65-byte uncompressed form (`0x04`, then the 32-byte x and y coordinates);
4. signs a registration payload (below) carrying the public key, and sends it to the server before any entry.

Opening the link again in the same browser reuses the key. Another browser, another device, or the same browser after its site data is cleared makes a new key, and so a new device ID. An observer may have several devices, each with its own chain of entries.

The server records the registration as an `observer.device_registered` event (below) and from then on accepts entries from that device only if they verify with the registered key. Because the device ID is derived from the key, no other key can be registered under it.

## Payloads

Everything a device sends is a payload: a JSON object, serialized canonically as specified in [EVENT_LOG.md](EVENT_LOG.md#canonical-serialization) (keys sorted, no whitespace, integers only, UTF-8, characters written as themselves). A payload's **hash** is the SHA-256 of that serialization, as 64 lowercase hex digits.

Every payload has these fields:

| Field | Value |
|---|---|
| `v` | `1`, the payload format version |
| `type` | `device_registration`, `entry`, `first_reading`, `heard_id`, `other_board`, `contest`, `ballot_done`, `entry_mode`, `acknowledged`, `list_switch`, `lookup_contest` or `lookup_done` |
| `observer` | the observer's actor ID, `observer:NAME`, as in the event log |
| `device_id` | the device ID, `d-` and 32 hex digits |
| `county` | the county the link is for, such as `La Plata` |
| `board` | the audit board, as a string, such as `"1"` |
| `client_time` | the device's clock when the entry was made, `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `device_seq` | the device's sequence number: 0 for its registration, then 1, 2, 3, ... with no gaps |
| `prev_payload_sha256` | the hash of the device's previous payload; 64 zeros for the registration |

The sequence numbers and the previous hashes make each device's payloads a chain, like the event log's: removing, inserting, reordering or altering any of them breaks it.

A **registration** (`device_registration`, always `device_seq` 0) also has:

| Field | Value |
|---|---|
| `public_key` | the 65-byte uncompressed P-256 public key, in standard padded base64 |
| `algorithm` | `ECDSA-P256-SHA256` |

An **entry** (`entry`: a match, a note, or a change of an earlier ballot's entry), a **first reading** (`first_reading`: the first of the two taps a match takes, made on hearing the first audit board member read the ballot) and a **heard ID** (`heard_id`: an imprinted ID the observer heard instead of the one expected, which the server records as the board reading a ballot out of order if it is on the board's list, and as an ID not on the list otherwise) also have:

| Field | Value |
|---|---|
| `extract_sha256` | the hash of the extract the page was showing ([EXTRACT.md](EXTRACT.md)): the RLA sample's or a targeted list's; for a looked-up ballot (`lookup_contest`, `lookup_done`), the redacted CVR's |
| `position` | the ballot's position in the county's extract (for a heard ID, the ballot expected) |
| `imprinted_id` | that ballot's imprinted ID |
| `content` | what the observer entered, below |

An entry's `content` is `{"match": true or false, "note": "...", "edit": true or false, "elapsed_ms": N or null}`: whether the ballot matched, what was heard instead in the observer's own words (empty for a plain match), whether this changes an earlier entry for the same ballot, and the time in milliseconds from the ballot being shown to the entry. A first reading's `content` is `{"elapsed_ms": N or null}`, the time from the ballot being shown to the first tap. A heard ID's `content` is `{"heard": "...", "elapsed_ms": N or null}`. An **other board** (`other_board`: one tap, "Skipped: other board", for a boundary ballot that the extract lists on two boards until the county's pull list arrives, and that this board did not pull; docs/EXTRACT.md) has `content` `{"elapsed_ms": N or null}`; it settles the ballot for the observer, as an entry does, without counting as a match or a note. Like the first reading, it was added within payload version 1.

The last three types are for **contest-by-contest entry** (below); they have the same fields as an entry.

A match takes two taps. The page shows each ballot with a **First reading** button; the first tap, when the observer has heard the first board member read the ballot and it matches, is signed and sent as a `first_reading` payload, and the button becomes **Confirmed match**. The second tap, after the second reading, is the match itself: an `entry` with `match` true, whose `elapsed_ms` is still counted from the ballot being shown, so the time from the first reading to the confirmation is the difference of the two `elapsed_ms`. A note typed at any point, before or after the first tap, is an `entry` with `match` false as before. A first reading without a later entry for the ballot is not an entry: the ballot is not done. The first-reading type was added within payload version 1: a page or checker that knows only the other types refuses it as an unknown `type`, and the other types are unchanged. Notes are at most 500 characters and heard IDs at most 64, with no control characters (U+0000 to U+001F and U+007F to U+009F) and no leading or trailing spaces; the page removes these before signing. An entry is a match or has a note.

### Contest by contest

An observer can choose, on their link's page, to enter each ballot contest by contest (the default) or with two taps (`corla_observer.contests`). Three payload types, added within payload version 1 like the first reading, carry it; each has the fields of an entry, with `position` and `imprinted_id` naming the ballot shown:

- **`entry_mode`**: the mode in effect. `content` is `{"mode": "two_tap" or "per_contest"}`. The ballot is the one shown when it was made. The page makes one each time the observer starts from the notice before the first ballot, with the mode chosen there (contest by contest on a device that has never chosen and whose observer has no mode recorded for the board; otherwise the one in effect), and one for each change of mode afterwards; the choice is kept on the device and can be changed at any time. Records made before the page recorded the mode at the start, when two taps was the default and an observer who never chose made none, are read as two taps where nothing else shows the mode.
- **`acknowledged`**: the observer tapped "I understand, start" on the notice shown before their page (the keep-out-of-sight rule and the essentials), the first time the link is opened on the device and again at the start of each browser session and each day. `content` is `{"notice": "out_of_sight_1"}`, naming the notice's text by version. The ballot is the one the page opens on.
- **`contest`**: one contest's outcome in one reading. `content` is `{"contest": ..., "row": N, "rows": N, "expected": ..., "reading": 1 or 2, "outcome": ..., "heard": ..., "correction": true or false, "elapsed_ms": N or null}`: the contest's name as the extract gives it, its row on the ballot (1 to `rows`) and the ballot's number of contests (at most 500), the expected choice shown, which of the two readings, the outcome (`confirmed`, `heard_instead` or `unclear`, the last meaning not heard clearly), what was heard (only for `heard_instead`, at most 200 characters; empty otherwise), whether it changes an earlier outcome for the same row and reading, and the time from the ballot being shown. The contest name and expected choice are at most 500 characters. Text follows the same rules as a note: no control characters, no leading or trailing spaces.
- **`list_switch`**: the page is showing another of the county's pull lists ([EXTRACT.md](EXTRACT.md#pull-lists)): the RLA sample, a targeted list or the lookup of ballots from boxes, chosen with the list in the page's header. `content` is `{"list": "rla", "lookup" or "t-" and 12 hex digits, "name": ...}`, the list's key and how the page named it ("RLA sample", "Targeted list 1", at most 120 characters); `extract_sha256` is that list's extract (the redacted CVR's hash for the lookup) and the ballot is the one the page opens on. It is signed the first time the device shows a list other than the one it showed last.
- **`lookup_contest`** and **`lookup_done`**: the same as `contest` and `ballot_done`, for a ballot the observer looked up by imprinted ID after the board pulled it from a storage box ([TARGETING.md](TARGETING.md), "Two kinds of targeted observation"). `extract_sha256` is the hash of the county's redacted CVR the expected votes were read from, `position` is the lookup's number on the device (1, 2, ...), and `imprinted_id` the ballot's. They are recorded apart from every pull list and never count toward one.
- **`ballot_done`**: the observer marked the ballot done. `content` is `{"reading": 1 or 2, "rows": N, "unresolved": N, "elapsed_ms": N or null}`: the reading the ballot was in, its number of contests (0 for a redacted ballot), and how many rows the device counted as unresolved (`unresolved` is at most `rows`).

Nothing is overwritten: a correction is a new payload, and the latest outcome for a row and reading is the one that counts. A ballot is done when it is marked done or when every row has an outcome in the second reading. A row is unresolved when it has no outcome in the second reading, when its latest outcome is `unclear`, or when the two readings were heard differently (a row not heard clearly in one reading and confirmed in the other is not a difference). A 60-contest ballot read twice makes 120 or more payloads: each is signed, chained and kept on the device as it is made, like any other, and gets its own receipt, but the page sends them in batches (every few seconds, every 20 waiting, and on leaving a ballot), up to 50 in one request.

Example: an entry for the second ballot of board 1, with a note.

```json
{"board":"1","client_time":"2026-11-17T16:05:41.950Z","content":{"edit":false,"elapsed_ms":4210,"match":false,"note":"heard “No” on 2"},"county":"La Plata","device_id":"d-6f1c0e3a9b2d4c5e8f7a6b5c4d3e2f10","device_seq":2,"extract_sha256":"9b1e...","imprinted_id":"101-1-2","observer":"observer:ann","position":2,"prev_payload_sha256":"3c4d...","type":"entry","v":1}
```

(Hashes are shortened here; in a payload they are always 64 hex digits.)

## Signatures

The device signs the payload's canonical bytes with ECDSA P-256 and SHA-256, as Web Crypto does: the signature is the 64 bytes `r || s` (each a 32-byte big-endian integer), in standard padded base64. It is sent with the payload as `{"payload": {...}, "signature": "..."}`.

Libraries that use the DER form of ECDSA signatures need `r` and `s` converted. With Python's `cryptography` package:

```python
import base64, json
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

def verify(public_key_b64, payload, signature_b64):
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(public_key_b64))
    sig = base64.b64decode(signature_b64)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    key.verify(encode_dss_signature(int.from_bytes(sig[:32]), int.from_bytes(sig[32:])), data, ec.ECDSA(hashes.SHA256()))
```

`verify` raises `InvalidSignature` if the signature doesn't verify.

## What the server records

The server checks each item: that it is signed (`{payload, signature}`), that the payload has exactly the fields above with valid values, that it names the link's observer, county and board, that its device is registered under that observer (or, for a registration, that it is signed by the key it registers), and that the signature verifies. It then records:

| Payload | Event |
|---|---|
| registration | `observer.device_registered`, with `data.public_key` and `data.algorithm` |
| entry | `observation.entry` |
| other board | `observation.other_board` |
| first reading | `observation.first_reading` |
| heard ID | `observation.out_of_order` or `observation.id_not_on_list` |
| contest | `observation.contest` |
| ballot done | `observation.ballot_done` |
| entry mode | `observation.entry_mode` |
| acknowledged notice | `observation.acknowledged` |
| list switch | `observation.list_switched` |
| looked-up ballot's contest, done | `observation.lookup_contest`, `observation.lookup_done` |

Each of these events has the device's signature in its `signature` field, the device ID in `device_id`, the payload's `client_time` in `client_time`, and in `data` the whole payload (`payload`), its hash (`payload_sha256`) and its `device_seq`, alongside the plain fields the dashboard and statistics count: `extract_sha256` (every type but a registration; the list the entry belongs to is that extract's, so entries on a targeted list never mix with the RLA sample's), `position`, `imprinted_id`, `match`, `note`, `edit`, `elapsed_ms` for an entry; `position`, `imprinted_id`, `elapsed_ms` for a first reading; `expected_position`, `heard` and what was found for a heard ID; for the contest-by-contest types, `position`, `imprinted_id` and every field of the payload's `content`). Anyone can check those plain fields against the signed payload; the checker does.

A payload sent again (the device resends its queue when a reply is lost) is recognized by its device and sequence number and not recorded again. A different payload under a sequence number already recorded for that device is refused.

Anything refused (unsigned, malformed, for another link, from an unregistered device, with a signature that doesn't verify, or conflicting with what the device already sent) is answered with an error and a short code (`unsigned`, `invalid`, `wrong_link`, `unknown_device`, `bad_signature`, `conflict`), and recorded as an `observation.rejected` event with the code, the reason, the device ID and sequence number where they could be read, and the payload's hash (not the payload).

## Receipts

The server answers every payload it records, or has already recorded, with a receipt: `{"receipt": {...}, "signature": "..."}`, where the receipt's body has:

| Field | Value |
|---|---|
| `v` | `1` |
| `type` | `receipt` |
| `key_id` | the SHA-256 (hex) of the server's 32-byte receipt public key |
| `event_seq`, `event_hash`, `event_kind` | the event that recorded the payload: its position in the log, its hash and its kind |
| `payload_sha256` | the payload's hash |
| `server_time` | when the server received it (the event's `server_time`) |
| `head_seq`, `head_hash` | the log's head when the receipt was made |

and `signature` is the server's Ed25519 signature over the body's canonical serialization, in standard padded base64.

A receipt commits the server: the event at `event_seq` has `event_hash` and holds this payload, and the log's event at `head_seq` has `head_hash`, so a log published later must agree with both. The page shows each ballot's **receipt code**, the event's sequence number and the first six hex digits of its hash (such as `412-3f9a1c`), which anyone can look up in the published log.

The receipt key is an Ed25519 key that the server creates on its first run, next to the server secret (`receipt-key.pem`, mode 0600, outside the repository and the workspace; `corla-observer serve --receipt-key-file` names another). Its public half is published:

- at `/receipt-key` (JSON: `algorithm`, `public_key` in base64, `key_id`) and `/receipt-key.pem`;
- on the public dashboard, by its `key_id`;
- in the event log, as a `receipt.key_published` event before its first receipt.

An observer who wants the receipts checked independently of the server should save the key from `/receipt-key` at the start of the audit, or compare its `key_id` with one published elsewhere.

## The observer-held record

The page keeps every payload, its signature, and its receipt (or the server's reason for refusing it) in IndexedDB as soon as it is made, before it is sent. Entries made while the network is down are signed and kept the same way and queued; they get their receipts when they are sent. The page shows "Queued" until then.

**Download my record**, at the foot of the page and offered when the board's last ballot has an entry, saves the record as one JSON file:

```json
{
 "format": "corla-observer-record",
 "version": 1,
 "observer": "observer:ann",
 "county": "La Plata",
 "board": "1",
 "device_id": "d-...",
 "public_key": "BP...",
 "algorithm": "ECDSA-P256-SHA256",
 "extract_sha256": "...",
 "server_receipt_key": {"algorithm": "Ed25519", "public_key": "...", "key_id": "..."},
 "page": {"software": "0.1.0", "observe_js": "sha384-...", "signing_js": "sha384-...", "percontest_js": "sha384-...", "contestlogic_js": "sha384-...", "entry_mode": "per_contest"},
 "exported_at": "2026-11-17T21:00:00.000Z",
 "entries": [
  {"payload": {...}, "signature": "...", "payload_sha256": "...", "receipt": {"receipt": {...}, "signature": "..."}, "refused": null}
 ]
}
```

`receipt` is `null` for an entry not yet acknowledged, and `refused` is `{"code": ..., "error": ...}` for one the server refused. `page` records the software version, the subresource integrity hashes of the page's scripts, and the entry mode the page was in. The **CSV** button saves the same entries as a spreadsheet, one row each: `device_seq`, `type`, `position`, `imprinted_id`, `match`, `note_or_heard` (`first reading` for a first reading), `edit`, `client_time`, `payload_sha256`, `receipt` (the code), `event_seq`, `server_time`, `status` (received, queued, or refused with the reason), and for contest-by-contest entries `contest` (its row and name), `reading` and `outcome`, with any cell that could be read as a formula prefixed with `'`.

The record is the device's own: it is only on the device until the observer downloads it, and it holds nothing the observer didn't enter, apart from the server's receipts.

## Checking a record

With this software installed (from a checkout of this repository, `uv sync`), anyone with the record and a copy of the published `events.jsonl` can run:

```
corla-observer check-record RECORD.json EVENTS.jsonl --server-key KEY [--anchor SEQ:HASH ...] [--json]
```

KEY is the server's receipt key from a source other than the log: a file holding the JSON from `/receipt-key`, the PEM from `/receipt-key.pem` or the base64 key, or the base64 key itself. Without `--server-key` the checker uses the key the log itself publishes, and says that this is not an independent check. `--anchor` adds a published chain head (such as the log head in an evidence commitment, [TIMESTAMPING.md](TIMESTAMPING.md)) that the log must agree with.

It reports:

- **the log's chain:** whether `events.jsonl` verifies ([EVENT_LOG.md](EVENT_LOG.md#verifying-a-log)), and whether it agrees with every anchor: each receipt's event and chain head, and each `--anchor`. A log cut off before a receipted event, or rewritten after a receipt was given, fails here;
- **the record itself:** each payload's hash and device signature, and the device's chain (sequence numbers from 0 with no gaps, each payload carrying the previous payload's hash);
- **receipts:** whether each is signed by the server key, is for this payload, and points to an event in the log that holds it;
- **entries missing from the log or differing from it:** a payload in the record that no event holds (refused by the server, sent without a receipt, or never sent, each said so), an event with the same sequence number but a different payload, and an event whose plain fields or `client_time` disagree with its signed payload;
- **entries in the log under this observer and device that the device did not make:** events under them whose signature doesn't verify with the device's key, or that have none. Events that the device did sign but that the record lacks, because it was downloaded before them, are noted but not counted as problems;
- **entries by pull list:** how many of the record's entries are for each list, as the log's `extract.built` events name the extracts they refer to ("RLA sample", "Targeted: NAME").

It exits 0 when it finds no problem, 1 when it finds any, and 2 when the files can't be read.

## Limits

- **The page's code comes from the server.** The JavaScript that makes the key, signs and keeps the record is served by the same server that receives the entries, so a server that altered it could have the page sign something other than what the observer entered, or send the private key's signatures elsewhere. The scripts are published in this repository, versioned, and served with subresource integrity hashes, which the page shows in the downloaded record (`page`); anyone can compare those hashes with the published files to check what code an observer ran. An observer who wants more independence can work on paper, or on paper and a screen together.
- **The key is only as safe as the browser.** Anything that can run code in the page's origin on the device can sign with the key, though not copy it.
- **The device clock is the device's.** `client_time` is what the device says; the server's `server_time` and the receipts' chain positions are the times that others can rely on.
- **A record shows only what one device did.** It can't show that another device, or an entry made under another observer link, is genuine; each device's record shows its own.
- **The checker checks consistency, not truth.** A record that checks shows that the log holds exactly what the device signed. Whether the observer heard correctly is a separate question, answered by more than one observer per board, paper, and the audit's own records.
