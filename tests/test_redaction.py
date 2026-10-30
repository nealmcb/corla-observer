"""Fast tests for corla_observer.cvr and corla_observer.redaction, on tiny synthetic CVRs.

The acceptance tests on real public CVRs, and the cross-check with the
public anonymizer, are in tests/acceptance/.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.cvr import (
    CvrFormatError,
    CvrReader,
    detect_encoding,
    is_redaction_marker,
    names_aggregate,
    unwrap,
)
from corla_observer.redaction import (
    FAIL,
    PASS,
    CheckLimits,
    RedactionReport,
    SharedPattern,
    analyze_redaction,
    check_redaction,
    format_report,
    style_hash,
)
from synthetic import CONTESTS, Ballot, ballots, make_cvr

MAYOR, COUNCIL, MEASURE, FIRE = CONTESTS


def write(tmp_path: Path, data: bytes, name: str = "cvr.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def analyze(tmp_path: Path, rows: list[Ballot], **options: object) -> RedactionReport:
    return analyze_redaction(write(tmp_path, make_cvr(rows, **options)))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Reading CVRs


def test_unwrap_and_markers() -> None:
    assert unwrap(' ="101-1-7" ') == "101-1-7"
    assert unwrap('=""') == ""
    assert unwrap(" 1 ") == "1"
    assert [is_redaction_marker(v) for v in ("*", "X", "x", "Redacted", "REDACTED BALLOT", "0", "1", "")] == [
        True, True, True, True, True, False, False, False,
    ]  # fmt: skip


def test_reader_header_and_rows(tmp_path: Path) -> None:
    rows = [
        Ballot((MAYOR, COUNCIL), "101", "BT-1"),
        Ballot((MAYOR,), "", "BT-2"),
        Ballot((MAYOR,), blank=True),
        Ballot((MAYOR, FIRE), marker="*"),
        Ballot((MAYOR,), cvr_number="AGGREGATED"),
    ]
    reader = CvrReader(write(tmp_path, make_cvr(rows)))
    header = reader.header
    assert (header.header_row, header.election, header.version) == (
        4,
        "2026 Synthetic County General Election",
        "5.17.17.1",
    )
    assert header.contests == tuple(CONTESTS)
    assert header.vote_start == 8
    assert (header.precinct_column, header.ballot_type_column) == ("PrecinctPortion", "BallotType")
    read = list(reader.rows())
    assert [r.status for r in read] == ["ballot", "ballot", "redacted", "ballot", "aggregate"]
    assert [r.marked for r in read] == [False, False, False, True, False]
    assert read[0].contests == frozenset({MAYOR, COUNCIL})
    assert (read[0].imprinted_id, read[0].precinct, read[0].ballot_type) == ("101-1-1", "101", "BT-1")
    assert read[1].precinct is None
    assert (read[2].has_votes, read[3].has_votes) == (False, True)
    assert read[3].contests == frozenset({MAYOR, FIRE})
    assert [r.record for r in read] == [5, 6, 7, 8, 9]


@pytest.mark.parametrize(
    "options",
    [
        {"excel": True},
        {"quote_all": True},
        {"excel": True, "quote_all": True},
        {"bom": True},
        {"line_ending": "\n"},
        {"line_ending": "\r"},
        {"counting_group": False, "precinct_column": False},
    ],
    ids=["excel", "quote-all", "excel-quote-all", "bom", "lf", "cr", "no-optional-columns"],
)
def test_county_quirks_read_alike(tmp_path: Path, options: dict[str, object]) -> None:
    rows = ballots(3, "Mayor", "Council") + ballots(2, "Mayor", precinct="")
    plain = list(CvrReader(write(tmp_path, make_cvr(rows), "plain.csv")).rows())
    quirky = list(CvrReader(write(tmp_path, make_cvr(rows, **options), "quirky.csv")).rows())  # type: ignore[arg-type]
    if options.get("precinct_column") is False:
        plain = [r.__class__(**{**r.__dict__, "precinct": None}) for r in plain]
    assert [(r.status, r.cvr_number, r.imprinted_id, r.precinct, r.contests) for r in quirky] == [
        (r.status, r.cvr_number, r.imprinted_id, r.precinct, r.contests) for r in plain
    ]


def test_first_cells_that_name_aggregates() -> None:
    aggregates = ("AGGREGATED", "Total", "Redacted and Consolidated 18 Ballots", "REDACTED 3", "Redacted (combined)")
    singles = ("", "17", "0017", "Redacted", "REDACTED BALLOT", "*", "X", "x")
    assert [names_aggregate(v) for v in aggregates] == [True] * len(aggregates)
    assert [names_aggregate(v) for v in singles] == [False] * len(singles)


def test_ballot_type_column_with_a_similar_name(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor"), precinct_column=False)
    lines = data.decode().split("\r\n")
    lines[3] = lines[3].replace("BallotType", "Ballot Style")
    reader = CvrReader(write(tmp_path, "\r\n".join(lines).encode()))
    # The vote columns then start at the contest row's first contest.
    assert (reader.header.ballot_type_column, reader.header.vote_start) == ("Ballot Style", 7)
    assert {row.ballot_type for row in reader.rows()} == {"BT-1"}


def test_latin1_and_utf16(tmp_path: Path) -> None:
    title = "Elección General 2026"
    rows = ballots(10, "Mayor")
    latin1 = write(tmp_path, make_cvr(rows, encoding="latin-1", title=title), "latin1.csv")
    assert detect_encoding(latin1) == "latin-1"
    assert CvrReader(latin1).header.election == title
    # A UTF-8 byte order mark on a file that is otherwise Latin-1.
    mixed = write(tmp_path, b"\xef\xbb\xbf" + make_cvr(rows, encoding="latin-1", title=title), "mixed.csv")
    assert CvrReader(mixed).header.election == title
    utf16 = write(tmp_path, make_cvr(rows, encoding="utf-16", title=title), "utf16.csv")
    assert detect_encoding(utf16) == "utf-16"
    assert analyze_redaction(utf16).verdict == PASS
    assert analyze_redaction(latin1).format["encoding"] == "latin-1"


def data_row(*meta: str, votes: tuple[str, ...] = ("1", "0")) -> bytes:
    """One data row with the given leading cells and votes, padded to the synthetic CVR's width."""
    width = 8 + sum(len(choices) for choices in CONTESTS.values())
    cells = list(meta) + [""] * (8 - len(meta)) + list(votes)
    return (",".join(cells + [""] * (width - len(cells))) + "\r\n").encode("latin-1")


def with_lines(data: bytes, edit: dict[int, str]) -> bytes:
    """The CVR with some lines (0-based) replaced, each given its new text."""
    lines = data.decode().split("\r\n")
    for index, text in edit.items():
        lines[index] = text
    return "\r\n".join(lines).encode()


def test_latin1_far_down_the_file(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor")) + data_row("11", "101", "1", "11", "101-1-11", "Mail", "Cañon", "BT-1")
    path = write(tmp_path, data)
    assert detect_encoding(path) == "latin-1"
    assert analyze_redaction(path).ballot_rows == 11


def test_header_without_title_row_and_tab_delimited(tmp_path: Path) -> None:
    lines = make_cvr(ballots(10, "Mayor")).decode().split("\r\n")
    path = write(tmp_path, "\r\n".join(lines[1:]).encode())
    assert CvrReader(path).header.header_row == 3
    tabbed = write(tmp_path, make_cvr(ballots(10, "Mayor")).replace(b",", b"\t"), "tabbed.tsv")
    assert CvrReader(tabbed).header.delimiter == "\t"
    assert analyze_redaction(tabbed).verdict == PASS


def test_not_a_cvr(tmp_path: Path) -> None:
    path = write(tmp_path, b"County,Tabulator,Batch,# of Ballots\r\nAdams,1,1,5\r\n")
    with pytest.raises(CvrFormatError):
        CvrReader(path)
    report = analyze_redaction(path)
    assert report.verdict == FAIL
    assert "not a readable CVR" in report.problems[0]


def test_ballot_type_and_contest_row_must_agree(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor"))
    contest_row = data.decode().split("\r\n")[1].split(",")
    contest_row[7] = "Stray contest"  # under BallotType
    report = analyze_redaction(write(tmp_path, with_lines(data, {1: ",".join(contest_row)})))
    assert report.verdict == FAIL
    assert "BallotType" in report.problems[0]


# ---------------------------------------------------------------------------
# The rule


def test_pass(tmp_path: Path) -> None:
    report = analyze(tmp_path, ballots(10, "Mayor", "Council") + ballots(12, "Mayor", "Measure"))
    assert report.verdict == PASS and report.passed
    assert (report.rows_read, report.ballot_rows, report.styles, report.groups) == (22, 22, 2, 2)
    assert report.grouped_by == "style+precinct+ballot_type"
    assert report.precinct_present and report.ballot_type_present
    assert report.rare_groups == () and report.problems == ()


def test_fail_by_rare_style(tmp_path: Path) -> None:
    rows = ballots(10, "Mayor", "Council") + ballots(3, "Mayor", "Fire")
    report = analyze(tmp_path, rows, precinct_column=False)
    assert report.verdict == FAIL
    assert report.grouped_by == "style+ballot_type" and report.precinct_column is None
    assert (report.rare_group_count, report.rare_ballots, report.rare_group_sizes) == (1, 3, ((3, 1),))
    group = report.rare_groups[0]
    assert (group.size, group.precinct, group.ballot_type, group.contests) == (3, None, "BT-1", 2)
    assert group.style_hash == style_hash([FIRE, MAYOR])
    assert group.contest_names is None


def test_ballot_types_sharing_a_contest_pattern(tmp_path: Path) -> None:
    rows = (
        ballots(10, "Mayor", "Council", ballot_type="03")
        + ballots(4, "Mayor", "Council", precinct="102", ballot_type="03")
        + ballots(2, "Mayor", "Council", ballot_type="11")
        + ballots(12, "Mayor", "Council", ballot_type="11 & PO-CC")
        + ballots(11, "Mayor", ballot_type="14")
        + ballots(10, "Mayor", ballot_type="")
        + ballots(15, "Mayor", "Fire", ballot_type="03")
    )
    report = analyze(tmp_path, rows)
    assert report.shared_pattern_count == 2
    assert report.shared_patterns == (
        SharedPattern(style_hash([COUNCIL, MAYOR]), 2, 28, 3, (("03", 14), ("11", 2), ("11 & PO-CC", 12))),
        SharedPattern(style_hash([MAYOR]), 1, 21, 2, (("14", 11), (None, 10))),
    )
    text = format_report(report)
    assert "2 contest pattern(s) shared by several ballot types" in text
    assert "[03, 11, 11 & PO-CC] (28 ballots: 14, 2, 12)" in text
    assert "[14, (blank)] (21 ballots: 11, 10)" in text
    assert RedactionReport.from_dict(json.loads(json.dumps(report.to_dict()))) == report
    # Without ballot type information there is nothing to list.
    no_types = analyze(tmp_path, [Ballot(b.contests, b.precinct, "") for b in rows])
    assert (no_types.shared_pattern_count, no_types.shared_patterns) == (0, ())
    # A report recorded before shared patterns were listed still reads.
    old = report.to_dict()
    del old["shared_pattern_count"], old["shared_patterns"]
    assert RedactionReport.from_dict(old).shared_pattern_count == 0


def test_fail_by_rare_style_and_precinct(tmp_path: Path) -> None:
    rows = ballots(12, "Mayor", precinct="101") + ballots(4, "Mayor", precinct="102") + ballots(
        8, "Mayor", precinct="103"
    )
    report = analyze(tmp_path, rows)
    assert report.verdict == FAIL
    assert (report.styles, report.groups, report.grouped_by) == (1, 3, "style+precinct+ballot_type")
    assert [(g.size, g.precinct) for g in report.rare_groups] == [(4, "102"), (8, "103")]
    # The same ballots with the precinct column removed, or blanked, pass.
    assert analyze(tmp_path, rows, precinct_column=False).verdict == PASS


def test_pass_when_precinct_is_blank_and_styles_are_large(tmp_path: Path) -> None:
    rows = ballots(12, "Mayor", precinct="") + ballots(10, "Mayor", "Council", precinct="")
    report = analyze(tmp_path, rows)
    assert report.verdict == PASS
    assert report.precinct_column == "PrecinctPortion"
    assert (report.precinct_present, report.grouped_by) == (False, "style+ballot_type")


def test_partly_blank_precincts_group_blank_rows_together(tmp_path: Path) -> None:
    rows = ballots(12, "Mayor", precinct="101") + ballots(3, "Mayor", precinct="")
    report = analyze(tmp_path, rows)
    assert report.verdict == FAIL
    assert [(g.size, g.precinct) for g in report.rare_groups] == [(3, None)]


def test_rows_with_no_vote_values_are_excluded(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor") + [Ballot((MAYOR, FIRE), blank=True)])
    # Markers in every vote cell, and markers beside blank vote cells, leave no vote values.
    data += data_row("12", "101", "1", "12", "101-1-12", "Mail", "101", "BT-1", votes=("*",) * 9)
    data += data_row("Redacted", "", "", "", "", "", "", "", votes=("X", "X"))
    report = analyze_redaction(write(tmp_path, data))
    assert report.verdict == PASS
    assert (report.rows_read, report.ballot_rows, report.already_redacted_rows) == (13, 10, 3)
    assert report.marked_ballot_rows == 0


def test_rows_marked_redacted_that_keep_votes_are_grouped(tmp_path: Path) -> None:
    marked = [
        Ballot((MAYOR, FIRE), marker="*"),
        Ballot((MAYOR, FIRE), marker="X"),
        Ballot((MAYOR, FIRE), marker="Redacted"),
        Ballot((MAYOR, FIRE), cvr_number="Redacted"),
    ]
    report = analyze(tmp_path, ballots(10, "Mayor") + marked)
    assert report.verdict == FAIL
    assert (report.rows_read, report.ballot_rows, report.marked_ballot_rows, report.already_redacted_rows) == (
        14,
        14,
        4,
        0,
    )
    # A marker cell is not a vote value, but the contest's other cells keep it on the card.
    assert [(g.size, g.contests) for g in report.rare_groups] == [(4, 2)]
    assert (report.marked_rows_in_rare_groups, report.rare_groups_with_marked_rows) == (4, 1)
    # Ten such rows make a group large enough.
    assert analyze(tmp_path, ballots(10, "Mayor") + marked * 3).verdict == PASS


def meta_row(cells: list[str], votes: tuple[str, ...], width: int) -> bytes:
    """A data row of a CVR with ``width`` columns: the given leading cells, then the votes."""
    row = cells + list(votes)
    return (",".join(row + [""] * (width - len(row))) + "\r\n").encode()


def test_boulder_pattern_identifiers_redacted_but_votes_kept(tmp_path: Path) -> None:
    """Boulder's 2025 CVR: ``Redacted`` over a ballot's identification, its 0/1 votes kept, no precinct column."""
    width = 7 + sum(len(choices) for choices in CONTESTS.values())
    data = make_cvr(ballots(20, "Mayor", ballot_type="06"), precinct_column=False)
    redacted = ["Redacted", "", "", "", "", "", "06"]
    # Such rows: 12 of a style found nowhere else (enough), 3 of another (too few),
    # and 3 of the unmarked ballots' style (joining their group).
    data += meta_row(redacted, ("0", "1", "", "", "", "1", "0"), width) * 12
    data += meta_row(redacted, ("1", "0", "", "", "", "", "", "0", "1"), width) * 3
    data += meta_row(redacted, ("1", "0"), width) * 3
    # An aggregate row that combines ballots, by its first cell and by its counts.
    data += meta_row(["Redacted and Consolidated 18 Ballots", "", "", "", "", "", "11 & PO-CC"], ("8", "9"), width)
    report = analyze_redaction(write(tmp_path, data))
    assert report.verdict == FAIL
    assert (report.ballot_rows, report.marked_ballot_rows, report.aggregate_rows) == (38, 18, 1)
    assert report.grouped_by == "style+ballot_type"
    assert [(g.size, g.ballot_type, g.contests) for g in report.rare_groups] == [(3, "06", 2)]
    assert (report.marked_rows_in_rare_groups, report.rare_groups_with_marked_rows) == (3, 1)


def test_aggregate_rows_are_excluded(tmp_path: Path) -> None:
    rows = ballots(10, "Mayor") + [
        Ballot((MAYOR, COUNCIL, MEASURE), cvr_number="AGGREGATED", ballot_type="AGGREGATED"),
        Ballot((FIRE,), cvr_number="Total"),
        Ballot((FIRE,), cvr_number="Redacted and Consolidated 18 Ballots"),
    ]
    data = make_cvr(rows)
    # A totals row with a blank first cell and counts above 1.
    data += data_row(votes=("7", "3"))
    report = analyze_redaction(write(tmp_path, data))
    assert report.verdict == PASS
    assert (report.aggregate_rows, report.ballot_rows, report.styles) == (4, 10, 1)


def test_a_blank_first_cell_alone_does_not_make_an_aggregate(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor")) + data_row(votes=("1", "0"))
    report = analyze_redaction(write(tmp_path, data))
    assert (report.verdict, report.ballot_rows, report.aggregate_rows) == (FAIL, 11, 0)


def test_threshold_is_configurable(tmp_path: Path) -> None:
    path = write(tmp_path, make_cvr(ballots(5, "Mayor") + ballots(7, "Council")))
    assert analyze_redaction(path).verdict == FAIL
    assert analyze_redaction(path, 5).verdict == PASS
    assert analyze_redaction(path, 6).rare_group_sizes == ((5, 1),)
    with pytest.raises(ValueError):
        analyze_redaction(path, 0)


def test_style_counts_contests_with_zeros_and_ignores_vote_values(tmp_path: Path) -> None:
    # A 0 in every column of a contest still puts it on the card.
    data = make_cvr(ballots(10, "Mayor", "Council"))
    lines = data.decode().split("\r\n")
    for index in range(4, 9):
        cells = lines[index].split(",")
        cells[8:10] = ["0", "0"]
        lines[index] = ",".join(cells)
    report = analyze_redaction(write(tmp_path, "\r\n".join(lines).encode()))
    assert (report.verdict, report.styles) == (PASS, 1)


def test_style_hash_is_stable_and_order_free() -> None:
    assert style_hash(["B", "A"]) == style_hash(["A", "B"]) == style_hash({"A", "B"})
    assert len(style_hash(["A"])) == 12
    assert style_hash(["A"]) != style_hash(["A", "B"])


def test_contest_names_only_when_asked(tmp_path: Path) -> None:
    path = write(tmp_path, make_cvr(ballots(10, "Mayor") + ballots(2, "Mayor", "Fire")))
    report = analyze_redaction(path, include_contest_names=True)
    assert report.rare_groups[0].contest_names == tuple(sorted((MAYOR, FIRE)))
    assert "Fire District" in format_report(report)


def test_report_never_holds_votes(tmp_path: Path) -> None:
    rows = ballots(10, "Mayor") + ballots(2, "Mayor", "Council", "Measure", "Fire", precinct="7")
    report = analyze_redaction(write(tmp_path, make_cvr(rows)), include_contest_names=True)
    text = json.dumps(report.to_dict(max_groups=None)) + format_report(report, max_groups=None)
    for choices in CONTESTS.values():
        for choice in choices:
            assert choice not in text


def test_rare_ballot_type_subgroup_fails_without_precinct(tmp_path: Path) -> None:
    rows = ballots(12, "Mayor", ballot_type="BT-1") + ballots(2, "Mayor", ballot_type="BT-2")
    report = analyze(tmp_path, rows, precinct_column=False)
    assert report.verdict == FAIL
    assert (report.precinct_column, report.ballot_type_column) == (None, "BallotType")
    assert (report.grouped_by, report.styles, report.groups) == ("style+ballot_type", 1, 2)
    assert [(g.size, g.precinct, g.ballot_type) for g in report.rare_groups] == [(2, None, "BT-2")]


def test_large_ballot_types_pass(tmp_path: Path) -> None:
    rows = (
        ballots(12, "Mayor", ballot_type="BT-1")
        + ballots(10, "Mayor", ballot_type="BT-2")
        + ballots(11, "Mayor", "Council", ballot_type="BT-3")
    )
    report = analyze(tmp_path, rows, precinct_column=False)
    assert report.verdict == PASS
    assert (report.grouped_by, report.styles, report.groups) == ("style+ballot_type", 2, 3)


def test_precinct_and_ballot_type_together(tmp_path: Path) -> None:
    rows = ballots(12, "Mayor", precinct="101", ballot_type="BT-1") + ballots(
        12, "Mayor", precinct="101", ballot_type="BT-2"
    )
    rows += ballots(5, "Mayor", precinct="102", ballot_type="BT-1") + ballots(
        5, "Mayor", precinct="102", ballot_type="BT-2"
    )
    report = analyze(tmp_path, rows)
    assert report.grouped_by == "style+precinct+ballot_type"
    assert [(g.size, g.precinct, g.ballot_type) for g in report.rare_groups] == [(5, "102", "BT-1"), (5, "102", "BT-2")]
    # A ballot type column left blank on every ballot row is not used.
    blank = [Ballot(b.contests, b.precinct, "") for b in rows]
    blanked = analyze(tmp_path, blank)
    assert (blanked.ballot_type_present, blanked.grouped_by) == (False, "style+precinct")
    assert [(g.size, g.precinct, g.ballot_type) for g in blanked.rare_groups] == []


def test_cells_beyond_the_header_fail(tmp_path: Path) -> None:
    data = make_cvr(ballots(10, "Mayor"))
    lines = data.decode().split("\r\n")
    report = analyze_redaction(write(tmp_path, with_lines(data, {5: lines[5] + ",1"})))
    assert report.verdict == FAIL
    assert report.problems == ("1 row(s) have non-blank cells beyond the header's 17 columns (first at CSV record 6)",)
    # Trailing empty cells are harmless.
    padded = with_lines(data, {i: lines[i] + ",," for i in range(4, 14)})
    assert analyze_redaction(write(tmp_path, padded, "padded.csv")).verdict == PASS


def test_report_round_trips(tmp_path: Path) -> None:
    report = analyze(tmp_path, ballots(10, "Mayor") + ballots(3, "Council", precinct="102"))
    assert RedactionReport.from_dict(report.to_dict(max_groups=None)) == report
    assert report.to_dict(max_groups=0)["rare_groups"] == []
    assert report.to_dict(max_groups=0)["rare_group_count"] == 1


# ---------------------------------------------------------------------------
# The limited worker process


def test_worker_agrees_with_in_process_analysis(tmp_path: Path) -> None:
    path = write(tmp_path, make_cvr(ballots(10, "Mayor") + ballots(3, "Council", precinct="102")))
    assert check_redaction(path, include_contest_names=True) == analyze_redaction(
        path, include_contest_names=True
    )


def test_memory_limit_stops_a_pathological_file(tmp_path: Path) -> None:
    # One row of 1.5 million distinct short cells needs far more than 128 MiB.
    head = make_cvr([]).decode()
    path = write(tmp_path, (head + "ab," * 1_500_000 + "\r\n").encode())
    report = check_redaction(path, limits=CheckLimits(max_memory_bytes=128 * 1024 * 1024))
    assert report.verdict == FAIL
    assert report.problems == ("check stopped: it needed more than 134217728 bytes of memory",)


def test_time_limit_stops_a_check_that_hangs(tmp_path: Path) -> None:
    # A named pipe that no one writes to: the worker blocks reading it.
    fifo = tmp_path / "stalled.csv"
    os.mkfifo(fifo)
    started = time.monotonic()
    report = check_redaction(fifo, limits=CheckLimits(timeout_seconds=0.5))
    assert time.monotonic() - started < 10
    assert report.verdict == FAIL
    assert report.problems == ("check stopped: it took longer than 0.5 seconds",)


def test_missing_file_fails(tmp_path: Path) -> None:
    report = check_redaction(tmp_path / "absent.csv")
    assert report.verdict == FAIL
    assert "could not read" in report.problems[0]


# ---------------------------------------------------------------------------
# Command line


def test_cli_check_redaction(tmp_path: Path) -> None:
    runner = CliRunner()
    good = write(tmp_path, make_cvr(ballots(10, "Mayor")), "good.csv")
    bad = write(tmp_path, make_cvr(ballots(10, "Mayor") + ballots(2, "Mayor", "Fire", precinct="9")), "bad.csv")
    passed = runner.invoke(app, ["check-redaction", str(good)])
    assert passed.exit_code == 0, passed.output
    assert passed.output.startswith("PASS")
    failed = runner.invoke(app, ["check-redaction", "--names", str(bad)])
    assert failed.exit_code == 1
    assert "2 ballot(s): precinct '9', ballot type 'BT-1', style" in failed.output
    assert "Fire District" in failed.output
    as_json = runner.invoke(app, ["check-redaction", "--json", "--threshold", "2", str(bad)])
    assert as_json.exit_code == 0
    assert json.loads(as_json.output)["verdict"] == PASS
    assert runner.invoke(app, ["check-redaction", str(tmp_path / "absent.csv")]).exit_code == 2
