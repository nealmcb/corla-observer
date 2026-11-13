"""Acceptance test: Douglas County's 2020 general manifest against its redacted CVR, batch by batch.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``. It needs a git checkout
of the mirror, https://github.com/nealmcb/auditcenter, named by ``CORLA_OBSERVER_MIRROR``, and the
county's redacted CVR (``Douglas CVR - Redacted.csv``, 68 MB, not in the mirror) named by
``CORLA_OBSERVER_DOUGLAS_2020_CVR``; the CVR is checked against its SHA-256 and never copied.

Of the manifest's 2,415 batches, all ``GEN-nnnn`` in the CVR, two are spelled differently:
``Gen-2026`` (30 cards) and ``GEn-2045`` (100 cards). CORLA matches batch IDs exactly, so a card
drawn from either would have been a phantom record; CORLA's own batch comparison shows neither.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from corla_observer.batches import compare_batches, comparison_lines, read_cvr_batches
from corla_observer.datamap import load_manifest_bytes
from corla_observer.workspace import sha256_of_file

pytestmark = pytest.mark.slow

#: The mirror's snapshot of the 2020 general, as in test_replay_2020_general.py.
COMMIT = "382a035fe0ff90291382a6dfb33bf583032d1605"
MANIFEST = "2020/general/round_1/manifest-Douglas.csv"
CVR_SHA256 = "64c3b9c6e7a79e7729caf53a5acf36825807f23566635aea515dbd4cce3cf762"


def test_douglas_2020_two_likely_typos() -> None:
    mirror = os.environ.get("CORLA_OBSERVER_MIRROR")
    if not mirror or not (Path(mirror) / ".git").exists():
        pytest.skip("set CORLA_OBSERVER_MIRROR to a git checkout of https://github.com/nealmcb/auditcenter")
    cvr = os.environ.get("CORLA_OBSERVER_DOUGLAS_2020_CVR")
    if not cvr or not Path(cvr).is_file():
        pytest.skip("set CORLA_OBSERVER_DOUGLAS_2020_CVR to Douglas County's 2020 general redacted CVR")
    shown = subprocess.run(["git", "-C", mirror, "show", f"{COMMIT}:{MANIFEST}"], capture_output=True)
    if shown.returncode != 0:
        pytest.skip(f"the mirror checkout lacks {MANIFEST} at commit {COMMIT}; fetch it")
    assert sha256_of_file(Path(cvr))[0] == CVR_SHA256, f"{cvr} is not the CVR this test expects"
    found = compare_batches("Douglas", load_manifest_bytes(shown.stdout), read_cvr_batches(cvr))
    assert (found.batches, found.matched, found.manifest_cards, found.cvr_rows) == (2415, 2413, 234272, 234272)
    assert not found.manifest_only and not found.cvr_only and not found.counts_differ and not found.repeated
    assert found.aggregate_rows == 0
    typos = [(t.manifest_tabulator, t.manifest_batch, t.cvr_tabulator, t.cvr_batch, t.manifest, t.cvr) for t in found.typos]
    assert typos == [("1", "Gen-2026", "1", "GEN-2026", 30, 30), ("1", "GEn-2045", "1", "GEN-2045", 100, 100)]
    assert all(t.how == "the batch IDs differ in letter case" for t in found.typos)
    assert [batch for _, batch, _ in comparison_lines(found)] == ["1-Gen-2026", "1-GEn-2045"]
