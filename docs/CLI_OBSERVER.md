# The command-line observer client

`scripts/corla-observe.py` is an observer client for the terminal, in one Python file that can be
read from top to bottom. It does what the observer page does for an observer who enters each
ballot with two taps, and nothing else. Its point is that anyone can see exactly what code an
observer ran: the file prints its own SHA-256 when it starts and writes it into the observer's
record, so the record names the code that made it.

It speaks the observer protocol of [RECEIPTS.md](RECEIPTS.md) unchanged, so the server treats it
like any other device. It needs only Python 3.12 and the `cryptography` package, and imports
nothing from this repository.

## Running it

Outside this repository, in an environment of its own:

```
python3 -m venv observe-env
observe-env/bin/pip install cryptography==50.0.2
observe-env/bin/python -I corla-observe.py 'https://HOST/PREFIX/observe/TOKEN'
```

In a checkout of this repository: `uv run python -I scripts/corla-observe.py LINK`. `-I` keeps
Python from loading anything from the current directory or the environment's settings.

Options:

- `--dir DIR`: the folder for this link's files (below). The default is
  `corla-observe-` and 12 hex digits derived from the link, in the current directory. Use the same
  folder every time for one link: it holds the device key and the record.
- `--server-key KEY`: the server's receipt key, from a source other than the server, such as the
  coordinator or a published evidence commitment. KEY is the key ID (64 hex digits), the base64
  key, the JSON served at `/receipt-key`, or a file holding any of these. The client refuses to
  start if the server's key is a different one.
- `--commitments FOLDER`: the published commitments (below), from a source apart from the server:
  a GitHub folder, such as `https://github.com/OWNER/REPO/tree/main/2026/general/observerfiles`,
  read through GitHub's API, or a local folder holding the SHA256SUMS files.
- `--check-every SECONDS`: how often to check the log while observing (default 300).

## Observing

The client shows the notice the page shows before the first ballot: keep the screen out of sight of
the audit board. Enter starts. It then signs the choice of entry mode (`entry_mode`, `two_tap`) and
the acknowledgement of the notice (`acknowledged`), as the page does.

Each ballot is shown in pull order: "Ballot n of N" and its position in the extract, the imprinted
ID on a line of its own, and the expected votes, laid out as in the text printout: each contest
right-aligned to the board's longest, so that the colons line up, then the choice. The names are
the ones the page shows, shortened by the server for a screen.

- **Enter** after the first board member reads the ballot signs a `first_reading`.
- **Enter** again after the second reading signs an `entry` with `match` true, and the next ballot
  is shown.
- **Text, then Enter**, at either point, is a mismatch with the text as the note (at most 500
  characters, control characters removed). The client asks to confirm it: Enter signs an `entry`
  with `match` false, anything else goes back to the ballot with nothing recorded. So a stray key
  can't record a mismatch.
- **An overvote**: when the board reads more choices in a contest than it allows, type the contest,
  "overvote" and the names heard, such as `Senate overvote: Ann Lee + Bob Ray`. The CVR shows a
  voter's overvote as NO VOTE, and CORLA counts no discrepancy for it (the coordinators' comparison
  with CORLA's results takes such a note as agreeing). The client's `/?` help says so too.

Commands start with `/`, which needs no shift key (`:` also works). So a note can't start with
either.

- `?`, `/?`, `/h` or `help` shows how to enter, the commands and the status. Typed at a ballot, it
  is never taken as a note.
- `/e N` or `/edit N` goes to ballot N. A new entry for a ballot already entered has `edit` true.
- `/c` copies the ballot shown, laid out as the text printout with `__` to mark, to the clipboard
  and to `copy.txt` in the client's folder, for marking it in an editor instead; `/c all` copies
  every ballot on the board. The clipboard is set with the OSC 52 escape sequence, which most
  terminals accept, also over SSH (inside tmux, with `set-clipboard on`); `copy.txt` is there
  when it isn't.
- `/ref` or `/refresh` shows the ballot again, clean.
- `/check` reads the commitments again and checks the log now.
- `/report` prints a statement of every alarm so far, to send to the coordinator.
- `/ack` acknowledges the alarms so far.
- `/s` or `/status` shows how many ballots are entered and entries queued, receipts checked, and
  alarms.
- `/save` writes `record.json` now; it is written after every entry and every answer from the
  server anyway.
- `/q` or `/quit`, or the end of input, ends the session.

At the end it sends what is queued, checks the log once more, writes `record.json`, and prints the
`check-record` command for it. The exit status is 0, 1 if the record holds an alarm, and 2 if it
could not start.

Each entry is signed, chained to the one before and written to disk before it is sent. When the
server can't be reached, entries are kept and queued, and sent, oldest first, when it can be. An
answer of 500 or more (such as 502, often from a proxy in front of the server, so the request may
never have arrived) is treated the same way: the client says once that the entries are not sent yet
and will be sent again, and once more when they are. Entries still queued at the end are sent at
the next run with the same folder. The server must be reachable to start, since the ballots come
from it.

If the server refuses entries with the code `unknown_device` (it has no registration for this
device, as after a server is restored from a backup), the client sends the same signed registration
again, then those entries, as the page does, and notes each step in `record.jsonl`. It does this
once in a session; if the server refuses them again, the refusal is final. Every other refusal is
final at once: the entry is kept with the server's reason, and raises an alarm.

## The folder

| File | What it holds |
|---|---|
| `device-key.pem` | the device's ECDSA P-256 private key, mode 0600 |
| `record.jsonl` | one JSON object a line, appended and synced to disk: every payload and signature as made, each receipt or refusal, each `unknown_device` answer and registration sent again, each alarm and acknowledgement, and each session's start with the client's SHA-256 |
| `server-key.json` | the receipt key pinned at the first start, as served at `/receipt-key` |
| `events.jsonl` | the server's event log, as far as the client has verified it |
| `record.json` | the record in the `corla-observer-record` format of [RECEIPTS.md](RECEIPTS.md#the-observer-held-record) |

The device ID is `d-` and the first 32 hex digits of the SHA-256 of the key's 65-byte uncompressed
public key, as on the page.

## What it checks

**At the start.** That `/receipt-key` is a consistent Ed25519 key (its `key_id` is the SHA-256 of
the key), that it is the key given with `--server-key` or pinned at an earlier start, and that
`data.json` names the same key. That the log has an `extract.built` event for the county whose file
is the extract that `data.json` gives (`extract_sha256`).

**Each receipt, as it arrives.** That it has exactly the fields of a receipt, is signed by the
pinned key, names that key, is for the payload sent (its SHA-256) and the event kind that records
it, and that its positions in the log agree with every earlier receipt's: no position with two
hashes, the device's entries in the order it made them, and no head earlier than one already
received.

**The log.** It fetches the head (`/head`) and then the new lines (`/observe/TOKEN/log`, 2,000 at a
time), and verifies each line as the verifier in [EVENT_LOG.md](EVENT_LOG.md#verifying-a-log)
does: its hash, its canonical form, and its chain from the line before, continuing from the copy it
verified earlier. It checks:

- that the log was not cut back to before a position it has already seen;
- that the log's hash at every position a receipt names is the one the receipt states;
- that every entry with a receipt is in the log at the event its receipt names;
- that every event under this device's ID holds a payload the device signed, with its signature,
  its kind, observer, county, board, `client_time` and sequence number;
- that no payload is recorded twice, and that a refusal recorded under the device's ID is of
  something the device sent;
- that the receipt key is published in the log (`receipt.key_published`) once it has signed
  receipts.

**The published commitments,** with `--commitments`. The coordinators commit to the evidence in
versioned SHA256SUMS files ([TIMESTAMPING.md](TIMESTAMPING.md)), timestamped and signed apart from
the server. The client reads every SHA256SUMS file in the folder and checks:

- that every commitment made at or after the extract was built (whose `event-log-head` is at or
  after the extract's `extract.built` event) lists the extract shown (a `derived/extract-*.json`
  line with its hash). An extract built after the latest commitment is not committed yet: the
  client says so, and the record notes it;
- that the receipt key is the one they list: it reads `derived/receipt-key.json` where the latest
  SUMS file listing it says (its paths are relative to the election's folder, the file's `base:`
  line), checks the file's SHA-256 against the listed hash, and compares its content, the
  canonical JSON of the key's `algorithm`, `public_key` and `key_id`, with `/receipt-key`.
  Commitments made before the key was committed list none, and the client says so;
- that the log agrees with each one's `event-log-head`: the head's position is in the log the
  server serves, and has that hash. With the hashes the receipts state, these are all points on
  the one log the client verifies from event 1.

Without `--commitments` the client says that these checks were not made. The record lists the
commitments read and each anchor, with whether the log agreed with it, under
`client_checks.commitments`.

## Alarms

Any failure is an **ALARM**: printed at once, with a short statement for the observer to send to
the coordinator and the other observers (not to the server). The statement gives what was found,
the position and hash in question, the server's address, the link's county and board (not the
link itself), the device ID, the client's SHA-256 and the time. Until the observer types `/ack`,
every prompt repeats how many alarms are unacknowledged and the latest one. `/report` prints the
statements of all alarms so far.

An alarm means the server's answers disagree with what the device did, with what it said before,
or with the published commitments. Keep the folder, send the statement, and keep observing on
paper. Alarms stay in `record.jsonl` and in the record (`client_checks`) after `/ack`, and the
exit status stays 1.

## Checking the record afterwards

Anyone with `record.json` and a copy of the published log can check it with this project's
checker, without trusting the client:

```
corla-observer check-record DIR/record.json events.jsonl --server-key DIR/server-key.json [--anchor SEQ:HASH ...]
```

The client prints this command at the end, with an `--anchor` for each commitment's head. The
checker also compares the plain fields of each event (position, match, note and so on) with the
signed payload, which the client does not.

## What it leaves out, and the tradeoffs

It does not do contest-by-contest entry, switching pull lists, looking up ballots from boxes,
heard imprinted IDs (an ID typed in is a note), skipping a boundary ballot as another board's,
photo checks or printouts. The page does those.

- **The key is a file.** Anything that can read `device-key.pem` can copy it and sign as the
  device; the page's key is non-extractable. Keep the folder private, on a computer of your own.
- **It must follow [RECEIPTS.md](RECEIPTS.md).** It builds the payloads itself, so a change to the
  protocol needs a change to it.

`tests/test_cli_observer.py` runs it against a server with Testland's extract: registration,
matches, a note, an edit, entries queued while the server can't be reached and sent later, a new
run that sends what an earlier one queued, `check-record` on the record, logs altered in several
ways, forged receipts, a server that forgets the device, the alarms' statements and
acknowledgement, published commitments that agree and that don't, and the file run alone with
`python3 -I`.
