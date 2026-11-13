"""A county's ballot manifest against its cast vote record (CVR), batch by batch.

CORLA finds a drawn ballot card's CVR record by county, tabulator, batch and
position (``CastVoteRecordQueries.atPosition``), with the tabulator read as a
whole number on both sides and the batch ID compared exactly, letter case
included. A manifest batch whose ID the CVR spells differently (Douglas's 2020
manifest has ``Gen-2026`` where its CVR has ``GEN-2026``) has no CVR records in
CORLA, so a card drawn from it becomes a PHANTOM RECORD; a CVR batch the
manifest lacks can never be drawn. CORLA's own ``batch_count_comparison``
export shows neither: its join from manifest to CVR keeps only batches with
CVR records, and it never starts from the CVR's side.

`compare_batches` compares a manifest's batches (`ChosenManifest.batches`, as
the data map chose and adjusted them) with the batches of the county's
redacted CVR as uploaded (`read_cvr_batches`), keyed by (tabulator, batch) as
CORLA keys them: the tabulator as a whole number when it is one, the batch ID
as written (after removing surrounding spaces and Excel's ``="..."``
wrapping, as the readers do). It reports, in both directions:

- **likely typos**: a batch only in the manifest and one only in the CVR
  whose IDs are the same when compared ignoring letter case, surrounding
  spaces and leading zeros in their numeric parts, with how they differ;
- batches only in the manifest, and batches only in the CVR;
- batches in both whose counts differ: the manifest's cards against the
  CVR's rows;
- batches the manifest lists more than once (their counts are added).

In the CVR, every row counts as one ballot card in its batch, including rows
whose votes were redacted or blanked (the anonymizer's redacted rows keep
their tabulator, batch and record IDs). Aggregate rows (`CvrRow.aggregate`,
or a ``BallotType`` of ``AGGREGATED``: the anonymizer's summary rows, or a
county's ``Redacted and Consolidated 18 Ballots``) stand for no card in any
batch: they are counted apart, with the ballots they name when they say how
many, and never reported as a batch.

The totals check (`corla_observer.totals`, check ``manifest_cvr_batches``)
runs this for every county with both a manifest and a redacted CVR. Only
counts and IDs are read and returned, never a vote.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from corla_observer.cvr import CvrReader
from corla_observer.sampling import ManifestBatch

#: A batch as CORLA keys it: (tabulator, batch ID), the tabulator a whole number when it is one.
BatchKey = tuple[int | str, str]

_DIGITS = re.compile(r"[0-9]+")
_CONSOLIDATED = re.compile(r"([0-9]+)\s*ballot", re.IGNORECASE)


class NoBatchColumns(ValueError):
    """The CVR has no tabulator or no batch column, so its batches are unknown."""


def batch_key(tabulator: str, batch: str) -> BatchKey:
    """The key CORLA would match on: the tabulator as a whole number if it is one, and the batch ID as written."""
    t = tabulator.strip()
    return (int(t) if t.isdigit() else t, batch)


def loose_id(value: str) -> str:
    """An ID compared loosely: surrounding spaces removed, letters casefolded, leading zeros dropped from each run of digits."""
    return _DIGITS.sub(lambda m: m.group().lstrip("0") or "0", value.strip()).casefold()


def how_ids_differ(a: str, b: str) -> list[str]:
    """How two IDs equal under `loose_id` differ: ``letter case``, ``leading zeros``, ``surrounding spaces``."""
    ways = []
    if a != a.strip() or b != b.strip():
        ways.append("surrounding spaces")
    a, b = a.strip(), b.strip()

    def zeros(s: str) -> str:
        return _DIGITS.sub(lambda m: m.group().lstrip("0") or "0", s)

    if zeros(a) != zeros(b):
        ways.append("letter case")
    if a.casefold() != b.casefold():
        ways.append("leading zeros")
    return ways


def show_batch(tabulator: str, batch: str) -> str:
    """A batch as ``tabulator-batch``, each ID quoted when blank or when it has surrounding spaces."""

    def show(value: str) -> str:
        return value if value and value == value.strip() else f'"{value}"'

    return f"{show(tabulator)}-{show(batch)}"


@dataclass
class CvrBatches:
    """A CVR's rows per batch, and its aggregate rows, which belong to no batch."""

    #: Rows per batch key, each with the tabulator and batch IDs as first written.
    counts: dict[BatchKey, int] = field(default_factory=dict)
    written: dict[BatchKey, tuple[str, str]] = field(default_factory=dict)
    rows: int = 0
    aggregate_rows: int = 0
    #: Ballots the aggregate rows name (``Redacted and Consolidated 18 Ballots``); rows that name none add nothing.
    aggregate_ballots: int = 0

    def add(self, tabulator: str, batch: str, count: int = 1) -> None:
        key = batch_key(tabulator, batch)
        self.counts[key] = self.counts.get(key, 0) + count
        self.written.setdefault(key, (tabulator, batch))
        self.rows += count


def read_cvr_batches(path: Path | str) -> CvrBatches:
    """The rows per batch of a CVR export (`corla_observer.cvr.CvrReader`); see the module documentation.

    Raises:
        NoBatchColumns: if the CVR has no tabulator or batch column.
        corla_observer.cvr.CvrFormatError: if it is not a CVR the reader recognizes.
    """
    reader = CvrReader(path)
    if reader.header.tabulator_index is None or reader.header.batch_index is None:
        raise NoBatchColumns("the CVR has no TabulatorNum or no BatchId column")
    found = CvrBatches()
    for row in reader.rows():
        if row.aggregate or (row.ballot_type or "").upper() == "AGGREGATED":
            found.aggregate_rows += 1
            named = _CONSOLIDATED.search(row.cvr_number)
            found.aggregate_ballots += int(named.group(1)) if named else 0
            continue
        found.add(row.tabulator, row.batch)
    return found


@dataclass(frozen=True)
class BatchCount:
    """A batch on one side only, or in both with different counts."""

    tabulator: str
    batch: str
    #: The manifest's cards (None if the batch is only in the CVR).
    manifest: int | None
    #: The CVR's rows (None if the batch is only in the manifest).
    cvr: int | None

    @property
    def label(self) -> str:
        return show_batch(self.tabulator, self.batch)


@dataclass(frozen=True)
class LikelyTypo:
    """A manifest batch and a CVR batch whose IDs match only loosely (`loose_id`)."""

    manifest_tabulator: str
    manifest_batch: str
    cvr_tabulator: str
    cvr_batch: str
    manifest: int
    cvr: int

    @property
    def how(self) -> str:
        """How the IDs differ, as ``the batch IDs differ in letter case`` and the like."""
        parts = []
        for name, a, b in (("tabulator", self.manifest_tabulator, self.cvr_tabulator), ("batch", self.manifest_batch, self.cvr_batch)):
            if (batch_key(a, "") != batch_key(b, "")) if name == "tabulator" else a != b:
                parts.append(f"the {name} IDs differ in {' and '.join(how_ids_differ(a, b))}")
        return "; ".join(parts)


@dataclass
class BatchComparison:
    """What `compare_batches` found for one county."""

    county: str
    batches: int = 0
    matched: int = 0
    manifest_cards: int = 0
    cvr_rows: int = 0
    aggregate_rows: int = 0
    aggregate_ballots: int = 0
    typos: list[LikelyTypo] = field(default_factory=list)
    manifest_only: list[BatchCount] = field(default_factory=list)
    cvr_only: list[BatchCount] = field(default_factory=list)
    counts_differ: list[BatchCount] = field(default_factory=list)
    #: Batches the manifest lists more than once: (tabulator, batch, times listed, cards in all).
    repeated: list[tuple[str, str, int, int]] = field(default_factory=list)

    @property
    def differences(self) -> int:
        return len(self.typos) + len(self.manifest_only) + len(self.cvr_only) + len(self.counts_differ) + len(self.repeated)


def compare_batches(county: str, manifest: Sequence[ManifestBatch], cvr: CvrBatches) -> BatchComparison:
    """Compare a manifest's batches with a CVR's; see the module documentation."""
    out = BatchComparison(county, aggregate_rows=cvr.aggregate_rows, aggregate_ballots=cvr.aggregate_ballots, cvr_rows=cvr.rows)
    cards: dict[BatchKey, int] = {}
    listed: dict[BatchKey, int] = {}
    written: dict[BatchKey, tuple[str, str]] = {}
    for b in manifest:
        key = batch_key(b.tabulator_id, b.batch_number)
        cards[key] = cards.get(key, 0) + b.ballot_count
        listed[key] = listed.get(key, 0) + 1
        written.setdefault(key, (b.tabulator_id, b.batch_number))
        out.manifest_cards += b.ballot_count
    out.batches = len(cards)
    out.repeated = [(*written[k], n, cards[k]) for k, n in listed.items() if n > 1]
    only_manifest = [k for k in cards if k not in cvr.counts]
    only_cvr = [k for k in cvr.counts if k not in cards]
    for key, n in cards.items():
        if key in cvr.counts:
            if cvr.counts[key] == n:
                out.matched += 1
            else:
                out.counts_differ.append(BatchCount(*written[key], n, cvr.counts[key]))

    def loose(key: BatchKey) -> tuple[str, str]:
        return loose_id(str(key[0])), loose_id(key[1])

    by_loose: dict[tuple[str, str], list[BatchKey]] = {}
    for key in only_cvr:
        by_loose.setdefault(loose(key), []).append(key)
    paired_cvr: set[BatchKey] = set()
    for key in only_manifest:
        candidates = by_loose.get(loose(key), [])
        other = next((c for c in candidates if c not in paired_cvr), None)
        if other is None:
            out.manifest_only.append(BatchCount(*written[key], cards[key], None))
            continue
        paired_cvr.add(other)
        out.typos.append(LikelyTypo(*written[key], *cvr.written[other], cards[key], cvr.counts[other]))
    out.cvr_only = [BatchCount(*cvr.written[k], None, cvr.counts[k]) for k in only_cvr if k not in paired_cvr]
    return out


def comparison_lines(c: BatchComparison) -> list[tuple[str, str, str]]:
    """Each difference as (kind, batch, line): kind ``typo``, ``manifest_only``, ``cvr_only``, ``count`` or ``repeated``, and the batch as the manifest writes it (else the CVR)."""
    lines: list[tuple[str, str, str]] = []
    for t in c.typos:
        lines.append(("typo", show_batch(t.manifest_tabulator, t.manifest_batch), (
            f"{c.county}: batch {show_batch(t.manifest_tabulator, t.manifest_batch)} in the manifest ({t.manifest:,} cards) vs "
            f"{show_batch(t.cvr_tabulator, t.cvr_batch)} in the CVR ({t.cvr:,} rows): likely a typo, {t.how}; CORLA matches batch IDs "
            "exactly, so a card drawn from it would be a phantom record"
        )))  # fmt: skip
    for b in c.manifest_only:
        lines.append(("manifest_only", b.label, f"{c.county}: batch {b.label} in the manifest ({b.manifest:,} cards) is not in the CVR: "
                                       "a card drawn from it would be a phantom record"))  # fmt: skip
    for b in c.cvr_only:
        lines.append(("cvr_only", b.label, f"{c.county}: batch {b.label} in the CVR ({b.cvr:,} rows) is not in the manifest: its ballots cannot be drawn"))
    for b in c.counts_differ:
        assert b.manifest is not None and b.cvr is not None
        gap = b.cvr - b.manifest
        why = ("a card drawn past the CVR's rows would be a phantom record" if gap < 0 else "the CVR's extra rows cannot be drawn")
        if gap < 0 and c.aggregate_rows:
            ballots = f", {c.aggregate_ballots:,} ballots" if c.aggregate_ballots else ""
            why += f" (the CVR's {c.aggregate_rows:,} aggregate row{'s' if c.aggregate_rows != 1 else ''}{ballots} belong to no batch)"
        lines.append(("count", b.label, f"{c.county}: batch {b.label}: the manifest's {b.manifest:,} cards vs {b.cvr:,} rows in the CVR ({gap:+,}): {why}"))
    for tab, batch, times, n in c.repeated:
        lines.append(("repeated", show_batch(tab, batch), f"{c.county}: batch {show_batch(tab, batch)} is listed {times} times in the manifest ({n:,} cards in all)"))
    return lines


def summary(c: BatchComparison) -> str:
    """The comparison's totals in a few words."""
    parts = [
        (len(c.typos), "likely typo", "likely typos"),
        (len(c.manifest_only), "batch only in the manifest", "batches only in the manifest"),
        (len(c.cvr_only), "batch only in the CVR", "batches only in the CVR"),
        (len(c.counts_differ), "count differing", "counts differing"),
        (len(c.repeated), "batch listed more than once", "batches listed more than once"),
    ]
    found = [f"{n:,} {one if n == 1 else many}" for n, one, many in parts if n]
    return ", ".join(found) or "no differences"
