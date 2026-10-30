"""Fast tests for corla_observer.sampling, on tiny synthetic fixtures built in code.

They need no network and no real election data. The acceptance tests on
the real 2026 primary data are in tests/acceptance/ and run with
``uv run pytest -m slow``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corla_observer.sampling import (
    BallotRef,
    Domain,
    ManifestAdjustment,
    ManifestBatch,
    apply_manifest_adjustment,
    build_domain,
    build_domain_from_manifests,
    contest_counties,
    county_id_order,
    county_key,
    find_duplicate_manifests,
    load_county_id_order,
    load_county_manifest,
    select_ballots,
    sha256_random_numbers,
    total_manifest_ballot_count,
)

# ---------------------------------------------------------------------------
# The PRNG


def test_sha256_random_numbers_known_answers() -> None:
    """Known answers for CORLA's PRNG, computed independently of this module.

    CORLA's ``PseudoRandomNumberGenerator.generateNext`` does, for draw
    counter i = 1, 2, ...:

        hash_input = seed + "," + i                      (a Java String)
        digest     = SHA-256(hash_input as UTF-8 bytes)
        value      = new BigInteger(1, digest)           (unsigned, big-endian)
        pick       = minimum + value mod (maximum - minimum + 1)

    and ``BallotSelection.randomSelection`` constructs it with minimum 1 and
    maximum = the domain size, so pick = value mod N + 1.

    Worked example for draw 1 of the 2026 primary seed:
    SHA-256("49006417086137856424,1") is
    451c6001e2f811a4a1acb666bc7b71058390940d6096ed5f58f6abed8c4e6b23 (hex);
    read as one integer, mod 1,444,047 (the statewide domain) it is
    533,790, so the pick is 533,791; mod 16,146 (La Plata) it is 7,209, so
    the pick is 7,210.

    Every list below was produced by running CORLA's own
    PseudoRandomNumberGenerator.java (unmodified apart from its package
    line) under Java 17, calling ``getRandomNumbers(0, count - 1)`` on a
    generator built with ``(seed, true, 1, N)``.
    """
    assert sha256_random_numbers("49006417086137856424", 10, 1_444_047) == [
        533791, 196795, 980741, 962787, 897500, 1341014, 1398359, 59627, 285881, 745664,
    ]
    assert sha256_random_numbers("49006417086137856424", 5, 16_146) == [
        7210, 3739, 2381, 288, 12308,
    ]
    assert sha256_random_numbers("53417960661093690826", 5, 2221) == [125, 2053, 369, 810, 299]
    assert sha256_random_numbers("12345678901234567890", 5, 100) == [26, 21, 29, 56, 38]


def test_sha256_random_numbers_rejects_nonpositive_domain() -> None:
    with pytest.raises(ValueError):
        sha256_random_numbers("seed", 1, 0)


def test_sha256_random_numbers_rejects_negative_count() -> None:
    with pytest.raises(ValueError):
        sha256_random_numbers("seed", -1, 10)


def test_sha256_random_numbers_prefix_property() -> None:
    """Draw i doesn't depend on how many draws are requested (consistent sampling)."""
    long = sha256_random_numbers("12345678901234567890", 50, 1000)
    assert sha256_random_numbers("12345678901234567890", 20, 1000) == long[:20]


# ---------------------------------------------------------------------------
# Manifest loading


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_county_manifest_forward_fills_blank_continuation_rows(tmp_path: Path) -> None:
    """A loader that trusted a literal per-row County value would drop every row after the first."""
    manifest = write(
        tmp_path / "Test.csv",
        "County,Tabulator,Batch,# of Ballots,Location\r\n"
        "Test,102,1,25,Bin 1\r\n"
        ",102,2,25,\r\n"
        ",102,3,10,\r\n",
    )
    batches = load_county_manifest(manifest)
    assert len(batches) == 3, "continuation rows must not be silently dropped"
    assert [b.county for b in batches] == ["Test", "Test", "Test"]
    assert sum(b.ballot_count for b in batches) == 60
    assert batches[0].location == "Bin 1"
    assert batches[1].location == ""


@pytest.mark.parametrize(
    "header",
    [
        "County,Tabulator ID,Batch ID,# of Ballot Cards,Location",
        "County,Tabulator,Batch #,# of Ballots Cards,Location",
        "County,Device,Batch,#of Ballots,Location",  # bare "Device", "#of" without space
        "County,Tabulator ID,Batch,# of Ballot Card,Location",  # singular
        "County,Tabulator ID,TrackingId,BatchCount,BallotStorageBox",
        "County,Device,Batch,Cards,Box,Machine,BoxSeq",  # no Location column
    ],
)
def test_load_county_manifest_header_variants(tmp_path: Path, header: str) -> None:
    ncols = header.count(",") + 1
    extra = "," * (ncols - 4)
    manifest = write(tmp_path / "X.csv", f"{header}\nX,7,1,12{extra}\n,7,2,8{extra}\n")
    batches = load_county_manifest(manifest)
    assert [(b.county, b.tabulator_id, b.batch_number, b.ballot_count) for b in batches] == [
        ("X", "7", "1", 12),
        ("X", "7", "2", 8),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "Conejos,Tabulator ID,Batch,# of Ballot Cards,Location",  # county name as header
        ",Device ID,Batch,# of Ballot Cards,Location",  # empty header name
    ],
)
def test_load_county_manifest_county_column_without_county_header(
    tmp_path: Path, header: str
) -> None:
    manifest = write(tmp_path / "C.csv", f"{header}\nConejos,1,1,5,A\n,1,2,6,A\n")
    batches = load_county_manifest(manifest)
    assert [b.county for b in batches] == ["Conejos", "Conejos"]
    assert sum(b.ballot_count for b in batches) == 11


def test_load_county_manifest_handles_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "Bom.csv"
    header = "County,Tabulator ID,Batch ID,# of Ballot Cards,Location"
    path.write_bytes(b"\xef\xbb\xbf" + f"{header}\nB,1,1,3,x\n".encode())
    assert load_county_manifest(path)[0].county == "B"


def test_load_county_manifest_county_aliases(tmp_path: Path) -> None:
    manifest = write(
        tmp_path / "M.csv",
        "County,Tabulator,Batch,# of Ballots,Location\nOtero,1,1,5,\nOtreo,1,2,5,\n",
    )
    assert {b.county for b in load_county_manifest(manifest)} == {"Otero", "Otreo"}
    aliased = load_county_manifest(manifest, county_aliases={"Otreo": "Otero"})
    assert {b.county for b in aliased} == {"Otero"}


def test_load_county_manifest_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not resolve"):
        load_county_manifest(write(tmp_path / "a.csv", "County,Foo,Bar\nX,1,2\n"))
    with pytest.raises(ValueError, match="non-integer"):
        load_county_manifest(
            write(tmp_path / "b.csv", "County,Tabulator,Batch,# of Ballots\nX,1,1,ten\n")
        )
    with pytest.raises(ValueError, match="forward-fill"):
        load_county_manifest(
            write(tmp_path / "c.csv", "County,Tabulator,Batch,# of Ballots\n,1,1,10\n")
        )


# ---------------------------------------------------------------------------
# Domains and selection, on small synthetic manifests


def batch(county: str, tab: str, num: str, count: int) -> ManifestBatch:
    return ManifestBatch(county, tab, num, count, "")


def test_domain_ballot_at_maps_positions_across_batches() -> None:
    domain = build_domain_from_manifests([[batch("A", "1", "1", 3), batch("A", "1", "2", 2)]])
    assert domain.size == 5
    assert domain.ballot_at(1) == BallotRef("A", "1", "1", 1)
    assert domain.ballot_at(3).imprinted_id() == "1-1-3"
    assert domain.ballot_at(4).imprinted_id() == "1-2-1"
    assert domain.ballot_at(5).imprinted_id() == "1-2-2"
    for bad in (0, 6):
        with pytest.raises(IndexError):
            domain.ballot_at(bad)


def test_empty_domain_has_size_zero() -> None:
    assert Domain().size == 0


def test_multi_county_domain_uses_county_id_order_not_input_order() -> None:
    order = county_id_order(
        [
            {"county_id": "64", "county_name": "Broomfield"},
            {"county_id": "7", "county_name": "Boulder"},
            {"county_id": "10", "county_name": "Clear Creek"},
            {"county_id": "7", "county_name": "Boulder"},
        ]
    )
    assert order == {"broomfield": 64, "boulder": 7, "clearcreek": 10}
    domain = build_domain_from_manifests(
        [
            [batch("Broomfield", "9", "1", 4)],
            [batch("ClearCreek", "5", "1", 2)],
            [batch("Boulder", "3", "1", 3)],
        ],
        county_order=order,
    )
    assert domain.county_ranges == {
        "Boulder": (1, 3),
        "ClearCreek": (4, 5),
        "Broomfield": (6, 9),
    }
    assert domain.ballot_at(4) == BallotRef("ClearCreek", "5", "1", 1)


def test_county_id_order_rejects_conflicting_ids() -> None:
    with pytest.raises(ValueError):
        county_id_order(
            [{"county_id": "1", "county_name": "Adams"}, {"county_id": "2", "county_name": "Adams"}]
        )


def test_build_domain_from_manifests_rejects_unresolvable_county_order() -> None:
    """A county missing from the order would otherwise land somewhere arbitrary."""
    with pytest.raises(ValueError, match="no county_order entry"):
        build_domain_from_manifests(
            [[batch("Adams", "1", "1", 1)], [batch("Mesa", "1", "1", 1)]],
            county_order={"adams": 1},
        )


def test_build_domain_from_manifests_rejects_bad_manifests() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_domain_from_manifests([[]])
    with pytest.raises(ValueError, match="other counties"):
        build_domain_from_manifests([[batch("A", "1", "1", 1), batch("B", "1", "2", 1)]])
    with pytest.raises(ValueError, match="more than one manifest"):
        build_domain_from_manifests([[batch("A", "1", "1", 1)], [batch("a", "2", "1", 1)]])


def test_manifest_adjustment_remove_insert_append() -> None:
    rows = [batch("W", "1", n, 10) for n in ("1", "2", "3", "4")]
    adjustment = ManifestAdjustment(
        county="W",
        reason="example",
        remove=(("1", "2"),),
        insert_after=((("1", "4"), batch("W", "1", "5", 7)),),
        append=(batch("W", "2", "1", 5),),
    )
    adjusted = apply_manifest_adjustment(rows, adjustment)
    assert [b.key for b in adjusted] == [("1", "1"), ("1", "3"), ("1", "4"), ("1", "5"), ("2", "1")]
    assert [b.batch_number for b in rows] == ["1", "2", "3", "4"], "input left unchanged"


def test_manifest_adjustment_rejects_keys_not_in_manifest() -> None:
    rows = [batch("W", "1", "1", 10)]
    new = batch("W", "1", "2", 1)
    with pytest.raises(ValueError, match="to remove"):
        apply_manifest_adjustment(rows, ManifestAdjustment("W", "r", remove=(("1", "9"),)))
    with pytest.raises(ValueError, match="anchor"):
        apply_manifest_adjustment(
            rows, ManifestAdjustment("W", "r", insert_after=((("1", "9"), new),))
        )
    with pytest.raises(ValueError, match="also removed"):
        apply_manifest_adjustment(
            rows,
            ManifestAdjustment("W", "r", remove=(("1", "1"),), insert_after=((("1", "1"), new),)),
        )


def test_adjustments_apply_only_to_their_county_and_are_recorded() -> None:
    gap = ManifestAdjustment("B", "missing batch", append=(batch("B", "2", "9", 4),))
    order = {"a": 1, "b": 2}
    with_b = build_domain_from_manifests(
        [[batch("A", "1", "1", 3)], [batch("B", "2", "1", 2)]], order, [gap]
    )
    assert with_b.size == 9
    assert with_b.county_ranges["B"] == (4, 9)
    assert with_b.applied_adjustments == [gap]
    assert with_b.epistemic_flags == ["manifest_adjustment_applied"]
    without_b = build_domain_from_manifests([[batch("A", "1", "1", 3)]], order, [gap])
    assert without_b.size == 3
    assert without_b.applied_adjustments == []
    assert without_b.epistemic_flags == []


def test_select_ballots_keeps_duplicates_in_draw_order() -> None:
    domain = build_domain_from_manifests([[batch("T", "1", "1", 3)]])
    draws = select_ballots("12345678901234567890", domain, 12)
    assert [d.index for d in draws] == list(range(1, 13))
    assert [d.position for d in draws] == sha256_random_numbers("12345678901234567890", 12, 3)
    assert all(d.imprinted_id == f"1-1-{d.position}" and d.county == "T" for d in draws)
    assert len({d.imprinted_id for d in draws}) < len(draws), "a 3-card domain must repeat"


MANIFEST_HEADER = "County,Tabulator,Batch,# of Ballots,Location\n"


def test_build_domain_from_files_filters_by_county_column_and_orders(tmp_path: Path) -> None:
    write(tmp_path / "Zeta.csv", MANIFEST_HEADER + "Adams,1,1,5,\n,1,2,5,\n")
    write(tmp_path / "Alpha.csv", MANIFEST_HEADER + "Weld,2,1,7,\n")
    write(tmp_path / "Mesa.csv", MANIFEST_HEADER + "MESA,3,1,4,\n")
    write(tmp_path / "uploadStatus.csv", "name,filename\nx,y\n")
    order = {"adams": 1, "mesa": 39, "weld": 62}

    single = build_domain(tmp_path, ["Weld"], county_order=order)
    assert single.size == 7 and list(single.county_ranges) == ["Weld"]

    multi = build_domain(tmp_path, ["Weld", "Mesa"], county_order=order)
    assert list(multi.county_ranges) == ["MESA", "Weld"]

    statewide = build_domain(tmp_path, county_order=order)
    assert list(statewide.county_ranges) == ["Adams", "MESA", "Weld"]
    assert statewide.size == total_manifest_ballot_count(tmp_path) == 21

    with pytest.raises(ValueError, match="no manifest found"):
        build_domain(tmp_path, ["Weld", "Yuma"], county_order=order)
    with pytest.raises(ValueError, match="ordered counties"):
        build_domain(tmp_path, county_order={**order, "yuma": 63})


def test_build_domain_drops_byte_identical_duplicate_files(tmp_path: Path) -> None:
    """One county's manifest served under another county's filename is read once."""
    same = MANIFEST_HEADER + "Saguache,102,1,25,\n"
    write(tmp_path / "LaPlata.csv", same)
    write(tmp_path / "Saguache.csv", same)
    write(tmp_path / "Weld.csv", MANIFEST_HEADER + "Weld,1,1,3,\n")
    assert list(find_duplicate_manifests(tmp_path).values()) == [
        [tmp_path / "LaPlata.csv", tmp_path / "Saguache.csv"]
    ]
    domain = build_domain(tmp_path)
    assert domain.size == 28
    assert domain.epistemic_flags == ["duplicate_manifest_file_dropped"]
    with pytest.raises(ValueError, match="La Plata"):
        build_domain(tmp_path, ["La Plata"])


def test_contest_counties(tmp_path: Path) -> None:
    path = write(
        tmp_path / "ContestIDsByCounty.csv",
        "county_id,county_name,contest_name,contest_id\n"
        "2,Alamosa,C1,1\n1,Adams,C1,2\n2,Alamosa,C2,3\n1,Adams,C1,4\n",
    )
    assert contest_counties(path, "C1") == ["Adams", "Alamosa"]
    assert load_county_id_order(path) == {"adams": 1, "alamosa": 2}



# ---------------------------------------------------------------------------
# Patterns seen in real elections, in miniature


def test_build_domain_refuses_county_typo_unless_aliased(tmp_path: Path) -> None:
    """One row misspelling the county (as Prowers.csv once did) is refused, not guessed."""
    rows = "Prowers,102,1,5,\nProwes,102,2,4,\n,102,3,3,\n"
    write(tmp_path / "Prowers.csv", MANIFEST_HEADER + rows)
    with pytest.raises(ValueError, match="Prowes"):
        build_domain(tmp_path, ["Prowers"])
    domain = build_domain(tmp_path, ["Prowers"], county_aliases={"Prowes": "Prowers"})
    assert domain.size == 12
    assert {b.county for b in domain.batches} == {"Prowers"}


def test_build_domain_keeps_blank_continuation_rows(tmp_path: Path) -> None:
    write(tmp_path / "Jackson.csv", MANIFEST_HEADER + "Jackson,1,1,4,\n,1,2,4,\n,1,3,2,\n")
    domain = build_domain(tmp_path, ["Jackson"])
    assert domain.size == 10
    assert domain.ballot_at(10).imprinted_id() == "1-3-2"


def test_statewide_domain_from_files_matches_report_spellings(tmp_path: Path) -> None:
    """Statewide, county-wide and multi-county domains from files, in county-ID order.

    The report and the manifests spell Clear Creek differently. Manifests
    are read by their County column and ordered by the IDs in a
    ContestIDsByCounty-shaped file, so Broomfield (ID 64) comes last
    although it sorts early by name.
    """
    files = tmp_path / "files"
    files.mkdir()
    write(files / "Broomfield.csv", MANIFEST_HEADER + "Broomfield,9,1,4,\n")
    write(files / "ClearCreek.csv", MANIFEST_HEADER + "ClearCreek,5,1,2,\n")
    write(files / "Adams.csv", MANIFEST_HEADER + "Adams,1,1,3,\n,1,2,1,\n")
    contest_ids = write(
        tmp_path / "ContestIDsByCounty.csv",
        "county_id,county_name,contest_name,contest_id\n"
        "64,Broomfield,Senator,1\n1,Adams,Senator,2\n10,Clear Creek,Senator,3\n"
        "10,Clear Creek,Clear Creek Assessor,4\n1,Adams,Adams Assessor,5\n"
        "1,Adams,District 7,6\n64,Broomfield,District 7,7\n",
    )
    order = load_county_id_order(contest_ids)
    assert order == {"adams": 1, "clearcreek": 10, "broomfield": 64}

    statewide = build_domain(files, county_order=order)
    assert statewide.county_ranges == {"Adams": (1, 4), "ClearCreek": (5, 6), "Broomfield": (7, 10)}
    assert statewide.ballot_at(5).imprinted_id() == "5-1-1"

    county_wide = build_domain(files, contest_counties(contest_ids, "Clear Creek Assessor"))
    assert county_wide.county_ranges == {"ClearCreek": (1, 2)}

    district = build_domain(files, contest_counties(contest_ids, "District 7"), county_order=order)
    assert district.county_ranges == {"Adams": (1, 4), "Broomfield": (5, 8)}


def test_missing_batch_adjustment_restores_corla_positions() -> None:
    """The "missing batch" pattern: a published manifest lacks a batch CORLA had.

    Here county A's published manifest ends at batch 2, but CORLA's own
    figures (its ballot-card count, and its manifest-to-CVR comparison
    listing batch 3) show a third batch of 2 cards. Without it the domain
    is short, so every position after A's end is off and the draws differ;
    the explicit adjustment restores the domain CORLA used, and the domain
    records it.
    """
    published = [[batch("A", "1", "1", 3), batch("A", "1", "2", 3)], [batch("B", "2", "1", 6)]]
    order = {"a": 1, "b": 2}
    corla_ballot_card_count = 14
    missing = ManifestAdjustment(
        county="A",
        reason="batch 1-3 is in CORLA's comparison and ballot-card count",
        append=(batch("A", "1", "3", 2),),
    )

    short = build_domain_from_manifests(published, order)
    assert short.size == corla_ballot_card_count - 2
    fixed = build_domain_from_manifests(published, order, [missing])
    assert fixed.size == corla_ballot_card_count
    assert fixed.ballot_at(8) == BallotRef("A", "1", "3", 2)
    assert fixed.ballot_at(9) == BallotRef("B", "2", "1", 1)
    assert short.ballot_at(9) == BallotRef("B", "2", "1", 3)
    assert fixed.applied_adjustments == [missing]
    assert fixed.epistemic_flags == ["manifest_adjustment_applied"]

    seed = "12345678901234567890"
    assert [d.imprinted_id for d in select_ballots(seed, short, 8)] != [
        d.imprinted_id for d in select_ballots(seed, fixed, 8)
    ]


def test_inserted_batch_position_shifts_later_cards() -> None:
    """Where an adjustment puts a batch matters, even when the total is the same."""
    rows = [batch("A", "1", "1", 2), batch("A", "1", "2", 2)]
    new = batch("A", "1", "9", 1)
    after_first = build_domain_from_manifests(
        [rows], adjustments=[ManifestAdjustment("A", "r", insert_after=((("1", "1"), new),))]
    )
    at_end = build_domain_from_manifests(
        [rows], adjustments=[ManifestAdjustment("A", "r", append=(new,))]
    )
    assert after_first.size == at_end.size == 5
    assert after_first.ballot_at(3).imprinted_id() == "1-9-1"
    assert at_end.ballot_at(3).imprinted_id() == "1-2-1"


def test_select_ballots_across_counties_with_repeats() -> None:
    """Each draw resolves to the right county's card; a repeated position is a repeated draw."""
    domain = build_domain_from_manifests(
        [[batch("A", "1", "1", 2)], [batch("B", "2", "1", 3)]], {"a": 1, "b": 2}
    )
    seed = "53417960661093690826"
    draws = select_ballots(seed, domain, 10)
    positions = sha256_random_numbers(seed, 10, 5)
    assert [d.position for d in draws] == positions
    for d in draws:
        expected = ("A", f"1-1-{d.position}") if d.position <= 2 else ("B", f"2-1-{d.position - 2}")
        assert (d.county, d.imprinted_id) == expected
    assert len(set(positions)) < len(positions)
    assert select_ballots(seed, domain, 4) == draws[:4]


def test_county_key_matches_spelling_variants() -> None:
    assert {county_key(s) for s in ["ClearCreek", "Clear Creek", "CLEAR CREEK"]} == {"clearcreek"}
    assert county_key("MESA") == county_key("Mesa")
    assert county_key("LaPlata") == county_key("La Plata")
