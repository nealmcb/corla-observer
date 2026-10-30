"""Recognize audit center files by their content, not their names.

The Secretary of State's Audit Center changes its layout, file names,
extensions and column headers from election to election: the same kind of
file has been ``county_manifest_Adams.csv``, ``Adams.csv`` and
``AdamsBallotManifest.csv``, and a file named
``CVRtoAuditBoardInterpretativeComparison.csv`` has held CORLA's
manifest-to-CVR batch comparison. So `classify_bytes` looks only at what a
file contains and returns a `Classification`: one of `KINDS`, the county
where the file is about a single county, and the evidence used, so that a
person can check the call.

How a file is recognized:

- **Container.** Zip files are opened as Excel workbooks (.xlsx) when they
  hold ``xl/workbook.xml``; PDF, Word, OpenDocument, legacy Office and image
  files are recognized by their leading bytes and reported as ``unknown``
  with their format.
- **Text.** Text is decoded as UTF-8 (with or without a byte order mark) or
  UTF-16 with a byte order mark, falling back to Latin-1, which never fails.
  HTML is recognized by its markup.
- **Tables.** CSV text is parsed with the delimiter `csv.Sniffer` suggests,
  then each common delimiter in turn. In the first rows of a CSV, or of each
  of a workbook's first sheets, a header row is sought that matches a
  signature (`SIGNATURES`): a set of required columns, each with its
  accepted spellings. Header cells are compared ignoring case, spacing and
  punctuation, so ``# of Ballot Cards``, ``#of ballot cards`` and
  ``Ballot Cards`` are one spelling; Excel's ``="..."`` wrapping is
  removed. The manifest signature accepts every column-name variant
  `corla_observer.sampling.load_county_manifest` accepts.
- **County files.** A Dominion cast vote record (``cvr``) by its
  ``CvrNumber``, ``ImprintedId`` and ``TabulatorNum`` columns. A county's
  pull list (``pull_list``, CORLA's "List of ballots to audit") by an
  imprinted ID column, or tabulator, batch and record columns, together with
  an audit board column; CORLA's published ballot lists, with an "audited"
  column instead of a board, are pull lists too. Pull-list columns are not
  documented, so the evidence says which were found.
- **State Audit Report.** A workbook whose "Summary" sheet has an "Audit
  Random Seed" row, or is titled "State Audit Report"; `extract_seed` reads
  the seed. A workbook titled "<County> County Audit Report" (CORLA's
  per-county report, which carries the seed too) is a
  ``county_audit_report`` for that county.
- **JSON.** CORLA's JSON exports (a list of flat objects, one per row, as
  published for the 2025 instant-runoff audit) are read as tables whose
  header is the objects' keys, and matched like CSV.
- **County.** For files with a county column, the county is the single
  Colorado county named in it (spelled as in `COLORADO_COUNTIES`), and
  ``None`` for files covering several counties. "Colorado" (statewide
  rows), "Multi-county", worksheet footers ("End of worksheet") and
  footnotes are counted in the evidence; other values that are not county names (a typo)
  are listed in it.

A file that matches nothing is ``unknown``, with whatever evidence was found
(format, encoding, header cells, sheet names). It is never dropped: the
caller keeps it and reports it.

The .xlsx reader here is deliberately minimal, built on `zipfile` and
`xml.etree`: it lists sheet names and reads cell values as text (shared and
inline strings, numbers as written). It refuses XML with a document type
declaration, the vehicle for entity-expansion attacks, and caps how much of
each part it will decompress.

Standard library only.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET

from corla_observer.events import JSONValue
from corla_observer.sampling import (
    BATCH_COLUMN_CANDIDATES,
    COUNT_COLUMN_CANDIDATES,
    LOCATION_COLUMN_CANDIDATES,
    TABULATOR_COLUMN_CANDIDATES,
    county_key,
)

#: Every kind `classify_bytes` can return.
KINDS = (
    "county_manifest",
    "contest_ids_by_county",
    "contests_list",
    "target_contests",
    "selection_list",
    "cvr_comparison",
    "manifest_cvr_comparison",
    "cvr",
    "pull_list",
    "upload_status",
    "file_hashes",
    "canonical_contest_list",
    "vote_totals",
    "state_audit_report",
    "county_audit_report",
    "discrepancy_report",
    "audit_activity_report",
    "seed",
    "html_page",
    "unknown",
)

#: Colorado's 64 counties, as the Secretary of State spells them.
COLORADO_COUNTIES = (
    "Adams", "Alamosa", "Arapahoe", "Archuleta", "Baca", "Bent", "Boulder", "Broomfield",
    "Chaffee", "Cheyenne", "Clear Creek", "Conejos", "Costilla", "Crowley", "Custer", "Delta",
    "Denver", "Dolores", "Douglas", "Eagle", "El Paso", "Elbert", "Fremont", "Garfield",
    "Gilpin", "Grand", "Gunnison", "Hinsdale", "Huerfano", "Jackson", "Jefferson", "Kiowa",
    "Kit Carson", "La Plata", "Lake", "Larimer", "Las Animas", "Lincoln", "Logan", "Mesa",
    "Mineral", "Moffat", "Montezuma", "Montrose", "Morgan", "Otero", "Ouray", "Park",
    "Phillips", "Pitkin", "Prowers", "Pueblo", "Rio Blanco", "Rio Grande", "Routt", "Saguache",
    "San Juan", "San Miguel", "Sedgwick", "Summit", "Teller", "Washington", "Weld", "Yuma",
)  # fmt: skip

_COUNTY_BY_KEY = {county_key(name): name for name in COLORADO_COUNTIES}

#: Demonstration counties: a real county's public files from an earlier
#: election, under a name of their own ("Delta (2020 demo)"), kept apart from
#: the county's current files as every test county is. Each with what to know
#: about its file. Every one passed the redaction check with nothing to redact.
DEMO_COUNTIES: dict[str, str] = {
    "Adams (2020 demo)": "Adams County's published 2020 general election CVR. This file differs slightly from the one CORLA audited: its hash does not reproduce the hash CORLA recorded.",
    "Broomfield (2020 demo)": "Broomfield's published 2020 general election CVR. This file differs slightly from the one CORLA audited: its hash does not reproduce the hash CORLA recorded.",
    "Delta (2020 demo)": "Delta County's published 2020 general election CVR, whose hash reproduces the hash CORLA recorded for the file it audited.",
}

#: Test counties, for practising observation and trying the pages: the
#: fictional Testland, with synthetic data (`corla_observer.testcounty`),
#: and the demonstration counties (`DEMO_COUNTIES`). A test county is never a
#: Colorado county: content detection (`canonical_county`) never names one,
#: so its files are uploaded with the county chosen explicitly; and its
#: uploads, extracts and observers are kept out of the data map (manifests,
#: statewide sampling domains, problems and flags), the prediction, the
#: scheduler's interim extracts, the evidence commitments and the public
#: dashboard. Only `known_county` accepts one: where a coordinator names the
#: county of an upload, an audit board count, an extract or a link.
TEST_COUNTIES = ("Testland", *DEMO_COUNTIES)
_TEST_BY_KEY = {county_key(name): name for name in TEST_COUNTIES}

#: County-column values that mean "the whole state" rather than a county.
_STATEWIDE_VALUES = frozenset({"colorado", "statewide", "state", "stateofcolorado"})
#: County-column values that mean "more than one county".
_MULTI_COUNTY_VALUES = frozenset({"multicounty", "multiplecounties"})
#: County-column values that end a worksheet in CORLA's and the state's
#: reports ("End of worksheet", "End of Sheet"): compared by `county_key` prefix.
_FOOTER_PREFIX = "endof"
#: A county-column value longer than this is a footnote (the target contest
#: lists end with one explaining the "# of CVRs" column), not a county name.
_FOOTNOTE_LENGTH = 64

#: Rows searched for a header row, from the top of a CSV or sheet.
HEADER_SEARCH_ROWS = 10
#: Workbook sheets searched for a header row (besides "Summary").
SHEETS_SEARCHED = 5
#: Data rows read to find a file's county.
COUNTY_SCAN_ROWS = 2_000_000
#: Largest uncompressed workbook part (shared strings, one sheet) read.
MAX_XLSX_PART_BYTES = 128 * 1024 * 1024

#: Longest string kept in evidence, and most items in an evidence list.
_EVIDENCE_TEXT = 200
_EVIDENCE_ITEMS = 40

_SEED_RE = re.compile(r"^\d{1,64}$")


def normalize_header(name: str) -> str:
    """A header cell reduced for comparison: Excel ``="..."`` removed, lowercase, letters and digits only."""
    text = name.strip()
    if text.startswith('="') and text.endswith('"'):
        text = text[2:-1]
    return re.sub(r"[^a-z0-9]", "", text.lower())


def canonical_county(value: str) -> str | None:
    """The county's standard spelling if ``value`` names a Colorado county, else None.

    Case, spaces and a trailing "County" are ignored: "BACA", "ClearCreek",
    "Conejos " and "El Paso County" are recognized.
    """
    key = county_key(_strip_excel(value).strip())
    if key.endswith("county") and key[:-6] in _COUNTY_BY_KEY:
        key = key[:-6]
    return _COUNTY_BY_KEY.get(key)


def test_county(value: str) -> str | None:
    """The test county's spelling if ``value`` names one of `TEST_COUNTIES` ("testland", "Testland County"), else None."""
    key = county_key(_strip_excel(value).strip())
    if key.endswith("county") and key[:-6] in _TEST_BY_KEY:
        key = key[:-6]
    return _TEST_BY_KEY.get(key)


def is_test_county(value: str | None) -> bool:
    """Whether ``value`` names a test county (`TEST_COUNTIES`)."""
    return bool(value) and test_county(str(value)) is not None


def known_county(value: str) -> str | None:
    """`canonical_county`, or a test county's spelling: for a county a coordinator names explicitly."""
    return canonical_county(value) or test_county(value)


#: County keys, longest first, so that a longer name wins over a prefix of it.
_KEYS_LONGEST_FIRST = sorted(_COUNTY_BY_KEY.items(), key=lambda item: -len(item[0]))
#: What may follow a county's name in the name of a file about that county.
_NAME_SUFFIXES = ("ballotmanifest", "manifest", "ballotlist", "cvr", "auditreport", "redacted", "county")


def county_from_filename(name: str) -> str | None:
    """The county a file's *name* suggests, or None; the content is not looked at.

    Matches the names the audit center and counties have used: ``Adams.csv``,
    ``county_manifest_Adams.csv``, ``manifest-Adams.csv``,
    ``AdamsBallotManifest.csv``, ``LaPlata_BallotManifest.csv``,
    ``ClearCreek.csv``, ``Weld_CVR_Export_20260709.csv``. Used only to flag a
    file whose content names a different county from its name, as when one
    county's manifest was published under other counties' names.
    """
    stem = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = re.sub(r"^(county_manifest_|manifest[-_]|county_report_|ballot_list_)", "", stem, flags=re.IGNORECASE)
    key = re.sub(r"[^a-z]", "", stem.lower())
    for county_k, county in _KEYS_LONGEST_FIRST:
        if key == county_k or (key.startswith(county_k) and key[len(county_k) :].startswith(_NAME_SUFFIXES)):
            return county
    return None


def _strip_excel(value: str) -> str:
    text = value.strip()
    if text.startswith('="') and text.endswith('"'):
        return text[2:-1]
    return text


# ---------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class Classification:
    """What a file was recognized as, and why.

    `evidence` holds JSON values only (strings, integers, booleans, lists
    and objects of them), small enough to record in an event.
    """

    kind: str
    county: str | None = None
    evidence: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        return {"kind": self.kind, "county": self.county, "evidence": self.evidence}


def _short(text: str) -> str:
    return text if len(text) <= _EVIDENCE_TEXT else text[: _EVIDENCE_TEXT - 3] + "..."


def _short_list(items: Sequence[str]) -> list[JSONValue]:
    return [_short(item) for item in items[:_EVIDENCE_ITEMS]]


# ---------------------------------------------------------------------------
# Header signatures


@dataclass(frozen=True)
class Column:
    """A column role in a signature, with every accepted spelling of its header."""

    role: str
    spellings: tuple[str, ...]

    def normalized(self) -> tuple[str, ...]:
        return tuple(normalize_header(s) for s in self.spellings)


@dataclass(frozen=True)
class Signature:
    """The columns that identify one kind of table.

    Every `required` column must be present; `optional` columns are
    reported when present. `county_role` names the column holding the
    county, if any.
    """

    kind: str
    required: tuple[Column, ...]
    optional: tuple[Column, ...] = ()
    county_role: str | None = None


_COUNTY = Column("county", ("county name", "county"))
_CONTEST = Column("contest", ("contest name", "contest"))
_IMPRINTED = Column("imprinted_id", ("imprinted id", "imprinted ballot id"))
_AUDIT_BOARD = Column(
    "audit_board",
    ("audit board", "audit board number", "audit board #", "audit board name", "board", "board number"),
)
_PULL_TABULATOR = Column(
    "tabulator", ("scanner id", "tabulator id", "tabulator num", "tabulator", "scanner", "device id")
)
_PULL_BATCH = Column("batch", ("batch id", "batch", "batch number", "batch #"))
_PULL_RECORD = Column("record", ("record id", "ballot position", "record", "position", "record number"))
_PULL_OPTIONAL = (
    _COUNTY,
    _PULL_TABULATOR,
    _PULL_BATCH,
    _PULL_RECORD,
    Column("ballot_type", ("ballot type",)),
    Column("cvr_number", ("cvr number",)),
    Column("audited", ("audited",)),
    Column("location", ("storage location", "location")),
)

#: The signatures, tried in order; the first whose required columns are all
#: present wins. More specific signatures come first: a discrepancy report
#: carries every column of a CVR comparison and more.
SIGNATURES: tuple[Signature, ...] = (
    Signature(
        "discrepancy_report",
        required=(_IMPRINTED, Column("reason", ("discrepancy reason", "discrepancy type"))),
        optional=(_COUNTY, _CONTEST),
        county_role="county",
    ),
    Signature(
        "audit_activity_report",
        required=(
            _IMPRINTED,
            Column("record_type", ("record type",)),
            Column("audit_board", ("audit board",)),
            Column("discrepancy", ("discrepancy",)),
        ),
        optional=(_COUNTY, Column("random_number", ("random number",))),
        county_role="county",
    ),
    Signature(
        "manifest_cvr_comparison",
        required=(
            Column("manifest_count", ("count per manifest", "manifest count", "ballots per manifest")),
            Column("cvr_count", ("count per cvr file", "count per cvr", "cvr count")),
        ),
        optional=(
            _COUNTY,
            Column("scanner", ("scanner id", "tabulator id", "tabulator")),
            Column("batch", ("batch id", "batch")),
            Column("difference", ("difference",)),
        ),
        county_role="county",
    ),
    Signature(
        "upload_status",
        required=(
            Column("filename", ("filename", "file name")),
            Column("computed_hash", ("computed hash",)),
            Column("status", ("status",)),
        ),
        optional=(
            Column("submitted_hash", ("submitted hash",)),
            Column("county", ("name", "county name", "county")),
            Column("type", ("type", "file type")),
        ),
        county_role="county",
    ),
    # The 2018 general election's upload status, before CORLA published hashes.
    Signature(
        "upload_status",
        required=(
            Column("filename", ("filename", "file name")),
            Column("hash_status", ("hash status",)),
            Column("status", ("status",)),
        ),
        optional=(Column("county", ("name", "county name", "county")), Column("approx_count", ("approx count",))),
        county_role="county",
    ),
    # CORLA's hashes of the CVR exports and manifests the counties uploaded
    # (2017 and 2018: m_cvr_hash.csv, m_manifest_hash.csv, cvr_hash.csv).
    Signature(
        "file_hashes",
        required=(
            Column("county", ("county name",)),
            Column("hash", ("cvr export hash", "ballot manifest hash")),
        ),
        county_role="county",
    ),
    Signature(
        "selection_list",
        required=(Column("cvr_ids", ("contest cvr ids", "cvr ids")), _CONTEST),
        optional=(Column("min_margin", ("min margin",)),),
    ),
    Signature(
        "cvr_comparison",
        required=(
            _IMPRINTED,
            Column("cvr_choice", ("choice per voting computer", "choice per cvr", "cvr choice")),
            Column("audit_board", ("audit board selection", "audit board choice")),
        ),
        optional=(_COUNTY, _CONTEST, Column("cvr_id", ("cvr id",)), Column("consensus", ("consensus",))),
        county_role="county",
    ),
    # A Dominion cast vote record: its column-name row is the fourth row.
    Signature(
        "cvr",
        required=(
            Column("cvr_number", ("cvr number",)),
            _IMPRINTED,
            Column("tabulator", ("tabulator num",)),
        ),
        optional=(
            Column("batch", ("batch id",)),
            Column("record", ("record id",)),
            Column("counting_group", ("counting group",)),
            Column("precinct", ("precinct portion",)),
            Column("ballot_type", ("ballot type",)),
        ),
    ),
    # CORLA's "List of ballots to audit", whose columns are not documented:
    # ballots by imprinted ID or by tabulator, batch and record, with the
    # audit board assignment; or CORLA's published ballot lists, with an
    # "audited" column and no board.
    Signature("pull_list", required=(_IMPRINTED, _AUDIT_BOARD), optional=_PULL_OPTIONAL, county_role="county"),
    Signature(
        "pull_list",
        required=(_PULL_TABULATOR, _PULL_BATCH, _PULL_RECORD, _AUDIT_BOARD),
        optional=(_COUNTY, _IMPRINTED) + _PULL_OPTIONAL[4:],
        county_role="county",
    ),
    Signature(
        "pull_list",
        required=(_IMPRINTED, Column("audited", ("audited",))),
        optional=_PULL_OPTIONAL[:-2] + _PULL_OPTIONAL[-1:],
        county_role="county",
    ),
    Signature(
        "contest_ids_by_county",
        required=(
            Column("county_id", ("county id",)),
            _COUNTY,
            _CONTEST,
            Column("contest_id", ("contest id",)),
        ),
        county_role="county",
    ),
    Signature(
        "contests_list",
        required=(
            Column("contest", ("contest name",)),
            Column("audit_reason", ("audit reason",)),
            Column(
                "sample_count",
                ("audited sample count", "estimated samples to audit", "optimistic samples to audit"),
            ),
        ),
        optional=(
            Column("ballot_card_count", ("ballot card count",)),
            Column("risk_limit", ("risk limit",)),
            Column("min_margin", ("min margin",)),
        ),
    ),
    # The canonical list of each county's contests and choices, published
    # before the audit in some elections and in finalReports/ in others.
    Signature(
        "canonical_contest_list",
        required=(
            Column("county", ("county name",)),
            Column("contest", ("contest name",)),
            Column("choices", ("contest choices",)),
        ),
        county_role="county",
    ),
    # CORLA's vote tabulations (tabulate.csv, tabulateCounty.csv,
    # CandidateVoteTotalsByCounty.csv and the like).
    Signature(
        "vote_totals",
        required=(_CONTEST, Column("choice", ("choice",)), Column("votes", ("votes",))),
        optional=(_COUNTY,),
        county_role="county",
    ),
    Signature(
        "target_contests",
        required=(
            _CONTEST,
            Column("diluted_margin", ("diluted margin",)),
            Column("risk_limit", ("risk limit",)),
        ),
        optional=(
            Column("county", ("county",)),
            Column("estimate", ("estimated # of cvrs to audit", "estimated samples to audit")),
        ),
        county_role="county",
    ),
    Signature(
        "county_manifest",
        required=(
            Column("tabulator", tuple(TABULATOR_COLUMN_CANDIDATES)),
            Column("batch", tuple(BATCH_COLUMN_CANDIDATES)),
            Column("count", tuple(COUNT_COLUMN_CANDIDATES)),
        ),
        optional=(Column("county", ("county",)), Column("location", tuple(LOCATION_COLUMN_CANDIDATES))),
        county_role="county",
    ),
    Signature("seed", required=(Column("seed", ("seed", "random seed", "audit random seed")),)),
)


def match_signature(header: Sequence[str], signature: Signature) -> dict[str, tuple[int, str]] | None:
    """Match a header row against a signature.

    Returns ``{role: (column index, header text)}`` for every required
    column and every optional column present, or None if a required column
    is missing. Each header column fills at most one role, and for each role
    the earliest-listed spelling wins.
    """
    normalized = [normalize_header(cell) for cell in header]
    used: set[int] = set()
    found: dict[str, tuple[int, str]] = {}
    for required, columns in ((True, signature.required), (False, signature.optional)):
        for column in columns:
            index = _find(normalized, column.normalized(), used)
            if index is None:
                if required:
                    return None
                continue
            used.add(index)
            found[column.role] = (index, header[index].strip())
    return found


def _find(normalized: Sequence[str], spellings: Sequence[str], used: set[int]) -> int | None:
    for spelling in spellings:
        for index, cell in enumerate(normalized):
            if cell == spelling and index not in used:
                return index
    return None


@dataclass(frozen=True)
class _TableMatch:
    signature: Signature
    header_row: int  # 0-based
    header: list[str]
    columns: dict[str, tuple[int, str]]


def _match_table(rows: Sequence[Sequence[str]]) -> _TableMatch | None:
    """Find the first header row (within the first rows) matching any signature."""
    for row_index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        cells = [cell or "" for cell in row]
        if not any(cell.strip() for cell in cells):
            continue
        for signature in SIGNATURES:
            columns = match_signature(cells, signature)
            if columns is not None:
                return _TableMatch(signature, row_index, cells, columns)
    return None


def _table_evidence(match: _TableMatch) -> dict[str, JSONValue]:
    header = [cell.strip() for cell in match.header]
    while header and not header[-1]:
        header.pop()
    return {
        "header_row": match.header_row + 1,
        "matched_columns": {role: _short(text) for role, (_, text) in match.columns.items()},
        "columns": _short_list(header),
    }


def _county_from_rows(
    match: _TableMatch, data_rows: Iterator[Sequence[str]]
) -> tuple[str | None, dict[str, JSONValue]]:
    """Find the single county a table's county column names, and evidence about it."""
    signature = match.signature
    if signature.county_role is None:
        return None, {}
    index: int | None = None
    header_county: str | None = None
    if signature.county_role in match.columns:
        index = match.columns[signature.county_role][0]
    elif signature.kind == "county_manifest" and match.header:
        # As in sampling.load_county_manifest: with no "County" header, the
        # first column is the county, unless another role claimed it. Some
        # counties put their own name in that header cell.
        claimed = {i for i, _ in match.columns.values()}
        if 0 not in claimed:
            index = 0
            header_county = canonical_county(match.header[0])
    if index is None:
        return None, {}
    recognized: dict[str, int] = {}
    other: dict[str, int] = {}
    statewide = multi_county = footer = 0
    rows = 0
    for row in data_rows:
        rows += 1
        if rows > COUNTY_SCAN_ROWS:
            break
        value = _strip_excel(row[index] if index < len(row) and row[index] is not None else "")
        if not value:
            continue
        canonical = canonical_county(value)
        key = county_key(value)
        if canonical is not None:
            recognized[canonical] = recognized.get(canonical, 0) + 1
        elif key in _STATEWIDE_VALUES:
            statewide += 1
        elif key.replace("-", "") in _MULTI_COUNTY_VALUES:
            multi_county += 1
        elif (key.startswith(_FOOTER_PREFIX) and len(key) < 24) or len(value) > _FOOTNOTE_LENGTH:
            footer += 1
        else:
            other[value] = other.get(value, 0) + 1
    if header_county is not None:
        recognized[header_county] = recognized.get(header_county, 0) + 1
    evidence: dict[str, JSONValue] = {"data_rows": rows}
    if len(recognized) > 1:
        evidence["county_count"] = len(recognized)
        evidence["counties"] = _short_list(sorted(recognized))
    if other:
        evidence["unrecognized_county_values"] = _short_list(sorted(other))
    if statewide:
        evidence["statewide_rows"] = statewide
    if multi_county:
        evidence["multi_county_rows"] = multi_county
    if footer:
        evidence["footer_rows"] = footer
    if header_county is not None:
        evidence["county_from_header_cell"] = header_county
    if len(recognized) == 1:
        return next(iter(recognized)), evidence
    if not recognized and len(other) == 1:
        value = next(iter(other))
        # Passed through as written only if it is plausible as a name.
        if len(value) <= 64 and value.isprintable():
            return value, evidence
    return None, evidence


def _classify_table(
    rows: Sequence[Sequence[str]], more_rows: Iterator[Sequence[str]], base: dict[str, JSONValue]
) -> Classification | None:
    """Classify a table given its first rows and an iterator over the rest."""
    match = _match_table(rows)
    if match is None:
        return None
    evidence = dict(base)
    evidence.update(_table_evidence(match))
    kind = match.signature.kind
    if kind == "seed":
        index = match.columns["seed"][0]
        values = [
            _strip_excel(row[index] or "")
            for row in rows[match.header_row + 1 :]
            if index < len(row) and (row[index] or "").strip()
        ]
        if not values or not _SEED_RE.match(values[0]):
            return None
        evidence["seed"] = values[0]
        return Classification("seed", None, evidence)

    def data_rows() -> Iterator[Sequence[str]]:
        yield from rows[match.header_row + 1 :]
        yield from more_rows

    county, county_evidence = _county_from_rows(match, data_rows())
    evidence.update(county_evidence)
    return Classification(kind, county, evidence)


# ---------------------------------------------------------------------------
# Text and CSV


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode text: UTF-8 (BOM optional), UTF-16 with a BOM, else Latin-1. Returns ``(text, encoding)``."""
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            return data[len(codecs.BOM_UTF8) :].decode("latin-1"), "latin-1"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


_DELIMITERS = (",", "\t", ";", "|")


def _sniff_delimiter(sample: str) -> str | None:
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        return None


def _csv_rows(text: str, delimiter: str) -> Iterator[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        yield from reader
    except csv.Error:
        return


def _classify_csv(text: str, encoding: str) -> Classification:
    sample = text[:65536]
    sniffed = _sniff_delimiter(sample)
    candidates = [sniffed] if sniffed else []
    candidates += [d for d in _DELIMITERS if d != sniffed]
    base: dict[str, JSONValue] = {"format": "csv", "encoding": encoding}
    for delimiter in candidates:
        rows_iter = _csv_rows(text, delimiter)
        head = [row for _, row in zip(range(HEADER_SEARCH_ROWS), rows_iter, strict=False)]
        found = _classify_table(head, rows_iter, base | {"delimiter": delimiter})
        if found is not None:
            return found
    head = [row for _, row in zip(range(1), _csv_rows(text, sniffed or ","), strict=False)]
    evidence = dict(base)
    if head:
        evidence["first_row"] = _short_list([cell.strip() for cell in head[0]])
    return Classification("unknown", None, evidence)


_HTML_RE = re.compile(r"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html|head|body)\b", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)


def looks_like_html(text: str) -> bool:
    """Whether text starts like an HTML document."""
    return bool(_HTML_RE.match(text[:4096])) or "<html" in text[:1024].lower()


def _classify_html(text: str, encoding: str) -> Classification:
    evidence: dict[str, JSONValue] = {"format": "html", "encoding": encoding}
    title = _TITLE_RE.search(text[:65536])
    if title:
        evidence["title"] = _short(" ".join(title.group(1).split()))
    # Cloudflare's interstitial challenge page, served instead of the real
    # page, sets _cf_chl_opt and is titled "Just a moment...".
    lowered = text[:200_000].lower()
    if "_cf_chl_opt" in lowered or (title and "just a moment" in title.group(1).lower()):
        evidence["cloudflare_challenge"] = True
    return Classification("html_page", None, evidence)


# ---------------------------------------------------------------------------
# Minimal .xlsx reader


class XlsxError(ValueError):
    """The bytes are not a workbook this reader can read."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _relationship_id(element: ET.Element) -> str | None:
    for name, value in element.attrib.items():
        if _local(name) == "id" and "relationships" in name:
            return value
    return None


def _column_index(ref: str) -> int | None:
    letters = re.match(r"[A-Z]+", ref.upper())
    if not letters:
        return None
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - 64)
    return index - 1


class XlsxReader:
    """Read sheet names and cell text from an .xlsx workbook, with the standard library only."""

    def __init__(self, data: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            names = set(self._zip.namelist())
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise XlsxError(f"not a zip file: {exc}") from None
        if "xl/workbook.xml" not in names:
            raise XlsxError("no xl/workbook.xml: not an Excel workbook")
        self._names = names
        self._shared: list[str] | None = None
        self.sheets: list[tuple[str, str]] = self._read_sheets()

    def _open_part(self, name: str) -> IO[bytes]:
        try:
            info = self._zip.getinfo(name)
        except KeyError:
            raise XlsxError(f"missing part {name}") from None
        if info.file_size > MAX_XLSX_PART_BYTES:
            raise XlsxError(f"part {name} is larger than {MAX_XLSX_PART_BYTES} bytes")
        with self._zip.open(info) as f:
            prolog = f.read(4096)
        if b"<!doctype" in prolog.lower():
            raise XlsxError(f"part {name} has a document type declaration; refused")
        return self._zip.open(info)

    def _parse(self, name: str) -> ET.Element:
        with self._open_part(name) as f:
            try:
                return ET.parse(f).getroot()
            except ET.ParseError as exc:
                raise XlsxError(f"part {name} is not well-formed XML: {exc}") from None

    def _read_sheets(self) -> list[tuple[str, str]]:
        workbook = self._parse("xl/workbook.xml")
        targets: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in self._names:
            for rel in self._parse("xl/_rels/workbook.xml.rels"):
                rel_id, target = rel.get("Id"), rel.get("Target")
                if rel_id and target:
                    if target.startswith("/"):
                        path = target.lstrip("/")
                    else:
                        path = posixpath.normpath(posixpath.join("xl", target))
                    targets[rel_id] = path
        sheets = []
        position = 0
        for element in workbook.iter():
            if _local(element.tag) != "sheet":
                continue
            position += 1
            rel_id = _relationship_id(element)
            path = targets.get(rel_id or "", f"xl/worksheets/sheet{position}.xml")
            sheets.append((element.get("name") or f"Sheet{position}", path))
        return sheets

    @property
    def sheet_names(self) -> list[str]:
        return [name for name, _ in self.sheets]

    def _shared_strings(self) -> list[str]:
        if self._shared is None:
            self._shared = []
            if "xl/sharedStrings.xml" in self._names:
                for si in self._parse("xl/sharedStrings.xml"):
                    if _local(si.tag) == "si":
                        self._shared.append(_string_item_text(si))
        return self._shared

    def rows(self, sheet: str, max_rows: int | None = None) -> Iterator[list[str]]:
        """Yield a sheet's rows as lists of cell text ("" for empty cells), top to bottom.

        Rows missing from the sheet's XML (entirely empty rows) are not
        yielded. Numbers are given as written in the file (``"0.03"``,
        ``"1444047.0"``).
        """
        path = dict(self.sheets).get(sheet)
        if path is None:
            raise XlsxError(f"no sheet named {sheet!r}")
        shared = self._shared_strings()
        count = 0
        with self._open_part(path) as f:
            try:
                for _, element in ET.iterparse(f, events=("end",)):
                    if _local(element.tag) != "row":
                        continue
                    yield _row_values(element, shared)
                    element.clear()
                    count += 1
                    if max_rows is not None and count >= max_rows:
                        return
            except ET.ParseError as exc:
                raise XlsxError(f"sheet {sheet!r} is not well-formed XML: {exc}") from None


def _string_item_text(si: ET.Element) -> str:
    """The text of a shared string item: its own <t>, or its rich-text runs' (not phonetic hints)."""
    parts = []
    for child in si:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _row_values(row: ET.Element, shared: Sequence[str]) -> list[str]:
    values: dict[int, str] = {}
    next_index = 0
    for cell in row:
        if _local(cell.tag) != "c":
            continue
        index = _column_index(cell.get("r", "")) if cell.get("r") else None
        if index is None:
            index = next_index
        next_index = index + 1
        values[index] = _cell_text(cell, shared)
    if not values:
        return []
    width = max(values) + 1
    return [values.get(i, "") for i in range(width)]


def _cell_text(cell: ET.Element, shared: Sequence[str]) -> str:
    kind = cell.get("t")
    value = next((child.text or "" for child in cell if _local(child.tag) == "v"), None)
    if kind == "inlineStr":
        for child in cell:
            if _local(child.tag) == "is":
                return _string_item_text(child)
        return ""
    if value is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def _find_sheet(reader: XlsxReader, name: str) -> str | None:
    wanted = normalize_header(name)
    for sheet in reader.sheet_names:
        if normalize_header(sheet) == wanted:
            return sheet
    return None


#: Rows of the Summary sheet searched for the seed.
_SEED_SEARCH_ROWS = 50

#: The title of a county's own audit report workbook ("La Plata County Audit Report").
_COUNTY_REPORT_RE = re.compile(r"^\s*(.+?)\s+County\s+Audit\s+Report\s*$", re.IGNORECASE)


def _seed_from_reader(reader: XlsxReader) -> tuple[str | None, bool, str | None]:
    """Return ``(seed, label_found, summary sheet name)`` from a workbook's Summary sheet."""
    summary = _find_sheet(reader, "Summary")
    if summary is None:
        return None, False, None
    for row in reader.rows(summary, _SEED_SEARCH_ROWS):
        for index, cell in enumerate(row):
            if normalize_header(cell) != "auditrandomseed":
                continue
            value = next((v.strip() for v in row[index + 1 :] if v.strip()), "")
            value = _strip_excel(value)
            return (value if _SEED_RE.match(value) else None), True, summary
    return None, False, summary


def extract_seed(xlsx_bytes: bytes) -> str | None:
    """The "Audit Random Seed" from a State Audit Report workbook's "Summary" sheet.

    The seed is the first non-empty cell to the right of the "Audit Random
    Seed" label, returned exactly as written if it is all digits. A seed
    stored as a floating-point number (which can't hold 20 digits exactly)
    or anything else is not a seed, and None is returned, as it is when
    there is no such label or the bytes are not a readable workbook.
    """
    try:
        seed, _, _ = _seed_from_reader(XlsxReader(xlsx_bytes))
    except XlsxError:
        return None
    return seed


def _classify_xlsx(data: bytes) -> Classification:
    try:
        reader = XlsxReader(data)
    except XlsxError as exc:
        return Classification("unknown", None, {"format": "xlsx", "error": _short(str(exc))})
    names = reader.sheet_names
    base: dict[str, JSONValue] = {
        "format": "xlsx",
        "sheet_count": len(names),
        "sheets": _short_list(names),
    }
    try:
        seed, labelled, summary = _seed_from_reader(reader)
        title = ""
        if summary is not None:
            first = next(reader.rows(summary, 1), [])
            title = first[0].strip() if first else ""
        county_report = _COUNTY_REPORT_RE.match(title)
        report_county = canonical_county(county_report.group(1)) if county_report else None
        if labelled or normalize_header(title) == "stateauditreport" or report_county:
            evidence = dict(base)
            evidence["sheet"] = summary
            if title:
                evidence["title"] = _short(title)
            if labelled:
                evidence["matched_label"] = "Audit Random Seed"
            evidence["audit_random_seed"] = seed
            if report_county is not None:
                return Classification("county_audit_report", report_county, evidence)
            return Classification("state_audit_report", None, evidence)
        searched = names[:SHEETS_SEARCHED]
        for sheet in searched:
            rows = reader.rows(sheet)
            head = [row for _, row in zip(range(HEADER_SEARCH_ROWS), rows, strict=False)]
            found = _classify_table(head, rows, base | {"sheet": _short(sheet)})
            if found is not None:
                kind, county, evidence = found.kind, found.county, dict(found.evidence)
                if kind == "cvr_comparison" and any(
                    "discrepanc" in " ".join(row).lower() for row in head[:2]
                ):
                    kind = "discrepancy_report"
                if len(names) > 1 and county is not None:
                    # One sheet of a many-sheet workbook (CORLA's
                    # per-contest reports) speaks only for itself.
                    evidence["matched_sheet_county"] = county
                    county = None
                return Classification(kind, county, evidence)
    except XlsxError as exc:
        return Classification("unknown", None, base | {"error": _short(str(exc))})
    return Classification("unknown", None, base)


# ---------------------------------------------------------------------------
# Entry points

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2 (legacy Office)"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _classify_zip(data: bytes) -> Classification:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            mimetype = ""
            if "mimetype" in names and z.getinfo("mimetype").file_size < 256:
                mimetype = z.read("mimetype").decode("ascii", "replace").strip()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return Classification("unknown", None, {"format": "zip", "error": _short(str(exc))})
    if "xl/workbook.xml" in names:
        return _classify_xlsx(data)
    if "word/document.xml" in names:
        return Classification("unknown", None, {"format": "docx"})
    if mimetype.startswith("application/vnd.oasis.opendocument"):
        return Classification("unknown", None, {"format": "opendocument", "mimetype": _short(mimetype)})
    return Classification("unknown", None, {"format": "zip", "entries": len(names)})


def classify_bytes(data: bytes) -> Classification:
    """Classify a file's content. Never raises for any input; unrecognized content is ``unknown``."""
    if not data:
        return Classification("unknown", None, {"format": "empty"})
    if data.startswith(b"PK\x03\x04"):
        return _classify_zip(data)
    for magic, name in _MAGIC:
        if data.startswith(magic):
            return Classification("unknown", None, {"format": name})
    text, encoding = decode_text(data)
    if "\x00" in text[:8192]:
        return Classification("unknown", None, {"format": "binary"})
    if looks_like_html(text):
        return _classify_html(text, encoding)
    if text.lstrip("﻿ \t\r\n")[:1] in ("[", "{"):
        found = _classify_json(text, encoding)
        if found is not None:
            return found
    return _classify_csv(text, encoding)


#: Largest JSON text parsed, in characters.
MAX_JSON_CHARS = 64 * 1024 * 1024


def _json_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _classify_json(text: str, encoding: str) -> Classification | None:
    """Classify CORLA's JSON exports (a list of flat objects, one per row) as tables.

    Returns None if the text is not JSON, so it is tried as CSV instead. JSON
    that is not a list of objects, or is larger than `MAX_JSON_CHARS`, is
    ``unknown`` with format ``json``.
    """
    base: dict[str, JSONValue] = {"format": "json", "encoding": encoding}
    if len(text) > MAX_JSON_CHARS:
        return Classification("unknown", None, base | {"error": "larger than the JSON size limit"})
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        shape = type(value).__name__
        return Classification("unknown", None, base | {"json_type": shape})
    header: list[str] = []
    for item in value:
        for key in item:
            if key not in header:
                header.append(str(key))
    rows = [header] + [[_json_cell(item.get(key)) for key in header] for item in value]
    found = _classify_table(rows[:HEADER_SEARCH_ROWS], iter(rows[HEADER_SEARCH_ROWS:]), base)
    if found is not None:
        return found
    return Classification("unknown", None, base | {"first_row": _short_list(header)})


def classify_file(path: Path | str) -> Classification:
    """Classify the file at ``path`` by its content (its name is not used)."""
    return classify_bytes(Path(path).read_bytes())
