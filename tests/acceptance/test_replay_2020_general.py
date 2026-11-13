"""Acceptance test: the 2020 general's replay predicts CORLA's round-one selections, given Teller's manifest correction.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``. It needs a git checkout
of the mirror, https://github.com/nealmcb/auditcenter, named by ``CORLA_OBSERVER_MIRROR``.

The 2020 general's published manifests sum to 4,114,491 cards, 91 short of the 4,114,582 CORLA
sampled for its statewide contests: Teller's manifest download stops at batch 1-681, and the
state's batch comparison lists four more batches (`corla_observer.rehearse.GENERAL_2020_OVERRIDES`).
With that adjustment the statewide domain is CORLA's, and the predicted draws are CORLA's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from corla_observer.events import EventLog
from corla_observer.rehearse import GENERAL_2020_OVERRIDES, stage_compare, stage_fetch, stage_overrides, stage_predict, stage_seed
from corla_observer.workspace import Workspace

pytestmark = pytest.mark.slow

#: The mirror's snapshot of the 2020 general (taken after the audit), and its seed (the notice of 16 November 2020).
COMMIT = "382a035fe0ff90291382a6dfb33bf583032d1605"
SEED = "92076602804272786701"


@pytest.fixture(scope="module")
def mirror() -> Path:
    path = os.environ.get("CORLA_OBSERVER_MIRROR")
    if not path or not (Path(path) / ".git").exists():
        pytest.skip("set CORLA_OBSERVER_MIRROR to a git checkout of https://github.com/nealmcb/auditcenter")
    found = subprocess.run(["git", "-C", path, "cat-file", "-e", f"{COMMIT}^{{commit}}"], capture_output=True)
    if found.returncode != 0:
        pytest.skip(f"the mirror checkout lacks commit {COMMIT}; fetch it")
    return Path(path)


def predicted_statewide(tmp_path: Path, mirror: Path, overrides: bool) -> tuple[int, str]:
    """The statewide domain size and its comparison with CORLA's selection, with or without the Teller adjustment."""
    ws = Workspace.open(tmp_path)
    with EventLog.open(tmp_path) as log:
        stage_fetch(ws, log, mirror, COMMIT, election="2020/general")
        if overrides:
            stage_overrides(ws, log, text=GENERAL_2020_OVERRIDES)  # applied the first time; unchanged after
        stage_seed(log, seed=SEED, source="the notice of the random seed, 16 November 2020", roll_time=None)
        _, prediction, _, _ = stage_predict(ws, log, counties=("Delta",))
        statewide = next(c for c in prediction.contests if c.name == "Presidential Electors")
        _, comparisons, _ = stage_compare(ws, log)
    status = next(c.status for c in comparisons if c.name == "Presidential Electors")
    return statewide.domain_size, status


def test_teller_adjustment_makes_the_statewide_domain_and_draws_corlas(tmp_path: Path, mirror: Path) -> None:
    size, status = predicted_statewide(tmp_path / "with", mirror, overrides=True)
    assert size == 4_114_582 and status == "match"
    size, status = predicted_statewide(tmp_path / "without", mirror, overrides=False)
    assert size == 4_114_491 and status == "mismatch"


def test_the_rolls_step_brings_corlas_round_one_contest_list(tmp_path: Path, mirror: Path) -> None:
    """The roll's step posts CORLA's contest list: its counts, and the statewide contests it added.

    Before the roll Morgan's own contest is estimated at 17 draws (as it turned out, CORLA's count)
    and the only statewide contest is Presidential Electors, none of whose draws fall in Morgan, so
    Morgan has 17 ballots. CORLA's round 1 also drew for Proposition 114 and the US Senate, one of
    whose draws falls in Morgan: 18 ballots, as in CORLA's round 1 comparison file. (A 19th came in
    round 2, which the replay does not post.)
    """
    from corla_observer.election import GENERAL_2020_SETUP, parse_setup

    setup = parse_setup(GENERAL_2020_SETUP)
    assert setup.mirror is not None
    before, after = setup.mirror.updates
    ws = Workspace.open(tmp_path)
    seen = []
    with EventLog.open(tmp_path) as log:
        stage_seed(log, seed=SEED, source="the notice of the random seed, 16 November 2020", roll_time=None)
        for update in (before, after):
            stage_fetch(ws, log, mirror, update.commit, election="2020/general", exclude=update.exclude)
            stage_overrides(ws, log, text=GENERAL_2020_OVERRIDES)  # applied the first time; unchanged after
            _, prediction, _, _ = stage_predict(ws, log, counties=("Morgan",))
            own = next(c for c in prediction.contests if c.name.startswith("Morgan County Commissioner - District 1"))
            ballots = {d.imprinted_id for c in prediction.contests for d in c.draws_for("Morgan")}
            seen.append((own.sample_count, own.estimated, sorted(c.name for c in prediction.contests if c.statewide), len(ballots)))
    assert seen[0][:3] == (17, True, ["Presidential Electors"])
    assert seen[1][:3] == (17, False, ["Presidential Electors", "Proposition 114 (STATUTORY)", "United States Senator"])
    assert (seen[0][3], seen[1][3]) == (17, 18)  # CORLA's round 1 comparison has 18 Morgan ballots


def test_otero_ids_saved_as_dates_are_read_back_and_reported(tmp_path: Path, mirror: Path) -> None:
    """The published round 1 comparison has 47 Otero imprinted IDs a spreadsheet saved as dates (3/19/2007 for
    3-19-7); they are read back as IDs, so CORLA's selections place in Otero, and the data map warns about the file."""
    from corla_observer.datamap import build_map
    from corla_observer.election import GENERAL_2020_SETUP, parse_setup
    from corla_observer.predict import published_selections

    setup = parse_setup(GENERAL_2020_SETUP)
    assert setup.mirror is not None
    ws = Workspace.open(tmp_path)
    with EventLog.open(tmp_path) as log:
        for update in setup.mirror.updates:
            stage_fetch(ws, log, mirror, update.commit, election="2020/general", exclude=update.exclude)
        datamap = build_map(log, ws)
        warned = [p for p in datamap.problems if p.code == "imprinted_ids_saved_as_dates"]
        assert any("47 imprinted IDs" in p.message and "contestComparison" in p.message for p in warned)
        otero = published_selections(datamap, ws).by_county.get("Otero", {})
    ids = [i for contest in otero.values() for i in contest]
    assert ids and not any("/" in i for i in ids)


def test_every_drawn_ballot_has_an_audit_record_once_dates_are_read_back(tmp_path: Path, mirror: Path) -> None:
    """In every round of the 2020 general, every CVR ID CORLA drew has an audit board record in that round's
    comparison file, with no phantom record or ballot not found, once Otero's imprinted IDs a spreadsheet saved as
    dates are read back: three drawn ballots (3-11-4 and 3-14-23 from round 1, 3-19-7 from round 2) match only
    after that repair. Round 3's comparison file also holds 300 El Paso CVR IDs that no contest of round 3's
    selection list drew: its list for City of Colorado Springs Ballot Question 2C has round 2's 816 draws, while
    its contest list counts 1,117 audited."""
    from corla_observer.datamap import build_map
    from corla_observer.drawn import ID_REPAIRED, drawn_report

    ws = Workspace.open(tmp_path)
    with EventLog.open(tmp_path) as log:
        stage_fetch(ws, log, mirror, COMMIT, election="2020/general")
        datamap = build_map(log, ws)
    assert not [p for p in datamap.problems if p.code in ("drawn_without_record", "worst_case_not_counted")]
    report = drawn_report(datamap, ws)
    assert [(r.round, r.ballots, r.draws) for r in report.rounds] == [(1, 6432, 6960), (2, 8006, 8621), (3, 8006, 8621)]
    repaired = [
        sorted((f.county, f.imprinted_id, f.stored_id) for f in r.findings if f.kind == ID_REPAIRED) for r in report.rounds
    ]
    first = [("Otero", "3-11-4", "3/11/2004"), ("Otero", "3-14-23", "3/14/2023")]
    assert repaired == [first, [*first, ("Otero", "3-19-7", "3/19/2007")], [*first, ("Otero", "3-19-7", "3/19/2007")]]
    assert all(f.kind == ID_REPAIRED for r in report.rounds for f in r.findings)
    assert all(not r.contests for r in report.rounds)
    assert [r.undrawn for r in report.rounds] == [{}, {}, {"El Paso": 300}]
