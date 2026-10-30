"""Slow tests of results files uploaded by hand and the totals check, on the 2026 primary's real files.

Needs a git checkout of https://github.com/nealmcb/auditcenter in
``CORLA_OBSERVER_MIRROR`` (for the audit center files, the observers' 10
July ``SOS_ENR_detail_20260710.xlsx`` and Weld's CVR) and, in
``CORLA_OBSERVER_ENR``, a directory holding the files downloaded from
Clarity's election 126592 (https://results.enr.clarityelections.com/CO/126592/,
version 377440, generated 23 July 2026): ``reports_detailxml.zip``,
``reports_detailxlsx.zip``, ``reports_summary.zip`` and La Plata's own
``LaPlata_summary.json``. Those are not in this repository; the tests are
skipped without them.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from corla_observer.datamap import build_map
from corla_observer.enr import load_results, read_results, receive_results, record_results_file
from corla_observer.estimate import estimate_contests
from corla_observer.events import EventLog
from corla_observer.rehearse import stage_fetch
from corla_observer.tally import cvr_margin, tally_cvr
from corla_observer.totals import last_totals_check, record_totals_check
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace

pytestmark = pytest.mark.slow

FINAL_COMMIT = "ba18ba6"  # 2026-07-16: CORLA's final reports, the manifests and the observers' files
TARGETS_COMMIT = "5108046"  # 2026-07-07: the target contest list
ENR_FILES = ("reports_detailxml.zip", "reports_detailxlsx.zip", "reports_summary.zip", "LaPlata_summary.json")
ACTOR = "coordinator:test"
STATE_URL = "https://results.enr.clarityelections.com/CO/126592/377440/reports/detailxml.zip"


@pytest.fixture(scope="module")
def mirror() -> Path:
    path = os.environ.get("CORLA_OBSERVER_MIRROR")
    if not path or not (Path(path) / ".git").exists():
        pytest.skip("set CORLA_OBSERVER_MIRROR to a git checkout of https://github.com/nealmcb/auditcenter")
    for commit in (FINAL_COMMIT, TARGETS_COMMIT):
        if subprocess.run(["git", "-C", path, "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode:
            pytest.skip(f"the mirror checkout lacks commit {commit}")
    return Path(path)


@pytest.fixture(scope="module")
def enr() -> Path:
    path = os.environ.get("CORLA_OBSERVER_ENR")
    if not path or not all((Path(path) / name).exists() for name in ENR_FILES):
        pytest.skip(f"set CORLA_OBSERVER_ENR to a directory with the 2026 primary's Clarity files ({', '.join(ENR_FILES)})")
    return Path(path)


def observer_file(mirror: Path, name: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(mirror), "show", f"{FINAL_COMMIT}:2026/primary/observerfiles/{name}"], check=True, capture_output=True
    ).stdout


@pytest.fixture(scope="module")
def checked(mirror: Path, enr: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Workspace, EventLog]]:
    """The final audit center files, Weld's CVR, the 23 July results fetched and the 10 July file uploaded by hand, checked."""
    root = tmp_path_factory.mktemp("totals")
    ws = Workspace.open(root)
    with EventLog.open(root) as log:
        stage_fetch(ws, log, mirror, FINAL_COMMIT, election="2026/primary")
        cvr = tmp_path_factory.mktemp("cvr") / "Weld_CVR_Export_20260709140755.csv"
        cvr.write_bytes(observer_file(mirror, cvr.name))
        assert receive(ws, log, "Weld", "redacted_cvr", cvr, cvr.name, ACTOR).accepted
        record_results_file(ws, log, (enr / "reports_detailxml.zip").read_bytes(), url=STATE_URL, scope="state", what="detail",
                            election_id="126592", version="377440", simulated={"source": "test", "time": "2026-07-23T22:30:00.000Z"})  # fmt: skip
        upload = receive_results(ws, log, observer_file(mirror, "SOS_ENR_detail_20260710.xlsx"), "SOS_ENR_detail_20260710.xlsx", ACTOR,
                                 note="the observers' download of 10 July")  # fmt: skip
        assert upload.accepted and upload.generated == "2026-07-10T20:30:45.000Z"
        assert record_totals_check(ws, log, build_map(log, ws)) is not None
        yield ws, log


def test_every_form_and_its_time(mirror: Path, enr: Path) -> None:
    times = {name: read_results((enr / name).read_bytes()).generated for name in ENR_FILES[:3]}
    assert set(times.values()) == {"2026-07-23T22:21:16.000Z"}  # 4:21:16 PM MDT, in all three forms
    summary = read_results((enr / "reports_summary.zip").read_bytes())
    senate = next(c for c in summary.contests if c.name.startswith("United States Senator - Democratic"))
    assert senate.totals == {"Julie Gonzales": 422488, "John Hickenlooper": 472870}
    la_plata = read_results((enr / "LaPlata_summary.json").read_bytes(), "La Plata")
    assert la_plata.form == "summary.json" and la_plata.generated is None and len(la_plata.contests) == 25


def lines(log: EventLog) -> list[dict[str, Any]]:
    found = last_totals_check(log)
    assert found is not None
    listed = found[2]["differences"]
    assert isinstance(listed, list)
    return [d for d in listed if isinstance(d, dict)]


def test_the_newer_fetched_file_is_used(checked: tuple[Workspace, EventLog]) -> None:
    ws, log = checked
    results, item = load_results(ws, log)["state"]
    assert not item.uploaded and item.generated == "2026-07-23T22:21:16.000Z"
    weld = next(c for c in results.contests if c.name == "Attorney General - Republican Party").by_area["Weld"]
    assert weld == {"Michael J. Allen": 19435, "David Willson": 14222} and results.ballots_cast["Weld"] == 69640


def test_weld(checked: tuple[Workspace, EventLog]) -> None:
    ws, log = checked
    diffs = lines(log)
    # Weld's AG-REP margin 5,213 is CORLA's, and its ballots cast are its manifest's total: no line.
    assert not [d for d in diffs if d["contest"] == "Attorney General - REP (Weld)" and d["check"] in ("corla_margin", "corla_votes")]
    assert not [d for d in diffs if d["check"] == "manifest" and d["county"] == "Weld"]
    assert build_map(log, ws).manifests["Weld"].ballot_cards == 69640
    # Its redacted CVR: margin 5,210, every ballot a row, the missing votes blanked by the redaction.
    cvr = next(d for d in diffs if d["check"] == "cvr" and d["contest"] == "Attorney General - REP (Weld)")
    assert not cvr["flag"] and "19,435–14,222 (margin 5,213)" in str(cvr["line"]) and "(margin 5,210)" in str(cvr["line"])
    assert "blanked by its redaction (margin +3)" in str(cvr["line"])


def test_weld_cvr_tally(mirror: Path, tmp_path: Path) -> None:
    path = tmp_path / "weld.csv"
    path.write_bytes(observer_file(mirror, "Weld_CVR_Export_20260709140755.csv"))
    tally = tally_cvr(path).find("Attorney General - REP")
    assert tally is not None and cvr_margin(tally) == 5210


def test_statewide_senate(checked: tuple[Workspace, EventLog]) -> None:
    _, log = checked
    diffs = [d for d in lines(log) if d["contest"] == "United States Senator - DEM"]
    by = {(d["check"], d["county"]): d for d in diffs}
    # San Juan: in Clarity's results but not in CORLA's audit, left out (+11 with it).
    san_juan = by[("excluded", "San Juan")]
    assert not san_juan["flag"] and "77–66 (margin 11)" in str(san_juan["line"])
    # Hinsdale: 7 more votes in the 23 July results than in CORLA's tally, which the 10 July file matched.
    hinsdale = by[("corla_votes", "Hinsdale")]
    assert hinsdale["flag"] and "71–32 (margin 39)" in str(hinsdale["line"]) and "70–26 (margin 44)" in str(hinsdale["line"])
    assert "7 more votes than CORLA's tally" in str(hinsdale["line"]) and "uploaded by hand by coordinator:test) agreed" in str(hinsdale["line"])
    # Every other county matches CORLA's tally exactly.
    assert [d["county"] for d in diffs if d["check"] == "corla_votes"] == ["Hinsdale"]
    margin = by[("corla_margin", None)]
    assert not margin["flag"] and "50,371, vs CORLA's min_margin 50,376 (-5): the county differences above account for it" in str(margin["line"])


def test_estimate_before_the_contest_list(mirror: Path, enr: Path, tmp_path: Path) -> None:
    """With only the target list, the statewide margin comes from the results, over CORLA's counties only."""
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        stage_fetch(ws, log, mirror, TARGETS_COMMIT, election="2026/primary")
        receive_results(ws, log, (enr / "reports_detailxml.zip").read_bytes(), "reports_detailxml.zip", ACTOR, note="test")
        receive_results(ws, log, (enr / "LaPlata_summary.json").read_bytes(), "LaPlata_summary.json", ACTOR, scope="La Plata", note="test")
        estimates = {e.name: e for e in estimate_contests(build_map(log, ws), results=load_results(ws, log), log=log)}
    senate = next(e for e in estimates.values() if e.statewide)
    assert senate.margin == 50371 and "uploaded by hand" in senate.margin_source
    assert any("San Juan's votes (John Hickenlooper 77, Julie Gonzales 66)" in n and "would be 50,382" in n for n in senate.notes)
    weld = next(e for name, e in estimates.items() if name.startswith("Attorney General - Republican Party") and e.counties == ["Weld"])
    assert weld.margin == 5213
