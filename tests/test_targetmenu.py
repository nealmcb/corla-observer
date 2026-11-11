"""Fast tests for the targeting menu and domain draws (corla_observer.targetmenu, docs/TARGETING.md, "The targeting menu")."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from corla_observer import targetmenu as tm
from corla_observer import targets as tg
from corla_observer.cli import app as cli_app
from corla_observer.events import EventLog, canonical_json
from corla_observer.extract import county_manifest, id_key
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr
from test_targets import ACTOR, COUNTY, LOCAL, _cvr_sha, _workspace
from test_web import csrf_of, login

MAYOR, MEASURE, FIRE, COUNCIL = "Mayor", "Measure 1", "Fire District", "Council"


# ---------------------------------------------------------------------------
# Kaplan-Markov, discrepancies, the recount rule and the categories


def test_kaplan_markov_matches_known_values() -> None:
    # rlacalc.KM_P_value (Stark's audit notebook) on the same inputs, and its own New Hampshire 2016 example.
    assert tm.km_risk(10, 0.1) == pytest.approx(0.6106860591035885, rel=1e-12)
    assert tm.km_risk(50, 0.02) == pytest.approx(0.6165971529284351, rel=1e-12)
    assert tm.km_risk(100, 0.05, o1=1) == pytest.approx(0.16877171576286734, rel=1e-12)
    assert tm.km_risk(200, 0.3, 2, 1, 0, 1) == pytest.approx(
        1.4460612646034847e-12, rel=1e-9
    )
    nh = (354040 - 337589) / (354040 + 337589 + 33234)
    assert tm.km_risk(200, nh, 1) == pytest.approx(0.21438135077031842, rel=1e-12)
    # Capped at 1; no margin, no risk below 1.
    assert (
        tm.km_risk(100, 0.05, 1, 1, 1, 1) == 1.0
        and tm.km_risk(0, 0.1) == 1.0
        and tm.km_risk(500, 0.0) == 1.0
    )
    # The fewest draws: the risk is at or under the target at k, above it at k - 1.
    k = tm.draws_to(0.04, 0.1)
    assert k == 66 and tm.km_risk(k, 0.1) <= 0.04 < tm.km_risk(k - 1, 0.1)
    assert (
        tm.draws_to(0.04, 0.1, n0=60) == 6
        and tm.draws_to(0.04, 0.1, n0=70) == 0
        and tm.draws_to(0.04, 0.0) is None
    )
    k = tm.draws_to(0.03, 0.05, 20, o1=1)
    assert k is not None and tm.km_risk(20 + k, 0.05, 1) <= 0.03 < tm.km_risk(
        19 + k, 0.05, 1
    )


def test_overstatements_as_corla_counts_them() -> None:
    w, lo = {"Ann"}, {"Bob", "Cy"}
    assert tm.overstatement({"Ann"}, {"Bob"}, w, lo) == 2
    assert tm.overstatement({"Ann"}, set(), w, lo) == 1
    assert tm.overstatement(set(), {"Ann"}, w, lo) == -1
    assert tm.overstatement({"Bob"}, {"Ann"}, w, {"Bob"}) == -2
    assert (
        tm.overstatement({"Bob"}, {"Ann"}, w, lo) == -1
    )  # the largest over the pairs: Ann-Cy gives -1
    assert (
        tm.overstatement({"Ann"}, {"ann "}, w, lo) == 0
    )  # compared ignoring case and spaces
    assert tm.overstatement({"Bob"}, {"Cy"}, w, lo) == 1  # Ann-Cy: (0 - 0) - (0 - 1)
    assert tm.overstatement({"Ann"}, {"Bob"}, w, set()) == 0
    assert tm.Discrepancies.of(2) == tm.Discrepancies(o2=1) and tm.Discrepancies.of(
        -1
    ) == tm.Discrepancies(u1=1)
    assert (
        tm.Discrepancies(o1=1) + tm.Discrepancies(o1=1, u2=1)
    ).text() == "2 o1, 1 u2"


def test_the_recount_rule_and_uncontested_contests() -> None:
    # 0.5% of the highest vote; for a multi-winner contest, of the lowest winner's votes (C.R.S. 1-10.5-101).
    assert tm.recount_status({"A": 1000, "B": 995}, 1).recount
    assert not tm.recount_status({"A": 1000, "B": 994}, 1).recount
    two = tm.recount_status({"A": 3000, "B": 1000, "C": 996}, 2)
    assert two.recount and two.reference == 1000 and two.margin == 4
    assert not tm.recount_status({"A": 3000, "B": 1000, "C": 990}, 2).recount
    # No more candidates than seats (write-in placeholders aside): uncontested, never a recount.
    alone = tm.recount_status({"A": 900, "Write-in": 3}, 1, ["A", "Write-in"])
    assert alone.uncontested and not alone.recount
    assert tm.recount_status({"A": 9, "B": 1, "C": 0}, 3).uncontested


def _fact(**kw: object) -> tm.ContestFacts:
    """A local contest with 50 cards, a margin of 10, and what ``kw`` changes."""
    base: dict[str, object] = {
        "name": "X", "index": 0, "scope": tm.LOCAL, "scope_source": "", "seats": 1, "choices": ["A", "B"],
        "cvr_cards": 50, "corla": None, "margin": 10, "margin_source": "", "recount": tm.Recount(10, 100, 0.5, False, False),
    }  # fmt: skip
    return tm.ContestFacts(**(base | kw))  # type: ignore[arg-type]


def test_the_properties_are_independent() -> None:
    # Flagged and settled: a discrepancy found, and still under the limit.
    f = _fact(
        margin=40,
        official_draws=50,
        official_sampled=50,
        official=tm.Discrepancies(o1=1),
    )
    assert (
        f.flagged
        and f.settled
        and not f.thin
        and not f.unseen
        and f.tags == ["flagged: o1 x1"]
    )
    assert (
        f.risk == pytest.approx(tm.km_risk(50, 0.8, o1=1))
        and f.label == "settled flagged: o1 x1"
    )
    # Unseen by the official sample, settled by domain draws: unseen stays, the domain draws shown apart.
    f = _fact(extra_draws=40)
    assert f.unseen and f.settled and f.risk == pytest.approx(tm.km_risk(40, 0.2))
    assert f.label == "settled unseen seen by 40 domain draws"
    # Unseen does not mean a risk of 100%: three domain draws at a diluted margin of 1 leave it at 14%, thin.
    f = _fact(margin=50, extra_draws=3)
    assert (
        f.unseen
        and f.risk == pytest.approx(tm.km_risk(3, 1.0))
        and 0.1 < (f.risk or 0) < 0.2
        and f.thin
    )
    # Unseen and uncontested: no risk, neither thin nor settled, but on the menu.
    f = _fact(recount=tm.Recount(None, None, None, False, True))
    assert (
        f.unseen
        and f.uncontested
        and f.risk is None
        and f.level == ""
        and not f.thin
        and not f.settled
    )
    assert f.tags == ["unseen", "uncontested"] and f.on_menu() and not f.allocatable
    f = _fact(
        recount=tm.Recount(None, None, None, False, True),
        official_draws=1,
        official_sampled=1,
    )
    assert not f.on_menu() and f.label == "uncontested"
    # A recount margin is a note; the contest is thin or settled like any other.
    f = _fact(
        recount=tm.Recount(1, 1000, 5.0, True, False),
        official_draws=10,
        official_sampled=10,
    )
    assert f.recount_margin and f.thin and f.tags == ["recount"] and f.allocatable
    # A tie: risk 1 whatever is drawn; the suggestion gives it a first draw, but minimax and ladder leave it out.
    f = _fact(margin=0, official_draws=10, official_sampled=10)
    assert (
        f.tie
        and f.risk == 1.0
        and f.thin
        and f.draws_to(0.03) is None
        and not f.allocatable
        and "tie" in f.tags
    )


# ---------------------------------------------------------------------------
# A small county: four contests on three sets of cards


def _votes(tmp_path: Path) -> tg.CvrVotes:
    """Rows 1-40 carry the Mayor and Measure 1; rows 41-50 those and the Fire District; rows 51-80 the Council."""
    rows = (
        ballots(40, "Mayor", "Measure")
        + ballots(10, "Mayor", "Measure", "Fire")
        + ballots(30, "Council")
    )
    path = tmp_path / "cvr.csv"
    path.write_bytes(make_cvr(rows))
    return tg.load_votes(path)


def _inputs(votes: tg.CvrVotes, **kw: object) -> tm.MenuInputs:
    corla = {
        "mayor": tm.CorlaContest("Mayor", min_margin=10, contest_cards=50),
        "measure 1": tm.CorlaContest("Measure 1", min_margin=45, contest_cards=50),
        "fire district": tm.CorlaContest(
            "Fire District", min_margin=2, contest_cards=10
        ),
    }
    inputs = tm.MenuInputs(
        county="Synthetic",
        cvr_sha256="c" * 64,
        votes=votes,
        corla=corla,
        results={
            "council": (
                # Two seats, a margin of 4 within 0.5% of the lowest winner's 1000: a recount, checked like any other.
                {"Cy Choi": 3000, "Di Dunn": 1000, "Ed Eng": 996},
                "the state's results",
            )
        },
        # Card 101-1-1 drawn twice: two draws.
        draws=["101-1-1", "101-1-1", "101-1-2", "101-1-45", "101-1-60", "9-9-9"],
        draws_source="test",
        comparisons={
            (id_key("101-1-2"), "mayor"): [
                (("Bob Baker",), ())
            ],  # a one-vote understatement
            (id_key("101-1-1"), "measure 1"): [
                (("Yes/For",), ())
            ],  # one vote overstated, on a card drawn twice
        },
        corla_comparisons=True,
    )
    for name, value in kw.items():
        setattr(inputs, name, value)
    return inputs


def _facts(menu: tm.Menu) -> dict[str, tm.ContestFacts]:
    return {f.name: f for f in menu.facts}


def test_pooled_risk_counts_draws_with_multiplicity(tmp_path: Path) -> None:
    votes = _votes(tmp_path)
    assert votes.contest_by_name(MAYOR).carry.bit_count() == 50  # type: ignore[union-attr]
    menu = tm.build_menu(_inputs(votes))
    f = _facts(menu)
    mayor, measure, fire, council = f[MAYOR], f[MEASURE], f[FIRE], f[COUNCIL]
    assert all(x.local and "assumed by name" in x.scope_source for x in f.values())
    # CORLA's draws on cards carrying each contest, a card drawn twice counted twice; 9-9-9 is in no card of the CVR.
    assert (
        mayor.official_draws,
        measure.official_draws,
        fire.official_draws,
        council.official_draws,
    ) == (4, 4, 1, 1)
    assert mayor.official == tm.Discrepancies(
        u1=1
    ) and measure.official == tm.Discrepancies(o1=2)
    # The draws on cards with no comparison row are not yet examined (assumed to show none).
    assert (mayor.unexamined, measure.unexamined, fire.unexamined) == (3, 2, 1)
    # Diluted margin over the contest's cards in the county; Kaplan-Markov over the pooled draws.
    assert mayor.diluted_margin == pytest.approx(
        10 / 50
    ) and mayor.risk == pytest.approx(tm.km_risk(4, 0.2, u1=1))
    assert measure.risk == pytest.approx(
        tm.km_risk(4, 0.9, o1=2)
    ) and fire.risk == pytest.approx(tm.km_risk(1, 0.2))
    assert mayor.risk_after(10) == pytest.approx(tm.km_risk(14, 0.2, u1=1))
    # Properties: the Mayor and the Measure are flagged and thin; the Council's recount is a note only.
    assert [(x.flagged, x.thin, x.unseen) for x in (mayor, measure, fire, council)] == [
        (True, True, False), (True, True, False), (False, True, False), (False, True, False)
    ]  # fmt: skip
    assert mayor.tags == ["flagged: u1 x1"] and measure.tags == ["flagged: o1 x2"]
    assert council.recount.source == "the state's results" and council.tags == [
        "recount"
    ]
    assert council.label == "thin recount" and council.risk == pytest.approx(
        tm.km_risk(1, 4 / 30)
    )
    # The larger of the CVR's and CORLA's card counts makes the domain.
    bigger = tm.build_menu(
        _inputs(
            votes,
            corla={
                "fire district": tm.CorlaContest(
                    "Fire District", min_margin=2, contest_cards=12
                )
            },
        )
    )
    assert _facts(bigger)[FIRE].domain_cards == 12 and [
        c[1] for c in bigger.card_checks()
    ] == [12]


def test_sampling_domains_and_coverage_only(tmp_path: Path) -> None:
    votes = _votes(tmp_path)
    menu = tm.build_menu(_inputs(votes))
    index = {cv.name: i for i, cv in votes.contests.items()}
    groups = {g.contests: g for g in menu.groups}
    assert set(groups) == {(MAYOR, MEASURE), (FIRE,), (COUNCIL,)}
    both = groups[(MAYOR, MEASURE)]
    # The Fire District's cards lie inside the Mayor's: each gets the other as coverage only, never a pooled update.
    assert (
        both.cards == 50
        and both.covered == (index[FIRE],)
        and groups[(FIRE,)].covered == tuple(sorted((index[MAYOR], index[MEASURE])))
    )
    assert groups[(COUNCIL,)].covered == () and groups[(COUNCIL,)].cards == 30


def test_the_menu_lines(tmp_path: Path) -> None:
    menu = tm.build_menu(_inputs(_votes(tmp_path)))
    # Riskiest first; the Council, a recount, is on the menu like any other.
    assert [line.group.contests for line in menu.lines] == [
        (COUNCIL,),
        (FIRE,),
        (MAYOR, MEASURE),
    ]
    council, fire, both = menu.lines
    parts = tm.line_parts(council)
    assert parts[0] == "Council  ·  30 cards, 1 contest (1 on the menu)"
    assert (
        parts[1]
        == "Riskiest: Council   93.6%  thin recount; margin 4 votes, 13.3% of its 30 cards"
    )
    assert fire.now == pytest.approx(tm.km_risk(1, 0.2)) and fire.needed == tm.draws_to(
        0.03, 0.2, 1
    )
    parts = tm.line_parts(fire)
    after = " / ".join(tm.pct(tm.km_risk(1 + k, 0.2)) for k in (5, 10, 20))
    assert parts[:3] == [
        "Fire District  ·  10 cards, 1 contest (1 on the menu)",
        "Riskiest: Fire District   90.4%  thin; margin 2 votes, 20.0% of its 10 cards",
        f"after 5 / 10 / 20 draws: {after}; under 3% after {fire.needed} draws",
    ]
    assert (
        parts[-1] == "Also lands on 2 menu contests: Mayor, Measure 1 (coverage only)."
    )
    parts = tm.line_parts(both)
    needed = tm.draws_to(0.03, 0.9, 4, o1=2)
    assert parts[0] == "Mayor and 1 other  ·  50 cards, 2 contests (2 on the menu)"
    assert (
        parts[1]
        == "Riskiest: Mayor   45.0%  thin flagged: u1 x1; margin 10 votes, 20.0% of its 50 cards"
    )
    assert (
        parts[3]
        == f"Others (1): 1 flagged, all thin; all under 3% after {needed} draws."
    )
    assert parts[-1] == "Also lands on 1 menu contest: Fire District (coverage only)."
    assert [o.name for o in both.others] == [MEASURE]
    assert tm.domain_title("Ballot Issue 6AB", 10) == "Ballot Issue 6AB and 9 others"
    assert tm.domain_title("A", 2) == "A and 1 other" and tm.domain_title("A", 1) == "A"
    plain = tm.menu_text(menu, 10)
    totals = "4 local contests: 0 unseen, 2 flagged, 4 thin, 0 settled, 0 uncontested, 1 recount, 0 tie."
    assert (
        tm.STYLE_NOTE in plain
        and totals in plain
        and menu.totals_text() + "." == totals
    )
    assert (
        "Riskiest: Council   93.6%  thin recount; margin 4 votes" in plain
        and "- minimax: " in plain
        and "- ladder: " in plain
    )


def test_a_tie_gets_a_first_draw_but_is_left_out_of_minimax_and_ladder(
    tmp_path: Path,
) -> None:
    tie = {
        "council": (
            {"Cy Choi": 3000, "Di Dunn": 1000, "Ed Eng": 1000},
            "the state's results",
        )
    }
    menu = tm.build_menu(_inputs(_votes(tmp_path), results=tie))
    council = _facts(menu)[COUNCIL]
    assert council.tie and council.thin and council.risk == 1.0
    line = next(x for x in menu.lines if x.group.contests == (COUNCIL,))
    assert (
        line.riskiest is None and line.now is None and menu.lines[-1] is line
    )  # still on the menu, last
    assert f"Council: {tm.TIE_TEXT}" in tm.line_parts(line)
    assert tm.TIE_TEXT == "tie: each card checks the CVR's count; the risk can't fall"
    # One first draw, as for an unseen domain (a card checks the CVR's count); none from minimax or ladder.
    for strategy in (tm.MINIMAX, tm.LADDER):
        result = menu.allocate(500, strategy)
        assert result.draws[line.group.key] == 1 and line.group.key in result.seeded
        # The highest risk the suggestion lowers leaves the tie out.
        assert (
            result.before == pytest.approx(max(x.now or 0 for x in menu.lines))
            and result.after < 1.0
        )
    assert menu.allocate(500, tm.MINIMAX).after <= 0.03
    assert tm.TIE_NOTE in tm.menu_text(menu, 10)


def test_regional_contests_coverage_only_and_left_off(tmp_path: Path) -> None:
    votes = _votes(tmp_path)
    corla = {
        "mayor": tm.CorlaContest("Mayor", min_margin=10, contest_cards=50, counties=1),
        "measure 1": tm.CorlaContest(
            "Measure 1", min_margin=30, contest_cards=50, counties=1
        ),
        # Regional: this county holds 10 of 400 cards, and CORLA drew none of them anywhere: left off.
        "fire district": tm.CorlaContest(
            "Fire District",
            min_margin=2,
            contest_cards=400,
            ballot_cards=400,
            counties=3,
        ),
        # Regional with half its cards here, measured by CORLA: 25 draws over its 400-card domain.
        "council": tm.CorlaContest(
            "Council",
            seats=2,
            min_margin=40,
            contest_cards=60,
            ballot_cards=400,
            audited=25,
            counties=2,
        ),
    }
    results = {
        "council": (
            {"Cy Choi": 100, "Di Dunn": 60, "Ed Eng": 20},
            "the state's results",
        )
    }
    draws = [
        "101-1-1",
        "101-1-1",
        "101-1-2",
        "101-1-60",
    ]  # none on the Fire District's cards
    menu = tm.build_menu(
        _inputs(votes, corla=corla, counties_known=True, results=results, draws=draws)
    )
    f = _facts(menu)
    assert (
        f[MAYOR].local
        and f[MAYOR].scope_source == "CORLA's contest list by county"
        and not f[FIRE].local
    )
    assert (
        f[FIRE].unseen
        and f[FIRE].share == pytest.approx(0.025)
        and tm.left_off(f[FIRE])
    )
    council = f[COUNCIL]
    assert (
        council.risk == pytest.approx(tm.km_risk(25, 40 / 400))
        and council.thin
        and not tm.left_off(council)
    )
    assert [x.name for x in menu.left_off] == [FIRE] and {
        x.name for x in menu.regional
    } == {FIRE, COUNCIL}
    # Regional contests are never domains: only the local pair is a group.
    assert [g.contests for g in menu.groups] == [(MAYOR, MEASURE)]
    (line,) = menu.lines
    assert (
        line.covered_on_menu == ()
    )  # the Fire District's small share keeps it off the menu
    assert not any(p.startswith("Also lands on") for p in tm.line_parts(line))
    assert (
        f"Left off, regional with under 5% of the cards here (1): {FIRE}"
        in tm.menu_text(menu)
    )
    # A cutoff of 2% puts it back on.
    assert (
        tm.build_menu(
            _inputs(
                votes, corla=corla, counties_known=True, results=results, draws=draws
            ),
            cutoff=0.02,
        ).left_off
        == []
    )


def test_minimax_and_ladder(tmp_path: Path) -> None:
    menu = tm.build_menu(_inputs(_votes(tmp_path)))
    by_index = {f.index: f for f in menu.facts}

    def risk(key: str, extra: int) -> float:
        group = menu.group(key)
        assert group is not None
        return max(
            r for i in group.indexes if (r := by_index[i].risk_after(extra)) is not None
        )

    keys = [line.group.key for line in menu.lines]
    # Nothing is unseen here, so nothing is given a first draw. Minimax gives its first draw to the riskiest domain.
    one = menu.allocate(1, tm.MINIMAX)
    assert (
        one.seeded == []
        and one.draws == {keys[0]: 1}
        and one.before == pytest.approx(menu.lines[0].now)
    )
    small = menu.allocate(3, tm.MINIMAX)
    assert sum(small.draws.values()) == 3 and small.after < small.before
    assert small.after == pytest.approx(
        max(risk(k, small.draws.get(k, 0)) for k in keys)
    )
    # A budget large enough brings every domain to the limit, each exactly as far as it needs.
    big = menu.allocate(500, tm.MINIMAX)
    assert (
        big.after <= 0.03
        and big.unspent > 0
        and sum(big.draws.values()) + big.unspent == 500
    )
    for line in menu.lines:
        members = [by_index[i] for i in line.group.indexes]
        assert big.draws[line.group.key] == max(m.draws_to(0.03) or 0 for m in members)
    # The ladder brings domains under 90%, 50%, 20%, 10% and the limit in turn, cheapest first.
    ladder = menu.allocate(10, tm.LADDER)
    assert (
        sum(ladder.draws.values()) + ladder.unspent == 10
        and ladder.after <= ladder.before
    )
    assert (
        menu.allocate(0, tm.MINIMAX).draws == {}
        and menu.allocate(0, tm.LADDER).after == one.before
    )
    with pytest.raises(ValueError):
        menu.allocate(5, "lottery")


def test_each_unseen_domain_gets_a_first_draw_uncontested_included(
    tmp_path: Path,
) -> None:
    votes = _votes(tmp_path)
    corla = {
        "mayor": tm.CorlaContest("Mayor", min_margin=10, contest_cards=50),
        "measure 1": tm.CorlaContest("Measure 1", min_margin=45, contest_cards=50),
        "fire district": tm.CorlaContest(
            "Fire District", min_margin=2, contest_cards=10
        ),
        # Three seats for three candidates: uncontested, no risk; unseen, it is still on the menu.
        "council": tm.CorlaContest("Council", seats=3),
    }
    menu = tm.build_menu(_inputs(votes, corla=corla, draws=[], comparisons={}))
    f = _facts(menu)
    assert f[COUNCIL].unseen and f[COUNCIL].uncontested and f[COUNCIL].risk is None
    assert {line.group.contests for line in menu.lines} == {
        (MAYOR, MEASURE),
        (FIRE,),
        (COUNCIL,),
    }
    council = next(line for line in menu.lines if line.group.contests == (COUNCIL,))
    assert tm.line_parts(council) == [
        "Council  ·  30 cards, 1 contest (1 on the menu)",
        "Also: 1 uncontested unseen (one card checks that the CVR records it)",
    ]
    pair = next(line for line in menu.lines if line.group.contests == (MAYOR, MEASURE))
    assert "Others (1): 1 also unseen, all thin" in tm.line_parts(pair)[3]
    keys = {line.group.contests: line.group.key for line in menu.lines}
    # A short budget: one draw each to the largest unseen domains.
    two = menu.allocate(2, tm.MINIMAX)
    assert two.seeded == [keys[(MAYOR, MEASURE)], keys[(COUNCIL,)]] and two.draws == {
        k: 1 for k in two.seeded
    }
    # Then the rest by minimax or ladder; the uncontested domain gets its one draw and no more.
    for strategy in (tm.MINIMAX, tm.LADDER):
        result = menu.allocate(30, strategy)
        assert len(result.seeded) == 3 and result.draws[keys[(COUNCIL,)]] == 1
        assert all(result.draws.get(k, 0) >= 1 for k in keys.values())
    # Domain draws: the Fire District stays unseen (by the official sample) but is settled, and gets no first draw.
    drawn = tg.domain_draw(votes, county="Synthetic", cvr_sha256="c" * 64, contests=(FIRE,), seed="s", seed_source="entered",
                           number=1, locations={}, manifest_sha256=None, actor=ACTOR, size=40)  # fmt: skip
    reached = tm.build_menu(
        _inputs(votes, corla=corla, draws=[], comparisons={}, domain_draws=[drawn])
    )
    fire = _facts(reached)[FIRE]
    assert (
        fire.unseen
        and fire.settled
        and fire.label == "settled unseen seen by 40 domain draws"
    )
    assert keys[(FIRE,)] not in reached.allocate(10, tm.MINIMAX).seeded
    # Once an official card is seen, an uncontested contest has no property putting it on the menu.
    seen = tm.build_menu(
        _inputs(votes, corla=corla, draws=["101-1-60"], comparisons={})
    )
    assert not _facts(seen)[COUNCIL].unseen and (COUNCIL,) not in {
        x.group.contests for x in seen.lines
    }


# ---------------------------------------------------------------------------
# Domain draws


def test_domain_draws_are_reproducible_and_with_replacement(tmp_path: Path) -> None:
    votes = _votes(tmp_path)

    def run(
        seed: str = "public words",
        number: int = 1,
        size: int = 30,
        contests: tuple[str, ...] = (FIRE,),
    ) -> tg.DomainDrawRecord:
        return tg.domain_draw(votes, county="Synthetic", cvr_sha256="c" * 64, contests=contests, seed=seed, seed_source="entered",
                              number=number, locations={}, manifest_sha256=None, actor=ACTOR, size=size, held={"101-1-41": "the RLA sample"})  # fmt: skip

    first = run()
    assert (
        first.to_json() == run().to_json()
        and run(seed="other").drawn_ids != first.drawn_ids
        and run(number=2).drawn_ids != first.drawn_ids
    )
    # Anyone can redo it: derived seed from the canonical inputs, each draw's card from SHA-256 of it and the draw's number.
    inputs = {
        "county": "Synthetic",
        "cvr_sha256": "c" * 64,
        "domain": [FIRE],
        "draw": 1,
        "method": tg.DOMAIN_METHOD,
        "seed": "public words",
    }
    assert first.derived_seed == hashlib.sha256(canonical_json(inputs)).hexdigest()
    rows = [
        f"101-1-{n}" for n in range(41, 51)
    ]  # the Fire District's ten cards, in CVR order
    redone = [
        rows[
            int(hashlib.sha256(f"{first.derived_seed},{i}".encode()).hexdigest(), 16)
            % 10
        ]
        for i in range(1, 31)
    ]
    assert first.drawn_ids == redone and first.domain_cards == 10
    # Thirty draws from ten cards: repeats kept in the draws, each card once in the ballots with how many times.
    assert len(first.draws) == 30 and len(first.ballots) == len(set(redone)) < 30
    assert (
        sum(b["times"] for b in first.ballots) == 30
        and max(b["times"] for b in first.ballots) > 1
    )
    # Nothing is skipped: a card already held is noted, not replaced.
    assert ("101-1-41" in redone) == bool(first.held)
    # A domain is contests on exactly the same cards.
    assert run(contests=(MAYOR, MEASURE)).domain_cards == 50
    with pytest.raises(tg.DrawError, match="not carried by exactly the same cards"):
        run(contests=(MAYOR, FIRE))
    with pytest.raises(tg.DrawError, match="as the county's current CVR names them"):
        run(contests=("Dog Catcher",))
    with pytest.raises(tg.DrawError, match="1 to"):
        run(size=0)


def _domain_draw(
    store: Workspace, log: EventLog, votes: tg.CvrVotes, size: int = 6, seed: str = "s"
) -> tg.DomainDrawRecord:
    manifest = county_manifest(store, log, COUNTY)
    record = tg.domain_draw(
        votes, county=COUNTY, cvr_sha256=_cvr_sha(log), contests=[LOCAL], seed=seed, seed_source="entered",
        number=tg.domain_draw_count(log, COUNTY) + 1, locations=manifest.locations, manifest_sha256=manifest.sha256, actor=ACTOR,
        size=size, held=tg.holders(store, log, COUNTY, tg.sources(store, log, COUNTY)),
    )  # fmt: skip
    return tg.record_domain_draw(store, log, record)[0]


def test_a_domain_draw_joins_the_working_list_and_the_risk(tmp_path: Path) -> None:
    store, root = _workspace(tmp_path)
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    with EventLog.open(root) as log:
        before = tm.gather(store, log, COUNTY)
        assert (
            before.draws == []
            and before.domain_draws == []
            and "observers' own entries" in before.comparisons_source
        )
        local = _facts(tm.build_menu(before))[LOCAL.split(" (")[0]]
        assert local.local and local.unseen and local.draws == 0
        drawn = _domain_draw(store, log, votes, size=200)
        event = [e for e in log.iter_events() if e.kind == "targets.domain_drawn"][-1]
        assert (
            event.data["draw"] == 1
            and event.data["size"] == 200
            and event.data["ballots"] == len(drawn.ballots)
            and event.file_sha256 == event.data["sha256"]
        )
        assert (
            json.loads(store.get_bytes(str(event.file_sha256)))["kind"]
            == "targeted_domain_draw"
        )
        assert "Synthetic" not in json.dumps(
            event.data
        )  # the contests are in the record, committed by hash
        # A source of the working list: each card once, the draws all counted in the pooled risk.
        (source,) = tg.sources(store, log, COUNTY)
        assert (
            source.id == "domain 1"
            and source.kind == "domain"
            and source.included
            and source.title == "Domain draw 1"
        )
        assert (
            "200 draws with replacement from the 82 cards carrying Synthetic County Commissioner"
            in source.description
        )
        manifest = county_manifest(store, log, COUNTY)
        entries = tg.working_list([source], votes, manifest.locations, manifest.sizes)
        assert len(entries) == len(drawn.ballots) < 200
        after = _facts(tm.build_menu(tm.gather(store, log, COUNTY)))[
            LOCAL.split(" (")[0]
        ]
        assert (
            after.extra_draws == 200
            and after.extra_unexamined == 200
            and after.unseen
            and after.settled
            and after.seen_text == "seen by 200 domain draws"
        )
        assert after.risk == pytest.approx(tm.km_risk(200, after.diluted_margin))
        # A pattern draw afterwards skips the domain draw's cards; excluding the domain draw takes it out of the risk.
        tg.set_included(log, COUNTY, "domain 1", False, ACTOR)
        assert tm.gather(store, log, COUNTY).domain_draws == []
        tg.set_included(log, COUNTY, "domain 1", True, ACTOR)
        # Sent: the list record keeps every draw, repeats included.
        sent = tg.send(
            store,
            log,
            COUNTY,
            ACTOR,
            votes=votes,
            locations=manifest.locations,
            sizes=manifest.sizes,
        )
        record = json.loads(store.get_bytes(sent.record_sha256))
        (item,) = record["sources"]
        assert (
            item["kind"] == "domain"
            and item["draws"] == drawn.drawn_ids
            and item["contests"] == [LOCAL.split(" (")[0]]
        )
        assert tg.record_patterns(record)[0]["name"] == "Domain draw 1"
        assert (
            len(tm.gather(store, log, COUNTY).domain_draws) == 1
        )  # sent draws still count
        assert (
            tg.domain_draw_count(log, COUNTY) == 1 and tg.draw_count(log, COUNTY) == 0
        )


def test_observers_entries_give_the_discrepancies_without_corla_comparisons(
    tmp_path: Path,
) -> None:
    store, root = _workspace(tmp_path)
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    name = LOCAL.split(" (")[0]
    with EventLog.open(root) as log:
        drawn = _domain_draw(store, log, votes, size=3)
        card = drawn.drawn_ids[0]
        common = {
            "position": 1,
            "imprinted_id": card,
            "contest": name,
            "row": 1,
            "rows": 4,
            "extract_sha256": "e" * 64,
        }
        local_votes, row = votes.contest_by_name(name), votes.row_of(card)
        assert local_votes is not None and row is not None
        expected = "Lou Local" if local_votes.masks[0] >> row & 1 else "Max Local"
        heard = "Max Local" if expected == "Lou Local" else "Lou Local"
        for reading in (1, 2):
            log.append("observation.contest", "observer:lori", county=COUNTY, board="1",
                       data=common | {"expected": expected, "reading": reading, "outcome": "heard_instead", "heard": heard})  # fmt: skip
        inputs = tm.gather(store, log, COUNTY)
        assert (id_key(card), "synthetic county commissioner") in inputs.observed
        local = _facts(tm.build_menu(inputs))[name]
        # The winner read as the loser, or the loser as the winner: a two-vote over- or understatement, per draw of that card.
        times = drawn.drawn_ids.count(card)
        assert (
            local.extra.total == times
            and local.extra_unexamined == 3 - times
            and local.flagged
        )
    assert tm.parse_reading("max local + LOU LOCAL", ["Lou Local", "Max Local"], 2) == (
        "Max Local",
        "Lou Local",
    )
    assert (
        tm.parse_reading("no vote", ["A", "B"], 1) == ()
        and tm.parse_reading("overvote: A + B", ["A", "B"], 1) == ()
    )
    assert (
        tm.parse_reading("Somebody Else", ["A", "B"], 1) is None
        and tm.parse_reading("A + B", ["A", "B"], 1) == ()
    )


def test_the_targets_page_opens_with_the_menu_and_a_domain_draw_from_it(
    tmp_path: Path,
) -> None:
    _, root = _workspace(tmp_path)
    app = create_app(
        AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None)
    )
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.get(
                f"/coord/targets/menu?county={COUNTY}", follow_redirects=False
            ).status_code
            == 303
        )
        assert login(client).status_code == 303
        page = client.get(f"/coord/targets?county={COUNTY}")
        text = page.text
        # The menu first; then the working list, drafts and sent lists; the other ways last, collapsed.
        order = [
            text.index(x) for x in ('id="menu"', 'id="list"', 'id="sent"', 'id="other"')
        ]
        assert order == sorted(order)
        assert (
            "Other ways to choose ballots" in text
            and '<details id="other-ways">' in text
        )
        assert text.index('id="targets-grid"') > text.index(
            'id="other"'
        ) and text.index('action="/coord/targets/list/add"') > text.index('id="other"')
        assert (
            "Where random draws would help most" in text
            and tm.STYLE_NOTE.replace("'", "&#39;") in text
        )
        assert text.index("Where the numbers come from") < text.index('id="list"')
        assert "Riskiest: Synthetic County Commissioner   100.0%  thin " in text
        assert (
            'class="badge badge-info"><span aria-hidden="true">&#9679;</span> unseen</span>'
            in text
            and "[unseen]" not in text
        )
        assert (
            "% of its 82 cards" in text
            and "Margin (votes, % of its cards)" in text
            and "Draw from this domain" in text
        )
        assert "No allowed total is recorded" in text and "Regional (3)" in text
        # The other ways open when one is in use (a pattern checked), and behave as before.
        votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
        local = votes.contest_by_name(LOCAL)
        assert local is not None
        assert (
            '<details id="other-ways" open>'
            in client.get(
                f"/coord/targets?county={COUNTY}&add=Synthetic+County+Commissioner&v={local.index}:0"
            ).text
        )
        # The menu's old page redirects here, its query kept.
        moved = client.get(
            f"/coord/targets/menu?county={COUNTY}&budget=12&strategy=ladder",
            follow_redirects=False,
        )
        assert moved.status_code == 303 and moved.headers["location"].endswith(
            "/coord/targets?county=Delta+%282020+demo%29&budget=12&strategy=ladder#menu"
        )
        suggested = client.get(moved.headers["location"])
        assert (
            "Ladder, 12 draws, one first to each of 1 domain with an unseen contest or a tie: the highest local pooled risk "
            "(ties left out) goes from 100.0% to "
            in suggested.text
            and "suggested: " in suggested.text
        )
        # The seed is shown once, read only, and each domain's draw form carries it; the Cards box holds the suggestion.
        assert "No RLA seed has been entered yet" in text  # this workspace has none
        menu_html = text[text.index('id="menu"') : text.index('id="list"')]
        assert (
            menu_html.count('name="seed"') == 2
        )  # the "Use another seed" box and the one draw form, hidden
        filled = re.search(
            r'name="size" value="(\d*)" min="1" max="500" inputmode="numeric" class="target-size" required',
            suggested.text,
        )
        assert (
            filled is not None and int(filled.group(1)) > 0
        )  # the ladder's suggestion for the one domain
        own = client.get(f"/coord/targets?county={COUNTY}&seed=our+words&budget=12")
        assert "<code>our words</code>" in own.text and "entered here" in own.text
        assert '<input type="hidden" name="seed" value="our words">' in own.text
        key = re.search(r'name="domain" value="([0-9,]+)"', text).group(1)  # type: ignore[union-attr]
        drawn = client.post("/coord/targets/domain/draw", data={"csrf": csrf_of(page), "county": COUNTY, "domain": key, "size": "7", "seed": "words"},
                            follow_redirects=False)  # fmt: skip
        assert (
            drawn.status_code == 303 and "domain_drawn=1" in drawn.headers["location"]
        )
        listed = client.get(drawn.headers["location"]).text
        assert (
            "Domain draw 1: 7 draws from the 82 cards carrying Synthetic County Commissioner"
            in listed
            and "Domain draw 1</strong>" in listed
        )
        assert "Includes 7 domain draws already made." in listed
        bad = client.post(
            "/coord/targets/domain/draw",
            data={"csrf": csrf_of(page), "county": COUNTY, "domain": "99", "size": "7"},
        )
        assert bad.status_code == 400 and "No such domain" in bad.text


def test_the_command_line(tmp_path: Path) -> None:
    _, root = _workspace(tmp_path)
    runner = CliRunner()
    shown = runner.invoke(
        cli_app,
        [
            "targets",
            "menu",
            "--workspace",
            str(root),
            "--county",
            COUNTY,
            "--budget",
            "10",
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert (
        "1. Synthetic County Commissioner  ·  82 cards, 1 contest (1 on the menu)"
        in shown.output
        and "- minimax: " in shown.output
    )
    domain = shown.output.split("[domain ")[1].split("]")[0]
    drawn = runner.invoke(cli_app, ["targets", "domain-draw", "--workspace", str(root), "--county", COUNTY, "--actor", ACTOR,
                                    "--domain", f"domain {domain}", "--seed", "words", "--size", "4"])  # fmt: skip
    assert drawn.exit_code == 0, drawn.output
    assert drawn.output.startswith(
        "Domain draw 1 (domain 1): 4 draws from the 82 cards carrying Synthetic County Commissioner"
    )
    wrong = runner.invoke(
        cli_app,
        [
            "targets",
            "domain-draw",
            "--workspace",
            str(root),
            "--county",
            COUNTY,
            "--actor",
            ACTOR,
            "--domain",
            "99",
            "--seed",
            "x",
        ],
    )
    assert wrong.exit_code == 2


def test_gathered_from_corla_files_the_seed_and_the_posted_comparison(
    tmp_path: Path,
) -> None:
    """A real county: CORLA's contest lists, the seed and the prediction give the draws; the posted comparison the discrepancies."""
    import csv
    import io

    from corla_observer.datamap import build_map
    from corla_observer.predict import latest_prediction, predict, record_prediction
    from corla_observer.seeds import enter_seed
    from corla_observer.uploads import receive
    from synthetic import CONTEST_IDS, SEED, election_files
    from test_datamap import fetch

    contests = (
        "contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,contest_ballot_card_count,winners,"
        "min_margin,risk_limit,audited_sample_count,two_vote_over_count,one_vote_over_count,one_vote_under_count,two_vote_under_count\r\n"
        'Governor - DEM,state_wide_contest,in_progress,1,165,150,"[""A""]",30,0.03,9,0,0,0,0\r\n'
        'Adams County Clerk - REP,county_wide_contest,in_progress,1,50,45,"[""B""]",20,0.03,4,0,0,0,0\r\n'
        'Mayor,opportunistic_benefits,in_progress,1,50,30,"[""Ann Able""]",6,0.03,0,0,0,0,0\r\n'
    ).encode()
    ids = CONTEST_IDS + b"1,Adams,Mayor,12\r\n"
    files = election_files(
        finalReports_ContestsListRound1_csv=contests,
        finalReports_ContestIDsByCounty_csv=ids,
    )
    store = Workspace.open(tmp_path / "ws")
    cvr = tmp_path / "cvr.csv"
    # The Mayor on rows 1-30 (101-1-1 to 101-1-30: the manifest's batch 1), the Council on the rest.
    cvr.write_bytes(make_cvr(ballots(30, "Mayor", "Measure") + ballots(20, "Council")))
    with EventLog.open(store.root) as log:
        fetch(store, log, files)
        enter_seed(log, SEED, ACTOR, "announced at the dice roll")
        record_prediction(
            store, log, predict(build_map(log, store), counties=["Adams"]), ACTOR
        )
        assert receive(
            store, log, "Adams", "redacted_cvr", cvr, "cvr.csv", ACTOR
        ).accepted
        inputs = tm.gather(store, log, "Adams")
        assert (
            inputs.counties_known
            and inputs.risk_limit == 0.03
            and inputs.risk_limit_source == "CORLA's contest list"
        )
        assert inputs.draws_source.startswith(
            "reproduced from the seed and the manifests for 2 sampling domain(s) of targeted contests"
        )
        found = latest_prediction(store, log)
        assert found is not None
        predicted: list[str] = []
        predictions: Any = found[1]["contests"]
        for contest in predictions:
            predicted += [
                str(d["imprinted_id"])
                for d in contest["draws"]
                if d["county"] == "Adams"
            ]
        assert sorted(inputs.draws) == sorted(
            predicted
        )  # every draw on Adams's cards, repeats kept
        on_mayor = [i for i in predicted if i.startswith("101-1-")]
        mayor = _facts(tm.build_menu(inputs))[MAYOR]
        assert (
            mayor.local
            and mayor.scope_source == "CORLA's contest list by county"
            and mayor.margin == 6
        )
        assert (
            mayor.official_draws == len(on_mayor) > 0 and mayor.discrepancies.total == 0
        )
        assert mayor.risk == pytest.approx(tm.km_risk(len(on_mayor), 6 / 30))
        assert "observers' own entries" in inputs.comparisons_source
        # CORLA posts its comparison: one Mayor card read as the loser.
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(["county_name", "contest_name", "imprinted_id", "ballot_type", "choice_per_voting_computer", "audit_board_selection",
                         "consensus", "record_type", "audit_board_comment", "timestamp", "cvr_id"])  # fmt: skip
        writer.writerow(
            [
                "Adams",
                "Mayor",
                on_mayor[0],
                "BT-1",
                '"Ann Able"',
                '"Bob Baker"',
                "YES",
                "uploaded",
                "",
                "2026-11-17",
                "1",
            ]
        )
        comparison = {
            "finalReports/CVRtoAuditBoardInterpretationComparison.csv": out.getvalue().encode()
        }
        fetch(store, log, files | comparison)
        inputs = tm.gather(store, log, "Adams")
        assert (
            inputs.corla_comparisons
            and "CORLA's posted CVR-to-audit-board comparison"
            in inputs.comparisons_source
        )
        mayor = _facts(tm.build_menu(inputs))[MAYOR]
        assert mayor.flagged and mayor.official.o2 == on_mayor.count(on_mayor[0])
        assert mayor.unexamined == len(on_mayor) - on_mayor.count(on_mayor[0])


def test_coverage_names_only_menu_contests_five_at_most() -> None:
    facts = [_fact(name=f"C{i}", index=i) for i in range(8)]  # all unseen: on the menu
    facts.append(
        _fact(name="Settled", index=8, official_draws=500, official_sampled=500)
    )  # not on the menu
    group = tm.DomainGroup(("C0",), (0,), 50, tuple(range(1, 9)))
    (line,) = tm.menu_lines([group], facts)
    assert [f.name for f in line.covered_on_menu] == [f"C{i}" for i in range(1, 8)]
    assert (
        tm.line_parts(line)[-1]
        == "Also lands on 7 menu contests: C1, C2, C3, C4, C5 and 2 more (coverage only)."
    )
    alone = tm.DomainGroup(("C0",), (0,), 50, (8,))
    assert not any(
        p.startswith("Also lands on")
        for p in tm.line_parts(tm.menu_lines([alone], facts)[0])
    )


def test_cvr_contests_matched_to_corla_by_spelling() -> None:
    def corla(name: str, *counties: str) -> tm.CorlaContest:
        return tm.CorlaContest(name, county_names=counties)

    listed = {
        tm.menu_key(c.name): c
        for c in (
            corla("City of Craig Ballot Question 2D", "Moffat"),
            corla("City of Craig Ballot Question 2B", "Moffat"),
            corla("Town of Dinosaur Ballot Issue 2A", "Moffat"),
            corla("Redlands 360 Metropolitan District No. 1 Ballot Issue 6AB", "Mesa"),
            corla("Elsewhere Metropolitan District Ballot Issue 6AB", "Delta"),
            corla("Justice of the Colorado Supreme Court - Hart", "Mesa", "Moffat"),
            corla("Gilpin County Ballot Issue 1A", "Gilpin"),
        )
    }
    keys = [
        tm.menu_key(n)
        for n in (
            "Ciyt of Craig Ballot Question 2D",
            "Town of DinosaurBallot Issue 2A",
            "Colorado Supreme Court Justice - Hart",
        )
    ]
    assert tm.match_corla(keys, listed, "Moffat") == {
        "ciyt of craig question 2d": "city of craig question 2d",
        "town of dinosaurballot issue 2a": "town of dinosaur issue 2a",
        "colorado supreme court justice hart": "justice of the colorado supreme court hart",
    }
    # The word "ballot" left out is no mismatch at all; a bare issue letter matches only within the county.
    assert tm.menu_key("Gilpin County Issue 1A") in listed
    mesa = tm.match_corla([tm.menu_key("BALLOT ISSUE 6AB")], listed, "Mesa")
    assert mesa == {
        "issue 6ab": tm.menu_key(
            "Redlands 360 Metropolitan District No. 1 Ballot Issue 6AB"
        )
    }
    assert (
        tm.match_corla([tm.menu_key("BALLOT ISSUE 6AB")], listed) == {}
    )  # two candidates without the county
    # Two CVR contests on one CORLA contest: neither is trusted.
    assert (
        tm.match_corla(
            ["city of craig question 2d x", "city of craig question 2d y"],
            listed,
            "Moffat",
        )
        == {}
    )


def test_posted_selection_lists_give_corla_s_own_draws(tmp_path: Path) -> None:
    """With CORLA's selection lists and comparison posted, its draws are read, not reproduced: repeats kept,
    one list per domain (the longest), placed in the county by the comparison's CVR IDs."""
    from corla_observer.seeds import enter_seed
    from corla_observer.uploads import receive
    from synthetic import SEED, contests_list, election_files, published_selections
    from test_datamap import fetch

    ids = b"county_id,county_name,contest_name,contest_id\r\n1,Adams,Governor - DEM,10\r\n1,Adams,Adams County Clerk - REP,11\r\n"
    ids += b"7,Boulder,Governor - DEM,10\r\n62,Weld,Governor - DEM,10\r\n1,Adams,Adams County Coroner - DEM,13\r\n"
    selections = {
        "Governor - DEM": ["101-1-3", "101-1-3", "101-2-4"],
        "Adams County Clerk - REP": ["101-1-5", "101-1-6"],
        "Adams County Coroner - DEM": [
            "101-1-5"
        ],  # the same domain as the Clerk's: its shorter list is not added
    }
    files = election_files(
        finalReports_ContestsListRound1_csv=contests_list(),
        finalReports_ContestIDsByCounty_csv=ids,
    )
    files |= published_selections(selections)
    store = Workspace.open(tmp_path / "ws")
    cvr = tmp_path / "cvr.csv"
    cvr.write_bytes(make_cvr(ballots(30, "Mayor", "Measure") + ballots(20, "Council")))
    with EventLog.open(store.root) as log:
        fetch(store, log, files)
        enter_seed(log, SEED, ACTOR, "announced at the dice roll")
        assert receive(
            store, log, "Adams", "redacted_cvr", cvr, "cvr.csv", ACTOR
        ).accepted
        inputs = tm.gather(store, log, "Adams")
    assert sorted(inputs.draws) == sorted(
        ["101-1-3", "101-1-3", "101-2-4", "101-1-5", "101-1-6"]
    )
    assert inputs.draws_source.startswith(
        "CORLA's posted selection lists (2 sampling domain(s)"
    )
    assert "5 draws, 4 distinct cards" in inputs.draws_source
