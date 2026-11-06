"""An observation report: what one observer found on one pull list, from their signed entries alone.

Written 2026-11-05 for the software-independence review (docs/si-review.md): the claim worth making
is not that each screen or page was right, but that the audit's own report (how many ballots, how
many discrepancies, in which contests) agrees with what the observers saw. That needs a report of
the observations that can be computed the same way from every record of them:

- **on the server,** from the payloads its log holds for an observer's device (`report_for_device`),
  signed with the server's receipt key (`sign_report`);
- **on the observer's device,** from the payloads it signed and kept, by the browser extension
  (``extension/report.js``, the same rules in JavaScript; tests/test_obsreport.py runs both on the
  same payloads and checks the reports are identical);
- **from a marked spreadsheet or a paper tally,** entered in the same terms (``report_from_marks``);
- **from the record an observer downloads** from the observer page (``report_from_record``), so the
  report of online entries can also be computed with no browser at all.

**Marking the spreadsheet.** The extract's spreadsheet (`corla_observer.extract.to_csv`) has two
blank columns per ballot, ``match`` and ``heard instead``. An observer, or someone transcribing a
paper tally, fills them in for each ballot read out:

- ``match``: ``y``, ``yes``, ``x``, ``✓`` or ``match`` when the ballot read out matched the expected
  votes; ``n``, ``no`` or ``different`` when it did not; ``other`` or ``other board`` when the board
  set the ballot aside as another board's. Left blank, the ballot was not observed, unless ``heard
  instead`` is filled in, which marks a discrepancy on its own;
- ``heard instead``: for a discrepancy, what was read out (``Bob for Governor``). It is the note of
  a discrepancy, as on the observer page.

The spreadsheet may be saved as CSV (any delimiter, with or without the leading quotes that guard
cells against formulas) or as an Excel workbook. Its identification rows give the extract's SHA-256,
which the report carries; a ``match`` that is none of the above, or ``yes`` with something heard
instead, is an error naming the row, never a guess. A spreadsheet of several boards needs the board
to report on.

**The report** (`build_report`), for one county, board and extract (the list), from payloads in the
order they were signed, is a JSON object:

- ``v`` 1, ``type`` ``observation_report``, ``observer``, ``county``, ``board``,
  ``extract_sha256``;
- ``ballots``: one per ballot entered, in position order: ``position``, ``imprinted_id``,
  ``verdict``, ``note`` and ``contests``. The verdict is ``match``, ``discrepancy``,
  ``other_board`` (marked as another board's ballot), or ``incomplete`` (entered contest by contest
  and not yet done). ``contests`` lists, for a discrepancy found contest by contest, each contest
  that differs: ``row``, ``contest``, ``expected`` and ``observed`` (``heard “Bob”``, ``not heard
  clearly``, or both readings when they differ). A two-tap discrepancy has the observer's ``note``
  and no contests;
- ``summary``: ``ballots``, ``matches``, ``discrepancies``, ``other_board``, ``incomplete``,
  ``ballot_level`` (discrepancies noted without naming contests) and ``by_contest`` (each contest's
  count of discrepancies).

(Integers are checked as JSON integers, ``true`` not being one, as in JavaScript.)

**The rules** are those of the observer page and the statistics (`corla_observer.contests`): a
ballot's latest entry counts, two taps or contest by contest, whichever was signed later; contest by
contest, a row's final outcome is its second reading's, else its first's; a ballot is done when
marked done or when every row has a second reading; it matches when done, every row entered, and
every final outcome confirmed with no readings that differ. Payloads of other types (first
readings, entry modes, notices, list switches, lookups) and of other extracts are left out.

Standard library only (the workbook reader is `corla_observer.classify`'s).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REPORT_VERSION = 1
ENTRY_TYPES = frozenset({"entry", "other_board", "contest", "ballot_done"})


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def _mark_text(mark: Mapping[str, Any]) -> str:
    outcome = mark["outcome"]
    if outcome == "confirmed":
        return "confirmed"
    if outcome == "unclear":
        return "not heard clearly"
    return f"heard “{mark['heard']}”"


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return bool(a["outcome"] == b["outcome"] and (a["outcome"] != "heard_instead" or _norm(a["heard"]) == _norm(b["heard"])))


def _contest_ballot(rows: Mapping[int, Mapping[str, Any]], total: int, marked_done: bool) -> tuple[str, list[dict[str, Any]], str]:
    """A ballot entered contest by contest: its verdict, the contests that differ, and its note."""
    complete = total > 0 and all(n in rows and 2 in rows[n]["readings"] for n in range(1, total + 1))
    if not (marked_done or complete):
        return "incomplete", [], ""
    differing = []
    for n in sorted(rows):
        row = rows[n]
        first, second = row["readings"].get(1), row["readings"].get(2)
        final = second or first
        differs = first is not None and second is not None and "unclear" not in (first["outcome"], second["outcome"]) and not _same(first, second)
        if differs:
            observed = f"first reading {_mark_text(first)}, second {_mark_text(second)}"
        elif final is not None and final["outcome"] != "confirmed":
            observed = _mark_text(final)
        else:
            continue
        differing.append({"row": n, "contest": row["contest"], "expected": row["expected"], "observed": observed})
    missing = sum(1 for n in range(1, total + 1) if n not in rows)
    note = f"{missing} contest{'s' if missing != 1 else ''} not entered" if missing else ""
    if differing or missing:
        return "discrepancy", differing, note
    return "match", [], ""


def build_report(
    payloads: Iterable[Mapping[str, Any]], *, observer: str, county: str, board: str, extract_sha256: str
) -> dict[str, Any]:
    """The observation report of ``payloads``, in the order signed (see the module documentation)."""
    two_tap: dict[int, tuple[int, Mapping[str, Any]]] = {}
    contest_rows: dict[int, dict[int, dict[str, Any]]] = {}
    totals: dict[int, int] = {}
    done: dict[int, bool] = {}
    last_contest: dict[int, int] = {}
    imprinted: dict[int, str] = {}
    for order, payload in enumerate(payloads):
        kind = payload.get("type")
        position = payload.get("position")
        if kind not in ENTRY_TYPES or payload.get("extract_sha256") != extract_sha256 or type(position) is not int:
            continue
        imprinted[position] = str(payload.get("imprinted_id") or "")
        content = payload.get("content") or {}
        if kind in ("entry", "other_board"):
            two_tap[position] = (order, payload)
            continue
        last_contest[position] = order
        rows_total = content.get("rows")
        if type(rows_total) is int:
            totals[position] = rows_total
        if kind == "ballot_done":
            done[position] = True
            continue
        row, reading = content.get("row"), content.get("reading")
        if type(row) is not int or type(reading) is not int or reading not in (1, 2):
            continue
        entry = contest_rows.setdefault(position, {}).setdefault(
            row, {"contest": str(content.get("contest") or ""), "expected": str(content.get("expected") or ""), "readings": {}}
        )
        entry["readings"][reading] = {"outcome": content.get("outcome"), "heard": str(content.get("heard") or "")}

    ballots: list[dict[str, Any]] = []
    for position in sorted(set(two_tap) | set(last_contest)):
        tap = two_tap.get(position)
        if tap is not None and tap[0] > last_contest.get(position, -1):
            payload = tap[1]
            content = payload.get("content") or {}
            if payload["type"] == "other_board":
                verdict, note = "other_board", ""
            elif content.get("match"):
                verdict, note = "match", ""
            else:
                verdict, note = "discrepancy", str(content.get("note") or "")
            contests: list[dict[str, Any]] = []
        else:
            verdict, contests, note = _contest_ballot(contest_rows.get(position, {}), totals.get(position, 0), done.get(position, False))
        ballots.append({"position": position, "imprinted_id": imprinted[position], "verdict": verdict, "note": note, "contests": contests})

    by_contest: dict[str, int] = {}
    for ballot in ballots:
        for found in ballot["contests"]:
            by_contest[found["contest"]] = by_contest.get(found["contest"], 0) + 1
    count = {v: sum(1 for b in ballots if b["verdict"] == v) for v in ("match", "discrepancy", "other_board", "incomplete")}
    summary = {
        "ballots": len(ballots), "matches": count["match"], "discrepancies": count["discrepancy"],
        "other_board": count["other_board"], "incomplete": count["incomplete"],
        "ballot_level": sum(1 for b in ballots if b["verdict"] == "discrepancy" and not b["contests"]),
        "by_contest": dict(sorted(by_contest.items())),
    }  # fmt: skip
    return {
        "v": REPORT_VERSION, "type": "observation_report", "observer": observer, "county": county, "board": board,
        "extract_sha256": extract_sha256, "ballots": ballots, "summary": summary,
    }  # fmt: skip


def device_payloads(events: Iterable[Any], device_id: str) -> list[Mapping[str, Any]]:
    """A device's payloads in the log, in the order the device signed them (by its sequence number)."""
    found: dict[int, Mapping[str, Any]] = {}
    for event in events:
        if event.device_id != device_id or not event.kind.startswith("observation."):
            continue
        payload = event.data.get("payload")
        if isinstance(payload, dict) and type(payload.get("device_seq")) is int:
            found.setdefault(payload["device_seq"], payload)
    return [found[n] for n in sorted(found)]


def report_for_device(events: Sequence[Any], *, observer: str, county: str, board: str, device_id: str, extract_sha256: str) -> dict[str, Any]:
    """The server's report of one device's entries on one extract, from the payloads its log holds."""
    return build_report(device_payloads(events, device_id), observer=observer, county=county, board=board, extract_sha256=extract_sha256)


def sign_report(signer: Any, report: Mapping[str, Any], *, device_id: str, head: tuple[int, str]) -> dict[str, Any]:
    """The report signed with the server's receipt key, with the device it is for and the log's head it was computed at."""
    body = dict(report) | {"device_id": device_id, "head_seq": head[0], "head_hash": head[1], "key_id": signer.key_id}
    return {"report": body, "signature": signer.sign(body)}


# ---------------------------------------------------------------------------
# Marked spreadsheets and paper tallies, and downloaded records

MATCH_MARKS = frozenset({"y", "yes", "x", "✓", "✔", "match", "matched", "ok", "1", "true"})
DIFFERENT_MARKS = frozenset({"n", "no", "different", "differs", "discrepancy", "mismatch", "0", "false"})
OTHER_BOARD_MARKS = frozenset({"other", "other board", "o", "skip", "skipped"})


class MarksError(ValueError):
    """A marked spreadsheet that cannot be read as one board's observations."""


def _sheet_rows(data: bytes) -> list[list[str]]:
    from corla_observer.classify import XlsxError, XlsxReader, decode_text

    if data[:2] == b"PK":
        try:
            reader = XlsxReader(data)
            return [list(r) for r in reader.rows(reader.sheet_names[0])]
        except (XlsxError, IndexError) as exc:
            raise MarksError(f"not a readable workbook: {exc}") from None
    text, _ = decode_text(data)
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))


def _cell(text: str) -> str:
    text = text.strip()
    return text[1:] if text.startswith("'") else text


def report_from_marks(data: bytes, *, observer: str, board: str | None = None) -> dict[str, Any]:
    """The observation report of a marked extract spreadsheet (see "Marking the spreadsheet" above).

    Raises:
        MarksError: if it is not an extract spreadsheet, names no extract, covers several boards
            and ``board`` is not given, or has marks that cannot be read.
    """
    rows = [[_cell(c) for c in row] for row in _sheet_rows(data)]
    ident = {row[0].lower(): row[1] for row in rows if len(row) >= 2 and row[0]}
    header_at = next((i for i, row in enumerate(rows) if [c.lower() for c in row[:1]] == ["board"] and "imprinted id" in [c.lower() for c in row]), None)
    if header_at is None:
        raise MarksError("no ballot header row (board, ..., imprinted id, ..., match, heard instead): not an extract spreadsheet")
    extract_sha = ident.get("extract", "").lower()
    if len(extract_sha) != 64 or any(c not in "0123456789abcdef" for c in extract_sha):
        raise MarksError("no extract SHA-256 in the identification rows (a row starting 'Extract')")
    header = [c.lower() for c in rows[header_at]]
    need = {name: header.index(name) for name in ("board", "position in county", "imprinted id", "match", "heard instead") if name in header}
    missing = {"board", "position in county", "imprinted id", "match", "heard instead"} - set(need)
    if missing:
        raise MarksError(f"the ballot header has no {', '.join(sorted(missing))} column")
    ballots = [row for row in rows[header_at + 1 :] if any(row)]
    boards = sorted({row[need["board"]] for row in ballots if len(row) > need["board"]})
    if board is None:
        if len(boards) != 1:
            raise MarksError(f"the spreadsheet covers boards {', '.join(boards)}: say which board to report on")
        board = boards[0]
    elif str(board) not in boards:
        raise MarksError(f"the spreadsheet has no ballots for board {board}")
    payloads: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        row = row + [""] * (len(header) - len(row))
        if not any(row) or row[need["board"]] != str(board):
            continue
        mark, heard = " ".join(row[need["match"]].lower().split()), row[need["heard instead"]].strip()
        position_text, imprinted = row[need["position in county"]], row[need["imprinted id"]]
        if not position_text.isdigit():
            problems.append(f"row {number}: position {position_text!r} is not a number")
            continue
        base = {"position": int(position_text), "imprinted_id": imprinted, "extract_sha256": extract_sha}
        if mark in MATCH_MARKS:
            if heard:
                problems.append(f"row {number} ({imprinted}): marked as a match, with {heard!r} heard instead")
                continue
            payloads.append(base | {"type": "entry", "content": {"match": True, "note": ""}})
        elif mark in DIFFERENT_MARKS or (not mark and heard):
            payloads.append(base | {"type": "entry", "content": {"match": False, "note": heard}})
        elif mark in OTHER_BOARD_MARKS:
            payloads.append(base | {"type": "other_board", "content": {}})
        elif mark:
            problems.append(f"row {number} ({imprinted}): {row[need['match']]!r} is not a mark this reads (yes, no or other board)")
    if problems:
        raise MarksError("; ".join(problems))
    county = ident.get("county", "")
    return build_report(payloads, observer=observer, county=county, board=str(board), extract_sha256=extract_sha)


def report_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The observation report of a record downloaded from the observer page (docs/RECEIPTS.md): its acknowledged entries, in the order signed.

    Check the record against the published log first (``corla-observer check-record``): this only computes.
    """
    entries = [e for e in record.get("entries", []) if isinstance(e, Mapping) and e.get("receipt") and isinstance(e.get("payload"), Mapping)]
    payloads = sorted((e["payload"] for e in entries), key=lambda p: p.get("device_seq") if type(p.get("device_seq")) is int else 0)
    return build_report(
        payloads, observer=str(record.get("observer", "")), county=str(record.get("county", "")),
        board=str(record.get("board", "")), extract_sha256=str(record.get("extract_sha256", "")),
    )  # fmt: skip


def render_report(report: Mapping[str, Any]) -> str:
    """A report as plain text: the summary, then each ballot that was not a match."""
    s = report["summary"]
    lines = [
        f"Observation report: {report['county']} board {report['board']}, observer {report['observer'] or '(not named)'}",
        f"Extract {report['extract_sha256']}",
        f"{s['ballots']} ballots: {s['matches']} matched, {s['discrepancies']} with discrepancies "
        f"({s['ballot_level']} noted without naming contests), {s['other_board']} another board's, {s['incomplete']} not finished",
    ]
    for contest, n in s["by_contest"].items():
        lines.append(f"  {contest}: {n}")
    for b in report["ballots"]:
        if b["verdict"] == "match":
            continue
        detail = "; ".join(f"{c['contest']}: expected {c['expected'] or '(no vote)'}, {c['observed']}" for c in b["contests"]) or b["note"]
        lines.append(f"  {b['position']:>5}  {b['imprinted_id']:<14} {b['verdict']}{': ' + detail if detail else ''}")
    return "\n".join(lines) + "\n"
