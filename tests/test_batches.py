"""Fast tests for the batch-by-batch comparison of a manifest with a CVR (corla_observer.batches) and its place in the totals check."""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from corla_observer.batches import (
    BatchComparison,
    NoBatchColumns,
    compare_batches,
    comparison_lines,
    how_ids_differ,
    read_cvr_batches,
    summary,
)
from corla_observer.datamap import build_map
from corla_observer.events import EventLog
from corla_observer.flags import check_flags
from corla_observer.sampling import ManifestBatch
from corla_observer.totals import MAX_COUNTY_LINES, check_totals, last_totals_check, record_totals_check
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import Ballot, election_files, make_cvr
from test_datamap import fetch

ACTOR = "coordinator:alice"
MAYOR = "Mayor (Vote For=1)"


def manifest(*batches: tuple[str, str, int]) -> list[ManifestBatch]:
    return [ManifestBatch("Testland", t, b, n, "") for t, b, n in batches]


def rows(tabulator: str, batch: str, count: int, **kwargs: object) -> list[Ballot]:
    return [Ballot((MAYOR,), tabulator=tabulator, batch=batch, **kwargs) for _ in range(count)]  # type: ignore[arg-type]


def compare(tmp_path: Path, batches: Sequence[ManifestBatch], cvr_rows: Sequence[Ballot]) -> BatchComparison:
    path = tmp_path / "cvr.csv"
    path.write_bytes(make_cvr(cvr_rows))
    return compare_batches("Testland", batches, read_cvr_batches(path))


def test_identical(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "1", 3), ("1", "2", 2)), rows("1", "1", 3) + rows("1", "2", 2))
    assert c.differences == 0 and c.matched == 2 and c.manifest_cards == c.cvr_rows == 5
    assert comparison_lines(c) == [] and summary(c) == "no differences"


def test_tabulator_compared_as_a_whole_number(tmp_path: Path) -> None:
    """CORLA reads the tabulator as an integer on both sides, so 01 and 1 are the same tabulator."""
    c = compare(tmp_path, manifest(("01", "7", 2)), rows("1", "7", 2))
    assert c.differences == 0 and c.matched == 1


def test_manifest_only_batch(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "1", 3), ("1", "2", 2)), rows("1", "1", 3))
    assert [(b.label, b.manifest, b.cvr) for b in c.manifest_only] == [("1-2", 2, None)]
    assert not c.cvr_only and not c.typos and not c.counts_differ
    [(kind, batch, line)] = comparison_lines(c)
    assert kind == "manifest_only" and batch == "1-2"
    assert line == "Testland: batch 1-2 in the manifest (2 cards) is not in the CVR: a card drawn from it would be a phantom record"


def test_cvr_only_batch(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "1", 3)), rows("1", "1", 3) + rows("2", "9", 4))
    assert [(b.label, b.manifest, b.cvr) for b in c.cvr_only] == [("2-9", None, 4)]
    assert not c.manifest_only and not c.typos
    [(kind, _, line)] = comparison_lines(c)
    assert kind == "cvr_only" and line == "Testland: batch 2-9 in the CVR (4 rows) is not in the manifest: its ballots cannot be drawn"


def test_case_only_typo(tmp_path: Path) -> None:
    """Douglas, 2020: the manifest's Gen-2026 is the CVR's GEN-2026."""
    c = compare(tmp_path, manifest(("1", "GEN-2025", 2), ("1", "Gen-2026", 3)), rows("1", "GEN-2025", 2) + rows("1", "GEN-2026", 3))
    assert not c.manifest_only and not c.cvr_only and not c.counts_differ
    [typo] = c.typos
    assert (typo.manifest_batch, typo.cvr_batch, typo.manifest, typo.cvr) == ("Gen-2026", "GEN-2026", 3, 3)
    assert typo.how == "the batch IDs differ in letter case"
    [(kind, batch, line)] = comparison_lines(c)
    assert kind == "typo" and batch == "1-Gen-2026"
    assert line == (
        "Testland: batch 1-Gen-2026 in the manifest (3 cards) vs 1-GEN-2026 in the CVR (3 rows): likely a typo, the batch IDs "
        "differ in letter case; CORLA matches batch IDs exactly, so a card drawn from it would be a phantom record"
    )
    assert summary(c) == "1 likely typo"


def test_leading_zero_typo(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "B-007", 2)), rows("1", "b-7", 2))
    [typo] = c.typos
    assert typo.how == "the batch IDs differ in letter case and leading zeros"
    assert how_ids_differ("007", "7") == ["leading zeros"] and how_ids_differ(" 7", "7") == ["surrounding spaces"]


def test_count_difference(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "1", 3), ("1", "2", 2)), rows("1", "1", 2) + rows("1", "2", 4))
    assert [(b.label, b.manifest, b.cvr) for b in c.counts_differ] == [("1-1", 3, 2), ("1-2", 2, 4)]
    lines = [line for _, _, line in comparison_lines(c)]
    assert lines == [
        "Testland: batch 1-1: the manifest's 3 cards vs 2 rows in the CVR (-1): a card drawn past the CVR's rows would be a phantom record",
        "Testland: batch 1-2: the manifest's 2 cards vs 4 rows in the CVR (+2): the CVR's extra rows cannot be drawn",
    ]


def test_repeated_manifest_batch(tmp_path: Path) -> None:
    c = compare(tmp_path, manifest(("1", "1", 3), ("1", "1", 3)), rows("1", "1", 6))
    assert c.repeated == [("1", "1", 2, 6)] and not c.counts_differ
    assert comparison_lines(c)[0][2] == "Testland: batch 1-1 is listed 2 times in the manifest (6 cards in all)"


def test_anonymizer_aggregate_row_belongs_to_no_batch(tmp_path: Path) -> None:
    """The anonymizer's AGGREGATED row (identifiers blank) and a county's consolidated row are counted apart."""
    aggregated = Ballot((MAYOR,), cvr_number="AGGREGATED", ballot_type="AGGREGATED", tabulator="", batch="")
    consolidated = Ballot((MAYOR,), cvr_number="Redacted and Consolidated 18 Ballots", tabulator="", batch="")
    path = tmp_path / "cvr.csv"
    path.write_bytes(make_cvr(rows("1", "1", 3) + [aggregated, consolidated]))
    found = read_cvr_batches(path)
    assert found.counts == {(1, "1"): 3} and found.rows == 3
    assert (found.aggregate_rows, found.aggregate_ballots) == (2, 18)
    c = compare_batches("Testland", manifest(("1", "1", 3)), found)
    assert c.differences == 0
    short = compare_batches("Testland", manifest(("1", "1", 4)), found)
    assert comparison_lines(short)[0][2].endswith("(the CVR's 2 aggregate rows, 18 ballots belong to no batch)")


def test_redacted_and_blanked_rows_count_in_their_batch(tmp_path: Path) -> None:
    """A row whose votes are redacted (marked or blanked entirely) is still a ballot card in its batch."""
    cvr_rows = rows("1", "1", 2) + rows("1", "1", 1, blank=True) + rows("1", "1", 1, marker="Redacted")
    c = compare(tmp_path, manifest(("1", "1", 4)), cvr_rows)
    assert c.differences == 0 and c.cvr_rows == 4


def test_cvr_without_batch_column(tmp_path: Path) -> None:
    path = tmp_path / "cvr.csv"
    data = make_cvr(rows("1", "1", 2)).decode().replace("BatchId", "Group")
    path.write_text(data)
    with pytest.raises(NoBatchColumns):
        read_cvr_batches(path)


# ---------------------------------------------------------------------------
# In the totals check


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def test_totals_check_compares_batches_before_any_results(ws: Workspace, log: EventLog) -> None:
    """Weld's manifest lists batches 7-1 (25), 7-2 (25), 7-3 (15); its CVR spells 7-1 as 7-01, lacks 7-3, adds 7-4."""
    fetch(ws, log, election_files(files_uploadStatus_csv=None))
    cvr_rows = rows("7", "01", 25) + rows("7", "2", 24) + rows("7", "4", 5)
    upload = receive(ws, log, "Weld", "redacted_cvr", io.BytesIO(make_cvr(cvr_rows)), "weld.csv", ACTOR)
    assert upload.accepted, upload.report
    datamap = build_map(log, ws)
    seq = record_totals_check(ws, log, datamap)
    assert seq is not None, "recorded with no results, since a county has a manifest and a CVR"
    found = last_totals_check(log)
    assert found is not None
    diffs = [d for d in found[2]["differences"] if isinstance(d, dict) and d["check"] == "manifest_cvr_batches"]  # type: ignore[union-attr]
    lines = sorted(str(d["line"]) for d in diffs)
    assert all(d["flag"] and d["county"] == "Weld" for d in diffs) and len(lines) == 4
    assert any("batch 7-1 in the manifest (25 cards) vs 7-01 in the CVR (25 rows): likely a typo, the batch IDs differ in leading zeros" in x for x in lines)
    assert any("batch 7-3 in the manifest (15 cards) is not in the CVR" in x for x in lines)
    assert any("batch 7-4 in the CVR (5 rows) is not in the manifest" in x for x in lines)
    assert any("batch 7-2: the manifest's 25 cards vs 24 rows in the CVR (-1)" in x for x in lines)
    flags = [f for f in check_flags(log, ws, datamap) if f.reason == "totals_differ"]
    assert len(flags) == 4 and all(f.counties == ("Weld",) for f in flags)


def test_totals_check_caps_batch_lines(ws: Workspace, log: EventLog) -> None:
    many = b"County,Device ID,Batch,# of Ballots\r\n" + b"".join(f"Weld,7,{n},1\r\n".encode() for n in range(1, 21))
    fetch(ws, log, election_files(files_uploadStatus_csv=None, files_Weld_csv=many))
    upload = receive(ws, log, "Weld", "redacted_cvr", io.BytesIO(make_cvr(rows("7", "1", 1) * 10)), "weld.csv", ACTOR)
    assert upload.accepted, upload.report
    report = check_totals(ws, log, build_map(log, ws))
    diffs = [d for d in report.differences if d.check == "manifest_cvr_batches"]
    assert len(diffs) == MAX_COUNTY_LINES + 1
    assert diffs[-1].line == (
        "Weld: and 12 more batch differences between the manifest and the CVR "
        "(19 batches only in the manifest, 1 count differing in all, over 20 manifest batches)"
    )
    assert not diffs[-1].flag and all(d.flag for d in diffs[:-1])
