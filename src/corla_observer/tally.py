"""Contest totals and margins from a county's (redacted) cast vote record, for checking the margins used in estimates.

`tally_cvr` adds up every vote column of a Dominion CVR export
(`corla_observer.cvr.CvrReader` reads its layout): each cell that is a
whole number counts that many votes for its choice. So the aggregate rows
the public anonymizer (https://github.com/loriinboulder/anonymize_cvr)
writes in place of the ballots it combines, whose cells hold the combined
counts, count in full; redaction markers (``*``, ``X``, ``Redacted``) and
blanks count nothing, so a ballot whose votes were redacted without being
aggregated is missing from the totals, and the result says how many rows
were like that.

`cvr_margin` is the contest's margin as CORLA computes it
(`corla_observer.enr.margin_of`). Only totals are returned, never a row.

Standard library only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from corla_observer.cvr import CvrReader, is_redaction_marker, unwrap
from corla_observer.datamap import contest_key
from corla_observer.enr import contest_label, margin_of


@dataclass
class ContestTally:
    """One contest's totals in a CVR: votes per choice, and its ``Vote For``."""

    name: str
    vote_for: int
    votes: dict[str, int] = field(default_factory=dict)
    #: Rows where the contest's cells were all redaction markers.
    redacted_rows: int = 0
    #: Rows whose cells for the contest summed to more than one ballot's worth (aggregate rows).
    aggregate_rows: int = 0


@dataclass
class CvrTally:
    contests: dict[str, ContestTally]
    rows: int

    def find(self, name: str) -> ContestTally | None:
        """The contest whose name matches ``name`` as `corla_observer.datamap.contest_key` compares them."""
        key = contest_key(name)
        return next((t for n, t in self.contests.items() if contest_key(n) == key), None)


def tally_cvr(path: Path | str) -> CvrTally:
    """Every contest's totals in a CVR export; see the module documentation."""
    reader = CvrReader(path)
    h = reader.header
    with open(reader.path, encoding=h.encoding, newline="") as f:
        rows = csv.reader(f, delimiter=h.delimiter)
        head: list[list[str]] = []
        for number, row in enumerate(rows, start=1):
            head.append(row)
            if number >= h.header_row:
                break
        choice_row = head[h.header_row - 2] if h.header_row >= 2 else []
        labels = [contest_label(c) for c in h.contests]
        columns: list[list[tuple[int, str]]] = [[] for _ in h.contests]
        for offset, contest in enumerate(h.column_contest):
            index = h.vote_start + offset
            choice = unwrap(choice_row[index]) if index < len(choice_row) else ""
            columns[contest].append((index, choice or f"choice {len(columns[contest]) + 1}"))
        tallies = {labels[i][0]: ContestTally(labels[i][0], labels[i][1], {c: 0 for _, c in cols}) for i, cols in enumerate(columns)}
        count = 0
        for row in rows:
            if not any(cell.strip() for cell in row):
                continue
            count += 1
            for i, cols in enumerate(columns):
                tally = tallies[labels[i][0]]
                cells = [unwrap(row[index]).strip() if index < len(row) else "" for index, _ in cols]
                if not any(cells):
                    continue
                if all(not c or is_redaction_marker(c) for c in cells):
                    tally.redacted_rows += 1
                    continue
                total = 0
                for (_, choice), cell in zip(cols, cells, strict=True):
                    if cell.isdigit():
                        tally.votes[choice] += int(cell)
                        total += int(cell)
                if total > tally.vote_for:
                    tally.aggregate_rows += 1
    return CvrTally(tallies, count)


def cvr_margin(tally: ContestTally) -> int:
    """The contest's margin in the CVR's totals: the last winner's votes minus the first loser's."""
    return margin_of(tally.votes, tally.vote_for)
