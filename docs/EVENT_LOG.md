# Event log format

Format version 1, 2026-10-23

Everything the tool does or records is an event in an append-only log: every upload, fetch, check result, seed entry, timestamp commitment, login, and every observer's match tap, note, edit or undo. Each event carries the SHA-256 hash of the one before it, so changing, removing, inserting or reordering any event breaks the chain.

The log is published as a JSONL file, `events.jsonl`, with one event per line. This document specifies that file precisely enough for anyone to verify a copy of it independently, in any language, without this project's software. A complete verifier in Python is given below, and the worked example at the end lets an implementation in another language be checked byte for byte.

## Events

Every event has exactly these 15 fields. Optional fields are always present, with the value `null` when they don't apply, so every event has the same shape.

| Field | Type | Meaning and constraints |
|---|---|---|
| `seq` | integer | Position in the log: 1 for the first event, then 2, 3, ... with no gaps. |
| `server_time` | string | When the server received or created the event, in UTC: `YYYY-MM-DDTHH:MM:SS.mmmZ`, always with exactly three digits of milliseconds and the letter `Z`. Sub-millisecond time is truncated. |
| `client_time` | string or null | The time on the client's device when the entry was made, in the same format, or `null`. For entries queued on a device while offline, this is when the entry was made and `server_time` is when it arrived. |
| `actor` | string | Who is responsible: `system`, `coordinator:<id>` or `observer:<id>`, where `<id>` is 1 to 64 of the characters `A-Z a-z 0-9 . _ -`. |
| `kind` | string | What happened, as a dotted lowercase name such as `file.received` or `observation.entered`: two or more parts separated by dots, each a lowercase letter followed by lowercase letters, digits or underscores, 64 characters at most. |
| `county` | string or null | The county the event concerns. |
| `board` | string or null | The audit board the event concerns. |
| `session_id` | string or null | The client's session ID: 1 to 128 of `A-Z a-z 0-9 . _ : -`. |
| `device_id` | string or null | The client's device ID, with the same characters as `session_id`. |
| `file_sha256` | string or null | The SHA-256 (64 lowercase hex digits) of the file the event concerns. Files are stored under this hash, and their original names, if any, appear only in `data`. |
| `signature` | string or null | A signature by the observer's device over the entry's payload, in standard padded base64, or `null`. The scheme (ECDSA P-256 with SHA-256 over the payload's canonical serialization, which the event carries in `data.payload`) is specified in [RECEIPTS.md](RECEIPTS.md). |
| `data` | object | The event's details, such as a file's name and size, or an observer's match and note. `{}` if there are none. |
| `v` | integer | The event format version: always `1` for the format specified here. An event with any other value, or without `v`, is not a version 1 event and fails verification. A later format, if there is one, will use a new value. |
| `prev_hash` | string | The previous event's `hash`, or 64 zeros (`0000...0000`) for the first event. |
| `hash` | string | The SHA-256, as 64 lowercase hex digits, of the event's hash input (below). |

`county` and `board` are 1 to 128 characters with no control characters. A value inside `data` is `null`, `true`, `false`, an integer, a string, an array or an object, nested to any depth, with these restrictions:

- **No floating-point numbers,** and no exponents or decimal points in numbers at all. A quantity that needs a fraction is written as a string or scaled to an integer.
- **Integers** lie between -(2^53 - 1) and 2^53 - 1, the range that every common JSON implementation, including JavaScript's, represents exactly. The same applies to `seq`.
- **Object keys** are 1 to 256 printable ASCII characters (U+0020 to U+007E), so that every language sorts them the same way.
- **Strings** are valid Unicode (no unpaired surrogates).

An event's hash input is at most 65,536 bytes.

### Observers' events

An observer's device signs each entry it sends ([RECEIPTS.md](RECEIPTS.md)), and the server records it as one event, with the signed payload in `data.payload`, its hash in `data.payload_sha256` and the device's sequence number in `data.device_seq`, beside plain fields that the dashboard counts:

- `observer.device_registered`: a device's public key;
- `observation.first_reading` and `observation.entry`: the two taps of a match, or a note of what was heard instead, with two-tap entry;
- `observation.other_board`, `observation.out_of_order` and `observation.id_not_on_list`: a boundary ballot another board pulled, and an imprinted ID heard instead of the one expected;
- `observation.entry_mode`: the observer chose two-tap or contest-by-contest entry (`data.mode`);
- `observation.contest`: with contest-by-contest entry, one contest's outcome in one reading (`data.contest`, `row`, `rows`, `expected`, `reading`, `outcome`, `heard`, `correction`, `elapsed_ms`);
- `observation.ballot_done`: with contest-by-contest entry, the observer marked a ballot done (`data.reading`, `rows`, `unresolved`);
- `observation.lookup_contest` and `observation.lookup_done`: the same two outcomes for a ballot the observer looked up by imprinted ID after the board pulled it from a storage box (docs/TARGETING.md), recorded apart from every pull list (`data.extract_sha256` is the redacted CVR's hash);
- `observation.rejected`: something a device sent that the server refused, with the reason and the payload's hash only.

An entry is never changed: a correction is a later event.

### Targeted ballots

The coordinators' choice of ballots outside the RLA sample (docs/TARGETING.md, which lists each event's fields): `targets.drawn` (a random draw from a pattern of votes, committing to its record by hash), `targets.listed` (imprinted IDs entered by hand), `targets.included` (a source included in or excluded from the working list), `targets.sent` (the working list sent to the county as Targeted list N, with its `extract.built`) and `targets.allowed_set`; a sent list is retired, restored and its details published with the `extract.retired`, `extract.restored`, `extract.details_published` and `extract.details_withdrawn` events (docs/EXTRACT.md). No pattern, expected vote or imprinted ID appears in these events until a list's details are published.

## Canonical serialization

The canonical serialization of a JSON value is:

- **Objects:** `{`, then the members sorted by key, as `"key":value` separated by `,`, then `}`. Keys are compared by Unicode code point, which for ASCII keys is the same as comparing bytes.
- **Arrays:** `[`, then the elements in order separated by `,`, then `]`.
- **No insignificant whitespace** anywhere: no spaces or newlines between tokens.
- **Integers** in plain decimal: an optional `-` and digits, with no leading zeros, `+`, decimal point or exponent. Zero is `0`.
- **`true`, `false` and `null`** as those words.
- **Strings** in double quotes. Every character is written as itself, including all non-ASCII characters, except:
  - `"` is written `\"` and `\` is written `\\`;
  - the control characters U+0008, U+0009, U+000A, U+000C and U+000D are written `\b`, `\t`, `\n`, `\f` and `\r`;
  - every other character below U+0020 is written `\u00XX`, with lowercase hex digits (for example `\u001f`).

  Nothing else is escaped: not `/`, not U+007F, not U+2028 or U+2029, and no `\u` escapes for any other character.
- **Encoding:** the result is encoded as UTF-8, with no byte order mark.

This is what Python's `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` and JavaScript's `JSON.stringify` (on objects whose keys have been sorted) produce for the values allowed above. Some JSON libraries escape more by default, such as Go's `encoding/json`, which escapes `<`, `>`, `&`, U+2028 and U+2029; such a library must be configured not to, or not used for re-serializing.

## Hash and chain

An event's **hash input** is the canonical serialization of the object holding every field of the event except `hash`, with absent optional fields as `null`. Its keys therefore always appear in this order:

```
actor, board, client_time, county, data, device_id, file_sha256, kind, prev_hash, seq, server_time, session_id, signature, v
```

The event's `hash` is the SHA-256 of its hash input, written as 64 lowercase hex digits.

The first event has `seq` 1 and `prev_hash` of 64 zeros. Every later event has `seq` one more than the event before it, and `prev_hash` equal to that event's `hash`.

## The JSONL file

`events.jsonl` holds one line per event, in `seq` order, each ending with a single line feed (`0x0A`), including the last. There is nothing else in the file: no header, no blank lines, no byte order mark.

Each line is the event's hash input with `,"hash":"<hash>"` inserted before its final `}`. That is, the line is the hash input minus its last byte, then the 9 bytes `,"hash":"`, the 64 hex digits of the hash, and the 2 bytes `"}`. Every other field is in the sorted order of the hash input, and `hash` comes last.

This makes the hash checkable from the line's own bytes, with no JSON serializer at all: remove the last 75 bytes (`,"hash":"`, 64 hex digits and `"}`), append `}`, and take the SHA-256 of the result. A JSON parser is needed only to read `seq` and `prev_hash`, and to confirm that the line is canonical.

## Verifying a log

A verifier reads the file line by line and, for each line, checks that:

1. the line ends with a line feed;
2. it ends (before the line feed) with `,"hash":"`, 64 lowercase hex digits and `"}`;
3. the SHA-256 of its hash input (the line without those 75 bytes, plus `}`) is the hash on the line;
4. the hash input parses as a JSON object with `v` equal to 1 and exactly the 14 fields above, with no floating-point numbers;
5. re-serializing the parsed object canonically gives back exactly the hash input, which rules out duplicate keys, extra whitespace and other ways of writing the same content that parsers might read differently;
6. `seq` is one more than the previous line's (1 on the first line), and `prev_hash` is the previous line's hash (64 zeros on the first line).

The first line that fails any check is the first broken link. Everything before it is intact.

This program does all six checks, with nothing but Python's standard library. It is run by this project's tests, so it stays correct.

<!-- verifier:python -->
```python
import hashlib
import json

FIELDS = {"seq", "server_time", "client_time", "actor", "kind", "county", "board",
          "session_id", "device_id", "file_sha256", "signature", "data", "v", "prev_hash"}
TAIL = 75  # ',"hash":"' + 64 hex digits + '"}'


def no_floats(text):
    raise ValueError(f"floating-point number {text}")


def verify(path):
    seq, prev_hash = 0, "0" * 64
    with open(path, "rb") as f:
        for number, line in enumerate(f, start=1):
            broken = f"BROKEN at line {number}: "
            if not line.endswith(b"\n"):
                return broken + "no line feed at the end"
            line = line[:-1]
            tail = line[-TAIL:].decode("ascii", "replace")
            claimed = tail[9:73]
            if not (len(line) > TAIL and tail.startswith(',"hash":"') and tail.endswith('"}')
                    and all(c in "0123456789abcdef" for c in claimed) and len(claimed) == 64):
                return broken + "the line does not end with its hash"
            hash_input = line[:-TAIL] + b"}"
            if hashlib.sha256(hash_input).hexdigest() != claimed:
                return broken + "the hash does not match the content"
            try:
                event = json.loads(hash_input.decode("utf-8"), parse_float=no_floats)
            except ValueError as error:
                return broken + f"not valid JSON: {error}"
            if not isinstance(event, dict):
                return broken + "not a JSON object"
            if event.get("v") != 1 or event.get("v") is True:
                return broken + f"unsupported format version v={event.get('v')!r}"
            if set(event) != FIELDS:
                return broken + "wrong fields"
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")
            if canonical != hash_input:
                return broken + "not in canonical form"
            if event["seq"] != seq + 1 or event["prev_hash"] != prev_hash:
                return broken + "the chain does not continue from the line before"
            seq, prev_hash = event["seq"], claimed
    return f"OK: {seq} events, head {prev_hash}"


if __name__ == "__main__":
    import sys
    print(verify(sys.argv[1]))
```

Saved as `verify_log.py`, it runs as `python3 verify_log.py events.jsonl`. With this project installed, `corla-observer log verify events.jsonl` makes the same checks on the JSONL file alone, and `corla-observer log verify DIRECTORY` also checks the server's database copy against it (see below).

### What verification shows, and what it doesn't

A log that verifies is internally consistent: no event in it has been altered, removed, inserted or moved since it was written, unless the whole chain from that point on was recomputed. The verifier cannot tell on its own:

- **whether events were cut off the end,** since a log truncated after any event is still a valid log;
- **whether the whole chain was rewritten,** with every hash recomputed from some point on.

Both are detected by comparing with a chain head (a `seq` and `hash`) recorded somewhere the server can't change afterwards. The head is published, signed and timestamped at fixed points (before the seed is rolled, when the extract is committed, at the end of each audit day and when an audit closes), and every receipt given to an observer's device includes the head at that moment. A log that verifies, and whose event at each published or receipted `seq` has the published or receipted `hash`, contains every event up to the latest such head exactly as it was.

Verification also says nothing about whether an event's content is true. That is what the device signatures, the observers' own device-held records and paper, and the committed inputs are for.

## Worked example

These two events form a complete log. The first records a county file's arrival, and the second an observer's entry with a note containing non-ASCII characters.

Event 1's hash input:

<!-- worked-example:event1-hash-input -->
```text
{"actor":"coordinator:c1","board":null,"client_time":null,"county":"Boulder","data":{"filename":"Boulder_redacted_CVR.csv","role":"redacted_cvr","size":8},"device_id":null,"file_sha256":"13550350a8681c84c861aac2e5b440161c2b33a3e4f302ac680ca5b686de48de","kind":"file.received","prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","seq":1,"server_time":"2026-11-16T15:30:00.123Z","session_id":null,"signature":null,"v":1}
```

It is all ASCII, so its bytes are exactly the characters shown, with no line feed:

<!-- worked-example:event1-hash -->
```text
445 bytes
SHA-256 e3a43e7cc5de3e9368f0ab206830ae747fc04ca2aefa1b54d5c9df0958f3d611
```

Event 1's line in `events.jsonl`, followed by a line feed:

<!-- worked-example:event1-line -->
```text
{"actor":"coordinator:c1","board":null,"client_time":null,"county":"Boulder","data":{"filename":"Boulder_redacted_CVR.csv","role":"redacted_cvr","size":8},"device_id":null,"file_sha256":"13550350a8681c84c861aac2e5b440161c2b33a3e4f302ac680ca5b686de48de","kind":"file.received","prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","seq":1,"server_time":"2026-11-16T15:30:00.123Z","session_id":null,"signature":null,"v":1,"hash":"e3a43e7cc5de3e9368f0ab206830ae747fc04ca2aefa1b54d5c9df0958f3d611"}
```

Event 2's hash input. Its `prev_hash` is event 1's hash:

<!-- worked-example:event2-hash-input -->
```text
{"actor":"observer:o7","board":"2","client_time":"2026-11-17T16:05:41.950Z","county":"Boulder","data":{"imprinted_id":"101-23-45","match":false,"note":"heard “No” on 2"},"device_id":"d-41aa","file_sha256":null,"kind":"observation.entered","prev_hash":"e3a43e7cc5de3e9368f0ab206830ae747fc04ca2aefa1b54d5c9df0958f3d611","seq":2,"server_time":"2026-11-17T16:05:42.007Z","session_id":"s-9f2c","signature":"c2lnbmF0dXJl","v":1}
```

The curly quotes in the note are written as themselves, not as `\u` escapes, so they are three bytes each in UTF-8. The note's value, with its double quotes, is these bytes:

<!-- worked-example:event2-note-bytes -->
```text
0000  22 68 65 61 72 64 20 e2 80 9c 4e 6f e2 80 9d 20
0010  6f 6e 20 32 22
```

Event 2's hash input is 422 characters, but in UTF-8:

<!-- worked-example:event2-hash -->
```text
426 bytes
SHA-256 495b12057bec322f24acc4db294b0f8650e35576cf4216455fe94add0796e025
```

Event 2's line:

<!-- worked-example:event2-line -->
```text
{"actor":"observer:o7","board":"2","client_time":"2026-11-17T16:05:41.950Z","county":"Boulder","data":{"imprinted_id":"101-23-45","match":false,"note":"heard “No” on 2"},"device_id":"d-41aa","file_sha256":null,"kind":"observation.entered","prev_hash":"e3a43e7cc5de3e9368f0ab206830ae747fc04ca2aefa1b54d5c9df0958f3d611","seq":2,"server_time":"2026-11-17T16:05:42.007Z","session_id":"s-9f2c","signature":"c2lnbmF0dXJl","v":1,"hash":"495b12057bec322f24acc4db294b0f8650e35576cf4216455fe94add0796e025"}
```

The file holding these two lines (each followed by a line feed) verifies with head `2 495b12057bec322f24acc4db294b0f8650e35576cf4216455fe94add0796e025`. In the example, `file_sha256` is the SHA-256 of the 8-byte file `example` plus a line feed, and the signature is a placeholder (the base64 of the word `signature`).

## The server's copy

This section describes how the server keeps the log. A verifier doesn't need it.

The server keeps each event twice:

- **`events.jsonl`,** the file specified above. Each event is appended to it, flushed and synced to disk before the event is committed to the database, so it is always at least as complete as the database. It is replicated off the server as it grows.
- **`events.sqlite3`,** a SQLite database in write-ahead-log mode, with an `events` table holding the same fields. Triggers abort any `UPDATE` or `DELETE` on the table, and any `INSERT` that does not extend the chain. Each append takes the database's write lock first, so appends from several threads or processes are serialized into one chain.

If the server stops between writing an event to the file and committing it to the database, the file holds exactly one event more than the database. The next time the log is opened, that event is checked and added to the database. Any other difference between the two is reported as an error for a person to look into, never repaired automatically. The database can be rebuilt from a verified `events.jsonl` alone.

The triggers guard against mistakes, not against someone with control of the server, who could remove them. Protection against that comes from the hash chain, the published heads and the copies held elsewhere.
