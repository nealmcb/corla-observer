"""Acceptance test: rehearse the July 2026 primary end to end from the public mirror.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``.

Unlike the other acceptance tests, this one needs a git checkout of the
mirror, https://github.com/nealmcb/auditcenter, named by the environment
variable ``CORLA_OBSERVER_MIRROR``: the rehearsal replays the audit center
as the mirror held it at several commits (`corla_observer.rehearse`). It is
skipped without one, or if the checkout lacks those commits.

It checks two runs:

- the full rehearsal (`run_all`): every fetch from the pre-roll posting to
  CORLA's final reports, the three counties' files through the upload path,
  the primary's overrides, the seed, the prediction for La Plata's, Morgan's
  and Weld's targeted contests and the statewide contest, and the comparison:
  all seven match CORLA's published selections;
- the roll-day run: only what was public by the dice roll and the counties'
  files, with sample counts estimated from the target list, compared once
  the final reports are fetched. Every contest matches or is consistent as a
  prefix, except one county contest: one draw falls in a batch that the
  manifest in the roll-day data numbers 102-46 and the manifest CORLA
  recorded importing at 11:46, after the roll, numbers 102-47.

Both runs also check the change flags (`corla_observer.flags`): the manifest
import CORLA's upload status records after the roll, the uploads it marks
HASH_MISMATCH on 9 July, the copies that differ from the imported file, and
the posted manifests that changed after the roll.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from corla_observer.datamap import build_map
from corla_observer.events import EventLog, verify_jsonl
from corla_observer.flags import Flag, check_flags
from corla_observer.rehearse import (
    PRIMARY_2026_COMMIT_IDS,
    PRIMARY_2026_COUNTY_FILES,
    run_all,
    stage_compare,
    stage_fetch,
    stage_overrides,
    stage_predict,
    stage_seed,
    stage_uploads,
)
from corla_observer.workspace import Workspace

pytestmark = pytest.mark.slow

LA_PLATA_URL = "https://www.coloradosos.gov/pubs/elections/RLA/2026/primary/files/LaPlata.csv"


def la_plata_flags(flags: list[Flag]) -> dict[str, list[Flag]]:
    found: dict[str, list[Flag]] = {}
    for flag in flags:
        if "La Plata" in flag.counties:
            found.setdefault(flag.reason, []).append(flag)
    return found


def check_la_plata_flags(flags: list[Flag]) -> dict[str, list[Flag]]:
    """The flags both runs raise for La Plata."""
    found = la_plata_flags(flags)
    (late,) = found["upload_after_roll"]
    assert late.severity == "high" and late.time == "2026-07-13T17:46:37.543Z"
    (row,) = late.aspects
    assert (row["filename"], row["type"], row["status"]) == (
        "LaPlata_BallotManifest_modified.csv", "manifest", "IMPORTED",
    )  # fmt: skip
    # Every hash mismatch of 9 July in one flag, listing its four rows.
    (mismatches,) = found["upload_hash_mismatch"]
    assert len(mismatches.aspects) == 4 and all(str(a["time"]).startswith("2026-07-09T") for a in mismatches.aspects)
    assert {a["filename"] for a in mismatches.aspects} == {
        "LaPlata_BallotManifest.csv", "Modified_CVR_Export_20260709081424.csv",
    }  # fmt: skip
    # Every copy unlike the import in one flag.
    (copies,) = found["manifest_differs_from_import"]
    copy = next(a for a in copies.aspects if "upload:La Plata:manifest" in a["refs"])  # type: ignore[operator]
    assert "DIFFERENT BATCHES: only in this copy 102-46 (50); only in the imported file 102-47 (50)" in str(copy["message"])
    assert copies.sha256 == "c4ab2dc98beb46ceb23551093165aa401404b3ffb3a10962fab32b76f4589c8c"
    # Identical content served under more than one county's name: one flag, naming each file served under another's.
    (duplicate,) = [f for f in flags if f.reason == "duplicate_content" and "La Plata" in f.counties]
    assert duplicate.counties == ("La Plata", "Morgan", "Saguache")
    assert any(a["reason"] == "manifest_under_other_name" and a["ref"] == LA_PLATA_URL for a in duplicate.aspects)
    assert not [f for f in flags if f.reason == "manifest_under_other_name" and LA_PLATA_URL in f.refs]
    # No finding is listed twice.
    keys = [alias for f in flags for alias in (f.key, *f.aliases)]
    assert len(keys) == len(set(keys))
    assert all(f.severity == "high" for f in flags if f.reason != "changed" and f.reason != "missing")
    return found


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


def test_full_rehearsal_matches_corla(tmp_path: Path, mirror: Path) -> None:
    report = run_all(tmp_path / "ws", mirror)
    assert report.overrides is not None and report.overrides.applied
    assert report.uploads is not None and all(r.accepted for r in report.uploads.results)
    assert all(placeholder for _, placeholder in report.uploads.audit_boards.values())
    first_alerts = report.fetches[0][1].alerts
    assert any(
        a["reason"] == "duplicate_content" and len(a["urls"]) == 3 for a in first_alerts  # type: ignore[arg-type]
    ), "three manifest URLs served identical content at the first commit"
    assert [(c.name, c.status) for c in report.comparisons] == [
        ("Attorney General - DEM (Morgan)", "match"),
        ("Attorney General - REP (Morgan)", "match"),
        ("Attorney General - REP (Weld)", "match"),
        ("Representative to the 120th United States Congress - District 3 - DEM (La Plata)", "match"),
        ("Representative to the 120th United States Congress - District 3 - REP (La Plata)", "match"),
        ("State Representative - District 19 - DEM (Weld)", "match"),
        ("United States Senator - DEM", "match"),
    ]
    statewide = next(c for c in report.prediction.contests if c.statewide)  # type: ignore[union-attr]
    assert (statewide.domain_size, statewide.sample_count) == (1_444_047, 210)
    assert report.datamap is not None
    codes = {p.code for p in report.datamap.check(PRIMARY_2026_COUNTY_FILES)}
    assert {"conflicting_manifests", "manifest_differs_from_import"} <= codes
    assert not [p for p in report.datamap.check(PRIMARY_2026_COUNTY_FILES) if p.severity == "error"]
    assert verify_jsonl(tmp_path / "ws" / "events.jsonl").ok
    assert str(mirror) not in (tmp_path / "ws" / "events.jsonl").read_text()
    found = check_la_plata_flags(report.flags)
    # The posting at 10:33, after the roll, and its replacement at 16:15, both after the roll.
    posted = [f for f in found["changed_after_roll"] if f.refs == (LA_PLATA_URL,)]
    assert [f.time for f in posted] == ["2026-07-13T16:33:59.000Z", "2026-07-13T22:15:36.000Z"]
    assert any(LA_PLATA_URL in f.refs for f in found["manifest_differs_from_import"]), "the 10:33 posting"


def test_roll_day_rehearsal(tmp_path: Path, mirror: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        for commit in PRIMARY_2026_COMMIT_IDS[:2]:
            stage_fetch(ws, log, mirror, commit)
        stage_uploads(ws, log, mirror, commit=PRIMARY_2026_COMMIT_IDS[2])
        assert stage_overrides(ws, log).applied
        stage_seed(log)
        datamap, prediction, _, _ = stage_predict(ws, log)
        assert all(c.estimated for c in prediction.contests)
        assert datamap.county_order.source.startswith("Colorado's standard county numbering")
        warnings = {p.code for p in datamap.problems}
        assert "manifest_under_other_name" in warnings
        stage_fetch(ws, log, mirror, PRIMARY_2026_COMMIT_IDS[-1])
        _, results, _ = stage_compare(ws, log)
        final = build_map(log, ws)
        flags = check_flags(log, ws, final)
    by_name = {r.name: r for r in results}
    la_plata = by_name["Representative to the 120th United States Congress - District 3 - Democratic Party (La Plata)"]
    assert la_plata.status == "mismatch"
    assert (la_plata.missing, la_plata.extra) == (("102-47-5",), ("102-46-5",))
    others = [r for r in results if r is not la_plata]
    assert all(r.status in ("match", "consistent") for r in others), [(r.name, r.status) for r in others]
    assert final.manifests["La Plata"].ref.endswith("files/LaPlata.csv")
    assert final.roll_time is not None and final.roll_time.value == "2026-07-13T16:10:00.000Z"
    found = check_la_plata_flags(flags)
    (posted,) = found["changed_after_roll"]
    assert posted.refs == (LA_PLATA_URL,) and posted.alerts, "the fetcher raised it as it fetched"
    assert (posted.previous_sha256, posted.sha256) == (
        "64dec2dcfb3df0755c1df8eb676c7e5e75bd7fb02fac1adb7f83e1834424366c",
        "c4ab2dc98beb46ceb23551093165aa401404b3ffb3a10962fab32b76f4589c8c",
    )
    after_roll = sorted(str(f.refs[0]).rsplit("/", 1)[-1] for f in flags if f.reason == "changed_after_roll")
    assert after_roll == [
        "Baca.csv", "ContestIDsByCounty.csv", "ContestsListRound1.csv", "LaPlata.csv", "Morgan.csv", "Washington.csv",
    ]  # fmt: skip
