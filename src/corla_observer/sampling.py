"""Reproduce CORLA's ballot selection from a seed and the county ballot manifests.

CORLA (Colorado's risk-limiting audit software) chooses the ballot cards to
audit for each targeted contest in three steps, all reproduced here:

1. **Domain.** The contest's counties' ballot manifests are concatenated
   into one numbered sequence of ballot cards (the sampling domain). A
   county-wide contest uses that county's whole manifest; a multi-county or
   statewide contest uses every participating county's manifest, combined in
   ascending official CORLA county-ID order and, within a county, in manifest
   row order (``BallotManifestInfo.Sort`` in CORLA's source). Position *n* in
   the domain is one physical card, identified by its imprinted ID
   ``{tabulator}-{batch}-{position in batch}``.
2. **Draws.** CORLA's SHA-256 pseudo-random number generator
   (``us.freeandfair.corla.crypto.PseudoRandomNumberGenerator``) turns the
   public 20-digit seed into a sequence of positions in ``[1, domain size]``,
   with replacement.
3. **Selection.** The first *N* draws, where *N* is the contest's
   ``audited_sample_count``, are the contest's selections, kept in draw
   order. A card drawn twice is two draws.

Derived from Neal McBurnett's selection-reproduction code, checked against
CORLA's historical selections.

Manifests as published sometimes differ from the manifest CORLA actually
used, for example when a batch is missing from the public download. This
module never corrects a manifest on its own initiative: every correction is a
`ManifestAdjustment` the caller supplies explicitly, with its reason, and a
`Domain` records which adjustments it applied.

Colorado's manifest CSVs use a blank-continuation-row convention: often only
the first row of a county's block carries a value in the County column, and
later rows leave it blank. A loader that filters or groups on a literal
non-empty County value silently drops every continuation row.
`load_county_manifest` forward-fills instead.

Standard library only.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: CORLA rejects seeds shorter than this (``MINIMUM_SEED_LENGTH`` in
#: ``PseudoRandomNumberGenerator``). Not enforced here, so that short seeds
#: can be used in tests.
CORLA_MINIMUM_SEED_LENGTH = 20


def sha256_random_numbers(seed: str, count: int, domain_size: int) -> list[int]:
    """Generate ``count`` picks in [1, domain_size] via CORLA's SHA-256 PRNG.

    Matches ``us.freeandfair.corla.crypto.PseudoRandomNumberGenerator`` as
    called by ``controller.BallotSelection.randomSelection`` (with
    replacement, minimum 1, maximum ``domain_size``): for each 1-based index
    i, hash the UTF-8 string ``"{seed},{i}"`` with SHA-256, interpret the
    digest as an unsigned big-endian integer, and take
    ``(int mod domain_size) + 1``.

    Picks are not deduplicated: sampling is with replacement, so a position
    drawn twice appears twice, at both of its draw indexes.

    Raises:
        ValueError: if ``domain_size`` is not positive or ``count`` is negative.
    """
    if domain_size <= 0:
        raise ValueError(f"domain_size must be positive, got {domain_size}")
    if count < 0:
        raise ValueError(f"count must not be negative, got {count}")
    picks = []
    for i in range(1, count + 1):
        digest = hashlib.sha256(f"{seed},{i}".encode("utf-8")).digest()
        value = int.from_bytes(digest, byteorder="big")
        picks.append(value % domain_size + 1)
    return picks


@dataclass(frozen=True)
class ManifestBatch:
    """One row of a ballot manifest: a contiguous run of ``ballot_count`` cards."""

    county: str
    tabulator_id: str
    batch_number: str
    ballot_count: int
    location: str

    @property
    def key(self) -> tuple[str, str]:
        """The ``(tabulator_id, batch_number)`` pair identifying this batch in its county."""
        return (self.tabulator_id, self.batch_number)


@dataclass(frozen=True)
class BallotRef:
    """A single physical ballot card, addressable by its imprinted ID."""

    county: str
    tabulator_id: str
    batch_number: str
    position: int

    def imprinted_id(self) -> str:
        """The card's imprinted ID, ``{tabulator}-{batch}-{position in batch}``."""
        return f"{self.tabulator_id}-{self.batch_number}-{self.position}"


def _normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_column(fieldnames: Sequence[str], candidates: Sequence[str]) -> str | None:
    by_normalized = {_normalize_header(f): f for f in fieldnames if f}
    for candidate in candidates:
        key = _normalize_header(candidate)
        if key in by_normalized:
            return by_normalized[key]
    return None


# Header spellings seen in counties' published manifests across elections.
# Matching ignores case and every non-alphanumeric character, so for example
# "#of Ballots" and "# of ballots" both match "# of ballots". Some counties
# use a bare "Device" column, "# of Ballot Card" (singular), or an entirely
# different vocabulary ("TrackingId"/"BatchCount"/"BallotStorageBox").
# Huerfano's 2024 primary manifest says "# Number of Ballot Cards", and
# Phillips's "tablulator id".
COUNT_COLUMN_CANDIDATES = [
    "# of ballot cards",
    "# of ballots",
    "# of ballots cards",
    "# of ballot",
    "# of ballot card",
    "number of ballots",
    "number of ballots batched",
    "number of ballot cards",
    "# cards",
    "# ballot cards",
    "# ballots",
    "# of cards",
    "# in batch",
    ". of ballots",
    "batchcount",
]
TABULATOR_COLUMN_CANDIDATES = [
    "tabulator",
    "tabulator id",
    "scanner",
    "scanner id",
    "device id",
    "device",
    "tablulator id",
]
BATCH_COLUMN_CANDIDATES = [
    "batch",
    "batch number",
    "batch no",
    "batch id",
    "batch #",
    "icc batch #",
    "trackingid",
]
LOCATION_COLUMN_CANDIDATES = ["location", "locations", "storage location", "ballotstoragebox"]

#: Non-manifest files that the audit center publishes alongside the county
#: manifests in its ``files/`` directory (the upload-status log), excluded
#: when globbing for manifests.
NON_MANIFEST_FILENAMES = frozenset({"uploadStatus.csv"})


def load_county_manifest(
    manifest_file: Path, county_aliases: Mapping[str, str] | None = None
) -> list[ManifestBatch]:
    """Load one county's ballot manifest, forward-filling blank-continuation County rows.

    Columns are found by header name, tolerating the spellings in
    ``*_COLUMN_CANDIDATES``. If no header names the county column, the first
    column is used, provided no other recognized column claims it: some
    counties put their own name, or nothing at all, in that header cell (for
    example "Conejos,Tabulator ID,Batch,..." or ",Device ID,Batch,...").

    `county_aliases`, if given, maps a County cell value as written to the
    name to use instead, for a caller who has found a misspelling in a
    published file. Nothing is renamed unless the caller asks.

    Raises:
        ValueError: if a required column can't be resolved from the header,
            if a data row has a non-integer ballot count, or if the first
            data row has no County value to seed the forward-fill.
    """
    with open(manifest_file, "r", encoding="utf-8-sig", newline="") as f:
        return parse_county_manifest(f, str(manifest_file), county_aliases)


def parse_county_manifest(
    lines: Iterable[str], source_name: str = "manifest", county_aliases: Mapping[str, str] | None = None
) -> list[ManifestBatch]:
    """`load_county_manifest` on already-decoded text lines (a file object or a list of lines).

    `source_name` names the source in error messages.
    """
    aliases = county_aliases or {}
    manifest_file = source_name
    reader = csv.DictReader(lines)
    fieldnames = list(reader.fieldnames or [])
    county_col = _find_column(fieldnames, ["county"])
    tabulator_col = _find_column(fieldnames, TABULATOR_COLUMN_CANDIDATES)
    batch_col = _find_column(fieldnames, BATCH_COLUMN_CANDIDATES)
    count_col = _find_column(fieldnames, COUNT_COLUMN_CANDIDATES)
    location_col = _find_column(fieldnames, LOCATION_COLUMN_CANDIDATES)
    if (
        county_col is None
        and fieldnames
        and fieldnames[0] not in (tabulator_col, batch_col, count_col, location_col)
    ):
        county_col = fieldnames[0]
    # An empty header name ("") is a real, resolved column key, so test
    # county_col with `is None`, not truthiness.
    if county_col is None or not tabulator_col or not batch_col or not count_col:
        raise ValueError(
            f"{manifest_file}: could not resolve manifest columns from header {fieldnames}"
        )

    batches = []
    current_county = ""
    for row_num, row in enumerate(reader, start=2):
        county_value = (row.get(county_col) or "").strip()
        county_value = aliases.get(county_value, county_value)
        if county_value:
            current_county = county_value
        elif not current_county:
            raise ValueError(
                f"{manifest_file}:{row_num}: blank County with no prior value to forward-fill"
            )

        raw_count = (row.get(count_col) or "").strip()
        try:
            ballot_count = int(raw_count)
        except ValueError as exc:
            raise ValueError(
                f"{manifest_file}:{row_num}: non-integer ballot count {raw_count!r}"
            ) from exc

        batches.append(
            ManifestBatch(
                county=current_county,
                tabulator_id=(row.get(tabulator_col) or "").strip(),
                batch_number=(row.get(batch_col) or "").strip(),
                ballot_count=ballot_count,
                location=(row.get(location_col) or "").strip() if location_col else "",
            )
        )
    return batches


def county_key(name: str) -> str:
    """Case- and space-insensitive key for matching county names across files.

    The same county is spelled differently across CORLA's public files:
    manifests may say "ClearCreek", "SanMiguel" or "MESA" where
    ``ContestIDsByCounty.csv`` says "Clear Creek", "San Miguel" or "Mesa".
    A join that misses one of these would misplace that county in a
    statewide domain and shift every position after it.
    """
    return name.replace(" ", "").lower()


def county_id_order(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    """Build ``{county_key(county_name): county_id}`` from rows with those two columns.

    The rows can come from any CORLA export that pairs each county with its
    official numeric ID, such as ``ContestIDsByCounty.csv``. Raises
    `ValueError` if one county appears with two different IDs.
    """
    order: dict[str, int] = {}
    for row in rows:
        key = county_key(row["county_name"])
        county_id = int(row["county_id"])
        if order.setdefault(key, county_id) != county_id:
            raise ValueError(
                f"county {row['county_name']!r} has two IDs: {order[key]} and {county_id}"
            )
    return order


def load_county_id_order(contest_ids_by_county_csv: Path) -> dict[str, int]:
    """Load each county's official numeric ID from a ``ContestIDsByCounty.csv``-shaped file.

    Returns ``{county_key(county_name): county_id}`` (see `county_id_order`).
    This is CORLA's multi-county and statewide domain concatenation order:
    ascending by this ID, matching the ``BallotManifestInfo.Sort`` comparator
    (county ID ascending, then manifest row order within a county). County
    IDs are not simply alphabetical: Broomfield, the newest county, is last
    (ID 64), and a county with no audited contests, such as hand-counted San
    Juan, may be absent from the file.
    """
    with open(contest_ids_by_county_csv, "r", encoding="utf-8-sig", newline="") as f:
        return county_id_order(csv.DictReader(f))


def contest_counties(contest_ids_by_county_csv: Path, contest_name: str) -> list[str]:
    """Return the sorted, deduplicated list of counties a contest appears in.

    Reads a ``ContestIDsByCounty.csv``-shaped file (columns ``county_name``
    and ``contest_name``). The result is suitable as the `counties` argument
    of `build_domain` for a multi-county contest.
    """
    counties = set()
    with open(contest_ids_by_county_csv, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["contest_name"] == contest_name:
                counties.add(row["county_name"])
    return sorted(counties)


@dataclass(frozen=True)
class ManifestAdjustment:
    """A caller-supplied correction to one county's published manifest.

    Applied in this order: drop each batch whose ``(tabulator_id,
    batch_number)`` key is in `remove`; splice each batch in `insert_after`
    immediately after the batch with the given anchor key; then append the
    batches in `append` after the county's last row.

    Insertion position matters as much as the batch itself: a batch placed
    anywhere but where CORLA had it shifts the domain position of every card
    after it, even when the county's total is right.

    `reason` should say what public evidence justifies the adjustment; it is
    kept with the `Domain` so the correction stays visible in any result.
    """

    county: str
    reason: str
    remove: tuple[tuple[str, str], ...] = ()
    insert_after: tuple[tuple[tuple[str, str], ManifestBatch], ...] = ()
    append: tuple[ManifestBatch, ...] = ()


def apply_manifest_adjustment(
    batches: Sequence[ManifestBatch], adjustment: ManifestAdjustment
) -> list[ManifestBatch]:
    """Return a new batch list with one `ManifestAdjustment` applied.

    Raises:
        ValueError: if a `remove` key or an `insert_after` anchor key is not
            among `batches`, or if an anchor is also being removed.
    """
    present = {b.key for b in batches}
    remove_keys = set(adjustment.remove)
    missing = sorted(remove_keys - present)
    if missing:
        raise ValueError(f"{adjustment.county}: batches to remove not in manifest: {missing}")
    insertions: dict[tuple[str, str], list[ManifestBatch]] = {}
    for anchor_key, new_batch in adjustment.insert_after:
        if anchor_key not in present:
            raise ValueError(f"{adjustment.county}: anchor batch {anchor_key} not in manifest")
        if anchor_key in remove_keys:
            raise ValueError(f"{adjustment.county}: anchor batch {anchor_key} is also removed")
        insertions.setdefault(anchor_key, []).append(new_batch)
    result: list[ManifestBatch] = []
    for batch in batches:
        if batch.key in remove_keys:
            continue
        result.append(batch)
        result.extend(insertions.get(batch.key, []))
    result.extend(adjustment.append)
    return result


@dataclass(frozen=True)
class Domain:
    """A concatenation of one or more county manifests, position-indexed for PRNG lookups.

    Kept at batch granularity (not expanded to one record per ballot) so a
    statewide domain of 1.4M+ ballot cards stays cheap to build and query.

    `county_ranges` maps each county name (as its manifest spells it) to its
    inclusive ``(first, last)`` positions. `applied_adjustments` lists the
    caller-supplied adjustments that were used, and `epistemic_flags` names
    anything else about how the domain was built that a reader of a result
    should know (``"duplicate_manifest_file_dropped"``,
    ``"manifest_adjustment_applied"``).
    """

    batches: list[ManifestBatch] = field(default_factory=list)
    cumulative_ends: list[int] = field(default_factory=list)
    county_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)
    applied_adjustments: list[ManifestAdjustment] = field(default_factory=list)
    epistemic_flags: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        """The number of ballot cards in the domain."""
        return self.cumulative_ends[-1] if self.cumulative_ends else 0

    def ballot_at(self, position: int) -> BallotRef:
        """Return the ballot at 1-indexed `position` in the concatenated domain."""
        if position < 1 or position > self.size:
            raise IndexError(f"position {position} out of range [1, {self.size}]")
        idx = bisect.bisect_left(self.cumulative_ends, position)
        batch = self.batches[idx]
        batch_start = self.cumulative_ends[idx] - batch.ballot_count + 1
        return BallotRef(
            batch.county, batch.tabulator_id, batch.batch_number, position - batch_start + 1
        )


def build_domain_from_manifests(
    manifests: Iterable[Sequence[ManifestBatch]],
    county_order: Mapping[str, int] | None = None,
    adjustments: Sequence[ManifestAdjustment] = (),
) -> Domain:
    """Concatenate already-loaded county manifests into one domain.

    Each element of `manifests` is one county's batches, all naming the same
    county (compared with `county_key`). If `county_order` is given (see
    `load_county_id_order`), counties are combined in ascending order of
    their ID there, which is CORLA's convention; every county must resolve
    in it, or this raises `ValueError` rather than guess a position. If
    `county_order` is omitted, counties are combined in the order given.

    Each adjustment in `adjustments` is applied to its county's batches
    before concatenation. An adjustment for a county not in this domain is
    ignored, so one list of adjustments for an election can be passed to
    every domain built for it.

    Raises:
        ValueError: if a manifest is empty, mixes counties, or repeats a
            county already given; if a county has no `county_order` entry;
            or if an adjustment does not fit its county's manifest (see
            `apply_manifest_adjustment`).
    """
    groups: list[tuple[str, list[ManifestBatch]]] = []
    seen: set[str] = set()
    for manifest in manifests:
        if not manifest:
            raise ValueError("empty manifest")
        name = manifest[0].county
        key = county_key(name)
        mixed = sorted({b.county for b in manifest if county_key(b.county) != key})
        if mixed:
            raise ValueError(f"manifest for {name!r} also names other counties: {mixed}")
        if key in seen:
            raise ValueError(f"more than one manifest for county {name!r}")
        seen.add(key)
        groups.append((name, list(manifest)))

    if county_order is not None:
        unresolved = [name for name, _ in groups if county_key(name) not in county_order]
        if unresolved:
            raise ValueError(f"no county_order entry for: {unresolved}")
        groups.sort(key=lambda item: county_order[county_key(item[0])])

    adjustments_by_county: dict[str, list[ManifestAdjustment]] = defaultdict(list)
    for adjustment in adjustments:
        adjustments_by_county[county_key(adjustment.county)].append(adjustment)

    domain_batches: list[ManifestBatch] = []
    cumulative_ends: list[int] = []
    county_ranges: dict[str, tuple[int, int]] = {}
    applied: list[ManifestAdjustment] = []
    running_total = 0
    for county_name, county_batches in groups:
        for adjustment in adjustments_by_county.get(county_key(county_name), []):
            county_batches = apply_manifest_adjustment(county_batches, adjustment)
            applied.append(adjustment)
        start = running_total + 1
        for batch in county_batches:
            running_total += batch.ballot_count
            domain_batches.append(batch)
            cumulative_ends.append(running_total)
        county_ranges[county_name] = (start, running_total)
    return Domain(
        batches=domain_batches,
        cumulative_ends=cumulative_ends,
        county_ranges=county_ranges,
        applied_adjustments=applied,
        epistemic_flags=["manifest_adjustment_applied"] if applied else [],
    )


def manifest_paths(
    files_dir: Path, filename_suffix: str = ".csv", filename_prefix: str = ""
) -> list[Path]:
    """List the county manifest files directly in `files_dir`, sorted by name.

    Matches ``{filename_prefix}*{filename_suffix}`` and skips
    `NON_MANIFEST_FILENAMES`. `filename_prefix` is for layouts where the
    county name follows a fixed prefix (``manifest-<County>.csv``) and a bare
    suffix would also match non-manifest files in the same directory.
    """
    return sorted(
        p
        for p in files_dir.glob(f"{filename_prefix}*{filename_suffix}")
        if p.name not in NON_MANIFEST_FILENAMES
    )


def build_domain(
    files_dir: Path,
    counties: Sequence[str] | None = None,
    *,
    county_order: Mapping[str, int] | None = None,
    adjustments: Sequence[ManifestAdjustment] = (),
    filename_suffix: str = ".csv",
    filename_prefix: str = "",
    candidate_paths: Sequence[Path] | None = None,
    county_aliases: Mapping[str, str] | None = None,
) -> Domain:
    """Build a single-county, multi-county or statewide domain from manifest files.

    Candidate files are `candidate_paths` if given, otherwise
    `manifest_paths(files_dir, filename_suffix, filename_prefix)`.

    If `counties` is given, only the manifests whose own County column names
    one of them (compared with `county_key`) are used, whatever the file is
    called; every requested county must be found. If `counties` is omitted,
    every candidate file is used: the statewide domain. In that case, if
    `county_order` is also given, every county in it must have a manifest,
    so that a missing file can't silently shrink the domain.

    Pass `county_order` (see `load_county_id_order`) for any domain of more
    than one county: CORLA combines counties in ascending county-ID order.
    Without it, counties are combined in filename order, which is only
    appropriate when order doesn't matter (for example, summing totals).

    Byte-identical candidate files are read once: the audit center has
    published one county's manifest under another county's filename too, and
    reading both would double-count that county. The domain is then flagged
    ``"duplicate_manifest_file_dropped"``.

    `adjustments` and `county_aliases` are passed through to
    `build_domain_from_manifests` and `load_county_manifest`.

    Raises:
        ValueError: if a requested county (or, statewide with `county_order`,
            an ordered county) has no manifest, or for any reason given by
            `build_domain_from_manifests`.
    """
    paths = (
        sorted(candidate_paths)
        if candidate_paths is not None
        else manifest_paths(files_dir, filename_suffix, filename_prefix)
    )

    seen_hashes: set[str] = set()
    duplicate_dropped = False
    manifests: list[list[ManifestBatch]] = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen_hashes:
            duplicate_dropped = True
            continue
        seen_hashes.add(digest)
        batches = load_county_manifest(path, county_aliases)
        if batches:
            manifests.append(batches)

    found = {county_key(m[0].county) for m in manifests}
    if counties is not None:
        wanted = {county_key(c) for c in counties}
        missing = sorted(c for c in counties if county_key(c) not in found)
        if missing:
            raise ValueError(f"no manifest found for: {missing}")
        manifests = [m for m in manifests if county_key(m[0].county) in wanted]
    elif county_order is not None:
        missing = sorted(k for k in county_order if k not in found)
        if missing:
            raise ValueError(f"no manifest found for ordered counties: {missing}")

    domain = build_domain_from_manifests(manifests, county_order, adjustments)
    if duplicate_dropped:
        domain.epistemic_flags.append("duplicate_manifest_file_dropped")
    return domain


@dataclass(frozen=True)
class Draw:
    """One draw of CORLA's PRNG, resolved to a ballot card.

    `index` is the 1-based draw number (the counter hashed with the seed),
    and `position` the 1-based domain position it picked.
    """

    index: int
    position: int
    ballot: BallotRef

    @property
    def imprinted_id(self) -> str:
        """The drawn card's imprinted ID."""
        return self.ballot.imprinted_id()

    @property
    def county(self) -> str:
        """The drawn card's county, as its manifest spells it."""
        return self.ballot.county


def select_ballots(seed: str, domain: Domain, count: int) -> list[Draw]:
    """Return the first `count` draws for `domain`, in draw order.

    `count` is the number of draws CORLA made for the contest (its
    ``audited_sample_count``). Duplicates are kept: a card drawn twice
    appears at both draw indexes, as in CORLA's own per-contest selection
    list.
    """
    picks = sha256_random_numbers(seed, count, domain.size)
    return [
        Draw(index=i, position=p, ballot=domain.ballot_at(p))
        for i, p in enumerate(picks, start=1)
    ]


def total_manifest_ballot_count(
    files_dir: Path, filename_suffix: str = ".csv", filename_prefix: str = ""
) -> int:
    """Sum ballot counts across every county manifest file in `files_dir`.

    A standing sanity check against CORLA's published ballot-card counts: a
    shortfall is the signature of a batch missing from the published
    manifests. See `manifest_paths` for the file-matching arguments.
    """
    return sum(
        b.ballot_count
        for path in manifest_paths(files_dir, filename_suffix, filename_prefix)
        for b in load_county_manifest(path)
    )


def find_duplicate_manifests(
    files_dir: Path, filename_suffix: str = ".csv", filename_prefix: str = ""
) -> dict[str, list[Path]]:
    """Find county manifest files with byte-identical content.

    The audit center has served one county's manifest under more than one
    county's filename (in the 2026 primary, three of the posted manifest
    files briefly held identical content). Two counties with the same
    manifest is almost certainly such an error.

    Returns ``{sha256_hex: [paths]}`` for every hash shared by two or more
    files; files with a unique hash are omitted.
    """
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in manifest_paths(files_dir, filename_suffix, filename_prefix):
        by_hash[hashlib.sha256(path.read_bytes()).hexdigest()].append(path)
    return {digest: paths for digest, paths in by_hash.items() if len(paths) > 1}
