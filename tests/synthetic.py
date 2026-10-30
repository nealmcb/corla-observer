"""Tiny synthetic audit center and county files, built in code for the fast tests.

`make_xlsx` writes a minimal but valid .xlsx workbook with the standard
library's zipfile, so the tests need no spreadsheet library and no real data.
`make_cvr` writes a small Dominion-style cast vote record.
"""

from __future__ import annotations

import codecs
import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORKSHEET_TYPE = REL_NS + "/worksheet"

#: A cell is a string (stored as a shared or inline string) or a raw
#: ``(type, value)`` pair written as ``<c t="type"><v>value</v></c>``, for
#: numbers (type "n") and the like.
Cell = str | tuple[str, str] | None


def _column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def make_xlsx(
    sheets: Mapping[str, Sequence[Sequence[Cell]]],
    *,
    inline_strings: bool = False,
    absolute_targets: bool = False,
    doctype_in_workbook: bool = False,
) -> bytes:
    """Build an .xlsx workbook holding ``sheets`` (name to rows of cells), in order."""
    shared: list[str] = []
    shared_index: dict[str, int] = {}
    sheet_xml: list[str] = []
    for rows in sheets.values():
        row_parts = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, cell in enumerate(row):
                ref = f"{_column_letters(c)}{r}"
                if cell is None:
                    continue
                if isinstance(cell, tuple):
                    kind, value = cell
                    cells.append(f'<c r="{ref}" t="{kind}"><v>{escape(value)}</v></c>')
                elif inline_strings:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(cell)}</t></is></c>')
                else:
                    if cell not in shared_index:
                        shared_index[cell] = len(shared)
                        shared.append(cell)
                    cells.append(f'<c r="{ref}" t="s"><v>{shared_index[cell]}</v></c>')
            row_parts.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheet_xml.append(
            f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN_NS}">'
            f"<sheetData>{''.join(row_parts)}</sheetData></worksheet>"
        )
    doctype = '<!DOCTYPE workbook [<!ENTITY a "aaaa">]>' if doctype_in_workbook else ""
    sheet_entries = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(sheets, start=1)
    )
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?>{doctype}'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><sheets>{sheet_entries}</sheets></workbook>'
    )
    prefix = "/xl/" if absolute_targets else ""
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{WORKSHEET_TYPE}" Target="{prefix}worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/workbook.xml", workbook)
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL_NS}">{rels}</Relationships>',
        )
        for i, xml in enumerate(sheet_xml, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
        if shared:
            items = "".join(f"<si><t>{escape(text)}</t></si>" for text in shared)
            z.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{MAIN_NS}">{items}</sst>',
            )
    return buffer.getvalue()


SEED = "49006417086137856424"


def state_audit_report(seed: Cell = SEED) -> bytes:
    """A State Audit Report workbook with the given "Audit Random Seed" cell."""
    return make_xlsx(
        {
            "Summary": [
                ["State Audit Report"],
                ["Generated 11/18/2026 04:02 PM"],
                ["General Election - 11/03/2026"],
                [],
                ["Audit Random Seed", seed],
                ["Audit Risk Limit", ("n", "0.03")],
            ],
            "Adams County": [["Adams County - Round Summary"]],
        }
    )


# ---------------------------------------------------------------------------
# Cast vote records

#: Contests (name to choices) of the synthetic CVRs, in column order.
CONTESTS: dict[str, tuple[str, ...]] = {
    "Mayor (Vote For=1)": ("Ann Able", "Bob Baker"),
    "Council (Vote For=2)": ("Cy Choi", "Di Dunn", "Ed Eng"),
    "Measure 1": ("Yes/For", "No/Against"),
    "Fire District": ("FD Yes", "FD No"),
}


@dataclass(frozen=True)
class Ballot:
    """One synthetic CVR row: the contests on the card, and how the row is written.

    Votes are 1 for each contest's first choice and 0 for the rest; their
    values never matter to the code under test, only which cells are blank.
    """

    contests: tuple[str, ...]
    precinct: str = "101"
    ballot_type: str = "BT-1"
    #: The CvrNumber cell; by default the row's position.
    cvr_number: str | None = None
    #: If given, written in place of the row's first non-blank vote cell.
    marker: str | None = None
    #: Write every vote cell blank (a county's redaction).
    blank: bool = False


def ballots(count: int, *contests: str, precinct: str = "101", ballot_type: str = "BT-1") -> list[Ballot]:
    """``count`` identical ballots with the given contests (prefixes of the contest names suffice)."""
    names = tuple(next(name for name in CONTESTS if name.startswith(prefix)) for prefix in contests)
    return [Ballot(names, precinct, ballot_type) for _ in range(count)]


def make_cvr(
    rows: Sequence[Ballot],
    *,
    precinct_column: bool = True,
    counting_group: bool = True,
    excel: bool = False,
    quote_all: bool = False,
    line_ending: str = "\r\n",
    encoding: str = "utf-8",
    bom: bool = False,
    title: str = "2026 Synthetic County General Election",
) -> bytes:
    """A Dominion-style CVR export of ``rows``, with the counties' optional quirks.

    ``excel`` wraps the identification cells as ``="..."``; ``quote_all``
    quotes every cell, as some exports do; ``precinct_column`` and
    ``counting_group`` include those columns.
    """
    meta = ["CvrNumber", "TabulatorNum", "BatchId", "RecordId", "ImprintedId"]
    if counting_group:
        meta.append("CountingGroup")
    if precinct_column:
        meta.append("PrecinctPortion")
    meta.append("BallotType")
    vote_columns = [(contest, choice) for contest, choices in CONTESTS.items() for choice in choices]
    pad = [""] * len(meta)
    lines = [
        [title, "5.17.17.1"] + [""] * (len(meta) + len(vote_columns) - 2),
        pad + [contest for contest, _ in vote_columns],
        pad + [choice for _, choice in vote_columns],
        meta + [""] * len(vote_columns),
    ]
    for number, ballot in enumerate(rows, start=1):
        record = str(number)
        ident = [ballot.cvr_number or record, "101", "1", record, f"101-1-{record}"]
        if excel:
            ident = [f'="{cell}"' if cell else cell for cell in ident]
        row = ident
        if counting_group:
            row.append("Mail")
        if precinct_column:
            row.append(ballot.precinct)
        row.append(ballot.ballot_type)
        votes: list[str] = []
        for contest, choices in CONTESTS.items():
            on_card = contest in ballot.contests and not ballot.blank
            votes += [("1" if i == 0 else "0") if on_card else "" for i in range(len(choices))]
        if ballot.marker is not None:
            first = next((i for i, v in enumerate(votes) if v), 0)
            votes[first] = ballot.marker
        lines.append(row + votes)
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        lineterminator=line_ending,
        quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
    )
    writer.writerows(lines)
    data = buffer.getvalue().encode(encoding)
    return codecs.BOM_UTF8 + data if bom else data


# ---------------------------------------------------------------------------
# A small synthetic election, as the audit center would publish it

#: The election's directory, as in the mirror and the audit center's URLs.
ELECTION = "2026/general"
#: The audit center URL of the election's directory.
ELECTION_URL = "https://www.coloradosos.gov/pubs/elections/RLA/2026/general/"

ADAMS_MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nAdams,101,1,30,Box 1\r\n,101,2,20,Box 1\r\n"
BOULDER_MANIFEST = b"County,Tabulator ID,Batch,# of Ballot Cards\r\nBoulder,1,1,40\r\nBoulder,1,2,10\r\n"
WELD_MANIFEST = b"County,Device ID,Batch,# of Ballots\r\nWeld,7,1,25\r\nWeld,7,2,25\r\nWeld,7,3,15\r\n"

TARGETS = (
    b"County,Contest,Vote For,Lowest Winner,Highest Loser,Contest Margin,Diluted Margin,Risk Limit,"
    b"Estimated # of CVRs to audit,# of CVRs,Remarks\r\n"
    b"Colorado,Governor - Democratic Party,1,90,60,30,0.2,0.03,40,165,All counties\r\n"
    b"Adams,Adams County Clerk - Republican Party,1,30,10,20,0.4,0.03,20,50,\r\n"
)
CONTEST_IDS = (
    b"county_id,county_name,contest_name,contest_id\r\n"
    b"1,Adams,Governor - DEM,10\r\n1,Adams,Adams County Clerk - REP,11\r\n"
    b"7,Boulder,Governor - DEM,10\r\n62,Weld,Governor - DEM,10\r\n"
)


def contests_list(statewide_count: int = 9, adams_count: int = 4, statewide_cards: int = 165) -> bytes:
    """CORLA's round-one contest list for the synthetic election."""
    return (
        "contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,"
        "contest_ballot_card_count,winners,min_margin,risk_limit,audited_sample_count\r\n"
        f'Governor - DEM,state_wide_contest,in_progress,1,{statewide_cards},150,"[""A""]",30,0.03,{statewide_count}\r\n'
        f'Adams County Clerk - REP,county_wide_contest,in_progress,1,50,45,"[""B""]",20,0.03,{adams_count}\r\n'
        'Adams County Coroner - DEM,opportunistic_benefits,in_progress,1,50,45,"[""C""]",5,0.03,0\r\n'
    ).encode()


def election_files(**overrides: bytes | None) -> dict[str, bytes]:
    """The synthetic election's audit center files by path under the election directory.

    Keyword arguments replace (or, with None, remove) a file, by its path
    with ``/`` and ``.`` written as ``_`` (``files_Adams_csv``).
    """
    files = {
        "files/Adams.csv": ADAMS_MANIFEST,
        "files/Boulder.csv": BOULDER_MANIFEST,
        "files/Weld.csv": WELD_MANIFEST,
        "files/targetContests.csv": TARGETS,
        "riskLimit.html": b"<!DOCTYPE html><html><head><title>Risk limit</title></head><body>3%</body></html>",
    }
    for key, value in overrides.items():
        path = next((p for p in list(files) + _OPTIONAL if p.replace("/", "_").replace(".", "_") == key), None)
        if path is None:
            raise KeyError(key)
        if value is None:
            files.pop(path, None)
        else:
            files[path] = value
    return files


_OPTIONAL = [
    "finalReports/ContestIDsByCounty.csv",
    "finalReports/ContestsListRound1.csv",
    "finalReports/ContestMarginAndContestCVR-IDs-Round1.csv",
    "finalReports/CVRtoAuditBoardInterpretationComparison.csv",
    "files/uploadStatus.csv",
    "files/Saguache.csv",
]


def published_selections(selections: Mapping[str, Sequence[str]]) -> dict[str, bytes]:
    """CORLA's selection list and CVR comparison files publishing ``{contest: [imprinted IDs]}``.

    Each distinct imprinted ID gets a CVR ID; repeats are kept, in the order given.
    """
    ids: dict[str, int] = {}
    for imprinted_ids in selections.values():
        for imprinted in imprinted_ids:
            ids.setdefault(imprinted, len(ids) + 1)
    selection = io.StringIO()
    writer = csv.writer(selection, lineterminator="\r\n")
    writer.writerow(["min_margin", "contest_name", "contest_cvr_ids"])
    for contest, imprinted_ids in selections.items():
        writer.writerow([1, contest, "[" + ",".join(str(ids[i]) for i in imprinted_ids) + "]"])
    comparison = io.StringIO()
    writer = csv.writer(comparison, lineterminator="\r\n")
    writer.writerow(
        ["county_name", "contest_name", "imprinted_id", "ballot_type", "choice_per_voting_computer",
         "audit_board_selection", "consensus", "record_type", "audit_board_comment", "timestamp", "cvr_id"]
    )  # fmt: skip
    for imprinted, cvr_id in ids.items():
        writer.writerow(["Adams", "x", imprinted, "1", '"A"', '"A"', "YES", "uploaded", "", "2026-11-17", cvr_id])
    return {
        "finalReports/ContestMarginAndContestCVR-IDs-Round1.csv": selection.getvalue().encode(),
        "finalReports/CVRtoAuditBoardInterpretationComparison.csv": comparison.getvalue().encode(),
    }


# ---------------------------------------------------------------------------
# Election night results, as Clarity publishes them


def clarity_detail(
    contests: Mapping[str, Mapping[str, Mapping[str, int]]],
    ballots_cast: Mapping[str, int],
    *,
    vote_for: Mapping[str, int] | None = None,
    zipped: bool = True,
    timestamp: str = "11/4/2026 9:00:00 PM MST",
) -> bytes:
    """A Clarity ``detail.xml`` (zipped as ``detailxml.zip`` by default): ``{contest: {choice: {county: votes}}}``, generated at ``timestamp``."""
    counties = "".join(f'<County name="{escape(c)}" totalVoters="1" ballotsCast="{n}" />' for c, n in ballots_cast.items())
    parts = [
        f'<?xml version="1.0"?>\n<ElectionResult><Timestamp>{escape(timestamp)}</Timestamp>'
        "<ElectionName>2026 General</ElectionName>"
        f'<ElectionVoterTurnout totalVoters="1" ballotsCast="{sum(ballots_cast.values())}"><Counties>{counties}</Counties></ElectionVoterTurnout>'
    ]
    for key, (contest, choices) in enumerate(contests.items(), start=1):
        parts.append(f'<Contest key="{key}" text="{escape(contest)}" voteFor="{(vote_for or {}).get(contest, 1)}">')
        for ckey, (choice, by_county) in enumerate(choices.items(), start=1):
            total = sum(by_county.values())
            areas = "".join(f'<County name="{escape(c)}" votes="{v}" />' for c, v in by_county.items())
            parts.append(f'<Choice key="{ckey}" text="{escape(choice)}" totalVotes="{total}"><VoteType name="Total Votes" votes="{total}">{areas}</VoteType></Choice>')
        parts.append("</Contest>")
    parts.append("</ElectionResult>")
    xml = "".join(parts).encode()
    if not zipped:
        return xml
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("detail.xml", xml)
    return out.getvalue()


def clarity_summary(contests: Mapping[str, Mapping[str, int]], *, gzipped: bool = True) -> bytes:
    """A county's Clarity ``summary.json``: ``{contest (with "(Vote For 1)" or not): {choice: votes}}``."""
    import gzip
    import json

    items = [{"C": c, "VF": 1, "CH": list(v), "V": list(v.values())} for c, v in contests.items()]
    data = json.dumps(items).encode()
    return gzip.compress(data) if gzipped else data
