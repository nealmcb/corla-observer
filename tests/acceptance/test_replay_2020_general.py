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
            assert stage_overrides(ws, log, text=GENERAL_2020_OVERRIDES).applied
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
