"""Acceptance test: build La Plata's extract from a rehearsal of the July 2026 primary.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``.
Needs a git checkout of the public mirror, https://github.com/nealmcb/auditcenter,
named by ``CORLA_OBSERVER_MIRROR`` (see test_rehearsal_2026_primary.py).

The rehearsal has no pull list, but it fetches CORLA's final reports, so the
extract follows CORLA's posted selections (``audit_center`` order): every
ballot drawn in La Plata for its two congressional primaries (127 and 69
draws) and for the statewide Senate primary, each once, in CORLA's pull
order (storage location, scanner, batch, record), with its expected votes
from La Plata's redacted CVR; they are exactly the predicted draws. Building it again gives the
same bytes and hash, and every page carries its identification.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from corla_observer.events import EventLog
from corla_observer.datamap import build_map
from corla_observer.pdfextract import Qr, Text, pdf_sheets
from corla_observer.extract import _locations, board_pages, build_extract, corla_order, extract_sha256, qr_payload, render_text, to_csv
from corla_observer.predict import latest_prediction
from corla_observer.rehearse import PRIMARY_2026_COMMIT_IDS, PRIMARY_2026_SEED, run_all
from corla_observer.workspace import Workspace

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def mirror() -> Path:
    path = os.environ.get("CORLA_OBSERVER_MIRROR")
    if not path or not (Path(path) / ".git").exists():
        pytest.skip("set CORLA_OBSERVER_MIRROR to a git checkout of https://github.com/nealmcb/auditcenter")
    for commit in PRIMARY_2026_COMMIT_IDS:
        found = subprocess.run(["git", "-C", path, "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True)
        if found.returncode != 0:
            pytest.skip(f"the mirror checkout lacks commit {commit}; fetch it")
    return Path(path)


def test_la_plata_extract(tmp_path: Path, mirror: Path) -> None:
    run_all(tmp_path / "ws", mirror)
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        latest = latest_prediction(ws, log)
        assert latest is not None
        built = build_extract(ws, log, "La Plata", boards=2, actor="coordinator:rehearsal")
        again = build_extract(ws, log, "La Plata", boards=2, actor="coordinator:rehearsal")
    extract = built.extract
    assert again.sha256 == built.sha256 == extract_sha256(extract)
    assert extract["seed"] == PRIMARY_2026_SEED and extract["order"] == "audit_center"

    prediction: Any = latest[1]
    draws = {str(c["name"]): [str(d["imprinted_id"]) for d in c["draws"] if d["county"] == "La Plata"] for c in prediction["contests"]}
    congress = [n for n in draws if "Congress" in n]
    assert sorted(len(draws[n]) for n in congress) == [69, 127]
    distinct = {i for ids in draws.values() for i in ids}
    listed = list(dict.fromkeys(b["imprinted_id"] for b in extract["ballots"]))
    assert set(listed) == distinct and not extract["checks"]["predicted_not_listed"] and not extract["checks"]["not_predicted"]
    datamap = build_map(EventLog.open(ws.root, read_only=True), ws)
    assert listed == corla_order(distinct, _locations(datamap, "La Plata"))
    scopes = {scope for b in extract["ballots"] for _, scope in b["picks"]}
    assert scopes == {"county", "statewide"}  # the Senate primary's La Plata picks alongside the county's
    assert extract["contests"]["United States Senator - DEM"]["scope"] == "statewide"
    # Every ballot is in La Plata's redacted CVR, with its votes.
    assert all(b["status"] == "votes" and b["votes"] for b in extract["ballots"])
    assert all(b["location"] for b in extract["ballots"])
    assert {b["board"] for b in extract["ballots"]} == {1, 2}
    # Without the pull list, a ballot at the boundary between the boards may be on either.
    assert extract["board_split"] == "uncertain"

    for board in (1, 2):
        pages = board_pages(extract, built.sha256, board)
        for layout in ("portrait-1", "landscape-3"):
            sheets = pdf_sheets(extract, built.sha256, board, layout)
            assert [s.page for s in sheets] == [None] + [p.number for p in board_pages(extract, built.sha256, board, layout)]
            codes = [i.payload for s in sheets[1:] for i in s.items if isinstance(i, Qr)]
            assert codes == [qr_payload(p) for p in board_pages(extract, built.sha256, board, layout)]
            assert all(t.room is None or t.width <= t.room + 1e-6 for s in sheets for t in s.items if isinstance(t, Text))
        assert render_text(extract, built.sha256, board).count("\f") == len(pages)  # the provenance page first
    assert to_csv(extract, built.sha256).count("\r\n") > len(extract["ballots"])
