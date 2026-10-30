"""Acceptance test: cross-check the redaction check with the public CVR anonymizer.

Marked ``slow``, and skipped unless the environment variable
``CORLA_OBSERVER_ANONYMIZER`` names a local checkout of
https://github.com/loriinboulder/anonymize_cvr. That repository has no
license file, so none of its code is copied here: its ``anonymize_cvr.py``
is imported from the checkout at test time, and its own check
(``CvrDatabase``, ``build_row_index`` in check mode and
``check_redaction_needs``, with ``redact_on_precinct`` set when the file
carries precinct information) is compared with `analyze_redaction`: the
verdict, and the sizes of the groups below the threshold. Its module imports
tkinter at the top level, for its window; if tkinter is unavailable, empty
stand-in modules are installed first.

The two agree on every synthetic file in `AGREE`, and on every real public
file in `REAL_AGREE`. The definitional differences found, each pinned down
by a test in `DIFFER` so that a change on either side is noticed:

1. **Rows marked with ``*`` that keep votes.** Both group rows marked
   ``X`` or ``Redacted`` (in the identification or in a vote cell) that
   still carry vote values, and both skip rows whose vote cells are all
   blank. The anonymizer also skips any row with ``*`` in a vote cell; here
   such a row is grouped if other vote values remain, since its votes and
   its set of contests are visible.
2. **Aggregate rows.** The anonymizer skips only rows whose CvrNumber or
   BallotType is ``AGGREGATED``. Here a row is also an aggregate, and
   skipped, if a vote value is a whole number above 1 or its first cell
   names an aggregate (``Total``, ``Redacted and Consolidated 18
   Ballots``). Both group a row with a blank first cell and 0/1 votes.
3. **Ballot type.** Here ballots are grouped by ballot type too, when the
   file carries one, so a rare ballot-type subgroup of a large style fails
   the file; the anonymizer groups by style (and precinct) only, and only
   warns when a style has several ballot types.
4. **Excel wrapping of vote cells.** A vote cell written ``=""`` is blank
   here, and non-blank (the contest is on the card) to the anonymizer.
5. **Row width.** The anonymizer refuses a file with any row whose number
   of cells differs from the header's; here blank trailing cells are
   accepted, and only non-blank cells beyond the header fail the check.
6. **Encoding.** The anonymizer reads UTF-8 only, and stops on a Latin-1
   file; here Latin-1 (and UTF-16 with a byte order mark) is read. The
   results agree once such a file is transcoded to UTF-8.

Differences 2 and 3 were found on a real published file as well as on the
synthetic ones; what that file's own verdict is, and what follows from it,
is a finding about a county's published record and is kept with the team,
not here.

Changed with the rule that groups marked rows and ballot type: before it,
rows marked ``X`` or ``Redacted`` were excluded here, a row with a blank
first cell was an aggregate here, and ballot type was only a warning.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from corla_observer.redaction import analyze_redaction
from synthetic import CONTESTS, Ballot, ballots, make_cvr

ANONYMIZER_ENV = "CORLA_OBSERVER_ANONYMIZER"

pytestmark = [
    pytest.mark.slow,
    # Skip before the session's mirror files are fetched for nothing.
    pytest.mark.skipif(
        not os.environ.get(ANONYMIZER_ENV),
        reason=f"set {ANONYMIZER_ENV} to a checkout of https://github.com/loriinboulder/anonymize_cvr",
    ),
]
MAYOR, COUNCIL, MEASURE, FIRE = CONTESTS

Result = tuple[str, list[int]]


@pytest.fixture(scope="module")
def anonymizer() -> Any:
    """The anonymizer's module, imported from the checkout named by the environment."""
    checkout = os.environ.get(ANONYMIZER_ENV, "")
    source = Path(checkout) / "anonymize_cvr.py"
    if not source.is_file():
        pytest.skip(f"no anonymize_cvr.py in {checkout}")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        stub = types.ModuleType("tkinter")
        for name in ("filedialog", "messagebox"):
            module = types.ModuleType(f"tkinter.{name}")
            setattr(stub, name, module)
            sys.modules[f"tkinter.{name}"] = module
        sys.modules["tkinter"] = stub
    spec = importlib.util.spec_from_file_location("anonymize_cvr_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def theirs(anonymizer: Any, path: Path, redact_on_precinct: bool, threshold: int = 10) -> Result:
    """The anonymizer's verdict and sorted rare-group sizes, its progress messages discarded."""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        db = anonymizer.CvrDatabase(str(path), 0, None)
        index = anonymizer.build_row_index(str(path), db, redact_on_precinct, check_mode=True)
        needs = anonymizer.check_redaction_needs(index, db, threshold, redact_on_precinct)
    verdict = "FAIL" if needs.needs_redaction() else "PASS"
    return verdict, sorted(needs.rare_privacy_unit_pairs.values())


def ours(path: Path, threshold: int = 10) -> tuple[Result, bool]:
    """Our verdict and sorted rare-group sizes, and whether precinct information was present."""
    report = analyze_redaction(path, threshold)
    assert report.problems == (), report.problems
    return (report.verdict, sorted(g.size for g in report.rare_groups)), report.precinct_present


def compare(anonymizer: Any, path: Path) -> tuple[Result, Result]:
    mine, precinct = ours(path)
    return mine, theirs(anonymizer, path, precinct)


#: Synthetic files on which the two definitions agree: (id, rows, make_cvr options).
AGREE: list[tuple[str, list[Ballot], dict[str, Any]]] = [
    ("pass", ballots(10, "Mayor", "Council") + ballots(12, "Mayor", "Measure"), {}),
    ("rare-style", ballots(10, "Mayor") + ballots(3, "Mayor", "Fire"), {"precinct_column": False}),
    (
        "rare-style-and-precinct",
        ballots(12, "Mayor", precinct="101") + ballots(4, "Mayor", precinct="102") + ballots(8, "Mayor", precinct="103"),
        {},
    ),
    ("blank-precinct", ballots(12, "Mayor", precinct="") + ballots(10, "Mayor", "Council", precinct=""), {}),
    ("partly-blank-precinct", ballots(12, "Mayor", precinct="101") + ballots(3, "Mayor", precinct=""), {}),
    ("blank-redaction", ballots(10, "Mayor") + [Ballot((MAYOR, FIRE), blank=True)], {}),
    ("x-marker-keeping-votes", ballots(10, "Mayor") + [Ballot((MAYOR, FIRE), marker="X")], {}),
    ("redacted-over-identification", ballots(10, "Mayor") + [Ballot((MAYOR, FIRE), cvr_number="Redacted")], {}),
    ("ballot-types-large", ballots(12, "Mayor", ballot_type="BT-1") + ballots(10, "Mayor", ballot_type="BT-2"), {}),
    (
        "aggregated-row",
        ballots(10, "Mayor") + [Ballot((MAYOR, COUNCIL), cvr_number="AGGREGATED", ballot_type="AGGREGATED")],
        {},
    ),
    ("excel-identification", ballots(10, "Mayor") + ballots(2, "Council", precinct="9"), {"excel": True}),
    ("quote-all", ballots(10, "Mayor") + ballots(2, "Council", precinct="9"), {"quote_all": True}),
    ("byte-order-mark", ballots(10, "Mayor") + ballots(2, "Council", precinct="9"), {"bom": True}),
    ("lf", ballots(9, "Mayor") + ballots(11, "Council"), {"line_ending": "\n"}),
]


@pytest.mark.parametrize(("name", "rows", "options"), AGREE, ids=[a[0] for a in AGREE])
def test_agrees_on_synthetic_files(
    anonymizer: Any, tmp_path: Path, name: str, rows: list[Ballot], options: dict[str, Any]
) -> None:
    path = tmp_path / f"{name}.csv"
    path.write_bytes(make_cvr(rows, **options))
    mine, other = compare(anonymizer, path)
    assert mine == other


def test_agrees_on_a_row_with_a_blank_first_cell(anonymizer: Any, tmp_path: Path) -> None:
    path = tmp_path / "blank-first-cell.csv"
    path.write_bytes(_blank_first_cell_row(make_cvr(ballots(10, "Mayor"))))
    assert compare(anonymizer, path) == (("FAIL", [1]), ("FAIL", [1]))


def test_agrees_on_latin1_once_transcoded(anonymizer: Any, tmp_path: Path) -> None:
    rows = ballots(10, "Mayor", precinct="Cañon") + ballots(2, "Mayor", precinct="Peñasco")
    latin1 = tmp_path / "latin1.csv"
    latin1.write_bytes(make_cvr(rows, encoding="latin-1"))
    with pytest.raises(UnicodeDecodeError):
        theirs(anonymizer, latin1, True)
    utf8 = tmp_path / "utf8.csv"
    utf8.write_bytes(latin1.read_bytes().decode("latin-1").encode("utf-8"))
    assert ours(latin1)[0] == theirs(anonymizer, utf8, True) == ("FAIL", [2])


def _excel_blank_vote_cells(data: bytes) -> bytes:
    lines = data.decode().split("\r\n")
    for index in range(4, len(lines) - 1):
        cells = lines[index].split(",")
        lines[index] = ",".join(cells[:8] + [cell or '=""' for cell in cells[8:]])
    return "\r\n".join(lines).encode()


def _blank_first_cell_row(data: bytes) -> bytes:
    return data + ",".join([""] * 15 + ["1", "0"]).encode() + b"\r\n"


def _consolidated_row(data: bytes) -> bytes:
    return data + ",".join(["Redacted and Consolidated 18 Ballots"] + [""] * 7 + ["8", "9"] + [""] * 7).encode() + b"\r\n"


def _pad_data_rows(data: bytes) -> bytes:
    lines = data.decode().split("\r\n")
    return "\r\n".join(lines[:4] + [line + ",," if line else line for line in lines[4:]]).encode()


#: (id, file contents, our result, the anonymizer's result or the exception it raises)
DIFFER: list[tuple[str, Callable[[], bytes], Result, Result | type[Exception]]] = [
    ("star-marker-keeping-votes", lambda: make_cvr(ballots(10, "Mayor") + [Ballot((MAYOR, FIRE), marker="*")]), ("FAIL", [1]), ("PASS", [])),
    ("consolidated-row", lambda: _consolidated_row(make_cvr(ballots(10, "Mayor"))), ("PASS", []), ("FAIL", [1])),
    (
        "rare-ballot-type",
        lambda: make_cvr(ballots(12, "Mayor", ballot_type="BT-1") + ballots(2, "Mayor", ballot_type="BT-2")),
        ("FAIL", [2]),
        ("PASS", []),
    ),
    (
        "excel-blank-vote-cells",
        lambda: _excel_blank_vote_cells(make_cvr(ballots(12, "Mayor") + ballots(3, "Council"))),
        ("FAIL", [3]),
        ("PASS", []),
    ),
    ("blank-trailing-cells", lambda: _pad_data_rows(make_cvr(ballots(10, "Mayor"))), ("PASS", []), ValueError),
]  # fmt: skip


@pytest.mark.parametrize(("name", "build", "mine", "other"), DIFFER, ids=[d[0] for d in DIFFER])
def test_documented_differences(
    anonymizer: Any,
    tmp_path: Path,
    name: str,
    build: Callable[[], bytes],
    mine: Result,
    other: Result | type[Exception],
) -> None:
    path = tmp_path / f"{name}.csv"
    path.write_bytes(build())
    result, precinct = ours(path)
    assert result == mine
    if isinstance(other, type):
        with pytest.raises(other):
            theirs(anonymizer, path, precinct)
    else:
        assert theirs(anonymizer, path, precinct) == other


REAL_AGREE = [
    "2025/cvrs/broomfield-Redacted_CVR_Export_20251119093616.csv",
    "2026/primary/observerfiles/LaPlata_RedactedTest_NoContestBalance_CVR_Export_20260709081424.csv",
    "2026/primary/observerfiles/Morgan_CVR_Export_20260709092315_Redacted.csv",
    "2026/primary/observerfiles/Weld_CVR_Export_20260709140755.csv",
]


@pytest.mark.parametrize("path", REAL_AGREE, ids=[Path(p).name for p in REAL_AGREE])
def test_agrees_on_real_files(anonymizer: Any, mirror_root: Path, path: str) -> None:
    mine, other = compare(anonymizer, mirror_root / path)
    assert mine == other == ("PASS", [])

