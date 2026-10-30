"""CORLA's pull-list order and its split of a county's ballots among audit boards, reimplemented.

**Order.** CORLA (server ``BallotSequencer.sortAndDeduplicateCVRs``, with
``CVRToAuditResponse.compareTo``) sorts a round's ballots, each once, by:

1. the storage location of the ballot's batch in the county's manifest,
   compared by CORLA's ``NaturalOrderComparator`` (`natural_compare`);
2. the scanner (tabulator) ID, as a number;
3. the batch ID, compared naturally;
4. the record ID, as a number.

`natural_compare` follows CORLA's comparator exactly, including its
quirks: a string is split into runs of ASCII digits and runs of anything
else; two digit runs compare as 64-bit numbers (falling back to a
case-insensitive text comparison when one does not fit); any other pair of
runs compares case-insensitively *in reverse*, so "B" sorts before "A"
(CORLA's code inverts the comparison while its comment says it intends
the opposite); when every run compared is equal, the string with fewer
runs comes first. This was read from CORLA's source
(``util/NaturalOrderComparator.java``) and written anew here.

The 2017 and 2018 published ballot lists, from older code, were in
tabulator, batch and record order without the location. Each round's list
holds only that round's ballots, sorted the same way.

**Boards.** CORLA (``util/BallotAssignment.assignToBoards``) gives each of
*b* boards ``n // b`` of the *n* ballots, adds the remainder one each to
the first boards, then shuffles the list of sizes at random, and each board
takes a contiguous run of the sorted list in board order. So without the
county's pull list, the sizes are known only as ``n // b`` or one more,
and a board's first and last ballots are uncertain by up to ``b - 1``
positions. `possible_boards` gives, for each position, every board it can
fall to.

Standard library only.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

_CHUNK_RE = re.compile(r"[0-9]+|[^0-9]+")
_LONG_MAX = 2**63 - 1


def _case_insensitive(a: str, b: str) -> int:
    """Java's ``String.CASE_INSENSITIVE_ORDER``: per character, upper case then lower case, then length."""
    for x, y in zip(a, b, strict=False):
        if x != y:
            x, y = x.upper(), y.upper()
            if x != y:
                x, y = x.lower(), y.lower()
                if x != y:
                    return ord(x[0]) - ord(y[0])
    return len(a) - len(b)


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def natural_compare(a: str, b: str) -> int:
    """CORLA's ``NaturalOrderComparator.compare``: negative, zero or positive, as in Java; see the module documentation."""
    xs, ys = _CHUNK_RE.findall(a or ""), _CHUNK_RE.findall(b or "")
    result = 0
    for x, y in zip(xs, ys, strict=False):
        if x.isascii() and x.isdigit() and y.isascii() and y.isdigit():
            nx, ny = int(x), int(y)
            if nx > _LONG_MAX or ny > _LONG_MAX:
                result = _case_insensitive(x, y)
            else:
                result = _sign(nx - ny)
        else:
            result = -_sign(_case_insensitive(x, y))
        if result != 0:
            return result
    if len(xs) < len(ys):
        return -1
    if len(xs) > len(ys):
        return 1
    return 0


def _number(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


def compare_ballots(a: tuple[str, str, str, str], b: tuple[str, str, str, str]) -> int:
    """CORLA's ``CVRToAuditResponse.compareTo`` on ``(location, scanner, batch, record)``."""
    by_location = natural_compare(a[0], b[0])
    if by_location:
        return by_location
    by_scanner = _number(a[1]) - _number(b[1])
    if by_scanner:
        return by_scanner
    by_batch = natural_compare(a[2], b[2])
    if by_batch:
        return by_batch
    return _number(a[3]) - _number(b[3])


def pull_order(items: Sequence[T], key: Callable[[T], tuple[str, str, str, str]]) -> list[T]:
    """``items`` in CORLA's pull-list order; ``key`` gives each its ``(location, scanner, batch, record)``."""
    def compare(x: T, y: T) -> int:
        return compare_ballots(key(x), key(y))

    return sorted(items, key=functools.cmp_to_key(compare))


def board_sizes(n: int, boards: int) -> tuple[int, int, int]:
    """``(smaller size, larger size, how many boards have the larger)``: ``n // boards``, one more, ``n % boards``."""
    if boards <= 0:
        raise ValueError("at least one board")
    return n // boards, n // boards + 1, n % boards


def possible_boards(n: int, boards: int) -> list[tuple[int, ...]]:
    """For each of ``n`` sorted ballots (0-based), the boards (1-based) it can fall to, whatever CORLA's shuffle gave.

    Board *i* (0-based) starts after *i* boards, of which between
    ``max(0, i - (b - r))`` and ``min(i, r)`` have the larger size.
    """
    q, _, r = board_sizes(n, boards)
    first = [i * q + max(0, i - (boards - r)) for i in range(boards)] + [n]
    last = [i * q + min(i, r) for i in range(boards)] + [n]
    return [tuple(i + 1 for i in range(boards) if first[i] <= p < last[i + 1]) for p in range(n)]
