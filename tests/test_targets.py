"""Fast tests for targeted ballots (corla_observer.targets, docs/TARGETING.md) and their coordinator pages (corla_observer.web.targets)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corla_observer import pulllists
from corla_observer import targets as tg
from corla_observer.classify import DEMO_COUNTIES, TEST_COUNTIES, canonical_county, known_county
from corla_observer.events import EventLog, canonical_json
from corla_observer.extract import build_extract, corla_order, county_manifest, id_key, latest_extract, targeted_key
from corla_observer.uploads import receive
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from test_web import csrf_of, login

ACTOR = "coordinator:alice"
PASSWORD = "correct horse battery"
COUNTY = "Delta (2020 demo)"
SENATE = "United States Senator (Vote For=1)"
GOVERNOR = "Governor (Vote For=1)"
HOUSE = "Representative to the 120th United States Congress - District 8 (Vote For=1)"
LOCAL = "Synthetic County Commissioner (Vote For=1)"
CONTESTS = {
    SENATE: (("Ann Senate", "DEM"), ("Bob Senate", "REP"), ("Cy Third", "LBR")),
    GOVERNOR: (("Gail Gov", "DEM"), ("Hal Gov", "REP")),
    HOUSE: (("Hank House", "DEM"), ("Iris House", "REP")),
    LOCAL: (("Lou Local", ""), ("Max Local", "")),
}
#: (count, senate, governor, house, local): choice indexes, None for no vote, "*" redacted, "o" an overvote.
GROUPS = [
    (30, 0, 0, 0, 0),
    (25, 1, 1, 1, 1),
    (6, 0, 1, 0, 0),  # split: Senate DEM, Governor REP
    (4, 1, 0, 1, 1),  # split: Senate REP, Governor DEM
    (3, 0, 1, 1, 0),  # Senate DEM, House REP
    (2, 2, 0, 0, 0),
    (2, "o", 1, 0, 0),  # an overvote in the Senate
    (10, 0, "*", 0, 0),  # the Governor's cells redacted (ten, so the redaction check passes)
]


def cvr_bytes(*, parties: bool = True, blank_rows: int = 1) -> bytes:
    """A small Dominion-style CVR: four header rows, then the ballots of `GROUPS`, a fully blank row, ten per batch."""
    meta = ["CvrNumber", "TabulatorNum", "BatchId", "RecordId", "ImprintedId", "PrecinctPortion", "BallotType"]
    columns = [(c, i) for c, choices in CONTESTS.items() for i in range(len(choices))]
    pad = [""] * len(meta)
    rows = [
        ["2026 Synthetic General Election", "5.17"] + [""] * (len(meta) + len(columns) - 2),
        pad + [c for c, _ in columns],
        pad + [CONTESTS[c][i][0] for c, i in columns],
        meta + [CONTESTS[c][i][1] if parties else "" for c, i in columns],
    ]
    n = 0
    for count, *marks in GROUPS + [(blank_rows, "blank", "blank", "blank", "blank")]:
        for _ in range(count):
            n += 1
            batch, position = divmod(n - 1, 10)
            row = [str(n), "1", str(batch + 1), str(position + 1), f"1-{batch + 1}-{position + 1}", "Precinct 1", "Style 1"]
            for contest, i in columns:
                mark = marks[list(CONTESTS).index(contest)]
                if mark == "blank":
                    row.append("")
                elif mark == "*":
                    row.append("*")
                elif mark == "o":
                    row.append("1" if i < 2 else "0")
                else:
                    row.append("1" if i == mark else "0")
            rows.append(row)
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue().encode()


def manifest_bytes() -> bytes:
    lines = ["County,Tabulator,Batch,# of Ballots,Location"]
    # Batch 1 is stored in "Box 10", the others in "Box 2": the pull order puts "Box 2" first (natural order).
    lines += [f"Delta,1,{b},10,{'Box 10' if b == 1 else 'Box 2'}" for b in range(1, 10)]
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture
def votes(tmp_path: Path) -> tg.CvrVotes:
    path = tmp_path / "cvr.csv"
    path.write_bytes(cvr_bytes())
    return tg.load_votes(path, [SENATE, GOVERNOR, HOUSE])


def contest(votes: tg.CvrVotes, name: str) -> tg.ContestVotes:
    found = votes.contest_by_name(name)
    assert found is not None
    return found


def test_demo_counties_are_test_counties_never_colorado_counties() -> None:
    assert set(DEMO_COUNTIES) <= set(TEST_COUNTIES)
    assert canonical_county(COUNTY) is None and known_county("delta (2020 demo)") == COUNTY and known_county("Delta") == "Delta"


def test_groups_by_name_and_by_results() -> None:
    assert tg.contest_group(SENATE, None) == tg.contest_group(GOVERNOR, None) == "statewide"
    assert tg.contest_group(HOUSE, None) == "house"
    assert tg.contest_group("US Rep - CD8 - REP", None) == "house"
    assert tg.contest_group("State Representative - District 77 (Vote For=1)", None) is None
    assert tg.contest_group(LOCAL, None) is None
    assert tg.contest_group("Amendment 76 (CONSTITUTIONAL)", None) is None
    assert tg.contest_group("Testland County Treasurer", None) is None


def test_counting_and_the_grid(votes: tg.CvrVotes) -> None:
    s, g, h = contest(votes, SENATE), contest(votes, GOVERNOR), contest(votes, HOUSE)
    assert votes.rows == 83 and votes.ballots.bit_count() == 82 and votes.redacted_rows == 1
    assert votes.count({}) == (82, 72)  # the ten rows with "*" are not clean
    assert votes.count({s.index: [0]}) == (49, 39)  # the overvotes show no vote
    assert votes.count({s.index: [0], g.index: [1]}) == (9, 9)
    assert votes.count({s.index: [0], g.index: [1], h.index: [1]}) == (3, 3)
    assert votes.count({g.index: [0]}) == (36, 36)  # the redacted cells never match
    assert g.redacted == 10 and s.overvotes == 2
    grid = votes.grid_counts({s.index: [0], g.index: [1]})
    assert grid[f"{g.index}:any"] == 49 and grid[f"{g.index}:0"] == 30 and grid[f"{h.index}:1"] == 3
    assert grid[f"{s.index}:1"] == 25  # Bob Senate with Hal Gov; the overvotes with Hal Gov don't count
    assert set(votes.grid_counts({}, [s.index])) == {f"{s.index}:any", f"{s.index}:0", f"{s.index}:1", f"{s.index}:2"}


def test_every_contest_is_loaded_by_default_and_searched_by_name(tmp_path: Path) -> None:
    path = tmp_path / "cvr.csv"
    path.write_bytes(cvr_bytes())
    everything = tg.load_votes(path)
    assert len(everything.contests) == 4 and everything.contest_by_name(LOCAL) is not None
    assert [c.name for c in tg.search_contests(everything, "commissioner")] == ["Synthetic County Commissioner"]
    assert [c.name for c in tg.search_contests(everything, "county SYNTHETIC")] == ["Synthetic County Commissioner"]
    assert tg.search_contests(everything, "") == [] and tg.search_contests(everything, "mayor") == []
    # A contest added by name is a row of the grid, in its own group after the others.
    shown = tg.target_contests(everything, None, {SENATE: "statewide", GOVERNOR: "statewide", HOUSE: "house", LOCAL: "added"})
    assert [c.group for c in shown] == ["statewide", "statewide", "house", "added"] and shown[-1].name == "Synthetic County Commissioner"


def test_order_parties_and_presets(votes: tg.CvrVotes) -> None:
    groups = {SENATE: "statewide", GOVERNOR: "statewide", HOUSE: "house"}
    shown = tg.target_contests(votes, None, groups)
    assert [c.group for c in shown] == ["statewide", "statewide", "house"]
    assert all(not c.from_results and c.result_source == "county" for c in shown)
    assert shown[0].votes.margin / shown[0].votes.total <= shown[1].votes.margin / shown[1].votes.total
    senate = next(c for c in shown if c.name.startswith("United States Senator"))
    assert [senate.votes.choices[k] for k in senate.order] == ["Ann Senate", "Bob Senate", "Cy Third"]
    assert senate.parties == ["DEM", "REP", "LBR"] and senate.top("REP") == 1
    presets = tg.presets(shown)
    assert len(presets) == 4  # the two statewide contests both ways, and the tightest against the House both ways
    assert all(len(p.requirement) == 2 for p in presets)


def test_parties_from_the_contest_name_when_the_cvr_has_none(tmp_path: Path) -> None:
    path = tmp_path / "cvr.csv"
    path.write_bytes(cvr_bytes(parties=False))
    votes = tg.load_votes(path, [SENATE, GOVERNOR, HOUSE])
    shown = tg.target_contests(votes, None, {SENATE: "statewide", GOVERNOR: "statewide", HOUSE: "house"})
    assert tg.presets(shown) == [] and all(p is None for c in shown for p in c.parties)
    assert tg.party_in_name("US Rep - CD8 - REP") == "REP" and tg.party_in_name("Jane Doe (DEM)") == "DEM" and tg.party_in_name("Governor") is None
    primary = tg.ContestVotes(9, "US Rep - CD8 - REP", 1, ["A", "B"], [None, None], [5, 3], [0, 0])
    item = tg.ShownContest(primary, "house", [0, 1], list(primary.parties))
    votes.contests[9] = primary
    found = tg.target_contests(votes, None, {"US Rep - CD8 - REP": "house"})
    assert found[0].parties == ["REP", "REP"] and item.parties == [None, None]


def test_results_order_the_grid_with_fallbacks(votes: tg.CvrVotes) -> None:
    from corla_observer.enr import ContestResult, Results

    counties = {f"County {i}": {"Ann Senate (DEM)": 10, "Bob Senate (REP)": 9} for i in range(64)}
    senate = ContestResult("United States Senator", 1, {"Ann Senate (DEM)": 640, "Bob Senate (REP)": 576}, counties)
    local = ContestResult("Governor", 1, {"Gail Gov": 5, "Hal Gov": 4}, {"County 1": {"Gail Gov": 5, "Hal Gov": 4}})
    results = Results("state", None, None, {}, [senate, local])
    assert tg.contest_group(SENATE, results) == "statewide"
    assert tg.contest_group(GOVERNOR, results) is None  # listed in only one county
    groups = {SENATE: "statewide", GOVERNOR: "statewide", HOUSE: "house"}
    # The state's results order the contests they cover (the Senate: 64 of 1216, 5.3%; the Governor: 1 of 9) before those they don't (the House, by the county's tally).
    shown = tg.target_contests(votes, results, groups)
    assert [(c.name, c.result_source) for c in shown] == [("United States Senator", "state"), ("Governor", "state"), (HOUSE.split(" (")[0], "county")]
    assert shown[0].result_margin == 64 and shown[0].result_total == 1216 and shown[0].from_results and shown[1].result_margin == 1
    # CORLA's posted totals cover a contest the state's results don't: used for it, by its margin over its ballot cards; the state's results win where both have it.
    corla = {GOVERNOR: tg.CorlaTotal(2, 1000), HOUSE: tg.CorlaTotal(300, 1000)}
    shown = tg.target_contests(votes, results, groups, corla)
    assert [(c.name, c.result_source, c.result_margin) for c in shown] == [("United States Senator", "state", 64), ("Governor", "state", 1), (HOUSE.split(" (")[0], "corla", 300)]
    assert shown[2].result_share == 0.3
    # Without the state's results, CORLA's totals order what they cover first.
    shown = tg.target_contests(votes, None, groups, corla)
    assert [(c.name, c.result_source) for c in shown] == [("Governor", "corla"), ("United States Senator", "county"), (HOUSE.split(" (")[0], "corla")]
    # Nothing loaded: the county's tally, marked as such.
    assert all(c.result_source == "county" for c in tg.target_contests(votes, None, groups))


def test_selection_and_pattern_round_trip(votes: tg.CvrVotes) -> None:
    s, g = contest(votes, SENATE), contest(votes, GOVERNOR)
    selection = tg.parse_selection(f"{g.index}:1, {s.index}:0,99:0,{s.index}:9,x", votes)
    assert selection == {s.index: [0], g.index: [1]} if s.index < g.index else {g.index: [1], s.index: [0]}
    definition = tg.pattern_definition(selection, votes)
    assert definition == [{"contest": "Governor", "choices": ["Hal Gov"]}, {"contest": "United States Senator", "choices": ["Ann Senate"]}]
    assert tg.requirement_of(definition, votes) == selection
    assert tg.requirement_of([{"contest": "Governor", "choices": ["Nobody"]}], votes) is None
    assert tg.describe(definition) == "Governor: Hal Gov; United States Senator: Ann Senate"
    pattern = tg.pattern_of(COUNTY, definition)
    assert pattern.name == tg.describe(definition) and pattern.sha256 == tg.pattern_of(COUNTY, definition).sha256
    with pytest.raises(tg.DrawError, match="at least one"):
        tg.pattern_of(COUNTY, [])


def _pattern(votes: tg.CvrVotes) -> tg.Pattern:
    s, h = contest(votes, SENATE), contest(votes, HOUSE)
    return tg.pattern_of(COUNTY, tg.pattern_definition({s.index: [0], h.index: [0]}, votes))


LOCATIONS = {("1", str(b)): ("Box 10" if b == 1 else "Box 2") for b in range(1, 10)}


def _draw(votes: tg.CvrVotes, number: int = 1, **kwargs: object) -> tg.DrawRecord:
    args: dict[str, object] = {"county": COUNTY, "cvr_sha256": "a" * 64, "pattern": _pattern(votes), "seed": "12345", "seed_source": "entered",
                               "locations": LOCATIONS, "manifest_sha256": None, "actor": ACTOR} | kwargs  # fmt: skip
    return tg.draw(votes, number=number, **args)  # type: ignore[arg-type]


def test_the_draw_is_reproducible_and_in_corla_order(votes: tg.CvrVotes) -> None:
    first, again, other = _draw(votes), _draw(votes), _draw(votes, number=2)
    ids = [b["imprinted_id"] for b in first.ballots]
    assert ids == [b["imprinted_id"] for b in again.ballots] and len(ids) == tg.DRAW_SIZE
    assert set(ids) != {b["imprinted_id"] for b in other.ballots}
    assert first.matching == 46  # Ann Senate and Hank House: 30 + 6, and 10 with the Governor redacted
    assert first.method == tg.METHOD and first.format == 2 and first.collisions == []
    # The derived seed and the keys are as documented.
    derived = tg.derived_seed(county=COUNTY, cvr_sha256="a" * 64, definition=first.definition, seed="12345", draw=1)
    assert first.derived_seed == derived
    matching = [votes.imprinted(i) for i in tg.bits_of(votes.match(tg.requirement_of(first.definition, votes) or {}))]
    expected = sorted(matching, key=lambda i: (tg.ballot_key(derived, i), i))[: tg.DRAW_SIZE]
    assert sorted(ids) == sorted(expected)
    assert ids == corla_order(expected, LOCATIONS)
    # Box 2 before Box 10 (natural order), then batch and position as numbers.
    boxes = [b["location"] for b in first.ballots]
    assert boxes == sorted(boxes, key=lambda b: int(b.split()[1]))
    assert all(b["tabulator"] == "1" and b["ballot_type"] == "Style 1" for b in first.ballots)


def test_a_draw_skips_ballots_already_held_and_picks_more(votes: tg.CvrVotes) -> None:
    """Collisions with the working list, a sent list or the RLA sample are skipped and noted; the draw still has N distinct new ballots."""
    plain = _draw(votes, size=5)
    first_two = [b["imprinted_id"] for b in plain.ballots[:2]]
    held = {id_key(first_two[0]): "the working list", id_key(first_two[1]): "Targeted list 1"}
    skipping = _draw(votes, size=5, exclude=held)
    ids = [b["imprinted_id"] for b in skipping.ballots]
    assert len(ids) == 5 and len(set(ids)) == 5 and not set(ids) & set(first_two)
    assert {c["imprinted_id"] for c in skipping.collisions} == set(first_two)
    assert {c["held_by"] for c in skipping.collisions} == {"the working list", "Targeted list 1"}
    # The same inputs and exclusions give the same draw; the ballots kept are the plain draw's others, then the next in key order.
    assert [b["imprinted_id"] for b in _draw(votes, size=5, exclude=held).ballots] == ids
    assert set(b["imprinted_id"] for b in plain.ballots) - set(first_two) <= set(ids)
    # A collision beyond the N-th pick is not a collision: it was never going to be drawn.
    by_key = sorted(plain_ids := [b["imprinted_id"] for b in _draw(votes, size=46).ballots], key=lambda i: (tg.ballot_key(plain.derived_seed, i), i))
    far = {id_key(i): "the RLA sample" for i in by_key[-3:]}
    assert len(plain_ids) == 46 and _draw(votes, size=5, exclude=far).collisions == []
    # Everything held: nothing to draw.
    with pytest.raises(tg.DrawError, match="already on a list"):
        _draw(votes, size=5, exclude={id_key(b["imprinted_id"]): "the RLA sample" for b in _draw(votes, size=46).ballots})


def test_the_seed_is_any_text(votes: tg.CvrVotes) -> None:
    worded = _draw(votes, seed="  the  observers'   own words, 2026 ")
    assert worded.seed == "the observers' own words, 2026" and len(worded.ballots) == tg.DRAW_SIZE
    assert worded.derived_seed != _draw(votes, seed="12345").derived_seed
    assert tg.clean_seed("x\ty") == "x y"
    with pytest.raises(tg.DrawError, match="enter a seed"):
        _draw(votes, seed="   ")
    with pytest.raises(tg.DrawError, match="at most"):
        _draw(votes, seed="s" * (tg.MAX_SEED + 1))


def test_no_draw_without_a_match(votes: tg.CvrVotes) -> None:
    s, g = contest(votes, SENATE), contest(votes, GOVERNOR)
    pattern = tg.pattern_of(COUNTY, tg.pattern_definition({s.index: [2], g.index: [1]}, votes))
    with pytest.raises(tg.DrawError, match="no ballot"):
        _draw(votes, pattern=pattern)
    rare = tg.pattern_of(COUNTY, tg.pattern_definition({s.index: [2]}, votes))
    record = _draw(votes, pattern=rare)
    assert record.matching == 2 and len(record.ballots) == 2  # a rare pattern is fine


def test_email_text_and_csv(votes: tg.CvrVotes) -> None:
    record = _draw(votes, number=3, locations={("1", "1"): "Box 10"})
    record.time = "2026-11-12T15:00:00.000Z"
    text = tg.draw_email(record, "b" * 64)
    lines = text.splitlines()
    assert lines[0] == f"Targeted ballots for {COUNTY}: draw 3"
    assert lines[1] == "20 ballots (draw size 20), draw 3, seed “12345”, drawn 2026-11-12 (targeted ballots, not part of the RLA sample)"
    assert lines[2] == "Record " + "b" * 16 and lines[3] == ""
    table = lines[4:]
    assert table[0].split() == ["#", "Imprinted", "ID", "Tabulator", "Batch", "Position", "Storage", "location", "Ballot", "type"]
    assert len(table) == 21 and all(len(line) <= len(table[0]) for line in table)
    assert table[1].index(record.ballots[0]["imprinted_id"]) == table[0].index("Imprinted ID")  # aligned
    assert "Ann Senate" not in text and "match" not in text  # no expected votes by default
    expected = tg.expected_for([b["imprinted_id"] for b in record.ballots], record.definition, votes)
    shown = tg.draw_email(record, "b" * 64, expected)
    assert "Pattern: Representative to the 120th United States Congress - District 8: Hank House; United States Senator: Ann Senate" in shown
    assert assert_csv(tg.to_csv(COUNTY, "draw 3", record.ballots), 20)
    assert tg.to_csv("=evil", "x", [{"imprinted_id": "1-1-1"}]).splitlines()[1].startswith("'=evil")
    # A list's text names the list, and says which ballots the RLA sample also holds.
    entries = [tg.Entry("1-2-1", ["draw 1"], location="Box 2"), tg.Entry("1-1-3", ["entry 7"], location="Box 10", in_rla=True)]
    listed = tg.list_email(COUNTY, "Targeted list 2", entries, "sent 2026-11-12", ["Extract abc"])
    assert listed.startswith(f"Targeted list 2 for {COUNTY}\n2 ballots, sent 2026-11-12 (targeted ballots, not part of the RLA sample); 1 also in the RLA sample")
    assert "(also in the RLA sample)" in listed.splitlines()[-1] and "Extract abc" in listed


def assert_csv(text: str, rows: int) -> bool:
    parsed = list(csv.reader(io.StringIO(text)))
    return parsed[0][:4] == ["county", "list", "position", "imprinted_id"] and len(parsed) == rows + 1


def test_lookup_and_ids(votes: tg.CvrVotes) -> None:
    sizes = {("1", str(b)): 10 for b in range(1, 10)}
    found = tg.lookup("1-1-5", votes, {("1", "1"): "Box 10"}, sizes)
    assert found.cvr_status == "ballot" and found.in_manifest and found.location == "Box 10" and found.ballot_type == "Style 1"
    assert tg.lookup("1-9-3", votes, {}, sizes).cvr_status == "redacted"  # the fully blank row
    beyond = tg.lookup("1-9-9", votes, {}, sizes)
    assert beyond.cvr_status is None and beyond.in_manifest and beyond.found
    assert not tg.lookup("2-1-1", votes, {}, sizes).found
    assert tg.normalize_id(" 101 - 12 - 5 ") == "101-12-5" and tg.normalize_id("hello") is None


def _workspace(tmp_path: Path) -> tuple[Workspace, Path]:
    root = tmp_path / "ws"
    store = Workspace.open(root)
    files = tmp_path / "files"
    files.mkdir()
    (files / "cvr.csv").write_bytes(cvr_bytes())
    (files / "manifest.csv").write_bytes(manifest_bytes())
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", PASSWORD, log)
        assert receive(store, log, COUNTY, "redacted_cvr", files / "cvr.csv", "cvr.csv", ACTOR).accepted
        assert receive(store, log, COUNTY, "manifest", files / "manifest.csv", "manifest.csv", ACTOR).accepted
    return store, root


def _cvr_sha(log: EventLog) -> str:
    from corla_observer.uploads import status

    return status(log, [COUNTY])[COUNTY].latest["redacted_cvr"].sha256


def _recorded_draw(store: Workspace, log: EventLog, votes: tg.CvrVotes, requirement: dict[int, list[int]], size: int = 3, seed: str = "s") -> tg.DrawRecord:
    manifest = county_manifest(store, log, COUNTY)
    found = tg.sources(store, log, COUNTY)
    record = tg.draw(
        votes, county=COUNTY, cvr_sha256=_cvr_sha(log), pattern=tg.pattern_of(COUNTY, tg.pattern_definition(requirement, votes)), seed=seed,
        seed_source="entered", number=tg.draw_count(log, COUNTY) + 1, locations=manifest.locations, manifest_sha256=manifest.sha256, actor=ACTOR,
        size=size, exclude=tg.holders(store, log, COUNTY, found),
    )  # fmt: skip
    return tg.record_draw(store, log, record)[0]


def test_sources_the_working_list_and_sending(tmp_path: Path) -> None:
    store, root = _workspace(tmp_path)
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    s, g = contest(votes, SENATE), contest(votes, GOVERNOR)
    with EventLog.open(root) as log:
        manifest = county_manifest(store, log, COUNTY)
        seq = tg.enter_ids(store, log, COUNTY, ["1-2-1", "1-1-3", "1-1-3"], ACTOR, note="seen in box 10")
        drawn = _recorded_draw(store, log, votes, {s.index: [0], g.index: [1]})
        found = tg.sources(store, log, COUNTY)
        assert [(x.id, x.kind, x.included, x.sent_in) for x in found] == [(f"entry {seq}", "entered", True, None), ("draw 1", "draw", True, None)]
        assert found[0].ids == ["1-2-1", "1-1-3"] and found[0].note == "seen in box 10" and "seen in box 10" in found[0].description
        assert found[1].ids == drawn.ids and "seed “s”" in found[1].description and found[1].title == "Draw 1"
        entries = tg.working_list(found, votes, manifest.locations, manifest.sizes)
        assert len(entries) == 2 + 3 - len(set(drawn.ids) & {"1-2-1", "1-1-3"})
        assert [e.imprinted_id for e in entries] == corla_order([e.imprinted_id for e in entries], manifest.locations)  # Box 2 before Box 10
        assert all(e.in_manifest for e in entries) and entries[0].sources
        # Excluding a source takes its ballots out (unless another source has them); including it again puts them back.
        tg.set_included(log, COUNTY, "draw 1", False, ACTOR)
        assert [e.imprinted_id for e in tg.working_list(tg.sources(store, log, COUNTY), votes, manifest.locations, manifest.sizes)] == ["1-2-1", "1-1-3"]
        tg.set_included(log, COUNTY, "draw 1", True, ACTOR)
        assert len(tg.working_list(tg.sources(store, log, COUNTY), votes, manifest.locations, manifest.sizes)) == len(entries)
        with pytest.raises(tg.DrawError, match="no such source"):
            tg.set_included(log, COUNTY, "draw 9", True, ACTOR)
        # A second draw skips the working list's ballots and says so.
        second = _recorded_draw(store, log, votes, {s.index: [0], g.index: [1]}, size=4)
        assert not set(second.ids) & {e.imprinted_id for e in entries}
        assert {c["held_by"] for c in second.collisions} <= {"the working list"} and second.format == 2

        # Send: Targeted list 1, frozen, with its extract and record; the sources leave the working list.
        sent = tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)
        assert sent.number == 1 and sent.name == "Targeted list 1" and sent.key == targeted_key("Targeted list 1")
        assert sorted(sent.sources) == sorted([f"entry {seq}", "draw 1", "draw 2"]) and len(sent.entries) == len(entries) + 4
        event = [e for e in log.iter_events() if e.kind == "targets.sent"][-1]
        assert event.data["number"] == 1 and event.data["ballots"] == len(sent.entries) and event.data["extract_sha256"] == sent.extract_sha256
        assert event.data["list_record_sha256"] == sent.record_sha256 and event.data["in_rla"] == 0
        record = json.loads(store.get_bytes(sent.record_sha256))
        assert record["format"] == 2 and record["number"] == 1 and {x["id"] for x in record["sources"]} == set(sent.sources)
        assert all(set(b) == {"imprinted_id", "location", "sources", "in_rla"} for b in record["ballots"])
        assert tg.record_patterns(record)[0]["description"] == "Governor: Hal Gov; United States Senator: Ann Senate"
        assert all(x.sent_in == 1 and x.sent_key == sent.key for x in tg.sources(store, log, COUNTY))
        assert tg.working_list(tg.sources(store, log, COUNTY), votes, manifest.locations, manifest.sizes) == []
        with pytest.raises(tg.DrawError, match="already sent|sent in Targeted list 1"):
            tg.set_included(log, COUNTY, "draw 1", False, ACTOR)
        with pytest.raises(tg.DrawError, match="empty"):
            tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)
        (item,) = pulllists.county_lists(log.iter_events(), COUNTY)
        assert item.targeted and item.sha256 == sent.extract_sha256 and item.number == 1 and item.label == "Targeted list 1" and item.public_label == "Targeted list 1"
        extract = pulllists.load(store, item)
        assert extract["order"] == "targeted" and [b["imprinted_id"] for b in extract["ballots"]] == [e.imprinted_id for e in sent.entries]
        assert latest_extract(store, log, COUNTY) is None  # a targeted extract is never the county's RLA extract
        assert pulllists.details(store, item)["patterns"][0]["draw"] == 1 and len(pulllists.details(store, item)["ids"]) == len(sent.entries)

        # The next working list: a draw skips the sent list's ballots; sending makes Targeted list 2.
        third = _recorded_draw(store, log, votes, {s.index: [0], g.index: [1]}, size=2)
        assert {c["held_by"] for c in third.collisions} <= {"Targeted list 1"} and not set(third.ids) & {e.imprinted_id for e in sent.entries}
        again = tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)
        assert again.number == 2 and again.name == "Targeted list 2" and again.sources == ["draw 3"]
        assert [x.label for x in pulllists.county_lists(log.iter_events(), COUNTY)] == ["Targeted list 1", "Targeted list 2"]
        # Nothing of a pattern in the events.
        assert all("Hal Gov" not in json.dumps(e.data) for e in log.iter_events())


def test_overlaps_are_marked_both_ways(tmp_path: Path) -> None:
    store, root = _workspace(tmp_path)
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    with EventLog.open(root) as log:
        manifest = county_manifest(store, log, COUNTY)
        rla = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=6)
        sampled = [b["imprinted_id"] for b in rla.extract["ballots"]]
        assert rla.extract["checks"].get("previously_targeted") is None and rla.seq
        # A targeted list with one of the sample's ballots: marked on the list, its record, its extract and the event.
        tg.enter_ids(store, log, COUNTY, [sampled[0], "1-2-1"], ACTOR)
        found = tg.sources(store, log, COUNTY)
        entries = tg.working_list(found, votes, manifest.locations, manifest.sizes, tg.rla_ids(store, log, COUNTY))
        assert {e.imprinted_id: e.in_rla for e in entries} == {sampled[0]: True, "1-2-1": "1-2-1" in sampled}
        held = tg.holders(store, log, COUNTY, found)
        assert held[id_key(sampled[1])] == "the RLA sample" and held[id_key("1-2-1")] in ("the working list", "the RLA sample")
        sent = tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)
        assert sent.in_rla >= 1
        item = pulllists.county_lists(log.iter_events(), COUNTY)[-1]
        extract = pulllists.load(store, item)
        marked = [b for b in extract["ballots"] if b.get("in_rla")]
        assert [b["imprinted_id"] for b in marked] == [e.imprinted_id for e in sent.entries if e.in_rla]
        assert extract["checks"]["in_rla"] == [b["imprinted_id"] for b in marked]
        built = [e for e in log.iter_events() if e.kind == "extract.built"][-1]
        assert built.data["in_rla"] == len(marked) and built.data["previously_targeted"] == 0
        from corla_observer.extract import IN_RLA_MARK, ballot_marks

        assert IN_RLA_MARK in ballot_marks(marked[0])
        text = tg.list_email(COUNTY, item.label, sent.entries, "sent today")
        assert "also in the RLA sample: no need to pull them again" in text or "also in the RLA sample: no need to pull it again" in text
        # The RLA extract rebuilt: its ballots on the earlier list are marked, counted in its checks and its event.
        again = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=6)
        flagged = [b for b in again.extract["ballots"] if b.get("targeted_in")]
        assert {b["imprinted_id"] for b in flagged} == {b["imprinted_id"] for b in marked}
        assert all(b["targeted_in"] == "Targeted list 1" for b in flagged)
        assert again.extract["checks"]["previously_targeted"] == [{"imprinted_id": b["imprinted_id"], "list": "Targeted list 1"} for b in flagged]
        event = [e for e in log.iter_events() if e.kind == "extract.built"][-1]
        assert event.data["previously_targeted"] == len(flagged) and event.data["in_rla"] == 0
        assert "PREVIOUSLY TARGETED: Targeted list 1, may already have been pulled" in ballot_marks(flagged[0])
        # On paper too: the printout draws the marks line.
        from corla_observer.pdfextract import printout

        assert printout(store, log, again.extract, again.sha256, 1, "portrait-2").startswith(b"%PDF")


def test_lists_built_by_the_first_design_read_as_sent_lists(tmp_path: Path) -> None:
    """The first design's events (a draw added to the pull list, built under a name, emptied; a draw never added; an ID entered) give the new model."""
    store, root = _workspace(tmp_path)
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    s, g = contest(votes, SENATE), contest(votes, GOVERNOR)
    with EventLog.open(root) as log:
        manifest = county_manifest(store, log, COUNTY)
        definition = tg.pattern_definition({s.index: [0], g.index: [1]}, votes)
        pattern = {"kind": "targeted_pattern", "format": 1, "county": COUNTY, "name": "Pattern A", "definition": definition}
        pattern_sha = store.put_bytes(canonical_json(pattern)).sha256
        log.append("targets.pattern_saved", ACTOR, county=COUNTY, file_sha256=pattern_sha, data={"sha256": pattern_sha})

        def old_draw(number: int, size: int) -> tg.DrawRecord:
            derived = tg.derived_seed(county=COUNTY, cvr_sha256=_cvr_sha(log), definition=definition, seed="20201106", draw=number, method=tg.METHOD_1)
            matching = [votes.imprinted(i) for i in tg.bits_of(votes.match({s.index: [0], g.index: [1]}))]
            chosen = sorted(matching, key=lambda i: (tg.ballot_key(derived, i), i))[:size]
            record = tg.DrawRecord(
                COUNTY, "Pattern A", pattern_sha, definition, _cvr_sha(log), manifest.sha256, "20201106", "entered for this draw", number, derived,
                len(matching), len(matching), [{"imprinted_id": i, "tabulator": "1", "batch": i.split("-")[1], "record": i.split("-")[2],
                                                 "location": manifest.locations.get(("1", i.split("-")[1])), "ballot_type": "Style 1"} for i in corla_order(chosen, manifest.locations)],
                ACTOR, "2026-10-29T16:04:59.064Z", tg.METHOD_1, size, [], 1,
            )  # fmt: skip
            sha = store.put_bytes(canonical_json(record.to_json())).sha256
            log.append("targets.drawn", ACTOR, county=COUNTY, file_sha256=sha, data={"draw": number, "ballots": len(chosen), "size": size, "sha256": sha, "pattern_sha256": pattern_sha})
            return record

        first = old_draw(1, 6)  # never added to the pull list
        second = old_draw(2, 3)  # added, built as "p1", emptied
        log.append("targets.listed", ACTOR, county=COUNTY, data={"ids": second.ids, "source": "draw 2"})
        entries = [tg.lookup(i, votes, manifest.locations, manifest.sizes) for i in second.ids]
        for e, i in zip(entries, second.ids, strict=True):
            e.imprinted_id = i
        record = {
            "kind": "targeted_list", "format": 1, "note": "Targeted ballots: not part of the RLA sample.", "county": COUNTY, "name": "p1",
            "ballots": [{"imprinted_id": e.imprinted_id, "source": "draw 2", "location": e.location} for e in entries],
            "patterns": [{"draw": 2, "name": "Pattern A", "definition": definition, "description": tg.describe(definition), "size": 3}],
        }  # fmt: skip
        record_sha = store.put_bytes(canonical_json(record)).sha256
        built = build_extract(store, log, COUNTY, actor=ACTOR, targeted=tg.pull_list_bytes(COUNTY, entries), list_name="p1", list_record=record_sha)
        log.append("targets.unlisted", ACTOR, county=COUNTY, data={"ids": second.ids})
        third = old_draw(3, 4)  # never added
        log.append("targets.listed", ACTOR, county=COUNTY, data={"ids": ["1-7-5"], "source": "entered"})
        entered_seq = log.head()[0]

        found = {x.id: x for x in tg.sources(store, log, COUNTY)}
        assert set(found) == {"draw 1", "draw 2", "draw 3", f"entry {entered_seq}"}
        assert found["draw 2"].sent_in == 1 and found["draw 2"].sent_key == targeted_key("p1") and found["draw 2"].ids == second.ids
        assert found["draw 1"].sent_in is None and not found["draw 1"].included and found["draw 1"].ids == first.ids
        assert found["draw 3"].sent_in is None and not found["draw 3"].included and len(found["draw 3"].ids) == 4 and third.format == 1
        assert found[f"entry {entered_seq}"].included and found[f"entry {entered_seq}"].ids == ["1-7-5"]
        working = tg.working_list(list(found.values()), votes, manifest.locations, manifest.sizes)
        assert [e.imprinted_id for e in working] == ["1-7-5"]
        (item,) = pulllists.county_lists(log.iter_events(), COUNTY)
        assert item.name == "p1" and item.number == 1 and item.label == "Targeted list 1 (p1)" and item.public_label == "Targeted list 1" and item.sha256 == built.sha256
        assert pulllists.details(store, item)["patterns"] == [{"draw": 2, "name": "Pattern A", "description": tg.describe(definition)}]
        assert tg.sent_entries(record)[0].sources == ["draw 2"]
        # Including draw 3 by hand, then sending: Targeted list 2, after p1.
        tg.set_included(log, COUNTY, "draw 3", True, ACTOR)
        assert tg.next_list_number(log, COUNTY) == (2, "Targeted list 2")
        sent = tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)
        assert sent.number == 2 and sorted(sent.sources) == ["draw 3", f"entry {entered_seq}"] and len(sent.entries) == 5
        assert [x.label for x in pulllists.county_lists(log.iter_events(), COUNTY)] == ["Targeted list 1 (p1)", "Targeted list 2"]
        # A new draw skips both lists' ballots.
        fourth = _recorded_draw(store, log, votes, {s.index: [0], g.index: [1]}, size=46)
        assert {c["held_by"] for c in fourth.collisions} == {"Targeted list 1", "Targeted list 2"}
        assert not set(fourth.ids) & (set(second.ids) | set(third.ids) | {"1-7-5"})


def test_the_coordinator_pages(tmp_path: Path) -> None:
    _, root = _workspace(tmp_path)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, counties=("Boulder",), crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(f"/coord/targets?county={COUNTY}", follow_redirects=False).status_code == 303
        assert client.get(f"/coord/targets/count?county={COUNTY}").status_code == 403
        assert login(client).status_code == 303
        page = client.get(f"/coord/targets?county={COUNTY}")
        assert page.status_code == 200 and "United States Senator" in page.text and "Split tickets" in page.text
        assert "Demonstration county" in page.text and "No results are loaded" in page.text and "No candidate checked yet" in page.text
        votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
        s, g, local = contest(votes, SENATE), contest(votes, GOVERNOR), contest(votes, LOCAL)
        counted = client.get(f"/coord/targets/count?county={COUNTY}&v={s.index}:0&v={g.index}:1").json()
        assert counted["matching"] == 9 and counted["selection"] and f"{local.index}:any" not in counted["grid"]
        # A contest added by name joins the grid and the counts.
        searched = client.get(f"/coord/targets?county={COUNTY}&q=commissioner").text
        assert "Synthetic County Commissioner" in searched and "add=Synthetic" in searched
        added = client.get(f"/coord/targets?county={COUNTY}&add=Synthetic+County+Commissioner&v={local.index}:0")
        assert "Other contests added" in added.text and f'name="v" value="{local.index}:0" data-contest="{local.index}" checked' in added.text
        assert f"{local.index}:any" in client.get(f"/coord/targets/count?county={COUNTY}&add=Synthetic+County+Commissioner").json()["grid"]
        csrf = csrf_of(page)
        # Draw from the grid, with a seed in words: the draw is a source of the working list.
        drawn = client.post("/coord/targets/draw", data={"csrf": csrf, "county": COUNTY, "selection": counted["selection"], "seed": "our own words", "size": "4"},
                            follow_redirects=False)  # fmt: skip
        assert drawn.status_code == 303 and "drawn=1" in drawn.headers["location"]
        page = client.get(drawn.headers["location"]).text
        assert "Draw 1: 4 ballots added to the working list." in page and "seed “our own words”" in page and "The working list now: 4 ballots" in page
        assert 'name="source" value="draw 1"' in page and "Send to county: make Targeted list 1" in page
        view = client.get(f"/coord/targets/draw?county={COUNTY}&n=1")
        assert "4 (draw size 4) of the 9 ballots" in view.text and "In the working list." in view.text
        assert "Expected votes" not in view.text and "Hal Gov" not in view.text.split("Copy for email")[1]
        assert "Hal Gov" in client.get(f"/coord/targets/draw?county={COUNTY}&n=1&votes=1").text
        assert client.get(f"/coord/targets/draw/csv?county={COUNTY}&n=1").status_code == 200
        assert json.loads(client.get(f"/coord/targets/draw/record?county={COUNTY}&n=1").content)["draw"] == 1
        # Draw again from the source: another draw, skipping the first's ballots.
        again = client.post("/coord/targets/draw/again", data={"csrf": csrf, "county": COUNTY, "source": "draw 1"}, follow_redirects=False)
        assert again.status_code == 303 and "drawn=2" in again.headers["location"]
        page = client.get(again.headers["location"]).text
        assert "Draw 2: 4 ballots added to the working list; 4 already listed or sampled, skipped." in page and "The working list now: 8 ballots" in page
        # IDs entered by hand, with a note: one source, its lookups shown inline.
        looked = client.post("/coord/targets/list/add", data={"csrf": csrf, "county": COUNTY, "ids": "1-1-1, 9-9-9, junk", "note": "box 10 looked odd"})
        assert "1-1-1: in the CVR, in the manifest, stored at Box 10" in looked.text and "9-9-9: not found" in looked.text and "junk: not an imprinted ID" in looked.text
        assert "Added 1 to the working list" in looked.text and "box 10 looked odd" in looked.text
        with EventLog.open(root) as log:
            entry_id = next(x.id for x in tg.sources(Workspace.open(root), log, COUNTY) if x.kind == "entered")
        # Excluding a source, and including it again.
        excluded = client.post("/coord/targets/include", data={"csrf": csrf, "county": COUNTY, "source": "draw 2", "included": "0"}, follow_redirects=False)
        assert excluded.status_code == 303 and "msg=excluded" in excluded.headers["location"]
        page = client.get(f"/coord/targets?county={COUNTY}").text
        assert "The working list now: 5 ballots" in page and 'class="is-excluded"' in page
        listed = client.get(f"/coord/targets/list/csv?county={COUNTY}").content.decode("utf-8-sig")
        assert len(listed.splitlines()) == 6 and "1-1-1" in listed
        assert client.post("/coord/targets/include", data={"csrf": csrf, "county": COUNTY, "source": "draw 2", "included": "1"}, follow_redirects=False).status_code == 303
        assert client.post("/coord/targets/include", data={"csrf": csrf, "county": COUNTY, "source": "draw 7", "included": "1"}).status_code == 400
        # Send to county.
        sent = client.post("/coord/targets/send", data={"csrf": csrf, "county": COUNTY, "boards": "1"}, follow_redirects=False)
        key = targeted_key("Targeted list 1")
        assert sent.status_code == 303 and "msg=sent" in sent.headers["location"] and f"list={key}" in sent.headers["location"]
        page = client.get(sent.headers["location"]).text
        assert "Targeted list 1 sent to the county" in page and "(9 ballots, marked TARGETED (NOT RLA)" in page
        assert f"Targeted list 1 for {COUNTY}" in page and "their links open on it" in page  # no RLA sample yet
        assert "Empty: check a source above" in page and "Send to county: make Targeted list 2" not in page
        assert f"list={key}\" download>PDF, portrait" in page
        extracts = client.get("/coord/extract").text
        assert "TARGETED (NOT RLA)" in extracts and "Targeted list 1" in extracts and f"No RLA-sample extract for {COUNTY} yet" in extracts
        listed = client.get(f"/coord/targets/sent/csv?county={COUNTY}&list={key}").content.decode("utf-8-sig")
        assert len(listed.splitlines()) == 10 and "Targeted list 1" in listed
        assert json.loads(client.get(f"/coord/targets/sent/record?county={COUNTY}&list={key}").content)["number"] == 1
        with EventLog.open(root) as log:
            found = {x.id: x for x in tg.sources(Workspace.open(root), log, COUNTY)}
            assert {found["draw 1"].sent_in, found["draw 2"].sent_in, found[entry_id].sent_in} == {1}
        # Sending nothing is refused; the next list is 2.
        assert client.post("/coord/targets/send", data={"csrf": csrf, "county": COUNTY, "boards": "1"}).status_code == 400
        assert client.post("/coord/targets/draw", data={"csrf": csrf, "county": COUNTY, "selection": counted["selection"], "seed": "x", "size": "1"},
                           follow_redirects=False).status_code == 303  # fmt: skip
        page = client.get(f"/coord/targets?county={COUNTY}").text
        assert "Send to county: make Targeted list 2" in page and "Draw 3" in page
        # Publishing a list's details, and retiring it.
        assert client.post("/coord/targets/lists", data={"csrf": csrf, "county": COUNTY, "list": key, "action": "publish"}, follow_redirects=False).status_code == 303
        with EventLog.open(root) as log:
            published = [e for e in log.iter_events() if e.kind == "extract.details_published"][0]
            details: Any = published.data["details"]
            assert details["name"] == "Targeted list 1" and len(details["ids"]) == 9
            assert "Hal Gov" in details["patterns"][0]["description"]
        assert client.post("/coord/targets/lists", data={"csrf": csrf, "county": COUNTY, "list": key, "action": "retire"}, follow_redirects=False).status_code == 303
        assert "retired" in client.get(f"/coord/targets?county={COUNTY}").text
        # Nothing of it on the public pages.
        public = client.get("/").text
        assert "Hal Gov" not in public and "targets" not in public.lower().split("<footer")[0].split("<main")[1]


def test_a_button_pressed_twice_sends_one_list(tmp_path: Path) -> None:
    """Two "Send to county" requests at once: one list, one extract, one targets.sent."""
    from concurrent.futures import ThreadPoolExecutor

    _, root = _workspace(tmp_path)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, counties=("Boulder",), crawl=lambda ws, log: None))
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    s, g = contest(votes, SENATE), contest(votes, GOVERNOR)
    with TestClient(app, base_url="https://testserver") as client:
        assert login(client).status_code == 303
        csrf = csrf_of(client.get(f"/coord/targets?county={COUNTY}"))
        assert client.post("/coord/targets/draw", data={"csrf": csrf, "county": COUNTY, "selection": f"{s.index}:0,{g.index}:1", "seed": "42", "size": "5"},
                           follow_redirects=False).status_code == 303  # fmt: skip
        data = {"csrf": csrf, "county": COUNTY, "boards": "1"}
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: client.post("/coord/targets/send", data=data, follow_redirects=False), range(2)))
        assert sorted(r.status_code for r in responses) == [303, 400]
        with EventLog.open(root) as log:
            kinds = [e.kind for e in log.iter_events()]
            assert kinds.count("extract.built") == 1 and kinds.count("targets.sent") == 1
            assert [x.name for x in pulllists.county_lists(log.iter_events(), COUNTY)] == ["Targeted list 1"]
