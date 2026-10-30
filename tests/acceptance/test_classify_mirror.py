"""Acceptance tests: classify real audit center files from four elections by content alone.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``.

The files are public files from the Secretary of State's Audit Center, read
from the mirror https://github.com/nealmcb/auditcenter at the commits pinned
in mirror_files.json (see mirror.py). They span four elections whose layouts,
file names, extensions and headers all differ:

- the 2019 coordinated election (``2019/round_1/county_manifest_<County>.csv``,
  ``contest.csv``, ``contestSelection.csv``, ``stateReport.xlsx``, and
  ``batch_count_comparison.csv``, ``contests_by_county.csv`` and
  ``upload_status.csv`` beside them);
- the 2022 general election (``2022/general/round_1/<County>.csv`` with
  upper-case and space-padded county names, a file named
  ``CVRtoAuditBoardInterpretativeComparison.csv`` that holds the
  manifest-to-CVR batch comparison, a Latin-1 canonical contest list);
- the 2024 general election (``<County>BallotManifest.csv``,
  ``targetedContests.xlsx``, ``seed.csv``, and a State Audit Report named
  ``20241120stateGeneral11052024report205PM.xlsx``);
- the 2026 primary (``files/<County>.csv``, ``targetContestSelections.xlsx``,
  the discrepancy report, and ``finalReports/``).

Also archived copies of the Audit Center page itself, from the mirror's
``auditCenters/`` directory, for the Cloudflare-token normalization and the
link extraction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corla_observer.classify import classify_file, extract_seed
from corla_observer.fetch import FetchPolicy, extract_links, is_followed, normalize_html
from corla_observer.sampling import load_county_manifest

pytestmark = pytest.mark.slow

#: (path in the mirror, expected kind, expected county)
EXPECTED: list[tuple[str, str, str | None]] = [
    # 2019 coordinated election
    ("2019/round_1/county_manifest_Adams.csv", "county_manifest", "Adams"),
    ("2019/round_1/county_manifest_Baca.csv", "county_manifest", "Baca"),
    ("2019/round_1/contest.csv", "contests_list", None),
    ("2019/round_1/contestSelection.csv", "selection_list", None),
    ("2019/round_1/stateReport.xlsx", "state_audit_report", None),
    ("2019/round_1/countyManifest.html", "html_page", None),
    ("2019/batch_count_comparison.csv", "manifest_cvr_comparison", None),
    ("2019/contests_by_county.csv", "contest_ids_by_county", None),
    ("2019/upload_status.csv", "upload_status", None),
    ("2019/tabulate.csv", "vote_totals", None),
    ("2019/2019CoordinatedTargetContests.pdf", "unknown", None),
    # 2022 general election
    ("2022/general/ContestsList.csv", "contests_list", None),
    ("2022/general/round_1/Adams.csv", "county_manifest", "Adams"),
    ("2022/general/round_1/Baca.csv", "county_manifest", "Baca"),
    ("2022/general/round_1/Conejos.csv", "county_manifest", "Conejos"),
    ("2022/general/round_1/CVRtoAuditBoardInterpretativeComparison.csv", "manifest_cvr_comparison", None),
    ("2022/general/round_1/contestSelection.csv", "selection_list", None),
    ("2022/general/round_1/contestsByCounty.csv", "contest_ids_by_county", None),
    ("2022/general/round_1/uploadStatus.csv", "upload_status", None),
    ("2022/general/round_1/StateAuditReport.xlsx", "state_audit_report", None),
    ("2022/general/round_1/AuditActivityReport.xlsx", "audit_activity_report", None),
    ("2022/general/round_1/canonicalListFinal.csv", "canonical_contest_list", None),
    # 2024 general election
    ("2024/general/AdamsBallotManifest.csv", "county_manifest", "Adams"),
    ("2024/general/targetedContests.xlsx", "target_contests", None),
    ("2024/general/2024GEDiscrepancyReport.xlsx", "discrepancy_report", None),
    ("2024/general/seed.csv", "seed", None),
    ("2024/general/uploadStatus.csv", "upload_status", None),
    ("2024/general/batchCountComparison.csv", "manifest_cvr_comparison", None),
    ("2024/general/round1/20241120stateGeneral11052024report205PM.xlsx", "state_audit_report", None),
    ("2024/general/round1/contestSelection.csv", "selection_list", None),
    ("2024/general/discrepancy.html", "html_page", None),
    # Older and newer variants: a county's own audit report, the 2018
    # upload status without hashes, CORLA's 2017 list of uploaded files'
    # hashes, header typos in two 2024 primary manifests, CORLA's JSON
    # exports, and a 2025 manifest published under another county's name.
    ("2018G/round_1/county_report_Adams.xlsx", "county_audit_report", "Adams"),
    ("2018G/round_1/upload_status.csv", "upload_status", None),
    ("2017/round_1/m_cvr_hash.csv", "file_hashes", None),
    ("2024/primary/HuerfanoManifest.csv", "county_manifest", "Huerfano"),
    ("2024/primary/PhillipsManifest.csv", "county_manifest", "Phillips"),
    ("2025-irv/seed.json", "seed", None),
    ("2025-irv/contest.json", "contests_list", None),
    ("2025/files/MontroseManifest.csv", "county_manifest", "Montezuma"),
    ("2025/2025CoordinatedRLASelectedContests.xlsx", "target_contests", None),
    # 2026 primary
    ("2026/primary/files/Adams.csv", "county_manifest", "Adams"),
    ("2026/primary/files/Jackson.csv", "county_manifest", "Jackson"),
    ("2026/primary/files/Prowers.csv", "county_manifest", "Prowers"),
    ("2026/primary/files/LaPlata.csv", "county_manifest", "La Plata"),
    ("2026/primary/files/targetContestSelections.xlsx", "target_contests", None),
    ("2026/primary/files/2026DiscrepancyReportPE.xlsx", "discrepancy_report", None),
    ("2026/primary/files/2026DiscrepancyReportPE.docx", "unknown", None),
    ("2026/primary/files/uploadStatus.csv", "upload_status", None),
    ("2026/primary/riskLimit.html", "html_page", None),
    ("2026/primary/finalReports/ContestIDsByCounty.csv", "contest_ids_by_county", None),
    ("2026/primary/finalReports/ContestsListRound1.csv", "contests_list", None),
    ("2026/primary/finalReports/ContestMarginAndContestCVR-IDs-Round1.csv", "selection_list", None),
    ("2026/primary/finalReports/CVRtoAuditBoardInterpretationComparison.csv", "cvr_comparison", None),
    ("2026/primary/finalReports/CountyBallotManifestToCVRcomparison.csv", "manifest_cvr_comparison", None),
    ("2026/primary/finalReports/StateAuditReport.xlsx", "state_audit_report", None),
]


@pytest.mark.parametrize(("path", "kind", "county"), EXPECTED, ids=[p for p, _, _ in EXPECTED])
def test_real_files_are_classified_by_content(
    mirror_root: Path, path: str, kind: str, county: str | None
) -> None:
    result = classify_file(mirror_root / path)
    assert (result.kind, result.county) == (kind, county), result.evidence


def test_every_2026_primary_manifest_is_a_manifest_of_its_own_county(mirror_root: Path) -> None:
    """All 63 published manifests, each naming the county its file is named for."""
    files = sorted((mirror_root / "2026/primary/files").glob("*.csv"))
    manifests = [p for p in files if p.name != "uploadStatus.csv"]
    assert len(manifests) == 63
    for path in manifests:
        result = classify_file(path)
        assert result.kind == "county_manifest", path.name
        assert result.county is not None and result.county.replace(" ", "") == path.stem, path.name
        # The manifest loader accepts every file the classifier calls a manifest.
        assert load_county_manifest(path)


def test_evidence_records_the_variants_found(mirror_root: Path) -> None:
    baca = classify_file(mirror_root / "2022/general/round_1/Baca.csv")
    assert baca.evidence["matched_columns"] == {
        "tabulator": "Tabulator ID",
        "batch": "Batch #",
        "count": "# of Ballot cards",
        "county": "County",
        "location": "Location",
    }
    adams_2024 = classify_file(mirror_root / "2024/general/AdamsBallotManifest.csv")
    assert adams_2024.evidence["matched_columns"] == {
        "tabulator": "Tabulator",
        "batch": "Batch",
        "count": "# of Ballot",
        "county": "County",
    }
    assert classify_file(mirror_root / "2026/primary/files/Adams.csv").evidence["encoding"] == "utf-8-sig"
    assert classify_file(mirror_root / "2026/primary/files/Prowers.csv").evidence[
        "unrecognized_county_values"
    ] == ["Prowes"]
    latin1 = classify_file(mirror_root / "2022/general/round_1/canonicalListFinal.csv")
    assert latin1.evidence["encoding"] == "latin-1"
    assert latin1.evidence["columns"] == ["CountyName", "ContestName", "ContestChoices"]
    target = classify_file(mirror_root / "2026/primary/files/targetContestSelections.xlsx")
    assert (target.evidence["sheet"], target.evidence["header_row"]) == ("Sheet1", 2)
    assert classify_file(mirror_root / "2026/primary/files/2026DiscrepancyReportPE.docx").evidence == {
        "format": "docx"
    }


SEEDS = {
    "2019/round_1/stateReport.xlsx": "87266501476678459695",
    "2022/general/round_1/StateAuditReport.xlsx": "23546992048967376252",
    "2024/general/round1/20241120stateGeneral11052024report205PM.xlsx": "53417960661093690826",
    "2026/primary/finalReports/StateAuditReport.xlsx": "49006417086137856424",
}


@pytest.mark.parametrize(("path", "seed"), SEEDS.items(), ids=list(SEEDS))
def test_extract_seed(mirror_root: Path, path: str, seed: str) -> None:
    data = (mirror_root / path).read_bytes()
    assert extract_seed(data) == seed
    assert classify_file(mirror_root / path).evidence["audit_random_seed"] == seed


def test_2024_seed_file_agrees_with_the_state_audit_report(mirror_root: Path) -> None:
    seed_file = classify_file(mirror_root / "2024/general/seed.csv")
    assert (
        seed_file.evidence["seed"]
        == SEEDS["2024/general/round1/20241120stateGeneral11052024report205PM.xlsx"]
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("auditCenter-2022-11-23T20:51:38-07:00.html", "auditCenter-2022-11-24T14:19:39-07:00.html"),
        ("auditCenter_2022-07-15T08-27.html", "auditCenter_2022-07-15T21-26.html"),
    ],
)
def test_archived_pages_differing_only_in_cloudflare_tokens_normalize_equal(
    mirror_root: Path, first: str, second: str
) -> None:
    a = (mirror_root / "auditCenters" / first).read_bytes()
    b = (mirror_root / "auditCenters" / second).read_bytes()
    assert a != b
    assert normalize_html(a) == normalize_html(b)
    assert b"__CF$cv$params" in a and b"__CF$cv$params" not in normalize_html(a)


def test_archived_pages_with_different_content_stay_different(mirror_root: Path) -> None:
    a = (mirror_root / "auditCenters/auditCenter_2022-07-15T21-26.html").read_bytes()
    b = (mirror_root / "auditCenters/auditCenter-2022-11-24T14:19:39-07:00.html").read_bytes()
    assert normalize_html(a) != normalize_html(b)


def test_links_followed_from_an_archived_audit_center_page(mirror_root: Path) -> None:
    """The July 2026 page, as served from its address then, links to the primary's files."""
    page = (mirror_root / "auditCenters/auditCenter-2026-07-07T12:30:05-06:00.html").read_bytes()
    links = extract_links(page, "https://www.coloradosos.gov/pubs/elections/auditCenter.html")
    followed = [link for link in links if is_followed(link, FetchPolicy())]
    base = "https://www.coloradosos.gov/pubs/elections/RLA/"
    assert base + "2026/primary/files/targetContestSelections.xlsx" in followed
    assert base + "2026/primary/riskLimit.html" in followed
    assert all(url.startswith(base) for url in followed)
    assert not any("mailto:" in url or "zoom.us" in url for url in followed)
