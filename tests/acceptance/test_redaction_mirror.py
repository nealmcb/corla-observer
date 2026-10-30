"""Acceptance tests: the redaction check and uploads on real public CVRs.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``.

The files are counties' cast vote records published for the 2025
coordinated election and the 2026 primary, read from the public audit center
mirror https://github.com/nealmcb/auditcenter at the commits pinned in
mirror_files.json (see mirror.py). Between them they show the counties'
different ways of redacting:

- Broomfield 2025: precinct column kept; redacted rows with every vote
  cell blank.
- La Plata and Morgan 2026: rows redacted with ``*`` in every vote cell,
  and an ``AGGREGATED`` row; Morgan without a precinct column. La Plata's
  export quotes its text cells.
- Weld 2026: precinct column kept, identification cells in Excel's
  ``="..."`` wrapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corla_observer.classify import classify_file
from corla_observer.events import EventLog
from corla_observer.redaction import analyze_redaction, check_redaction
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace

pytestmark = pytest.mark.slow

MORGAN = "2026/primary/observerfiles/Morgan_CVR_Export_20260709092315_Redacted.csv"

#: (path, verdict, rows read, ballots grouped, already redacted, aggregate rows, grouped by, styles, groups)
EXPECTED: list[tuple[str, str, int, int, int, int, str, int, int]] = [
    ("2025/cvrs/broomfield-Redacted_CVR_Export_20251119093616.csv", "PASS", 29678, 29669, 9, 0, "style+precinct+ballot_type", 11, 11),
    ("2026/primary/observerfiles/LaPlata_RedactedTest_NoContestBalance_CVR_Export_20260709081424.csv", "PASS", 16147, 16137, 9, 1, "style+precinct+ballot_type", 3, 3),
    (MORGAN, "PASS", 5221, 5195, 25, 1, "style+ballot_type", 2, 2),
    ("2026/primary/observerfiles/Weld_CVR_Export_20260709140755.csv", "PASS", 69640, 69620, 20, 0, "style+precinct+ballot_type", 38, 44),
]  # fmt: skip


@pytest.mark.parametrize("expected", EXPECTED, ids=[Path(e[0]).name for e in EXPECTED])
def test_real_cvrs(mirror_root: Path, expected: tuple[str, str, int, int, int, int, str, int, int]) -> None:
    path, *counts = expected
    report = analyze_redaction(mirror_root / path)
    assert [
        report.verdict,
        report.rows_read,
        report.ballot_rows,
        report.already_redacted_rows,
        report.aggregate_rows,
        report.grouped_by,
        report.styles,
        report.groups,
    ] == counts, report
    assert report.problems == ()
    assert classify_file(mirror_root / path).kind == "cvr"


def test_worker_process_on_a_real_file(mirror_root: Path) -> None:
    path = mirror_root / MORGAN
    assert check_redaction(path, include_contest_names=True) == analyze_redaction(path, include_contest_names=True)


def test_upload_of_a_real_redacted_cvr(mirror_root: Path, tmp_path: Path) -> None:
    workspace = Workspace.open(tmp_path / "ws")
    with EventLog.open(workspace.root) as log:
        result = receive(workspace, log, "Morgan", "redacted_cvr", mirror_root / MORGAN, Path(MORGAN).name, "coordinator:t")
        assert result.accepted
        assert workspace.has(result.sha256 or "")
        assert [e.kind for e in log.iter_events()] == ["file.received", "redaction.checked"]
        assert log.verify().ok
