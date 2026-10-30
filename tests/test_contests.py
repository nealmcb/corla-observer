"""Fast tests for contest-by-contest entry (corla_observer.contests): payloads, recording, receipts, record checks and statistics."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from corla_observer.contests import compare_board, fold, latest_mode, observer_modes, page_state, plain_fields
from corla_observer.events import EventLog
from corla_observer.observer_record import check_record, render_report
from corla_observer.practice import Link, practice_stats
from corla_observer.receipts import PayloadError, check_payload
from devices import Device
from test_receipts import LINK, Server, by_seq, kinds, record_of

CONTESTS = [("President", "Biden / Harris"), ("US Senator", "Hickenlooper"), ("Prop 115", "No/Against")]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    s = Server(tmp_path / "log")
    yield s
    s.log.close()


def ballot(device: Device, position: int, imprinted_id: str, *, reading: int, changes: dict[int, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Every contest of a three-contest ballot in one reading: confirmed, or as ``changes`` says (row: (outcome, heard))."""
    items = []
    for row, (contest, expected) in enumerate(CONTESTS, start=1):
        outcome, heard = (changes or {}).get(row, ("confirmed", ""))
        items.append(device.contest(position, imprinted_id, row, len(CONTESTS), contest, expected, reading=reading, outcome=outcome, heard=heard))
    return items


# ---------------------------------------------------------------------------
# Payloads


def test_contest_payloads_are_accepted() -> None:
    device = Device()
    check_payload(device.contest(1, "101-1-1", 2, 61, "US Senator", "Hickenlooper", reading=2, outcome="heard_instead", heard="Gardner")["payload"])
    check_payload(device.contest(1, "101-1-1", 61, 61, "Prop 115", "no vote", outcome="unclear")["payload"])
    check_payload(device.ballot_done(1, "101-1-1", 61, unresolved=3)["payload"])
    check_payload(device.ballot_done(2, "101-1-2", 0, reading=1)["payload"])
    check_payload(device.entry_mode("per_contest")["payload"])


@pytest.mark.parametrize(
    ("kind", "change", "message"),
    [
        ("contest", lambda c: c.update(row=62), "row is 1 to rows"),
        ("contest", lambda c: c.update(row=0), "row is 1 to rows"),
        ("contest", lambda c: c.update(rows=501), "rows"),
        ("contest", lambda c: c.update(reading=3), "reading"),
        ("contest", lambda c: c.update(reading=True), "reading"),
        ("contest", lambda c: c.update(outcome="maybe"), "outcome"),
        ("contest", lambda c: c.update(outcome="heard_instead"), "heard is empty"),
        ("contest", lambda c: c.update(heard="Gardner"), "heard is empty unless"),
        ("contest", lambda c: c.update(outcome="heard_instead", heard=" Gardner"), "leading or trailing"),
        ("contest", lambda c: c.update(outcome="heard_instead", heard="x" * 201), "heard"),
        ("contest", lambda c: c.update(contest=""), "contest is empty"),
        ("contest", lambda c: c.update(expected="a\x07"), "expected"),
        ("contest", lambda c: c.update(correction="yes"), "correction"),
        ("contest", lambda c: c.pop("expected"), "content fields"),
        ("ballot_done", lambda c: c.update(unresolved=62), "unresolved"),
        ("ballot_done", lambda c: c.update(reading=0), "reading"),
        ("entry_mode", lambda c: c.update(mode="fast"), "mode"),
        ("entry_mode", lambda c: c.update(elapsed_ms=1), "content fields"),
    ],
)
def test_contest_payloads_are_checked(kind: str, change: Any, message: str) -> None:
    device = Device()
    item = {
        "contest": lambda: device.contest(1, "101-1-1", 61, 61, "Prop 115", "No/Against"),
        "ballot_done": lambda: device.ballot_done(1, "101-1-1", 61),
        "entry_mode": lambda: device.entry_mode("two_tap"),
    }[kind]()
    payload = copy.deepcopy(item["payload"])
    change(payload["content"])
    with pytest.raises(PayloadError, match=message):
        check_payload(payload)


# ---------------------------------------------------------------------------
# Recording, receipts and the record checker


def test_contest_entries_are_recorded_with_receipts_and_check(server: Server) -> None:
    device = Device()
    items = [device.registration(), device.entry_mode("per_contest")]
    items += ballot(device, 1, "101-1-1", reading=1, changes={2: ("unclear", "")})
    items += ballot(device, 1, "101-1-1", reading=2, changes={3: ("heard_instead", "Yes/For")})
    items.append(device.ballot_done(1, "101-1-1", 3, unresolved=1))
    outcomes = server.send(*items)
    assert [o.status for o in outcomes] == ["recorded"] * len(items)
    events = list(server.log.iter_events())
    contest = [e for e in events if e.kind == "observation.contest"]
    assert len(contest) == 6
    heard = contest[-1]
    assert heard.data["outcome"] == "heard_instead" and heard.data["heard"] == "Yes/For" and heard.data["reading"] == 2
    assert heard.data["contest"] == "Prop 115" and heard.data["expected"] == "No/Against" and heard.data["row"] == 3
    assert heard.signature and heard.data["payload"] == items[-2]["payload"]
    receipt_kinds = {str(o.receipt["receipt"]["event_kind"]) for o in outcomes if o.receipt}  # type: ignore[call-overload,index]
    assert receipt_kinds >= {"observation.contest", "observation.ballot_done", "observation.entry_mode"}
    # Each payload is its own link in the device's chain, and resending the whole batch records nothing again.
    again = server.send(*items)
    assert {o.status for o in again} == {"duplicate"}
    assert len(list(server.log.iter_events())) == len(events)
    report = check_record(record_of(device, by_seq(outcomes)), server.jsonl, server_key=server.signer.public_raw)
    assert report.ok, render_report(report)
    assert report.entries == len(items)


def test_checker_finds_a_contest_entry_whose_plain_fields_differ(tmp_path: Path) -> None:
    server = Server(tmp_path / "log")
    device = Device()
    outcomes = server.send(device.registration(), *ballot(device, 1, "101-1-1", reading=1))
    server.log.close()
    lines = server.jsonl.read_text().splitlines()
    # Rewrite the log as a server might, changing a contest's plain outcome and re-chaining it.
    from corla_observer.events import EventLog as Log

    events = [json.loads(line) for line in lines]
    target = next(e for e in events if e["kind"] == "observation.contest")
    target["data"]["outcome"] = "heard_instead"
    copied = tmp_path / "copy"
    with Log.open(copied) as log:
        for e in events:
            log.append(e["kind"], e["actor"], county=e["county"], board=e["board"], device_id=e["device_id"],
                       client_time=e["client_time"], signature=e["signature"], data=e["data"])  # fmt: skip
    report = check_record(record_of(device, by_seq(outcomes)), copied / "events.jsonl")
    assert "differs" in kinds(report.findings)
    assert any("outcome" in f.message and "contest 1 'President'" in f.message for f in report.findings), render_report(report)


def test_plain_fields_are_what_the_payload_says() -> None:
    device = Device()
    payload = device.contest(4, "101-1-4", 2, 3, "US Senator", "Hickenlooper", reading=2, outcome="unclear")["payload"]
    assert plain_fields(payload) == {
        "contest": "US Senator", "row": 2, "rows": 3, "expected": "Hickenlooper", "reading": 2, "outcome": "unclear",
        "heard": "", "correction": False, "elapsed_ms": 600,
    }
    assert plain_fields(device.entry_mode("two_tap")["payload"]) == {"mode": "two_tap"}
    with pytest.raises(ValueError):
        plain_fields(device.entry(1, "101-1-1")["payload"])


# ---------------------------------------------------------------------------
# A ballot's state, statistics and the board comparison


def test_ballot_state_done_unresolved_and_notes(server: Server) -> None:
    device = Device()
    server.send(device.registration())
    server.send(*ballot(device, 1, "101-1-1", reading=1, changes={2: ("unclear", "")}))
    rows = fold(server.log.iter_events())[(LINK.actor, LINK.county, "1")][1]
    assert not rows.done and rows.unresolved() == [1, 2, 3]  # no second reading yet
    server.send(*ballot(device, 1, "101-1-1", reading=2, changes={3: ("heard_instead", "Yes/For")}))
    rows = fold(server.log.iter_events())[(LINK.actor, LINK.county, "1")][1]
    assert rows.complete and rows.done and not rows.marked_done
    assert rows.unresolved() == [3]  # unclear then confirmed is resolved; confirmed then heard instead differs
    assert rows.rows[3].differs and not rows.rows[2].differs
    assert not rows.match
    assert rows.note() == "Prop 115: first reading confirmed, second heard “Yes/For”"
    # A correction is a new entry; the latest outcome for the row and reading counts.
    server.send(device.contest(1, "101-1-1", 3, 3, "Prop 115", "No/Against", reading=2, correction=True))
    rows = fold(server.log.iter_events())[(LINK.actor, LINK.county, "1")][1]
    assert rows.match and rows.unresolved() == [] and rows.rows[3].corrections == 1 and rows.rows[3].marks == 3
    state = page_state({1: rows})
    assert state["1"]["rows"]["2"]["1"]["outcome"] == "unclear" and state["1"]["done"] is False


def test_marked_done_with_contests_missing(server: Server) -> None:
    device = Device()
    server.send(device.registration(), device.contest(2, "101-1-2", 1, 3, "President", "Biden / Harris"))
    server.send(device.ballot_done(2, "101-1-2", 3, reading=1, unresolved=3))
    rows = fold(server.log.iter_events())[(LINK.actor, LINK.county, "1")][2]
    assert rows.done and rows.marked_done and not rows.complete and not rows.match
    assert rows.note() == "2 contests not entered"


def test_stats_count_both_modes_and_switches(server: Server) -> None:
    ann, bob = Device(), Device(observer="observer:bob")
    bob_link = Link("observer", "bob", "La Plata", 1, "2026-10-24T00:00:00Z")
    # Ann: two taps on ballot 1, then contest by contest on ballots 2 and 3, then back to two taps.
    server.send(ann.registration(), ann.first_reading(1, "101-1-1"), ann.entry(1, "101-1-1"), ann.entry_mode("per_contest", 2, "101-1-2"))
    server.send(*ballot(ann, 2, "101-1-2", reading=1), *ballot(ann, 2, "101-1-2", reading=2))
    server.send(*ballot(ann, 3, "101-2-3", reading=1, changes={1: ("unclear", "")}), *ballot(ann, 3, "101-2-3", reading=2, changes={1: ("unclear", "")}))
    server.send(ann.entry_mode("two_tap", 4, "101-2-4"))
    # Bob: two taps throughout, with a note on ballot 3 and a match on ballot 2.
    server.send(bob.registration(), bob.entry(2, "101-1-2"), bob.entry(3, "101-2-3", False, "President: heard Trump"), link=bob_link)
    events = list(server.log.iter_events())
    assert latest_mode(events, LINK.actor, LINK.county, "1") == "two_tap"
    assert observer_modes(events)[(LINK.actor, LINK.county, "1")] == ["two_tap", "per_contest"]
    observers, boards = practice_stats(events)
    stats = {o.actor: o for o in observers}
    a = stats["observer:ann"]
    assert a.modes == ["two_tap", "per_contest"] and a.mode_text == "two taps, contest by contest"
    assert a.positions == 3 and a.matches == 2 and a.notes == 1 and a.entries == 1
    assert a.contest_marks == 12 and a.contest_unclear == 1 and a.contest_ballots == 2
    assert stats["observer:bob"].mode_text == "two taps"
    assert boards[0].done == 3 and boards[0].per_contest == ["observer:ann"]
    assert boards[0].notes == 1 and boards[0].matches == 2

    comparison = {c.position: c for c in compare_board(events, "La Plata", "1")}
    assert comparison[1].precision == "ballot" and comparison[1].verdict == "match"
    assert comparison[2].precision == "contest" and comparison[2].verdict == "match"
    assert sorted(comparison[2].matched_by) == ["observer:ann", "observer:bob"]
    third = comparison[3]
    assert third.precision == "contest" and third.verdict == "difference"
    assert [(f.observer, f.contest, f.text) for f in third.contests] == [("observer:ann", "President", "not heard clearly")]
    assert third.ballot_notes == [("observer:bob", "President: heard Trump")]


def test_comparison_flags_a_two_tap_note_against_contests_all_confirmed(server: Server) -> None:
    ann, bob = Device(), Device(observer="observer:bob")
    bob_link = Link("observer", "bob", "La Plata", 1, "2026-10-24T00:00:00Z")
    server.send(ann.registration(), *ballot(ann, 1, "101-1-1", reading=1), *ballot(ann, 1, "101-1-1", reading=2))
    server.send(bob.registration(), bob.entry(1, "101-1-1", False, "heard Yes on 115"), link=bob_link)
    server.send(*ballot(ann, 2, "101-1-2", reading=1, changes={3: ("heard_instead", "Yes/For")}),
                *ballot(ann, 2, "101-1-2", reading=2, changes={3: ("heard_instead", "Yes/For")}))  # fmt: skip
    server.send(bob.entry(2, "101-1-2"), link=bob_link)
    comparison = {c.position: c for c in compare_board(server.log.iter_events(), "La Plata", "1")}
    assert comparison[1].verdict == "conflict" and comparison[2].verdict == "conflict"
    assert [f.text for f in comparison[2].contests] == ["heard “Yes/For”"]


def test_events_without_county_or_rows_are_ignored(tmp_path: Path) -> None:
    with EventLog.open(tmp_path / "log") as log:
        log.append("observation.contest", "observer:ann", data={"position": 1})
        log.append("observation.contest", "observer:ann", county="La Plata", board="1", data={"position": "x"})
        log.append("observation.contest", "observer:ann", county="La Plata", board="1", data={"position": 1, "row": 1, "reading": 3, "outcome": "confirmed"})
        found = fold(log.iter_events())
    assert found[("observer:ann", "La Plata", "1")][1].rows == {}
