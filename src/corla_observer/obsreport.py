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
- **from a marked spreadsheet or a paper tally,** entered in the same terms (``report_from_marks``).

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

Standard library only.
"""

from __future__ import annotations

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
