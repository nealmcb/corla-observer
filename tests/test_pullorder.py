"""Fast tests for corla_observer.pullorder: CORLA's pull-list order and board split, reimplemented."""

from __future__ import annotations

import pytest

from corla_observer.pullorder import board_sizes, compare_ballots, natural_compare, possible_boards, pull_order


@pytest.mark.parametrize(
    ("a", "b", "sign"),
    [
        ("Bin 2", "Bin 10", -1),  # digit runs compare as numbers
        ("Bin 10", "Bin 2", 1),
        ("B", "A", -1),  # other runs compare in reverse, as CORLA's comparator does
        ("A", "B", 1),
        ("a", "A", 0),  # ignoring case
        ("Box 1", "Box 1a", -1),  # fewer runs first when the rest is equal
        ("", "Box", -1),
        ("007", "7", 0),
        ("1", "A", 1),  # a digit run against text compares as text, reversed: "A" first
        ("99999999999999999999", "100000000000000000000", 1),  # too large for a long: compared as text
    ],
)
def test_natural_compare(a: str, b: str, sign: int) -> None:
    result = natural_compare(a, b)
    assert (result > 0) - (result < 0) == sign


def test_compare_ballots_location_first() -> None:
    # Location, then scanner as a number, then batch naturally, then record as a number.
    ballots = [
        ("Box 10", "1", "1", "1"),
        ("Box 2", "9", "1", "1"),
        ("Box 2", "10", "1", "1"),
        ("Box 2", "9", "10", "1"),
        ("Box 2", "9", "2", "1"),
        ("Box 2", "9", "2", "10"),
        ("Box 2", "9", "2", "9"),
    ]
    ordered = pull_order(ballots, lambda b: b)
    assert ordered == [
        ("Box 2", "9", "1", "1"),
        ("Box 2", "9", "2", "1"),
        ("Box 2", "9", "2", "9"),
        ("Box 2", "9", "2", "10"),
        ("Box 2", "9", "10", "1"),
        ("Box 2", "10", "1", "1"),
        ("Box 10", "1", "1", "1"),
    ]
    assert compare_ballots(("", "1", "1", "1"), ("", "1", "1", "1")) == 0


def test_board_split() -> None:
    assert board_sizes(10, 3) == (3, 4, 1)
    assert possible_boards(6, 1) == [(1,)] * 6
    assert possible_boards(6, 3) == [(1,), (1,), (2,), (2,), (3,), (3,)]  # an even split is certain
    # 10 ballots on 3 boards: sizes 3, 3 and 4 in some order.
    assert possible_boards(10, 3) == [(1,), (1,), (1,), (1, 2), (2,), (2,), (2, 3), (3,), (3,), (3,)]
    # 11 on 3: sizes 3, 4, 4.
    assert possible_boards(11, 3) == [(1,), (1,), (1,), (1, 2), (2,), (2,), (2,), (2, 3), (3,), (3,), (3,)]
    # Every possible shuffle of sizes agrees with the boards listed.
    import itertools

    for n, b in ((10, 3), (11, 4), (7, 4), (23, 5)):
        q, big, r = board_sizes(n, b)
        sizes = [big] * r + [q] * (b - r)
        possible = possible_boards(n, b)
        for perm in set(itertools.permutations(sizes)):
            position = 0
            for board, size in enumerate(perm, start=1):
                for _ in range(size):
                    assert board in possible[position]
                    position += 1
