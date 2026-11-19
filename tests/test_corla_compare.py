"""Fast tests for corla_observer.corla_compare and its coordinator page: observers' findings against CORLA's results.

Builds on tests/test_round_results.py's synthetic Audit Center files; the
observers' entries are appended to the event log as the observer page records
them (two-tap ``observation.entry``, contest-by-contest ``observation.contest``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from synthetic import election_files

from corla_observer import round_results as rr
from corla_observer.corla_compare import (
    AGREE,
    AGREE_OVERVOTE,
    BOTH_DIFFER,
    CORLA_ONLY,
    NOT_IN_CORLA,
    NOT_IN_CVR,
    NOT_OBSERVED,
    OBSERVER_ONLY,
    corla_comparison,
    says_overvote,
)
from corla_observer.datamap import build_map
from corla_observer.events import EventLog
from corla_observer.web.views import build_snapshot, redacted_cvrs
from corla_observer.workspace import Workspace
from test_county_status import fetch
from test_round_results import FILES, comparison, receive_cvr
from test_web import Factory, login, make_client, workspace  # noqa: F401 (fixtures)

WELD = comparison([
    ("Weld", "United States Senator", "101-1-3", '"John Hickenlooper"', '"John Hickenlooper","Ryan Apelbaum"'),
    ("Weld", "Weld County Council - At-Large", "101-1-3", '"Ann"', '"Ann"'),
    ("Weld", "United States Senator", "101-1-4", '"Ann"', '"Ann"'),
    ("Weld", "United States Senator", "101-1-6", '"Lee"', '"Lee"'),
    ("Weld", "Colorado Court of Appeals Judge - Harris", "101-1-6", '"No"', '"No"'),
    ("Weld", "United States Senator", "101-1-7", '"Lee"', '"Lee"'),
    ("Weld", "United States Senator", "101-1-99", '"Yes"', '"No"'),
])  # fmt: skip


def two_tap(log: EventLog, actor: str, position: int, card: str, *, note: str = "") -> None:
    data: dict[str, Any] = {"position": position, "imprinted_id": card, "match": not note, "note": note}
    log.append("observation.entry", actor, county="Weld", board="1", data=data)


def per_contest(log: EventLog, actor: str, position: int, card: str, contest: str, expected: str, heard: str) -> None:
    for reading in (1, 2):
        data = {"position": position, "imprinted_id": card, "rows": 1, "row": 1, "reading": reading, "contest": contest,
                "expected": expected, "outcome": "heard_instead", "heard": heard}  # fmt: skip
        log.append("observation.contest", actor, county="Weld", board="1", data=data)


def setup(store: Workspace, log: EventLog, tmp_path: Path) -> None:
    rr.clear_cache()
    fetch(store, log, election_files() | FILES | {"files/round1/contestComparison.csv": WELD})
    receive_cvr(store, log, "Weld", tmp_path)
    for position, card in enumerate(("101-1-3", "101-1-4", "101-1-99", "101-1-5", "101-1-7"), start=1):
        two_tap(log, "observer:neal", position, card)
    two_tap(log, "observer:john", 6, "101-1-6", note="harris blank")
    per_contest(log, "observer:ann", 1, "101-1-3", "United States Senator (Vote For=1)", "John Hickenlooper", "Hickenlooper and Apelbaum")
    two_tap(log, "observer:ann", 3, "101-1-99", note="something else")


def test_each_card_in_one_category(tmp_path: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        setup(store, log, tmp_path)
        snap = build_snapshot(log, store, ["Weld"])
        events = list(log.iter_events())
        datamap = build_map(log, store)
    boards = [(b.county, b.board, b.observers) for b in snap.boards if not b.targeted]
    assert boards == [("Weld", "1", ["observer:ann", "observer:john", "observer:neal"])]
    report = corla_comparison(datamap, store, events, boards, redacted_cvrs(snap))
    assert report.ref == "RLA/2026/general/files/round1/contestComparison.csv"
    (board,) = report.boards
    assert "no RLA extract" in board.notes[0]
    ann, john, neal = board.observers

    assert {k: v for k, v in neal.counts.items() if v} == {AGREE: 2, CORLA_ONLY: 1, NOT_IN_CVR: 1, NOT_IN_CORLA: 1, NOT_OBSERVED: 1}
    by_card = {c.imprinted_id: c for c in neal.cards}
    corla_only = by_card["101-1-3"]
    assert corla_only.category == CORLA_ONLY and [(r.contest, r.cvr_choice, r.board_choice) for r in corla_only.corla] == [
        ("United States Senator", "John Hickenlooper", "John Hickenlooper and Ryan Apelbaum (two choices)")
    ]
    assert by_card["101-1-5"].category == NOT_IN_CORLA and by_card["101-1-6"].category == NOT_OBSERVED
    # Not in Weld's redacted CVR: nothing of either side is shown.
    hidden = by_card["101-1-99"]
    assert hidden.category == NOT_IN_CVR and hidden.corla == [] and hidden.observer == []
    assert neal.counts_text == "2 agree, 1 CORLA only, 1 not observed, 1 not in CORLA's comparison, 1 not in the redacted CVR"

    observer_only = next(c for c in john.cards if c.imprinted_id == "101-1-6")
    assert observer_only.category == OBSERVER_ONLY and observer_only.observer == ["note: harris blank"]
    # A two-tap note names no contest: CORLA's rows for the contests whose words it uses.
    assert observer_only.corla_guessed and [(r.contest, r.cvr_choice, r.board_choice) for r in observer_only.corla] == [
        ("Colorado Court of Appeals Judge - Harris", "No", "No")
    ]

    both = next(c for c in ann.cards if c.imprinted_id == "101-1-3")
    assert both.category == BOTH_DIFFER and both.observer == [
        "United States Senator (Vote For=1): heard “Hickenlooper and Apelbaum” (CVR John Hickenlooper)"
    ]
    # Ann's note on a card not in the redacted CVR is never shown.
    ann_hidden = next(c for c in ann.cards if c.imprinted_id == "101-1-99")
    assert ann_hidden.category == NOT_IN_CVR and ann_hidden.observer == []


def test_the_coordinator_page_and_not_the_public_dashboard(workspace: Path, make_client: Factory, tmp_path: Path) -> None:  # noqa: F811
    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        setup(store, log, tmp_path)
    client: TestClient = make_client(counties=("Weld",))
    assert client.get("/coord/corla", follow_redirects=False).status_code == 303
    public = client.get("/").text
    assert "harris blank" not in public and "observer:john" not in public and "Observers and CORLA" not in public
    assert login(client).status_code == 303
    page = client.get("/coord/corla").text
    assert "Observers and CORLA's results" in page and "observer:neal" in page and "CORLA only" in page
    assert "harris blank" in page and "contests the note may refer to" in page and "Not observed: 1 card" in page
    assert "something else" not in page  # Ann's note on a card not in the redacted CVR
    assert 'href="/coord/corla"' in client.get("/coord/stats").text


OVERVOTES = comparison([
    ("Weld", "United States Senator", "101-1-8", "", '"Ann","Bob"'),  # a voter's overvote, as the board read it
    ("Weld", "United States Senator", "101-1-9", "", '"Ann","Bob"'),
    ("Weld", "United States Senator", "101-1-10", '"Ann"', '"Ann","Bob"'),  # the CVR has a choice: CORLA differs
    ("Weld", "United States Senator", "101-1-11", "", '"Ann","Bob"'),
    ("Weld", "United States Senator", "101-1-12", "", '"Ann","Bob"'),
])  # fmt: skip


def test_an_overvote_the_observer_and_the_board_both_read(tmp_path: Path) -> None:
    assert says_overvote("overvote: Ann + Bob", 1) and says_overvote("Ann + Bob", 1) and says_overvote("Ann, Bob, Cy", 2)
    assert not says_overvote("Ann", 1) and not says_overvote("Ann + Bob", 2)
    rr.clear_cache()
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        fetch(store, log, election_files() | FILES | {"files/round1/contestComparison.csv": OVERVOTES})
        receive_cvr(store, log, "Weld", tmp_path)
        senate = "United States Senator (Vote For=1)"
        per_contest(log, "observer:ann", 1, "101-1-8", senate, "no vote", "overvote: Ann + Bob")
        two_tap(log, "observer:ann", 2, "101-1-9", note="Senate overvote")
        per_contest(log, "observer:ann", 3, "101-1-10", senate, "Ann", "Ann + Bob")
        per_contest(log, "observer:ann", 4, "101-1-11", senate, "no vote", "Ann")
        two_tap(log, "observer:ann", 5, "101-1-12")
        snap = build_snapshot(log, store, ["Weld"])
        events = list(log.iter_events())
        datamap = build_map(log, store)
    boards = [(b.county, b.board, b.observers) for b in snap.boards if not b.targeted]
    (board,) = corla_comparison(datamap, store, events, boards, redacted_cvrs(snap)).boards
    (ann,) = board.observers
    by_card = {c.imprinted_id: c for c in ann.cards}
    # Both read an overvote where the CVR has no vote: CORLA counts no discrepancy, and the observer agrees.
    assert by_card["101-1-8"].category == by_card["101-1-9"].category == AGREE_OVERVOTE
    assert [(r.cvr_choice, r.board_choice, r.differs) for r in by_card["101-1-8"].corla] == [("no vote", "Ann and Bob (two choices)", False)]
    # The CVR has a choice: CORLA treats the board's overvote as no vote, a difference.
    both = by_card["101-1-10"]
    assert both.category == BOTH_DIFFER and both.corla[0].differs and "an overvote" in both.corla[0].note
    # The observer heard one choice where the board read an overvote: the observer's difference only.
    assert by_card["101-1-11"].category == OBSERVER_ONLY
    # A match where the board read a voter's overvote: CORLA's reading matches the CVR.
    assert "101-1-12" not in by_card and ann.counts[AGREE] == 1
