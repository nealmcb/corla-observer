"""Read county cast vote record (CVR) files, tolerating the counties' export quirks.

Colorado's counties export their CVRs from Dominion's election management
system as CSV with four header rows, then one row per ballot card:

1. a title row: the election name and the software version;
2. a contest row: each vote column's contest name, repeated across the
   contest's choices (``United States Senator - DEM (Vote For=1)``);
3. a choice row: each vote column's choice (candidate) name;
4. a column-name row: ``CvrNumber``, ``TabulatorNum``, ``BatchId``,
   ``RecordId``, ``ImprintedId``, optionally ``CountingGroup`` and
   ``PrecinctPortion``, then ``BallotType``; in primaries the vote columns
   of this row carry party names.

Each data row has a 0 or 1 in every vote column of a contest on that card,
and blanks in the columns of contests not on it.

Quirks handled:

- **Header detection.** The column-name row is the first of the first
  `HEADER_SEARCH_ROWS` rows with both ``CvrNumber`` and ``ImprintedId``
  (compared ignoring case, spacing and punctuation); the contest row is two
  rows above it. Files with a missing title row are read the same way.
- **Vote columns** start just after ``BallotType``. Without a
  ``BallotType`` column, they start at the contest row's first non-empty
  cell. If both are present and disagree, the header is reported as a
  problem rather than guessed at.
- **Excel wrapping.** Cells written as ``="..."`` (so that Excel keeps
  leading zeros) are unwrapped, in quote-all exports too.
- **Encodings.** UTF-8 with or without a byte order mark, UTF-16 with a
  byte order mark, else Latin-1 (which never fails). The whole file is
  checked for valid UTF-8 before it is read as UTF-8, so a Latin-1 byte
  far down the file is not misread.
- **Line endings** CRLF, LF or CR, and blank lines, which are skipped.
- **Delimiter.** Comma, else tab, semicolon or vertical bar: the first that
  gives a recognizable column-name row.

A cell holds a redaction marker if, after unwrapping, it is ``*``, ``X``
(either case), or text beginning ``redact`` (``Redacted``, ``REDACTED``).
A vote value is a vote cell that is neither blank nor a redaction marker.
Each data row is classified (`CvrRow.status`):

``redacted``
    No vote values are left: every vote cell is blank or a redaction marker.
``aggregate``
    The row combines several ballots: some vote value is a whole number
    above 1, or the first cell names an aggregate. The first cell names an
    aggregate when it is text that is neither a whole number nor a
    redaction marker (``AGGREGATED``, ``Total``), or a redaction marker
    that contains a digit or one of the words in `AGGREGATE_WORDS`
    (``Redacted and Consolidated 18 Ballots``).
``ballot``
    Everything else: one ballot card, whose vote values are visible. A
    ballot row may still be marked redacted (`CvrRow.marked`), as when a
    county writes ``Redacted`` over its identification, ``*`` or ``X`` in
    some cells, but keeps its votes; its contests are visible all the same.
    A blank first cell does not make a row an aggregate.

A row never exposes its votes: `CvrRow` carries only the card's
identification, its precinct and ballot type, and which contests are on it.

Standard library only.
"""

from __future__ import annotations

import codecs
import csv
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Rows searched for the column-name row, from the top of the file.
HEADER_SEARCH_ROWS = 10

#: Bytes read per step when checking a file's encoding.
_CHUNK = 1024 * 1024

_DELIMITERS = (",", "\t", ";", "|")

#: Column-name spellings (normalized: lowercase letters and digits only).
CVR_NUMBER_NAMES = ("cvrnumber",)
TABULATOR_NAMES = ("tabulatornum", "tabulator", "tabulatorid")
BATCH_NAMES = ("batchid", "batch")
RECORD_NAMES = ("recordid", "record")
IMPRINTED_NAMES = ("imprintedid",)
COUNTING_GROUP_NAMES = ("countinggroup",)
#: Columns taken as precinct information.
PRECINCT_NAMES = (
    "precinctportion",
    "precinct",
    "precinctname",
    "precinctid",
    "precinctsplit",
    "precinctcode",
)
#: Columns taken as ballot type information, the first found before the vote columns.
BALLOT_TYPE_NAMES = (
    "ballottype",
    "ballottypeid",
    "ballottypecode",
    "ballotstyle",
    "ballotstyleid",
    "ballotstylecode",
)
#: The column after which the vote columns start, when present.
VOTE_START_AFTER = "ballottype"
#: Words that, in a first cell marked redacted, show that the row combines several ballots.
AGGREGATE_WORDS = ("aggregat", "consolidat", "combin", "total")

ROW_STATUSES = ("ballot", "redacted", "aggregate")

_WHOLE_NUMBER = re.compile(r"[0-9]+")
_UTF8_BOM_AS_LATIN1 = codecs.BOM_UTF8.decode("latin-1")


class CvrFormatError(ValueError):
    """The file is not a CVR this reader recognizes."""


def unwrap(cell: str) -> str:
    """A cell's value: surrounding whitespace and Excel's ``="..."`` wrapping removed."""
    if not cell:
        return ""
    text = cell.strip()
    if len(text) >= 3 and text.startswith('="') and text.endswith('"'):
        text = text[2:-1].strip()
    return text


def normalize_name(cell: str) -> str:
    """A header cell reduced for comparison: unwrapped, lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", unwrap(cell).lower())


def is_redaction_marker(value: str) -> bool:
    """Whether an (unwrapped) cell value marks a redaction: ``*``, ``X``/``x`` or ``Redacted...``."""
    return value in ("*", "X", "x") or value[:6].lower() == "redact"


def detect_encoding(path: Path | str) -> str:
    """The file's encoding: ``utf-8-sig``, ``utf-8``, ``utf-16`` or ``latin-1``.

    Reads the whole file in chunks to confirm it is valid UTF-8 before
    calling it UTF-8.
    """
    with open(path, "rb") as f:
        start = f.read(4)
        if start.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        bom = start.startswith(codecs.BOM_UTF8)
        f.seek(len(codecs.BOM_UTF8) if bom else 0)
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while chunk := f.read(_CHUNK):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return "latin-1"
    return "utf-8-sig" if bom else "utf-8"


@dataclass(frozen=True)
class CvrHeader:
    """What the header rows of a CVR file say about its layout."""

    encoding: str
    delimiter: str
    #: 1-based CSV record number of the column-name row (4 in a standard export),
    #: counting blank lines as records.
    header_row: int
    #: The title row's first two cells, unwrapped (election name and version), if present.
    election: str | None
    version: str | None
    #: The column-name row, unwrapped.
    columns: tuple[str, ...]
    #: Number of cells in the column-name row: the expected row width.
    width: int
    #: Index of the first vote column.
    vote_start: int
    #: Unique contest names, in the order their first column appears.
    contests: tuple[str, ...]
    #: For each vote column (from `vote_start`), the index into `contests` of its contest.
    column_contest: tuple[int, ...]
    cvr_number_index: int
    imprinted_index: int
    tabulator_index: int | None
    batch_index: int | None
    record_index: int | None
    counting_group_index: int | None
    precinct_index: int | None
    #: The ballot type column (``BallotType`` or a similar name), if any.
    ballot_type_index: int | None
    #: Vote columns with an empty contest-row cell, each treated as a contest of its own.
    unnamed_vote_columns: int = 0

    @property
    def precinct_column(self) -> str | None:
        """The precinct column's name as written, or None if there is none."""
        return None if self.precinct_index is None else self.columns[self.precinct_index]

    @property
    def ballot_type_column(self) -> str | None:
        """The ballot type column's name as written, or None if there is none."""
        return None if self.ballot_type_index is None else self.columns[self.ballot_type_index]


@dataclass(frozen=True)
class CvrRow:
    """One data row of a CVR, without its votes."""

    #: 1-based position among the file's non-blank data rows.
    number: int
    #: 1-based CSV record number in the file (the header rows count).
    record: int
    #: "ballot", "redacted" or "aggregate" (see the module documentation).
    status: str
    #: Whether some cell holds a redaction marker.
    marked: bool
    cvr_number: str
    imprinted_id: str | None
    #: The precinct column's value, or None if there is no such column or it is blank.
    precinct: str | None
    ballot_type: str | None
    #: Names of the contests with at least one vote value in this row.
    contests: frozenset[str]
    #: Whether the row has any vote value.
    has_votes: bool
    #: Whether the row has non-blank cells beyond the column-name row's width.
    extra_cells: bool = False

    @property
    def redacted(self) -> bool:
        return self.status == "redacted"

    @property
    def aggregate(self) -> bool:
        return self.status == "aggregate"


def _find(names: Sequence[str], wanted: Sequence[str], stop: int | None = None) -> int | None:
    limit = len(names) if stop is None else min(stop, len(names))
    for spelling in wanted:
        for index in range(limit):
            if names[index] == spelling:
                return index
    return None


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return unwrap(row[index])


def _read_head(path: Path, encoding: str, delimiter: str) -> list[list[str]]:
    with open(path, encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows: list[list[str]] = []
        try:
            for row in reader:
                rows.append(row)
                if len(rows) >= HEADER_SEARCH_ROWS:
                    break
        except csv.Error:
            pass
    if rows and rows[0] and encoding == "latin-1" and rows[0][0].startswith(_UTF8_BOM_AS_LATIN1):
        rows[0][0] = rows[0][0][len(_UTF8_BOM_AS_LATIN1) :]
    return rows


def _parse_header(rows: list[list[str]], encoding: str, delimiter: str) -> CvrHeader | None:
    for index, row in enumerate(rows):
        names = [normalize_name(cell) for cell in row]
        if "cvrnumber" in names and "imprintedid" in names:
            break
    else:
        return None
    if index < 2:
        raise CvrFormatError(
            f"the column-name row is row {index + 1}; there is no contest row two rows above it"
        )
    contest_row = [unwrap(cell) for cell in rows[index - 2]]
    title_row = [unwrap(cell) for cell in rows[index - 3]] if index >= 3 else []
    columns = tuple(unwrap(cell) for cell in row)
    width = len(columns)
    ballot_type = _find(names, (VOTE_START_AFTER,))
    first_contest = next(
        (i for i, name in enumerate(contest_row) if name and i < width), None
    )
    if ballot_type is not None:
        vote_start = ballot_type + 1
        if first_contest is not None and first_contest != vote_start:
            raise CvrFormatError(
                f"the contest row's first contest is in column {first_contest + 1}, "
                f"but BallotType is column {ballot_type + 1}"
            )
    elif first_contest is not None:
        vote_start = first_contest
    else:
        raise CvrFormatError("no vote columns: no BallotType column and an empty contest row")
    if vote_start >= width:
        raise CvrFormatError("no vote columns after BallotType")
    contests: list[str] = []
    contest_index: dict[str, int] = {}
    column_contest: list[int] = []
    unnamed = 0
    for column in range(vote_start, width):
        name = contest_row[column] if column < len(contest_row) else ""
        if not name:
            unnamed += 1
            name = f"(unnamed column {column + 1})"
        if name not in contest_index:
            contest_index[name] = len(contests)
            contests.append(name)
        column_contest.append(contest_index[name])
    cvr_number = _find(names, CVR_NUMBER_NAMES, vote_start)
    imprinted = _find(names, IMPRINTED_NAMES, vote_start)
    if cvr_number is None or imprinted is None:
        raise CvrFormatError("CvrNumber and ImprintedId must come before the vote columns")
    return CvrHeader(
        encoding=encoding,
        delimiter=delimiter,
        header_row=index + 1,
        election=(title_row[0] or None) if title_row else None,
        version=(title_row[1] or None) if len(title_row) > 1 else None,
        columns=columns,
        width=width,
        vote_start=vote_start,
        contests=tuple(contests),
        column_contest=tuple(column_contest),
        cvr_number_index=cvr_number,
        imprinted_index=imprinted,
        tabulator_index=_find(names, TABULATOR_NAMES, vote_start),
        batch_index=_find(names, BATCH_NAMES, vote_start),
        record_index=_find(names, RECORD_NAMES, vote_start),
        counting_group_index=_find(names, COUNTING_GROUP_NAMES, vote_start),
        precinct_index=_find(names, PRECINCT_NAMES, vote_start),
        ballot_type_index=_find(names, BALLOT_TYPE_NAMES, vote_start),
        unnamed_vote_columns=unnamed,
    )


class CvrReader:
    """A CVR file opened for reading: its `header`, then its `rows`.

    Raises:
        CvrFormatError: on construction, if no CVR header is found.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        encoding = detect_encoding(self.path)
        header: CvrHeader | None = None
        for delimiter in _DELIMITERS:
            header = _parse_header(_read_head(self.path, encoding, delimiter), encoding, delimiter)
            if header is not None:
                break
        if header is None:
            raise CvrFormatError(
                f"no CVR column-name row (with CvrNumber and ImprintedId) in the first "
                f"{HEADER_SEARCH_ROWS} rows"
            )
        self.header = header

    def rows(self) -> Iterator[CvrRow]:
        """Yield every non-blank data row after the header, in file order.

        Raises:
            csv.Error: if the CSV is malformed past repair (for example, a
                field longer than the csv module's field size limit).
        """
        h = self.header
        contests = h.contests
        column_contest = h.column_contest
        vote_start, width = h.vote_start, h.width
        cache: dict[tuple[int, ...], frozenset[str]] = {}
        number = 0
        with open(self.path, encoding=h.encoding, newline="") as f:
            reader = csv.reader(f, delimiter=h.delimiter)
            for record, row in enumerate(reader, start=1):
                if record <= h.header_row or not any(cell.strip() for cell in row):
                    continue
                number += 1
                yield _classify_row(
                    row, number, record, h, contests, column_contest, vote_start, width, cache
                )

    def __iter__(self) -> Iterator[CvrRow]:
        return self.rows()


def names_aggregate(first: str) -> bool:
    """Whether a row's (unwrapped) first cell says the row combines several ballots.

    True for text that is neither a whole number nor a redaction marker
    (``AGGREGATED``, ``Total``), and for a redaction marker containing a
    digit or one of `AGGREGATE_WORDS` (``Redacted and Consolidated 18
    Ballots``). False for a blank cell, a whole number, and a plain marker
    (``Redacted``, ``*``, ``X``).
    """
    if not first or _WHOLE_NUMBER.fullmatch(first):
        return False
    if not is_redaction_marker(first):
        return True
    lowered = first.lower()
    return any(c.isdigit() for c in first) or any(word in lowered for word in AGGREGATE_WORDS)


def _classify_row(
    row: list[str],
    number: int,
    record: int,
    h: CvrHeader,
    contests: tuple[str, ...],
    column_contest: tuple[int, ...],
    vote_start: int,
    width: int,
    cache: dict[tuple[int, ...], frozenset[str]],
) -> CvrRow:
    marker = False
    counts = False
    present: list[int] = []
    last = -1
    end = min(width, len(row))
    for column in range(vote_start, end):
        cell = row[column]
        if not cell:
            continue
        value = unwrap(cell)
        if not value:
            continue
        if is_redaction_marker(value):
            marker = True
            continue
        if not counts and _WHOLE_NUMBER.fullmatch(value) and value.lstrip("0") not in ("", "1"):
            counts = True  # a whole number above 1
        contest = column_contest[column - vote_start]
        if contest != last:
            present.append(contest)
            last = contest
    if not marker:
        for column in range(min(vote_start, len(row))):
            if row[column] and is_redaction_marker(unwrap(row[column])):
                marker = True
                break
    extra = False
    if len(row) > width:
        for cell in row[width:]:
            value = unwrap(cell)
            if value:
                extra = True
                marker = marker or is_redaction_marker(value)
    key = tuple(sorted(set(present)))
    names = cache.get(key)
    if names is None:
        names = cache[key] = frozenset(contests[i] for i in key)
    has_votes = bool(key)
    first = unwrap(row[0]) if row else ""
    if not has_votes:
        status = "redacted"
    elif counts or names_aggregate(first):
        status = "aggregate"
    else:
        status = "ballot"
    return CvrRow(
        number=number,
        record=record,
        status=status,
        marked=marker,
        cvr_number=_cell(row, h.cvr_number_index),
        imprinted_id=_cell(row, h.imprinted_index) or None,
        precinct=_cell(row, h.precinct_index) or None,
        ballot_type=_cell(row, h.ballot_type_index) or None,
        contests=names,
        has_votes=has_votes,
        extra_cells=extra,
    )


def open_cvr(path: Path | str) -> CvrReader:
    """Open a CVR file and read its header; see `CvrReader`."""
    return CvrReader(path)
