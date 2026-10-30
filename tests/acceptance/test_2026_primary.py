"""Acceptance tests: reproduce CORLA's ballot selection for the July 2026 primary.

Marked ``slow`` and skipped by default; run with ``uv run pytest -m slow``.

The data are public files from the Secretary of State's Audit Center, read
from the public mirror https://github.com/nealmcb/auditcenter at the commits
pinned in mirror_files.json (see mirror.py for how they are obtained). They
are:

- the 63 county ballot manifests in ``2026/primary/files/``: every county
  manifest published for this election (San Juan County, which counts by
  hand, has none). 59 are pinned at the mirror's first commit of the
  election's manifests; two were added at a later commit, and two are
  pinned at the later commits that last changed them, their first postings
  having held content byte-for-byte identical to a third manifest's;
- ``ContestIDsByCounty.csv``, ``ContestsListRound1.csv`` and
  ``ContestMarginAndContestCVR-IDs-Round1.csv`` from ``finalReports/``;
- two larger ``finalReports/`` files that the tests read only in part:
  ``CVRtoAuditBoardInterpretationComparison.csv`` (14 MB), for the imprinted
  ID of each selected CVR, and ``CountyBallotManifestToCVRcomparison.csv``,
  for Alamosa's lines.

The seed, 49006417086137856424, is in the Secretary of State's 2026 primary
State Audit Report, ``2026/primary/finalReports/StateAuditReport.xlsx`` in
the mirror (commit ba18ba6874c5ed4be713b3745a5d32c261cc6083), on the
"Summary" sheet as "Audit Random Seed". It is written here rather than read
from that file.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from corla_observer.sampling import (
    Domain,
    ManifestAdjustment,
    ManifestBatch,
    build_domain,
    contest_counties,
    county_key,
    find_duplicate_manifests,
    load_county_id_order,
    load_county_manifest,
    select_ballots,
    total_manifest_ballot_count,
)

pytestmark = pytest.mark.slow

SEED_2026_PRIMARY = "49006417086137856424"
ELECTION = Path("2026/primary")

#: An adjustment supplies one batch of Alamosa's rows, so the statewide
#: domain is the one CORLA sampled. files/Alamosa.csv ends at tabulator 102,
#: batch 138, and sums to 3,464 cards; CORLA's own published figures account
#: for 11 cards more:
#: - ContestsListRound1.csv gives ballot_card_count 3,475 for Alamosa's
#:   county-wide contests, and 1,444,047 for the statewide contest, where
#:   the 63 published manifests sum to 1,444,036;
#: - CountyBallotManifestToCVRcomparison.csv, CORLA's batch-by-batch
#:   comparison of the manifest it holds with the CVR file, lists
#:   "Alamosa,102,139,11,11,0": a batch 139 of 11 cards with no discrepancy.
#: So the manifest CORLA used has that batch after batch 138. No storage
#: location for it is published, and it doesn't affect sampling.
#: `test_alamosa_evidence` checks this evidence; the other tests then supply
#: the batch as an explicit adjustment.
ALAMOSA_2026_PRIMARY_ADJUSTMENT = ManifestAdjustment(
    county="Alamosa",
    reason=(
        "Batch 102-139 (11 cards) is accounted for by CORLA's "
        "CountyBallotManifestToCVRcomparison.csv and its ballot_card_count"
    ),
    append=(ManifestBatch("Alamosa", "102", "139", 11, ""),),
)
ADJUSTMENTS_2026_PRIMARY = [ALAMOSA_2026_PRIMARY_ADJUSTMENT]

#: files/Prowers.csv spells its county "Prowes" on one row (batch 102-4);
#: every other row says "Prowers". build_domain refuses a manifest that
#: names two counties, so the caller has to say what the typo means.
COUNTY_ALIASES_2026_PRIMARY = {"Prowes": "Prowers"}


class Primary2026:
    """The 2026 primary's public files, and CORLA's selections derived from them."""

    def __init__(self, mirror_root: Path) -> None:
        self.files = mirror_root / ELECTION / "files"
        self.reports = mirror_root / ELECTION / "finalReports"
        self.contest_ids = self.reports / "ContestIDsByCounty.csv"
        with open(self.reports / "ContestsListRound1.csv", encoding="utf-8-sig", newline="") as f:
            self.contests = {row["contest_name"]: row for row in csv.DictReader(f)}
        margin = self.reports / "ContestMarginAndContestCVR-IDs-Round1.csv"
        with open(margin, encoding="utf-8-sig", newline="") as f:
            self.cvr_ids = {
                row["contest_name"]: [str(c) for c in json.loads(row["contest_cvr_ids"])]
                for row in csv.DictReader(f)
            }
        self.imprinted = self._selected_imprinted_ids()

    def _selected_imprinted_ids(self) -> dict[str, str]:
        """``{cvr_id: imprinted_id}`` for every CVR in any contest's selection.

        Read from CVRtoAuditBoardInterpretationComparison.csv, which has one
        row per selected CVR and contest. Each selected CVR must have a
        single county and imprinted ID throughout it.
        """
        wanted = {c for ids in self.cvr_ids.values() for c in ids}
        found: dict[str, tuple[str, str]] = {}
        comparison = self.reports / "CVRtoAuditBoardInterpretationComparison.csv"
        with open(comparison, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                cvr_id = row["cvr_id"]
                if cvr_id not in wanted:
                    continue
                pair = (row["county_name"], row["imprinted_id"].strip())
                assert found.setdefault(cvr_id, pair) == pair, f"cvr_id {cvr_id} has two identities"
        assert wanted <= found.keys(), f"{len(wanted - found.keys())} selected CVRs not found"
        return {cvr_id: imprinted for cvr_id, (_, imprinted) in found.items()}

    def corla_selection(self, contest_name: str) -> list[str]:
        """CORLA's published selection for a contest, as imprinted IDs in its published order.

        ``contest_cvr_ids`` in ContestMarginAndContestCVR-IDs-Round1.csv is
        CORLA's list of the CVR records drawn for the contest; the imprinted
        IDs come from CVRtoAuditBoardInterpretationComparison.csv.
        """
        return [self.imprinted[c] for c in self.cvr_ids[contest_name]]

    def targeted_contests(self) -> list[str]:
        return [
            name
            for name, row in self.contests.items()
            if row["audit_reason"] in ("county_wide_contest", "state_wide_contest")
            and int(row["audited_sample_count"]) > 0
        ]

    def domain_for(self, contest_name: str, adjustments: Sequence[ManifestAdjustment]) -> Domain:
        order = load_county_id_order(self.contest_ids)
        counties = contest_counties(self.contest_ids, contest_name)
        return build_domain(
            self.files,
            None if len(counties) == len(order) else counties,
            county_order=order,
            adjustments=adjustments,
            county_aliases=COUNTY_ALIASES_2026_PRIMARY,
        )

    def reproduce(self, contest_name: str) -> tuple[list[str], list[str], Domain]:
        """Return (our draws, CORLA's selection, domain) for one contest."""
        row = self.contests[contest_name]
        domain = self.domain_for(contest_name, ADJUSTMENTS_2026_PRIMARY)
        assert domain.size == int(row["ballot_card_count"]), "domain size differs from CORLA's"
        draws = select_ballots(SEED_2026_PRIMARY, domain, int(row["audited_sample_count"]))
        return [d.imprinted_id for d in draws], self.corla_selection(contest_name), domain


@pytest.fixture(scope="module")
def primary(mirror_root: Path) -> Primary2026:
    return Primary2026(mirror_root)


def test_alamosa_evidence(primary: Primary2026) -> None:
    rows = primary.contests
    assert total_manifest_ballot_count(primary.files) == 1_444_036
    assert int(rows["United States Senator - DEM"]["ballot_card_count"]) == 1_444_047
    alamosa = load_county_manifest(primary.files / "Alamosa.csv")
    assert alamosa[-1].key == ("102", "138")
    assert sum(b.ballot_count for b in alamosa) == 3464
    assert int(rows["Alamosa County Assessor - DEM"]["ballot_card_count"]) == 3475
    comparison = primary.reports / "CountyBallotManifestToCVRcomparison.csv"
    with open(comparison, encoding="utf-8-sig", newline="") as f:
        reconciled = {
            (r["scanner_id"], r["batch_id"]): r
            for r in csv.DictReader(f)
            if r["county_name"] == "Alamosa"
        }
    assert len(reconciled) == len(alamosa) + 1
    assert reconciled[("102", "139")]["count_per_manifest"] == "11"
    assert reconciled[("102", "139")]["difference"] == "0"


def test_no_duplicate_manifests_in_2026_primary_files(primary: Primary2026) -> None:
    assert find_duplicate_manifests(primary.files) == {}


def test_prowers_typo_is_refused_unless_aliased(primary: Primary2026) -> None:
    order = load_county_id_order(primary.contest_ids)
    with pytest.raises(ValueError, match="Prowes"):
        build_domain(primary.files, ["Prowers"], county_order=order)
    domain = build_domain(
        primary.files, ["Prowers"], county_order=order, county_aliases=COUNTY_ALIASES_2026_PRIMARY
    )
    assert {b.county for b in domain.batches} == {"Prowers"}


def test_jackson_manifest_uses_blank_continuation_rows(primary: Primary2026) -> None:
    jackson = primary.files / "Jackson.csv"
    raw = jackson.read_text(encoding="utf-8-sig").splitlines()[1:]
    assert sum(1 for line in raw if line.startswith(",")) > 0
    batches = load_county_manifest(jackson)
    assert len(batches) == len(raw)
    assert all(b.county == "Jackson" for b in batches)


ACCEPTANCE_CONTESTS = [
    "Representative to the 120th United States Congress - District 3 - DEM (La Plata)",
    "Representative to the 120th United States Congress - District 3 - REP (La Plata)",
    "Attorney General - REP (Weld)",
    "State Representative - District 19 - DEM (Weld)",
    "Attorney General - DEM (Morgan)",
    "Attorney General - REP (Morgan)",
    "United States Senator - DEM",
]


@pytest.mark.parametrize("contest_name", ACCEPTANCE_CONTESTS)
def test_2026_primary_reproduces_corla_selection(primary: Primary2026, contest_name: str) -> None:
    """Our first N draws equal CORLA's published selection, duplicates included.

    Compared as multisets: CORLA's published ``contest_cvr_ids`` lists the
    same cards with the same repeat counts, but not in draw order (its order
    comes from how CORLA looked the drawn cards up in its database).
    """
    ours, corla, _ = primary.reproduce(contest_name)
    assert len(ours) == len(corla)
    assert Counter(ours) == Counter(corla)


def test_2026_primary_statewide_needs_the_manifest_adjustment(primary: Primary2026) -> None:
    """Without the adjustment the statewide domain is 11 cards smaller than CORLA's, and the draws differ."""
    contest = "United States Senator - DEM"
    row = primary.contests[contest]
    domain = primary.domain_for(contest, adjustments=())
    assert domain.size == int(row["ballot_card_count"]) - 11
    draws = select_ballots(SEED_2026_PRIMARY, domain, int(row["audited_sample_count"]))
    assert Counter(d.imprinted_id for d in draws) != Counter(primary.corla_selection(contest))

    adjusted = primary.domain_for(contest, ADJUSTMENTS_2026_PRIMARY)
    assert adjusted.applied_adjustments == [ALAMOSA_2026_PRIMARY_ADJUSTMENT]


def test_2026_primary_reproduces_every_targeted_contest(primary: Primary2026) -> None:
    """Every targeted contest of the 2026 primary, county-wide and statewide."""
    contests = primary.targeted_contests()
    assert len(contests) == 113
    mismatched = []
    for name in contests:
        ours, corla, _ = primary.reproduce(name)
        if Counter(ours) != Counter(corla):
            mismatched.append(name)
    assert mismatched == []


def test_2026_primary_selection_lists_include_repeat_draws(primary: Primary2026) -> None:
    """CORLA's per-contest lists keep repeat draws; so must ours."""
    ours, corla, _ = primary.reproduce("State Representative - District 19 - DEM (Weld)")
    assert len(ours) == 307
    assert len(set(ours)) == 306
    assert Counter(ours) == Counter(corla)


def test_county_key_matches_manifest_and_report_spellings(primary: Primary2026) -> None:
    order = load_county_id_order(primary.contest_ids)
    assert len(order) == 63
    for spelling in ["ClearCreek", "SanMiguel", "MESA", "La Plata", "LaPlata"]:
        assert county_key(spelling) in order
    assert order["broomfield"] == 64
